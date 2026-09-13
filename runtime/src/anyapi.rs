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
use std::time::Duration;

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

/// Bounds on the one-shot calls (ADR-009 §8 q.3). The server is
/// local, so a connect that does not land in 10s is a port nobody
/// serves, and 5 min per request sits past every wait the server
/// itself keeps (the 30s registry convergence, the 2-min bundle
/// create): a hung call fails the boot step it belongs to instead of
/// hanging serve with nothing in the log (BOB-113). The SSE stream
/// keeps NO read bound — the server heartbeats, a dead peer FINs.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(300);

/// ureq-backed transport against a base URL (localhost `any` server).
pub struct HttpTransport {
    base: String,
    agent: ureq::Agent,
    request_timeout: Duration,
}

impl HttpTransport {
    pub fn new(base_url: &str) -> Self {
        Self::with_timeouts(base_url, CONNECT_TIMEOUT, REQUEST_TIMEOUT)
    }

    /// `connect` bounds every dial; `request` bounds each one-shot
    /// call end to end (the stream is exempt).
    pub fn with_timeouts(base_url: &str, connect: Duration, request: Duration) -> Self {
        HttpTransport {
            base: base_url.trim_end_matches('/').to_string(),
            agent: ureq::AgentBuilder::new().timeout_connect(connect).build(),
            request_timeout: request,
        }
    }

    /// An unbounded request — the stream's; one-shot calls go through
    /// `bounded`.
    fn request(&self, method: &str, path: &str) -> ureq::Request {
        self.agent
            .request(method, &format!("{}{}", self.base, path))
            .set("Content-Type", "application/json")
    }

    /// A one-shot request under `REQUEST_TIMEOUT`.
    fn bounded(&self, method: &str, path: &str) -> ureq::Request {
        self.request(method, path).timeout(self.request_timeout)
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
    // read the body ourselves: ureq's `into_string` refuses anything
    // over 10 MB, and a trace-store page of a long run (blob rows up
    // to 1 MiB each, ADR-024 §1) legitimately is — the server is
    // local and the sizes are bounded by what this runtime wrote
    let mut raw = String::new();
    resp.into_reader()
        .read_to_string(&mut raw)
        .map_err(transport_err)?;
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
        let resp = Self::dispatch(self.bounded(method, path), body)?;
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
        let req = self.bounded(method, path).set("Content-Type", content_type);
        let resp = match req.send_bytes(body) {
            Ok(r) => r,
            Err(ureq::Error::Status(_, r)) => r, // error envelope is data
            Err(e) => return Err(transport_err(e)),
        };
        json_response(resp)
    }

    fn read_raw(&self, path: &str) -> Result<Vec<u8>, AnyError> {
        let resp = Self::dispatch(self.bounded("GET", path), None)?;
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

fn ids_of(reply: Value) -> Vec<String> {
    records_of(reply, "ids")
        .into_iter()
        .filter_map(|v| v.as_str().map(str::to_string))
        .collect()
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

/// The editor module's canonical collection — the shared body an object
/// holds while it carries `page` or a type with a shared editor part.
pub const EDITOR_BLOCKS: &str = "editor_blocks";

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

    /// GET /v1/spaces/{spaceId} — the single-space handle. No chat id
    /// rides it: the general chat is the catalog's (`catalog_setup`,
    /// ADR-027 §1). A deleted space still answers 200 with
    /// `status: "deleted"` — branch on the status, never on the code.
    pub fn get_space(&self, space_id: &str) -> Result<Value, AnyError> {
        self.call("GET", &format!("/v1/spaces/{space_id}"), None)
    }

    /// POST /v1/spaces — the `ensure_space` create half (ADR-006 §0),
    /// non-registry names only. Reply carries the new space `id`.
    /// Store provisioning is ADR-017's job (bundle children at serve
    /// boot) — there is no server-side flag. `spaceType` is
    /// deliberately OMITTED: empty means the server's canonical
    /// default on every vintage, while the literal "anytype.space" is
    /// REJECTED since SDK v0.0.10 (renamed to "any.space" — sending
    /// either string ties us to one side).
    pub fn create_space(&self, name: &str) -> Result<Value, AnyError> {
        self.call("POST", "/v1/spaces", Some(&json!({"name": name})))
    }

    /// DELETE /v1/spaces/:id — a space this account owns (409
    /// `space.derived_undeletable` on a derived one). Test cleanup.
    pub fn delete_space(&self, id: &str) -> Result<Value, AnyError> {
        self.call("DELETE", &format!("/v1/spaces/{id}"), None)
    }

    /// GET /v1/spaces/derived — the server's compiled-in derived-space
    /// registry (SYN-164), boot-resolved: one row per well-known name,
    /// `{name, spaceId, created, status?}`. Resolving never creates.
    /// 404 on pre-registry servers — callers treat that as "no
    /// registry", not a failure.
    pub fn list_derived_spaces(&self) -> Result<Vec<Value>, AnyError> {
        Ok(records_of(
            self.call("GET", "/v1/spaces/derived", None)?,
            "spaces",
        ))
    }

    /// POST /v1/spaces/derived/{name} — materialize a registry space
    /// (lazy + idempotent, 201 SpaceInfo). Only registry names exist
    /// (404 `space.derived_unknown` otherwise); derived spaces are
    /// permanent — DELETE refuses them. No `agent_space` flag needed:
    /// the config object derives idempotently on every single-space GET.
    pub fn create_derived_space(&self, name: &str) -> Result<Value, AnyError> {
        self.call("POST", &format!("/v1/spaces/derived/{name}"), None)
    }

    // --- catalog (ADR-027 §1) ---
    /// POST /v1/catalog/{usecase}/setup — adopt-or-install one of the
    /// server's well-known usecases into a space, dependencies first.
    /// Reply `{usecase, bundles: [{usecase, id, bundle: {id, rootId,
    /// roots, losers?, derived?}, installed, typeId?, properties?,
    /// miniapp?}]}`. Idempotent on every member and device; the
    /// server runs the registry-convergence wait itself, so 409
    /// `bundle.not_ready` is the only retryable answer.
    pub fn catalog_setup(&self, usecase: &str, space_id: &str) -> Result<Value, AnyError> {
        self.call(
            "POST",
            &format!("/v1/catalog/{usecase}/setup"),
            Some(&json!({"spaceId": space_id})),
        )
    }

    // --- bundles (SYN-163) ---
    /// POST /v1/spaces/{s}/bundles — adopt-or-install a client bundle:
    /// one root object registered under a permanent id in the space's
    /// bundles registry ("bao/v1" — the slash is part of the id, sent
    /// verbatim in bodies; ids under `system:` are the catalog's, 409
    /// `bundle.reserved`). With a winner already registered this is a
    /// local read (`installed: false`); otherwise the server mints the
    /// root with `root_types` attached and registers it in one change.
    /// Reply `{bundle: {id, rootId, roots, losers, derived},
    /// installed}`. `derived: true` installs on the root DERIVED from
    /// the bundle id — the same id on every device, computed offline,
    /// so the install can never fork; the price is permanence (a
    /// derived root is undeletable, so no uninstall). Without it the
    /// root is created fresh and `rootId` is provisional until the
    /// space syncs. 409 `bundle.not_ready` means a winner's tree hasn't
    /// landed on this device yet (retryable).
    pub fn ensure_bundle(
        &self,
        space_id: &str,
        id: &str,
        name: &str,
        root_types: &[&str],
        derived: bool,
    ) -> Result<Value, AnyError> {
        let mut body = json!({"id": id, "name": name, "rootTypes": root_types});
        if derived {
            body["derived"] = json!(true);
        }
        self.call(
            "POST",
            &format!("/v1/spaces/{space_id}/bundles"),
            Some(&body),
        )
    }

    /// GET /v1/spaces/{s}/bundles — the space's bundles registry, a
    /// LOCKED read: the reply comes back after the convergence wait,
    /// so `synced: true` + no row is a definitive miss while
    /// `synced: false` (cold device, wait expired) makes absence
    /// provisional — never install on it. Reply `{bundles: [{id, name,
    /// rootId, roots, losers, derived}], synced}`.
    pub fn list_bundles(&self, space_id: &str) -> Result<Value, AnyError> {
        self.call("GET", &format!("/v1/spaces/{space_id}/bundles"), None)
    }

    /// POST /v1/spaces/{s}/bundles/{id}/children — derive a setup
    /// object under the bundle's winner: deterministic per (space,
    /// root, seed), same id on every device, cascade-deleted with the
    /// root. `types` are type ids attached on first materialization
    /// (ignored after). Seeds are permanent. 409 `bundle.not_ready`
    /// until the winner's tree is local (retryable).
    pub fn bundle_child(
        &self,
        space_id: &str,
        bundle_id: &str,
        seed: &str,
        types: &[&str],
    ) -> Result<Value, AnyError> {
        let enc = bundle_id.replace('/', "%2F");
        let mut body = json!({"seed": seed});
        if !types.is_empty() {
            body["types"] = json!(types);
        }
        self.call(
            "POST",
            &format!("/v1/spaces/{space_id}/bundles/{enc}/children"),
            Some(&body),
        )
    }

    // --- sharing (ADR-009 §8) ---
    /// POST /v1/spaces/{s}/invites — mint the space's RequestToJoin
    /// invite. Reply `{inviteToken, spaceId}`. The token carries NO
    /// permission — the grant happens at `acl_accept`.
    pub fn create_invite(&self, space_id: &str) -> Result<Value, AnyError> {
        self.call("POST", &format!("/v1/spaces/{space_id}/invites"), None)
    }

    /// POST /v1/spaces/join — request membership with an invite token.
    /// Returns (status, SpaceInfo): 201 = joined, 202 = pending the
    /// owner's approval.
    pub fn join_space(
        &self,
        invite_token: &str,
        metadata: Option<&Value>,
    ) -> Result<(u16, Value), AnyError> {
        let mut body = json!({"inviteToken": invite_token});
        if let Some(m) = metadata {
            body["metadata"] = m.clone();
        }
        let (status, data) = self
            .transport
            .send("POST", "/v1/spaces/join", Some(&body))?;
        Ok((status, unwrap_envelope(status, data)?))
    }

    /// GET /v1/spaces/{s}/members/requests — pending join requests
    /// (`[{recordId, identity, name, ...}]`).
    pub fn join_requests(&self, space_id: &str) -> Result<Vec<Value>, AnyError> {
        Ok(records_of(
            self.call(
                "GET",
                &format!("/v1/spaces/{space_id}/members/requests"),
                None,
            )?,
            "requests",
        ))
    }

    /// POST /v1/spaces/{s}/acl/accept — approve one pending join
    /// request with a permission (`reader` = view-only, ADR-009 §8).
    pub fn acl_accept(
        &self,
        space_id: &str,
        request_record_id: &str,
        permission: &str,
    ) -> Result<(), AnyError> {
        self.call(
            "POST",
            &format!("/v1/spaces/{space_id}/acl/accept"),
            Some(&json!({"requestRecordId": request_record_id,
                         "permission": permission})),
        )?;
        Ok(())
    }

    /// GET /v1/spaces/{s}/members — the space's member list.
    pub fn members(&self, space_id: &str) -> Result<Vec<Value>, AnyError> {
        Ok(records_of(
            self.call("GET", &format!("/v1/spaces/{space_id}/members"), None)?,
            "members",
        ))
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

    /// Publish one event on the account-wide bus (any docs/21 —
    /// POST /v1/events): ephemeral, at-most-once, no replay. `sender`
    /// is server-stamped. A lost publish is a dropped beat by nature —
    /// callers treat failure as noise to log, never a run failure.
    pub fn publish_event(&self, body: &Value) -> Result<Value, AnyError> {
        self.call("POST", "/v1/events", Some(body))
    }

    // --- devices (tech-space registry, ADR-015 / SYN-165) ---
    /// GET /v1/devices — the account's device rows + the server-computed
    /// `active: {<slug>: <peerId>}` winner map (the ONE implementation
    /// of the election rule — never recompute it client-side).
    pub fn list_devices(&self) -> Result<Value, AnyError> {
        self.call("GET", "/v1/devices", None)
    }

    /// PUT /v1/devices/me — upsert the caller's own row (server stamps
    /// peerId/os/hostname; body carries only what the app owns).
    pub fn upsert_self_device(&self, body: &Value) -> Result<Value, AnyError> {
        self.call("PUT", "/v1/devices/me", Some(body))
    }

    /// POST /v1/devices/activate — claim the active slot for `app` on
    /// THIS device (no remote activation, SYN-165 v1).
    pub fn activate_device(&self, app: &str) -> Result<Value, AnyError> {
        self.call("POST", "/v1/devices/activate", Some(&json!({"app": app})))
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

    // --- editor markdown (content, NOT markdown — wire landmine) ---
    // The routes name the collection: the shared `editor_blocks` body
    // an object holds while it carries `page` or a type with a shared
    // editor part (ADR-027 §3). No write attaches a type: an object
    // without one answers 400 `dataset.not_declared`.
    pub fn get_markdown(&self, space_id: &str, object_id: &str) -> Result<String, AnyError> {
        let reply = self.call(
            "GET",
            &format!("/v1/spaces/{space_id}/objects/{object_id}/editor/{EDITOR_BLOCKS}/markdown"),
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
            &format!("/v1/spaces/{space_id}/objects/{object_id}/editor/{EDITOR_BLOCKS}/markdown"),
            Some(&json!({"content": content})),
        )
    }

    // --- types & properties (catalog source) ---
    /// GET /v1/spaces/{s}/types?includeHidden=true — every type,
    /// hidden ones included: the harness types are hidden (ADR-027
    /// §2) and the built-ins `page` / `miniapp` / `bin` / `dataview`
    /// are hidden by construction, so a listing that omits them would
    /// re-create what exists. Rows: `{id, xKey, name, hidden?,
    /// builtIn?, weight?, layout?}`.
    pub fn list_types(&self, space_id: &str) -> Result<Vec<Value>, AnyError> {
        Ok(records_of(
            self.call(
                "GET",
                &format!("/v1/spaces/{space_id}/types?includeHidden=true"),
                None,
            )?,
            "types",
        ))
    }

    /// PATCH /v1/spaces/{s}/types/{t} — the type's rendering slice:
    /// `hidden`, `weight`, `layout`, `meta`. 400 `type.registered` on
    /// a built-in.
    pub fn patch_type(
        &self,
        space_id: &str,
        type_id: &str,
        body: &Value,
    ) -> Result<Value, AnyError> {
        self.call(
            "PATCH",
            &format!("/v1/spaces/{space_id}/types/{type_id}"),
            Some(body),
        )
    }

    /// POST /v1/spaces/{s}/properties/{o}/attach/{t} — the object
    /// gains the type (idempotent). The one way an object comes to
    /// hold a type's collections after create.
    pub fn attach_type(
        &self,
        space_id: &str,
        object_id: &str,
        type_id: &str,
    ) -> Result<Value, AnyError> {
        self.call(
            "POST",
            &format!("/v1/spaces/{space_id}/properties/{object_id}/attach/{type_id}"),
            None,
        )
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

    // --- local store (any PR #195, docs/26-local-store.md; ADR-023) ---
    // Device-local any-store collections under /v1/local: never synced,
    // no DAG, same filter/sort/pipeline language as the synced data.
    // `coll` is the wire address `{scope, spaceId?, name}`.

    /// The wire address of a space-scoped local collection.
    pub fn local_coll(space_id: &str, name: &str) -> Value {
        json!({"scope": "space", "spaceId": space_id, "name": name})
    }

    /// PUT /v1/local/collections — idempotent create + index ensure.
    pub fn local_ensure(&self, coll: &Value, indexes: &[Value]) -> Result<Value, AnyError> {
        let body = merged(&[("indexes", json!(indexes))], coll);
        self.call("PUT", "/v1/local/collections", Some(&body))
    }

    /// GET /v1/local/collections?scope=&spaceId= — `[{scope, spaceId,
    /// name, storageName, count, indexes}]`.
    pub fn local_collections(
        &self,
        scope: Option<&str>,
        space_id: Option<&str>,
    ) -> Result<Vec<Value>, AnyError> {
        let mut q = Vec::new();
        if let Some(s) = scope {
            q.push(format!("scope={s}"));
        }
        if let Some(sp) = space_id {
            q.push(format!("spaceId={sp}"));
        }
        let path = if q.is_empty() {
            "/v1/local/collections".to_string()
        } else {
            format!("/v1/local/collections?{}", q.join("&"))
        };
        Ok(records_of(self.call("GET", &path, None)?, "collections"))
    }

    /// DELETE /v1/local/collections — drop (the cleanup path; nothing
    /// drops a space-scoped collection when its space goes away).
    pub fn local_drop(&self, coll: &Value) -> Result<Value, AnyError> {
        let space = coll["spaceId"].as_str().unwrap_or("");
        let path = format!(
            "/v1/local/collections?scope={}&spaceId={space}&name={}",
            coll["scope"].as_str().unwrap_or("space"),
            coll["name"].as_str().unwrap_or("")
        );
        self.call("DELETE", &path, None)
    }

    /// POST /v1/local/insert — ≤1000 docs, written 256 per tx; a
    /// missing `id` is minted. Returns the ids in request order.
    pub fn local_insert(&self, coll: &Value, docs: &[Value]) -> Result<Vec<String>, AnyError> {
        let reply = self.call(
            "POST",
            "/v1/local/insert",
            Some(&json!({"coll": coll, "docs": docs})),
        )?;
        Ok(ids_of(reply))
    }

    /// POST /v1/local/upsert — same shape as insert, replaces on id.
    pub fn local_upsert(&self, coll: &Value, docs: &[Value]) -> Result<Vec<String>, AnyError> {
        let reply = self.call(
            "POST",
            "/v1/local/upsert",
            Some(&json!({"coll": coll, "docs": docs})),
        )?;
        Ok(ids_of(reply))
    }

    /// POST /v1/local/update — `$set {flag: true}` on one document.
    pub fn local_update_flag(&self, coll: &Value, id: &str, flag: &str) -> Result<Value, AnyError> {
        self.call(
            "POST",
            "/v1/local/update",
            Some(&json!({"coll": coll, "id": id, "modifier": {"$set": {flag: true}}})),
        )
    }

    /// POST /v1/local/get — one document by id (404 local.doc_not_found).
    pub fn local_get(&self, coll: &Value, id: &str) -> Result<Value, AnyError> {
        let reply = self.call(
            "POST",
            "/v1/local/get",
            Some(&json!({"coll": coll, "id": id})),
        )?;
        Ok(reply.get("record").cloned().unwrap_or(Value::Null))
    }

    /// POST /v1/local/query — `opts`: filter/sort/limit/offset/
    /// includeTotal (limit default 100, cap 1000). Returns the whole
    /// reply `{records, total?, hasNext?}` so callers can page.
    pub fn local_query(&self, coll: &Value, opts: &Value) -> Result<Value, AnyError> {
        let body = merged(&[("coll", coll.clone())], opts);
        self.call("POST", "/v1/local/query", Some(&body))
    }

    /// POST /v1/local/aggregate — `{records}` | `{written}` | `{plan}`.
    pub fn local_aggregate(
        &self,
        coll: &Value,
        pipeline: &Value,
        opts: &Value,
    ) -> Result<Value, AnyError> {
        let body = merged(
            &[("coll", coll.clone()), ("pipeline", pipeline.clone())],
            opts,
        );
        self.call("POST", "/v1/local/aggregate", Some(&body))
    }

    /// POST /v1/local/delete — by ids, or by filter (collected under
    /// one read, removed 256 per tx — not atomic). Returns `deleted`.
    pub fn local_delete(
        &self,
        coll: &Value,
        ids: Option<&[String]>,
        filter: Option<&Value>,
    ) -> Result<u64, AnyError> {
        let mut body = json!({"coll": coll});
        if let Some(ids) = ids {
            body["ids"] = json!(ids);
        }
        if let Some(f) = filter {
            body["filter"] = f.clone();
        }
        let reply = self.call("POST", "/v1/local/delete", Some(&body))?;
        Ok(reply["deleted"].as_u64().unwrap_or(0))
    }

    pub fn create_type(&self, space_id: &str, body: &Value) -> Result<Value, AnyError> {
        self.call("POST", &format!("/v1/spaces/{space_id}/types"), Some(body))
    }

    /// GET /v1/spaces/{s}/types/{t}/datasets — the type's datasets,
    /// the flat compiled view: `[{id, key, collection, module, shared?,
    /// partId, idRule, deleteBy, search?, fields, invalid?}]`. The
    /// `collection` is the address every read and write carries; it is
    /// read here, never composed (ADR-027 §2).
    pub fn list_datasets(&self, space_id: &str, type_id: &str) -> Result<Vec<Value>, AnyError> {
        Ok(records_of(
            self.call(
                "GET",
                &format!("/v1/spaces/{space_id}/types/{type_id}/datasets"),
                None,
            )?,
            "datasets",
        ))
    }

    /// POST /v1/spaces/{s}/types/{t}/parts — declare a part and the
    /// datasets under it in one change: `{key, name?, pos?, ui?,
    /// datasets: [{key, module?, shared?, …records draft}]}` → `201
    /// {partId}`. A records dataset lands in `<typeId>_<key>`, a
    /// shared module dataset in the module's canonical collection.
    /// Behavioral parts (module, shared, idRule, deleteBy, fields) pin
    /// on first write; only the search.* leaves stay mutable (PATCH
    /// …/datasets/:defId). 409 `dataset.key_conflict` on a key the
    /// type already declares.
    pub fn add_part(
        &self,
        space_id: &str,
        type_id: &str,
        draft: &Value,
    ) -> Result<Value, AnyError> {
        self.call(
            "POST",
            &format!("/v1/spaces/{space_id}/types/{type_id}/parts"),
            Some(draft),
        )
    }

    /// Add one declared field to an existing dataset definition (the
    /// additive reconcile of a harness store, ADR-021 §2).
    pub fn add_dataset_field(
        &self,
        space_id: &str,
        type_id: &str,
        def_id: &str,
        field: &Value,
    ) -> Result<Value, AnyError> {
        self.call(
            "POST",
            &format!("/v1/spaces/{space_id}/types/{type_id}/datasets/{def_id}/fields"),
            Some(field),
        )
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

    /// Full property record of one object (`{"record": {...}}`;
    /// record is null for a property-less object).
    pub fn get_properties(&self, space_id: &str, object_id: &str) -> Result<Value, AnyError> {
        self.call(
            "GET",
            &format!("/v1/spaces/{space_id}/properties/{object_id}"),
            None,
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

    /// GET /v1/spaces/{s}/objects/{o}/backlinks — the link index's
    /// edges pointing at the object (`object`) and at its records or
    /// property values (`parts`): `{object: [edge], parts: [edge],
    /// truncated?}`, each edge `{source: {spaceId, objectId, dataset,
    /// recordId, typeId?, field?}, kind, target: {uri, …}}`. 409
    /// `index.disabled` when the search index is off.
    pub fn backlinks(&self, space_id: &str, object_id: &str) -> Result<Value, AnyError> {
        self.call(
            "GET",
            &format!("/v1/spaces/{space_id}/objects/{object_id}/backlinks"),
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
        let err = c.get_space("sp").unwrap_err();
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
    fn local_store_wire_shapes() {
        // ADR-023: the local-store client speaks docs/26-local-store.md
        let (c, log) = stub_client();
        let coll = Client::local_coll("sp", "trace_records");
        c.local_ensure(
            &coll,
            &[json!({"fields": ["runId", "seq"], "unique": true})],
        )
        .unwrap();
        c.local_insert(&coll, &[json!({"id": "r:1", "seq": 1})])
            .unwrap();
        c.local_query(
            &coll,
            &json!({"filter": {"runId": "r"}, "sort": ["seq"], "limit": 1000}),
        )
        .unwrap();
        c.local_aggregate(&coll, &json!([{"$match": {"runId": "r"}}]), &json!({}))
            .unwrap();
        c.local_delete(&coll, None, Some(&json!({"runId": "r"})))
            .unwrap();
        c.local_collections(Some("space"), Some("sp")).unwrap();
        let calls = log.lock().unwrap();
        assert_eq!(calls[0].0, "PUT");
        assert_eq!(calls[0].1, "/v1/local/collections");
        assert_eq!(
            calls[0].2,
            Some(
                json!({"scope": "space", "spaceId": "sp", "name": "trace_records",
                        "indexes": [{"fields": ["runId", "seq"], "unique": true}]})
            )
        );
        assert_eq!(calls[1].1, "/v1/local/insert");
        assert_eq!(
            calls[1].2,
            Some(json!({"coll": coll, "docs": [{"id": "r:1", "seq": 1}]}))
        );
        assert_eq!(calls[2].1, "/v1/local/query");
        assert_eq!(calls[2].2.as_ref().unwrap()["coll"], coll);
        assert_eq!(calls[2].2.as_ref().unwrap()["limit"], 1000);
        assert_eq!(calls[3].1, "/v1/local/aggregate");
        assert_eq!(calls[4].1, "/v1/local/delete");
        assert_eq!(
            calls[4].2,
            Some(json!({"coll": coll, "filter": {"runId": "r"}}))
        );
        assert_eq!(calls[5].0, "GET");
        assert_eq!(calls[5].1, "/v1/local/collections?scope=space&spaceId=sp");
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
    fn sharing_wire_shapes() {
        let (c, log) = stub_client();
        c.create_invite("sp").unwrap();
        c.join_space("tok123", Some(&json!({"name": "bao"})))
            .unwrap();
        c.join_requests("sp").unwrap();
        c.acl_accept("sp", "req1", "reader").unwrap();
        c.members("sp").unwrap();
        let calls = log.lock().unwrap();
        assert_eq!(calls[0].1, "/v1/spaces/sp/invites");
        assert_eq!(
            (calls[1].1.as_str(), calls[1].2.clone()),
            (
                "/v1/spaces/join",
                Some(json!({"inviteToken": "tok123", "metadata": {"name": "bao"}}))
            )
        );
        assert_eq!(calls[2].1, "/v1/spaces/sp/members/requests");
        assert_eq!(
            (calls[3].1.as_str(), calls[3].2.clone()),
            (
                "/v1/spaces/sp/acl/accept",
                Some(json!({"permission": "reader", "requestRecordId": "req1"}))
            )
        );
        assert_eq!(calls[4].1, "/v1/spaces/sp/members");
    }

    #[test]
    fn join_flow_round_trip_over_fake_space() {
        // publisher mints an invite; joiner requests; approval as
        // reader lands them active — the ADR-009 §8 handshake
        let c = Client::with_transport(Box::new(crate::testutil::FakeSpace::new()));
        let inv = c.create_invite("repo").unwrap();
        let token = inv["inviteToken"].as_str().unwrap();

        let (status, info) = c.join_space(token, Some(&json!({"name": "bao"}))).unwrap();
        assert_eq!(status, 202); // pending the owner's approval
        assert_eq!(info["status"], json!("joining"));

        let reqs = c.join_requests("repo").unwrap();
        assert_eq!(reqs.len(), 1);
        let rid = reqs[0]["recordId"].as_str().unwrap();

        c.acl_accept("repo", rid, "reader").unwrap();
        assert!(c.join_requests("repo").unwrap().is_empty());
        let members = c.members("repo").unwrap();
        assert_eq!(members[0]["permission"], json!("reader"));
        assert_eq!(members[0]["status"], json!("active"));

        // a bogus token is a 400, not a silent pend
        assert!(c.join_space("nope", None).is_err());
        // accepting a vanished request is a 404
        assert!(c.acl_accept("repo", rid, "reader").is_err());
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
        // the routes name the shared editor collection (ADR-027 §3)
        assert_eq!(
            calls[0],
            (
                "GET".into(),
                "/v1/spaces/sp/objects/o/editor/editor_blocks/markdown".into(),
                None
            )
        );
        assert_eq!(
            calls[1],
            (
                "PUT".into(),
                "/v1/spaces/sp/objects/o/editor/editor_blocks/markdown".into(),
                Some(json!({"content": "body"}))
            )
        );
    }

    #[test]
    fn catalog_types_and_attach_paths() {
        let (c, log) = stub_client();
        c.catalog_setup("general-chat", "sp").unwrap();
        c.list_types("sp").unwrap();
        c.patch_type("sp", "t1", &json!({"hidden": true})).unwrap();
        c.attach_type("sp", "o1", "page").unwrap();
        c.add_part(
            "sp",
            "t1",
            &json!({"key": "entries", "datasets": [{"key": "entries", "idRule": "user"}]}),
        )
        .unwrap();
        let calls = log.lock().unwrap();
        assert_eq!(
            calls[0],
            (
                "POST".into(),
                "/v1/catalog/general-chat/setup".into(),
                Some(json!({"spaceId": "sp"}))
            )
        );
        // hidden types (the harness's, the built-ins) must list
        assert_eq!(calls[1].1, "/v1/spaces/sp/types?includeHidden=true");
        assert_eq!(
            calls[2],
            (
                "PATCH".into(),
                "/v1/spaces/sp/types/t1".into(),
                Some(json!({"hidden": true}))
            )
        );
        assert_eq!(
            (calls[3].0.as_str(), calls[3].1.as_str()),
            ("POST", "/v1/spaces/sp/properties/o1/attach/page")
        );
        assert_eq!(calls[4].1, "/v1/spaces/sp/types/t1/parts");
    }

    #[test]
    fn memory_and_misc_paths() {
        let (c, log) = stub_client();
        c.create_space("bao").unwrap();
        c.list_derived_spaces().unwrap();
        c.create_derived_space("bao").unwrap();
        c.list_spaces(Some("active")).unwrap();
        c.search("sp", "q", &json!({"limit": 3})).unwrap();
        c.backlinks("sp", "o").unwrap();
        c.bundle_child("sp", "bao/v1", "bao/config/v1", &["t1"])
            .unwrap();
        c.add_part("sp", "t1", &json!({"key": "d", "datasets": [{"key": "d"}]}))
            .unwrap();
        c.list_datasets("sp", "t1").unwrap();
        let calls = log.lock().unwrap();
        let paths: Vec<(&str, &str)> = calls
            .iter()
            .map(|(m, p, _)| (m.as_str(), p.as_str()))
            .collect();
        assert_eq!(
            paths,
            [
                ("POST", "/v1/spaces"),
                ("GET", "/v1/spaces/derived"),
                ("POST", "/v1/spaces/derived/bao"),
                ("GET", "/v1/spaces?status=active"),
                ("POST", "/v1/spaces/sp/search"),
                ("GET", "/v1/spaces/sp/objects/o/backlinks"),
                ("POST", "/v1/spaces/sp/bundles/bao%2Fv1/children"),
                ("POST", "/v1/spaces/sp/types/t1/parts"),
                ("GET", "/v1/spaces/sp/types/t1/datasets"),
            ]
        );
        assert_eq!(
            calls[0].2,
            // no spaceType: empty = server default on every vintage
            // ("anytype.space" is rejected since SDK v0.0.10); no
            // agent flag — provisioning is ADR-017's bundle children
            Some(json!({"name": "bao"}))
        );
        assert_eq!(calls[4].2, Some(json!({"query": "q", "limit": 3})));
        assert_eq!(
            calls[6].2,
            Some(json!({"seed": "bao/config/v1", "types": ["t1"]}))
        );
    }

    #[test]
    fn ensure_bundle_posts_id_verbatim_in_body() {
        let (c, log) = stub_client();
        c.ensure_bundle("sp", "bao/v1", "bao", &["page"], false)
            .unwrap();
        c.ensure_bundle("sp", "x/v1", "X", &[], true).unwrap();
        let calls = log.lock().unwrap();
        assert_eq!(calls[0].0, "POST");
        assert_eq!(calls[0].1, "/v1/spaces/sp/bundles");
        // the slash is part of the id — encoded only in PATH segments,
        // verbatim in bodies; a created install carries no `derived`
        // key at all
        assert_eq!(
            calls[0].2,
            Some(json!({"id": "bao/v1", "name": "bao",
                        "rootTypes": ["page"]}))
        );
        assert_eq!(
            calls[1].2,
            Some(json!({"id": "x/v1", "name": "X", "rootTypes": [],
                        "derived": true}))
        );
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

    /// A one-shot call against a server that accepts and never answers
    /// fails within the request bound instead of hanging the caller
    /// (BOB-113): the boot step that made it can report and move on.
    #[test]
    fn http_transport_bounds_a_hung_call() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let (release_tx, release_rx) = std::sync::mpsc::channel::<()>();
        std::thread::spawn(move || {
            // hold the accepted socket open, silent, until the test ends
            let held = listener.accept().ok();
            let _ = release_rx.recv_timeout(Duration::from_secs(10));
            drop(held);
        });
        let transport = HttpTransport::with_timeouts(
            &format!("http://{addr}"),
            Duration::from_secs(2),
            Duration::from_millis(300),
        );
        let started = std::time::Instant::now();
        let err = transport
            .send("GET", "/v1/spaces", None)
            .expect_err("a silent server must not answer");
        let _ = release_tx.send(());
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "bounded call took {:?}: {err}",
            started.elapsed()
        );
        assert_eq!(err.code, "transport");
    }
}
