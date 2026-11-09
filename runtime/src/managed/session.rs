//! The native command consumer (ADR-034 §4/§7): one immutable lease/session
//! per worker process, the serialized state machine over main's six
//! commands, every callback stamped with a monotone `stateSeq` and the
//! highest applied policy sequence, the owned takeover task with
//! generation-token invalidation, and the deadline-tracked Release/Stop
//! paths. The consumer never performs HTTP: takeover runs on its own owned
//! thread and posts its outcome back; the execution service (N2b-2) hangs
//! off the `Execution` hook, installed through `Owner::build_execution` when
//! preparation first settles Prepared, and while none is installed `ready`
//! is false by construction, so nothing can open. The session alone moves
//! the host transport between its phases (N2b-2 design §3.2). Rust never
//! calls `election::*` and never writes the registry: `verdict.active` is
//! the fence's open grant.
use super::grant::{
    ActivationIntent, BootClock, CalibratedBootClock, GrantError, GrantFence, OpenGrant,
    PendingOpen,
};
use super::preparation::SetupReason;
use super::protocol::{
    AdmissionDecision, CallbackSink, ClockCalibration, DrainResult, WorkerCallback,
    WorkerCallbackStamp, WorkerCommand, WorkerCommandKind, WorkerFailure, WorkerLease, WorkerPhase,
    WorkerRequest, WorkerStartOptions, WorkerStatus,
};
use super::takeover::{self, TakeoverError, TakeoverInputs, TakeoverOutcome};
use super::transport::Phase;
use super::{
    ManagedFailure, PreparationStatus, PreseededFixture, Shared, Task, TaskContext, WaitClass,
};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender, SyncSender, TryRecvError};
use std::sync::Arc;
use std::time::{Duration, Instant};

/// The consumer's own cadence: grant expiry, deadlines and preparation are
/// re-checked at least this often (ADR-034 §7: expiry is checked at every
/// admission too, by `permits_dispatch`, in the execution service).
const TICK: Duration = Duration::from_secs(1);
/// The idle wait for the next command. A queued command is taken before a
/// queued takeover outcome on every pass (`Inbox::next`), so a Close/Release/
/// Stop is never overtaken by an outcome posted after it was queued; an
/// outcome posted during the wait is taken at most this long later.
const POLL: Duration = Duration::from_millis(250);
/// Commands ride ONE queue in arrival order (a control command never overtakes
/// an earlier ordinary one — N2b-1 review item 4: L refuses a Status answered
/// ahead of its Close); the reserved control capacity (ADR-034 §7 "a reserved
/// control slot") means a full ordinary backlog never refuses a Close/Release/
/// Stop. Internal outcomes (a takeover's) have their own unbounded channel.
const INBOUND_CAPACITY: usize = 64;
const CONTROL_CAPACITY: usize = 8;

/// Outcomes the owned tasks post back to the consumer.
pub(crate) enum Internal {
    TakeoverDone {
        gen: u64,
        outcome: Result<TakeoverOutcome, TakeoverError>,
    },
}

pub(crate) enum Inbound {
    Command(WorkerCommand),
    Internal(Internal),
}

/// The bounded queue refused the command. Commands are oneway Binder calls, so
/// main never sees this refusal: the JNI adapter's fail-closed action is to
/// drop the command on its Binder thread (never block, never retry), and
/// main's 5 s callback-receipt budget turns the silence into a health failure
/// (HostPolicyOwner: expired receipt → uncertain worker → RequestStatus →
/// retirement). `health().commands_refused` counts every refusal here.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PortFull;

/// What the JNI adapter holds: `offer` never blocks and never runs the
/// state machine on the caller's thread.
#[derive(Clone)]
pub struct CommandPort {
    commands: SyncSender<WorkerCommand>,
    /// Ordinary commands reserved or queued and not yet taken: bounded at
    /// `INBOUND_CAPACITY` so the control reserve stays free.
    ordinary: Arc<AtomicUsize>,
    /// `Shared.commands_refused`.
    refused: Arc<AtomicU64>,
}

impl CommandPort {
    /// An ordinary command first reserves one of the `INBOUND_CAPACITY` slots
    /// with a bounded compare-and-swap, then sends, rolling the reservation
    /// back if the queue refuses: concurrent producers can neither overshoot
    /// into the control reserve nor let the consumer's decrement wrap the
    /// count (Step B review F9).
    pub fn offer(&self, command: WorkerCommand) -> Result<(), PortFull> {
        let control = command.is_control();
        if !control
            && self
                .ordinary
                .try_update(Ordering::AcqRel, Ordering::Acquire, |n| {
                    (n < INBOUND_CAPACITY).then_some(n + 1)
                })
                .is_err()
        {
            return self.refuse();
        }
        if self.commands.try_send(command).is_err() {
            if !control {
                self.ordinary.fetch_sub(1, Ordering::AcqRel);
            }
            return self.refuse();
        }
        Ok(())
    }

    fn refuse(&self) -> Result<(), PortFull> {
        self.refused.fetch_add(1, Ordering::Relaxed);
        Err(PortFull)
    }
}

/// What the session knows about this worker before any command: the leased
/// origin and the fixture identity explicit setup recorded. A Start naming
/// another origin, account or peer is refused and terminal.
#[derive(Clone)]
pub(crate) struct SessionConfig {
    pub(crate) leased_origin: String,
    pub(crate) space: String,
    pub(crate) fixture: Option<PreseededFixture>,
}

/// Preparation as the session reads it (ADR-034 §5): missing or unsynced
/// prerequisites are NeedsSetup with a typed reason in health, final for this
/// session — main's monitor re-requests preparation (a fresh worker), never a
/// Rust retry loop.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Preparedness {
    Preparing,
    Prepared,
    NotReady,
}

fn preparedness(status: PreparationStatus) -> Preparedness {
    match status {
        PreparationStatus::Initializing => Preparedness::Preparing,
        PreparationStatus::Prepared => Preparedness::Prepared,
        _ => Preparedness::NotReady,
    }
}

/// The execution service's face to the session (N2b-2 implements it on the
/// owned service, `execution::ExecutionService`). While none is installed
/// the worker is never ready.
pub(crate) trait Execution: Send + Sync {
    /// AppliedOpen: dispatch may begin, beats carry this grant's activation
    /// pair, admission checks the grant's freshness on `clock`. `takeover` is
    /// the checked takeover this Open performed — `Some` for every Open that
    /// needed one (a new activation: the registry's standing entries and the
    /// responder come from it), `None` for a renewal within a fresh grant.
    /// Lifts only the `GateCause::Policy` hold on the lane gate
    /// (`Shared::reopen`), never another cause's (F8).
    fn open(
        &self,
        grant: &OpenGrant,
        session_id: &str,
        takeover: Option<&TakeoverOutcome>,
        clock: &CalibratedBootClock,
    );
    /// Admission closed (`Shared::stand_down(GateCause::Policy, false)`),
    /// queued work cancelled; live runs finish within their budgets.
    /// Revoking (ownership loss, deauthorization, prune, Release, Stop) also
    /// stands down this activation's ownership as desktop's stand-down does:
    /// the standing built-ins unowned locally, the responder's owner cleared
    /// on its record (the one stand-down write, released by
    /// `release_owner_clear`), one `shutdown` beat. A revoking Close does not
    /// interrupt live runs (desktop parity, user ruling 2026-11-06).
    fn close(&self, revoking: bool);
    /// Release and Stop only, after `close(true)`: every live, pending and
    /// admitted-not-started run is interrupted (the protocol's bounded
    /// quiescence, ADR-034 §7).
    fn interrupt(&self);
    /// Every admitted job's report callback returned, nothing queued, the
    /// stand-down write, the `shutdown` beat and housekeeping's stamp writes
    /// settled.
    fn quiescent(&self) -> bool;
    /// The bounded queue saturated: visible, and the worker is not ready.
    fn capacity_fault(&self) -> bool;
    /// This peer owns the enabled chat-responder record (the stamped takeover write).
    fn owns_chat(&self) -> bool;
    /// No stand-down write pending or in flight: a takeover may start (E5).
    fn standdown_idle(&self) -> bool;
    /// The session's go for the stand-down write, given only while no
    /// takeover is in flight (E5): a superseded takeover's in-flight record
    /// write lands before the owner is cleared.
    fn release_owner_clear(&self);
    /// The session's go for the owed `shutdown` beats, given after a close
    /// restored cleanup authorization: none is published into a gate still
    /// in Preparation (round A-r2 review B2).
    fn release_shutdown_beats(&self);
}

#[derive(Clone, Copy)]
struct Reading {
    preparedness: Preparedness,
    failed: bool,
}

/// What the session asks of the owner (`Shared`), behind a trait so the
/// state machine is testable without threads.
pub(crate) trait Owner: Send {
    fn preparedness(&self) -> Preparedness;
    /// The execution service for this owner, started (its threads spawned)
    /// when preparation first settles Prepared (N2b-2, ADR-034 §7). None
    /// leaves the worker not ready: production builds none until N2b-2 C4;
    /// a refused spawn (the owner already cancelled) builds none, and a
    /// failed spawn is an owner failure (`ExecutionSpawnFailed`).
    fn build_execution(&self, clock: Arc<dyn BootClock>) -> Option<Arc<dyn Execution>>;
    /// An owner failure (panic, unexpected exit, stale supervisor) is terminal
    /// for readiness: the worker retires (ADR-034 §2).
    fn failed(&self) -> bool;
    /// Skill drift, or a tombstoned standing record, at Open: NeedsSetup
    /// (`Invalid`) until explicit setup changes.
    fn mark_needs_setup(&self);
    /// Hand the takeover for `gen` to an owned thread. True means exactly one
    /// `TakeoverDone{gen}` will be posted — by the task, or by its outcome
    /// guard when the thread never ran the body or unwound (Step B review
    /// F3); false means nothing was started and nothing will be posted.
    /// `superseded` is set by the session when the takeover no longer matters
    /// (a Close/Release/Stop or a newer intent): the task checks it before
    /// every write and stops (N2b-1 review item 1).
    fn spawn_takeover(&self, gen: u64, superseded: Arc<AtomicBool>) -> bool;
    /// The owner was cancelled by something other than this session's Stop
    /// (a JNI drain, a failure): the worker is not ready and is stopping.
    fn cancelled(&self) -> bool;
    fn gate(&self, phase: Phase);
    /// Stop: cancel the owner (every owned thread, every live interrupt).
    fn cancel(&self);
    /// Join every finished owned thread except the consumer; the count still running.
    fn join_finished(&self) -> usize;
    fn publish(&self, word: u64);
    /// The owner is draining/dropping: the consumer must exit.
    fn released(&self) -> bool;
}

/// `health()`'s view of the session, packed in one atomic word.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct SessionSnapshot {
    pub bound: bool,
    pub phase: Option<WorkerPhase>,
    pub ready: bool,
    pub terminal: bool,
    pub takeover_pending: bool,
    /// AppliedOpen in force, in the same word as the phase (S6).
    pub admission: bool,
    pub state_seq: u64,
}

impl SessionSnapshot {
    pub(crate) fn from_word(word: u64) -> Self {
        let phase = match word & 0xf {
            1 => Some(WorkerPhase::Preparing),
            2 => Some(WorkerPhase::Prepared),
            3 => Some(WorkerPhase::Opening),
            4 => Some(WorkerPhase::Open),
            5 => Some(WorkerPhase::Releasing),
            6 => Some(WorkerPhase::Quiescent),
            7 => Some(WorkerPhase::Stopping),
            8 => Some(WorkerPhase::Drained),
            9 => Some(WorkerPhase::Failed),
            _ => None,
        };
        SessionSnapshot {
            bound: word & 0x10 != 0,
            phase,
            ready: word & 0x20 != 0,
            terminal: word & 0x40 != 0,
            takeover_pending: word & 0x80 != 0,
            admission: word & 0x100 != 0,
            state_seq: word >> 16,
        }
    }

    fn word(&self) -> u64 {
        let phase = match self.phase {
            None => 0,
            Some(WorkerPhase::Preparing) => 1,
            Some(WorkerPhase::Prepared) => 2,
            Some(WorkerPhase::Opening) => 3,
            Some(WorkerPhase::Open) => 4,
            Some(WorkerPhase::Releasing) => 5,
            Some(WorkerPhase::Quiescent) => 6,
            Some(WorkerPhase::Stopping) => 7,
            Some(WorkerPhase::Drained) => 8,
            Some(WorkerPhase::Failed) => 9,
        };
        phase
            | if self.bound { 0x10 } else { 0 }
            | if self.ready { 0x20 } else { 0 }
            | if self.terminal { 0x40 } else { 0 }
            | if self.takeover_pending { 0x80 } else { 0 }
            | if self.admission { 0x100 } else { 0 }
            | (self.state_seq.min(u64::MAX >> 16) << 16)
    }
}

/// The execution-side stage once a session is bound; Preparing/Failed/
/// Stopping/Drained derive from the owner and the Stop wait.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Stage {
    Prepared,
    Opening,
    Open,
    Releasing,
    Quiescent,
}

struct Bound {
    request: WorkerRequest,
    options: WorkerStartOptions,
}

struct PendingTakeover {
    gen: u64,
    pending: PendingOpen,
    grant: OpenGrant,
    request: WorkerRequest,
    /// Raised when this takeover is superseded: the task stops before its next write.
    superseded: Arc<AtomicBool>,
    /// Its task has been handed to the owner. A takeover accepted while an
    /// earlier (superseded) task is still in flight waits for that one (F2).
    spawned: bool,
}

/// One accepted Release awaiting quiescence. Each is answered exactly once —
/// `Released`, or `Failed(DEADLINE_EXCEEDED)` at its own deadline, after
/// which L has removed it (its first non-Accepted callback, HPO:482).
struct ReleaseWait {
    request: WorkerRequest,
    deadline_ms: u64,
}

struct StopWait {
    request: WorkerRequest,
    deadline_ms: u64,
    cancelled: bool,
    result: Option<DrainResult>,
}

pub(crate) struct Session {
    config: SessionConfig,
    sink: Arc<dyn CallbackSink>,
    clock: Arc<dyn BootClock>,
    owner: Box<dyn Owner>,
    execution: Option<Arc<dyn Execution>>,
    state_seq: u64,
    bound: Option<Bound>,
    fence: Option<GrantFence>,
    intent: Option<ActivationIntent>,
    grant: Option<OpenGrant>,
    stage: Stage,
    terminal: bool,
    transient: Option<WorkerFailure>,
    pending: Option<PendingTakeover>,
    gen: u64,
    /// Takeover tasks started and not yet reported back, superseded ones
    /// included: Release and Stop wait for them (N2b-1 review item 1). Only
    /// a `true` from `spawn_takeover` counts, and only a `TakeoverDone`
    /// uncounts (F3).
    takeovers_in_flight: usize,
    /// AppliedOpen in force: set by `finish_open`, cleared by every local close.
    admitted: bool,
    /// Releases accepted and not yet answered, in arrival order (S9b: a second
    /// Release never overwrites the first).
    releases: Vec<ReleaseWait>,
    /// A Release failed its deadline and quiescence has not been reached yet.
    release_overdue: bool,
    stop: Option<StopWait>,
    announced: Option<Preparedness>,
    failed_announced: bool,
    fault_announced: bool,
    cancel_announced: bool,
    /// `Owner::build_execution` was asked once (N2b-2).
    install_attempted: bool,
    /// The phase this session last set on the host transport: it alone sets
    /// it (design §3.2, D1).
    gate_phase: Phase,
    /// A close left the transport in Execution for the execution service's
    /// last bookkeeping; `tick` returns it to Preparation at quiescence.
    gate_return_pending: bool,
}

impl Session {
    pub(crate) fn new(
        config: SessionConfig,
        sink: Arc<dyn CallbackSink>,
        clock: Arc<dyn BootClock>,
        owner: Box<dyn Owner>,
    ) -> Self {
        Session {
            config,
            sink,
            clock,
            owner,
            execution: None,
            state_seq: 0,
            bound: None,
            fence: None,
            intent: None,
            grant: None,
            stage: Stage::Prepared,
            terminal: false,
            transient: None,
            pending: None,
            gen: 0,
            takeovers_in_flight: 0,
            admitted: false,
            releases: Vec::new(),
            release_overdue: false,
            stop: None,
            announced: None,
            failed_announced: false,
            fault_announced: false,
            cancel_announced: false,
            install_attempted: false,
            gate_phase: Phase::Preparation,
            gate_return_pending: false,
        }
    }

    /// The execution service is installed here (`maybe_install`, N2b-2);
    /// until then `ready` is false. Installing can flip `ready`, so a session
    /// whose settled Status already went out announces again (N2b1-P-6a): L
    /// opens only on a `ready` Status.
    pub(crate) fn install_execution(&mut self, execution: Arc<dyn Execution>) {
        self.execution = Some(execution);
        if self.bound.is_some() && self.announced.is_some() {
            self.spontaneous_status();
        }
    }

    // --- status ----------------------------------------------------------------

    fn now_ms(&self) -> u64 {
        // a failing boot clock fails closed: every deadline is past
        self.clock.now_ms().unwrap_or(u64::MAX)
    }

    fn applied(&self) -> u64 {
        self.fence
            .as_ref()
            .map_or(0, GrantFence::applied_policy_seq)
    }

    /// One reading of the owner's state: what a Status reports and what the
    /// local close before it decides on are the same snapshot (SBC-P-2,
    /// SBC2-P-2).
    fn reading(&self) -> Reading {
        Reading {
            preparedness: self.owner.preparedness(),
            failed: self.owner.failed(),
        }
    }

    fn ready(&self) -> bool {
        self.ready_at(self.reading())
    }

    fn ready_at(&self, reading: Reading) -> bool {
        !self.terminal
            && self.bound.is_some()
            && reading.preparedness == Preparedness::Prepared
            && !reading.failed
            && !self.owner.cancelled()
            && self.execution.as_ref().is_some_and(|e| !e.capacity_fault())
    }

    /// Nothing of this session's is still running on the owner: the execution
    /// service stood down and no takeover task is in flight (N2b-1 item 1).
    fn quiescent(&self) -> bool {
        self.takeovers_in_flight == 0 && self.execution.as_ref().is_none_or(|e| e.quiescent())
    }

    fn worker_phase(&self) -> WorkerPhase {
        self.worker_phase_at(self.reading())
    }

    fn worker_phase_at(&self, reading: Reading) -> WorkerPhase {
        if let Some(stop) = &self.stop {
            return match stop.result {
                Some(DrainResult::Drained) => WorkerPhase::Drained,
                _ => WorkerPhase::Stopping,
            };
        }
        if reading.failed {
            return WorkerPhase::Failed;
        }
        if self.owner.cancelled() {
            // an owner drain that is not this session's Stop: going away (item 7)
            return WorkerPhase::Stopping;
        }
        match reading.preparedness {
            Preparedness::Preparing => WorkerPhase::Preparing,
            Preparedness::NotReady => WorkerPhase::Failed,
            Preparedness::Prepared => match self.stage {
                Stage::Prepared => WorkerPhase::Prepared,
                Stage::Opening => WorkerPhase::Opening,
                Stage::Open => WorkerPhase::Open,
                Stage::Releasing => WorkerPhase::Releasing,
                Stage::Quiescent => WorkerPhase::Quiescent,
            },
        }
    }

    fn failure_at(&self, reading: Reading) -> Option<WorkerFailure> {
        if reading.failed || self.execution.as_ref().is_some_and(|e| e.capacity_fault()) {
            return Some(WorkerFailure::Internal);
        }
        if reading.preparedness == Preparedness::NotReady {
            return Some(WorkerFailure::NotReady);
        }
        if self.release_overdue
            || self
                .stop
                .as_ref()
                .is_some_and(|s| s.result == Some(DrainResult::DeadlineExceeded))
        {
            return Some(WorkerFailure::DeadlineExceeded);
        }
        self.transient
    }

    /// The Status a reply would carry now (tests; every reply goes through
    /// `status_reply`, which reconciles the gate with the same reading).
    #[cfg(test)]
    pub(crate) fn status(&self) -> WorkerStatus {
        self.status_at(self.reading())
    }

    fn status_at(&self, reading: Reading) -> WorkerStatus {
        let phase = self.worker_phase_at(reading);
        WorkerStatus {
            phase,
            ready: self.ready_at(reading),
            intent: self.intent.clone(),
            // a FAILED (or stopping) worker never reports an open grant
            open_grant: if self.stage == Stage::Open && phase == WorkerPhase::Open {
                self.grant.clone()
            } else {
                None
            },
            failure: self.failure_at(reading),
        }
    }

    pub(crate) fn snapshot(&self) -> SessionSnapshot {
        SessionSnapshot {
            bound: self.bound.is_some(),
            phase: self.bound.as_ref().map(|_| self.worker_phase()),
            ready: self.ready(),
            terminal: self.terminal,
            takeover_pending: self.pending.is_some(),
            admission: self.admitted,
            state_seq: self.state_seq,
        }
    }

    fn publish(&self) {
        self.owner.publish(self.snapshot().word());
    }

    /// The consumer is exiting (the owner drains or drops): nothing can be
    /// admitted any more, and health must not keep reporting an open gate (S6b).
    fn retire(&mut self) {
        self.admitted = false;
        self.publish();
    }

    // --- callbacks --------------------------------------------------------------

    fn emit(
        &mut self,
        request: &WorkerRequest,
        applied: u64,
        build: impl FnOnce(WorkerCallbackStamp) -> WorkerCallback,
    ) {
        self.state_seq += 1;
        let stamp = WorkerCallbackStamp {
            request: request.clone(),
            state_seq: self.state_seq,
            applied_policy_seq: applied,
        };
        self.sink.emit(build(stamp));
        self.publish();
    }

    fn fail(&mut self, request: &WorkerRequest, failure: WorkerFailure) {
        let applied = self.applied();
        self.emit(request, applied, |stamp| WorkerCallback::Failed {
            stamp,
            failure,
        });
    }

    fn accepted(&mut self, request: &WorkerRequest, command: WorkerCommandKind, applied: u64) {
        self.emit(request, applied, |stamp| WorkerCallback::Accepted {
            stamp,
            command,
        });
    }

    /// Every Status, requested or spontaneous, is built from ONE reading of
    /// the owner (preparedness and owner failure), and that same reading
    /// first closes the gate locally when it says FAILED (NotReady, or an
    /// owner failure) while Opening/Open: a FAILED Status never goes out
    /// beside an open gate, whatever changed between two reads (SB-P-6,
    /// SBC-P-2, SBC2-P-2).
    fn status_reply(&mut self, request: &WorkerRequest) {
        let reading = self.reading();
        if (reading.preparedness == Preparedness::NotReady || reading.failed)
            && matches!(self.stage, Stage::Opening | Stage::Open)
        {
            self.close_locally(false);
        }
        let status = self.status_at(reading);
        let applied = self.applied();
        self.emit(request, applied, |stamp| WorkerCallback::Status {
            stamp,
            status,
        });
    }

    /// A Start-correlated Status (ADR-034 §7): preparation settled, expiry,
    /// applied Close, a fault — the same lease/session and a fresh stateSeq.
    fn spontaneous_status(&mut self) {
        let Some(request) = self.bound.as_ref().map(|b| b.request.clone()) else {
            return;
        };
        self.status_reply(&request);
    }

    fn announce_if_settled(&mut self) {
        if self.bound.is_none() {
            return;
        }
        let now = self.owner.preparedness();
        if now == Preparedness::Prepared {
            self.maybe_install();
        }
        if now != Preparedness::Preparing && self.announced != Some(now) {
            self.announced = Some(now);
            self.spontaneous_status();
        }
    }

    /// Ask the owner for the execution service once, when preparation first
    /// settles Prepared (ADR-034 §7): the first Prepared Status already says
    /// `ready`. None (production before C4, a refused or failed spawn) leaves
    /// the worker not ready for this session.
    fn maybe_install(&mut self) {
        if self.execution.is_some() || self.install_attempted {
            return;
        }
        self.install_attempted = true;
        if let Some(execution) = self.owner.build_execution(self.clock.clone()) {
            self.install_execution(execution);
        }
    }

    // --- the host transport's phase (design §3.2) -------------------------------

    fn set_gate(&mut self, phase: Phase) {
        self.gate_phase = phase;
        self.owner.gate(phase);
    }

    /// After a close: the transport goes back to Preparation once nothing can
    /// still need the execution routes — no takeover pending or in flight,
    /// not Opening/Open, and the execution service quiescent (D1).
    fn maybe_return_gate(&mut self) {
        if self.gate_return_pending
            && self.pending.is_none()
            && self.takeovers_in_flight == 0
            && !matches!(self.stage, Stage::Opening | Stage::Open)
            && self.execution.as_ref().is_none_or(|e| e.quiescent())
        {
            self.gate_return_pending = false;
            self.set_gate(Phase::Preparation);
        }
    }

    /// The stand-down write may go only while no takeover is in flight (E5).
    fn release_standdown(&self) {
        if self.takeovers_in_flight == 0 {
            if let Some(execution) = &self.execution {
                execution.release_owner_clear();
            }
        }
    }

    /// Release and Stop interrupt the live runs (ADR-034 §7); a revoking
    /// Close does not (desktop parity, user ruling 2026-11-06).
    fn interrupt_live(&self) {
        if let Some(execution) = &self.execution {
            execution.interrupt();
        }
    }

    // --- commands ---------------------------------------------------------------

    pub(crate) fn handle(&mut self, inbound: Inbound) {
        match inbound {
            Inbound::Command(command) => self.command(command),
            Inbound::Internal(Internal::TakeoverDone { gen, outcome }) => {
                self.takeover_done(gen, outcome)
            }
        }
    }

    pub(crate) fn command(&mut self, command: WorkerCommand) {
        if let Err(e) = command.validate() {
            tracing::warn!("worker command refused: {e}");
            if matches!(command, WorkerCommand::Start { .. }) && self.bound.is_none() {
                // a refused first Start is terminal for the process (ADR-034 §7)
                self.terminal = true;
            }
            let request = command.request().clone();
            self.fail(&request, WorkerFailure::Internal);
            return;
        }
        if let WorkerCommand::Start {
            request,
            options,
            calibration,
        } = command
        {
            self.start(request, options, calibration);
            return;
        }
        let request = command.request().clone();
        if !self.same_session(&request) {
            self.fail(&request, WorkerFailure::InvalidLease);
            return;
        }
        if matches!(command, WorkerCommand::RequestStatus { .. }) {
            self.status_reply(&request);
            return;
        }
        if self.terminal {
            self.fail(&request, WorkerFailure::InvalidSequence);
            return;
        }
        match command {
            WorkerCommand::Activate { intent, .. } => self.activate(request, intent),
            WorkerCommand::Release {
                deadline_elapsed_realtime,
                revoke_through_intent_revision,
                ..
            } => self.release(
                request,
                deadline_elapsed_realtime,
                revoke_through_intent_revision,
            ),
            WorkerCommand::SetAdmission {
                policy_seq,
                decision,
                ..
            } => self.set_admission(request, policy_seq, decision),
            WorkerCommand::Stop {
                deadline_elapsed_realtime,
                ..
            } => self.stop(request, deadline_elapsed_realtime),
            WorkerCommand::Start { .. } | WorkerCommand::RequestStatus { .. } => {}
        }
    }

    fn same_session(&self, request: &WorkerRequest) -> bool {
        self.bound.as_ref().is_some_and(|b| {
            b.request.session_id == request.session_id && b.request.lease == request.lease
        })
    }

    fn lease_refusal(
        &self,
        lease: &WorkerLease,
        options: &WorkerStartOptions,
        calibration: &ClockCalibration,
    ) -> Option<WorkerFailure> {
        if lease.base_url != self.config.leased_origin {
            return Some(WorkerFailure::InvalidLease);
        }
        if let Some(fixture) = &self.config.fixture {
            if lease.account_id != fixture.account_id || lease.self_peer_id != fixture.self_peer_id
            {
                return Some(WorkerFailure::InvalidLease);
            }
        }
        if options.elapsed_realtime_at_start > calibration.android_elapsed {
            return Some(WorkerFailure::InvalidLease);
        }
        None
    }

    /// One session per process (ADR-034 §4): the first Start binds; the identical
    /// Start is acknowledged only; a different one is refused. A refused first
    /// Start is terminal — nothing binds in this process afterwards.
    fn start(
        &mut self,
        request: WorkerRequest,
        options: WorkerStartOptions,
        calibration: ClockCalibration,
    ) {
        if self.terminal {
            // after Stop (or a refused first Start) nothing starts again, not even
            // the identical request: main retires the process (ADR-034 §4)
            self.fail(&request, WorkerFailure::InvalidLease);
            return;
        }
        if let Some(bound) = &self.bound {
            if bound.request == request && bound.options == options {
                let applied = self.applied();
                self.accepted(&request, WorkerCommandKind::Start, applied);
            } else {
                self.fail(&request, WorkerFailure::InvalidLease);
            }
            return;
        }
        let refusal = match self.lease_refusal(&request.lease, &options, &calibration) {
            Some(failure) => Some(failure),
            None => match CalibratedBootClock::new(
                self.clock.clone(),
                calibration.native_before,
                calibration.android_elapsed,
                calibration.native_after,
            ) {
                Ok(calibrated) => {
                    self.fence = Some(GrantFence::new(calibrated));
                    None
                }
                Err(_) => Some(WorkerFailure::Internal),
            },
        };
        if let Some(failure) = refusal {
            self.terminal = true;
            self.fail(&request, failure);
            return;
        }
        self.bound = Some(Bound {
            request: request.clone(),
            options,
        });
        self.stage = Stage::Prepared;
        self.accepted(&request, WorkerCommandKind::Start, 0);
        self.announce_if_settled();
    }

    fn activate(&mut self, request: WorkerRequest, intent: ActivationIntent) {
        if self.stage == Stage::Releasing {
            // S9a: nothing activates or opens until the Release is quiescent
            self.fail(&request, WorkerFailure::NotReady);
            return;
        }
        let ready = self.ready();
        let result = self.fence.as_mut().map_or(Err(GrantError::NotReady), |f| {
            f.activate(intent.clone(), ready)
        });
        match result {
            Ok(()) => {
                self.intent = Some(intent.clone());
                self.transient = None;
                let applied = self.applied();
                self.accepted(&request, WorkerCommandKind::Activate, applied);
                self.emit(&request, applied, |stamp| WorkerCallback::ActivationReady {
                    stamp,
                    intent,
                });
            }
            Err(e) => self.fail(&request, e.into()),
        }
    }

    fn set_admission(
        &mut self,
        request: WorkerRequest,
        policy_seq: u64,
        decision: AdmissionDecision,
    ) {
        match decision {
            AdmissionDecision::Open(grant) => self.open(request, policy_seq, grant),
            AdmissionDecision::Close {
                reason,
                revoke_through_intent_revision,
            } => {
                let before = self.applied();
                let result = self.fence.as_mut().map_or(Err(GrantError::NotReady), |f| {
                    f.close(policy_seq, reason, revoke_through_intent_revision)
                });
                match result {
                    Err(e) => self.fail(&request, e.into()),
                    Ok(()) => {
                        self.transient = None;
                        // Accepted precedes the result and never carries the new policySeq
                        self.accepted(&request, WorkerCommandKind::SetAdmission, before);
                        let revoking = reason.revokes_intent();
                        if let Some(barrier) = revoke_through_intent_revision {
                            if self
                                .intent
                                .as_ref()
                                .is_some_and(|i| i.intent_revision <= barrier)
                            {
                                self.intent = None;
                            }
                        }
                        self.close_locally(revoking);
                        let applied = self.applied();
                        self.emit(&request, applied, |stamp| {
                            WorkerCallback::AdmissionApplied {
                                stamp,
                                decision: AdmissionDecision::Close {
                                    reason,
                                    revoke_through_intent_revision,
                                },
                            }
                        });
                        self.spontaneous_status();
                    }
                }
            }
        }
    }

    fn open(&mut self, request: WorkerRequest, policy_seq: u64, grant: OpenGrant) {
        if !self.ready() || self.stage == Stage::Releasing {
            // S9a: an Open while Releasing would let the Release report
            // Released over a freshly opened gate
            self.fail(&request, WorkerFailure::NotReady);
            return;
        }
        let result = self.fence.as_mut().map_or(Err(GrantError::NotReady), |f| {
            f.begin_open(policy_seq, grant.clone())
        });
        match result {
            Err(e) => {
                self.transient = Some(e.into());
                self.fail(&request, e.into());
            }
            Ok(pending) => {
                self.transient = None;
                let before = self.applied();
                self.accepted(&request, WorkerCommandKind::SetAdmission, before);
                // L re-sends Open on every 5 s sample: an Open for the intent
                // whose takeover is in flight adopts the newer policy/evidence
                // and keeps that one takeover (N2b-1 review item 2)
                if let Some(in_flight) = self
                    .pending
                    .as_mut()
                    .filter(|p| p.grant.intent == grant.intent)
                {
                    in_flight.pending = pending;
                    in_flight.grant = grant;
                    in_flight.request = request;
                    self.publish();
                    return;
                }
                if pending.requires_takeover && self.stage == Stage::Open {
                    // the grant expired before the tick saw it: an activation
                    // Open re-takes over, so the old gate closes first (S6c)
                    self.close_locally(false);
                }
                self.supersede_takeover();
                self.gen += 1;
                self.gate_return_pending = false;
                self.set_gate(Phase::Execution);
                if pending.requires_takeover {
                    self.stage = Stage::Opening;
                    self.pending = Some(PendingTakeover {
                        gen: self.gen,
                        pending,
                        grant,
                        request,
                        superseded: Arc::new(AtomicBool::new(false)),
                        spawned: false,
                    });
                    self.publish();
                    self.spawn_pending();
                } else {
                    self.finish_open(pending, grant, request, Ok(None));
                }
            }
        }
    }

    /// Start the pending takeover's task — only once no earlier takeover is
    /// still in flight: a superseded task's one in-flight whole-record write
    /// could otherwise land after this one's verified read-back (Step B
    /// review F2). A deferred takeover starts from `takeover_done` when the
    /// last earlier task reports; one that never reports leaves the Open
    /// pending until main's claim deadline closes it. It also waits for the
    /// execution service's stand-down write (E5: no owner-clear crosses a new
    /// takeover's verification), retried from `tick`.
    fn spawn_pending(&mut self) {
        if self.takeovers_in_flight > 0
            || self.execution.as_ref().is_some_and(|e| !e.standdown_idle())
        {
            return;
        }
        let Some(pending) = self.pending.as_mut().filter(|p| !p.spawned) else {
            return;
        };
        pending.spawned = true;
        let (gen, superseded) = (pending.gen, pending.superseded.clone());
        if self.owner.spawn_takeover(gen, superseded) {
            self.takeovers_in_flight += 1;
        } else {
            // nothing started, so nothing will report: complete it here,
            // without touching the in-flight count (F3)
            self.takeover_outcome(
                gen,
                Err(TakeoverError::Write("takeover task could not start".into())),
            );
        }
    }

    /// A pending takeover no longer matters: tell its task to stop before its
    /// next write (the outcome, if it still arrives, is ignored by generation).
    fn supersede_takeover(&mut self) {
        if let Some(pending) = self.pending.take() {
            pending.superseded.store(true, Ordering::Release);
        }
    }

    /// A started task reported (it posts exactly once, superseded or not).
    fn takeover_done(&mut self, gen: u64, outcome: Result<TakeoverOutcome, TakeoverError>) {
        self.takeovers_in_flight = self.takeovers_in_flight.saturating_sub(1);
        self.takeover_outcome(gen, outcome);
        self.release_standdown();
        self.spawn_pending();
    }

    fn takeover_outcome(&mut self, gen: u64, outcome: Result<TakeoverOutcome, TakeoverError>) {
        let Some(pending) = self.pending.take_if(|p| p.gen == gen) else {
            tracing::info!("takeover outcome for generation {gen} ignored (superseded)");
            self.check_release();
            self.check_stop();
            return;
        };
        let PendingTakeover {
            pending: open,
            grant,
            request,
            ..
        } = pending;
        match outcome {
            Ok(outcome) => {
                tracing::info!(
                    "takeover complete: chat responder {:?}, {} standing, {} descriptors",
                    outcome.chat_watch_id,
                    outcome.standing.len(),
                    outcome.descriptors
                );
                self.finish_open(open, grant, request, Ok(Some(outcome)));
            }
            Err(e) => {
                tracing::warn!("takeover failed: {e}");
                // drift and a tombstoned standing built-in are setup-level:
                // retrying with fresh evidence cannot help, so the worker
                // reports NeedsSetup(Invalid) and is no longer ready — L then
                // stops reopening it (SB-P-5)
                if matches!(e, TakeoverError::SkillDrift | TakeoverError::Tombstoned(_)) {
                    self.owner.mark_needs_setup();
                }
                self.finish_open(open, grant, request, Err(GrantError::Takeover));
            }
        }
    }

    /// AppliedOpen, or not: the fence re-checks policy, intent, revocation and
    /// freshness after the takeover, and readiness is re-checked here (an
    /// owner failure, NeedsSetup or a capacity fault during the takeover, S7);
    /// only success flips admission. `Ok(Some(outcome))` carries a completed
    /// takeover to the execution service, `Ok(None)` is a renewal.
    fn finish_open(
        &mut self,
        pending: PendingOpen,
        grant: OpenGrant,
        request: WorkerRequest,
        outcome: Result<Option<TakeoverOutcome>, GrantError>,
    ) {
        let outcome = if outcome.is_ok() && !self.ready() {
            Err(GrantError::NotReady)
        } else {
            outcome
        };
        let (takeover, checked) = match outcome {
            Ok(takeover) => (takeover, Ok(())),
            Err(e) => (None, Err(e)),
        };
        let result = self.fence.as_mut().map_or(Err(GrantError::NotReady), |f| {
            f.complete_open(pending, checked)
        });
        match result {
            Ok(()) => {
                self.stage = Stage::Open;
                self.grant = Some(grant.clone());
                self.admitted = true;
                if let (Some(execution), Some(bound), Some(fence)) =
                    (&self.execution, &self.bound, &self.fence)
                {
                    execution.open(
                        &grant,
                        &bound.request.session_id,
                        takeover.as_ref(),
                        fence.clock(),
                    );
                }
                let applied = self.applied();
                self.emit(&request, applied, |stamp| {
                    WorkerCallback::AdmissionApplied {
                        stamp,
                        decision: AdmissionDecision::Open(grant),
                    }
                });
            }
            Err(e) => {
                self.close_locally(false);
                self.transient = Some(e.into());
                self.fail(&request, e.into());
                self.spontaneous_status();
            }
        }
    }

    /// Admission closed on this side: the grant is gone, a pending takeover is
    /// superseded, the execution hook stands down. The gate returns to
    /// Preparation only when nothing can still need the execution routes:
    /// immediately without an execution service; otherwise this session
    /// returns it from `tick` once the service is quiescent (D1). A revoking
    /// close whose stand-down write arises after that return re-authorizes
    /// the execution routes for it, admission staying closed (E10).
    fn close_locally(&mut self, revoking: bool) {
        self.grant = None;
        self.supersede_takeover();
        self.gen += 1;
        if matches!(self.stage, Stage::Opening | Stage::Open) {
            self.stage = Stage::Prepared;
        }
        self.admitted = false;
        if let Some(execution) = self.execution.clone() {
            execution.close(revoking);
            self.gate_return_pending = true;
            // after `close(true)` nothing new is admitted, so what keeps the
            // service from quiescence is its stand-down (the owner-clear, the
            // owed `shutdown` beat): it needs the execution routes back
            if revoking && !execution.quiescent() && self.gate_phase == Phase::Preparation {
                self.set_gate(Phase::Execution);
            }
            // only now may the owed `shutdown` beat go out (B2)
            execution.release_shutdown_beats();
        } else {
            self.set_gate(Phase::Preparation);
        }
        self.release_standdown();
        self.publish();
    }

    fn release(&mut self, request: WorkerRequest, deadline_ms: u64, barrier: u64) {
        let result = self
            .fence
            .as_mut()
            .map_or(Err(GrantError::NotReady), |f| f.release(barrier));
        match result {
            Err(e) => self.fail(&request, e.into()),
            Ok(()) => {
                self.transient = None;
                let applied = self.applied();
                self.accepted(&request, WorkerCommandKind::Release, applied);
                if self
                    .intent
                    .as_ref()
                    .is_some_and(|i| i.intent_revision <= barrier)
                {
                    self.intent = None;
                }
                self.close_locally(true);
                self.interrupt_live();
                self.stage = Stage::Releasing;
                if self.releases.len() >= INBOUND_CAPACITY {
                    // L holds at most 64 commands; anything beyond is answered now
                    let oldest = self.releases.remove(0);
                    self.release_overdue = true;
                    self.fail(&oldest.request, WorkerFailure::DeadlineExceeded);
                }
                self.releases.push(ReleaseWait {
                    request,
                    deadline_ms,
                });
                self.check_release();
            }
        }
    }

    /// Releasing ends at quiescence: every Release still waiting is answered
    /// `Released`, in arrival order. Before that, each one past its own
    /// deadline is answered `Failed(DEADLINE_EXCEEDED)` once and is done — L
    /// dropped it at that callback, so no late `Released` follows for it; the
    /// spontaneous Status reports QUIESCENT when it is reached (N2b-1 item 5).
    fn check_release(&mut self) {
        if self.stage != Stage::Releasing || self.stop.is_some() {
            return;
        }
        if self.quiescent() {
            self.stage = Stage::Quiescent;
            self.release_overdue = false;
            self.gate_return_pending = false;
            self.set_gate(Phase::Preparation);
            for wait in std::mem::take(&mut self.releases) {
                let applied = self.applied();
                self.emit(&wait.request, applied, |stamp| WorkerCallback::Released {
                    stamp,
                });
            }
            self.spontaneous_status();
            return;
        }
        let now = self.now_ms();
        let (overdue, waiting): (Vec<_>, Vec<_>) = std::mem::take(&mut self.releases)
            .into_iter()
            .partition(|wait| now >= wait.deadline_ms);
        self.releases = waiting;
        for wait in overdue {
            self.release_overdue = true;
            self.fail(&wait.request, WorkerFailure::DeadlineExceeded);
        }
    }

    /// Terminal for the session (ADR-034 §7): stand down, then cancel the
    /// owner and poll-join the owned execution threads to the deadline. The
    /// consumer itself stays to answer `RequestStatus` after `Drained`.
    fn stop(&mut self, request: WorkerRequest, deadline_ms: u64) {
        if let Some(fence) = self.fence.as_mut() {
            fence.stop();
        }
        self.terminal = true;
        self.transient = None;
        let applied = self.applied();
        self.accepted(&request, WorkerCommandKind::Stop, applied);
        self.intent = None;
        self.close_locally(true);
        self.interrupt_live();
        // Stop supersedes the waiting Releases unanswered (ADR-034 §7's one
        // exception, sb-fix review SBC-P-4): main sends Stop only to retire
        // the worker, and a retiring main waits on neither
        self.releases.clear();
        self.release_overdue = false;
        self.stop = Some(StopWait {
            request,
            deadline_ms,
            cancelled: false,
            result: None,
        });
        self.check_stop();
    }

    fn check_stop(&mut self) {
        let Some(wait) = self.stop.as_ref() else {
            return;
        };
        if wait.result.is_some() {
            return;
        }
        let request = wait.request.clone();
        let deadline = wait.deadline_ms;
        if !wait.cancelled {
            // the stand-down writes and the shutdown beat need the transport,
            // which refuses everything once the owner is cancelled; a takeover
            // still in flight is waited for the same way (item 1)
            if !self.quiescent() && self.now_ms() < deadline {
                return;
            }
            self.gate_return_pending = false;
            self.set_gate(Phase::Preparation);
            self.owner.cancel();
            if let Some(wait) = self.stop.as_mut() {
                wait.cancelled = true;
            }
        }
        let remaining = self.owner.join_finished();
        let result = if remaining == 0 {
            Some(if self.owner.failed() {
                DrainResult::Failed
            } else {
                DrainResult::Drained
            })
        } else if self.now_ms() >= deadline {
            Some(DrainResult::DeadlineExceeded)
        } else {
            None
        };
        if let Some(result) = result {
            if let Some(wait) = self.stop.as_mut() {
                wait.result = Some(result);
            }
            let applied = self.applied();
            self.emit(&request, applied, |stamp| WorkerCallback::Drained {
                stamp,
                result,
            });
        }
    }

    // --- the tick ------------------------------------------------------------------

    fn check_grant(&mut self) {
        let owns_chat = self.execution.as_ref().is_some_and(|e| e.owns_chat());
        let healthy = self.ready();
        let clock_ok = self
            .fence
            .as_mut()
            .map_or(Ok(false), |f| {
                f.permits_dispatch(healthy, true, owns_chat, false)
            })
            .is_ok();
        let now = self.now_ms();
        let fresh = self.grant.as_ref().is_some_and(|g| g.fresh(now));
        if !clock_ok || !fresh {
            // expiry (or a dead clock) closes on this side: a Status, never a Close
            self.close_locally(false);
            self.spontaneous_status();
        }
    }

    pub(crate) fn tick(&mut self) {
        if self.owner.preparedness() == Preparedness::NotReady
            && matches!(self.stage, Stage::Opening | Stage::Open)
        {
            // NeedsSetup while open (an owner-side flip; no periodic skill
            // check runs while open, user ruling 2026-11-06): close at once,
            // even when no Status is due (SB-P-6; ADR-034 §2: drift is a
            // non-revoking close plus NeedsSetup). A flip after this read is
            // closed by `status_reply` before its FAILED Status (SBC-P-2).
            self.close_locally(false);
        }
        self.announce_if_settled();
        if self.owner.failed() && !self.failed_announced {
            self.failed_announced = true;
            if matches!(self.stage, Stage::Opening | Stage::Open) {
                self.close_locally(false);
            }
            self.spontaneous_status();
        }
        if self.owner.cancelled() && self.stop.is_none() && !self.cancel_announced {
            // an owner cancel that is not this session's Stop (item 7): not
            // ready, nothing open, one Status says so
            self.cancel_announced = true;
            if matches!(self.stage, Stage::Opening | Stage::Open) {
                self.close_locally(false);
            }
            self.spontaneous_status();
        }
        let fault = self.execution.as_ref().is_some_and(|e| e.capacity_fault());
        if fault != self.fault_announced {
            self.fault_announced = fault;
            self.spontaneous_status();
        }
        if self.stage == Stage::Open {
            self.check_grant();
        }
        self.check_release();
        self.check_stop();
        if self.stop.is_none() {
            self.maybe_return_gate();
            // a takeover deferred behind the stand-down write (E5)
            self.release_standdown();
            self.spawn_pending();
        }
        if self.stop.is_none() {
            // finished takeover threads are joined as they complete, never accumulated
            self.owner.join_finished();
        }
        self.publish();
    }
}

// --- the consumer thread and the real owner ------------------------------------------

/// The consumer's two sources in one order (ADR-034 §7 "Close/Stop preempts
/// queued Open"; Step B review F4/SB-P-2, sb-fix review OWN-1/SBC-P-3): a
/// queued command first (FIFO among commands); then a takeover outcome — but
/// only after the command queue is polled again once the outcome is in hand,
/// so a Close/Release/Stop offered before the outcome was posted, even one
/// that arrived between the two polls, is applied first and the outcome waits
/// behind it (`stashed`), finding its generation superseded; then a wait of
/// at most `wait` for the next command.
pub(crate) struct Inbox {
    commands: Receiver<WorkerCommand>,
    ordinary: Arc<AtomicUsize>,
    internal: Receiver<Internal>,
    /// An outcome already taken while a command was found on the re-poll.
    stashed: Option<Internal>,
}

impl Inbox {
    pub(crate) fn new(
        commands: Receiver<WorkerCommand>,
        ordinary: Arc<AtomicUsize>,
        internal: Receiver<Internal>,
    ) -> Self {
        Inbox {
            commands,
            ordinary,
            internal,
            stashed: None,
        }
    }

    fn next(&mut self, wait: Duration) -> Option<Inbound> {
        self.next_with(wait, &mut || {})
    }

    /// `between_polls` runs after the first command poll found nothing: the
    /// window the re-poll closes. Production passes a no-op; a test uses it
    /// to queue a Close and then an outcome exactly there.
    fn next_with(&mut self, wait: Duration, between_polls: &mut dyn FnMut()) -> Option<Inbound> {
        if let Some(command) = self.try_command() {
            return Some(command);
        }
        between_polls();
        let outcome = self
            .stashed
            .take()
            .or_else(|| self.internal.try_recv().ok());
        if let Some(outcome) = outcome {
            // the outcome's receive follows its send: a command offered before
            // the outcome was posted is visible to this re-poll
            if let Some(command) = self.try_command() {
                self.stashed = Some(outcome);
                return Some(command);
            }
            return Some(Inbound::Internal(outcome));
        }
        match self.commands.recv_timeout(wait) {
            Ok(command) => Some(self.taken(command)),
            Err(RecvTimeoutError::Timeout) => None,
            Err(RecvTimeoutError::Disconnected) => {
                std::thread::sleep(wait);
                None
            }
        }
    }

    fn try_command(&mut self) -> Option<Inbound> {
        match self.commands.try_recv() {
            Ok(command) => Some(self.taken(command)),
            Err(TryRecvError::Empty | TryRecvError::Disconnected) => None,
        }
    }

    fn taken(&self, command: WorkerCommand) -> Inbound {
        if !command.is_control() {
            self.ordinary.fetch_sub(1, Ordering::AcqRel);
        }
        Inbound::Command(command)
    }
}

/// The consumer loop: one message (`Inbox::next`) or a short idle wait, then
/// the tick. It exits only when the owner releases it (`drain_until`/Drop),
/// never on cancellation alone, so `RequestStatus` is answered after
/// `Drained`; on exit the published word no longer shows admission.
pub(crate) fn run_consumer(
    ctx: TaskContext,
    mut session: Session,
    mut inbox: Inbox,
) -> Result<(), ManagedFailure> {
    let mut last_tick = Instant::now();
    session.publish();
    loop {
        if session.owner.released() {
            session.retire();
            return Ok(());
        }
        ctx.wait(WaitClass::Idle, None);
        let next = inbox.next(POLL);
        ctx.running();
        if let Some(message) = next {
            session.handle(message);
        }
        if last_tick.elapsed() >= TICK {
            session.tick();
            last_tick = Instant::now();
        }
    }
}

/// The owner as the session sees it in production: the atomics of `Shared`,
/// the retained service for the takeover inputs and the gate, the owned
/// registry for Stop's join polling.
pub(crate) struct SharedOwner {
    pub(crate) shared: Arc<Shared>,
    pub(crate) internal: Sender<Internal>,
    pub(crate) config: SessionConfig,
}

impl Owner for SharedOwner {
    fn preparedness(&self) -> Preparedness {
        preparedness(PreparationStatus::from_atomic(
            self.shared.preparation_status.load(Ordering::Acquire),
        ))
    }

    fn failed(&self) -> bool {
        self.shared.status.load(Ordering::Acquire) >> super::FAILURE_SHIFT != 0
    }

    /// Production stays closed until N2b-2 C4 (design §3.3, ADR-034 §7): no
    /// execution service is built, so `ready` stays false and nothing opens.
    fn build_execution(&self, _clock: Arc<dyn BootClock>) -> Option<Arc<dyn Execution>> {
        None
    }

    fn mark_needs_setup(&self) {
        // drift: `Invalid`, final for this session (ADR-034 §2/§5)
        self.shared.preparation_status.store(
            PreparationStatus::NeedsSetup(SetupReason::Invalid).atomic(),
            Ordering::Release,
        );
    }

    fn cancelled(&self) -> bool {
        self.shared.cancel.is_cancelled()
    }

    fn spawn_takeover(&self, gen: u64, superseded: Arc<AtomicBool>) -> bool {
        let Some(service) = self
            .shared
            .service
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
        else {
            return false;
        };
        let Some(fixture) = self.config.fixture.as_ref() else {
            return false;
        };
        let inputs = TakeoverInputs {
            client: service.client.clone(),
            space: self.config.space.clone(),
            chat: fixture.chat.clone(),
            self_peer: fixture.self_peer_id.clone(),
            triggers: fixture.triggers.clone(),
            secrets: fixture.secrets.clone(),
            declared: service.frozen.declared.clone(),
            skills: service.readiness.skills.clone(),
            aliases: service.frozen.aliases.clone(),
            now: Arc::new(super::service::epoch_s),
            superseded,
        };
        // the guard posts the one TakeoverDone whatever happens to the body:
        // run, dropped unrun (spawn refused, thread started after a cancel) or
        // unwound (F3) — so a `true` here always comes back exactly once
        let report = TakeoverReport::new(self.internal.clone(), gen);
        let _ = self.shared.spawn_owned(Task::Takeover, move |ctx| {
            takeover_body(ctx, report, |cancel| takeover::run(&inputs, cancel))
        });
        true
    }

    fn gate(&self, phase: Phase) {
        if let Some(service) = self
            .shared
            .service
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .as_ref()
        {
            service.gate.set(phase);
        }
    }

    fn cancel(&self) {
        self.shared.cancel();
    }

    fn join_finished(&self) -> usize {
        let mut owned = self.shared.owned.lock().unwrap_or_else(|e| e.into_inner());
        let mut index = 0;
        let mut remaining = 0;
        while index < owned.len() {
            if owned[index].0 == Task::Consumer {
                index += 1;
                continue;
            }
            if owned[index].1.is_finished() {
                let (task, handle) = owned.swap_remove(index);
                if handle.join().is_err() {
                    self.shared.fail(super::panic_failure(task));
                }
            } else {
                remaining += 1;
                index += 1;
            }
        }
        remaining
    }

    fn publish(&self, word: u64) {
        self.shared.session_word.store(word, Ordering::Release);
    }

    fn released(&self) -> bool {
        self.shared.consumer_release.load(Ordering::Acquire)
    }
}

/// One takeover task: run, then publish its slot `Exited` (finalizing the
/// context) BEFORE handing the outcome over — on the normal return and on
/// unwind alike. The consumer may start the next (deferred) takeover on that
/// outcome, and both share the `Task::Takeover` slot: published the other way
/// round, this task's `Exited` could overwrite its successor's HTTP wait in
/// health (sb-fix review OWN-4, r2 review OWN-R2-2). `Handoff`'s field order
/// is that order on unwind (fields drop in declaration order).
pub(crate) fn takeover_body(
    ctx: TaskContext,
    report: TakeoverReport,
    run: impl FnOnce(&super::CancelToken) -> Result<TakeoverOutcome, TakeoverError>,
) -> Result<(), ManagedFailure> {
    struct Handoff {
        ctx: TaskContext,
        report: TakeoverReport,
    }
    let handoff = Handoff { ctx, report };
    handoff.ctx.wait(WaitClass::Http, None);
    let cancel = handoff.ctx.shared.cancel.clone();
    let outcome = run(&cancel);
    let Handoff { ctx, report } = handoff;
    drop(ctx);
    report.send(outcome);
    Ok(())
}

/// Posts a takeover's one `TakeoverDone`: `send` with the outcome, or — when
/// the body never ran or unwound — the `Drop` posts a failed outcome, so the
/// session's in-flight count always comes back down (Step B review F3).
pub(crate) struct TakeoverReport {
    internal: Option<Sender<Internal>>,
    gen: u64,
    /// Runs right before the outcome is sent (tests observe the slot there).
    #[cfg(test)]
    before_send: Option<Box<dyn FnOnce() + Send>>,
}

impl TakeoverReport {
    pub(crate) fn new(internal: Sender<Internal>, gen: u64) -> Self {
        TakeoverReport {
            internal: Some(internal),
            gen,
            #[cfg(test)]
            before_send: None,
        }
    }

    pub(crate) fn send(mut self, outcome: Result<TakeoverOutcome, TakeoverError>) {
        if let Some(internal) = self.internal.take() {
            #[cfg(test)]
            if let Some(hook) = self.before_send.take() {
                hook();
            }
            let _ = internal.send(Internal::TakeoverDone {
                gen: self.gen,
                outcome,
            });
        }
    }
}

impl Drop for TakeoverReport {
    fn drop(&mut self) {
        if let Some(internal) = self.internal.take() {
            #[cfg(test)]
            if let Some(hook) = self.before_send.take() {
                hook();
            }
            let _ = internal.send(Internal::TakeoverDone {
                gen: self.gen,
                outcome: Err(TakeoverError::Write(
                    "the takeover task ended without an outcome".into(),
                )),
            });
        }
    }
}

/// `ManagedAgent::attach`: the consumer thread into the owner registry and the
/// port the JNI adapter keeps. One consumer per owner.
pub(crate) fn spawn_consumer(
    shared: &Arc<Shared>,
    config: SessionConfig,
    sink: Arc<dyn CallbackSink>,
    clock: Arc<dyn BootClock>,
) -> Result<CommandPort, ManagedFailure> {
    let (commands_tx, commands_rx) = mpsc::sync_channel(INBOUND_CAPACITY + CONTROL_CAPACITY);
    let (internal_tx, internal_rx) = mpsc::channel();
    let ordinary = Arc::new(AtomicUsize::new(0));
    let owner = SharedOwner {
        shared: shared.clone(),
        internal: internal_tx,
        config: config.clone(),
    };
    let session = Session::new(config, sink, clock, Box::new(owner));
    let queued = ordinary.clone();
    shared.spawn_owned(Task::Consumer, move |ctx| {
        run_consumer(ctx, session, Inbox::new(commands_rx, queued, internal_rx))
    })?;
    Ok(CommandPort {
        commands: commands_tx,
        ordinary,
        refused: shared.commands_refused.clone(),
    })
}

#[cfg(test)]
mod tests;
