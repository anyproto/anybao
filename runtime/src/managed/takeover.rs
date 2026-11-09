//! The owned takeover (ADR-034 §5 "Confirmed activation/Open"): the checked
//! writes a confirmed Open performs before the fence may complete it. In
//! order: the pinned skills are re-verified (they are live guest reads, the
//! one set the frozen snapshot cannot serve); the standing built-ins are
//! stamped to this peer with their next occurrence written strictly forward
//! (the ADR-006 §4 cold-sync rule, written explicitly rather than armed
//! lazily, and persisted as `nextDue` so a reader of the store can prove the
//! advance); the chat-responder record is stamped, or seeded when absent;
//! the declared credential descriptors are reconciled (label/help/note and
//! `status: missing` for an absent row — never a value, never a host binding:
//! the host transport refuses every `hosts` write). Every write is read
//! back and compared field by field: any error, rejection, tombstone, read
//! failure or mismatch is a failed takeover, so no AppliedOpen (the fence
//! consumes the sequences; main needs fresh evidence to try again). Pure
//! helpers over `&Client` + addresses; the owned thread and the generation
//! check that discards a late outcome live in `session.rs`. Rust never
//! touches the registry: the only transport here is the gated host client,
//! which denies `/v1/devices*` in every phase.
use super::frozen::{self, FrozenContent, Verify};
use super::preparation::{FrozenSkill, StoreAddress};
use super::CancelToken;
use crate::anyapi::Client;
use crate::chatwatch::{reply_tombstoned, seed_chat_watch_checked};
use crate::stores::upsert_secret_row;
use crate::tracestore::fold_integral_floats;
use crate::triggers::{
    is_chat_watch, record_to_trigger, standing_triggers, trigger_to_record, Scheduler, Trigger,
};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

/// Why a takeover did not complete. `SkillDrift` is drift: `NeedsSetup`
/// until explicit setup changes. Everything else may succeed on a later
/// Open with fresh evidence.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum TakeoverError {
    /// The owner cancelled before the takeover finished.
    Cancelled,
    /// A read failed or returned an unusable shape.
    Read(String),
    /// A write failed or was rejected.
    Write(String),
    /// A standing record's fixed id is a tombstone (deleted from the dataset):
    /// every Open fails until it is restored by explicit setup (S11).
    Tombstoned(String),
    /// This takeover was superseded by a Close/Release/Stop or a newer intent
    /// before its next write: nothing more was written.
    Superseded,
    /// The read-back did not equal what was written.
    Mismatch(String),
    /// A pinned skill changed, vanished or gained a sibling.
    SkillDrift,
}

impl std::fmt::Display for TakeoverError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TakeoverError::Cancelled => f.write_str("takeover cancelled"),
            TakeoverError::Read(what) => write!(f, "takeover read failed: {what}"),
            TakeoverError::Write(what) => write!(f, "takeover write failed: {what}"),
            TakeoverError::Tombstoned(id) => {
                write!(f, "takeover: standing record {id} is a tombstone")
            }
            TakeoverError::Superseded => f.write_str("takeover superseded before its next write"),
            TakeoverError::Mismatch(what) => write!(f, "takeover read-back mismatch: {what}"),
            TakeoverError::SkillDrift => f.write_str("pinned skills drifted (NeedsSetup)"),
        }
    }
}

/// Everything a takeover reads or writes, taken from the retained service and
/// the fixture at spawn time (`session.rs`), never from a command payload.
pub(crate) struct TakeoverInputs {
    pub(crate) client: Arc<Client>,
    pub(crate) space: String,
    pub(crate) chat: String,
    pub(crate) self_peer: String,
    pub(crate) triggers: StoreAddress,
    pub(crate) secrets: StoreAddress,
    /// The frozen declared table (`FrozenContent::declared`): ref → `about`.
    pub(crate) declared: BTreeMap<String, Value>,
    /// The verified skill hashes (`Readiness::skills`): `alias:name` → SHA-256.
    pub(crate) skills: BTreeMap<String, String>,
    /// The configured overlay aliases (`FrozenContent::aliases`): alias → space.
    pub(crate) aliases: BTreeMap<String, String>,
    /// Unix seconds, for the strictly-forward cron re-arm.
    pub(crate) now: Arc<dyn Fn() -> f64 + Send + Sync>,
    /// Raised by the session when this takeover no longer matters: checked
    /// before every write, so a stale task never lands a late `owner=self`.
    pub(crate) superseded: Arc<AtomicBool>,
}

impl TakeoverInputs {
    fn halted(&self, cancel: &CancelToken) -> Result<(), TakeoverError> {
        if cancel.is_cancelled() {
            Err(TakeoverError::Cancelled)
        } else if self.superseded.load(Ordering::Acquire) {
            Err(TakeoverError::Superseded)
        } else {
            Ok(())
        }
    }
}

pub(crate) struct TakeoverOutcome {
    /// The live chat-responder record id (`chat-watch` or a reseeded generation).
    pub(crate) chat_watch_id: String,
    /// The chat-responder record as written and read back (owner = this
    /// peer): the execution service's registry entry for it (N2b-2), so
    /// `owns_chat` follows the stamped record, enabled state included.
    pub(crate) chat_watch: Trigger,
    /// The standing built-ins as written (owner, enabled state, `next_due`).
    pub(crate) standing: Vec<Trigger>,
    /// Declared refs whose descriptor was stamped and read back.
    pub(crate) descriptors: usize,
}

/// The whole takeover, cancellation checked between steps.
pub(crate) fn run(
    inputs: &TakeoverInputs,
    cancel: &CancelToken,
) -> Result<TakeoverOutcome, TakeoverError> {
    // cancellation and supersession are checked between phases AND before
    // every write inside them (N2b-1 review item 1)
    let check = || inputs.halted(cancel);
    check()?;
    reverify_skills(&inputs.client, &inputs.aliases, &inputs.skills)?;
    check()?;
    let rows = trigger_rows(&inputs.client, &inputs.space, &inputs.triggers)?;
    let standing = stamp_standing(inputs, cancel, &rows)?;
    check()?;
    let chat_watch = stamp_or_seed_chat_watch(inputs, cancel, &rows)?;
    check()?;
    let descriptors = reconcile_descriptors(inputs, cancel)?;
    Ok(TakeoverOutcome {
        chat_watch_id: chat_watch.id.clone(),
        chat_watch,
        standing,
        descriptors,
    })
}

/// The pinned skills, read exactly as `toolcaller@v1` reads them (the same
/// `frozen::verify_skills` preparation used), compared to the verified
/// hashes: a changed body, a vanished pin or an unpinned sibling is drift.
pub(crate) fn reverify_skills(
    client: &Client,
    aliases: &BTreeMap<String, String>,
    expected: &BTreeMap<String, String>,
) -> Result<(), TakeoverError> {
    let mut pins: BTreeMap<&str, Vec<FrozenSkill>> = BTreeMap::new();
    for (key, sha256) in expected {
        let (alias, name) = key
            .split_once(':')
            .ok_or_else(|| TakeoverError::Read(format!("skill pin {key:?} has no alias")))?;
        pins.entry(alias).or_default().push(FrozenSkill {
            alias: alias.to_owned(),
            name: name.to_owned(),
            sha256: sha256.clone(),
        });
    }
    let mut scratch = FrozenContent::default();
    for (alias, space) in aliases {
        let pinned: Vec<&FrozenSkill> = pins
            .get(alias.as_str())
            .map(|v| v.iter().collect())
            .unwrap_or_default();
        match frozen::verify_skills(client, space, alias, &pinned, &mut scratch, true) {
            Ok(()) => {}
            // a pin preparation verified on this device that is gone now is drift (S10)
            Err(Verify::NeedsSetup | Verify::Missing) => return Err(TakeoverError::SkillDrift),
            Err(Verify::Syncing) => {
                return Err(TakeoverError::Read(format!(
                    "skills of {alias}: read failed"
                )))
            }
        }
    }
    if &scratch.skills != expected {
        return Err(TakeoverError::SkillDrift);
    }
    Ok(())
}

fn trigger_rows(
    client: &Client,
    space: &str,
    triggers: &StoreAddress,
) -> Result<Vec<Value>, TakeoverError> {
    client
        .query(space, &triggers.object_id, &triggers.collection, &json!({}))
        .map_err(|e| TakeoverError::Read(format!("agent_triggers: {e}")))
}

/// The standing built-ins (ADR-006 §2): the code definition, the record's
/// user/runtime state (`enabled`, the rollup) when a record exists, this
/// peer as owner and, for crons, the next occurrence — the record's own
/// persisted `nextDue` when it is in the future but no further than the
/// occurrence armed from now (a reopen must not push the jobs out again and
/// again, N2b-1 review item 3; a far-future value — a clock stepped back, an
/// edited record — must not silence the job either, Step B review F7), else
/// strictly forward of now. Written and read back one by one.
pub(crate) fn stamp_standing(
    inputs: &TakeoverInputs,
    cancel: &CancelToken,
    rows: &[Value],
) -> Result<Vec<Trigger>, TakeoverError> {
    let now = inputs.now.clone();
    let scheduler = Scheduler::new(&inputs.self_peer, Box::new(move || now()));
    let mut out = Vec::new();
    for mut t in standing_triggers(&inputs.space, &inputs.chat, &inputs.self_peer) {
        let base = (inputs.now)();
        let mut persisted_due = None;
        if let Some(row) = rows
            .iter()
            .find(|r| r.get("id").and_then(Value::as_str) == Some(t.id.as_str()))
        {
            if let Some(enabled) = row.get("enabled").and_then(Value::as_bool) {
                t.enabled = enabled;
            }
            t.last_run_at = row.get("lastRunAt").and_then(Value::as_f64);
            t.last_status = row
                .get("lastStatus")
                .and_then(Value::as_str)
                .map(str::to_owned);
            t.consecutive_failures = row
                .get("consecutiveFailures")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            persisted_due = row
                .get("nextDue")
                .and_then(Value::as_f64)
                .filter(|due| *due > base);
        }
        t.owner = inputs.self_peer.clone();
        t.next_due = if t.kind == "cron" {
            let armed = scheduler.compute_next_due(&t);
            match (persisted_due, armed) {
                (Some(due), Some(bound)) if due <= bound => Some(due),
                (_, armed) => armed,
            }
        } else {
            None
        };
        if t.kind == "cron" && !t.next_due.is_some_and(|due| due > base) {
            return Err(TakeoverError::Write(format!(
                "{}: next occurrence is not strictly forward",
                t.id
            )));
        }
        inputs.halted(cancel)?;
        write_trigger(&inputs.client, &inputs.space, &inputs.triggers, &t)?;
        out.push(t);
    }
    Ok(out)
}

/// The chat responder (ADR-018 §3): an existing `chat-watch*` record is
/// re-stamped to this peer (the newest explicit act wins over an earlier
/// repin); an absent one is seeded through the shared generation fallthrough
/// and then verified like every other write. Returns the record as written.
pub(crate) fn stamp_or_seed_chat_watch(
    inputs: &TakeoverInputs,
    cancel: &CancelToken,
    rows: &[Value],
) -> Result<Trigger, TakeoverError> {
    let client = &inputs.client;
    let existing = rows.iter().find_map(|r| {
        let id = r.get("id")?.as_str()?;
        is_chat_watch(id).then(|| (id.to_owned(), r))
    });
    inputs.halted(cancel)?;
    if let Some((id, row)) = existing {
        let mut t = record_to_trigger(&id, row)
            .ok_or_else(|| TakeoverError::Read(format!("chat responder {id}: malformed record")))?;
        t.owner = inputs.self_peer.clone();
        write_trigger(client, &inputs.space, &inputs.triggers, &t)?;
        return Ok(t);
    }
    // the seed reads, then writes up to five generations: each write is
    // preceded by the same check (F5/SB-I-8), and a halted seed reports why
    let (id, seeded) = match seed_chat_watch_checked(
        client,
        &inputs.space,
        &inputs.triggers.object_id,
        &inputs.triggers.collection,
        &inputs.chat,
        &inputs.self_peer,
        &|| inputs.halted(cancel).is_err(),
    ) {
        Ok(seeded) => seeded,
        Err(e) => {
            inputs.halted(cancel)?;
            return Err(TakeoverError::Write(format!("chat responder: {e:#}")));
        }
    };
    match seeded {
        Some(t) => {
            verify_row(
                client,
                &inputs.space,
                &inputs.triggers,
                &id,
                &persisted_record(&t),
            )?;
            Ok(t)
        }
        None => {
            // a record appeared between the two reads: stamp it like an existing one
            let row = read_trigger_row(client, &inputs.space, &inputs.triggers, &id)?
                .ok_or_else(|| TakeoverError::Mismatch(format!("{id}: absent after seed")))?;
            let mut t = record_to_trigger(&id, &row).ok_or_else(|| {
                TakeoverError::Read(format!("chat responder {id}: malformed record"))
            })?;
            t.owner = inputs.self_peer.clone();
            inputs.halted(cancel)?;
            write_trigger(client, &inputs.space, &inputs.triggers, &t)?;
            Ok(t)
        }
    }
}

/// The persisted shape of a trigger on the managed path: the desktop record
/// plus `nextDue`, so a reader of the store sees the forward re-arm. Desktop
/// readers ignore the field (`record_to_trigger` arms lazily) and a desktop
/// write of the same record drops it; nothing on desktop changes.
pub(crate) fn persisted_record(t: &Trigger) -> Value {
    let mut record = trigger_to_record(t);
    record["nextDue"] = json!(t.next_due);
    record
}

fn write_trigger(
    client: &Client,
    space: &str,
    triggers: &StoreAddress,
    t: &Trigger,
) -> Result<(), TakeoverError> {
    let record = persisted_record(t);
    let reply = client
        .upsert_record(
            space,
            &triggers.object_id,
            &triggers.collection,
            &t.id,
            &record,
        )
        .map_err(|e| TakeoverError::Write(format!("{}: {e}", t.id)))?;
    if reply_tombstoned(&reply) {
        return Err(TakeoverError::Tombstoned(t.id.clone()));
    }
    if reply["rejections"]
        .as_array()
        .is_some_and(|r| !r.is_empty())
    {
        return Err(TakeoverError::Write(format!("{}: rejected", t.id)));
    }
    verify_row(client, space, triggers, &t.id, &record)
}

fn read_trigger_row(
    client: &Client,
    space: &str,
    triggers: &StoreAddress,
    id: &str,
) -> Result<Option<Value>, TakeoverError> {
    let rows = client
        .query(
            space,
            &triggers.object_id,
            &triggers.collection,
            &json!({"filter": {"id": id}, "limit": 1}),
        )
        .map_err(|e| TakeoverError::Read(format!("{id}: {e}")))?;
    Ok(rows
        .into_iter()
        .find(|r| r.get("id").and_then(Value::as_str) == Some(id)))
}

fn verify_row(
    client: &Client,
    space: &str,
    triggers: &StoreAddress,
    id: &str,
    written: &Value,
) -> Result<(), TakeoverError> {
    let row = read_trigger_row(client, space, triggers, id)?
        .ok_or_else(|| TakeoverError::Mismatch(format!("{id}: absent after write")))?;
    if read_back_equal(written, &row) {
        Ok(())
    } else {
        Err(TakeoverError::Mismatch(format!("{id}: read-back differs")))
    }
}

/// "Verify = read-back equal" (ADR-034 §5): every key the write carried reads
/// back with an equal value; a null written field may read back absent;
/// integral floats compare as integers (`fold_integral_floats`); keys the
/// server adds (`_addSeq`, stamps) are not compared.
pub(crate) fn read_back_equal(written: &Value, row: &Value) -> bool {
    let Some(fields) = written.as_object() else {
        return false;
    };
    fields.iter().all(|(key, value)| {
        let got = row.get(key).cloned().unwrap_or(Value::Null);
        fold_integral_floats(got) == fold_integral_floats(value.clone())
    })
}

/// The patch `stamp_declared` (desktop boot, ADR-021 §8.1/§8.4) builds for one
/// declared ref: the manifest is the authority for `label`/`hosts`/`help`/
/// `note` (a key dropped from the declaration is unset), and a row that does
/// not exist is created `missing`. Verbatim from `serve::stamp_declared`'s loop.
pub(crate) fn declared_stamp_patch(about: &Value, existing: Option<&Value>) -> Map<String, Value> {
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
    patch
}

/// The managed reconcile patch: `declared_stamp_patch` without `hosts`, set
/// or unset. Declared refs take their hosts from the frozen declared table at
/// injection (ADR-021 §8.1), so a row's `hosts` is cosmetic there, and the
/// managed host never writes a host binding (ADR-034 §2: the host transport
/// refuses every `hosts` write). The difference from desktop's
/// `stamp_declared`, which overwrites `hosts`, is stated in ADR-034 §5.
pub(crate) fn managed_descriptor_patch(
    about: &Value,
    existing: Option<&Value>,
) -> Map<String, Value> {
    let mut patch = declared_stamp_patch(about, existing);
    patch.remove("hosts");
    patch
}

/// Reconcile every declared ref's descriptor row (ADR-034 §5), each stamp read
/// back by key. Returns how many rows were stamped.
pub(crate) fn reconcile_descriptors(
    inputs: &TakeoverInputs,
    cancel: &CancelToken,
) -> Result<usize, TakeoverError> {
    let (client, space, secrets, declared) = (
        inputs.client.as_ref(),
        inputs.space.as_str(),
        &inputs.secrets,
        &inputs.declared,
    );
    let rows = client
        .query(space, &secrets.object_id, &secrets.collection, &json!({}))
        .map_err(|e| TakeoverError::Read(format!("agent_secrets: {e}")))?;
    let mut stamped = 0;
    for (key, about) in declared {
        let existing = rows
            .iter()
            .find(|r| r.get("key").and_then(Value::as_str) == Some(key.as_str()));
        let patch = managed_descriptor_patch(about, existing);
        if patch.is_empty() {
            continue;
        }
        let written = Value::Object(patch);
        inputs.halted(cancel)?;
        upsert_secret_row(
            client,
            space,
            &secrets.object_id,
            &secrets.collection,
            key,
            &written,
        )
        .map_err(|e| TakeoverError::Write(format!("{key}: {e:#}")))?;
        let back = client
            .query(
                space,
                &secrets.object_id,
                &secrets.collection,
                &json!({"filter": {"key": key}, "limit": 2}),
            )
            .map_err(|e| TakeoverError::Read(format!("{key}: {e}")))?;
        let row = match back.as_slice() {
            [row] => row,
            _ => {
                return Err(TakeoverError::Mismatch(format!(
                    "{key}: {} rows after write",
                    back.len()
                )))
            }
        };
        if !read_back_equal(&written, row) {
            return Err(TakeoverError::Mismatch(format!("{key}: read-back differs")));
        }
        stamped += 1;
    }
    Ok(stamped)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::anyapi::{AnyError, Transport};
    use crate::managed::frozen::sha256_hex;
    use crate::testutil::CallLog;
    use std::sync::Mutex;

    type Rows = BTreeMap<String, Map<String, Value>>;

    /// A record store that applies `modify` ops and answers `query` from them,
    /// so every read-back is real, plus the three reads skill re-verification
    /// makes. One call can be scripted to fail, one record to read back wrong,
    /// ids can be tombstoned.
    #[derive(Default)]
    struct Mirror {
        calls: CallLog,
        store: Mutex<BTreeMap<(String, String), Rows>>,
        types: Mutex<Vec<Value>>,
        objects: Mutex<Vec<Value>>,
        markdown: Mutex<String>,
        tombstoned: Mutex<Vec<String>>,
        corrupt: Mutex<Option<(String, String)>>,
        fail_once: Mutex<Option<&'static str>>,
        /// raised by the first modify the store sees: the session superseding
        /// a takeover between two of its writes
        supersede_on_modify: Mutex<Option<Arc<AtomicBool>>>,
        /// raised by the second whole-collection read of the triggers (the
        /// chat-responder seed's own read, after the takeover's first); with
        /// `true`, a `chat-watch` record appears in that same read
        supersede_on_seed_read: Mutex<Option<(Arc<AtomicBool>, bool)>>,
        trigger_scans: Mutex<usize>,
    }

    impl Mirror {
        fn seed(&self, object: &str, dataset: &str, id: &str, fields: Value) {
            self.store
                .lock()
                .unwrap()
                .entry((object.into(), dataset.into()))
                .or_default()
                .insert(id.into(), fields.as_object().cloned().unwrap_or_default());
        }
        fn rows(&self, object: &str, dataset: &str) -> Rows {
            self.store
                .lock()
                .unwrap()
                .get(&(object.to_owned(), dataset.to_owned()))
                .cloned()
                .unwrap_or_default()
        }
        fn query(&self, body: &Value) -> Value {
            let key = (
                body["objectId"].as_str().unwrap_or("").to_owned(),
                body["dataset"].as_str().unwrap_or("").to_owned(),
            );
            let want_id = body["filter"]["id"].as_str().map(str::to_owned);
            let want_key = body["filter"]["key"].as_str().map(str::to_owned);
            let limit = body["limit"].as_u64().unwrap_or(u64::MAX) as usize;
            if key.0 == TRIGGERS && body.get("filter").is_none() {
                let mut scans = self.trigger_scans.lock().unwrap();
                *scans += 1;
                if *scans == 2 {
                    if let Some((flag, appear)) = self.supersede_on_seed_read.lock().unwrap().take()
                    {
                        flag.store(true, Ordering::Release);
                        if appear {
                            self.seed(
                                TRIGGERS,
                                "agent_triggers",
                                "chat-watch",
                                json!({"kind": "event", "program": "internal:chat-watch",
                                       "name": "Chat responder", "owner": "other",
                                       "spec": {"dataset": "chat_messages", "objectId": "chat-1"},
                                       "enabled": true, "args": {}, "limits": {},
                                       "maxConsecutiveFailures": 3, "consecutiveFailures": 0}),
                            );
                        }
                    }
                }
            }
            let store = self.store.lock().unwrap();
            let records: Vec<Value> = store
                .get(&key)
                .into_iter()
                .flat_map(|rows| rows.iter())
                .filter(|(id, fields)| {
                    want_id.as_deref().is_none_or(|w| w == id.as_str())
                        && want_key
                            .as_deref()
                            .is_none_or(|w| fields.get("key").and_then(Value::as_str) == Some(w))
                })
                .take(limit)
                .map(|(id, fields)| {
                    let mut rec = fields.clone();
                    rec.insert("id".into(), json!(id));
                    Value::Object(rec)
                })
                .collect();
            json!({"records": records})
        }
        fn modify(&self, body: &Value) -> Value {
            if let Some(flag) = self.supersede_on_modify.lock().unwrap().take() {
                flag.store(true, Ordering::Release);
            }
            let key = (
                body["objectId"].as_str().unwrap_or("").to_owned(),
                body["dataset"].as_str().unwrap_or("").to_owned(),
            );
            let mut store = self.store.lock().unwrap();
            let rows = store.entry(key).or_default();
            let tombstoned = self.tombstoned.lock().unwrap();
            let corrupt = self.corrupt.lock().unwrap();
            let mut rejections = Vec::new();
            for rec in body["records"].as_array().cloned().unwrap_or_default() {
                let rid = rec["id"].as_str().unwrap_or("").to_owned();
                if tombstoned.contains(&rid) {
                    rejections.push(json!({"id": rid, "code": "record_deleted"}));
                    continue;
                }
                let entry = rows.entry(rid.clone()).or_default();
                for op in rec["ops"].as_array().cloned().unwrap_or_default() {
                    let path = op["path"].as_str().unwrap_or("");
                    match op["type"].as_str() {
                        Some("$set") if path.is_empty() => {
                            *entry = op["value"].as_object().cloned().unwrap_or_default();
                        }
                        Some("$set") => {
                            entry.insert(path.to_owned(), op["value"].clone());
                        }
                        Some("$unset") => {
                            entry.remove(path);
                        }
                        _ => {}
                    }
                }
                if let Some((cid, field)) = corrupt.as_ref() {
                    if *cid == rid {
                        entry.insert(field.clone(), json!("corrupted"));
                    }
                }
            }
            json!({"ok": true, "rejections": rejections})
        }
    }

    struct Via(Arc<Mirror>);
    impl Transport for Via {
        fn send(
            &self,
            method: &str,
            path: &str,
            body: Option<&Value>,
        ) -> Result<(u16, Value), AnyError> {
            let m = &self.0;
            m.calls
                .lock()
                .unwrap()
                .push((method.into(), path.into(), body.cloned()));
            {
                let mut fail = m.fail_once.lock().unwrap();
                if let Some(suffix) = *fail {
                    if path.ends_with(suffix) {
                        *fail = None;
                        return Err(AnyError {
                            status: 503,
                            code: "unavailable".into(),
                            message: "scripted failure".into(),
                        });
                    }
                }
            }
            let body = body.cloned().unwrap_or(Value::Null);
            if path.ends_with("/types?includeHidden=true") {
                return Ok((200, json!({"types": *m.types.lock().unwrap()})));
            }
            if path.ends_with("/objects/query") {
                return Ok((200, json!({"records": *m.objects.lock().unwrap()})));
            }
            if path.ends_with("/markdown") {
                return Ok((200, json!({"content": *m.markdown.lock().unwrap()})));
            }
            if path.ends_with("/query") {
                return Ok((200, m.query(&body)));
            }
            if path.ends_with("/modify") {
                return Ok((200, m.modify(&body)));
            }
            Ok((200, json!({})))
        }
        fn open_stream(
            &self,
            _: &str,
            _: Option<&Value>,
        ) -> Result<Box<dyn Iterator<Item = String>>, AnyError> {
            Err(AnyError {
                status: 0,
                code: "stream".into(),
                message: "no streams here".into(),
            })
        }
    }

    const TRIGGERS: &str = "trig-obj";
    const SECRETS: &str = "sec-obj";

    fn inputs(
        mirror: &Arc<Mirror>,
        declared: BTreeMap<String, Value>,
        skills: BTreeMap<String, String>,
    ) -> TakeoverInputs {
        TakeoverInputs {
            client: Arc::new(Client::with_transport(Box::new(Via(mirror.clone())))),
            space: "sp".into(),
            chat: "chat-1".into(),
            self_peer: "peer".into(),
            triggers: StoreAddress {
                object_id: TRIGGERS.into(),
                collection: "agent_triggers".into(),
            },
            secrets: StoreAddress {
                object_id: SECRETS.into(),
                collection: "agent_secrets".into(),
            },
            declared,
            skills,
            aliases: BTreeMap::from([("agent".to_string(), "sp".to_string())]),
            now: Arc::new(|| 1000.0),
            superseded: Arc::new(AtomicBool::new(false)),
        }
    }

    fn declared() -> BTreeMap<String, Value> {
        BTreeMap::from([
            (
                "llm.key.openai".to_string(),
                json!({"label": "OpenAI", "hosts": ["api.openai.com"]}),
            ),
            (
                "google.key.gemini".to_string(),
                json!({"label": "Gemini", "hosts": ["generativelanguage.googleapis.com"], "help": "h"}),
            ),
        ])
    }

    fn seeded() -> Arc<Mirror> {
        let m = Arc::new(Mirror::default());
        m.seed(
            TRIGGERS,
            "agent_triggers",
            "chat-watch",
            json!({"kind": "event", "program": "internal:chat-watch", "name": "Chat responder",
                   "spec": {"dataset": "chat_messages", "objectId": "chat-1"}, "owner": "other",
                   "enabled": true, "args": {}, "limits": {}, "maxConsecutiveFailures": 3,
                   "consecutiveFailures": 0}),
        );
        m.seed(
            TRIGGERS,
            "agent_triggers",
            "rollup",
            json!({"kind": "cron", "program": "agent:rollup@v1", "name": "history rollup",
                   "spec": {"every_s": 3600.0}, "owner": "other", "enabled": false,
                   "lastStatus": "ok", "consecutiveFailures": 2, "args": {}, "limits": {},
                   "maxConsecutiveFailures": 3}),
        );
        m.seed(
            SECRETS,
            "agent_secrets",
            "llm.key.openai",
            json!({"key": "llm.key.openai", "secret": true, "hosts": ["api.openai.com"],
                   "label": "old", "value": "sk-live"}),
        );
        m
    }

    #[test]
    fn takeover_writes_the_exact_set_and_reads_every_write_back() {
        let mirror = seeded();
        let outcome = run(
            &inputs(&mirror, declared(), BTreeMap::new()),
            &CancelToken::new(),
        )
        .unwrap();
        assert_eq!(outcome.chat_watch_id, "chat-watch");
        // the outcome carries the responder as written (N2b-2's registry entry)
        assert_eq!(outcome.chat_watch.id, "chat-watch");
        assert_eq!(outcome.chat_watch.owner, "peer");
        assert_eq!(outcome.standing.len(), 6);
        assert_eq!(outcome.descriptors, 2);
        let rows = mirror.rows(TRIGGERS, "agent_triggers");
        for id in [
            "rollup",
            "extraction",
            "linkgen",
            "decay",
            "reflection",
            "evolution",
            "chat-watch",
        ] {
            assert_eq!(rows[id]["owner"], "peer", "{id}");
        }
        // the record's user/runtime state survives; the definition is code-owned
        assert_eq!(rows["rollup"]["enabled"], false);
        assert_eq!(rows["rollup"]["consecutiveFailures"], 2);
        assert_eq!(rows["rollup"]["lastStatus"], "ok");
        assert_eq!(rows["extraction"]["enabled"], true);
        // next occurrence strictly forward of now, persisted
        assert_eq!(rows["rollup"]["nextDue"], 4600.0);
        assert_eq!(rows["extraction"]["nextDue"], 1900.0);
        assert_eq!(rows["decay"]["nextDue"], 87_400.0);
        assert!(rows["chat-watch"]["nextDue"].is_null());
        assert_eq!(rows["chat-watch"]["spec"]["objectId"], "chat-1");
        let secrets = mirror.rows(SECRETS, "agent_secrets");
        let gemini = &secrets["google.key.gemini"];
        assert_eq!(gemini["status"], "missing");
        assert_eq!(gemini["label"], "Gemini");
        assert_eq!(gemini["help"], "h");
        assert!(
            gemini.get("hosts").is_none(),
            "no host binding is ever written"
        );
        assert_eq!(gemini["secret"], true);
        assert!(gemini.get("value").is_none());
        let openai = &secrets["llm.key.openai"];
        assert_eq!(openai["label"], "OpenAI");
        assert_eq!(openai["hosts"], json!(["api.openai.com"]), "left as it was");
        assert!(
            openai.get("status").is_none(),
            "an existing row gets no status stamp"
        );
        assert_eq!(openai["value"], "sk-live", "the value is never touched");
        // only reads and modifies on the two fixture objects; on the secrets
        // object only descriptor paths the gate allows (never value or hosts)
        for (method, path, body) in mirror.calls.lock().unwrap().iter() {
            if path.ends_with("/modify") {
                assert_eq!(method, "POST");
                let object = body.as_ref().unwrap()["objectId"].as_str().unwrap();
                assert!(object == TRIGGERS || object == SECRETS, "{path} {object}");
                for rec in body.as_ref().unwrap()["records"].as_array().unwrap() {
                    assert_eq!(rec["upsert"], true);
                    assert!(rec["id"].as_str().is_some_and(|id| !id.is_empty()));
                    for op in rec["ops"].as_array().unwrap() {
                        let path = op["path"].as_str().unwrap_or("");
                        assert!(matches!(op["type"].as_str(), Some("$set" | "$unset")));
                        if object == SECRETS {
                            assert!(
                                ["key", "secret", "status", "label", "help", "note"]
                                    .contains(&path),
                                "{path}"
                            );
                        } else {
                            assert!(path.is_empty());
                            assert!(op["value"].get("value").is_none());
                        }
                    }
                }
            } else {
                assert!(
                    path.ends_with("/query") || path.ends_with("/types?includeHidden=true"),
                    "{method} {path}"
                );
            }
        }
    }

    #[test]
    fn a_mismatching_read_back_a_rejection_a_failed_write_or_cancellation_fails_the_takeover() {
        let mirror = seeded();
        *mirror.corrupt.lock().unwrap() = Some(("extraction".into(), "nextDue".into()));
        assert!(matches!(
            run(
                &inputs(&mirror, declared(), BTreeMap::new()),
                &CancelToken::new()
            ),
            Err(TakeoverError::Mismatch(_))
        ));

        let mirror = seeded();
        *mirror.fail_once.lock().unwrap() = Some("/modify");
        assert!(matches!(
            run(
                &inputs(&mirror, declared(), BTreeMap::new()),
                &CancelToken::new()
            ),
            Err(TakeoverError::Write(_))
        ));

        // superseded mid-way (item 1): the flag is checked before every write,
        // so a takeover superseded after its first write lands nothing more
        let mirror = seeded();
        let inputs_s = inputs(&mirror, declared(), BTreeMap::new());
        *mirror.supersede_on_modify.lock().unwrap() = Some(inputs_s.superseded.clone());
        assert_eq!(
            run(&inputs_s, &CancelToken::new()).err(),
            Some(TakeoverError::Superseded)
        );
        let writes = mirror
            .calls
            .lock()
            .unwrap()
            .iter()
            .filter(|(_, p, _)| p.ends_with("/modify"))
            .count();
        assert_eq!(writes, 1, "stopped before the second write");
        // a persisted future nextDue survives a takeover; a past one is re-armed (item 3)
        let mirror = seeded();
        mirror.seed(
            TRIGGERS,
            "agent_triggers",
            "extraction",
            json!({"kind": "cron", "program": "agent:extraction@v1", "name": "memory extraction",
                   "spec": {"every_s": 900.0}, "owner": "other", "enabled": true, "args": {},
                   "limits": {}, "maxConsecutiveFailures": 3, "consecutiveFailures": 0,
                   "nextDue": 1500.0}),
        );
        mirror.seed(
            TRIGGERS,
            "agent_triggers",
            "linkgen",
            json!({"kind": "cron", "program": "agent:linkgen@v1", "name": "memory link generation",
                   "spec": {"every_s": 3600.0}, "owner": "other", "enabled": true, "args": {},
                   "limits": {}, "maxConsecutiveFailures": 3, "consecutiveFailures": 0,
                   "nextDue": 900.0}),
        );
        run(
            &inputs(&mirror, declared(), BTreeMap::new()),
            &CancelToken::new(),
        )
        .unwrap();
        let rows = mirror.rows(TRIGGERS, "agent_triggers");
        assert_eq!(
            rows["extraction"]["nextDue"], 1500.0,
            "a future nextDue is kept"
        );
        assert_eq!(
            rows["linkgen"]["nextDue"], 4600.0,
            "a past nextDue is armed forward"
        );

        let mirror = seeded();
        let cancel = CancelToken::new();
        cancel.cancel();
        assert_eq!(
            run(&inputs(&mirror, declared(), BTreeMap::new()), &cancel).err(),
            Some(TakeoverError::Cancelled)
        );
        assert!(mirror.calls.lock().unwrap().is_empty());
    }

    #[test]
    fn an_absent_responder_is_seeded_past_a_tombstoned_bare_id_and_verified() {
        let mirror = Arc::new(Mirror::default());
        mirror.tombstoned.lock().unwrap().push("chat-watch".into());
        let outcome = run(
            &inputs(&mirror, BTreeMap::new(), BTreeMap::new()),
            &CancelToken::new(),
        )
        .unwrap();
        assert_eq!(outcome.chat_watch_id, "chat-watch-g2");
        assert_eq!(outcome.chat_watch.id, "chat-watch-g2");
        assert_eq!(outcome.chat_watch.owner, "peer");
        let rows = mirror.rows(TRIGGERS, "agent_triggers");
        assert_eq!(rows["chat-watch-g2"]["owner"], "peer");
        assert_eq!(rows["chat-watch-g2"]["enabled"], true);
        assert_eq!(rows["chat-watch-g2"]["kind"], "event");
        assert!(!rows.contains_key("chat-watch"));
        assert_eq!(outcome.descriptors, 0);
    }

    #[test]
    fn skill_drift_is_needs_setup_and_a_failed_skill_read_is_retryable() {
        let pinned = BTreeMap::from([("agent:_soul".to_string(), sha256_hex("hello"))]);
        let mirror = seeded();
        *mirror.types.lock().unwrap() =
            vec![json!({"id": "t-skill", "xKey": crate::deploy::SKILL_TYPE})];
        *mirror.objects.lock().unwrap() = vec![json!({"id": "sk1", "any": {"name": "_soul"}})];
        *mirror.markdown.lock().unwrap() = "hello".into();
        assert!(run(
            &inputs(&mirror, BTreeMap::new(), pinned.clone()),
            &CancelToken::new()
        )
        .is_ok());

        *mirror.markdown.lock().unwrap() = "edited".into();
        assert_eq!(
            run(
                &inputs(&mirror, BTreeMap::new(), pinned.clone()),
                &CancelToken::new()
            )
            .err(),
            Some(TakeoverError::SkillDrift)
        );

        *mirror.markdown.lock().unwrap() = "hello".into();
        *mirror.objects.lock().unwrap() = vec![
            json!({"id": "sk1", "any": {"name": "_soul"}}),
            json!({"id": "sk2", "any": {"name": "extra"}}),
        ];
        assert_eq!(
            run(
                &inputs(&mirror, BTreeMap::new(), pinned.clone()),
                &CancelToken::new()
            )
            .err(),
            Some(TakeoverError::SkillDrift)
        );

        *mirror.objects.lock().unwrap() = vec![json!({"id": "sk1", "any": {"name": "_soul"}})];
        *mirror.fail_once.lock().unwrap() = Some("/markdown");
        assert!(matches!(
            run(
                &inputs(&mirror, BTreeMap::new(), pinned),
                &CancelToken::new()
            ),
            Err(TakeoverError::Read(_))
        ));
        // drift is decided before any trigger or secret write
        let rows = mirror.rows(TRIGGERS, "agent_triggers");
        assert_eq!(
            rows["rollup"]["owner"], "peer",
            "the first, successful run stamped it"
        );
    }

    #[test]
    fn read_back_equal_folds_integral_floats_and_treats_a_missing_field_as_null() {
        let written = json!({"a": 3, "b": null, "c": [1, 2], "d": "x"});
        assert!(read_back_equal(
            &written,
            &json!({"a": 3.0, "c": [1.0, 2], "d": "x", "_addSeq": 9})
        ));
        assert!(!read_back_equal(
            &written,
            &json!({"a": 4, "c": [1, 2], "d": "x"})
        ));
        assert!(!read_back_equal(
            &written,
            &json!({"a": 3, "b": 1, "c": [1, 2], "d": "x"})
        ));
        assert!(!read_back_equal(&json!("not an object"), &json!({})));
        let patch = managed_descriptor_patch(
            &json!({"label": "L", "hosts": ["h"]}),
            Some(&json!({"label": "old", "hosts": ["old"], "note": "n"})),
        );
        assert_eq!(patch.get("label"), Some(&json!("L")));
        assert!(patch.get("hosts").is_none(), "hosts are never set or unset");
        assert_eq!(
            patch.get("note"),
            Some(&Value::Null),
            "dropped from the declaration"
        );
        assert!(patch.get("status").is_none());
        let created = managed_descriptor_patch(&json!({"label": "L", "hosts": ["h"]}), None);
        assert!(
            created.get("hosts").is_none(),
            "not even on a row being created"
        );
        assert_eq!(created.get("status"), Some(&json!("missing")));
        // the desktop stamp keeps the overwrite
        let desktop = declared_stamp_patch(&json!({"label": "L", "hosts": ["h"]}), None);
        assert_eq!(desktop.get("hosts"), Some(&json!(["h"])));
    }

    /// Step B review F7: a persisted `nextDue` further out than the occurrence
    /// armed from now (a clock stepped back, an edited record) is re-armed;
    /// one inside it is kept.
    #[test]
    fn a_far_future_next_due_is_re_armed_and_a_near_one_kept() {
        let mirror = seeded();
        for (id, every, due) in [
            ("extraction", 900.0, 1_000_000.0),
            ("linkgen", 3600.0, 4000.0),
        ] {
            mirror.seed(
                TRIGGERS,
                "agent_triggers",
                id,
                json!({"kind": "cron", "program": format!("agent:{id}@v1"), "name": id,
                       "spec": {"every_s": every}, "owner": "other", "enabled": true,
                       "args": {}, "limits": {}, "maxConsecutiveFailures": 3,
                       "consecutiveFailures": 0, "nextDue": due}),
            );
        }
        run(
            &inputs(&mirror, declared(), BTreeMap::new()),
            &CancelToken::new(),
        )
        .unwrap();
        let rows = mirror.rows(TRIGGERS, "agent_triggers");
        assert_eq!(
            rows["extraction"]["nextDue"], 1900.0,
            "1e6 is beyond now + 900 s: armed from now"
        );
        assert_eq!(
            rows["linkgen"]["nextDue"], 4000.0,
            "inside one period from now: kept"
        );
    }

    /// Step B review F5 / SB-I-8 / SB-P-4: a takeover superseded while the
    /// chat-responder seed reads writes nothing more — neither a seeded
    /// generation nor the re-stamp of a record that appeared in that read.
    #[test]
    fn a_takeover_superseded_during_the_seed_read_writes_no_responder() {
        for appear in [false, true] {
            let mirror = Arc::new(Mirror::default());
            let inputs_s = inputs(&mirror, BTreeMap::new(), BTreeMap::new());
            *mirror.supersede_on_seed_read.lock().unwrap() =
                Some((inputs_s.superseded.clone(), appear));
            assert_eq!(
                run(&inputs_s, &CancelToken::new()).err(),
                Some(TakeoverError::Superseded),
                "appear={appear}"
            );
            let responder_writes = mirror
                .calls
                .lock()
                .unwrap()
                .iter()
                .filter(|(_, p, b)| {
                    p.ends_with("/modify")
                        && b.as_ref().is_some_and(|b| {
                            b["records"][0]["id"]
                                .as_str()
                                .is_some_and(|id| id.starts_with("chat-watch"))
                        })
                })
                .count();
            assert_eq!(responder_writes, 0, "appear={appear}");
        }
    }

    /// SB-P-5: a tombstoned standing built-in fails the takeover typed (the
    /// session maps it to NeedsSetup); S10/N-2: a vanished pin, a vanished
    /// skill type and a pin emptied after preparation are drift at Open,
    /// never a retryable read.
    #[test]
    fn a_tombstoned_standing_record_and_a_vanished_or_emptied_pin_are_typed() {
        let mirror = seeded();
        mirror.tombstoned.lock().unwrap().push("rollup".into());
        assert_eq!(
            run(
                &inputs(&mirror, declared(), BTreeMap::new()),
                &CancelToken::new()
            )
            .err(),
            Some(TakeoverError::Tombstoned("rollup".into()))
        );

        let pinned = BTreeMap::from([
            ("agent:_soul".to_string(), sha256_hex("hello")),
            ("agent:_core".to_string(), sha256_hex("core")),
        ]);
        let mirror = seeded();
        *mirror.types.lock().unwrap() =
            vec![json!({"id": "t-skill", "xKey": crate::deploy::SKILL_TYPE})];
        *mirror.objects.lock().unwrap() = vec![json!({"id": "sk1", "any": {"name": "_soul"}})];
        *mirror.markdown.lock().unwrap() = "hello".into();
        assert_eq!(
            run(
                &inputs(&mirror, BTreeMap::new(), pinned),
                &CancelToken::new()
            )
            .err(),
            Some(TakeoverError::SkillDrift),
            "a pin gone from the device"
        );

        let pinned = BTreeMap::from([("agent:_soul".to_string(), sha256_hex("hello"))]);
        *mirror.markdown.lock().unwrap() = String::new();
        assert_eq!(
            run(
                &inputs(&mirror, BTreeMap::new(), pinned.clone()),
                &CancelToken::new()
            )
            .err(),
            Some(TakeoverError::SkillDrift),
            "a verified pin emptied since preparation"
        );
        mirror.types.lock().unwrap().clear();
        assert_eq!(
            run(
                &inputs(&mirror, BTreeMap::new(), pinned),
                &CancelToken::new()
            )
            .err(),
            Some(TakeoverError::SkillDrift),
            "the skill type gone since preparation"
        );
        assert!(
            mirror.rows(TRIGGERS, "agent_triggers")["rollup"]["owner"] == "other",
            "drift is decided before any write"
        );
    }
}
