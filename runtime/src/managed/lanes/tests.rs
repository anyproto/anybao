use super::*;
use crate::managed::admission::{Admission, GateCause, PendingQueue, PendingWork};
use crate::managed::{
    spawn_task, start_with, CancelToken, DrainOutcome, ManagedFailure, ManagedPhase, TaskPhase,
};
use std::sync::mpsc::{self, Receiver, Sender};
use std::time::Instant;

const BUDGET: Duration = Duration::from_secs(5);

fn wait_for(mut predicate: impl FnMut() -> bool) {
    let deadline = Instant::now() + BUDGET;
    while !predicate() {
        assert!(Instant::now() < deadline, "condition did not become true");
        std::thread::sleep(Duration::from_millis(2));
    }
}

fn job(lane: Lane, id: &str) -> LaneJob {
    LaneJob {
        lane,
        work_id: id.into(),
        run_id: format!("run_{id}"),
        spec: "agent:fixture@v1".into(),
        args: json!({"userText": "  hello  "}),
        trigger: None,
        mailbox: Default::default(),
        interrupt: Arc::new(AtomicBool::new(false)),
    }
}

/// A runner that blocks until released or interrupted, panics on demand, and
/// attaches the permit as a real store would. `attached` hears the work id
/// once the permit is attached — after the run registered and the store
/// counted — so a test synchronizes on that event, never on polling.
struct FakeRunner {
    release: Mutex<Option<Receiver<()>>>,
    panic_on: Option<&'static str>,
    attached: Option<Mutex<Sender<String>>>,
}
impl FakeRunner {
    fn new(release: Option<Receiver<()>>, panic_on: Option<&'static str>) -> Self {
        FakeRunner {
            release: Mutex::new(release),
            panic_on,
            attached: None,
        }
    }
    /// The same runner plus the attach signal.
    fn signalled(release: Option<Receiver<()>>) -> (Self, Receiver<String>) {
        let (tx, rx) = mpsc::channel();
        let mut runner = Self::new(release, None);
        runner.attached = Some(Mutex::new(tx));
        (runner, rx)
    }
}
impl LaneRunner for FakeRunner {
    fn run(
        &self,
        job: &LaneJob,
        _activity: SharedActivity,
        mut permit: StorePermit,
    ) -> LaneOutcome {
        assert_eq!(permit.lane(), job.lane);
        permit.attach().unwrap();
        // claim the release receiver BEFORE announcing the attach: a test that
        // offers the next job on this signal can never see that job's runner
        // take the receiver meant for this one (sb-fix review SB-FIX-I-2)
        let release = self.release.lock().unwrap().take();
        if let Some(attached) = &self.attached {
            let _ = attached.lock().unwrap().send(job.work_id.clone());
        }
        if self.panic_on == Some(job.work_id.as_str()) {
            panic!("fake run panicked");
        }
        let status = if let Some(release) = release {
            loop {
                if job.interrupt.load(Ordering::Relaxed) {
                    break "interrupted";
                }
                match release.recv_timeout(Duration::from_millis(2)) {
                    Ok(()) => break "ok",
                    Err(mpsc::RecvTimeoutError::Timeout) => continue,
                    Err(_) => break "ok",
                }
            }
        } else {
            "ok"
        };
        LaneOutcome {
            result: Ok(RunResult {
                status: status.into(),
                duration_ms: 1,
                trace_ref: Some(job.run_id.clone()),
                fuel: Some(1),
                error: None,
                missing_secrets: vec![],
                missing_programs: vec![],
                // an agent-authored program refused typed (`NotPinned`)
                refused_programs: if job.work_id.starts_with("authoring") {
                    vec![("ws".into(), "mine@v1".into())]
                } else {
                    vec![]
                },
                mutated: false,
            }),
            summary: Some(json!({"id": job.run_id})),
        }
    }
}

type Harness = (
    crate::managed::ManagedAgent,
    Lanes,
    Receiver<LaneReport>,
    Arc<Mutex<Vec<Value>>>,
);

fn harness(runner: FakeRunner) -> Harness {
    harness_with(runner, None, 64)
}

/// `report_gate`: the report callback blocks on it for the named work id (a
/// worker "still reporting"); `backlog`: how many leftovers the sink accepts.
fn harness_with(
    runner: FakeRunner,
    report_gate: Option<(&'static str, Receiver<()>)>,
    backlog: usize,
) -> Harness {
    let (lanes_tx, lanes_rx) = mpsc::channel();
    let leftovers: Arc<Mutex<Vec<Value>>> = Default::default();
    let sink = leftovers.clone();
    let gate = Mutex::new(report_gate);
    let agent = start_with(CancelToken::new(), move |ctx| {
        let (report_tx, report_rx) = mpsc::channel::<LaneReport>();
        let report = Arc::new(move |r: LaneReport| {
            if let Some((id, wait)) = gate.lock().unwrap().as_ref() {
                if r.work_id == *id {
                    let _ = wait.recv_timeout(BUDGET);
                }
            }
            let _ = report_tx.send(r);
        });
        let leftover: LeftoverSink = Arc::new(move |items: Vec<Value>| {
            let mut held = sink.lock().unwrap();
            let room = backlog.saturating_sub(held.len());
            let (taken, refused) = items.split_at(room.min(items.len()));
            held.extend(taken.iter().cloned());
            if refused.is_empty() {
                Ok(())
            } else {
                Err(refused.to_vec())
            }
        });
        let lanes = Lanes::spawn(&ctx.shared, Arc::new(runner), report, leftover)?;
        lanes_tx.send((lanes, report_rx)).unwrap();
        while !ctx.idle_until(Instant::now() + Duration::from_secs(300)) {}
        Ok(())
    });
    let (lanes, reports) = lanes_rx.recv_timeout(BUDGET).unwrap();
    // an AppliedOpen is in force: the policy gate admits
    agent.shared.pool.open_gate(GateCause::Policy);
    (agent, lanes, reports, leftovers)
}

fn permit(agent: &crate::managed::ManagedAgent, lane: Lane) -> StorePermit {
    match agent.shared.pool.acquire(lane) {
        Admission::Admitted(p) => p,
        Admission::CapacityDeferred => panic!("lane busy"),
    }
}

#[test]
fn lanes_run_admitted_jobs_register_live_runs_and_hand_leftover_input_back() {
    let (release_tx, release_rx): (Sender<()>, Receiver<()>) = mpsc::channel();
    let (runner, attached) = FakeRunner::signalled(Some(release_rx));
    let (mut agent, lanes, reports, leftovers) = harness(runner);
    let chat = job(Lane::Chat, "chat-1");
    let live = agent.shared.live_runs.clone();
    assert!(lanes
        .offer(chat, permit(&agent, Lane::Chat), &agent.shared)
        .is_ok());
    // SB-I-1: the run registered, then its permit attached (the real event)
    assert_eq!(attached.recv_timeout(BUDGET).unwrap(), "chat-1");
    let stamp = live.lock().unwrap()["run_chat-1"].stamp.clone();
    assert_eq!(stamp["title"], "hello");
    assert_eq!(agent.health().live_stores, 1);
    assert_eq!(agent.health().lanes[0].phase, TaskPhase::Running);
    // a message for the live run lands in its mailbox under the live lock
    inject(
        &agent.shared,
        "run_chat-1",
        json!({"kind": "inject", "text": "later"}),
    )
    .unwrap();
    assert!(matches!(
        agent.shared.pool.acquire(Lane::Chat),
        Admission::CapacityDeferred
    ));
    assert!(lanes
        .offer(
            job(Lane::Scheduled, "cron-1"),
            permit(&agent, Lane::Scheduled),
            &agent.shared
        )
        .is_ok());
    let first = reports.recv_timeout(BUDGET).unwrap();
    assert_eq!(first.work_id, "cron-1");
    assert_eq!(first.outcome.result.unwrap().status, "ok");
    release_tx.send(()).unwrap();
    let second = reports.recv_timeout(BUDGET).unwrap();
    assert_eq!(second.work_id, "chat-1");
    assert_eq!(second.outcome.summary, Some(json!({"id": "run_chat-1"})));
    wait_for(|| agent.health().live_stores == 0);
    assert!(live.lock().unwrap().is_empty());
    // the undrained item was handed back, never dropped; a late inject is refused typed
    assert_eq!(leftovers.lock().unwrap().len(), 1);
    assert_eq!(
        inject(&agent.shared, "run_chat-1", json!({"kind": "inject"})),
        Err(MailboxError::NotLive)
    );
    assert_eq!(agent.health().capacity_faults, 0, "NotLive is not a fault");
    assert_eq!(
        agent.drain_until(Instant::now() + BUDGET),
        DrainOutcome::Drained
    );
    assert!(agent.health().all_threads_joined);
}

#[test]
fn drain_interrupts_a_live_run_and_a_late_offer_is_refused_intact() {
    let (_release_tx, release_rx): (Sender<()>, Receiver<()>) = mpsc::channel();
    let (runner, attached) = FakeRunner::signalled(Some(release_rx));
    let (mut agent, lanes, reports, _) = harness(runner);
    let event = job(Lane::Event, "evt-1");
    let interrupt = event.interrupt.clone();
    assert!(lanes
        .offer(event, permit(&agent, Lane::Event), &agent.shared)
        .is_ok());
    // SB-I-1: wait for the attach itself, not the earlier registration
    assert_eq!(attached.recv_timeout(BUDGET).unwrap(), "evt-1");
    assert!(agent
        .shared
        .live_runs
        .lock()
        .unwrap()
        .contains_key("run_evt-1"));
    // the epoch driver is a separate owned thread; here the permit is attached by the fake
    assert_eq!(agent.health().live_stores, 1);
    // drain sets every live interrupt: the compute-bound guest stops, the run reports interrupted
    assert_eq!(
        agent.drain_until(Instant::now()),
        DrainOutcome::DeadlineExceeded
    );
    assert!(interrupt.load(Ordering::Relaxed));
    let report = reports.recv_timeout(BUDGET).unwrap();
    assert_eq!(report.outcome.result.unwrap().status, "interrupted");
    assert_eq!(
        agent.drain_until(Instant::now() + BUDGET),
        DrainOutcome::Drained
    );
    assert_eq!(agent.health().phase, ManagedPhase::Drained);
    // after the workers exited, an offer is refused with the job intact (never stranded)
    let late = job(Lane::Chat, "late");
    let permit = match LanePool::opened().acquire(Lane::Chat) {
        Admission::Admitted(p) => p,
        Admission::CapacityDeferred => unreachable!(),
    };
    let back = lanes.offer(late, permit, &agent.shared).unwrap_err();
    assert_eq!(back.0.work_id, "late");
}

#[test]
fn an_offer_after_cancel_is_refused_untouched_and_the_live_run_is_interrupted() {
    // hold the chat worker busy so a second chat job cannot run yet
    let (_release_tx, release_rx): (Sender<()>, Receiver<()>) = mpsc::channel();
    let (runner, attached) = FakeRunner::signalled(Some(release_rx));
    let (mut agent, lanes, reports, _) = harness(runner);
    let first = job(Lane::Scheduled, "first");
    assert!(lanes
        .offer(first, permit(&agent, Lane::Scheduled), &agent.shared)
        .is_ok());
    // wait for the store itself (after registration): a cancel before the
    // attach would refuse it, which is a different scenario (SB-I-1 pattern)
    assert_eq!(attached.recv_timeout(BUDGET).unwrap(), "first");
    // an event job waits in its inbox: pending, not yet a run
    let waiting = job(Lane::Event, "waiting");
    let waiting_flag = waiting.interrupt.clone();
    let permit_event = permit(&agent, Lane::Event);
    // simulate the worker being busy by never letting it see the slot before cancel:
    // cancel first (closes the pool, sets pending interrupts), then observe reports
    agent.cancel();
    assert!(
        lanes.offer(waiting, permit_event, &agent.shared).is_err(),
        "closed pool refuses"
    );
    assert!(
        !waiting_flag.load(Ordering::Relaxed),
        "a refused job is untouched"
    );
    let report = reports.recv_timeout(BUDGET).unwrap();
    assert_eq!(report.work_id, "first");
    assert_eq!(report.outcome.result.unwrap().status, "interrupted");
    assert_eq!(
        agent.drain_until(Instant::now() + BUDGET),
        DrainOutcome::Drained
    );
}

/// R2-C7 / N2a-r2 item 11: a job accepted while the worker is still reporting
/// the previous one, then cancelled, ends in exactly one typed `Cancelled`
/// report through the worker's own exit path — never run, never stranded.
#[test]
fn a_job_queued_behind_a_reporting_worker_and_cancelled_is_reported_cancelled_once() {
    let (release_tx, release_rx): (Sender<()>, Receiver<()>) = mpsc::channel();
    let (gate_tx, gate_rx): (Sender<()>, Receiver<()>) = mpsc::channel();
    let (mut agent, lanes, reports, leftovers) = harness_with(
        FakeRunner::new(Some(release_rx), None),
        Some(("first", gate_rx)),
        1,
    );
    let first = job(Lane::Chat, "first");
    assert!(lanes
        .offer(first, permit(&agent, Lane::Chat), &agent.shared)
        .is_ok());
    wait_for(|| {
        agent
            .shared
            .live_runs
            .lock()
            .unwrap()
            .contains_key("run_first")
    });
    // the run ends and its permit drops; the worker is now blocked in `report`
    release_tx.send(()).unwrap();
    wait_for(|| agent.shared.pool.occupied_lanes() == 0);
    // the next chat job, its mailbox pre-filled from the backlog, is accepted
    // into the inbox behind the reporting worker
    let second = job(Lane::Chat, "second");
    for n in 0..3 {
        second
            .mailbox
            .push(json!({"kind": "inject", "n": n}))
            .unwrap();
    }
    second.mailbox.soft_break().unwrap();
    let second_flag = second.interrupt.clone();
    assert!(lanes
        .offer(second, permit(&agent, Lane::Chat), &agent.shared)
        .is_ok());
    agent.cancel();
    assert!(
        second_flag.load(Ordering::Relaxed),
        "pending interrupts are set"
    );
    gate_tx.send(()).unwrap();
    let first_report = reports.recv_timeout(BUDGET).unwrap();
    assert_eq!(first_report.work_id, "first");
    let second_report = reports.recv_timeout(BUDGET).unwrap();
    assert_eq!(second_report.work_id, "second");
    // OWN-2: the inbox-cancelled job's input reached the sink (it took one)
    // and the refused rest rides the report; the break marker is not input
    assert_eq!(leftovers.lock().unwrap().len(), 1);
    assert_eq!(second_report.refused_input.len(), 2);
    assert!(second_report
        .refused_input
        .iter()
        .all(|i| i["kind"] == "inject"));
    assert_eq!(agent.health().capacity_faults, 1);
    assert_eq!(agent.health().orphaned_jobs, 0);
    assert_eq!(
        second_report.outcome.result.unwrap_err(),
        LaneFailure::Cancelled
    );
    assert!(
        reports.recv_timeout(Duration::from_millis(50)).is_err(),
        "exactly one report"
    );
    assert_eq!(
        agent.drain_until(Instant::now() + BUDGET),
        DrainOutcome::Drained
    );
    assert!(matches!(
        agent.shared.spawn_owned(Task::Epoch, |_| Ok(())),
        Err(ManagedFailure::Cancelled)
    ));
    assert!(
        agent.health().failures.is_empty(),
        "a refused spawn is not a fault"
    );
}

/// R2-C5: a revoking stand-down interrupts live and pending work and refuses
/// new permits without cancelling the generation; a reopen admits again and
/// the workers never spun.
#[test]
fn a_stand_down_interrupts_and_refuses_without_ending_the_generation_and_reopens() {
    let (_release_tx, release_rx): (Sender<()>, Receiver<()>) = mpsc::channel();
    let (mut agent, lanes, reports, _) = harness(FakeRunner::new(Some(release_rx), None));
    let first = job(Lane::Scheduled, "first");
    assert!(lanes
        .offer(first, permit(&agent, Lane::Scheduled), &agent.shared)
        .is_ok());
    wait_for(|| agent.health().live_stores == 1);
    agent.shared.stand_down(GateCause::Policy, true);
    assert!(!agent.health().lane_admission_open);
    assert!(matches!(
        agent.shared.pool.acquire(Lane::Chat),
        Admission::CapacityDeferred
    ));
    let report = reports.recv_timeout(BUDGET).unwrap();
    assert_eq!(report.outcome.result.unwrap().status, "interrupted");
    assert!(!agent.health().cancelled, "the generation lives on");
    let lanes_progress = agent.health().lanes[0].progress;
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!(
        agent.health().lanes[0].progress,
        lanes_progress,
        "no spinning worker"
    );
    agent.shared.reopen(GateCause::Policy);
    assert!(agent.health().lane_admission_open);
    assert!(lanes
        .offer(
            job(Lane::Event, "after"),
            permit(&agent, Lane::Event),
            &agent.shared
        )
        .is_ok());
    let report = reports.recv_timeout(BUDGET).unwrap();
    assert_eq!(report.work_id, "after");
    assert_eq!(report.outcome.result.unwrap().status, "ok");
    assert_eq!(
        agent.drain_until(Instant::now() + BUDGET),
        DrainOutcome::Drained
    );
}

/// R2-C1: a permit taken before Stop cannot become a store after it, and the
/// epoch driver outlives the cancel while a taken permit could still attach.
#[test]
fn a_permit_taken_before_stop_cannot_attach_after_it() {
    let mut agent = start_with(CancelToken::new(), |ctx| {
        let mut config = wasmtime::Config::new();
        config.epoch_interruption(true);
        EpochDriver::spawn(&ctx.shared, wasmtime::Engine::new(&config).unwrap())?;
        while !ctx.idle_until(Instant::now() + Duration::from_secs(300)) {}
        Ok(())
    });
    wait_for(|| agent.health().epoch.phase != TaskPhase::NotStarted);
    agent.shared.pool.open_gate(GateCause::Policy);
    let mut taken = permit(&agent, Lane::Scheduled);
    assert_eq!(
        agent.drain_until(Instant::now()),
        DrainOutcome::DeadlineExceeded
    );
    assert!(taken.attach().is_err(), "no store starts after Stop");
    assert_eq!(agent.health().live_stores, 0);
    assert_ne!(
        agent.health().epoch.phase,
        TaskPhase::Exited,
        "a permit is still occupied"
    );
    drop(taken);
    assert_eq!(
        agent.drain_until(Instant::now() + BUDGET),
        DrainOutcome::Drained
    );
}

/// R2-C3: leftovers the sink cannot hold come back on the report as a visible
/// capacity fault; break markers are never handed on as input.
#[test]
fn refused_leftovers_ride_the_report_as_a_capacity_fault() {
    let (release_tx, release_rx): (Sender<()>, Receiver<()>) = mpsc::channel();
    let (runner, attached) = FakeRunner::signalled(Some(release_rx));
    let (mut agent, lanes, reports, leftovers) = harness_with(runner, None, 2);
    let chat = job(Lane::Chat, "chat");
    assert!(lanes
        .offer(chat, permit(&agent, Lane::Chat), &agent.shared)
        .is_ok());
    assert_eq!(attached.recv_timeout(BUDGET).unwrap(), "chat");
    for n in 0..4 {
        inject(&agent.shared, "run_chat", json!({"kind": "inject", "n": n})).unwrap();
    }
    soft_break_live(&agent.shared, "run_chat").unwrap();
    release_tx.send(()).unwrap();
    let report = reports.recv_timeout(BUDGET).unwrap();
    assert_eq!(leftovers.lock().unwrap().len(), 2);
    assert_eq!(report.refused_input.len(), 2);
    assert!(report.refused_input.iter().all(|i| i["kind"] == "inject"));
    assert_eq!(agent.health().capacity_faults, 1);
    assert_eq!(
        agent.drain_until(Instant::now() + BUDGET),
        DrainOutcome::Drained
    );
}

#[test]
fn a_panicking_run_releases_its_slot_and_permit_and_retires_the_owner() {
    let (mut agent, lanes, reports, _) = harness(FakeRunner::new(None, Some("boom")));
    assert!(lanes
        .offer(
            job(Lane::Scheduled, "boom"),
            permit(&agent, Lane::Scheduled),
            &agent.shared
        )
        .is_ok());
    wait_for(|| agent.health().phase == ManagedPhase::Failed);
    assert!(agent
        .health()
        .failures
        .contains(&ManagedFailure::ExecutionPanicked));
    assert_eq!(
        agent.health().live_stores,
        0,
        "the permit dropped with the unwound store"
    );
    assert!(
        agent.shared.live_runs.lock().unwrap().is_empty(),
        "RunSlot released on unwind"
    );
    // OWN-5: the unwound job got no report, and is counted orphaned once
    assert!(reports.try_recv().is_err());
    assert_eq!(agent.health().orphaned_jobs, 1);
    assert_eq!(
        agent.drain_until(Instant::now() + BUDGET),
        DrainOutcome::Failed(vec![ManagedFailure::ExecutionPanicked])
    );
    assert!(agent.health().all_threads_joined);
}

#[test]
fn epoch_driver_is_silent_while_idle_ticks_for_attached_stores_and_outlives_cancel() {
    let mut config = wasmtime::Config::new();
    config.epoch_interruption(true);
    let engine = wasmtime::Engine::new(&config).unwrap();
    let mut agent = start_with(CancelToken::new(), {
        let engine = engine.clone();
        move |ctx| {
            EpochDriver::spawn(&ctx.shared, engine)?;
            while !ctx.idle_until(Instant::now() + Duration::from_secs(300)) {}
            Ok(())
        }
    });
    // the baseline is taken only once the body has published its idle wait
    // (the wrapper's Running is 1, the idle wait 2): startup progress is not
    // an idle wakeup (r2 review OWN-R2-4)
    wait_for(|| {
        let epoch = agent.health().epoch;
        epoch.phase == TaskPhase::Waiting && epoch.progress >= 2
    });
    let idle_before = agent.health().epoch.progress;
    std::thread::sleep(Duration::from_millis(150));
    assert_eq!(
        agent.health().epoch.progress,
        idle_before,
        "no idle wakeups"
    );
    // a permit alone (no store yet) does not wake the driver
    agent.shared.pool.open_gate(GateCause::Policy);
    let mut permit = permit(&agent, Lane::Chat);
    std::thread::sleep(Duration::from_millis(60));
    assert_eq!(agent.health().epoch.phase, TaskPhase::Waiting);
    assert_eq!(agent.health().live_stores, 0);
    permit.attach().unwrap();
    wait_for(|| agent.health().epoch.phase == TaskPhase::Running);
    assert_eq!(agent.health().live_stores, 1);
    // epochs advance: a store with deadline 1 traps once the engine has ticked
    #[cfg(feature = "compiler")]
    {
        let mut store = wasmtime::Store::new(&engine, ());
        store.set_epoch_deadline(1);
        let module =
            wasmtime::Module::new(&engine, "(module (func (export \"spin\") (loop br 0)))")
                .unwrap();
        let instance = wasmtime::Instance::new(&mut store, &module, &[]).unwrap();
        let spin = instance
            .get_typed_func::<(), ()>(&mut store, "spin")
            .unwrap();
        assert!(
            spin.call(&mut store, ()).is_err(),
            "the live epoch driver interrupts a spinning guest"
        );
    }
    // cancel while the store is live: the driver keeps ticking until it is gone
    assert_eq!(
        agent.drain_until(Instant::now()),
        DrainOutcome::DeadlineExceeded
    );
    std::thread::sleep(Duration::from_millis(30));
    assert_eq!(agent.health().epoch.phase, TaskPhase::Running);
    drop(permit);
    assert_eq!(
        agent.drain_until(Instant::now() + BUDGET),
        DrainOutcome::Drained
    );
}

#[test]
fn mailbox_and_backlog_are_bounded_and_saturation_is_a_typed_fault() {
    let pool = LanePool::opened();
    let mailbox = Mailbox::default();
    for i in 0..MAX_MAILBOX_ITEMS {
        mailbox.push(json!({"kind": "inject", "n": i})).unwrap();
    }
    assert_eq!(
        mailbox.push(json!({"kind": "inject"})),
        Err(MailboxError::Full)
    );
    assert_eq!(mailbox.len(), MAX_MAILBOX_ITEMS);
    let big = Mailbox::default();
    let text = "x".repeat(MAX_MAILBOX_BYTES);
    assert_eq!(big.push(json!({"text": text})), Err(MailboxError::Full));
    assert!(big
        .push(json!({"text": "x".repeat(MAX_MAILBOX_BYTES / 2)}))
        .is_ok());
    assert_eq!(
        big.push(json!({"text": "x".repeat(MAX_MAILBOX_BYTES / 2)})),
        Err(MailboxError::Full)
    );
    assert!(big.soft_break().is_err() || big.has_break());
    let mut backlog = Backlog::new(&pool);
    for i in 0..MAX_BACKLOG {
        backlog.push(json!({"n": i})).unwrap();
    }
    assert_eq!(pool.capacity_faults(), 0);
    assert_eq!(backlog.push(json!({})), Err(BacklogFull));
    assert_eq!(
        pool.capacity_faults(),
        1,
        "counted where it is refused (R2-C8)"
    );
    assert_eq!(backlog.drain().len(), MAX_BACKLOG);
    assert_eq!(backlog.len(), 0);
}

#[test]
fn a_descriptor_is_bound_to_a_lane_job_only_under_its_permit() {
    // the queue hands the permit first; the caller binds its source afterwards
    let pool = LanePool::opened();
    let mut queue = PendingQueue::default();
    queue
        .push(
            PendingWork {
                id: "chat".into(),
                lane: Lane::Chat,
                heartbeat: false,
            },
            &pool,
        )
        .unwrap();
    let (work, permit) = queue.admit(&pool).unwrap();
    assert_eq!(permit.lane(), work.lane);
    let bound = job(work.lane, &work.id);
    assert_eq!(bound.work_id, "chat");
    drop(permit);
    assert_eq!(pool.occupied_lanes(), 0);
}

#[cfg(feature = "desktop")]
#[test]
fn the_real_kernel_instantiates_and_runs_under_a_lane_permit_with_the_table_bound() {
    let cage = crate::runner::Cage::embedded().unwrap();
    let pool = LanePool::opened();
    let permit = match pool.acquire(Lane::Scheduled) {
        Admission::Admitted(p) => p,
        Admission::CapacityDeferred => unreachable!(),
    };
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(
        dir.path().join("smoke@v1.py"),
        "def main(args):\n    return 7\n",
    )
    .unwrap();
    let broker = crate::broker::Broker::new(
        crate::trace::TraceWriter::new(json!({"id": "lane-smoke"})),
        Default::default(),
        Default::default(),
        Some(dir.path().to_path_buf()),
        crate::routes::Classifier::new(None),
    );
    let outcome = crate::runner::run_program_with(
        &cage,
        broker,
        "smoke@v1",
        &json!({}),
        Default::default(),
        Arc::new(AtomicBool::new(false)),
        120.0,
        Some(permit),
    )
    .unwrap();
    assert_eq!(outcome.status, "ok", "{:?}", outcome.error);
    assert_eq!(outcome.value, json!(7));
    assert_eq!(pool.live_stores(), 0, "the permit dropped with the store");
    assert_eq!(pool.occupied_lanes(), 0);
}

/// Step B review F1: a job whose permit was taken before a revoking
/// stand-down and offered after it is interrupted, reported `Cancelled` once
/// and never reaches the runner — the epoch is the permit's, from `acquire`.
#[test]
fn a_job_admitted_before_a_revoking_stand_down_and_offered_after_it_never_runs() {
    let (runner, attached) = FakeRunner::signalled(None);
    let (mut agent, lanes, reports, _) = harness(runner);
    // the dispatcher holds the permit and consumes its source under it...
    let taken = permit(&agent, Lane::Chat);
    // ...while the consumer applies Close(OWNERSHIP_LOST)
    agent.shared.stand_down(GateCause::Policy, true);
    let admitted = job(Lane::Chat, "admitted");
    let flag = admitted.interrupt.clone();
    assert!(
        lanes.offer(admitted, taken, &agent.shared).is_ok(),
        "the pool is not closed: the offer is accepted"
    );
    let report = reports.recv_timeout(BUDGET).unwrap();
    assert_eq!(report.work_id, "admitted");
    assert_eq!(report.outcome.result.unwrap_err(), LaneFailure::Cancelled);
    assert!(report.refused_input.is_empty());
    assert!(flag.load(Ordering::Relaxed));
    assert!(attached.try_recv().is_err(), "the runner was never called");
    assert!(
        reports.recv_timeout(Duration::from_millis(50)).is_err(),
        "exactly one report"
    );
    // a permit taken after the reopen runs normally
    agent.shared.reopen(GateCause::Policy);
    assert!(lanes
        .offer(
            job(Lane::Chat, "later"),
            permit(&agent, Lane::Chat),
            &agent.shared
        )
        .is_ok());
    assert_eq!(attached.recv_timeout(BUDGET).unwrap(), "later");
    assert_eq!(reports.recv_timeout(BUDGET).unwrap().work_id, "later");
    assert_eq!(
        agent.drain_until(Instant::now() + BUDGET),
        DrainOutcome::Drained
    );
}

/// Step B review F6 / SB-I-9: input that reached an interrupted-before-start
/// job's mailbox goes to the leftover sink, and what the sink refuses rides
/// the `Cancelled` report as a capacity fault — never dropped.
#[test]
fn an_interrupted_before_start_job_hands_its_input_on_and_refused_input_rides_the_report() {
    let (runner, attached) = FakeRunner::signalled(None);
    let (mut agent, lanes, reports, leftovers) = harness_with(runner, None, 1);
    let taken = permit(&agent, Lane::Chat);
    let prefilled = job(Lane::Chat, "prefilled");
    // the dispatcher pre-filled the mailbox from the backlog
    for n in 0..3 {
        prefilled
            .mailbox
            .push(json!({"kind": "inject", "n": n}))
            .unwrap();
    }
    agent.shared.stand_down(GateCause::Policy, true);
    assert!(lanes.offer(prefilled, taken, &agent.shared).is_ok());
    let report = reports.recv_timeout(BUDGET).unwrap();
    assert_eq!(report.outcome.result.unwrap_err(), LaneFailure::Cancelled);
    assert_eq!(leftovers.lock().unwrap().len(), 1, "the sink took one");
    assert_eq!(report.refused_input.len(), 2, "the rest rides the report");
    assert_eq!(agent.health().capacity_faults, 1);
    assert!(attached.try_recv().is_err());
    assert_eq!(
        agent.drain_until(Instant::now() + BUDGET),
        DrainOutcome::Drained
    );
}

/// SB-I-2: a lane worker whose thread starts after a cancel never runs its
/// body, yet the job accepted into its inbox in the meantime is reported
/// `Cancelled` exactly once and its permit is released — the inbox guard is
/// owned by the body, so dropping the body unrun runs it. Driven through the
/// real `spawn_task` wrapper: offer, then cancel, then start the threads.
#[test]
fn a_lane_worker_that_never_starts_still_reports_its_accepted_job_and_frees_the_lane() {
    let mut agent = start_with(CancelToken::new(), |ctx| {
        while !ctx.idle_until(Instant::now() + Duration::from_secs(300)) {}
        Ok(())
    });
    let (report_tx, reports) = mpsc::channel::<LaneReport>();
    let report = Arc::new(move |r: LaneReport| {
        let _ = report_tx.send(r);
    });
    let (runner, attached) = FakeRunner::signalled(None);
    // a sink whose backlog is full: it refuses everything it is handed
    let leftover: LeftoverSink = Arc::new(|items: Vec<Value>| Err(items));
    let (lanes, workers) = Lanes::build(&agent.shared, Arc::new(runner), report, leftover);
    agent.shared.pool.open_gate(GateCause::Policy);
    let accepted = job(Lane::Scheduled, "accepted");
    for n in 0..2 {
        accepted
            .mailbox
            .push(json!({"kind": "inject", "n": n}))
            .unwrap();
    }
    assert!(lanes
        .offer(accepted, permit(&agent, Lane::Scheduled), &agent.shared)
        .is_ok());
    assert_eq!(agent.shared.pool.occupied_lanes(), 1);
    // Stop lands before any worker thread runs its first instruction
    agent.cancel();
    let handles: Vec<_> = workers
        .into_iter()
        .map(|(lane, work)| spawn_task(agent.shared.clone(), Task::lane(lane), work).unwrap())
        .collect();
    for handle in handles {
        handle.join().unwrap();
    }
    let report = reports.recv_timeout(BUDGET).unwrap();
    assert_eq!(report.work_id, "accepted");
    // OWN-2/SB-FIX-I-1: its input went to the sink, which refused all of it
    assert_eq!(report.refused_input.len(), 2, "never dropped");
    assert_eq!(agent.health().capacity_faults, 1);
    assert_eq!(report.outcome.result.unwrap_err(), LaneFailure::Cancelled);
    assert!(reports.try_recv().is_err(), "exactly one report");
    assert!(attached.try_recv().is_err(), "the body never ran");
    assert_eq!(
        agent.shared.pool.occupied_lanes(),
        0,
        "the permit is released: an epoch driver could exit"
    );
    // the inbox is closed: a late offer comes back intact
    let late = lanes
        .offer(
            job(Lane::Event, "late"),
            match LanePool::opened().acquire(Lane::Event) {
                Admission::Admitted(p) => p,
                Admission::CapacityDeferred => unreachable!(),
            },
            &agent.shared,
        )
        .unwrap_err();
    assert_eq!(late.0.work_id, "late");
    assert_eq!(
        agent.drain_until(Instant::now() + BUDGET),
        DrainOutcome::Drained
    );
}

/// R2-C6/R2-C8: a full live mailbox is counted where it is refused; the soft
/// break goes through the same live-locked path. N-1: a run result's refused
/// programs reach `health().refused_programs`.
#[test]
fn live_mailbox_saturation_is_counted_and_refused_programs_reach_health() {
    let (release_tx, release_rx): (Sender<()>, Receiver<()>) = mpsc::channel();
    let (runner, attached) = FakeRunner::signalled(Some(release_rx));
    let (mut agent, lanes, reports, _) = harness(runner);
    assert!(lanes
        .offer(
            job(Lane::Chat, "authoring"),
            permit(&agent, Lane::Chat),
            &agent.shared
        )
        .is_ok());
    assert_eq!(attached.recv_timeout(BUDGET).unwrap(), "authoring");
    for n in 0..MAX_MAILBOX_ITEMS {
        inject(&agent.shared, "run_authoring", json!({"n": n})).unwrap();
    }
    assert_eq!(
        inject(&agent.shared, "run_authoring", json!({"n": "over"})),
        Err(MailboxError::Full)
    );
    assert_eq!(
        soft_break_live(&agent.shared, "run_authoring"),
        Err(MailboxError::Full)
    );
    assert_eq!(agent.health().capacity_faults, 2);
    release_tx.send(()).unwrap();
    let report = reports.recv_timeout(BUDGET).unwrap();
    assert_eq!(report.outcome.result.unwrap().refused_programs.len(), 1);
    assert_eq!(agent.health().refused_programs, 1);
    assert_eq!(
        agent.drain_until(Instant::now() + BUDGET),
        DrainOutcome::Drained
    );
}

/// sb-fix review OWN-3: an epoch driver whose thread first runs after the
/// cancel still ticks while a store attached before the cancel is live, and
/// exits once it drops. The delay is real: the driver body is started through
/// the real `spawn_task` wrapper only after the store attached and the owner
/// was cancelled. (The wait is bounded and on a monotonic condition: the
/// driver stays in its tick loop until the permit drops.)
#[test]
fn an_epoch_driver_started_after_the_cancel_still_ticks_for_an_attached_store() {
    let agent = start_with(CancelToken::new(), |ctx| {
        while !ctx.idle_until(Instant::now() + Duration::from_secs(300)) {}
        Ok(())
    });
    agent.shared.pool.open_gate(GateCause::Policy);
    let mut store = permit(&agent, Lane::Chat);
    store.attach().unwrap();
    agent.cancel();
    let engine = wasmtime::Engine::default();
    let driver = spawn_task(agent.shared.clone(), Task::Epoch, move |ctx| {
        EpochDriver::body(ctx, engine)
    })
    .unwrap();
    // the wrapper publishes Running (1); a skipped body would publish only
    // Exited (2) next, the body publishes its idle wait (2) and Running (3)
    // before it enters the tick loop, where it stays while the store is live
    wait_for(|| {
        let epoch = agent.health().epoch;
        epoch.progress >= 3 && epoch.phase == TaskPhase::Running
    });
    assert!(!driver.is_finished(), "ticking while the store is live");
    drop(store);
    driver.join().unwrap();
    assert_eq!(agent.health().epoch.phase, TaskPhase::Exited);
    assert!(agent.health().failures.is_empty());
}

/// r2 review OWN-R2-3: completion accounting around panics in our own
/// callbacks. A leftover sink that panics while an inbox-cancelled job's input
/// is handed on counts that job orphaned once (no report); a report callback
/// that delivers and then panics is never also counted orphaned.
#[test]
fn a_panicking_callback_counts_a_job_once_never_twice() {
    // (a) the sink panics on the inbox-cancel path (a worker that never ran)
    let agent = start_with(CancelToken::new(), |ctx| {
        while !ctx.idle_until(Instant::now() + Duration::from_secs(300)) {}
        Ok(())
    });
    let (report_tx, reports) = mpsc::channel::<LaneReport>();
    let report = Arc::new(move |r: LaneReport| {
        let _ = report_tx.send(r);
    });
    let sink: LeftoverSink = Arc::new(|_items: Vec<Value>| panic!("leftover sink panicked"));
    let (lanes, workers) = Lanes::build(
        &agent.shared,
        Arc::new(FakeRunner::new(None, None)),
        report,
        sink,
    );
    agent.shared.pool.open_gate(GateCause::Policy);
    let queued = job(Lane::Event, "queued");
    queued
        .mailbox
        .push(json!({"kind": "inject", "n": 1}))
        .unwrap();
    assert!(lanes
        .offer(queued, permit(&agent, Lane::Event), &agent.shared)
        .is_ok());
    agent.cancel();
    for (lane, work) in workers {
        spawn_task(agent.shared.clone(), Task::lane(lane), work)
            .unwrap()
            .join()
            .unwrap();
    }
    assert!(reports.try_recv().is_err(), "no report");
    assert_eq!(agent.health().orphaned_jobs, 1, "counted once");
    assert!(agent
        .health()
        .failures
        .contains(&ManagedFailure::ExecutionPanicked));

    // (b) the report callback delivers, then panics
    let agent = start_with(CancelToken::new(), |ctx| {
        while !ctx.idle_until(Instant::now() + Duration::from_secs(300)) {}
        Ok(())
    });
    let (report_tx, reports) = mpsc::channel::<LaneReport>();
    let report = Arc::new(move |r: LaneReport| {
        let _ = report_tx.send(r);
        panic!("report callback panicked after delivering");
    });
    let sink: LeftoverSink = Arc::new(|_items: Vec<Value>| Ok(()));
    let lanes = Lanes::spawn(
        &agent.shared,
        Arc::new(FakeRunner::new(None, None)),
        report,
        sink,
    )
    .unwrap();
    agent.shared.pool.open_gate(GateCause::Policy);
    assert!(lanes
        .offer(
            job(Lane::Chat, "noisy"),
            permit(&agent, Lane::Chat),
            &agent.shared
        )
        .is_ok());
    assert_eq!(reports.recv_timeout(BUDGET).unwrap().work_id, "noisy");
    wait_for(|| agent.health().phase == ManagedPhase::Failed);
    assert_eq!(
        agent.health().orphaned_jobs,
        0,
        "reported, so never orphaned"
    );
}
