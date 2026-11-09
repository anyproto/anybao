use super::*;
use crate::managed::grant::{CloseReason, EvidenceGrade};
use crate::managed::protocol::{ReleaseReason, WorkerArtifact, WorkerLease};
use crate::managed::{FrozenPins, FrozenProgram, StoreAddress};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize};
use std::sync::Mutex;

// --- fakes -----------------------------------------------------------------------------

#[derive(Default)]
struct Clock(AtomicU64);
impl BootClock for Clock {
    fn now_ms(&self) -> Result<u64, GrantError> {
        Ok(self.0.load(Ordering::Relaxed))
    }
}

#[derive(Default)]
struct Sink(Mutex<Vec<WorkerCallback>>);
impl CallbackSink for Sink {
    fn emit(&self, callback: WorkerCallback) {
        self.0.lock().unwrap().push(callback);
    }
}
impl Sink {
    fn drain(&self) -> Vec<WorkerCallback> {
        std::mem::take(&mut *self.0.lock().unwrap())
    }
}

struct OwnerState {
    preparedness: Mutex<Preparedness>,
    /// When set to n, n more reads return the current value, then it flips to
    /// NotReady (a NeedsSetup landing between two reads, SBC-P-2)
    flip_after: AtomicUsize,
    failed: AtomicBool,
    needs_setup: AtomicBool,
    spawned: Mutex<Vec<u64>>,
    /// the supersede flag of every spawned takeover, in spawn order
    superseded: Mutex<Vec<Arc<AtomicBool>>>,
    spawn_ok: AtomicBool,
    gates: Mutex<Vec<Phase>>,
    admission: AtomicBool,
    cancelled: AtomicBool,
    remaining: AtomicUsize,
    words: Mutex<Vec<u64>>,
    released: AtomicBool,
    /// what `build_execution` hands out (N2b-2); None = production before C4
    build: Mutex<Option<Arc<dyn Execution>>>,
    builds: AtomicUsize,
}
impl OwnerState {
    fn new(preparedness: Preparedness) -> Arc<Self> {
        Arc::new(OwnerState {
            preparedness: Mutex::new(preparedness),
            flip_after: AtomicUsize::new(0),
            failed: AtomicBool::new(false),
            needs_setup: AtomicBool::new(false),
            spawned: Mutex::new(Vec::new()),
            superseded: Mutex::new(Vec::new()),
            spawn_ok: AtomicBool::new(true),
            gates: Mutex::new(Vec::new()),
            admission: AtomicBool::new(false),
            cancelled: AtomicBool::new(false),
            remaining: AtomicUsize::new(0),
            words: Mutex::new(Vec::new()),
            released: AtomicBool::new(false),
            build: Mutex::new(None),
            builds: AtomicUsize::new(0),
        })
    }
    fn set(&self, preparedness: Preparedness) {
        *self.preparedness.lock().unwrap() = preparedness;
    }
}

struct FakeOwner(Arc<OwnerState>);
impl Owner for FakeOwner {
    fn preparedness(&self) -> Preparedness {
        let now = *self.0.preparedness.lock().unwrap();
        if self.0.flip_after.load(Ordering::Relaxed) > 0
            && self.0.flip_after.fetch_sub(1, Ordering::Relaxed) == 1
        {
            self.0.set(Preparedness::NotReady);
        }
        now
    }
    fn build_execution(&self, _clock: Arc<dyn BootClock>) -> Option<Arc<dyn Execution>> {
        self.0.builds.fetch_add(1, Ordering::Relaxed);
        self.0.build.lock().unwrap().clone()
    }
    fn failed(&self) -> bool {
        self.0.failed.load(Ordering::Relaxed)
    }
    fn mark_needs_setup(&self) {
        self.0.needs_setup.store(true, Ordering::Relaxed);
        self.0.set(Preparedness::NotReady);
    }
    fn spawn_takeover(&self, gen: u64, superseded: Arc<AtomicBool>) -> bool {
        self.0.spawned.lock().unwrap().push(gen);
        self.0.superseded.lock().unwrap().push(superseded);
        self.0.spawn_ok.load(Ordering::Relaxed)
    }
    fn cancelled(&self) -> bool {
        self.0.cancelled.load(Ordering::Relaxed)
    }
    fn gate(&self, phase: Phase) {
        self.0.gates.lock().unwrap().push(phase);
    }
    fn cancel(&self) {
        self.0.cancelled.store(true, Ordering::Relaxed);
    }
    fn join_finished(&self) -> usize {
        self.0.remaining.load(Ordering::Relaxed)
    }
    fn publish(&self, word: u64) {
        // what health reads: admission rides the session word (S6)
        self.0.admission.store(
            SessionSnapshot::from_word(word).admission,
            Ordering::Relaxed,
        );
        self.0.words.lock().unwrap().push(word);
    }
    fn released(&self) -> bool {
        self.0.released.load(Ordering::Relaxed)
    }
}

struct FakeExecution {
    quiescent: AtomicBool,
    fault: AtomicBool,
    owns_chat: AtomicBool,
    opened: Mutex<Vec<(OpenGrant, String)>>,
    /// whether each open carried a takeover outcome (N2b-2)
    takeovers: Mutex<Vec<bool>>,
    closed: Mutex<Vec<bool>>,
    interrupts: AtomicUsize,
    standdown_idle: AtomicBool,
    owner_clear_releases: AtomicUsize,
    shutdown_releases: AtomicUsize,
}
impl Default for FakeExecution {
    fn default() -> Self {
        FakeExecution {
            quiescent: AtomicBool::new(false),
            fault: AtomicBool::new(false),
            owns_chat: AtomicBool::new(false),
            opened: Mutex::new(Vec::new()),
            takeovers: Mutex::new(Vec::new()),
            closed: Mutex::new(Vec::new()),
            interrupts: AtomicUsize::new(0),
            standdown_idle: AtomicBool::new(true),
            owner_clear_releases: AtomicUsize::new(0),
            shutdown_releases: AtomicUsize::new(0),
        }
    }
}
impl Execution for FakeExecution {
    fn open(
        &self,
        grant: &OpenGrant,
        session_id: &str,
        takeover: Option<&TakeoverOutcome>,
        _clock: &CalibratedBootClock,
    ) {
        self.opened
            .lock()
            .unwrap()
            .push((grant.clone(), session_id.to_owned()));
        self.takeovers.lock().unwrap().push(takeover.is_some());
    }
    fn close(&self, revoking: bool) {
        self.closed.lock().unwrap().push(revoking);
    }
    fn interrupt(&self) {
        self.interrupts.fetch_add(1, Ordering::Relaxed);
    }
    fn standdown_idle(&self) -> bool {
        self.standdown_idle.load(Ordering::Relaxed)
    }
    fn release_owner_clear(&self) {
        self.owner_clear_releases.fetch_add(1, Ordering::Relaxed);
    }
    fn release_shutdown_beats(&self) {
        self.shutdown_releases.fetch_add(1, Ordering::Relaxed);
    }
    fn quiescent(&self) -> bool {
        self.quiescent.load(Ordering::Relaxed)
    }
    fn capacity_fault(&self) -> bool {
        self.fault.load(Ordering::Relaxed)
    }
    fn owns_chat(&self) -> bool {
        self.owns_chat.load(Ordering::Relaxed)
    }
}

// --- the Kotlin side as an oracle ----------------------------------------------------

fn lease() -> WorkerLease {
    WorkerLease {
        main_nonce: "nonce".into(),
        generation: 1,
        base_url: "http://127.0.0.1:7001".into(),
        auth_epoch: 1,
        account_id: "acc".into(),
        self_peer_id: "peer".into(),
    }
}

fn intent(rev: u64) -> ActivationIntent {
    ActivationIntent {
        activation_id: format!("intent-{rev}"),
        intent_revision: rev,
    }
}

fn grant(rev: u64, evidence_seq: u64, observed_at: u64, grade: EvidenceGrade) -> OpenGrant {
    OpenGrant {
        intent: intent(rev),
        evidence_seq,
        observed_at,
        expires_at: observed_at + 30_000,
        grade,
    }
}

fn start(request: WorkerRequest) -> WorkerCommand {
    WorkerCommand::Start {
        request,
        options: WorkerStartOptions {
            artifact: WorkerArtifact::AndroidPocV1,
            elapsed_realtime_at_start: 100,
        },
        calibration: ClockCalibration {
            native_before: 100,
            android_elapsed: 100,
            native_after: 100,
        },
    }
}

fn open(request: WorkerRequest, policy_seq: u64, grant: OpenGrant) -> WorkerCommand {
    WorkerCommand::SetAdmission {
        request,
        policy_seq,
        decision: AdmissionDecision::Open(grant),
    }
}

fn close(
    request: WorkerRequest,
    policy_seq: u64,
    reason: CloseReason,
    barrier: Option<u64>,
) -> WorkerCommand {
    WorkerCommand::SetAdmission {
        request,
        policy_seq,
        decision: AdmissionDecision::Close {
            reason,
            revoke_through_intent_revision: barrier,
        },
    }
}

/// `HostPolicyOwner`'s command bookkeeping plus `WorkerCallbackFence.accept`
/// (N2b-1 review item 5; sb-fix review SBC-P-1). Kotlin sources, read-only:
/// `agent-host-impl/.../policy/HostPolicyOwner.kt` (HPO) and
/// `.../protocol/WorkerProtocolFence.kt` (WPF). Modelled, line for line:
///
/// - **Holding commands.** The Start is held for the session (HPO:84); every
///   other command from L's `send` (HPO:549-558) until its first accepted
///   non-`Accepted` callback removes it (HPO:482). Sending a SetAdmission
///   removes every earlier SetAdmission and their receipts (HPO:554-556).
/// - **Sending.** `send` is suppressed while retiring (HPO:549); at 64 held
///   commands L retires instead of sending (HPO:550); a refused `offer`
///   retires too (HPO:557, `refuse_offers`).
/// - **Receipts and the clock.** Every sent command but the Start has a 5 s
///   receipt, `now` + 5000 on L's deterministic clock (`now`, the tests' boot
///   clock domain), removed by the first accepted callback (HPO:453). `tick`
///   runs HPO:238-244: an overdue Release deadline without quiescence
///   retires; an expired receipt clears them all and enters `uncertainWorker`;
///   an overdue status query closes HEALTH_FAILURE and retires.
/// - **Retirement.** `request_retirement` (HPO:561-569) sets `retiring` once
///   and has H send Stop with the deadline HPO computes; L never registers
///   that Stop, so it drops every callback correlated to it.
/// - **Reactions that change the bookkeeping** (`reacting` only, HPO:454-480),
///   run before the completed command is removed: Status clears the status
///   query; Released sets quiescent and clears the Release deadline; Failed
///   revokes (no activation Open outstanding: `revoke`, HPO:502-520 — a
///   Close with the barrier, then a Release) or enters `uncertainWorker`
///   (HPO:536-546 — a HEALTH_FAILURE Close, then a RequestStatus). Commands a
///   reaction sends wait in `outbox` until the test `deliver`s them.
/// - **The fence** (WPF:79-101), and HPO:450-452: a callback for a session or
///   command L does not hold, or one the fence refuses, is dropped and
///   changes nothing (`dropped`).
///
/// Two modes. `Main::new()` is **scripted**: the test plays L's decisions
/// (which commands, which policy sequences), and no reaction runs — what it
/// checks is the fence and the command bookkeeping. `Main::reacting()` runs
/// the reactions and allocates the policy sequence of the commands they send
/// as L's `++policySeq`. `send` (through L) must see no callback dropped;
/// `inject` is raw worker-command injection, never L's, and reports what L
/// dropped.
///
/// Not modelled, and so each reacting-mode test is restricted to states where
/// none of it can act (sb-fix r2 review SBC2-P-1): sampling, evidence and
/// grant construction (tests script Opens); claims, registry writes and the
/// ActivationReady reaction (`attemptClaim` and its activation deadline,
/// which can enter `uncertainWorker` before the Activate is removed —
/// reacting tests deliver ActivationReady inside that deadline); the claim,
/// beat and sync-gap deadlines (no test advances L's clock past a claim or a
/// beat deadline with one pending); `appliedOpen`'s persistence; durable
/// intents and ReleaseOwed; Start registration checks and
/// `nextRequest = max(…)` (one session per test); a disabled account
/// (`enabledAccount` set, so Released never retires); confirmed-death session
/// removal (tests end before it). Commands of L's own come only from its
/// modelled actions (`revoke`, `close`, `uncertain_worker`, `tick`), so each
/// carries the timers those actions set.
struct Main {
    reacting: bool,
    now: u64,
    session: Option<WorkerRequest>,
    commands: BTreeMap<u64, WorkerCommand>,
    /// request id → receipt deadline (HPO:553)
    receipts: BTreeMap<u64, u64>,
    next_request: u64,
    highest_sent: u64,
    state_seq: u64,
    applied: u64,
    seen: Vec<WorkerCallback>,
    dropped: Vec<WorkerCallback>,
    // HostPolicyOwner state the reactions read or write
    retiring: bool,
    refuse_offers: bool,
    stops: Vec<WorkerCommand>,
    outbox: Vec<WorkerCommand>,
    suppressed: Vec<WorkerCommand>,
    intent: Option<u64>,
    revision: u64,
    open_sent: bool,
    release_deadline: Option<u64>,
    quiescent: bool,
    status_query_at: Option<u64>,
    uncertain: usize,
}

const CALLBACK_BUDGET_MS: u64 = 5_000;

impl Main {
    fn new() -> Self {
        Main {
            reacting: false,
            now: 100,
            session: None,
            commands: BTreeMap::new(),
            receipts: BTreeMap::new(),
            next_request: 1,
            highest_sent: 0,
            state_seq: 0,
            applied: 0,
            seen: Vec::new(),
            dropped: Vec::new(),
            retiring: false,
            refuse_offers: false,
            stops: Vec::new(),
            outbox: Vec::new(),
            suppressed: Vec::new(),
            intent: None,
            revision: 0,
            open_sent: false,
            release_deadline: None,
            quiescent: false,
            status_query_at: None,
            uncertain: 0,
        }
    }

    fn reacting() -> Self {
        Main {
            reacting: true,
            ..Main::new()
        }
    }

    fn request(&mut self) -> WorkerRequest {
        let request_id = self.next_request;
        self.next_request += 1;
        WorkerRequest {
            request_id,
            session_id: "s1".into(),
            lease: lease(),
        }
    }

    /// `HostPolicyOwner.send` (HPO:549-558), or `Session(start)` for the first
    /// Start (HPO:84: held for the session, no receipt). True when the command
    /// was offered to the worker.
    fn register(&mut self, command: WorkerCommand) -> bool {
        assert!(
            !matches!(command, WorkerCommand::Stop { .. }),
            "L never sends Stop (H retires the worker)"
        );
        let id = command.request().request_id;
        if matches!(command, WorkerCommand::Start { .. }) {
            if self.session.is_none() {
                self.session = Some(command.request().clone());
                self.commands.insert(id, command);
            }
            return true;
        }
        if self.retiring {
            self.suppressed.push(command);
            return false;
        }
        if self.commands.len() >= 64 {
            self.request_retirement();
            self.suppressed.push(command);
            return false;
        }
        match &command {
            WorkerCommand::SetAdmission {
                policy_seq,
                decision,
                ..
            } => {
                self.highest_sent = self.highest_sent.max(*policy_seq);
                if matches!(decision, AdmissionDecision::Open(g) if g.grade == EvidenceGrade::Activation)
                {
                    self.open_sent = true; // HPO:445
                }
            }
            WorkerCommand::Activate { intent, .. } => {
                self.intent = Some(intent.intent_revision);
                self.revision = self.revision.max(intent.intent_revision);
            }
            _ => {}
        }
        let superseding = matches!(command, WorkerCommand::SetAdmission { .. })
            .then(|| command.request().clone());
        self.commands.insert(id, command);
        self.receipts.insert(id, self.now + CALLBACK_BUDGET_MS);
        if let Some(latest) = superseding {
            self.commands.retain(|_, previous| {
                !matches!(previous, WorkerCommand::SetAdmission { .. })
                    || *previous.request() == latest
            });
            let commands = &self.commands;
            self.receipts.retain(|id, _| commands.contains_key(id));
        }
        if self.refuse_offers {
            self.request_retirement(); // HPO:557
            return false;
        }
        true
    }

    /// L sends a scripted command; every callback must pass L.
    fn send(
        &mut self,
        session: &mut Session,
        sink: &Sink,
        build: impl FnOnce(WorkerRequest) -> WorkerCommand,
    ) -> Vec<WorkerCallback> {
        let (accepted, dropped) = self.send_split(session, sink, build);
        assert!(dropped.is_empty(), "L dropped {dropped:?}");
        accepted
    }

    /// L sends a scripted command; what L accepted and what it dropped.
    fn send_split(
        &mut self,
        session: &mut Session,
        sink: &Sink,
        build: impl FnOnce(WorkerRequest) -> WorkerCommand,
    ) -> (Vec<WorkerCallback>, Vec<WorkerCallback>) {
        let request = self.request();
        let command = build(request);
        if self.register(command.clone()) {
            session.command(command);
        }
        self.consume_split(sink)
    }

    /// L sends a command whose delivery is lost (the adapter refused it on
    /// the worker's Binder thread: oneway, so L's `offer` still succeeded).
    fn send_lost(&mut self, build: impl FnOnce(WorkerRequest) -> WorkerCommand) {
        let request = self.request();
        self.register(build(request));
    }

    /// Raw worker-command injection, never L's: nothing is registered, and L
    /// drops whatever is correlated to it. Returns (accepted, dropped).
    fn inject(
        &mut self,
        session: &mut Session,
        sink: &Sink,
        command: WorkerCommand,
    ) -> (Vec<WorkerCallback>, Vec<WorkerCallback>) {
        session.command(command);
        self.consume_split(sink)
    }

    /// `requestRetirement` (HPO:561-569): H retires the worker with Stop.
    fn request_retirement(&mut self) {
        if !self.retiring {
            self.retiring = true;
            let deadline = self
                .release_deadline
                .map_or(self.now + 15_000, |d| d + 5_000);
            let request = self.request();
            self.stops.push(WorkerCommand::Stop {
                request,
                deadline_elapsed_realtime: deadline,
            });
        }
    }

    /// L asks for retirement and H's Stop reaches the worker; L drops every
    /// callback correlated to it. Returns them for H's own assertions.
    fn retire(&mut self, session: &mut Session, sink: &Sink) -> Vec<WorkerCallback> {
        self.request_retirement();
        self.deliver_stops(session, sink)
    }

    fn deliver_stops(&mut self, session: &mut Session, sink: &Sink) -> Vec<WorkerCallback> {
        for stop in std::mem::take(&mut self.stops) {
            session.command(stop);
        }
        self.consume_dropped(sink)
    }

    /// Deliver what L's reactions sent, in order, consuming each answer
    /// (which may send more); every callback must pass L.
    fn deliver(&mut self, session: &mut Session, sink: &Sink) -> Vec<WorkerCallback> {
        let mut accepted = Vec::new();
        while !self.outbox.is_empty() {
            let command = self.outbox.remove(0);
            session.command(command);
            accepted.extend(self.consume(sink));
        }
        accepted
    }

    /// One of L's own commands, from a reaction (`++policySeq`, HPO:528).
    fn l_send(&mut self, build: impl FnOnce(WorkerRequest, u64) -> WorkerCommand) {
        let request = self.request();
        let command = build(request, self.highest_sent + 1);
        if self.register(command.clone()) {
            self.outbox.push(command);
        }
    }

    /// `close` (HPO:524-530).
    fn close(&mut self, reason: CloseReason) {
        let barrier = reason.revokes_intent().then_some(self.revision);
        self.l_send(|request, seq| close(request, seq, reason, barrier));
    }

    /// `revoke` (HPO:502-520), drain = true, with a session.
    fn revoke(&mut self, reason: CloseReason) {
        self.intent = None;
        self.open_sent = false;
        self.close(reason);
        self.release_deadline = Some(self.now + 10_000);
        let (deadline, barrier) = (self.now + 10_000, self.revision);
        self.l_send(|request, _| WorkerCommand::Release {
            request,
            reason: ReleaseReason::UserStop,
            deadline_elapsed_realtime: deadline,
            revoke_through_intent_revision: barrier,
        });
    }

    /// `uncertainWorker` (HPO:532-546).
    fn uncertain_worker(&mut self) {
        self.uncertain += 1;
        if self.intent.is_some() && !self.open_sent {
            self.revoke(CloseReason::UserStop);
            self.request_retirement();
            return;
        }
        self.close(CloseReason::HealthFailure);
        if self.status_query_at.is_none() && !self.retiring {
            self.status_query_at = Some(self.now);
            self.l_send(|request, _| WorkerCommand::RequestStatus { request });
        }
    }

    /// L's tick at `now` (HPO:238-244).
    fn tick(&mut self, now: u64) {
        self.now = now;
        if self.release_deadline.is_some_and(|d| now >= d) && !self.quiescent {
            self.request_retirement();
        }
        if self.receipts.values().any(|due| now >= *due) {
            self.receipts.clear();
            self.uncertain_worker();
        }
        if self
            .status_query_at
            .is_some_and(|at| now - at >= CALLBACK_BUDGET_MS)
        {
            self.status_query_at = None;
            self.close(CloseReason::HealthFailure);
            self.request_retirement();
        }
    }

    /// Every callback must pass L.
    fn consume(&mut self, sink: &Sink) -> Vec<WorkerCallback> {
        let (accepted, dropped) = self.consume_split(sink);
        assert!(dropped.is_empty(), "L dropped {dropped:?}");
        accepted
    }

    /// Every callback must be one L drops (a Stop's, a removed command's).
    fn consume_dropped(&mut self, sink: &Sink) -> Vec<WorkerCallback> {
        let (accepted, dropped) = self.consume_split(sink);
        assert!(accepted.is_empty(), "L accepted {accepted:?}");
        dropped
    }

    /// `consumeCallback` (HPO:449-482) for each callback, in emission order.
    fn consume_split(&mut self, sink: &Sink) -> (Vec<WorkerCallback>, Vec<WorkerCallback>) {
        let (mut accepted, mut dropped) = (Vec::new(), Vec::new());
        for callback in sink.drain() {
            if self.accept(&callback) {
                self.seen.push(callback.clone());
                accepted.push(callback);
            } else {
                self.dropped.push(callback.clone());
                dropped.push(callback);
            }
        }
        (accepted, dropped)
    }

    /// No receipt is outstanding: every command L sent has been answered.
    fn assert_receipts_clear(&self) {
        assert!(
            self.receipts.is_empty(),
            "L's 5 s receipt is still running for {:?}",
            self.receipts.keys()
        );
    }

    fn accept(&mut self, callback: &WorkerCallback) -> bool {
        // HPO:450-451: no session, or a command L does not hold: dropped
        let Some(session) = self.session.clone() else {
            return false;
        };
        let stamp = callback.stamp();
        let Some(command) = self.commands.get(&stamp.request.request_id).cloned() else {
            return false;
        };
        // WorkerCallbackFence.accept (WPF:79-101)
        if stamp.request.session_id != session.session_id
            || stamp.request.lease != session.lease
            || stamp.request != *command.request()
            || stamp.state_seq <= self.state_seq
            || stamp.applied_policy_seq < self.applied
            || stamp.applied_policy_seq > self.highest_sent
        {
            return false;
        }
        let matches = match callback {
            WorkerCallback::Accepted { command: kind, .. } => *kind == command.kind(),
            WorkerCallback::Status { status, .. } => {
                (matches!(command, WorkerCommand::RequestStatus { .. })
                    || (matches!(command, WorkerCommand::Start { .. })
                        && *command.request() == session))
                    && (status.open_grant.is_none()
                        || stamp.applied_policy_seq == self.highest_sent)
            }
            WorkerCallback::ActivationReady { intent, .. } => {
                matches!(&command, WorkerCommand::Activate { intent: sent, .. } if sent == intent)
            }
            WorkerCallback::Released { .. } => matches!(command, WorkerCommand::Release { .. }),
            WorkerCallback::AdmissionApplied { decision, .. } => matches!(
                &command,
                WorkerCommand::SetAdmission { policy_seq, decision: sent, .. }
                    if sent == decision
                        && stamp.applied_policy_seq == *policy_seq
                        && *policy_seq == self.highest_sent
            ),
            WorkerCallback::Drained { .. } => matches!(command, WorkerCommand::Stop { .. }),
            WorkerCallback::Failed { .. } => true,
        };
        if !matches {
            return false;
        }
        self.state_seq = stamp.state_seq;
        self.applied = stamp.applied_policy_seq;
        let id = stamp.request.request_id;
        self.receipts.remove(&id); // HPO:453
        if self.reacting {
            // HPO:454-480, before the completed command is removed at :482
            match callback {
                WorkerCallback::Status { .. } => self.status_query_at = None,
                WorkerCallback::Released { .. } => {
                    self.quiescent = true;
                    self.release_deadline = None;
                }
                WorkerCallback::Failed { .. } if !self.open_sent => {
                    self.revoke(CloseReason::UserStop)
                }
                WorkerCallback::Failed { .. } => self.uncertain_worker(),
                _ => {}
            }
        }
        if !matches!(callback, WorkerCallback::Accepted { .. })
            && !matches!(command, WorkerCommand::Start { .. })
        {
            self.commands.remove(&id); // HPO:482
        }
        true
    }
}

// --- the harness -----------------------------------------------------------------------

fn address(id: &str) -> StoreAddress {
    StoreAddress {
        object_id: id.into(),
        collection: format!("{id}-coll"),
    }
}

fn fixture() -> PreseededFixture {
    PreseededFixture {
        account_root: PathBuf::from("/data/acc"),
        account_id: "acc".into(),
        self_peer_id: "peer".into(),
        bao_root: "bao".into(),
        chat: "chat-1".into(),
        config: address("cfg"),
        secrets: address("sec"),
        triggers: address("trig"),
        runs: address("runs"),
        turns: address("turns"),
        frozen: FrozenPins {
            source_revision: "rev".into(),
            anyrt_revision: "test".into(),
            trace_schema: crate::runner::TRACE_SCHEMA,
            kernel_source_sha256: "0".repeat(64),
            profile_sha256: "0".repeat(64),
            overlays: vec!["agent".into()],
            programs: vec![FrozenProgram {
                alias: "agent".into(),
                name: "toolcaller".into(),
                version: "v1".into(),
                fingerprint: "0".repeat(64),
                credentials_sha256: "0".repeat(64),
                manifest: false,
            }],
            skills: vec![],
        },
    }
}

fn config() -> SessionConfig {
    SessionConfig {
        leased_origin: "http://127.0.0.1:7001".into(),
        space: "sp".into(),
        fixture: Some(fixture()),
    }
}

struct Harness {
    session: Session,
    owner: Arc<OwnerState>,
    sink: Arc<Sink>,
    clock: Arc<Clock>,
    execution: Arc<FakeExecution>,
}

fn harness(preparedness: Preparedness, with_execution: bool) -> Harness {
    let owner = OwnerState::new(preparedness);
    let sink = Arc::new(Sink::default());
    let clock = Arc::new(Clock(AtomicU64::new(100)));
    let execution = Arc::new(FakeExecution::default());
    let sink_dyn: Arc<dyn CallbackSink> = sink.clone();
    let clock_dyn: Arc<dyn BootClock> = clock.clone();
    let mut session = Session::new(
        config(),
        sink_dyn,
        clock_dyn,
        Box::new(FakeOwner(owner.clone())),
    );
    if with_execution {
        let execution_dyn: Arc<dyn Execution> = execution.clone();
        session.install_execution(execution_dyn);
    }
    Harness {
        session,
        owner,
        sink,
        clock,
        execution,
    }
}

fn outcome() -> TakeoverOutcome {
    TakeoverOutcome {
        chat_watch_id: "chat-watch".into(),
        chat_watch: crate::triggers::chat_watch_trigger("chat-watch", "chat-1", "peer"),
        standing: vec![],
        descriptors: 0,
    }
}

/// Start, Activate, the initial Open and a successful takeover: the worker is Open.
fn open_session(h: &mut Harness, main: &mut Main) -> OpenGrant {
    let callbacks = main.send(&mut h.session, &h.sink, start);
    assert!(matches!(
        callbacks.as_slice(),
        [
            WorkerCallback::Accepted {
                command: WorkerCommandKind::Start,
                ..
            },
            WorkerCallback::Status { .. }
        ]
    ));
    let callbacks = main.send(&mut h.session, &h.sink, |request| WorkerCommand::Activate {
        request,
        intent: intent(1),
    });
    assert!(matches!(
        callbacks.as_slice(),
        [
            WorkerCallback::Accepted {
                command: WorkerCommandKind::Activate,
                ..
            },
            WorkerCallback::ActivationReady { .. }
        ]
    ));
    let g = grant(1, 1, 100, EvidenceGrade::Activation);
    let callbacks = main.send(&mut h.session, &h.sink, |request| {
        open(request, 1, g.clone())
    });
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Accepted {
            command: WorkerCommandKind::SetAdmission,
            ..
        }]
    ));
    assert_eq!(callbacks[0].stamp().applied_policy_seq, 0);
    let gen = *h.owner.spawned.lock().unwrap().last().unwrap();
    assert!(h.session.snapshot().takeover_pending);
    assert_eq!(h.session.status().phase, WorkerPhase::Opening);
    h.session.handle(Inbound::Internal(Internal::TakeoverDone {
        gen,
        outcome: Ok(outcome()),
    }));
    let callbacks = main.consume(&h.sink);
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::AdmissionApplied {
            decision: AdmissionDecision::Open(_),
            ..
        }]
    ));
    assert_eq!(callbacks[0].stamp().applied_policy_seq, 1);
    assert_eq!(h.session.status().phase, WorkerPhase::Open);
    assert!(h.owner.admission.load(Ordering::Relaxed));
    g
}

// --- tests ---------------------------------------------------------------------------

#[test]
fn first_start_binds_the_session_and_a_duplicate_is_only_acknowledged() {
    let mut h = harness(Preparedness::Preparing, false);
    let mut main = Main::new();
    let callbacks = main.send(&mut h.session, &h.sink, start);
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Accepted {
            command: WorkerCommandKind::Start,
            ..
        }]
    ));
    assert_eq!(callbacks[0].stamp().state_seq, 1);
    assert!(h.session.snapshot().bound);
    // the identical retransmission is acknowledged and nothing else happens
    let bound = main.session.clone().unwrap();
    h.session.command(start(bound.clone()));
    let callbacks = main.consume(&h.sink);
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Accepted {
            command: WorkerCommandKind::Start,
            ..
        }]
    ));
    assert_eq!(callbacks[0].stamp().state_seq, 2);
    // a second session is refused; the bound one stays
    let mut other = bound.clone();
    other.session_id = "s2".into();
    h.session.command(start(other.clone()));
    let callbacks = h.sink.drain();
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::InvalidLease,
            ..
        }]
    ));
    assert_eq!(callbacks[0].stamp().request, other);
    h.session.command(WorkerCommand::RequestStatus {
        request: other.clone(),
    });
    assert!(matches!(
        h.sink.drain().as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::InvalidLease,
            ..
        }]
    ));
    assert!(h.session.snapshot().bound && !h.session.snapshot().terminal);
    // status answers while preparing
    let callbacks = main.send(&mut h.session, &h.sink, |request| {
        WorkerCommand::RequestStatus { request }
    });
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Status {
            status: WorkerStatus {
                phase: WorkerPhase::Preparing,
                ready: false,
                ..
            },
            ..
        }]
    ));
    // a malformed payload is refused as the adapter's fault, never bound
    let mut bad = bound.clone();
    bad.request_id = 0;
    h.session
        .command(WorkerCommand::RequestStatus { request: bad });
    assert!(matches!(
        h.sink.drain().as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::Internal,
            ..
        }]
    ));
}

#[test]
fn a_refused_first_start_is_terminal_and_every_lease_mismatch_is_refused() {
    // a pre-bind command
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    let request = main.request();
    h.session.command(WorkerCommand::RequestStatus {
        request: request.clone(),
    });
    assert!(matches!(
        h.sink.drain().as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::InvalidLease,
            ..
        }]
    ));
    // the wrong origin: refused, and terminal for the process
    let mut foreign = request.clone();
    foreign.lease.base_url = "http://127.0.0.1:9999".into();
    h.session.command(start(foreign));
    assert!(matches!(
        h.sink.drain().as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::InvalidLease,
            ..
        }]
    ));
    assert!(h.session.snapshot().terminal && !h.session.snapshot().bound);
    h.session.command(start(request.clone()));
    assert!(matches!(
        h.sink.drain().as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::InvalidLease,
            ..
        }]
    ));
    assert!(!h.session.snapshot().bound);

    // another account or peer than explicit setup recorded
    for (field, value) in [("account", "other-acc"), ("peer", "other-peer")] {
        let mut h = harness(Preparedness::Prepared, true);
        let mut wrong = request.clone();
        if field == "account" {
            wrong.lease.account_id = value.into();
        } else {
            wrong.lease.self_peer_id = value.into();
        }
        h.session.command(start(wrong));
        assert!(
            matches!(
                h.sink.drain().as_slice(),
                [WorkerCallback::Failed {
                    failure: WorkerFailure::InvalidLease,
                    ..
                }]
            ),
            "{field}"
        );
        assert!(h.session.snapshot().terminal);
    }

    // main's elapsedRealtimeAtStart after the worker's own Android sample
    let mut h = harness(Preparedness::Prepared, true);
    h.session.command(WorkerCommand::Start {
        request: request.clone(),
        options: WorkerStartOptions {
            artifact: WorkerArtifact::AndroidPocV1,
            elapsed_realtime_at_start: 101,
        },
        calibration: ClockCalibration {
            native_before: 100,
            android_elapsed: 100,
            native_after: 100,
        },
    });
    assert!(matches!(
        h.sink.drain().as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::InvalidLease,
            ..
        }]
    ));

    // a bracket in the future of the boot clock: the worker's own fault
    let mut h = harness(Preparedness::Prepared, true);
    h.session.command(WorkerCommand::Start {
        request: request.clone(),
        options: WorkerStartOptions {
            artifact: WorkerArtifact::AndroidPocV1,
            elapsed_realtime_at_start: 100,
        },
        calibration: ClockCalibration {
            native_before: 100,
            android_elapsed: 150,
            native_after: 200,
        },
    });
    assert!(matches!(
        h.sink.drain().as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::Internal,
            ..
        }]
    ));
    assert!(h.session.snapshot().terminal);
}

#[test]
fn preparation_settling_announces_a_status_and_the_worker_is_not_ready_without_execution() {
    let mut h = harness(Preparedness::Preparing, false);
    let mut main = Main::new();
    main.send(&mut h.session, &h.sink, start);
    h.session.tick();
    assert!(
        h.sink.drain().is_empty(),
        "nothing to announce while preparing"
    );
    h.owner.set(Preparedness::Prepared);
    h.session.tick();
    let callbacks = main.consume(&h.sink);
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Status {
            status: WorkerStatus {
                phase: WorkerPhase::Prepared,
                ready: false,
                failure: None,
                ..
            },
            ..
        }]
    ));
    assert_eq!(callbacks[0].stamp().request, main.session.clone().unwrap());
    h.session.tick();
    assert!(h.sink.drain().is_empty(), "announced once");
    // ready is false by construction without the execution service: no Activate
    let callbacks = main.send(&mut h.session, &h.sink, |request| WorkerCommand::Activate {
        request,
        intent: intent(1),
    });
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::NotReady,
            ..
        }]
    ));
    assert!(h.session.status().intent.is_none());
    let callbacks = main.send(&mut h.session, &h.sink, |request| {
        open(request, 1, grant(1, 1, 100, EvidenceGrade::Activation))
    });
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::NotReady,
            ..
        }]
    ));
    assert!(h.owner.spawned.lock().unwrap().is_empty());
    // NeedsSetup is final for the session: FAILED / NOT_READY
    h.owner.set(Preparedness::NotReady);
    h.session.tick();
    let callbacks = main.consume(&h.sink);
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Status {
            status: WorkerStatus {
                phase: WorkerPhase::Failed,
                ready: false,
                failure: Some(WorkerFailure::NotReady),
                ..
            },
            ..
        }]
    ));
    assert!(!h.session.snapshot().ready);
    // an owner failure is announced once and shows as INTERNAL
    h.owner.set(Preparedness::Prepared);
    h.session.tick();
    main.consume(&h.sink);
    h.owner.failed.store(true, Ordering::Relaxed);
    h.session.tick();
    let callbacks = main.consume(&h.sink);
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Status {
            status: WorkerStatus {
                phase: WorkerPhase::Failed,
                failure: Some(WorkerFailure::Internal),
                ..
            },
            ..
        }]
    ));
    h.session.tick();
    assert!(h.sink.drain().is_empty());
}

#[test]
fn the_open_path_sequences_callbacks_as_the_kotlin_fence_expects() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    let g = open_session(&mut h, &mut main);
    assert_eq!(h.session.status().open_grant, Some(g.clone()));
    assert_eq!(
        *h.execution.opened.lock().unwrap(),
        vec![(g.clone(), "s1".to_string())]
    );
    assert_eq!(*h.owner.gates.lock().unwrap(), vec![Phase::Execution]);
    assert_eq!(*h.owner.spawned.lock().unwrap(), vec![1]);
    // a renewal needs no takeover and applies at once
    let renewal = grant(1, 2, 100, EvidenceGrade::Renewal);
    let callbacks = main.send(&mut h.session, &h.sink, |request| {
        open(request, 2, renewal.clone())
    });
    assert!(matches!(
        callbacks.as_slice(),
        [
            WorkerCallback::Accepted { .. },
            WorkerCallback::AdmissionApplied {
                decision: AdmissionDecision::Open(_),
                ..
            }
        ]
    ));
    assert_eq!(callbacks[0].stamp().applied_policy_seq, 1);
    assert_eq!(callbacks[1].stamp().applied_policy_seq, 2);
    assert_eq!(*h.owner.spawned.lock().unwrap(), vec![1]);
    assert_eq!(h.session.status().open_grant, Some(renewal.clone()));
    // a non-revoking close: Accepted with the old seq, then applied, then a Status
    let callbacks = main.send(&mut h.session, &h.sink, |request| {
        close(request, 3, CloseReason::SyncLoss, None)
    });
    assert!(matches!(
        callbacks.as_slice(),
        [
            WorkerCallback::Accepted { .. },
            WorkerCallback::AdmissionApplied {
                decision: AdmissionDecision::Close {
                    reason: CloseReason::SyncLoss,
                    revoke_through_intent_revision: None
                },
                ..
            },
            WorkerCallback::Status {
                status: WorkerStatus {
                    phase: WorkerPhase::Prepared,
                    open_grant: None,
                    ..
                },
                ..
            }
        ]
    ));
    assert_eq!(callbacks[0].stamp().applied_policy_seq, 2);
    assert_eq!(callbacks[1].stamp().applied_policy_seq, 3);
    assert_eq!(callbacks[2].stamp().applied_policy_seq, 3);
    assert_eq!(
        h.session.status().intent,
        Some(intent(1)),
        "sync loss keeps the intent"
    );
    assert_eq!(*h.execution.closed.lock().unwrap(), vec![false]);
    assert!(!h.owner.admission.load(Ordering::Relaxed));
    assert_eq!(
        *h.owner.gates.lock().unwrap(),
        vec![Phase::Execution, Phase::Execution],
        "every accepted Open asserts Execution; with an execution service the gate flips \
         back after its last bookkeeping, not here"
    );
    // a revoking close drops the intent at or below the barrier
    let callbacks = main.send(&mut h.session, &h.sink, |request| {
        close(request, 4, CloseReason::OwnershipLost, Some(1))
    });
    assert_eq!(callbacks.len(), 3);
    assert!(h.session.status().intent.is_none());
    assert_eq!(*h.execution.closed.lock().unwrap(), vec![false, true]);
    // stateSeq strictly increasing over everything the oracle saw
    for pair in main.seen.windows(2) {
        assert!(pair[0].stamp().state_seq < pair[1].stamp().state_seq);
    }
    assert_eq!(main.seen.len(), 14);
}

#[test]
fn a_late_takeover_outcome_is_ignored_and_a_failed_one_never_opens() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    main.send(&mut h.session, &h.sink, start);
    main.send(&mut h.session, &h.sink, |request| WorkerCommand::Activate {
        request,
        intent: intent(1),
    });
    main.send(&mut h.session, &h.sink, |request| {
        open(request, 1, grant(1, 1, 100, EvidenceGrade::Activation))
    });
    let first = *h.owner.spawned.lock().unwrap().last().unwrap();
    // a close while the takeover is in flight supersedes it
    let callbacks = main.send(&mut h.session, &h.sink, |request| {
        close(request, 2, CloseReason::SyncLoss, None)
    });
    assert_eq!(callbacks.len(), 3);
    assert!(!h.session.snapshot().takeover_pending);
    h.session.handle(Inbound::Internal(Internal::TakeoverDone {
        gen: first,
        outcome: Ok(outcome()),
    }));
    assert!(
        h.sink.drain().is_empty(),
        "a superseded outcome emits nothing"
    );
    assert!(!h.owner.admission.load(Ordering::Relaxed));
    assert!(h.execution.opened.lock().unwrap().is_empty());
    assert_eq!(h.session.status().phase, WorkerPhase::Prepared);
    // a failed takeover: Failed(TAKEOVER_FAILED), a Status, no AppliedOpen
    main.send(&mut h.session, &h.sink, |request| {
        open(request, 3, grant(1, 2, 100, EvidenceGrade::Activation))
    });
    let second = *h.owner.spawned.lock().unwrap().last().unwrap();
    assert!(second > first);
    h.session.handle(Inbound::Internal(Internal::TakeoverDone {
        gen: second,
        outcome: Err(TakeoverError::Write("rollup: rejected".into())),
    }));
    let callbacks = main.consume(&h.sink);
    assert!(matches!(
        callbacks.as_slice(),
        [
            WorkerCallback::Failed {
                failure: WorkerFailure::TakeoverFailed,
                ..
            },
            WorkerCallback::Status {
                status: WorkerStatus {
                    phase: WorkerPhase::Prepared,
                    ready: true,
                    failure: Some(WorkerFailure::TakeoverFailed),
                    ..
                },
                ..
            }
        ]
    ));
    assert!(!h.owner.admission.load(Ordering::Relaxed));
    assert!(!h.owner.needs_setup.load(Ordering::Relaxed));
    // the fence consumed the sequences: the same policy/evidence cannot be replayed
    let callbacks = main.send(&mut h.session, &h.sink, |request| {
        open(request, 3, grant(1, 2, 100, EvidenceGrade::Activation))
    });
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::InvalidSequence,
            ..
        }]
    ));
    // skill drift at Open is NeedsSetup
    main.send(&mut h.session, &h.sink, |request| {
        open(request, 4, grant(1, 3, 100, EvidenceGrade::Activation))
    });
    let third = *h.owner.spawned.lock().unwrap().last().unwrap();
    h.session.handle(Inbound::Internal(Internal::TakeoverDone {
        gen: third,
        outcome: Err(TakeoverError::SkillDrift),
    }));
    let callbacks = main.consume(&h.sink);
    assert!(h.owner.needs_setup.load(Ordering::Relaxed));
    assert!(matches!(
        callbacks.as_slice(),
        [
            WorkerCallback::Failed {
                failure: WorkerFailure::TakeoverFailed,
                ..
            },
            WorkerCallback::Status {
                status: WorkerStatus {
                    phase: WorkerPhase::Failed,
                    ready: false,
                    failure: Some(WorkerFailure::NotReady),
                    ..
                },
                ..
            }
        ]
    ));
    // a takeover thread that cannot start fails the same way
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    h.owner.spawn_ok.store(false, Ordering::Relaxed);
    main.send(&mut h.session, &h.sink, start);
    main.send(&mut h.session, &h.sink, |request| WorkerCommand::Activate {
        request,
        intent: intent(1),
    });
    let callbacks = main.send(&mut h.session, &h.sink, |request| {
        open(request, 1, grant(1, 1, 100, EvidenceGrade::Activation))
    });
    assert!(matches!(
        callbacks.as_slice(),
        [
            WorkerCallback::Accepted { .. },
            WorkerCallback::Failed {
                failure: WorkerFailure::TakeoverFailed,
                ..
            },
            WorkerCallback::Status { .. }
        ]
    ));
}

#[test]
fn grant_expiry_self_closes_with_a_status_and_never_a_close() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    open_session(&mut h, &mut main);
    h.clock.0.store(30_099, Ordering::Relaxed);
    h.session.tick();
    assert!(h.sink.drain().is_empty(), "still fresh");
    h.clock.0.store(30_100, Ordering::Relaxed);
    h.session.tick();
    let callbacks = main.consume(&h.sink);
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Status {
            status: WorkerStatus {
                phase: WorkerPhase::Prepared,
                open_grant: None,
                intent: Some(_),
                ..
            },
            ..
        }]
    ));
    assert!(!h.owner.admission.load(Ordering::Relaxed));
    assert_eq!(*h.execution.closed.lock().unwrap(), vec![false]);
    h.session.tick();
    assert!(h.sink.drain().is_empty());
    // reopening needs activation-grade evidence (the fence's rule), and takes over again
    let callbacks = main.send(&mut h.session, &h.sink, |request| {
        open(request, 2, grant(1, 2, 30_100, EvidenceGrade::Renewal))
    });
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::InvalidGrant,
            ..
        }]
    ));
    let callbacks = main.send(&mut h.session, &h.sink, |request| {
        open(request, 3, grant(1, 3, 30_100, EvidenceGrade::Activation))
    });
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Accepted { .. }]
    ));
    assert_eq!(h.owner.spawned.lock().unwrap().len(), 2);
}

#[test]
fn release_reports_released_when_quiescent_and_deadline_exceeded_otherwise() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    open_session(&mut h, &mut main);
    let callbacks = main.send(&mut h.session, &h.sink, |request| WorkerCommand::Release {
        request,
        reason: ReleaseReason::UserStop,
        deadline_elapsed_realtime: 10_000,
        revoke_through_intent_revision: 1,
    });
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Accepted {
            command: WorkerCommandKind::Release,
            ..
        }]
    ));
    let status = h.session.status();
    assert_eq!(status.phase, WorkerPhase::Releasing);
    assert!(status.intent.is_none() && status.open_grant.is_none());
    assert_eq!(*h.execution.closed.lock().unwrap(), vec![true]);
    assert!(!h.owner.admission.load(Ordering::Relaxed));
    h.clock.0.store(9_999, Ordering::Relaxed);
    h.session.tick();
    assert!(h.sink.drain().is_empty());
    h.clock.0.store(10_000, Ordering::Relaxed);
    h.session.tick();
    let callbacks = main.consume(&h.sink);
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::DeadlineExceeded,
            ..
        }]
    ));
    assert_eq!(
        h.session.status().failure,
        Some(WorkerFailure::DeadlineExceeded)
    );
    h.session.tick();
    assert!(h.sink.drain().is_empty(), "reported once");
    // L removed the Release at that Failed (HPO:482): late quiescence is
    // reported by the Start-correlated Status alone, never a Released L drops
    h.execution.quiescent.store(true, Ordering::Relaxed);
    h.session.tick();
    let callbacks = main.consume(&h.sink);
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Status {
            status: WorkerStatus {
                phase: WorkerPhase::Quiescent,
                ready: true,
                failure: None,
                ..
            },
            ..
        }]
    ));
    main.assert_receipts_clear();
    assert_eq!(
        *h.owner.gates.lock().unwrap().last().unwrap(),
        Phase::Preparation
    );
    // a fresh intent above the barrier activates; a stale barrier is refused
    let callbacks = main.send(&mut h.session, &h.sink, |request| WorkerCommand::Activate {
        request,
        intent: intent(2),
    });
    assert_eq!(callbacks.len(), 2);
    let callbacks = main.send(&mut h.session, &h.sink, |request| WorkerCommand::Release {
        request,
        reason: ReleaseReason::UserStop,
        deadline_elapsed_realtime: 20_000,
        revoke_through_intent_revision: 1,
    });
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::InvalidGrant,
            ..
        }]
    ));
    assert_eq!(h.session.status().intent, Some(intent(2)));
}

#[test]
fn stop_drains_the_owned_execution_threads_and_status_still_answers() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    open_session(&mut h, &mut main);
    h.execution.quiescent.store(true, Ordering::Relaxed);
    h.owner.remaining.store(2, Ordering::Relaxed);
    // H retires the worker with Stop; L holds no Stop, so it drops all of it
    let callbacks = main.retire(&mut h.session, &h.sink);
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Accepted {
            command: WorkerCommandKind::Stop,
            ..
        }]
    ));
    assert!(h.owner.cancelled.load(Ordering::Relaxed));
    assert_eq!(
        *h.owner.gates.lock().unwrap().last().unwrap(),
        Phase::Preparation
    );
    assert_eq!(*h.execution.closed.lock().unwrap(), vec![true]);
    assert_eq!(h.session.status().phase, WorkerPhase::Stopping);
    h.session.tick();
    assert!(h.sink.drain().is_empty(), "two threads still running");
    h.owner.remaining.store(0, Ordering::Relaxed);
    h.session.tick();
    let callbacks = main.consume_dropped(&h.sink);
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Drained {
            result: DrainResult::Drained,
            ..
        }]
    ));
    assert!(h.session.snapshot().terminal);
    // status still answers; nothing else is accepted, not even the identical
    // Start. A retiring L sends nothing (HPO:549): these are raw injections.
    let request = main.request();
    let (_, callbacks) = main.inject(
        &mut h.session,
        &h.sink,
        WorkerCommand::RequestStatus { request },
    );
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Status {
            status: WorkerStatus {
                phase: WorkerPhase::Drained,
                ready: false,
                ..
            },
            ..
        }]
    ));
    let request = main.request();
    let (_, callbacks) = main.inject(
        &mut h.session,
        &h.sink,
        WorkerCommand::Activate {
            request,
            intent: intent(2),
        },
    );
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::InvalidSequence,
            ..
        }]
    ));
    h.session.command(start(main.session.clone().unwrap()));
    assert!(matches!(
        h.sink.drain().as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::InvalidLease,
            ..
        }]
    ));

    // the deadline: Drained(DEADLINE_EXCEEDED), the handles stay the owner's
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    open_session(&mut h, &mut main);
    h.execution.quiescent.store(true, Ordering::Relaxed);
    h.owner.remaining.store(3, Ordering::Relaxed);
    main.retire(&mut h.session, &h.sink);
    // HPO:564: no Release deadline pending, so H's Stop deadline is now + 15 s
    h.clock.0.store(15_100, Ordering::Relaxed);
    h.session.tick();
    let callbacks = main.consume_dropped(&h.sink);
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Drained {
            result: DrainResult::DeadlineExceeded,
            ..
        }]
    ));
    let status = h.session.status();
    assert_eq!(status.phase, WorkerPhase::Stopping);
    assert_eq!(status.failure, Some(WorkerFailure::DeadlineExceeded));

    // execution not yet quiescent: the cancel waits for the stand-down writes
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    open_session(&mut h, &mut main);
    main.retire(&mut h.session, &h.sink);
    assert!(!h.owner.cancelled.load(Ordering::Relaxed));
    h.execution.quiescent.store(true, Ordering::Relaxed);
    h.session.tick();
    assert!(h.owner.cancelled.load(Ordering::Relaxed));
    let callbacks = main.consume_dropped(&h.sink);
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Drained {
            result: DrainResult::Drained,
            ..
        }]
    ));
}

#[test]
fn a_capacity_fault_is_announced_and_the_worker_is_not_ready() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    open_session(&mut h, &mut main);
    h.execution.fault.store(true, Ordering::Relaxed);
    h.session.tick();
    let callbacks = main.consume(&h.sink);
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Status {
            status: WorkerStatus {
                ready: false,
                failure: Some(WorkerFailure::Internal),
                ..
            },
            ..
        }]
    ));
    h.session.tick();
    assert!(h.sink.drain().is_empty());
    h.execution.fault.store(false, Ordering::Relaxed);
    h.session.tick();
    assert_eq!(
        main.consume(&h.sink).len(),
        1,
        "the recovery is announced too"
    );
}

#[test]
fn the_session_word_round_trips_for_health() {
    for snapshot in [
        SessionSnapshot::default(),
        SessionSnapshot {
            bound: true,
            phase: Some(WorkerPhase::Opening),
            ready: true,
            terminal: false,
            takeover_pending: true,
            admission: false,
            state_seq: 42,
        },
        SessionSnapshot {
            bound: true,
            phase: Some(WorkerPhase::Open),
            ready: true,
            terminal: false,
            takeover_pending: false,
            admission: true,
            state_seq: 7,
        },
        SessionSnapshot {
            bound: true,
            phase: Some(WorkerPhase::Drained),
            ready: false,
            terminal: true,
            takeover_pending: false,
            admission: false,
            state_seq: u64::MAX >> 16,
        },
    ] {
        assert_eq!(SessionSnapshot::from_word(snapshot.word()), snapshot);
    }
    let h = harness(Preparedness::Preparing, false);
    let words = h.owner.words.lock().unwrap().clone();
    assert!(words.is_empty());
    assert_eq!(h.session.snapshot(), SessionSnapshot::default());
}

#[test]
fn the_consumer_thread_runs_under_the_owner_answers_through_the_port_and_exits_on_release() {
    use crate::managed::{start_with, CancelToken, DrainOutcome};
    let budget = Duration::from_secs(5);
    let mut agent = start_with(CancelToken::new(), |ctx| {
        while !ctx.idle_until(Instant::now() + Duration::from_secs(300)) {}
        Ok(())
    });
    let sink = Arc::new(Sink::default());
    let clock = Arc::new(Clock(AtomicU64::new(100)));
    let sink_dyn: Arc<dyn CallbackSink> = sink.clone();
    let clock_dyn: Arc<dyn BootClock> = clock.clone();
    let port = spawn_consumer(&agent.shared, config(), sink_dyn, clock_dyn).unwrap();
    let mut main = Main::new();
    let request = main.request();
    main.register(start(request.clone()));
    port.offer(start(request.clone())).unwrap();
    let deadline = Instant::now() + budget;
    while sink.0.lock().unwrap().is_empty() {
        assert!(Instant::now() < deadline, "no callback from the consumer");
        std::thread::sleep(Duration::from_millis(2));
    }
    let callbacks = main.consume(&sink);
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Accepted {
            command: WorkerCommandKind::Start,
            ..
        }]
    ));
    // the owner reads the session word without any lock
    let deadline = Instant::now() + budget;
    while !agent.health().session.bound {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(2));
    }
    assert_eq!(agent.health().session.phase, Some(WorkerPhase::Preparing));
    assert!(!agent.health().admission_open);
    // a control command rides its own slot and is answered (to H: L holds no Stop)
    let stop = main.request();
    port.offer(WorkerCommand::Stop {
        request: stop,
        deadline_elapsed_realtime: 100,
    })
    .unwrap();
    let deadline = Instant::now() + budget;
    while sink.0.lock().unwrap().len() < 2 {
        assert!(Instant::now() < deadline, "no Drained from the consumer");
        std::thread::sleep(Duration::from_millis(2));
    }
    let callbacks = main.consume_dropped(&sink);
    assert!(matches!(
        callbacks.as_slice(),
        [
            WorkerCallback::Accepted {
                command: WorkerCommandKind::Stop,
                ..
            },
            WorkerCallback::Drained { .. }
        ]
    ));
    // the consumer survives the cancel and exits only when the owner releases it
    assert!(agent.health().cancelled);
    assert_eq!(
        agent.drain_until(Instant::now() + budget),
        DrainOutcome::Drained
    );
    assert!(agent.health().all_threads_joined);
}

// --- N2b-1 review items 1, 2, 4, 7 and the cheap P3s ------------------------------------

/// Item 1: Release waits for an in-flight takeover; the takeover is told to
/// stop (its supersede flag) and `Released` follows only its report.
#[test]
fn release_waits_for_a_superseded_takeover_and_the_task_is_told_to_stop() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    open_session(&mut h, &mut main);
    // a non-revoking close, then a reopen whose takeover stays in flight
    main.send(&mut h.session, &h.sink, |request| {
        close(request, 2, CloseReason::SyncLoss, None)
    });
    let g = grant(1, 2, 100, EvidenceGrade::Activation);
    main.send(&mut h.session, &h.sink, |request| {
        open(request, 3, g.clone())
    });
    assert_eq!(h.session.status().phase, WorkerPhase::Opening);
    assert_eq!(h.owner.spawned.lock().unwrap().len(), 2);
    let flag = h.owner.superseded.lock().unwrap().last().unwrap().clone();
    assert!(!flag.load(Ordering::Relaxed));
    h.execution.quiescent.store(true, Ordering::Relaxed);
    let callbacks = main.send(&mut h.session, &h.sink, |request| WorkerCommand::Release {
        request,
        reason: ReleaseReason::UserStop,
        deadline_elapsed_realtime: 10_000,
        revoke_through_intent_revision: 1,
    });
    assert!(
        matches!(
            callbacks.as_slice(),
            [WorkerCallback::Accepted {
                command: WorkerCommandKind::Release,
                ..
            }]
        ),
        "no Released while the takeover is in flight"
    );
    assert!(
        flag.load(Ordering::Relaxed),
        "the stale takeover is told to stop"
    );
    assert_eq!(h.session.status().phase, WorkerPhase::Releasing);
    // its (superseded) report arrives: now the session is quiescent
    let gen = *h.owner.spawned.lock().unwrap().last().unwrap();
    h.session.handle(Inbound::Internal(Internal::TakeoverDone {
        gen,
        outcome: Err(TakeoverError::Superseded),
    }));
    let callbacks = main.consume(&h.sink);
    assert!(matches!(
        callbacks.as_slice(),
        [
            WorkerCallback::Released { .. },
            WorkerCallback::Status { .. }
        ]
    ));
    assert_eq!(h.session.status().phase, WorkerPhase::Quiescent);
    assert!(!h.owner.admission.load(Ordering::Relaxed));
}

/// Item 2: an Open re-sent for the intent whose takeover is in flight adopts
/// the newer policy/evidence and starts no second takeover; the one outcome
/// opens with the latest sequence.
#[test]
fn a_repeated_open_during_its_takeover_is_idempotent_and_opens_with_the_latest_sequence() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    open_session(&mut h, &mut main);
    main.send(&mut h.session, &h.sink, |request| {
        close(request, 2, CloseReason::SyncLoss, None)
    });
    let g = grant(1, 2, 100, EvidenceGrade::Activation);
    main.send(&mut h.session, &h.sink, |request| {
        open(request, 3, g.clone())
    });
    let gen = *h.owner.spawned.lock().unwrap().last().unwrap();
    // the 5 s re-send: a newer policy and evidence sequence, the same intent
    let again = grant(1, 3, 100, EvidenceGrade::Activation);
    let callbacks = main.send(&mut h.session, &h.sink, |request| {
        open(request, 4, again.clone())
    });
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Accepted {
            command: WorkerCommandKind::SetAdmission,
            ..
        }]
    ));
    assert_eq!(
        h.owner.spawned.lock().unwrap().len(),
        2,
        "no second takeover"
    );
    assert_eq!(h.session.status().phase, WorkerPhase::Opening);
    h.session.handle(Inbound::Internal(Internal::TakeoverDone {
        gen,
        outcome: Ok(outcome()),
    }));
    let callbacks = main.consume(&h.sink);
    match callbacks.as_slice() {
        [WorkerCallback::AdmissionApplied {
            stamp,
            decision: AdmissionDecision::Open(opened),
        }] => {
            assert_eq!(stamp.applied_policy_seq, 4);
            assert_eq!(opened.evidence_seq, 3);
        }
        other => panic!("{other:?}"),
    }
    assert_eq!(h.session.status().phase, WorkerPhase::Open);
}

/// Item 7: an owner cancel that is not this session's Stop closes locally,
/// announces one Status, and the worker is neither ready nor open.
#[test]
fn an_owner_cancel_outside_stop_closes_and_is_announced_once() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    open_session(&mut h, &mut main);
    h.owner.cancelled.store(true, Ordering::Relaxed);
    h.session.tick();
    let callbacks = main.consume(&h.sink);
    match callbacks.as_slice() {
        [WorkerCallback::Status { status, .. }] => {
            assert_eq!(status.phase, WorkerPhase::Stopping);
            assert!(!status.ready);
            assert!(status.open_grant.is_none());
        }
        other => panic!("{other:?}"),
    }
    assert!(!h.owner.admission.load(Ordering::Relaxed));
    h.session.tick();
    assert!(main.consume(&h.sink).is_empty(), "announced once");
    let callbacks = main.send(&mut h.session, &h.sink, |request| WorkerCommand::Activate {
        request,
        intent: intent(2),
    });
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Failed { .. }]
    ));
}

/// P3: a first Start refused by validation is terminal; a FAILED worker never
/// reports an open grant.
#[test]
fn a_first_start_refused_by_validation_is_terminal_and_a_failed_status_carries_no_grant() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    let request = main.request();
    let unordered = WorkerCommand::Start {
        request: request.clone(),
        options: WorkerStartOptions {
            artifact: WorkerArtifact::AndroidPocV1,
            elapsed_realtime_at_start: 100,
        },
        calibration: ClockCalibration {
            native_before: 100,
            android_elapsed: 90,
            native_after: 100,
        },
    };
    main.register(unordered.clone());
    h.session.command(unordered);
    assert!(matches!(
        main.consume(&h.sink).as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::Internal,
            ..
        }]
    ));
    assert!(h.session.snapshot().terminal);
    // a later Start (a new request L never registered: it holds one Start per
    // session, HPO:84) is refused terminal; L drops that refusal
    let (accepted, dropped) = main.send_split(&mut h.session, &h.sink, start);
    assert!(accepted.is_empty());
    assert!(matches!(
        dropped.as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::InvalidLease,
            ..
        }]
    ));
    // a FAILED worker (owner failure while open) reports no grant
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    open_session(&mut h, &mut main);
    h.owner.failed.store(true, Ordering::Relaxed);
    let status = h.session.status();
    assert_eq!(status.phase, WorkerPhase::Failed);
    assert!(status.open_grant.is_none());
}

/// Item 4: through the real consumer, a Close offered before a RequestStatus
/// is answered before it — the control reserve never reorders commands.
#[test]
fn commands_are_answered_in_arrival_order_through_the_real_consumer() {
    use crate::managed::{start_with, CancelToken, DrainOutcome};
    let budget = Duration::from_secs(5);
    let mut agent = start_with(CancelToken::new(), |ctx| {
        while !ctx.idle_until(Instant::now() + Duration::from_secs(300)) {}
        Ok(())
    });
    let sink = Arc::new(Sink::default());
    let clock = Arc::new(Clock(AtomicU64::new(100)));
    let sink_dyn: Arc<dyn CallbackSink> = sink.clone();
    let clock_dyn: Arc<dyn BootClock> = clock.clone();
    let port = spawn_consumer(&agent.shared, config(), sink_dyn, clock_dyn).unwrap();
    let mut main = Main::new();
    let request = main.request();
    main.register(start(request.clone()));
    port.offer(start(request)).unwrap();
    let deadline = Instant::now() + budget;
    while sink.0.lock().unwrap().is_empty() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(2));
    }
    main.consume(&sink);
    // Close (control) then RequestStatus (ordinary), offered back to back
    let close_request = main.request();
    main.register(close(close_request.clone(), 1, CloseReason::SyncLoss, None));
    let status_request = main.request();
    main.register(WorkerCommand::RequestStatus {
        request: status_request.clone(),
    });
    port.offer(close(close_request.clone(), 1, CloseReason::SyncLoss, None))
        .unwrap();
    port.offer(WorkerCommand::RequestStatus {
        request: status_request.clone(),
    })
    .unwrap();
    let deadline = Instant::now() + budget;
    while sink.0.lock().unwrap().len() < 4 {
        assert!(Instant::now() < deadline, "{:?}", sink.0.lock().unwrap());
        std::thread::sleep(Duration::from_millis(2));
    }
    let callbacks = main.consume(&sink);
    let ids: Vec<u64> = callbacks
        .iter()
        .map(|c| c.stamp().request.request_id)
        .collect();
    assert_eq!(
        ids,
        vec![
            close_request.request_id,
            close_request.request_id,
            1,
            status_request.request_id
        ],
        "{callbacks:?}"
    );
    assert_eq!(
        agent.drain_until(Instant::now() + budget),
        DrainOutcome::Drained
    );
}

// --- Step B review corrections ----------------------------------------------------------

fn release(revoke_through: u64, deadline: u64) -> impl FnOnce(WorkerRequest) -> WorkerCommand {
    move |request| WorkerCommand::Release {
        request,
        reason: ReleaseReason::UserStop,
        deadline_elapsed_realtime: deadline,
        revoke_through_intent_revision: revoke_through,
    }
}

/// Open, then a non-revoking Close and a reopen whose takeover stays in flight.
fn reopen_in_flight(h: &mut Harness, main: &mut Main) -> u64 {
    open_session(h, main);
    main.send(&mut h.session, &h.sink, |request| {
        close(request, 2, CloseReason::SyncLoss, None)
    });
    let g = grant(1, 2, 100, EvidenceGrade::Activation);
    main.send(&mut h.session, &h.sink, |request| open(request, 3, g));
    *h.owner.spawned.lock().unwrap().last().unwrap()
}

/// L-driven (`Main::reacting`), from a state L produces: Start, Activate and
/// the initial activation Open, whose takeover is still pending; L then
/// observes a foreign winner and revokes for ownership loss (HPO:502-520) —
/// its revoking Close and its Release reach the worker, which supersedes the
/// takeover and waits for its task. (Not a Close followed by a reopen of the
/// same intent: L's sync-gap rules forbid that, sb-fix r3 review SBC3-P-1.)
/// Returns the in-flight takeover's generation.
fn release_waiting_on_a_takeover(h: &mut Harness, main: &mut Main) -> u64 {
    main.send(&mut h.session, &h.sink, start);
    main.send(&mut h.session, &h.sink, |request| WorkerCommand::Activate {
        request,
        intent: intent(1),
    });
    let g = grant(1, 1, 100, EvidenceGrade::Activation);
    main.send(&mut h.session, &h.sink, |request| open(request, 1, g));
    let gen = *h.owner.spawned.lock().unwrap().last().unwrap();
    assert_eq!(h.session.status().phase, WorkerPhase::Opening);
    h.execution.quiescent.store(true, Ordering::Relaxed);
    main.revoke(CloseReason::OwnershipLost);
    let callbacks = main.deliver(&mut h.session, &h.sink);
    assert!(matches!(
        callbacks.last(),
        Some(WorkerCallback::Accepted {
            command: WorkerCommandKind::Release,
            ..
        })
    ));
    assert_eq!(main.release_deadline, Some(10_100));
    assert_eq!(h.session.status().phase, WorkerPhase::Releasing);
    gen
}

/// SBC-P-1, ordering A — L's tick reaches the Release deadline before the
/// worker's Failed arrives: L retires (HPO:238), so the Failed's revoke sends
/// nothing (HPO:549); H's Stop drains the worker; no Released is ever sent
/// and L is left with nothing outstanding.
/// Setup: `release_waiting_on_a_takeover` (initial Open pending, then an
/// ownership-loss revoke). Restriction: one session, no claim or beat
/// deadline in play, the test ends before confirmed death.
#[test]
fn a_release_deadline_seen_by_l_first_retires_and_suppresses_its_second_release() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::reacting();
    let gen = release_waiting_on_a_takeover(&mut h, &mut main);
    main.tick(10_100);
    assert!(main.retiring, "release deadline without quiescence");
    h.clock.0.store(10_100, Ordering::Relaxed);
    h.session.tick();
    assert!(matches!(
        main.consume(&h.sink).as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::DeadlineExceeded,
            ..
        }]
    ));
    assert_eq!(main.suppressed.len(), 2, "the revoke's Close and Release");
    assert!(main.outbox.is_empty());
    let stopped = main.deliver_stops(&mut h.session, &h.sink);
    assert!(matches!(
        stopped.as_slice(),
        [WorkerCallback::Accepted {
            command: WorkerCommandKind::Stop,
            ..
        }]
    ));
    h.session.handle(Inbound::Internal(Internal::TakeoverDone {
        gen,
        outcome: Err(TakeoverError::Superseded),
    }));
    let drained = main.consume_dropped(&h.sink);
    assert!(matches!(
        drained.as_slice(),
        [WorkerCallback::Drained {
            result: DrainResult::Drained,
            ..
        }]
    ));
    assert!(!main
        .seen
        .iter()
        .chain(main.dropped.iter())
        .any(|c| matches!(c, WorkerCallback::Released { .. })));
    main.assert_receipts_clear();
}

/// SBC-P-1, ordering B — the worker's Failed arrives before L's tick: L
/// revokes again (a revoking Close and a second Release, new deadline), its
/// tick then sees that later deadline, and the second Release gets
/// `Released` at quiescence; L never retires and never enters recovery.
/// Setup: `release_waiting_on_a_takeover` (initial Open pending, then an
/// ownership-loss revoke). Restriction: one session, no claim or beat
/// deadline in play, the test ends before confirmed death.
#[test]
fn a_release_failed_before_ls_tick_is_revoked_again_and_the_second_release_gets_released() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::reacting();
    let gen = release_waiting_on_a_takeover(&mut h, &mut main);
    h.clock.0.store(10_100, Ordering::Relaxed);
    h.session.tick();
    main.now = 10_100;
    assert!(matches!(
        main.consume(&h.sink).as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::DeadlineExceeded,
            ..
        }]
    ));
    assert_eq!(main.outbox.len(), 2, "the revoke's Close and Release");
    main.tick(10_100);
    assert!(!main.retiring, "the new Release deadline is 20_100");
    main.deliver(&mut h.session, &h.sink);
    h.session.handle(Inbound::Internal(Internal::TakeoverDone {
        gen,
        outcome: Err(TakeoverError::Superseded),
    }));
    let callbacks = main.consume(&h.sink);
    assert!(matches!(
        callbacks.as_slice(),
        [
            WorkerCallback::Released { .. },
            WorkerCallback::Status { .. }
        ]
    ));
    assert!(main.quiescent && main.release_deadline.is_none());
    main.tick(40_000);
    assert!(!main.retiring && main.uncertain == 0);
    assert!(main.dropped.is_empty(), "L dropped {:?}", main.dropped);
    main.assert_receipts_clear();
}

/// SBC-P-1/SBC2-P-1: a renewal Open whose delivery is lost expires its
/// receipt on L's clock (HPO:239-241): L enters `uncertainWorker` — a
/// HEALTH_FAILURE Close and a RequestStatus, which starts the status-query
/// timer — and the worker's answers end that query: no retirement. If those
/// are lost too, the next tick finds the receipts expired (a second
/// `uncertainWorker`, which sends no second query) and the query overdue: L
/// closes and retires (HPO:242-244). Restriction: activation completed, no
/// claim or beat deadline pending.
#[test]
fn a_lost_renewal_expires_its_receipt_and_l_recovers_or_retires_by_its_status_query() {
    for answered in [true, false] {
        let mut h = harness(Preparedness::Prepared, true);
        let mut main = Main::reacting();
        open_session(&mut h, &mut main);
        main.send_lost(|request| open(request, 2, grant(1, 2, 100, EvidenceGrade::Renewal)));
        main.tick(5_099);
        assert_eq!(main.uncertain, 0, "inside the 5 s budget");
        main.tick(5_100);
        assert_eq!(main.uncertain, 1);
        assert_eq!(main.status_query_at, Some(5_100), "L's query path");
        assert!(matches!(
            main.outbox.as_slice(),
            [
                WorkerCommand::SetAdmission {
                    decision: AdmissionDecision::Close {
                        reason: CloseReason::HealthFailure,
                        ..
                    },
                    ..
                },
                WorkerCommand::RequestStatus { .. }
            ]
        ));
        if answered {
            main.deliver(&mut h.session, &h.sink);
            assert!(main.status_query_at.is_none(), "answered");
            main.tick(20_000);
            assert!(!main.retiring, "answered: no retirement");
        } else {
            main.outbox.clear(); // the Close and the query are lost too
            main.tick(10_100);
            assert_eq!(main.uncertain, 2);
            assert!(main.retiring, "the query went unanswered");
            assert_eq!(main.stops.len(), 1);
        }
    }
}

/// SBC-P-1: at 64 held commands L retires instead of sending (HPO:550), and
/// a retiring L sends nothing (HPO:549); a refused offer retires too (HPO:557).
#[test]
fn l_retires_at_sixty_four_held_commands_or_on_a_refused_offer() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::reacting();
    main.send(&mut h.session, &h.sink, start);
    for _ in 0..63 {
        main.send_lost(|request| WorkerCommand::RequestStatus { request });
    }
    assert_eq!(main.commands.len(), 64);
    assert!(!main.retiring);
    main.send_lost(|request| WorkerCommand::RequestStatus { request });
    assert!(main.retiring);
    assert_eq!((main.commands.len(), main.suppressed.len()), (64, 1));
    assert_eq!(main.stops.len(), 1, "H retires the worker with Stop");
    main.send_lost(|request| WorkerCommand::RequestStatus { request });
    assert_eq!(main.suppressed.len(), 2);

    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::reacting();
    main.send(&mut h.session, &h.sink, start);
    main.refuse_offers = true;
    let (accepted, _) = main.send_split(&mut h.session, &h.sink, |request| {
        WorkerCommand::RequestStatus { request }
    });
    assert!(accepted.is_empty(), "never delivered");
    assert!(main.retiring && main.commands.len() == 2);
}

/// SBC-P-4/SBC2-P-3/SBC3-P-2: Stop supersedes waiting Releases without
/// answering them (the ADR-034 §7 exception), and that is safe for L:
/// retirement suppresses every send (HPO:549), `requestRetirement` is
/// idempotent, and confirmed death removes the session. L's own `revoke`
/// sends the second Release (moving L's Release deadline to 15 000); the
/// worker receives it before any deadline, but its `Accepted` is delayed. L's
/// tick at 15 000 retires (the deadline, unquiescent) and in the same tick
/// expires that Release's receipt — `uncertainWorker` while retiring issues no
/// worker command and does not restart retirement. Both clocks move together;
/// H's Stop reaches the worker before its next release-deadline check.
/// Restriction: one session, initial Open pending (no claim or beat deadline
/// in play), the test ends before confirmed death.
#[test]
fn stop_supersedes_waiting_releases_and_retiring_l_sends_nothing_more() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::reacting();
    let gen = release_waiting_on_a_takeover(&mut h, &mut main);
    let at = |h: &Harness, main: &mut Main, ms: u64| {
        h.clock.0.store(ms, Ordering::Relaxed);
        main.now = ms;
    };
    at(&h, &mut main, 5_000);
    main.revoke(CloseReason::UserStop);
    assert_eq!(
        main.release_deadline,
        Some(15_000),
        "revoke moved the deadline"
    );
    let second = main.outbox.pop().unwrap();
    assert!(matches!(second, WorkerCommand::Release { .. }));
    main.deliver(&mut h.session, &h.sink); // the Close
    h.session.command(second); // the second Release arrives in time ...
    let delayed = h.sink.drain(); // ... but its Accepted is delayed
    assert!(matches!(
        delayed.as_slice(),
        [WorkerCallback::Accepted {
            command: WorkerCommandKind::Release,
            ..
        }]
    ));
    at(&h, &mut main, 15_000);
    main.tick(15_000);
    assert!(main.retiring, "the Release deadline passed unquiescent");
    assert_eq!(main.uncertain, 1, "the second Release's receipt expired");
    assert!(
        main.outbox.is_empty(),
        "a retiring L sends no worker command"
    );
    assert_eq!(main.stops.len(), 1, "retirement is not restarted");
    // the delayed Accepted lands now; L still holds that Release
    for callback in delayed {
        h.sink.emit(callback);
    }
    assert_eq!(main.consume(&h.sink).len(), 1);
    // H's Stop, before the worker's next release-deadline check
    main.deliver_stops(&mut h.session, &h.sink);
    h.session.handle(Inbound::Internal(Internal::TakeoverDone {
        gen,
        outcome: Err(TakeoverError::Superseded),
    }));
    assert!(matches!(
        main.consume_dropped(&h.sink).as_slice(),
        [WorkerCallback::Drained { .. }]
    ));
    assert!(!main
        .seen
        .iter()
        .chain(main.dropped.iter())
        .any(|c| matches!(c, WorkerCallback::Released { .. })));
    main.tick(60_000);
    assert!(main.outbox.is_empty() && main.stops.is_empty());
}

/// sb-fix review OWN-4, r2 review OWN-R2-2: a takeover publishes its slot
/// `Exited` before its outcome is sent — on the normal return and on unwind —
/// so the next (deferred) takeover the consumer starts on that outcome can
/// never have its HTTP wait overwritten. The slot is read at the moment of
/// sending (the report's test hook), which rejects the old unwind order.
#[test]
fn a_takeover_publishes_its_slot_exited_before_it_hands_over_its_outcome() {
    use crate::managed::{start_with, CancelToken, TaskPhase};
    let agent = start_with(CancelToken::new(), |ctx| {
        while !ctx.idle_until(Instant::now() + Duration::from_secs(300)) {}
        Ok(())
    });
    for panics in [false, true] {
        let (tx, rx) = mpsc::channel();
        let (seen_tx, seen) = mpsc::channel();
        let mut report = TakeoverReport::new(tx, 7);
        let shared = agent.shared.clone();
        report.before_send = Some(Box::new(move || {
            let slot = shared.tasks[Task::Takeover as usize].snapshot(shared.started);
            let _ = seen_tx.send(slot.phase);
        }));
        agent
            .shared
            .spawn_owned(Task::Takeover, move |ctx| {
                takeover_body(ctx, report, move |_| {
                    assert!(!panics, "takeover panicked");
                    Ok(outcome())
                })
            })
            .unwrap();
        assert_eq!(
            seen.recv_timeout(Duration::from_secs(5)).unwrap(),
            TaskPhase::Exited,
            "panics={panics}"
        );
        let Internal::TakeoverDone { gen, outcome } =
            rx.recv_timeout(Duration::from_secs(5)).unwrap();
        assert_eq!(gen, 7);
        assert_eq!(outcome.is_ok(), !panics);
    }
}

/// SBC2-P-2: an owner failure seen by a RequestStatus answered before the
/// tick closes the gate from the same reading that makes the Status FAILED —
/// the gate is already closed when the callback is emitted.
#[test]
fn an_owner_failure_seen_by_a_request_status_closes_before_the_failed_status() {
    struct Probe {
        inner: Sink,
        owner: Arc<OwnerState>,
        admitted_at_emit: Mutex<Vec<bool>>,
    }
    impl CallbackSink for Probe {
        fn emit(&self, callback: WorkerCallback) {
            self.admitted_at_emit
                .lock()
                .unwrap()
                .push(self.owner.admission.load(Ordering::Relaxed));
            self.inner.emit(callback);
        }
    }
    let mut h = harness(Preparedness::Prepared, true);
    let probe = Arc::new(Probe {
        inner: Sink::default(),
        owner: h.owner.clone(),
        admitted_at_emit: Mutex::default(),
    });
    let probe_dyn: Arc<dyn CallbackSink> = probe.clone();
    let clock_dyn: Arc<dyn BootClock> = h.clock.clone();
    h.session = Session::new(
        config(),
        probe_dyn,
        clock_dyn,
        Box::new(FakeOwner(h.owner.clone())),
    );
    let execution: Arc<dyn Execution> = h.execution.clone();
    h.session.install_execution(execution);
    let mut main = Main::new();
    main.send(&mut h.session, &probe.inner, start);
    main.send(&mut h.session, &probe.inner, |request| {
        WorkerCommand::Activate {
            request,
            intent: intent(1),
        }
    });
    let g = grant(1, 1, 100, EvidenceGrade::Activation);
    main.send(&mut h.session, &probe.inner, |request| open(request, 1, g));
    let gen = *h.owner.spawned.lock().unwrap().last().unwrap();
    h.session.handle(Inbound::Internal(Internal::TakeoverDone {
        gen,
        outcome: Ok(outcome()),
    }));
    main.consume(&probe.inner);
    assert!(h.owner.admission.load(Ordering::Relaxed), "open");
    // an owned task fails; the consumer answers a RequestStatus before its tick
    h.owner.failed.store(true, Ordering::Relaxed);
    probe.admitted_at_emit.lock().unwrap().clear();
    let callbacks = main.send(&mut h.session, &probe.inner, |request| {
        WorkerCommand::RequestStatus { request }
    });
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Status {
            status: WorkerStatus {
                phase: WorkerPhase::Failed,
                open_grant: None,
                ..
            },
            ..
        }]
    ));
    assert_eq!(
        *probe.admitted_at_emit.lock().unwrap(),
        vec![false],
        "closed at emission"
    );
    assert_eq!(*h.execution.closed.lock().unwrap(), vec![false]);
}

/// SBC-P-2: one preparedness reading builds a Status and decides the local
/// close. Here preparedness flips right after the tick's first read: the
/// FAILED Status that follows is still preceded by the close. And a
/// RequestStatus answered after a flip, before any tick, closes first too.
#[test]
fn a_failed_status_is_never_sent_beside_an_open_gate_whatever_changed_between_reads() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    open_session(&mut h, &mut main);
    h.owner.flip_after.store(1, Ordering::Relaxed);
    h.session.tick();
    match main.consume(&h.sink).as_slice() {
        [WorkerCallback::Status { status, .. }] => {
            assert_eq!(status.phase, WorkerPhase::Failed);
            assert!(status.open_grant.is_none());
        }
        other => panic!("{other:?}"),
    }
    assert!(!h.owner.admission.load(Ordering::Relaxed));
    assert_eq!(*h.execution.closed.lock().unwrap(), vec![false]);

    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    open_session(&mut h, &mut main);
    h.owner.set(Preparedness::NotReady);
    let callbacks = main.send(&mut h.session, &h.sink, |request| {
        WorkerCommand::RequestStatus { request }
    });
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Status {
            status: WorkerStatus {
                phase: WorkerPhase::Failed,
                open_grant: None,
                ..
            },
            ..
        }]
    ));
    assert!(!h.owner.admission.load(Ordering::Relaxed));
    assert_eq!(*h.execution.closed.lock().unwrap(), vec![false]);
}

/// S9: Activate and Open are refused while a Release waits; a second Release
/// never overwrites the first — quiescence answers both, in arrival order,
/// and L accepts both (each is still in its map).
#[test]
fn nothing_opens_while_releasing_and_every_waiting_release_is_answered() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    open_session(&mut h, &mut main);
    let first = main.next_request;
    main.send(&mut h.session, &h.sink, release(1, 10_000));
    let callbacks = main.send(&mut h.session, &h.sink, |request| WorkerCommand::Activate {
        request,
        intent: intent(2),
    });
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::NotReady,
            ..
        }]
    ));
    let g = grant(1, 2, 100, EvidenceGrade::Activation);
    let callbacks = main.send(&mut h.session, &h.sink, |request| open(request, 2, g));
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Failed {
            failure: WorkerFailure::NotReady,
            ..
        }]
    ));
    assert_eq!(h.owner.spawned.lock().unwrap().len(), 1, "no takeover");
    let second = main.next_request;
    main.send(&mut h.session, &h.sink, release(1, 10_000));
    h.execution.quiescent.store(true, Ordering::Relaxed);
    h.session.tick();
    let callbacks = main.consume(&h.sink);
    let released: Vec<u64> = callbacks
        .iter()
        .filter(|c| matches!(c, WorkerCallback::Released { .. }))
        .map(|c| c.stamp().request.request_id)
        .collect();
    assert_eq!(released, vec![first, second]);
    assert_eq!(h.session.status().phase, WorkerPhase::Quiescent);
    main.assert_receipts_clear();
}

/// Step B review F2: a reopen after a Close does not start its takeover while
/// the superseded one is still in flight; it starts when that one reports,
/// and opens normally.
#[test]
fn a_reopen_waits_for_the_superseded_takeover_before_starting_its_own() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    let stale = reopen_in_flight(&mut h, &mut main);
    // Close(HEALTH_FAILURE) supersedes T1 while its write is in flight
    main.send(&mut h.session, &h.sink, |request| {
        close(request, 4, CloseReason::HealthFailure, None)
    });
    let g = grant(1, 3, 100, EvidenceGrade::Activation);
    let callbacks = main.send(&mut h.session, &h.sink, |request| open(request, 5, g));
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::Accepted {
            command: WorkerCommandKind::SetAdmission,
            ..
        }]
    ));
    assert_eq!(h.owner.spawned.lock().unwrap().len(), 2, "T2 deferred");
    assert_eq!(h.session.status().phase, WorkerPhase::Opening);
    assert!(h.session.snapshot().takeover_pending);
    // T1 reports (superseded): now T2 starts
    h.session.handle(Inbound::Internal(Internal::TakeoverDone {
        gen: stale,
        outcome: Err(TakeoverError::Superseded),
    }));
    assert!(
        main.consume(&h.sink).is_empty(),
        "the stale outcome is ignored"
    );
    let spawned = h.owner.spawned.lock().unwrap().clone();
    assert_eq!(spawned.len(), 3, "T2 started after T1 reported");
    let fresh = *spawned.last().unwrap();
    assert_ne!(fresh, stale);
    h.session.handle(Inbound::Internal(Internal::TakeoverDone {
        gen: fresh,
        outcome: Ok(outcome()),
    }));
    let callbacks = main.consume(&h.sink);
    assert!(matches!(
        callbacks.as_slice(),
        [WorkerCallback::AdmissionApplied {
            decision: AdmissionDecision::Open(_),
            ..
        }]
    ));
    assert_eq!(h.session.status().phase, WorkerPhase::Open);
    main.assert_receipts_clear();
}

/// Step B review F3(a): while a superseded task is in flight the new takeover
/// is deferred (F2), so its spawn cannot fail and uncount the live one — a
/// Release still waits for the task really in flight; and a takeover whose
/// task could not start is completed without touching the count.
#[test]
fn a_refused_takeover_spawn_is_not_counted_and_a_deferred_one_never_hides_one_in_flight() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    let stale = reopen_in_flight(&mut h, &mut main);
    main.send(&mut h.session, &h.sink, |request| {
        close(request, 4, CloseReason::HealthFailure, None)
    });
    h.owner.spawn_ok.store(false, Ordering::Relaxed);
    let g = grant(1, 3, 100, EvidenceGrade::Activation);
    let (_, dropped) = main.send_split(&mut h.session, &h.sink, |request| open(request, 5, g));
    assert!(dropped.is_empty());
    // the deferred T2 has not been handed over yet: nothing failed so far
    assert_eq!(h.session.status().phase, WorkerPhase::Opening);
    h.execution.quiescent.store(true, Ordering::Relaxed);
    main.send(&mut h.session, &h.sink, release(1, 10_000));
    assert_eq!(
        h.session.status().phase,
        WorkerPhase::Releasing,
        "T1 is still in flight: no Released"
    );
    h.session.handle(Inbound::Internal(Internal::TakeoverDone {
        gen: stale,
        outcome: Err(TakeoverError::Superseded),
    }));
    assert!(matches!(
        main.consume(&h.sink).as_slice(),
        [
            WorkerCallback::Released { .. },
            WorkerCallback::Status { .. }
        ]
    ));
    // and a spawn refusal with nothing in flight fails that Open at once
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    main.send(&mut h.session, &h.sink, start);
    main.send(&mut h.session, &h.sink, |request| WorkerCommand::Activate {
        request,
        intent: intent(1),
    });
    h.owner.spawn_ok.store(false, Ordering::Relaxed);
    h.execution.quiescent.store(true, Ordering::Relaxed);
    let g = grant(1, 1, 100, EvidenceGrade::Activation);
    let callbacks = main.send(&mut h.session, &h.sink, |request| open(request, 1, g));
    assert!(matches!(
        callbacks.as_slice(),
        [
            WorkerCallback::Accepted { .. },
            WorkerCallback::Failed {
                failure: WorkerFailure::TakeoverFailed,
                ..
            },
            WorkerCallback::Status { .. }
        ]
    ));
    main.send(&mut h.session, &h.sink, release(1, 10_000));
    assert_eq!(h.session.status().phase, WorkerPhase::Quiescent);
}

/// Step B review F3(b): the takeover's outcome guard posts exactly one
/// `TakeoverDone` whether the body sends, is dropped unrun — a refused spawn,
/// or a thread that starts after the cancel (the real `spawn_task` wrapper) —
/// or unwinds.
#[test]
fn the_takeover_outcome_guard_reports_exactly_once_however_the_body_ends() {
    use crate::managed::{spawn_task, start_with, CancelToken};
    let count = |rx: &Receiver<Internal>, gen: u64| {
        let mut n = 0;
        while let Ok(Internal::TakeoverDone { gen: got, outcome }) = rx.try_recv() {
            assert_eq!(got, gen);
            assert!(outcome.is_err());
            n += 1;
        }
        n
    };
    let (tx, rx) = mpsc::channel();
    // sent: one, and the drop adds nothing
    TakeoverReport::new(tx.clone(), 1).send(Err(TakeoverError::Superseded));
    assert_eq!(count(&rx, 1), 1);
    // dropped unrun
    drop(TakeoverReport::new(tx.clone(), 2));
    assert_eq!(count(&rx, 2), 1);
    // unwound
    let report = TakeoverReport::new(tx.clone(), 3);
    let unwound = std::panic::catch_unwind(std::panic::AssertUnwindSafe(move || {
        let _report = report;
        panic!("takeover panicked");
    }));
    assert!(unwound.is_err());
    assert_eq!(count(&rx, 3), 1);
    // through the owner: a cancelled owner refuses the spawn, and a thread
    // started after the cancel drops the body unrun — one report each
    let agent = start_with(CancelToken::new(), |ctx| {
        while !ctx.idle_until(Instant::now() + Duration::from_secs(300)) {}
        Ok(())
    });
    agent.cancel();
    let report = TakeoverReport::new(tx.clone(), 4);
    assert!(agent
        .shared
        .spawn_owned(Task::Takeover, move |_| {
            report.send(Ok(outcome()));
            Ok(())
        })
        .is_err());
    assert_eq!(count(&rx, 4), 1);
    let report = TakeoverReport::new(tx, 5);
    spawn_task(agent.shared.clone(), Task::Takeover, move |_| {
        report.send(Ok(outcome()));
        Ok(())
    })
    .unwrap()
    .join()
    .unwrap();
    assert_eq!(count(&rx, 5), 1, "the body never ran; the guard reported");
}

/// Step B review F4/SB-P-2 and sb-fix review OWN-1/SBC-P-3: a Close queued
/// before a takeover outcome was posted is taken first — also when both
/// arrive between the consumer's first (empty) command poll and its outcome
/// poll; the hook runs exactly there. FIFO holds among commands, and the
/// stashed outcome follows.
#[test]
fn a_queued_close_is_taken_before_a_takeover_outcome_posted_after_it() {
    let wait = Duration::from_millis(1);
    let outcome = || Internal::TakeoverDone {
        gen: 1,
        outcome: Ok(outcome()),
    };
    let kind = |next: Option<Inbound>| match next {
        Some(Inbound::Command(WorkerCommand::RequestStatus { .. })) => "status",
        Some(Inbound::Command(WorkerCommand::SetAdmission { .. })) => "close",
        Some(Inbound::Internal(_)) => "outcome",
        Some(Inbound::Command(_)) => "other",
        None => "none",
    };
    let mut main = Main::new();
    // (a) all three already queued
    let (commands_tx, commands) = mpsc::sync_channel(8);
    let (internal_tx, internal) = mpsc::channel();
    let ordinary = Arc::new(AtomicUsize::new(1));
    let mut inbox = Inbox::new(commands, ordinary.clone(), internal);
    commands_tx
        .send(WorkerCommand::RequestStatus {
            request: main.request(),
        })
        .unwrap();
    commands_tx
        .send(close(
            main.request(),
            2,
            CloseReason::OwnershipLost,
            Some(1),
        ))
        .unwrap();
    internal_tx.send(outcome()).unwrap();
    let kinds: Vec<&str> = (0..4).map(|_| kind(inbox.next(wait))).collect();
    assert_eq!(kinds, vec!["status", "close", "outcome", "none"]);
    assert_eq!(
        ordinary.load(Ordering::Relaxed),
        0,
        "the status was uncounted"
    );
    // (b) the queues are empty at the first poll; main queues a Close and the
    // takeover posts its outcome right after it, between the two polls
    let closing = close(main.request(), 3, CloseReason::OwnershipLost, Some(1));
    let mut window = Some((closing, outcome()));
    let first = inbox.next_with(wait, &mut || {
        if let Some((closing, done)) = window.take() {
            commands_tx.send(closing).unwrap();
            internal_tx.send(done).unwrap();
        }
    });
    assert_eq!(kind(first), "close", "the earlier Close wins the race");
    assert_eq!(
        kind(inbox.next(wait)),
        "outcome",
        "the stashed outcome follows"
    );
    assert_eq!(kind(inbox.next(wait)), "none");
}

/// Step B review F9 (strengthened per the sb-fix review): the ordinary bound
/// and the count hold with concurrent producers AND a concurrent consumer.
/// The old offer (check, send, then increment) let the consumer's decrement
/// run before the producer's increment — the count wrapped to `usize::MAX`
/// and every producer then saw a spurious `PortFull` on a near-empty queue —
/// and let two producers both pass the check at 63. A monitor samples the
/// count throughout; the reservation-first offer never lets it leave
/// `0..=INBOUND_CAPACITY`. (A stress test: the old code fails it whenever
/// the window is hit, which the monitor makes likely, not certain.)
#[test]
fn the_port_never_overshoots_its_ordinary_bound_under_concurrent_producers() {
    let (tx, rx) = mpsc::sync_channel(INBOUND_CAPACITY + CONTROL_CAPACITY);
    let port = CommandPort {
        commands: tx,
        ordinary: Arc::new(AtomicUsize::new(0)),
        refused: Arc::new(AtomicU64::new(0)),
    };
    let (producers, per) = (4usize, 5_000usize);
    let done = Arc::new(AtomicUsize::new(0));
    let barrier = Arc::new(std::sync::Barrier::new(producers + 2));
    let worst = Arc::new(AtomicUsize::new(0));
    let monitor = {
        let (count, done, barrier, worst) = (
            port.ordinary.clone(),
            done.clone(),
            barrier.clone(),
            worst.clone(),
        );
        std::thread::spawn(move || {
            barrier.wait();
            while done.load(Ordering::Acquire) <= producers {
                worst.fetch_max(count.load(Ordering::Acquire), Ordering::Relaxed);
            }
        })
    };
    let consumer = {
        let (ordinary, done, barrier) = (port.ordinary.clone(), done.clone(), barrier.clone());
        std::thread::spawn(move || {
            let mut inbox = Inbox::new(rx, ordinary, mpsc::channel().1);
            barrier.wait();
            let mut taken = 0usize;
            loop {
                // read "all producers finished" BEFORE polling: every send they
                // made happens-before that read, so an empty poll after it means
                // the queue is drained (read after the poll, a last command sent
                // between the poll and the producers' finish would be missed)
                let finished = done.load(Ordering::Acquire) == producers;
                match inbox.next(Duration::ZERO) {
                    Some(_) => taken += 1,
                    None if finished => break,
                    None => {}
                }
            }
            done.fetch_add(1, Ordering::Release); // releases the monitor
            taken
        })
    };
    let accepted: usize = (0..producers)
        .map(|p| {
            let (port, barrier, done) = (port.clone(), barrier.clone(), done.clone());
            std::thread::spawn(move || {
                barrier.wait();
                let accepted = (0..per)
                    .filter(|n| {
                        let request = WorkerRequest {
                            request_id: (p * per + n + 1) as u64,
                            session_id: "s1".into(),
                            lease: lease(),
                        };
                        port.offer(WorkerCommand::RequestStatus { request }).is_ok()
                    })
                    .count();
                done.fetch_add(1, Ordering::Release);
                accepted
            })
        })
        .collect::<Vec<_>>()
        .into_iter()
        .map(|t| t.join().unwrap())
        .sum();
    let taken = consumer.join().unwrap();
    monitor.join().unwrap();
    assert!(
        worst.load(Ordering::Relaxed) <= INBOUND_CAPACITY,
        "the count left 0..=64: {}",
        worst.load(Ordering::Relaxed)
    );
    assert_eq!(
        taken, accepted,
        "every accepted command reached the consumer"
    );
    assert_eq!(
        port.ordinary.load(Ordering::Acquire),
        0,
        "the count is back to 0"
    );
    assert_eq!(
        port.refused.load(Ordering::Relaxed) as usize,
        producers * per - accepted
    );
    // quiescent bound: exactly 64 ordinary fit, then only the 8 control
    let (tx, rx) = mpsc::sync_channel(INBOUND_CAPACITY + CONTROL_CAPACITY);
    let port = CommandPort {
        commands: tx,
        ordinary: Arc::new(AtomicUsize::new(0)),
        refused: Arc::new(AtomicU64::new(0)),
    };
    let mut main = Main::new();
    for _ in 0..INBOUND_CAPACITY {
        port.offer(WorkerCommand::RequestStatus {
            request: main.request(),
        })
        .unwrap();
    }
    assert_eq!(
        port.offer(WorkerCommand::RequestStatus {
            request: main.request(),
        }),
        Err(PortFull)
    );
    for _ in 0..CONTROL_CAPACITY {
        assert!(port
            .offer(close(main.request(), 1, CloseReason::SyncLoss, None))
            .is_ok());
    }
    assert!(port
        .offer(close(main.request(), 1, CloseReason::SyncLoss, None))
        .is_err());
    let mut inbox = Inbox::new(rx, port.ordinary.clone(), mpsc::channel().1);
    assert!(inbox.next(Duration::ZERO).is_some());
    assert_eq!(port.ordinary.load(Ordering::Acquire), INBOUND_CAPACITY - 1);
}

/// S7: readiness lost during the takeover (an owner failure here) means no
/// AppliedOpen; S6: admission rides the session word and the consumer's exit
/// clears it; S6c: an activation Open on an expired-but-unticked grant closes
/// the old gate before its takeover.
#[test]
fn admission_follows_readiness_and_the_session_word() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    let gen = reopen_in_flight(&mut h, &mut main);
    h.owner.failed.store(true, Ordering::Relaxed);
    h.session.handle(Inbound::Internal(Internal::TakeoverDone {
        gen,
        outcome: Ok(outcome()),
    }));
    let callbacks = main.consume(&h.sink);
    assert!(
        !callbacks
            .iter()
            .any(|c| matches!(c, WorkerCallback::AdmissionApplied { .. })),
        "{callbacks:?}"
    );
    assert!(callbacks.iter().any(|c| matches!(
        c,
        WorkerCallback::Failed {
            failure: WorkerFailure::NotReady,
            ..
        }
    )));
    assert!(!h.owner.admission.load(Ordering::Relaxed));

    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    open_session(&mut h, &mut main);
    assert!(h.session.snapshot().admission);
    assert!(h.owner.admission.load(Ordering::Relaxed), "published");
    // the consumer's exit (a drain or Drop of the owner) clears it (S6b)
    h.session.retire();
    assert!(!h.owner.admission.load(Ordering::Relaxed));

    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    open_session(&mut h, &mut main);
    // the grant expired, but no tick has seen it: a new activation Open
    h.clock.0.store(30_100, Ordering::Relaxed);
    let g = grant(1, 2, 30_100, EvidenceGrade::Activation);
    main.send(&mut h.session, &h.sink, |request| open(request, 2, g));
    assert_eq!(h.session.status().phase, WorkerPhase::Opening);
    assert!(!h.session.snapshot().admission, "the old gate is closed");
    assert_eq!(*h.execution.closed.lock().unwrap(), vec![false]);
}

/// N2b1-P-6a: installing the execution service after the settled Status
/// announces again, so L sees `ready`.
#[test]
fn installing_execution_after_the_settled_status_announces_ready() {
    let mut h = harness(Preparedness::Prepared, false);
    let mut main = Main::new();
    let callbacks = main.send(&mut h.session, &h.sink, start);
    assert!(matches!(
        callbacks.as_slice(),
        [
            WorkerCallback::Accepted { .. },
            WorkerCallback::Status {
                status: WorkerStatus { ready: false, .. },
                ..
            }
        ]
    ));
    let execution: Arc<dyn Execution> = h.execution.clone();
    h.session.install_execution(execution);
    assert!(matches!(
        main.consume(&h.sink).as_slice(),
        [WorkerCallback::Status {
            status: WorkerStatus { ready: true, .. },
            ..
        }]
    ));
}

/// SB-P-5: a tombstoned standing built-in is setup-level, like drift: the
/// Open fails, the worker reports NeedsSetup and is no longer ready, so L
/// (which opens only a ready worker) stops reopening it.
#[test]
fn a_tombstoned_standing_record_is_needs_setup_and_the_worker_stops_being_ready() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    main.send(&mut h.session, &h.sink, start);
    main.send(&mut h.session, &h.sink, |request| WorkerCommand::Activate {
        request,
        intent: intent(1),
    });
    let g = grant(1, 1, 100, EvidenceGrade::Activation);
    main.send(&mut h.session, &h.sink, |request| open(request, 1, g));
    let gen = *h.owner.spawned.lock().unwrap().last().unwrap();
    h.session.handle(Inbound::Internal(Internal::TakeoverDone {
        gen,
        outcome: Err(TakeoverError::Tombstoned("rollup".into())),
    }));
    let callbacks = main.consume(&h.sink);
    assert!(h.owner.needs_setup.load(Ordering::Relaxed));
    match callbacks.as_slice() {
        [WorkerCallback::Failed {
            failure: WorkerFailure::TakeoverFailed,
            ..
        }, WorkerCallback::Status { status, .. }] => {
            assert_eq!(status.phase, WorkerPhase::Failed);
            assert!(!status.ready);
            assert_eq!(status.failure, Some(WorkerFailure::NotReady));
        }
        other => panic!("{other:?}"),
    }
}

/// SB-P-6: NeedsSetup while Open (N2b-2's periodic skill check) closes the
/// gate locally before the FAILED Status goes out; that Status carries no grant.
#[test]
fn a_needs_setup_flip_while_open_closes_before_the_failed_status() {
    let mut h = harness(Preparedness::Prepared, true);
    let mut main = Main::new();
    open_session(&mut h, &mut main);
    h.owner.set(Preparedness::NotReady);
    h.session.tick();
    assert!(!h.owner.admission.load(Ordering::Relaxed));
    assert_eq!(*h.execution.closed.lock().unwrap(), vec![false]);
    match main.consume(&h.sink).as_slice() {
        [WorkerCallback::Status { status, .. }] => {
            assert_eq!(status.phase, WorkerPhase::Failed);
            assert!(status.open_grant.is_none());
        }
        other => panic!("{other:?}"),
    }
    h.session.tick();
    assert!(main.consume(&h.sink).is_empty(), "announced once");
}

/// N2b-1 S8b: a command the full port refuses is counted in `health()`. The
/// consumer is held inside its first callback (the Start's `Accepted`), so the
/// queue state is exact: 64 ordinary commands fit, the 65th is refused.
#[test]
fn a_refused_command_is_counted_in_health_through_the_real_port() {
    use crate::managed::{start_with, CancelToken, DrainOutcome};
    struct Held {
        entered: Mutex<Option<Sender<()>>>,
        release: Mutex<Option<Receiver<()>>>,
        inner: Sink,
    }
    impl CallbackSink for Held {
        fn emit(&self, callback: WorkerCallback) {
            if let Some(entered) = self.entered.lock().unwrap().take() {
                entered.send(()).unwrap();
                let release = self.release.lock().unwrap().take().unwrap();
                release.recv_timeout(Duration::from_secs(5)).unwrap();
            }
            self.inner.emit(callback);
        }
    }
    let mut agent = start_with(CancelToken::new(), |ctx| {
        while !ctx.idle_until(Instant::now() + Duration::from_secs(300)) {}
        Ok(())
    });
    let (entered_tx, entered) = mpsc::channel();
    let (release, release_rx) = mpsc::channel();
    let sink = Arc::new(Held {
        entered: Mutex::new(Some(entered_tx)),
        release: Mutex::new(Some(release_rx)),
        inner: Sink::default(),
    });
    let sink_dyn: Arc<dyn CallbackSink> = sink.clone();
    let clock: Arc<dyn BootClock> = Arc::new(Clock(AtomicU64::new(100)));
    let port = spawn_consumer(&agent.shared, config(), sink_dyn, clock).unwrap();
    let mut main = Main::new();
    let request = main.request();
    port.offer(start(request)).unwrap();
    // the consumer took the Start and is held in its Accepted
    entered.recv_timeout(Duration::from_secs(5)).unwrap();
    for _ in 0..INBOUND_CAPACITY {
        port.offer(WorkerCommand::RequestStatus {
            request: main.request(),
        })
        .unwrap();
    }
    assert_eq!(
        port.offer(WorkerCommand::RequestStatus {
            request: main.request(),
        }),
        Err(PortFull)
    );
    assert_eq!(agent.health().commands_refused, 1);
    release.send(()).unwrap();
    assert_eq!(
        agent.drain_until(Instant::now() + Duration::from_secs(5)),
        DrainOutcome::Drained
    );
}
