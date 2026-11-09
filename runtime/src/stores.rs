//! Shared agent-store access and checked record helpers (ADR-006 §3, ADR-021
//! §4/§8.1, ADR-027 §2): the config/secret stores every Broker reads through,
//! the declared credential table and the per-path upsert/stamp writes.
//!
//! Always compiled. Desktop `serve` re-exports every item here unchanged and
//! keeps provisioning, seeding, boot stamps and its threads to itself; the
//! managed owner (ADR-034 §5) builds its Broker from these same types without
//! importing that desktop boot. Moved from `serve.rs` verbatim (N1).

use crate::anyapi::Client;
use crate::broker::{DeclaredCredentials, SecretSource};
use anyhow::Result;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tracing::warn;

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

/// The secrets store's value field (ADR-021 §4): the account-scoped
/// secret on each `agent_secrets` row; the key lives beside it.
pub(crate) const SECRETS_FIELD: &str = "value";

/// One synced upsert of a config row — per-path ops, no
/// read-merge-write (a concurrent UI save is never clobbered).
pub(crate) fn upsert_config_row(
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
    pub(crate) client: Arc<Client>,
    pub(crate) space: String,
    pub(crate) obj: String,
    /// the `agent_config` collection (resolved at provisioning)
    pub(crate) dataset: String,
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

/// A config store with a read-only shadow in front: keys in
/// `overrides` answer from the map, everything else reads through;
/// every write goes to the inner store.
pub struct ShadowedConfigStore {
    pub(crate) inner: ServeConfigStore,
    pub(crate) overrides: BTreeMap<String, Value>,
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

/// The stored secret value for `key` from already-queried rows
/// (None when unset/empty).
pub(crate) fn stored_local_secret(rows: &[Value], key: &str, field: &str) -> Option<String> {
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
pub(crate) fn persist_local_secret(
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
pub(crate) fn upsert_secret_row(
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
pub(crate) fn descriptor_patch(exists: bool, about: &Value, base: Value) -> Value {
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
                &json!({"filter": {"any.type": schema.type_id}, "limit": 500}),
            ) {
                Ok(r) => r,
                Err(e) => {
                    warn!("credentials: cannot list programs in {space} ({e})");
                    continue;
                }
            };
            for row in &rows {
                let props = schema.read(row);
                for (r, about) in declared_entries(&props) {
                    let r = r.as_str();
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

/// One program's `credentials` declaration (ADR-021 §8.1): `[{ref, about}]`
/// parsed off the xKey-keyed property group; a missing or malformed
/// property declares nothing. Shared by the live table and the managed frozen table.
pub(crate) fn declared_entries(props: &Value) -> Vec<(String, Value)> {
    let mut out = Vec::new();
    let Some(text) = props.get("credentials").and_then(Value::as_str) else {
        return out;
    };
    let Ok(Value::Array(list)) = serde_json::from_str::<Value>(text) else {
        return out;
    };
    for entry in list {
        let Some(r) = entry.get("ref").and_then(Value::as_str) else {
            continue;
        };
        let about = entry.get("about").cloned().unwrap_or(Value::Null);
        out.push((r.to_string(), about));
    }
    out
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

pub(crate) fn now_rfc3339() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

/// The serve-side secret write path for managed OAuth (ADR-011 §3) —
/// the same two-step local-scope write the bootstrap uses, plus the
/// synced non-secret metadata record.
pub struct ServeSecretStore {
    pub(crate) client: Arc<Client>,
    pub(crate) space: String,
    pub(crate) obj: String,
    /// the `agent_secrets` collection (resolved at provisioning)
    pub(crate) dataset: String,
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

    /// ADR-032 §3 (ADR-021 §8.4 amended): the descriptor of the first
    /// request an open, hostless row served — written once, onto the
    /// existing row, so the key is bound from here on.
    fn bind_hosts(&self, key: &str, about: &Value) {
        let patch = descriptor_patch(false, about, json!({}));
        if patch.as_object().is_some_and(|p| p.is_empty()) {
            return;
        }
        if let Err(e) = upsert_secret_row(
            &self.client,
            &self.space,
            &self.obj,
            &self.dataset,
            key,
            &patch,
        ) {
            warn!("secrets: could not bind hosts for {key} ({e})");
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
