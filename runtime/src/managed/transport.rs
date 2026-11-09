//! The managed host client's transport gate (ADR-034 §5): an exact
//! method/segment/body allowlist per phase, named here in full.
//!
//! Every phase (the verification and resolver reads):
//! `GET /v1/spaces/{s}`, `GET /v1/spaces/{s}/bundles`,
//! `GET /v1/spaces/{s}/types?includeHidden=true`,
//! `GET /v1/spaces/{s}/types/{t}/{properties|datasets}`,
//! `GET /v1/spaces/{s}/objects/{o}/editor/editor_blocks/markdown`,
//! `POST /v1/spaces/{s}/query`, `POST /v1/spaces/{s}/objects/query`
//! for `s` in the working space and the configured overlays;
//! `PUT /v1/local/collections` for the three trace collections of the working
//! space (`trace_records`, `trace_blobs`, `trace_runs`); and
//! `POST /v1/local/{insert|upsert|update|get|query|aggregate|delete}` on those
//! three collections — exactly what `AnyTraceStore` issues (streamed records,
//! blobs, the summary, retention, the guest's `trace.*` reads) — an
//! `aggregate` with a sink stage refused. The device-local trace routes are
//! allowed in every phase, so retention sweeps on desktop's cadence in every
//! phase (user ruling 2026-11-06, N2b-2 design §10 R7).
//!
//! Execution phase adds, working space only: `POST /v1/spaces/{w}/modify` on
//! the config/triggers/runs fixture objects with their exact `dataset`, and on
//! the secrets object with descriptor and status-stamp fields only
//! (`$set`/`$unset` of `key`, `secret`, `status`, `label`, `help`, `note`,
//! `lastUsedAt`, `requestedBy`, `requestedIn`, `requestedAt`, `rejectedAt`,
//! `rejectedWith`, `updatedAt` — the exact fields the broker's miss/rejection
//! stamps and the takeover's reconcile write; never `value`, never `hosts`,
//! no other op, no dotted path);
//! `POST /v1/spaces/{w}/modify` on the setup-recorded `turns` log child with
//! its exact `dataset`, for one record only: the ADR-005 §3 interrupted turn
//! (`id` eight ASCII digits, `upsert: true`, one `$set` of path `""` whose
//! value has exactly the keys `seq`, `fromAgent`, `userText`, `replies`,
//! `interrupted`, `traceRef`, `searchText`, `llm`, with `replies: []`,
//! `interrupted: true` and `llm: {stopReason: break_hard}`; N2b-2 C2);
//! `POST /v1/spaces/{w}/objects/{chat}/chat/messages`; `POST /v1/events` with
//! `type: bao.status`, `scope: account` and `target` = the self peer;
//! and the one stream `POST /v1/spaces/{w}/query/subscribe` with `dataset` =
//! `chat_messages` and `objectId` an identifier of the working space — the
//! chat, or a chat object an own event trigger names (ADR-018 §2; N2b-2 C3) —
//! lines capped at `MAX_STREAM_LINE_BYTES` (an over-long line ends the stream
//! and counts a capacity fault).
//!
//! Registry, auth, account, provisioning, type/property mutation, bundle
//! children, local export, foreign spaces and secret VALUE writes are denied in
//! every phase. Paths are matched as whole segments after rejecting `.`, `..`,
//! empty, percent-encoded, backslash, fragment and control/non-printable
//! characters, so a spliced id cannot escape a template (N1-R7, N1-I1).
//! Progress is published into the calling thread's own task slot (N1-R6).
use super::{Shared, TaskPhase, WaitClass};
use crate::anyapi::{AnyError, Transport};
use crate::managed::admission::MAX_STREAM_LINE_BYTES;
use serde_json::Value;
use std::collections::BTreeSet;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Arc, Weak};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Phase {
    Preparation,
    Execution,
}

/// The phase switch the owned service flips at takeover and stand-down.
#[derive(Clone, Default)]
pub(crate) struct PhaseGate(Arc<AtomicU8>);
impl PhaseGate {
    #[allow(dead_code)] // flipped by the command consumer at takeover/stand-down (N2b)
    pub(crate) fn set(&self, phase: Phase) {
        self.0.store(phase as u8, Ordering::Release);
    }
    pub(crate) fn get(&self) -> Phase {
        match self.0.load(Ordering::Acquire) {
            1 => Phase::Execution,
            _ => Phase::Preparation,
        }
    }
}

/// The working-space store objects (with their exact collections), the chat
/// and the self peer the fixture names.
#[derive(Clone)]
pub(crate) struct FixtureObjects {
    pub(crate) config: (String, String),
    pub(crate) secrets: (String, String),
    pub(crate) triggers: (String, String),
    pub(crate) runs: (String, String),
    /// The general chat's `bao/log/v1` child and its `agent_turns` collection.
    pub(crate) turns: (String, String),
    pub(crate) chat: String,
    pub(crate) self_peer: String,
}

const TRACE_COLLECTIONS: [&str; 3] = ["trace_records", "trace_blobs", "trace_runs"];
/// The keys of the interrupted turn record (`chatwatch::interrupted_turn_record`).
const INTERRUPTED_TURN_KEYS: [&str; 8] = [
    "seq",
    "fromAgent",
    "userText",
    "replies",
    "interrupted",
    "traceRef",
    "searchText",
    "llm",
];
/// The secrets-row fields the managed host may write (ADR-034 §5): the
/// descriptor minus `hosts`, plus the status stamps `ServeSecretStore` makes
/// (`mark_missing`, `mark_rejected`, `mark_used`) and the takeover reconcile.
/// `value` and `hosts` are absent by design (ADR-021 §4/§7).
const DESCRIPTOR_FIELDS: [&str; 13] = [
    "key",
    "secret",
    "status",
    "label",
    "help",
    "note",
    "lastUsedAt",
    "requestedBy",
    "requestedIn",
    "requestedAt",
    "rejectedAt",
    "rejectedWith",
    "updatedAt",
];

pub(crate) struct GatedTransport {
    inner: Box<dyn Transport + Send + Sync>,
    shared: Weak<Shared>,
    spaces: BTreeSet<String>,
    working: String,
    objects: FixtureObjects,
    gate: PhaseGate,
}

fn denied(code: &str) -> AnyError {
    AnyError {
        status: 0,
        code: code.into(),
        message: "managed transport: route not permitted in this phase".into(),
    }
}

/// The gate refused the request before it was sent for a reason that
/// passes — Preparation's phase, or the owner's cancel — as opposed to a
/// failure of a request that went out, or a route never permitted
/// (`execution_route_denied`), which retrying cannot cure.
pub(crate) fn refused_by_gate(e: &AnyError) -> bool {
    e.status == 0 && matches!(e.code.as_str(), "preparation_write_denied" | "cancelled")
}

/// An Any id as a path segment: `<cid>`, `<cid>.<suffix>`, a hyphenated name.
/// Dots are allowed inside, never as a whole dot segment, never doubled.
pub(crate) fn identifier(segment: &str) -> bool {
    !segment.is_empty()
        && segment.len() <= 256
        && !segment.starts_with('.')
        && !segment.ends_with('.')
        && !segment.contains("..")
        && segment
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
}

/// Whole, canonical segments of `path` (query split off): None when any
/// segment is empty, a dot segment, percent-encoded, or carries a backslash,
/// fragment, control or non-printable byte (anything the `url` crate would
/// strip or re-encode).
pub(crate) fn segments(path: &str) -> Option<(Vec<&str>, Option<&str>)> {
    if path.bytes().any(|b| !(0x21..=0x7e).contains(&b)) {
        return None;
    }
    let (path, query) = match path.split_once('?') {
        Some((p, q)) => (p, Some(q)),
        None => (path, None),
    };
    let rest = path.strip_prefix('/')?;
    let parts: Vec<&str> = rest.split('/').collect();
    if parts.iter().any(|s| {
        s.is_empty()
            || *s == "."
            || *s == ".."
            || s.contains('%')
            || s.contains('\\')
            || s.contains('#')
    }) {
        return None;
    }
    Some((parts, query))
}

fn local_collection(body: Option<&Value>, key: &str, working: &str) -> bool {
    body.is_some_and(|body| {
        let coll = &body[key];
        coll["scope"] == "space"
            && coll["spaceId"] == working
            && coll["name"]
                .as_str()
                .is_some_and(|name| TRACE_COLLECTIONS.contains(&name))
    })
}

impl GatedTransport {
    pub(crate) fn new(
        inner: Box<dyn Transport + Send + Sync>,
        shared: Weak<Shared>,
        spaces: BTreeSet<String>,
        working: String,
        objects: FixtureObjects,
        gate: PhaseGate,
    ) -> Self {
        GatedTransport {
            inner,
            shared,
            spaces,
            working,
            objects,
            gate,
        }
    }

    fn space(&self, segment: &str) -> bool {
        self.spaces.contains(segment)
    }

    /// Reads permitted in every phase (the verification and resolver reads).
    fn read_allowed(&self, method: &str, parts: &[&str], query: Option<&str>) -> bool {
        match (method, parts) {
            ("GET", ["v1", "spaces", s]) => self.space(s) && query.is_none(),
            ("GET", ["v1", "spaces", s, "bundles"]) => self.space(s) && query.is_none(),
            ("GET", ["v1", "spaces", s, "types"]) => {
                self.space(s) && query == Some("includeHidden=true")
            }
            ("GET", ["v1", "spaces", s, "types", t, "properties" | "datasets"]) => {
                self.space(s) && identifier(t) && query.is_none()
            }
            ("GET", ["v1", "spaces", s, "objects", o, "editor", coll, "markdown"]) => {
                self.space(s)
                    && identifier(o)
                    && *coll == crate::anyapi::EDITOR_BLOCKS
                    && query.is_none()
            }
            ("POST", ["v1", "spaces", s, "query"])
            | ("POST", ["v1", "spaces", s, "objects", "query"]) => self.space(s) && query.is_none(),
            _ => false,
        }
    }

    fn trace_create_allowed(&self, method: &str, parts: &[&str], body: Option<&Value>) -> bool {
        method == "PUT"
            && parts == ["v1", "local", "collections"]
            && body.is_some_and(|body| {
                body["scope"] == "space"
                    && body["spaceId"] == self.working
                    && body["name"]
                        .as_str()
                        .is_some_and(|name| TRACE_COLLECTIONS.contains(&name))
            })
    }

    /// The trace store's own routes (ADR-023 §3/§5/§6), on its three
    /// collections, in every phase (R7).
    fn trace_allowed(
        &self,
        method: &str,
        parts: &[&str],
        query: Option<&str>,
        body: Option<&Value>,
    ) -> bool {
        match (method, parts) {
            (
                "POST",
                ["v1", "local", "insert" | "upsert" | "update" | "get" | "query" | "delete"],
            ) => query.is_none() && local_collection(body, "coll", &self.working),
            ("POST", ["v1", "local", "aggregate"]) => {
                query.is_none()
                    && local_collection(body, "coll", &self.working)
                    && body.is_some_and(|b| {
                        crate::tracestore::find_sink_stage(&b["pipeline"]).is_none()
                    })
            }
            _ => false,
        }
    }

    /// A `modify` on the secrets object may touch descriptor fields only.
    fn descriptor_only(body: &Value) -> bool {
        let Some(records) = body["records"].as_array() else {
            return false;
        };
        !records.is_empty()
            && records.iter().all(|record| {
                record["id"].as_str().is_some_and(|id| !id.is_empty())
                    && record["upsert"] == true
                    && record["ops"].as_array().is_some_and(|ops| {
                        ops.iter().all(|op| {
                            let path = op["path"].as_str().unwrap_or("");
                            matches!(op["type"].as_str(), Some("$set" | "$unset"))
                                && DESCRIPTOR_FIELDS.contains(&path)
                        })
                    })
            })
    }

    /// A `modify` on the `turns` log child: exactly one interrupted turn.
    fn interrupted_turn_only(body: &Value) -> bool {
        let Some([record]) = body["records"].as_array().map(Vec::as_slice) else {
            return false;
        };
        let Some([op]) = record["ops"].as_array().map(Vec::as_slice) else {
            return false;
        };
        let value = &op["value"];
        let keys: BTreeSet<&str> = value
            .as_object()
            .map(|m| m.keys().map(String::as_str).collect())
            .unwrap_or_default();
        record["id"]
            .as_str()
            .is_some_and(|id| id.len() == 8 && id.bytes().all(|b| b.is_ascii_digit()))
            && record["upsert"] == true
            && op["type"] == "$set"
            && op["path"] == ""
            && keys == INTERRUPTED_TURN_KEYS.into_iter().collect()
            && value["replies"] == serde_json::json!([])
            && value["interrupted"] == true
            && value["llm"] == serde_json::json!({"stopReason": "break_hard"})
    }

    /// Writes the execution phase adds (ADR-034 §5 "Confirmed activation/Open").
    fn execution_allowed(
        &self,
        method: &str,
        parts: &[&str],
        query: Option<&str>,
        body: Option<&Value>,
    ) -> bool {
        let w = self.working.as_str();
        match (method, parts) {
            ("POST", ["v1", "spaces", s, "modify"]) if *s == w && query.is_none() => {
                let Some(body) = body else { return false };
                let object = body["objectId"].as_str().unwrap_or("");
                let dataset = body["dataset"].as_str().unwrap_or("");
                let store = |(id, coll): &(String, String)| id == object && coll == dataset;
                if store(&self.objects.secrets) {
                    Self::descriptor_only(body)
                } else if store(&self.objects.turns) {
                    Self::interrupted_turn_only(body)
                } else {
                    store(&self.objects.config)
                        || store(&self.objects.triggers)
                        || store(&self.objects.runs)
                }
            }
            ("POST", ["v1", "spaces", s, "objects", o, "chat", "messages"]) => {
                *s == w && *o == self.objects.chat && query.is_none()
            }
            ("POST", ["v1", "events"]) => {
                query.is_none()
                    && body.is_some_and(|b| {
                        b["type"] == "bao.status"
                            && b["scope"] == "account"
                            && b["target"] == self.objects.self_peer
                    })
            }
            _ => false,
        }
    }

    fn admit(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
        stream: bool,
    ) -> Result<Arc<Shared>, AnyError> {
        let shared = self.shared.upgrade().ok_or_else(|| denied("cancelled"))?;
        if shared.cancel.is_cancelled() {
            return Err(denied("cancelled"));
        }
        let (parts, query) = segments(path).ok_or_else(|| denied("path_not_canonical"))?;
        let phase = self.gate.get();
        let allowed = if stream {
            phase == Phase::Execution
                && method == "POST"
                && query.is_none()
                && parts.as_slice() == ["v1", "spaces", self.working.as_str(), "query", "subscribe"]
                && body.is_some_and(|b| {
                    b["objectId"].as_str().is_some_and(identifier)
                        && b["dataset"] == "chat_messages"
                })
        } else {
            self.read_allowed(method, &parts, query)
                || self.trace_create_allowed(method, &parts, body)
                || self.trace_allowed(method, &parts, query, body)
                || (phase == Phase::Execution
                    && self.execution_allowed(method, &parts, query, body))
        };
        if !allowed {
            return Err(denied(match phase {
                Phase::Preparation => "preparation_write_denied",
                Phase::Execution => "execution_route_denied",
            }));
        }
        Ok(shared)
    }
}

/// The stream-line bound (ADR-034 §2): the managed `HttpTransport` reads at
/// most `MAX_STREAM_LINE_BYTES + 1` bytes of a line (N2a-r2 item 7), so an
/// over-long line is never buffered whole; here it ends the stream and is
/// counted as a capacity fault in health, never silently truncated.
struct BoundedLines {
    inner: Box<dyn Iterator<Item = String>>,
    shared: Arc<Shared>,
    ended: bool,
}
impl Iterator for BoundedLines {
    type Item = String;
    fn next(&mut self) -> Option<String> {
        if self.ended {
            return None;
        }
        let line = self.inner.next()?;
        if line.len() > MAX_STREAM_LINE_BYTES {
            self.ended = true;
            self.shared.capacity_fault();
            return None;
        }
        Some(line)
    }
}

impl Transport for GatedTransport {
    fn send(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
    ) -> Result<(u16, Value), AnyError> {
        let shared = self.admit(method, path, body, false)?;
        let data_read = method == "POST" && path.ends_with("/query");
        shared.publish_current(
            TaskPhase::Waiting,
            WaitClass::Http,
            shared.elapsed_ms() + 300_001,
        );
        let result = self.inner.send(method, path, body);
        shared.publish_current(TaskPhase::Running, WaitClass::None, 0);
        if let Ok((status, reply)) = &result {
            if *status < 300 && data_read && !reply["records"].is_array() {
                return Err(denied("malformed_read"));
            }
        }
        result
    }
    fn open_stream(
        &self,
        path: &str,
        body: Option<&Value>,
    ) -> Result<Box<dyn Iterator<Item = String>>, AnyError> {
        let shared = self.admit("POST", path, body, true)?;
        shared.publish_current(TaskPhase::Waiting, WaitClass::Stream, 0);
        let inner = self.inner.open_stream(path, body)?;
        Ok(Box::new(BoundedLines {
            inner,
            shared,
            ended: false,
        }))
    }
}

#[cfg(test)]
mod tests;
