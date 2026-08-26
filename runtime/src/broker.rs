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
use std::io::Read as _;
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

// Trace-side replay machinery + grant policy ride under the broker
// module (record-mode main.rs keeps its module tree untouched; the
// replay/serve wiring lifts them to top-level modules when it lands).

use crate::caps::GrantSet;
use crate::oauth::{OauthState, OAUTH_REF_PREFIX};
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

/// The secrets store as the broker sees it (ADR-021 §4): the
/// device-local `agent_secrets` row is the credential, read at
/// injection time — after the effect is recorded, so the trace stays
/// value-free and replay never reads it. `secrets` (the seeded map)
/// is the no-store fallback only.
pub trait SecretSource: Send + Sync {
    /// Err = the store could not be reached (a typed failure, not a
    /// miss); Ok(None) = no row / empty value.
    fn read(&self, key: &str) -> Result<Option<String>, String>;
    /// Stamp the ref's row `status: "missing"` + the descriptor the
    /// guest passed (`about`), so the run wrapper can post ONE request
    /// bubble and the dashboard can render it (ADR-021 §2).
    fn mark_missing(&self, key: &str, about: &Value, run_id: &str);
    /// The destination answered 401 to a request carrying this ref:
    /// the stored value is wrong. Stamp `status: "rejected"` so the
    /// same request bubble asks for a replacement (ADR-021 §2).
    fn mark_rejected(&self, key: &str, about: &Value, run_id: &str, http_status: u16);
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
    /// Remaining fuel, refreshed by the runner's epoch callback every
    /// tick (host fns can't reach the store). Read by `fuel.state`;
    /// ≤EPOCH_TICK_MS stale — a checkpoint signal, not an exact meter.
    pub fuel_gauge: Arc<std::sync::atomic::AtomicU64>,
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
    /// Guest http requests that reference the `agent_secrets` dataset —
    /// or this object id (the per-space secrets object; on a pre-split
    /// any server, the config object secrets still live on) — are
    /// refused before execution (ADR-008: secrets never enter guest
    /// code or traces; connectors authenticate via `credential: {ref}`).
    pub secrets_guard: Option<String>,
    /// Managed OAuth state (ADR-011) — process-shared; None = no oauth
    /// wiring (managed refs fail typed `not_configured`).
    pub oauth: Option<Arc<OauthState>>,
    /// The secrets store (ADR-021 §4); None = seeds only (`anyrt run`,
    /// or a server without the secrets object).
    pub secret_store: Option<Arc<dyn SecretSource>>,
    /// Static refs that need the human this run — resolved to nothing,
    /// or rejected (401) by their destination — in first-event order
    /// (ADR-021 §2); the run wrapper posts the request bubbles.
    pub missing_secrets: Vec<String>,
    /// Host-emit depth (ADR-011 §6): >0 while a syscall re-enters
    /// `call` to record its own nested effect (`oauth.refresh`).
    hosted: u32,
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
            fuel_gauge: Arc::new(std::sync::atomic::AtomicU64::new(0)),
            interrupt: Arc::new(AtomicBool::new(false)),
            classifier,
            mode: Mode::Record,
            cursor: None,
            mock_index: None,
            mock_unmatched: MockUnmatched::Fail,
            blobs: BTreeMap::new(),
            grants: None,
            secrets_guard: None,
            oauth: None,
            secret_store: None,
            missing_secrets: Vec::new(),
            hosted: 0,
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
        // Host-emitted calls skip it (ADR-011 §6): the guest asked for
        // the http cap; the nested refresh is the host's implementation
        // detail, and a guest lacking `oauth.refresh` must not be able
        // to break credentialed http.
        if let Some(grants) = &self.grants {
            if self.hosted == 0 && !grants.allowed(&cap) {
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
            // Host-emitted records (meta.hosted, ADR-011 §6) precede the
            // guest record that triggered them; drain them through —
            // write-through with the recorded outcome, error included
            // (the guest record that follows already reflects its
            // consequence) — instead of diverging on them.
            loop {
                let drained = self
                    .cursor
                    .as_mut()
                    .expect("replay mode requires a cursor")
                    .take_hosted_mismatch(name, &key);
                let Some(rec) = drained else { break };
                let rec_name = rec["effect"].as_str().unwrap_or("").to_string();
                let rec_key = rec["key"].as_str().unwrap_or("").to_string();
                let rec_input = rec.get("input").cloned().unwrap_or(Value::Null);
                let rec_class = self.classify(&rec_name, &rec_input);
                self.bump(rec_class);
                let mut meta = rec
                    .get("meta")
                    .and_then(|m| m.as_object())
                    .cloned()
                    .unwrap_or_default();
                meta.insert("mocked".into(), json!(true));
                meta.insert("class".into(), json!(rec_class));
                let _ =
                    self.record_mocked(&rec_name, rec_input, &rec_key, span.as_deref(), &rec, meta);
            }
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
        if self.hosted > 0 {
            // ADR-011 §6: mark host-emitted records — replay drains on
            // this flag — and name the credential they serve
            meta.insert("hosted".into(), json!(true));
            if let Some(p) = payload.get("provider").and_then(|p| p.as_str()) {
                meta.insert(
                    "credential".into(),
                    json!({"ref": format!("{OAUTH_REF_PREFIX}{p}")}),
                );
            }
        }
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
        match name {
            // ADR-011 §5: the token lifecycle mutates device state
            "oauth.connect" | "oauth.disconnect" | "oauth.refresh" => "mutate",
            _ => "read",
        }
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
            // ADR-019 §8: the host's UTC offset rides the same recorded
            // effect — the guest renders times in the user's local zone
            // without a second nondeterministic source.
            "time.now" => Ok(json!({
                "epoch": SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64(),
                "offset_s": chrono::Local::now().offset().local_minus_utc(),
                "tz": std::env::var("TZ").ok(),
            })),
            // Cooperative budgeting (ADR-003 §2 fuel): long jobs check
            // remaining fuel and checkpoint + exit before exhausting —
            // an out-of-fuel trap kills the whole run unrecoverably.
            "fuel.state" => Ok(json!({
                "remaining": self.fuel_gauge.load(std::sync::atomic::Ordering::Relaxed),
                "budget": crate::runner::FUEL_PER_CELL,
            })),
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
            "oauth.connect" => self.sys_oauth_connect(payload),
            "oauth.status" => self.sys_oauth_status(payload),
            "oauth.disconnect" => self.sys_oauth_disconnect(payload),
            "oauth.refresh" => self.sys_oauth_refresh(payload),
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

    /// Host-emitted nested effect (ADR-011 §6, the batch re-entry
    /// precedent): records inside the current crossing, at a lower seq
    /// than the outer record; skips the grant gate.
    fn call_hosted(&mut self, name: &str, payload: Value) -> Result<Value, EffectFailure> {
        self.hosted += 1;
        let out = self.call(name, payload);
        self.hosted -= 1;
        out
    }

    fn oauth_state(&self) -> Result<Arc<OauthState>, EffectFailure> {
        self.oauth.clone().ok_or_else(|| EffectFailure {
            type_: "not_configured".into(),
            message: "oauth is not wired into this runtime".into(),
        })
    }

    /// `oauth.connect` (ADR-011 §5): consent as an effect that returns
    /// no tokens — blocks while the human clicks (default 120s).
    fn sys_oauth_connect(&mut self, payload: &Value) -> Result<Value, EffectFailure> {
        let state = self.oauth_state()?;
        let provider = payload
            .get("provider")
            .and_then(|p| p.as_str())
            .unwrap_or("");
        let scopes = payload.get("scopes").and_then(|s| s.as_array()).map(|a| {
            a.iter()
                .filter_map(|v| v.as_str().map(str::to_string))
                .collect()
        });
        let timeout = payload.get("timeout").and_then(|t| t.as_f64());
        // the connector's bundled public client (ADR-011 §3) — not a
        // secret, recorded in the trace like the rest of the payload
        let client = payload.get("client_id").and_then(|v| v.as_str()).map(|id| {
            (
                id.to_string(),
                payload
                    .get("client_secret")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
            )
        });
        state.connect(provider, scopes, timeout, client)
    }

    fn sys_oauth_status(&mut self, payload: &Value) -> Result<Value, EffectFailure> {
        let state = self.oauth_state()?;
        let provider = payload
            .get("provider")
            .and_then(|p| p.as_str())
            .unwrap_or("");
        state.status(provider)
    }

    fn sys_oauth_disconnect(&mut self, payload: &Value) -> Result<Value, EffectFailure> {
        let state = self.oauth_state()?;
        let provider = payload
            .get("provider")
            .and_then(|p| p.as_str())
            .unwrap_or("");
        state.disconnect(provider)
    }

    /// The refresh-token exchange as its own recorded effect (ADR-011
    /// §6) — host-emitted only: the one nondeterministic step that
    /// would otherwise hide inside an http call.
    fn sys_oauth_refresh(&mut self, payload: &Value) -> Result<Value, EffectFailure> {
        if self.hosted == 0 {
            return Err(EffectFailure {
                type_: "host_only".into(),
                message: "oauth.refresh is host-emitted at credential injection — \
                          pass `credential: {ref}` on http calls instead"
                    .into(),
            });
        }
        let state = self.oauth.clone().ok_or_else(|| EffectFailure {
            type_: "not_configured".into(),
            message: "oauth is not wired into this runtime".into(),
        })?;
        let provider = payload
            .get("provider")
            .and_then(|p| p.as_str())
            .unwrap_or("");
        state.refresh(provider)
    }

    /// ADR-011 §6: managed-ref resolution at injection time — cached
    /// access token while it outlives the margin, else a single-flight
    /// refresh through a host-emitted `oauth.refresh` record.
    fn resolve_managed(&mut self, r: &str) -> Result<String, EffectFailure> {
        let state = self.oauth.clone().ok_or_else(|| EffectFailure {
            type_: "not_configured".into(),
            message: format!("credential ref {r:?} is managed but oauth is not wired in"),
        })?;
        let provider = r.strip_prefix(OAUTH_REF_PREFIX).unwrap_or("");
        if !state.providers.contains_key(provider) {
            // sub-refs (`connector.oauth.google.refresh`) land here too:
            // only provider HANDLES are injectable — the stored values
            // never are (ADR-011 §4)
            return Err(EffectFailure {
                type_: "not_configured".into(),
                message: format!(
                    "no oauth provider for credential ref {r:?} — managed refs \
                     name a provider handle (e.g. connector.oauth.google); \
                     stored sub-refs are host-internal"
                ),
            });
        }
        if let Some(tok) = state.fresh_token(r) {
            return Ok(tok);
        }
        let gate = state.flight_gate(r);
        let _flight = gate.lock().expect("oauth flight gate poisoned");
        if let Some(tok) = state.fresh_token(r) {
            return Ok(tok); // refreshed while we waited on the gate
        }
        if state.secret(&format!("{r}.refresh")).is_none() {
            // no grant, nothing happened — no refresh record either
            return Err(EffectFailure {
                type_: "not_connected".into(),
                message: format!("{provider} is not connected — run the provider's connect first"),
            });
        }
        self.call_hosted("oauth.refresh", json!({"provider": provider}))?;
        state.fresh_token(r).ok_or_else(|| EffectFailure {
            type_: "RuntimeError".into(),
            message: "oauth refresh reported ok but produced no cached token".into(),
        })
    }

    /// A static ref (`connector.key.*`, `llm.key.*`): the store row if
    /// there is a store, else the seeded map (ADR-021 §4). A miss is
    /// typed `SecretMissing` — the message stays byte-identical to what
    /// connectors string-match — and is remembered for the run wrapper.
    fn resolve_static(&mut self, r: &str, about: Option<&Value>) -> Result<String, EffectFailure> {
        let from_store = match &self.secret_store {
            Some(store) => store.read(r).map_err(|e| EffectFailure {
                type_: "RuntimeError".into(),
                message: format!("secret store unreachable resolving {r:?}: {e}"),
            })?,
            None => None,
        };
        if let Some(v) = from_store.or_else(|| self.secrets.get(r).cloned()) {
            return Ok(v);
        }
        if !self.missing_secrets.iter().any(|m| m == r) {
            self.missing_secrets.push(r.to_string());
            if let Some(store) = &self.secret_store {
                store.mark_missing(r, about.unwrap_or(&Value::Null), &self.writer.run_id());
            }
        }
        Err(EffectFailure {
            type_: "SecretMissing".into(),
            message: format!("no secret for credential ref {r:?}"),
        })
    }

    fn sys_http(&mut self, name: &str, payload: &Value) -> Result<Value, EffectFailure> {
        let verb = name.strip_prefix("http.").unwrap().to_uppercase();
        let mut url = payload
            .get("url")
            .and_then(|u| u.as_str())
            .ok_or(EffectFailure {
                type_: "TypeError".into(),
                message: "http call without url".into(),
            })?
            .to_string();
        self.secrets_read_guard(&url, payload)?;
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
        let explicit_redirects = payload.get("redirects").and_then(|r| r.as_u64());
        // named-credential injection: value resolved AFTER recording.
        // Static refs read the per-run map; managed oauth refs resolve
        // through the token lifecycle (ADR-011 §6) — same guest shape,
        // different custody.
        let mut static_ref: Option<(String, Value)> = None;
        let cred = match payload.get("credential").and_then(|c| c.as_object()) {
            Some(cred) => {
                let r = cred
                    .get("ref")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let header = cred
                    .get("header")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let prefix = cred
                    .get("prefix")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let value = if r.starts_with(OAUTH_REF_PREFIX) {
                    self.resolve_managed(&r)?
                } else {
                    static_ref =
                        Some((r.clone(), cred.get("about").cloned().unwrap_or(Value::Null)));
                    self.resolve_static(&r, cred.get("about"))?
                };
                Some((header, format!("{prefix}{value}")))
            }
            None => None,
        };
        // The client never follows a credentialed request: ureq strips by
        // header NAME, so any custom credential header would replay at
        // whatever host answers 3xx (and Authorization is dropped even
        // same-host). The host owns the follow decision — default manual,
        // an explicit count follows same-origin only with the header
        // re-attached per hop — ADR-011 §4.
        let (agent, mut hops_left) = if cred.is_some() {
            let agent = ureq::AgentBuilder::new().redirects(0).build();
            (agent, explicit_redirects.unwrap_or(0))
        } else {
            let agent = match explicit_redirects {
                Some(max) => ureq::AgentBuilder::new().redirects(max as u32).build(),
                None => ureq::agent(),
            };
            (agent, 0) // uncredentialed: the client follows internally
        };
        let mut cur_url = url;
        let mut cur_verb = verb;
        let mut with_body = true;
        let resp = loop {
            let mut req = agent
                .request(&cur_verb, &cur_url)
                .timeout(std::time::Duration::from_secs_f64(timeout));
            if let Some(headers) = payload.get("headers").and_then(|h| h.as_object()) {
                for (k, v) in headers {
                    if let Some(s) = v.as_str() {
                        req = req.set(k, s);
                    }
                }
            }
            if let Some((header, value)) = &cred {
                req = req.set(header, value);
            }
            let sent = if !with_body {
                req.call() // 301/302/303 hop downgraded to a bare GET
            } else if let Some(body) = payload.get("json").filter(|v| !v.is_null()) {
                req.set("Content-Type", "application/json")
                    .send_string(&crate::trace::canonical_json(body))
            } else if let Some(body) = payload.get("body").and_then(|b| b.as_str()) {
                req.send_string(body)
            } else {
                req.call()
            };
            let resp = match sent {
                Ok(r) => r,
                Err(ureq::Error::Status(_, r)) => r, // non-2xx is data, not error
                Err(e) => {
                    return Err(EffectFailure {
                        type_: "URLError".into(),
                        message: e.to_string(),
                    })
                }
            };
            if hops_left == 0 || !matches!(resp.status(), 301 | 302 | 303 | 307 | 308) {
                break resp;
            }
            let next = resp
                .header("location")
                .and_then(|loc| same_origin_target(&cur_url, loc));
            let Some(next) = next else {
                break resp; // cross-origin (or unparsable): the 3xx is data
            };
            self.secrets_read_guard(&next, payload)?; // guard every hop
            hops_left -= 1;
            if matches!(resp.status(), 301..=303) && cur_verb != "GET" && cur_verb != "HEAD" {
                cur_verb = "GET".to_string();
                with_body = false;
            }
            cur_url = next;
        };
        let status = resp.status();
        // ADR-021 §2: a 401 to a static-ref request means the stored
        // value is wrong — the host asks for a replacement, the same
        // way it asks for a missing one. 403 is left alone (scopes,
        // not the key); managed refs have their own reconsent path.
        if status == 401 {
            if let Some((r, about)) = &static_ref {
                if !self.missing_secrets.iter().any(|m| m == r) {
                    self.missing_secrets.push(r.clone());
                    if let Some(store) = &self.secret_store {
                        store.mark_rejected(r, about, &self.writer.run_id(), status);
                    }
                }
            }
        }
        let final_url = resp.get_url().to_string(); // post-redirect (ADR-008 §2)
        let headers: Map<String, Value> = resp
            .headers_names()
            .iter()
            .filter_map(|h| resp.header(h).map(|v| (h.to_lowercase(), json!(v))))
            .collect();
        // `response: "base64"` — raw bytes, base64 in `body` (ADR-020 §1);
        // default is text, as before. A multi-MB body spills to the blob
        // sidecar like any big output.
        let as_base64 = payload.get("response").and_then(|r| r.as_str()) == Some("base64");
        let mut out = json!({"status": status, "headers": headers, "url": final_url});
        if as_base64 {
            let mut bytes = Vec::new();
            resp.into_reader()
                .read_to_end(&mut bytes)
                .map_err(|e| EffectFailure {
                    type_: "URLError".into(),
                    message: e.to_string(),
                })?;
            use base64::Engine as _;
            out["body"] = json!(base64::engine::general_purpose::STANDARD.encode(&bytes));
            out["encoding"] = json!("base64");
        } else {
            out["body"] = json!(resp.into_string().map_err(|e| EffectFailure {
                type_: "URLError".into(),
                message: e.to_string(),
            })?);
        }
        Ok(out)
    }

    /// Refuse guest http requests that would touch stored secrets: a
    /// body whose `dataset` names `agent_secrets` (exact field match —
    /// covers query/subscribe/modify/aggregate/delete-records on ANY
    /// space, since the server only acts on a literal dataset name), or
    /// a url/body referencing the guarded object id (object-scoped
    /// routes on the home space's secrets object). Fired BEFORE
    /// execution, so the refusal is the recorded fact — deterministic
    /// on replay, and no secret ever reaches the trace. Content-based
    /// on purpose: host aliasing (localhost vs 127.0.0.1) can't dodge
    /// it, and a benign body that merely MENTIONS the words in text
    /// fields passes.
    fn secrets_read_guard(&self, url: &str, payload: &Value) -> Result<(), EffectFailure> {
        let forbidden = |what: &str| EffectFailure {
            type_: "forbidden".into(),
            message: format!(
                "{what} is host-only: secrets never enter guest code — connectors \
                 authenticate via credential: {{ref}}; keys are added/rotated via \
                 Help > Import connector keys (CLI: .connectors.env beside \
                 anybao.toml)"
            ),
        };
        let body = payload.get("json");
        let body_dataset = body.and_then(|b| b.get("dataset")).and_then(|d| d.as_str());
        if body_dataset == Some("agent_secrets") {
            return Err(forbidden("the agent_secrets dataset"));
        }
        if let Some(id) = &self.secrets_guard {
            let body_object = body
                .and_then(|b| b.get("objectId"))
                .and_then(|o| o.as_str());
            if url.contains(id.as_str()) || body_object == Some(id.as_str()) {
                return Err(forbidden("the secrets object"));
            }
        }
        Ok(())
    }

    fn sys_config_get(&self, payload: &Value) -> Result<Value, EffectFailure> {
        let key = payload.get("key").and_then(|k| k.as_str()).unwrap_or("");
        // secret refs are refused by NAMESPACE, not presence — oauth
        // values live in the managed state (ADR-011 §9) and static ones
        // in the store (ADR-021 §4), never in this map, so a presence
        // check could not cover them; the map is the seeds fallback
        if self.secrets.contains_key(key)
            || key.starts_with(OAUTH_REF_PREFIX)
            || key.starts_with("connector.key.")
            || key.starts_with("llm.key.")
        {
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

/// Resolve a redirect `location` against the current url, keeping it only
/// when the target stays on the SAME ORIGIN (scheme + host + port). A
/// credentialed request must never carry its header to another origin —
/// a cross-origin 3xx goes back to the guest as data (ADR-011 §4).
fn same_origin_target(current: &str, location: &str) -> Option<String> {
    let base = url::Url::parse(current).ok()?;
    let next = base.join(location).ok()?;
    (next.scheme() == base.scheme()
        && next.host_str() == base.host_str()
        && next.port_or_known_default() == base.port_or_known_default())
    .then(|| next.to_string())
}

pub(crate) fn urlencode(s: &str) -> String {
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

pub(crate) fn getrandom(buf: &mut [u8]) {
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
    fn secrets_guard_blocks_dataset_and_object() {
        let mut b = make_broker("run_guard");
        b.secrets_guard = Some("bafysecretsobj".into());

        // dataset reference in the body → refused (any space)
        let err = b
            .call(
                "http.post",
                json!({"url": "http://127.0.0.1:7001/v1/spaces/s/query",
                       "json": {"objectId": "whatever", "dataset": "agent_secrets"}}),
            )
            .unwrap_err();
        assert_eq!(err.type_, "forbidden");
        assert!(err.message.contains("credential"));

        // guarded object id in the url → refused
        let err = b
            .call(
                "http.get",
                json!({"url": "http://127.0.0.1:7001/v1/spaces/s/objects/bafysecretsobj/editor/markdown"}),
            )
            .unwrap_err();
        assert_eq!(err.type_, "forbidden");

        // guarded object id as the body objectId → refused
        let err = b
            .call(
                "http.post",
                json!({"url": "http://127.0.0.1:7001/v1/spaces/s/query",
                       "json": {"objectId": "bafysecretsobj", "dataset": "agent_config"}}),
            )
            .unwrap_err();
        assert_eq!(err.type_, "forbidden");

        // refusals are recorded facts (one effect record each)
        let denials = b
            .writer
            .records
            .iter()
            .filter(|r| {
                r.get("error")
                    .map(|e| e["type"] == "forbidden")
                    .unwrap_or(false)
            })
            .count();
        assert_eq!(denials, 3);
    }

    #[test]
    fn secrets_guard_ignores_benign_mentions() {
        let mut b = make_broker("run_guard_ok");
        b.secrets_guard = Some("bafysecretsobj".into());
        // a chat message TALKING about agent_secrets is not a read of it —
        // the words appear in a text field, not the dataset field. The
        // request fails at network level (no server), never at the guard.
        let err = b
            .call(
                "http.post",
                json!({"url": "http://127.0.0.1:1/v1/spaces/s/objects/o/chat/messages",
                       "json": {"text": "the agent_secrets dataset is guarded"},
                       "timeout": 0.05}),
            )
            .unwrap_err();
        assert_ne!(err.type_, "forbidden");
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

    // --- managed OAuth refs (ADR-011 §6) --------------------------------

    use crate::oauth::ProviderDescriptor;
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// Serve up to `n` requests off an ephemeral loopback port.
    fn fake_server<F>(n: usize, handler: F) -> String
    where
        F: Fn(tiny_http::Request) + Send + 'static,
    {
        let server = tiny_http::Server::http("127.0.0.1:0").expect("bind test server");
        let port = server.server_addr().to_ip().expect("ip addr").port();
        std::thread::spawn(move || {
            for _ in 0..n {
                let Ok(req) = server.recv() else { return };
                handler(req);
            }
        });
        format!("http://127.0.0.1:{port}")
    }

    fn token_endpoint(hits: Arc<AtomicUsize>, delay_ms: u64) -> String {
        let base = fake_server(2, move |req| {
            hits.fetch_add(1, Ordering::SeqCst);
            if delay_ms > 0 {
                std::thread::sleep(std::time::Duration::from_millis(delay_ms));
            }
            let body = json!({"access_token": "tok-access", "expires_in": 3600,
                              "scope": "s"});
            let _ = req.respond(tiny_http::Response::from_string(body.to_string()));
        });
        format!("{base}/token")
    }

    /// An API endpoint capturing every Authorization header it sees.
    fn api_endpoint(seen: Arc<Mutex<Vec<String>>>) -> String {
        fake_server(4, move |req| {
            let auth = req
                .headers()
                .iter()
                .find(|h| h.field.equiv("authorization"))
                .map(|h| h.value.as_str().to_string())
                .unwrap_or_default();
            seen.lock().unwrap().push(auth);
            let _ = req.respond(tiny_http::Response::from_string("{}"));
        })
    }

    fn oauth_state(token_url: &str, with_grant: bool) -> Arc<OauthState> {
        let mut providers = BTreeMap::new();
        providers.insert(
            "testprov".to_string(),
            ProviderDescriptor {
                authorize_url: "http://unused/auth".into(),
                token_url: token_url.into(),
                revoke_url: String::new(),
                auth_params: BTreeMap::new(),
                default_scopes: vec![],
                rotates_refresh_token: false,
                client_auth: "post_body".into(),
            },
        );
        let state = OauthState::new(providers, None);
        let mut seeds = BTreeMap::from([(
            "connector.oauth.testprov.client_id".to_string(),
            "cid".to_string(),
        )]);
        if with_grant {
            seeds.insert(
                "connector.oauth.testprov.refresh".to_string(),
                "refresh-secret".to_string(),
            );
        }
        state.seed(&mut seeds);
        Arc::new(state)
    }

    fn oauth_payload(api_url: &str) -> Value {
        json!({"url": format!("{api_url}/v1/x"), "credential":
            {"ref": "connector.oauth.testprov", "header": "Authorization",
             "prefix": "Bearer "}})
    }

    fn effect_names(b: &Broker) -> Vec<String> {
        b.writer
            .records
            .iter()
            .filter(|r| r["kind"] == "effect")
            .map(|r| r["effect"].as_str().unwrap_or("").to_string())
            .collect()
    }

    #[test]
    fn managed_ref_refreshes_at_injection() {
        let hits = Arc::new(AtomicUsize::new(0));
        let token_url = token_endpoint(hits.clone(), 0);
        let seen = Arc::new(Mutex::new(Vec::new()));
        let api_url = api_endpoint(seen.clone());
        let mut b = make_broker("run_oauth");
        b.oauth = Some(oauth_state(&token_url, true));

        let out = b.call("http.get", oauth_payload(&api_url)).unwrap();
        assert_eq!(out["status"], json!(200));
        assert_eq!(seen.lock().unwrap().as_slice(), ["Bearer tok-access"]);
        // the refresh is its own record, hosted + mutate, BEFORE the http one
        assert_eq!(effect_names(&b), ["oauth.refresh", "http.get"]);
        let refresh = b
            .writer
            .records
            .iter()
            .find(|r| r["effect"] == "oauth.refresh")
            .unwrap();
        assert_eq!(refresh["meta"]["hosted"], json!(true));
        assert_eq!(refresh["meta"]["class"], json!("mutate"));
        assert_eq!(
            refresh["meta"]["credential"]["ref"],
            json!("connector.oauth.testprov")
        );
        assert_eq!(refresh["output"]["ok"], json!(true));

        // cache hit: the second call adds only an http record
        b.call("http.get", oauth_payload(&api_url)).unwrap();
        assert_eq!(hits.load(Ordering::SeqCst), 1);
        assert_eq!(effect_names(&b), ["oauth.refresh", "http.get", "http.get"]);

        // no token material anywhere in the trace (structural redaction)
        let dump = serde_json::to_string(&b.writer.records).unwrap();
        assert!(!dump.contains("tok-access"));
        assert!(!dump.contains("refresh-secret"));
    }

    #[test]
    fn managed_refresh_is_single_flight_across_brokers() {
        let hits = Arc::new(AtomicUsize::new(0));
        let token_url = token_endpoint(hits.clone(), 250);
        let seen = Arc::new(Mutex::new(Vec::new()));
        let api_url = api_endpoint(seen.clone());
        let state = oauth_state(&token_url, true);

        let handles: Vec<_> = (0..2)
            .map(|i| {
                let state = state.clone();
                let api_url = api_url.clone();
                std::thread::spawn(move || {
                    let mut b = make_broker(&format!("run_sf{i}"));
                    b.oauth = Some(state);
                    b.call("http.get", oauth_payload(&api_url)).unwrap();
                })
            })
            .collect();
        for h in handles {
            h.join().unwrap();
        }
        // one exchange serves both runs (Google's live-token budget, §6)
        assert_eq!(hits.load(Ordering::SeqCst), 1);
        assert_eq!(seen.lock().unwrap().len(), 2);
    }

    #[test]
    fn replay_drains_hosted_refresh_records() {
        let hits = Arc::new(AtomicUsize::new(0));
        let token_url = token_endpoint(hits.clone(), 0);
        let seen = Arc::new(Mutex::new(Vec::new()));
        let api_url = api_endpoint(seen.clone());
        let mut b = make_broker("run_oauth_rec");
        b.oauth = Some(oauth_state(&token_url, true));
        let payload = oauth_payload(&api_url);
        b.call("http.get", payload.clone()).unwrap();

        // replay from the recorded trace: no oauth state, no exchange,
        // no store read — the hosted record drains, the http one matches
        let mut rb = make_broker("run_oauth_rep");
        rb.mode = Mode::Replay;
        rb.cursor = Some(ReplayCursor::new(&b.writer.records));
        let out = rb.call("http.get", payload).unwrap();
        assert_eq!(out["status"], json!(200));
        assert_eq!(effect_names(&rb), ["oauth.refresh", "http.get"]);
        let refresh = rb
            .writer
            .records
            .iter()
            .find(|r| r["effect"] == "oauth.refresh")
            .unwrap();
        assert_eq!(refresh["meta"]["mocked"], json!(true));
        assert_eq!(refresh["meta"]["hosted"], json!(true));
        assert_eq!(hits.load(Ordering::SeqCst), 1); // record run only
    }

    #[test]
    fn hosted_refresh_bypasses_grants_but_guest_calls_stay_gated() {
        let hits = Arc::new(AtomicUsize::new(0));
        let token_url = token_endpoint(hits.clone(), 0);
        let seen = Arc::new(Mutex::new(Vec::new()));
        let api_url = api_endpoint(seen.clone());
        let mut b = make_broker("run_oauth_caps");
        b.oauth = Some(oauth_state(&token_url, true));
        b.grants = Some(GrantSet::of(["net.http"]));

        // http is granted; the nested refresh needs no oauth.* grant
        b.call("http.get", oauth_payload(&api_url)).unwrap();
        assert_eq!(hits.load(Ordering::SeqCst), 1);

        // a direct guest call is still capability-gated (recorded denial)
        let err = b
            .call("oauth.refresh", json!({"provider": "testprov"}))
            .unwrap_err();
        assert_eq!(err.type_, "capability_denied");
    }

    #[test]
    fn managed_ref_errors_are_typed() {
        // guest-called refresh on a permissive broker: host-emitted only
        let mut b = make_broker("run_oauth_err");
        b.oauth = Some(oauth_state("http://127.0.0.1:1/token", true));
        let err = b
            .call("oauth.refresh", json!({"provider": "testprov"}))
            .unwrap_err();
        assert_eq!(err.type_, "host_only");

        // a stored sub-ref is not a provider handle — never injectable
        let err = b
            .call(
                "http.get",
                json!({"url": "http://127.0.0.1:1/x", "credential":
                    {"ref": "connector.oauth.testprov.refresh",
                     "header": "Authorization"}}),
            )
            .unwrap_err();
        assert_eq!(err.type_, "not_configured");
        assert!(!err.message.contains("refresh-secret"));

        // no grant stored → not_connected, and NO refresh record
        let mut b2 = make_broker("run_oauth_nogrant");
        b2.oauth = Some(oauth_state("http://127.0.0.1:1/token", false));
        let err = b2
            .call(
                "http.get",
                json!({"url": "http://127.0.0.1:1/x", "credential":
                    {"ref": "connector.oauth.testprov", "header": "Authorization"}}),
            )
            .unwrap_err();
        assert_eq!(err.type_, "not_connected");
        assert_eq!(effect_names(&b2), ["http.get"]);

        // config.get refuses the whole managed namespace (ADR-011 §9) —
        // the values live outside the broker map, so presence can't cover them
        let err = b
            .call(
                "config.get",
                json!({"key": "connector.oauth.testprov.refresh"}),
            )
            .unwrap_err();
        assert_eq!(err.type_, "ConfigError");
        assert!(err.message.contains("is a secret"));

        // the static-ref miss message stays byte-identical — six
        // connectors substring-match it (ADR-011 §7); the type is what
        // the runtime acts on (ADR-021 §2)
        let err = b
            .call(
                "http.get",
                json!({"url": "http://127.0.0.1:1/x", "credential":
                    {"ref": "connector.key.nope", "header": "Authorization"}}),
            )
            .unwrap_err();
        assert_eq!(err.type_, "SecretMissing");
        assert_eq!(
            err.message,
            "no secret for credential ref \"connector.key.nope\""
        );
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

    // --- credentialed redirects (ADR-011 §4, E8) -------------------------

    /// A server whose every request bumps `hits`; used as the redirect
    /// TARGET — the assertion is that it is never contacted.
    fn counting_target(hits: Arc<AtomicUsize>) -> String {
        fake_server(1, move |req| {
            hits.fetch_add(1, Ordering::SeqCst);
            let _ = req.respond(tiny_http::Response::from_string("leaked"));
        })
    }

    /// A server answering every request with a 302 to `location`.
    fn redirecting_origin(location: String) -> String {
        fake_server(2, move |req| {
            let resp = tiny_http::Response::from_string("")
                .with_status_code(302)
                .with_header(tiny_http::Header::from_bytes("Location", location.as_str()).unwrap());
            let _ = req.respond(resp);
        })
    }

    fn cred_payload(url: String) -> Value {
        json!({"url": url, "credential":
            {"ref": "connector.key.x", "header": "X-Api-Token"}})
    }

    /// ADR-021 §4: a store-backed static ref — the value never enters
    /// the seeded map, and a miss is typed + remembered with its `about`.
    struct MemStore {
        rows: BTreeMap<String, String>,
        missing: Mutex<Vec<(String, Value)>>,
    }
    impl SecretSource for MemStore {
        fn read(&self, key: &str) -> Result<Option<String>, String> {
            Ok(self.rows.get(key).cloned())
        }
        fn mark_missing(&self, key: &str, about: &Value, _run_id: &str) {
            self.missing
                .lock()
                .unwrap()
                .push((key.into(), about.clone()));
        }
        fn mark_rejected(&self, key: &str, about: &Value, _run_id: &str, status: u16) {
            self.missing
                .lock()
                .unwrap()
                .push((format!("{key}#{status}"), about.clone()));
        }
    }

    #[test]
    fn a_401_on_a_static_ref_marks_it_rejected() {
        let base = fake_server(1, move |req| {
            let _ = req.respond(tiny_http::Response::from_string("nope").with_status_code(401));
        });
        let mut b = make_broker("run_adr021_401");
        let store = Arc::new(MemStore {
            rows: [("connector.key.x".to_string(), "sk-wrong".to_string())].into(),
            missing: Mutex::new(Vec::new()),
        });
        b.secret_store = Some(store.clone());
        let out = b
            .call("http.get", cred_payload(format!("{base}/a")))
            .unwrap();
        assert_eq!(out["status"], json!(401));
        assert_eq!(b.missing_secrets, vec!["connector.key.x".to_string()]);
        assert_eq!(store.missing.lock().unwrap()[0].0, "connector.key.x#401");
    }

    #[test]
    fn static_ref_resolves_from_the_store_not_the_map() {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let seen2 = seen.clone();
        let base = fake_server(1, move |req| {
            let tok = req
                .headers()
                .iter()
                .find(|h| h.field.equiv("x-api-token"))
                .map(|h| h.value.as_str().to_string())
                .unwrap_or_default();
            seen2.lock().unwrap().push(tok);
            let _ = req.respond(tiny_http::Response::from_string("ok"));
        });
        let mut b = make_broker("run_adr021_store");
        let store = Arc::new(MemStore {
            rows: [("connector.key.x".to_string(), "sk-store".to_string())].into(),
            missing: Mutex::new(Vec::new()),
        });
        b.secret_store = Some(store.clone());
        let out = b
            .call("http.get", cred_payload(format!("{base}/a")))
            .unwrap();
        assert_eq!(out["status"], json!(200));
        assert_eq!(seen.lock().unwrap().as_slice(), ["sk-store"]);
        assert!(b.secrets.is_empty());
        assert!(b.missing_secrets.is_empty());
        let dump = serde_json::to_string(&b.writer.records).unwrap();
        assert!(!dump.contains("sk-store"));
    }

    #[test]
    fn missing_static_ref_is_typed_and_marked_with_its_descriptor() {
        let mut b = make_broker("run_adr021_miss");
        let store = Arc::new(MemStore {
            rows: BTreeMap::new(),
            missing: Mutex::new(Vec::new()),
        });
        b.secret_store = Some(store.clone());
        let mut payload = cred_payload("http://127.0.0.1:9/never".into());
        payload["credential"]["about"] = json!({"label": "X token", "hosts": ["api.x.test"]});
        let err = b.call("http.get", payload.clone()).unwrap_err();
        assert_eq!(err.type_, "SecretMissing");
        assert_eq!(
            err.message,
            "no secret for credential ref \"connector.key.x\""
        );
        // a second miss in the same run is not re-marked
        let _ = b.call("http.get", payload).unwrap_err();
        assert_eq!(b.missing_secrets, vec!["connector.key.x".to_string()]);
        let marked = store.missing.lock().unwrap();
        assert_eq!(marked.len(), 1);
        assert_eq!(marked[0].0, "connector.key.x");
        assert_eq!(marked[0].1["hosts"], json!(["api.x.test"]));
    }

    #[test]
    fn config_get_refuses_secret_namespaces_without_a_map() {
        let b = make_broker("run_adr021_cfg");
        let err = b
            .sys_config_get(&json!({"key": "connector.key.github"}))
            .unwrap_err();
        assert_eq!(err.type_, "ConfigError");
        let err = b
            .sys_config_get(&json!({"key": "llm.key.anthropic"}))
            .unwrap_err();
        assert_eq!(err.type_, "ConfigError");
    }

    #[test]
    fn credentialed_redirect_defaults_to_manual() {
        // the leak half: without an explicit count the 302 is DATA —
        // nothing follows, the credential header travels nowhere
        let hits = Arc::new(AtomicUsize::new(0));
        let target = counting_target(hits.clone());
        let origin = redirecting_origin(format!("{target}/steal"));
        let mut b = make_broker("run_e8_manual");
        b.secrets.insert("connector.key.x".into(), "sk-live".into());

        let out = b
            .call("http.get", cred_payload(format!("{origin}/a")))
            .unwrap();
        assert_eq!(out["status"], json!(302));
        assert_eq!(out["headers"]["location"], json!(format!("{target}/steal")));
        assert_eq!(hits.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn credentialed_redirect_never_crosses_origin() {
        // even an explicit count refuses a cross-origin hop: the 3xx
        // comes back as data and the foreign host sees no request
        let hits = Arc::new(AtomicUsize::new(0));
        let target = counting_target(hits.clone());
        let origin = redirecting_origin(format!("{target}/steal"));
        let mut b = make_broker("run_e8_cross");
        b.secrets.insert("connector.key.x".into(), "sk-live".into());

        let mut payload = cred_payload(format!("{origin}/a"));
        payload["redirects"] = json!(3);
        let out = b.call("http.get", payload).unwrap();
        assert_eq!(out["status"], json!(302));
        assert_eq!(hits.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn credentialed_redirect_follows_same_origin_reattaching_header() {
        // the breakage half: an explicit count follows same-origin hops
        // with the credential re-attached on every one (ureq's default
        // would strip Authorization; a custom header must not leak past
        // the origin — both replaced by the host-side loop)
        let seen = Arc::new(Mutex::new(Vec::new()));
        let seen2 = seen.clone();
        let base = fake_server(2, move |req| {
            let tok = req
                .headers()
                .iter()
                .find(|h| h.field.equiv("x-api-token"))
                .map(|h| h.value.as_str().to_string())
                .unwrap_or_default();
            seen2.lock().unwrap().push(format!("{} {tok}", req.url()));
            if req.url() == "/hop" {
                let resp = tiny_http::Response::from_string("")
                    .with_status_code(302)
                    .with_header(tiny_http::Header::from_bytes("Location", "/real").unwrap());
                let _ = req.respond(resp);
            } else {
                let _ = req.respond(tiny_http::Response::from_string("ok"));
            }
        });
        let mut b = make_broker("run_e8_follow");
        b.secrets.insert("connector.key.x".into(), "sk-live".into());

        let mut payload = cred_payload(format!("{base}/hop"));
        payload["redirects"] = json!(3);
        let out = b.call("http.get", payload).unwrap();
        assert_eq!(out["status"], json!(200));
        assert_eq!(out["body"], json!("ok"));
        assert!(out["url"].as_str().unwrap().ends_with("/real"));
        assert_eq!(
            seen.lock().unwrap().as_slice(),
            ["/hop sk-live", "/real sk-live"]
        );
        // the secret still never reaches the trace
        let dump = serde_json::to_string(&b.writer.records).unwrap();
        assert!(!dump.contains("sk-live"));
    }

    /// `response: "base64"` reads raw bytes (a PNG is not UTF-8) and
    /// hands them back base64 with `encoding` set — ADR-020 §1.
    #[test]
    fn http_get_base64_response() {
        let png_head: Vec<u8> = vec![0x89, b'P', b'N', b'G', 0xff, 0xfe, 0x00];
        let bytes = png_head.clone();
        let base = fake_server(1, move |req| {
            let resp = tiny_http::Response::from_data(bytes.clone())
                .with_header(tiny_http::Header::from_bytes("Content-Type", "image/png").unwrap());
            let _ = req.respond(resp);
        });
        let mut b = make_broker("run_b64");
        let out = b
            .call(
                "http.get",
                json!({"url": format!("{base}/f"), "response": "base64"}),
            )
            .unwrap();
        assert_eq!(out["status"], json!(200));
        assert_eq!(out["encoding"], json!("base64"));
        assert_eq!(out["headers"]["content-type"], json!("image/png"));
        use base64::Engine as _;
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(out["body"].as_str().unwrap())
            .unwrap();
        assert_eq!(decoded, png_head);
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
