//! Test doubles for the anyapi Transport: a scripted stub (wire-shape
//! assertions) and an in-memory fake `any` space (deploy/resolver
//! flows). Test-only — compiled under #[cfg(test)] from lib.rs.

use crate::anyapi::{AnyError, Transport};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, VecDeque};
use std::sync::Arc;
use std::sync::Mutex;

pub type CallLog = Arc<Mutex<Vec<(String, String, Option<Value>)>>>;

/// Scripted transport: records every call, replays queued replies
/// ((200, {}) once the queue is empty), and serves a canned SSE line
/// stream.
#[derive(Default)]
pub struct StubTransport {
    calls: CallLog,
    replies: Mutex<VecDeque<(u16, Value)>>,
    stream: Mutex<Vec<String>>,
    raw_replies: Mutex<VecDeque<Vec<u8>>>,
}

impl StubTransport {
    pub fn new() -> Self {
        Self::default()
    }

    /// Shared handle onto the recorded (method, path, body) calls.
    pub fn log(&self) -> CallLog {
        Arc::clone(&self.calls)
    }

    pub fn push(&self, status: u16, body: Value) {
        self.replies.lock().unwrap().push_back((status, body));
    }

    pub fn push_raw(&self, bytes: Vec<u8>) {
        self.raw_replies.lock().unwrap().push_back(bytes);
    }

    pub fn set_stream(&self, lines: &[&str]) {
        *self.stream.lock().unwrap() = lines.iter().map(|s| s.to_string()).collect();
    }
}

impl Transport for StubTransport {
    fn send(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
    ) -> Result<(u16, Value), AnyError> {
        self.calls
            .lock()
            .unwrap()
            .push((method.to_string(), path.to_string(), body.cloned()));
        Ok(self
            .replies
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or((200, json!({}))))
    }

    fn open_stream(
        &self,
        path: &str,
        body: Option<&Value>,
    ) -> Result<Box<dyn Iterator<Item = String>>, AnyError> {
        self.calls
            .lock()
            .unwrap()
            .push(("POST".to_string(), path.to_string(), body.cloned()));
        Ok(Box::new(self.stream.lock().unwrap().clone().into_iter()))
    }

    // raw calls land in the same log; the body is summarized as its
    // byte length (wire-shape tests assert method/path, not payloads)
    fn send_raw(
        &self,
        method: &str,
        path: &str,
        body: &[u8],
        _content_type: &str,
    ) -> Result<(u16, Value), AnyError> {
        self.calls.lock().unwrap().push((
            method.to_string(),
            path.to_string(),
            Some(json!({"rawBytes": body.len()})),
        ));
        Ok(self
            .replies
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or((200, json!({}))))
    }

    fn read_raw(&self, path: &str) -> Result<Vec<u8>, AnyError> {
        self.calls
            .lock()
            .unwrap()
            .push(("GET".to_string(), path.to_string(), None));
        Ok(self
            .raw_replies
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or_default())
    }
}

/// A minimal in-memory `any` server speaking the ADR-027 + ADR-029
/// contract: per-space objects with ONE type (`any.type`) and any
/// number of collections (`any.collections`), property groups keyed
/// by owner, types with parts whose datasets live in server-computed
/// storage collections (`<typeId>_<key>`, or a module's canonical
/// collection when shared), the write gate (an object holds a storage
/// collection only while its type declares it), the hidden built-in
/// types `page` / `dataview` and collections `miniapp` / `bin`, the
/// bundles registry with children, and the catalog's `general-chat`
/// usecase — just enough surface for deploy / skills / resolver /
/// serve provisioning.
#[derive(Default)]
pub struct FakeSpace {
    state: Mutex<State>,
}

/// The catalog's general chat: one derived, self-typed root per space.
pub const GENERAL_CHAT_BUNDLE: &str = "system:general-chat/v1";

#[derive(Default)]
struct State {
    /// (space, oid) → property groups ({"any": {"type", "collections",
    /// …}, "<ownerId>": {...}})
    objects: BTreeMap<(String, String), Value>,
    /// (space, oid, collection) → rid → stored record (with _addSeq injected)
    datasets: BTreeMap<(String, String, String), BTreeMap<String, Value>>,
    addseq: BTreeMap<(String, String, String), i64>,
    /// space → type rows `{id, xKey, name, hidden?, builtIn?}`; the
    /// built-ins are seeded on first touch
    types: BTreeMap<String, Vec<Value>>,
    /// space → collection rows, same shape; `miniapp` / `bin` seeded
    collections: BTreeMap<String, Vec<Value>>,
    /// (space, ownerId) → property definitions — the owner is a type
    /// or a collection (one property surface, ADR-029 §4)
    props: BTreeMap<(String, String), Vec<Value>>,
    /// (space, typeId) → compiled dataset rows `{id, key, collection,
    /// module, shared?, partId, …draft}`
    dataset_defs: BTreeMap<(String, String), Vec<Value>>,
    markdown: BTreeMap<(String, String), String>,
    /// (space, fileId) → (objectId, name, bytes)
    files: BTreeMap<(String, String), (String, String, Vec<u8>)>,
    /// inviteToken → spaceId (ADR-009 §8)
    invites: BTreeMap<String, String>,
    /// space → [(recordId, identity)] pending join requests
    pending: BTreeMap<String, Vec<(String, String)>>,
    /// (space, identity) → (permission, status)
    members: BTreeMap<(String, String), (String, String)>,
    /// space → bundle id → registry row `{id, rootId, roots, derived?}`
    bundles: BTreeMap<String, BTreeMap<String, Value>>,
    /// (space, bundle id, seed) → the derived child's object id
    children: BTreeMap<(String, String, String), String>,
    next_obj: u64,
    next_type: u64,
    next_coll: u64,
    next_prop: u64,
    next_file: u64,
    next_req: u64,
    next_part: u64,
}

impl FakeSpace {
    pub fn new() -> Self {
        Self::default()
    }

    /// Seed one object row verbatim (property groups + top-level keys such
    /// as a `createdAt` instant) — for tests that rank server stamps.
    pub fn seed_object(&self, space: &str, oid: &str, props: Value) {
        self.state
            .lock()
            .expect("fake lock")
            .objects
            .insert((space.to_string(), oid.to_string()), props);
    }
}

fn group_get<'a>(props: &'a Value, dotted: &str) -> Option<&'a Value> {
    let (group, field) = dotted.split_once('.')?;
    props.get(group)?.get(field)
}

/// One filter leaf: an array-valued property (`any.collections`)
/// matches on membership; `$in` / `$nin` over scalars and arrays
/// (`$nin` matches a row lacking the key, as the server does for the
/// bin exclusion — ADR-029 §2); anything else is equality.
fn matches(got: Option<&Value>, want: &Value) -> bool {
    let values: Vec<&Value> = match got {
        Some(Value::Array(a)) => a.iter().collect(),
        Some(v) => vec![v],
        None => Vec::new(),
    };
    if let Some(ops) = want
        .as_object()
        .filter(|o| o.keys().any(|k| k.starts_with('$')))
    {
        return ops.iter().all(|(op, arg)| {
            let set = arg.as_array().cloned().unwrap_or_default();
            match op.as_str() {
                "$in" => values.iter().any(|v| set.contains(v)),
                "$nin" => !values.iter().any(|v| set.contains(v)),
                _ => false,
            }
        });
    }
    values.iter().any(|v| *v == want)
}

fn err(status: u16, code: &str, message: impl Into<String>) -> (u16, Value) {
    (
        status,
        json!({"error": {"code": code, "message": message.into()}}),
    )
}

/// The registered built-in types every space has (hidden, static).
/// `page` is the one with a part: the shared editor body.
const BUILTIN_TYPES: [&str; 2] = ["page", "dataview"];
/// The registered built-in collections (hidden, static): the sidebar
/// and the bin.
const BUILTIN_COLLECTIONS: [&str; 2] = ["miniapp", "bin"];

/// The keys a type or collection create / patch accepts; anything
/// else is `400 request.unknown_field` (`weight`, `properties`, …).
const DEF_KEYS: [&str; 7] = [
    "name",
    "description",
    "iconCid",
    "xKey",
    "layout",
    "hidden",
    "meta",
];

impl State {
    /// Seed the built-in types on a space's first touch.
    fn touch(&mut self, space: &str) {
        if self.types.contains_key(space) {
            return;
        }
        let rows = BUILTIN_TYPES
            .iter()
            .map(|t| json!({"id": t, "xKey": t, "name": t, "hidden": true, "builtIn": true}))
            .collect();
        self.types.insert(space.to_string(), rows);
        let colls = BUILTIN_COLLECTIONS
            .iter()
            .map(|c| json!({"id": c, "xKey": c, "name": c, "hidden": true, "builtIn": true}))
            .collect();
        self.collections.insert(space.to_string(), colls);
        self.dataset_defs.insert(
            (space.to_string(), "page".to_string()),
            vec![
                json!({"id": "page_body", "key": "editor_blocks", "collection": "editor_blocks",
                        "module": "editor", "shared": true, "partId": "page_body"}),
            ],
        );
    }

    fn type_exists(&self, space: &str, tid: &str) -> bool {
        self.types
            .get(space)
            .map(|rows| rows.iter().any(|t| t["id"] == tid))
            .unwrap_or(false)
    }

    fn collection_exists(&self, space: &str, cid: &str) -> bool {
        self.collections
            .get(space)
            .map(|rows| rows.iter().any(|c| c["id"] == cid))
            .unwrap_or(false)
    }

    /// Whether a handle (xKey or id) is held on either surface — types
    /// and collections share one namespace (ADR-029 §2). Returns the
    /// `type.xkey_conflict` details key naming the holder.
    fn handle_holder(&self, space: &str, xkey: &str) -> Option<(&'static str, String)> {
        let held = |rows: &Vec<Value>| {
            rows.iter()
                .find(|r| r["xKey"] == xkey || r["id"] == xkey)
                .and_then(|r| r["id"].as_str().map(str::to_string))
        };
        if let Some(id) = self.types.get(space).and_then(held) {
            return Some(("existingTypeId", id));
        }
        if let Some(id) = self.collections.get(space).and_then(held) {
            return Some(("existingCollectionId", id));
        }
        None
    }

    /// Validate the two membership slots of a create (`type` required
    /// and a type; every `collections` entry a collection). `None` =
    /// well-formed.
    fn check_membership(&self, space: &str, body: &Value) -> Option<(u16, Value)> {
        if body.get("types").is_some() {
            return Some(err(
                400,
                "request.unknown_field",
                "types: an object has one type (`type`) and any number of collections (`collections`)",
            ));
        }
        let Some(tid) = body["type"].as_str().filter(|t| !t.is_empty()) else {
            return Some(err(
                400,
                "request.missing_field",
                "type: every object has a type (`page` for a plain document)",
            ));
        };
        if !self.type_exists(space, tid) {
            if self.collection_exists(space, tid) {
                return Some(err(
                    400,
                    "type.not_a_type",
                    format!("{tid} is a collection"),
                ));
            }
            return Some(err(
                400,
                "type.not_found",
                format!("type names a type this space does not have: {tid}"),
            ));
        }
        for c in body["collections"].as_array().into_iter().flatten() {
            let Some(cid) = c.as_str() else {
                return Some(err(400, "request.invalid_field", "collections: ids"));
            };
            if !self.collection_exists(space, cid) {
                if self.type_exists(space, cid) {
                    return Some(err(
                        400,
                        "collection.not_a_collection",
                        format!("{cid} is a type"),
                    ));
                }
                return Some(err(
                    400,
                    "collection.not_found",
                    format!("no collection {cid}"),
                ));
            }
        }
        None
    }

    /// The storage collections an object holds: every dataset its ONE
    /// type declares — plus its own, when it is a definition (a
    /// `__type__` row hosts itself: the general-chat root).
    fn held_collections(&self, space: &str, oid: &str) -> Vec<String> {
        let Some(props) = self.objects.get(&(space.to_string(), oid.to_string())) else {
            return Vec::new();
        };
        let mut owners: Vec<String> = Vec::new();
        match props["any"]["type"].as_str() {
            Some("__type__") => owners.push(oid.to_string()),
            Some(tid) => owners.push(tid.to_string()),
            None => {}
        }
        let mut out = Vec::new();
        for tid in owners {
            if let Some(defs) = self.dataset_defs.get(&(space.to_string(), tid)) {
                for d in defs {
                    if let Some(c) = d["collection"].as_str() {
                        out.push(c.to_string());
                    }
                }
            }
        }
        out
    }

    fn records_collection_known(&self, space: &str, collection: &str) -> bool {
        self.dataset_defs
            .iter()
            .filter(|((sp, _), _)| sp == space)
            .any(|(_, defs)| {
                defs.iter()
                    .any(|d| d["collection"] == collection && d["module"] == "records")
            })
    }

    fn create_object(&mut self, space: &str, body: &Value) -> (u16, Value) {
        self.touch(space);
        if let Some(e) = self.check_membership(space, body) {
            return e;
        }
        self.next_obj += 1;
        let oid = format!("obj{}", self.next_obj);
        let mut props = body.get("initialProperties").cloned().unwrap_or(json!({}));
        // mirror the server: the one type is queryable as any.type, the
        // filings as any.collections (absent when filed under none)
        if props.get("any").is_none() {
            props["any"] = json!({});
        }
        props["any"]["type"] = body["type"].clone();
        if let Some(colls) = body["collections"].as_array().filter(|c| !c.is_empty()) {
            props["any"]["collections"] = json!(colls);
        }
        self.objects.insert((space.to_string(), oid.clone()), props);
        (201, json!({"objectId": oid}))
    }

    fn query_objects(&self, space: &str, body: &Value) -> Value {
        let filter = body.get("filter").and_then(Value::as_object);
        let limit = body["limit"].as_u64().unwrap_or(u64::MAX) as usize;
        let records: Vec<Value> = self
            .objects
            .iter()
            .filter(|((sp, _), _)| sp == space)
            .filter(|(_, props)| {
                filter.is_none_or(|f| f.iter().all(|(k, want)| matches(group_get(props, k), want)))
            })
            .take(limit)
            .map(|((_, oid), props)| {
                let mut rec = Map::new();
                rec.insert("id".to_string(), json!(oid));
                if let Some(groups) = props.as_object() {
                    for (g, v) in groups {
                        rec.insert(g.clone(), v.clone());
                    }
                }
                Value::Object(rec)
            })
            .collect();
        json!({"records": records})
    }

    /// A read of a collection nobody serves answers an empty list —
    /// the server's tolerance for a declaring type that has not
    /// synced yet; a mis-keyed collection is silent here too.
    fn query(&self, space: &str, body: &Value) -> Value {
        let oid = body["objectId"].as_str().unwrap_or("").to_string();
        let dataset = body["dataset"].as_str().unwrap_or("").to_string();
        let records: Vec<Value> = self
            .datasets
            .get(&(space.to_string(), oid, dataset))
            .map(|d| {
                d.iter()
                    .map(|(rid, stored)| {
                        let mut rec = Map::new();
                        rec.insert("id".to_string(), json!(rid));
                        if let Some(fields) = stored.as_object() {
                            for (k, v) in fields {
                                rec.insert(k.clone(), v.clone());
                            }
                        }
                        Value::Object(rec)
                    })
                    .collect()
            })
            .unwrap_or_default();
        json!({"records": records})
    }

    /// The write gate: the collection must be a records dataset some
    /// type of the space declares (else `dataset.unknown`) and the
    /// object's type must declare it (else `dataset.not_declared`).
    /// Module collections are never written through /modify.
    fn modify(&mut self, space: &str, body: &Value) -> (u16, Value) {
        let oid = body["objectId"].as_str().unwrap_or("").to_string();
        let dataset = body["dataset"].as_str().unwrap_or("").to_string();
        if !self.records_collection_known(space, &dataset) {
            return err(
                400,
                "dataset.unknown",
                format!("dataset {dataset:?} is not a records dataset this space declares"),
            );
        }
        if !self
            .held_collections(space, &oid)
            .iter()
            .any(|c| c == &dataset)
        {
            return err(
                400,
                "dataset.not_declared",
                "the object's type declares no such collection",
            );
        }
        let key = (space.to_string(), oid, dataset);
        for rec in body["records"].as_array().cloned().unwrap_or_default() {
            let rid = rec["id"].as_str().unwrap_or("").to_string();
            for op in rec["ops"].as_array().cloned().unwrap_or_default() {
                match op["type"].as_str() {
                    Some("$set") => {
                        let seq = self.addseq.entry(key.clone()).or_insert(0);
                        *seq += 1;
                        let mut stored = op["value"].clone();
                        stored["_addSeq"] = json!(*seq);
                        self.datasets
                            .entry(key.clone())
                            .or_default()
                            .insert(rid.clone(), stored);
                    }
                    Some("$unset") => {
                        if let Some(d) = self.datasets.get_mut(&key) {
                            d.remove(&rid);
                        }
                    }
                    _ => {}
                }
            }
        }
        (200, json!({"ok": true}))
    }

    fn set_properties(&mut self, space: &str, oid: &str, tid: &str, patch: &Value) -> Value {
        let props = self
            .objects
            .entry((space.to_string(), oid.to_string()))
            .or_insert(json!({}));
        if props.get(tid).is_none() {
            props[tid] = json!({});
        }
        if let (Some(group), Some(p)) = (props[tid].as_object_mut(), patch.as_object()) {
            for (k, v) in p {
                group.insert(k.clone(), v.clone());
            }
        }
        json!({"ok": true})
    }

    /// POST …/properties/:o/type/:t — replace the object's one type;
    /// values under the old type stay as orphans (ADR-029 §4).
    fn set_type(&mut self, space: &str, oid: &str, tid: &str) -> (u16, Value) {
        self.touch(space);
        if !self.type_exists(space, tid) {
            if self.collection_exists(space, tid) {
                return err(400, "type.not_a_type", format!("{tid} is a collection"));
            }
            return err(404, "type.not_found", format!("no type {tid}"));
        }
        let Some(props) = self.objects.get_mut(&(space.to_string(), oid.to_string())) else {
            return err(404, "object.not_found", format!("no object {oid}"));
        };
        if props.get("any").is_none() {
            props["any"] = json!({});
        }
        props["any"]["type"] = json!(tid);
        (200, json!({"ok": true}))
    }

    /// POST / DELETE …/properties/:o/collections/:c — file / unfile,
    /// idempotent; unfiling keeps the values.
    fn file(&mut self, space: &str, oid: &str, cid: &str, add: bool) -> (u16, Value) {
        self.touch(space);
        if !self.collection_exists(space, cid) {
            if self.type_exists(space, cid) {
                return err(
                    400,
                    "collection.not_a_collection",
                    format!("{cid} is a type"),
                );
            }
            return err(404, "collection.not_found", format!("no collection {cid}"));
        }
        let Some(props) = self.objects.get_mut(&(space.to_string(), oid.to_string())) else {
            return err(404, "object.not_found", format!("no object {oid}"));
        };
        if props.get("any").is_none() {
            props["any"] = json!({});
        }
        let mut colls = props["any"]["collections"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        colls.retain(|c| c != cid);
        if add {
            colls.push(json!(cid));
        }
        props["any"]["collections"] = json!(colls);
        (200, json!({"ok": true}))
    }

    /// The shared create gate of the two definition surfaces: `xKey`
    /// required, unknown keys refused, the handle free on BOTH.
    fn check_definition(&self, space: &str, body: &Value) -> Result<String, (u16, Value)> {
        if let Some(k) = body
            .as_object()
            .into_iter()
            .flat_map(|o| o.keys())
            .find(|k| !DEF_KEYS.contains(&k.as_str()))
        {
            return Err(err(
                400,
                "request.unknown_field",
                format!("unknown field {k}"),
            ));
        }
        let Some(xkey) = body["xKey"].as_str().filter(|x| !x.is_empty()) else {
            return Err(err(400, "type.xkey_required", "xKey required"));
        };
        if let Some((which, id)) = self.handle_holder(space, xkey) {
            return Err((
                409,
                json!({"error": {"code": "type.xkey_conflict",
                                 "message": format!("xKey {xkey:?} is held"),
                                 "details": {"xKey": xkey, which: id}}}),
            ));
        }
        Ok(xkey.to_string())
    }

    fn create_type(&mut self, space: &str, body: &Value) -> (u16, Value) {
        self.touch(space);
        let xkey = match self.check_definition(space, body) {
            Ok(x) => x,
            Err(e) => return e,
        };
        self.next_type += 1;
        let tid = format!("type{}", self.next_type);
        let mut row = json!({"id": tid, "xKey": xkey, "name": body["name"]});
        if body["hidden"] == json!(true) {
            row["hidden"] = json!(true);
        }
        self.types.entry(space.to_string()).or_default().push(row);
        (201, json!({"typeId": tid}))
    }

    fn create_collection(&mut self, space: &str, body: &Value) -> (u16, Value) {
        self.touch(space);
        let xkey = match self.check_definition(space, body) {
            Ok(x) => x,
            Err(e) => return e,
        };
        self.next_coll += 1;
        let cid = format!("coll{}", self.next_coll);
        let mut row = json!({"id": cid, "xKey": xkey, "name": body["name"]});
        if body["hidden"] == json!(true) {
            row["hidden"] = json!(true);
        }
        self.collections
            .entry(space.to_string())
            .or_default()
            .push(row);
        (201, json!({"collectionId": cid}))
    }

    /// The owner of a `…/collections/:c/properties*` route: a
    /// collection this space has (a type id there is
    /// `collection.not_a_collection`, anything else `collection.not_found`).
    fn collection_owner(&mut self, space: &str, cid: &str) -> Option<(u16, Value)> {
        self.touch(space);
        if self.collection_exists(space, cid) {
            return None;
        }
        if self.type_exists(space, cid) {
            return Some(err(
                400,
                "collection.not_a_collection",
                format!("{cid} is a type"),
            ));
        }
        Some(err(
            404,
            "collection.not_found",
            format!("no collection {cid}"),
        ))
    }

    fn patch_type(&mut self, space: &str, tid: &str, body: &Value) -> (u16, Value) {
        self.touch(space);
        let Some(row) = self
            .types
            .get_mut(space)
            .and_then(|rows| rows.iter_mut().find(|t| t["id"] == tid))
        else {
            return err(404, "type.not_found", format!("no type {tid}"));
        };
        if row["builtIn"] == json!(true) {
            return err(400, "type.registered", "built-in types are static");
        }
        if let Some(k) = body
            .as_object()
            .into_iter()
            .flat_map(|o| o.keys())
            .find(|k| !DEF_KEYS.contains(&k.as_str()) || *k == "xKey")
        {
            return err(400, "request.unknown_field", format!("unknown field {k}"));
        }
        for k in ["name", "description", "iconCid", "hidden", "layout", "meta"] {
            if let Some(v) = body.get(k) {
                row[k] = v.clone();
            }
        }
        (204, json!({}))
    }

    /// POST …/types/:tid/parts — the part and its datasets in one
    /// change; the collection is computed here, exactly as the server
    /// does, and only ever read back by clients.
    fn add_part(&mut self, space: &str, tid: &str, draft: &Value) -> (u16, Value) {
        self.touch(space);
        if !self.type_exists(space, tid) {
            return err(404, "type.not_found", format!("no type {tid}"));
        }
        if self.types[space]
            .iter()
            .any(|t| t["id"] == tid && t["builtIn"] == json!(true))
        {
            return err(400, "type.registered", "built-in types are static");
        }
        let Some(part_key) = draft["key"].as_str().filter(|k| !k.is_empty()) else {
            return err(400, "dataset.decl_invalid", "part key required");
        };
        let key = (space.to_string(), tid.to_string());
        let existing = self.dataset_defs.entry(key.clone()).or_default();
        // a part key is unique within the type (pinned) — the fake keeps
        // it on every dataset row the part declares (`partKey`)
        let taken = |k: &str, defs: &Vec<Value>| {
            defs.iter()
                .any(|d| d["key"] == k || d["partId"] == k || d["partKey"] == k)
        };
        if taken(part_key, existing) {
            return err(
                409,
                "dataset.key_conflict",
                format!("key {part_key:?} exists"),
            );
        }
        self.next_part += 1;
        let part_id = format!("part{}", self.next_part);
        let mut rows = Vec::new();
        for ds in draft["datasets"].as_array().cloned().unwrap_or_default() {
            let module = ds["module"].as_str().unwrap_or("records").to_string();
            let shared = ds["shared"] == json!(true);
            let (dkey, collection) = match (module.as_str(), shared) {
                ("editor", true) => ("editor_blocks".to_string(), "editor_blocks".to_string()),
                ("chat", _) => return err(400, "dataset.module_reserved", "chat is the server's"),
                ("records", true) => {
                    return err(400, "dataset.shared_conflict", "records never shares")
                }
                (m, _) if m != "records" && m != "editor" => {
                    return err(400, "dataset.module_unknown", format!("module {m:?}"))
                }
                _ => {
                    let Some(k) = ds["key"].as_str().filter(|k| !k.is_empty()) else {
                        return err(400, "dataset.decl_invalid", "dataset key required");
                    };
                    (k.to_string(), format!("{tid}_{k}"))
                }
            };
            if ds.get("name").is_some() {
                return err(
                    400,
                    "request.unknown_field",
                    "name is not a dataset field; use key",
                );
            }
            if taken(&dkey, existing) || rows.iter().any(|r: &Value| r["key"] == dkey) {
                return err(409, "dataset.key_conflict", format!("key {dkey:?} exists"));
            }
            let mut row = ds.clone();
            row["id"] = json!(format!("ds_{tid}_{dkey}"));
            row["key"] = json!(dkey);
            row["collection"] = json!(collection);
            row["module"] = json!(module);
            row["partId"] = json!(part_id);
            row["partKey"] = json!(part_key);
            if shared {
                row["shared"] = json!(true);
            }
            rows.push(row);
        }
        existing.extend(rows);
        (201, json!({"partId": part_id}))
    }

    /// One property surface behind an owner parameter — a type or a
    /// collection (ADR-029 §4).
    fn add_property(&mut self, space: &str, owner: &str, body: &Value) -> Value {
        self.next_prop += 1;
        let pid = format!("prop{}", self.next_prop);
        self.props
            .entry((space.to_string(), owner.to_string()))
            .or_default()
            .push(json!({"id": pid, "xKey": body["xKey"],
                         "name": body["name"], "kind": body["kind"],
                         // keep the definition's descriptor/meta/scope so
                         // host tests can observe them (ADR-027 §4)
                         "xFormat": body["xFormat"], "meta": body["meta"],
                         "scope": body["scope"]}));
        json!({"propId": pid})
    }

    /// The catalog's `general-chat` usecase: a derived, hidden root
    /// that is its own type (`__type__` in the type slot, hosting
    /// itself), filed under `miniapp`, the one declaration of the
    /// chat module. Adopt-or-install, idempotent.
    fn catalog_setup(&mut self, usecase: &str, space: &str) -> (u16, Value) {
        if usecase != "general-chat" {
            return err(404, "catalog.not_found", format!("no usecase {usecase:?}"));
        }
        self.touch(space);
        let root = format!("chat-{space}");
        let reg = self.bundles.entry(space.to_string()).or_default();
        let installed = !reg.contains_key(GENERAL_CHAT_BUNDLE);
        if installed {
            reg.insert(
                GENERAL_CHAT_BUNDLE.into(),
                json!({"id": GENERAL_CHAT_BUNDLE, "name": "General", "rootId": root,
                       "roots": [root], "losers": [], "derived": true}),
            );
            self.types.entry(space.to_string()).or_default().push(
                json!({"id": root, "xKey": "general_chat", "name": "General", "hidden": true}),
            );
            self.dataset_defs.insert(
                (space.to_string(), root.clone()),
                vec![
                    json!({"id": format!("ds_{root}_chat"), "key": "chat_messages",
                            "collection": "chat_messages", "module": "chat",
                            "shared": true, "partId": "chat"}),
                ],
            );
            self.objects.insert(
                (space.to_string(), root.clone()),
                json!({"any": {"name": "General", "type": "__type__",
                               "collections": ["miniapp"]},
                       "type": {"xkey": "general_chat", "hidden": true,
                                "layout": {"type": "chat"}},
                       "miniapp": {"bundle": GENERAL_CHAT_BUNDLE}}),
            );
        }
        let row = self.bundles[space][GENERAL_CHAT_BUNDLE].clone();
        (
            200,
            json!({"usecase": usecase, "bundles": [{
            "usecase": usecase, "id": GENERAL_CHAT_BUNDLE, "bundle": row,
            "installed": installed, "typeId": root, "collectionId": Value::Null,
            "miniapp": {"bundle": GENERAL_CHAT_BUNDLE}}]}),
        )
    }

    fn ensure_bundle(&mut self, space: &str, body: &Value) -> (u16, Value) {
        self.touch(space);
        let id = body["id"].as_str().unwrap_or("").to_string();
        if id.starts_with("system:") {
            return err(409, "bundle.reserved", "ids under system: are the server's");
        }
        if let Some(row) = self.bundles.get(space).and_then(|r| r.get(&id)) {
            return (200, json!({"bundle": row, "installed": false}));
        }
        let derived = body["derived"] == json!(true);
        if body.get("rootTypes").is_some() {
            return err(400, "request.unknown_field", "rootTypes: use rootType");
        }
        // the runtime declares nothing on its roots, so `rootType` is
        // required here as on the wire (ADR-029 §7)
        let (status, created) = self.create_object(
            space,
            &json!({"type": body["rootType"], "collections": body["rootCollections"],
                    "initialProperties": {"any": {"name": body["name"]}}}),
        );
        if status >= 400 {
            return (status, created);
        }
        let root = created["objectId"].as_str().unwrap_or("").to_string();
        let mut row = json!({"id": id, "name": body["name"], "rootId": root,
                             "roots": [root], "losers": []});
        if derived {
            row["derived"] = json!(true);
        }
        self.bundles
            .entry(space.to_string())
            .or_default()
            .insert(id, row.clone());
        (200, json!({"bundle": row, "installed": true}))
    }

    fn bundle_child(&mut self, space: &str, bundle: &str, body: &Value) -> (u16, Value) {
        let Some(row) = self.bundles.get(space).and_then(|r| r.get(bundle)).cloned() else {
            return err(404, "bundle.not_found", format!("no bundle {bundle}"));
        };
        let seed = body["seed"].as_str().unwrap_or("").to_string();
        if seed.is_empty() {
            return err(400, "request.missing_field", "seed required");
        }
        let key = (space.to_string(), bundle.to_string(), seed.clone());
        if let Some(oid) = self.children.get(&key) {
            return (200, json!({"objectId": oid}));
        }
        if let Some(e) = self.check_membership(space, body) {
            return e;
        }
        let oid = format!(
            "child-{}-{}",
            row["rootId"].as_str().unwrap_or(""),
            seed.replace('/', "-")
        );
        let mut any = json!({"type": body["type"]});
        if let Some(colls) = body["collections"].as_array().filter(|c| !c.is_empty()) {
            any["collections"] = json!(colls);
        }
        self.objects
            .insert((space.to_string(), oid.clone()), json!({"any": any}));
        self.children.insert(key, oid.clone());
        (200, json!({"objectId": oid}))
    }

    /// The editor write gate: the object's type must declare the
    /// shared body (`page`, or a type with a shared editor part).
    fn holds_body(&self, space: &str, oid: &str) -> bool {
        self.held_collections(space, oid)
            .iter()
            .any(|c| c == "editor_blocks")
    }
}

impl Transport for FakeSpace {
    fn send(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
    ) -> Result<(u16, Value), AnyError> {
        let mut s = self.state.lock().unwrap();
        let body = body.cloned().unwrap_or(json!({}));
        let (route, query) = path.split_once('?').unwrap_or((path, ""));
        let segs: Vec<&str> = route.trim_start_matches('/').split('/').collect();
        let reply = match (method, segs.as_slice()) {
            ("POST", ["v1", "catalog", usecase, "setup"]) => {
                let sp = body["spaceId"].as_str().unwrap_or("").to_string();
                return Ok(s.catalog_setup(usecase, &sp));
            }
            ("POST", ["v1", "spaces", sp, "objects", "query"]) => s.query_objects(sp, &body),
            ("POST", ["v1", "spaces", sp, "objects"]) => return Ok(s.create_object(sp, &body)),
            ("POST", ["v1", "spaces", sp, "query"]) => s.query(sp, &body),
            ("POST", ["v1", "spaces", sp, "modify"]) => return Ok(s.modify(sp, &body)),
            ("POST", ["v1", "spaces", sp, "properties", oid, "set", tid]) => {
                let (sp, oid, tid) = (sp.to_string(), oid.to_string(), tid.to_string());
                s.set_properties(&sp, &oid, &tid, &body["patch"])
            }
            ("POST", ["v1", "spaces", sp, "properties", oid, "type", tid]) => {
                let (sp, oid, tid) = (sp.to_string(), oid.to_string(), tid.to_string());
                return Ok(s.set_type(&sp, &oid, &tid));
            }
            ("POST", ["v1", "spaces", sp, "properties", oid, "collections", cid]) => {
                let (sp, oid, cid) = (sp.to_string(), oid.to_string(), cid.to_string());
                return Ok(s.file(&sp, &oid, &cid, true));
            }
            ("DELETE", ["v1", "spaces", sp, "properties", oid, "collections", cid]) => {
                let (sp, oid, cid) = (sp.to_string(), oid.to_string(), cid.to_string());
                return Ok(s.file(&sp, &oid, &cid, false));
            }
            ("GET", ["v1", "spaces", sp, "properties", oid]) => {
                json!({"record": s.objects.get(&(sp.to_string(), oid.to_string())).cloned()})
            }
            ("GET", ["v1", "spaces", sp, "types"]) => {
                s.touch(sp);
                let include_hidden = query.contains("includeHidden=true");
                let rows: Vec<Value> = s.types[*sp]
                    .iter()
                    .filter(|t| include_hidden || t["hidden"] != json!(true))
                    .cloned()
                    .collect();
                json!({"types": rows})
            }
            ("POST", ["v1", "spaces", sp, "types"]) => return Ok(s.create_type(sp, &body)),
            ("PATCH", ["v1", "spaces", sp, "types", tid]) => {
                let (sp, tid) = (sp.to_string(), tid.to_string());
                return Ok(s.patch_type(&sp, &tid, &body));
            }
            ("POST", ["v1", "spaces", sp, "types", tid, "parts"]) => {
                let (sp, tid) = (sp.to_string(), tid.to_string());
                return Ok(s.add_part(&sp, &tid, &body));
            }
            ("GET", ["v1", "spaces", sp, "types", tid, "datasets"]) => {
                json!({"datasets": s.dataset_defs
                    .get(&(sp.to_string(), tid.to_string())).cloned().unwrap_or_default()})
            }
            ("GET", ["v1", "spaces", sp, "types", tid, "properties"]) => {
                json!({"properties": s.props.get(&(sp.to_string(), tid.to_string()))
                                      .cloned().unwrap_or_default()})
            }
            ("POST", ["v1", "spaces", sp, "types", tid, "properties"]) => {
                let (sp, tid) = (sp.to_string(), tid.to_string());
                s.add_property(&sp, &tid, &body)
            }
            ("GET", ["v1", "spaces", sp, "collections"]) => {
                s.touch(sp);
                let include_hidden = query.contains("includeHidden=true");
                let rows: Vec<Value> = s.collections[*sp]
                    .iter()
                    .filter(|c| include_hidden || c["hidden"] != json!(true))
                    .cloned()
                    .collect();
                json!({"collections": rows})
            }
            ("POST", ["v1", "spaces", sp, "collections"]) => {
                return Ok(s.create_collection(sp, &body));
            }
            ("GET", ["v1", "spaces", sp, "collections", cid, "properties"]) => {
                if let Some(e) = s.collection_owner(sp, cid) {
                    return Ok(e);
                }
                json!({"properties": s.props.get(&(sp.to_string(), cid.to_string()))
                                      .cloned().unwrap_or_default()})
            }
            ("POST", ["v1", "spaces", sp, "collections", cid, "properties"]) => {
                let (sp, cid) = (sp.to_string(), cid.to_string());
                if let Some(e) = s.collection_owner(&sp, &cid) {
                    return Ok(e);
                }
                s.add_property(&sp, &cid, &body)
            }
            (
                "GET",
                ["v1", "spaces", sp, "objects", oid, "editor", "editor_blocks", "markdown"],
            ) => {
                json!({"content": s.markdown.get(&(sp.to_string(), oid.to_string()))
                                   .cloned().unwrap_or_default()})
            }
            (
                "PUT",
                ["v1", "spaces", sp, "objects", oid, "editor", "editor_blocks", "markdown"],
            ) => {
                if !s.holds_body(sp, oid) {
                    return Ok(err(
                        400,
                        "dataset.not_declared",
                        "the object's type declares no such collection",
                    ));
                }
                let content = body["content"].as_str().unwrap_or("").to_string();
                s.markdown
                    .insert((sp.to_string(), oid.to_string()), content);
                json!({"ok": true})
            }
            ("POST", ["v1", "spaces", sp, "bundles"]) => return Ok(s.ensure_bundle(sp, &body)),
            ("GET", ["v1", "spaces", sp, "bundles"]) => {
                let rows: Vec<Value> = s
                    .bundles
                    .get(*sp)
                    .map(|r| r.values().cloned().collect())
                    .unwrap_or_default();
                json!({"bundles": rows, "synced": true})
            }
            ("POST", ["v1", "spaces", sp, "bundles", enc, "children"]) => {
                let bundle = enc.replace("%2F", "/").replace("%3A", ":");
                return Ok(s.bundle_child(sp, &bundle, &body));
            }
            ("GET", ["v1", "spaces", sp, "files", fid]) => {
                match s.files.get(&(sp.to_string(), fid.to_string())) {
                    Some((oid, name, bytes)) => json!({"fileId": fid, "objectId": oid,
                                                       "name": name, "size": bytes.len()}),
                    None => return Ok(err(404, "not_found", format!("no file {fid}"))),
                }
            }
            // --- sharing (ADR-009 §8) ---
            ("POST", ["v1", "spaces", sp, "invites"]) => {
                let token = format!("inv_{sp}");
                s.invites.insert(token.clone(), sp.to_string());
                json!({"inviteToken": token, "spaceId": sp})
            }
            ("POST", ["v1", "spaces", "join"]) => {
                let token = body["inviteToken"].as_str().unwrap_or("");
                let Some(sp) = s.invites.get(token).cloned() else {
                    return Ok(err(400, "invite.invalid", "unknown invite"));
                };
                s.next_req += 1;
                let rid = format!("req{}", s.next_req);
                let identity = body["metadata"]["name"].as_str().unwrap_or("joiner");
                s.pending
                    .entry(sp.clone())
                    .or_default()
                    .push((rid, identity.to_string()));
                return Ok((202, json!({"id": sp, "status": "joining"})));
            }
            ("GET", ["v1", "spaces", sp, "members", "requests"]) => {
                let reqs: Vec<Value> = s
                    .pending
                    .get(*sp)
                    .map(|v| {
                        v.iter()
                            .map(|(rid, id)| json!({"recordId": rid, "identity": id}))
                            .collect()
                    })
                    .unwrap_or_default();
                json!({"requests": reqs})
            }
            ("POST", ["v1", "spaces", sp, "acl", "accept"]) => {
                let rid = body["requestRecordId"].as_str().unwrap_or("");
                let perm = body["permission"].as_str().unwrap_or("none").to_string();
                let Some(list) = s.pending.get_mut(*sp) else {
                    return Ok(err(404, "request.not_found", "no pending requests"));
                };
                let Some(pos) = list.iter().position(|(r, _)| r == rid) else {
                    return Ok(err(404, "request.not_found", format!("no request {rid}")));
                };
                let (_, identity) = list.remove(pos);
                s.members
                    .insert((sp.to_string(), identity), (perm, "active".into()));
                json!({})
            }
            ("GET", ["v1", "spaces", sp, "members"]) => {
                let members: Vec<Value> = s
                    .members
                    .iter()
                    .filter(|((space, _), _)| space == sp)
                    .map(|((_, id), (perm, status))| {
                        json!({"identity": id, "permission": perm, "status": status})
                    })
                    .collect();
                json!({"members": members})
            }
            _ => return Ok(err(404, "request.not_found", format!("{method} {path}"))),
        };
        Ok((200, reply))
    }

    fn open_stream(
        &self,
        _path: &str,
        _body: Option<&Value>,
    ) -> Result<Box<dyn Iterator<Item = String>>, AnyError> {
        Ok(Box::new(std::iter::empty()))
    }

    fn send_raw(
        &self,
        method: &str,
        path: &str,
        body: &[u8],
        _content_type: &str,
    ) -> Result<(u16, Value), AnyError> {
        let mut s = self.state.lock().unwrap();
        let (route, query) = path.split_once('?').unwrap_or((path, ""));
        let segs: Vec<&str> = route.trim_start_matches('/').split('/').collect();
        match (method, segs.as_slice()) {
            ("POST", ["v1", "spaces", sp, "objects", oid, "files"]) => {
                let name = query
                    .split('&')
                    .find_map(|kv| kv.strip_prefix("name="))
                    .unwrap_or("")
                    .to_string();
                s.next_file += 1;
                let fid = format!("file{}", s.next_file);
                s.files.insert(
                    (sp.to_string(), fid.clone()),
                    (oid.to_string(), name.clone(), body.to_vec()),
                );
                Ok((
                    201,
                    json!({"fileId": fid, "objectId": oid, "name": name,
                           "size": body.len()}),
                ))
            }
            _ => Ok(err(404, "request.not_found", format!("{method} {path}"))),
        }
    }

    fn read_raw(&self, path: &str) -> Result<Vec<u8>, AnyError> {
        let s = self.state.lock().unwrap();
        let segs: Vec<&str> = path.trim_start_matches('/').split('/').collect();
        if let ["v1", "spaces", sp, "files", fid, "content"] = segs.as_slice() {
            if let Some((_, _, bytes)) = s.files.get(&(sp.to_string(), fid.to_string())) {
                return Ok(bytes.clone());
            }
        }
        Err(AnyError {
            status: 404,
            code: "not_found".into(),
            message: format!("no file at {path}"),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::anyapi::Client;

    /// The fake speaks the wire's membership contract (ADR-029 §2/§3):
    /// one required `type`, `collections` optional, the old plural a
    /// hard refusal, `$nin bin` matching rows filed under nothing.
    #[test]
    fn create_gate_and_membership_filters() {
        let c = Client::with_transport(Box::new(FakeSpace::new()));
        let old = c
            .create_object("sp", &json!({"types": ["page"]}))
            .unwrap_err();
        assert_eq!(old.code, "request.unknown_field");
        let none = c.create_object("sp", &json!({})).unwrap_err();
        assert_eq!(none.code, "request.missing_field");
        let slot = c
            .create_object("sp", &json!({"type": "miniapp"}))
            .unwrap_err();
        assert_eq!(slot.code, "type.not_a_type");
        let slot = c
            .create_object("sp", &json!({"type": "page", "collections": ["dataview"]}))
            .unwrap_err();
        assert_eq!(slot.code, "collection.not_a_collection");
        let plain = c.create_object("sp", &json!({"type": "page"})).unwrap();
        let filed = c
            .create_object("sp", &json!({"type": "page", "collections": ["bin"]}))
            .unwrap();
        let rows = c
            .query_objects(
                "sp",
                &json!({"filter": {"any.type": "page",
                                   "any.collections": {"$nin": ["bin"]}}}),
            )
            .unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["id"], plain["objectId"]);
        assert!(rows[0]["any"].get("collections").is_none());
        let binned = c
            .query_objects("sp", &json!({"filter": {"any.collections": "bin"}}))
            .unwrap();
        assert_eq!(binned[0]["id"], filed["objectId"]);
        // one handle namespace across the two surfaces
        let e = c
            .create_type("sp", &json!({"name": "Bin", "xKey": "bin"}))
            .unwrap_err();
        assert_eq!(e.code, "type.xkey_conflict");
        let e = c
            .create_type("sp", &json!({"name": "W", "xKey": "w", "weight": 1}))
            .unwrap_err();
        assert_eq!(e.code, "request.unknown_field");
    }
}
