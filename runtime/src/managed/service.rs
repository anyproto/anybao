//! The owned managed service (ADR-034 §§2, 5, 7): what preparation retains until
//! cancellation, the one factory every managed guest Broker comes from, and the
//! real lane runner. No desktop `serve` thread, election, OAuth or control
//! listener is reachable from here; the command consumer is N2b's.
use super::frozen::{FrozenContent, FrozenDeclared, FrozenResolver};
use super::lanes::{LaneFailure, LaneJob, LaneOutcome, LaneRunner, RUN_WALL_S};
use super::transport::PhaseGate;
use super::{ManagedFailure, ManagedOptions, PreseededFixture, StoreAddress};
use crate::anyapi::Client;
use crate::broker::{
    Broker, ConfigStore, DeclaredCredentials, SecretSource, SharedActivity, SharedPresence,
};
use crate::guest_egress::AndroidPocEgress;
use crate::managed::admission::StorePermit;
use crate::routes::Classifier;
use crate::runner::Cage;
use crate::stores::{alias_map, ServeConfigStore, ServeSecretStore};
use crate::trace::TraceWriter;
use crate::tracestore::{AnyTraceStore, TraceStore};
use crate::triggers::RunResult;
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, RwLock};
use std::time::{SystemTime, UNIX_EPOCH};

/// The zone `time.now` reports on Android (ADR-034 §2): the embedder's worker
/// scope creates it and updates it on startup, timezone changes, at the
/// zone's next offset transition and on a wall-clock change
/// (`ACTION_TIME_CHANGED`). The runtime never reads `TZ` or `HOME` for
/// it and never writes process environment. A sample is served only while it
/// is valid; past `valid_until_s`, or after a failed update, `time.now` is a
/// typed refusal (`TimeContextStale`) until a fresh sample arrives — a wrong
/// offset is never served (N1-R5).
#[derive(Clone)]
pub struct ManagedTimeContext(Arc<TimeInner>);

struct TimeInner {
    sample: RwLock<Option<ZoneSample>>,
    /// The sample's validity window as ONE word (N2a-r2 I-9: three atomics
    /// could be read torn): `from` in the high and `until` in the low 32 bits,
    /// whole Unix seconds, each 0 when unbounded, `from` rounded up and `until`
    /// rounded down (fail closed); `NO_SAMPLE` when there is none. Written
    /// under the sample's write lock; `health()` reads it alone, `now()` reads
    /// it under the read lock — both through `window_valid`, so they agree.
    window: AtomicU64,
}

const NO_SAMPLE: u64 = u64::MAX;

fn pack_window(sample: Option<&ZoneSample>) -> u64 {
    let Some(sample) = sample else {
        return NO_SAMPLE;
    };
    // Rounding only ever narrows the window: `from` up, `until` down. A bound
    // the word cannot hold is never clamped the unsafe way — clamping `from`
    // down would serve the offset before its window opens — so the sample is
    // then always stale (sb-fix review ISO-2). `from` stays below u32::MAX, so
    // a packed sample never equals NO_SAMPLE.
    let from = match sample.valid_from_s {
        None => 0,
        Some(v) if v.ceil() > f64::from(u32::MAX - 1) => return NO_SAMPLE,
        Some(v) => v.ceil().max(1.0) as u64,
    };
    let until = match sample.valid_until_s {
        None => 0,
        Some(v) if !(1.0..=f64::from(u32::MAX)).contains(&v.floor()) => return NO_SAMPLE,
        Some(v) => v.floor() as u64,
    };
    (from << 32) | until
}

fn window_valid(word: u64, now_s: f64) -> bool {
    let (from, until) = (word >> 32, word & u64::from(u32::MAX));
    word != NO_SAMPLE && (from == 0 || now_s >= from as f64) && (until == 0 || now_s < until as f64)
}

#[derive(Clone, Debug, PartialEq)]
pub struct ZoneSample {
    /// Zone id as Android reports it (`Europe/Berlin`, `GMT+05:30`); recorded as `tz`.
    pub zone_id: String,
    /// Local minus UTC, seconds; recorded as `offset_s`.
    pub offset_s: i32,
    /// Unix seconds from which this offset is valid (the previous transition,
    /// or the sampling instant): a wall clock set back before it is stale too.
    pub valid_from_s: Option<f64>,
    /// Unix seconds at which this offset stops being valid (the zone's next
    /// transition, `ZoneRules.nextTransition`); None when the zone has none.
    pub valid_until_s: Option<f64>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TimeContextStale;
impl std::fmt::Display for TimeContextStale {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("the Android zone sample is stale or missing; time.now refused until it is refreshed (ADR-034 §2)")
    }
}
impl std::error::Error for TimeContextStale {}

fn zone_sample(
    zone_id: &str,
    offset_s: i32,
    valid_from_s: Option<f64>,
    valid_until_s: Option<f64>,
) -> Result<ZoneSample, ManagedFailure> {
    let valid_id = !zone_id.is_empty()
        && zone_id.len() <= 64
        && zone_id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'/' | b'_' | b'-' | b'+' | b':'));
    let bound_ok = |v: Option<f64>| v.is_none_or(|v| v.is_finite() && v > 0.0);
    if !valid_id
        || offset_s.abs() > 18 * 3_600
        || !bound_ok(valid_from_s)
        || !bound_ok(valid_until_s)
        || valid_until_s.is_some_and(|until| until <= epoch_s()) // already expired: never accepted
        || valid_from_s
            .zip(valid_until_s)
            .is_some_and(|(from, until)| from >= until)
    {
        return Err(ManagedFailure::InvalidConfiguration);
    }
    Ok(ZoneSample {
        zone_id: zone_id.to_owned(),
        offset_s,
        valid_from_s,
        valid_until_s,
    })
}

pub(crate) fn epoch_s() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

impl ManagedTimeContext {
    pub fn new(
        zone_id: &str,
        offset_s: i32,
        valid_from_s: Option<f64>,
        valid_until_s: Option<f64>,
    ) -> Result<Self, ManagedFailure> {
        let context = Self(Arc::new(TimeInner {
            sample: RwLock::new(None),
            window: AtomicU64::new(NO_SAMPLE),
        }));
        context.update(zone_id, offset_s, valid_from_s, valid_until_s)?;
        Ok(context)
    }

    fn install(&self, sample: Option<ZoneSample>) {
        let mut slot = self.0.sample.write().unwrap_or_else(|e| e.into_inner());
        self.0
            .window
            .store(pack_window(sample.as_ref()), Ordering::Release);
        *slot = sample;
    }

    /// Android's startup/timezone-change/transition path. An invalid update
    /// (including a validity end already in the past) discards the current
    /// sample: the context is stale until a valid one arrives.
    pub fn update(
        &self,
        zone_id: &str,
        offset_s: i32,
        valid_from_s: Option<f64>,
        valid_until_s: Option<f64>,
    ) -> Result<(), ManagedFailure> {
        match zone_sample(zone_id, offset_s, valid_from_s, valid_until_s) {
            Ok(sample) => {
                self.install(Some(sample));
                Ok(())
            }
            Err(e) => {
                self.install(None);
                Err(e)
            }
        }
    }

    pub fn sample(&self) -> Option<ZoneSample> {
        self.0
            .sample
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    /// Absent, not yet valid, or expired — computed from the sample's own
    /// window at the moment of asking; `health()` reads it without the lock,
    /// as one word, by the same rule `now()` applies.
    pub fn is_stale(&self) -> bool {
        !window_valid(self.0.window.load(Ordering::Acquire), epoch_s())
    }

    /// The `time.now` reply (ADR-019 §8 shape): the wall clock plus this sample.
    /// A stale context refuses the whole effect (epoch included): fail closed.
    pub(crate) fn now(&self) -> Result<Value, TimeContextStale> {
        let epoch = epoch_s();
        let sample = {
            let slot = self.0.sample.read().unwrap_or_else(|e| e.into_inner());
            window_valid(self.0.window.load(Ordering::Acquire), epoch)
                .then(|| slot.clone())
                .flatten()
        };
        let Some(sample) = sample else {
            return Err(TimeContextStale);
        };
        Ok(json!({
            "epoch": epoch,
            "offset_s": sample.offset_s,
            "tz": sample.zone_id,
        }))
    }
}

/// The managed secret store (ADR-034 §2/§5): `ServeSecretStore` with the one
/// difference the gate enforces — a miss or rejection stamp on a row that does
/// not exist yet writes the descriptor WITHOUT `hosts`, and nothing ever binds
/// a row from first use. The destination of a declared ref is the frozen
/// declared table's; an open ref's is the row's, stamped by explicit setup.
pub(crate) struct ManagedSecretStore(pub(crate) ServeSecretStore);

fn without_hosts(about: &Value) -> Value {
    let mut about = about.clone();
    if let Some(map) = about.as_object_mut() {
        map.remove("hosts");
    }
    about
}

impl SecretSource for ManagedSecretStore {
    fn read(&self, key: &str) -> Result<Option<String>, String> {
        self.0.read(key)
    }
    fn mark_missing(&self, key: &str, about: &Value, run_id: &str) {
        self.0.mark_missing(key, &without_hosts(about), run_id)
    }
    fn mark_rejected(&self, key: &str, about: &Value, run_id: &str, http_status: u16) {
        self.0
            .mark_rejected(key, &without_hosts(about), run_id, http_status)
    }
    fn read_row(&self, key: &str) -> Result<Option<Value>, String> {
        self.0.read_row(key)
    }
    fn mark_used(&self, key: &str) {
        self.0.mark_used(key)
    }
    fn bind_hosts(&self, _key: &str, _about: &Value) {}
}

/// What preparation verified. A report for the owner and the later activation
/// path, never a Ready/Open assertion (ADR-034 §5): provider usability is
/// configuration, binding and stored-key evidence, not a provider call.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Readiness {
    /// The frozen overlays' pinned source revision, as recorded by explicit setup.
    pub source_revision: String,
    /// `alias:name@version` → verified frozen content fingerprint.
    pub programs: BTreeMap<String, String>,
    /// `alias:name` → verified skill content hash.
    pub skills: BTreeMap<String, String>,
    /// Configured `llm.tier.*` keys whose endpoint, credential kind, binding and stored key check out.
    pub tiers: BTreeSet<String>,
    pub credential_refs: BTreeSet<String>,
}

/// The prepared resources the owner retains (ADR-034 §4): the trusted cage, the
/// gated client, the device-local trace store, the shared stores, the frozen
/// content and the readiness report. The run lanes take brokers from here;
/// dropping the service releases the cage and stores.
#[allow(dead_code)] // the command consumer (N2b) flips the gate and reads the addresses
pub(crate) struct PreparedService {
    pub(crate) cage: Arc<Cage>,
    pub(crate) client: Arc<Client>,
    pub(crate) traces: Arc<AnyTraceStore>,
    pub(crate) config_store: Arc<ServeConfigStore>,
    pub(crate) secret_store: Arc<ManagedSecretStore>,
    pub(crate) declared: Arc<FrozenDeclared>,
    pub(crate) frozen: Arc<FrozenContent>,
    pub(crate) readiness: Readiness,
    /// The transport's phase switch: Execution from takeover until quiescent.
    pub(crate) gate: PhaseGate,
    /// The `bao.status` line slot the guest sets and the responder beat reads.
    pub(crate) status: SharedPresence,
    pub(crate) triggers: StoreAddress,
    pub(crate) runs: StoreAddress,
    leased_origin: String,
    space: String,
    secrets_object: String,
    secrets_collection: String,
    runtime: BTreeMap<String, Value>,
    egress: AndroidPocEgress,
    time: ManagedTimeContext,
}

impl PreparedService {
    pub(crate) fn new(
        cfg: &crate::Config,
        options: &ManagedOptions,
        fixture: &PreseededFixture,
        cage: Arc<Cage>,
        client: Arc<Client>,
        gate: PhaseGate,
        prepared: super::preparation::PreparedData,
    ) -> Self {
        let aliases = alias_map(&cfg.overlays, &cfg.agent_space);
        // The same `runtime.get` keys desktop serve exposes, from this device's config.
        let mut runtime = BTreeMap::new();
        runtime.insert("any.base_url".to_owned(), Value::String(cfg.addr.clone()));
        runtime.insert(
            "bao.space".to_owned(),
            Value::String(cfg.agent_space.clone()),
        );
        runtime.insert(
            "overlays.aliases".to_owned(),
            serde_json::to_value(&aliases).unwrap_or(Value::Null),
        );
        runtime.insert("shell".to_owned(), crate::shell_runtime_value());
        PreparedService {
            config_store: Arc::new(ServeConfigStore::new(
                client.clone(),
                &cfg.agent_space,
                &fixture.config.object_id,
                &fixture.config.collection,
            )),
            secret_store: Arc::new(ManagedSecretStore(ServeSecretStore {
                client: client.clone(),
                space: cfg.agent_space.clone(),
                obj: fixture.secrets.object_id.clone(),
                dataset: fixture.secrets.collection.clone(),
            })),
            cage,
            client,
            traces: Arc::new(prepared.traces),
            declared: Arc::new(prepared.declared),
            frozen: Arc::new(prepared.frozen),
            readiness: prepared.readiness,
            gate,
            status: Default::default(),
            triggers: fixture.triggers.clone(),
            runs: fixture.runs.clone(),
            leased_origin: options.leased_origin().to_owned(),
            space: cfg.agent_space.clone(),
            secrets_object: fixture.secrets.object_id.clone(),
            secrets_collection: fixture.secrets.collection.clone(),
            runtime,
            egress: options.guest_egress().clone(),
            time: options.time_context().clone(),
        }
    }

    /// The one managed Broker (ADR-034 §§2, 5). Guarantees, in order:
    /// the lease's guest-egress policy is installed on the bare broker before
    /// any store, resolver or effect exists on it, so no live effect can run
    /// unfenced; OAuth state is absent and `oauth.*`/`sh.*`/`fs.*` are refused
    /// typed before any consent, grant or token read; `time.now` reports the
    /// embedder's zone sample, with no process environment read or written;
    /// module resolution serves the frozen snapshot only (no HTTP); the shared
    /// config/secret/declared stores, trace store, presence slot and the
    /// secrets guards are the desktop shape. Nothing here opens admission.
    #[allow(dead_code)] // the lane runner below is called by the execution service (N2b)
    pub(crate) fn broker(&self, spec: &str, run_id: &str) -> Broker {
        let mut writer = TraceWriter::new(json!({"id": run_id, "program": spec, "host": "rust"}));
        if let Err(e) = writer.stream_to(self.traces.as_ref()) {
            tracing::warn!("trace streaming unavailable ({e}); will write at run end");
        }
        let mut b = Broker::new(
            writer,
            BTreeMap::new(),
            BTreeMap::new(),
            None,
            Classifier::new(Some(&self.leased_origin)),
        );
        b.set_guest_egress(self.egress.clone());
        b.set_time_context(self.time.clone());
        b.oauth = None;
        b.secrets_guard = Some(self.secrets_object.clone());
        b.secrets_collection = Some(self.secrets_collection.clone());
        b.trace_store = Some(self.traces.clone() as Arc<dyn TraceStore>);
        b.secret_store = Some(self.secret_store.clone() as Arc<dyn SecretSource>);
        b.config_store = Some(self.config_store.clone() as Arc<dyn ConfigStore>);
        b.declared = Some(self.declared.clone() as Arc<dyn DeclaredCredentials>);
        b.managed_credentials = true; // never a guest-supplied host, never a first-use bind
        b.runtime = self.runtime.clone();
        b.presence = Some(self.status.clone());
        b.resolver = Some(Box::new(FrozenResolver::new(self.frozen.clone())));
        b
    }
}

/// The real lane runner: the program under the lane permit, the trace landed
/// in the device-local store, the ADR-023 summary back to the service.
impl LaneRunner for PreparedService {
    fn run(&self, job: &LaneJob, activity: SharedActivity, permit: StorePermit) -> LaneOutcome {
        let mut broker = self.broker(&job.spec, &job.run_id);
        broker.writer.trigger = job.trigger.clone();
        broker.activity = Some(activity);
        let outcome = crate::runner::run_program_with(
            &self.cage,
            broker,
            &job.spec,
            &job.args,
            job.mailbox.raw(),
            job.interrupt.clone(),
            RUN_WALL_S,
            Some(permit),
        );
        let mut outcome = match outcome {
            Ok(outcome) => outcome,
            // a Stop that landed before or during instantiation is a cancel,
            // never a run failure (R2-C7): the input is restorable
            Err(e)
                if e.chain().any(|c| {
                    matches!(
                        c.downcast_ref::<crate::runner::ExecutionStop>(),
                        Some(crate::runner::ExecutionStop::Interrupted)
                    )
                }) =>
            {
                return LaneOutcome {
                    result: Err(LaneFailure::Cancelled),
                    summary: None,
                }
            }
            Err(e) => {
                return LaneOutcome {
                    result: Err(LaneFailure::RunPath(e.to_string())),
                    summary: None,
                }
            }
        };
        // ADR-023 §3: a run whose trace could not be landed is a failed run, named
        let summary = match outcome.broker.writer.dump(self.traces.as_ref()) {
            Ok(summary) => summary,
            Err(e) => {
                return LaneOutcome {
                    result: Err(LaneFailure::TraceUnpersisted(format!(
                        "trace.unpersisted: run {}: {e:#}",
                        job.run_id
                    ))),
                    summary: None,
                }
            }
        };
        LaneOutcome {
            result: Ok(RunResult {
                status: match outcome.status.as_str() {
                    "ok" => "ok".into(),
                    "interrupted" => "interrupted".into(),
                    _ => "error".into(),
                },
                duration_ms: outcome.duration_ms,
                trace_ref: Some(job.run_id.clone()),
                fuel: Some(outcome.fuel_used as i64),
                error: outcome.error.map(|e| e.to_string()),
                missing_secrets: outcome.broker.missing_secrets.clone(),
                missing_programs: outcome.broker.missing_programs.clone(),
                refused_programs: outcome.broker.refused_programs.clone(),
                mutated: outcome.broker.mutations > 0,
            }),
            summary: Some(summary),
        }
    }
}

#[cfg(test)]
mod tests;
