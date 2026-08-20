//! `anyrt serve` — the outer loop: ensure space/chat/anchor, resolve
//! overlays from the space (space-only, ADR-009 §5 — `anyrt deploy`
//! is the publish step; the kernel is embedded), then watch the chat
//! (SSE; the snapshot seeds the unanswered-message backlog, ADR-009
//! §8), tick triggers, and answer the localhost control API.
//! Conversations and trigger runs are guest programs through the
//! shared cage.

use crate::anyapi::Client;
use crate::broker::{Broker, SharedMailbox};
use crate::config::Config;
use crate::resolver::AnyModuleResolver;
use crate::routes::Classifier;
use crate::runner::{run_program, Cage};
use crate::trace::TraceWriter;
use crate::triggers::{
    record_to_trigger, rollup, standing_triggers, trigger_to_record, RunResult, Scheduler, Trigger,
    WatchAction, Watcher,
};
use anyhow::{Context, Result};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tracing::{error, info, warn};

fn now_s() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
}

/// Resolve the agent space (ADR-006 §0, amended 2026-08-19). Registry
/// names resolve through the server's derived-space registry (SYN-164)
/// and NEVER name-scan: a well-known space (`bao`) is a pure function
/// of the account keys, so every device converges on the same id —
/// materialized rows resolve, unmaterialized ones derive on the spot
/// (lazy + idempotent). No migration path: a legacy same-named space
/// is simply not the agent space anymore (clean cut, no-backcompat —
/// the registry route is required, servers without it are
/// unsupported). Non-registry names keep the v1 rule: name scan,
/// create on miss.
pub fn ensure_space(c: &Client, name: &str) -> Result<String> {
    let rows = c
        .list_derived_spaces()
        .map_err(|e| anyhow::anyhow!(e))
        .context("derived-space registry")?;
    if let Some(row) = rows.iter().find(|r| r["name"] == name) {
        if row["created"] == Value::Bool(true) {
            return Ok(row["spaceId"].as_str().unwrap_or_default().to_string());
        }
        let created = c.create_derived_space(name)?;
        return Ok(created["id"].as_str().unwrap_or_default().to_string());
    }
    for sp in c.list_spaces(None)? {
        if sp["name"] == name && sp.get("status").map(|s| s == "active").unwrap_or(true) {
            return Ok(sp["id"].as_str().unwrap_or_default().to_string());
        }
    }
    let created = c.create_space(name)?;
    Ok(created["id"].as_str().unwrap_or_default().to_string())
}

/// Strict lookup by name or id — `run --from-space` must not mint a
/// space on a typo; ensure_space's create is serve/deploy-only.
pub fn find_space(c: &Client, name_or_id: &str) -> Result<String> {
    for sp in c.list_spaces(None)? {
        if (sp["name"] == name_or_id || sp["id"] == name_or_id)
            && sp.get("status").map(|s| s == "active").unwrap_or(true)
        {
            return Ok(sp["id"].as_str().unwrap_or_default().to_string());
        }
    }
    anyhow::bail!("space not found: {name_or_id:?} (run --from-space never creates one)")
}

/// The space's general chat — the `general-chat/v1` bundle's winning
/// root (ADR-006 §0, amended 2026-08-20). The server keeps no catalog
/// and installs nothing on its own (SYN-163: chats are not
/// server-owned), so anybao ensures the bundle itself: adopt-or-install
/// is idempotent and every client that runs it lands on the same chat.
/// 409 `bundle.not_ready` (the winner's tree hasn't landed on this
/// device yet) is retried briefly. The bundles route is REQUIRED — a
/// server without it is unsupported (no-backcompat).
fn general_chat(c: &Client, space: &str) -> Result<String> {
    let mut last_err = None;
    for _ in 0..5 {
        match c.ensure_bundle(space, "general-chat/v1", "General", &["chat"]) {
            Ok(reply) => {
                return reply["bundle"]["rootId"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .map(str::to_string)
                    .context("general-chat bundle ensure returned no rootId");
            }
            Err(e) if e.status == 409 => {
                // bundle.not_ready — winner's tree still syncing in
                last_err = Some(e);
                std::thread::sleep(Duration::from_secs(2));
            }
            Err(e) => return Err(e).context("general-chat bundle ensure"),
        }
    }
    Err(last_err.unwrap()).context("general-chat bundle never became ready")
}

/// The host-written agent stores (ADR-017 §0/§1): anyrt registers the
/// `bao/v1` bundle and derives + declares ONLY what it writes before
/// guest code can run — config, secrets, triggers. Brain and chat
/// logs are guest-owned (any@v1 ensures them lazily).
pub struct AgentStores {
    pub config: String,
    pub secrets: String,
    pub triggers: String,
}

fn ensure_type(c: &Client, space: &str, name: &str, xkey: &str) -> Result<String> {
    for t in c.list_types(space)? {
        if t["xKey"] == xkey {
            return Ok(t["id"].as_str().unwrap_or_default().to_string());
        }
    }
    let created = c.create_type(space, &json!({"name": name, "xKey": xkey}))?;
    Ok(created["typeId"].as_str().unwrap_or_default().to_string())
}

fn ensure_dataset(c: &Client, space: &str, type_id: &str, draft: &Value) -> Result<()> {
    let name = draft["name"].as_str().unwrap_or_default();
    // no reconcile needed: the host stores declare no mutable
    // search.* leaves
    if c.list_datasets(space, type_id)?.iter().any(|d| d["name"] == name) {
        return Ok(());
    }
    c.create_dataset(space, type_id, draft)?;
    Ok(())
}

/// bundle_child with the same brief `bundle.not_ready` retry policy as
/// the chat ensure (winner's tree still syncing to this device).
fn bundle_child_retry(
    c: &Client,
    space: &str,
    bundle: &str,
    seed: &str,
    types: &[&str],
) -> Result<String> {
    let mut last_err = None;
    for _ in 0..5 {
        match c.bundle_child(space, bundle, seed, types) {
            Ok(reply) => {
                return reply["objectId"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .map(str::to_string)
                    .context("bundle child returned no objectId");
            }
            Err(e) if e.status == 409 => {
                last_err = Some(e);
                std::thread::sleep(Duration::from_secs(2));
            }
            Err(e) => return Err(e).context(format!("bundle child {seed}")),
        }
    }
    Err(last_err.unwrap()).context(format!("bundle child {seed} never became ready"))
}

/// Every field the trigger registry reads or writes — the datasets are
/// non-dynamic, an undeclared field rejects the write.
const TRIGGER_FIELDS: &[&str] = &[
    "name",
    "kind",
    "spec",
    "program",
    "args",
    "owner",
    "enabled",
    "limits",
    "maxConsecutiveFailures",
    "lastRunAt",
    "lastStatus",
    "lastDurationMs",
    "lastFuel",
    "lastCostUsd",
    "runCount",
    "consecutiveFailures",
    "lastRunRef",
];
const TRIGGER_RUN_FIELDS: &[&str] = &[
    "triggerId",
    "ts",
    "status",
    "durationMs",
    "error",
    "traceRef",
    "fuel",
    "costUsd",
];

fn mutable_fields(keys: &[&str]) -> Vec<Value> {
    keys.iter()
        .map(|k| json!({"key": k, "mutableBy": "any"}))
        .collect()
}

pub fn provision_agent_stores(c: &Client, space: &str) -> Result<AgentStores> {
    // The bundle: adopt-or-install, brief not_ready retry (same class
    // as the chat ensure).
    let mut last_err = None;
    let mut registered = false;
    for _ in 0..5 {
        match c.ensure_bundle(space, "bao/v1", "bao", &["page"]) {
            Ok(_) => {
                registered = true;
                break;
            }
            Err(e) if e.status == 409 => {
                last_err = Some(e);
                std::thread::sleep(Duration::from_secs(2));
            }
            Err(e) => return Err(e).context("bao/v1 bundle ensure"),
        }
    }
    if !registered {
        return Err(last_err.unwrap()).context("bao/v1 bundle never became ready");
    }

    let cfg_t = ensure_type(c, space, "Agent Config", "agent_config")?;
    let sec_t = ensure_type(c, space, "Agent Secrets", "agent_secrets")?;
    let trg_t = ensure_type(c, space, "Agent Trigger", "agent_trigger")?;
    ensure_dataset(
        c,
        space,
        &cfg_t,
        &json!({
            "name": CONFIG_DATASET, "displayName": "Agent Config",
            "idRule": "user", "deleteBy": "anyone", "skipHistory": true,
            "fields": [
                {"key": "key", "kind": "string", "mutableBy": "any"},
                {"key": "value", "mutableBy": "any"},
                {"key": "secret", "kind": "boolean", "mutableBy": "any"},
                {"key": "localValue", "scope": "local", "mutableBy": "any"},
            ]}),
    )?;
    ensure_dataset(
        c,
        space,
        &sec_t,
        &json!({
            "name": SECRETS_DATASET, "displayName": "Agent Secrets",
            "idRule": "user", "deleteBy": "anyone", "skipHistory": true,
            "fields": [
                {"key": "key", "kind": "string", "mutableBy": "any"},
                {"key": "secret", "kind": "boolean", "mutableBy": "any"},
                {"key": SECRETS_FIELD, "scope": "local", "mutableBy": "any"},
            ]}),
    )?;
    ensure_dataset(
        c,
        space,
        &trg_t,
        &json!({
            "name": "agent_triggers", "displayName": "Agent Triggers",
            "idRule": "user", "deleteBy": "anyone", "skipHistory": true,
            "fields": mutable_fields(TRIGGER_FIELDS)}),
    )?;
    ensure_dataset(
        c,
        space,
        &trg_t,
        &json!({
            "name": "agent_trigger_runs", "displayName": "Agent Trigger Runs",
            "idRule": "user", "deleteBy": "anyone", "skipHistory": true,
            "fields": mutable_fields(TRIGGER_RUN_FIELDS)}),
    )?;
    Ok(AgentStores {
        config: bundle_child_retry(c, space, "bao/v1", "bao/config/v1", &[&cfg_t])?,
        secrets: bundle_child_retry(c, space, "bao/v1", "bao/secrets/v1", &[&sec_t])?,
        triggers: bundle_child_retry(c, space, "bao/v1", "bao/triggers/v1", &[&trg_t])?,
    })
}

/// The Anthropic API key's config key — the record id/`key` on the config
/// object AND the `secrets` map ref the llm effect resolves (config
/// defaults declare `api_key_ref: "llm.key.anthropic"`).
const ANTHROPIC_SECRET_REF: &str = "llm.key.anthropic";

/// The `agent_config` dataset name (mirrors the server-side type).
const CONFIG_DATASET: &str = "agent_config";

/// The `agent_secrets` dataset + its local-scope value field (mirrors
/// the server-side `internal/agentsecrets` type): the dedicated home of
/// device-local secrets, split out of `agent_config` so the broker can
/// block guest reads of the whole dataset by name/object id while the
/// config object stays guest-readable.
const SECRETS_DATASET: &str = "agent_secrets";
const SECRETS_FIELD: &str = "value";

/// Space-scope config overrides read off the config object's
/// `agent_config` dataset: one record per dotted key, `{key, value}`.
/// These shadow the hardcoded `bootstrap` defaults (cascade:
/// space-override ?? default). Best-effort: a query hiccup or an
/// empty/fresh object yields no overrides rather than failing serve.
fn config_overrides(c: &Client, space: &str, obj: &str) -> Vec<(String, Value)> {
    let rows = match c.query(space, obj, CONFIG_DATASET, &json!({})) {
        Ok(rows) => rows,
        Err(e) => {
            warn!("config overrides unavailable ({e}); using defaults");
            return Vec::new();
        }
    };
    rows.iter()
        .filter_map(|r| {
            let k = r.get("key")?.as_str()?.to_string();
            Some((k, r.get("value")?.clone()))
        })
        .collect()
}

/// Device-local secret persistence (ADR-006 §3). Config secrets (the
/// provider API keys and any other ref) live as a never-synced
/// `localValue` on their config record, not in synced space data. On
/// serve start, three passes:
///
/// 1. HARD seeds (`Config::secret_overrides` — the `.connectors.env`
///    file or an embedder's in-memory feed; open ref set): persisted
///    write-through — a stored value that differs is ROTATED, an empty
///    override DELETES the stored secret. This is the rotation path;
///    env vars never rotate.
/// 2. Stored device-local values (every secret-marked record) load
///    into `secrets`; stored wins over embedder-fed soft seeds.
/// 3. Soft seeds (whatever the embedder put in `Config::secrets`, e.g.
///    bundled demo keys — env vars are NOT read, removed 2026-07-28):
///    persisted only when nothing is stored, so later starts need no
///    seed. No anthropic key anywhere → warn (serve still starts; the
///    llm effect fails on first use until one is imported); silent for
///    other refs.
///
/// Best-effort: a read/write hiccup never fails serve — it falls back to
/// whatever seeds/overrides supplied this run.
fn bootstrap_secrets(
    c: &Client,
    space: &str,
    obj: &str,
    dataset: &str,
    field: &str,
    secrets: &mut BTreeMap<String, String>,
    overrides: &BTreeMap<String, String>,
) {
    let rows = c.query(space, obj, dataset, &json!({})).unwrap_or_default();

    // 1. Hard seeds: write-through, open ref set, empty deletes.
    for (secret_ref, value) in overrides {
        let stored = stored_local_secret(&rows, secret_ref, field);
        if value.is_empty() {
            secrets.remove(secret_ref);
            if stored.is_some() {
                match persist_local_secret(c, space, obj, dataset, field, secret_ref, "") {
                    Ok(()) => info!("config: {secret_ref} removed from device-local store"),
                    Err(e) => warn!("config: could not remove {secret_ref} device-locally ({e})"),
                }
            }
            continue;
        }
        secrets.insert(secret_ref.clone(), value.clone());
        match stored {
            Some(ref s) if s == value => {
                info!("config: {secret_ref} loaded from device-local store")
            }
            other => match persist_local_secret(c, space, obj, dataset, field, secret_ref, value) {
                Ok(()) if other.is_some() => info!("config: {secret_ref} rotated (hard seed)"),
                Ok(()) => info!("config: {secret_ref} bootstrapped to device-local store"),
                Err(e) => warn!(
                    "config: could not persist {secret_ref} device-locally ({e}); \
                     using the seeded value this run"
                ),
            },
        }
    }

    // 2. Stored secrets — every secret-marked record, overrides win.
    for row in &rows {
        if row.get("secret").and_then(|v| v.as_bool()) != Some(true) {
            continue;
        }
        let Some(secret_ref) = row.get("key").and_then(|v| v.as_str()) else {
            continue;
        };
        if overrides.contains_key(secret_ref) {
            continue;
        }
        let stored = row
            .get(field)
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty());
        if let Some(key) = stored {
            secrets.insert(secret_ref.into(), key.into());
            info!("config: {secret_ref} loaded from device-local store");
        }
    }

    // 3. Soft seeds: persist-if-missing, every embedder-fed entry.
    let soft: Vec<(String, String)> = secrets
        .iter()
        .filter(|(k, v)| {
            !v.is_empty()
                && !overrides.contains_key(*k)
                && stored_local_secret(&rows, k, field).is_none()
        })
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    for (secret_ref, key) in soft {
        match persist_local_secret(c, space, obj, dataset, field, &secret_ref, &key) {
            Ok(()) => info!("config: {secret_ref} bootstrapped to device-local store"),
            Err(e) => warn!(
                "config: could not persist {secret_ref} device-locally ({e}); \
                 using the seeded value this run"
            ),
        }
    }
    if !secrets.contains_key(ANTHROPIC_SECRET_REF) {
        warn!(
            "config: no anthropic key — import an .env with \
             {ANTHROPIC_SECRET_REF}=<key> (any-ui: Help → Import connector \
             keys; CLI: .connectors.env beside the config file); llm effects \
             will fail until one is set"
        );
    }
}

/// The device-local secret value for `key` from already-queried rows
/// (None when unset/empty).
fn stored_local_secret(rows: &[Value], key: &str, field: &str) -> Option<String> {
    rows.iter()
        .find(|r| r.get("key").and_then(|v| v.as_str()) == Some(key))
        .and_then(|r| r.get(field).and_then(|v| v.as_str()))
        .map(str::to_string)
        .filter(|s| !s.is_empty())
}

/// The two-step local-scope write the server requires (ADR-006 §3): a
/// synced upsert materializes the record — carrying only the non-secret
/// key name + a `secret` marker — then a device-local `$set` writes the
/// secret into the never-synced `localValue` field. Local scope cannot
/// create records, hence the synced record first.
fn persist_local_secret(
    c: &Client,
    space: &str,
    obj: &str,
    dataset: &str,
    field: &str,
    key: &str,
    secret: &str,
) -> Result<()> {
    c.upsert_record(
        space,
        obj,
        dataset,
        key,
        &json!({"key": key, "secret": true}),
    )?;
    c.set_local_field(space, obj, dataset, key, field, &json!(secret))?;
    Ok(())
}

/// The serve-side secret write path for managed OAuth (ADR-011 §3) —
/// the same two-step local-scope write the bootstrap uses, plus the
/// synced non-secret metadata record.
struct ServeSecretStore {
    client: Arc<Client>,
    space: String,
    obj: String,
}

impl crate::oauth::SecretPersist for ServeSecretStore {
    fn persist_secret(&self, key: &str, value: &str) -> Result<()> {
        persist_local_secret(
            &self.client,
            &self.space,
            &self.obj,
            SECRETS_DATASET,
            SECRETS_FIELD,
            key,
            value,
        )
    }

    fn persist_meta(&self, key: &str, value: &Value) -> Result<()> {
        self.client.upsert_record(
            &self.space,
            &self.obj,
            SECRETS_DATASET,
            key,
            &json!({"key": key, "value": value}),
        )?;
        Ok(())
    }
}

struct Shared {
    triggers: Mutex<BTreeMap<String, Trigger>>,
    scheduler: Mutex<Scheduler>,
    watcher: Mutex<Watcher>,
    /// User texts awaiting readiness (ADR-009 §8): snapshot backlog and
    /// live messages that arrived while overlays were pending. Drained
    /// by the trigger ticker once ensure_ready clears.
    backlog: Mutex<Vec<String>>,
}

/// The resolver alias namespace (ADR-009 §2): every `[overlays]` entry
/// (its space id) verbatim; a missing `agent` entry binds the alias to
/// the working space — the degenerate single-space shape.
pub fn alias_map(
    overlays: &BTreeMap<String, crate::config::Overlay>,
    working_space: &str,
) -> BTreeMap<String, String> {
    let mut m: BTreeMap<String, String> = overlays
        .iter()
        .map(|(name, o)| (name.clone(), o.space.clone()))
        .collect();
    m.entry("agent".into())
        .or_insert_with(|| working_space.to_string());
    m
}

/// The embedder's handle on a running agent (ADR-009 §6): `ctx` is the
/// per-turn API (`RunCtx::run`); `stop()` flips the shutdown flag and
/// joins the watch/ticker/control threads. In-flight conversation
/// threads are not joined — a running turn finishes on its own.
pub struct AgentHandle {
    pub ctx: Arc<RunCtx>,
    shutdown: Arc<AtomicBool>,
    threads: Vec<std::thread::JoinHandle<()>>,
}

impl AgentHandle {
    /// Signal shutdown and join the service threads. Latency is
    /// bounded by the SSE stream: the watcher only observes the flag
    /// on the next frame/heartbeat (or the sliced reconnect sleep).
    pub fn stop(mut self) -> Result<()> {
        self.shutdown.store(true, Ordering::Relaxed);
        self.join_all()
    }

    /// Block until the service threads exit (the CLI path — they only
    /// exit on `stop()` from another handle-holder or a bind failure).
    pub fn join(mut self) -> Result<()> {
        self.join_all()
    }

    fn join_all(&mut self) -> Result<()> {
        for t in self.threads.drain(..) {
            t.join()
                .map_err(|_| anyhow::anyhow!("agent service thread panicked"))?;
        }
        Ok(())
    }
}

/// The blocking CLI surface: start + wait forever.
pub fn serve(cfg: Config) -> Result<()> {
    start(cfg)?.join()
}

/// One overlay's membership state after the boot probe (ADR-009 §8).
#[derive(Debug, PartialEq, Eq)]
pub enum OverlayMembership {
    /// space visible on this device — usable now
    Member,
    /// join requested (or still syncing) — boot proceeds, readiness is
    /// re-checked when a chat message arrives
    Pending,
}

/// The overlay space's status in THIS account's space list (a local
/// read — deliberately NOT `get_space`, which for a never-tracked id
/// sends the server on an unbounded remote load that can wedge the
/// whole Spaces surface; observed live 2026-07-21, upstream fix
/// pending). None = the account doesn't track the space at all.
fn tracked_status(client: &Client, id: &str) -> Result<Option<String>> {
    for sp in client.list_spaces(None)? {
        if sp["id"] == id {
            return Ok(Some(sp["status"].as_str().unwrap_or("active").to_string()));
        }
    }
    Ok(None)
}

/// ADR-009 §8: probe one overlay; when this account isn't a member yet
/// and an invite is configured, send the join request and PROCEED —
/// never block or poll. The invite may be a RequestToJoin token (owner
/// approval grants `reader`) or a public GUEST token (read-only, no
/// approval — the server auto-detects). A missing invite is a hard
/// boot error. Membership is read off the SPACE LIST (local), never a
/// remote-loading single-space GET.
pub fn probe_or_join_overlay(
    client: &Client,
    name: &str,
    overlay: &crate::config::Overlay,
) -> Result<OverlayMembership> {
    let id = &overlay.space;
    match tracked_status(client, id)? {
        Some(status) if status == "active" => return Ok(OverlayMembership::Member),
        // tracked but still joining/loading (a prior join, a guest
        // load in progress) — nothing to send, just not ready yet
        Some(status) => {
            info!("overlay {name:?}: space tracked, status {status:?} — waiting for it");
            return Ok(OverlayMembership::Pending);
        }
        None => {}
    }
    let Some(invite) = &overlay.invite else {
        anyhow::bail!(
            "overlay {name:?} space {id}: not joined and no invite \
             configured; set `{name} = {{ space = \"{id}\", invite = \"…\" }}` \
             in [overlays] (ADR-009 §8)"
        );
    };
    let status = match client.join_space(invite, Some(&json!({"name": "anybao"}))) {
        Ok((status, _info)) => status,
        // 409 space.already_member / space.deleted: the account already
        // tracks the space (raced our probe, or a locally-deleted guest
        // space) — pending; the message-driven recheck settles it.
        Err(e) if e.status == 409 => {
            info!("overlay {name:?}: {e} — treating as pending sync");
            return Ok(OverlayMembership::Pending);
        }
        Err(e) => anyhow::bail!("overlay {name:?}: join request failed: {e}"),
    };
    if status == 202 {
        // member invite: the publisher's approval is pending; guest
        // token: the space is loading in the background — either way,
        // wait for it to arrive.
        info!("overlay {name:?}: join accepted — waiting for the space to arrive");
    } else {
        info!("overlay {name:?}: joined, waiting for first sync");
    }
    Ok(OverlayMembership::Pending)
}

/// Compile the cage: the embedded kernel, or a `--kernel <path>` dev
/// override (ADR-009 §4 — binary + kernel are one artifact).
fn load_cage(cfg: &Config) -> Result<Arc<Cage>> {
    match &cfg.kernel {
        Some(path) => Cage::new(
            &std::fs::read(path).with_context(|| format!("kernel at {}", path.display()))?,
        ),
        None => Cage::embedded(),
    }
}

/// Everything serve does up to the watch loop, which is spawned —
/// returns immediately with the handle (the lib-mode surface).
pub fn start(mut cfg: Config) -> Result<AgentHandle> {
    let client = Arc::new(Client::new(&cfg.addr));
    let space = ensure_space(&client, &cfg.agent_space)?;
    let chat = general_chat(&client, &space)?;
    // ADR-017 §0: the bao/v1 bundle + the host-written store children
    // (config, secrets, triggers). The trigger anchor IS the triggers
    // child — deterministic, no name-scan.
    let stores = provision_agent_stores(&client, &space)?;
    let anchor = stores.triggers.clone();

    // Config cascade (ADR-006 §3): hardcoded defaults (from `bootstrap`)
    // under the space-scope override layer read off the config child.
    let config_obj = Some(stores.config.clone());
    let secrets_obj = Some(stores.secrets.clone());
    if let Some(obj) = &config_obj {
        let overrides = config_overrides(&client, &space, obj);
        info!("config obj={obj} overrides={}", overrides.len());
        for (k, v) in overrides {
            cfg.config.insert(k, v);
        }
    }
    // The guest read-guard target — the secrets object, threaded into
    // every run's Broker (sys_http). No migration from the pre-split
    // layout: secrets left on an old config object are ignored, and
    // re-importing an .env is a two-click operation.
    let secrets_guard = match &secrets_obj {
        Some(sobj) => {
            let overrides = std::mem::take(&mut cfg.secret_overrides);
            bootstrap_secrets(
                &client,
                &space,
                sobj,
                SECRETS_DATASET,
                SECRETS_FIELD,
                &mut cfg.secrets,
                &overrides,
            );
            Some(sobj.clone())
        }
        None => {
            warn!(
                "space has no agentSecretsObjectId — any server too old for the \
                 agent_secrets dataset; nothing persists this run (update the \
                 server so imported keys are stored)"
            );
            // No store ≠ no secrets: hard seeds still power this run
            // (the documented degraded mode — "runs on whatever the
            // seeds supplied"); an empty value (a delete against the
            // store) just masks any soft seed.
            for (secret_ref, value) in std::mem::take(&mut cfg.secret_overrides) {
                if value.is_empty() {
                    cfg.secrets.remove(&secret_ref);
                } else {
                    info!("config: {secret_ref} seeded for this run only (no store)");
                    cfg.secrets.insert(secret_ref, value);
                }
            }
            if !cfg.secrets.contains_key(ANTHROPIC_SECRET_REF) {
                warn!("config: no anthropic key seeded; llm effects will fail");
            }
            None
        }
    };
    // Managed OAuth state (ADR-011): drains connector.oauth.* out of
    // the static secret map — per-run broker snapshots never hold a
    // refresh token (§4/§6). persist None = the documented degraded
    // no-store mode: flows work, nothing survives the process.
    let persist: Option<Box<dyn crate::oauth::SecretPersist>> = secrets_obj.as_ref().map(|obj| {
        Box::new(ServeSecretStore {
            client: client.clone(),
            space: space.clone(),
            obj: obj.clone(),
        }) as Box<dyn crate::oauth::SecretPersist>
    });
    let shutdown = Arc::new(AtomicBool::new(false));
    let mut oauth_state = crate::oauth::OauthState::new(crate::oauth::builtin_providers(), persist);
    oauth_state.consent = cfg.consent_hook.clone();
    oauth_state.shutdown = shutdown.clone();
    let oauth = Arc::new(oauth_state);
    oauth.seed(&mut cfg.secrets);
    // grant metadata (.granted_scopes/.account — synced, non-secret)
    // loads back so oauth.status survives a restart; best-effort
    if let Some(sobj) = &secrets_obj {
        if let Ok(rows) = client.query(&space, sobj, SECRETS_DATASET, &json!({})) {
            for row in &rows {
                let Some(k) = row.get("key").and_then(|v| v.as_str()) else {
                    continue;
                };
                if k.starts_with(crate::oauth::OAUTH_REF_PREFIX)
                    && (k.ends_with(".granted_scopes") || k.ends_with(".account"))
                {
                    if let Some(v) = row.get("value").filter(|v| !v.is_null()) {
                        oauth.seed_meta(k, v.clone());
                    }
                }
            }
        }
    }

    // serve is space-only (ADR-009 §5): programs, skills, and the kernel
    // are already IN the space(s) — `anyrt deploy` is the publish step.
    // The system prompt is composed guest-side (toolcaller@v1) from the
    // space; the host injects no prompt wording (isolation: the agent's
    // context comes from `any`, not the filesystem).

    // Overlays (ADR-009 §2, §8): probe each configured space; unseen +
    // invite → join request sent, boot proceeds (no polling —
    // readiness is re-checked per incoming message).
    let mut pending: BTreeMap<String, String> = BTreeMap::new();
    for (name, overlay) in &cfg.overlays {
        if overlay.space != space
            && probe_or_join_overlay(&client, name, overlay)? == OverlayMembership::Pending
        {
            pending.insert(name.clone(), overlay.space.clone());
        }
    }
    let aliases = alias_map(&cfg.overlays, &space);
    let code_space = aliases["agent"].clone();
    // guest-visible alias map — the programs@v1 shadow guard reads it
    // to refuse overlay-exported specs (ADR-013 §1)
    cfg.config
        .insert("overlays.aliases".into(), serde_json::to_value(&aliases)?);

    // kernel is embedded (ADR-009 §4) — the cage always boots eagerly;
    // pending overlays only gate program resolution
    let cage = load_cage(&cfg)?;
    if !pending.is_empty() {
        info!(
            "overlays still joining/syncing: {:?} — will answer with status until synced",
            pending.keys().collect::<Vec<_>>()
        );
    }
    std::fs::create_dir_all(&cfg.traces_dir)?;

    // Single-active election (ADR-015): register this device in the
    // tech-space registry and take the gate's boot verdict. Standby ⇒
    // chat watch and ticker stay idle (and the standing-trigger records
    // below aren't stamped) until the election thread flips the gate.
    let election = crate::election::boot(&client, env!("CARGO_PKG_VERSION"));

    let instance = format!("anyrt-{}", std::process::id());
    let mut sched = Scheduler::new(&instance, Box::new(now_s));
    sched.arm();
    let mut registry = BTreeMap::new();
    for t in standing_triggers(&space, &chat, &instance) {
        if election.active.load(Ordering::Relaxed) {
            client.upsert_record(
                &space,
                &anchor,
                "agent_triggers",
                &t.id,
                &trigger_to_record(&t),
            )?;
        }
        registry.insert(t.id.clone(), t);
    }
    let shared = Arc::new(Shared {
        triggers: Mutex::new(registry),
        scheduler: Mutex::new(sched),
        watcher: Mutex::new(Watcher::new(&cfg.agent_name)),
        backlog: Mutex::new(Vec::new()),
    });

    let ctx = Arc::new(RunCtx {
        cage,
        pending_overlays: Mutex::new(pending),
        client: client.clone(),
        cfg,
        space: space.clone(),
        chat: chat.clone(),
        anchor: anchor.clone(),
        aliases,
        code_space,
        secrets_guard,
        oauth,
        active: election.active.clone(),
        self_peer: election.self_peer.clone(),
    });

    let mut threads = vec![
        control_api(shared.clone(), ctx.clone(), shutdown.clone()),
        trigger_ticker(shared.clone(), ctx.clone(), shutdown.clone()),
    ];
    if election.enabled {
        threads.push(election_thread(
            shared.clone(),
            ctx.clone(),
            shutdown.clone(),
        ));
    }
    info!(
        "anyrt serving space={space} chat={chat} control=127.0.0.1:{}",
        ctx.cfg.control_port
    );

    {
        let (shared, ctx, stop) = (shared.clone(), ctx.clone(), shutdown.clone());
        threads.push(std::thread::spawn(move || {
            while !stop.load(Ordering::Relaxed) {
                // standby (ADR-015 §3): stay DISCONNECTED, not muted —
                // nothing lands in the seen-set, so takeover's snapshot
                // yields the whole missed backlog
                if !ctx.active.load(Ordering::Relaxed) {
                    sliced_sleep(Duration::from_millis(500), &stop);
                    continue;
                }
                // reconnect loop: each feed's snapshot re-seeds the
                // backlog scan; the watcher's seen-set dedups replays
                match watch_chat(&shared, &ctx, &stop) {
                    Ok(()) => {}
                    Err(e) => warn!("feed error: {e}; reconnecting in 2s"),
                }
                sliced_sleep(Duration::from_secs(2), &stop);
            }
        }));
    }

    Ok(AgentHandle {
        ctx,
        shutdown,
        threads,
    })
}

/// Sleep in 100ms slices so a shutdown flag is observed promptly.
fn sliced_sleep(total: Duration, stop: &AtomicBool) {
    let mut left = total;
    while !stop.load(Ordering::Relaxed) && !left.is_zero() {
        let step = left.min(Duration::from_millis(100));
        std::thread::sleep(step);
        left -= step;
    }
}

pub struct RunCtx {
    pub cage: Arc<Cage>,
    /// overlays whose spaces haven't synced yet (ADR-009 §8):
    /// name → space id — gates program resolution, not the cage
    pub pending_overlays: Mutex<BTreeMap<String, String>>,
    pub client: Arc<Client>,
    pub cfg: Config,
    pub space: String,
    pub chat: String,
    pub anchor: String,
    /// resolver alias namespace (ADR-009 §2) — overlays + the `agent`
    /// default
    pub aliases: BTreeMap<String, String>,
    /// the agent overlay's space id (= aliases["agent"])
    pub code_space: String,
    /// the object guest http reads must never touch (the secrets
    /// object, or the config object on a pre-split server) — threaded
    /// into every Broker
    pub secrets_guard: Option<String>,
    /// managed OAuth state (ADR-011) — one per serve, threaded into
    /// every Broker
    pub oauth: Arc<crate::oauth::OauthState>,
    /// single-active gate (ADR-015 §3): false = standby (chat watch
    /// disconnected, ticker idle). Written ONLY by the election thread
    /// after boot. `ctx.run()` itself is not gated — embedder/CLI runs
    /// are explicit.
    pub active: Arc<AtomicBool>,
    /// this device's peer id in the devices registry; None = server
    /// predates /v1/devices (election disabled, gate permanently true)
    pub self_peer: Option<String>,
}

impl RunCtx {
    pub fn new_run_id() -> String {
        format!("run_{}", &uuid::Uuid::new_v4().simple().to_string()[..16])
    }

    /// Cheap readiness check (no network, never clears pending —
    /// that's `ensure_ready`'s probe).
    pub fn is_ready(&self) -> bool {
        self.pending_overlays.lock().unwrap().is_empty()
    }

    /// Re-probe pending overlays (ADR-009 §8). The error text is
    /// user-facing status — the watcher bubbles it into the chat while
    /// not ready.
    pub fn ensure_ready(&self) -> Result<()> {
        let mut pending = self.pending_overlays.lock().unwrap();
        if pending.is_empty() {
            return Ok(());
        }
        pending.retain(|_, sid| {
            !matches!(tracked_status(&self.client, sid), Ok(Some(ref st)) if st == "active")
        });
        if pending.is_empty() {
            info!("overlays synced — agent ready");
            return Ok(());
        }
        let list = pending
            .iter()
            .map(|(name, sid)| format!("overlay `{name}` (space `{sid}`)"))
            .collect::<Vec<_>>()
            .join(", ");
        anyhow::bail!("{list} still joining/syncing — waiting for the publisher's approval")
    }

    fn broker(&self, spec: &str, run_id: String) -> Broker {
        let mut writer = TraceWriter::new(json!({"id": run_id, "program": spec,
                                             "host": "rust"}));
        let trace_path = self.cfg.traces_dir.join(format!("{run_id}.jsonl"));
        if let Err(e) = writer.stream_to(&trace_path) {
            warn!("trace streaming unavailable ({e}); will write at run end");
        }
        let mut b = Broker::new(
            writer,
            self.cfg.config.clone(),
            self.cfg.secrets.clone(),
            // no local programs dir: serve resolves modules from the
            // space only (the resolver below) — never the filesystem
            None,
            Classifier::new(Some(&self.cfg.addr)),
        );
        b.secrets_guard = self.secrets_guard.clone();
        b.oauth = Some(self.oauth.clone());
        b.resolver = Some(Box::new(AnyModuleResolver::new(
            self.client.clone(),
            &self.space,
            None,
            self.aliases.clone(),
        )));
        b
    }

    pub fn run(
        &self,
        spec: &str,
        args: &Value,
        mailbox: SharedMailbox,
        interrupt: Arc<AtomicBool>,
        // Some(id) ties this run's trace to an id already handed to the
        // guest (agent_turns.traceRef); None mints a fresh one
        run_id: Option<String>,
    ) -> Result<(String, RunResult)> {
        self.ensure_ready()?;
        let broker = self.broker(spec, run_id.unwrap_or_else(Self::new_run_id));
        let run_id = broker.writer.run_id();
        let outcome = run_program(&self.cage, broker, spec, args, mailbox, interrupt, 600.0)?;
        let path = self.cfg.traces_dir.join(format!("{run_id}.jsonl"));
        outcome.broker.writer.dump(&path)?;
        Ok((
            run_id.clone(),
            RunResult {
                status: if outcome.status == "ok" {
                    "ok".into()
                } else {
                    "error".into()
                },
                duration_ms: outcome.duration_ms,
                trace_ref: Some(run_id),
                fuel: Some(outcome.fuel_used as i64),
                error: outcome.error.map(|e| e.to_string()),
            },
        ))
    }
}

/// Start a run for `text` — or, when one is already live on this chat,
/// inject into its mailbox instead. Check-and-register happens under
/// ONE watcher lock: the watch thread and the ticker's backlog drain
/// may race to start.
fn start_or_inject(shared: &Arc<Shared>, ctx: &Arc<RunCtx>, text: String) {
    let mailbox: SharedMailbox = Default::default();
    {
        let mut w = shared.watcher.lock().unwrap();
        if let Some(live) = w.live.get(&ctx.chat) {
            live.lock()
                .unwrap()
                .push_back(json!({"kind": "inject", "text": text}));
            return;
        }
        w.live.insert(ctx.chat.clone(), mailbox.clone());
    }
    let interrupt = Arc::new(AtomicBool::new(false));
    let shared = shared.clone();
    let ctx = ctx.clone();
    std::thread::spawn(move || {
        let run_id = RunCtx::new_run_id();
        // agent code resolves through the explicit alias (ADR-009 §2);
        // codeSpace lets the guest read overlay data (skills) directly.
        // Other overlays ride along for the prompt's repo inventory —
        // `agent` is excluded (it has its own Runtime-context line).
        let overlays: Map<String, Value> = ctx
            .aliases
            .iter()
            .filter(|(name, _)| name.as_str() != "agent")
            .map(|(name, id)| (name.clone(), json!(id)))
            .collect();
        let args = json!({
            "space": ctx.space, "chatId": ctx.chat, "userText": text,
            "agentName": ctx.cfg.agent_name, "traceRef": run_id,
            "codeSpace": ctx.code_space, "overlays": overlays});
        let result = ctx.run(
            "agent:toolcaller@v1",
            &args,
            mailbox,
            interrupt,
            Some(run_id),
        );
        if let Ok((trace_ref, rr)) = &result {
            if rr.status != "ok" {
                // The typed error goes INTO the chat: the next turn
                // boots with this message in its window, so the model
                // can act on it (FuelExhausted's text says to redo the
                // work in smaller chunks — a bare "something broke"
                // teaches nothing).
                let detail = rr.error.as_deref().map(|raw| {
                    serde_json::from_str::<Value>(raw)
                        .ok()
                        .and_then(|v| {
                            Some(format!(
                                "{}: {}",
                                v["type"].as_str()?,
                                v["message"].as_str()?
                            ))
                        })
                        .unwrap_or_else(|| raw.to_string())
                });
                let text = match detail {
                    Some(d) => format!("Something broke mid-run (trace {trace_ref}): {d}"),
                    None => format!("Something broke mid-run (trace {trace_ref})."),
                };
                let _ = ctx.client.chat_send(
                    &ctx.space,
                    &ctx.chat,
                    &json!({
                    "text": text,
                    "agent": {"name": ctx.cfg.agent_name, "done": true}}),
                );
            }
        }
        shared.watcher.lock().unwrap().conversation_done(&ctx.chat);
    });
}

fn watch_chat(shared: &Arc<Shared>, ctx: &Arc<RunCtx>, stop: &AtomicBool) -> Result<()> {
    for frame in ctx.client.subscribe_dataset(
        &ctx.space,
        &ctx.chat,
        "chat_messages",
        &json!({"sort": ["-createdAt"], "limit": 64}),
    )? {
        // shutdown and stand-down (ADR-015 §3) are observed per frame —
        // the read itself blocks until the server's next event/heartbeat
        // (ADR-009 open Q3); returning drops the stream
        if stop.load(Ordering::Relaxed) || !ctx.active.load(Ordering::Relaxed) {
            return Ok(());
        }
        match frame.event.as_str() {
            "ready" => continue,
            "closed" => return Ok(()),
            // snapshot backlog (ADR-009 §8): user messages newer than
            // the agent's last reply — texts sent while the runtime was
            // down or booting — are answered instead of dropped. The
            // watcher's seen-set dedups across reconnect snapshots.
            "snapshot" => {
                let backlog = snapshot_backlog(&frame.data, &ctx.cfg.agent_name);
                if backlog.is_empty() {
                    continue;
                }
                let ready = ctx.ensure_ready().is_ok();
                for record in backlog {
                    let action = shared
                        .watcher
                        .lock()
                        .unwrap()
                        .on_message(&ctx.chat, &record);
                    if let WatchAction::Start = action {
                        let text = Watcher::attributed_text(&record);
                        if ready {
                            info!("backlog conversation: {:?}", preview(&text));
                            start_or_inject(shared, ctx, text);
                        } else {
                            // no bubble for stale messages — a burst of
                            // "not ready" is noise; the ticker drains
                            // the queue once overlays sync
                            shared.backlog.lock().unwrap().push(text);
                        }
                    }
                }
            }
            "changes" => {
                for record in records_in(&frame.data) {
                    let action = shared
                        .watcher
                        .lock()
                        .unwrap()
                        .on_message(&ctx.chat, &record);
                    if let WatchAction::Start = action {
                        let text = Watcher::attributed_text(&record);
                        // deferred boot (ADR-009 §8): a message while
                        // overlays are pending gets a status bubble —
                        // the one host-authored operational reply —
                        // and queues for a real answer once synced
                        match ctx.ensure_ready() {
                            Ok(_) => {
                                info!("conversation started: {:?}", preview(&text));
                                start_or_inject(shared, ctx, text);
                            }
                            Err(status) => {
                                warn!("not ready: {status}");
                                let _ = ctx.client.chat_send(
                                    &ctx.space,
                                    &ctx.chat,
                                    &json!({
                                    "text": format!("Not ready yet: {status}."),
                                    "agent": {"name": ctx.cfg.agent_name, "done": true}}),
                                );
                                shared.backlog.lock().unwrap().push(text);
                            }
                        }
                    }
                }
            }
            other => anyhow::bail!("unexpected frame {other:?}"),
        }
    }
    Ok(())
}

/// Messages newer than this agent's last own reply, oldest first. The
/// snapshot window is sorted `-createdAt`: walk from the newest record
/// and stop at the first SELF-authored one (own `agent.name`, or a
/// nameless agent record — same rule as the watcher, ADR-009 §8
/// amendment) — everything before it is unanswered. Foreign
/// agent-named messages (`trigger:*` nudges, peers) count as
/// unanswered input. Snapshot records are bare docs (each carries its
/// own `id`), unlike the `{id, doc}` entries of `changes` frames.
fn snapshot_backlog(data: &Value, self_name: &str) -> Vec<Value> {
    let mut out: Vec<Value> = Vec::new();
    for rec in data["records"].as_array().unwrap_or(&Vec::new()) {
        if Watcher::is_self_message(rec, self_name) {
            break;
        }
        // content = text OR attachments (the server enforces at least
        // one); an attachment-only message is real input, not noise
        let has_text = rec["text"].as_str().is_some_and(|t| !t.is_empty());
        let has_atts = rec["attachments"].as_object().is_some_and(|a| !a.is_empty());
        if has_text || has_atts {
            out.push(rec.clone());
        }
    }
    out.reverse();
    out
}

/// Log-safe head of a message (char-boundary aware — byte slicing
/// panics mid-codepoint).
fn preview(text: &str) -> String {
    text.chars().take(60).collect()
}

fn records_in(data: &Value) -> Vec<Value> {
    let mut out = Vec::new();
    if let Some(batches) = data.as_array() {
        for batch in batches {
            for key in ["added", "updated"] {
                for entry in batch[key].as_array().unwrap_or(&Vec::new()) {
                    let mut rec = entry["doc"].as_object().cloned().unwrap_or_default();
                    rec.insert("id".into(), entry["id"].clone());
                    out.push(Value::Object(rec));
                }
            }
        }
    }
    out
}

fn trigger_ticker(
    shared: Arc<Shared>,
    ctx: Arc<RunCtx>,
    stop: Arc<AtomicBool>,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || loop {
        sliced_sleep(Duration::from_secs(5), &stop);
        if stop.load(Ordering::Relaxed) {
            return;
        }
        // standby (ADR-015 §3): no runs, no adoption, no ownership
        // stamping — the election thread owns the transitions
        if !ctx.active.load(Ordering::Relaxed) {
            continue;
        }
        // deferred boot (ADR-009 §8): don't burn trigger runs (and the
        // circuit breaker) while overlays are still syncing. This is a
        // PROBE, not the cheap check — readiness must clear without
        // waiting for a chat message (the backlog drain below and
        // trigger start both hang off it); a no-op once synced.
        if ctx.ensure_ready().is_err() {
            continue;
        }
        // answer user messages deferred while overlays were syncing
        let deferred: Vec<String> = std::mem::take(&mut *shared.backlog.lock().unwrap());
        for text in deferred {
            info!("deferred conversation: {:?}", preview(&text));
            start_or_inject(&shared, &ctx, text);
        }
        // the dataset is the source of truth (ADR-006 §4): adopt records
        // the registry has never seen (ownerless or ours), honor enabled
        // edits on adopted ones; foreign owners and malformed records are
        // left alone (the latter loudly)
        if let Ok(recs) = ctx
            .client
            .query(&ctx.space, &ctx.anchor, "agent_triggers", &json!({}))
        {
            let instance = shared.scheduler.lock().unwrap().instance_id().to_string();
            let mut reg = shared.triggers.lock().unwrap();
            let standing: std::collections::BTreeSet<String> =
                standing_triggers(&ctx.space, "", "")
                    .into_iter()
                    .map(|t| t.id)
                    .collect();
            for rec in recs {
                let Some(id) = rec.get("id").and_then(|v| v.as_str()) else {
                    continue;
                };
                if standing.contains(id) {
                    continue; // built-ins are code-owned, not record-owned
                }
                match reg.get_mut(id) {
                    Some(live) => {
                        if let Some(en) = rec.get("enabled").and_then(|v| v.as_bool()) {
                            live.enabled = en;
                        }
                    }
                    None => {
                        let Some(mut t) = record_to_trigger(id, &rec) else {
                            warn!("agent_triggers {id:?}: malformed record — skipped");
                            continue;
                        };
                        if !t.owner.is_empty() && t.owner != instance {
                            continue; // foreign-owned
                        }
                        t.owner = instance.clone(); // adopt + stamp
                        let _ = ctx.client.upsert_record(
                            &ctx.space,
                            &ctx.anchor,
                            "agent_triggers",
                            id,
                            &trigger_to_record(&t),
                        );
                        info!("trigger adopted from dataset: {id:?} ({})", t.kind);
                        reg.insert(id.to_string(), t);
                    }
                }
            }
        }
        let due: Vec<Trigger> = {
            let mut reg = shared.triggers.lock().unwrap();
            let sched = shared.scheduler.lock().unwrap();
            let mut out = Vec::new();
            for t in reg.values_mut() {
                if t.kind == "cron" && sched.cron_due(t) {
                    out.push(t.clone());
                    sched.advance_cron(t);
                } else if sched.once_due(t) {
                    out.push(t.clone());
                    // consume the shot before the run: at-most-once even
                    // if the run path dies mid-way (ADR-006 §4)
                    t.enabled = false;
                }
            }
            out
        };
        for t in due {
            let mailbox: SharedMailbox = Default::default();
            let interrupt = Arc::new(AtomicBool::new(false));
            let result = ctx.run(&t.program, &t.args, mailbox, interrupt, None);
            let rr = result.map(|(_, rr)| rr).unwrap_or_else(|e| RunResult {
                status: "error".into(),
                duration_ms: 0,
                trace_ref: None,
                fuel: None,
                error: Some(e.to_string()),
            });
            let mut reg = shared.triggers.lock().unwrap();
            if let Some(live) = reg.get_mut(&t.id) {
                let sched = shared.scheduler.lock().unwrap();
                let run_rec = sched.record_run(live, &rr);
                let ts_ms = (now_s() * 1000.0) as i64;
                let rid = format!("{}:{:020}", t.id, ts_ms);
                let _ = ctx.client.upsert_record(
                    &ctx.space,
                    &ctx.anchor,
                    "agent_trigger_runs",
                    &rid,
                    &run_rec,
                );
                let _ = ctx.client.upsert_record(
                    &ctx.space,
                    &ctx.anchor,
                    "agent_triggers",
                    &t.id,
                    &trigger_to_record(live),
                );
            }
        }
    })
}

/// The election reconcile loop (ADR-015 §3/§4): poll the registry,
/// flip the gate on verdict transitions. The ONLY writer of
/// `ctx.active` after boot, so load-then-store is race-free.
fn election_thread(
    shared: Arc<Shared>,
    ctx: Arc<RunCtx>,
    stop: Arc<AtomicBool>,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let Some(peer) = ctx.self_peer.clone() else {
            return; // enabled implies a peer id; belt and braces
        };
        loop {
            sliced_sleep(crate::election::POLL, &stop);
            if stop.load(Ordering::Relaxed) {
                return;
            }
            let verdict = crate::election::reconcile(&ctx.client, &peer, crate::election::APP_SLUG);
            match verdict {
                None => {} // transient read failure — keep the last state
                Some(true) if !ctx.active.load(Ordering::Relaxed) => {
                    takeover(&shared, &ctx);
                    ctx.active.store(true, Ordering::Relaxed); // AFTER re-arm
                    info!("election: TAKEOVER — this device is now the active bao");
                }
                Some(false) if ctx.active.load(Ordering::Relaxed) => {
                    ctx.active.store(false, Ordering::Relaxed);
                    // the new active device answers these; in-flight
                    // runs finish on their own (never interrupt a turn)
                    shared.backlog.lock().unwrap().clear();
                    info!("election: stand-down — another device is the active bao");
                }
                Some(_) => {} // verdict matches the current state
            }
        }
    })
}

/// Takeover prep (ADR-015 §3), run BEFORE the gate flips: re-arm every
/// cron strictly forward (a missed occurrence while standby does not
/// exist — the ADR-006 §4 cold-sync rule; prevents the wake-and-replay
/// burst), then stamp + publish the registry's trigger records that the
/// standby boot skipped.
fn takeover(shared: &Shared, ctx: &RunCtx) {
    let instance = shared.scheduler.lock().unwrap().instance_id().to_string();
    let mut reg = shared.triggers.lock().unwrap();
    for t in reg.values_mut() {
        if t.kind == "cron" {
            t.next_due = None; // next tick arms forward, no fire
        }
        t.owner = instance.clone();
        let _ = ctx.client.upsert_record(
            &ctx.space,
            &ctx.anchor,
            "agent_triggers",
            &t.id,
            &trigger_to_record(t),
        );
    }
}

// --- the localhost control API -------------------------------------------------

fn control_api(
    shared: Arc<Shared>,
    ctx: Arc<RunCtx>,
    stop: Arc<AtomicBool>,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let server = match tiny_http::Server::http(("127.0.0.1", ctx.cfg.control_port)) {
            Ok(s) => s,
            Err(e) => {
                error!("control API bind failed: {e}");
                return;
            }
        };
        // recv_timeout instead of incoming_requests: the accept loop
        // must observe the shutdown flag (ADR-009 §6)
        while !stop.load(Ordering::Relaxed) {
            let mut req = match server.recv_timeout(Duration::from_millis(250)) {
                Ok(Some(r)) => r,
                Ok(None) => continue,
                Err(e) => {
                    error!("control API accept failed: {e}");
                    return;
                }
            };
            let path = req.url().to_string();
            let method = req.method().as_str().to_string();
            let mut body = String::new();
            let _ = std::io::Read::read_to_string(req.as_reader(), &mut body);
            let reply = handle_control(&shared, &ctx, &method, &path, &body);
            let (status, payload) = match reply {
                Ok(v) => (200, v),
                Err(e) => (400, json!({"error": e.to_string()})),
            };
            let data = payload.to_string();
            let _ = req.respond(
                tiny_http::Response::from_string(data)
                    .with_status_code(status)
                    .with_header(
                        "Content-Type: application/json"
                            .parse::<tiny_http::Header>()
                            .unwrap(),
                    ),
            );
        }
    })
}

fn handle_control(
    shared: &Shared,
    ctx: &RunCtx,
    method: &str,
    path: &str,
    body: &str,
) -> Result<Value> {
    let parts: Vec<&str> = path.trim_matches('/').split('/').collect();
    let mut reg = shared.triggers.lock().unwrap();
    match (method, parts.as_slice()) {
        // election observability (ADR-015 §5); winner via a live
        // registry read, null when unavailable
        ("GET", ["election"]) => {
            let winner = ctx
                .self_peer
                .as_ref()
                .and_then(|_| ctx.client.list_devices().ok())
                .and_then(|r| {
                    r["active"][crate::election::APP_SLUG]
                        .as_str()
                        .map(str::to_string)
                });
            Ok(json!({
                "app": crate::election::APP_SLUG,
                "enabled": ctx.self_peer.is_some(),
                "active": ctx.active.load(Ordering::Relaxed),
                "peerId": ctx.self_peer,
                "winner": winner,
            }))
        }
        ("GET", ["triggers"]) => Ok(Value::Array(reg.values().map(rollup).collect())),
        ("GET", ["triggers", id]) => {
            let t = reg.get(*id).context("trigger not found")?;
            Ok(json!({"id": t.id})
                .as_object()
                .cloned()
                .map(|mut m| {
                    m.extend(
                        trigger_to_record(t)
                            .as_object()
                            .cloned()
                            .unwrap_or_default(),
                    );
                    Value::Object(m)
                })
                .unwrap())
        }
        ("GET", ["triggers", id, "runs"]) => {
            let rows = ctx.client.query(
                &ctx.space,
                &ctx.anchor,
                "agent_trigger_runs",
                &json!({"filter": {"triggerId": id}, "sort": ["-ts"],
                        "limit": 20}),
            )?;
            Ok(Value::Array(rows))
        }
        ("PATCH", ["triggers", id]) => {
            let t = reg.get_mut(*id).context("trigger not found")?;
            let patch: Map<String, Value> = serde_json::from_str(body)?;
            if let Some(spec) = patch.get("spec") {
                t.spec = spec.clone();
                t.next_due = None; // re-arm forward on the new schedule
            }
            if let Some(en) = patch.get("enabled").and_then(|v| v.as_bool()) {
                t.enabled = en;
            }
            Ok(trigger_to_record(t))
        }
        ("POST", ["triggers", id, "enable"]) => {
            let t = reg.get_mut(*id).context("trigger not found")?;
            t.enabled = true;
            t.consecutive_failures = 0; // manual re-enable resets the breaker
            t.next_due = None;
            Ok(trigger_to_record(t))
        }
        ("POST", ["triggers", id, "disable"]) => {
            let t = reg.get_mut(*id).context("trigger not found")?;
            t.enabled = false;
            Ok(trigger_to_record(t))
        }
        _ => anyhow::bail!("no route: {method} {path}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Overlay;
    use crate::testutil::StubTransport;

    fn overlay(space: &str, invite: Option<&str>) -> Overlay {
        Overlay {
            space: space.into(),
            invite: invite.map(str::to_string),
        }
    }

    fn scripted(replies: &[(u16, Value)]) -> (Client, crate::testutil::CallLog) {
        let stub = StubTransport::new();
        for (status, body) in replies {
            stub.push(*status, body.clone());
        }
        let log = stub.log();
        (Client::with_transport(Box::new(stub)), log)
    }

    fn spaces_reply(rows: Value) -> (u16, Value) {
        (200, json!({"spaces": rows}))
    }

    #[test]
    fn ensure_space_prefers_materialized_derived_row() {
        // registry row created:true wins outright — no space-list scan,
        // no ambiguity with a same-named legacy space
        let (c, log) = scripted(&[spaces_reply(
            json!([{"name": "bao", "spaceId": "derived-id", "created": true}]),
        )]);
        assert_eq!(ensure_space(&c, "bao").unwrap(), "derived-id");
        let calls = log.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].1, "/v1/spaces/derived");
    }

    #[test]
    fn ensure_space_derives_unmaterialized_registry_name() {
        // registry name, unmaterialized → derive on the spot; a legacy
        // same-named space is never scanned for (clean cut) and
        // POST /v1/spaces never happens
        let (c, log) = scripted(&[
            spaces_reply(json!([{"name": "bao", "spaceId": "derived-id", "created": false}])),
            (201, json!({"id": "derived-id", "derived": true})),
        ]);
        assert_eq!(ensure_space(&c, "bao").unwrap(), "derived-id");
        let calls = log.lock().unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[1].0, "POST");
        assert_eq!(calls[1].1, "/v1/spaces/derived/bao");
    }

    #[test]
    fn ensure_space_requires_the_registry_route() {
        // no-backcompat: a server without the derived registry is
        // unsupported — error, never a name-scan fallback
        let (c, _) = scripted(&[(
            404,
            json!({"error": {"code": "request.not_found", "message": "Not Found"}}),
        )]);
        assert!(ensure_space(&c, "bao").is_err());
    }

    #[test]
    fn ensure_space_non_registry_name_scans_by_name() {
        // registry exists but doesn't know this name → v1 rule
        let (c, _) = scripted(&[
            spaces_reply(json!([{"name": "bao", "spaceId": "derived-id", "created": true}])),
            spaces_reply(json!([{"id": "mine", "name": "myspace", "status": "active"}])),
        ]);
        assert_eq!(ensure_space(&c, "myspace").unwrap(), "mine");
    }

    #[test]
    fn general_chat_ensures_the_bundle() {
        let (c, log) = scripted(&[(
            200,
            json!({"bundle": {"id": "general-chat/v1", "rootId": "chat-root"},
                   "installed": false}),
        )]);
        assert_eq!(general_chat(&c, "sp").unwrap(), "chat-root");
        let calls = log.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].1, "/v1/spaces/sp/bundles");
    }

    #[test]
    fn general_chat_requires_the_bundles_route() {
        // no-backcompat: a server without bundles is unsupported —
        // error, never a SpaceInfo-field fallback
        let (c, log) = scripted(&[(
            404,
            json!({"error": {"code": "request.not_found", "message": "Not Found"}}),
        )]);
        assert!(general_chat(&c, "sp").is_err());
        assert_eq!(log.lock().unwrap().len(), 1);
    }

    #[test]
    fn probe_member_when_space_active_in_list() {
        // membership is a LOCAL space-list read — never a single-space
        // GET (which remote-loads never-tracked ids server-side)
        let (c, log) = scripted(&[spaces_reply(json!([{"id": "repo", "status": "active"}]))]);
        let m = probe_or_join_overlay(&c, "agent", &overlay("repo", None)).unwrap();
        assert_eq!(m, OverlayMembership::Member);
        let calls = log.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].1, "/v1/spaces");
    }

    #[test]
    fn probe_pending_when_tracked_but_loading() {
        // a prior join / guest load in progress: tracked, not active —
        // nothing to send, just wait
        let (c, log) = scripted(&[spaces_reply(json!([{"id": "repo", "status": "loading"}]))]);
        let m = probe_or_join_overlay(&c, "agent", &overlay("repo", Some("tok"))).unwrap();
        assert_eq!(m, OverlayMembership::Pending);
        assert_eq!(log.lock().unwrap().len(), 1); // no join call
    }

    #[test]
    fn probe_joins_and_proceeds_pending() {
        // untracked → join 202 → PENDING, no polling (ADR-009 §8)
        let (c, log) = scripted(&[
            spaces_reply(json!([])),
            (202, json!({"id": "repo", "status": "joining"})),
        ]);
        let m = probe_or_join_overlay(&c, "agent", &overlay("repo", Some("tok"))).unwrap();
        assert_eq!(m, OverlayMembership::Pending);
        let calls = log.lock().unwrap();
        assert_eq!(calls.len(), 2); // list + join — nothing else
        assert_eq!(calls[1].1, "/v1/spaces/join");
        assert_eq!(calls[1].2.as_ref().unwrap()["inviteToken"], json!("tok"));
    }

    #[test]
    fn probe_tolerates_already_tracked_409() {
        // guest-token join racing the list read: 409 space.already_member
        // — pending, not a boot failure (ADR-009 §8)
        let (c, _) = scripted(&[
            spaces_reply(json!([])),
            (
                409,
                json!({"error": {"code": "space.already_member",
                                 "message": "already tracks the space"}}),
            ),
        ]);
        let m = probe_or_join_overlay(&c, "agent", &overlay("repo", Some("guestTok"))).unwrap();
        assert_eq!(m, OverlayMembership::Pending);
    }

    #[test]
    fn probe_without_invite_names_the_fix() {
        let (c, _) = scripted(&[spaces_reply(json!([]))]);
        let err = probe_or_join_overlay(&c, "agent", &overlay("repo", None)).unwrap_err();
        assert!(err.to_string().contains("no invite"), "{err}");
        assert!(err.to_string().contains("[overlays]"), "{err}");
    }

    /// The readiness core, minus RunCtx plumbing (a real Cage needs
    /// wasm) — same retain-probe + status text as ensure_ready.
    fn recheck(client: &Client, pending: &mut BTreeMap<String, String>) -> Result<()> {
        pending.retain(|_, sid| client.get_space(sid).is_err());
        if pending.is_empty() {
            return Ok(());
        }
        let list = pending
            .iter()
            .map(|(name, sid)| format!("overlay `{name}` (space `{sid}`)"))
            .collect::<Vec<_>>()
            .join(", ");
        anyhow::bail!("{list} still joining/syncing — waiting for the publisher's approval")
    }

    #[test]
    fn readiness_reports_pending_overlay_status() {
        // FakeSpace has no get_space route → the space is still unseen
        let c = Client::with_transport(Box::new(crate::testutil::FakeSpace::new()));
        let mut pending: BTreeMap<String, String> =
            [("agent".to_string(), "repo".to_string())].into();
        let err = recheck(&c, &mut pending).expect_err("must be pending");
        assert!(err.to_string().contains("overlay `agent`"), "{err}");
        assert!(err.to_string().contains("still joining/syncing"), "{err}");
    }

    #[test]
    fn readiness_clears_once_spaces_are_visible() {
        let (c, _) = scripted(&[spaces_reply(json!([{"id": "repo", "status": "active"}]))]);
        let mut pending: BTreeMap<String, String> =
            [("agent".to_string(), "repo".to_string())].into();
        recheck(&c, &mut pending).unwrap();
        assert!(pending.is_empty());
    }

    fn msg(id: &str, text: &str, agent: bool) -> Value {
        let mut m = json!({"id": id, "text": text, "createdAt": 1});
        if agent {
            m["agent"] = json!({"name": "bao", "done": true});
        }
        m
    }

    #[test]
    fn snapshot_backlog_stops_at_the_agents_last_reply() {
        // newest-first window: two unanswered user messages, then the
        // agent's reply, then answered history — backlog is the two,
        // oldest first
        let data = json!({"records": [
            msg("u3", "third", false),
            msg("u2", "second", false),
            msg("a1", "reply", true),
            msg("u1", "answered", false),
        ]});
        let backlog = snapshot_backlog(&data, "bao");
        let ids: Vec<&str> = backlog.iter().map(|r| r["id"].as_str().unwrap()).collect();
        assert_eq!(ids, ["u2", "u3"]);
    }

    #[test]
    fn snapshot_backlog_empty_when_reply_is_newest() {
        let data = json!({"records": [
            msg("a1", "reply", true),
            msg("u1", "answered", false),
        ]});
        assert!(snapshot_backlog(&data, "bao").is_empty());
    }

    #[test]
    fn snapshot_backlog_takes_whole_window_without_a_reply() {
        // fresh chat / reply scrolled out of the window: everything
        // visible is unanswered
        let data = json!({"records": [msg("u2", "b", false), msg("u1", "a", false)]});
        let backlog = snapshot_backlog(&data, "bao");
        let ids: Vec<&str> = backlog.iter().map(|r| r["id"].as_str().unwrap()).collect();
        assert_eq!(ids, ["u1", "u2"]);
    }

    #[test]
    fn snapshot_backlog_skips_contentless_but_keeps_attachment_only() {
        // null docs and empty messages can't start a run; a message
        // that is ONLY an attachment (no caption) is real input
        let mut with_atts = msg("u3", "", false);
        with_atts["attachments"] =
            json!({"a0": {"type": "link", "link": "any://o/sp1/obj1"}});
        let data = json!({"records": [
            msg("u2", "real", false),
            with_atts,
            null,
            msg("u1", "", false),
        ]});
        let backlog = snapshot_backlog(&data, "bao");
        let ids: Vec<&str> = backlog.iter().map(|r| r["id"].as_str().unwrap()).collect();
        assert_eq!(ids, ["u3", "u2"]);
    }

    #[test]
    fn preview_respects_char_boundaries() {
        let s = "é".repeat(80); // 2 bytes per char — byte-60 is mid-codepoint
        assert_eq!(preview(&s).chars().count(), 60);
    }

    #[test]
    fn alias_map_defaults_agent_to_working_space() {
        let m = alias_map(&BTreeMap::new(), "ws1");
        assert_eq!(m.get("agent").map(String::as_str), Some("ws1"));
    }

    #[test]
    fn alias_map_keeps_configured_overlays_verbatim() {
        let mut overlays = BTreeMap::new();
        overlays.insert(
            "agent".to_string(),
            crate::config::Overlay {
                space: "codeSpace".to_string(),
                invite: None,
            },
        );
        overlays.insert(
            "std".to_string(),
            crate::config::Overlay {
                space: "stdSpace".to_string(),
                invite: Some("tok".to_string()),
            },
        );
        let m = alias_map(&overlays, "ws1");
        assert_eq!(m.get("agent").map(String::as_str), Some("codeSpace"));
        assert_eq!(m.get("std").map(String::as_str), Some("stdSpace"));
        assert_eq!(m.len(), 2);
    }
}
