//! anyapi — blocking typed HTTP client for the `any` server, the Rust
//! twin of anybao/anyclient.py (the serve slice). The transport is
//! injectable (`send(method, path, body) -> (status, json)`) so the
//! client is unit-testable with no server; SSE rides a separate
//! line-stream transport, exactly like the Python reference.

// the ported surface IS the contract; the bin grows into it
#![allow(dead_code)]

use serde_json::{json, Map, Value};
use std::fmt;
use std::io::{BufRead, BufReader, Read};

/// Error envelope of the `any` server: `{"error": {code, message}}`
/// mapped from any >= 400 status. Transport-level failures surface as
/// status 0 / code "transport".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AnyError {
    pub status: u16,
    pub code: String,
    pub message: String,
}

impl fmt::Display for AnyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} {}: {}", self.status, self.code, self.message)
    }
}

impl std::error::Error for AnyError {}

/// Strip NUL bytes from strings before any dataset write. anyenc/
/// fastjson rejects \x00 in JSON strings and we deliberately don't fork
/// upstream — guard at the write boundary (docs/m0-notes.md, project
/// gotcha). Binary-ish HTTP bodies are the realistic source.
pub fn sanitize_nuls(obj: &Value) -> Value {
    match obj {
        Value::String(s) => {
            if s.contains('\u{0}') {
                Value::String(s.replace('\u{0}', "\u{FFFD}"))
            } else {
                obj.clone()
            }
        }
        Value::Object(m) => Value::Object(
            m.iter()
                .map(|(k, v)| (k.clone(), sanitize_nuls(v)))
                .collect(),
        ),
        Value::Array(a) => Value::Array(a.iter().map(sanitize_nuls).collect()),
        _ => obj.clone(),
    }
}

/// One parsed SSE frame: `event` (default "message") + JSON-decoded
/// `data` (raw-string fallback).
#[derive(Debug, Clone, PartialEq)]
pub struct Frame {
    pub event: String,
    pub data: Value,
}

/// Fold raw SSE lines into frames. `data:` may span multiple lines
/// (joined with \n); a blank line dispatches the frame; `:` comment
/// lines (heartbeats) are skipped. A trailing frame with no closing
/// blank line is dropped — exactly the Python `_parse_sse` contract.
pub struct SseFrames<I> {
    lines: I,
}

pub fn parse_sse<I: Iterator<Item = String>>(lines: I) -> SseFrames<I> {
    SseFrames { lines }
}

impl<I: Iterator<Item = String>> Iterator for SseFrames<I> {
    type Item = Frame;

    fn next(&mut self) -> Option<Frame> {
        let mut event = "message".to_string();
        let mut data_lines: Vec<String> = Vec::new();
        for line in self.lines.by_ref() {
            if line.is_empty() {
                if !data_lines.is_empty() {
                    let raw = data_lines.join("\n");
                    let data = serde_json::from_str(&raw).unwrap_or(Value::String(raw));
                    return Some(Frame { event, data });
                }
                event = "message".to_string();
                data_lines.clear();
                continue;
            }
            if line.starts_with(':') {
                continue; // SSE comment / heartbeat
            }
            let (field, value) = match line.split_once(':') {
                Some((f, v)) => (f, v.strip_prefix(' ').unwrap_or(v)),
                None => (line.as_str(), ""),
            };
            match field {
                "event" => event = value.to_string(),
                "data" => data_lines.push(value.to_string()),
                _ => {}
            }
        }
        None
    }
}

/// Injectable wire layer: one-shot JSON request/response + an SSE line
/// stream. Non-2xx statuses are DATA here — the client maps them to
/// `AnyError`; only transport-level failures error out.
pub trait Transport {
    fn send(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
    ) -> Result<(u16, Value), AnyError>;
    fn open_stream(
        &self,
        path: &str,
        body: Option<&Value>,
    ) -> Result<Box<dyn Iterator<Item = String>>, AnyError>;
    /// Raw-bytes request (file upload): bytes in, JSON reply out.
    /// Defaulted so JSON-only doubles keep compiling (ADR-009 §4).
    fn send_raw(
        &self,
        method: &str,
        path: &str,
        body: &[u8],
        content_type: &str,
    ) -> Result<(u16, Value), AnyError> {
        let _ = (method, path, body, content_type);
        Err(transport_err("raw bytes unsupported by this transport"))
    }
    /// Raw-bytes response (file download). Non-2xx replies surface as
    /// AnyError here — there is no JSON channel on this path.
    fn read_raw(&self, path: &str) -> Result<Vec<u8>, AnyError> {
        let _ = path;
        Err(transport_err("raw bytes unsupported by this transport"))
    }
}

fn transport_err(e: impl fmt::Display) -> AnyError {
    AnyError {
        status: 0,
        code: "transport".into(),
        message: e.to_string(),
    }
}

/// ureq-backed transport against a base URL (localhost `any` server).
pub struct HttpTransport {
    base: String,
    agent: ureq::Agent,
}

impl HttpTransport {
    pub fn new(base_url: &str) -> Self {
        HttpTransport {
            base: base_url.trim_end_matches('/').to_string(),
            agent: ureq::agent(),
        }
    }

    fn request(&self, method: &str, path: &str) -> ureq::Request {
        self.agent
            .request(method, &format!("{}{}", self.base, path))
            .set("Content-Type", "application/json")
    }

    fn dispatch(req: ureq::Request, body: Option<&Value>) -> Result<ureq::Response, AnyError> {
        let sent = match body {
            Some(b) => req.send_string(&b.to_string()),
            None => req.call(),
        };
        match sent {
            Ok(r) => Ok(r),
            Err(ureq::Error::Status(_, r)) => Ok(r), // error envelope is data
            Err(e) => Err(transport_err(e)),
        }
    }
}

/// Decode a response body as the (status, JSON) pair `send` promises.
fn json_response(resp: ureq::Response) -> Result<(u16, Value), AnyError> {
    let status = resp.status();
    let raw = resp.into_string().map_err(transport_err)?;
    let data = if raw.is_empty() {
        json!({})
    } else {
        serde_json::from_str(&raw).map_err(|e| AnyError {
            status,
            code: "bad_json".into(),
            message: e.to_string(),
        })?
    };
    Ok((status, data))
}

impl Transport for HttpTransport {
    fn send(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
    ) -> Result<(u16, Value), AnyError> {
        let resp = Self::dispatch(self.request(method, path), body)?;
        json_response(resp)
    }

    fn open_stream(
        &self,
        path: &str,
        body: Option<&Value>,
    ) -> Result<Box<dyn Iterator<Item = String>>, AnyError> {
        let req = self
            .request("POST", path)
            .set("Accept", "text/event-stream");
        let resp = Self::dispatch(req, body)?;
        Ok(Box::new(RawLines {
            reader: BufReader::new(resp.into_reader()),
        }))
    }

    fn send_raw(
        &self,
        method: &str,
        path: &str,
        body: &[u8],
        content_type: &str,
    ) -> Result<(u16, Value), AnyError> {
        let req = self
            .agent
            .request(method, &format!("{}{}", self.base, path))
            .set("Content-Type", content_type);
        let resp = match req.send_bytes(body) {
            Ok(r) => r,
            Err(ureq::Error::Status(_, r)) => r, // error envelope is data
            Err(e) => return Err(transport_err(e)),
        };
        json_response(resp)
    }

    fn read_raw(&self, path: &str) -> Result<Vec<u8>, AnyError> {
        let resp = Self::dispatch(self.request("GET", path), None)?;
        let status = resp.status();
        if status >= 400 {
            let (_, data) = json_response(resp)?;
            let err = data.get("error").cloned().unwrap_or(json!({}));
            return Err(AnyError {
                status,
                code: err["code"].as_str().unwrap_or("unknown").to_string(),
                message: err["message"].as_str().unwrap_or("").to_string(),
            });
        }
        let mut buf = Vec::new();
        resp.into_reader()
            .read_to_end(&mut buf)
            .map_err(transport_err)?;
        Ok(buf)
    }
}

/// Decoded text lines off a byte stream: split on `\n`, lossy UTF-8,
/// trailing `\n` stripped (and nothing else — the Python transport's
/// `rstrip("\n")`).
struct RawLines<R: Read> {
    reader: BufReader<R>,
}

impl<R: Read> Iterator for RawLines<R> {
    type Item = String;

    fn next(&mut self) -> Option<String> {
        let mut buf = Vec::new();
        match self.reader.read_until(b'\n', &mut buf) {
            Ok(0) | Err(_) => None,
            Ok(_) => {
                if buf.last() == Some(&b'\n') {
                    buf.pop();
                }
                Some(String::from_utf8_lossy(&buf).into_owned())
            }
        }
    }
}

/// Merge extra JSON-object options over a base body (the Rust stand-in
/// for Python's `**opts` kwargs).
fn merged(base: &[(&str, Value)], opts: &Value) -> Value {
    let mut m = Map::new();
    for (k, v) in base {
        m.insert((*k).to_string(), v.clone());
    }
    if let Some(o) = opts.as_object() {
        for (k, v) in o {
            m.insert(k.clone(), v.clone());
        }
    }
    Value::Object(m)
}

fn records_of(reply: Value, key: &str) -> Vec<Value> {
    reply
        .get(key)
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

/// Map the server's `{"error": {code, message}}` envelope to AnyError
/// on non-2xx; pass data through otherwise.
fn unwrap_envelope(status: u16, data: Value) -> Result<Value, AnyError> {
    if status >= 400 {
        let err = data.get("error").cloned().unwrap_or(json!({}));
        return Err(AnyError {
            status,
            code: err["code"].as_str().unwrap_or("unknown").to_string(),
            message: err["message"].as_str().unwrap_or("").to_string(),
        });
    }
    Ok(data)
}

/// Percent-encode one query-string value (RFC 3986 unreserved set).
fn urlencode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

pub struct Client {
    transport: Box<dyn Transport + Send + Sync>,
}

impl Client {
    pub fn new(base_url: &str) -> Self {
        Client {
            transport: Box::new(HttpTransport::new(base_url)),
        }
    }

    pub fn with_transport(transport: Box<dyn Transport + Send + Sync>) -> Self {
        Client { transport }
    }

    fn call(&self, method: &str, path: &str, body: Option<&Value>) -> Result<Value, AnyError> {
        let sanitized = body.map(sanitize_nuls); // write guard
        let (status, data) = self.transport.send(method, path, sanitized.as_ref())?;
        unwrap_envelope(status, data)
    }

    // --- spaces ---
    pub fn list_spaces(&self, status: Option<&str>) -> Result<Vec<Value>, AnyError> {
        let path = match status {
            Some(s) => format!("/v1/spaces?status={s}"),
            None => "/v1/spaces".to_string(),
        };
        Ok(records_of(self.call("GET", &path, None)?, "spaces"))
    }

    /// GET /v1/spaces/{spaceId} — the single-space handle. Unlike the
    /// list route, this reply carries the derived `generalChatObjectId`
    /// (server materializes the space's one general chat on first sight)
    /// and `spaceIndexObjectId` — the fields anybao resolves its chat
    /// against (ADR-006 §0).
    pub fn get_space(&self, space_id: &str) -> Result<Value, AnyError> {
        self.call("GET", &format!("/v1/spaces/{space_id}"), None)
    }

    /// POST /v1/spaces — the `ensure_space` create half (ADR-006 §0).
    /// Reply carries the new space `id`. `agent_space: true` provisions
    /// the per-space config object (ADR-006 §3) so the harness can
    /// resolve `agentConfigObjectId` off the very first GET.
    pub fn create_space(&self, name: &str) -> Result<Value, AnyError> {
        self.call(
            "POST",
            "/v1/spaces",
            Some(&json!({
                "name": name,
                "spaceType": "anytype.space",
                "agent_space": true
            })),
        )
    }

    // --- files (ADR-009 §4) ---
    /// POST /v1/spaces/{s}/objects/{o}/files?name=… — attach raw bytes
    /// to an object; the reply is the server's FileInfo (fileId, size,
    /// rootCid, …).
    pub fn attach_file(
        &self,
        space_id: &str,
        object_id: &str,
        name: &str,
        bytes: &[u8],
    ) -> Result<Value, AnyError> {
        let path = format!(
            "/v1/spaces/{space_id}/objects/{object_id}/files?name={}",
            urlencode(name)
        );
        let (status, data) =
            self.transport
                .send_raw("POST", &path, bytes, "application/octet-stream")?;
        unwrap_envelope(status, data)
    }

    /// GET /v1/spaces/{s}/files/{f} — one file's FileInfo.
    pub fn file_info(&self, space_id: &str, file_id: &str) -> Result<Value, AnyError> {
        self.call(
            "GET",
            &format!("/v1/spaces/{space_id}/files/{file_id}"),
            None,
        )
    }

    /// GET /v1/spaces/{s}/files/{f}/content — the raw bytes.
    pub fn download_file(&self, space_id: &str, file_id: &str) -> Result<Vec<u8>, AnyError> {
        self.transport
            .read_raw(&format!("/v1/spaces/{space_id}/files/{file_id}/content"))
    }

    // --- objects ---
    pub fn create_object(&self, space_id: &str, body: &Value) -> Result<Value, AnyError> {
        self.call(
            "POST",
            &format!("/v1/spaces/{space_id}/objects"),
            Some(body),
        )
    }

    /// Cross-object query over the per-space objects collection.
    /// `opts`: JSON object — filter/sort/limit/…
    pub fn query_objects(&self, space_id: &str, opts: &Value) -> Result<Vec<Value>, AnyError> {
        let reply = self.call(
            "POST",
            &format!("/v1/spaces/{space_id}/objects/query"),
            Some(opts),
        )?;
        Ok(records_of(reply, "records"))
    }

    /// Per-object dataset query (chat_messages, editor_blocks, …).
    pub fn query(
        &self,
        space_id: &str,
        object_id: &str,
        dataset: &str,
        opts: &Value,
    ) -> Result<Vec<Value>, AnyError> {
        let body = merged(
            &[("objectId", json!(object_id)), ("dataset", json!(dataset))],
            opts,
        );
        let reply = self.call("POST", &format!("/v1/spaces/{space_id}/query"), Some(&body))?;
        Ok(records_of(reply, "records"))
    }

    pub fn modify(&self, space_id: &str, body: &Value) -> Result<Value, AnyError> {
        self.call("POST", &format!("/v1/spaces/{space_id}/modify"), Some(body))
    }

    /// Write one dataset record (whole-value $set, upsert) — the generic
    /// path for plain (unregistered) datasets like agent_triggers.
    pub fn upsert_record(
        &self,
        space_id: &str,
        object_id: &str,
        dataset: &str,
        record_id: &str,
        value: &Value,
    ) -> Result<Value, AnyError> {
        self.modify(
            space_id,
            &json!({
                "objectId": object_id, "dataset": dataset,
                "records": [{"id": record_id, "upsert": true,
                             "ops": [{"type": "$set", "path": "", "value": value}]}]}),
        )
    }

    /// Set a single DEVICE-LOCAL field on an existing record — the
    /// `scope: "local"` write route (ADR-006 §3). The value never syncs;
    /// the dataset schema must declare `field` local-scope (else the
    /// server rejects it as a synced field). The record must already
    /// exist from a prior synced write: local scope cannot create records
    /// (no upsert), needs an explicit id, and carries no traceIds. Only
    /// `field` is touched — a partial `$set`, so sibling synced fields
    /// (which a local change may not write) are left alone.
    pub fn set_local_field(
        &self,
        space_id: &str,
        object_id: &str,
        dataset: &str,
        record_id: &str,
        field: &str,
        value: &Value,
    ) -> Result<Value, AnyError> {
        self.modify(
            space_id,
            &json!({
                "objectId": object_id, "dataset": dataset, "scope": "local",
                "records": [{"id": record_id,
                             "ops": [{"type": "$set", "path": field, "value": value}]}]}),
        )
    }

    // --- editor markdown (content, NOT markdown — wire landmine) ---
    pub fn get_markdown(&self, space_id: &str, object_id: &str) -> Result<String, AnyError> {
        let reply = self.call(
            "GET",
            &format!("/v1/spaces/{space_id}/objects/{object_id}/editor/markdown"),
            None,
        )?;
        Ok(reply["content"].as_str().unwrap_or("").to_string())
    }

    pub fn put_markdown(
        &self,
        space_id: &str,
        object_id: &str,
        content: &str,
    ) -> Result<Value, AnyError> {
        self.call(
            "PUT",
            &format!("/v1/spaces/{space_id}/objects/{object_id}/editor/markdown"),
            Some(&json!({"content": content})),
        )
    }

    // --- types & properties (catalog source) ---
    pub fn list_types(&self, space_id: &str) -> Result<Vec<Value>, AnyError> {
        Ok(records_of(
            self.call("GET", &format!("/v1/spaces/{space_id}/types"), None)?,
            "types",
        ))
    }

    /// [{id, name, xKey, kind}] — the xKey↔propId catalog map.
    pub fn list_properties(&self, space_id: &str, type_id: &str) -> Result<Vec<Value>, AnyError> {
        let reply = self.call(
            "GET",
            &format!("/v1/spaces/{space_id}/types/{type_id}/properties"),
            None,
        )?;
        Ok(reply
            .get("properties")
            .and_then(Value::as_array)
            .or_else(|| reply.as_array())
            .cloned()
            .unwrap_or_default())
    }

    pub fn create_type(&self, space_id: &str, body: &Value) -> Result<Value, AnyError> {
        self.call("POST", &format!("/v1/spaces/{space_id}/types"), Some(body))
    }

    pub fn add_property(
        &self,
        space_id: &str,
        type_id: &str,
        body: &Value,
    ) -> Result<Value, AnyError> {
        self.call(
            "POST",
            &format!("/v1/spaces/{space_id}/types/{type_id}/properties"),
            Some(body),
        )
    }

    pub fn set_properties(
        &self,
        space_id: &str,
        object_id: &str,
        type_id: &str,
        patch: &Value,
    ) -> Result<Value, AnyError> {
        self.call(
            "POST",
            &format!("/v1/spaces/{space_id}/properties/{object_id}/set/{type_id}"),
            Some(&json!({"patch": patch})),
        )
    }

    // --- agent turns / chunks (v2, server-assigned seq) ---
    pub fn append_turn(
        &self,
        space_id: &str,
        object_id: &str,
        body: &Value,
    ) -> Result<Value, AnyError> {
        self.call(
            "POST",
            &format!("/v1/spaces/{space_id}/objects/{object_id}/agent/turns"),
            Some(body),
        )
    }

    pub fn create_chunk(
        &self,
        space_id: &str,
        object_id: &str,
        body: &Value,
    ) -> Result<Value, AnyError> {
        self.call(
            "POST",
            &format!("/v1/spaces/{space_id}/objects/{object_id}/agent/chunks"),
            Some(body),
        )
    }

    // --- chat messages ---
    pub fn chat_send(
        &self,
        space_id: &str,
        object_id: &str,
        body: &Value,
    ) -> Result<Value, AnyError> {
        self.call(
            "POST",
            &format!("/v1/spaces/{space_id}/objects/{object_id}/chat/messages"),
            Some(body),
        )
    }

    // --- search ---
    /// `opts`: JSON object — scopes/limit/mode.
    pub fn search(&self, space_id: &str, query: &str, opts: &Value) -> Result<Value, AnyError> {
        let body = merged(&[("query", json!(query))], opts);
        self.call(
            "POST",
            &format!("/v1/spaces/{space_id}/search"),
            Some(&body),
        )
    }

    /// Objects that reference object_id through a links-format property.
    /// Returns the `backlinks` list unwrapped from the envelope — each
    /// `{objectId, typeId, propId}` (never null).
    pub fn backlinks(&self, space_id: &str, object_id: &str) -> Result<Vec<Value>, AnyError> {
        let reply = self.call(
            "GET",
            &format!("/v1/spaces/{space_id}/objects/{object_id}/backlinks"),
            None,
        )?;
        Ok(records_of(reply, "backlinks"))
    }

    // --- agent memory (M5 write path; reads go through /query on the brain) ---
    /// The derived per-space brain object id hosting agent_memory_items.
    /// `{objectId}` — deterministic, no create race.
    pub fn get_brain(&self, space_id: &str) -> Result<Value, AnyError> {
        self.call("GET", &format!("/v1/spaces/{space_id}/agent/brain"), None)
    }

    /// Create a memory item (category + context required). Server
    /// resolves the brain object. Returns ModifyResult — recordIds[0]
    /// is the item id.
    pub fn create_memory(&self, space_id: &str, fields: &Value) -> Result<Value, AnyError> {
        self.call(
            "POST",
            &format!("/v1/spaces/{space_id}/agent/memory"),
            Some(fields),
        )
    }

    /// Evolve a memory item's mutable fields (author only; modifiedAt
    /// bumped server-side). accessCount bump on recall rides this path.
    pub fn evolve_memory(
        &self,
        space_id: &str,
        item_id: &str,
        fields: &Value,
    ) -> Result<Value, AnyError> {
        self.call(
            "PATCH",
            &format!("/v1/spaces/{space_id}/agent/memory/{item_id}"),
            Some(fields),
        )
    }

    pub fn delete_memory(&self, space_id: &str, item_id: &str) -> Result<Value, AnyError> {
        self.call(
            "DELETE",
            &format!("/v1/spaces/{space_id}/agent/memory/{item_id}"),
            None,
        )
    }

    // --- SSE subscribe (windowed query/subscribe primitive) ---
    /// Open an SSE stream over a `/query/subscribe`-shaped route and
    /// yield parsed frames in order: `ready` → `snapshot` → `changes`*
    /// → `closed` (docs/04-events.md). Terminal on `closed`.
    pub fn subscribe(
        &self,
        path: &str,
        body: Option<&Value>,
    ) -> Result<impl Iterator<Item = Frame>, AnyError> {
        let sanitized = body.map(sanitize_nuls);
        let lines = self.transport.open_stream(path, sanitized.as_ref())?;
        Ok(parse_sse(lines))
    }

    /// Subscribe over one object's dataset (POST
    /// /v1/spaces/:id/query/subscribe). `opts`: filter/sort/limit/…
    pub fn subscribe_dataset(
        &self,
        space_id: &str,
        object_id: &str,
        dataset: &str,
        opts: &Value,
    ) -> Result<impl Iterator<Item = Frame>, AnyError> {
        let body = merged(
            &[("objectId", json!(object_id)), ("dataset", json!(dataset))],
            opts,
        );
        self.subscribe(
            &format!("/v1/spaces/{space_id}/query/subscribe"),
            Some(&body),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::StubTransport;

    fn lines(v: &[&str]) -> impl Iterator<Item = String> {
        v.iter()
            .map(|s| s.to_string())
            .collect::<Vec<_>>()
            .into_iter()
    }

    // --- sanitize_nuls ---

    #[test]
    fn sanitize_nuls_replaces_in_nested_values() {
        let v = json!({"a": "x\u{0}y", "b": ["\u{0}", 3, {"c": "ok"}], "d": null});
        assert_eq!(
            sanitize_nuls(&v),
            json!({"a": "x\u{FFFD}y", "b": ["\u{FFFD}", 3, {"c": "ok"}], "d": null})
        );
    }

    #[test]
    fn sanitize_nuls_leaves_clean_values_alone() {
        let v = json!({"a": "clean", "n": 1.5, "t": true});
        assert_eq!(sanitize_nuls(&v), v);
    }

    // --- SSE parser (the _parse_sse contract) ---

    #[test]
    fn sse_basic_event_and_json_data() {
        let frames: Vec<Frame> =
            parse_sse(lines(&["event: ready", "data: {\"n\": 1}", ""])).collect();
        assert_eq!(
            frames,
            [Frame {
                event: "ready".into(),
                data: json!({"n": 1})
            }]
        );
    }

    #[test]
    fn sse_default_event_is_message() {
        let frames: Vec<Frame> = parse_sse(lines(&["data: 42", ""])).collect();
        assert_eq!(
            frames,
            [Frame {
                event: "message".into(),
                data: json!(42)
            }]
        );
    }

    #[test]
    fn sse_multiline_data_joined_with_newline() {
        let frames: Vec<Frame> = parse_sse(lines(&[
            "event: snapshot",
            "data: line one",
            "data: line two",
            "",
        ]))
        .collect();
        // not JSON → raw-string fallback, lines joined with \n
        assert_eq!(
            frames,
            [Frame {
                event: "snapshot".into(),
                data: json!("line one\nline two")
            }]
        );
    }

    #[test]
    fn sse_comments_and_heartbeats_skipped() {
        let frames: Vec<Frame> = parse_sse(lines(&[
            ": heartbeat",
            "data: 1",
            ": mid-frame comment",
            "data: 2",
            "",
        ]))
        .collect();
        assert_eq!(frames[0].data, json!("1\n2"));
    }

    #[test]
    fn sse_blank_line_without_data_resets_event() {
        let frames: Vec<Frame> = parse_sse(lines(&["event: ready", "", "data: {}", ""])).collect();
        // the ready frame had no data → dropped; next frame is back to "message"
        assert_eq!(
            frames,
            [Frame {
                event: "message".into(),
                data: json!({})
            }]
        );
    }

    #[test]
    fn sse_trailing_frame_without_blank_line_is_dropped() {
        let frames: Vec<Frame> = parse_sse(lines(&["event: changes", "data: [1]"])).collect();
        assert!(frames.is_empty());
    }

    #[test]
    fn sse_value_space_stripped_once_and_unknown_fields_ignored() {
        let frames: Vec<Frame> = parse_sse(lines(&[
            "id: 7",         // unknown field → ignored
            "data:  padded", // only ONE leading space stripped
            "retry: 100",    // ignored
            "",
        ]))
        .collect();
        assert_eq!(frames[0].data, json!(" padded"));
    }

    #[test]
    fn sse_multiple_frames_in_order() {
        let frames: Vec<Frame> = parse_sse(lines(&[
            "event: ready",
            "data: {\"sub\": \"s1\"}",
            "",
            "data: {\"rows\": []}",
            "",
            "event: closed",
            "data: {}",
            "",
        ]))
        .collect();
        let events: Vec<&str> = frames.iter().map(|f| f.event.as_str()).collect();
        assert_eq!(events, ["ready", "message", "closed"]);
    }

    // --- wire shapes through a stub transport ---

    fn stub_client() -> (Client, crate::testutil::CallLog) {
        let stub = StubTransport::new();
        let log = stub.log();
        (Client::with_transport(Box::new(stub)), log)
    }

    fn stub_client_with(replies: &[(u16, Value)]) -> (Client, crate::testutil::CallLog) {
        let stub = StubTransport::new();
        for (s, v) in replies {
            stub.push(*s, v.clone());
        }
        let log = stub.log();
        (Client::with_transport(Box::new(stub)), log)
    }

    #[test]
    fn call_maps_error_envelope() {
        let (c, _) = stub_client_with(&[(
            404,
            json!({"error": {"code": "not_found", "message": "no such space"}}),
        )]);
        let err = c.get_brain("sp").unwrap_err();
        assert_eq!(
            err,
            AnyError {
                status: 404,
                code: "not_found".into(),
                message: "no such space".into()
            }
        );
        assert_eq!(err.to_string(), "404 not_found: no such space");
    }

    #[test]
    fn call_maps_unknown_error_shape() {
        let (c, _) = stub_client_with(&[(500, json!({}))]);
        let err = c.list_types("sp").unwrap_err();
        assert_eq!(err.code, "unknown");
        assert_eq!(err.message, "");
    }

    #[test]
    fn query_wire_shape_merges_opts() {
        let (c, log) = stub_client_with(&[(200, json!({"records": [{"id": "r1"}]}))]);
        let recs = c
            .query(
                "sp",
                "obj",
                "chat_messages",
                &json!({"limit": 5, "sort": ["-createdAt"]}),
            )
            .unwrap();
        assert_eq!(recs, vec![json!({"id": "r1"})]);
        let calls = log.lock().unwrap();
        assert_eq!(calls[0].0, "POST");
        assert_eq!(calls[0].1, "/v1/spaces/sp/query");
        assert_eq!(
            calls[0].2,
            Some(json!({"objectId": "obj", "dataset": "chat_messages",
                        "limit": 5, "sort": ["-createdAt"]}))
        );
    }

    #[test]
    fn upsert_record_wire_shape() {
        let (c, log) = stub_client();
        c.upsert_record(
            "sp",
            "obj",
            "agent_triggers",
            "t1",
            &json!({"kind": "cron"}),
        )
        .unwrap();
        let calls = log.lock().unwrap();
        assert_eq!(calls[0].1, "/v1/spaces/sp/modify");
        assert_eq!(
            calls[0].2,
            Some(json!({"objectId": "obj", "dataset": "agent_triggers",
                        "records": [{"id": "t1", "upsert": true,
                                     "ops": [{"type": "$set", "path": "", "value": {"kind": "cron"}}]}]}))
        );
    }

    #[test]
    fn set_local_field_wire_shape() {
        let (c, log) = stub_client();
        c.set_local_field(
            "sp",
            "obj",
            "agent_config",
            "llm.key.anthropic",
            "localValue",
            &json!("sk-secret"),
        )
        .unwrap();
        let calls = log.lock().unwrap();
        assert_eq!(calls[0].1, "/v1/spaces/sp/modify");
        assert_eq!(
            calls[0].2,
            // scope=local, explicit id, NO upsert, partial $set on the one field.
            Some(
                json!({"objectId": "obj", "dataset": "agent_config", "scope": "local",
                        "records": [{"id": "llm.key.anthropic",
                                     "ops": [{"type": "$set", "path": "localValue", "value": "sk-secret"}]}]})
            )
        );
    }

    #[test]
    fn attach_file_wire_shape_percent_encodes_name() {
        let (c, log) = stub_client();
        c.attach_file("sp", "obj", "kernel v1.wasm", b"wasm-bytes")
            .unwrap();
        let calls = log.lock().unwrap();
        assert_eq!(calls[0].0, "POST");
        assert_eq!(
            calls[0].1,
            "/v1/spaces/sp/objects/obj/files?name=kernel%20v1.wasm"
        );
        assert_eq!(calls[0].2, Some(json!({"rawBytes": 10})));
    }

    #[test]
    fn download_file_wire_shape_returns_bytes() {
        let stub = crate::testutil::StubTransport::new();
        stub.push_raw(b"the-bytes".to_vec());
        let log = stub.log();
        let c = Client::with_transport(Box::new(stub));
        let bytes = c.download_file("sp", "file1").unwrap();
        assert_eq!(bytes, b"the-bytes");
        assert_eq!(
            log.lock().unwrap()[0].1,
            "/v1/spaces/sp/files/file1/content"
        );
    }

    #[test]
    fn fake_space_file_round_trip() {
        let c = Client::with_transport(Box::new(crate::testutil::FakeSpace::new()));
        let info = c
            .attach_file("sp", "obj1", "kernel.wasm", b"abc123")
            .unwrap();
        let fid = info["fileId"].as_str().unwrap();
        assert_eq!(info["size"], json!(6));
        assert_eq!(c.download_file("sp", fid).unwrap(), b"abc123");
        let meta = c.file_info("sp", fid).unwrap();
        assert_eq!(meta["name"], json!("kernel.wasm"));
        assert_eq!(meta["objectId"], json!("obj1"));
        // unknown file id is a 404, not empty bytes
        assert!(c.download_file("sp", "nope").is_err());
    }

    #[test]
    fn write_bodies_are_nul_sanitized() {
        let (c, log) = stub_client();
        c.chat_send("sp", "chat", &json!({"text": "bad\u{0}byte"}))
            .unwrap();
        assert_eq!(
            log.lock().unwrap()[0].2,
            Some(json!({"text": "bad\u{FFFD}byte"}))
        );
    }

    #[test]
    fn markdown_paths_and_content_key() {
        let (c, log) = stub_client_with(&[(200, json!({"content": "# hi"}))]);
        assert_eq!(c.get_markdown("sp", "o").unwrap(), "# hi");
        c.put_markdown("sp", "o", "body").unwrap();
        let calls = log.lock().unwrap();
        assert_eq!(
            calls[0],
            (
                "GET".into(),
                "/v1/spaces/sp/objects/o/editor/markdown".into(),
                None
            )
        );
        assert_eq!(
            calls[1],
            (
                "PUT".into(),
                "/v1/spaces/sp/objects/o/editor/markdown".into(),
                Some(json!({"content": "body"}))
            )
        );
    }

    #[test]
    fn memory_and_misc_paths() {
        let (c, log) = stub_client();
        c.create_memory("sp", &json!({"category": "c", "context": "x"}))
            .unwrap();
        c.evolve_memory("sp", "m1", &json!({"context": "y"}))
            .unwrap();
        c.delete_memory("sp", "m1").unwrap();
        c.append_turn("sp", "o", &json!({"role": "user"})).unwrap();
        c.create_chunk("sp", "o", &json!({"text": "t"})).unwrap();
        c.create_space("bao").unwrap();
        c.list_spaces(Some("active")).unwrap();
        c.search("sp", "q", &json!({"limit": 3})).unwrap();
        c.backlinks("sp", "o").unwrap();
        let calls = log.lock().unwrap();
        let paths: Vec<(&str, &str)> = calls
            .iter()
            .map(|(m, p, _)| (m.as_str(), p.as_str()))
            .collect();
        assert_eq!(
            paths,
            [
                ("POST", "/v1/spaces/sp/agent/memory"),
                ("PATCH", "/v1/spaces/sp/agent/memory/m1"),
                ("DELETE", "/v1/spaces/sp/agent/memory/m1"),
                ("POST", "/v1/spaces/sp/objects/o/agent/turns"),
                ("POST", "/v1/spaces/sp/objects/o/agent/chunks"),
                ("POST", "/v1/spaces"),
                ("GET", "/v1/spaces?status=active"),
                ("POST", "/v1/spaces/sp/search"),
                ("GET", "/v1/spaces/sp/objects/o/backlinks"),
            ]
        );
        assert_eq!(
            calls[5].2,
            Some(json!({"name": "bao", "spaceType": "anytype.space", "agent_space": true}))
        );
        assert_eq!(calls[7].2, Some(json!({"query": "q", "limit": 3})));
    }

    #[test]
    fn list_properties_unwraps_envelope_or_bare_list() {
        let (c, _) = stub_client_with(&[
            (200, json!({"properties": [{"id": "p1"}]})),
            (200, json!([{"id": "p2"}])),
        ]);
        assert_eq!(
            c.list_properties("sp", "t").unwrap(),
            vec![json!({"id": "p1"})]
        );
        assert_eq!(
            c.list_properties("sp", "t").unwrap(),
            vec![json!({"id": "p2"})]
        );
    }

    #[test]
    fn subscribe_dataset_posts_subscribe_route_and_parses_frames() {
        let stub = StubTransport::new();
        let log = stub.log();
        stub.set_stream(&[
            "event: ready",
            "data: {}",
            "",
            "event: changes",
            "data: [{\"added\": []}]",
            "",
        ]);
        let c = Client::with_transport(Box::new(stub));
        let frames: Vec<Frame> = c
            .subscribe_dataset("sp", "chat", "chat_messages", &json!({"limit": 64}))
            .unwrap()
            .collect();
        assert_eq!(frames.len(), 2);
        assert_eq!(frames[0].event, "ready");
        assert_eq!(frames[1].data, json!([{"added": []}]));
        let calls = log.lock().unwrap();
        assert_eq!(calls[0].1, "/v1/spaces/sp/query/subscribe");
        assert_eq!(
            calls[0].2,
            Some(json!({"objectId": "chat", "dataset": "chat_messages", "limit": 64}))
        );
    }
}
