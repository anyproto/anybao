//! `anyrt serve` — the outer loop: ensure space/chat/anchor, resolve
//! overlays from the space (space-only, ADR-009 §5 — `anyrt deploy`
//! is the publish step; the kernel is embedded), then watch the chat
//! (SSE; the snapshot seeds the unanswered-message backlog, ADR-009
//! §8), tick triggers, and answer the localhost control API.
//! Conversations and trigger runs are guest programs through the
//! shared cage.

// ADR-034 §7: managed startup is available without the desktop serve dependencies.
pub use crate::managed::{start_managed, CancelToken, ManagedAgent, ManagedOptions};
pub use crate::stores::{
    alias_map, dataset_collection, store_collection, AgentStores, DeclaredTable, ServeConfigStore,
    ServeSecretStore, ShadowedConfigStore,
};
// ADR-025 / ADR-009 §8 helpers shared with the managed host (N2b-1): moved
// verbatim to `presence` / `chatwatch`, re-exported here for the callers.
pub use crate::presence::STATUS_BEAT_S;

use crate::anyapi::Client;
use crate::broker::{Broker, DeclaredCredentials, SharedMailbox, SharedPresence};
use crate::chatwatch::{
    escalate_soft_break, follow_up_body, follow_ups, preview, records_in, release_chat_watch,
    resolve_reply, seed_chat_watch, snapshot_backlog, FollowUp, BREAK_GRACE,
};
use crate::config::Config;
use crate::presence::{
    current_run, presence_pass, publish_status_beat, status_envelope, BeatRole, PresenceLoop,
};
use crate::resolver::{AnyModuleResolver, ModuleResolver};
use crate::routes::Classifier;
use crate::runner::{run_program, Cage};
use crate::stores::{
    now_rfc3339, persist_local_secret, stored_local_secret, upsert_config_row, upsert_secret_row,
    SECRETS_FIELD,
};
use crate::trace::TraceWriter;
use crate::tracestore::TraceStore;
use crate::triggers::{
    desired_event_sources, event_args, event_targets, health_pass, owned_chat_watch,
    reconcile_registry, record_to_trigger, rollup, standing_triggers, trigger_to_record, ChatInput,
    EventSource, LiveRun, RunResult, Scheduler, Trigger, WatchAction, Watcher, CHAT_MESSAGES,
};
use anyhow::{Context, Result};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
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
    type_id: &str,
) -> Result<String> {
    let mut last_err = None;
    for _ in 0..5 {
        match c.bundle_child(space, bundle, seed, type_id, &[]) {
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
        // default — a derived root could never be uninstalled. The root
        // is a plain document: type `page` (ADR-029 §7), filed nowhere.
        match c.ensure_bundle(space, "bao/v1", "bao", "page", &[], false) {
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
        config: bundle_child_retry(c, space, "bao/v1", "bao/config/v1", &cfg_t)?,
        secrets: bundle_child_retry(c, space, "bao/v1", "bao/secrets/v1", &sec_t)?,
        triggers: bundle_child_retry(c, space, "bao/v1", "bao/triggers/v1", &trg_t)?,
        runs: bundle_child_retry(c, space, "bao/v1", "bao/runs/v1", &trg_t)?,
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
/// The secrets store's key (its value field is `stores::SECRETS_FIELD`):
/// the dedicated home of account-scoped secrets (ADR-021 §4), split out
/// of `agent_config` so
/// the broker can block guest reads of the whole collection (by the
/// `_agent_secrets` suffix, ADR-011 §4) and of the object while the
/// config object stays guest-readable.
const SECRETS_KEY: &str = "agent_secrets";
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
        let patch = crate::managed::takeover::declared_stamp_patch(&about, existing);
        match upsert_secret_row(c, space, obj, dataset, &key, &Value::Object(patch)) {
            Ok(()) => stamped += 1,
            Err(e) => warn!("credentials: could not stamp declared {key} ({e})"),
        }
    }
    info!("credentials: {stamped} declared refs stamped from the overlays");
}

struct Shared {
    triggers: Mutex<BTreeMap<String, Trigger>>,
    scheduler: Mutex<Scheduler>,
    watcher: Mutex<Watcher>,
    /// User texts awaiting readiness (ADR-009 §8): snapshot backlog,
    /// live messages that arrived while an overlay was unreadable, and
    /// messages deferred on a program miss. Drained by the trigger
    /// ticker once ensure_ready clears.
    backlog: Mutex<Vec<ChatInput>>,
    /// Live event sources (ADR-018 §2): `(space, chat object id)` → the
    /// stop flag of the thread watching it. Converged on the registry
    /// every tick.
    event_sources: Mutex<BTreeMap<(String, String), Arc<AtomicBool>>>,
    /// The chat-responder record id (ADR-018 §3) — `chat-watch`, or a
    /// generation-suffixed reseed when the bare id was tombstoned.
    chat_watch_id: String,
}

/// The presence thread's poll: how fast a `bao.status` set republishes.
const STATUS_POLL_S: f64 = 1.0;

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

/// How long serve waits for a missed program to sync before calling it
/// absent (ADR-009 §8): long enough for a cold guest join over a slow
/// link, short enough that a phantom `use()` cannot wedge the loop.
pub const PROGRAM_MISS_PATIENCE: Duration = Duration::from_secs(600);

/// What serve is waiting for before it runs (ADR-009 §8). Readiness is
/// "usable", never "up to date": a device with a complete-but-older
/// program set runs (offline included); only a configured space it
/// cannot read yet, or a program a run asked for and did not find,
/// holds the loop — and the program wait is bounded.
#[derive(Default)]
pub struct WaitSet {
    /// overlay name → space id: configured overlays this account can't
    /// read yet (join pending / still loading). No deadline — nothing
    /// resolves without them, and the join is the only cure.
    overlays: BTreeMap<String, String>,
    /// `(space, name@vN)` → first miss: programs a run asked for that
    /// weren't on this device. A deploy that hasn't synced here yet
    /// and a program that was never deployed look the same, so the
    /// wait gives up after `PROGRAM_MISS_PATIENCE`.
    programs: BTreeMap<(String, String), Instant>,
}

impl WaitSet {
    pub fn overlays(pending: BTreeMap<String, String>) -> Self {
        WaitSet {
            overlays: pending,
            ..Default::default()
        }
    }

    pub fn is_empty(&self) -> bool {
        self.overlays.is_empty() && self.programs.is_empty()
    }

    /// Register a run's misses; returns how many were new.
    pub fn wait_for_programs(&mut self, missed: &[(String, String)], now: Instant) -> usize {
        let mut added = 0;
        for key in missed {
            if let std::collections::btree_map::Entry::Vacant(e) = self.programs.entry(key.clone())
            {
                e.insert(now);
                added += 1;
            }
        }
        added
    }

    /// Re-probe every item and drop what is there now (or what the
    /// wait has given up on). `Ok` = nothing left. The error text is
    /// user-facing status: the watcher bubbles it into the chat.
    pub fn settle(
        &mut self,
        overlay_ok: impl Fn(&str) -> bool,
        program_ok: impl Fn(&str, &str) -> bool,
        now: Instant,
    ) -> Result<()> {
        self.overlays.retain(|_, sid| !overlay_ok(sid));
        self.programs.retain(|(space, spec), since| {
            if program_ok(space, spec) {
                info!("program `{spec}` arrived (space `{space}`)");
                return false;
            }
            if now.duration_since(*since) >= PROGRAM_MISS_PATIENCE {
                warn!(
                    "program `{spec}` (space `{space}`) did not arrive in {}s — giving up the \
                     wait; the next run fails on it",
                    PROGRAM_MISS_PATIENCE.as_secs()
                );
                return false;
            }
            true
        });
        if self.is_empty() {
            return Ok(());
        }
        let list = self
            .overlays
            .iter()
            .map(|(name, sid)| format!("overlay `{name}` (space `{sid}`) still joining/syncing"))
            .chain(self.programs.keys().map(|(space, spec)| {
                format!("program `{spec}` hasn't synced to this device yet (space `{space}`)")
            }))
            .collect::<Vec<_>>()
            .join("; ");
        anyhow::bail!("{list}")
    }
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
    // the wait set (§8) only gates program runs
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
        waiting: Mutex::new(WaitSet::overlays(pending)),
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
        roster: Mutex::new(None),
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
/// boot, then hourly; a no-op unless `[traces] retain_*` / `max_bytes`
/// is set (every one has a default, so it always runs).
fn retention_thread(ctx: Arc<RunCtx>, stop: Arc<AtomicBool>) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let configured = ctx.cfg.retain_conversations_s.is_some()
            || ctx.cfg.retain_jobs_s.is_some()
            || ctx.cfg.max_trace_bytes.is_some();
        if !configured {
            return;
        }
        sliced_sleep(Duration::from_secs(60), &stop);
        while !stop.load(Ordering::Relaxed) {
            // the sweep reads `stop` between requests: a shutdown ends
            // it at the next page, never a server-side read in flight
            // outliving the server (SYN-388)
            match ctx.expire_traces(&stop) {
                Ok(_) => {}
                Err(_) if stop.load(Ordering::Relaxed) => {
                    info!("trace retention: sweep stopped for shutdown")
                }
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
    /// what the loop is waiting for (ADR-009 §8): unreadable overlays
    /// and missed programs — gates program runs, never the cage
    pub waiting: Mutex<WaitSet>,
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
    /// the chat space's member roster for the sender fold (BOB-192,
    /// ADR-005 §5), with the instant it was read — refreshed by
    /// `resolve_sender` on age or on an unknown creator; None until
    /// the first successful read
    pub roster: Mutex<Option<(Instant, crate::triggers::Roster)>>,
}

impl RunCtx {
    pub fn new_run_id() -> String {
        format!("run_{}", &uuid::Uuid::new_v4().simple().to_string()[..16])
    }

    /// Cheap readiness check (no network, never clears pending —
    /// that's `ensure_ready`'s probe).
    pub fn is_ready(&self) -> bool {
        self.waiting.lock().unwrap().is_empty()
    }

    /// Re-probe the wait set (ADR-009 §8): an overlay clears when the
    /// space list shows it active, a missed program when it resolves.
    /// The error text is user-facing status — the watcher bubbles it
    /// into the chat while not ready.
    pub fn ensure_ready(&self) -> Result<()> {
        let mut waiting = self.waiting.lock().unwrap();
        if waiting.is_empty() {
            return Ok(());
        }
        let settled = waiting.settle(
            |sid| matches!(tracked_status(&self.client, sid), Ok(Some(ref st)) if st == "active"),
            |space, spec| self.program_present(space, spec),
            Instant::now(),
        );
        if settled.is_ok() {
            info!("wait set clear — agent ready");
        }
        settled
    }

    /// Does `name@vN` resolve in `space` on this device right now? The
    /// same resolver a run uses, addressed by raw space id (strict).
    fn program_present(&self, space: &str, spec: &str) -> bool {
        AnyModuleResolver::new(self.client.clone(), &self.space, None, self.aliases.clone())
            .resolve(&format!("{space}:{spec}"), None)
            .is_ok()
    }

    /// The synced per-run summary (ADR-023 §1): one `agent_runs` record
    /// per run in the `bao/runs/v1` child — every device sees every
    /// device's runs. Best-effort: the trace itself is already landed;
    /// a failed publish is logged, never fails the run.
    fn publish_run(&self, summary: Value, trigger: Option<&str>) {
        let id = summary["id"].as_str().unwrap_or_default().to_string();
        if let Err(e) = crate::tracestore::publish_run_summary(
            &self.client,
            &self.space,
            &self.runs_anchor,
            &self.runs_ds,
            self.self_peer.as_deref(),
            summary,
            trigger,
        ) {
            warn!("agent_runs {id}: summary not published: {e}");
        }
    }

    /// Retention pass (ADR-023 §6): drop run bodies past the configured
    /// ages; summaries stay. No-op when nothing is configured.
    /// One retention sweep (ADR-023 §6). `stop` = the serve's shutdown
    /// flag; the sweep ends between requests once it is set.
    pub fn expire_traces(&self, stop: &AtomicBool) -> Result<usize> {
        let now = now_s();
        let conv = self.cfg.retain_conversations_s.map(|s| now - s as f64);
        let jobs = self.cfg.retain_jobs_s.map(|s| now - s as f64);
        let max = self.cfg.max_trace_bytes;
        if conv.is_none() && jobs.is_none() && max.is_none() {
            return Ok(0);
        }
        self.traces.expire(CHAT_PROGRAM, conv, jobs, max, stop)
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
                missing_programs: outcome.broker.missing_programs.clone(),
                refused_programs: outcome.broker.refused_programs.clone(),
                mutated: outcome.broker.mutations > 0,
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

/// Post the credential-request bubbles for this run's missing refs
/// (ADR-021 §2): the shared body in `chatwatch` (N2b-2 C0), fed from this
/// serve's stores. No secrets store = nothing to post.
fn post_credential_requests(ctx: &RunCtx, refs: &[String]) -> usize {
    let Some(store) = &ctx.secret_store else {
        return 0;
    };
    crate::chatwatch::post_credential_requests(
        &ctx.client,
        &ctx.space,
        &ctx.chat,
        &store.obj,
        &store.dataset,
        ctx.config_store
            .as_deref()
            .map(|cs| cs as &dyn crate::broker::ConfigStore),
        &ctx.cfg.agent_name,
        refs,
        // the shared body's warnings, under this module's target as before
        &|warning| warn!("{warning}"),
    )
}

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
        escalate_soft_break(&chat, &live);
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
        .bundle_child(&ctx.space, &root, "bao/log/v1", &tid, &[])?;
    let log = child["objectId"]
        .as_str()
        .context("bundle_child returned no objectId")?
        .to_string();
    // the seq probe and the record are shared with the managed host
    // (`chatwatch`, N2b-2 C0)
    let seq = crate::chatwatch::next_turn_seq(&ctx.client, &ctx.space, &log, &turns)?;
    ctx.client.upsert_record(
        &ctx.space,
        &log,
        &turns,
        &format!("{seq:08}"),
        &crate::chatwatch::interrupted_turn_record(seq, &ctx.cfg.agent_name, user_text, trace_ref),
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
                                  "context": input.context,
                                  "messageId": input.message_id}));
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
            // the selection and the bubbles are shared with the managed
            // host (`chatwatch::follow_ups`, N2b-2 C0): the credential
            // cards first, then at most one follow-up, in this order
            let posted = post_credential_requests(&ctx, &rr.missing_secrets);
            for follow_up in follow_ups(rr, posted, input.deferrals) {
                if let FollowUp::ProgramDeferred { specs } = &follow_up {
                    let added = ctx
                        .waiting
                        .lock()
                        .unwrap()
                        .wait_for_programs(&rr.missing_programs, Instant::now());
                    warn!(
                        "deferred on a program miss ({specs}, {added} new) — re-run once it syncs"
                    );
                }
                let _ = ctx.client.chat_send(
                    &ctx.space,
                    &ctx.chat,
                    &follow_up_body(&follow_up, &ctx.cfg.agent_name, trace_ref),
                );
                match follow_up {
                    FollowUp::ProgramDeferred { .. } => {
                        shared.backlog.lock().unwrap().push(ChatInput {
                            deferrals: input.deferrals + 1,
                            ..input.clone()
                        });
                    }
                    FollowUp::Stopped => {
                        // …and writes the turn the guest could not (ADR-005 §3):
                        // without it the next boot window has no trace of the
                        // stopped request and the model's view of the
                        // conversation diverges from the chat on screen
                        if let Err(e) = append_interrupted_turn(&ctx, &input.text, trace_ref) {
                            warn!("interrupted turn not logged (trace {trace_ref}): {e}");
                        }
                    }
                    FollowUp::StillWaiting { .. } | FollowUp::Error { .. } => {}
                }
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
                        message_id: item["messageId"].as_str().unwrap_or_default().to_string(),
                        deferrals: 0,
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
                    let record = resolve_reply(
                        &ctx.client,
                        &ctx.space,
                        &ctx.chat,
                        resolve_sender(ctx, record),
                    );
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
                    let record = resolve_reply(
                        &ctx.client,
                        &ctx.space,
                        &ctx.chat,
                        resolve_sender(ctx, record),
                    );
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

/// How long a roster read stays fresh — a rename or a join shows up
/// within this; an unknown creator forces an earlier re-read, at most
/// once per `ROSTER_MISS_BACKOFF` so a stream of messages from an
/// identity nobody lists does not hammer the members route.
const ROSTER_TTL: Duration = Duration::from_secs(600);
const ROSTER_MISS_BACKOFF: Duration = Duration::from_secs(30);

/// A human message's sender, resolved against the chat space's member
/// roster before the watcher sees the record (ADR-005 §5, BOB-192):
/// the roster is read once and cached on the ctx, re-read when stale
/// or when the creator is not in it; a creator the roster never
/// names goes to the identity directory. Like the reply referent,
/// host-side input and not an effect — the folded text is what the
/// trace records. A failed read leaves the record as it was: a name
/// is never worth a stalled message.
fn resolve_sender(ctx: &RunCtx, record: Value) -> Value {
    if Watcher::sender_id(&record).is_none() {
        return record;
    }
    let creator = Watcher::sender_id(&record).unwrap_or("").to_string();
    let mut cached = ctx.roster.lock().unwrap();
    let now = Instant::now();
    let reread = match cached.as_ref() {
        None => true,
        Some((at, roster)) => {
            let age = now.duration_since(*at);
            age > ROSTER_TTL || (!roster.names.contains_key(&creator) && age > ROSTER_MISS_BACKOFF)
        }
    };
    if reread {
        match ctx.client.members(&ctx.space) {
            Ok(rows) => *cached = Some((now, crate::triggers::Roster::from_members(&rows))),
            Err(e) => warn!("members of {}: {e}", ctx.space),
        }
    }
    let Some((_, roster)) = cached.as_mut() else {
        return record;
    };
    Watcher::with_sender(record, roster, |id| match ctx.client.identity(id) {
        Ok(v) => v.get("name").and_then(|n| n.as_str()).map(str::to_string),
        Err(e) => {
            warn!("identity {id}: {e}");
            None
        }
    })
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
        .unwrap_or_else(|e| RunResult::failed(e.to_string()))
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
        event_targets(&reg, &instance, &ctx.space, space, object_id)
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
                    if let Some(t) = reg.remove(&shared.chat_watch_id) {
                        let _ = release_chat_watch(
                            &ctx.client,
                            &ctx.space,
                            &ctx.anchor,
                            &ctx.triggers_ds,
                            &t,
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
    use crate::anyapi::AnyError;
    use crate::chatwatch::is_model_setup;
    use crate::config::Overlay;
    use crate::presence::is_signed_out;
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
    fn a_signed_out_server_is_not_a_beat_warning() {
        let err = |status: u16, code: &str| AnyError {
            status,
            code: code.into(),
            message: String::new(),
        };
        assert!(is_signed_out(&err(401, "auth.required")));
        assert!(!is_signed_out(&err(401, "control.forbidden")));
        assert!(!is_signed_out(&err(500, "internal")));
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
        // the chat's log child derives under the catalog bundle; it
        // carries its one type from the first change
        let child = c
            .bundle_child("sp", "system:general-chat/v1", "bao/log/v1", "page", &[])
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

    // --- the wait set (ADR-009 §8): ensure_ready minus RunCtx plumbing
    // (a real Cage needs wasm) — the probes are closures here, the
    // space-list read and the resolver in serve.

    fn overlay_wait() -> WaitSet {
        WaitSet::overlays([("agent".to_string(), "repo".to_string())].into())
    }

    fn missed(space: &str, spec: &str) -> Vec<(String, String)> {
        vec![(space.to_string(), spec.to_string())]
    }

    #[test]
    fn readiness_reports_pending_overlay_status() {
        let mut w = overlay_wait();
        let err = w
            .settle(|_| false, |_, _| true, Instant::now())
            .expect_err("must be pending");
        assert!(err.to_string().contains("overlay `agent`"), "{err}");
        assert!(err.to_string().contains("still joining/syncing"), "{err}");
    }

    #[test]
    fn readiness_clears_once_spaces_are_visible() {
        let mut w = overlay_wait();
        w.settle(|sid| sid == "repo", |_, _| true, Instant::now())
            .unwrap();
        assert!(w.is_empty());
    }

    #[test]
    fn a_missed_program_holds_the_loop_until_it_resolves() {
        // BOB-133: the space is active but `llm@v1` hasn't landed —
        // the run's miss is a wait, cleared the moment it resolves
        let mut w = WaitSet::default();
        let t0 = Instant::now();
        assert_eq!(w.wait_for_programs(&missed("repo", "llm@v1"), t0), 1);
        assert_eq!(w.wait_for_programs(&missed("repo", "llm@v1"), t0), 0); // dedup
        let err = w.settle(|_| true, |_, _| false, t0).expect_err("must wait");
        assert!(err.to_string().contains("program `llm@v1`"), "{err}");
        assert!(err.to_string().contains("hasn't synced"), "{err}");
        w.settle(
            |_| true,
            |space, spec| space == "repo" && spec == "llm@v1",
            t0,
        )
        .unwrap();
        assert!(w.is_empty());
    }

    #[test]
    fn a_program_wait_gives_up_after_patience() {
        // a phantom `use()` must not wedge serve: the wait is bounded,
        // and the next run fails on it like any error
        let mut w = WaitSet::default();
        let t0 = Instant::now();
        w.wait_for_programs(&missed("repo", "ghost@v9"), t0);
        let just_before = t0 + PROGRAM_MISS_PATIENCE - Duration::from_secs(1);
        assert!(w.settle(|_| true, |_, _| false, just_before).is_err());
        w.settle(|_| true, |_, _| false, t0 + PROGRAM_MISS_PATIENCE)
            .unwrap();
        assert!(w.is_empty());
    }

    #[test]
    fn readiness_status_lists_overlays_and_programs() {
        let mut w = overlay_wait();
        w.wait_for_programs(&missed("other", "x@v1"), Instant::now());
        let err = w
            .settle(|_| false, |_, _| false, Instant::now())
            .expect_err("both pending");
        let text = err.to_string();
        assert!(text.contains("overlay `agent`"), "{text}");
        assert!(text.contains("program `x@v1`"), "{text}");
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
            "added": [{"id": "u7", "doc": {"text": "new question"},
                "ops": [{"type": "$set", "path": ["text"], "payload": "new question"}]}],
            "updated": [{"id": "u1", "doc": {"text": "old question",
                "reactions": {"👍": {"acct": {"$date": "2026-09-10T10:00:00.000Z"}}}}}]
        }]);
        let recs = records_in(&data);
        let ids: Vec<&str> = recs.iter().map(|r| r["id"].as_str().unwrap()).collect();
        assert_eq!(ids, ["u7"]);
        assert_eq!(recs[0]["text"], "new question");
    }

    #[test]
    fn records_in_skips_an_added_without_ops_the_row_promoted_past_the_limit() {
        // BOB-204: a visible row is deleted and the server slides the row
        // past the limit into view — the frame the server sent on staging
        // (limit 2, delete the newest): the promoted `added` ships the
        // stored doc and no ops, the deletion rides as `removed`. A
        // 6-day-old answered message entered a live run this way.
        let data = json!([{
            "versionId": "",
            "added": [{"id": "fWsk9MrCjT", "doc": {"id": "fWsk9MrCjT",
                "text": "another 15 book recommendations in Alpha",
                "createdAt": {"$date": "2026-10-14T08:56:53.000Z"}}}],
            "removed": [{"id": "b9570", "reason": "deleted"}]
        }]);
        assert!(records_in(&data).is_empty());
        // empty ops is the same signal; a written record in the same
        // frame still comes through
        let data = json!([{
            "versionId": "!!7}",
            "added": [
                {"id": "old", "doc": {"text": "promoted"}, "ops": []},
                {"id": "new", "doc": {"text": "typed just now"},
                 "ops": [{"type": "$set", "path": ["createdAt"],
                          "payload": {"$date": "2026-10-20T10:38:10.000Z"}}]}
            ]
        }]);
        let ids: Vec<String> = records_in(&data)
            .iter()
            .map(|r| r["id"].as_str().unwrap().to_string())
            .collect();
        assert_eq!(ids, ["new"]);
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
