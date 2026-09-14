//! The broker + syscall surface (ADR-002 thin host) — the one
//! pipeline for every effect call: classify → key → capability check →
//! replay/mock consult → execute → record (ADR-002 §2). The capability
//! RULE lives here — an optional `grants` set is consulted before
//! anything runs; denials are recorded (`error.type =
//! "capability_denied"`) and returned. No `grants` = the permissive
//! default profile. Grant POLICY lives in `caps` (harness-side data).

use crate::routes::Classifier;
use crate::trace::{input_key, TraceWriter};
use crate::tracestore::{valid_run_id, TraceStore};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::io::Read as _;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64};
use std::sync::{Arc, Mutex};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

// Trace-side replay machinery + grant policy ride under the broker
// module (record-mode main.rs keeps its module tree untouched; the
// replay/serve wiring lifts them to top-level modules when it lands).

use crate::caps::GrantSet;
use crate::oauth::{OauthState, OAUTH_REF_PREFIX};
use crate::replay::{
    never_mockable, resolve_blobs, DivergenceError, MockIndex, MockSpec, ReplayCursor, Unmatched,
};

pub type SharedMailbox = std::sync::Arc<std::sync::Mutex<std::collections::VecDeque<Value>>>;

/// Serve-shared presence state (ADR-025 §3): bao's authored status
/// line crosses the effect boundary (`bao.status`) into THIS slot —
/// serve is the sole publisher, folding the line into every beat and
/// republishing immediately on set. One per serve (threaded into
/// every Broker + the presence thread), like the mailbox.
pub type SharedPresence = Arc<PresenceState>;

/// The line drops out of beats this long after `set` (ADR-025 §3:
/// a forgotten update degrades to machine truth instead of lying).
pub const STATUS_LINE_TTL_S: f64 = 90.0;

/// Only the LINE lives here — `working`/`run` derive from
/// `RunCtx::live_runs`, the same registry `/break` resolves against
/// (one source of truth for what's running, never a parallel counter).
#[derive(Default)]
pub struct PresenceState {
    /// (line, unix seconds at set) — None = never set or cleared
    line: Mutex<Option<(String, f64)>>,
    /// bumped on every set/clear — the presence thread's wake signal
    line_gen: AtomicU64,
}

impl PresenceState {
    /// Store/clear the line (`""` clears) at `now` (unix seconds —
    /// passed in so decay is testable against a fake clock) and bump
    /// the generation so the presence thread republishes immediately.
    pub fn set_line(&self, line: &str, now: f64) {
        let mut slot = self.line.lock().expect("presence line lock poisoned");
        *slot = if line.is_empty() {
            None
        } else {
            Some((line.to_string(), now))
        };
        drop(slot);
        self.line_gen
            .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    }

    /// The line while fresh (ADR-025 §3 decay): None once older than
    /// `STATUS_LINE_TTL_S` or never set/cleared.
    pub fn line(&self, now: f64) -> Option<(String, f64)> {
        self.line
            .lock()
            .expect("presence line lock poisoned")
            .clone()
            .filter(|(_, at)| now - *at <= STATUS_LINE_TTL_S)
    }

    /// The generation counter — the presence thread's set-signal.
    pub fn line_gen(&self) -> u64 {
        self.line_gen.load(std::sync::atomic::Ordering::SeqCst)
    }
}

/// One run's live activity for presence beats (ADR-025 §1: `run.cells`
/// / `run.cell`): tool calls counted and previewed as their spans cross
/// the boundary. Created per run, shared between the run's Broker
/// (writer) and its `LiveRun` entry (the presence thread's reader) —
/// dropped with both, no cleanup path.
pub type SharedActivity = Arc<RunActivity>;

/// Preview budget for `run.cell` — one glanceable line, not the code.
const CELL_PREVIEW_CHARS: usize = 48;

#[derive(Default)]
pub struct RunActivity {
    /// Tool calls so far: `cell` + `bash` spans begun this run.
    cells: std::sync::atomic::AtomicUsize,
    /// Whitespace-collapsed head of the newest cell's code (bash: the
    /// command) — what bao is doing RIGHT NOW, truncated to a glance.
    preview: Mutex<Option<String>>,
}

impl RunActivity {
    /// A tool-call span began: count it and keep its preview.
    pub fn note_cell(&self, preview: Option<&str>) {
        self.cells.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let mut slot = self.preview.lock().expect("activity preview lock poisoned");
        *slot = preview.map(collapse_preview).filter(|p| !p.is_empty());
    }

    pub fn cells(&self) -> usize {
        self.cells.load(std::sync::atomic::Ordering::SeqCst)
    }

    pub fn preview(&self) -> Option<String> {
        self.preview
            .lock()
            .expect("activity preview lock poisoned")
            .clone()
    }
}

/// Collapse runs of whitespace and cut at the preview budget (on a char
/// boundary) — code arrives multi-line, the beat wants one short line.
fn collapse_preview(text: &str) -> String {
    let mut out = String::with_capacity(CELL_PREVIEW_CHARS + 1);
    let mut chars = 0usize;
    for word in text.split_whitespace() {
        if chars > 0 {
            out.push(' ');
            chars += 1;
        }
        for ch in word.chars() {
            if chars >= CELL_PREVIEW_CHARS {
                out.push('…');
                return out;
            }
            out.push(ch);
            chars += 1;
        }
    }
    out
}

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

struct SpanFrame {
    id: String,
    name: String,
    /// Guest-declared narrative kind (getter|mutator|setup|program),
    /// recorded on the end record's meta (ADR-001 §4d). None = undeclared.
    kind: Option<String>,
    t0: Instant,
    effects: u64,
    mutations: u64,
    /// effects inside this span served from a mock (ADR-028 §5: the
    /// digest's `(mocked)` / `(mixed …)` facade suffix reads this)
    mocked: u64,
    /// this span installed a span-scoped mock index (dropped on end)
    owns_mock: bool,
}

/// The secrets store as the broker sees it (ADR-021 §4): the
/// device-local `agent_secrets` row is the credential, read at
/// injection time — after the effect is recorded, so the trace stays
/// value-free and replay never reads it. `secrets` (the seeded map)
/// is the no-store fallback only.
/// The agent-config store (ADR-006 §3): the space's `agent_config`
/// dataset, one `{key, value}` row per dotted key. `config.get` reads
/// through to it on every call and `config.set` writes it — the host
/// holds no copy, so a row written by anyone (a cell, the UI, another
/// device) is live for the next cell. None = `anyrt run` (no space):
/// the broker's `config` seeds map stands in.
pub trait ConfigStore: Send + Sync {
    /// Err = the store could not be reached; Ok(None) = no row.
    fn read(&self, key: &str) -> Result<Option<Value>, String>;
    /// Err = the store could not be written (typed `ConfigError`).
    fn set(&self, key: &str, value: &Value) -> Result<(), String>;
}

pub trait SecretSource: Send + Sync {
    /// Err = the store could not be reached (a typed failure, not a
    /// miss); Ok(None) = no row / empty value.
    fn read(&self, key: &str) -> Result<Option<String>, String>;
    /// Stamp the ref's row `status: "missing"` + the descriptor the
    /// guest passed (`about`), so the run wrapper can post ONE request
    /// bubble and the dashboard can render it (ADR-021 §2).
    fn mark_missing(&self, key: &str, about: &Value, run_id: &str);
    /// The destination rejected the credential on a request carrying
    /// this ref (401, or Google's 400 `API_KEY_INVALID`): the stored
    /// value is wrong. Stamp `status: "rejected"` so the
    /// same request bubble asks for a replacement (ADR-021 §2).
    fn mark_rejected(&self, key: &str, about: &Value, run_id: &str, http_status: u16);
    /// The whole row (value + the binding the human saw — `hosts`,
    /// ADR-021 §7), one read per credentialed effect. Default: the
    /// value alone, for stores that keep no metadata.
    fn read_row(&self, key: &str) -> Result<Option<Value>, String> {
        Ok(self.read(key)?.map(|value| json!({"value": value})))
    }
    /// `lastUsedAt` — the audit stamp (ADR-021 §8.4), once per run per
    /// ref (the broker dedups). Default: nothing.
    fn mark_used(&self, _key: &str) {}
}

/// ADR-021 §8.1: the declared table — every ref a deployed overlay
/// module lists in `__any_credentials__`, read by serve from the
/// program objects' `credentials` property (spaces the guest cannot
/// write). `about(ref)` = its descriptor, the host's only source for
/// a declared ref's label and hosts; None = undeclared. An
/// implementation may refresh from the overlays on a miss (a redeploy
/// while serve runs).
pub trait DeclaredCredentials: Send + Sync {
    fn about(&self, key: &str) -> Option<Value>;
}

/// The open credential namespace (ADR-021 §8.1): any program may name
/// one; its descriptor is taken once, at row creation, hosts mandatory.
pub const LOCAL_KEY_PREFIX: &str = crate::deploy::LOCAL_KEY_PREFIX;

/// How a static ref resolves under the trust model (ADR-021 §8.1).
enum RefKind {
    /// Listed by a deployed overlay: descriptor = the declared one, the
    /// payload's `about` is ignored.
    Declared(Value),
    /// `local.key.*`, or an `llm.key.*` no overlay declares (a
    /// self-hosted backend): the payload's `about` describes it.
    Open,
    /// Outside the open namespaces and not declared: refused.
    Undeclared,
}

pub struct Broker {
    pub writer: TraceWriter,
    /// space-backed module resolution (serve); None = programs_dir only
    pub resolver: Option<Box<dyn crate::resolver::ModuleResolver + Send>>,
    pub current_cell: Option<String>,
    /// Offline agent-config seeds — `anyrt run` only (no space, no
    /// store). Serve passes an empty map: the store is the truth.
    pub config: BTreeMap<String, Value>,
    /// Runtime wiring the guest may read via `runtime.get`
    /// (`any.base_url`, `overlays.aliases`): derived from the runtime
    /// config per device, never agent config (ADR-006 §3).
    pub runtime: BTreeMap<String, Value>,
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
    /// Serve-shared presence state (ADR-025 §3) — the `bao.status`
    /// syscall's write target. None = this runtime does not publish
    /// presence (`anyrt run`, tests): the syscall fails typed
    /// `not_configured`, never a silent no-op.
    pub presence: Option<SharedPresence>,
    /// THIS run's live activity (ADR-025 §1: `run.cells`/`run.cell`) —
    /// tool-call spans note themselves here; the presence thread reads
    /// the same Arc off the run's `LiveRun`. None outside serve.
    pub activity: Option<SharedActivity>,
    /// Remaining fuel, refreshed by the runner's epoch callback every
    /// tick (host fns can't reach the store). Read by `fuel.state`;
    /// ≤EPOCH_TICK_MS stale — a checkpoint signal, not an exact meter.
    pub fuel_gauge: Arc<std::sync::atomic::AtomicU64>,
    /// Shared interrupt flag for the serve loop (exposed for wiring;
    /// not yet consulted by the pipeline — except by `sh.run`, which
    /// kills its child on it, ADR-024 §1).
    #[allow(dead_code)] // consulted by the serve wiring in main.rs (next round)
    pub interrupt: Arc<AtomicBool>,
    /// The running cell's wall deadline, set by the runner; `sh.run`
    /// clamps its timeout to it (ADR-024 §1). None = no cell budget.
    pub deadline: Option<Instant>,
    pub classifier: Classifier,
    /// Trace storage (ADR-001 §8) for the guest's past-run reads
    /// (`trace.effects_of(run=…)` & co., ADR-003 §4). None = offline
    /// broker with no store: `run=` fails typed.
    pub trace_store: Option<Arc<dyn TraceStore>>,
    /// Past runs loaded this broker's lifetime (blob-resolved) — a run
    /// is immutable once written, so one read per run suffices.
    run_cache: BTreeMap<String, Arc<Vec<Value>>>,
    pub mode: Mode,
    pub cursor: Option<ReplayCursor>,
    /// Run-level mock index (`anyrt run --mock`, ADR-028 §4); consulted
    /// under `Mode::Mock` when no span-scoped index is active.
    pub mock_index: Option<MockIndex>,
    /// Span-scoped indices (ADR-028 §3): installed by a `span.begin`
    /// carrying `input.mock`, dropped by its `span.end`; the innermost
    /// is consulted. Works in every mode — a mocked cell inside a live
    /// conversation is the point.
    span_mocks: Vec<MockIndex>,
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
    /// The secrets store's collection as resolved at boot
    /// (`AgentStores::secrets_ds`, ADR-027 §2) — refused in guest bodies
    /// next to the `_agent_secrets` suffix rule, so the guard does not
    /// rest on the server's collection naming alone.
    pub secrets_collection: Option<String>,
    /// Managed OAuth state (ADR-011) — process-shared; None = no oauth
    /// wiring (managed refs fail typed `not_configured`).
    pub oauth: Option<Arc<OauthState>>,
    /// The secrets store (ADR-021 §4); None = seeds only (`anyrt run`,
    /// or a server without the secrets object).
    pub secret_store: Option<Arc<dyn SecretSource>>,
    /// The config store (ADR-006 §3); None = `config.set` refused.
    pub config_store: Option<Arc<dyn ConfigStore>>,
    /// Static refs that need the human this run — resolved to nothing,
    /// or rejected (401) by their destination — in first-event order
    /// (ADR-021 §2); the run wrapper posts the request bubbles.
    pub missing_secrets: Vec<String>,
    /// The declared table (ADR-021 §8.1). None = no overlays behind this
    /// broker (`anyrt run`, tests): every ref is open and a ref with no
    /// known hosts is injected unbound — the seed file is the
    /// developer's explicit grant for that one-shot run (§7).
    pub declared: Option<Arc<dyn DeclaredCredentials>>,
    /// Refs injected at least once this run — `lastUsedAt` stamps once
    /// (ADR-021 §8.4).
    used_refs: BTreeSet<String>,
    /// Host-emit depth (ADR-011 §6): >0 while a syscall re-enters
    /// `call` to record its own nested effect (`oauth.refresh`).
    hosted: u32,
    span_stack: Vec<SpanFrame>,
    span_n: u64,
    resolve_cache: BTreeMap<String, Value>,
}

/// ADR-026 §3: the media types whose bodies come back as `body` text;
/// every other type is bytes → a raw blob ref.
fn is_text_media(mime: &str) -> bool {
    mime.starts_with("text/")
        || matches!(
            mime,
            "application/json"
                | "application/xml"
                | "application/x-www-form-urlencoded"
                | "application/javascript"
                | "application/x-ndjson"
        )
        || mime.ends_with("+json")
        || mime.ends_with("+xml")
}

/// `Content-Type` → the bare media type, lowercased; absent = unknown.
fn media_of(content_type: Option<&str>) -> Option<String> {
    content_type
        .map(|c| {
            c.split(';')
                .next()
                .unwrap_or("")
                .trim()
                .to_ascii_lowercase()
        })
        .filter(|m| !m.is_empty())
}

impl Broker {
    /// The bytes behind a raw ref (ADR-026 §3/§4) from this run's blob
    /// directory; a missing file is a typed failure.
    fn blob_bytes(&self, r: &Value) -> Result<Vec<u8>, EffectFailure> {
        let hash = r["__blob"].as_str().unwrap_or("");
        let missing = || EffectFailure {
            type_: "blob_missing".into(),
            message: format!("no blob {hash} in this bao's blob directory"),
        };
        let dir = self.writer.blob_dir.as_ref().ok_or_else(missing)?;
        dir.read(hash)
            .map_err(|e| EffectFailure {
                type_: "blob_missing".into(),
                message: format!("blob {hash}: {e}"),
            })?
            .ok_or_else(missing)
    }

    /// ADR-026 §3: a raw ref anywhere inside a `json` payload is sent
    /// as the base64 of its bytes (the provider wire for images and
    /// PDFs); the recorded input keeps the ref.
    fn expand_refs(&self, v: &Value) -> Result<Value, EffectFailure> {
        if crate::blob::is_raw_ref(v) {
            use base64::Engine as _;
            let bytes = self.blob_bytes(v)?;
            let b64 = base64::engine::general_purpose::STANDARD.encode(bytes);
            return Ok(Value::String(if v["encoding"] == "data-uri" {
                format!("data:{};base64,{b64}", v["mime"].as_str().unwrap_or(""))
            } else {
                b64
            }));
        }
        Ok(match v {
            Value::Object(m) => {
                let mut out = Map::new();
                for (k, x) in m {
                    out.insert(k.clone(), self.expand_refs(x)?);
                }
                Value::Object(out)
            }
            Value::Array(a) => Value::Array(
                a.iter()
                    .map(|x| self.expand_refs(x))
                    .collect::<Result<Vec<_>, _>>()?,
            ),
            other => other.clone(),
        })
    }

    /// `blob.read {hash, offset?, length?}` → `{data: base64, bytes}`
    /// from the directory (ADR-026 §4).
    fn sys_blob_read(&self, payload: &Value) -> Result<Value, EffectFailure> {
        let hash = payload.get("hash").and_then(|h| h.as_str()).unwrap_or("");
        let offset = payload.get("offset").and_then(|o| o.as_u64()).unwrap_or(0);
        let length = payload
            .get("length")
            .and_then(|l| l.as_u64())
            .unwrap_or(u64::MAX);
        let missing = || EffectFailure {
            type_: "blob_missing".into(),
            message: format!("no blob {hash} in this bao's blob directory"),
        };
        let dir = self.writer.blob_dir.as_ref().ok_or_else(missing)?;
        let bytes = dir
            .read_range(hash, offset, length)
            .map_err(|e| EffectFailure {
                type_: "blob_missing".into(),
                message: format!("blob {hash}: {e}"),
            })?
            .ok_or_else(missing)?;
        use base64::Engine as _;
        Ok(json!({
            "data": base64::engine::general_purpose::STANDARD.encode(&bytes),
            "bytes": bytes.len(),
        }))
    }

    /// `blob.put {data: base64, mime}` → the payload the record sees:
    /// `{ref}` (ADR-026 §4).
    fn materialize_put(&self, payload: &Value) -> Result<Value, EffectFailure> {
        use base64::Engine as _;
        let data = payload.get("data").and_then(|d| d.as_str()).unwrap_or("");
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(data)
            .map_err(|e| EffectFailure {
                type_: "TypeError".into(),
                message: format!("blob.put: data is not base64: {e}"),
            })?;
        let mime = payload
            .get("mime")
            .and_then(|m| m.as_str())
            .unwrap_or("application/octet-stream");
        Ok(json!({"ref": self.writer.put_raw(&bytes, mime)}))
    }

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
            runtime: BTreeMap::new(),
            secrets,
            env: BTreeMap::new(),
            programs_dir,
            mailbox: Arc::new(Mutex::new(VecDeque::new())),
            presence: None,
            activity: None,
            fuel_gauge: Arc::new(std::sync::atomic::AtomicU64::new(0)),
            interrupt: Arc::new(AtomicBool::new(false)),
            deadline: None,
            classifier,
            trace_store: None,
            run_cache: BTreeMap::new(),
            mode: Mode::Record,
            cursor: None,
            mock_index: None,
            span_mocks: Vec::new(),
            blobs: BTreeMap::new(),
            grants: None,
            secrets_guard: None,
            secrets_collection: None,
            oauth: None,
            secret_store: None,
            config_store: None,
            missing_secrets: Vec::new(),
            declared: None,
            used_refs: BTreeSet::new(),
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
        // A tool call is starting (the toolcaller wraps each in a `cell`
        // span; `bash` carries its command) — note it for presence beats
        // (ADR-025 §1: `run.cells`/`run.cell`).
        if name == "cell" || name == "bash" {
            if let Some(activity) = &self.activity {
                let preview = input
                    .get("preview")
                    .or_else(|| input.get("command"))
                    .and_then(|v| v.as_str());
                activity.note_cell(preview);
            }
        }
        // ADR-028 §3: a spec on the span input installs a mock index
        // for the span's lifetime — a bad spec fails the begin itself,
        // before any effect of the span runs. The recorded input keeps
        // the spec as given.
        let owns_mock = match input.get("mock") {
            Some(spec) if !spec.is_null() => {
                let idx = self
                    .build_mock_index(spec)
                    .map_err(|message| EffectFailure {
                        type_: "mock_spec".into(),
                        message,
                    })?;
                self.span_mocks.push(idx);
                true
            }
            _ => false,
        };
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
            mocked: 0,
            owns_mock,
        });
        Ok(sid)
    }

    /// A mock index from a spec value (ADR-028 §1): `from` runs load
    /// blob-resolved through the trace store (this run's own log when
    /// it names itself; the offline broker has no store → spec error).
    pub fn build_mock_index(&mut self, spec: &Value) -> Result<MockIndex, String> {
        let spec = MockSpec::parse(spec)?;
        let own = self.writer.run_id();
        let mut runs: BTreeMap<String, Arc<Vec<Value>>> = BTreeMap::new();
        for run in &spec.from {
            let records = if *run == own {
                Arc::new(self.writer.records.clone())
            } else {
                self.trace_records(&json!({"run": run}))
                    .map_err(|e| format!("mock.from {run}: {}", e.message))?
            };
            runs.insert(run.clone(), records);
        }
        MockIndex::build(spec, |run| Ok(runs[run].as_ref().clone()))
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
    ) -> Result<Value, EffectFailure> {
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
        // the owning span's end carries the filter hit counts (ADR-028
        // §5): a glob that matched nothing becomes a digest warning
        let filter = if top.owns_mock {
            self.span_mocks.pop().and_then(|idx| idx.filter_hits())
        } else {
            None
        };
        let cell = self.current_cell.clone();
        let mut meta = json!({
            "durMs": top.t0.elapsed().as_millis() as i64,
            "effects": top.effects, "mutations": top.mutations,
        });
        if top.mocked > 0 {
            meta["mocked"] = json!(top.mocked);
        }
        if let Some(f) = filter {
            meta["mockFilter"] = f;
        }
        if let Some(k) = &top.kind {
            // narrative label (ADR-001 §4d); mutations stays the oracle
            meta.as_object_mut()
                .unwrap()
                .insert("kind".into(), json!(k));
        }
        self.writer.span_end(
            &top.id,
            &top.name,
            cell.as_deref(),
            ok,
            output,
            error,
            meta.clone(),
        );
        Ok(meta)
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
        self.bump_mocked(class, false)
    }

    fn bump_mocked(&mut self, class: &str, mocked: bool) {
        for s in self.span_stack.iter_mut() {
            s.effects += 1;
            if class == "mutate" {
                s.mutations += 1;
            }
            if mocked {
                s.mocked += 1;
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
        // ADR-026 §4: `blob.put`'s normaliser materialises — the bytes go
        // to the directory here, the recorded input is the ref (its key
        // is content-addressed; replay re-mints the same ref).
        let payload = if name == "blob.put" {
            self.materialize_put(&payload)?
        } else {
            payload
        };
        let class = self.classify(name, &payload);
        let cap = self.cap_of(name, &payload);
        // ADR-024 §1: credential-looking `env` values on sh.* are masked
        // before recording (deterministic → the replay key matches)
        #[cfg(feature = "shell")]
        let canonical = crate::shell::redact_input(name, &payload);
        #[cfg(not(feature = "shell"))]
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
            let out = self.record_mocked(name, canonical, &key, span.as_deref(), &rec, meta)?;
            // ADR-026 §4: the record holds the ref, the directory the
            // bytes — a replayed read is served from the directory
            return if name == "blob.read" {
                self.execute(name, &payload)
            } else {
                Ok(out)
            };
        }

        // Loose mock consult (ADR-028 §1/§3): the innermost span-scoped
        // index, else the run-level one under Mode::Mock. Outside the
        // mockable set the call is plainly live (no meta.mock at all);
        // inside it a miss follows the spec's policy.
        let mut unmatched_live = false;
        if !never_mockable(name) {
            // the globs see the enclosing facades too (ADR-028 §1)
            let facades: Vec<&str> = self.span_stack.iter().map(|s| s.name.as_str()).collect();
            let active = match self.span_mocks.last_mut() {
                Some(idx) => Some(idx),
                None if self.mode == Mode::Mock => self.mock_index.as_mut(),
                None => None,
            };
            if let Some(idx) = active {
                if idx.decide(name, &facades).mockable() {
                    match idx.take(name, &key) {
                        Some(entry) => {
                            self.bump_mocked(class, true);
                            let mut meta = Map::new();
                            meta.insert("mocked".into(), json!(true));
                            meta.insert("class".into(), json!(class));
                            meta.insert("mock".into(), entry.provenance);
                            return self.record_mocked(
                                name,
                                canonical,
                                &key,
                                span.as_deref(),
                                &entry.rec,
                                meta,
                            );
                        }
                        None => match idx.spec.unmatched {
                            Unmatched::Fail => {
                                let message = format!("no mock for {name} (key {key})");
                                self.bump(class);
                                let cell = self.current_cell.clone();
                                self.writer.effect(
                                    name,
                                    cell.as_deref(),
                                    canonical,
                                    &key,
                                    None,
                                    Some(json!({"type": "mock_unmatched", "message": message})),
                                    json!({"mocked": false, "class": class,
                                           "mock": {"unmatched": true}}),
                                    span.as_deref(),
                                );
                                return Err(EffectFailure {
                                    type_: "mock_unmatched".into(),
                                    message,
                                });
                            }
                            // executed live, stamped: the traceDiff view (§6)
                            Unmatched::Live => unmatched_live = true,
                        },
                    }
                }
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
        if unmatched_live {
            meta.insert("mock".into(), json!({"unmatched": true}));
        }
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
                // ADR-026 §4: a blob read's bytes are never a record —
                // the ref in the input names them, the directory holds them
                let recorded = if name == "blob.read" {
                    json!({"bytes": output["bytes"]})
                } else {
                    output.clone()
                };
                self.writer.effect(
                    name,
                    cell.as_deref(),
                    canonical,
                    &key,
                    Some(recorded),
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
        #[cfg(feature = "shell")]
        if crate::shell::owns(name) {
            return crate::shell::classify(name);
        }
        match name {
            // ADR-011 §5: the token lifecycle mutates device state
            "oauth.connect" | "oauth.disconnect" | "oauth.refresh" => "mutate",
            // ADR-006 §3: a store row + the live map
            "config.set" => "mutate",
            // ADR-025 §3: a serve-state write folded into the next beat
            "bao.status" => "mutate",
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
            "config.set" => self.sys_config_set(payload),
            "runtime.get" => self.sys_runtime_get(payload),
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
            // ADR-025 §3: the status line crosses the effect boundary
            // into serve state — serve (sole publisher) folds it into
            // every beat and republishes immediately on set
            "bao.status" => self.sys_bao_status(payload),
            "batch" => self.sys_batch(payload),
            "trace.effects_of" => self.sys_effects_of(payload),
            "trace.effect_get" => self.sys_effect_get(payload),
            "trace.runs" => self.sys_trace_runs(payload),
            "trace.stats" => self.sys_trace_stats(payload),
            "trace.query" => self.sys_trace_query(payload),
            "blob.read" => self.sys_blob_read(payload),
            "blob.put" => Ok(payload.get("ref").cloned().unwrap_or(Value::Null)),
            "kernel.boot" => Ok(payload.clone()), // pins echo into the record
            // ADR-024: shell effects, only in a `--features shell` build
            #[cfg(feature = "shell")]
            n if crate::shell::owns(n) => crate::shell::execute(
                &crate::shell::Ctx::from_env(self.interrupt.clone(), self.deadline)
                    .with_blob_dir(self.writer.blob_dir.clone()),
                n,
                payload,
            ),
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

    /// Where a static ref's descriptor comes from (ADR-021 §8.1). With no
    /// declared table every ref is open (developer mode).
    fn ref_kind(&self, r: &str) -> RefKind {
        let Some(table) = &self.declared else {
            return RefKind::Open;
        };
        if let Some(about) = table.about(r) {
            return RefKind::Declared(about);
        }
        if r.starts_with(LOCAL_KEY_PREFIX) || r.starts_with("llm.key.") {
            RefKind::Open
        } else {
            RefKind::Undeclared
        }
    }

    /// A static ref (`connector.key.*`, `llm.key.*`, `local.key.*`): the
    /// store row if there is a store, else the seeded map (ADR-021 §4),
    /// under the trust model of ADR-021 §7/§8 — an undeclared ref is
    /// refused, an open ref needs a destination, and the value is
    /// injected only toward the hosts the human saw. A miss is typed
    /// `SecretMissing` — the message stays byte-identical to what
    /// connectors string-match — and is remembered for the run wrapper.
    /// Returns the value and the descriptor the store was stamped with
    /// (the declared one, or the payload's for an open ref).
    fn resolve_static(
        &mut self,
        r: &str,
        about: Option<&Value>,
        url: &str,
    ) -> Result<(String, Value), EffectFailure> {
        let guest_about = about.cloned().unwrap_or(Value::Null);
        let kind = self.ref_kind(r);
        let descriptor = match &kind {
            RefKind::Declared(a) => a.clone(),
            RefKind::Open => guest_about.clone(),
            RefKind::Undeclared => {
                return Err(EffectFailure {
                    type_: "secret_ref_undeclared".into(),
                    message: format!(
                        "credential ref {r:?} is not declared by any deployed connector — \
                         a program you write names its own key as `{LOCAL_KEY_PREFIX}<service>` \
                         (with about.hosts); connector.key.* belongs to the reviewed \
                         connectors (ADR-021 §8.1)"
                    ),
                })
            }
        };
        let row = match &self.secret_store {
            Some(store) => store.read_row(r).map_err(|e| EffectFailure {
                type_: "RuntimeError".into(),
                message: format!("secret store unreachable resolving {r:?}: {e}"),
            })?,
            None => None,
        };
        let value = row
            .as_ref()
            .and_then(|row| row.get("value"))
            .and_then(Value::as_str)
            .filter(|v| !v.is_empty())
            .map(str::to_string)
            .or_else(|| self.secrets.get(r).cloned());
        // the binding (§7): declared hosts, else the row's (frozen at
        // creation, human-edited), else the payload's — which is also what
        // a fresh open row is stamped with
        let declared_hosts = matches!(kind, RefKind::Declared(_))
            .then(|| hosts_of(&descriptor))
            .filter(|h| !h.is_empty());
        let hosts = declared_hosts
            .or_else(|| row.as_ref().map(hosts_of).filter(|h| !h.is_empty()))
            .unwrap_or_else(|| hosts_of(&guest_about));
        let strict = self.declared.is_some();
        if hosts.is_empty() && strict {
            // an open ref with no destination anywhere: no card, no
            // injection — the destination is what the human is asked to check
            return Err(EffectFailure {
                type_: "secret_hosts_required".into(),
                message: format!(
                    "credential ref {r:?} names no destination — pass about.hosts \
                     (the host[:port] the key is sent to) on the request, or set hosts \
                     for it in Credentials (ADR-021 §7)"
                ),
            });
        }
        let Some(value) = value else {
            if !self.missing_secrets.iter().any(|m| m == r) {
                self.missing_secrets.push(r.to_string());
                if let Some(store) = &self.secret_store {
                    store.mark_missing(r, &descriptor, &self.writer.run_id());
                }
            }
            return Err(EffectFailure {
                type_: "SecretMissing".into(),
                message: format!("no secret for credential ref {r:?}"),
            });
        };
        if !hosts.is_empty() && !host_allowed(url, &hosts) {
            return Err(EffectFailure {
                type_: "secret_host_mismatch".into(),
                message: format!(
                    "credential ref {r:?} is bound to {} — it is never sent to {} \
                     (ADR-021 §7; the human can edit the hosts in Credentials)",
                    hosts.join(", "),
                    url_host(url).unwrap_or_else(|| url.to_string())
                ),
            });
        }
        if self.used_refs.insert(r.to_string()) {
            if let Some(store) = &self.secret_store {
                store.mark_used(r);
            }
        }
        Ok((value, descriptor))
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
                    let value = self.resolve_managed(&r)?;
                    // a managed ref is bound when its connector declares
                    // the API hosts (ADR-021 §8.1, googleAuth); the token
                    // endpoint is host-side and never a guest destination
                    if let Some(hosts) = self
                        .declared
                        .as_ref()
                        .and_then(|d| d.about(&r))
                        .map(|a| hosts_of(&a))
                        .filter(|h| !h.is_empty())
                    {
                        if !host_allowed(&url, &hosts) {
                            return Err(EffectFailure {
                                type_: "secret_host_mismatch".into(),
                                message: format!(
                                    "credential ref {r:?} is bound to {} — it is never sent \
                                     to {} (ADR-021 §7)",
                                    hosts.join(", "),
                                    url_host(&url).unwrap_or_else(|| url.clone())
                                ),
                            });
                        }
                    }
                    value
                } else {
                    let (value, descriptor) = self.resolve_static(&r, cred.get("about"), &url)?;
                    static_ref = Some((r.clone(), descriptor));
                    value
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
        // the wire body (ADR-026 §3): `json` with refs expanded to base64,
        // a text `body`, or a raw ref as raw bytes + its mime
        enum Wire {
            None,
            Json(String),
            Text(String),
            Bytes(Vec<u8>, String),
        }
        let wire = if let Some(body) = payload.get("json").filter(|v| !v.is_null()) {
            Wire::Json(crate::trace::canonical_json(&self.expand_refs(body)?))
        } else if let Some(body) = payload.get("body").and_then(|b| b.as_str()) {
            Wire::Text(body.to_string())
        } else if let Some(r) = payload.get("body").filter(|b| crate::blob::is_raw_ref(b)) {
            let mime = r["mime"]
                .as_str()
                .unwrap_or("application/octet-stream")
                .to_string();
            Wire::Bytes(self.blob_bytes(r)?, mime)
        } else {
            Wire::None
        };
        let has_content_type = payload
            .get("headers")
            .and_then(|h| h.as_object())
            .is_some_and(|h| h.keys().any(|k| k.eq_ignore_ascii_case("content-type")));
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
            } else {
                match &wire {
                    Wire::Json(text) => req
                        .set("Content-Type", "application/json")
                        .send_string(text),
                    Wire::Text(text) => req.send_string(text),
                    Wire::Bytes(bytes, mime) => {
                        if !has_content_type {
                            req = req.set("Content-Type", mime);
                        }
                        req.send_bytes(bytes)
                    }
                    Wire::None => req.call(),
                }
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
        let final_url = resp.get_url().to_string(); // post-redirect (ADR-008 §2)
        let headers: Map<String, Value> = resp
            .headers_names()
            .iter()
            .filter_map(|h| resp.header(h).map(|v| (h.to_lowercase(), json!(v))))
            .collect();
        // ADR-026 §3: the host classifies the body — a text media type
        // that decodes as UTF-8 is `body` text (a `text/*` that does not
        // decodes lossily: text by declaration); anything else is bytes,
        // written to the blob directory and returned as a raw ref.
        // `response: "text"` forces text.
        let mut bytes = Vec::new();
        resp.into_reader()
            .read_to_end(&mut bytes)
            .map_err(|e| EffectFailure {
                type_: "URLError".into(),
                message: e.to_string(),
            })?;
        let force_text = payload.get("response").and_then(|r| r.as_str()) == Some("text");
        let media = media_of(headers.get("content-type").and_then(|v| v.as_str()));
        let declared_text = media.as_deref().is_some_and(is_text_media);
        let body = if force_text || media.as_deref().is_some_and(|m| m.starts_with("text/")) {
            Value::String(String::from_utf8_lossy(&bytes).into_owned())
        } else if declared_text || media.is_none() {
            match String::from_utf8(bytes) {
                Ok(text) => Value::String(text),
                Err(e) => self.writer.put_raw(
                    e.as_bytes(),
                    media.as_deref().unwrap_or("application/octet-stream"),
                ),
            }
        } else {
            self.writer.put_raw(
                &bytes,
                media.as_deref().unwrap_or("application/octet-stream"),
            )
        };
        let mut out = json!({"status": status, "headers": headers, "url": final_url});
        out["body"] = body;
        // ADR-021 §2: a rejection of a static-ref request means the
        // stored value is wrong — the host asks for a replacement, the
        // same way it asks for a missing one. 403 is left alone (scopes,
        // not the key); managed refs have their own reconsent path.
        if let Some((r, about)) = &static_ref {
            if credential_rejected(status, out["body"].as_str().unwrap_or(""))
                && !self.missing_secrets.iter().any(|m| m == r)
            {
                self.missing_secrets.push(r.clone());
                if let Some(store) = &self.secret_store {
                    store.mark_rejected(r, about, &self.writer.run_id(), status);
                }
            }
        }
        Ok(out)
    }

    /// Refuse guest http requests that would touch stored secrets: a
    /// body whose `dataset` is the secrets store's collection — every
    /// space's is `<typeId>_agent_secrets` (ADR-027 §2), so the
    /// `_agent_secrets` suffix is the invariant (ADR-011 §4); covers
    /// query/subscribe/modify/aggregate/delete-records on ANY space,
    /// since the server only acts on a literal collection name — or a
    /// url/body referencing the guarded object id (object-scoped
    /// routes on the home space's secrets object). Fired BEFORE
    /// execution, so the refusal is the recorded fact — deterministic
    /// on replay, and no secret ever reaches the trace. Content-based
    /// on purpose: host aliasing (localhost vs 127.0.0.1) can't dodge
    /// it, and a benign body that merely MENTIONS the words in text
    /// fields passes.
    fn secrets_read_guard(&self, url: &str, payload: &Value) -> Result<(), EffectFailure> {
        // ADR-021 §8.6: a credential card is the host's statement — a guest
        // chat write (any verb, any path under chat/messages, batch items
        // included since they re-enter `call`) carrying a host-only
        // attachment type is refused before it leaves
        if url.contains("/chat/messages") {
            let types = payload
                .get("json")
                .and_then(|b| b.get("attachments"))
                .and_then(Value::as_object)
                .into_iter()
                .flat_map(|atts| atts.values())
                .filter_map(|a| a.get("type").and_then(Value::as_str));
            for ty in types {
                if HOST_ONLY_ATTACHMENTS.contains(&ty) {
                    return Err(EffectFailure {
                        type_: "host_only".into(),
                        message: format!(
                            "credential cards are posted by the host — a `{ty}` attachment \
                             never comes from guest code (ADR-021 §8.6); a missing key posts \
                             its own card when a credentialed request misses it"
                        ),
                    });
                }
            }
        }
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
        if body_dataset.is_some_and(|d| {
            d.ends_with("_agent_secrets")
                || d == "agent_secrets"
                || self.secrets_collection.as_deref() == Some(d)
        }) {
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
        // ADR-023 §9: the trace collections are host-written. A guest may
        // read them (query / aggregate / get) and never write — insert,
        // upsert, update, delete, index or collection changes, and
        // aggregate sinks ($out/$merge) that name a trace collection.
        if let Some(rest) = url.split("/v1/local/").nth(1) {
            let op = rest.split('?').next().unwrap_or("");
            let coll = body
                .and_then(|b| b.get("coll"))
                .and_then(|c| c.get("name"))
                .and_then(|n| n.as_str())
                .unwrap_or("");
            let names_trace = coll.starts_with("trace_") || url.contains("name=trace_");
            let write_op = matches!(
                op,
                "insert" | "upsert" | "update" | "delete" | "indexes" | "collections"
            );
            if names_trace && write_op {
                return Err(EffectFailure {
                    type_: "forbidden".into(),
                    message: "the trace collections (trace_*) are host-written: read them \
                              with effects.of/get/runs/query, never write them"
                        .into(),
                });
            }
            if op == "aggregate" {
                if let Some(pipeline) = body.and_then(|b| b.get("pipeline")) {
                    if crate::tracestore::find_sink_stage(pipeline).is_some()
                        && pipeline.to_string().contains("_trace_")
                    {
                        return Err(EffectFailure {
                            type_: "forbidden".into(),
                            message: "aggregate sinks may not target a trace collection".into(),
                        });
                    }
                }
            }
        }
        Ok(())
    }

    /// Secret refs are refused by NAMESPACE, not presence — oauth
    /// values live in the managed state (ADR-011 §9) and static ones
    /// in the store (ADR-021 §4), never in this map, so a presence
    /// check could not cover them; the map is the seeds fallback.
    fn is_secret_key(&self, key: &str) -> bool {
        self.secrets.contains_key(key)
            || key.starts_with(OAUTH_REF_PREFIX)
            || key.starts_with("connector.key.")
            || key.starts_with("llm.key.")
    }

    fn sys_config_get(&self, payload: &Value) -> Result<Value, EffectFailure> {
        let key = payload.get("key").and_then(|k| k.as_str()).unwrap_or("");
        if self.is_secret_key(key) {
            return Err(EffectFailure {
                type_: "ConfigError".into(),
                message: format!("{key:?} is a secret — not readable from cells"),
            });
        }
        // read-through: the store row is the value (ADR-006 §3); the
        // seeds map answers only when there is no store (`anyrt run`)
        let found = match &self.config_store {
            Some(store) => store.read(key).map_err(|e| EffectFailure {
                type_: "ConfigError".into(),
                message: format!("config store unreachable for {key:?}: {e}"),
            })?,
            None => self.config.get(key).cloned(),
        };
        match found {
            Some(v) => Ok(json!({"value": v})),
            None => Err(EffectFailure {
                type_: "ConfigError".into(),
                message: format!("no config value for {key:?}"),
            }),
        }
    }

    /// `runtime.get {key}` → `{value}`: runtime wiring (`any.base_url`,
    /// `overlays.aliases`) — a different namespace from agent config,
    /// served from the runtime config, never the space.
    fn sys_runtime_get(&self, payload: &Value) -> Result<Value, EffectFailure> {
        let key = payload.get("key").and_then(|k| k.as_str()).unwrap_or("");
        match self.runtime.get(key) {
            Some(v) => Ok(json!({"value": v})),
            None => Err(EffectFailure {
                type_: "KeyError".into(),
                message: format!("no runtime value for {key:?}"),
            }),
        }
    }

    /// `config.set {key, value}` (ADR-006 §3): upsert the store row.
    /// Secret namespaces are refused (the credential flow owns them).
    fn sys_config_set(&mut self, payload: &Value) -> Result<Value, EffectFailure> {
        let key = payload
            .get("key")
            .and_then(|k| k.as_str())
            .filter(|k| !k.is_empty())
            .ok_or(EffectFailure {
                type_: "TypeError".into(),
                message: "config.set without key".into(),
            })?;
        let value = payload.get("value").cloned().ok_or(EffectFailure {
            type_: "TypeError".into(),
            message: "config.set without value".into(),
        })?;
        if self.is_secret_key(key) {
            return Err(EffectFailure {
                type_: "ConfigError".into(),
                message: format!("{key:?} is a secret — set it through the credential flow"),
            });
        }
        let Some(store) = &self.config_store else {
            return Err(EffectFailure {
                type_: "ConfigError".into(),
                message: "no config store — config.set needs serve or `run --from-space \
                          <bao space>`; a plain run reads its seeds only"
                    .into(),
            });
        };
        store.set(key, &value).map_err(|e| EffectFailure {
            type_: "ConfigError".into(),
            message: format!("could not persist {key:?}: {e}"),
        })?;
        Ok(json!({"ok": true}))
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

    /// `bao.status` (ADR-025 §3): store/clear bao's status line in the
    /// serve-shared presence slot. The guest never publishes events —
    /// this side of the effect boundary is a plain state write; the
    /// presence thread is the sole publisher.
    fn sys_bao_status(&mut self, payload: &Value) -> Result<Value, EffectFailure> {
        let presence = self.presence.clone().ok_or_else(|| EffectFailure {
            type_: "not_configured".into(),
            message: "bao status publishing is not wired into this runtime                       (serve only — `anyrt run` has no presence)"
                .into(),
        })?;
        let line = payload
            .get("line")
            .and_then(|v| v.as_str())
            .ok_or_else(|| EffectFailure {
                type_: "ValueError".into(),
                message: "line must be a string".into(),
            })?;
        let line = line.trim();
        let at = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs_f64();
        presence.set_line(line, at);
        Ok(json!({"ok": true, "line": line, "at": at}))
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

    /// The records a `trace.*` view reads: this run's live log, or —
    /// with `run` — a past run from the trace store (ADR-003 §4:
    /// `traceRef` / `lastRunRef` are guest-dereferenceable). Past runs
    /// come back blob-resolved; this run's records resolve per-record
    /// in `effect_get` (the writer's blobs live in `self.writer.blobs`).
    fn trace_records(&mut self, payload: &Value) -> Result<Arc<Vec<Value>>, EffectFailure> {
        let Some(run) = run_arg(payload)? else {
            return Ok(Arc::new(self.writer.records.clone()));
        };
        if run == self.writer.run_id() {
            return Ok(Arc::new(self.writer.records.clone()));
        }
        let run = run.as_str();
        if let Some(r) = self.run_cache.get(run) {
            return Ok(r.clone());
        }
        let Some(store) = self.trace_store.as_ref() else {
            return Err(EffectFailure {
                type_: "unavailable".into(),
                message: "no trace store on this broker (offline run)".into(),
            });
        };
        let records = store.load_resolved(run).map_err(|e| EffectFailure {
            type_: "KeyError".into(),
            message: format!("{e:#}"),
        })?;
        let records = Arc::new(records);
        self.run_cache.insert(run.to_string(), records.clone());
        Ok(records)
    }

    fn need_store(&self) -> Result<&dyn TraceStore, EffectFailure> {
        self.trace_store.as_deref().ok_or_else(|| EffectFailure {
            type_: "unavailable".into(),
            message: "no trace store on this broker (offline run)".into(),
        })
    }

    /// A cell's (or span's) IMMEDIATE children (ADR-001 §4d): bare effect
    /// records directly in the scope, PLUS child span-end records whose
    /// parent is the scope — so a composite tool call renders as one line,
    /// its inner effects reachable by drilling into the child span. A span
    /// nested one level down appears as a single row here, not its effects.
    /// With `run`, the same view over a past run: no cell/span = the run's
    /// root — its turns (`llm.chat` spans) and top-level effects.
    /// The run ROOT (no cell, no span) is spans-first: the boot's
    /// `kernel.boot` / `module.resolve` reads (~50 rows, 8 KB) are what
    /// every forensic walk had to wade through — they are hidden unless
    /// `all` is set; bare effects that mutated or failed stay visible.
    fn sys_effects_of(&mut self, payload: &Value) -> Result<Value, EffectFailure> {
        let records = self.trace_records(payload)?;
        let cell = payload.get("cell").and_then(|c| c.as_str());
        let span = payload.get("span").and_then(|s| s.as_str());
        let all = payload
            .get("all")
            .and_then(|a| a.as_bool())
            .unwrap_or(false);
        let mut rows = immediate_children(&records, cell, span);
        if cell.is_none() && span.is_none() && !all {
            rows.retain(|r| {
                r.get("effect").is_none() || r["class"] == "mutate" || !r["error"].is_null()
            });
        }
        Ok(json!({"records": rows}))
    }

    fn sys_effect_get(&mut self, payload: &Value) -> Result<Value, EffectFailure> {
        let seq = payload.get("seq").and_then(|s| s.as_i64()).unwrap_or(-1);
        let records = self.trace_records(payload)?;
        for r in records.iter() {
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

    /// `trace ls` as data (ADR-003 §4): `{program?, limit?}` → rows
    /// `{id, program, status, duration, turns, title, modifiedAt}`,
    /// newest first.
    /// With `filter`/`sort` (ADR-023 §5) the finder is an any-store
    /// query over the per-run summaries; `program` is sugar for a
    /// substring filter on the summary's program. A store without
    /// summaries (the in-memory test double) derives the rows from the logs, and
    /// then only `program`/`limit` apply.
    fn sys_trace_runs(&mut self, payload: &Value) -> Result<Value, EffectFailure> {
        let store = self.need_store()?;
        let program = payload.get("program").and_then(|p| p.as_str());
        let limit = payload.get("limit").and_then(|l| l.as_u64()).unwrap_or(50) as usize;
        let io = |e: anyhow::Error| EffectFailure {
            type_: "IOError".into(),
            message: format!("{e:#}"),
        };
        let mut filter = payload
            .get("filter")
            .cloned()
            .filter(|f| f.is_object())
            .unwrap_or_else(|| json!({}));
        if let Some(p) = program {
            filter["program"] = json!({"$regex": regex_escape(p)});
        }
        let sort = payload.get("sort").cloned().unwrap_or(Value::Null);
        let limit_q = if limit == 0 { 1000 } else { limit };
        if let Some(rows) = store.find_runs(&filter, &sort, limit_q).map_err(io)? {
            return Ok(json!({"runs": rows}));
        }
        if payload.get("filter").is_some() || !sort.is_null() {
            return Err(EffectFailure {
                type_: "unavailable".into(),
                message: "trace.runs filter/sort need the any trace store (this bao keeps traces in files)".into(),
            });
        }
        let rows = crate::view::list_data(store, program, limit).map_err(io)?;
        Ok(json!({"runs": rows}))
    }

    /// Read-only aggregation over a trace collection (ADR-023 §5).
    fn sys_trace_query(&mut self, payload: &Value) -> Result<Value, EffectFailure> {
        let store = self.need_store()?;
        let coll = payload
            .get("coll")
            .and_then(|c| c.as_str())
            .unwrap_or("records");
        let pipeline = payload.get("pipeline").cloned().unwrap_or(Value::Null);
        store.query(coll, &pipeline).map_err(|e| EffectFailure {
            type_: "ValueError".into(),
            message: format!("{e:#}"),
        })
    }

    /// `trace show --stats` as data (ADR-003 §4): `{run}` → `{run: {id,
    /// program, model, status, durationMs, fuel, error}, turns: [...],
    /// total}`.
    fn sys_trace_stats(&mut self, payload: &Value) -> Result<Value, EffectFailure> {
        let run = run_arg(payload)?.ok_or_else(|| EffectFailure {
            type_: "ValueError".into(),
            message: "trace.stats needs run=run_<id>".into(),
        })?;
        let store = self.need_store()?;
        crate::view::stats_data(store, &run).map_err(|e| EffectFailure {
            type_: "KeyError".into(),
            message: format!("{e:#}"),
        })
    }
}

/// Escape a literal for an any-store `$regex` (substring match).
fn regex_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        if r"\.+*?()[]{}|^$".contains(c) {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

/// The `run` argument of a `trace.*` view: absent/null = this run
/// (`None`); a valid run id = that run; anything else — a non-string,
/// or a string that isn't `run_<id>` — is a typed error. "Present but
/// wrong" must never fall through to the live log: the guest asked
/// for a specific run and would read the wrong one without noticing.
fn run_arg(payload: &Value) -> Result<Option<String>, EffectFailure> {
    match payload.get("run") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if valid_run_id(s) => Ok(Some(s.clone())),
        Some(Value::String(s)) => Err(EffectFailure {
            type_: "ValueError".into(),
            message: format!("not a run id: {s:?} (expected run_<id>)"),
        }),
        Some(other) => Err(EffectFailure {
            type_: "ValueError".into(),
            message: format!(
                "run must be a run id string (run_<id>), got {other} — pass the `traceRef` / `lastRunRef` value itself"
            ),
        }),
    }
}

/// The immediate-children view (ADR-001 §4d) over any record log.
/// "Directly in the scope": for a span query, the record's own span
/// (effects) / parent (spans) equals it; for a cell-only (or root)
/// query, that link is null. A span's parent lives on its BEGIN record
/// only (the end record repeats none of the begin's links), so span
/// rows — rendered from the end record — look their parent up by id.
fn immediate_children(records: &[Value], cell: Option<&str>, span: Option<&str>) -> Vec<Value> {
    let parents: BTreeMap<&str, &Value> = records
        .iter()
        .filter(|r| r["kind"] == "span" && r["phase"] == "begin")
        .filter_map(|r| Some((r["span"].as_str()?, &r["parent"])))
        .collect();
    let direct = |link: Option<&Value>| match span {
        Some(s) => link.map(|v| v == s).unwrap_or(false),
        None => link.is_none_or(|v| v.is_null()),
    };
    let parent_of = |r: &Value| -> Option<&Value> {
        r["span"].as_str().and_then(|id| parents.get(id).copied())
    };
    records
        .iter()
        .filter(|r| cell.is_none_or(|c| r["cell"] == c))
        .filter_map(|r| match r["kind"].as_str() {
            Some("effect") if direct(r.get("span")) => Some(json!({
                "seq": r["seq"], "effect": r["effect"],
                "class": r["meta"].get("class").cloned().unwrap_or(Value::Null),
                "mocked": r["meta"].get("mocked").cloned().unwrap_or(Value::Null),
                // executed live inside a mock's mockable set (ADR-028 §6)
                "unmatched": r["meta"]["mock"]["unmatched"] == true,
                "error": r["error"].get("type").cloned().unwrap_or(Value::Null),
                "span": r.get("span").cloned().unwrap_or(Value::Null),
            })),
            Some("span") if r["phase"] == "end" && direct(parent_of(r)) => {
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
                    // inner effects served from a mock (ADR-028 §5: the
                    // digest's (mocked) / (mixed …) facade suffix)
                    "mocked": r["meta"].get("mocked").cloned().unwrap_or(json!(0)),
                    "error": r["error"].get("type").cloned().unwrap_or(Value::Null),
                }))
            }
            _ => None,
        })
        .collect()
}

/// Resolve a redirect `location` against the current url, keeping it only
/// when the target stays on the SAME ORIGIN (scheme + host + port). A
/// credentialed request must never carry its header to another origin —
/// a cross-origin 3xx goes back to the guest as data (ADR-011 §4).
/// Chat attachment types only the host may author (ADR-021 §8.6).
const HOST_ONLY_ATTACHMENTS: [&str; 2] = ["credential_request", "credential_set"];

/// The `hosts` list of a descriptor or row (`about.hosts` / row
/// `hosts`): trimmed, lowercased, empties dropped.
fn hosts_of(v: &Value) -> Vec<String> {
    v.get("hosts")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(|s| s.trim().to_ascii_lowercase())
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_default()
}

/// `host[:port]` of a url as the binding compares it (port only when
/// explicit).
fn url_host(url: &str) -> Option<String> {
    let u = url::Url::parse(url).ok()?;
    let host = u.host_str()?.to_ascii_lowercase();
    Some(match u.port() {
        Some(p) => format!("{host}:{p}"),
        None => host,
    })
}

/// ADR-021 §7: the request's host is one of the binding's — exact host
/// (case-insensitive, no suffix matching), a declared port must match,
/// an undeclared one admits any.
fn host_allowed(url: &str, hosts: &[String]) -> bool {
    let Ok(u) = url::Url::parse(url) else {
        return false;
    };
    let Some(host) = u.host_str().map(|h| h.to_ascii_lowercase()) else {
        return false;
    };
    let port = u.port_or_known_default();
    hosts.iter().any(|allowed| {
        let (ah, ap) = match allowed.rsplit_once(':') {
            Some((h, p)) if p.chars().all(|c| c.is_ascii_digit()) && !h.is_empty() => {
                (h, p.parse::<u16>().ok())
            }
            _ => (allowed.as_str(), None),
        };
        ah == host && ap.is_none_or(|p| Some(p) == port)
    })
}

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
    // uuid's rng is already OS-backed; reuse it (oauth's PKCE verifier)
    for chunk in buf.chunks_mut(16) {
        let bytes = *uuid::Uuid::new_v4().as_bytes();
        chunk.copy_from_slice(&bytes[..chunk.len()]);
    }
}

/// Did the destination reject the credential itself (ADR-021 §2)?
/// 401 is the HTTP convention; Google APIs answer a bad key with
/// `400 {"error": {"status": "INVALID_ARGUMENT", "details": [{"reason":
/// "API_KEY_INVALID"}]}}`, so a 400 whose body names that reason counts
/// too; Anthropic answers a key that is not scoped to one workspace
/// with `400 … anthropic-workspace-id is required when authenticating
/// with an identity-linked API key` — bao never sends that header, so
/// the key itself is the wrong kind and the card asks for a
/// workspace-scoped one. Anything else (403 scopes, 400 bad request)
/// is not the key.
fn credential_rejected(status: u16, body: &str) -> bool {
    status == 401
        || (status == 400
            && (body.contains("API_KEY_INVALID")
                || body.contains("anthropic-workspace-id is required")))
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

    // --- span-scoped mocks (ADR-028 §3/§5/§8) ------------------------------------

    fn span_with_mock(b: &mut Broker, spec: Value) -> String {
        b.try_span_begin("cell", None, json!({"cell": "c1", "mock": spec}))
            .unwrap()
    }

    #[test]
    fn span_mock_serves_inline_records_and_drops_on_end() {
        let mut b = make_broker("sm1");
        span_with_mock(
            &mut b,
            json!({"records": [{"effect": "time.now", "output": {"epoch": 1.0}}]}),
        );
        let out = b.call("time.now", json!({})).unwrap();
        assert_eq!(out, json!({"epoch": 1.0}));
        let rec = b.writer.records.last().unwrap().clone();
        assert_eq!(rec["meta"]["mocked"], true);
        assert_eq!(rec["meta"]["mock"], json!({"inline": 0}));
        // consumed: the same call inside the span now misses → fail, recorded
        let err = b.call("time.now", json!({})).unwrap_err();
        assert_eq!(err.type_, "mock_unmatched");
        assert_eq!(
            b.writer.records.last().unwrap()["meta"]["mock"]["unmatched"],
            true
        );
        // kernel plumbing is never consulted, even under `fail` (§7)
        let out = b.call("kernel.boot", json!({"pin": 1})).unwrap();
        assert_eq!(out, json!({"pin": 1}));
        assert!(b.writer.records.last().unwrap()["meta"]
            .get("mock")
            .is_none());
        b.span_end(true, None, None).unwrap();
        let end = b.writer.records.last().unwrap().clone();
        assert_eq!(end["kind"], "span");
        assert_eq!(end["meta"]["mocked"], 1); // one served of three effects
        assert_eq!(end["meta"]["effects"], 3);
        // the spec is recorded as given, on the begin record
        let begin = b
            .writer
            .records
            .iter()
            .find(|r| r["kind"] == "span" && r["phase"] == "begin")
            .unwrap();
        assert_eq!(begin["input"]["mock"]["records"][0]["effect"], "time.now");
        // outside the span: live, no meta.mock at all
        let out = b.call("time.now", json!({})).unwrap();
        assert!(out["epoch"].as_f64().unwrap() > 1.0);
        let rec = b.writer.records.last().unwrap();
        assert_eq!(rec["meta"]["mocked"], false);
        assert!(rec["meta"].get("mock").is_none());
    }

    #[test]
    fn span_mock_only_filter_leaves_the_rest_live_and_nested_spans_inherit() {
        let mut b = make_broker("sm2");
        span_with_mock(
            &mut b,
            json!({"only": ["http.*"],
                   "records": [{"effect": "http.get", "output": {"status": 200}, "repeat": true}]}),
        );
        // outside the mockable set: plainly live, unmatched policy irrelevant
        let out = b.call("time.now", json!({})).unwrap();
        assert!(out["epoch"].as_f64().unwrap() > 1.0);
        assert!(b.writer.records.last().unwrap()["meta"]
            .get("mock")
            .is_none());
        // a nested span without its own spec inherits the enclosing index
        b.try_span_begin("any.query", None, json!({"q": 1}))
            .unwrap();
        let out = b.call("http.get", json!({"url": "https://x"})).unwrap();
        assert_eq!(out["status"], 200);
        b.span_end(true, None, None).unwrap();
        let inner = b.writer.records.last().unwrap().clone();
        assert_eq!(inner["meta"]["mocked"], 1);
        // the nested end did not drop the outer index
        let out = b.call("http.get", json!({"url": "https://y"})).unwrap();
        assert_eq!(out["status"], 200);
        b.span_end(true, None, None).unwrap();
        assert!(b.span_mocks.is_empty());
    }

    #[test]
    fn span_mock_globs_match_enclosing_facades_and_count_hits() {
        // F2 (questionary 09-14): `except: ["any.*"]` must mean the any
        // server's http traffic — effects under any.* facades
        let mut b = make_broker("sm4");
        span_with_mock(
            &mut b,
            json!({"except": ["any.*"],
                   "records": [{"effect": "http.get", "output": {"status": 200}, "repeat": true}]}),
        );
        // bare http.get (httpbin): mocked
        let out = b
            .call("http.get", json!({"url": "https://httpbin.org/uuid"}))
            .unwrap();
        assert_eq!(out["status"], 200);
        assert_eq!(b.writer.records.last().unwrap()["meta"]["mocked"], true);
        // the same effect under an any.* facade: excepted → runs live
        // (offline broker: the live http fails, which proves it was not served)
        b.try_span_begin("any.list_types", None, json!({})).unwrap();
        let err = b
            .call("http.get", json!({"url": "http://127.0.0.1:1/v1/types"}))
            .unwrap_err();
        assert_ne!(err.type_, "mock_unmatched");
        let rec = b.writer.records.last().unwrap();
        assert_eq!(rec["meta"]["mocked"], false);
        assert!(rec["meta"].get("mock").is_none());
        b.span_end(true, None, None).unwrap();
        let end = b.span_end(true, None, None).unwrap();
        assert_eq!(end["mockFilter"], json!({"except": 1}));

        // a glob that matches nothing: counted as zero → the digest warns
        let mut b = make_broker("sm5");
        span_with_mock(&mut b, json!({"only": ["any.*"], "unmatched": "live"}));
        let out = b.call("time.now", json!({})).unwrap();
        assert!(out["epoch"].is_number());
        assert!(b.writer.records.last().unwrap()["meta"]
            .get("mock")
            .is_none());
        let end = b.span_end(true, None, None).unwrap();
        assert_eq!(end["mockFilter"], json!({"only": 0}));
        // no filter in the spec → no counts on the record
        let mut b = make_broker("sm6");
        span_with_mock(&mut b, json!({"unmatched": "live"}));
        let end = b.span_end(true, None, None).unwrap();
        assert!(end.get("mockFilter").is_none());
    }

    #[test]
    fn span_mock_live_policy_stamps_tracediff_and_bad_spec_fails_begin() {
        let mut b = make_broker("sm3");
        span_with_mock(&mut b, json!({"unmatched": "live"}));
        let out = b.call("uuid4", json!({})).unwrap();
        assert!(out["hex"].is_string());
        let rec = b.writer.records.last().unwrap();
        assert_eq!(rec["meta"]["mocked"], false);
        assert_eq!(rec["meta"]["mock"], json!({"unmatched": true}));
        b.span_end(true, None, None).unwrap();

        let err = b
            .try_span_begin("cell", None, json!({"mock": {"from": "not-a-run"}}))
            .unwrap_err();
        assert_eq!(err.type_, "mock_spec");
        assert!(b.span_mocks.is_empty());
        assert!(b.span_stack.is_empty()); // the begin never opened
                                          // an offline broker cannot load another run
        let err = b
            .try_span_begin("cell", None, json!({"mock": "run_0123456789abcdef"}))
            .unwrap_err();
        assert_eq!(err.type_, "mock_spec");
        assert!(err.message.contains("no trace store"));
    }

    #[test]
    fn span_mock_from_this_run_replays_its_own_records() {
        let mut b = make_broker("run_cccccccccccccccc");
        let first = b.call("uuid4", json!({})).unwrap();
        span_with_mock(&mut b, json!({"from": "run_cccccccccccccccc"}));
        let out = b.call("uuid4", json!({})).unwrap();
        assert_eq!(out, first); // the recorded value, not a fresh one
        let rec = b.writer.records.last().unwrap();
        assert_eq!(rec["meta"]["mock"]["from"], "run_cccccccccccccccc");
        assert_eq!(rec["meta"]["mock"]["seq"], b.writer.records[1]["seq"]);
        b.span_end(true, None, None).unwrap();
    }

    // --- bao.status syscall + PresenceState (ADR-025 §3) -----------------------

    #[test]
    fn presence_line_set_clear_and_gen() {
        let p = PresenceState::default();
        assert_eq!(p.line_gen(), 0);
        assert_eq!(p.line(0.0), None);
        p.set_line("migrating the mail dataset", 100.0);
        assert_eq!(p.line_gen(), 1);
        assert_eq!(
            p.line(100.0).unwrap(),
            ("migrating the mail dataset".into(), 100.0)
        );
        // "" clears — the beat drops the line again
        p.set_line("", 101.0);
        assert_eq!(p.line_gen(), 2);
        assert_eq!(p.line(101.0), None);
    }

    #[test]
    fn presence_line_decays_after_ttl() {
        let p = PresenceState::default();
        p.set_line("working on BOB-73", 100.0);
        let at = 100.0;
        // fresh at the TTL edge, gone past it (ADR-025 §3: 90s decay)
        assert!(p.line(at + STATUS_LINE_TTL_S).is_some());
        assert_eq!(p.line(at + STATUS_LINE_TTL_S + 0.001), None);
    }

    #[test]
    fn bao_status_writes_the_shared_slot() {
        let mut b = make_broker("run_status");
        let slot: SharedPresence = Default::default();
        b.presence = Some(slot.clone());
        let before = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs_f64();
        let out = b
            .call("bao.status", json!({"line": "  wiring serve beats  "}))
            .unwrap();
        assert_eq!(out["ok"], json!(true));
        assert_eq!(out["line"], "wiring serve beats");
        let at = out["at"].as_f64().unwrap();
        assert!(at >= before && at <= before + 5.0);
        assert_eq!(slot.line(at).unwrap().0, "wiring serve beats");
        // the syscall classifies mutate — the trace records it as one
        assert!(b.call("bao.status", json!({"line": "x"})).is_ok());
    }

    #[test]
    fn bao_status_fails_typed_without_presence() {
        // no-backward-compat rule: `anyrt run` (no presence wiring)
        // fails loudly, never a silent no-op
        let mut b = make_broker("run_no_status");
        let err = b.call("bao.status", json!({"line": "x"})).unwrap_err();
        assert_eq!(err.type_, "not_configured");
    }

    #[test]
    fn bao_status_rejects_non_string_line() {
        let mut b = make_broker("run_bad_status");
        b.presence = Some(Default::default());
        let err = b.call("bao.status", json!({"line": 3})).unwrap_err();
        assert_eq!(err.type_, "ValueError");
    }

    // --- run activity for presence beats (ADR-025 §1 run.cells/run.cell) ------

    #[test]
    fn tool_call_spans_note_run_activity() {
        let mut b = make_broker("run_act");
        let act: SharedActivity = Default::default();
        b.activity = Some(act.clone());
        b.try_span_begin("cell", None, json!({"cell": "c1", "preview": "x = 1"}))
            .unwrap();
        b.span_end(true, None, None).unwrap();
        // bash tool calls count too — their preview is the command
        b.try_span_begin(
            "bash",
            None,
            json!({"cell": "c2", "command": "ls   -la\n /tmp"}),
        )
        .unwrap();
        b.span_end(true, None, None).unwrap();
        // a non-tool span (an @span facade, llm.chat) is not a tool call
        b.try_span_begin("llm.chat", None, json!({"turn": 1}))
            .unwrap();
        assert_eq!(act.cells(), 2);
        assert_eq!(act.preview().unwrap(), "ls -la /tmp");
    }

    #[test]
    fn activity_preview_collapses_and_truncates() {
        let a = RunActivity::default();
        a.note_cell(Some(&"word ".repeat(40)));
        let p = a.preview().unwrap();
        assert!(p.chars().count() <= 49, "48 chars + ellipsis at most");
        assert!(p.ends_with('…'));
        // a whitespace-only cell counts but shows no preview
        a.note_cell(Some("   "));
        assert_eq!(a.preview(), None);
        assert_eq!(a.cells(), 2);
    }

    #[test]
    fn secrets_guard_blocks_dataset_and_object() {
        let mut b = make_broker("run_guard");
        b.secrets_guard = Some("bafysecretsobj".into());
        b.secrets_collection = Some("resolved-at-boot".into());

        // the secrets collection in the body → refused: the one resolved
        // at boot, or any `<typeId>_agent_secrets` by the naming rule
        for ds in [
            "bafytype_agent_secrets",
            "agent_secrets",
            "resolved-at-boot",
        ] {
            let err = b
                .call(
                    "http.post",
                    json!({"url": "http://127.0.0.1:7001/v1/spaces/s/query",
                           "json": {"objectId": "whatever", "dataset": ds}}),
                )
                .unwrap_err();
            assert_eq!(err.type_, "forbidden");
            assert!(err.message.contains("credential"));
        }

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
        assert_eq!(denials, 5);
    }

    #[test]
    fn trace_collections_are_host_written() {
        // ADR-023 §9: guest writes naming a trace_* local collection are refused
        let mut b = make_broker("run_fence");
        let coll = json!({"scope": "space", "spaceId": "sp", "name": "trace_records"});
        for (op, body) in [
            ("insert", json!({"coll": coll, "docs": [{"id": "x"}]})),
            ("upsert", json!({"coll": coll, "docs": [{"id": "x"}]})),
            ("delete", json!({"coll": coll, "filter": {}})),
            (
                "update",
                json!({"coll": coll, "id": "x", "modifier": {"$set": {"a": 1}}}),
            ),
            ("indexes", json!({"coll": coll, "drop": ["name"]})),
        ] {
            let e = b
                .call(
                    "http.post",
                    json!({"url": format!("http://any.local:8080/v1/local/{op}"), "json": body}),
                )
                .unwrap_err();
            assert_eq!(e.type_, "forbidden", "{op}");
        }
        let e = b
            .call(
                "http.delete",
                json!({"url": "http://any.local:8080/v1/local/collections?scope=space&spaceId=sp&name=trace_runs"}),
            )
            .unwrap_err();
        assert_eq!(e.type_, "forbidden");
        let e = b
            .call(
                "http.post",
                json!({"url": "http://any.local:8080/v1/local/aggregate",
                       "json": {"coll": {"scope": "space", "spaceId": "sp", "name": "scratch"},
                                "pipeline": [{"$match": {}}, {"$out": "l_s_sp_trace_runs"}]}}),
            )
            .unwrap_err();
        assert_eq!(e.type_, "forbidden");
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

    /// ADR-026 §4 on the fs surface: `fs.read(encoding="blob")` puts
    /// the file in the run's directory and records a ref (stamped in
    /// `blobs`, class read); `fs.write` with that ref streams the bytes
    /// out (class mutate, the ref stays in the record); replay serves
    /// both from the trace without touching the disk.
    #[cfg(feature = "shell")]
    #[test]
    fn fs_blob_legs_record_refs_and_replay_from_trace() {
        let dir = tempfile::tempdir().unwrap();
        let src = dir.path().join("in.bin");
        let bytes = b"\x89PNG\r\n\x1a\n";
        std::fs::write(&src, bytes).unwrap();
        let dst = dir.path().join("out/copy.bin");
        let (srcs, dsts) = (
            src.to_string_lossy().into_owned(),
            dst.to_string_lossy().into_owned(),
        );
        let mut rec = make_broker("fsblob");
        rec.writer.blob_dir = Some(crate::blob::BlobDir::new(&dir.path().join("traces")));
        rec.current_cell = Some("main".into());
        let out = rec
            .call(
                "fs.read",
                json!({"path": srcs, "encoding": "blob", "mime": "image/png"}),
            )
            .unwrap();
        let r = out["blob"].clone();
        assert!(crate::blob::is_raw_ref(&r), "{out}");
        let hash = r["__blob"].as_str().unwrap().to_string();
        assert_eq!(hash, crate::blob::hash_of(bytes));
        let record = rec.writer.records.last().unwrap();
        assert_eq!(record["effect"], "fs.read");
        assert_eq!(record["meta"]["class"], "read");
        assert_eq!(record["blobs"], json!([hash]));
        let out = rec
            .call(
                "fs.write",
                json!({"path": dsts, "content": r, "mkdirs": true}),
            )
            .unwrap();
        assert_eq!(out["bytes"], bytes.len());
        assert_eq!(std::fs::read(&dst).unwrap(), bytes);
        let record = rec.writer.records.last().unwrap();
        assert_eq!(record["meta"]["class"], "mutate");
        assert_eq!(record["input"]["content"], r); // the ref, never the bytes
        assert_eq!(record["blobs"], json!([hash]));
        rec.try_cell_done("main", true, None, false, json!({}))
            .unwrap();

        std::fs::remove_file(&dst).unwrap();
        let mut rb = make_broker("fsblob2");
        rb.mode = Mode::Replay;
        rb.cursor = Some(ReplayCursor::new(&rec.writer.records));
        rb.current_cell = Some("main".into());
        let out = rb
            .call(
                "fs.read",
                json!({"path": srcs, "encoding": "blob", "mime": "image/png"}),
            )
            .unwrap();
        assert_eq!(out["blob"], r);
        let out = rb
            .call(
                "fs.write",
                json!({"path": dsts, "content": r, "mkdirs": true}),
            )
            .unwrap();
        assert_eq!(out["bytes"], bytes.len());
        assert!(!dst.exists()); // served from the trace, never re-run
    }

    /// ADR-024 §1: every sh.* is mutate and replay never re-runs a
    /// command — the recorded result comes back even when the command
    /// would produce something else now.
    #[cfg(feature = "shell")]
    #[test]
    fn shell_effects_classify_and_replay_from_trace() {
        let mut rec = make_broker("sh1");
        rec.current_cell = Some("main".into());
        let out = rec
            .call(
                "sh.run",
                json!({"cmd": "echo recorded", "env": {"MY_TOKEN": "s3cret"}}),
            )
            .unwrap();
        assert_eq!(out["stdout"], "recorded\n");
        let r = &rec.writer.records[1]; // [0] = run header
        assert_eq!(r["effect"], "sh.run");
        assert_eq!(r["meta"]["class"], "mutate");
        assert_eq!(r["input"]["env"]["MY_TOKEN"], "***"); // masked before recording
        assert!(!crate::trace::canonical_json(r).contains("s3cret"));
        rec.call("fs.list", json!({"path": "."})).unwrap();
        assert_eq!(rec.writer.records[2]["meta"]["class"], "read");
        rec.try_cell_done("main", true, None, false, json!({}))
            .unwrap();

        let mut rb = make_broker("sh2");
        rb.mode = Mode::Replay;
        rb.cursor = Some(ReplayCursor::new(&rec.writer.records));
        rb.current_cell = Some("main".into());
        // same guest payload (raw secret) → same masked key → served
        // from the trace; `exit 7` would fail if it actually ran
        let out = rb
            .call(
                "sh.run",
                json!({"cmd": "echo recorded", "env": {"MY_TOKEN": "s3cret"}}),
            )
            .unwrap();
        assert_eq!(out["stdout"], "recorded\n");
        assert_eq!(out["exit"], 0);
        assert_eq!(rb.writer.records[1]["meta"]["mocked"], true);
        rb.call("fs.list", json!({"path": "."})).unwrap();
        let err = rb.call("sh.run", json!({"cmd": "exit 7"})).unwrap_err();
        assert_eq!(err.type_, "DivergenceError"); // not in the trace → never executed
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
        assert_eq!(b_fail.writer.records[1]["meta"]["mock"]["from"], "rec");
        let err = b_fail
            .call("http.get", json!({"url": "https://new"}))
            .unwrap_err();
        assert_eq!(err.type_, "mock_unmatched");
        // an unmatched fail IS recorded (ADR-028 §8): an error record,
        // traceDiff-marked
        assert_eq!(b_fail.writer.records.len(), 3);
        assert_eq!(b_fail.writer.records[2]["error"]["type"], "mock_unmatched");
        assert_eq!(b_fail.writer.records[2]["meta"]["mock"]["unmatched"], true);

        let mut b_live = make_broker("m2");
        b_live.mode = Mode::Mock;
        let mut idx = MockIndex::new(&w1.records);
        idx.spec.unmatched = Unmatched::Live;
        b_live.mock_index = Some(idx);
        // unmatched → executed live → traceDiff material
        let out = b_live.call("uuid4", json!({})).unwrap();
        assert!(out["hex"].is_string());
        assert_eq!(b_live.writer.records[1]["meta"]["mocked"], false);
        assert_eq!(b_live.writer.records[1]["meta"]["mock"]["unmatched"], true);
        // kernel plumbing: plainly live even under mock mode (§7)
        let out = b_live.call("kernel.boot", json!({"pin": 1})).unwrap();
        assert_eq!(out, json!({"pin": 1}));
        assert!(b_live.writer.records[2]["meta"].get("mock").is_none());
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
    fn trace_views_read_a_past_run_through_the_store() {
        // ADR-003 §4: `run=` dereferences a traceRef — the same three
        // views over a persisted run, root-level = its turns.
        let store = Arc::new(crate::tracestore::MemTraceStore::new());
        // write a run: one llm.chat turn wrapping one cell span with an effect
        let mut past = make_broker("run_past");
        past.trace_store = Some(store.clone());
        let turn = past
            .try_span_begin("llm.chat", None, json!({"turn": 1}))
            .unwrap();
        past.current_cell = Some("c1".into());
        let cell = past
            .try_span_begin("cell", None, json!({"cell": "c1"}))
            .unwrap();
        past.call("kernel.boot", json!({"x": 1})).unwrap();
        past.span_end(true, None, None).unwrap();
        past.current_cell = None;
        past.span_end(true, Some(json!({"stop": "done"})), None)
            .unwrap();
        past.writer.dump(store.as_ref()).unwrap();

        let mut b = make_broker("run_now");
        b.trace_store = Some(store.clone());
        // root of the past run: the turn span row only
        let root = b
            .call("trace.effects_of", json!({"run": "run_past"}))
            .unwrap();
        let rows = root["records"].as_array().unwrap();
        assert_eq!(rows.len(), 1, "{root}");
        assert_eq!(rows[0]["name"], "llm.chat");
        // spans-first root: the bare boot read (kernel.boot at top level)
        // is hidden unless all=true
        let mut past2 = make_broker("run_past2");
        past2.trace_store = Some(store.clone());
        past2.call("kernel.boot", json!({"top": 1})).unwrap();
        past2.try_span_begin("llm.chat", None, json!({})).unwrap();
        past2.span_end(true, None, None).unwrap();
        past2.writer.dump(store.as_ref()).unwrap();
        let root2 = b
            .call("trace.effects_of", json!({"run": "run_past2"}))
            .unwrap();
        assert_eq!(root2["records"].as_array().unwrap().len(), 1, "{root2}");
        let root2_all = b
            .call("trace.effects_of", json!({"run": "run_past2", "all": true}))
            .unwrap();
        assert_eq!(
            root2_all["records"].as_array().unwrap().len(),
            2,
            "{root2_all}"
        );
        assert_eq!(rows[0]["span"], turn);
        // drill: turn → cell span → the effect
        let cells = b
            .call("trace.effects_of", json!({"run": "run_past", "span": turn}))
            .unwrap();
        assert_eq!(cells["records"][0]["name"], "cell");
        assert_eq!(cells["records"][0]["span"], cell);
        let effs = b
            .call("trace.effects_of", json!({"run": "run_past", "span": cell}))
            .unwrap();
        assert_eq!(effs["records"][0]["effect"], "kernel.boot");
        let seq = effs["records"][0]["seq"].as_i64().unwrap();
        let rec = b
            .call("trace.effect_get", json!({"run": "run_past", "seq": seq}))
            .unwrap();
        assert_eq!(rec["input"], json!({"x": 1}));
        // runs + stats
        let runs = b.call("trace.runs", json!({})).unwrap();
        assert!(runs["runs"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["id"] == "run_past"));
        let stats = b.call("trace.stats", json!({"run": "run_past"})).unwrap();
        assert_eq!(stats["run"]["id"], "run_past");
        assert_eq!(stats["turns"].as_array().unwrap().len(), 1);
        assert_eq!(stats["turns"][0]["stop"], "done");
        assert_eq!(stats["turns"][0]["cells"], 1);
        // guards: not-a-run-id, unknown run, no store
        let bad = b.call("trace.effects_of", json!({"run": "../etc"}));
        assert_eq!(bad.unwrap_err().type_, "ValueError");
        // present-but-not-a-string must NOT fall through to the live run
        for bad_run in [json!({"id": "run_past"}), json!(42), json!(["run_past"])] {
            for view in ["trace.effects_of", "trace.effect_get", "trace.stats"] {
                let e = b.call(view, json!({"run": bad_run, "seq": 1})).unwrap_err();
                assert_eq!(e.type_, "ValueError", "{view} {bad_run}");
            }
        }
        // null = this run, like absent
        let live = b.call("trace.effects_of", json!({"run": null})).unwrap();
        assert!(live["records"].as_array().is_some());
        let missing = b.call("trace.effects_of", json!({"run": "run_nope"}));
        assert_eq!(missing.unwrap_err().type_, "KeyError");
        let mut offline = make_broker("run_off");
        let off = offline.call("trace.effects_of", json!({"run": "run_past"}));
        assert_eq!(off.unwrap_err().type_, "unavailable");
        // and the reads themselves are recorded effects of THIS run
        assert!(b.writer.records.iter().any(|r| r["effect"] == "trace.runs"));
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
        /// the row's `hosts` (ADR-021 §7) for every key, when set
        hosts: Mutex<Option<Value>>,
        used: Mutex<Vec<String>>,
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
        fn read_row(&self, key: &str) -> Result<Option<Value>, String> {
            let Some(value) = self.rows.get(key) else {
                return Ok(None);
            };
            let mut row = json!({"key": key, "value": value});
            if let Some(h) = self.hosts.lock().unwrap().clone() {
                row["hosts"] = h;
            }
            Ok(Some(row))
        }
        fn mark_used(&self, key: &str) {
            self.used.lock().unwrap().push(key.into());
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
            hosts: Mutex::new(None),
            used: Mutex::new(Vec::new()),
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
    fn anthropic_identity_linked_key_400_marks_it_rejected() {
        // a personal key created without a workspace: the API wants a
        // header bao never sends, so the stored key is the wrong kind
        let body = r#"{"type":"error","error":{"type":"invalid_request_error","message":"anthropic-workspace-id is required when authenticating with an identity-linked API key; send the id of the workspace this request acts in."}}"#;
        let base = fake_server(1, move |req| {
            let _ = req.respond(tiny_http::Response::from_string(body).with_status_code(400));
        });
        let mut b = make_broker("run_adr021_wrkspc");
        let store = Arc::new(MemStore {
            rows: [("connector.key.x".to_string(), "sk-ant-unscoped".to_string())].into(),
            missing: Mutex::new(Vec::new()),
            hosts: Mutex::new(None),
            used: Mutex::new(Vec::new()),
        });
        b.secret_store = Some(store.clone());
        let out = b
            .call("http.get", cred_payload(format!("{base}/a")))
            .unwrap();
        assert_eq!(out["status"], json!(400));
        assert_eq!(store.missing.lock().unwrap()[0].0, "connector.key.x#400");
        // an ordinary 400 is not the key
        assert!(!credential_rejected(
            400,
            r#"{"error":"max_tokens too large"}"#
        ));
    }

    #[test]
    fn a_google_400_api_key_invalid_marks_it_rejected() {
        let base = fake_server(1, move |req| {
            let body = r#"{"error":{"code":400,"status":"INVALID_ARGUMENT","details":[{"reason":"API_KEY_INVALID"}]}}"#;
            let _ = req.respond(tiny_http::Response::from_string(body).with_status_code(400));
        });
        let mut b = make_broker("run_adr021_400");
        let store = Arc::new(MemStore {
            rows: [("connector.key.x".to_string(), "sk-wrong".to_string())].into(),
            missing: Mutex::new(Vec::new()),
            hosts: Mutex::new(None),
            used: Mutex::new(Vec::new()),
        });
        b.secret_store = Some(store.clone());
        let out = b
            .call("http.get", cred_payload(format!("{base}/a")))
            .unwrap();
        assert_eq!(out["status"], json!(400));
        assert_eq!(store.missing.lock().unwrap()[0].0, "connector.key.x#400");
    }

    #[test]
    fn a_plain_400_is_not_a_rejection() {
        let base = fake_server(1, move |req| {
            let _ = req.respond(tiny_http::Response::from_string("bad json").with_status_code(400));
        });
        let mut b = make_broker("run_adr021_400_plain");
        let store = Arc::new(MemStore {
            rows: [("connector.key.x".to_string(), "sk-ok".to_string())].into(),
            missing: Mutex::new(Vec::new()),
            hosts: Mutex::new(None),
            used: Mutex::new(Vec::new()),
        });
        b.secret_store = Some(store.clone());
        b.call("http.get", cred_payload(format!("{base}/a")))
            .unwrap();
        assert!(b.missing_secrets.is_empty());
        assert!(store.missing.lock().unwrap().is_empty());
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
            hosts: Mutex::new(None),
            used: Mutex::new(Vec::new()),
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
            hosts: Mutex::new(None),
            used: Mutex::new(Vec::new()),
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

    /// ADR-021 §8.1: a declared table over a fixed map.
    struct Table(BTreeMap<String, Value>);
    impl DeclaredCredentials for Table {
        fn about(&self, key: &str) -> Option<Value> {
            self.0.get(key).cloned()
        }
    }
    fn declared_github() -> Arc<Table> {
        Arc::new(Table(
            [(
                "connector.key.github".to_string(),
                json!({"label": "GitHub token", "hosts": ["api.github.com"]}),
            )]
            .into(),
        ))
    }

    #[test]
    fn host_binding_compares_exact_host_and_optional_port() {
        let h = |s: &[&str]| s.iter().map(|x| x.to_string()).collect::<Vec<_>>();
        assert!(host_allowed(
            "https://api.github.com/repos",
            &h(&["api.github.com"])
        ));
        assert!(host_allowed(
            "https://API.GitHub.com/x",
            &h(&["api.github.com"])
        ));
        assert!(!host_allowed(
            "https://api.github.com.evil.io/x",
            &h(&["api.github.com"])
        ));
        assert!(!host_allowed(
            "https://evil.io/api.github.com",
            &h(&["api.github.com"])
        ));
        assert!(host_allowed(
            "http://127.0.0.1:8737/meetings",
            &h(&["127.0.0.1:8737"])
        ));
        assert!(!host_allowed(
            "http://127.0.0.1:9999/meetings",
            &h(&["127.0.0.1:8737"])
        ));
        assert!(host_allowed(
            "http://127.0.0.1:9999/meetings",
            &h(&["127.0.0.1"])
        ));
        assert!(host_allowed("https://api.x.test/", &h(&["api.x.test:443"])));
        assert!(!host_allowed("not a url", &h(&["api.x.test"])));
    }

    #[test]
    fn undeclared_ref_is_refused_under_a_declared_table() {
        let mut b = make_broker("run_adr021_undeclared");
        b.declared = Some(declared_github());
        b.secrets.insert("connector.key.bogus".into(), "sk".into());
        let err = b
            .call(
                "http.get",
                json!({"url": "http://127.0.0.1:1/x", "credential":
                    {"ref": "connector.key.bogus", "header": "Authorization"}}),
            )
            .unwrap_err();
        assert_eq!(err.type_, "secret_ref_undeclared");
        assert!(err.message.contains("local.key.<service>"));
        // never marked missing — the ref does not exist as a card
        assert!(b.missing_secrets.is_empty());
    }

    #[test]
    fn declared_ref_ignores_guest_about_and_binds_to_declared_hosts() {
        let seen = Arc::new(Mutex::new(Vec::<String>::new()));
        let seen2 = seen.clone();
        let base = fake_server(1, move |req| {
            let tok = req
                .headers()
                .iter()
                .find(|h| h.field.equiv("authorization"))
                .map(|h| h.value.as_str().to_string())
                .unwrap_or_default();
            seen2.lock().unwrap().push(tok);
            let _ = req.respond(tiny_http::Response::from_string("ok"));
        });
        let mut b = make_broker("run_adr021_bound");
        b.declared = Some(declared_github());
        b.secrets
            .insert("connector.key.github".into(), "ghp_live".into());
        // a guest `about` claiming the local server as a host is ignored:
        // the declared binding says api.github.com, the request goes to
        // 127.0.0.1 — refused before the header is attached
        let err = b
            .call(
                "http.get",
                json!({"url": format!("{base}/exfil"), "credential":
                    {"ref": "connector.key.github", "header": "Authorization",
                     "about": {"label": "GitHub", "hosts": ["127.0.0.1"]}}}),
            )
            .unwrap_err();
        assert_eq!(err.type_, "secret_host_mismatch");
        assert!(err.message.contains("api.github.com"));
        assert!(seen.lock().unwrap().is_empty(), "nothing was sent");
        // the refusal is the recorded fact: no value in the trace
        let dump = serde_json::to_string(&b.writer.records).unwrap();
        assert!(!dump.contains("ghp_live"));
    }

    #[test]
    fn open_ref_needs_hosts_then_binds_to_its_row_and_stamps_used_once() {
        let seen = Arc::new(Mutex::new(Vec::<String>::new()));
        let seen2 = seen.clone();
        let base = fake_server(2, move |req| {
            let tok = req
                .headers()
                .iter()
                .find(|h| h.field.equiv("authorization"))
                .map(|h| h.value.as_str().to_string())
                .unwrap_or_default();
            seen2.lock().unwrap().push(tok);
            let _ = req.respond(tiny_http::Response::from_string("ok"));
        });
        let port = url::Url::parse(&base).unwrap().port().unwrap();
        let mut b = make_broker("run_adr021_open");
        b.declared = Some(declared_github());
        // no hosts anywhere → typed, no card, no injection
        let err = b
            .call(
                "http.get",
                json!({"url": format!("{base}/m"), "credential":
                    {"ref": "local.key.anyscribe", "header": "Authorization"}}),
            )
            .unwrap_err();
        assert_eq!(err.type_, "secret_hosts_required");
        assert!(b.missing_secrets.is_empty());
        // the row carries the binding the human saw; the payload's hosts
        // are irrelevant once a row exists
        let store = Arc::new(MemStore {
            rows: [("local.key.anyscribe".to_string(), "asc_live".to_string())].into(),
            missing: Mutex::new(Vec::new()),
            hosts: Mutex::new(None),
            used: Mutex::new(Vec::new()),
        });
        b.secret_store = Some(store.clone());
        *store.hosts.lock().unwrap() = Some(json!([format!("127.0.0.1:{port}")]));
        let ok = b
            .call(
                "http.get",
                json!({"url": format!("{base}/m"), "credential":
                    {"ref": "local.key.anyscribe", "header": "Authorization", "prefix": "Bearer ",
                     "about": {"label": "x", "hosts": ["evil.example"]}}}),
            )
            .unwrap();
        assert_eq!(ok["status"], json!(200));
        let _ = b
            .call(
                "http.get",
                json!({"url": format!("{base}/m2"), "credential":
                    {"ref": "local.key.anyscribe", "header": "Authorization", "prefix": "Bearer "}}),
            )
            .unwrap();
        assert_eq!(
            seen.lock().unwrap().as_slice(),
            ["Bearer asc_live", "Bearer asc_live"]
        );
        assert_eq!(
            *store.used.lock().unwrap(),
            vec!["local.key.anyscribe".to_string()]
        );
        // another host than the row's → refused
        let err = b
            .call(
                "http.get",
                json!({"url": "https://evil.example/m", "credential":
                    {"ref": "local.key.anyscribe", "header": "Authorization"}}),
            )
            .unwrap_err();
        assert_eq!(err.type_, "secret_host_mismatch");
    }

    #[test]
    fn open_ref_miss_is_marked_with_the_payload_descriptor_and_posts_a_card() {
        let mut b = make_broker("run_adr021_open_miss");
        b.declared = Some(declared_github());
        let store = Arc::new(MemStore {
            rows: BTreeMap::new(),
            missing: Mutex::new(Vec::new()),
            hosts: Mutex::new(None),
            used: Mutex::new(Vec::new()),
        });
        b.secret_store = Some(store.clone());
        let err = b
            .call(
                "http.get",
                json!({"url": "http://127.0.0.1:8737/m", "credential":
                    {"ref": "local.key.anyscribe", "header": "Authorization",
                     "about": {"label": "Anyscribe token", "hosts": ["127.0.0.1:8737"]}}}),
            )
            .unwrap_err();
        assert_eq!(err.type_, "SecretMissing");
        assert_eq!(b.missing_secrets, vec!["local.key.anyscribe".to_string()]);
        let marked = store.missing.lock().unwrap();
        assert_eq!(marked[0].1["hosts"], json!(["127.0.0.1:8737"]));
        // an undeclared llm ref (a self-hosted backend) is open too
        drop(marked);
        let err = b
            .call(
                "http.get",
                json!({"url": "http://127.0.0.1:11434/v1/chat/completions", "credential":
                    {"ref": "llm.key.ollama", "header": "Authorization",
                     "about": {"label": "Ollama API key", "hosts": ["127.0.0.1:11434"]}}}),
            )
            .unwrap_err();
        assert_eq!(err.type_, "SecretMissing");
    }

    #[test]
    fn guest_chat_writes_never_carry_host_only_attachment_types() {
        let mut b = make_broker("run_adr021_hostonly");
        let chat = "http://127.0.0.1:1/v1/spaces/s/objects/chat1/chat/messages";
        for ty in ["credential_request", "credential_set"] {
            let err = b
                .call(
                    "http.post",
                    json!({"url": chat, "json": {"text": "I need a credential to continue",
                        "attachments": {"credreq": {"type": ty,
                            "link": "any://o/sec?key=connector.key.github&setup=model"}}}}),
                )
                .unwrap_err();
            assert_eq!(err.type_, "host_only", "{ty}");
            assert!(err.message.contains(ty));
        }
        // a patch of an existing message is the same write
        let err = b
            .call(
                "http.patch",
                json!({"url": format!("{chat}/m1"), "json": {"attachments":
                    {"a": {"type": "credential_request", "link": "any://o/x?key=y"}}}}),
            )
            .unwrap_err();
        assert_eq!(err.type_, "host_only");
        // a batch item re-enters `call`: refused per item, as data
        let out = b
            .call(
                "batch",
                json!({"name": "http.post", "payloads": [
                    {"url": chat, "json": {"attachments": {"a": {"type": "credential_set"}}}}]}),
            )
            .unwrap();
        assert_eq!(out["results"][0]["error"]["type"], "host_only");
        // every other attachment type passes the guard (fails later on
        // the unreachable port, which proves the guard let it through)
        let err = b
            .call(
                "http.post",
                json!({"url": chat, "json": {"text": "see", "attachments":
                    {"a": {"type": "link", "link": "any://o/sp/obj"}}}}),
            )
            .unwrap_err();
        assert_ne!(err.type_, "host_only");
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

    struct MemConfigStore(Mutex<BTreeMap<String, Value>>);
    impl ConfigStore for MemConfigStore {
        fn read(&self, key: &str) -> Result<Option<Value>, String> {
            Ok(self.0.lock().unwrap().get(key).cloned())
        }
        fn set(&self, key: &str, value: &Value) -> Result<(), String> {
            self.0.lock().unwrap().insert(key.into(), value.clone());
            Ok(())
        }
    }

    #[test]
    fn config_set_persists_and_get_reads_through() {
        let store = Arc::new(MemConfigStore(Mutex::new(BTreeMap::new())));
        let mut b = make_broker("run_adr006_set");
        b.config_store = Some(store.clone());
        let v = json!({"provider": "gemini", "model": "gemini-x"});
        b.sys_config_set(&json!({"key": "search.provider.websearch", "value": v}))
            .unwrap();
        assert_eq!(store.0.lock().unwrap()["search.provider.websearch"], v);
        assert_eq!(
            b.sys_config_get(&json!({"key": "search.provider.websearch"}))
                .unwrap()["value"],
            v
        );
        // classified as a mutation (ADR-002: a store write is a side effect)
        assert_eq!(b.classify("config.set", &json!({})), "mutate");
    }

    #[test]
    fn config_get_reads_the_store_not_a_snapshot() {
        // a row written behind the broker's back (UI, another device)
        // is visible on the next config.get — no host copy (ADR-006 §3)
        let store = Arc::new(MemConfigStore(Mutex::new(BTreeMap::new())));
        let mut b = make_broker("run_adr006_readthrough");
        b.config
            .insert("llm.tier.codegen".into(), json!("stale-seed"));
        b.config_store = Some(store.clone());
        assert_eq!(
            b.sys_config_get(&json!({"key": "llm.tier.codegen"}))
                .unwrap_err()
                .type_,
            "ConfigError"
        );
        store
            .set("llm.tier.codegen", &json!({"model": "fresh"}))
            .unwrap();
        assert_eq!(
            b.sys_config_get(&json!({"key": "llm.tier.codegen"}))
                .unwrap()["value"]["model"],
            "fresh"
        );
    }

    #[test]
    fn runtime_get_is_its_own_namespace() {
        let mut b = make_broker("run_adr006_runtime");
        b.runtime.insert("any.base_url".into(), json!("http://x"));
        assert_eq!(
            b.sys_runtime_get(&json!({"key": "any.base_url"})).unwrap()["value"],
            "http://x"
        );
        assert_eq!(
            b.sys_runtime_get(&json!({"key": "nope"}))
                .unwrap_err()
                .type_,
            "KeyError"
        );
        // never reachable through config.get
        assert!(b.sys_config_get(&json!({"key": "any.base_url"})).is_err());
    }

    #[test]
    fn config_set_refuses_secrets_and_no_store() {
        let mut b = make_broker("run_adr006_set_refuse");
        let err = b
            .sys_config_set(&json!({"key": "llm.tier.codegen", "value": {}}))
            .unwrap_err();
        assert_eq!(err.type_, "ConfigError"); // no store (anyrt run)
        b.config_store = Some(Arc::new(MemConfigStore(Mutex::new(BTreeMap::new()))));
        for key in ["llm.key.anthropic", "connector.key.github"] {
            let err = b
                .sys_config_set(&json!({"key": key, "value": "x"}))
                .unwrap_err();
            assert_eq!(err.type_, "ConfigError", "{key}");
        }
        let err = b
            .sys_config_set(&json!({"key": "llm.tier.codegen"}))
            .unwrap_err();
        assert_eq!(err.type_, "TypeError");
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

    /// ADR-026 §3: a binary body comes back as a raw ref, the bytes in
    /// the run's blob directory; a JSON body stays text.
    #[test]
    fn http_binary_body_is_a_blob_ref() {
        let png_head: Vec<u8> = vec![0x89, b'P', b'N', b'G', 0xff, 0xfe, 0x00];
        let bytes = png_head.clone();
        let base = fake_server(2, move |req| {
            let resp = if req.url().ends_with("/f") {
                tiny_http::Response::from_data(bytes.clone()).with_header(
                    tiny_http::Header::from_bytes("Content-Type", "image/png").unwrap(),
                )
            } else {
                tiny_http::Response::from_data(b"{\"a\":1}".to_vec()).with_header(
                    tiny_http::Header::from_bytes("Content-Type", "application/json").unwrap(),
                )
            };
            let _ = req.respond(resp);
        });
        let dir = tempfile::tempdir().unwrap();
        let mut b = make_broker("run_blob");
        b.writer.blob_dir = Some(crate::blob::BlobDir::new(dir.path()));
        let out = b
            .call("http.get", json!({"url": format!("{base}/f")}))
            .unwrap();
        assert_eq!(out["status"], json!(200));
        assert!(crate::blob::is_raw_ref(&out["body"]), "{out}");
        assert_eq!(out["body"]["mime"], json!("image/png"));
        assert_eq!(out["body"]["bytes"], json!(7));
        let hash = out["body"]["__blob"].as_str().unwrap();
        assert_eq!(
            b.writer
                .blob_dir
                .as_ref()
                .unwrap()
                .read(hash)
                .unwrap()
                .unwrap(),
            png_head
        );
        // the record lists the raw ref (ADR-026 §2)
        let rec = b.writer.records.last().unwrap();
        assert_eq!(rec["blobs"], json!([hash]));
        let out = b
            .call("http.get", json!({"url": format!("{base}/j")}))
            .unwrap();
        assert_eq!(out["body"], json!("{\"a\":1}"));
    }

    /// ADR-026 §3: a raw ref inside `json` goes out as base64, a raw
    /// ref as `body` goes out as the bytes with its mime — and the
    /// recorded input keeps the refs.
    #[test]
    fn http_forwards_blob_refs_on_the_wire() {
        // (url, content-type, body) per request the fake server saw
        type Seen = Arc<Mutex<Vec<(String, String, Vec<u8>)>>>;
        let seen: Seen = Arc::new(Mutex::new(Vec::new()));
        let sink = seen.clone();
        let base = fake_server(2, move |mut req| {
            let ct = req
                .headers()
                .iter()
                .find(|h| h.field.equiv("content-type"))
                .map(|h| h.value.as_str().to_string())
                .unwrap_or_default();
            let mut body = Vec::new();
            req.as_reader().read_to_end(&mut body).unwrap();
            sink.lock().unwrap().push((req.url().to_string(), ct, body));
            let _ = req.respond(tiny_http::Response::from_string("{}"));
        });
        let dir = tempfile::tempdir().unwrap();
        let mut b = make_broker("run_fwd");
        let blobs = crate::blob::BlobDir::new(dir.path());
        b.writer.blob_dir = Some(blobs.clone());
        let r = blobs.put(b"\x89PNG", "image/png").unwrap();
        b.call(
            "http.post",
            json!({"url": format!("{base}/json"), "json": {"parts": [{"data": r}]}}),
        )
        .unwrap();
        b.call(
            "http.post",
            json!({"url": format!("{base}/raw"), "body": r}),
        )
        .unwrap();
        let seen = seen.lock().unwrap();
        assert_eq!(seen[0].1, "application/json");
        assert_eq!(seen[0].2, br#"{"parts":[{"data":"iVBORw=="}]}"#.to_vec());
        assert_eq!(seen[1].1, "image/png");
        assert_eq!(seen[1].2, b"\x89PNG".to_vec());
        // the data-URI form (the OpenAI image_url / file_data wire)
        let mut du = r.clone();
        du["encoding"] = json!("data-uri");
        assert!(crate::blob::is_raw_ref(&du));
        assert_eq!(
            b.expand_refs(&json!({"url": du})).unwrap(),
            json!({"url": "data:image/png;base64,iVBORw=="})
        );
        let recs: Vec<&Value> = b
            .writer
            .records
            .iter()
            .filter(|r| r["kind"] == "effect")
            .collect();
        assert!(crate::blob::is_raw_ref(
            &recs[0]["input"]["json"]["parts"][0]["data"]
        ));
        assert!(crate::blob::is_raw_ref(&recs[1]["input"]["body"]));
        assert_eq!(recs[1]["blobs"], json!([r["__blob"]]));
    }

    /// ADR-026 §4: `blob.put` records the ref, never the bytes;
    /// `blob.read` records the byte count, and a replayed read is served
    /// from the directory.
    #[test]
    fn blob_syscalls_record_refs_and_replay_from_the_directory() {
        use base64::Engine as _;
        let b64 = |b: &[u8]| base64::engine::general_purpose::STANDARD.encode(b);
        let dir = tempfile::tempdir().unwrap();
        let mut b = make_broker("run_put");
        b.writer.blob_dir = Some(crate::blob::BlobDir::new(dir.path()));
        let r = b
            .call(
                "blob.put",
                json!({"data": b64(b"hello blob"), "mime": "text/plain"}),
            )
            .unwrap();
        assert!(crate::blob::is_raw_ref(&r));
        assert_eq!(r["bytes"], json!(10));
        let hash = r["__blob"].as_str().unwrap().to_string();
        let out = b
            .call("blob.read", json!({"hash": hash, "offset": 6, "length": 4}))
            .unwrap();
        assert_eq!(out["data"], json!(b64(b"blob")));
        let recs: Vec<Value> = b
            .writer
            .records
            .iter()
            .filter(|r| r["kind"] == "effect")
            .cloned()
            .collect();
        assert_eq!(recs[0]["input"], json!({"ref": r}));
        assert!(recs[0]["input"]["ref"]["__blob"].is_string());
        assert_eq!(recs[1]["output"], json!({"bytes": 4})); // no data in the record
        assert_eq!(recs[1]["blobs"], Value::Null); // the hash is a string arg, not a ref

        let mut rb = make_broker("run_put");
        rb.writer.blob_dir = Some(crate::blob::BlobDir::new(dir.path()));
        rb.mode = Mode::Replay;
        rb.cursor = Some(ReplayCursor::new(&b.writer.records));
        let r2 = rb
            .call(
                "blob.put",
                json!({"data": b64(b"hello blob"), "mime": "text/plain"}),
            )
            .unwrap();
        assert_eq!(r2, r);
        let out2 = rb
            .call("blob.read", json!({"hash": hash, "offset": 6, "length": 4}))
            .unwrap();
        assert_eq!(out2["data"], json!(b64(b"blob")));
        let missing = rb
            .call("blob.read", json!({"hash": "sha256:00", "offset": 0}))
            .unwrap_err();
        // an unrecorded read is a divergence in strict replay, not a
        // silent directory read
        assert_eq!(missing.type_, "DivergenceError", "{missing:?}");
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
