//! Managed startup ownership (ADR-034 §4/§7).

pub mod admission;
pub(crate) mod execution;
mod frozen;
pub mod grant;
pub(crate) mod lanes;
mod options;
mod preparation;
pub mod protocol;
mod service;
pub(crate) mod session;
pub(crate) mod takeover;
pub(crate) mod transport;
pub use execution::ExecutionSnapshot;
pub use options::{ControlApi, ElectionPolicy, ManagedOptions, TrustedArtifact};
pub use preparation::{
    FrozenPins, FrozenProgram, FrozenSkill, PreparationStatus, PreseededFixture, SetupReason,
    StoreAddress,
};
pub use service::{ManagedTimeContext, Readiness, TimeContextStale, ZoneSample};
pub use session::{CommandPort, PortFull, SessionSnapshot};

use crate::Config;
use admission::{GateCause, Lane, LanePool};
use std::cell::Cell;
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const SUPERVISOR_PERIOD: Duration = Duration::from_secs(1);
const SUPERVISOR_STALE: Duration = Duration::from_secs(30);
const STOP_REQUESTED: u64 = 1;
const ALL_JOINED: u64 = 2;
const FAILURE_SHIFT: u32 = 8;

/// Cancellation exists before startup. Cancelling any clone wakes managed waits;
/// it cannot interrupt an already-running blocking socket or resolver call.
#[derive(Clone, Default)]
pub struct CancelToken(Arc<Cancellation>);

#[derive(Default)]
struct Cancellation {
    cancelled: AtomicBool,
    wake: Condvar,
    lock: Mutex<()>,
}

impl CancelToken {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn cancel(&self) {
        let _guard = self.0.lock.lock().unwrap_or_else(|e| e.into_inner());
        self.0.cancelled.store(true, Ordering::Release);
        self.0.wake.notify_all();
    }

    pub fn is_cancelled(&self) -> bool {
        self.0.cancelled.load(Ordering::Acquire)
    }

    /// The cancellation as the flag a desktop-shaped loop polls (the
    /// retention sweep's `stop`, N2b-2 C3).
    pub(crate) fn flag(&self) -> &AtomicBool {
        &self.0.cancelled
    }

    /// Returns true on cancellation, false on reaching the deadline.
    pub fn wait_until(&self, deadline: Instant) -> bool {
        let mut guard = self.0.lock.lock().unwrap_or_else(|e| e.into_inner());
        while !self.is_cancelled() {
            let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
                return false;
            };
            guard = self
                .0
                .wake
                .wait_timeout(guard, remaining)
                .unwrap_or_else(|e| e.into_inner())
                .0;
        }
        true
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ManagedFailure {
    InvalidConfiguration,
    IncompatibleArtifact,
    PreparationSpawnFailed,
    SupervisorSpawnFailed,
    PreparationPanicked,
    SupervisorPanicked,
    PreparationExitedUnexpectedly,
    SupervisorExitedUnexpectedly,
    PreparationWaitExpired,
    SupervisorStale,
    /// An owned execution thread (epoch driver, lane worker) could not start.
    ExecutionSpawnFailed,
    /// An owned execution thread panicked: the worker retires (ADR-034 §2).
    ExecutionPanicked,
    ExecutionExitedUnexpectedly,
    /// A spawn refused because the owner is already stopping: not a fault,
    /// never recorded on the status word.
    Cancelled,
}

impl ManagedFailure {
    fn bit(self) -> u64 {
        1 << (FAILURE_SHIFT + self as u32)
    }
}

impl std::fmt::Display for ManagedFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for ManagedFailure {}

const FAILURES: [ManagedFailure; 13] = [
    ManagedFailure::InvalidConfiguration,
    ManagedFailure::IncompatibleArtifact,
    ManagedFailure::PreparationSpawnFailed,
    ManagedFailure::SupervisorSpawnFailed,
    ManagedFailure::PreparationPanicked,
    ManagedFailure::SupervisorPanicked,
    ManagedFailure::PreparationExitedUnexpectedly,
    ManagedFailure::SupervisorExitedUnexpectedly,
    ManagedFailure::PreparationWaitExpired,
    ManagedFailure::SupervisorStale,
    ManagedFailure::ExecutionSpawnFailed,
    ManagedFailure::ExecutionPanicked,
    ManagedFailure::ExecutionExitedUnexpectedly,
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ManagedPhase {
    Preparing,
    Stopping,
    Failed,
    Drained,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TaskPhase {
    NotStarted,
    Running,
    Waiting,
    Exited,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WaitClass {
    None,
    Initialization,
    Http,
    Dns,
    Stream,
    Idle,
}

#[derive(Clone, Copy, Debug)]
pub struct TaskSnapshot {
    pub phase: TaskPhase,
    pub wait_class: WaitClass,
    /// Failure deadline for a bounded operation, never an intentional idle wake.
    pub wait_deadline: Option<Instant>,
    pub progress: u64,
}

#[derive(Clone, Debug)]
pub struct HealthSnapshot {
    pub phase: ManagedPhase,
    pub failures: Vec<ManagedFailure>,
    pub cancelled: bool,
    /// AppliedOpen is in force: set only by the session's `complete_open`
    /// after checked takeover, cleared on every close (ADR-034 §5/§7). Read
    /// from the session word, so it never disagrees with `session.phase`.
    pub admission_open: bool,
    pub preparation_status: PreparationStatus,
    pub preparation: TaskSnapshot,
    pub supervisor: TaskSnapshot,
    pub supervisor_age: Duration,
    /// The epoch driver and the chat/scheduled/event lane workers (N2a).
    pub epoch: TaskSnapshot,
    pub lanes: [TaskSnapshot; 3],
    /// The command consumer and the owned takeover task (N2b-1), and the
    /// session as the consumer last published it (one atomic word).
    pub consumer: TaskSnapshot,
    pub takeover: TaskSnapshot,
    pub session: SessionSnapshot,
    pub live_stores: usize,
    /// The Android zone sample is absent, not yet valid or expired: `time.now`
    /// refuses. Computed from the sample itself, never a latch.
    pub time_context_stale: bool,
    /// Host-buffer saturations seen so far (mailbox, backlog, pending queue,
    /// leftovers, stream line), counted where each bound is enforced: visible
    /// capacity faults, never silent drops (ADR-034 §2).
    pub capacity_faults: u64,
    /// Commands the bounded port refused (`PortFull`). Oneway Binder delivery
    /// cannot tell main; main's 5 s receipt budget does, and this says so here.
    pub commands_refused: u64,
    /// Jobs a panicking lane worker could not report back.
    pub orphaned_jobs: u64,
    /// Agent-authored programs refused typed on the frozen host (ADR-034 §2),
    /// summed over the run results the lanes report.
    pub refused_programs: u64,
    /// The reopenable admission gate of the lane pool: subordinate to
    /// `admission_open` — closed until the first AppliedOpen and whenever any
    /// stand-down cause holds.
    pub lane_admission_open: bool,
    /// The execution service's threads (N2b-2): dispatcher, responder beats,
    /// housekeeping, retention, the stand-down write, the chat watch and the
    /// four event sources.
    pub service: TaskSnapshot,
    pub presence: TaskSnapshot,
    pub housekeeping: TaskSnapshot,
    pub retention: TaskSnapshot,
    pub standdown: TaskSnapshot,
    pub chat_watch: TaskSnapshot,
    pub event_sources: [TaskSnapshot; 4],
    /// The execution service as it last published itself (one atomic word);
    /// all false/zero while none is installed.
    pub execution: ExecutionSnapshot,
    /// The worker's own resident set (`VmRSS`, bytes) at its last successful
    /// sample, and that sample's CLOCK_BOOTTIME (= Android elapsedRealtime)
    /// in ms: reported only — Kotlin main judges staleness and enforces every
    /// memory rule (ADR-034 §2, user ruling 2026-11-06). None before the
    /// first sample.
    pub rss_bytes: Option<u64>,
    pub rss_sampled_at_ms: Option<u64>,
    pub all_threads_joined: bool,
}

/// Failed means all owned threads were joined, but at least one task failed.
/// DeadlineExceeded is never a drain/death confirmation; health retains failures.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DrainOutcome {
    Drained,
    DeadlineExceeded,
    Failed(Vec<ManagedFailure>),
}

#[derive(Default)]
struct TaskProgress {
    // One atomic publication for the phase, wait class and relative deadline.
    state: AtomicU64,
    progress: AtomicU64,
}

impl TaskProgress {
    fn publish(&self, phase: TaskPhase, wait: WaitClass, deadline_ms: u64) {
        self.state.store(
            phase as u64 | ((wait as u64) << 8) | (deadline_ms.min(u64::MAX >> 16) << 16),
            Ordering::Release,
        );
        self.progress.fetch_add(1, Ordering::Relaxed);
    }

    fn snapshot(&self, started: Instant) -> TaskSnapshot {
        let state = self.state.load(Ordering::Acquire);
        let phase = match state & 0xff {
            0 => TaskPhase::NotStarted,
            1 => TaskPhase::Running,
            2 => TaskPhase::Waiting,
            _ => TaskPhase::Exited,
        };
        let wait_class = match (state >> 8) & 0xff {
            1 => WaitClass::Initialization,
            2 => WaitClass::Http,
            3 => WaitClass::Dns,
            4 => WaitClass::Stream,
            5 => WaitClass::Idle,
            _ => WaitClass::None,
        };
        let deadline_ms = state >> 16;
        TaskSnapshot {
            phase,
            wait_class,
            wait_deadline: (deadline_ms != 0)
                .then(|| started + Duration::from_millis(deadline_ms - 1)),
            progress: self.progress.load(Ordering::Relaxed),
        }
    }
}

/// Every owned thread has a fixed slot (ADR-034 §7: per-task atomic progress).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Task {
    Preparation,
    Supervisor,
    Epoch,
    LaneChat,
    LaneScheduled,
    LaneEvent,
    /// The command consumer (N2b-1): lives until the owner releases it.
    Consumer,
    /// One owned takeover per accepted initial Open (N2b-1): finishes by
    /// design and is joined by the consumer's polling.
    Takeover,
    /// The execution service (N2b-2, ADR-034 §2): the dispatcher.
    Service,
    /// The responder beats (ADR-034 §7), only while Open.
    Presence,
    /// Registry reads and stamps while Open.
    Housekeeping,
    /// Trace retention (ADR-023 §6).
    Retention,
    /// The execution service's one stand-down write (N2b-2 round A review A7).
    Standdown,
    /// The chat watch (ADR-018 §3).
    ChatWatch,
    /// Up to four event-source streams (ADR-018 §2): a retired source exits
    /// by design, and its slot is reused once the owner joined it.
    EventSource0,
    EventSource1,
    EventSource2,
    EventSource3,
}
const TASKS: usize = 18;
impl Task {
    #[allow(dead_code)] // the execution service (N2b-2) spawns the lanes in production
    pub(crate) fn lane(lane: Lane) -> Task {
        match lane {
            Lane::Chat => Task::LaneChat,
            Lane::Scheduled => Task::LaneScheduled,
            Lane::Event => Task::LaneEvent,
        }
    }
    fn name(self) -> &'static str {
        match self {
            Task::Preparation => "anyrt-prepare",
            Task::Supervisor => "anyrt-supervisor",
            Task::Epoch => "anyrt-epoch",
            Task::LaneChat => "anyrt-lane-chat",
            Task::LaneScheduled => "anyrt-lane-scheduled",
            Task::LaneEvent => "anyrt-lane-event",
            Task::Consumer => "anyrt-consumer",
            Task::Takeover => "anyrt-takeover",
            Task::Service => "anyrt-service",
            Task::Presence => "anyrt-presence",
            Task::Housekeeping => "anyrt-housekeeping",
            Task::Retention => "anyrt-retention",
            Task::Standdown => "anyrt-standdown",
            Task::ChatWatch => "anyrt-chat-watch",
            Task::EventSource0 => "anyrt-event-0",
            Task::EventSource1 => "anyrt-event-1",
            Task::EventSource2 => "anyrt-event-2",
            Task::EventSource3 => "anyrt-event-3",
        }
    }
    /// A task that returns before cancellation is an unexpected exit — except
    /// the takeover and an event source, whose jobs are finite (a source the
    /// registry no longer wants exits at its next frame, design D17).
    fn exit_is_failure(self) -> bool {
        !matches!(
            self,
            Task::Takeover
                | Task::EventSource0
                | Task::EventSource1
                | Task::EventSource2
                | Task::EventSource3
        )
    }
    /// The wrapper skips a body whose thread starts after a cancel — except
    /// those whose job includes the cancel: the epoch driver (a store attached
    /// before the cancel must still be ticked until it drops, or a
    /// compute-bound guest is never interrupted, sb-fix review OWN-3; it
    /// exits once nothing is occupied), the supervisor (an external token
    /// cancel reaches the pool and the live runs only through its `on_stop`,
    /// sb-fix r2 review OWN-R2-1; it runs that at once and exits), and the
    /// execution service's stand-down and beat threads (their epilogues
    /// settle the obligations a close marked, which nothing else could, N2b-2
    /// round A review A6; their loops end at once).
    fn runs_after_cancel(self) -> bool {
        matches!(
            self,
            Task::Epoch | Task::Supervisor | Task::Standdown | Task::Presence
        )
    }
}

thread_local! {
    /// The owned task running on this thread, so shared helpers (the gated
    /// transport) publish into the caller's own slot, never a fixed one (N1-R6).
    static CURRENT_TASK: Cell<Option<Task>> = const { Cell::new(None) };
}

pub(crate) struct Shared {
    pub(crate) cancel: CancelToken,
    started: Instant,
    status: AtomicU64,
    supervisor_beat_ms: AtomicU64,
    preparation_status: AtomicU64,
    tasks: [TaskProgress; TASKS],
    /// What preparation retains for the owned run service (ADR-034 §5): set once
    /// preparation succeeds, cleared by its task's slot guard. `health()` never reads it.
    service: Mutex<Option<Arc<service::PreparedService>>>,
    /// The lane/memory pool of this generation; closed with the owner.
    pub(crate) pool: LanePool,
    /// Threads spawned by the running service (epoch driver, lanes, N2b's
    /// consumer/watchers): joined by `drain_until`/Drop exactly like the
    /// owner's own two.
    owned: Mutex<Vec<(Task, JoinHandle<()>)>>,
    time: Option<ManagedTimeContext>,
    /// Every run in flight (ADR-034 §7): the lane workers register under this
    /// lock, `interrupt_all` reads under it.
    pub(crate) live_runs: lanes::LiveRuns,
    /// Jobs accepted by a lane inbox but not yet registered as runs, by run id,
    /// with the revocation epoch of their permit (`StorePermit::epoch`): a job
    /// that registers after `interrupt_all`'s scan compares it with the pool's
    /// and interrupts itself, so neither the acquire→offer nor the
    /// take→register handoff can be missed (R2-C5, Step B review F1).
    pub(crate) pending_interrupts: Mutex<BTreeMap<String, (Arc<AtomicBool>, u64)>>,
    /// Jobs a panicking lane worker could not report (counted, never dropped silently).
    orphaned: AtomicU64,
    /// Agent-authored programs refused typed (`NotPinned`) across runs (ADR-034 §2).
    pub(crate) refused_programs: AtomicU64,
    /// Commands `CommandPort::offer` refused (shared with the port).
    pub(crate) commands_refused: Arc<AtomicU64>,
    /// The session's packed `SessionSnapshot` (admission included), published
    /// by the consumer.
    session_word: AtomicU64,
    /// Set by `drain_until`/Drop before joining: the consumer exits on this,
    /// never on cancellation alone, so it answers status after `Drained`.
    consumer_release: AtomicBool,
    /// What `attach` hands the session: the leased origin and the fixture identity.
    session_config: Mutex<Option<session::SessionConfig>>,
    /// `attach` ran: one consumer, one session per owner (N2b-1 review item 6).
    attached: AtomicBool,
    /// The execution service's packed `ExecutionSnapshot` (N2b-2).
    pub(crate) execution_word: AtomicU64,
    /// The last successful `VmRSS` sample (bytes) and its boot-clock time
    /// (ms + 1, so 0 means none yet): written by the dispatcher, reported in
    /// `health()` only (ADR-034 §2, user ruling 2026-11-06).
    pub(crate) rss_bytes: AtomicU64,
    pub(crate) rss_sampled_at: AtomicU64,
}

impl Shared {
    /// The one constructor (tests included, N2b1-I-5(d)).
    pub(crate) fn new(
        cancel: CancelToken,
        started: Instant,
        time: Option<ManagedTimeContext>,
    ) -> Self {
        Shared {
            cancel,
            started,
            status: AtomicU64::new(0),
            supervisor_beat_ms: AtomicU64::new(0),
            preparation_status: AtomicU64::new(0),
            tasks: Default::default(),
            service: Mutex::new(None),
            pool: LanePool::default(),
            owned: Mutex::new(Vec::new()),
            time,
            live_runs: Default::default(),
            pending_interrupts: Mutex::new(BTreeMap::new()),
            orphaned: AtomicU64::new(0),
            refused_programs: AtomicU64::new(0),
            commands_refused: Arc::new(AtomicU64::new(0)),
            session_word: AtomicU64::new(0),
            consumer_release: AtomicBool::new(false),
            session_config: Mutex::new(None),
            attached: AtomicBool::new(false),
            execution_word: AtomicU64::new(0),
            rss_bytes: AtomicU64::new(0),
            rss_sampled_at: AtomicU64::new(0),
        }
    }

    fn cancel(&self) {
        self.status.fetch_or(STOP_REQUESTED, Ordering::Release);
        self.cancel.cancel();
        self.on_stop();
    }

    fn fail(&self, failure: ManagedFailure) {
        self.status
            .fetch_or(failure.bit() | STOP_REQUESTED, Ordering::AcqRel);
        self.cancel.cancel();
        self.on_stop();
    }

    /// Stop/fail/cancel (ADR-034 §4/§7): no further permit, every waiter
    /// woken, every live and pending run interrupted. Idempotent; also run by
    /// the supervisor when it observes an external token cancel.
    pub(crate) fn on_stop(&self) {
        self.pool.close();
        self.interrupt_all();
    }

    /// A stand-down that keeps the generation alive (ADR-034 §5/§7): `cause`
    /// holds the gate closed, so new admission is refused; `interrupt`
    /// (Release and Stop — a revoking Close lets live runs finish, as
    /// desktop's stand-down does: N2b-2, user ruling 2026-11-06) also
    /// interrupts every live, pending and already-admitted run. No token
    /// cancel, no thread exits; `reopen(cause)` admits again once no other
    /// cause holds.
    #[cfg_attr(
        not(test),
        expect(
            dead_code,
            reason = "the execution service's Execution::close, wired at N2b-2 C4"
        )
    )]
    pub(crate) fn stand_down(&self, cause: GateCause, interrupt: bool) {
        self.pool.close_gate(cause);
        if interrupt {
            self.interrupt_all();
        }
    }

    #[cfg_attr(
        not(test),
        expect(
            dead_code,
            reason = "the execution service's Execution::open, wired at N2b-2 C4"
        )
    )]
    pub(crate) fn reopen(&self, cause: GateCause) {
        self.pool.open_gate(cause);
    }

    pub(crate) fn interrupt_all(&self) {
        // the revocation first: a permit taken before it, and a job
        // registering after the scans below, both see the newer epoch
        self.pool.revoke();
        for run in self
            .live_runs
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .values()
        {
            run.interrupt.store(true, Ordering::Relaxed);
        }
        for (flag, _) in self
            .pending_interrupts
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .values()
        {
            flag.store(true, Ordering::Relaxed);
        }
    }

    pub(crate) fn capacity_fault(&self) {
        self.pool.capacity_fault();
    }

    pub(crate) fn orphaned(&self) {
        self.orphaned.fetch_add(1, Ordering::Relaxed);
    }

    pub(crate) fn elapsed_ms(&self) -> u64 {
        self.started.elapsed().as_millis().min(u64::MAX as u128) as u64
    }

    /// Publish into the calling thread's task slot; a thread the owner does
    /// not run (tests, a future JNI caller) publishes nothing.
    pub(crate) fn publish_current(&self, phase: TaskPhase, wait: WaitClass, deadline_ms: u64) {
        if let Some(task) = CURRENT_TASK.with(Cell::get) {
            self.tasks[task as usize].publish(phase, wait, deadline_ms);
        }
    }

    /// Spawn an owned execution thread into the registry (same wrapper, stack
    /// and panic accounting as the owner's own tasks). When the thread starts
    /// after a cancel, `work` is dropped unrun: anything it owns (a lane's
    /// inbox guard, a takeover's outcome guard) must report from its `Drop`.
    pub(crate) fn spawn_owned(
        self: &Arc<Self>,
        task: Task,
        work: impl FnOnce(TaskContext) -> Result<(), ManagedFailure> + Send + 'static,
    ) -> Result<(), ManagedFailure> {
        // cancel is decided under the registry lock, so no spawn follows a
        // drain's registry sweep
        let mut owned = self.owned.lock().unwrap_or_else(|e| e.into_inner());
        if self.cancel.is_cancelled() {
            return Err(ManagedFailure::Cancelled);
        }
        match spawn_task(self.clone(), task, work) {
            Ok(handle) => {
                owned.push((task, handle));
                Ok(())
            }
            Err(failure) => {
                self.fail(failure);
                Err(failure)
            }
        }
    }
}

fn spawn_failure(task: Task) -> ManagedFailure {
    match task {
        Task::Preparation => ManagedFailure::PreparationSpawnFailed,
        Task::Supervisor => ManagedFailure::SupervisorSpawnFailed,
        _ => ManagedFailure::ExecutionSpawnFailed,
    }
}

fn exit_failure(task: Task) -> ManagedFailure {
    match task {
        Task::Preparation => ManagedFailure::PreparationExitedUnexpectedly,
        Task::Supervisor => ManagedFailure::SupervisorExitedUnexpectedly,
        _ => ManagedFailure::ExecutionExitedUnexpectedly,
    }
}

fn panic_failure(task: Task) -> ManagedFailure {
    match task {
        Task::Preparation => ManagedFailure::PreparationPanicked,
        Task::Supervisor => ManagedFailure::SupervisorPanicked,
        _ => ManagedFailure::ExecutionPanicked,
    }
}

/// The one thread wrapper: 8 MiB stack, task slot bound to the thread,
/// panic/unexpected-exit accounting on the shared status word.
fn spawn_task(
    shared: Arc<Shared>,
    task: Task,
    work: impl FnOnce(TaskContext) -> Result<(), ManagedFailure> + Send + 'static,
) -> Result<JoinHandle<()>, ManagedFailure> {
    thread::Builder::new()
        .name(task.name().into())
        .stack_size(crate::engine_profile::RUN_THREAD_STACK_BYTES)
        .spawn(move || {
            CURRENT_TASK.with(|slot| slot.set(Some(task)));
            shared.tasks[task as usize].publish(TaskPhase::Running, WaitClass::None, 0);
            let ctx = TaskContext {
                shared: shared.clone(),
                task,
            };
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                if shared.cancel.is_cancelled() && !task.runs_after_cancel() {
                    return Ok(());
                }
                work(ctx)
            }));
            match result {
                Ok(Err(failure)) => shared.fail(failure),
                Err(_) => shared.fail(panic_failure(task)),
                Ok(Ok(())) if !shared.cancel.is_cancelled() && task.exit_is_failure() => {
                    shared.fail(exit_failure(task))
                }
                Ok(Ok(())) => {}
            }
        })
        .map_err(|_| spawn_failure(task))
}

pub(crate) struct TaskContext {
    pub(crate) shared: Arc<Shared>,
    task: Task,
}

impl TaskContext {
    pub(crate) fn idle_until(&self, wake_at: Instant) -> bool {
        // ADR-034 §4/§7: an idle wake is not an expired operation budget.
        self.wait(WaitClass::Idle, None);
        self.shared.cancel.wait_until(wake_at)
    }

    pub(crate) fn wait(&self, class: WaitClass, deadline: Option<Instant>) {
        let millis = deadline.map_or(0, |deadline| {
            deadline
                .saturating_duration_since(self.shared.started)
                .as_millis()
                .min((u64::MAX >> 16) as u128 - 1) as u64
                + 1
        });
        self.shared.tasks[self.task as usize].publish(TaskPhase::Waiting, class, millis);
    }

    pub(crate) fn running(&self) {
        self.shared.tasks[self.task as usize].publish(TaskPhase::Running, WaitClass::None, 0);
    }
}

impl Drop for TaskContext {
    fn drop(&mut self) {
        self.shared.tasks[self.task as usize].publish(TaskPhase::Exited, WaitClass::None, 0);
    }
}

/// Owns every managed thread. Keep this handle after a drain timeout until a later
/// join or worker process death. Dropping it cancels and **blocks** joining any
/// remaining work; never drop an unfinished handle on a JNI/deadline/UI thread.
/// It does not wrap the legacy desktop `AgentHandle`.
#[must_use = "retain the managed owner until drain or process death"]
pub struct ManagedAgent {
    shared: Arc<Shared>,
    threads: Vec<(Task, JoinHandle<()>)>,
}

/// Returns an owned preparation task without waiting for HTTP/kernel work.
/// The caller already holds cancellation before either owned thread can start.
/// HostControlled performs only permitted preparation. The remaining owned
/// execution integration is an explicit readiness gate (ADR-034 §5).
pub fn start_managed(cfg: Config, options: ManagedOptions, cancel: CancelToken) -> ManagedAgent {
    let time = Some(options.time_context().clone());
    let config = session::SessionConfig {
        leased_origin: options.leased_origin().to_owned(),
        space: cfg.agent_space.clone(),
        fixture: options.fixture.clone(),
    };
    let agent = start_inner(cancel, time, move |ctx| {
        ctx.wait(WaitClass::Initialization, None);
        if ctx.shared.cancel.is_cancelled() {
            return Ok(());
        }
        options.validate_config(&cfg)?;
        preparation::prepare_host_controlled(cfg, options, ctx)
    });
    *agent
        .shared
        .session_config
        .lock()
        .unwrap_or_else(|e| e.into_inner()) = Some(config);
    agent
}

#[cfg(test)]
fn start_with(
    cancel: CancelToken,
    prepare: impl FnOnce(TaskContext) -> Result<(), ManagedFailure> + Send + 'static,
) -> ManagedAgent {
    start_inner(cancel, None, prepare)
}

fn start_inner(
    cancel: CancelToken,
    time: Option<ManagedTimeContext>,
    prepare: impl FnOnce(TaskContext) -> Result<(), ManagedFailure> + Send + 'static,
) -> ManagedAgent {
    let shared = Arc::new(Shared::new(cancel, Instant::now(), time));
    let mut agent = ManagedAgent {
        shared,
        threads: Vec::with_capacity(2),
    };
    if agent.shared.cancel.is_cancelled() {
        agent
            .shared
            .status
            .store(STOP_REQUESTED | ALL_JOINED, Ordering::Release);
        return agent;
    }
    agent.spawn(Task::Supervisor, supervisor);
    if !agent.shared.cancel.is_cancelled() {
        agent.spawn(Task::Preparation, prepare);
    }
    agent
}

/// The supervisor: preparation's wait deadline while running, then — on any
/// cancel, also one that landed before this thread first ran — the stop
/// propagation (`on_stop`): an external token cancel reaches the pool and the
/// live runs here.
fn supervisor(ctx: TaskContext) -> Result<(), ManagedFailure> {
    while !ctx.shared.cancel.is_cancelled() {
        ctx.shared
            .supervisor_beat_ms
            .store(ctx.shared.elapsed_ms(), Ordering::Release);
        let preparation = ctx.shared.tasks[Task::Preparation as usize].snapshot(ctx.shared.started);
        if preparation
            .wait_deadline
            .is_some_and(|deadline| Instant::now() >= deadline)
        {
            ctx.shared.fail(ManagedFailure::PreparationWaitExpired);
            break;
        }
        let deadline = Instant::now() + SUPERVISOR_PERIOD;
        ctx.idle_until(deadline);
    }
    ctx.shared.on_stop();
    Ok(())
}

impl ManagedAgent {
    fn spawn(
        &mut self,
        task: Task,
        work: impl FnOnce(TaskContext) -> Result<(), ManagedFailure> + Send + 'static,
    ) {
        match spawn_task(self.shared.clone(), task, work) {
            Ok(handle) => self.threads.push((task, handle)),
            Err(failure) => self.shared.fail(failure),
        }
    }

    /// No service or ownership mutexes: only atomic status/progress reads.
    pub fn health(&self) -> HealthSnapshot {
        let mut status = self.shared.status.load(Ordering::Acquire);
        let snapshot = |task: Task| self.shared.tasks[task as usize].snapshot(self.shared.started);
        let supervisor = snapshot(Task::Supervisor);
        let supervisor_age = Duration::from_millis(
            self.shared
                .elapsed_ms()
                .saturating_sub(self.shared.supervisor_beat_ms.load(Ordering::Acquire)),
        );
        let cancelled = self.shared.cancel.is_cancelled();
        if !cancelled && supervisor_age >= SUPERVISOR_STALE {
            let failure = ManagedFailure::SupervisorStale.bit();
            self.shared.status.fetch_or(failure, Ordering::AcqRel);
            status |= failure;
        }
        let failures: Vec<_> = FAILURES
            .into_iter()
            .filter(|failure| status & failure.bit() != 0)
            .collect();
        let all_threads_joined = status & ALL_JOINED != 0;
        let phase = if !failures.is_empty() {
            ManagedPhase::Failed
        } else if all_threads_joined {
            ManagedPhase::Drained
        } else if cancelled {
            ManagedPhase::Stopping
        } else {
            ManagedPhase::Preparing
        };
        let session = SessionSnapshot::from_word(self.shared.session_word.load(Ordering::Acquire));
        // the sample time first, then the bytes: the dispatcher stores the
        // bytes before the time (Release), so the bytes read here are never
        // older than the time they are reported with
        let rss_sampled_at_ms = self
            .shared
            .rss_sampled_at
            .load(Ordering::Acquire)
            .checked_sub(1);
        let rss_bytes = rss_sampled_at_ms.map(|_| self.shared.rss_bytes.load(Ordering::Relaxed));
        HealthSnapshot {
            phase,
            failures,
            cancelled,
            admission_open: session.admission,
            preparation_status: PreparationStatus::from_atomic(
                self.shared.preparation_status.load(Ordering::Acquire),
            ),
            preparation: snapshot(Task::Preparation),
            supervisor,
            supervisor_age,
            epoch: snapshot(Task::Epoch),
            lanes: [
                snapshot(Task::LaneChat),
                snapshot(Task::LaneScheduled),
                snapshot(Task::LaneEvent),
            ],
            consumer: snapshot(Task::Consumer),
            takeover: snapshot(Task::Takeover),
            session,
            live_stores: self.shared.pool.live_stores(),
            time_context_stale: self
                .shared
                .time
                .as_ref()
                .is_some_and(ManagedTimeContext::is_stale),
            capacity_faults: self.shared.pool.capacity_faults(),
            commands_refused: self.shared.commands_refused.load(Ordering::Relaxed),
            orphaned_jobs: self.shared.orphaned.load(Ordering::Relaxed),
            refused_programs: self.shared.refused_programs.load(Ordering::Relaxed),
            lane_admission_open: self.shared.pool.admission_open(),
            service: snapshot(Task::Service),
            presence: snapshot(Task::Presence),
            housekeeping: snapshot(Task::Housekeeping),
            retention: snapshot(Task::Retention),
            standdown: snapshot(Task::Standdown),
            chat_watch: snapshot(Task::ChatWatch),
            event_sources: [
                snapshot(Task::EventSource0),
                snapshot(Task::EventSource1),
                snapshot(Task::EventSource2),
                snapshot(Task::EventSource3),
            ],
            execution: ExecutionSnapshot::from_word(
                self.shared.execution_word.load(Ordering::Acquire),
            ),
            rss_bytes,
            rss_sampled_at_ms,
            all_threads_joined,
        }
    }

    pub fn cancel(&self) {
        self.shared.cancel();
    }

    /// The JNI adapter attaches its callback sink and the boot clock once
    /// (ADR-034 §7): the command consumer starts here, and the returned port
    /// is what the adapter offers every command through. The consumer never
    /// runs on the caller's thread, and it exits only with this owner. A
    /// second attach is refused: one session per worker process (ADR §4).
    pub fn attach(
        &self,
        sink: Arc<dyn protocol::CallbackSink>,
        clock: Arc<dyn grant::BootClock>,
    ) -> Result<CommandPort, ManagedFailure> {
        if self.shared.attached.swap(true, Ordering::AcqRel) {
            return Err(ManagedFailure::InvalidConfiguration);
        }
        let config = self
            .shared
            .session_config
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
            .ok_or(ManagedFailure::InvalidConfiguration)?;
        session::spawn_consumer(&self.shared, config, sink, clock)
    }

    /// What preparation verified (ADR-034 §5): a report, never a Ready/Open
    /// assertion. None before preparation succeeds and after its task exits.
    pub fn readiness(&self) -> Option<Readiness> {
        self.prepared_service()
            .map(|service| service.readiness.clone())
    }

    /// The retained prepared service, for the owned run consumer (N2b) and tests;
    /// None before preparation succeeds and after its task exits.
    pub(crate) fn prepared_service(&self) -> Option<Arc<service::PreparedService>> {
        self.shared
            .service
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    /// Every handle this owner must join: its own two plus the service registry.
    fn take_finished(&mut self) -> Vec<(Task, JoinHandle<()>)> {
        let mut finished = Vec::new();
        let mut owned = self.shared.owned.lock().unwrap_or_else(|e| e.into_inner());
        for threads in [&mut self.threads, &mut *owned] {
            let mut index = 0;
            while index < threads.len() {
                if threads[index].1.is_finished() {
                    finished.push(threads.swap_remove(index));
                } else {
                    index += 1;
                }
            }
        }
        finished
    }

    fn remaining(&self) -> usize {
        self.threads.len()
            + self
                .shared
                .owned
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .len()
    }

    /// Cancels and joins finished threads, retaining every unfinished handle on
    /// timeout. Retry on the same owner; do not drop it to enforce a deadline.
    pub fn drain_until(&mut self, deadline: Instant) -> DrainOutcome {
        self.cancel();
        self.shared.consumer_release.store(true, Ordering::Release);
        loop {
            for (task, handle) in self.take_finished() {
                if handle.join().is_err() {
                    self.shared.fail(panic_failure(task));
                }
            }
            if self.remaining() == 0 {
                self.shared.status.fetch_or(ALL_JOINED, Ordering::Release);
                let failures = self.health().failures;
                return if failures.is_empty() {
                    DrainOutcome::Drained
                } else {
                    DrainOutcome::Failed(failures)
                };
            }
            let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
                return DrainOutcome::DeadlineExceeded;
            };
            thread::sleep(remaining.min(Duration::from_millis(2)));
        }
    }
}

impl Drop for ManagedAgent {
    fn drop(&mut self) {
        self.cancel();
        self.shared.consumer_release.store(true, Ordering::Release);
        // the registry is swept until empty: a thread that joined late or was
        // registered under the lock before cancel is still ours to join
        loop {
            let owned: Vec<_> = self
                .shared
                .owned
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .drain(..)
                .collect();
            let batch: Vec<_> = self.threads.drain(..).chain(owned).collect();
            if batch.is_empty() {
                break;
            }
            for (task, handle) in batch {
                if handle.join().is_err() {
                    self.shared.fail(panic_failure(task));
                }
            }
        }
        self.shared.status.fetch_or(ALL_JOINED, Ordering::Release);
    }
}

#[cfg(test)]
mod tests;
