//! `anyrt serve` — the outer loop: ensure space/chat/anchor, resolve
//! overlays from the space (space-only, ADR-009 §5 — `anyrt deploy`
//! is the publish step; the kernel is embedded), then watch the chat
//! (SSE; the snapshot seeds the unanswered-message backlog, ADR-009
//! §8), tick triggers, and answer the localhost control API.
//! Conversations and trigger runs are guest programs through the
//! shared cage.

use crate::anyapi::Client;
use crate::broker::{
    Broker, DeclaredCredentials, PresenceState, SecretSource, SharedMailbox, SharedPresence,
};
use crate::config::Config;
use crate::resolver::AnyModuleResolver;
use crate::routes::Classifier;
use crate::runner::{run_program, Cage};
use crate::trace::TraceWriter;
use crate::tracestore::TraceStore;
use crate::triggers::{
    chat_watch_trigger, desired_event_sources, event_args, event_source, event_space, health_pass,
    is_chat_watch, owned_chat_watch, reconcile_registry, record_to_trigger, rollup,
    standing_triggers, trigger_to_record, ChatInput, EventSource, LiveRun, RunResult, Scheduler,
    Trigger, WatchAction, Watcher, CHAT_MESSAGES, CHAT_WATCH_ID,
};
use anyhow::{Context, Result};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tracing::{error, info, warn};

/// The chat loop's program spec — the "conversation" class for
/// retention (ADR-023 §6).
pub const CHAT_PROGRAM: &str = "agent:toolcaller@v1";

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

/// The space's general chat — the catalog's `general-chat` usecase
/// (ADR-027 §1): `POST /v1/catalog/general-chat/setup` adopts or
/// installs the one chat, on a root DERIVED from the bundle id —
/// identical on every device and member, computed offline, so the
/// chat can never fork (chat content cannot be merged across objects,
/// so a fork must be impossible rather than resolvable). The server
/// runs the registry-convergence wait itself; `409 bundle.not_ready`
/// (a winner's tree still syncing to this device) is retried briefly.
/// A non-derived root is a server this runtime does not support:
/// serve stops naming the object. The catalog route is REQUIRED — a
/// server without it is unsupported (no-backcompat).
fn general_chat(c: &Client, space: &str) -> Result<String> {
    let mut last_err = None;
    for _ in 0..5 {
        match c.catalog_setup("general-chat", space) {
            Ok(reply) => {
                let bundle = &reply["bundles"][0]["bundle"];
                let root = bundle["rootId"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .context("general-chat setup reply carries no rootId")?;
                if bundle["derived"] != json!(true) {
                    anyhow::bail!(
                        "derived general chat not found in space {space}: the catalog's \
                         general-chat is bound to non-derived chat object {root}"
                    );
                }
                return Ok(root.to_string());
            }
            Err(e) if e.status == 409 => {
                info!("boot: general chat not ready ({e}) — retrying in 2s");
                last_err = Some(e);
                std::thread::sleep(Duration::from_secs(2));
            }
            Err(e) => return Err(e).context("general-chat catalog setup"),
        }
    }
    Err(last_err.unwrap()).context("general chat never became ready")
}

/// The host-written agent stores (ADR-017 §0/§1, ADR-027 §2): anyrt
/// registers the `bao/v1` bundle and derives + declares ONLY what it
/// writes before guest code can run — config, secrets, triggers,
/// runs. Brain and chat logs are guest-owned (any@v1 ensures them
/// lazily). Every store is a part of its type; the collection each
/// one lives in is read back from the declaration, never composed.
pub struct AgentStores {
    pub config: String,
    pub secrets: String,
    pub triggers: String,
    /// `bao/runs/v1` — the synced per-run summaries (ADR-023 §1)
    pub runs: String,
    /// the collections (`<typeId>_<key>`) the four stores' records live in
    pub config_ds: String,
    pub secrets_ds: String,
    pub triggers_ds: String,
    pub runs_ds: String,
}

/// Find-or-create a harness type by xKey. Harness types are HIDDEN
/// (ADR-027 §2): a client's type picker never offers them; the
/// listing includes hidden rows, so the find never re-creates one.
fn ensure_type(c: &Client, space: &str, name: &str, xkey: &str) -> Result<String> {
    for t in c.list_types(space)? {
        if t["xKey"] == xkey {
            let tid = t["id"].as_str().unwrap_or_default().to_string();
            if t["hidden"] != json!(true) {
                c.patch_type(space, &tid, &json!({"hidden": true}))?;
            }
            return Ok(tid);
        }
    }
    let created = c.create_type(space, &json!({"name": name, "xKey": xkey, "hidden": true}))?;
    Ok(created["typeId"].as_str().unwrap_or_default().to_string())
}

/// The collection a type's dataset lives in, read off the datasets
/// listing (`None` = the type declares no such key).
pub fn dataset_collection(
    c: &Client,
    space: &str,
    type_id: &str,
    key: &str,
) -> Result<Option<String>> {
    Ok(c.list_datasets(space, type_id)?
        .iter()
        .find(|d| d["key"] == key)
        .and_then(|d| d["collection"].as_str())
        .map(str::to_string))
}

/// The collection of a harness store addressed by type xKey + dataset
/// key — for stores another writer declares (the guest's `agent_log`).
/// `None` when the type or the key is not there yet.
pub fn store_collection(
    c: &Client,
    space: &str,
    type_xkey: &str,
    key: &str,
) -> Result<Option<String>> {
    let Some(tid) = c
        .list_types(space)?
        .into_iter()
        .find(|t| t["xKey"] == type_xkey)
        .and_then(|t| t["id"].as_str().map(str::to_string))
    else {
        return Ok(None);
    };
    dataset_collection(c, space, &tid, key)
}

/// Declare one store as a part of its type — `{key, datasets: [draft]}`
/// with the dataset under the same key — unless the key is already
/// declared, and return the collection the records live in. No
/// reconcile: the host stores declare no mutable search.* leaves.
fn ensure_dataset(c: &Client, space: &str, type_id: &str, draft: &Value) -> Result<String> {
    let key = draft["key"]
        .as_str()
        .context("store draft carries no key")?
        .to_string();
    if let Some(existing) = c
        .list_datasets(space, type_id)?
        .into_iter()
        .find(|d| d["key"] == key)
    {
        reconcile_fields(c, space, type_id, &existing, draft);
        if let Some(coll) = existing["collection"].as_str() {
            return Ok(coll.to_string());
        }
    }
    c.add_part(space, type_id, &json!({"key": key, "datasets": [draft]}))?;
    dataset_collection(c, space, type_id, &key)?
        .with_context(|| format!("store {key} declared but not listed"))
}

/// Additive field reconcile of a declared (non-dynamic) store (ADR-021
/// §2): a field the draft declares that the existing definition lacks
/// is added; nothing is removed or retyped. Best-effort — a failure
/// only means the newest stamps are rejected until the server catches
/// up, and is logged as such.
fn reconcile_fields(c: &Client, space: &str, type_id: &str, existing: &Value, draft: &Value) {
    let Some(def_id) = existing["id"].as_str() else {
        return;
    };
    let have: Vec<&str> = existing["fields"]
        .as_array()
        .map(|fs| fs.iter().filter_map(|f| f["key"].as_str()).collect())
        .unwrap_or_default();
    let key = draft["key"].as_str().unwrap_or("?");
    for field in draft["fields"].as_array().into_iter().flatten() {
        let Some(k) = field["key"].as_str() else {
            continue;
        };
        if have.contains(&k) {
            continue;
        }
        match c.add_dataset_field(space, type_id, def_id, field) {
            Ok(_) => info!("store {key}: declared field {k} added"),
            Err(e) => warn!("store {key}: could not add declared field {k} ({e})"),
        }
    }
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

pub fn provision_agent_stores(c: &Client, space: &str) -> Result<AgentStores> {
    // The bundle: adopt-or-install, brief not_ready retry (same class
    // as the chat ensure).
    let mut last_err = None;
    let mut registered = false;
    for _ in 0..5 {
        // Created root on purpose: the bao space is single-account
        // (owner escape covers offline installs) and created stays the
        // default — a derived root could never be uninstalled. `page`
        // (built-in) gives the root a body.
        match c.ensure_bundle(space, "bao/v1", "bao", &["page"], false) {
            Ok(_) => {
                registered = true;
                break;
            }
            Err(e) if e.status == 409 => {
                info!("boot: bao/v1 bundle not ready ({e}) — retrying in 2s");
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
    // Declared fields carry behavior (a declared field needs a kind);
    // everything free-form rides the `dynamic` keyspace — undeclared
    // fields are any-typed and freely mutable. Config `value` is
    // any-typed (tier objects, strings, lists) and the whole trigger
    // record shape stays undeclared for exactly that reason.
    // `agent_config` is `{key, value}` synced only (ADR-006 §3) — no
    // device-local tier.
    let config_ds = ensure_dataset(
        c,
        space,
        &cfg_t,
        &json!({
        "key": CONFIG_KEY, "displayName": "Agent Config",
        "idRule": "user", "deleteBy": "anyone", "skipHistory": true,
        "dynamic": true,
        "fields": [
            {"key": "key", "kind": "string", "mutableBy": "any"},
        ]}),
    )?;
    let secrets_ds = ensure_dataset(
        c,
        space,
        &sec_t,
        &json!({
        "key": SECRETS_KEY, "displayName": "Agent Secrets",
        "idRule": "user", "deleteBy": "anyone", "skipHistory": true,
        // A system dataset: declared, not dynamic. The value is
        // ACCOUNT-scoped (synced — ADR-021 §4: any-sync encrypts every
        // change with the space's ACL read key, so a synced field is
        // end-to-end encrypted and the owner-only bao space is the
        // at-rest guarantee); a key entered on any device reaches the
        // device running the agent.
        "fields": [
            {"key": "key", "kind": "string", "mutableBy": "any"},
            {"key": "secret", "kind": "boolean", "mutableBy": "any"},
            {"key": SECRETS_FIELD, "kind": "string", "mutableBy": "any"},
            {"key": "status", "kind": "string", "mutableBy": "any"},
            {"key": "updatedAt", "kind": "datetime", "mutableBy": "any"},
            {"key": "label", "kind": "string", "mutableBy": "any"},
            {"key": "hosts", "kind": "array", "mutableBy": "any"},
            {"key": "help", "kind": "string", "mutableBy": "any"},
            {"key": "note", "kind": "string", "mutableBy": "any"},
            {"key": "requestedBy", "kind": "string", "mutableBy": "any"},
            {"key": "requestedIn", "kind": "string", "mutableBy": "any"},
            {"key": "requestedAt", "kind": "datetime", "mutableBy": "any"},
            {"key": "rejectedAt", "kind": "datetime", "mutableBy": "any"},
            {"key": "rejectedWith", "kind": "number", "mutableBy": "any"},
            // ADR-021 §8.4: the audit stamp, once per run per ref
            {"key": "lastUsedAt", "kind": "datetime", "mutableBy": "any"},
            // non-secret OAuth metadata rows (`.granted_scopes`, `.account`,
            // the bundled `.client_id`/`.client_secret`): `{value}`
            {"key": "meta", "kind": "object", "mutableBy": "any"},
        ]}),
    )?;
    let triggers_ds = ensure_dataset(
        c,
        space,
        &trg_t,
        &json!({
            "key": TRIGGERS_KEY, "displayName": "Agent Triggers",
            "idRule": "user", "deleteBy": "anyone", "skipHistory": true,
            "dynamic": true, "fields": []}),
    )?;
    let runs_ds = ensure_dataset(
        c,
        space,
        &trg_t,
        &json!({
            "key": RUNS_KEY, "displayName": "Agent Runs",
            "idRule": "user", "deleteBy": "anyone", "skipHistory": true,
            "dynamic": true, "fields": []}),
    )?;
    let stores = AgentStores {
        config: bundle_child_retry(c, space, "bao/v1", "bao/config/v1", &[&cfg_t])?,
        secrets: bundle_child_retry(c, space, "bao/v1", "bao/secrets/v1", &[&sec_t])?,
        triggers: bundle_child_retry(c, space, "bao/v1", "bao/triggers/v1", &[&trg_t])?,
        runs: bundle_child_retry(c, space, "bao/v1", "bao/runs/v1", &[&trg_t])?,
        config_ds,
        secrets_ds,
        triggers_ds,
        runs_ds,
    };
    // Display names only — every consumer resolves these anchors by
    // bundle seed, never by name (ADR-017 §0), but a derived object
    // materializes nameless and is illegible in the UI (and a nameless
    // triggers anchor has already baited an agent into minting a
    // name-discoverable duplicate the trigger loop never reads).
    for (id, name) in [
        (&stores.config, "agent-config"),
        (&stores.secrets, "agent-secrets"),
        (&stores.triggers, "agent-triggers"),
        (&stores.runs, "agent-runs"),
    ] {
        ensure_child_name(c, space, id, name)?;
    }
    Ok(stores)
}

/// Idempotent display-name stamp: read first so a no-op boot appends
/// no change to the child's tree.
fn ensure_child_name(c: &Client, space: &str, object_id: &str, want: &str) -> Result<()> {
    let current = c
        .get_properties(space, object_id)
        .with_context(|| format!("read child properties {object_id}"))?
        .pointer("/record/any/name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    if current == want {
        return Ok(());
    }
    c.set_properties(space, object_id, "any", &json!({"name": want}))
        .with_context(|| format!("name child {object_id}"))?;
    Ok(())
}

/// The Anthropic API key's config key — the record id/`key` on the config
/// object AND the `secrets` map ref the llm effect resolves (config
/// defaults declare `api_key_ref: "llm.key.anthropic"`).
const ANTHROPIC_SECRET_REF: &str = "llm.key.anthropic";

/// The host stores' dataset KEYS (ADR-027 §2) — the part/dataset key
/// on each store's type. Records live in the collection the
/// declaration reports (`<typeId>_<key>`, `AgentStores::*_ds`); no
/// read or write names a key on the wire.
const CONFIG_KEY: &str = "agent_config";
/// The secrets store's key + its value field: the dedicated home of
/// account-scoped secrets (ADR-021 §4), split out of `agent_config` so
/// the broker can block guest reads of the whole collection (by the
/// `_agent_secrets` suffix, ADR-011 §4) and of the object while the
/// config object stays guest-readable.
const SECRETS_KEY: &str = "agent_secrets";
const SECRETS_FIELD: &str = "value";
const TRIGGERS_KEY: &str = "agent_triggers";
const RUNS_KEY: &str = "agent_runs";

/// Config seeding (ADR-006 §3) — the seed passes of
/// [`bootstrap_secrets`], over the `agent_config` store (one `{key,
/// value}` row per dotted key). Nothing is loaded into the host: the
/// store is read through on every `config.get`.
///
/// 1. HARD seeds (`Config::config_overrides` — the config file's
///    `[config]` table / serve's `--config`): write-through — a stored
///    value that differs is overwritten. The per-rig lever.
/// 2. SOFT seeds (`config_defaults.json`): persisted only for keys with
///    no row — fresh-space defaults, never rewriting an existing space.
///
/// Best-effort: a failed query or write warns and serve goes on (a
/// missing row then fails its `config.get` loudly, per key).
fn bootstrap_config(
    c: &Client,
    space: &str,
    obj: &str,
    dataset: &str,
    hard: &BTreeMap<String, Value>,
) {
    let rows = match c.query(space, obj, dataset, &json!({})) {
        Ok(rows) => rows,
        Err(e) => {
            warn!("config store unavailable at seed time ({e})");
            return;
        }
    };
    let stored: BTreeMap<String, Value> = rows
        .iter()
        .filter_map(|r| {
            let k = r.get("key")?.as_str()?.to_string();
            Some((k, r.get("value")?.clone()))
        })
        .collect();
    for (key, value) in hard {
        if stored.get(key) == Some(value) {
            continue;
        }
        match upsert_config_row(c, space, obj, dataset, key, value) {
            Ok(()) if stored.contains_key(key) => info!("config: {key} overwritten (hard seed)"),
            Ok(()) => info!("config: {key} bootstrapped to store (hard seed)"),
            Err(e) => warn!("config: could not persist {key} ({e})"),
        }
    }
    for (key, value) in crate::config::config_defaults() {
        if hard.contains_key(&key) || stored.contains_key(&key) {
            continue;
        }
        match upsert_config_row(c, space, obj, dataset, &key, &value) {
            Ok(()) => info!("config: {key} bootstrapped to store (default)"),
            Err(e) => warn!("config: could not persist {key} ({e})"),
        }
    }
}

/// One synced upsert of a config row — per-path ops, no
/// read-merge-write (a concurrent UI save is never clobbered).
fn upsert_config_row(
    c: &Client,
    space: &str,
    obj: &str,
    dataset: &str,
    key: &str,
    value: &Value,
) -> Result<()> {
    c.modify(
        space,
        &json!({
            "objectId": obj, "dataset": dataset,
            "records": [{"id": key, "upsert": true, "ops": [
                {"type": "$set", "path": "key", "value": key},
                {"type": "$set", "path": "value", "value": value}]}]}),
    )?;
    Ok(())
}

/// The serve-side agent-config store (ADR-006 §3): the config child's
/// `agent_config` dataset, read through per `config.get`, written per
/// `config.set`. One per serve, threaded into every Broker; no cache.
pub struct ServeConfigStore {
    client: Arc<Client>,
    space: String,
    obj: String,
    /// the `agent_config` collection (resolved at provisioning)
    dataset: String,
}

impl ServeConfigStore {
    pub fn new(client: Arc<Client>, space: &str, obj: &str, dataset: &str) -> Self {
        Self {
            client,
            space: space.to_string(),
            obj: obj.to_string(),
            dataset: dataset.to_string(),
        }
    }
}

impl crate::broker::ConfigStore for ServeConfigStore {
    fn read(&self, key: &str) -> Result<Option<Value>, String> {
        let rows = self
            .client
            .query(
                &self.space,
                &self.obj,
                &self.dataset,
                &json!({"filter": {"key": key}}),
            )
            .map_err(|e| e.to_string())?;
        Ok(rows
            .iter()
            .find(|r| r.get("key").and_then(|v| v.as_str()) == Some(key))
            .and_then(|r| r.get("value").cloned()))
    }

    fn set(&self, key: &str, value: &Value) -> Result<(), String> {
        upsert_config_row(
            &self.client,
            &self.space,
            &self.obj,
            &self.dataset,
            key,
            value,
        )
        .map_err(|e| e.to_string())
    }
}

/// The `anyrt run --from-space` store binding (ADR-004 §6 parity):
/// the space's `agent_config` rows read through and written exactly
/// like serve, so a one-shot run reads the tiers the serve reads and
/// `config.set` lands where the serve will see it. The run's own
/// `--config` keys shadow READS only — an explicit per-run override
/// never rewrites the space's rows (a scratch run is not a serve).
///
/// `None` = the space carries no `bao/v1` bundle (locked registry
/// read: not a bao space, or one never served) — nothing is
/// provisioned from a run; the broker's seeds map stands in and
/// `config.set` is refused. With the row present, store resolution is
/// serve's own `provision_agent_stores` (adopt-or-install is a local
/// read on a provisioned space).
pub fn run_config_store(
    client: &Arc<Client>,
    space: &str,
    overrides: BTreeMap<String, Value>,
) -> Result<Option<Arc<dyn crate::broker::ConfigStore>>> {
    let reg = client
        .list_bundles(space)
        .context("bundles registry (run --from-space config store)")?;
    let has_bao = reg["bundles"]
        .as_array()
        .map(|rows| rows.iter().any(|b| b["id"] == json!("bao/v1")))
        .unwrap_or(false);
    if !has_bao {
        return Ok(None);
    }
    let stores = provision_agent_stores(client, space)?;
    Ok(Some(Arc::new(ShadowedConfigStore {
        inner: ServeConfigStore::new(client.clone(), space, &stores.config, &stores.config_ds),
        overrides,
    })))
}

/// A config store with a read-only shadow in front: keys in
/// `overrides` answer from the map, everything else reads through;
/// every write goes to the inner store.
pub struct ShadowedConfigStore {
    inner: ServeConfigStore,
    overrides: BTreeMap<String, Value>,
}

impl crate::broker::ConfigStore for ShadowedConfigStore {
    fn read(&self, key: &str) -> Result<Option<Value>, String> {
        if let Some(v) = self.overrides.get(key) {
            return Ok(Some(v.clone()));
        }
        crate::broker::ConfigStore::read(&self.inner, key)
    }

    fn set(&self, key: &str, value: &Value) -> Result<(), String> {
        crate::broker::ConfigStore::set(&self.inner, key, value)
    }
}

/// Secret persistence (ADR-021 §4). Config secrets (the provider API
/// keys and any other ref) live as the account-scoped `value` of their
/// `agent_secrets` row — synced end-to-end encrypted, owner-only space.
/// On serve start, three passes:
///
/// 1. HARD seeds (`Config::secret_overrides` — the `.connectors.env`
///    file or an embedder's in-memory feed; open ref set): persisted
///    write-through — a stored value that differs is ROTATED, an empty
///    override DELETES the stored secret. This is the rotation path;
///    env vars never rotate.
/// 2. Stored values (every secret-marked record) load
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
) -> Vec<String> {
    let rows = c.query(space, obj, dataset, &json!({})).unwrap_or_default();
    // refs whose value could NOT be written to the store: they must
    // survive in the map for this run (the only place they exist)
    let mut unpersisted = Vec::new();

    // 1. Hard seeds: write-through, open ref set, empty deletes.
    for (secret_ref, value) in overrides {
        let stored = stored_local_secret(&rows, secret_ref, field);
        if value.is_empty() {
            secrets.remove(secret_ref);
            if stored.is_some() {
                match persist_local_secret(c, space, obj, dataset, field, secret_ref, "") {
                    Ok(()) => info!("config: {secret_ref} removed from store"),
                    Err(e) => warn!("config: could not remove {secret_ref} ({e})"),
                }
            }
            continue;
        }
        secrets.insert(secret_ref.clone(), value.clone());
        match stored {
            Some(ref s) if s == value => {
                info!("config: {secret_ref} loaded from store");
                stamp_set_if_needed(c, space, obj, dataset, &rows, secret_ref);
            }
            other => match persist_local_secret(c, space, obj, dataset, field, secret_ref, value) {
                Ok(()) if other.is_some() => info!("config: {secret_ref} rotated (hard seed)"),
                Ok(()) => info!("config: {secret_ref} bootstrapped to store"),
                Err(e) => {
                    warn!(
                        "config: could not persist {secret_ref} ({e}); \
                         using the seeded value this run"
                    );
                    unpersisted.push(secret_ref.to_string());
                }
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
            info!("config: {secret_ref} loaded from store");
            stamp_set_if_needed(c, space, obj, dataset, &rows, secret_ref);
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
            Ok(()) => info!("config: {secret_ref} bootstrapped to store"),
            Err(e) => {
                warn!(
                    "config: could not persist {secret_ref} ({e}); \
                         using the seeded value this run"
                );
                unpersisted.push(secret_ref.to_string());
            }
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
    unpersisted
}

/// A stored row that predates ADR-021 (or was written by a seed
/// without touching metadata) gets its `status: "set"` stamp at boot.
fn stamp_set_if_needed(
    c: &Client,
    space: &str,
    obj: &str,
    dataset: &str,
    rows: &[Value],
    key: &str,
) {
    let status = rows
        .iter()
        .find(|r| r.get("key").and_then(|v| v.as_str()) == Some(key))
        .and_then(|r| r.get("status"))
        .and_then(|v| v.as_str());
    if status == Some("set") {
        return;
    }
    let patch = json!({"status": "set", "updatedAt": {"$date": now_rfc3339()},
                       "requestedIn": Value::Null});
    if let Err(e) = upsert_secret_row(c, space, obj, dataset, key, &patch) {
        warn!("config: could not stamp {key} set ({e})");
    }
}

/// The stored secret value for `key` from already-queried rows
/// (None when unset/empty).
fn stored_local_secret(rows: &[Value], key: &str, field: &str) -> Option<String> {
    rows.iter()
        .find(|r| r.get("key").and_then(|v| v.as_str()) == Some(key))
        .and_then(|r| r.get(field).and_then(|v| v.as_str()))
        .map(str::to_string)
        .filter(|s| !s.is_empty())
}

/// One synced per-path write (ADR-021 §4): metadata stamps + the value.
/// The value is account-scoped — end-to-end encrypted by any-sync, never
/// in a trace (resolved after recording), never guest-readable (the
/// read-guard covers the whole object).
fn persist_local_secret(
    c: &Client,
    space: &str,
    obj: &str,
    dataset: &str,
    field: &str,
    key: &str,
    secret: &str,
) -> Result<()> {
    let status = if secret.is_empty() { "missing" } else { "set" };
    upsert_secret_row(
        c,
        space,
        obj,
        dataset,
        key,
        &json!({"status": status, "updatedAt": {"$date": now_rfc3339()},
                "requestedIn": Value::Null, field: secret}),
    )
}

/// Stamp `patch` onto the ref's synced row as per-path ops (`$set`,
/// null = `$unset`), upserting the record — no read-merge-write, so a
/// concurrent UI save is never clobbered by a stamp built on a stale
/// read. `key`/`secret` ride along on every write (the row may be new);
/// the device-local value is never touched (local scope, separate
/// write). ADR-021 §2/§4.
fn upsert_secret_row(
    c: &Client,
    space: &str,
    obj: &str,
    dataset: &str,
    key: &str,
    patch: &Value,
) -> Result<()> {
    let mut ops = vec![
        json!({"type": "$set", "path": "key", "value": key}),
        json!({"type": "$set", "path": "secret", "value": true}),
    ];
    if let Some(p) = patch.as_object() {
        for (k, v) in p {
            if v.is_null() {
                ops.push(json!({"type": "$unset", "path": k}));
            } else {
                ops.push(json!({"type": "$set", "path": k, "value": v}));
            }
        }
    }
    let reply = c.modify(
        space,
        &json!({
            "objectId": obj, "dataset": dataset,
            "records": [{"id": key, "upsert": true, "ops": ops}]}),
    )?;
    // a 200 can still carry per-record rejections (a tombstoned id, an
    // undeclared field) — the row did not change, say so
    if let Some(rej) = reply["rejections"].as_array().filter(|r| !r.is_empty()) {
        let reason = rej[0]["reason"]
            .as_str()
            .or_else(|| rej[0]["code"].as_str())
            .unwrap_or("rejected");
        anyhow::bail!("modify rejected {key}: {reason}");
    }
    Ok(())
}

/// The patch a miss/rejection writes (ADR-021 §8.4): the status base
/// always; the descriptor only when the row does not exist yet.
fn descriptor_patch(exists: bool, about: &Value, base: Value) -> Value {
    let mut patch = base.as_object().cloned().unwrap_or_default();
    if !exists {
        for k in ["label", "hosts", "help", "note"] {
            if let Some(v) = about.get(k).filter(|v| !v.is_null()) {
                patch.insert(k.into(), v.clone());
            }
        }
    }
    Value::Object(patch)
}

/// ADR-021 §8.1: the declared table — every `__any_credentials__` entry
/// of every program in the configured overlay spaces, read off the
/// program objects' `credentials` property (deploy-written, guest-
/// unwritable). Refreshed at boot and, rate-limited, on a miss.
pub struct DeclaredTable {
    client: Arc<Client>,
    spaces: Vec<String>,
    table: Mutex<BTreeMap<String, Value>>,
    refreshed: Mutex<Option<std::time::Instant>>,
}

impl DeclaredTable {
    const MISS_REFRESH_EVERY: Duration = Duration::from_secs(10);

    pub fn new(client: Arc<Client>, spaces: Vec<String>) -> Self {
        DeclaredTable {
            client,
            spaces,
            table: Mutex::new(BTreeMap::new()),
            refreshed: Mutex::new(None),
        }
    }

    /// Re-read every overlay's declarations. A space that is not synced
    /// yet (no program type) contributes nothing until it is. Two
    /// programs declaring one ref must agree; the first wins and a
    /// conflict is logged.
    pub fn refresh(&self) -> usize {
        let mut fresh: BTreeMap<String, Value> = BTreeMap::new();
        for space in &self.spaces {
            let schema = match crate::program_schema::ProgramSchema::lookup(&self.client, space) {
                Ok(Some(s)) => s,
                Ok(None) => continue,
                Err(e) => {
                    warn!("credentials: cannot read programs in {space} ({e})");
                    continue;
                }
            };
            if schema.credentials_prop().is_none() {
                continue; // deployed before ADR-021 §8.1: declares nothing
            }
            let rows = match self.client.query_objects(
                space,
                &json!({"filter": {"any.types": schema.type_id}, "limit": 500}),
            ) {
                Ok(r) => r,
                Err(e) => {
                    warn!("credentials: cannot list programs in {space} ({e})");
                    continue;
                }
            };
            for row in &rows {
                let props = schema.read(row);
                let Some(text) = props.get("credentials").and_then(Value::as_str) else {
                    continue;
                };
                let Ok(Value::Array(list)) = serde_json::from_str::<Value>(text) else {
                    continue;
                };
                for entry in list {
                    let Some(r) = entry.get("ref").and_then(Value::as_str) else {
                        continue;
                    };
                    let about = entry.get("about").cloned().unwrap_or(Value::Null);
                    let program = props.get("name").and_then(Value::as_str).unwrap_or("?");
                    match fresh.get(r) {
                        Some(prev) if *prev != about => warn!(
                            "credentials: {r} declared twice with different descriptors \
                             ({program} keeps the first)"
                        ),
                        Some(_) => {}
                        None => {
                            fresh.insert(r.to_string(), about);
                        }
                    }
                }
            }
        }
        let n = fresh.len();
        *self.table.lock().expect("declared table poisoned") = fresh;
        *self.refreshed.lock().expect("declared table poisoned") = Some(std::time::Instant::now());
        n
    }

    pub fn snapshot(&self) -> BTreeMap<String, Value> {
        self.table.lock().expect("declared table poisoned").clone()
    }
}

impl DeclaredCredentials for DeclaredTable {
    fn about(&self, key: &str) -> Option<Value> {
        if let Some(a) = self.table.lock().expect("declared table poisoned").get(key) {
            return Some(a.clone());
        }
        // a miss may be a redeploy while serve runs (or an overlay that
        // synced after boot): re-read, at most every few seconds
        let stale = self
            .refreshed
            .lock()
            .expect("declared table poisoned")
            .is_none_or(|t| t.elapsed() >= Self::MISS_REFRESH_EVERY);
        if stale {
            self.refresh();
            return self
                .table
                .lock()
                .expect("declared table poisoned")
                .get(key)
                .cloned();
        }
        None
    }
}

/// Boot stamp (ADR-021 §8.1/§8.4): every declared ref's row carries the
/// declared descriptor — the manifest is the authority for label and
/// hosts, so an existing row is overwritten here (and only here); a
/// row that did not exist is created `missing`, so the Credentials
/// dashboard lists every connector's key before any miss.
fn stamp_declared(c: &Client, space: &str, obj: &str, dataset: &str, table: &DeclaredTable) {
    let rows = c.query(space, obj, dataset, &json!({})).unwrap_or_default();
    let mut stamped = 0;
    for (key, about) in table.snapshot() {
        let existing = rows
            .iter()
            .find(|r| r.get("key").and_then(Value::as_str) == Some(key.as_str()));
        let mut patch = Map::new();
        for k in ["label", "hosts", "help", "note"] {
            match about.get(k).filter(|v| !v.is_null()) {
                Some(v) => {
                    patch.insert(k.into(), v.clone());
                }
                None if existing.is_some_and(|r| r.get(k).is_some()) => {
                    patch.insert(k.into(), Value::Null); // dropped from the declaration
                }
                None => {}
            }
        }
        if existing.is_none() {
            patch.insert("status".into(), json!("missing"));
        }
        match upsert_secret_row(c, space, obj, dataset, &key, &Value::Object(patch)) {
            Ok(()) => stamped += 1,
            Err(e) => warn!("credentials: could not stamp declared {key} ({e})"),
        }
    }
    info!("credentials: {stamped} declared refs stamped from the overlays");
}

fn now_rfc3339() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

/// The serve-side secret write path for managed OAuth (ADR-011 §3) —
/// the same two-step local-scope write the bootstrap uses, plus the
/// synced non-secret metadata record.
pub struct ServeSecretStore {
    client: Arc<Client>,
    space: String,
    obj: String,
    /// the `agent_secrets` collection (resolved at provisioning)
    dataset: String,
}

impl crate::broker::SecretSource for ServeSecretStore {
    fn read(&self, key: &str) -> Result<Option<String>, String> {
        let rows = self
            .client
            .query(&self.space, &self.obj, &self.dataset, &json!({}))
            .map_err(|e| e.to_string())?;
        Ok(stored_local_secret(&rows, key, SECRETS_FIELD))
    }

    fn mark_missing(&self, key: &str, about: &Value, run_id: &str) {
        self.stamp(
            key,
            about,
            json!({"status": "missing", "requestedBy": run_id}),
        );
    }

    fn mark_rejected(&self, key: &str, about: &Value, run_id: &str, http_status: u16) {
        self.stamp(
            key,
            about,
            // a rejection is a new event: the previous bubble (a
            // "missing" card, or an older rejection) no longer covers
            // it — clearing requestedIn lets the wrapper re-ask
            json!({"status": "rejected", "requestedBy": run_id,
                   "rejectedAt": {"$date": now_rfc3339()}, "rejectedWith": http_status,
                   "requestedIn": Value::Null}),
        );
    }

    fn read_row(&self, key: &str) -> Result<Option<Value>, String> {
        let rows = self
            .client
            .query(&self.space, &self.obj, &self.dataset, &json!({}))
            .map_err(|e| e.to_string())?;
        Ok(rows
            .into_iter()
            .find(|r| r.get("key").and_then(Value::as_str) == Some(key)))
    }

    fn mark_used(&self, key: &str) {
        let patch = json!({"lastUsedAt": {"$date": now_rfc3339()}});
        if let Err(e) = upsert_secret_row(
            &self.client,
            &self.space,
            &self.obj,
            &self.dataset,
            key,
            &patch,
        ) {
            warn!("secrets: could not stamp lastUsedAt for {key} ({e})");
        }
    }
}

impl ServeSecretStore {
    /// ADR-021 §8.4 stamp rule: descriptor fields (`label`, `hosts`,
    /// `help`, `note`) are written only when this stamp CREATES the row
    /// — never onto an existing one, so a later miss or a 401 cannot
    /// move where the secret goes; status fields stamp as before.
    fn stamp(&self, key: &str, about: &Value, base: Value) {
        let exists = self.read_row(key).ok().flatten().is_some();
        let patch = descriptor_patch(exists, about, base);
        if let Err(e) = upsert_secret_row(
            &self.client,
            &self.space,
            &self.obj,
            &self.dataset,
            key,
            &patch,
        ) {
            warn!("secrets: could not stamp {key} ({e})");
        }
    }
}

impl crate::oauth::SecretPersist for ServeSecretStore {
    fn persist_secret(&self, key: &str, value: &str) -> Result<()> {
        persist_local_secret(
            &self.client,
            &self.space,
            &self.obj,
            &self.dataset,
            SECRETS_FIELD,
            key,
            value,
        )
    }

    fn persist_meta(&self, key: &str, value: &Value) -> Result<()> {
        let mut ops = vec![
            json!({"type": "$set", "path": "key", "value": key}),
            json!({"type": "$set", "path": "secret", "value": false}),
        ];
        ops.push(if value.is_null() {
            json!({"type": "$unset", "path": "meta"})
        } else {
            json!({"type": "$set", "path": "meta", "value": {"value": value}})
        });
        self.client.modify(
            &self.space,
            &json!({"objectId": self.obj, "dataset": self.dataset,
                    "records": [{"id": key, "upsert": true, "ops": ops}]}),
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
    backlog: Mutex<Vec<ChatInput>>,
    /// Live event sources (ADR-018 §2): `(space, chat object id)` → the
    /// stop flag of the thread watching it. Converged on the registry
    /// every tick.
    event_sources: Mutex<BTreeMap<(String, String), Arc<AtomicBool>>>,
    /// The chat-responder record id (ADR-018 §3) — `chat-watch`, or a
    /// generation-suffixed reseed when the bare id was tombstoned.
    chat_watch_id: String,
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

// --- bao presence beats (ADR-025) ------------------------------------------------
//
// Layer 1 only — deterministic, never model-generated (§1). Serve is
// the SOLE publisher of `bao.status`: a beat every `STATUS_BEAT_S`
// (and immediately when the guest sets a status line — the generation
// poll below), a full-state idempotent envelope on the at-most-once
// bus. The UI marks offline after 3 missed beats (30s, its clock); a
// graceful shutdown publishes one `shutdown` beat for instant
// offline, a crash is covered by the beat TTL.

/// Beat cadence; the UI pairs it with a 3-missed-beats offline cutoff.
pub const STATUS_BEAT_S: f64 = 10.0;
/// The presence thread's poll: how fast a `bao.status` set republishes.
const STATUS_POLL_S: f64 = 1.0;

/// The freshest in-flight run's stamp — presence derives `working`
/// from `RunCtx::live_runs`, the SAME registry `/break` resolves
/// against (one source of truth for what's running, never a parallel
/// counter). Freshest by `startedAt`; stampless entries (only the
/// watcher's chat-keyed overlay has those, and it never lands here)
/// are skipped defensively.
fn current_run(runs: &BTreeMap<String, crate::triggers::LiveRun>) -> Option<Value> {
    runs.values()
        .filter(|l| l.stamp.is_object())
        .max_by(|a, b| {
            let at = |l: &&crate::triggers::LiveRun| l.stamp["startedAt"].as_f64().unwrap_or(0.0);
            at(a).total_cmp(&at(b))
        })
        .map(|l| {
            // live activity rides the stamp (ADR-025 §1): tool calls so
            // far + the newest cell's preview — what bao is doing NOW
            let mut run = l.stamp.clone();
            run["cells"] = json!(l.activity.cells());
            if let Some(preview) = l.activity.preview() {
                run["cell"] = json!(preview);
            }
            run
        })
}

/// What a beat says about this device (ADR-025 §1 `role`/`winner`).
/// `responder` becomes `role`: does this device OWN the enabled
/// chat-watch record — the check the chat watch itself connects on
/// (ADR-018 §3) — so `role: active` means "chat is answered here",
/// not "won the election": a paused or repinned responder leaves the
/// election winner beating while nothing answers, which is the
/// BOB-111 symptom. `winner` is the election's claim holder (ADR-015
/// §2), for naming the device that holds bao when this one does not.
#[derive(Clone, Debug, PartialEq, Eq)]
struct BeatRole {
    responder: bool,
    /// None when no claim exists, or the election is disabled/pruned
    winner: Option<String>,
}

/// The role a beat carries right now, read against a registry guard
/// the caller already holds (the control API keeps it across a
/// request) — the responder check is `answers_chat`'s.
fn beat_role(shared: &Shared, reg: &BTreeMap<String, Trigger>, ctx: &RunCtx) -> BeatRole {
    let instance = shared.scheduler.lock().unwrap().instance_id().to_string();
    BeatRole {
        responder: owned_chat_watch(reg, &instance).is_some(),
        winner: ctx.verdict.lock().unwrap().winner.clone(),
    }
}

/// Why chat is not answered on this device, for the change-gated log
/// line (ADR-015 §5): every transition of chat ownership is in
/// `agent.log`, so an unanswered chat is diagnosable from the log
/// alone without the log repeating itself.
fn not_answering_line(ctx: &RunCtx) -> String {
    if ctx.pruned {
        return "election: standby (this device was pruned from the registry) — \
                nothing runs here"
            .into();
    }
    let verdict = ctx.verdict.lock().unwrap().clone();
    if verdict.active {
        return "chat: this device is the active bao but does not own the enabled \
                chat responder (paused, or pinned to another device) — \
                chat is not answered here"
            .into();
    }
    format!(
        "election: {} — chat is not answered here",
        verdict.describe()
    )
}

/// The line for the flip back: this device answers chat again.
const ANSWERING_LINE: &str = "chat: answered here — this device owns the enabled chat responder";

/// The log's change gate (ADR-015 §5): one line when the reason this
/// device does not answer chat changes (standby naming a winner, the
/// winner moving, pruned, active-but-not-the-responder), one when it
/// answers again; nothing while the state holds. The first
/// observation is the baseline and is not logged — the boot line
/// already said it.
#[derive(Default)]
struct NotAnsweringLog {
    /// the last observed reason; `""` = answering; `None` = unobserved
    last: Option<String>,
}

impl NotAnsweringLog {
    /// `reason` is why chat is not answered here, `None` while it is.
    /// Returns the line to log, when the state changed.
    fn note(&mut self, reason: Option<String>) -> Option<String> {
        let now = reason.unwrap_or_default();
        let changed = match &self.last {
            None => false,
            Some(last) => *last != now,
        };
        self.last = Some(now.clone());
        if !changed {
            return None;
        }
        Some(if now.is_empty() {
            ANSWERING_LINE.to_string()
        } else {
            now
        })
    }
}

/// The full-state beat envelope (ADR-025 §1). `state` ∈ boot | idle |
/// working | shutdown; `role` ∈ active | standby with `winner` beside
/// it when the registry names one; `run` rides only while working
/// (the caller passes `current_run`), the line only while fresh
/// (decay: `PresenceState::line`). Timestamps are unix seconds
/// (staleness math is the consumer's job, same as `AgentTypingRow`'s
/// `sinceSec`).
fn status_envelope(
    identity: &str,
    status: &PresenceState,
    run: Option<Value>,
    state: &str,
    role: &BeatRole,
    now: f64,
) -> Value {
    let mut data = json!({
        "identity": identity,
        "state": state,
        "role": if role.responder { "active" } else { "standby" },
    });
    if let Some(w) = &role.winner {
        data["winner"] = json!(w);
    }
    if let Some(run) = run {
        data["run"] = run;
    }
    if let Some((line, at)) = status.line(now) {
        data["line"] = json!(line);
        data["lineAt"] = json!(at);
    }
    json!({
        "type": "bao.status",
        "scope": "account",
        // the serving peer id (ADR-015 identity) — filterable + the
        // multi-device dedup key (ADR-025 §6)
        "target": identity,
        "data": data,
    })
}

/// Publish one beat at `now` (threaded for the fake-clock sequence
/// tests). Best-effort like every bus write: a failed beat is logged
/// noise, never a serve failure (the next one covers it).
fn publish_status_beat(
    client: &Client,
    identity: &str,
    status: &PresenceState,
    run: Option<Value>,
    state: &str,
    role: &BeatRole,
    now: f64,
) {
    if let Err(e) = client.publish_event(&status_envelope(identity, status, run, state, role, now))
    {
        warn!("bao.status beat not published: {e}");
    }
}

/// The presence loop's carried state — split out so a pass is
/// unit-testable without real sleeps.
#[derive(Default)]
struct PresenceLoop {
    last_beat: Option<f64>,
    last_sig: String,
    booted: bool,
}

/// What makes a beat DUE besides cadence: any change in what the beat
/// would say — the line generation, which run is live, how far it
/// has come, and the role/winner. Run start/end, every new tool
/// call, a responder flip (takeover, stand-down, pause, repin) and a
/// winner change republish within a poll (~1s),
/// which is what keeps the UI's working/idle flip, the call counter
/// and the role live instead of up to a beat behind (ADR-025 §2).
fn presence_sig(status: &PresenceState, run: Option<&Value>, role: &BeatRole) -> String {
    format!(
        "{}|{}|{}|{}|{}",
        status.line_gen(),
        run.and_then(|r| r["id"].as_str()).unwrap_or(""),
        run.and_then(|r| r["cells"].as_u64()).unwrap_or(0),
        role.responder,
        role.winner.as_deref().unwrap_or(""),
    )
}

/// One poll pass: publish a beat if due (boot, cadence, or a change —
/// line set, run start/end, tool call, role flip). Returns true when a
/// beat went out.
fn presence_pass(
    client: &Client,
    identity: &str,
    status: &PresenceState,
    run: Option<Value>,
    role: &BeatRole,
    st: &mut PresenceLoop,
    now: f64,
) -> bool {
    let sig = presence_sig(status, run.as_ref(), role);
    let due = st.last_beat.is_none()
        || now - st.last_beat.unwrap() >= STATUS_BEAT_S
        || sig != st.last_sig;
    if !due {
        return false;
    }
    // boot once, then working/idle off the live-run registry (ADR-025 §1)
    let state = if !st.booted {
        "boot"
    } else if run.is_some() {
        "working"
    } else {
        "idle"
    };
    publish_status_beat(client, identity, status, run, state, role, now);
    st.last_beat = Some(now);
    st.last_sig = sig;
    st.booted = true;
    true
}

/// The presence thread: beats while serve lives, one `shutdown` beat
/// on stop (the graceful path; a crash is covered by the beat TTL).
/// Also the home of the chat-ownership log line (ADR-015 §5): it
/// reads the same role the beat carries, so a flip still lands in the
/// log while registry reads fail and on a pruned device (no election
/// thread there).
fn presence_thread(
    shared: Arc<Shared>,
    ctx: Arc<RunCtx>,
    stop: Arc<AtomicBool>,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let mut st = PresenceLoop::default();
        let mut answering = NotAnsweringLog::default();
        let role_now = || {
            let reg = shared.triggers.lock().unwrap();
            beat_role(&shared, &reg, &ctx)
        };
        loop {
            sliced_sleep(Duration::from_secs_f64(STATUS_POLL_S), &stop);
            if stop.load(Ordering::Relaxed) {
                // the graceful-offline beat (ADR-025 §2)
                publish_status_beat(
                    &ctx.client,
                    &presence_identity(&ctx),
                    &ctx.status,
                    None,
                    "shutdown",
                    &role_now(),
                    now_s(),
                );
                return;
            }
            let now = now_s();
            let role = role_now();
            let reason = (!role.responder).then(|| not_answering_line(&ctx));
            if let Some(line) = answering.note(reason) {
                info!("{line}");
            }
            let run = current_run(&ctx.live_runs.lock().unwrap());
            presence_pass(
                &ctx.client,
                &presence_identity(&ctx),
                &ctx.status,
                run,
                &role,
                &mut st,
                now,
            );
        }
    })
}

/// This serve's publisher identity: the election peer id when the
/// server has one (ADR-015), else the process-stamped fallback —
/// same value the UI dedups multi-device beats by (ADR-025 §6).
fn presence_identity(ctx: &RunCtx) -> String {
    ctx.self_peer
        .clone()
        .unwrap_or_else(|| format!("anyrt-{}", std::process::id()))
}

/// The run stamp for `working` beats (ADR-025 §1 `run`): the freshest
/// run's id + a deterministic live title — bao's own message on chat
/// runs, else the program spec. The ADR-023 summary title only exists
/// at dump; this is what beats can say while the run is live.
fn run_stamp(run_id: &str, spec: &str, args: &Value) -> Value {
    let title = args["userText"]
        .as_str()
        .map(str::trim)
        .filter(|t| !t.is_empty())
        .map(preview)
        .unwrap_or_else(|| spec.to_string());
    json!({"id": run_id, "title": title, "startedAt": now_s()})
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
    /// The lib embedder's secret write (ADR-021 §4): the same two-step
    /// row write the boot seeds use — no restart, the next credentialed
    /// effect reads the row. Empty `value` deletes. Errors on a server
    /// without the secrets object (seeds-only mode has nothing to
    /// write to) and for managed OAuth sub-refs, which have their own
    /// custody (ADR-011).
    pub fn set_secret(&self, key: &str, value: &str) -> Result<()> {
        if key.starts_with(crate::oauth::OAUTH_REF_PREFIX) && key.ends_with(".refresh") {
            anyhow::bail!("{key} is a managed OAuth token — use the provider's connect()");
        }
        let store = self
            .ctx
            .secret_store
            .as_ref()
            .context("no secrets store (server without the agent secrets object)")?;
        persist_local_secret(
            &store.client,
            &store.space,
            &store.obj,
            &store.dataset,
            SECRETS_FIELD,
            key,
            value,
        )
    }

    /// Signal shutdown and join the service threads. Latency is
    /// bounded by the SSE stream: the watcher only observes the flag
    /// on the next frame/heartbeat (or the sliced reconnect sleep).
    pub fn stop(mut self) -> Result<()> {
        self.shutdown.store(true, Ordering::Relaxed);
        let joined = self.join_all();
        // runs are detached and finish on their own; a run mid-command
        // must not leave its child behind when the process goes
        // (ADR-024 §1). A child spawned after this sweep is the
        // PDEATHSIG/SIGPIPE case, not ours.
        #[cfg(feature = "shell")]
        crate::shell::kill_all();
        joined
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
    // Boot is serial, every step below can wait on the server (a
    // registry convergence, a space still syncing), and nothing shows
    // — no control port, no presence beat — until all of it is done.
    // Each step logs its elapsed time so `agent.log` says where a slow
    // boot went (BOB-113).
    let boot = std::time::Instant::now();
    let step = |what: &str| info!("boot: {what} (+{:.1}s)", boot.elapsed().as_secs_f64());
    let client = Arc::new(Client::new(&cfg.addr));
    let space = ensure_space(&client, &cfg.agent_space)?;
    step(&format!("bao space {space}"));
    let chat = general_chat(&client, &space)?;
    step("general chat ready");
    // ADR-017 §0: the bao/v1 bundle + the host-written store children
    // (config, secrets, triggers). The trigger anchor IS the triggers
    // child — deterministic, no name-scan.
    let stores = provision_agent_stores(&client, &space)?;
    step("agent stores provisioned");
    let anchor = stores.triggers.clone();
    let runs_anchor = stores.runs.clone();

    // Config store (ADR-006 §3): seed the config child (hard, then
    // soft), then read through to it — the host keeps no copy.
    let config_obj = Some(stores.config.clone());
    let secrets_obj = Some(stores.secrets.clone());
    let config_store: Option<Arc<ServeConfigStore>> = config_obj.as_ref().map(|obj| {
        let hard = std::mem::take(&mut cfg.config_overrides);
        bootstrap_config(&client, &space, obj, &stores.config_ds, &hard);
        info!("config obj={obj} collection={}", stores.config_ds);
        Arc::new(ServeConfigStore {
            client: client.clone(),
            space: space.clone(),
            obj: obj.clone(),
            dataset: stores.config_ds.clone(),
        })
    });
    // The guest read-guard target — the secrets object, threaded into
    // every run's Broker (sys_http). No migration from the pre-split
    // layout: secrets left on an old config object are ignored, and
    // re-importing an .env is a two-click operation.
    let mut unpersisted = Vec::new();
    let secrets_guard = match &secrets_obj {
        Some(sobj) => {
            let overrides = std::mem::take(&mut cfg.secret_overrides);
            unpersisted = bootstrap_secrets(
                &client,
                &space,
                sobj,
                &stores.secrets_ds,
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
    let secret_store: Option<Arc<ServeSecretStore>> = secrets_obj.as_ref().map(|obj| {
        Arc::new(ServeSecretStore {
            client: client.clone(),
            space: space.clone(),
            obj: obj.clone(),
            dataset: stores.secrets_ds.clone(),
        })
    });
    // The declared table (ADR-021 §8.1): what the overlays' programs say
    // they send, read off deploy-written objects; the rows of every
    // declared ref carry that descriptor from boot on (§8.4)
    let declared = Arc::new(DeclaredTable::new(
        client.clone(),
        cfg.overlays.values().map(|o| o.space.clone()).collect(),
    ));
    let n_declared = declared.refresh();
    step(&format!("credentials declared ({n_declared} refs)"));
    if let Some(sobj) = &secrets_obj {
        stamp_declared(&client, &space, sobj, &stores.secrets_ds, &declared);
    }
    let persist: Option<Box<dyn crate::oauth::SecretPersist>> = secrets_obj.as_ref().map(|obj| {
        Box::new(ServeSecretStore {
            client: client.clone(),
            space: space.clone(),
            obj: obj.clone(),
            dataset: stores.secrets_ds.clone(),
        }) as Box<dyn crate::oauth::SecretPersist>
    });
    let shutdown = Arc::new(AtomicBool::new(false));
    let mut oauth_state = crate::oauth::OauthState::new(crate::oauth::builtin_providers(), persist);
    oauth_state.consent = cfg.consent_hook.clone();
    oauth_state.shutdown = shutdown.clone();
    oauth_state.source = secret_store
        .clone()
        .map(|s| s as Arc<dyn crate::broker::SecretSource>);
    let oauth = Arc::new(oauth_state);
    oauth.seed(&mut cfg.secrets);
    // ADR-021 §4: with a store, the row is the credential and brokers
    // read it at injection time — the seeded map is dropped so no
    // per-run snapshot exists to go stale. Without a store the map is
    // the documented fallback.
    if secret_store.is_some() {
        // seeds the store refused keep living in the map — the
        // broker's fallback after a store miss
        cfg.secrets.retain(|k, _| unpersisted.contains(k));
    }
    // grant metadata (.granted_scopes/.account — synced, non-secret)
    // loads back so oauth.status survives a restart; best-effort
    if let Some(sobj) = &secrets_obj {
        if let Ok(rows) = client.query(&space, sobj, &stores.secrets_ds, &json!({})) {
            for row in &rows {
                let Some(k) = row.get("key").and_then(|v| v.as_str()) else {
                    continue;
                };
                if k.starts_with(crate::oauth::OAUTH_REF_PREFIX)
                    && (k.ends_with(".granted_scopes")
                        || k.ends_with(".account")
                        || k.ends_with(".client_id")
                        || k.ends_with(".client_secret"))
                {
                    if let Some(v) = row
                        .get("meta")
                        .and_then(|m| m.get("value"))
                        .filter(|v| !v.is_null())
                    {
                        oauth.seed_meta(k, v.clone());
                    }
                }
            }
        }
    }

    step("config + secrets seeded");

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
    step(&format!("overlays probed ({} pending)", pending.len()));
    let aliases = alias_map(&cfg.overlays, &space);
    let code_space = aliases["agent"].clone();
    // runtime wiring the guest reads via `runtime.get` (ADR-006 §3):
    // the server url and the alias map the programs@v1 shadow guard
    // uses to refuse overlay-exported specs (ADR-013 §1)
    let mut runtime: BTreeMap<String, Value> = BTreeMap::new();
    runtime.insert("any.base_url".into(), Value::String(cfg.addr.clone()));
    // `runtime.get("bao.space")`: the bao space id — memory lives there
    // and nowhere else (ADR-017 §0); the guest memory verbs take no space
    runtime.insert("bao.space".into(), Value::String(space.clone()));
    runtime.insert("overlays.aliases".into(), serde_json::to_value(&aliases)?);
    // `runtime.get("shell")`: the shell's whereabouts in a `--features
    // shell` build, null otherwise (ADR-024 §4/§6); the guest binds
    // `sh`/`fs` on a non-null value
    runtime.insert("shell".into(), crate::shell_runtime_value());

    // kernel is embedded (ADR-009 §4) — the cage always boots eagerly;
    // pending overlays only gate program resolution
    let cage = load_cage(&cfg)?;
    step("kernel compiled");
    if !pending.is_empty() {
        info!(
            "overlays still joining/syncing: {:?} — will answer with status until synced",
            pending.keys().collect::<Vec<_>>()
        );
    }

    // Single-active election (ADR-015): register this device in the
    // tech-space registry and take the gate's boot verdict. Standby ⇒
    // chat watch and ticker stay idle (and the standing-trigger records
    // below aren't stamped) until the election thread flips the gate.
    let election = crate::election::boot(&client, env!("CARGO_PKG_VERSION"));
    step("election settled");

    // trace storage (ADR-001 §8 / ADR-023 §1): local-store collections
    // of the bao space, raw blobs in `traces_dir` beside them (ADR-026
    // §1); every writer and reader — serve, the guest's `trace.*`
    // syscalls — goes through the trait. The blob directory is created
    // here, and an unwritable one fails the boot: a blob dropped later
    // would only surface as `blob_missing` on read.
    let blobs = crate::blob::BlobDir::create(&cfg.traces_dir)?;
    step(&format!("blob directory {}", blobs.dir().display()));
    let traces: Arc<dyn TraceStore> = Arc::new(
        crate::tracestore::AnyTraceStore::new(
            client.clone(),
            &space,
            election.self_peer.clone(),
            Some(blobs),
        )
        .context("trace store: ensuring the bao space's local collections")?,
    );
    step("trace store ready");

    // This device's trigger identity (ADR-006 §4): the registry peer
    // id — stable across restarts, so a pinned record survives them.
    // No peer id (devices API unavailable) falls back to the legacy
    // `anyrt-<pid>` stamp, which every reader treats as UNOWNED — the
    // degrade path stays restart-safe too.
    let instance = election
        .self_peer
        .clone()
        .unwrap_or_else(|| format!("anyrt-{}", std::process::id()));
    let boot_active = election.verdict.active;
    let mut sched = Scheduler::new(&instance, Box::new(now_s));
    sched.arm();
    let mut registry = BTreeMap::new();
    // The standing built-ins follow the ELECTION, not a pin: a standby
    // boot seeds them ownerless (not runnable) and takeover stamps
    // them; device-pinned records are the ticker's reconcile job.
    let standing_owner = if boot_active { instance.as_str() } else { "" };
    for t in standing_triggers(&space, &chat, standing_owner) {
        if boot_active {
            client.upsert_record(
                &space,
                &anchor,
                &stores.triggers_ds,
                &t.id,
                &trigger_to_record(&t),
            )?;
        }
        registry.insert(t.id.clone(), t);
    }
    // The chat responder (ADR-018 §3): a record like any other — seeded
    // once, then claimed/repinned/paused through the dataset. A fresh
    // seed on an active boot is stamped right away so the watch
    // connects without waiting for the first reconcile tick.
    let (chat_watch_id, seeded) = seed_chat_watch(
        &client,
        &space,
        &anchor,
        &stores.triggers_ds,
        &chat,
        standing_owner,
    )?;
    if let Some(t) = seeded {
        if boot_active {
            registry.insert(t.id.clone(), t);
        }
    }
    let shared = Arc::new(Shared {
        triggers: Mutex::new(registry),
        scheduler: Mutex::new(sched),
        watcher: Mutex::new(Watcher::new(&cfg.agent_name)),
        backlog: Mutex::new(Vec::new()),
        event_sources: Mutex::new(BTreeMap::new()),
        chat_watch_id,
    });

    let ctx = Arc::new(RunCtx {
        cage,
        pending_overlays: Mutex::new(pending),
        client: client.clone(),
        cfg,
        traces,
        space: space.clone(),
        chat: chat.clone(),
        anchor: anchor.clone(),
        runs_anchor,
        triggers_ds: stores.triggers_ds.clone(),
        runs_ds: stores.runs_ds.clone(),
        secrets_ds: stores.secrets_ds.clone(),
        aliases,
        code_space,
        secrets_guard,
        oauth,
        secret_store,
        declared,
        config_store,
        runtime,
        verdict: Mutex::new(election.verdict.clone()),
        self_peer: election.self_peer.clone(),
        pruned: election.pruned,
        live_runs: Mutex::new(BTreeMap::new()),
        status: Default::default(),
    });

    let mut threads = vec![
        control_api(shared.clone(), ctx.clone(), shutdown.clone()),
        trigger_ticker(shared.clone(), ctx.clone(), shutdown.clone()),
        retention_thread(ctx.clone(), shutdown.clone()),
        presence_thread(shared.clone(), ctx.clone(), shutdown.clone()),
    ];
    if election.enabled {
        threads.push(election_thread(
            shared.clone(),
            ctx.clone(),
            shutdown.clone(),
        ));
    }
    info!(
        "anyrt serving space={space} chat={chat} control=127.0.0.1:{} boot={:.1}s",
        ctx.cfg.control_port,
        boot.elapsed().as_secs_f64()
    );

    {
        let (shared, ctx, stop) = (shared.clone(), ctx.clone(), shutdown.clone());
        threads.push(std::thread::spawn(move || {
            while !stop.load(Ordering::Relaxed) {
                // the watch connects iff this device OWNS the enabled
                // chat-responder record (ADR-018 §3) — not merely muted:
                // nothing lands in the seen-set, so a later takeover's
                // snapshot yields the whole missed backlog (ADR-015 §3)
                if !answers_chat(&shared) {
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

/// Trace retention (ADR-023 §6): host housekeeping, not a guest
/// program — it writes the trace store. First pass a minute after
/// boot, then hourly; a no-op unless `[traces] retain_*` is set.
fn retention_thread(ctx: Arc<RunCtx>, stop: Arc<AtomicBool>) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let configured =
            ctx.cfg.retain_conversations_s.is_some() || ctx.cfg.retain_jobs_s.is_some();
        if !configured {
            return;
        }
        sliced_sleep(Duration::from_secs(60), &stop);
        while !stop.load(Ordering::Relaxed) {
            match ctx.expire_traces() {
                Ok(0) => {}
                Ok(n) => info!("trace retention: {n} run bodies expired"),
                Err(e) => warn!("trace retention failed: {e:#}"),
            }
            sliced_sleep(Duration::from_secs(3600), &stop);
        }
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
    /// trace storage (ADR-001 §8) — threaded into every Broker so the
    /// guest reads past runs through the same store serve writes
    pub traces: Arc<dyn TraceStore>,
    pub space: String,
    pub chat: String,
    pub anchor: String,
    /// the `bao/runs/v1` child — one synced `agent_runs` summary per
    /// run (ADR-023 §1), written by `publish_run`
    pub runs_anchor: String,
    /// the collections the triggers / runs records live in (ADR-027 §2)
    pub triggers_ds: String,
    pub runs_ds: String,
    /// the secrets store's collection, resolved at boot — the broker's
    /// exact-collection guard next to the suffix rule (ADR-027 §2)
    pub secrets_ds: String,
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
    /// the secrets store (ADR-021 §4) — None on a server without the
    /// secrets object (seeds-only degraded mode)
    pub secret_store: Option<Arc<ServeSecretStore>>,
    /// the declared table (ADR-021 §8.1) — threaded into every Broker
    pub declared: Arc<DeclaredTable>,
    /// the agent-config store (ADR-006 §3) — read through, no cache
    pub config_store: Option<Arc<ServeConfigStore>>,
    /// runtime wiring for `runtime.get` (`any.base_url`,
    /// `overlays.aliases`) — per device, from the runtime config
    pub runtime: BTreeMap<String, Value>,
    /// single-active gate (ADR-015 §3): false = standby (chat watch
    /// disconnected; ownerless-trigger adoption and the standing
    /// built-ins idle — device-pinned triggers still fire, ADR-006
    /// §4). Written ONLY by the election thread after boot.
    /// `ctx.run()` itself is not gated — embedder/CLI runs are
    /// explicit. Held with the claim holder as ONE snapshot (the
    /// `Verdict` of the reconcile that produced it), written whole
    /// after a takeover finishes re-arming: the beat's `winner`
    /// (ADR-025 §1) and `GET /election` read this, never the registry.
    pub verdict: Mutex<crate::election::Verdict>,
    /// this device's peer id in the devices registry; None = server
    /// predates /v1/devices (election disabled, gate permanently true)
    pub self_peer: Option<String>,
    /// tombstoned device (ADR-015 §4): unlike standby, a pruned device
    /// fires NOTHING — not even its pins
    pub pruned: bool,
    /// every run in flight on this serve, by run id — chat, trigger
    /// and control runs alike. The control API's `POST /break/<runId>`
    /// resolves here (ADR-005 §3); entries live exactly as long as
    /// `run_program` does.
    pub live_runs: Mutex<BTreeMap<String, crate::triggers::LiveRun>>,
    /// serve-shared presence state (ADR-025): the `bao.status` syscall's
    /// write target + the presence thread's read; sole publisher = serve
    pub status: SharedPresence,
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

    /// The synced per-run summary (ADR-023 §1): one `agent_runs` record
    /// per run in the `bao/runs/v1` child — every device sees every
    /// device's runs. Best-effort: the trace itself is already landed;
    /// a failed publish is logged, never fails the run.
    fn publish_run(&self, mut summary: Value, trigger: Option<&str>) {
        let Some(id) = summary["id"].as_str().map(str::to_string) else {
            return;
        };
        summary["triggerId"] = json!(trigger);
        if summary["device"].is_null() {
            summary["device"] = json!(self.self_peer);
        }
        // the record id is the run id; `id` inside a value is immutable
        // to the dataset validator — `runId` carries it in the row
        if let Some(o) = summary.as_object_mut() {
            o.remove("id");
        }
        if let Err(e) =
            self.client
                .upsert_record(&self.space, &self.runs_anchor, &self.runs_ds, &id, &summary)
        {
            warn!("agent_runs {id}: summary not published: {e}");
        }
    }

    /// Retention pass (ADR-023 §6): drop run bodies past the configured
    /// ages; summaries stay. No-op when nothing is configured.
    pub fn expire_traces(&self) -> Result<usize> {
        let now = now_s();
        let conv = self.cfg.retain_conversations_s.map(|s| now - s as f64);
        let jobs = self.cfg.retain_jobs_s.map(|s| now - s as f64);
        if conv.is_none() && jobs.is_none() {
            return Ok(0);
        }
        self.traces.expire(CHAT_PROGRAM, conv, jobs)
    }

    fn broker(&self, spec: &str, run_id: String) -> Broker {
        let mut writer = TraceWriter::new(json!({"id": run_id, "program": spec,
                                             "host": "rust"}));
        if let Err(e) = writer.stream_to(self.traces.as_ref()) {
            warn!("trace streaming unavailable ({e}); will write at run end");
        }
        // agent config is read through the store (ADR-006 §3): the
        // seeds map is empty here — nothing to go stale
        let mut b = Broker::new(
            writer,
            BTreeMap::new(),
            self.cfg.secrets.clone(),
            // no local programs dir: serve resolves modules from the
            // space only (the resolver below) — never the filesystem
            None,
            Classifier::new(Some(&self.cfg.addr)),
        );
        b.secrets_guard = self.secrets_guard.clone();
        b.secrets_collection = Some(self.secrets_ds.clone());
        b.oauth = Some(self.oauth.clone());
        b.trace_store = Some(self.traces.clone());
        b.secret_store = self
            .secret_store
            .clone()
            .map(|s| s as Arc<dyn crate::broker::SecretSource>);
        b.config_store = self
            .config_store
            .clone()
            .map(|s| s as Arc<dyn crate::broker::ConfigStore>);
        b.declared = Some(self.declared.clone() as Arc<dyn DeclaredCredentials>);
        b.runtime = self.runtime.clone();
        b.presence = Some(self.status.clone());
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
        // the trigger that fired this run (ADR-023 §8: `triggerId` on
        // the run summary); None = chat/control/embedder run
        trigger: Option<&str>,
    ) -> Result<(String, RunResult)> {
        self.ensure_ready()?;
        let mut broker = self.broker(spec, run_id.unwrap_or_else(Self::new_run_id));
        broker.writer.trigger = trigger.map(str::to_string);
        let run_id = broker.writer.run_id();
        // the stamp + activity make this entry double as the presence
        // source: working beats carry the freshest live run (ADR-025 §1)
        let activity: crate::broker::SharedActivity = Default::default();
        broker.activity = Some(activity.clone());
        self.live_runs.lock().unwrap().insert(
            run_id.clone(),
            crate::triggers::LiveRun {
                mailbox: mailbox.clone(),
                interrupt: interrupt.clone(),
                stamp: run_stamp(&run_id, spec, args),
                activity,
            },
        );
        let outcome = run_program(&self.cage, broker, spec, args, mailbox, interrupt, 1200.0);
        self.live_runs.lock().unwrap().remove(&run_id);
        let mut outcome = outcome?;
        // ADR-005 §3: a failed trailing log append leaves the run ok
        // and names itself in the result — surface it here, since the
        // chat wrapper never reads the value
        if let Some(e) = outcome.value.get("logError").and_then(|v| v.as_str()) {
            warn!("run {run_id}: agent_turns append failed after the reply landed — {e}");
        }
        // ADR-023 §3: a run whose trace could not be landed is a failed
        // run, named — never a silent gap
        let summary = outcome
            .broker
            .writer
            .dump(self.traces.as_ref())
            .map_err(|e| anyhow::anyhow!("trace.unpersisted: run {run_id}: {e:#}"))?;
        self.publish_run(summary, trigger);
        Ok((
            run_id.clone(),
            RunResult {
                // ok | error | interrupted — a hard break (ADR-005 §3)
                // must reach the chat wrapper as what it is, not as a
                // generic error
                status: match outcome.status.as_str() {
                    "ok" => "ok".into(),
                    "interrupted" => "interrupted".into(),
                    _ => "error".into(),
                },
                duration_ms: outcome.duration_ms,
                trace_ref: Some(run_id),
                fuel: Some(outcome.fuel_used as i64),
                error: outcome.error.map(|e| e.to_string()),
                missing_secrets: outcome.broker.missing_secrets.clone(),
            },
        ))
    }

    /// One-shot program run for the control API's POST /run
    /// (ADR-009 §6): same resolver and trace discipline as `run`, but
    /// returns the full CLI-run envelope — the caller wants the
    /// program's VALUE, not just the status bookkeeping. `source`
    /// serves the entry spec from caller-provided text (InlineResolver)
    /// instead of a deployed program; its imports still resolve
    /// through the space resolver.
    pub fn run_value(&self, spec: &str, args: &Value, source: Option<&str>) -> Result<Value> {
        self.ensure_ready()?;
        let mut broker = self.broker(spec, Self::new_run_id());
        if let Some(src) = source {
            let inner = broker
                .resolver
                .take()
                .context("serve broker always carries a resolver")?;
            broker.resolver = Some(Box::new(crate::resolver::InlineResolver {
                spec: spec.to_string(),
                source: src.to_string(),
                inner,
            }));
        }
        let run_id = broker.writer.run_id();
        let mailbox: SharedMailbox = Default::default();
        let interrupt = Arc::new(AtomicBool::new(false));
        let activity: crate::broker::SharedActivity = Default::default();
        broker.activity = Some(activity.clone());
        self.live_runs.lock().unwrap().insert(
            run_id.clone(),
            crate::triggers::LiveRun {
                mailbox: mailbox.clone(),
                interrupt: interrupt.clone(),
                stamp: run_stamp(&run_id, spec, args),
                activity,
            },
        );
        let outcome = run_program(&self.cage, broker, spec, args, mailbox, interrupt, 1200.0);
        self.live_runs.lock().unwrap().remove(&run_id);
        let mut outcome = outcome?;
        let summary = outcome
            .broker
            .writer
            .dump(self.traces.as_ref())
            .map_err(|e| anyhow::anyhow!("trace.unpersisted: run {run_id}: {e:#}"))?;
        self.publish_run(summary, None);
        Ok(json!({
            "status": outcome.status,
            "value": outcome.value,
            "error": outcome.error,
            "traceRef": run_id,
            "durationMs": outcome.duration_ms,
            "fuelUsed": outcome.fuel_used,
        }))
    }
}

/// `type: message` of a run's error envelope, clipped for a chat line.
fn short_error(raw: &str) -> String {
    let s = serde_json::from_str::<Value>(raw)
        .ok()
        .and_then(|v| {
            Some(format!(
                "{}: {}",
                v["type"].as_str()?,
                v["message"].as_str()?
            ))
        })
        .unwrap_or_else(|| raw.to_string());
    let s = s.replace('\n', " ");
    if s.chars().count() > 160 {
        format!("{}…", s.chars().take(160).collect::<String>())
    } else {
        s
    }
}

/// Post the credential-request bubble for each ref still `missing`
/// whose row does not already name this chat as the place a live
/// request sits (`requestedIn`; cleared by the write that sets the
/// value) — ADR-021 §2. Everything the UI renders comes off the row;
/// the message only links it.
/// The onboarding marker (BOB-78, ADR-021 §2): a missing model key on a
/// space that has never chosen a provider — the client renders the
/// provider chooser instead of the plain key card. Derived from state,
/// never tracked: the missing ref is the codegen tier's `api_key_ref` AND
/// every `llm.tier.*` row still equals the embedded seed
/// (`config_defaults.json`). A row seeded by a toml `[config]` table or
/// written by a previous choice differs, and the plain card is posted.
/// No config store (or an unreadable one) = no marker.
fn is_model_setup(missing_ref: &str, tier: impl Fn(&str) -> Option<Value>) -> bool {
    let defaults = crate::config::config_defaults();
    let tiers: Vec<(&String, &Value)> = defaults
        .iter()
        .filter(|(k, _)| k.starts_with("llm.tier."))
        .collect();
    let Some(codegen) = tier("llm.tier.codegen") else {
        return false;
    };
    if codegen.get("api_key_ref").and_then(|v| v.as_str()) != Some(missing_ref) {
        return false;
    }
    tiers.iter().all(|(key, seed)| {
        tier(key)
            .as_ref()
            .map(|row| tier_identity(row) == tier_identity(seed))
            == Some(true)
    })
}

/// The four fields that make a tier row THIS provider — what the seed and a
/// stored row are compared on (extra inferred keys never break equality).
fn tier_identity(row: &Value) -> [Option<String>; 4] {
    ["provider", "model", "base_url", "api_key_ref"].map(|k| {
        row.get(k)
            .and_then(|v| v.as_str())
            .map(|s| s.trim_end_matches('/').to_string())
    })
}

fn post_credential_requests(ctx: &RunCtx, refs: &[String]) -> usize {
    let Some(store) = &ctx.secret_store else {
        return 0;
    };
    let mut posted = 0;
    for r in refs {
        let rows = ctx
            .client
            .query(&ctx.space, &store.obj, &store.dataset, &json!({}))
            .unwrap_or_default();
        let row = rows
            .iter()
            .find(|row| row.get("key").and_then(|v| v.as_str()) == Some(r.as_str()));
        let status = row
            .and_then(|row| row.get("status"))
            .and_then(|v| v.as_str())
            .unwrap_or("missing");
        let requested_in = row
            .and_then(|row| row.get("requestedIn"))
            .and_then(|v| v.as_str());
        if status == "set" || requested_in == Some(ctx.chat.as_str()) {
            continue;
        }
        let label = row
            .and_then(|row| row.get("label"))
            .and_then(|v| v.as_str())
            .map(str::to_string)
            .unwrap_or_else(|| r.clone());
        let hosts: Vec<String> = row
            .and_then(|row| row.get("hosts"))
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|h| h.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        let used_for = if hosts.is_empty() {
            String::new()
        } else {
            format!(", used for {}", hosts.join(", "))
        };
        // BOB-78: a never-chosen space asking for its model key gets the
        // provider chooser — a `&setup=model` marker on the row LINK (the
        // server's strict body rejects extra attachment fields; the link
        // is stored opaquely and an old client reads only `key`, so it
        // renders today's plain card) and provider-neutral text.
        let setup_model = status != "rejected"
            && ctx.config_store.as_ref().is_some_and(|cs| {
                is_model_setup(r, |key| {
                    crate::broker::ConfigStore::read(cs.as_ref(), key)
                        .ok()
                        .flatten()
                })
            });
        let unreviewed = r.starts_with(crate::broker::LOCAL_KEY_PREFIX);
        let text = if unreviewed && status != "rejected" {
            // ADR-021 §8.2: the warning is in the TEXT (old clients render
            // only that); no program is named — a name would be the
            // requester's claim; the run is the provenance the host vouches for
            format!(
                "⚠ Code written by {} (reviewed by no one) asks for a credential: {label} \
                 (`{r}`). It will be sent only to {}.",
                ctx.cfg.agent_name,
                if hosts.is_empty() {
                    "nowhere".to_string()
                } else {
                    hosts.join(", ")
                }
            )
        } else if status == "rejected" {
            let code = row
                .and_then(|row| row.get("rejectedWith"))
                .and_then(|v| v.as_u64())
                .unwrap_or(401);
            let by = if hosts.is_empty() {
                "the service".to_string()
            } else {
                hosts.join(", ")
            };
            format!("The {label} was rejected by {by} ({code}) — please enter a new one (`{r}`).")
        } else if setup_model {
            "Before I can think, I need a language model. Choose a provider and paste its \
             API key — you can change this later in Model settings."
                .to_string()
        } else {
            format!("I need a credential to continue: {label} (`{r}`{used_for}).")
        };
        let marker = if setup_model { "&setup=model" } else { "" };
        let mut agent = json!({"name": ctx.cfg.agent_name, "done": true});
        if let Some(run) = row
            .and_then(|row| row.get("requestedBy"))
            .and_then(Value::as_str)
            .filter(|_| unreviewed)
        {
            agent["debugLink"] = json!(run); // §8.2: the trace shows the code
        }
        let sent = ctx.client.chat_send(
            &ctx.space,
            &ctx.chat,
            &json!({
                "text": text,
                "agent": agent,
                "attachments": {"credreq": {
                    "type": "credential_request",
                    "link": format!("any://o/{}?key={r}{marker}", store.obj)}}}),
        );
        match sent {
            Ok(_) => {
                posted += 1;
                // requestedIn/At only — status was stamped by the miss;
                // re-asserting it here could undo a save that landed
                // between the miss and this write
                let patch = json!({"requestedIn": ctx.chat,
                                   "requestedAt": {"$date": now_rfc3339()}});
                if let Err(e) = upsert_secret_row(
                    &ctx.client,
                    &ctx.space,
                    &store.obj,
                    &store.dataset,
                    r,
                    &patch,
                ) {
                    warn!("secrets: could not stamp request for {r} ({e})");
                }
            }
            Err(e) => warn!("secrets: could not post request for {r} ({e})"),
        }
    }
    posted
}

/// Soft-break grace (ADR-005 §3): the guest only drains its mailbox
/// between turns, so a run stuck inside a long cell cannot honor
/// "stop" by itself. If the break item is still UNDRAINED after this
/// long, the flag goes up and the epoch callback traps the guest at
/// its next tick (a blocked host call — an LLM or HTTP request —
/// returns first; the broker does not yet check the flag mid-call).
/// A guest that HAS drained the item is wrapping up: the escalation
/// stands down and the run's wall deadline is the backstop.
const BREAK_GRACE: Duration = Duration::from_secs(20);

/// A stop landed on a live run. Hard: the watcher already set the
/// flag. Soft: arm the escalation — after BREAK_GRACE, if the break
/// item still sits in the mailbox (the guest never drained it), set
/// the flag (the Arc is the run's identity; a finished run's flag is
/// a dead letter).
fn on_break(chat: &str, live: &crate::triggers::LiveRun, hard: bool) {
    if hard {
        info!("hard break on chat {chat}");
        return;
    }
    info!(
        "soft break on chat {chat} (hard in {}s unless acknowledged)",
        BREAK_GRACE.as_secs()
    );
    let live = live.clone();
    let chat = chat.to_string();
    std::thread::spawn(move || {
        std::thread::sleep(BREAK_GRACE);
        let undrained = live
            .mailbox
            .lock()
            .unwrap()
            .iter()
            .any(|m| m["kind"] == "break");
        if !undrained {
            return; // acknowledged: the guest is wrapping up
        }
        if !live.interrupt.swap(true, Ordering::Relaxed) && Arc::strong_count(&live.interrupt) > 1 {
            info!("soft break on chat {chat} escalated to hard");
        }
    });
}

/// The minimal `agent_turns` record for a hard-broken run — the guest
/// skipped its own `append_turn`, so the host writes `{userText,
/// replies: [], interrupted: true, traceRef}` at the data layer
/// (ADR-005 §3): the next boot window and the log-reading crons see
/// the stopped exchange. Mirrors the guest's `_append_log` (log child
/// = the chat bundle's `bao/log/v1`, client-assigned seq). Skipped
/// with a warning when the agent_log type was never provisioned —
/// only possible when the chat's very first turn is the broken one.
fn append_interrupted_turn(ctx: &RunCtx, user_text: &str, trace_ref: &str) -> Result<()> {
    let bundles = ctx.client.list_bundles(&ctx.space)?;
    let root = bundles["bundles"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|b| b["rootId"] == json!(ctx.chat.as_str()))
        .and_then(|b| b["id"].as_str())
        .context("chat is not a bundle root")?
        .to_string();
    let tid = ctx
        .client
        .list_types(&ctx.space)?
        .into_iter()
        .find(|t| t["xKey"] == json!("agent_log"))
        .and_then(|t| t["id"].as_str().map(str::to_string))
        .context("agent_log type not provisioned yet (no turn ever logged)")?;
    // the guest declares the log store (ADR-017 §1); its collection is
    // read off the declaration, never composed (ADR-027 §2)
    let turns = dataset_collection(&ctx.client, &ctx.space, &tid, "agent_turns")?
        .context("agent_log type declares no agent_turns dataset")?;
    let child = ctx
        .client
        .bundle_child(&ctx.space, &root, "bao/log/v1", &[tid.as_str()])?;
    let log = child["objectId"]
        .as_str()
        .context("bundle_child returned no objectId")?
        .to_string();
    // ADR-017 §2: one past the highest id ever written, tombstones
    // included (a deleted id never reuses; tombstones carry no `seq`,
    // so the probe sorts on the record id = the zero-padded seq)
    let rows = ctx.client.query(
        &ctx.space,
        &log,
        &turns,
        &json!({"includeDeleted": true, "sort": ["-id"], "limit": 1}),
    )?;
    let seq = rows
        .first()
        .and_then(|r| r["id"].as_str())
        .and_then(|id| id.parse::<i64>().ok())
        .unwrap_or(0)
        + 1;
    ctx.client.upsert_record(
        &ctx.space,
        &log,
        &turns,
        &format!("{seq:08}"),
        &json!({
            "seq": seq, "fromAgent": ctx.cfg.agent_name,
            "userText": user_text, "replies": [], "interrupted": true,
            "traceRef": trace_ref, "searchText": user_text,
            "llm": {"stopReason": "break_hard"}}),
    )?;
    Ok(())
}

/// Start a run for `input` — or, when one is already live on this chat,
/// inject into its mailbox instead. Check-and-register happens under
/// ONE watcher lock: the watch thread and the ticker's backlog drain
/// may race to start.
fn start_or_inject(shared: &Arc<Shared>, ctx: &Arc<RunCtx>, input: ChatInput) {
    let mailbox: SharedMailbox = Default::default();
    let interrupt = Arc::new(AtomicBool::new(false));
    {
        let mut w = shared.watcher.lock().unwrap();
        if let Some(live) = w.live.get(&ctx.chat) {
            live.mailbox
                .lock()
                .unwrap()
                .push_back(json!({"kind": "inject", "text": input.text,
                                  "context": input.context}));
            return;
        }
        w.live.insert(
            ctx.chat.clone(),
            LiveRun {
                mailbox: mailbox.clone(),
                interrupt: interrupt.clone(),
                ..Default::default()
            },
        );
    }
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
            "space": ctx.space, "chatId": ctx.chat, "userText": input.text,
            "uiContext": input.context, "agentName": ctx.cfg.agent_name, "traceRef": run_id,
            "codeSpace": ctx.code_space, "overlays": overlays});
        let result = ctx.run(
            "agent:toolcaller@v1",
            &args,
            mailbox.clone(),
            interrupt,
            Some(run_id),
            Some(&shared.chat_watch_id),
        );
        // Take the run down FIRST: a message typed while the terminal
        // bubbles post must start a fresh run, not inject into this
        // dead mailbox (its id would enter `seen` and the message
        // would be lost for good — review). Whatever was injected
        // after the end is re-dispatched below.
        let leftovers: Vec<Value> = {
            let mut w = shared.watcher.lock().unwrap();
            w.conversation_done(&ctx.chat);
            mailbox.lock().unwrap().drain(..).collect()
        };
        if let Ok((trace_ref, rr)) = &result {
            // ADR-021 §2: one request bubble per missing ref (the host
            // is the only emitter). A run that died ON the miss — the
            // LLM key — gets the request instead of "Something broke".
            // …or on the destination's 401 (LlmError) — either way the
            // request bubble (or the "still waiting" line) IS the reply
            // "error" only: an interrupted run's reply is "Stopped.",
            // never the "Still waiting for the credential" line (review)
            let died_on_miss = rr.status == "error" && !rr.missing_secrets.is_empty();
            let posted = post_credential_requests(&ctx, &rr.missing_secrets);
            if died_on_miss && posted == 0 {
                // the bubble for this ref already sits in the chat
                // (dedup) — a silent turn would read as "bao is gone";
                // the last error rides along so the human can see WHY
                let refs = rr.missing_secrets.join("`, `");
                let why = rr
                    .error
                    .as_deref()
                    .map(short_error)
                    .filter(|s| !s.is_empty())
                    .map(|s| format!(" Last attempt: {s}"))
                    .unwrap_or_default();
                let _ = ctx.client.chat_send(
                    &ctx.space,
                    &ctx.chat,
                    &json!({
                    "text": format!("Still waiting for `{refs}` — see the credential prompt above.{why}"),
                    "agent": {"name": ctx.cfg.agent_name, "done": true}}),
                );
            }
            if rr.status == "interrupted" {
                // a hard break (ADR-005 §3): the host says the one thing
                // the guest no longer can — the run is over
                let _ = ctx.client.chat_send(
                    &ctx.space,
                    &ctx.chat,
                    &json!({
                    "text": "Stopped.",
                    "agent": {"name": ctx.cfg.agent_name, "done": true,
                              "outcome": "interrupted", "debugLink": trace_ref}}),
                );
                // …and writes the turn the guest could not (ADR-005 §3):
                // without it the next boot window has no trace of the
                // stopped request and the model's view of the
                // conversation diverges from the chat on screen
                if let Err(e) = append_interrupted_turn(&ctx, &input.text, trace_ref) {
                    warn!("interrupted turn not logged (trace {trace_ref}): {e}");
                }
            } else if rr.status != "ok" && !died_on_miss {
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
                // How the run ended rides the `agent` group (`outcome`,
                // the run id as `debugLink`), not the text (ADR-005 §3):
                // the client renders the mark, the text stays human
                let text = match detail {
                    Some(d) => format!("Something broke mid-run: {d}"),
                    None => "Something broke mid-run.".to_string(),
                };
                let _ = ctx.client.chat_send(
                    &ctx.space,
                    &ctx.chat,
                    &json!({
                    "text": text,
                    "agent": {"name": ctx.cfg.agent_name, "done": true,
                              "outcome": "error", "debugLink": trace_ref}}),
                );
            }
        }
        for item in leftovers {
            if item["kind"] == "inject" {
                start_or_inject(
                    &shared,
                    &ctx,
                    ChatInput {
                        text: item["text"].as_str().unwrap_or_default().to_string(),
                        context: item["context"].clone(),
                    },
                );
            }
        }
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
        if stop.load(Ordering::Relaxed) || !answers_chat(shared) {
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
                    let record = resolve_reply(ctx, record);
                    let action = shared
                        .watcher
                        .lock()
                        .unwrap()
                        .on_message(&ctx.chat, &record);
                    match &action {
                        WatchAction::Break { live, hard } => on_break(&ctx.chat, live, *hard),
                        WatchAction::BreakIdle => {
                            // the stop cancels messages still deferred on
                            // overlay sync — nothing must start for them
                            let dropped = std::mem::take(&mut *shared.backlog.lock().unwrap());
                            if !dropped.is_empty() {
                                info!(
                                    "break with no live run: dropped {} deferred message(s)",
                                    dropped.len()
                                );
                            }
                        }
                        _ => {}
                    }
                    if let WatchAction::Start = action {
                        let input = Watcher::input(&record);
                        note_chat_start(shared, ctx);
                        if ready {
                            info!("backlog conversation: {:?}", preview(&input.text));
                            start_or_inject(shared, ctx, input);
                        } else {
                            // no bubble for stale messages — a burst of
                            // "not ready" is noise; the ticker drains
                            // the queue once overlays sync
                            shared.backlog.lock().unwrap().push(input);
                        }
                    }
                }
            }
            "changes" => {
                for record in records_in(&frame.data) {
                    let record = resolve_reply(ctx, record);
                    let action = shared
                        .watcher
                        .lock()
                        .unwrap()
                        .on_message(&ctx.chat, &record);
                    match &action {
                        WatchAction::Break { live, hard } => on_break(&ctx.chat, live, *hard),
                        WatchAction::BreakIdle => {
                            // the stop cancels messages still deferred on
                            // overlay sync — nothing must start for them
                            let dropped = std::mem::take(&mut *shared.backlog.lock().unwrap());
                            if !dropped.is_empty() {
                                info!(
                                    "break with no live run: dropped {} deferred message(s)",
                                    dropped.len()
                                );
                            }
                        }
                        _ => {}
                    }
                    if let WatchAction::Start = action {
                        let input = Watcher::input(&record);
                        note_chat_start(shared, ctx);
                        // deferred boot (ADR-009 §8): a message while
                        // overlays are pending gets a status bubble —
                        // the one host-authored operational reply —
                        // and queues for a real answer once synced
                        match ctx.ensure_ready() {
                            Ok(_) => {
                                info!("conversation started: {:?}", preview(&input.text));
                                start_or_inject(shared, ctx, input);
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
                                shared.backlog.lock().unwrap().push(input);
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
            // a done:false progress bubble is not an answer — keep
            // scanning; the cut is the last TERMINAL self reply
            if rec["agent"]["done"].as_bool().unwrap_or(true) {
                break;
            }
            continue;
        }
        // a control record (a Stop pressed during a feed gap) must
        // reach on_message — it is text-less by contract
        if crate::triggers::is_control(rec) {
            out.push(rec.clone());
            continue;
        }
        // content = text OR attachments (the server enforces at least
        // one); an attachment-only message is real input, not noise
        let has_text = rec["text"].as_str().is_some_and(|t| !t.is_empty());
        let has_atts = rec["attachments"]
            .as_object()
            .is_some_and(|a| !a.is_empty());
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

/// The NEW `chat_messages` records in a `changes` frame. Only `added`
/// entries are input: an `updated` entry is a reaction toggle or a
/// text edit on a message that already had its turn, and neither
/// starts or injects a run (ADR-018 §2). Reading `updated` too made
/// a reaction on a message older than the process — absent from the
/// watcher's seen-set, which is seeded from the unanswered backlog
/// only — look like a fresh message, and the agent re-answered it.
/// A reply's referent, read from the chat before the watcher sees the
/// record (ADR-005 §5, BOB-65): deleted rows included so a tombstone
/// folds as such; a failed read folds as "not found" — a reply never
/// stalls on its target. Not an effect: like the record itself, it is
/// host-side input; the folded text is what the trace records.
fn resolve_reply(ctx: &RunCtx, record: Value) -> Value {
    Watcher::with_reply(record, |id| {
        match ctx.client.query(
            &ctx.space,
            &ctx.chat,
            "chat_messages",
            &json!({"filter": {"id": id}, "includeDeleted": true, "limit": 1}),
        ) {
            Ok(mut rows) if !rows.is_empty() => Some(rows.remove(0)),
            Ok(_) => None,
            Err(e) => {
                warn!("reply target {id}: {e}");
                None
            }
        }
    })
}

fn records_in(data: &Value) -> Vec<Value> {
    let mut out = Vec::new();
    if let Some(batches) = data.as_array() {
        for batch in batches {
            for entry in batch["added"].as_array().unwrap_or(&Vec::new()) {
                let mut rec = entry["doc"].as_object().cloned().unwrap_or_default();
                rec.insert("id".into(), entry["id"].clone());
                out.push(Value::Object(rec));
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
    std::thread::spawn(move || {
        // "why isn't it firing" must be answerable from the log
        // (ADR-006 §4 observability): gate and reconcile failures log
        // on TRANSITIONS — visible without flooding a 5s loop.
        let mut was_ready = true;
        let mut reconcile_failing = false;
        loop {
            sliced_sleep(Duration::from_secs(5), &stop);
            if stop.load(Ordering::Relaxed) {
                return;
            }
            // pruned (ADR-015 §4): a tombstoned device fires NOTHING —
            // standby, by contrast, still fires its device-pinned
            // records. Boot already warned loudly; stay quiet here.
            if ctx.pruned {
                continue;
            }
            // deferred boot (ADR-009 §8): don't burn trigger runs (and
            // the circuit breaker) while overlays are still syncing.
            // This is a PROBE, not the cheap check — readiness must
            // clear without waiting for a chat message (the backlog
            // drain below and trigger start both hang off it); a no-op
            // once synced.
            match ctx.ensure_ready() {
                Err(e) => {
                    if was_ready {
                        info!("trigger ticker: paused — {e}");
                        was_ready = false;
                    }
                    continue;
                }
                Ok(()) => {
                    if !was_ready {
                        info!("trigger ticker: overlays ready — resuming");
                        was_ready = true;
                    }
                }
            }
            let active = ctx.verdict.lock().unwrap().active;
            if answers_chat(&shared) {
                // answer user messages deferred while overlays were syncing
                let deferred: Vec<ChatInput> = std::mem::take(&mut *shared.backlog.lock().unwrap());
                for input in deferred {
                    info!("deferred conversation: {:?}", preview(&input.text));
                    start_or_inject(&shared, &ctx, input);
                }
            }
            // the dataset is the source of truth (ADR-006 §4): converge
            // the registry on the records — adopt pins + (when active)
            // unowned records, refresh edited definitions, evict
            // repinned and deleted ones. Runs on standby too: pins are
            // election-independent.
            match ctx
                .client
                .query(&ctx.space, &ctx.anchor, &ctx.triggers_ds, &json!({}))
            {
                Err(e) => {
                    if !reconcile_failing {
                        warn!(
                            "agent_triggers reconcile query failed ({e}) — \
                             registry runs unrefreshed until it recovers"
                        );
                        reconcile_failing = true;
                    }
                }
                Ok(recs) => {
                    if reconcile_failing {
                        info!("agent_triggers reconcile recovered");
                        reconcile_failing = false;
                    }
                    let instance = shared.scheduler.lock().unwrap().instance_id().to_string();
                    let standing: std::collections::BTreeSet<String> =
                        standing_triggers(&ctx.space, "", "")
                            .into_iter()
                            .map(|t| t.id)
                            .collect();
                    let stamp = {
                        let mut reg = shared.triggers.lock().unwrap();
                        reconcile_registry(&mut reg, &recs, &instance, active, &standing)
                    };
                    for t in &stamp {
                        let _ = ctx.client.upsert_record(
                            &ctx.space,
                            &ctx.anchor,
                            &ctx.triggers_ds,
                            &t.id,
                            &trigger_to_record(t),
                        );
                        info!("trigger adopted from dataset: {:?} ({})", t.id, t.kind);
                    }
                    // health pass: mark enabled-but-inert definitions on
                    // their records (and clear the marks once fixed)
                    let marked = {
                        let mut reg = shared.triggers.lock().unwrap();
                        let sched = shared.scheduler.lock().unwrap();
                        health_pass(&sched, &mut reg, &standing)
                    };
                    for t in &marked {
                        let _ = ctx.client.upsert_record(
                            &ctx.space,
                            &ctx.anchor,
                            &ctx.triggers_ds,
                            &t.id,
                            &trigger_to_record(t),
                        );
                    }
                    reconcile_event_sources(&shared, &ctx, &stop);
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
                let rr = run_trigger_program(&ctx, &t.id, &t.program, &t.args);
                finish_run(&shared, &ctx, &t.id, &rr);
            }
        }
    })
}

/// One trigger fire: the program run, folded into a RunResult (a run
/// path failure is an error result, never a panic).
fn run_trigger_program(ctx: &RunCtx, trigger_id: &str, program: &str, args: &Value) -> RunResult {
    let mailbox: SharedMailbox = Default::default();
    let interrupt = Arc::new(AtomicBool::new(false));
    ctx.run(program, args, mailbox, interrupt, None, Some(trigger_id))
        .map(|(_, rr)| rr)
        .unwrap_or_else(|e| RunResult {
            status: "error".into(),
            duration_ms: 0,
            trace_ref: None,
            fuel: None,
            error: Some(e.to_string()),
            missing_secrets: Vec::new(),
        })
}

/// Run bookkeeping (ADR-006 §4): the breaker + scheduler state on the
/// registry entry, the trigger record rewritten. Run history is the
/// `agent_runs` summary the run itself published (ADR-023 §8). A no-op
/// if the trigger was evicted mid-run.
fn finish_run(shared: &Shared, ctx: &RunCtx, trigger_id: &str, rr: &RunResult) {
    let mut reg = shared.triggers.lock().unwrap();
    let Some(live) = reg.get_mut(trigger_id) else {
        return;
    };
    let sched = shared.scheduler.lock().unwrap();
    sched.record_run(live, rr);
    let _ = ctx.client.upsert_record(
        &ctx.space,
        &ctx.anchor,
        &ctx.triggers_ds,
        trigger_id,
        &trigger_to_record(live),
    );
}

// --- event sources (ADR-018 §2) ------------------------------------------------

/// Converge the live source threads on the registry: one watch per
/// distinct `(space, chat object)` across this device's enabled
/// `chat_messages` event triggers — a record's `spec.spaceId`, else
/// the agent space. New sources get a thread; sources no trigger
/// needs any more (disabled, repinned away, deleted, breaker-tripped)
/// get their thread stopped.
fn reconcile_event_sources(shared: &Arc<Shared>, ctx: &Arc<RunCtx>, stop: &Arc<AtomicBool>) {
    let desired = {
        let reg = shared.triggers.lock().unwrap();
        let instance = shared.scheduler.lock().unwrap().instance_id().to_string();
        desired_event_sources(&reg, &instance, &ctx.space)
    };
    let mut live = shared.event_sources.lock().unwrap();
    let stale: Vec<(String, String)> = live
        .keys()
        .filter(|k| !desired.contains_key(*k))
        .cloned()
        .collect();
    for key in stale {
        if let Some(flag) = live.remove(&key) {
            flag.store(true, Ordering::Relaxed);
            info!("event source stopped: chat {} in space {}", key.1, key.0);
        }
    }
    for ((space, object_id), trigger_ids) in desired {
        if live.contains_key(&(space.clone(), object_id.clone())) {
            continue;
        }
        let flag = Arc::new(AtomicBool::new(false));
        live.insert((space.clone(), object_id.clone()), flag.clone());
        info!("event source started: chat {object_id} in space {space} → {trigger_ids:?}");
        let (shared, ctx, stop) = (shared.clone(), ctx.clone(), stop.clone());
        std::thread::spawn(move || event_source_thread(shared, ctx, space, object_id, flag, stop));
    }
}

/// One chat object's watch (ADR-018 §2, live-only): reconnect loop
/// around the SSE feed in the source's own space; a snapshot only
/// seeds the seen-set, `changes` fire the triggers that name this
/// source. Exits when its own flag (source no longer desired) or the
/// serve-wide stop is raised.
fn event_source_thread(
    shared: Arc<Shared>,
    ctx: Arc<RunCtx>,
    space: String,
    object_id: String,
    own_stop: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
) {
    let halted = || own_stop.load(Ordering::Relaxed) || stop.load(Ordering::Relaxed);
    let mut state = EventSource::default();
    while !halted() {
        let feed = ctx.client.subscribe_dataset(
            &space,
            &object_id,
            CHAT_MESSAGES,
            &json!({"sort": ["-createdAt"], "limit": 64}),
        );
        match feed {
            Err(e) => warn!(
                "event source {object_id} in space {space}: subscribe failed ({e}); retrying in 2s"
            ),
            Ok(frames) => {
                for frame in frames {
                    if halted() {
                        return;
                    }
                    match frame.event.as_str() {
                        "ready" => {}
                        "closed" => break,
                        "snapshot" => {
                            let records = frame.data["records"]
                                .as_array()
                                .cloned()
                                .unwrap_or_default();
                            state.seed(&records);
                        }
                        "changes" => {
                            for record in state.fresh(records_in(&frame.data), &ctx.cfg.agent_name)
                            {
                                fire_event(&shared, &ctx, &space, &object_id, &record);
                            }
                        }
                        other => {
                            warn!("event source {object_id}: unexpected frame {other:?}");
                            break;
                        }
                    }
                }
            }
        }
        sliced_sleep(Duration::from_secs(2), &stop);
    }
}

/// Fire every enabled trigger of this device that names the
/// `(space, object_id)` source, sequentially in the source thread,
/// with full run bookkeeping. Deferred boot (ADR-009 §8) drops the
/// event rather than queueing it — live-only means live-only.
fn fire_event(shared: &Shared, ctx: &RunCtx, space: &str, object_id: &str, record: &Value) {
    if let Err(status) = ctx.ensure_ready() {
        warn!("event on chat {object_id} dropped — not ready: {status}");
        return;
    }
    let targets: Vec<Trigger> = {
        let reg = shared.triggers.lock().unwrap();
        let instance = shared.scheduler.lock().unwrap().instance_id().to_string();
        reg.values()
            .filter(|t| t.owner == instance && t.enabled && !is_chat_watch(&t.id))
            .filter(|t| matches!(event_source(t), Some((CHAT_MESSAGES, oid)) if oid == object_id))
            .filter(|t| event_space(t, &ctx.space) == space)
            .cloned()
            .collect()
    };
    for t in targets {
        let args = event_args(&t, space, object_id, record);
        info!(
            "event trigger {:?} fired by message {:?} on chat {object_id}",
            t.id,
            record.get("id").and_then(|v| v.as_str()).unwrap_or("")
        );
        let rr = run_trigger_program(ctx, &t.id, &t.program, &args);
        finish_run(shared, ctx, &t.id, &rr);
    }
}

/// The election reconcile loop (ADR-015 §3/§4): poll the registry,
/// flip the gate on verdict transitions. The ONLY writer of
/// `ctx.verdict` after boot, so read-then-store is race-free.
fn election_thread(
    shared: Arc<Shared>,
    ctx: Arc<RunCtx>,
    stop: Arc<AtomicBool>,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let Some(peer) = ctx.self_peer.clone() else {
            return; // enabled implies a peer id; belt and braces
        };
        let mut read_failing = false;
        loop {
            sliced_sleep(crate::election::POLL, &stop);
            if stop.load(Ordering::Relaxed) {
                return;
            }
            let Some(verdict) =
                crate::election::reconcile(&ctx.client, &peer, crate::election::APP_SLUG)
            else {
                // transient read failure — keep the last state, and say
                // so once: a standby whose registry reads keep failing
                // must not look like a healthy standby in the log
                if !read_failing {
                    warn!(
                        "election: registry read failed — keeping the last verdict ({}) \
                         until it recovers",
                        ctx.verdict.lock().unwrap().describe()
                    );
                    read_failing = true;
                }
                continue;
            };
            if read_failing {
                info!("election: registry reads recovered");
                read_failing = false;
            }
            let was_active = ctx.verdict.lock().unwrap().active;
            match (verdict.active, was_active) {
                (true, false) => {
                    takeover(&shared, &ctx);
                    // the snapshot moves AFTER re-arm, gate and winner
                    // together: a beat in the takeover window still
                    // says standby + the old winner, never standby
                    // naming this very device
                    *ctx.verdict.lock().unwrap() = verdict;
                    info!("election: TAKEOVER — this device is now the active bao");
                }
                (false, true) => {
                    *ctx.verdict.lock().unwrap() = verdict.clone();
                    // the new active device answers these; in-flight
                    // runs finish on their own (never interrupt a turn)
                    shared.backlog.lock().unwrap().clear();
                    // the standing built-ins follow the election: drop
                    // their local ownership so they stop firing here
                    // (the new winner's takeover stamps the records —
                    // no write from the loser, no race). Device-pinned
                    // records stay runnable — that's the pin.
                    let standing: std::collections::BTreeSet<String> =
                        standing_triggers(&ctx.space, "", "")
                            .into_iter()
                            .map(|t| t.id)
                            .collect();
                    let mut reg = shared.triggers.lock().unwrap();
                    for t in reg.values_mut() {
                        if standing.contains(&t.id) {
                            t.owner.clear();
                        }
                    }
                    // the chat responder is released for real — owner
                    // cleared ON THE RECORD (the one stand-down write,
                    // ADR-018 §3): a local-only evict would be undone by
                    // the next reconcile, which still reads our peer id
                    if let Some(mut t) = reg.remove(&shared.chat_watch_id) {
                        t.owner.clear();
                        let _ = ctx.client.upsert_record(
                            &ctx.space,
                            &ctx.anchor,
                            &ctx.triggers_ds,
                            &t.id,
                            &trigger_to_record(&t),
                        );
                    }
                    info!("election: stand-down — {}", verdict.describe());
                }
                // no gate change; the claim holder may still have moved
                // (a switch between two OTHER devices) and rides the
                // next beat. The standby repeat lives in the presence
                // thread (ADR-015 §5), not here.
                _ => *ctx.verdict.lock().unwrap() = verdict,
            }
        }
    })
}

/// Takeover prep (ADR-015 §3), run BEFORE the gate flips. Only the
/// STANDING built-ins move with the election: re-arm their crons
/// strictly forward (a missed occurrence while standby does not exist
/// — the ADR-006 §4 cold-sync rule; prevents the wake-and-replay
/// burst), stamp + publish the records the standby boot skipped.
/// Device-pinned records never move on an election flip — they were
/// firing here all along (or belong to another device).
fn takeover(shared: &Shared, ctx: &RunCtx) {
    let instance = shared.scheduler.lock().unwrap().instance_id().to_string();
    let standing: std::collections::BTreeSet<String> = standing_triggers(&ctx.space, "", "")
        .into_iter()
        .map(|t| t.id)
        .collect();
    let mut reg = shared.triggers.lock().unwrap();
    for t in reg.values_mut() {
        if !standing.contains(&t.id) {
            continue;
        }
        if t.kind == "cron" {
            t.next_due = None; // next tick arms forward, no fire
        }
        t.owner = instance.clone();
        let _ = ctx.client.upsert_record(
            &ctx.space,
            &ctx.anchor,
            &ctx.triggers_ds,
            &t.id,
            &trigger_to_record(t),
        );
    }
    // the chat responder follows the election too (ADR-018 §3): the
    // new winner re-stamps the record — an explicit write, and the
    // newest explicit act wins over an earlier repin. The record is
    // read back rather than taken from the registry: a standby never
    // held it (foreign-owned → not adopted).
    let Ok(recs) = ctx
        .client
        .query(&ctx.space, &ctx.anchor, &ctx.triggers_ds, &json!({}))
    else {
        return;
    };
    for rec in recs {
        let Some(id) = rec.get("id").and_then(|v| v.as_str()) else {
            continue;
        };
        if id != shared.chat_watch_id {
            continue;
        }
        if let Some(mut t) = record_to_trigger(id, &rec) {
            t.owner = instance.clone();
            let _ = ctx.client.upsert_record(
                &ctx.space,
                &ctx.anchor,
                &ctx.triggers_ds,
                id,
                &trigger_to_record(&t),
            );
            reg.insert(id.to_string(), t);
        }
    }
}

/// Does this device answer chat right now — own the enabled
/// chat-responder record (ADR-018 §3)?
fn answers_chat(shared: &Shared) -> bool {
    let reg = shared.triggers.lock().unwrap();
    let instance = shared.scheduler.lock().unwrap().instance_id().to_string();
    owned_chat_watch(&reg, &instance).is_some()
}

/// A conversation start counts as one "run" on the responder's rollup
/// (no per-message run records — turns are logged as agent_turns).
fn note_chat_start(shared: &Shared, ctx: &RunCtx) {
    let mut reg = shared.triggers.lock().unwrap();
    let Some(t) = reg.get_mut(&shared.chat_watch_id) else {
        return;
    };
    t.last_run_at = Some(now_s());
    t.last_status = Some("ok".into());
    t.consecutive_failures = 0;
    let _ = ctx.client.upsert_record(
        &ctx.space,
        &ctx.anchor,
        &ctx.triggers_ds,
        &t.id,
        &trigger_to_record(t),
    );
}

/// Seed the chat-responder record once (ADR-018 §3): an existing
/// `chat-watch*` record is left untouched (its owner/enabled are user
/// state); otherwise write `chat-watch`, falling through generation
/// suffixes when the bare id was tombstoned by a delete. Returns the
/// live id and the trigger when this boot created it.
fn seed_chat_watch(
    client: &Client,
    space: &str,
    anchor: &str,
    dataset: &str,
    chat: &str,
    owner: &str,
) -> Result<(String, Option<Trigger>)> {
    let recs = client
        .query(space, anchor, dataset, &json!({}))
        .context("reading agent_triggers to seed the chat responder")?;
    if let Some(id) = recs
        .iter()
        .filter_map(|r| r.get("id").and_then(|v| v.as_str()))
        .find(|id| is_chat_watch(id))
    {
        return Ok((id.to_string(), None));
    }
    for gen in 1..=5u32 {
        let id = if gen == 1 {
            CHAT_WATCH_ID.to_string()
        } else {
            format!("{CHAT_WATCH_ID}-g{gen}")
        };
        let t = chat_watch_trigger(&id, chat, owner);
        match client.upsert_record(space, anchor, dataset, &id, &trigger_to_record(&t)) {
            Ok(reply) if reply_tombstoned(&reply) => continue,
            Ok(_) => return Ok((id, Some(t))),
            Err(e) if e.code.contains("record_deleted") => continue,
            Err(e) => return Err(e).context("seeding the chat responder record"),
        }
    }
    anyhow::bail!("chat responder: every generation id is tombstoned")
}

/// A modify reply whose rejections say the id is a deleted record.
fn reply_tombstoned(reply: &Value) -> bool {
    reply["rejections"].as_array().is_some_and(|rs| {
        rs.iter().any(|r| {
            ["reason", "code"]
                .iter()
                .any(|k| r[k].as_str().is_some_and(|v| v.contains("record_deleted")))
        })
    })
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
        // presence observability (ADR-025 §7): exactly what the next
        // beat would carry + the scheduler state, for "is it beating?"
        ("GET", ["status"]) => {
            let run = current_run(&ctx.live_runs.lock().unwrap());
            let state = if run.is_some() { "working" } else { "idle" };
            Ok(json!({
                "envelope": status_envelope(&presence_identity(ctx), &ctx.status, run, state, &beat_role(shared, &reg, ctx), now_s()),
                "beatSec": STATUS_BEAT_S,
            }))
        }
        // election observability (ADR-015 §5): the verdict snapshot
        // the beat carries — no live registry read, so the two can
        // never disagree
        ("GET", ["election"]) => {
            let verdict = ctx.verdict.lock().unwrap().clone();
            Ok(json!({
                "app": crate::election::APP_SLUG,
                "enabled": ctx.self_peer.is_some(),
                "active": verdict.active,
                "peerId": ctx.self_peer,
                "winner": verdict.winner,
            }))
        }
        // break a run in flight — ANY run: chat, trigger, control
        // (ADR-005 §3). The id is what every trace, log line and
        // Stopped-bubble debugLink shows. Body `{"hard": bool}`.
        ("POST", ["break", run_id]) => {
            let hard = serde_json::from_str::<Value>(body)
                .ok()
                .and_then(|b| b.get("hard").and_then(|h| h.as_bool()))
                .unwrap_or(false);
            let live = ctx
                .live_runs
                .lock()
                .unwrap()
                .get(*run_id)
                .cloned()
                .context("no run in flight with that id")?;
            crate::triggers::break_live_run(&live, hard);
            on_break(run_id, &live, hard);
            Ok(json!({"run": run_id, "hard": hard}))
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
        // run history = the run summaries (ADR-023 §8): every run this
        // trigger fired, newest first, on the bao/runs/v1 child
        ("GET", ["triggers", id, "runs"]) => {
            let rows = ctx.client.query(
                &ctx.space,
                &ctx.runs_anchor,
                &ctx.runs_ds,
                &json!({"filter": {"triggerId": id}, "sort": ["-startedAt"],
                        "limit": 20}),
            )?;
            Ok(Value::Array(rows))
        }
        // The mutating routes write THROUGH to the dataset (ADR-006 §4:
        // the dataset is the source of truth — a registry-only edit
        // would be reverted by the next reconcile tick).
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
            let rec = trigger_to_record(t);
            let _ = ctx
                .client
                .upsert_record(&ctx.space, &ctx.anchor, &ctx.triggers_ds, id, &rec);
            Ok(rec)
        }
        ("POST", ["triggers", id, "enable"]) => {
            let t = reg.get_mut(*id).context("trigger not found")?;
            t.enabled = true;
            t.consecutive_failures = 0; // manual re-enable resets the breaker
            t.next_due = None;
            let rec = trigger_to_record(t);
            let _ = ctx
                .client
                .upsert_record(&ctx.space, &ctx.anchor, &ctx.triggers_ds, id, &rec);
            Ok(rec)
        }
        ("POST", ["triggers", id, "disable"]) => {
            let t = reg.get_mut(*id).context("trigger not found")?;
            t.enabled = false;
            let rec = trigger_to_record(t);
            let _ = ctx
                .client
                .upsert_record(&ctx.space, &ctx.anchor, &ctx.triggers_ds, id, &rec);
            Ok(rec)
        }
        // one-shot program run (ADR-009 §6): {program, args?} runs a
        // deployed program (serve resolver: space programs + overlay
        // aliases, alias-qualified specs); {source, args?, program?}
        // runs caller-provided program TEXT instead — the entry module
        // comes from the body (program names the trace, default
        // "adhoc@v1"), its use() imports still resolve through the
        // space resolver. Either way → the CLI run envelope {status,
        // value, error, traceRef, durationMs, fuelUsed}. Runs inline
        // on the control thread; the trigger registry lock is released
        // first so a long run never stalls the ticker.
        ("POST", ["run"]) => {
            drop(reg);
            let req: Map<String, Value> = serde_json::from_str(body)
                .context("body must be JSON: {program | source, args?}")?;
            let source = req.get("source").and_then(|v| v.as_str());
            let spec = match req.get("program").and_then(|v| v.as_str()) {
                Some(p) => p,
                None if source.is_some() => "adhoc@v1",
                None => anyhow::bail!(
                    "body must carry program: \"<alias:name@vN>\" or source: \"<python>\""
                ),
            };
            let args = req.get("args").cloned().unwrap_or_else(|| json!({}));
            ctx.run_value(spec, &args, source)
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

    // --- bao.status beats (ADR-025 §7): StubTransport publish sequence --

    /// The single-device role every pre-BOB-111 sequence ran under.
    fn active() -> BeatRole {
        BeatRole {
            responder: true,
            winner: None,
        }
    }

    #[test]
    fn not_answering_log_speaks_only_when_the_state_changes() {
        // ADR-015 §5: the first observation is the baseline (the boot
        // line said it); then one line per change of the reason, one
        // for the flip back to answering, nothing while a state holds
        let standby = || {
            Some(
                "election: standby (the active bao is peer A) — chat is not answered here"
                    .to_string(),
            )
        };
        let moved = || {
            Some(
                "election: standby (the active bao is peer B) — chat is not answered here"
                    .to_string(),
            )
        };
        let mut l = NotAnsweringLog::default();
        assert_eq!(l.note(standby()), None); // baseline, boot line covers it
        assert_eq!(l.note(standby()), None);
        assert_eq!(l.note(standby()), None); // however long it holds
        assert_eq!(l.note(moved()), moved()); // the winner moved
        assert_eq!(l.note(moved()), None);
        assert_eq!(l.note(None), Some(ANSWERING_LINE.to_string())); // answers again
        assert_eq!(l.note(None), None);
        assert_eq!(l.note(standby()), standby()); // a fresh silence is logged once
        assert_eq!(l.note(standby()), None);

        // an answering boot is silent until something changes
        let mut l = NotAnsweringLog::default();
        assert_eq!(l.note(None), None);
        assert_eq!(l.note(None), None);
        assert_eq!(l.note(standby()), standby());
    }

    fn status_calls(log: &crate::testutil::CallLog) -> Vec<Value> {
        log.lock()
            .unwrap()
            .iter()
            .filter(|(m, p, _)| m == "POST" && p == "/v1/events")
            .map(|(_, _, b)| b.clone().unwrap_or_default()["data"].clone())
            .collect()
    }

    #[test]
    fn status_envelope_shape_matches_the_adr() {
        let status: SharedPresence = Default::default();
        let v = status_envelope("peer1", &status, None, "idle", &active(), 100.0);
        assert_eq!(v["type"], "bao.status");
        assert_eq!(v["scope"], "account");
        assert_eq!(v["target"], "peer1");
        assert_eq!(v["data"]["identity"], "peer1");
        assert_eq!(v["data"]["state"], "idle");
        assert!(v["data"]["run"].is_null(), "no run while idle");
        assert!(v["data"].get("line").is_none(), "no line when unset");
    }

    #[test]
    fn status_envelope_carries_the_responder_role() {
        // BOB-111: a standby beats too (state idle) — only `role`
        // tells it from the device that answers chat (the responder,
        // not the election gate); `winner` names the election's holder
        let status: SharedPresence = Default::default();
        let v = status_envelope("peer1", &status, None, "idle", &active(), 100.0);
        assert_eq!(v["data"]["role"], "active");
        assert!(v["data"].get("winner").is_none(), "no claim known");
        let standby = BeatRole {
            responder: false,
            winner: Some("mac".into()),
        };
        let v = status_envelope("peer1", &status, None, "idle", &standby, 100.0);
        assert_eq!(v["data"]["state"], "idle");
        assert_eq!(v["data"]["role"], "standby");
        assert_eq!(v["data"]["winner"], "mac");
    }

    #[test]
    fn presence_pass_republishes_on_a_role_flip() {
        // a takeover/stand-down shows on the bus within a poll, not a
        // full beat later (ADR-025 §2 change signature)
        let (c, log) = scripted(&[]);
        let status: SharedPresence = Default::default();
        let mut st = PresenceLoop::default();
        let standby = BeatRole {
            responder: false,
            winner: Some("mac".into()),
        };
        assert!(presence_pass(
            &c, "peer1", &status, None, &standby, &mut st, 0.0
        )); // boot
        assert!(!presence_pass(
            &c, "peer1", &status, None, &standby, &mut st, 1.0
        )); // not due
        let won = BeatRole {
            responder: true,
            winner: Some("peer1".into()),
        };
        assert!(presence_pass(
            &c, "peer1", &status, None, &won, &mut st, 2.0
        )); // flip ⇒ immediate
        assert!(!presence_pass(
            &c, "peer1", &status, None, &won, &mut st, 3.0
        ));
        let beats = status_calls(&log);
        assert_eq!(beats.len(), 2);
        assert_eq!(beats[0]["role"], "standby");
        assert_eq!(beats[0]["winner"], "mac");
        assert_eq!(beats[1]["role"], "active");
        assert_eq!(beats[1]["winner"], "peer1");
    }

    #[test]
    fn status_envelope_working_carries_run_and_line() {
        let status: SharedPresence = Default::default();
        status.set_line("wiring the UI atom", 99.0);
        let run = json!({"id": "run_1", "title": "impl BOB-73", "startedAt": 1.0});
        let v = status_envelope("peer1", &status, Some(run), "working", &active(), 100.0);
        assert_eq!(v["data"]["state"], "working");
        assert_eq!(v["data"]["run"]["id"], "run_1");
        assert_eq!(v["data"]["run"]["title"], "impl BOB-73");
        assert_eq!(v["data"]["line"], "wiring the UI atom");
        assert_eq!(v["data"]["lineAt"], 99.0);
    }

    #[test]
    fn status_envelope_line_decays_past_90s() {
        let status: SharedPresence = Default::default();
        status.set_line("stale soon", 100.0);
        let at = 100.0;
        let v = status_envelope("peer1", &status, None, "idle", &active(), at + 90.0);
        assert_eq!(v["data"]["line"], "stale soon"); // fresh at the edge
        let v = status_envelope("peer1", &status, None, "idle", &active(), at + 90.001);
        assert!(v["data"].get("line").is_none(), "decayed — machine truth");
    }

    #[test]
    fn current_run_is_the_freshest_stamped_entry_with_live_activity() {
        use crate::triggers::LiveRun;
        let mut runs: BTreeMap<String, LiveRun> = BTreeMap::new();
        assert_eq!(current_run(&runs), None);
        runs.insert(
            "run_a".into(),
            LiveRun {
                stamp: json!({"id": "run_a", "title": "older", "startedAt": 1.0}),
                ..Default::default()
            },
        );
        runs.insert(
            "run_b".into(),
            LiveRun {
                stamp: json!({"id": "run_b", "title": "fresher", "startedAt": 2.0}),
                ..Default::default()
            },
        );
        // a stampless (watcher-style) entry never wins
        runs.insert("chat1".into(), LiveRun::default());
        let run = current_run(&runs).unwrap();
        assert_eq!(run["id"], "run_b");
        assert_eq!(run["cells"], 0);
        assert!(run.get("cell").is_none(), "no preview before a tool call");
        // tool calls noted on the run's activity ride the folded value
        runs.get("run_b")
            .unwrap()
            .activity
            .note_cell(Some("c.query(space,\n  filter)"));
        let run = current_run(&runs).unwrap();
        assert_eq!(run["cells"], 1);
        assert_eq!(run["cell"], "c.query(space, filter)");
    }

    #[test]
    fn run_stamp_title_is_the_chat_text_or_spec() {
        let stamp = run_stamp(
            "run_1",
            "agent:toolcaller@v1",
            &json!({"userText": "  fix the flaky test  "}),
        );
        assert_eq!(stamp["title"], "fix the flaky test");
        let stamp = run_stamp("run_2", "myProg@v1", &json!({}));
        assert_eq!(stamp["title"], "myProg@v1"); // deterministic fallback
    }

    #[test]
    fn presence_pass_publishes_the_adr_sequence() {
        // ADR-025 §7: boot → idle → working (run start ⇒ immediate,
        // +run title) → tool call ⇒ immediate (+cells/cell) → line set
        // ⇒ immediate → run end ⇒ immediate idle → cadence beat with
        // the line decayed → shutdown. Runs come and go through the
        // live-runs registry, exactly as `RunCtx::run` maintains it.
        use crate::triggers::LiveRun;
        let (c, log) = scripted(&[]);
        let status: SharedPresence = Default::default();
        let mut runs: BTreeMap<String, LiveRun> = BTreeMap::new();
        let mut st = PresenceLoop::default();
        let mut t = 0.0;
        let pass = |runs: &BTreeMap<String, LiveRun>, st: &mut PresenceLoop, t| {
            presence_pass(&c, "peer1", &status, current_run(runs), &active(), st, t)
        };
        pass(&runs, &mut st, t); // boot beat at once
        pass(&runs, &mut st, t + 1.0); // cadence: not due
        t += 10.0;
        pass(&runs, &mut st, t); // idle
        runs.insert(
            "run_1".into(),
            LiveRun {
                stamp: json!({"id": "run_1", "title": "hello there", "startedAt": t}),
                ..Default::default()
            },
        );
        // run start republishes within a poll — no cadence wait
        pass(&runs, &mut st, t + 1.0);
        runs.get("run_1")
            .unwrap()
            .activity
            .note_cell(Some("c.query(space)"));
        // ...and so does every tool call (the live counter)
        pass(&runs, &mut st, t + 2.0);
        status.set_line("greeting the user", t + 3.0);
        pass(&runs, &mut st, t + 3.0); // immediate on set
        runs.remove("run_1");
        pass(&runs, &mut st, t + 4.0); // immediate idle
        pass(&runs, &mut st, t + 5.0); // no change: not due
        t += 95.0; // past the 90s decay from the line's set (ADR-025 §3)
        pass(&runs, &mut st, t); // cadence, line gone
        publish_status_beat(&c, "peer1", &status, None, "shutdown", &active(), t + 1.0);
        let mut i = status_calls(&log).into_iter();
        let boot = i.next().unwrap();
        assert_eq!(boot["state"], "boot");
        assert_eq!(boot["identity"], "peer1");
        let idle = i.next().unwrap();
        assert_eq!(idle["state"], "idle");
        let working = i.next().unwrap();
        assert_eq!(working["state"], "working");
        assert_eq!(working["run"]["title"], "hello there");
        assert_eq!(working["run"]["cells"], 0);
        let called = i.next().unwrap();
        assert_eq!(called["run"]["cells"], 1);
        assert_eq!(called["run"]["cell"], "c.query(space)");
        let lined = i.next().unwrap();
        assert_eq!(lined["line"], "greeting the user");
        let idle2 = i.next().unwrap();
        assert_eq!(idle2["state"], "idle");
        assert!(idle2["run"].is_null(), "run ended");
        let idle3 = i.next().unwrap();
        assert_eq!(idle3["state"], "idle");
        assert!(idle3.get("line").is_none(), "line decayed");
        let last = i.next().unwrap();
        assert_eq!(last["state"], "shutdown");
        assert!(i.next().is_none());
    }

    #[test]
    fn presence_pass_resets_gen_on_publish_only() {
        // a beat swallows the change signature; without one a set waits
        // at most a beat interval, never a full cadence
        let (c, _log) = scripted(&[]);
        let status: SharedPresence = Default::default();
        let mut st = PresenceLoop::default();
        assert!(presence_pass(
            &c,
            "p",
            &status,
            None,
            &active(),
            &mut st,
            0.0
        ));
        status.set_line("x", 0.5);
        assert!(presence_pass(
            &c,
            "p",
            &status,
            None,
            &active(),
            &mut st,
            0.5
        ));
        assert!(!presence_pass(
            &c,
            "p",
            &status,
            None,
            &active(),
            &mut st,
            1.0
        ));
    }

    #[test]
    fn ensure_type_hides_a_listed_type_that_is_not_hidden_yet() {
        // harness types are hidden (ADR-027 §2): a row found by xKey
        // without the flag gets one PATCH, no create
        let (c, log) = scripted(&[
            (
                200,
                json!({"types": [{"id": "t-cfg", "xKey": "agent_config", "name": "Agent Config"}]}),
            ),
            (204, json!({})),
        ]);
        assert_eq!(
            ensure_type(&c, "s1", "Agent Config", "agent_config").unwrap(),
            "t-cfg"
        );
        let calls = log.lock().unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].1, "/v1/spaces/s1/types?includeHidden=true");
        assert_eq!(
            (calls[1].0.as_str(), calls[1].1.as_str(), calls[1].2.clone()),
            (
                "PATCH",
                "/v1/spaces/s1/types/t-cfg",
                Some(json!({"hidden": true}))
            )
        );
    }

    #[test]
    fn provisioning_declares_parts_and_reads_collections_back() {
        // ADR-027 §1/§2 end to end over the in-memory server: the chat
        // is the catalog's derived root; every store is a part of its
        // hidden type; the collection is read back, never composed;
        // a second boot adopts everything
        let c = Client::with_transport(Box::new(crate::testutil::FakeSpace::new()));
        assert_eq!(general_chat(&c, "sp").unwrap(), "chat-sp");
        let stores = provision_agent_stores(&c, "sp").unwrap();
        for (ds, key) in [
            (&stores.config_ds, "_agent_config"),
            (&stores.secrets_ds, "_agent_secrets"),
            (&stores.triggers_ds, "_agent_triggers"),
            (&stores.runs_ds, "_agent_runs"),
        ] {
            assert!(ds.ends_with(key), "{ds}");
        }
        // triggers and runs share the trigger type's collection prefix
        assert_eq!(
            stores.triggers_ds.trim_end_matches("_agent_triggers"),
            stores.runs_ds.trim_end_matches("_agent_runs")
        );
        for t in c.list_types("sp").unwrap() {
            if t["xKey"].as_str().unwrap_or("").starts_with("agent_") {
                assert_eq!(t["hidden"], json!(true), "{t}");
            }
        }
        // a write by the resolved collection lands; by the bare key it
        // is refused (the server knows no such records dataset)
        c.upsert_record(
            "sp",
            &stores.config,
            &stores.config_ds,
            "k",
            &json!({"key": "k"}),
        )
        .unwrap();
        let err = c
            .upsert_record(
                "sp",
                &stores.config,
                "agent_config",
                "k",
                &json!({"key": "k"}),
            )
            .unwrap_err();
        assert_eq!(err.code, "dataset.unknown");
        let again = provision_agent_stores(&c, "sp").unwrap();
        assert_eq!(again.config, stores.config);
        assert_eq!(again.config_ds, stores.config_ds);
        assert_eq!(again.runs_ds, stores.runs_ds);
        // the chat's log child derives under the catalog bundle
        let child = c
            .bundle_child("sp", "system:general-chat/v1", "bao/log/v1", &[])
            .unwrap();
        assert!(child["objectId"].as_str().is_some_and(|s| !s.is_empty()));
    }

    #[test]
    fn ensure_type_creates_when_nothing_matches() {
        // the meta-type catalog row (id == xKey == "type") is not a
        // name-match candidate — a fresh space still creates
        let (c, log) = scripted(&[
            (
                200,
                json!({"types": [{"id": "type", "xKey": "type", "name": "Type"}]}),
            ),
            (201, json!({"typeId": "t-new"})),
        ]);
        assert_eq!(
            ensure_type(&c, "s1", "Agent Config", "agent_config").unwrap(),
            "t-new"
        );
        let calls = log.lock().unwrap();
        assert_eq!(calls[0].1, "/v1/spaces/s1/types?includeHidden=true");
        assert_eq!(calls[1].1, "/v1/spaces/s1/types");
        // created hidden from the first change (ADR-027 §2)
        assert_eq!(
            calls[1].2,
            Some(json!({"name": "Agent Config", "xKey": "agent_config", "hidden": true}))
        );
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

    fn chat_setup_reply(installed: bool) -> (u16, Value) {
        (
            200,
            json!({"usecase": "general-chat", "bundles": [{
                "usecase": "general-chat", "id": "system:general-chat/v1",
                "bundle": {"id": "system:general-chat/v1", "rootId": "chat-root",
                           "roots": ["chat-root"], "derived": true},
                "installed": installed, "typeId": "chat-root",
                "miniapp": {"bundle": "system:general-chat/v1"}}]}),
        )
    }

    #[test]
    fn general_chat_is_the_catalog_setup_root() {
        // one call, adopt or install alike (ADR-027 §1)
        for installed in [true, false] {
            let (c, log) = scripted(&[chat_setup_reply(installed)]);
            assert_eq!(general_chat(&c, "sp").unwrap(), "chat-root");
            let calls = log.lock().unwrap();
            assert_eq!(calls.len(), 1);
            assert_eq!(
                (calls[0].0.as_str(), calls[0].1.as_str(), calls[0].2.clone()),
                (
                    "POST",
                    "/v1/catalog/general-chat/setup",
                    Some(json!({"spaceId": "sp"}))
                )
            );
        }
    }

    #[test]
    fn general_chat_retries_not_ready_then_lands() {
        // a winner's tree still syncing to this device: retried, never
        // installed around
        let (c, log) = scripted(&[
            (
                409,
                json!({"error": {"code": "bundle.not_ready", "message": "syncing"}}),
            ),
            chat_setup_reply(false),
        ]);
        assert_eq!(general_chat(&c, "sp").unwrap(), "chat-root");
        assert_eq!(log.lock().unwrap().len(), 2);
    }

    #[test]
    fn general_chat_rejects_a_non_derived_root() {
        // a created root under the catalog id is not a general chat:
        // stop, name the object, never fall back to it
        let (c, log) = scripted(&[(
            200,
            json!({"usecase": "general-chat", "bundles": [{
                "id": "system:general-chat/v1",
                "bundle": {"id": "system:general-chat/v1", "rootId": "old-root"},
                "installed": false}]}),
        )]);
        let err = general_chat(&c, "sp").unwrap_err().to_string();
        assert!(
            err.starts_with("derived general chat not found in space sp"),
            "{err}"
        );
        assert!(err.contains("old-root"), "{err}");
        assert_eq!(log.lock().unwrap().len(), 1);
    }

    #[test]
    fn chat_watch_seeds_once_and_leaves_an_existing_record_alone() {
        // fresh space: one read, one write of the bare id
        let (c, log) = scripted(&[(200, json!({"records": []})), (200, json!({}))]);
        let (id, seeded) =
            seed_chat_watch(&c, "sp", "anchor", "t_agent_triggers", "chat-1", "peer-A").unwrap();
        assert_eq!(id, "chat-watch");
        let t = seeded.expect("this boot created it");
        assert_eq!((t.kind.as_str(), t.owner.as_str()), ("event", "peer-A"));
        assert_eq!(t.spec["objectId"], json!("chat-1"));
        let calls = log.lock().unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[1].1, "/v1/spaces/sp/modify");
        assert_eq!(
            calls[1].2.as_ref().unwrap()["records"][0]["id"],
            json!("chat-watch")
        );
        drop(calls);

        // an existing (even generation-suffixed) record is user state:
        // owner/enabled untouched, no write
        let (c, log) = scripted(&[(
            200,
            json!({"records": [{"id": "rollup"}, {"id": "chat-watch-g2", "owner": "peer-B",
                                "enabled": false}]}),
        )]);
        let (id, seeded) =
            seed_chat_watch(&c, "sp", "anchor", "t_agent_triggers", "chat-1", "peer-A").unwrap();
        assert_eq!(id, "chat-watch-g2");
        assert!(seeded.is_none());
        assert_eq!(log.lock().unwrap().len(), 1);
    }

    #[test]
    fn chat_watch_reseeds_under_a_generation_when_tombstoned() {
        let (c, log) = scripted(&[
            (200, json!({"records": []})),
            (
                200,
                json!({"rejections": [{"recordId": "chat-watch",
                                          "reason": "upsert.record_deleted"}]}),
            ),
            (200, json!({})),
        ]);
        let (id, seeded) =
            seed_chat_watch(&c, "sp", "anchor", "t_agent_triggers", "chat-1", "").unwrap();
        assert_eq!(id, "chat-watch-g2");
        assert_eq!(seeded.unwrap().owner, ""); // standby boot: unassigned
        let calls = log.lock().unwrap();
        assert_eq!(calls.len(), 3);
        assert_eq!(
            calls[2].2.as_ref().unwrap()["records"][0]["id"],
            json!("chat-watch-g2")
        );
    }

    #[test]
    fn general_chat_requires_the_catalog_route() {
        // no-backcompat: a server without the catalog is unsupported —
        // error, never a registry or SpaceInfo-field fallback
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
        // server stamps are instants (ADR-019); the backlog cut reads
        // authorship, never the stamp, so an older peer's bare seconds
        // (see `msg_legacy`) must walk the same
        let mut m = json!({"id": id, "text": text,
            "createdAt": {"$date": "2026-08-25T16:00:00.000Z"}});
        if agent {
            m["agent"] = json!({"name": "bao", "done": true});
        }
        m
    }

    #[test]
    fn records_in_takes_added_only_a_reaction_or_edit_is_not_input() {
        // a reaction toggle / text edit on an old message arrives as an
        // `updated` entry; it must never reach the watcher as a message
        // (it would Start a run for a message the agent already
        // answered — the seen-set only knows the unanswered backlog)
        let data = json!([{
            "versionId": "v9",
            "added": [{"id": "u7", "doc": {"text": "new question"}}],
            "updated": [{"id": "u1", "doc": {"text": "old question",
                "reactions": {"👍": {"acct": {"$date": "2026-09-10T10:00:00.000Z"}}}}}]
        }]);
        let recs = records_in(&data);
        let ids: Vec<&str> = recs.iter().map(|r| r["id"].as_str().unwrap()).collect();
        assert_eq!(ids, ["u7"]);
        assert_eq!(recs[0]["text"], "new question");
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
    fn snapshot_backlog_keeps_control_records_and_scans_past_progress_bubbles() {
        // a Stop pressed during a feed gap arrives text-less in the
        // snapshot; a done:false progress bubble posted by the live
        // run must not cut the scan before it (review)
        let brk = json!({"id": "b1", "text": "",
            "control": {"kind": "break", "hard": true},
            "createdAt": {"$date": "2026-08-25T16:00:01.000Z"}});
        let mut progress = msg("a2", "working on it…", true);
        progress["agent"]["done"] = json!(false);
        let data = json!({"records": [
            progress,
            brk,
            msg("u2", "do the thing", false),
            msg("a1", "reply", true),
        ]});
        let out = snapshot_backlog(&data, "bao");
        assert_eq!(out.len(), 2);
        assert_eq!(out[0]["id"], "u2"); // oldest first
        assert_eq!(out[1]["id"], "b1");
        assert!(crate::triggers::is_control(&out[1]));
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
        with_atts["attachments"] = json!({"a0": {"type": "link", "link": "any://o/sp1/obj1"}});
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

    #[test]
    fn bootstrap_config_seeds_defaults_only_where_no_row() {
        // ADR-006 §3: soft seeds persist-if-missing; a stored row is
        // left alone (the json default never rewrites a space)
        let defaults = crate::config::config_defaults();
        let n = defaults.len();
        let stored_ws = json!({"provider": "gemini", "model": "custom-model"});
        let mut replies = vec![(
            200,
            json!({"records": [{"id": "search.provider.websearch",
                                "key": "search.provider.websearch",
                                "value": stored_ws}]}),
        )];
        for _ in 0..n - 1 {
            replies.push((200, json!({})));
        }
        let (c, log) = scripted(&replies);
        bootstrap_config(&c, "s1", "cfgobj", "t1_agent_config", &BTreeMap::new());
        let calls = log.lock().unwrap();
        // one query + one upsert per missing default (the stored key skipped)
        assert_eq!(calls.len(), n);
        let written: Vec<&str> = calls[1..]
            .iter()
            .map(|c| c.2.as_ref().unwrap()["records"][0]["id"].as_str().unwrap())
            .collect();
        assert!(!written.contains(&"search.provider.websearch"));
        assert!(written.contains(&"llm.tier.codegen"));
        assert!(calls[1].1.ends_with("/modify"));
    }

    #[test]
    fn bootstrap_config_hard_seed_overwrites_a_differing_row() {
        let defaults = crate::config::config_defaults();
        let rows: Vec<Value> = defaults
            .iter()
            .map(|(k, v)| json!({"id": k, "key": k, "value": v}))
            .collect();
        let (c, log) = scripted(&[(200, json!({"records": rows})), (200, json!({}))]);
        let mut hard = BTreeMap::new();
        let forced = json!({"provider": "gemini", "model": "rig-model"});
        hard.insert("search.provider.deepresearch".to_string(), forced.clone());
        bootstrap_config(&c, "s1", "cfgobj", "t1_agent_config", &hard);
        let calls = log.lock().unwrap();
        assert_eq!(calls.len(), 2);
        let rec = &calls[1].2.as_ref().unwrap()["records"][0];
        assert_eq!(rec["id"], "search.provider.deepresearch");
        assert_eq!(rec["upsert"], true);
        assert_eq!(
            rec["ops"][1],
            json!({"type": "$set", "path": "value", "value": forced})
        );
    }

    #[test]
    fn bootstrap_config_store_unreachable_writes_nothing() {
        let (c, log) = scripted(&[(500, json!({"error": "boom"}))]);
        bootstrap_config(&c, "s1", "cfgobj", "t1_agent_config", &BTreeMap::new());
        assert_eq!(log.lock().unwrap().len(), 1);
    }

    #[test]
    fn model_setup_marker_needs_the_codegen_key_and_untouched_seed_rows() {
        let defaults = crate::config::config_defaults();
        let seeded = |key: &str| defaults.get(key).cloned();
        // a fresh space asking for the seeded model key → chooser
        assert!(is_model_setup("llm.key.anthropic", seeded));
        // extra inferred keys / a trailing slash on a row keep equality
        let decorated = |key: &str| {
            defaults.get(key).cloned().map(|mut v| {
                v["backend"] = json!("anthropic");
                v["base_url"] = json!("https://api.anthropic.com/");
                v
            })
        };
        assert!(is_model_setup("llm.key.anthropic", decorated));
        // a different missing ref (a connector key) → plain card
        assert!(!is_model_setup("connector.key.github", seeded));
        // a chosen provider (rows differ from the seed) → plain card
        let chosen = |key: &str| {
            defaults.get(key).cloned().map(|mut v| {
                v["model"] = json!("z-ai/glm-5.3");
                v["api_key_ref"] = json!("llm.key.openrouter");
                v
            })
        };
        assert!(!is_model_setup("llm.key.openrouter", chosen));
        // no store rows at all → plain card
        assert!(!is_model_setup("llm.key.anthropic", |_| None));
    }

    #[test]
    fn run_config_store_binds_nothing_without_a_bao_bundle() {
        // a space that is not a bao space (or was never served): a
        // one-shot run provisions nothing — locked read, no writes
        let (c, log) = scripted(&[(200, json!({"bundles": [], "synced": true}))]);
        let store = run_config_store(&Arc::new(c), "s1", BTreeMap::new()).unwrap();
        assert!(store.is_none());
        let calls = log.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "GET");
        assert!(calls[0].1.ends_with("/spaces/s1/bundles"));
    }

    #[test]
    fn shadowed_config_store_reads_overrides_first_and_writes_through() {
        use crate::broker::ConfigStore as _;
        let (c, log) = scripted(&[
            (
                200,
                json!({"records": [{"id": "llm.tier.codegen", "key": "llm.tier.codegen",
                                      "value": {"model": "space"}}]}),
            ),
            (200, json!({"ok": true})),
        ]);
        let mut overrides = BTreeMap::new();
        overrides.insert("llm.tier.classify".to_string(), json!({"model": "shadow"}));
        let store = ShadowedConfigStore {
            inner: ServeConfigStore::new(Arc::new(c), "s1", "cfgobj", "t1_agent_config"),
            overrides,
        };
        // shadowed key: answered locally, no wire call
        assert_eq!(
            store.read("llm.tier.classify").unwrap(),
            Some(json!({"model": "shadow"}))
        );
        assert_eq!(log.lock().unwrap().len(), 0);
        // other keys read through to the space
        assert_eq!(
            store.read("llm.tier.codegen").unwrap(),
            Some(json!({"model": "space"}))
        );
        // writes always land in the space — even for a shadowed key
        store
            .set("llm.tier.classify", &json!({"model": "m2"}))
            .unwrap();
        let calls = log.lock().unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[1].0, "POST");
    }

    #[test]
    fn serve_config_store_reads_through_by_key() {
        let (c, log) = scripted(&[(
            200,
            json!({"records": [{"id": "llm.tier.codegen", "key": "llm.tier.codegen",
                                "value": {"model": "m"}}]}),
        )]);
        let store = ServeConfigStore {
            client: Arc::new(c),
            space: "s1".into(),
            obj: "cfgobj".into(),
            dataset: "t1_agent_config".into(),
        };
        use crate::broker::ConfigStore as _;
        assert_eq!(
            store.read("llm.tier.codegen").unwrap(),
            Some(json!({"model": "m"}))
        );
        let calls = log.lock().unwrap();
        assert!(calls[0].1.ends_with("/query"));
        // the resolved collection, never the bare key (ADR-027 §2)
        assert_eq!(calls[0].2.as_ref().unwrap()["dataset"], "t1_agent_config");
    }
}
