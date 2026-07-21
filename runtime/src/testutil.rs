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

/// A minimal in-memory `any` server: per-space objects with property
/// groups, per-object datasets with `_addSeq`, types/properties,
/// markdown and the agent brain — just enough surface for
/// deploy/skills/resolver.
#[derive(Default)]
pub struct FakeSpace {
    state: Mutex<State>,
}

#[derive(Default)]
struct State {
    /// (space, oid) → property groups ({"any": {...}, "program": {...}})
    objects: BTreeMap<(String, String), Value>,
    /// (space, oid, dataset) → rid → stored record (with _addSeq injected)
    datasets: BTreeMap<(String, String, String), BTreeMap<String, Value>>,
    addseq: BTreeMap<(String, String, String), i64>,
    types: BTreeMap<String, Vec<Value>>,
    props: BTreeMap<(String, String), Vec<Value>>,
    markdown: BTreeMap<(String, String), String>,
    /// (space, fileId) → (objectId, name, bytes)
    files: BTreeMap<(String, String), (String, String, Vec<u8>)>,
    /// inviteToken → spaceId (ADR-009 §8)
    invites: BTreeMap<String, String>,
    /// space → [(recordId, identity)] pending join requests
    pending: BTreeMap<String, Vec<(String, String)>>,
    /// (space, identity) → (permission, status)
    members: BTreeMap<(String, String), (String, String)>,
    next_obj: u64,
    next_type: u64,
    next_prop: u64,
    next_file: u64,
    next_req: u64,
}

impl FakeSpace {
    pub fn new() -> Self {
        Self::default()
    }
}

fn group_get<'a>(props: &'a Value, dotted: &str) -> Option<&'a Value> {
    let (group, field) = dotted.split_once('.')?;
    props.get(group)?.get(field)
}

impl State {
    fn create_object(&mut self, space: &str, body: &Value) -> Value {
        self.next_obj += 1;
        let oid = format!("obj{}", self.next_obj);
        let mut props = body.get("initialProperties").cloned().unwrap_or(json!({}));
        // mirror the server: requested types are queryable as any.types
        if let Some(types) = body.get("types") {
            if props.get("any").is_none() {
                props["any"] = json!({});
            }
            props["any"]["types"] = types.clone();
        }
        self.objects.insert((space.to_string(), oid.clone()), props);
        json!({"objectId": oid})
    }

    fn query_objects(&self, space: &str, body: &Value) -> Value {
        let filter = body.get("filter").and_then(Value::as_object);
        let limit = body["limit"].as_u64().unwrap_or(u64::MAX) as usize;
        let records: Vec<Value> = self
            .objects
            .iter()
            .filter(|((sp, _), _)| sp == space)
            .filter(|(_, props)| {
                filter.is_none_or(|f| {
                    f.iter().all(|(k, want)| match group_get(props, k) {
                        // array-valued property (any.types) matches on membership
                        Some(Value::Array(a)) => a.contains(want),
                        got => got == Some(want),
                    })
                })
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

    fn modify(&mut self, space: &str, body: &Value) -> Value {
        let oid = body["objectId"].as_str().unwrap_or("").to_string();
        let dataset = body["dataset"].as_str().unwrap_or("").to_string();
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
        json!({"ok": true})
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

    fn create_type(&mut self, space: &str, body: &Value) -> Value {
        self.next_type += 1;
        let tid = format!("type{}", self.next_type);
        self.types
            .entry(space.to_string())
            .or_default()
            .push(json!({"id": tid, "xKey": body["xKey"], "name": body["name"]}));
        json!({"typeId": tid})
    }

    fn add_property(&mut self, space: &str, tid: &str, body: &Value) -> Value {
        self.next_prop += 1;
        let pid = format!("prop{}", self.next_prop);
        self.props
            .entry((space.to_string(), tid.to_string()))
            .or_default()
            .push(json!({"id": pid, "xKey": body["xKey"],
                         "name": body["name"], "kind": body["kind"]}));
        json!({"propId": pid})
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
        let segs: Vec<&str> = path.trim_start_matches('/').split('/').collect();
        let reply = match (method, segs.as_slice()) {
            ("POST", ["v1", "spaces", sp, "objects", "query"]) => s.query_objects(sp, &body),
            ("POST", ["v1", "spaces", sp, "objects"]) => s.create_object(sp, &body),
            ("POST", ["v1", "spaces", sp, "query"]) => s.query(sp, &body),
            ("POST", ["v1", "spaces", sp, "modify"]) => s.modify(sp, &body),
            ("POST", ["v1", "spaces", sp, "properties", oid, "set", tid]) => {
                let (sp, oid, tid) = (sp.to_string(), oid.to_string(), tid.to_string());
                s.set_properties(&sp, &oid, &tid, &body["patch"])
            }
            ("GET", ["v1", "spaces", sp, "types"]) => {
                json!({"types": s.types.get(*sp).cloned().unwrap_or_default()})
            }
            ("POST", ["v1", "spaces", sp, "types"]) => s.create_type(sp, &body),
            ("GET", ["v1", "spaces", sp, "types", tid, "properties"]) => {
                json!({"properties": s.props.get(&(sp.to_string(), tid.to_string()))
                                      .cloned().unwrap_or_default()})
            }
            ("POST", ["v1", "spaces", sp, "types", tid, "properties"]) => {
                let (sp, tid) = (sp.to_string(), tid.to_string());
                s.add_property(&sp, &tid, &body)
            }
            ("GET", ["v1", "spaces", sp, "objects", oid, "editor", "markdown"]) => {
                json!({"content": s.markdown.get(&(sp.to_string(), oid.to_string()))
                                   .cloned().unwrap_or_default()})
            }
            ("PUT", ["v1", "spaces", sp, "objects", oid, "editor", "markdown"]) => {
                let content = body["content"].as_str().unwrap_or("").to_string();
                s.markdown
                    .insert((sp.to_string(), oid.to_string()), content);
                json!({"ok": true})
            }
            ("GET", ["v1", "spaces", sp, "files", fid]) => {
                match s.files.get(&(sp.to_string(), fid.to_string())) {
                    Some((oid, name, bytes)) => json!({"fileId": fid, "objectId": oid,
                                                       "name": name, "size": bytes.len()}),
                    None => {
                        return Ok((
                            404,
                            json!({"error": {"code": "not_found",
                                             "message": format!("no file {fid}")}}),
                        ))
                    }
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
                    return Ok((
                        400,
                        json!({"error": {"code": "invite.invalid",
                                         "message": "unknown invite"}}),
                    ));
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
                    return Ok((
                        404,
                        json!({"error": {"code": "request.not_found",
                                         "message": "no pending requests"}}),
                    ));
                };
                let Some(pos) = list.iter().position(|(r, _)| r == rid) else {
                    return Ok((
                        404,
                        json!({"error": {"code": "request.not_found",
                                         "message": format!("no request {rid}")}}),
                    ));
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
            ("GET", ["v1", "spaces", _, "agent", "brain"]) => json!({"objectId": "brain"}),
            _ => {
                return Ok((
                    404,
                    json!({"error": {"code": "no_route",
                                     "message": format!("{method} {path}")}}),
                ))
            }
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
            _ => Ok((
                404,
                json!({"error": {"code": "no_route",
                                 "message": format!("{method} {path}")}}),
            )),
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
