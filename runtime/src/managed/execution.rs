//! The managed execution service (ADR-034 §§2, 4, 5, 7; N2b-2 design r3):
//! desktop's orchestration in `serve` — the trigger ticker, `finish_run`, the
//! election stand-down and the presence thread — transposed onto owned
//! threads and the run lanes. Everything that computes is shared with
//! desktop: the scheduler's due rules (`cron_due`, `once_due`, `record_run`),
//! `reconcile_registry`, `health_pass`, the run summary, the beat envelope
//! and the stand-down write. What is managed-only is structural: owned and
//! joined threads; dispatch under a lane permit, with the occurrence consumed
//! only once the permit is held and the trigger reserved until its report's
//! bookkeeping returned; admission only while the session's AppliedOpen is in
//! force and its grant is fresh on the calibrated boot clock; quiescence
//! counted through every report callback, the stand-down write and the
//! `shutdown` beat, so the session's Released means it.
//!
//! Desktop parity by the user's ruling (2026-11-06, design §10 R1–R9): a
//! revoking Close lets live runs finish (Release and Stop interrupt them,
//! `Execution::interrupt`); a once shot is consumed in memory at admission and
//! persisted at its report, so a worker death mid-run fires it again on the
//! next owner; a saturated queue is a reported capacity fault, nothing more;
//! skills are verified at Open only; nothing durable marks a run started.
//!
//! Threads (fixed `Task` slots, joined by the owner): the dispatcher
//! (`Task::Service`: the RSS sample, the due scan, dispatch, soft-break
//! deadlines and the chat inputs parked for a run not live yet; it performs no
//! HTTP), the one stand-down write on its own thread (`Task::Standdown`, so
//! nothing waits on it: round A review A7), the responder beats
//! (`Task::Presence`), housekeeping (`Task::Housekeeping`: the registry
//! reconcile and its stamps), the chat watch (`Task::ChatWatch`, `chat.rs`:
//! desktop's `watch_chat` on an owned thread), up to four event-source streams
//! (`Task::EventSource0..3`, `sources.rs`: desktop's `event_source_thread` on
//! owned slots) and trace retention (`Task::Retention`, desktop's cadence in
//! every phase, R7). Run bookkeeping — a chat run's
//! follow-ups included — runs on the lane thread that ran the job, as desktop
//! runs it on the run thread. One lock, `state`, is
//! never held across HTTP; the lock order is `state` → inbox slot / pending
//! interrupts / live runs → mailbox → pool. `health()` reads the published
//! word only.
#![cfg_attr(
    not(test),
    expect(
        dead_code,
        reason = "reachable only through `ExecutionService::start`: production wires it at \
                  N2b-2 C4, `SharedOwner::build_execution` returns None until then"
    )
)]

use super::admission::{
    Admission, GateCause, Lane, PendingQueue, PendingWork, StorePermit, MAX_BACKLOG, MAX_PENDING,
};
use super::frozen::WORKING_ALIAS;
use super::grant::{ActivationIntent, BootClock, CalibratedBootClock, OpenGrant};
use super::lanes::{EpochDriver, LaneFailure, LaneJob, LaneReport, LaneRunner, Lanes};
use super::session::Execution;
use super::takeover::{persisted_record, TakeoverOutcome};
use super::transport::refused_by_gate;
use super::{ManagedFailure, Shared, StoreAddress, Task, TaskContext, WaitClass};
use crate::anyapi::Client;
use crate::broker::{ConfigStore, SharedPresence};
use crate::chatwatch::{escalate_soft_break, release_chat_watch, BREAK_GRACE};
use crate::presence::{
    current_run, is_signed_out, presence_decide, status_envelope, BeatRole, PresenceLoop,
};
use crate::tracestore::{publish_run_summary, TraceStore};
use crate::triggers::{
    health_pass, is_chat_watch, owned_chat_watch, reconcile_registry, standing_triggers, RunResult,
    Scheduler, Trigger, Watcher,
};
use serde_json::{json, Value};
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock};
use std::time::{Duration, Instant};
use tracing::{debug, info, warn};

/// The dispatcher's longest wait between passes; reports, opens and closes
/// wake it earlier.
const SERVICE_WAKE: Duration = Duration::from_secs(1);
/// `VmRSS` cadence (ADR-034 §2: every 5 s; Kotlin judges a sample older than
/// 15 s stale).
const SAMPLE_EVERY: Duration = Duration::from_secs(5);
/// The due scan, desktop's ticker cadence (`serve::trigger_ticker`).
const SCAN_EVERY: Duration = Duration::from_secs(5);
/// The registry reconcile, desktop's ticker cadence.
const RECONCILE_EVERY: Duration = Duration::from_secs(5);
/// The beat thread's poll (`serve::STATUS_POLL_S`).
const BEAT_POLL: Duration = Duration::from_secs(1);
/// The PoC heartbeat program (ADR-034 §6): priority among due scheduled work.
pub(crate) const HEARTBEAT_PROGRAM: &str = "android-poc-heartbeat";
/// The chat watch's pause between subscriptions (desktop's reconnect loop).
const RECONNECT: Duration = Duration::from_secs(2);
/// Event-source convergence, desktop's ticker cadence.
const CONVERGE_EVERY: Duration = Duration::from_secs(5);
/// Trace retention (desktop `retention_thread`): a minute after start, then
/// hourly.
const RETENTION_FIRST: Duration = Duration::from_secs(60);
const RETENTION_EVERY: Duration = Duration::from_secs(3600);

mod chat;
mod sources;
use chat::{ChatRun, Parked};
use sources::Source;

/// The service loops' cadence: desktop's in production (`Cadence::default`);
/// tests pass their own.
#[derive(Clone, Copy, Debug)]
pub(crate) struct Cadence {
    pub(crate) wake: Duration,
    pub(crate) sample: Duration,
    pub(crate) scan: Duration,
    pub(crate) reconcile: Duration,
    pub(crate) beat: Duration,
    /// A stream's pause before it subscribes again.
    pub(crate) reconnect: Duration,
    /// The soft-break grace (ADR-005 §3, `BREAK_GRACE`).
    pub(crate) break_grace: Duration,
    /// Event-source convergence.
    pub(crate) converge: Duration,
    pub(crate) retention_first: Duration,
    pub(crate) retention_every: Duration,
}

impl Default for Cadence {
    fn default() -> Self {
        Cadence {
            wake: SERVICE_WAKE,
            sample: SAMPLE_EVERY,
            scan: SCAN_EVERY,
            reconcile: RECONCILE_EVERY,
            beat: BEAT_POLL,
            reconnect: RECONNECT,
            break_grace: BREAK_GRACE,
            converge: CONVERGE_EVERY,
            retention_first: RETENTION_FIRST,
            retention_every: RETENTION_EVERY,
        }
    }
}

/// What the service runs against: the working space, its chat and the self
/// peer from explicit setup, the trigger, run, secret and turn stores, and
/// what a chat run is started with (desktop `start_or_inject`'s args).
#[derive(Clone)]
pub(crate) struct ExecutionConfig {
    pub(crate) space: String,
    pub(crate) chat: String,
    pub(crate) self_peer: String,
    /// This agent's chat identity (`agent.name` on its own bubbles).
    pub(crate) agent_name: String,
    pub(crate) triggers: StoreAddress,
    pub(crate) runs: StoreAddress,
    pub(crate) secrets: StoreAddress,
    /// The general chat's `bao/log/v1` child and its `agent_turns`
    /// collection, recorded by explicit setup (ADR-034 §5, design Q1).
    pub(crate) turns: StoreAddress,
    /// The `agent` overlay's space (`codeSpace`) and the other overlays by
    /// alias (`overlays`), as desktop passes them to the toolcaller.
    pub(crate) code_space: String,
    pub(crate) overlays: serde_json::Map<String, Value>,
    /// The `[traces]` retention ages and size bound (desktop's config).
    pub(crate) retention: Retention,
    pub(crate) cadence: Cadence,
}

/// ADR-023 §6's retention settings, as desktop's `Config` carries them.
#[derive(Clone, Copy, Debug, Default)]
pub(crate) struct Retention {
    pub(crate) conversations_s: Option<u64>,
    pub(crate) jobs_s: Option<u64>,
    pub(crate) max_bytes: Option<u64>,
}

/// What the service runs with: in production the retained `PreparedService`
/// (runner, gated client, presence slot, the cage's engine), the session's
/// boot clock and the process's `VmRSS`; tests pass fakes.
pub(crate) struct ExecutionParts {
    pub(crate) runner: Arc<dyn LaneRunner>,
    pub(crate) client: Arc<Client>,
    pub(crate) status: SharedPresence,
    pub(crate) engine: wasmtime::Engine,
    pub(crate) clock: Arc<dyn BootClock>,
    pub(crate) rss: Arc<dyn Fn() -> Option<u64> + Send + Sync>,
    /// Unix seconds: the scheduler's clock and the beats' timestamps.
    pub(crate) now: Arc<dyn Fn() -> f64 + Send + Sync>,
    /// The agent config store the credential card reads (desktop
    /// `RunCtx::config_store`).
    pub(crate) config_store: Option<Arc<dyn ConfigStore>>,
    /// The device-local trace store retention sweeps.
    pub(crate) traces: Arc<dyn TraceStore>,
}

/// The worker's own resident set, from `/proc/self/status` (`VmRSS`, kB).
/// None where there is no procfs (macOS tests) or the line is missing.
pub(crate) fn vm_rss() -> Option<u64> {
    let status = std::fs::read_to_string("/proc/self/status").ok()?;
    let line = status.lines().find(|l| l.starts_with("VmRSS:"))?;
    let kb: u64 = line
        .trim_start_matches("VmRSS:")
        .trim()
        .trim_end_matches("kB")
        .trim()
        .parse()
        .ok()?;
    kb.checked_mul(1024)
}

/// The execution service as `health()` reports it: one atomic word,
/// published after every state change.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ExecutionSnapshot {
    pub installed: bool,
    /// AppliedOpen is in force on the service (dispatch, beats, housekeeping).
    pub open: bool,
    pub owns_chat: bool,
    /// The stand-down write (responder owner cleared) is due or in flight.
    pub standdown_pending: bool,
    /// A revoking close's `shutdown` beat is not out yet.
    pub shutdown_beat_pending: bool,
    pub capacity_fault: bool,
    /// A responder beat is being published.
    pub beat_in_flight: bool,
    /// Jobs from admission until their report callback returned.
    pub in_flight: u8,
    pub queued: u8,
    /// Activations (completed takeovers) so far, modulo 2^16.
    pub activation: u16,
    /// Stand-down writes that failed or were left unsettled by a cancel
    /// (saturating at 4095).
    pub standdown_failures: u16,
    /// Report bookkeeping writes the store or the transport refused
    /// (saturating at 4095): logged, never retried.
    pub bookkeeping_failures: u16,
}

impl ExecutionSnapshot {
    pub(crate) fn from_word(word: u64) -> Self {
        ExecutionSnapshot {
            installed: word & 1 != 0,
            open: word & 2 != 0,
            owns_chat: word & 4 != 0,
            standdown_pending: word & 8 != 0,
            shutdown_beat_pending: word & 16 != 0,
            capacity_fault: word & 32 != 0,
            beat_in_flight: word & 64 != 0,
            in_flight: (word >> 8) as u8,
            queued: (word >> 16) as u8,
            activation: (word >> 24) as u16,
            standdown_failures: ((word >> 40) & 0xfff) as u16,
            bookkeeping_failures: ((word >> 52) & 0xfff) as u16,
        }
    }

    fn word(&self) -> u64 {
        u64::from(self.installed)
            | u64::from(self.open) << 1
            | u64::from(self.owns_chat) << 2
            | u64::from(self.standdown_pending) << 3
            | u64::from(self.shutdown_beat_pending) << 4
            | u64::from(self.capacity_fault) << 5
            | u64::from(self.beat_in_flight) << 6
            | u64::from(self.in_flight) << 8
            | u64::from(self.queued) << 16
            | u64::from(self.activation) << 24
            | u64::from(self.standdown_failures.min(0xfff)) << 40
            | u64::from(self.bookkeeping_failures.min(0xfff)) << 52
    }
}

/// A job from its admission until its report callback returned.
#[derive(Clone, Debug)]
enum JobKind {
    /// A due cron occurrence or once shot; `once` = a once shot this job
    /// consumed in memory at admission (restored if it never started).
    Scheduled { trigger: String, once: bool },
    /// A conversation run: the text it started with (the interrupted turn's
    /// `userText`) and the responder it runs under (the summary's trigger).
    Chat {
        user_text: String,
        responder: Option<String>,
    },
    /// One event trigger fired by one message (ADR-018 §2).
    Event { trigger: String },
}

/// One activation as its beats name it (ADR-034 §7).
#[derive(Clone, Debug, PartialEq, Eq)]
struct BeatKey {
    session_id: String,
    activation: ActivationIntent,
}

struct State {
    /// The open grant and the clock its freshness is checked on.
    grant: Option<(OpenGrant, CalibratedBootClock)>,
    /// Desktop's one registry (`Shared.triggers`), across activations.
    registry: BTreeMap<String, Trigger>,
    scheduler: Scheduler,
    /// Due scheduled work waiting for its lane (heartbeat first).
    queue: PendingQueue,
    /// The last due scan had work the saturated queue refused.
    scan_refused: bool,
    /// run id → job, from admission until the report callback returned.
    jobs: BTreeMap<String, JobKind>,
    /// trigger id → run id: admitted and not yet reported back.
    reserved: BTreeMap<String, String>,
    /// Soft-break deadlines (ADR-005 §3): (when, run id).
    breaks: Vec<(Instant, String)>,
    /// The responder entry a revoking close took out, for the stand-down write.
    owner_clear: Option<Trigger>,
    /// The activation that has been open and owes one `shutdown` beat at its
    /// revoking close; a non-revoking close keeps it owed, a new takeover
    /// replaces it (round A review A5).
    beat_owed: Option<BeatKey>,
    /// `shutdown` beats due, one per activation, oldest first.
    shutdowns: VecDeque<BeatKey>,
    /// Trigger ids whose housekeeping write (adoption stamp, health mark) has
    /// not landed yet: not dispatchable until it has, as desktop writes them
    /// before its scan — no report write of theirs can be overtaken (A1).
    settling: BTreeSet<String>,
    /// The scheduling revision, bumped when a report ends its trigger's
    /// reservation (after the report's last write), and the revision at
    /// which each trigger's reservation last ended. A reconcile captures the
    /// revision before its read and leaves the rows of triggers whose
    /// reservation ended since unapplied — desktop's synchronous ticker never
    /// overlaps a read with a report — so a stale response cannot undo a
    /// breaker or rearm a shot (round A-r2 review B1). A read applied while
    /// the reservation stands skips the trigger anyway, and a job that never
    /// started persisted nothing, so neither the start nor a cancel bumps
    /// it. Pruned at every applied pass: the one reader is the housekeeping
    /// thread, so no older read is still open.
    sched_rev: u64,
    touched: BTreeMap<String, u64>,
    /// The stand-down / beat thread has exited (the owner was cancelled): a
    /// close marks no obligation it can no longer carry. Set under this
    /// lock, as the close's marking is.
    standdown_exited: bool,
    beats_exited: bool,
    /// Desktop's dedup and routing for chat records (`live` stays empty: the
    /// managed chat run is `chat`); reset by every close, so the next
    /// snapshot re-derives what a close dropped.
    watcher: Watcher,
    /// The conversation run from its offer until its report callback took it
    /// down (desktop's `Watcher::live` entry).
    chat: Option<ChatRun>,
    /// Inputs for `chat` while it is not live yet (offered, or in its
    /// take→register gap) or already torn down: injected in order once it is
    /// live, else its report re-dispatches them — desktop's watcher holds the
    /// run's mailbox from before it starts. Bounded like the deferred chat
    /// backlog (ADR-034 §2); each with the close generation it arrived in.
    parked: VecDeque<(u64, Parked)>,
    /// The running event sources by `(space, chat object)`: their slot and
    /// stop flag; a source removes its own entry when it exits.
    sources: BTreeMap<(String, String), Source>,
    /// Desired sources not watched — another space, or no free slot —
    /// logged (and a fifth source counted) once until watched.
    sources_noted: BTreeSet<(String, String)>,
}

impl State {
    /// Admission's grant check (ADR-034 §7 "checked at every admission"):
    /// the grant is fresh on the calibrated boot clock; a failing clock fails
    /// closed.
    fn grant_fresh(&self) -> bool {
        self.grant
            .as_ref()
            .is_some_and(|(grant, clock)| clock.now_ms().is_ok_and(|now| grant.fresh(now)))
    }
}

struct Inner {
    /// This service, for the threads it starts later (event sources).
    me: std::sync::Weak<Inner>,
    shared: Arc<Shared>,
    config: ExecutionConfig,
    client: Arc<Client>,
    status: SharedPresence,
    clock: Arc<dyn BootClock>,
    rss: Arc<dyn Fn() -> Option<u64> + Send + Sync>,
    now: Arc<dyn Fn() -> f64 + Send + Sync>,
    config_store: Option<Arc<dyn ConfigStore>>,
    traces: Arc<dyn TraceStore>,
    lanes: OnceLock<Lanes>,
    /// Bumped by every close: a reconcile read that spans a close is dropped.
    gen: AtomicU64,
    /// Bumped by every Open that performed a takeover.
    activation_gen: AtomicU64,
    open: AtomicBool,
    owns_chat: AtomicBool,
    /// `state.jobs.len()`, updated in the same critical section.
    inflight: AtomicUsize,
    queue_len: AtomicUsize,
    /// `capacity_fault()`: the pending queue is full, or refused due work at
    /// the last scan (design §3.8) — reported, never acted on (R3 parity).
    fault: AtomicBool,
    owner_clear_due: AtomicBool,
    owner_clear_in_flight: AtomicBool,
    /// The session's go for the write (no takeover in flight, E5).
    owner_clear_released: AtomicBool,
    /// The stand-down thread's wake (the write was released).
    standdown_kick: AtomicBool,
    /// `state.shutdowns.len()` plus the one being published, until its
    /// publish attempt returned: counted in quiescence.
    shutdowns_pending: AtomicUsize,
    /// An ordinary responder beat is being published: counted in quiescence,
    /// so Released never precedes a beat already admitted (A5).
    beat_in_flight: AtomicBool,
    /// The session's go for the queued `shutdown` beats
    /// (`release_shutdown_beats`), given after its cleanup authorization of
    /// the transport: a beat never runs into a gate that still says
    /// Preparation (round A-r2 review B2). Unlike the owner-clear's, it does
    /// not wait for a takeover: a beat writes no record.
    shutdowns_released: AtomicBool,
    /// Housekeeping is persisting the stamps of a pass that converged while
    /// open: counted in quiescence, so no adoption stamp (`owner` = this
    /// peer) lands after Released.
    housekeeping_writes: AtomicBool,
    /// The responder's conversation-start writes in flight (desktop
    /// `note_chat_start`): counted in quiescence, and the stand-down write
    /// waits for them, so none lands after the owner was cleared.
    chat_notes: AtomicUsize,
    /// Housekeeping reconciles at once (an Open happened).
    reconcile_now: AtomicBool,
    /// A dispatcher pass is wanted (a report, an open, a close).
    woken: AtomicBool,
    /// Report callbacks returned: an event fire waiting for its lane (or its
    /// trigger's last write) retries when this moves.
    reports: AtomicU64,
    standdown_failures: AtomicU64,
    bookkeeping_failures: AtomicU64,
    /// Reconcile passes that converged the registry, and beat-thread passes
    /// (diagnostics; tests wait on them).
    reconcile_passes: AtomicU64,
    beat_passes: AtomicU64,
    /// Retention wakes and the sweeps attempted at them (diagnostics; tests
    /// wait on them).
    retention_wakes: AtomicU64,
    sweeps: AtomicU64,
    state: Mutex<State>,
    /// Test-only: run on the scanning thread before `due_scan` takes `state`.
    #[cfg(test)]
    before_scan_lock: Mutex<Option<Box<dyn FnOnce() + Send>>>,
}

/// The owned execution service. `start` spawns its threads; the session
/// drives it through `Execution`.
pub(crate) struct ExecutionService {
    inner: Arc<Inner>,
}

thread_local! {
    /// What the lane worker's leftover sink took on this thread, for the
    /// report callback that follows on the same thread (`RunSlot::release`
    /// and `InboxGuard` hand on, then report).
    static LEFTOVERS: RefCell<Vec<Value>> = const { RefCell::new(Vec::new()) };
}

/// The lanes' leftover sink: undrained mailbox input waits on its lane
/// thread for that job's report callback, bounded like the backlog (a
/// refusal comes back to the lane, which counts it as a capacity fault).
fn stash_leftovers(items: Vec<Value>) -> Result<(), Vec<Value>> {
    LEFTOVERS.with(|stash| {
        let mut stash = stash.borrow_mut();
        if items.len() > MAX_BACKLOG.saturating_sub(stash.len()) {
            return Err(items);
        }
        stash.extend(items);
        Ok(())
    })
}

fn take_leftovers() -> Vec<Value> {
    LEFTOVERS.with(|stash| std::mem::take(&mut *stash.borrow_mut()))
}

/// Desktop's run id shape (`RunCtx::new_run_id`): a valid trace run id.
fn new_run_id() -> String {
    format!("run_{}", &uuid::Uuid::new_v4().simple().to_string()[..16])
}

/// A scheduled job of the PoC heartbeat: `android-poc-heartbeat@vN`,
/// unqualified, under the reserved `working` alias, or under the working
/// space's id — the pinned working-space program (ADR-034 §2/§6, Q8).
fn is_heartbeat(program: &str, working_space: &str) -> bool {
    let (prefix, spec) = match program.split_once(':') {
        Some((prefix, spec)) => (Some(prefix), spec),
        None => (None, program),
    };
    spec.split('@').next() == Some(HEARTBEAT_PROGRAM)
        && prefix.is_none_or(|p| p == WORKING_ALIAS || p == working_space)
}

/// A report persisted its trigger's runtime state and ended the reservation:
/// a reconcile read begun before this must not apply its row (B1).
fn touch(st: &mut State, id: &str) {
    st.sched_rev += 1;
    st.touched.insert(id.to_owned(), st.sched_rev);
}

fn standing_ids(space: &str) -> BTreeSet<String> {
    standing_triggers(space, "", "")
        .into_iter()
        .map(|t| t.id)
        .collect()
}

impl ExecutionService {
    /// Spawn the epoch driver, the three lanes and the service threads into
    /// the owner's registry. A refused spawn (the owner already cancelled)
    /// is `Cancelled`; a failed one has already failed the owner.
    pub(crate) fn start(
        shared: &Arc<Shared>,
        config: ExecutionConfig,
        parts: ExecutionParts,
    ) -> Result<Arc<ExecutionService>, ManagedFailure> {
        let inner = Inner::new(shared, config, &parts);
        let ExecutionParts { runner, engine, .. } = parts;
        // the epoch driver before the lanes: a refused spawn then means no
        // store can attach (lanes.rs `EpochDriver`)
        EpochDriver::spawn(shared, engine)?;
        let report = {
            let inner = inner.clone();
            Arc::new(move |report: LaneReport| inner.on_report(report))
        };
        let lanes = Lanes::spawn(shared, runner, report, Arc::new(stash_leftovers))?;
        let _ = inner.lanes.set(lanes);
        for (task, body) in [
            (
                Task::Service,
                Inner::service_loop as fn(&Inner, &TaskContext),
            ),
            (Task::Presence, Inner::presence_loop),
            (Task::Housekeeping, Inner::housekeeping_loop),
            (Task::Standdown, Inner::standdown_loop),
            (Task::ChatWatch, Inner::chat_loop),
            (Task::Retention, Inner::retention_loop),
        ] {
            let inner = inner.clone();
            shared.spawn_owned(task, move |ctx| {
                body(&inner, &ctx);
                Ok(())
            })?;
        }
        inner.publish();
        Ok(Arc::new(ExecutionService { inner }))
    }
}

impl Execution for ExecutionService {
    fn open(
        &self,
        grant: &OpenGrant,
        session_id: &str,
        takeover: Option<&TakeoverOutcome>,
        clock: &CalibratedBootClock,
    ) {
        let i = &self.inner;
        {
            let mut st = i.lock();
            st.grant = Some((grant.clone(), clock.clone()));
            if let Some(outcome) = takeover {
                // a new activation (design §3.9): the standing built-ins and
                // the responder as the takeover wrote them replace their
                // entries; the device-pinned user entries stay (desktop's one
                // registry); its stand-down obligations start fresh (E11)
                i.activation_gen.fetch_add(1, Ordering::SeqCst);
                let replaced: BTreeSet<&str> =
                    outcome.standing.iter().map(|t| t.id.as_str()).collect();
                st.registry
                    .retain(|id, _| !replaced.contains(id.as_str()) && !is_chat_watch(id));
                for t in outcome.standing.iter().chain([&outcome.chat_watch]) {
                    st.registry.insert(t.id.clone(), t.clone());
                }
                st.owner_clear = None;
                i.owner_clear_released.store(false, Ordering::SeqCst);
                i.owner_clear_due.store(false, Ordering::SeqCst);
            }
            // the activation now beating, owing one `shutdown` beat: a takeover
            // starts a new one, a renewal keeps it
            if takeover.is_some() || st.beat_owed.is_none() {
                st.beat_owed = Some(BeatKey {
                    session_id: session_id.to_owned(),
                    activation: grant.intent.clone(),
                });
            }
            i.owns_chat.store(
                owned_chat_watch(&st.registry, &i.config.self_peer).is_some(),
                Ordering::SeqCst,
            );
            i.open.store(true, Ordering::SeqCst);
            i.shared.reopen(GateCause::Policy);
        }
        i.publish();
        i.reconcile_now.store(true, Ordering::SeqCst);
        i.wake();
    }

    fn close(&self, revoking: bool) {
        let i = &self.inner;
        {
            let mut st = i.lock();
            i.open.store(false, Ordering::SeqCst);
            i.shared.stand_down(GateCause::Policy, false);
            i.gen.fetch_add(1, Ordering::SeqCst);
            // ADR-034 §7: a close cancels queued admissions
            st.queue.cancel();
            st.scan_refused = false;
            i.queue_len.store(0, Ordering::SeqCst);
            i.fault.store(false, Ordering::SeqCst);
            st.grant = None;
            // desktop's reconnect re-derives the chat from the next snapshot
            // (the answered-cut, a fresh seen-set): what a close dropped —
            // the streams exit at their next frame — is read again there
            st.watcher = Watcher::new(i.config.agent_name.clone());
            let dropped = std::mem::take(&mut st.parked).len();
            if dropped > 0 {
                info!("chat: {dropped} input(s) for a run not live yet dropped by the close");
            }
            if revoking {
                // desktop's stand-down (`serve::election_thread`): the standing
                // built-ins unowned locally — the next winner's takeover stamps
                // their records — and the responder taken out, its owner
                // cleared on the record by the one stand-down write
                let standing = standing_ids(&i.config.space);
                for t in st.registry.values_mut() {
                    if standing.contains(&t.id) {
                        t.owner.clear();
                    }
                }
                let responders: Vec<String> = st
                    .registry
                    .keys()
                    .filter(|id| is_chat_watch(id))
                    .cloned()
                    .collect();
                for id in responders {
                    let Some(t) = st.registry.remove(&id) else {
                        continue;
                    };
                    if st.standdown_exited {
                        // the owner was cancelled: nobody can write it now
                        i.standdown_failures.fetch_add(1, Ordering::SeqCst);
                        warn!("chat responder {id:?}: stand-down write unsettled (cancelled)");
                        continue;
                    }
                    st.owner_clear = Some(t);
                    i.owner_clear_released.store(false, Ordering::SeqCst);
                    i.owner_clear_due.store(true, Ordering::SeqCst);
                }
                i.owns_chat.store(false, Ordering::SeqCst);
                // the activation that has been open owes its `shutdown` beat,
                // also when a non-revoking close came first (A5)
                if let Some(key) = st.beat_owed.take() {
                    if !st.beats_exited {
                        st.shutdowns.push_back(key);
                        i.shutdowns_pending.fetch_add(1, Ordering::SeqCst);
                        // runnable only once the session released it (B2)
                        i.shutdowns_released.store(false, Ordering::SeqCst);
                    }
                }
            }
        }
        i.publish();
        i.wake();
    }

    fn interrupt(&self) {
        let i = &self.inner;
        let mut st = i.lock();
        i.shared.interrupt_all();
        // a trapped run needs no escalation
        st.breaks.clear();
    }

    fn quiescent(&self) -> bool {
        let i = &self.inner;
        i.inflight.load(Ordering::SeqCst) == 0
            && i.queue_len.load(Ordering::SeqCst) == 0
            && !i.owner_clear_due.load(Ordering::SeqCst)
            && !i.owner_clear_in_flight.load(Ordering::SeqCst)
            && i.shutdowns_pending.load(Ordering::SeqCst) == 0
            && !i.beat_in_flight.load(Ordering::SeqCst)
            && !i.housekeeping_writes.load(Ordering::SeqCst)
            && i.chat_notes.load(Ordering::SeqCst) == 0
    }

    fn capacity_fault(&self) -> bool {
        self.inner.fault.load(Ordering::SeqCst)
    }

    fn owns_chat(&self) -> bool {
        self.inner.owns_chat.load(Ordering::SeqCst)
    }

    fn standdown_idle(&self) -> bool {
        let i = &self.inner;
        !i.owner_clear_due.load(Ordering::SeqCst) && !i.owner_clear_in_flight.load(Ordering::SeqCst)
    }

    fn release_owner_clear(&self) {
        let i = &self.inner;
        if i.owner_clear_due.load(Ordering::SeqCst)
            && !i.owner_clear_released.swap(true, Ordering::SeqCst)
        {
            i.standdown_kick.store(true, Ordering::SeqCst);
            i.shared.pool.notify();
        }
    }

    fn release_shutdown_beats(&self) {
        let i = &self.inner;
        if !i.shutdowns_released.swap(true, Ordering::SeqCst) {
            i.shared.pool.notify();
        }
    }
}

impl Inner {
    fn new(shared: &Arc<Shared>, config: ExecutionConfig, parts: &ExecutionParts) -> Arc<Inner> {
        let mut scheduler = {
            let now = parts.now.clone();
            Scheduler::new(&config.self_peer, Box::new(move || now()))
        };
        let watcher = Watcher::new(config.agent_name.clone());
        // dispatch itself is gated on AppliedOpen; the scheduler's own boot
        // disarm (desktop: until overlays sync) is preparation's job here
        scheduler.arm();
        Arc::new_cyclic(|me| Inner {
            me: me.clone(),
            shared: shared.clone(),
            config,
            client: parts.client.clone(),
            status: parts.status.clone(),
            clock: parts.clock.clone(),
            rss: parts.rss.clone(),
            now: parts.now.clone(),
            config_store: parts.config_store.clone(),
            traces: parts.traces.clone(),
            lanes: OnceLock::new(),
            gen: AtomicU64::new(0),
            activation_gen: AtomicU64::new(0),
            open: AtomicBool::new(false),
            owns_chat: AtomicBool::new(false),
            inflight: AtomicUsize::new(0),
            queue_len: AtomicUsize::new(0),
            fault: AtomicBool::new(false),
            owner_clear_due: AtomicBool::new(false),
            owner_clear_in_flight: AtomicBool::new(false),
            owner_clear_released: AtomicBool::new(false),
            standdown_kick: AtomicBool::new(false),
            shutdowns_pending: AtomicUsize::new(0),
            beat_in_flight: AtomicBool::new(false),
            shutdowns_released: AtomicBool::new(false),
            housekeeping_writes: AtomicBool::new(false),
            chat_notes: AtomicUsize::new(0),
            reconcile_now: AtomicBool::new(false),
            woken: AtomicBool::new(false),
            reports: AtomicU64::new(0),
            standdown_failures: AtomicU64::new(0),
            bookkeeping_failures: AtomicU64::new(0),
            reconcile_passes: AtomicU64::new(0),
            beat_passes: AtomicU64::new(0),
            retention_wakes: AtomicU64::new(0),
            sweeps: AtomicU64::new(0),
            state: Mutex::new(State {
                grant: None,
                registry: BTreeMap::new(),
                scheduler,
                queue: PendingQueue::default(),
                scan_refused: false,
                jobs: BTreeMap::new(),
                reserved: BTreeMap::new(),
                breaks: Vec::new(),
                owner_clear: None,
                beat_owed: None,
                shutdowns: VecDeque::new(),
                settling: BTreeSet::new(),
                sched_rev: 0,
                touched: BTreeMap::new(),
                standdown_exited: false,
                beats_exited: false,
                watcher,
                chat: None,
                parked: VecDeque::new(),
                sources: BTreeMap::new(),
                sources_noted: BTreeSet::new(),
            }),
            #[cfg(test)]
            before_scan_lock: Mutex::new(None),
        })
    }

    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn wake(&self) {
        self.woken.store(true, Ordering::SeqCst);
        self.shared.pool.notify();
    }

    fn publish(&self) {
        let u8_of = |n: usize| n.min(u8::MAX as usize) as u8;
        let u12_of = |n: u64| n.min(0xfff) as u16;
        let snapshot = ExecutionSnapshot {
            installed: true,
            open: self.open.load(Ordering::SeqCst),
            owns_chat: self.owns_chat.load(Ordering::SeqCst),
            standdown_pending: self.owner_clear_due.load(Ordering::SeqCst)
                || self.owner_clear_in_flight.load(Ordering::SeqCst),
            shutdown_beat_pending: self.shutdowns_pending.load(Ordering::SeqCst) > 0,
            capacity_fault: self.fault.load(Ordering::SeqCst),
            beat_in_flight: self.beat_in_flight.load(Ordering::SeqCst),
            in_flight: u8_of(self.inflight.load(Ordering::SeqCst)),
            queued: u8_of(self.queue_len.load(Ordering::SeqCst)),
            activation: self.activation_gen.load(Ordering::SeqCst) as u16,
            standdown_failures: u12_of(self.standdown_failures.load(Ordering::SeqCst)),
            bookkeeping_failures: u12_of(self.bookkeeping_failures.load(Ordering::SeqCst)),
        };
        self.shared
            .execution_word
            .store(snapshot.word(), Ordering::Release);
    }

    fn cancelled(&self) -> bool {
        self.shared.cancel.is_cancelled()
    }

    /// A pool wait that ends on cancel, on `wake` (or `flag`), or at `timeout`;
    /// the predicate reads atomics only (it runs under the pool's lock).
    fn wait(&self, ctx: &TaskContext, timeout: Duration, flag: &AtomicBool) {
        ctx.wait(WaitClass::Idle, None);
        self.shared.pool.wait_until(timeout, || {
            self.shared.cancel.is_cancelled() || flag.swap(false, Ordering::SeqCst)
        });
        ctx.running();
    }

    // --- the dispatcher (`Task::Service`) ---------------------------------------------

    fn service_loop(&self, ctx: &TaskContext) {
        let mut next_sample = Instant::now();
        // desktop's ticker sleeps its period before the first scan
        let mut next_scan = Instant::now() + self.config.cadence.scan;
        let mut next_converge = Instant::now();
        while !self.cancelled() {
            let now = Instant::now();
            let cadence = self.config.cadence;
            if now >= next_sample {
                self.sample_rss();
                next_sample = now + cadence.sample;
            }
            if now >= next_scan {
                self.due_scan();
                next_scan = now + cadence.scan;
            }
            if now >= next_converge {
                self.converge_sources();
                next_converge = now + cadence.converge;
            }
            self.dispatch();
            self.flush_parked();
            self.soft_break_deadlines(now);
            self.publish();
            self.wait(ctx, cadence.wake, &self.woken);
        }
        self.publish();
    }

    /// ADR-034 §2 (user ruling 2026-11-06): the worker samples its own
    /// `VmRSS` and reports it with the sample's boot-clock time; it closes
    /// nothing on memory. A failed sample keeps the previous one, whose age
    /// main judges.
    fn sample_rss(&self) {
        let (Some(bytes), Ok(at)) = ((self.rss)(), self.clock.now_ms()) else {
            return;
        };
        self.shared.rss_bytes.store(bytes, Ordering::Relaxed);
        self.shared
            .rss_sampled_at
            .store(at.saturating_add(1), Ordering::Release);
    }

    /// Desktop's due selection (`serve::trigger_ticker`) on the real registry
    /// entries — `cron_due` arms a `None` deadline forward on the entry it is
    /// given (D6) — handed to the pending queue instead of run inline. A
    /// reserved trigger is skipped until its report returned (D3).
    fn due_scan(&self) {
        #[cfg(test)]
        self.test_before_scan_lock();
        let mut refused = false;
        {
            let mut st = self.lock();
            // under the lock a close takes: a scan racing a Release finds it
            // closed and arms or queues nothing (round A review A4)
            if !self.open.load(Ordering::SeqCst) {
                return;
            }
            let State {
                registry,
                scheduler,
                queue,
                reserved,
                scan_refused,
                settling,
                ..
            } = &mut *st;
            for t in registry.values_mut() {
                // reserved until its report's last write (A3); settling until
                // its housekeeping write landed (A1)
                if reserved.contains_key(&t.id) || settling.contains(&t.id) {
                    continue;
                }
                let due = (t.kind == "cron" && scheduler.cron_due(t)) || scheduler.once_due(t);
                if !due {
                    continue;
                }
                let work = PendingWork {
                    id: t.id.clone(),
                    lane: Lane::Scheduled,
                    heartbeat: is_heartbeat(&t.program, &self.config.space),
                };
                if queue.push(work, &self.shared.pool).is_err() {
                    // counted on the pool where it is refused (R2-C8)
                    refused = true;
                }
            }
            *scan_refused = refused;
            self.queue_len.store(queue.len(), Ordering::SeqCst);
            self.fault
                .store(refused || queue.len() == MAX_PENDING, Ordering::SeqCst);
        }
        if refused {
            warn!("pending queue saturated ({MAX_PENDING}): due work refused, a capacity fault");
        }
    }

    /// Admission (ADR-034 §2/§7): while open, with a fresh grant and an open
    /// lane gate, each queued descriptor that gets its lane's permit is bound.
    fn dispatch(&self) {
        let Some(lanes) = self.lanes.get() else {
            return;
        };
        {
            let mut st = self.lock();
            while self.open.load(Ordering::SeqCst)
                && st.grant_fresh()
                && self.shared.pool.admission_open()
            {
                let Some((work, permit)) = st.queue.admit(&self.shared.pool) else {
                    break;
                };
                self.bind(&mut st, lanes, work, permit);
            }
            self.queue_len.store(st.queue.len(), Ordering::SeqCst);
            self.fault.store(
                st.scan_refused || st.queue.len() == MAX_PENDING,
                Ordering::SeqCst,
            );
        }
        self.publish();
    }

    /// Consume under the permit, reserve, offer (design §3.5). The occurrence
    /// is revalidated on the real entry: a trigger disabled, repinned or
    /// evicted since its scan consumes nothing and its permit is dropped (E20).
    fn bind(&self, st: &mut State, lanes: &Lanes, work: PendingWork, permit: StorePermit) {
        if st.reserved.contains_key(&work.id) || st.settling.contains(&work.id) {
            return;
        }
        let bound = {
            let State {
                registry,
                scheduler,
                ..
            } = &mut *st;
            registry.get_mut(&work.id).and_then(|entry| {
                let once = entry.kind == "once";
                // a once is consumed as desktop's ticker consumes it, by
                // `enabled = false`; the reservation keeps a reconcile from
                // reading the still-`enabled: true` row back in until the
                // report persisted it (round A review A3; D3/E9)
                if !permit.consume_scheduled(scheduler, entry) {
                    return None;
                }
                Some((entry.program.clone(), entry.args.clone(), once))
            })
        };
        let Some((spec, args, once)) = bound else {
            return;
        };
        let run_id = new_run_id();
        let job = LaneJob {
            lane: Lane::Scheduled,
            work_id: work.id.clone(),
            run_id: run_id.clone(),
            spec,
            args,
            trigger: Some(work.id.clone()),
            mailbox: Default::default(),
            interrupt: Arc::new(AtomicBool::new(false)),
        };
        st.reserved.insert(work.id.clone(), run_id.clone());
        st.jobs.insert(
            run_id.clone(),
            JobKind::Scheduled {
                trigger: work.id.clone(),
                once,
            },
        );
        self.inflight.store(st.jobs.len(), Ordering::SeqCst);
        if let Err(refused) = lanes.offer(job, permit, &self.shared) {
            // the one reachable refusal: the pool closed between `acquire`
            // and `offer` (D13) — the job never started, so it is finalized
            // `Cancelled` at once, its input restored
            drop(refused);
            info!("trigger {:?}: offer refused (stopping), not run", work.id);
            self.cancelled_scheduled(st, &run_id, &work.id, once);
            st.jobs.remove(&run_id);
            self.inflight.store(st.jobs.len(), Ordering::SeqCst);
        }
    }

    /// A scheduled job that never started: nothing persisted, its reservation
    /// released, a once shot's in-memory consumption restored (design §3.7).
    fn cancelled_scheduled(&self, st: &mut State, run_id: &str, trigger: &str, once: bool) {
        if st.reserved.get(trigger).map(String::as_str) == Some(run_id) {
            st.reserved.remove(trigger);
        }
        if once {
            if let Some(entry) = st.registry.get_mut(trigger).filter(|e| e.kind == "once") {
                entry.enabled = true;
                entry.last_run_at = None;
            }
        }
    }

    /// The soft-break escalation (ADR-005 §3) at its tracked deadline — no
    /// detached sleeper (ADR-034 §4). Deadlines survive a non-revoking close;
    /// an interrupt clears them; a finished run's deadline is dropped.
    fn soft_break_deadlines(&self, now: Instant) {
        let due: Vec<String> = {
            let mut st = self.lock();
            let (due, waiting): (Vec<_>, Vec<_>) = std::mem::take(&mut st.breaks)
                .into_iter()
                .partition(|(at, _)| now >= *at);
            st.breaks = waiting;
            due.into_iter().map(|(_, run)| run).collect()
        };
        if due.is_empty() {
            return;
        }
        let runs = self
            .shared
            .live_runs
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        for run in due {
            if let Some(live) = runs.get(&run) {
                escalate_soft_break(&self.config.chat, live);
            }
        }
    }

    // --- the stand-down write (`Task::Standdown`) -----------------------------------

    /// Its own owned thread (round A review A7): the write may block for its
    /// whole HTTP budget, and nothing else — dispatch, soft-break escalation,
    /// the memory sample — waits on it, as desktop's escalation is independent
    /// of its election thread's write. On cancel the epilogue settles a write
    /// still due; it runs also when the thread first starts after the cancel
    /// (`Task::runs_after_cancel`, A6).
    fn standdown_loop(&self, ctx: &TaskContext) {
        while !self.cancelled() {
            self.owner_clear();
            self.wait(ctx, self.config.cadence.wake, &self.standdown_kick);
        }
        // the transport refuses everything once the owner is cancelled: a
        // stand-down write still due will never land — unsettled, counted;
        // under `state`, so a close after this marks none (`standdown_exited`)
        {
            let mut st = self.lock();
            st.standdown_exited = true;
            if self.owner_clear_due.swap(false, Ordering::SeqCst) {
                st.owner_clear = None;
                self.standdown_failures.fetch_add(1, Ordering::SeqCst);
                warn!("chat responder stand-down write unsettled: the owner was cancelled");
            }
        }
        self.publish();
    }

    /// The one stand-down write (desktop `serve::election_thread`), once the
    /// session released it (no takeover in flight, E5), outside `state`. A
    /// failure is logged and counted and does not hold quiescence: the next
    /// winner's takeover re-stamps the record (ADR-018 §3, last write wins).
    fn owner_clear(&self) {
        // a conversation-start write of the responder (owner = this peer)
        // still in flight lands first; its end kicks this thread again
        if !self.owner_clear_due.load(Ordering::SeqCst)
            || !self.owner_clear_released.load(Ordering::SeqCst)
            || self.chat_notes.load(Ordering::SeqCst) > 0
        {
            return;
        }
        let taken = {
            let mut st = self.lock();
            // in flight before due is cleared: `standdown_idle` never sees
            // both false while the write is outstanding
            self.owner_clear_in_flight.store(true, Ordering::SeqCst);
            self.owner_clear_due.store(false, Ordering::SeqCst);
            st.owner_clear.take()
        };
        self.publish();
        if let Some(t) = taken {
            let triggers = &self.config.triggers;
            if let Err(e) = release_chat_watch(
                &self.client,
                &self.config.space,
                &triggers.object_id,
                &triggers.collection,
                &t,
            ) {
                self.standdown_failures.fetch_add(1, Ordering::SeqCst);
                warn!("chat responder {:?}: stand-down write failed: {e}", t.id);
            } else {
                info!("chat responder {:?}: released (owner cleared)", t.id);
            }
        }
        self.owner_clear_in_flight.store(false, Ordering::SeqCst);
        self.publish();
    }

    // --- report bookkeeping (on the lane thread) ----------------------------------

    /// Desktop runs its bookkeeping inline on the run thread; this runs it on
    /// the lane thread that ran the job, HTTP outside `state`. The job counts
    /// toward quiescence until this returns (D2).
    fn on_report(&self, report: LaneReport) {
        let leftovers = take_leftovers();
        let job = self.lock().jobs.get(&report.run_id).cloned();
        match job {
            Some(JobKind::Scheduled { trigger, once }) => {
                if !leftovers.is_empty() || !report.refused_input.is_empty() {
                    warn!(
                        "trigger {trigger:?}: {} undrained mailbox item(s) dropped (nothing \
                         injects into a scheduled run)",
                        leftovers.len() + report.refused_input.len()
                    );
                }
                self.finish_scheduled(&report, &trigger, once);
            }
            Some(JobKind::Chat {
                user_text,
                responder,
            }) => self.finish_chat(&report, &user_text, responder.as_deref(), leftovers),
            Some(JobKind::Event { trigger }) => {
                if !leftovers.is_empty() || !report.refused_input.is_empty() {
                    warn!(
                        "event trigger {trigger:?}: {} undrained mailbox item(s) dropped (nothing \
                         injects into an event run)",
                        leftovers.len() + report.refused_input.len()
                    );
                }
                self.finish_scheduled(&report, &trigger, false);
            }
            None => warn!("lane report for an unknown job {:?}", report.run_id),
        }
        {
            let mut st = self.lock();
            if let Some(JobKind::Scheduled { trigger, .. } | JobKind::Event { trigger }) =
                st.jobs.remove(&report.run_id)
            {
                // the reservation ends with the job's last write (A3, ADR-034
                // §2): no rebind, and no stale reconcile, before it landed
                if st.reserved.get(&trigger) == Some(&report.run_id) {
                    st.reserved.remove(&trigger);
                    touch(&mut st, &trigger);
                }
            }
            self.inflight.store(st.jobs.len(), Ordering::SeqCst);
        }
        self.reports.fetch_add(1, Ordering::SeqCst);
        self.publish();
        self.wake();
    }

    /// `finish_run` for a scheduled or event job (design §3.7; desktop's
    /// `finish_run` serves both): `record_run` on the
    /// registry entry (the breaker; a no-op when the trigger was evicted), a
    /// once shot left disabled; then the run summary (desktop publishes it
    /// from the run itself) and the record, with `nextDue`. The reservation is
    /// released only after these writes (`on_report`). A run that never
    /// started persists nothing.
    fn finish_scheduled(&self, report: &LaneReport, trigger: &str, once: bool) {
        let rr = match &report.outcome.result {
            Ok(rr) => rr.clone(),
            Err(LaneFailure::RunPath(e)) | Err(LaneFailure::TraceUnpersisted(e)) => {
                RunResult::failed(e.clone())
            }
            Err(LaneFailure::Cancelled) => {
                let mut st = self.lock();
                self.cancelled_scheduled(&mut st, &report.run_id, trigger, once);
                return;
            }
        };
        let record = {
            let mut st = self.lock();
            let State {
                registry,
                scheduler,
                ..
            } = &mut *st;
            registry.get_mut(trigger).map(|entry| {
                scheduler.record_run(entry, &rr);
                if entry.kind == "once" {
                    // desktop disables the shot before its run and never
                    // re-enables it; a reconcile during the run may have read
                    // the still-enabled row back into memory
                    entry.enabled = false;
                }
                persisted_record(entry)
            })
        };
        let (space, runs, triggers) =
            (&self.config.space, &self.config.runs, &self.config.triggers);
        if let Some(summary) = report.outcome.summary.clone() {
            if let Err(e) = publish_run_summary(
                &self.client,
                space,
                &runs.object_id,
                &runs.collection,
                Some(&self.config.self_peer),
                summary,
                Some(trigger),
            ) {
                warn!("agent_runs {}: summary not published: {e}", report.run_id);
            }
        }
        if let Some(record) = record {
            if let Err(e) = self.client.upsert_record(
                space,
                &triggers.object_id,
                &triggers.collection,
                trigger,
                &record,
            ) {
                // ADR-034 §4: refused by the transport once the owner is
                // cancelled, or by the store; logged, never retried
                self.bookkeeping_failures.fetch_add(1, Ordering::SeqCst);
                warn!("trigger {trigger:?}: run bookkeeping not persisted: {e}");
            }
        }
    }

    // --- housekeeping (`Task::Housekeeping`) -----------------------------------------

    /// Desktop's reconcile pass (`serve::trigger_ticker`), only while open:
    /// the record read outside `state`, then `reconcile_registry` (this peer
    /// is the active device while open) and `health_pass` under it, then the
    /// adoption stamps and health marks persisted. A close since the read
    /// drops it (the registry is not converged on a stale read).
    fn housekeeping_loop(&self, ctx: &TaskContext) {
        let mut failing = false;
        loop {
            // like desktop's ticker, wait first: an Open's `reconcile_now`
            // ends the wait at once, so it is reconciled exactly once
            self.wait(ctx, self.config.cadence.reconcile, &self.reconcile_now);
            if self.cancelled() {
                return;
            }
            if self.open.load(Ordering::SeqCst) {
                self.reconcile(&mut failing);
            }
        }
    }

    /// One pass; true when it converged the registry (false: the read
    /// failed, or a close since the read dropped it).
    fn reconcile(&self, failing: &mut bool) -> bool {
        let gen = self.gen.load(Ordering::SeqCst);
        // before the read: a trigger touched after this is not in it (B1)
        let read_at = self.lock().sched_rev;
        let (space, triggers) = (&self.config.space, &self.config.triggers);
        let read = self
            .client
            .query(space, &triggers.object_id, &triggers.collection, &json!({}));
        let recs = match read {
            Ok(recs) => {
                if std::mem::take(failing) {
                    info!("agent_triggers reconcile recovered");
                }
                recs
            }
            Err(e) => {
                if !std::mem::replace(failing, true) {
                    warn!(
                        "agent_triggers reconcile query failed ({e}) — registry runs \
                         unrefreshed until it recovers"
                    );
                }
                return false;
            }
        };
        let (stamp, marked) = {
            let mut st = self.lock();
            if self.gen.load(Ordering::SeqCst) != gen || !self.open.load(Ordering::SeqCst) {
                return false;
            }
            let standing = standing_ids(space);
            let State {
                registry,
                scheduler,
                reserved,
                settling,
                touched,
                ..
            } = &mut *st;
            // a reserved trigger's entry carries runtime state its report has
            // not persisted yet (the breaker, `nextDue`, a consumed once), and
            // a trigger touched since the read began may be stale in it: both
            // sit the pass out, as on desktop, whose ticker never reconciles
            // while it runs a trigger inline (round A review A3, A-r2 B1)
            let skipped: BTreeSet<String> = reserved
                .keys()
                .cloned()
                .chain(
                    touched
                        .iter()
                        .filter(|(_, rev)| **rev > read_at)
                        .map(|(id, _)| id.clone()),
                )
                .collect();
            // older touches are reflected in this read
            touched.retain(|_, rev| *rev > read_at);
            let held: Vec<Trigger> = skipped
                .iter()
                .filter_map(|id| registry.remove(id))
                .collect();
            let recs: Vec<Value> = recs
                .into_iter()
                .filter(|r| {
                    r.get("id")
                        .and_then(Value::as_str)
                        .is_none_or(|id| !skipped.contains(id))
                })
                .collect();
            let stamp =
                reconcile_registry(registry, &recs, &self.config.self_peer, true, &standing);
            let marked = health_pass(scheduler, registry, &standing);
            for t in held {
                registry.insert(t.id.clone(), t);
            }
            self.owns_chat.store(
                owned_chat_watch(registry, &self.config.self_peer).is_some(),
                Ordering::SeqCst,
            );
            // the stamped entries are not dispatchable until their writes
            // landed (A1); set under the lock a close takes: a close after
            // this waits for the writes below, one before it dropped the pass
            settling.extend(stamp.iter().chain(&marked).map(|t| t.id.clone()));
            self.housekeeping_writes
                .store(!settling.is_empty(), Ordering::SeqCst);
            (stamp, marked)
        };
        for t in stamp.iter().chain(&marked) {
            if let Err(e) = self.client.upsert_record(
                space,
                &triggers.object_id,
                &triggers.collection,
                &t.id,
                &persisted_record(t),
            ) {
                warn!("trigger {:?}: stamp not persisted: {e}", t.id);
            }
        }
        {
            let mut st = self.lock();
            for t in stamp.iter().chain(&marked) {
                st.settling.remove(&t.id);
            }
            self.housekeeping_writes
                .store(!st.settling.is_empty(), Ordering::SeqCst);
        }
        for t in &stamp {
            info!("trigger adopted from dataset: {:?} ({})", t.id, t.kind);
        }
        self.publish();
        self.reconcile_passes.fetch_add(1, Ordering::SeqCst);
        true
    }

    // --- trace retention (`Task::Retention`) ----------------------------------------

    /// Desktop's `retention_thread` (ADR-023 §6): a minute after start, then
    /// hourly, in every phase — the device-local trace routes are allowed in
    /// every phase (user ruling 2026-11-06, design §10 R7). The sweep ends at
    /// its next request once the owner is cancelled (the cancel is its stop
    /// flag, and the transport refuses every request after it). With nothing
    /// configured the thread only waits: `Task::Retention` exits on cancel.
    fn retention_loop(&self, ctx: &TaskContext) {
        let r = self.config.retention;
        let configured = r.conversations_s.is_some() || r.jobs_s.is_some() || r.max_bytes.is_some();
        let mut next = Instant::now() + self.config.cadence.retention_first;
        while !ctx.idle_until(next) {
            if configured {
                self.sweep(r);
            }
            self.retention_wakes.fetch_add(1, Ordering::SeqCst);
            next = Instant::now() + self.config.cadence.retention_every;
        }
    }

    /// One sweep (desktop `RunCtx::expire_traces`).
    fn sweep(&self, r: Retention) {
        let now = (self.now)();
        let conversations = r.conversations_s.map(|s| now - s as f64);
        let jobs = r.jobs_s.map(|s| now - s as f64);
        match self.traces.expire(
            chat::CHAT_PROGRAM,
            conversations,
            jobs,
            r.max_bytes,
            self.shared.cancel.flag(),
        ) {
            Ok(_) => {}
            Err(_) if self.cancelled() => info!("trace retention: sweep stopped for shutdown"),
            Err(e) => warn!("trace retention failed: {e:#}"),
        }
        self.sweeps.fetch_add(1, Ordering::SeqCst);
    }

    // --- responder beats (`Task::Presence`) ------------------------------------------

    /// The responder beat (ADR-034 §7): desktop's `presence_decide` cadence
    /// and envelope plus `data.sessionId`/`data.activation`, only while open;
    /// one `shutdown` beat per activation a revoking close ended, each settled
    /// by its own publish (round A review A5). On cancel the epilogue settles
    /// what is still due; it runs also when the thread first starts after the
    /// cancel (`Task::runs_after_cancel`, A6).
    fn presence_loop(&self, ctx: &TaskContext) {
        let mut st = PresenceLoop::default();
        let mut was_open = false;
        while !self.cancelled() {
            self.shutdown_beats();
            if self.open.load(Ordering::SeqCst) {
                if !was_open {
                    // a (re)open beats at once
                    st.last_beat = None;
                    was_open = true;
                }
                self.beat(&mut st);
            } else {
                was_open = false;
            }
            self.beat_passes.fetch_add(1, Ordering::SeqCst);
            ctx.idle_until(Instant::now() + self.config.cadence.beat);
        }
        // the transport refuses everything once the owner is cancelled: a
        // `shutdown` beat still due will never go out — settled unsent, so it
        // holds no quiescence (as the stand-down thread settles its write);
        // under `state`, so a close after this marks none (`beats_exited`)
        {
            let mut st = self.lock();
            st.beats_exited = true;
            let unsent = std::mem::take(&mut st.shutdowns).len();
            if unsent > 0 {
                self.shutdowns_pending.fetch_sub(unsent, Ordering::SeqCst);
                info!("{unsent} bao.status shutdown beat(s) not sent: the owner was cancelled");
            }
        }
        self.publish();
    }

    fn role(&self, responder: bool) -> BeatRole {
        BeatRole {
            responder,
            winner: Some(self.config.self_peer.clone()),
        }
    }

    fn beat(&self, loop_state: &mut PresenceLoop) {
        let key = {
            let st = self.lock();
            // under the lock a close takes: a close after this waits for the
            // publish (counted in quiescence), one before it is seen here
            if !self.open.load(Ordering::SeqCst) {
                return;
            }
            let Some(key) = st.beat_owed.clone() else {
                return;
            };
            self.beat_in_flight.store(true, Ordering::SeqCst);
            key
        };
        let role = self.role(self.owns_chat.load(Ordering::SeqCst));
        let run = current_run(
            &self
                .shared
                .live_runs
                .lock()
                .unwrap_or_else(|e| e.into_inner()),
        );
        let now = (self.now)();
        if let Some(state) = presence_decide(&self.status, run.as_ref(), &role, loop_state, now) {
            let _ = self.publish_beat(state, run, &role, &key, now);
        }
        self.beat_in_flight.store(false, Ordering::SeqCst);
        self.publish();
    }

    /// Every `shutdown` beat due, oldest first, once the session released
    /// them (B2); each stays counted until its own publish attempt returned,
    /// so one activation's beat never settles another's. A publish the host
    /// transport itself refuses (its phase, or the owner's cancel) is not an
    /// attempt: the beat stays owed and is retried on a later pass, or
    /// settled unsent by the epilogue after a cancel. Any other failure is a
    /// best-effort beat that did not land, as on desktop.
    fn shutdown_beats(&self) {
        while self.shutdowns_released.load(Ordering::SeqCst) {
            let Some(key) = self.lock().shutdowns.pop_front() else {
                return;
            };
            let now = (self.now)();
            if let Err(e) = self.publish_beat("shutdown", None, &self.role(false), &key, now) {
                if refused_by_gate(&e) {
                    self.lock().shutdowns.push_front(key);
                    return;
                }
            }
            self.shutdowns_pending.fetch_sub(1, Ordering::SeqCst);
            self.publish();
        }
    }

    fn publish_beat(
        &self,
        state: &str,
        run: Option<Value>,
        role: &BeatRole,
        key: &BeatKey,
        now: f64,
    ) -> Result<(), crate::anyapi::AnyError> {
        let mut envelope =
            status_envelope(&self.config.self_peer, &self.status, run, state, role, now);
        envelope["data"]["sessionId"] = json!(key.session_id);
        envelope["data"]["activation"] = json!({
            "activationId": key.activation.activation_id,
            "intentRevision": key.activation.intent_revision,
        });
        let published = self.client.publish_event(&envelope).map(|_| ());
        if let Err(e) = &published {
            if is_signed_out(e) {
                debug!("bao.status beat not published: {e}");
            } else {
                warn!("bao.status beat not published: {e}");
            }
        }
        published
    }

    #[cfg(test)]
    fn test_before_scan_lock(&self) {
        let hook = self
            .before_scan_lock
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take();
        if let Some(hook) = hook {
            hook();
        }
    }
}

#[cfg(test)]
mod tests;
