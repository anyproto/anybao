//! The broker + syscall surface (ADR-002 thin host) — the one
//! pipeline for every effect call: classify → key → capability check →
//! replay/mock consult → execute → record (ADR-002 §2). The capability
//! RULE lives here — an optional `grants` set is consulted before
//! anything runs; denials are recorded (`error.type =
//! "capability_denied"`) and returned. No `grants` = the permissive
//! default profile. Grant POLICY lives in `caps` (harness-side data).

use crate::routes::Classifier;
use crate::trace::{input_key, TraceWriter};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, VecDeque};
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

// Trace-side replay machinery + grant policy ride under the broker
// module (record-mode main.rs keeps its module tree untouched; the
// replay/serve wiring lifts them to top-level modules when it lands).

use crate::caps::GrantSet;
use crate::replay::{resolve_blobs, DivergenceError, MockIndex, ReplayCursor};

pub type SharedMailbox = std::sync::Arc<std::sync::Mutex<std::collections::VecDeque<Value>>>;

#[derive(Debug)]
pub struct EffectFailure {
    pub type_: String,
    pub message: String,
}

impl From<DivergenceError> for EffectFailure {
    fn from(e: DivergenceError) -> Self {
        EffectFailure {
            type_: "DivergenceError".into(),
            message: e.to_string(),
        }
    }
}

/// ADR-001 §5: record executes live; replay is strict (cursor,
/// divergence is a hard error); mock is loose FIFO by (effect, key).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Record,
    Replay,
    Mock,
}

/// Policy for a mock miss: fail (tests) or execute live (interactive —
/// the executed call is traceDiff material, meta.mocked = false).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MockUnmatched {
    Fail,
    #[allow(dead_code)] // constructed by the mock wiring in main.rs (next round)
    Live,
}

struct SpanFrame {
    id: String,
    name: String,
    /// Guest-declared narrative kind (getter|mutator|setup|program),
    /// recorded on the end record's meta (ADR-001 §4d). None = undeclared.
    kind: Option<String>,
    t0: Instant,
    effects: u64,
    mutations: u64,
}

pub struct Broker {
    pub writer: TraceWriter,
    /// space-backed module resolution (serve); None = programs_dir only
    pub resolver: Option<Box<dyn crate::resolver::ModuleResolver + Send>>,
    pub current_cell: Option<String>,
    pub config: BTreeMap<String, Value>,
    pub secrets: BTreeMap<String, String>,
    pub env: BTreeMap<String, String>,
    /// Local programs dir for offline resolution (`anyrt run` only).
    /// None in serve — chat replies are served by programs from any
    /// objects; the serve broker structurally CANNOT read program
    /// source off the filesystem (isolation principle).
    pub programs_dir: Option<PathBuf>,
    /// Shared: the serve watcher pushes cross-thread; drained by the
    /// mailbox.drain syscall.
    pub mailbox: Arc<Mutex<VecDeque<Value>>>,
    /// Shared interrupt flag for the serve loop (exposed for wiring;
    /// not yet consulted by the pipeline).
    #[allow(dead_code)] // consulted by the serve wiring in main.rs (next round)
    pub interrupt: Arc<AtomicBool>,
    pub classifier: Classifier,
    pub mode: Mode,
    pub cursor: Option<ReplayCursor>,
    pub mock_index: Option<MockIndex>,
    pub mock_unmatched: MockUnmatched,
    /// Sidecar of the trace being replayed (blob refs resolve here).
    pub blobs: BTreeMap<String, String>,
    /// None = permissive default profile (ADR-002 §2).
    pub grants: Option<GrantSet>,
    span_stack: Vec<SpanFrame>,
    span_n: u64,
    resolve_cache: BTreeMap<String, Value>,
}

impl Broker {
    /// Record-mode permissive broker — the shape main.rs builds today.
    /// Replay/mock/grants wiring sets the public fields (`mode`,
    /// `cursor`, `mock_index`, `mock_unmatched`, `blobs`, `grants`).
    pub fn new(
        writer: TraceWriter,
        config: BTreeMap<String, Value>,
        secrets: BTreeMap<String, String>,
        programs_dir: Option<PathBuf>,
        classifier: Classifier,
    ) -> Self {
        Broker {
            writer,
            resolver: None,
            current_cell: None,
            config,
            secrets,
            env: BTreeMap::new(),
            programs_dir,
            mailbox: Arc::new(Mutex::new(VecDeque::new())),
            interrupt: Arc::new(AtomicBool::new(false)),
            classifier,
            mode: Mode::Record,
            cursor: None,
            mock_index: None,
            mock_unmatched: MockUnmatched::Fail,
            blobs: BTreeMap::new(),
            grants: None,
            span_stack: Vec::new(),
            span_n: 0,
            resolve_cache: BTreeMap::new(),
        }
    }

    /// Open a guest-declared span (ADR-001 §4c). In strict replay the
    /// begin record is a checkpoint, consumed on (name, key).
    pub fn try_span_begin(
        &mut self,
        name: &str,
        kind: Option<String>,
        input: Value,
    ) -> Result<String, EffectFailure> {
        self.span_n += 1;
        let sid = format!("s{}", self.span_n); // execution order => deterministic
        let key = input_key(name, &input);
        if self.mode == Mode::Replay {
            self.cursor
                .as_mut()
                .expect("replay mode requires a cursor")
                .expect_span_begin(name, &key)?;
        }
        let parent = self.span_stack.last().map(|s| s.id.clone());
        let cell = self.current_cell.clone();
        self.writer
            .span_begin(&sid, name, cell.as_deref(), input, &key, parent.as_deref());
        self.span_stack.push(SpanFrame {
            id: sid.clone(),
            name: name.into(),
            kind,
            t0: Instant::now(),
            effects: 0,
            mutations: 0,
        });
        Ok(sid)
    }

    /// Record-mode convenience (infallible without a cursor); replay
    /// wiring must use `try_span_begin`.
    #[allow(dead_code)] // record-mode convenience kept for tools
    pub fn span_begin(&mut self, name: &str, input: Value) -> String {
        self.try_span_begin(name, None, input)
            .expect("span.begin diverged — replay wiring must use try_span_begin")
    }

    /// Close the innermost open span. In strict replay the end record
    /// is a checkpoint, consumed on (name, ok).
    pub fn span_end(
        &mut self,
        ok: bool,
        output: Option<Value>,
        error: Option<Value>,
    ) -> Result<(), EffectFailure> {
        let top = self.span_stack.pop().ok_or(EffectFailure {
            type_: "no_open_span".into(),
            message: "span.end without span.begin".into(),
        })?;
        if self.mode == Mode::Replay {
            self.cursor
                .as_mut()
                .expect("replay mode requires a cursor")
                .expect_span_end(&top.name, ok)?;
        }
        let cell = self.current_cell.clone();
        let mut meta = json!({
            "durMs": top.t0.elapsed().as_millis() as i64,
            "effects": top.effects, "mutations": top.mutations,
        });
        if let Some(k) = &top.kind {
            // narrative label (ADR-001 §4d); mutations stays the oracle
            meta.as_object_mut()
                .unwrap()
                .insert("kind".into(), json!(k));
        }
        self.writer
            .span_end(&top.id, &top.name, cell.as_deref(), ok, output, error, meta);
        Ok(())
    }

    /// Cell lifecycle record (ADR-001 §4b). In strict replay the
    /// matching cell checkpoint is consumed (cell id + ok compared;
    /// metrics legitimately vary run-to-run).
    pub fn try_cell_done(
        &mut self,
        cell: &str,
        ok: bool,
        error: Option<Value>,
        interrupted: bool,
        metrics: Value,
    ) -> Result<(), EffectFailure> {
        while !self.span_stack.is_empty() {
            // a trapped cell skips guest finally — force-close (ADR-001 §4c)
            let err = json!({"type": "unclosed_span",
                             "message": format!("cell {cell} ended with span open")});
            self.span_end(false, None, Some(err))?;
        }
        if self.mode == Mode::Replay {
            self.cursor
                .as_mut()
                .expect("replay mode requires a cursor")
                .expect_cell(cell, ok)?;
        }
        self.writer.cell(cell, ok, error, interrupted, metrics);
        Ok(())
    }

    /// Record-mode convenience (infallible without a cursor); replay
    /// wiring must use `try_cell_done`.
    pub fn cell_done(
        &mut self,
        cell: &str,
        ok: bool,
        error: Option<Value>,
        interrupted: bool,
        metrics: Value,
    ) {
        self.try_cell_done(cell, ok, error, interrupted, metrics)
            .expect("cell checkpoint diverged — replay wiring must use try_cell_done")
    }

    // span meta counters: one bump per record written
    fn bump(&mut self, class: &str) {
        for s in self.span_stack.iter_mut() {
            s.effects += 1;
            if class == "mutate" {
                s.mutations += 1;
            }
        }
    }

    /// Write a replayed/mocked effect record and return the recorded
    /// outcome: recorded error → Err, else blob-resolved output.
    fn record_mocked(
        &mut self,
        name: &str,
        canonical: Value,
        key: &str,
        span: Option<&str>,
        rec: &Value,
        meta: Map<String, Value>,
    ) -> Result<Value, EffectFailure> {
        let cell = self.current_cell.clone();
        let output = rec.get("output").cloned().unwrap_or(Value::Null);
        let error = rec.get("error").cloned().unwrap_or(Value::Null);
        self.writer.effect(
            name,
            cell.as_deref(),
            canonical,
            key,
            if output.is_null() {
                None
            } else {
                Some(output.clone())
            },
            if error.is_null() {
                None
            } else {
                Some(error.clone())
            },
            Value::Object(meta),
            span,
        );
        if !error.is_null() {
            return Err(EffectFailure {
                type_: error
                    .get("type")
                    .and_then(|t| t.as_str())
                    .unwrap_or("")
                    .into(),
                message: error
                    .get("message")
                    .and_then(|m| m.as_str())
                    .unwrap_or("")
                    .into(),
            });
        }
        Ok(resolve_blobs(output, &self.blobs))
    }

    pub fn call(&mut self, name: &str, payload: Value) -> Result<Value, EffectFailure> {
        let class = self.classify(name, &payload);
        let cap = self.cap_of(name, &payload);
        let canonical = payload.clone();
        let key = input_key(name, &canonical);
        let span = self.span_stack.last().map(|s| s.id.clone());

        // Capability check precedes replay/mock consult AND execute
        // (ADR-002 §2): a denial is a recorded fact, never a silent gap.
        if let Some(grants) = &self.grants {
            if !grants.allowed(&cap) {
                let message = format!("capability not granted: {cap} (effect {name})");
                self.bump(class);
                let cell = self.current_cell.clone();
                self.writer.effect(
                    name,
                    cell.as_deref(),
                    canonical,
                    &key,
                    None,
                    Some(json!({"type": "capability_denied", "message": message})),
                    json!({"mocked": false, "class": class}),
                    span.as_deref(),
                );
                return Err(EffectFailure {
                    type_: "capability_denied".into(),
                    message,
                });
            }
        }

        if self.mode == Mode::Replay {
            let rec = self
                .cursor
                .as_mut()
                .expect("replay mode requires a cursor")
                .expect_effect(name, &key)?;
            self.bump(class);
            // recorded meta (durMs of the original run, …) is kept,
            // mocked/class restamped — parity with the reference host
            let mut meta = rec
                .get("meta")
                .and_then(|m| m.as_object())
                .cloned()
                .unwrap_or_default();
            meta.insert("mocked".into(), json!(true));
            meta.insert("class".into(), json!(class));
            return self.record_mocked(name, canonical, &key, span.as_deref(), &rec, meta);
        }

        if self.mode == Mode::Mock {
            let popped = self
                .mock_index
                .as_mut()
                .expect("mock mode requires a mock index")
                .pop(name, &key);
            match popped {
                Some(rec) => {
                    self.bump(class);
                    let mut meta = Map::new();
                    meta.insert("mocked".into(), json!(true));
                    meta.insert("class".into(), json!(class));
                    return self.record_mocked(name, canonical, &key, span.as_deref(), &rec, meta);
                }
                None => match self.mock_unmatched {
                    MockUnmatched::Fail => {
                        return Err(EffectFailure {
                            type_: "unmatched_mock".into(),
                            message: format!("no recorded output for {name} {key}"),
                        })
                    }
                    // fall through: live execution (traceDiff view = mocked: false)
                    MockUnmatched::Live => {}
                },
            }
        }

        self.bump(class);
        let t0 = Instant::now();
        let result = self.execute(name, &payload);
        let dur_ms = t0.elapsed().as_millis() as i64;
        let mut meta = Map::new();
        meta.insert("durMs".into(), json!(dur_ms));
        meta.insert("mocked".into(), json!(false));
        meta.insert("class".into(), json!(class));
        let cell = self.current_cell.clone();
        match result {
            Ok(output) => {
                self.writer.effect(
                    name,
                    cell.as_deref(),
                    canonical,
                    &key,
                    Some(output.clone()),
                    None,
                    Value::Object(meta),
                    span.as_deref(),
                );
                Ok(output)
            }
            Err(e) => {
                let err = json!({"type": e.type_, "message": e.message});
                self.writer.effect(
                    name,
                    cell.as_deref(),
                    canonical,
                    &key,
                    None,
                    Some(err),
                    Value::Object(meta),
                    span.as_deref(),
                );
                Err(e)
            }
        }
    }

    fn classify(&self, name: &str, payload: &Value) -> &'static str {
        if let Some(verb) = name.strip_prefix("http.") {
            let url = payload.get("url").and_then(|u| u.as_str()).unwrap_or("");
            return self.classifier.kind(&verb.to_uppercase(), url);
        }
        "read" // every other syscall is a read
    }

    // Boundary-owned capability truth: http routes classify to
    // llm.chat / data.read / data.write / net.http; every other
    // syscall's cap is its own name (the reference-host default).
    fn cap_of(&self, name: &str, payload: &Value) -> String {
        if let Some(verb) = name.strip_prefix("http.") {
            let url = payload.get("url").and_then(|u| u.as_str()).unwrap_or("");
            return self.classifier.cap(&verb.to_uppercase(), url).to_string();
        }
        name.to_string()
    }

    // --- the syscall implementations -------------------------------------
    fn execute(&mut self, name: &str, payload: &Value) -> Result<Value, EffectFailure> {
        match name {
            n if n.starts_with("http.") => self.sys_http(n, payload),
            "config.get" => self.sys_config_get(payload),
            "mailbox.drain" => {
                let items: Vec<Value> = self
                    .mailbox
                    .lock()
                    .expect("mailbox lock poisoned")
                    .drain(..)
                    .collect();
                Ok(json!({"items": items}))
            }
            "time.now" => Ok(json!({"epoch": SystemTime::now()
                .duration_since(UNIX_EPOCH).unwrap().as_secs_f64()})),
            "random.random" => {
                // secrets-grade uniform in [0,1), mirroring the reference host
                let mut buf = [0u8; 8];
                getrandom(&mut buf);
                let bits = u64::from_le_bytes(buf) >> 11; // 53 bits
                Ok(json!({"value": bits as f64 / (1u64 << 53) as f64}))
            }
            "uuid4" => Ok(json!({"hex": uuid::Uuid::new_v4().to_string()})),
            "sleep" => {
                let secs = payload
                    .get("seconds")
                    .and_then(|s| s.as_f64())
                    .unwrap_or(0.0);
                std::thread::sleep(std::time::Duration::from_secs_f64(secs.min(300.0)));
                Ok(json!({"slept": payload.get("seconds").cloned().unwrap_or(Value::Null)}))
            }
            "env.get" => {
                let key = payload.get("name").and_then(|n| n.as_str()).unwrap_or("");
                let present = self.env.contains_key(key);
                Ok(json!({"present": present, "value": self.env.get(key)}))
            }
            "module.resolve" => self.sys_module_resolve(payload),
            "batch" => self.sys_batch(payload),
            "trace.effects_of" => self.sys_effects_of(payload),
            "trace.effect_get" => self.sys_effect_get(payload),
            "kernel.boot" => Ok(payload.clone()), // pins echo into the record
            other => Err(EffectFailure {
                type_: "unknown_effect".into(),
                message: format!("no such effect: {other}"),
            }),
        }
    }

    fn sys_http(&self, name: &str, payload: &Value) -> Result<Value, EffectFailure> {
        let verb = name.strip_prefix("http.").unwrap().to_uppercase();
        let mut url = payload
            .get("url")
            .and_then(|u| u.as_str())
            .ok_or(EffectFailure {
                type_: "TypeError".into(),
                message: "http call without url".into(),
            })?
            .to_string();
        if let Some(params) = payload.get("params").and_then(|p| p.as_object()) {
            let qs: Vec<String> = params
                .iter()
                .map(|(k, v)| {
                    let val = v
                        .as_str()
                        .map(str::to_string)
                        .unwrap_or_else(|| crate::trace::canonical_json(v));
                    format!("{}={}", urlencode(k), urlencode(&val))
                })
                .collect();
            url.push(if url.contains('?') { '&' } else { '?' });
            url.push_str(&qs.join("&"));
        }
        let timeout = payload
            .get("timeout")
            .and_then(|t| t.as_f64())
            .unwrap_or(180.0);
        // redirects: max follows for THIS request; 0 = manual (the 3xx
        // and its location header come back as data — ADR-008 §2)
        let mut req = match payload.get("redirects").and_then(|r| r.as_u64()) {
            Some(max) => ureq::AgentBuilder::new()
                .redirects(max as u32)
                .build()
                .request(&verb, &url),
            None => ureq::request(&verb, &url),
        }
        .timeout(std::time::Duration::from_secs_f64(timeout));
        if let Some(headers) = payload.get("headers").and_then(|h| h.as_object()) {
            for (k, v) in headers {
                if let Some(s) = v.as_str() {
                    req = req.set(k, s);
                }
            }
        }
        // named-credential injection: value resolved AFTER recording
        if let Some(cred) = payload.get("credential").and_then(|c| c.as_object()) {
            let r = cred.get("ref").and_then(|v| v.as_str()).unwrap_or("");
            let header = cred.get("header").and_then(|v| v.as_str()).unwrap_or("");
            let prefix = cred.get("prefix").and_then(|v| v.as_str()).unwrap_or("");
            let secret = self.secrets.get(r).ok_or(EffectFailure {
                type_: "RuntimeError".into(),
                message: format!("no secret for credential ref {r:?}"),
            })?;
            req = req.set(header, &format!("{prefix}{secret}"));
        }
        let resp = if let Some(body) = payload.get("json").filter(|v| !v.is_null()) {
            req.set("Content-Type", "application/json")
                .send_string(&crate::trace::canonical_json(body))
        } else if let Some(body) = payload.get("body").and_then(|b| b.as_str()) {
            req.send_string(body)
        } else {
            req.call()
        };
        let resp = match resp {
            Ok(r) => r,
            Err(ureq::Error::Status(_, r)) => r, // non-2xx is data, not error
            Err(e) => {
                return Err(EffectFailure {
                    type_: "URLError".into(),
                    message: e.to_string(),
                })
            }
        };
        let status = resp.status();
        let final_url = resp.get_url().to_string(); // post-redirect (ADR-008 §2)
        let headers: Map<String, Value> = resp
            .headers_names()
            .iter()
            .filter_map(|h| resp.header(h).map(|v| (h.to_lowercase(), json!(v))))
            .collect();
        let body = resp.into_string().map_err(|e| EffectFailure {
            type_: "URLError".into(),
            message: e.to_string(),
        })?;
        Ok(json!({"status": status, "headers": headers, "body": body, "url": final_url}))
    }

    fn sys_config_get(&self, payload: &Value) -> Result<Value, EffectFailure> {
        let key = payload.get("key").and_then(|k| k.as_str()).unwrap_or("");
        if self.secrets.contains_key(key) {
            return Err(EffectFailure {
                type_: "ConfigError".into(),
                message: format!("{key:?} is a secret — not readable from cells"),
            });
        }
        match self.config.get(key) {
            Some(v) => Ok(json!({"value": v})),
            None => Err(EffectFailure {
                type_: "ConfigError".into(),
                message: format!("no config value for {key:?}"),
            }),
        }
    }

    fn sys_module_resolve(&mut self, payload: &Value) -> Result<Value, EffectFailure> {
        let spec = payload
            .get("spec")
            .and_then(|s| s.as_str())
            .ok_or(EffectFailure {
                type_: "TypeError".into(),
                message: "module.resolve without spec".into(),
            })?;
        if let Some(resolver) = self.resolver.as_mut() {
            // space-backed resolution (serve); the local dir below is
            // the `run` subcommand's offline path
            let frm = payload.get("frm").and_then(|f| f.as_str());
            return resolver.resolve(spec, frm).map_err(|e| EffectFailure {
                type_: "KeyError".into(),
                message: e.to_string(),
            });
        }
        let cache_state = if self.resolve_cache.contains_key(spec) {
            "hit"
        } else {
            "miss"
        };
        if let Some(cached) = self.resolve_cache.get(spec) {
            let mut out = cached.clone();
            out["cache"] = json!(cache_state);
            return Ok(out);
        }
        let dir = self.programs_dir.as_ref().ok_or(EffectFailure {
            type_: "KeyError".into(),
            message: format!(
                "module.resolve for {spec:?} without a resolver or a local \
                 programs dir — serve resolves from the space only"
            ),
        })?;
        let path = crate::resolver::local_source_path(dir, spec);
        let source = std::fs::read_to_string(&path).map_err(|_| EffectFailure {
            type_: "KeyError".into(),
            message: format!("program not found: {spec} ({})", path.display()),
        })?;
        let mut h = Sha256::new();
        h.update(source.as_bytes());
        let out = json!({
            "spaceId": "local", "objectId": spec, "marker": 0,
            "sourceHash": format!("sha256:{}", hex::encode(h.finalize())),
            "source": source, "cache": cache_state,
        });
        self.resolve_cache.insert(spec.into(), out.clone());
        Ok(out)
    }

    fn sys_batch(&mut self, payload: &Value) -> Result<Value, EffectFailure> {
        let name = payload
            .get("name")
            .and_then(|n| n.as_str())
            .unwrap_or("")
            .to_string();
        let payloads = payload
            .get("payloads")
            .and_then(|p| p.as_array())
            .cloned()
            .unwrap_or_default();
        let results: Vec<Value> = payloads
            .into_iter()
            .map(|p| match self.call(&name, p) {
                Ok(v) => v,
                Err(e) => json!({"error": {"type": e.type_, "message": e.message}}),
            })
            .collect();
        Ok(json!({"results": results}))
    }

    /// A cell's (or span's) IMMEDIATE children (ADR-001 §4d): bare effect
    /// records directly in the scope, PLUS child span-end records whose
    /// parent is the scope — so a composite tool call renders as one line,
    /// its inner effects reachable by drilling into the child span. A span
    /// nested one level down appears as a single row here, not its effects.
    fn sys_effects_of(&self, payload: &Value) -> Result<Value, EffectFailure> {
        let cell = payload.get("cell").and_then(|c| c.as_str());
        let span = payload.get("span").and_then(|s| s.as_str());
        // "directly in the scope": for a span query, the record's own span
        // (effects) / parent (spans) equals it; for a cell-only query, that
        // link is null (top level of the cell).
        let direct = |link: Option<&Value>| match span {
            Some(s) => link.map(|v| v == s).unwrap_or(false),
            None => link.is_none_or(|v| v.is_null()),
        };
        let out: Vec<Value> = self
            .writer
            .records
            .iter()
            .filter(|r| cell.is_none_or(|c| r["cell"] == c))
            .filter_map(|r| match r["kind"].as_str() {
                Some("effect") if direct(r.get("span")) => Some(json!({
                    "seq": r["seq"], "effect": r["effect"],
                    "class": r["meta"].get("class").cloned().unwrap_or(Value::Null),
                    "mocked": r["meta"].get("mocked").cloned().unwrap_or(Value::Null),
                    "error": r["error"].get("type").cloned().unwrap_or(Value::Null),
                    "span": r.get("span").cloned().unwrap_or(Value::Null),
                })),
                Some("span") if r["phase"] == "end" && direct(r.get("parent")) => {
                    let muts = r["meta"]["mutations"].as_u64().unwrap_or(0);
                    Some(json!({
                        // a span row is a collapsed op: `name` (not `effect`),
                        // narrative `kind`, and a boundary-backed `class` from
                        // the inner mutate count so the digest marks mutations.
                        "seq": r["seq"], "span": r["span"], "name": r["name"],
                        "kind": r["meta"].get("kind").cloned().unwrap_or(Value::Null),
                        "class": if muts > 0 { json!("mutate") } else { json!("read") },
                        "ok": r["ok"], "mutations": muts,
                        "effects": r["meta"].get("effects").cloned().unwrap_or(json!(0)),
                        "error": r["error"].get("type").cloned().unwrap_or(Value::Null),
                    }))
                }
                _ => None,
            })
            .collect();
        Ok(json!({"records": out}))
    }

    fn sys_effect_get(&self, payload: &Value) -> Result<Value, EffectFailure> {
        let seq = payload.get("seq").and_then(|s| s.as_i64()).unwrap_or(-1);
        for r in &self.writer.records {
            // effect OR span (a digest-cited #seq for a facade is a span
            // record) — so effects.get walks from a digest line to the span,
            // whose `span` id then feeds effects.of(span=…) (ADR-001 §4d).
            if matches!(r["kind"].as_str(), Some("effect" | "span")) && r["seq"] == seq {
                return Ok(r.clone());
            }
        }
        Err(EffectFailure {
            type_: "KeyError".into(),
            message: format!("no effect or span record with seq {seq}"),
        })
    }
}

fn urlencode(s: &str) -> String {
    let mut out = String::new();
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

fn getrandom(buf: &mut [u8]) {
    // uuid's rng is already OS-backed; reuse it for the random syscall
    for chunk in buf.chunks_mut(16) {
        let bytes = *uuid::Uuid::new_v4().as_bytes();
        chunk.copy_from_slice(&bytes[..chunk.len()]);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_broker(run: &str) -> Broker {
        Broker::new(
            TraceWriter::new(json!({"id": run})),
            BTreeMap::new(),
            BTreeMap::new(),
            Some(PathBuf::from("programs")),
            Classifier::new(None),
        )
    }

    #[test]
    fn serve_shaped_broker_refuses_local_resolution() {
        // serve builds the broker with programs_dir: None; without a
        // space resolver the disk path must refuse, not read the fs
        let mut b = make_broker("r");
        b.programs_dir = None;
        let err = b
            .call("module.resolve", json!({"spec": "toolcaller@v1"}))
            .unwrap_err();
        assert!(err.message.contains("serve resolves from the space only"));
    }

    /// A recorded trace with one http.get — built by hand so replay and
    /// mock consult can be tested without a network.
    fn recorded_http_trace() -> TraceWriter {
        let mut w = TraceWriter::new(json!({"id": "rec"}));
        let key = input_key("http.get", &json!({"url": "https://a"}));
        w.effect(
            "http.get",
            Some("main"),
            json!({"url": "https://a"}),
            &key,
            Some(json!({"status": 200, "body": "hello"})),
            None,
            json!({"durMs": 7, "mocked": false, "class": "read"}),
            None,
        );
        w.cell("main", true, None, false, json!({}));
        w
    }

    #[test]
    fn denial_is_recorded_and_returned() {
        let mut b = make_broker("d1");
        b.current_cell = Some("main".into());
        b.grants = Some(GrantSet::of(["data.read"]));
        let err = b.call("http.get", json!({"url": "https://a"})).unwrap_err();
        assert_eq!(err.type_, "capability_denied");
        let rec = &b.writer.records[1];
        assert_eq!(rec["error"]["type"], "capability_denied");
        assert_eq!(rec["output"], Value::Null);
        assert_eq!(rec["meta"], json!({"class": "read", "mocked": false}));
        assert_eq!(rec["cell"], "main");
    }

    #[test]
    fn permissive_default_and_covering_grant() {
        let mut b = make_broker("d2");
        assert!(b.call("time.now", json!({})).is_ok()); // grants = None
        let mut b2 = make_broker("d3");
        b2.grants = Some(GrantSet::of(["time.*"])); // wildcard covers time.now
        assert!(b2.call("time.now", json!({})).is_ok());
        let mut b3 = make_broker("d4");
        b3.grants = Some(GrantSet::of(["kernel.boot"])); // exact cap = effect name
        assert!(b3.call("kernel.boot", json!({"pin": 1})).is_ok());
        assert!(b3.call("time.now", json!({})).is_err());
    }

    #[test]
    fn replay_returns_recorded_without_executing() {
        let w1 = recorded_http_trace();
        let mut b = make_broker("r1");
        b.mode = Mode::Replay;
        b.cursor = Some(ReplayCursor::new(&w1.records));
        b.current_cell = Some("main".into());
        // https://a would URLError if executed — replay consult short-circuits
        let out = b.call("http.get", json!({"url": "https://a"})).unwrap();
        assert_eq!(out, json!({"status": 200, "body": "hello"}));
        let rec = &b.writer.records[1];
        assert_eq!(rec["meta"]["mocked"], true);
        assert_eq!(rec["meta"]["durMs"], 7); // recorded meta kept
        b.try_cell_done("main", true, None, false, json!({}))
            .unwrap();
        assert!(b.cursor.as_ref().unwrap().exhausted());
    }

    #[test]
    fn replay_divergence_on_changed_input() {
        let w1 = recorded_http_trace();
        let mut b = make_broker("r2");
        b.mode = Mode::Replay;
        b.cursor = Some(ReplayCursor::new(&w1.records));
        let err = b
            .call("http.get", json!({"url": "https://CHANGED"}))
            .unwrap_err();
        assert_eq!(err.type_, "DivergenceError");
    }

    #[test]
    fn grants_check_precedes_replay_consult() {
        let w1 = recorded_http_trace();
        let mut b = make_broker("r3");
        b.mode = Mode::Replay;
        b.cursor = Some(ReplayCursor::new(&w1.records));
        b.grants = Some(GrantSet::of(Vec::<String>::new()));
        // denial wins over the cursor: no checkpoint consumed
        let err = b.call("http.get", json!({"url": "https://a"})).unwrap_err();
        assert_eq!(err.type_, "capability_denied");
        assert!(!b.cursor.as_ref().unwrap().exhausted());
    }

    #[test]
    fn replay_error_is_recorded_fact_and_err() {
        let mut w = TraceWriter::new(json!({"id": "re"}));
        let key = input_key("http.get", &json!({"url": "https://a"}));
        w.effect(
            "http.get",
            None,
            json!({"url": "https://a"}),
            &key,
            None,
            Some(json!({"type": "URLError", "message": "boom"})),
            json!({"mocked": false, "class": "read"}),
            None,
        );
        let mut b = make_broker("re2");
        b.mode = Mode::Replay;
        b.cursor = Some(ReplayCursor::new(&w.records));
        let err = b.call("http.get", json!({"url": "https://a"})).unwrap_err();
        assert_eq!(err.type_, "URLError");
        assert_eq!(err.message, "boom");
        assert_eq!(b.writer.records[1]["error"]["type"], "URLError");
        assert_eq!(b.writer.records[1]["meta"]["mocked"], true);
    }

    #[test]
    fn mock_fifo_matched_and_unmatched_fail_vs_live() {
        let w1 = recorded_http_trace();
        let mut b_fail = make_broker("m1");
        b_fail.mode = Mode::Mock;
        b_fail.mock_index = Some(MockIndex::new(&w1.records));
        let out = b_fail
            .call("http.get", json!({"url": "https://a"}))
            .unwrap();
        assert_eq!(out["status"], 200);
        assert_eq!(b_fail.writer.records[1]["meta"]["mocked"], true);
        let err = b_fail
            .call("http.get", json!({"url": "https://new"}))
            .unwrap_err();
        assert_eq!(err.type_, "unmatched_mock");
        // an unmatched fail writes no record
        assert_eq!(b_fail.writer.records.len(), 2);

        let mut b_live = make_broker("m2");
        b_live.mode = Mode::Mock;
        b_live.mock_index = Some(MockIndex::new(&w1.records));
        b_live.mock_unmatched = MockUnmatched::Live;
        // unmatched → executed live (kernel.boot echoes) → traceDiff material
        let out = b_live.call("kernel.boot", json!({"pin": 1})).unwrap();
        assert_eq!(out, json!({"pin": 1}));
        assert_eq!(b_live.writer.records[1]["meta"]["mocked"], false);
    }

    #[test]
    fn replay_resolves_spilled_output() {
        let big = json!({"body": "y".repeat(100_000)});
        let mut b1 = make_broker("blob1");
        b1.call("kernel.boot", big.clone()).unwrap(); // echo → spilled output
        assert!(b1.writer.records[1]["output"].get("__blob").is_some());

        let mut b2 = make_broker("blob2");
        b2.mode = Mode::Replay;
        b2.cursor = Some(ReplayCursor::new(&b1.writer.records));
        b2.blobs = b1.writer.blobs.iter().cloned().collect();
        assert_eq!(b2.call("kernel.boot", big.clone()).unwrap(), big);
    }

    #[test]
    fn span_replay_checkpoints_and_divergence() {
        // record: a span around one effect
        let mut b1 = make_broker("sp1");
        b1.span_begin("helper.sync", json!({"kwargs": {"n": 1}}));
        b1.call("kernel.boot", json!({"p": 1})).unwrap();
        b1.span_end(true, Some(json!({"n": 1})), None).unwrap();

        // identical rerun replays clean, span records consumed as checkpoints
        let mut b2 = make_broker("sp2");
        b2.mode = Mode::Replay;
        b2.cursor = Some(ReplayCursor::new(&b1.writer.records));
        b2.try_span_begin("helper.sync", None, json!({"kwargs": {"n": 1}}))
            .unwrap();
        b2.call("kernel.boot", json!({"p": 1})).unwrap();
        b2.span_end(true, Some(json!({"n": 1})), None).unwrap();
        assert!(b2.cursor.as_ref().unwrap().exhausted());

        // changed facade input diverges at the begin checkpoint
        let mut b3 = make_broker("sp3");
        b3.mode = Mode::Replay;
        b3.cursor = Some(ReplayCursor::new(&b1.writer.records));
        let err = b3
            .try_span_begin("helper.sync", None, json!({"kwargs": {"n": 999}}))
            .unwrap_err();
        assert_eq!(err.type_, "DivergenceError");
    }

    #[test]
    fn effects_of_returns_immediate_children_and_span_rows() {
        // ADR-001 §4d: a cell-scope view surfaces the cell's IMMEDIATE
        // children — bare effects plus a one-row collapse of each child span,
        // never the effects nested inside those spans.
        let mut b = make_broker("io1");
        b.current_cell = Some("c1".into());
        b.call("kernel.boot", json!({"top": 1})).unwrap(); // bare, top-level
        let sid = b
            .try_span_begin(
                "any.query_objects",
                Some("getter".into()),
                json!({"space": "s"}),
            )
            .unwrap();
        b.call("kernel.boot", json!({"inner": 1})).unwrap(); // nested in the span
        b.span_end(true, Some(json!(["row"])), None).unwrap();

        let out = b.call("trace.effects_of", json!({"cell": "c1"})).unwrap();
        let recs = out["records"].as_array().unwrap();
        assert_eq!(recs.len(), 2); // bare effect + span row, NOT the inner effect
        let eff = recs.iter().find(|r| r.get("effect").is_some()).unwrap();
        assert_eq!(eff["effect"], "kernel.boot");
        let row = recs.iter().find(|r| r.get("name").is_some()).unwrap();
        assert_eq!(row["name"], "any.query_objects");
        assert_eq!(row["kind"], "getter"); // narrative, declared
        assert_eq!(row["class"], "read"); // boundary: 0 inner mutations
        assert_eq!(row["ok"], true);
        assert!(row.get("effect").is_none()); // a span row, not an effect

        // span-scope query drills in: the inner effect is addressable there.
        let inner = b.call("trace.effects_of", json!({"span": sid})).unwrap();
        let irecs = inner["records"].as_array().unwrap();
        assert_eq!(irecs.len(), 1);
        assert_eq!(irecs[0]["effect"], "kernel.boot");

        // a facade's cited #seq resolves via effect_get to its span record,
        // whose `span` id then feeds effects.of(span=…) — the drill path.
        let span_seq = row["seq"].as_i64().unwrap();
        let rec = b
            .call("trace.effect_get", json!({"seq": span_seq}))
            .unwrap();
        assert_eq!(rec["kind"], "span");
        assert_eq!(rec["phase"], "end");
        assert_eq!(rec["name"], "any.query_objects");
        assert_eq!(rec["span"], sid);
    }

    #[test]
    fn span_end_records_declared_kind_in_meta() {
        let mut b = make_broker("k1");
        b.try_span_begin(
            "any.create_object",
            Some("mutator".into()),
            json!({"space": "s"}),
        )
        .unwrap();
        b.span_end(true, Some(json!({"objectId": "o1"})), None)
            .unwrap();
        let end = b
            .writer
            .records
            .iter()
            .find(|r| r["kind"] == "span" && r["phase"] == "end")
            .unwrap();
        assert_eq!(end["meta"]["kind"], "mutator");
        assert_eq!(end["meta"]["mutations"], 0); // narrative kind != the oracle
    }

    #[test]
    fn cell_done_force_closes_dangling_spans() {
        let mut b = make_broker("sp4");
        b.current_cell = Some("c1".into());
        b.span_begin("helper.sync", json!({}));
        b.cell_done(
            "c1",
            false,
            Some(json!({"type": "Interrupted", "message": "fuel"})),
            true,
            json!({}),
        );
        let end = &b.writer.records[2];
        assert_eq!(end["kind"], "span");
        assert_eq!(end["phase"], "end");
        assert_eq!(end["ok"], false);
        assert_eq!(end["error"]["type"], "unclosed_span");
        assert_eq!(b.writer.records[3]["kind"], "cell");
        assert!(b.span_stack.is_empty());
    }

    #[test]
    fn mailbox_is_shared_across_threads() {
        let mut b = make_broker("mb1");
        let mailbox = b.mailbox.clone();
        let t = std::thread::spawn(move || {
            mailbox
                .lock()
                .unwrap()
                .push_back(json!({"kind": "trigger", "n": 1}));
        });
        t.join().unwrap();
        let out = b.call("mailbox.drain", json!({})).unwrap();
        assert_eq!(out["items"], json!([{"kind": "trigger", "n": 1}]));
        assert!(b.mailbox.lock().unwrap().is_empty());
    }
}
