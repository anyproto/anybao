use super::*;
use crate::engine_profile::{ArtifactManifestV1, BuildProvenance, EngineProfileV1};
use std::sync::mpsc::{self, Receiver, Sender};

const TEST_BUDGET: Duration = Duration::from_secs(5);

fn until() -> Instant {
    Instant::now() + TEST_BUDGET
}

fn wait_for(mut predicate: impl FnMut() -> bool) {
    let deadline = until();
    while !predicate() {
        assert!(Instant::now() < deadline, "condition did not become true");
        thread::sleep(Duration::from_millis(2));
    }
}

fn blocked_preparation() -> (ManagedAgent, Receiver<()>, Sender<()>) {
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let agent = start_with(CancelToken::new(), move |ctx| {
        ctx.wait(WaitClass::Http, Some(until()));
        entered_tx.send(()).unwrap();
        release_rx.recv_timeout(TEST_BUDGET).unwrap();
        Ok(())
    });
    (agent, entered_rx, release_tx)
}

fn options() -> ManagedOptions {
    let manifest = ArtifactManifestV1 {
        schema_version: 1,
        wasmtime_version: crate::engine_profile::WASMTIME_VERSION.into(),
        profile: EngineProfileV1::android(),
        resolved_profile: serde_json::Value::Null,
        profile_sha256: String::new(),
        source_sha256: String::new(),
        artifact_sha256: String::new(),
        provenance: BuildProvenance {
            anyrt_revision: "test".into(),
            rust_version: "test".into(),
            ndk_version: "test".into(),
            jni_protocol_version: None,
            any_version_pin: "v0.3.5".into(),
            any_versions_tested: vec![],
        },
    };
    // Static test fixture; production preparation must fail before trying to load it.
    let artifact = unsafe { TrustedArtifact::from_build(b"invalid test artifact", manifest) };
    let zone = ManagedTimeContext::new("Europe/Berlin", 3_600, None, None).unwrap();
    ManagedOptions::android_poc("http://127.0.0.1:7001", artifact, zone).unwrap()
}

#[test]
fn handle_is_controllable_while_boot_is_blocked_and_supervisor_progresses() {
    let (mut agent, entered, release) = blocked_preparation();
    entered.recv_timeout(TEST_BUDGET).unwrap();
    let first = agent.health();
    assert_eq!(first.phase, ManagedPhase::Preparing);
    assert!(!first.admission_open);
    assert_eq!(first.preparation.wait_class, WaitClass::Http);
    assert!(first.preparation.wait_deadline.is_some());
    wait_for(|| agent.health().supervisor.progress > first.supervisor.progress);
    assert!(agent.health().failures.is_empty());
    agent.cancel();
    release.send(()).unwrap();
    assert_eq!(agent.drain_until(until()), DrainOutcome::Drained);
}

#[test]
fn timeout_retains_owner_and_retry_joins_every_thread() {
    let (mut agent, entered, release) = blocked_preparation();
    entered.recv_timeout(TEST_BUDGET).unwrap();
    assert_eq!(
        agent.drain_until(Instant::now()),
        DrainOutcome::DeadlineExceeded
    );
    assert!(!agent.threads.is_empty());
    let health = agent.health();
    assert!(health.cancelled);
    assert!(!health.all_threads_joined);
    assert_eq!(health.phase, ManagedPhase::Stopping);
    release.send(()).unwrap();
    assert_eq!(agent.drain_until(until()), DrainOutcome::Drained);
    assert!(agent.threads.is_empty());
    let health = agent.health();
    assert_eq!(health.preparation.phase, TaskPhase::Exited);
    assert_eq!(health.supervisor.phase, TaskPhase::Exited);
    assert!(health.all_threads_joined);
    assert_eq!(agent.drain_until(Instant::now()), DrainOutcome::Drained);
}

#[test]
fn cancellation_before_start_performs_no_initialization() {
    let cancel = CancelToken::new();
    cancel.cancel();
    let mut agent = start_with(cancel, |_| panic!("must not initialize"));
    assert!(agent.threads.is_empty());
    assert_eq!(agent.health().preparation.phase, TaskPhase::NotStarted);
    assert_eq!(agent.drain_until(until()), DrainOutcome::Drained);
}

#[test]
fn external_cancellation_wakes_a_managed_wait() {
    let cancel = CancelToken::new();
    let (entered_tx, entered_rx) = mpsc::channel();
    let mut agent = start_with(cancel.clone(), move |ctx| {
        let deadline = Instant::now() + Duration::from_secs(300);
        ctx.wait(WaitClass::Idle, None);
        entered_tx.send(()).unwrap();
        assert!(ctx.shared.cancel.wait_until(deadline));
        Ok(())
    });
    entered_rx.recv_timeout(TEST_BUDGET).unwrap();
    cancel.cancel();
    assert_eq!(agent.drain_until(until()), DrainOutcome::Drained);
}

#[test]
fn normal_idle_wake_stays_healthy_when_supervisor_runs_before_preparation_resumes() {
    let (woke_tx, woke_rx) = mpsc::channel();
    let (resume_tx, resume_rx) = mpsc::channel();
    let mut agent = start_with(CancelToken::new(), move |ctx| {
        // The normal timeout has elapsed; retain that published idle state until
        // the test has observed a later supervisor pass, then explicitly cancel.
        assert!(!ctx.idle_until(Instant::now()));
        woke_tx.send(()).unwrap();
        resume_rx.recv_timeout(TEST_BUDGET).unwrap();
        assert!(ctx.shared.cancel.is_cancelled());
        Ok(())
    });
    woke_rx.recv_timeout(TEST_BUDGET).unwrap();
    let first = agent.health();
    assert_eq!(first.preparation.wait_class, WaitClass::Idle);
    assert!(first.preparation.wait_deadline.is_none());
    wait_for(|| agent.health().supervisor.progress > first.supervisor.progress);
    let after_supervision = agent.health();
    assert_eq!(after_supervision.phase, ManagedPhase::Preparing);
    assert!(after_supervision.failures.is_empty());
    assert!(!after_supervision.cancelled);
    agent.cancel();
    resume_tx.send(()).unwrap();
    assert_eq!(agent.drain_until(until()), DrainOutcome::Drained);
    assert!(agent.health().all_threads_joined);
}

#[test]
fn panic_with_poisoned_service_lock_reports_failure_and_joins_supervisor() {
    let service_lock = Arc::new(Mutex::new(()));
    let worker_lock = service_lock.clone();
    let mut agent = start_with(CancelToken::new(), move |_| {
        let _guard = worker_lock.lock().unwrap();
        panic!("test initialization panic");
    });
    wait_for(|| {
        agent
            .health()
            .failures
            .contains(&ManagedFailure::PreparationPanicked)
    });
    assert!(service_lock.is_poisoned());
    assert_eq!(
        agent.drain_until(until()),
        DrainOutcome::Failed(vec![ManagedFailure::PreparationPanicked])
    );
    assert!(agent.health().all_threads_joined);
    assert_eq!(agent.health().supervisor.phase, TaskPhase::Exited);
}

#[test]
fn unexpected_essential_exit_is_failure() {
    let mut agent = start_with(CancelToken::new(), |_| Ok(()));
    wait_for(|| agent.health().phase == ManagedPhase::Failed);
    assert_eq!(
        agent.drain_until(until()),
        DrainOutcome::Failed(vec![ManagedFailure::PreparationExitedUnexpectedly])
    );
}

#[test]
fn expired_wait_is_a_failure_but_retains_the_blocked_task() {
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let mut agent = start_with(CancelToken::new(), move |ctx| {
        ctx.wait(WaitClass::Dns, Some(Instant::now()));
        entered_tx.send(()).unwrap();
        release_rx.recv_timeout(TEST_BUDGET).unwrap();
        Ok(())
    });
    entered_rx.recv_timeout(TEST_BUDGET).unwrap();
    wait_for(|| {
        agent
            .health()
            .failures
            .contains(&ManagedFailure::PreparationWaitExpired)
    });
    assert_eq!(
        agent.drain_until(Instant::now()),
        DrainOutcome::DeadlineExceeded
    );
    assert!(!agent.health().all_threads_joined);
    release_tx.send(()).unwrap();
    assert_eq!(
        agent.drain_until(until()),
        DrainOutcome::Failed(vec![ManagedFailure::PreparationWaitExpired])
    );
}

#[test]
fn drop_cancels_and_waits_for_owned_work_instead_of_detaching_it() {
    let (agent, entered, release) = blocked_preparation();
    entered.recv_timeout(TEST_BUDGET).unwrap();
    let state = agent.shared.clone();
    let (done_tx, done_rx) = mpsc::channel();
    let dropper = thread::spawn(move || {
        drop(agent);
        done_tx.send(()).unwrap();
    });
    wait_for(|| state.cancel.is_cancelled());
    assert!(done_rx.try_recv().is_err());
    assert_eq!(state.status.load(Ordering::Acquire) & ALL_JOINED, 0);
    release.send(()).unwrap();
    done_rx.recv_timeout(TEST_BUDGET).unwrap();
    dropper.join().unwrap();
    assert_ne!(state.status.load(Ordering::Acquire) & ALL_JOINED, 0);
}

#[test]
fn all_join_errors_are_collected_after_the_first_failure() {
    let mut agent = start_with(CancelToken::new(), |ctx| {
        ctx.shared.cancel.wait_until(until());
        Ok(())
    });
    // Also exercise JoinHandle's fallback for a panic outside the task wrapper.
    agent
        .threads
        .push((Task::Preparation, thread::spawn(|| panic!("prepare"))));
    agent
        .threads
        .push((Task::Supervisor, thread::spawn(|| panic!("supervisor"))));
    assert_eq!(
        agent.drain_until(until()),
        DrainOutcome::Failed(vec![
            ManagedFailure::PreparationPanicked,
            ManagedFailure::SupervisorPanicked
        ])
    );
    assert!(agent.threads.is_empty());
    assert!(agent.health().all_threads_joined);
}

#[test]
fn stale_supervision_failure_survives_cancellation_and_drain() {
    let mut agent = ManagedAgent {
        shared: Arc::new(Shared::new(
            CancelToken::new(),
            Instant::now() - SUPERVISOR_STALE - Duration::from_secs(1),
            None,
        )),
        threads: Vec::new(),
    };
    assert_eq!(
        agent.health().failures,
        vec![ManagedFailure::SupervisorStale]
    );
    assert_eq!(
        agent.drain_until(until()),
        DrainOutcome::Failed(vec![ManagedFailure::SupervisorStale])
    );
}

#[test]
fn incompatible_artifact_fails_before_http_or_desktop_boot() {
    let opts = options();
    assert_eq!(opts.control_api(), ControlApi::Disabled);
    assert_eq!(opts.election_policy(), ElectionPolicy::HostControlled);
    let cfg = Config {
        traces_dir: "/this/path/must/not/be/created".into(),
        control_port: 0, // Not used as a substitute for Disabled.
        overlays: std::collections::BTreeMap::from([(
            "agent".to_string(),
            crate::config::Overlay {
                space: "overlay".into(),
                invite: None,
            },
        )]),
        ..Config::default()
    };
    let mut agent = start_managed(cfg, opts, CancelToken::new());
    wait_for(|| agent.health().phase == ManagedPhase::Failed);
    assert!(!agent.health().admission_open);
    assert_eq!(
        agent.drain_until(until()),
        DrainOutcome::Failed(vec![ManagedFailure::IncompatibleArtifact])
    );
    assert!(!std::path::Path::new("/this/path/must/not/be/created").exists());
}

#[test]
fn managed_options_reject_lease_mismatch_paths_boot_seeds_and_consent() {
    let opts = options();
    assert!(ManagedOptions::android_poc(
        "http://localhost:7001",
        opts.artifact().clone(),
        opts.time_context().clone()
    )
    .is_err());
    let mut cfg = Config::default();
    assert_eq!(
        opts.validate_config(&cfg),
        Err(ManagedFailure::InvalidConfiguration)
    );
    cfg.traces_dir = "/private/account/traces".into();
    assert!(opts.validate_config(&cfg).is_ok());
    cfg.addr = "http://127.0.0.1:7002".into();
    assert!(opts.validate_config(&cfg).is_err());
    cfg.addr = "http://127.0.0.1:7001".into();
    cfg.kernel = Some("/a/kernel.wasm".into());
    assert!(opts.validate_config(&cfg).is_err());
    cfg.kernel = None;
    cfg.secret_overrides
        .insert("provider.key".into(), "not-a-real-key".into());
    assert!(opts.validate_config(&cfg).is_err());
    cfg.secret_overrides.clear();
    cfg.config_overrides
        .insert("model".into(), serde_json::json!("test"));
    assert!(opts.validate_config(&cfg).is_err());
    cfg.config_overrides.clear();
    cfg.consent_hook = Some(crate::oauth::ConsentHook(Arc::new(|_| {
        panic!("must not deliver consent")
    })));
    assert!(opts.validate_config(&cfg).is_err());
}

/// r2 review OWN-R2-1: an external token cancel that lands before the
/// supervisor thread first runs still reaches the pool and the live runs —
/// the supervisor's stop propagation runs anyway. Checked before any drain.
#[test]
fn an_external_cancel_before_the_supervisor_runs_still_closes_the_pool_and_interrupts_runs() {
    let shared = Arc::new(Shared::new(CancelToken::new(), Instant::now(), None));
    shared.pool.open_gate(admission::GateCause::Policy);
    let interrupt = Arc::new(AtomicBool::new(false));
    shared.live_runs.lock().unwrap().insert(
        "run_live".into(),
        crate::triggers::LiveRun {
            mailbox: Default::default(),
            interrupt: interrupt.clone(),
            stamp: serde_json::json!({"id": "run_live"}),
            activity: Default::default(),
        },
    );
    // the external token alone: nothing has run `on_stop` yet
    shared.cancel.cancel();
    assert!(!shared.pool.is_closed());
    spawn_task(shared.clone(), Task::Supervisor, supervisor)
        .unwrap()
        .join()
        .unwrap();
    assert!(
        shared.pool.is_closed(),
        "no permit after the external cancel"
    );
    assert!(
        interrupt.load(Ordering::Relaxed),
        "the live run is interrupted"
    );
    assert!(matches!(
        shared.pool.acquire(Lane::Chat),
        admission::Admission::CapacityDeferred
    ));
}
