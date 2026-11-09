//! The owned run lanes (ADR-034 §2/§4): three lane workers on the 8 MiB run
//! stack, one epoch driver that ticks only while a store is live, RAII run
//! registration, and the bounded mailbox/backlog. Who feeds a lane (the
//! due/event/chat sources and the pending queue) is the execution service's
//! job (N2b); the lanes themselves never consult a trigger, a chat or the
//! registry. Every accepted job ends in exactly one `LaneReport`.
use super::admission::{
    Lane, LanePool, StorePermit, MAX_BACKLOG, MAX_MAILBOX_BYTES, MAX_MAILBOX_ITEMS,
};
use super::{ManagedFailure, Shared, Task, TaskContext, WaitClass};
use crate::broker::{SharedActivity, SharedMailbox};
use crate::triggers::{LiveRun, RunResult};
use serde_json::{json, Value};
use std::collections::{BTreeMap, VecDeque};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// The existing run wall cap (ADR-034 §2 "the existing 1200 s run wall cap").
#[allow(dead_code)] // consumed by the execution service (N2b)
pub(crate) const RUN_WALL_S: f64 = 1200.0;
#[allow(dead_code)] // consumed by the execution service (N2b)
const IDLE_WAKE: Duration = Duration::from_secs(60);

/// Epochs advance only while a store exists (ADR-034 §2 "Idle epochs stop
/// when no store is live"): the driver parks on the pool and wakes when the
/// first store attaches its permit. It keeps ticking through cancellation
/// until the last store is gone, so an interrupted guest can still trap —
/// also when its thread first runs after the cancel (`Task::Epoch` is the one
/// task whose body runs anyway, sb-fix review OWN-3). Spawn it before the
/// lanes: a refused spawn (already cancelled) then means no store attached.
#[allow(dead_code)] // consumed by the execution service (N2b)
pub(crate) struct EpochDriver;
#[allow(dead_code)] // consumed by the execution service (N2b)
impl EpochDriver {
    pub(crate) fn spawn(
        shared: &Arc<Shared>,
        engine: wasmtime::Engine,
    ) -> Result<(), ManagedFailure> {
        shared.spawn_owned(Task::Epoch, move |ctx| Self::body(ctx, engine))
    }

    pub(crate) fn body(ctx: TaskContext, engine: wasmtime::Engine) -> Result<(), ManagedFailure> {
        let pool = ctx.shared.pool.clone();
        loop {
            ctx.wait(WaitClass::Idle, None);
            if pool.wait_live(IDLE_WAKE) == 0 {
                // exit only when nothing can still attach: a taken permit
                // may become a store after the cancel (R2-C1; the pool
                // refuses the attach once closed, so this is belt and braces)
                if ctx.shared.cancel.is_cancelled() && pool.occupied_lanes() == 0 {
                    return Ok(());
                }
                continue;
            }
            ctx.running();
            while pool.live_stores() > 0 {
                std::thread::sleep(Duration::from_millis(crate::runner::EPOCH_TICK_MS));
                engine.increment_epoch();
            }
        }
    }
}

/// One admitted unit of work, fully bound: the source already consumed its
/// once shot / cron occurrence / seen mark under the permit (ADR-034 §2).
#[allow(dead_code)] // consumed by the execution service (N2b)
pub(crate) struct LaneJob {
    pub(crate) lane: Lane,
    pub(crate) work_id: String,
    pub(crate) run_id: String,
    pub(crate) spec: String,
    pub(crate) args: Value,
    pub(crate) trigger: Option<String>,
    pub(crate) mailbox: Mailbox,
    pub(crate) interrupt: Arc<AtomicBool>,
}

/// Why a lane produced no run result. `Cancelled` jobs never started: their
/// input is intact and the service may restore it to its source.
#[derive(Clone, Debug, PartialEq, Eq)]
#[allow(dead_code)] // consumed by the execution service (N2b)
pub(crate) enum LaneFailure {
    Cancelled,
    RunPath(String),
    TraceUnpersisted(String),
}

#[allow(dead_code)] // consumed by the execution service (N2b)
pub(crate) struct LaneOutcome {
    pub(crate) result: Result<RunResult, LaneFailure>,
    /// the ADR-023 run summary the trace store produced, when the trace landed
    pub(crate) summary: Option<Value>,
}

#[allow(dead_code)] // consumed by the execution service (N2b)
pub(crate) struct LaneReport {
    pub(crate) lane: Lane,
    pub(crate) work_id: String,
    pub(crate) run_id: String,
    pub(crate) outcome: LaneOutcome,
    /// Undrained mailbox items the leftover sink refused (its backlog was
    /// full): counted as a capacity fault and handed back here, never dropped.
    pub(crate) refused_input: Vec<Value>,
}

/// What a lane does with a job: the real runner is `PreparedService`
/// (`runner::run_program_with` under the permit); tests use fakes.
#[allow(dead_code)] // consumed by the execution service (N2b)
pub(crate) trait LaneRunner: Send + Sync {
    fn run(&self, job: &LaneJob, activity: SharedActivity, permit: StorePermit) -> LaneOutcome;
}

#[allow(dead_code)] // consumed by the execution service (N2b)
pub(crate) type LiveRuns = Arc<Mutex<BTreeMap<String, LiveRun>>>;

#[derive(Debug, PartialEq, Eq)]
#[allow(dead_code)] // consumed by the execution service (N2b)
pub(crate) enum MailboxError {
    /// Host-buffer bound (ADR-034 §2): a visible capacity fault, never a drop.
    Full,
    /// The run is over: the item was not accepted; the caller re-dispatches it.
    NotLive,
    /// `inject_if`'s admission check failed under the live lock (a close since
    /// the caller decided to inject): not accepted, nothing pushed.
    Refused,
}

/// The bounded run mailbox (ADR-034 §2): at most `MAX_MAILBOX_ITEMS` items and
/// `MAX_MAILBOX_BYTES` of serialized text. The broker's `mailbox.drain` sees
/// the inner queue. Host code never pushes into a `Mailbox` directly — a clone
/// kept past the run's teardown would accept an item nothing drains (R2-C6):
/// every host-side push goes through `inject`/`soft_break_live`, under the
/// live-run lock. The desktop helpers that push a raw `SharedMailbox`
/// (`triggers::break_live_run`, `Watcher`) are not used on the managed path.
#[derive(Clone, Default)]
#[allow(dead_code)] // consumed by the execution service (N2b)
pub(crate) struct Mailbox(SharedMailbox);
#[allow(dead_code)] // consumed by the execution service (N2b)
impl Mailbox {
    fn push(&self, item: Value) -> Result<(), MailboxError> {
        let mut queue = self.0.lock().unwrap_or_else(|e| e.into_inner());
        let bytes: usize =
            queue.iter().map(|v| v.to_string().len()).sum::<usize>() + item.to_string().len();
        if queue.len() >= MAX_MAILBOX_ITEMS || bytes > MAX_MAILBOX_BYTES {
            return Err(MailboxError::Full);
        }
        queue.push_back(item);
        Ok(())
    }
    /// A soft break (ADR-005 §3): the guest drains it between turns.
    fn soft_break(&self) -> Result<(), MailboxError> {
        self.push(json!({"kind": "break", "hard": false}))
    }
    pub(crate) fn len(&self) -> usize {
        self.0.lock().unwrap_or_else(|e| e.into_inner()).len()
    }
    pub(crate) fn has_break(&self) -> bool {
        self.0
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .any(|m| m["kind"] == "break")
    }
    fn drain(&self) -> Vec<Value> {
        self.0
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .drain(..)
            .collect()
    }
    /// The broker's view, for `prepare_store` only.
    pub(crate) fn raw(&self) -> SharedMailbox {
        self.0.clone()
    }
}

/// Push into a live run's mailbox under the live-run lock, so a run that is
/// tearing down cannot accept an item it would then lose (ADR-034 §2); a full
/// mailbox is counted as a capacity fault here, where the bound is enforced
/// (R2-C8).
fn push_live(
    shared: &Shared,
    run_id: &str,
    push: impl FnOnce(&Mailbox) -> Result<(), MailboxError>,
) -> Result<(), MailboxError> {
    let runs = shared.live_runs.lock().unwrap_or_else(|e| e.into_inner());
    let run = runs.get(run_id).ok_or(MailboxError::NotLive)?;
    let result = push(&Mailbox(run.mailbox.clone()));
    if result == Err(MailboxError::Full) {
        shared.capacity_fault();
    }
    result
}

/// A new message for a live run (ADR-034 §2: no second store).
#[allow(dead_code)] // consumed by the execution service (N2b)
pub(crate) fn inject(shared: &Shared, run_id: &str, item: Value) -> Result<(), MailboxError> {
    push_live(shared, run_id, |mailbox| mailbox.push(item))
}

/// `inject`, admitted by `admit` evaluated under the live-run lock (N2b-2):
/// the chat source passes its watch-connection check (the close generation
/// it captured still current, admission open), so a close that lands between
/// the source's decision and the push refuses the item instead of handing it
/// to a run the close already stood down (design §3.2; R4 desktop parity: no
/// grant or gate check beyond that). `admit` must not take the live lock.
#[allow(dead_code)] // consumed by the execution service's chat source (N2b-2)
pub(crate) fn inject_if(
    shared: &Shared,
    run_id: &str,
    item: Value,
    admit: impl FnOnce() -> bool,
) -> Result<(), MailboxError> {
    push_live(shared, run_id, |mailbox| {
        if !admit() {
            return Err(MailboxError::Refused);
        }
        mailbox.push(item)
    })
}

/// A soft break into a live run (ADR-005 §3), under the same lock as `inject`.
#[allow(dead_code)] // consumed by the execution service (N2b)
pub(crate) fn soft_break_live(shared: &Shared, run_id: &str) -> Result<(), MailboxError> {
    push_live(shared, run_id, Mailbox::soft_break)
}

/// Deferred chat inputs (ADR-009 §8) with the host-buffer bound: saturation is
/// a visible capacity fault, counted on the pool where it is refused (R2-C8),
/// never a silent drop.
#[allow(dead_code)] // consumed by the execution service (N2b)
pub(crate) struct Backlog {
    items: VecDeque<Value>,
    pool: LanePool,
}
#[derive(Debug, PartialEq, Eq)]
#[allow(dead_code)] // consumed by the execution service (N2b)
pub(crate) struct BacklogFull;
#[allow(dead_code)] // consumed by the execution service (N2b)
impl Backlog {
    pub(crate) fn new(pool: &LanePool) -> Self {
        Backlog {
            items: VecDeque::new(),
            pool: pool.clone(),
        }
    }
    pub(crate) fn push(&mut self, input: Value) -> Result<(), BacklogFull> {
        if self.items.len() >= MAX_BACKLOG {
            self.pool.capacity_fault();
            return Err(BacklogFull);
        }
        self.items.push_back(input);
        Ok(())
    }
    pub(crate) fn drain(&mut self) -> Vec<Value> {
        self.items.drain(..).collect()
    }
    pub(crate) fn len(&self) -> usize {
        self.items.len()
    }
}

/// Where a finished run's undrained mailbox items go (the execution service's
/// backlog in N2b). The sink may refuse (its backlog is bounded, ADR-034 §2):
/// the refused items come back, are counted as a capacity fault and ride the
/// `LaneReport` — input is never silently lost (R2-C3).
#[allow(dead_code)] // consumed by the execution service (N2b)
pub(crate) type LeftoverSink = Arc<dyn Fn(Vec<Value>) -> Result<(), Vec<Value>> + Send + Sync>;

/// Undrained input of a job that leaves a lane — a finished or interrupted run,
/// or a job cancelled in its inbox — goes to the leftover sink; what the sink
/// refuses comes back (a capacity fault) for the report. Soft break markers
/// are control, not input, and are dropped. During unwind no service code is
/// called: the job carrying the input is counted orphaned, once, by its
/// owner (`Completion` or `InboxGuard`); the owner retires anyway.
fn hand_on(shared: &Shared, leftovers: &LeftoverSink, items: Vec<Value>) -> Vec<Value> {
    let items: Vec<Value> = items
        .into_iter()
        .filter(|item| item["kind"] != "break")
        .collect();
    if items.is_empty() || std::thread::panicking() {
        return Vec::new();
    }
    match leftovers(items) {
        Ok(()) => Vec::new(),
        Err(refused) => {
            shared.capacity_fault();
            refused
        }
    }
}

/// RAII run registration: the live-run entry (what presence and `/break`
/// resolve against) exists exactly while the run does, including unwind.
/// Registration happens under the live lock and compares the revocation epoch
/// of the job's permit with the pool's: a revocation that ran after the permit
/// was taken — before the offer, or between the take and this registration —
/// still reaches the run (R2-C5, Step B review F1).
#[allow(dead_code)] // consumed by the execution service (N2b)
pub(crate) struct RunSlot {
    shared: Arc<Shared>,
    run_id: String,
    mailbox: Mailbox,
    leftovers: LeftoverSink,
    released: bool,
    pub(crate) activity: SharedActivity,
}
#[allow(dead_code)] // consumed by the execution service (N2b)
impl RunSlot {
    pub(crate) fn register(
        shared: &Arc<Shared>,
        job: &LaneJob,
        accepted_epoch: u64,
        leftovers: LeftoverSink,
    ) -> Self {
        let activity: SharedActivity = Default::default();
        let title = job.args["userText"]
            .as_str()
            .map(str::trim)
            .filter(|t| !t.is_empty())
            .map(|t| t.chars().take(80).collect::<String>())
            .unwrap_or_else(|| job.spec.clone());
        let mut runs = shared.live_runs.lock().unwrap_or_else(|e| e.into_inner());
        runs.insert(
            job.run_id.clone(),
            LiveRun {
                mailbox: job.mailbox.raw(),
                interrupt: job.interrupt.clone(),
                stamp: json!({"id": job.run_id, "title": title, "startedAt": super::service::epoch_s()}),
                activity: activity.clone(),
            },
        );
        // under the live lock: an interrupt_all that ran before this insert
        // bumped the epoch first, one that runs after it finds the entry
        if shared.cancel.is_cancelled() || shared.pool.revocation_epoch() > accepted_epoch {
            job.interrupt.store(true, Ordering::Relaxed);
        }
        RunSlot {
            shared: shared.clone(),
            run_id: job.run_id.clone(),
            mailbox: job.mailbox.clone(),
            leftovers,
            released: false,
            activity,
        }
    }

    /// Teardown: the run leaves the live set and its undrained input goes to
    /// the sink; what the sink refuses comes back (a capacity fault). Soft
    /// break markers are control, not input, and are dropped.
    fn teardown(&mut self) -> Vec<Value> {
        // Teardown and `inject` share the live lock: an item lands either in
        // the live mailbox before this, and is handed on below, or is refused.
        let leftovers = {
            let mut runs = self
                .shared
                .live_runs
                .lock()
                .unwrap_or_else(|e| e.into_inner());
            runs.remove(&self.run_id);
            self.mailbox.drain()
        };
        hand_on(&self.shared, &self.leftovers, leftovers)
    }

    /// The normal exit: the refused leftovers for the report.
    pub(crate) fn release(mut self) -> Vec<Value> {
        self.released = true;
        self.teardown()
    }
}
impl Drop for RunSlot {
    fn drop(&mut self) {
        if !self.released {
            let _ = self.teardown();
        }
    }
}

#[derive(Default)]
#[allow(dead_code)] // consumed by the execution service (N2b)
struct Inbox {
    /// The job and its permit; `closed` once the worker has exited, so an
    /// offer after that is refused rather than stranded.
    slot: Mutex<(Option<(LaneJob, StorePermit)>, bool)>,
}

/// The three owned lane workers.
#[allow(dead_code)] // consumed by the execution service (N2b)
pub(crate) struct Lanes {
    inboxes: [Arc<Inbox>; 3],
    pool: LanePool,
}

/// One lane worker's body before it runs: it owns the lane's `InboxGuard`, so
/// a closure dropped unrun (its thread started after a cancel) still closes
/// the inbox and reports a job accepted in the meantime (SB-I-2).
pub(crate) type LaneWork = Box<dyn FnOnce(TaskContext) -> Result<(), ManagedFailure> + Send>;

#[allow(dead_code)] // consumed by the execution service (N2b)
impl Lanes {
    pub(crate) fn spawn(
        shared: &Arc<Shared>,
        runner: Arc<dyn LaneRunner>,
        report: Arc<dyn Fn(LaneReport) + Send + Sync>,
        leftovers: LeftoverSink,
    ) -> Result<Self, ManagedFailure> {
        let (lanes, workers) = Self::build(shared, runner, report, leftovers);
        for (lane, work) in workers {
            shared.spawn_owned(Task::lane(lane), work)?;
        }
        Ok(lanes)
    }

    /// The lanes and their three worker bodies, each already holding its
    /// inbox guard.
    fn build(
        shared: &Arc<Shared>,
        runner: Arc<dyn LaneRunner>,
        report: Arc<dyn Fn(LaneReport) + Send + Sync>,
        leftovers: LeftoverSink,
    ) -> (Self, Vec<(Lane, LaneWork)>) {
        let lanes = Lanes {
            inboxes: Default::default(),
            pool: shared.pool.clone(),
        };
        let workers = Lane::ALL
            .into_iter()
            .map(|lane| {
                let inbox = lanes.inboxes[lane as usize].clone();
                let guard = InboxGuard {
                    shared: shared.clone(),
                    inbox: inbox.clone(),
                    lane,
                    report: report.clone(),
                    leftovers: leftovers.clone(),
                };
                let (runner, report, leftovers) =
                    (runner.clone(), report.clone(), leftovers.clone());
                let work: LaneWork =
                    Box::new(move |ctx| worker(ctx, lane, guard, inbox, runner, report, leftovers));
                (lane, work)
            })
            .collect();
        (lanes, workers)
    }

    /// Hand an admitted job to its lane. The permit proves the lane is free,
    /// so the inbox is empty unless the worker is still reporting the previous
    /// job or has exited; then the job and permit come back to the caller
    /// intact. An accepted job is registered as pending with its permit's
    /// revocation epoch, so a cancel — or a revocation since its `acquire` —
    /// reaches its interrupt flag before it starts.
    pub(crate) fn offer(
        &self,
        job: LaneJob,
        permit: StorePermit,
        shared: &Shared,
    ) -> Result<(), Box<(LaneJob, StorePermit)>> {
        if permit.lane() != job.lane {
            return Err(Box::new((job, permit)));
        }
        let inbox = &self.inboxes[job.lane as usize];
        let mut slot = inbox.slot.lock().unwrap_or_else(|e| e.into_inner());
        if slot.1 || slot.0.is_some() || self.pool.is_closed() {
            return Err(Box::new((job, permit)));
        }
        // keyed by run id (a work id may recur across lanes, R2-C5), with the
        // permit's epoch from `acquire` — never the epoch at offer, which a
        // revocation between the two would already have bumped (F1)
        let epoch = permit.epoch();
        shared
            .pending_interrupts
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(job.run_id.clone(), (job.interrupt.clone(), epoch));
        slot.0 = Some((job, permit));
        drop(slot);
        self.pool.notify();
        Ok(())
    }
}

#[allow(dead_code)] // consumed by the execution service (N2b)
fn cancelled_report(lane: Lane, job: LaneJob, refused_input: Vec<Value>) -> LaneReport {
    LaneReport {
        lane,
        work_id: job.work_id,
        run_id: job.run_id,
        outcome: LaneOutcome {
            result: Err(LaneFailure::Cancelled),
            summary: None,
        },
        refused_input,
    }
}

/// Closes the lane's inbox on every worker exit — return, cancel, unwind, and
/// a worker body dropped before it ever ran (SB-I-2) — so a queued job is
/// reported `Cancelled` (outside the inbox lock) or, while panicking, counted
/// as orphaned rather than stranded with no report (R2-C2). The job's mailbox
/// (pre-filled from the backlog) goes to the leftover sink and what the sink
/// refuses rides the report, as on every other path (sb-fix review OWN-2).
#[allow(dead_code)] // consumed by the execution service (N2b)
struct InboxGuard {
    shared: Arc<Shared>,
    inbox: Arc<Inbox>,
    lane: Lane,
    report: Arc<dyn Fn(LaneReport) + Send + Sync>,
    leftovers: LeftoverSink,
}
impl Drop for InboxGuard {
    fn drop(&mut self) {
        let taken = {
            let mut slot = self.inbox.slot.lock().unwrap_or_else(|e| e.into_inner());
            slot.1 = true;
            slot.0.take()
        };
        if let Some((job, permit)) = taken {
            drop(permit);
            unregister_pending(&self.shared, &job.run_id);
            // counted orphaned once if the sink panics, or while unwinding
            let mut completion = Completion {
                shared: &self.shared,
                reported: false,
            };
            let refused = hand_on(&self.shared, &self.leftovers, job.mailbox.drain());
            if !std::thread::panicking() {
                completion.reported = true;
                (self.report)(cancelled_report(self.lane, job, refused));
            }
            drop(completion);
            self.shared.pool.notify();
        }
    }
}

/// A job's completion (sb-fix review OWN-5, r2 review OWN-R2-3): every path
/// reports the job; one that unwinds before its report is handed over — a
/// panicking run, a panicking leftover sink — is counted orphaned here,
/// exactly once. The guard is disarmed at the report handoff, so a job is
/// never both reported and orphaned. Accepted residual: a report callback
/// that panics before it delivers leaves the job uncounted; the callback is
/// ours, and its panic fails the owner (ADR-034 §2).
struct Completion<'a> {
    shared: &'a Shared,
    reported: bool,
}
impl Drop for Completion<'_> {
    fn drop(&mut self) {
        if !self.reported {
            self.shared.orphaned();
        }
    }
}

#[allow(dead_code)] // consumed by the execution service (N2b)
fn worker(
    ctx: TaskContext,
    lane: Lane,
    _guard: InboxGuard,
    inbox: Arc<Inbox>,
    runner: Arc<dyn LaneRunner>,
    report: Arc<dyn Fn(LaneReport) + Send + Sync>,
    leftovers: LeftoverSink,
) -> Result<(), ManagedFailure> {
    let pool = ctx.shared.pool.clone();
    loop {
        ctx.wait(WaitClass::Idle, None);
        pool.wait_until(IDLE_WAKE, || {
            ctx.shared.cancel.is_cancelled()
                || inbox
                    .slot
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .0
                    .is_some()
        });
        // Exit and take happen under the inbox lock, the same lock `offer`
        // uses: a job either reaches the worker or is refused, never stranded.
        // On cancel the guard closes the inbox and reports the queued job.
        let taken = {
            let mut slot = inbox.slot.lock().unwrap_or_else(|e| e.into_inner());
            if ctx.shared.cancel.is_cancelled() {
                return Ok(());
            }
            slot.0.take()
        };
        let Some((job, permit)) = taken else {
            continue;
        };
        let mut completion = Completion {
            shared: &ctx.shared,
            reported: false,
        };
        ctx.running();
        // the take→register gap (N2b-2 design §7 test 3, E21): a test can
        // hold the job here, out of the inbox and not yet a live run
        #[cfg(test)]
        test_hooks::after_take(&job.work_id);
        let accepted_epoch = take_pending(&ctx.shared, &job.run_id);
        let slot = RunSlot::register(&ctx.shared, &job, accepted_epoch, leftovers.clone());
        if job.interrupt.load(Ordering::Relaxed) {
            // interrupted before it started (a Stop or a revoking stand-down
            // since its permit was taken): never run, input intact (R2-C7);
            // whatever reached its mailbox meanwhile goes to the leftover sink,
            // and what the sink refuses rides the report (F6)
            let refused_input = slot.release();
            drop(permit);
            completion.reported = true;
            report(cancelled_report(lane, job, refused_input));
            pool.notify();
            continue;
        }
        let outcome = runner.run(&job, slot.activity.clone(), permit);
        if let Ok(result) = &outcome.result {
            // ADR-034 §2 "health counts it": every run result passes here
            ctx.shared
                .refused_programs
                .fetch_add(result.refused_programs.len() as u64, Ordering::Relaxed);
        }
        let refused_input = slot.release();
        completion.reported = true;
        report(LaneReport {
            lane,
            work_id: job.work_id,
            run_id: job.run_id,
            outcome,
            refused_input,
        });
        pool.notify();
    }
}

/// The job's accepted epoch, removed from the pending set (the run slot
/// takes over from here); a job never offered through `offer` reads as epoch 0.
#[allow(dead_code)] // consumed by the execution service (N2b)
fn take_pending(shared: &Shared, run_id: &str) -> u64 {
    shared
        .pending_interrupts
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .remove(run_id)
        .map_or(0, |(_, epoch)| epoch)
}

#[allow(dead_code)] // consumed by the execution service (N2b)
fn unregister_pending(shared: &Shared, run_id: &str) {
    take_pending(shared, run_id);
}

/// Test-only hooks into the lane worker (N2b-2): keyed by work id, so a
/// hook registered by one test never fires in another.
#[cfg(test)]
pub(crate) mod test_hooks {
    use std::collections::BTreeMap;
    use std::sync::Mutex;

    type Hook = Box<dyn FnOnce() + Send>;
    static AFTER_TAKE: Mutex<BTreeMap<String, Hook>> = Mutex::new(BTreeMap::new());

    /// Run `hook` on the lane worker right after it takes the job named
    /// `work_id` from its inbox, before the job registers as a live run.
    pub(crate) fn hold_after_take(work_id: &str, hook: impl FnOnce() + Send + 'static) {
        AFTER_TAKE
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(work_id.to_owned(), Box::new(hook));
    }

    pub(super) fn after_take(work_id: &str) {
        let hook = AFTER_TAKE
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(work_id);
        if let Some(hook) = hook {
            hook();
        }
    }
}

#[cfg(test)]
mod tests;
