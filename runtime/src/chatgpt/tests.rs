use super::*;
use crate::local_grant::GrantStoreError;
use base64::Engine;
use serde_json::Value;
use std::collections::{BTreeSet, VecDeque};
use std::io::{Read, Write};
use std::path::PathBuf;
use std::sync::atomic::AtomicUsize;
use std::sync::mpsc;
#[path = "custody_tests.rs"]
mod custody_tests;

const NOW: u64 = 2_000_000_000;
const AUTH_CLAIMS: &str = "https://api.openai.com/auth";
static BROWSER_TEST: Mutex<()> = Mutex::new(());

fn private_dir() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    }
    dir
}

#[derive(Default)]
struct SyntheticStore {
    value: Arc<Mutex<Option<GrantEnvelope>>>,
    failure: Arc<Mutex<Option<GrantStoreError>>>,
    access_hook: Mutex<Option<Box<dyn FnOnce() + Send>>>,
    access_commits: Arc<AtomicUsize>,
    fail_saves: Mutex<BTreeSet<usize>>,
    ambiguous_saves: Mutex<BTreeSet<usize>>,
    saves: AtomicUsize,
    reads: AtomicUsize,
    panic_reads: AtomicBool,
}
impl LocalGrantStore for SyntheticStore {
    fn namespace(&self) -> &str {
        "anybao.synthetic-test"
    }
    fn read(&self, _: &GrantSlot) -> Result<Option<GrantEnvelope>, GrantStoreError> {
        assert!(
            !self.panic_reads.load(Ordering::Relaxed),
            "unexpected custody access"
        );
        self.reads.fetch_add(1, Ordering::Relaxed);
        if let Some(e) = *self.failure.lock().unwrap() {
            return Err(e);
        }
        Ok(self.value.lock().unwrap().clone())
    }
    fn replace(&self, _: &GrantSlot, value: &GrantEnvelope) -> Result<(), GrantStoreError> {
        let n = self.saves.fetch_add(1, Ordering::Relaxed) + 1;
        if let Some(e) = *self.failure.lock().unwrap() {
            return Err(e);
        }
        if self.fail_saves.lock().unwrap().contains(&n) {
            return Err(GrantStoreError::Unavailable);
        }
        *self.value.lock().unwrap() = Some(value.clone());
        if self.ambiguous_saves.lock().unwrap().contains(&n) {
            return Err(GrantStoreError::Unavailable);
        }
        Ok(())
    }
    fn prepare_access(
        &self,
        _: &GrantSlot,
        empty: &GrantEnvelope,
        cancelled: &(dyn Fn() -> bool + Sync),
    ) -> Result<Box<dyn crate::local_grant::PreparedGrantAccess>, GrantStoreError> {
        if let Some(hook) = self.access_hook.lock().unwrap().take() {
            hook();
        }
        if cancelled() {
            return Err(GrantStoreError::Cancelled);
        }
        let value = self
            .value
            .lock()
            .unwrap()
            .clone()
            .unwrap_or_else(|| empty.clone());
        Ok(Box::new(SyntheticAccess {
            store: self.value.clone(),
            failure: self.failure.clone(),
            commits: self.access_commits.clone(),
            value,
        }))
    }
    fn recovery(
        &self,
        _: &GrantSlot,
    ) -> Result<crate::local_grant::StoreRecovery, GrantStoreError> {
        use crate::local_grant::{RecoveryReason, StoreRecovery};
        use sha2::{Digest, Sha256};
        let reason = match *self.failure.lock().unwrap() {
            Some(GrantStoreError::MissingKey) => RecoveryReason::MissingKey,
            Some(GrantStoreError::CorruptRecord) => RecoveryReason::CorruptRecord,
            Some(error) => return Err(error),
            None => return Err(GrantStoreError::NotCurrent),
        };
        let value = self.value.lock().unwrap();
        let bytes = value
            .as_ref()
            .ok_or(GrantStoreError::NotCurrent)?
            .encode()?;
        Ok(StoreRecovery {
            fingerprint: Sha256::digest(bytes).into(),
            reason,
        })
    }
    fn reset(
        &self,
        slot: &GrantSlot,
        expected: &crate::local_grant::StoreRecovery,
    ) -> Result<(), GrantStoreError> {
        if &self.recovery(slot)? != expected {
            return Err(GrantStoreError::NotCurrent);
        }
        *self.value.lock().unwrap() = None;
        *self.failure.lock().unwrap() = None;
        Ok(())
    }
}
struct SyntheticAccess {
    store: Arc<Mutex<Option<GrantEnvelope>>>,
    failure: Arc<Mutex<Option<GrantStoreError>>>,
    commits: Arc<AtomicUsize>,
    value: GrantEnvelope,
}
impl crate::local_grant::PreparedGrantAccess for SyntheticAccess {
    fn commit(self: Box<Self>) -> Result<(), GrantStoreError> {
        self.commits.fetch_add(1, Ordering::SeqCst);
        *self.store.lock().unwrap() = Some(self.value);
        *self.failure.lock().unwrap() = None;
        Ok(())
    }
}

struct TestClock {
    now: AtomicU64,
    advance: AtomicBool,
    fraction: Mutex<Duration>,
    waits: Mutex<Vec<Duration>>,
}
impl TestClock {
    fn new() -> Self {
        Self {
            now: AtomicU64::new(NOW),
            advance: AtomicBool::new(true),
            fraction: Mutex::new(Duration::ZERO),
            waits: Mutex::new(Vec::new()),
        }
    }
}
impl Clock for TestClock {
    fn now(&self) -> u64 {
        self.now.load(Ordering::Acquire)
    }
    fn wait(&self, duration: Duration, cancel: &AtomicBool) {
        self.waits.lock().unwrap().push(duration);
        std::thread::sleep(Duration::from_millis(2));
        if !cancel.load(Ordering::Acquire) && self.advance.load(Ordering::Acquire) {
            let mut fraction = self.fraction.lock().unwrap();
            *fraction += duration;
            self.now.fetch_add(fraction.as_secs(), Ordering::Release);
            *fraction = Duration::from_nanos(fraction.subsec_nanos().into());
        }
    }
}

type Step = Box<dyn FnOnce(Endpoint, AuthBody) -> Result<AuthReply, TransportFailure> + Send>;
#[derive(Default)]
struct Script {
    steps: Mutex<VecDeque<Step>>,
    calls: AtomicUsize,
}
impl Script {
    fn add(&self, step: Step) {
        self.steps.lock().unwrap().push_back(step);
    }
    fn response(&self, endpoint: Endpoint, status: u16, body: Value) {
        self.add(Box::new(move |actual, _| {
            assert_eq!(actual, endpoint);
            Ok(AuthReply {
                status,
                body,
                retry_after: None,
            })
        }));
    }
    fn blocked(
        &self,
        endpoint: Endpoint,
        status: u16,
        body: Value,
    ) -> (mpsc::Receiver<()>, mpsc::Sender<()>) {
        let (sent, entered) = mpsc::channel();
        let (release, wait) = mpsc::channel();
        self.add(Box::new(move |actual, _| {
            assert_eq!(actual, endpoint);
            sent.send(()).unwrap();
            wait.recv_timeout(Duration::from_secs(10)).unwrap();
            Ok(AuthReply {
                status,
                body,
                retry_after: None,
            })
        }));
        (entered, release)
    }
}
impl AuthTransport for Script {
    fn post(
        &self,
        endpoint: Endpoint,
        body: AuthBody,
        timeout: Duration,
    ) -> Result<AuthReply, TransportFailure> {
        assert!(timeout > Duration::ZERO && timeout <= Duration::from_secs(30));
        self.calls.fetch_add(1, Ordering::Relaxed);
        let step = self
            .steps
            .lock()
            .unwrap()
            .pop_front()
            .expect("unexpected synthetic auth call");
        step(endpoint, body)
    }
}

fn slot() -> GrantSlot {
    GrantSlot::new("bao/test", "profile", "any-account", "device").unwrap()
}
fn token(claims: Value) -> String {
    format!(
        "e30.{}.synthetic-signature",
        base64::engine::general_purpose::URL_SAFE_NO_PAD
            .encode(serde_json::to_vec(&claims).unwrap())
    )
}
fn claims(account: &str, expiry: u64) -> Value {
    json!({"iss": ISSUER, "sub": "synthetic-subject", "exp": expiry,
        AUTH_CLAIMS: {"chatgpt_account_id": account, "chatgpt_plan_type": "synthetic-plan"}})
}
fn tokens(account: &str, expiry: u64, refresh: Option<&str>) -> Value {
    let mut body = json!({"access_token": token(claims(account, expiry)), "token_type": "Bearer", "expires_in": 3600});
    if let Some(refresh) = refresh {
        body["refresh_token"] = json!(refresh);
    }
    body
}
fn seed(store: &SyntheticStore, expiry: u64) -> GrantEnvelope {
    let mut env = GrantEnvelope::empty(&slot());
    env.revision = 1;
    env.grant = Some(
        parse_grant(
            &tokens("synthetic-account", expiry, Some("synthetic-refresh-1")),
            NOW,
            None,
        )
        .unwrap(),
    );
    *store.value.lock().unwrap() = Some(env.clone());
    env
}
fn service(
    dir: &std::path::Path,
    store: Arc<dyn LocalGrantStore>,
    script: Arc<Script>,
    clock: Arc<dyn Clock>,
) -> Arc<ChatGptAuth> {
    let locks = GrantLockDirectory::new(dir).unwrap();
    let lock = locks.slot_lock(&slot(), store.namespace()).unwrap();
    ChatGptAuth::create(
        Some(Storage {
            slot: slot(),
            store,
            lock,
        }),
        "device".into(),
        71,
        None,
        script,
        clock,
    )
}
struct Harness {
    dir: tempfile::TempDir,
    store: Arc<SyntheticStore>,
    script: Arc<Script>,
    clock: Arc<TestClock>,
    auth: Arc<ChatGptAuth>,
}
impl Harness {
    fn new() -> Self {
        let dir = private_dir();
        let store = Arc::new(SyntheticStore::default());
        let script = Arc::new(Script::default());
        let clock = Arc::new(TestClock::new());
        let auth = service(dir.path(), store.clone(), script.clone(), clock.clone());
        Self {
            dir,
            store,
            script,
            clock,
            auth,
        }
    }
    fn peer(&self) -> Arc<ChatGptAuth> {
        service(
            self.dir.path(),
            self.store.clone(),
            self.script.clone(),
            self.clock.clone(),
        )
    }
    fn authorize(&self) -> Result<DispatchCredential, AuthError> {
        self.auth.authorize_dispatch(None, &AtomicBool::new(false))
    }
    fn login_script(&self) {
        self.script.response(Endpoint::DeviceCode, 200, json!({"device_auth_id": "synthetic-device-id", "user_code": "SYN-CODE", "interval": 1}));
        self.script.response(
            Endpoint::DevicePoll,
            200,
            json!({"authorization_code": "synthetic-code", "code_verifier": "synthetic-verifier"}),
        );
        self.script.response(
            Endpoint::Token,
            200,
            tokens("synthetic-account", NOW + 3600, Some("synthetic-refresh-2")),
        );
    }
}

fn eventually(mut predicate: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while !predicate() {
        assert!(
            Instant::now() < deadline,
            "synthetic lifecycle did not settle"
        );
        std::thread::sleep(Duration::from_millis(5));
    }
}
fn workers_done(auth: &ChatGptAuth) {
    eventually(|| auth.session.lock().unwrap().login_workers == 0);
}

fn completion(auth: &ChatGptAuth, stamp: &DispatchStamp) -> CompletionStatus {
    auth.check_completion(
        stamp,
        &AtomicBool::new(false),
        Instant::now() + Duration::from_secs(10),
    )
    .unwrap()
}

struct ReceiptHookClock {
    base: Arc<TestClock>,
    armed_reads: AtomicUsize,
    entered: mpsc::Sender<()>,
    release: Mutex<mpsc::Receiver<()>>,
}
impl Clock for ReceiptHookClock {
    fn now(&self) -> u64 {
        // Pause at parse_grant's receipt-time read, after the final observation
        // has released its lock. Starting an actual refresh below verifies that
        // this hook is outside the slot transaction, not an earlier locked read.
        if self.armed_reads.load(Ordering::Acquire) > 0
            && self.armed_reads.fetch_add(1, Ordering::AcqRel) == 6
        {
            self.entered.send(()).unwrap();
            self.release
                .lock()
                .unwrap()
                .recv_timeout(Duration::from_secs(5))
                .unwrap();
        }
        self.base.now()
    }
    fn wait(&self, duration: Duration, cancel: &AtomicBool) {
        self.base.wait(duration, cancel);
    }
}

struct LoginCommitRace {
    h: Harness,
    auth: Arc<ChatGptAuth>,
    login: LoginStart,
    release_refresh: mpsc::Sender<()>,
    refresh: std::thread::JoinHandle<Result<DispatchCredential, AuthError>>,
    started: Instant,
}
fn login_racing_refresh() -> LoginCommitRace {
    let h = Harness::new();
    seed(&h.store, NOW + 3600);
    let (at_receipt, received) = mpsc::channel();
    let (resume_receipt, receipt_wait) = mpsc::channel();
    let clock = Arc::new(ReceiptHookClock {
        base: h.clock.clone(),
        armed_reads: AtomicUsize::new(0),
        entered: at_receipt,
        release: Mutex::new(receipt_wait),
    });
    let auth = service(
        h.dir.path(),
        h.store.clone(),
        h.script.clone(),
        clock.clone(),
    );
    let stamp = auth
        .authorize_dispatch(None, &AtomicBool::new(false))
        .unwrap()
        .stamp;
    h.script.response(
        Endpoint::DeviceCode,
        200,
        json!({"device_auth_id":"synthetic-id","user_code":"SYN","interval":3}),
    );
    h.script.response(
        Endpoint::DevicePoll,
        200,
        json!({"authorization_code":"synthetic-code","code_verifier":"synthetic-verifier"}),
    );
    h.script.add(Box::new(move |endpoint, _| {
        assert_eq!(endpoint, Endpoint::Token);
        clock.armed_reads.store(1, Ordering::Release);
        Ok(AuthReply {
            status: 200,
            body: tokens(
                "synthetic-replacement",
                NOW + 3600,
                Some("synthetic-login-refresh"),
            ),
            retry_after: None,
        })
    }));
    let (refresh_sent, refresh_entered) = mpsc::channel();
    let (release_refresh, refresh_wait) = mpsc::channel();
    h.script.add(Box::new(move |endpoint, _| {
        assert_eq!(endpoint, Endpoint::Token);
        refresh_sent.send(()).unwrap();
        refresh_wait.recv_timeout(Duration::from_secs(25)).unwrap();
        Ok(AuthReply {
            status: 200,
            body: tokens(
                "synthetic-account",
                NOW + 3600,
                Some("synthetic-peer-refresh"),
            ),
            retry_after: None,
        })
    }));
    let login = auth.start_login(LoginFlow::DeviceCode).unwrap();
    received.recv_timeout(Duration::from_secs(2)).unwrap();
    let peer = h.peer();
    let refresh =
        std::thread::spawn(move || peer.authorize_dispatch(Some(&stamp), &AtomicBool::new(false)));
    refresh_entered
        .recv_timeout(Duration::from_secs(2))
        .unwrap();
    resume_receipt.send(()).unwrap();
    LoginCommitRace {
        h,
        auth,
        login,
        release_refresh,
        refresh,
        started: Instant::now(),
    }
}

fn outwait_old_commit_and_cleanup_budget(started: Instant) {
    std::thread::sleep(Duration::from_millis(10_200).saturating_sub(started.elapsed()));
}

#[test]
fn login_result_survives_long_refresh_after_final_observation_without_repeating_oauth() {
    let race = login_racing_refresh();
    outwait_old_commit_and_cleanup_budget(race.started);
    assert_eq!(race.auth.session.lock().unwrap().login_workers, 1);
    assert_eq!(race.h.script.calls.load(Ordering::Relaxed), 4);
    race.release_refresh.send(()).unwrap();
    race.refresh.join().unwrap().unwrap();
    workers_done(&race.auth);
    let env = race.h.store.value.lock().unwrap().clone().unwrap();
    assert!(env.pending.is_none());
    let grant = env.grant.unwrap();
    assert_eq!(grant.principal.account_id, "synthetic-replacement");
    assert_eq!(grant.refresh.0, "synthetic-login-refresh");
    assert!(race.auth.session.lock().unwrap().work.is_none());
    assert_eq!(
        race.auth.session.lock().unwrap().snapshot.state,
        ConnectionState::Connected
    );
    assert_eq!(race.h.script.calls.load(Ordering::Relaxed), 4);
}

#[test]
fn cancelled_login_waits_out_refresh_to_clear_only_pending_and_keeps_old_pair() {
    let race = login_racing_refresh();
    assert_eq!(
        race.auth
            .cancel_login(&race.login.login_id)
            .unwrap_err()
            .code,
        AuthErrorCode::AuthBusy
    );
    outwait_old_commit_and_cleanup_budget(race.started);
    assert_eq!(race.auth.session.lock().unwrap().login_workers, 1);
    race.release_refresh.send(()).unwrap();
    race.refresh.join().unwrap().unwrap();
    workers_done(&race.auth);
    let env = race.h.store.value.lock().unwrap().clone().unwrap();
    assert!(env.pending.is_none());
    assert_eq!(env.grant.unwrap().refresh.0, "synthetic-peer-refresh");
    assert_eq!(
        race.auth.session.lock().unwrap().snapshot.error_code,
        Some(AuthErrorCode::Cancelled)
    );
    race.h.login_script();
    race.auth.start_login(LoginFlow::DeviceCode).unwrap();
    workers_done(&race.auth);
    assert_eq!(race.h.script.calls.load(Ordering::Relaxed), 7);
}

#[test]
fn expired_login_never_installs_after_refresh_but_finishes_matching_cleanup() {
    let race = login_racing_refresh();
    let work = race.auth.session.lock().unwrap().work.clone().unwrap();
    *work.deadline.lock().unwrap() = Instant::now();
    outwait_old_commit_and_cleanup_budget(race.started);
    assert_eq!(race.auth.session.lock().unwrap().login_workers, 1);
    race.release_refresh.send(()).unwrap();
    race.refresh.join().unwrap().unwrap();
    workers_done(&race.auth);
    let env = race.h.store.value.lock().unwrap().clone().unwrap();
    assert!(env.pending.is_none());
    assert_eq!(env.grant.unwrap().principal.account_id, "synthetic-account");
    assert_eq!(
        race.auth.session.lock().unwrap().snapshot.error_code,
        Some(AuthErrorCode::LoginExpired)
    );
    assert_eq!(race.h.script.calls.load(Ordering::Relaxed), 4);
}

#[test]
fn session_shutdown_during_commit_wait_cleans_up_after_refresh_without_late_install() {
    let race = login_racing_refresh();
    let auth = race.auth.clone();
    let stopping = std::thread::spawn(move || auth.shutdown());
    eventually(|| race.auth.ended.load(Ordering::Acquire));
    outwait_old_commit_and_cleanup_budget(race.started);
    assert!(!stopping.is_finished());
    race.release_refresh.send(()).unwrap();
    race.refresh.join().unwrap().unwrap();
    eventually(|| stopping.is_finished());
    stopping.join().unwrap();
    let env = race.h.store.value.lock().unwrap().clone().unwrap();
    assert!(env.pending.is_none());
    assert_eq!(env.grant.unwrap().refresh.0, "synthetic-peer-refresh");
    let session = race.auth.session.lock().unwrap();
    assert_eq!(session.login_workers, 0);
    assert!(session.work.is_none());
    assert_eq!(
        session.snapshot.error_code,
        Some(AuthErrorCode::SessionEnded)
    );
}

#[test]
fn fresh_device_login_restart_and_safe_events() {
    let h = Harness::new();
    h.login_script();
    let watch = h.auth.subscribe();
    assert_eq!(h.auth.get_connection().state, ConnectionState::Disconnected);
    let started = h.auth.start_login(LoginFlow::DeviceCode).unwrap();
    assert!(matches!(
        started.presentation,
        LoginPresentation::DeviceCode { .. }
    ));
    workers_done(&h.auth);
    let snapshot = h.auth.get_connection();
    assert_eq!(snapshot.state, ConnectionState::Connected);
    assert!(snapshot.pending_login.is_none());
    let event = watch.latest().unwrap();
    assert_eq!(snapshot, event);
    let safe = format!(
        "{:?}{}{:?}{:?}",
        snapshot,
        serde_json::to_string(&event).unwrap(),
        started,
        h.store.value.lock().unwrap()
    );
    for hidden in [
        "synthetic-refresh",
        "synthetic-subject",
        "synthetic-account",
        "synthetic-device-id",
        "SYN-CODE",
        "synthetic-code",
        "access_token",
        "refresh_token",
    ] {
        assert!(!safe.contains(hidden), "unsafe diagnostic field");
    }
    assert_eq!(h.peer().get_connection().state, ConnectionState::Connected);
    assert_eq!(h.authorize().unwrap().account_id, "synthetic-account");
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 3);
    assert_eq!(
        h.clock.waits.lock().unwrap().iter().sum::<Duration>(),
        Duration::from_secs(3)
    );
}

#[test]
fn device_form_and_fixed_destinations_are_host_owned() {
    let h = Harness::new();
    h.script.add(Box::new(|endpoint, body| {
        assert_eq!(endpoint, Endpoint::DeviceCode);
        match body {
            AuthBody::Json(body) => assert_eq!(body, json!({"client_id": CLIENT_ID})),
            _ => panic!("wrong protocol"),
        }
        Ok(AuthReply {
            status: 200,
            body: json!({"device_auth_id": "synthetic-id", "user_code": "SYNTHETIC"}),
            retry_after: None,
        })
    }));
    h.script.add(Box::new(|endpoint, body| {
        assert_eq!(endpoint, Endpoint::DevicePoll);
        match body { AuthBody::Json(body) => assert_eq!(body, json!({"device_auth_id": "synthetic-id", "user_code": "SYNTHETIC"})), _ => panic!("wrong protocol") }
        Ok(AuthReply { status: 200, body: json!({"authorization_code": "synthetic-code", "code_verifier": "synthetic-verifier"}), retry_after: None })
    }));
    h.script.add(Box::new(|endpoint, body| {
        assert_eq!(endpoint, Endpoint::Token);
        match body {
            AuthBody::Form(pairs) => {
                assert!(pairs.contains(&("redirect_uri", DEVICE_REDIRECT.into())));
                assert!(pairs.contains(&("client_id", CLIENT_ID.into())));
                assert!(pairs.contains(&("code_verifier", "synthetic-verifier".into())));
            }
            _ => panic!("wrong protocol"),
        }
        Ok(AuthReply {
            status: 200,
            body: tokens("synthetic-account", NOW + 3600, Some("synthetic-refresh")),
            retry_after: None,
        })
    }));
    h.auth.start_login(LoginFlow::DeviceCode).unwrap();
    workers_done(&h.auth);
    assert_eq!(h.auth.get_connection().state, ConnectionState::Connected);
    assert_eq!(
        h.clock.waits.lock().unwrap().iter().sum::<Duration>(),
        Duration::from_secs(5)
    );
}

#[test]
fn duplicate_start_joins_owner_and_foreign_process_gets_only_safe_pending() {
    let h = Harness::new();
    let (entered, release) = h.script.blocked(
        Endpoint::DeviceCode,
        200,
        json!({"device_auth_id": "synthetic-id", "user_code": "SYNTHETIC"}),
    );
    let auth = h.auth.clone();
    let first = std::thread::spawn(move || auth.start_login(LoginFlow::DeviceCode));
    entered.recv_timeout(Duration::from_secs(2)).unwrap();
    let pending = h.auth.get_connection().pending_login.unwrap();
    let error = h.peer().start_login(LoginFlow::DeviceCode).unwrap_err();
    assert_eq!(error.code, AuthErrorCode::LoginInProgress);
    assert_eq!(error.pending_login, Some(pending.clone()));
    assert_eq!(
        h.auth.start_login(LoginFlow::Browser).unwrap_err().code,
        AuthErrorCode::LoginInProgress
    );
    let auth = h.auth.clone();
    let duplicate = std::thread::spawn(move || auth.start_login(LoginFlow::DeviceCode));
    eventually(|| {
        h.auth
            .session
            .lock()
            .unwrap()
            .work
            .as_ref()
            .is_some_and(|w| Arc::strong_count(w) >= 4)
    });
    h.auth.cancel_login(&pending.login_id).unwrap();
    release.send(()).unwrap();
    assert!(first.join().unwrap().is_err());
    assert!(duplicate.join().unwrap().is_err());
    workers_done(&h.auth);
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 1);
    assert_eq!(h.auth.get_connection().state, ConnectionState::Disconnected);
}

#[test]
fn owned_cleanup_rejects_a_foreign_pending_event_but_interrupts_own_acquisition() {
    let h = Harness::new();
    let (entered, release) = h.script.blocked(
        Endpoint::DeviceCode,
        200,
        json!({"device_auth_id": "synthetic-id", "user_code": "SYN"}),
    );
    let work = h.auth.begin_login(LoginFlow::DeviceCode).unwrap();
    entered.recv_timeout(Duration::from_secs(2)).unwrap();
    let peer = h.peer();
    let error = peer.start_login(LoginFlow::DeviceCode).unwrap_err();
    assert_eq!(error.code, AuthErrorCode::LoginInProgress);
    assert_eq!(
        peer.get_connection().pending_login.unwrap().login_id,
        work.id
    );
    let before = h.store.value.lock().unwrap().clone().unwrap();
    let saves = h.store.saves.load(Ordering::Relaxed);
    assert_eq!(
        peer.cancel_login_if_owned(&work.id).unwrap(),
        CancelOutcome::NotCurrent
    );
    assert_eq!(h.store.saves.load(Ordering::Relaxed), saves);
    assert_eq!(
        json!(h.store.value.lock().unwrap().as_ref().unwrap().pending),
        json!(before.pending)
    );
    assert!(!work.cancel.load(Ordering::Acquire));
    assert_eq!(
        h.auth.cancel_login_if_owned(&work.id).unwrap(),
        CancelOutcome::Cancelled
    );
    assert!(work.cancel.load(Ordering::Acquire));
    assert_eq!(
        work.wait_start().unwrap_err().code,
        AuthErrorCode::Cancelled
    );
    release.send(()).unwrap();
    workers_done(&h.auth);
}

#[test]
fn owned_cleanup_rechecks_durable_identity_under_the_transaction() {
    for (same_id, expired) in [(false, false), (true, false), (true, true)] {
        let h = Harness::new();
        let (entered, release) = h.script.blocked(
            Endpoint::DeviceCode,
            200,
            json!({"device_auth_id": "synthetic-id", "user_code": "SYN"}),
        );
        let work = h.auth.begin_login(LoginFlow::DeviceCode).unwrap();
        entered.recv_timeout(Duration::from_secs(2)).unwrap();
        let transaction = h
            .auth
            .storage
            .as_ref()
            .unwrap()
            .lock
            .acquire(LOCK_WAIT, || false)
            .unwrap();
        let auth = h.auth.clone();
        let id = work.id.clone();
        let cancelling = std::thread::spawn(move || auth.cancel_login_if_owned(&id));
        eventually(|| work.cancel.load(Ordering::Acquire));
        let foreign = {
            let mut saved = h.store.value.lock().unwrap();
            let pending = saved.as_mut().unwrap().pending.as_mut().unwrap();
            pending.owner_instance = random_id();
            if !same_id {
                pending.id = random_id();
            }
            if expired {
                pending.expires_at = NOW - 1;
            }
            pending.clone()
        };
        drop(transaction);
        assert_eq!(
            cancelling.join().unwrap().unwrap(),
            CancelOutcome::NotCurrent
        );
        assert_eq!(
            json!(h.store.value.lock().unwrap().as_ref().unwrap().pending),
            json!(Some(&foreign))
        );
        release.send(()).unwrap();
        workers_done(&h.auth);
        if !expired {
            assert_eq!(
                json!(h.store.value.lock().unwrap().as_ref().unwrap().pending),
                json!(Some(&foreign))
            );
        }
    }
}

#[test]
fn owned_cleanup_before_reservation_does_not_cancel_a_peer_or_disarm_later_cleanup() {
    let h = Harness::new();
    let peer = h.peer();
    let (entered, release) = h.script.blocked(
        Endpoint::DeviceCode,
        200,
        json!({"device_auth_id": "synthetic-peer", "user_code": "PEER"}),
    );
    let foreign = peer.begin_login(LoginFlow::DeviceCode).unwrap();
    entered.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(
        h.auth.cancel_login_if_owned(&foreign.id).unwrap(),
        CancelOutcome::NotCurrent
    );
    assert!(!foreign.cancel.load(Ordering::Acquire));
    peer.cancel_login(&foreign.id).unwrap();
    release.send(()).unwrap();
    workers_done(&peer);
    let (entered, release) = h.script.blocked(
        Endpoint::DeviceCode,
        200,
        json!({"device_auth_id": "synthetic-own", "user_code": "OWN"}),
    );
    let own = h.auth.begin_login(LoginFlow::DeviceCode).unwrap();
    entered.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(
        h.auth.cancel_login_if_owned(&own.id).unwrap(),
        CancelOutcome::Cancelled
    );
    release.send(()).unwrap();
    workers_done(&h.auth);
}

#[test]
fn peer_cancel_stops_device_wait_and_keeps_the_established_grant() {
    let h = Harness::new();
    let old = seed(&h.store, NOW + 3600).grant.unwrap().id;
    h.clock.advance.store(false, Ordering::Release);
    h.script.response(
        Endpoint::DeviceCode,
        200,
        json!({"device_auth_id": "synthetic-id", "user_code": "SYN", "interval": 60}),
    );
    let watch = h.auth.subscribe();
    let start = h.auth.start_login(LoginFlow::DeviceCode).unwrap();
    watch.latest();
    eventually(|| !h.clock.waits.lock().unwrap().is_empty());
    let peer = h.peer();
    assert_eq!(
        peer.cancel_login(&start.login_id).unwrap(),
        CancelOutcome::Cancelled
    );
    assert_eq!(
        peer.cancel_login(&start.login_id).unwrap(),
        CancelOutcome::AlreadyFinished
    );
    workers_done(&h.auth);
    let observed = watch
        .latest()
        .expect("owner observes durable cancellation while waiting");
    assert_eq!(observed.state, ConnectionState::Connected);
    assert!(observed.pending_login.is_none());
    let env = h.store.value.lock().unwrap().clone().unwrap();
    assert!(env.pending.is_none());
    assert_eq!(env.grant.unwrap().id, old);
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 1);
}

#[test]
fn peer_cancel_discards_an_inflight_device_result_before_polling_or_commit() {
    let h = Harness::new();
    let old = seed(&h.store, NOW + 3600).grant.unwrap().id;
    let (entered, release) = h.script.blocked(
        Endpoint::DeviceCode,
        200,
        json!({"device_auth_id": "synthetic-id", "user_code": "SYN"}),
    );
    let auth = h.auth.clone();
    let start = std::thread::spawn(move || auth.start_login(LoginFlow::DeviceCode));
    entered.recv_timeout(Duration::from_secs(2)).unwrap();
    let peer = h.peer();
    let pending = peer.get_connection().pending_login.unwrap();
    assert_eq!(
        peer.cancel_login(&pending.login_id).unwrap(),
        CancelOutcome::Cancelled
    );
    release.send(()).unwrap();
    assert_eq!(
        start.join().unwrap().unwrap_err().code,
        AuthErrorCode::Cancelled
    );
    workers_done(&h.auth);
    assert_eq!(
        h.store
            .value
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .grant
            .as_ref()
            .unwrap()
            .id,
        old
    );
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 1);
}

#[test]
fn device_pending_rate_limit_and_absolute_server_expiry() {
    let h = Harness::new();
    h.script.response(Endpoint::DeviceCode, 200, json!({"device_auth_id": "synthetic-id", "user_code": "SYN", "interval": "3", "expires_in": 13}));
    h.script.response(Endpoint::DevicePoll, 403, Value::Null);
    h.script.response(Endpoint::DevicePoll, 404, Value::Null);
    h.script.response(
        Endpoint::DevicePoll,
        429,
        json!({"error": "synthetic-untrusted-text"}),
    );
    let start = h.auth.start_login(LoginFlow::DeviceCode).unwrap();
    assert_eq!(start.expires_at, NOW + 13);
    workers_done(&h.auth);
    let snapshot = h.auth.session.lock().unwrap().snapshot.clone();
    assert_eq!(snapshot.state, ConnectionState::Disconnected);
    assert_eq!(snapshot.error_code, Some(AuthErrorCode::LoginExpired));
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 4);
    assert!(h
        .store
        .value
        .lock()
        .unwrap()
        .as_ref()
        .unwrap()
        .pending
        .is_none());
}

#[test]
fn device_deny_disabled_and_malformed_are_bounded_errors() {
    for (status, body, expected) in [
        (
            403,
            json!({"error": "secret-reflection"}),
            AuthErrorCode::DeviceFlowDisabled,
        ),
        (
            400,
            json!({"error": "access_denied", "error_description": "secret-reflection"}),
            AuthErrorCode::ConsentDenied,
        ),
        (
            429,
            json!({"error": "secret-reflection"}),
            AuthErrorCode::AuthRateLimited,
        ),
        (
            200,
            json!({"device_auth_id": "secret-reflection"}),
            AuthErrorCode::UnsupportedTokenShape,
        ),
    ] {
        let h = Harness::new();
        h.script.response(Endpoint::DeviceCode, status, body);
        let error = h.auth.start_login(LoginFlow::DeviceCode).unwrap_err();
        assert_eq!(error.code, expected);
        assert!(!format!("{error:?}{error}").contains("secret-reflection"));
        workers_done(&h.auth);
        assert!(h
            .store
            .value
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .grant
            .is_none());
    }
}

#[test]
fn replacement_cancel_failure_and_late_success_leave_old_grant() {
    for disconnect in [false, true] {
        let h = Harness::new();
        let old = seed(&h.store, NOW + 3600).grant.unwrap().id;
        h.script.response(
            Endpoint::DeviceCode,
            200,
            json!({"device_auth_id": "synthetic-id", "user_code": "SYN"}),
        );
        h.script.response(
            Endpoint::DevicePoll,
            200,
            json!({"authorization_code": "synthetic-code", "code_verifier": "synthetic-verifier"}),
        );
        let (entered, release) = h.script.blocked(
            Endpoint::Token,
            200,
            tokens(
                "synthetic-replacement",
                NOW + 3600,
                Some("synthetic-new-refresh"),
            ),
        );
        let pending = h.auth.start_login(LoginFlow::DeviceCode).unwrap();
        entered.recv_timeout(Duration::from_secs(2)).unwrap();
        let snapshot = h.auth.get_connection();
        assert_eq!(snapshot.state, ConnectionState::Connected);
        assert!(snapshot.pending_login.is_some());
        if disconnect {
            h.peer().disconnect().unwrap();
        } else {
            h.auth.cancel_login(&pending.login_id).unwrap();
        }
        release.send(()).unwrap();
        workers_done(&h.auth);
        let env = h.store.value.lock().unwrap().clone().unwrap();
        assert!(env.pending.is_none());
        if disconnect {
            assert!(env.grant.is_none());
        } else {
            assert_eq!(env.grant.unwrap().id, old);
        }
    }
}

#[test]
fn failed_login_save_never_reports_connected_or_uses_undurable_pair() {
    let h = Harness::new();
    h.login_script();
    h.store.fail_saves.lock().unwrap().insert(3); // reserve, presentation, final pair
    let _ = h.auth.start_login(LoginFlow::DeviceCode);
    workers_done(&h.auth);
    assert_eq!(
        h.auth.session.lock().unwrap().snapshot.state,
        ConnectionState::Unavailable
    );
    assert!(h
        .store
        .value
        .lock()
        .unwrap()
        .as_ref()
        .unwrap()
        .grant
        .is_none());
    assert_eq!(h.authorize().unwrap_err().code, AuthErrorCode::NotConnected);
}

#[test]
fn replacement_fences_old_completion_only_when_the_new_grant_is_durable() {
    for (fail, ambiguous) in [(true, false), (true, true), (false, false)] {
        let h = Harness::new();
        let old = seed(&h.store, NOW + 3600).grant.unwrap();
        let stamp = h.authorize().unwrap().stamp;
        h.login_script();
        if fail {
            let saves = if ambiguous {
                &h.store.ambiguous_saves
            } else {
                &h.store.fail_saves
            };
            saves.lock().unwrap().insert(3); // reserve, presentation, final pair
        }
        h.auth.start_login(LoginFlow::DeviceCode).unwrap();
        workers_done(&h.auth);
        if fail {
            let snapshot = h.auth.session.lock().unwrap().snapshot.clone();
            assert_eq!(snapshot.state, ConnectionState::Unavailable);
            assert_eq!(
                snapshot.error_code,
                Some(AuthErrorCode::LocalStoreUnavailable)
            );
        }
        let stored = h.store.value.lock().unwrap().clone().unwrap();
        assert!(stored.pending.is_none());
        if fail && !ambiguous {
            let grant = stored.grant.unwrap();
            assert_eq!(grant.id, old.id);
            assert_eq!(grant.pair_revision, old.pair_revision);
            assert_eq!(completion(&h.auth, &stamp), CompletionStatus::Accepted);
        } else {
            assert_ne!(stored.grant.unwrap().id, old.id);
            // A store error after an actual commit must still reject the old response,
            // even though this host did not advance its in-memory fence.
            assert_eq!(completion(&h.auth, &stamp), CompletionStatus::Invalidated);
        }
        assert_eq!(h.auth.session.lock().unwrap().fence, u64::from(!fail));
    }
}

fn failed_login_with_deferred_cleanup() -> Harness {
    let h = Harness::new();
    seed(&h.store, NOW + 3600);
    h.login_script();
    // Both the final replacement and its cleanup fail. No token pair may be
    // retained by the recovery marker after this worker has exited.
    h.store.fail_saves.lock().unwrap().extend([3, 4]);
    h.auth.start_login(LoginFlow::DeviceCode).unwrap();
    workers_done(&h.auth);
    let session = h.auth.session.lock().unwrap();
    assert!(session.work.is_none());
    let cleanup = session.cleanup.as_ref().unwrap();
    assert_eq!(cleanup.generation, session.generation);
    assert_eq!(cleanup.owner_instance, h.auth.instance);
    let env = h.store.value.lock().unwrap().clone().unwrap();
    assert_eq!(env.pending.as_ref().unwrap().id, cleanup.id);
    assert_eq!(env.epoch, cleanup.epoch);
    assert_eq!(session.snapshot.state, ConnectionState::Unavailable);
    drop(session);
    h
}

#[test]
fn failed_initial_reservation_never_starts_oauth_and_next_start_reconciles_it() {
    for connected in [false, true] {
        for ambiguous in [false, true] {
            let h = Harness::new();
            let old = connected.then(|| seed(&h.store, NOW + 3600).grant.unwrap().id);
            let failures = if ambiguous {
                &h.store.ambiguous_saves
            } else {
                &h.store.fail_saves
            };
            failures.lock().unwrap().insert(1);
            assert_eq!(
                h.auth.start_login(LoginFlow::DeviceCode).unwrap_err().code,
                AuthErrorCode::LocalStoreUnavailable
            );
            assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
            let session = h.auth.session.lock().unwrap();
            assert_eq!(session.login_workers, 0);
            assert!(session.work.is_none());
            assert_eq!(session.snapshot.state, ConnectionState::Unavailable);
            assert_eq!(
                session.snapshot.error_code,
                Some(AuthErrorCode::LocalStoreUnavailable)
            );
            let cleanup = session.cleanup.clone().unwrap();
            assert_eq!(cleanup.generation, session.generation);
            assert_eq!(cleanup.owner_instance, h.auth.instance);
            drop(session);
            let before = h.store.value.lock().unwrap().clone();
            assert_eq!(
                before
                    .as_ref()
                    .and_then(|env| env.grant.as_ref().map(|g| g.id.clone())),
                old
            );
            if ambiguous {
                let pending = before.unwrap().pending.unwrap();
                assert_eq!(pending.id, cleanup.id);
                assert_eq!(pending.epoch, cleanup.epoch);
            }

            // No status query or cancellation is needed to recover the dead reservation.
            h.login_script();
            let next = h.auth.start_login(LoginFlow::DeviceCode).unwrap();
            assert_ne!(next.login_id, cleanup.id);
            workers_done(&h.auth);
            assert!(h.auth.session.lock().unwrap().cleanup.is_none());
            assert!(h
                .store
                .value
                .lock()
                .unwrap()
                .as_ref()
                .unwrap()
                .pending
                .is_none());
            assert_eq!(h.script.calls.load(Ordering::Relaxed), 3);
        }
    }
}

#[test]
fn failed_presentation_save_cleans_pending_without_polling_or_hiding_custody_error() {
    for connected in [false, true] {
        for ambiguous in [false, true] {
            let h = Harness::new();
            let old = connected.then(|| seed(&h.store, NOW + 3600).grant.unwrap().id);
            let failures = if ambiguous {
                &h.store.ambiguous_saves
            } else {
                &h.store.fail_saves
            };
            failures.lock().unwrap().insert(2);
            h.script.response(
                Endpoint::DeviceCode,
                200,
                json!({"device_auth_id":"synthetic-id", "user_code":"SYN"}),
            );
            assert_eq!(
                h.auth.start_login(LoginFlow::DeviceCode).unwrap_err().code,
                AuthErrorCode::LocalStoreUnavailable
            );
            workers_done(&h.auth);
            let session = h.auth.session.lock().unwrap();
            assert!(session.work.is_none());
            assert!(session.cleanup.is_none());
            assert_eq!(session.snapshot.state, ConnectionState::Unavailable);
            assert_eq!(
                session.snapshot.error_code,
                Some(AuthErrorCode::LocalStoreUnavailable)
            );
            let stored = h.store.value.lock().unwrap().clone().unwrap();
            assert!(stored.pending.is_none());
            assert_eq!(stored.grant.as_ref().map(|g| g.id.clone()), old);
            assert_eq!(h.script.calls.load(Ordering::Relaxed), 1);
        }
    }
}

#[test]
fn shutdown_reconciles_preexisting_deferred_cleanup_and_failed_initial_reservations() {
    for reservation in [false, true] {
        let h = if reservation {
            let h = Harness::new();
            seed(&h.store, NOW + 3600);
            h.store.ambiguous_saves.lock().unwrap().insert(1);
            assert_eq!(
                h.auth.start_login(LoginFlow::DeviceCode).unwrap_err().code,
                AuthErrorCode::LocalStoreUnavailable
            );
            h
        } else {
            failed_login_with_deferred_cleanup()
        };
        let old = h
            .store
            .value
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .grant
            .clone()
            .unwrap();
        let generation = h.auth.session.lock().unwrap().generation;
        h.auth.shutdown();
        let session = h.auth.session.lock().unwrap();
        assert!(session.cleanup.is_none());
        assert!(session.work.is_none());
        assert_eq!(session.generation, generation + 1);
        assert_eq!(
            session.snapshot.error_code,
            Some(AuthErrorCode::SessionEnded)
        );
        drop(session);
        let env = h.store.value.lock().unwrap().clone().unwrap();
        assert!(env.pending.is_none());
        let grant = env.grant.unwrap();
        assert_eq!(grant.id, old.id);
        assert_eq!(grant.pair_revision, old.pair_revision);
        assert_eq!(
            h.script.calls.load(Ordering::Relaxed),
            if reservation { 0 } else { 3 }
        );
        h.login_script();
        let peer = h.peer();
        peer.start_login(LoginFlow::DeviceCode).unwrap();
        workers_done(&peer);
    }
}

#[test]
fn shutdown_reconciles_cleanup_produced_by_its_retiring_worker() {
    let h = Harness::new();
    let old = seed(&h.store, NOW + 3600).grant.unwrap();
    h.script.response(
        Endpoint::DeviceCode,
        200,
        json!({"device_auth_id":"synthetic-id", "user_code":"SYN"}),
    );
    h.script.response(
        Endpoint::DevicePoll,
        200,
        json!({"authorization_code":"synthetic-code", "code_verifier":"synthetic-verifier"}),
    );
    let (entered, release) = h.script.blocked(
        Endpoint::Token,
        200,
        tokens(
            "synthetic-account",
            NOW + 3600,
            Some("never-installed-pair"),
        ),
    );
    h.store.fail_saves.lock().unwrap().extend([3, 4]);
    h.auth.start_login(LoginFlow::DeviceCode).unwrap();
    entered.recv_timeout(Duration::from_secs(2)).unwrap();
    let auth = h.auth.clone();
    let shutdown = std::thread::spawn(move || auth.shutdown());
    eventually(|| h.auth.ended.load(Ordering::Acquire));
    release.send(()).unwrap();
    shutdown.join().unwrap();
    assert_eq!(h.store.saves.load(Ordering::Relaxed), 5);
    let session = h.auth.session.lock().unwrap();
    assert_eq!(session.login_workers, 0);
    assert!(session.cleanup.is_none());
    assert_eq!(
        session.snapshot.error_code,
        Some(AuthErrorCode::SessionEnded)
    );
    drop(session);
    let env = h.store.value.lock().unwrap().clone().unwrap();
    assert!(env.pending.is_none());
    let grant = env.grant.unwrap();
    assert_eq!(grant.id, old.id);
    assert_eq!(grant.pair_revision, old.pair_revision);
    assert_eq!(grant.refresh.0, old.refresh.0);
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 3);
    let peer = h.peer();
    h.login_script();
    peer.start_login(LoginFlow::DeviceCode).unwrap();
    workers_done(&peer);
}

#[test]
fn shutdown_waits_for_an_ordinary_peer_refresh_before_settling_deferred_pending() {
    let h = failed_login_with_deferred_cleanup();
    let old = h
        .store
        .value
        .lock()
        .unwrap()
        .as_ref()
        .unwrap()
        .grant
        .clone()
        .unwrap();
    let peer = h.peer();
    let stamp = peer
        .authorize_dispatch(None, &AtomicBool::new(false))
        .unwrap()
        .stamp;
    let (entered, release) = h.script.blocked(
        Endpoint::Token,
        200,
        tokens(
            "synthetic-account",
            NOW + 3600,
            Some("synthetic-peer-rotated"),
        ),
    );
    let refresh =
        std::thread::spawn(move || peer.authorize_dispatch(Some(&stamp), &AtomicBool::new(false)));
    entered.recv_timeout(Duration::from_secs(2)).unwrap();
    let (finished, wait) = mpsc::channel();
    let auth = h.auth.clone();
    let shutdown = std::thread::spawn(move || {
        auth.shutdown();
        finished.send(()).unwrap();
    });
    eventually(|| h.auth.ended.load(Ordering::Acquire));
    assert!(wait
        .recv_timeout(LOCK_WAIT + Duration::from_millis(500))
        .is_err());
    assert!(h
        .store
        .value
        .lock()
        .unwrap()
        .as_ref()
        .unwrap()
        .pending
        .is_some());
    release.send(()).unwrap();
    refresh.join().unwrap().unwrap();
    wait.recv_timeout(Duration::from_secs(3)).unwrap();
    shutdown.join().unwrap();
    let env = h.store.value.lock().unwrap().clone().unwrap();
    assert!(env.pending.is_none());
    let grant = env.grant.unwrap();
    assert_eq!(grant.id, old.id);
    assert_eq!(grant.pair_revision, old.pair_revision + 1);
    assert_eq!(grant.refresh.0, "synthetic-peer-rotated");
    assert!(h.auth.session.lock().unwrap().cleanup.is_none());
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 4);
}

#[test]
fn bounded_shutdown_cleanup_can_be_retried_after_custody_recovers() {
    let h = failed_login_with_deferred_cleanup();
    let before = h.store.value.lock().unwrap().clone().unwrap();
    let cleanup = h.auth.session.lock().unwrap().cleanup.clone().unwrap();
    *h.store.failure.lock().unwrap() = Some(GrantStoreError::Locked);
    let started = Instant::now();
    let (finished, wait) = mpsc::channel();
    let auth = h.auth.clone();
    let shutdown = std::thread::spawn(move || {
        auth.shutdown();
        finished.send(()).unwrap();
    });
    wait.recv_timeout(LOGIN_SETTLE_WAIT + Duration::from_secs(5))
        .unwrap();
    shutdown.join().unwrap();
    assert!(started.elapsed() >= LOGIN_SETTLE_WAIT);
    let session = h.auth.session.lock().unwrap();
    assert!(session.cleanup.as_ref() == Some(&cleanup));
    assert_eq!(
        session.snapshot.error_code,
        Some(AuthErrorCode::SessionEnded)
    );
    let generation = session.generation;
    drop(session);
    assert_eq!(
        h.store.value.lock().unwrap().as_ref().unwrap().revision,
        before.revision
    );
    *h.store.failure.lock().unwrap() = None;
    h.auth.shutdown();
    let session = h.auth.session.lock().unwrap();
    assert_eq!(session.generation, generation);
    assert!(session.cleanup.is_none());
    drop(session);
    let after = h.store.value.lock().unwrap().clone().unwrap();
    assert!(after.pending.is_none());
    assert_eq!(after.grant.unwrap().id, before.grant.unwrap().id);
    assert!(h.peer().get_connection().pending_login.is_none());
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 3);
}

#[test]
fn shutdown_deferred_cleanup_preserves_newer_peer_attempts_and_grants() {
    for complete in [false, true] {
        let h = failed_login_with_deferred_cleanup();
        let old = h.auth.session.lock().unwrap().cleanup.clone().unwrap();
        let peer = h.peer();
        assert_eq!(
            peer.cancel_login(&old.id).unwrap(),
            CancelOutcome::Cancelled
        );
        let blocked = if complete {
            h.login_script();
            peer.start_login(LoginFlow::DeviceCode).unwrap();
            workers_done(&peer);
            None
        } else {
            let (entered, release) = h.script.blocked(
                Endpoint::DeviceCode,
                200,
                json!({"device_auth_id":"synthetic-new-id", "user_code":"NEW"}),
            );
            let work = peer.begin_login(LoginFlow::DeviceCode).unwrap();
            entered.recv_timeout(Duration::from_secs(2)).unwrap();
            Some((work, release))
        };
        let before = h.store.value.lock().unwrap().clone().unwrap();
        let writes = h.store.saves.load(Ordering::Relaxed);
        h.auth.shutdown();
        assert!(h.auth.session.lock().unwrap().cleanup.is_none());
        assert_eq!(h.store.saves.load(Ordering::Relaxed), writes);
        let after = h.store.value.lock().unwrap().clone().unwrap();
        assert_eq!(after.revision, before.revision);
        assert_eq!(
            after.grant.as_ref().unwrap().id,
            before.grant.as_ref().unwrap().id
        );
        assert_eq!(
            after.pending.as_ref().map(|p| &p.id),
            before.pending.as_ref().map(|p| &p.id)
        );
        if let Some((work, release)) = blocked {
            peer.cancel_login(&work.id).unwrap();
            release.send(()).unwrap();
            workers_done(&peer);
        }
    }
}

#[test]
fn early_cancel_and_open_returns_publish_authoritative_recovery_without_status_queries() {
    for operation in [0, 1, 2] {
        let h = failed_login_with_deferred_cleanup();
        let (id, before) = {
            let session = h.auth.session.lock().unwrap();
            (
                session.cleanup.as_ref().unwrap().id.clone(),
                session.snapshot.clone(),
            )
        };
        let watch = h.auth.subscribe();
        assert!(watch.latest().is_none());
        match operation {
            0 => assert_eq!(
                h.auth.cancel_login(&id).unwrap(),
                CancelOutcome::AlreadyFinished
            ),
            1 => assert_eq!(
                h.auth.cancel_login("stale-login-id").unwrap(),
                CancelOutcome::NotCurrent
            ),
            _ => assert_eq!(
                h.auth.open_login(&id).unwrap_err().code,
                AuthErrorCode::NotCurrent
            ),
        }
        let observed = watch.latest().unwrap();
        assert!(observed.revision > before.revision);
        assert_eq!(observed.state, ConnectionState::Connected);
        assert!(observed.pending_login.is_none());
        assert!(observed.error_code.is_none());
        assert!(h.auth.session.lock().unwrap().cleanup.is_none());
        assert_eq!(h.script.calls.load(Ordering::Relaxed), 3);
    }
}

#[test]
fn failed_authoritative_open_read_publishes_typed_unavailability() {
    let h = Harness::new();
    seed(&h.store, NOW + 3600);
    assert_eq!(h.auth.get_connection().state, ConnectionState::Connected);
    let watch = h.auth.subscribe();
    *h.store.failure.lock().unwrap() = Some(GrantStoreError::Locked);
    assert_eq!(
        h.auth.open_login("stale-login-id").unwrap_err().code,
        AuthErrorCode::StoreLocked
    );
    let observed = watch.latest().unwrap();
    assert_eq!(observed.state, ConnectionState::Unavailable);
    assert_eq!(observed.error_code, Some(AuthErrorCode::StoreLocked));
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
}

#[test]
fn joining_existing_login_publishes_custody_recovery_without_a_status_query() {
    let h = Harness::new();
    h.script.response(
        Endpoint::DeviceCode,
        200,
        json!({"device_auth_id":"synthetic-id", "user_code":"SYN"}),
    );
    let (entered, release) = h.script.blocked(Endpoint::DevicePoll, 404, Value::Null);
    let started = h.auth.start_login(LoginFlow::DeviceCode).unwrap();
    entered.recv_timeout(Duration::from_secs(2)).unwrap();
    *h.store.failure.lock().unwrap() = Some(GrantStoreError::Locked);
    assert_eq!(h.auth.get_connection().state, ConnectionState::Unavailable);
    *h.store.failure.lock().unwrap() = None;
    let watch = h.auth.subscribe();
    let joined = h.auth.start_login(LoginFlow::DeviceCode).unwrap();
    assert_eq!(joined.login_id, started.login_id);
    let observed = watch.latest().unwrap();
    assert_eq!(observed.state, ConnectionState::Connecting);
    assert_eq!(observed.pending_login.unwrap().login_id, started.login_id);
    assert!(observed.error_code.is_none());
    h.auth.cancel_login(&started.login_id).unwrap();
    release.send(()).unwrap();
    workers_done(&h.auth);
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 2);
}

#[test]
fn failed_cleanup_is_reconciled_by_the_next_read_or_start_without_dead_pending() {
    for start in [false, true] {
        let h = failed_login_with_deferred_cleanup();
        if start {
            h.login_script();
            h.auth.start_login(LoginFlow::DeviceCode).unwrap();
            workers_done(&h.auth);
            assert_eq!(h.script.calls.load(Ordering::Relaxed), 6);
        } else {
            let connection = h.auth.get_connection();
            assert_eq!(connection.state, ConnectionState::Connected);
            assert!(connection.pending_login.is_none());
            assert_eq!(h.script.calls.load(Ordering::Relaxed), 3);
        }
        assert!(h.auth.session.lock().unwrap().cleanup.is_none());
        assert!(h
            .store
            .value
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .pending
            .is_none());
    }
}

#[test]
fn an_ambiguous_login_save_cannot_overwrite_a_newer_peers_connected_presentation() {
    struct FailedWriteClock {
        base: Arc<TestClock>,
        store: Arc<SyntheticStore>,
        paused: AtomicBool,
        entered: mpsc::Sender<()>,
        release: Mutex<mpsc::Receiver<()>>,
    }
    impl Clock for FailedWriteClock {
        fn now(&self) -> u64 {
            if self.store.saves.load(Ordering::Acquire) >= 3
                && !self.paused.swap(true, Ordering::AcqRel)
            {
                self.entered.send(()).unwrap();
                self.release
                    .lock()
                    .unwrap()
                    .recv_timeout(Duration::from_secs(5))
                    .unwrap();
            }
            self.base.now()
        }
        fn wait(&self, duration: Duration, cancel: &AtomicBool) {
            self.base.wait(duration, cancel);
        }
    }
    let h = Harness::new();
    seed(&h.store, NOW + 3600);
    h.store.ambiguous_saves.lock().unwrap().insert(3);
    let (sent, entered) = mpsc::channel();
    let (release, wait) = mpsc::channel();
    let auth = service(
        h.dir.path(),
        h.store.clone(),
        h.script.clone(),
        Arc::new(FailedWriteClock {
            base: h.clock.clone(),
            store: h.store.clone(),
            paused: AtomicBool::new(false),
            entered: sent,
            release: Mutex::new(wait),
        }),
    );
    h.login_script();
    auth.start_login(LoginFlow::DeviceCode).unwrap();
    entered.recv_timeout(Duration::from_secs(2)).unwrap();
    let peer = h.peer();
    h.script.response(
        Endpoint::DeviceCode,
        200,
        json!({"device_auth_id":"synthetic-peer-id","user_code":"PEER"}),
    );
    h.script.response(Endpoint::DevicePoll, 200, json!({"authorization_code":"synthetic-peer-code","code_verifier":"synthetic-peer-verifier"}));
    let mut body = tokens(
        "synthetic-peer-account",
        NOW + 3600,
        Some("synthetic-peer-token"),
    );
    let mut metadata = claims("synthetic-peer-account", NOW + 3600);
    metadata["email"] = json!("new-peer@example.test");
    body["access_token"] = json!(token(metadata));
    h.script.response(Endpoint::Token, 200, body);
    peer.start_login(LoginFlow::DeviceCode).unwrap();
    workers_done(&peer);
    let watch = auth.subscribe();
    release.send(()).unwrap();
    workers_done(&auth);
    let observed = watch.latest().unwrap();
    assert_eq!(observed.state, ConnectionState::Connected);
    assert_eq!(
        observed.account_label.as_deref(),
        Some("new-peer@example.test")
    );
    assert!(observed.error_code.is_none());
    assert!(observed.pending_login.is_none());
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 6);
}

#[test]
fn deferred_cleanup_cannot_clear_a_newer_peer_attempt_or_change_its_presentation() {
    let h = failed_login_with_deferred_cleanup();
    let old_id = h
        .auth
        .session
        .lock()
        .unwrap()
        .cleanup
        .as_ref()
        .unwrap()
        .id
        .clone();
    let peer = h.peer();
    assert_eq!(
        peer.cancel_login(&old_id).unwrap(),
        CancelOutcome::Cancelled
    );
    let (entered, release) = h.script.blocked(
        Endpoint::DeviceCode,
        200,
        json!({"device_auth_id": "synthetic-new-attempt", "user_code": "NEW"}),
    );
    let new = peer.begin_login(LoginFlow::DeviceCode).unwrap();
    entered.recv_timeout(Duration::from_secs(2)).unwrap();
    let observed = h.auth.get_connection();
    assert_eq!(observed.state, ConnectionState::Connected);
    assert_eq!(observed.pending_login.unwrap().login_id, new.id);
    assert!(observed.error_code.is_none());
    assert!(h.auth.session.lock().unwrap().cleanup.is_none());
    assert_eq!(
        h.store
            .value
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .pending
            .as_ref()
            .unwrap()
            .id,
        new.id
    );
    assert_ne!(
        h.auth.cancel_login(&old_id).unwrap(),
        CancelOutcome::Cancelled
    );
    assert_eq!(
        peer.cancel_login(&new.id).unwrap(),
        CancelOutcome::Cancelled
    );
    release.send(()).unwrap();
    workers_done(&peer);
}

#[test]
fn terminal_cleanup_has_a_finite_window_and_reconciles_after_longer_lock_failure() {
    let h = Harness::new();
    let old = seed(&h.store, NOW + 3600).grant.unwrap().id;
    let (entered, release) = h.script.blocked(
        Endpoint::DeviceCode,
        200,
        json!({"device_auth_id": "synthetic-id", "user_code": "SYN"}),
    );
    let work = h.auth.begin_login(LoginFlow::DeviceCode).unwrap();
    entered.recv_timeout(Duration::from_secs(2)).unwrap();
    let transaction = h
        .auth
        .storage
        .as_ref()
        .unwrap()
        .lock
        .acquire(LOCK_WAIT, || false)
        .unwrap();
    let auth = h.auth.clone();
    let id = work.id.clone();
    let cancelling = std::thread::spawn(move || auth.cancel_login(&id));
    eventually(|| work.cancel.load(Ordering::Acquire));
    let started = Instant::now();
    release.send(()).unwrap();
    assert_eq!(
        cancelling.join().unwrap().unwrap_err().code,
        AuthErrorCode::AuthBusy
    );
    while h.auth.session.lock().unwrap().login_workers != 0 {
        assert!(started.elapsed() < LOGIN_SETTLE_WAIT + Duration::from_secs(2));
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(started.elapsed() >= LOGIN_SETTLE_WAIT);
    let session = h.auth.session.lock().unwrap();
    assert!(session.work.is_none());
    assert_eq!(session.cleanup.as_ref().unwrap().id, work.id);
    assert_eq!(session.snapshot.error_code, Some(AuthErrorCode::AuthBusy));
    drop(session);
    drop(transaction);
    let snapshot = h.auth.get_connection();
    assert_eq!(snapshot.state, ConnectionState::Connected);
    assert!(snapshot.pending_login.is_none());
    assert_eq!(
        h.store
            .value
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .grant
            .as_ref()
            .unwrap()
            .id,
        old
    );
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 1);
}

#[test]
fn missing_store_locked_store_and_unavailable_updates_have_increasing_revisions() {
    let absent = ChatGptAuth::unavailable("device", 9);
    assert_eq!(absent.get_connection().state, ConnectionState::Unavailable);
    assert_eq!(
        absent.start_login(LoginFlow::Browser).unwrap_err().code,
        AuthErrorCode::LocalStoreUnavailable
    );
    let h = Harness::new();
    seed(&h.store, NOW + 3600);
    let connected = h.auth.get_connection();
    let watch = h.auth.subscribe();
    *h.store.failure.lock().unwrap() = Some(GrantStoreError::Locked);
    let locked = h.auth.get_connection();
    assert_eq!(locked.error_code, Some(AuthErrorCode::StoreLocked));
    assert!(locked.revision > connected.revision);
    *h.store.failure.lock().unwrap() = Some(GrantStoreError::Unavailable);
    let unavailable = h.auth.get_connection();
    assert!(unavailable.revision > locked.revision);
    assert_eq!(watch.latest().unwrap(), unavailable);
    assert!(unavailable.account_label.is_none());
    assert_eq!(
        h.authorize().unwrap_err().code,
        AuthErrorCode::LocalStoreUnavailable
    );
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
}

#[test]
fn status_reads_expired_access_as_connected_without_network() {
    let h = Harness::new();
    seed(&h.store, NOW + 1);
    h.clock.now.store(NOW + 100, Ordering::Release);
    assert_eq!(h.auth.get_connection().state, ConnectionState::Connected);
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
}

#[test]
fn two_instances_single_refresh_and_peer_adoption() {
    let h = Harness::new();
    seed(&h.store, NOW + 10);
    let (entered, release) = h.script.blocked(
        Endpoint::Token,
        200,
        tokens("synthetic-account", NOW + 3600, Some("synthetic-rotated")),
    );
    let auth = h.auth.clone();
    let first = std::thread::spawn(move || auth.authorize_dispatch(None, &AtomicBool::new(false)));
    entered.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(matches!(
        h.store
            .value
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .grant
            .as_ref()
            .unwrap()
            .state,
        GrantState::RefreshInflight { .. }
    ));
    let peer = h.peer();
    let second = std::thread::spawn(move || peer.authorize_dispatch(None, &AtomicBool::new(false)));
    release.send(()).unwrap();
    let one = first.join().unwrap().unwrap();
    let two = second.join().unwrap().unwrap();
    assert_eq!(one.access.0, two.access.0);
    assert_eq!(one.stamp.pair_revision, 2);
    assert_eq!(two.stamp.pair_revision, 2);
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 1);
    assert_eq!(
        h.store
            .value
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .grant
            .as_ref()
            .unwrap()
            .refresh
            .0,
        "synthetic-rotated"
    );
}

#[test]
fn refresh_omitted_refresh_token_preserves_valid_old_pair_only() {
    let h = Harness::new();
    seed(&h.store, NOW + 1);
    h.script.response(
        Endpoint::Token,
        200,
        tokens("synthetic-account", NOW + 3600, None),
    );
    assert!(h.authorize().is_ok());
    assert_eq!(
        h.store
            .value
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .grant
            .as_ref()
            .unwrap()
            .refresh
            .0,
        "synthetic-refresh-1"
    );
    assert!(parse_grant(&tokens("synthetic-account", NOW + 3600, None), NOW, None).is_err());
}

#[test]
fn refresh_marker_save_failure_sends_nothing_and_pair_save_failure_never_replays() {
    for failure_at in [1, 2] {
        let h = Harness::new();
        seed(&h.store, NOW + 1);
        h.store.fail_saves.lock().unwrap().insert(failure_at);
        if failure_at == 2 {
            h.script.response(
                Endpoint::Token,
                200,
                tokens("synthetic-account", NOW + 3600, Some("synthetic-rotated")),
            );
        }
        assert_eq!(
            h.authorize().unwrap_err().code,
            AuthErrorCode::LocalStoreUnavailable
        );
        assert_eq!(h.script.calls.load(Ordering::Relaxed), failure_at - 1);
        if failure_at == 2 {
            let peer = h.peer();
            assert_eq!(
                peer.authorize_dispatch(None, &AtomicBool::new(false))
                    .unwrap_err()
                    .code,
                AuthErrorCode::RefreshUncertain
            );
            assert_eq!(h.script.calls.load(Ordering::Relaxed), 1);
            assert!(h
                .store
                .value
                .lock()
                .unwrap()
                .as_ref()
                .unwrap()
                .grant
                .as_ref()
                .unwrap()
                .refresh
                .0
                .is_empty());
        }
    }
}

#[test]
fn refresh_transport_unknown_errors_and_malformed_success_are_never_retried() {
    for case in 0..5 {
        let h = Harness::new();
        seed(&h.store, NOW + 1);
        match case {
            0 => h
                .script
                .add(Box::new(|_, _| Err(TransportFailure::Uncertain))),
            1 => h.script.response(
                Endpoint::Token,
                200,
                json!({"access_token": "synthetic-malformed", "refresh_token": "synthetic-new"}),
            ),
            2 => h
                .script
                .response(Endpoint::Token, 500, json!({"error": "synthetic-secret"})),
            3 => h
                .script
                .response(Endpoint::Token, 429, json!({"error": "unknown-rate-limit"})),
            _ => h.script.response(
                Endpoint::Token,
                302,
                json!({"location": "https://synthetic.invalid/"}),
            ),
        }
        assert_eq!(
            h.authorize().unwrap_err().code,
            AuthErrorCode::RefreshUncertain
        );
        assert_eq!(
            h.peer()
                .authorize_dispatch(None, &AtomicBool::new(false))
                .unwrap_err()
                .code,
            AuthErrorCode::RefreshUncertain
        );
        assert_eq!(h.script.calls.load(Ordering::Relaxed), 1);
        assert_eq!(
            h.auth.get_connection().state,
            ConnectionState::ReauthRequired
        );
    }
}

#[test]
fn definitively_unsent_failure_can_restore_ready_but_rejected_or_changed_principal_cannot() {
    for case in 0..3 {
        let h = Harness::new();
        seed(&h.store, NOW + 1);
        let expected = match case {
            0 => {
                h.script
                    .add(Box::new(|_, _| Err(TransportFailure::BeforeSend)));
                AuthErrorCode::AuthNetwork
            }
            1 => {
                h.script.response(
                    Endpoint::Token,
                    400,
                    json!({"error": "invalid_grant", "error_description": "synthetic-secret"}),
                );
                AuthErrorCode::GrantRejected
            }
            _ => {
                h.script.response(
                    Endpoint::Token,
                    200,
                    tokens(
                        "synthetic-other-principal",
                        NOW + 3600,
                        Some("synthetic-rotated"),
                    ),
                );
                AuthErrorCode::AccountChanged
            }
        };
        assert_eq!(h.authorize().unwrap_err().code, expected);
        assert_eq!(
            h.auth.get_connection().state,
            if case == 0 {
                ConnectionState::Connected
            } else {
                ConnectionState::ReauthRequired
            }
        );
    }
}

#[test]
fn pending_login_revision_does_not_suppress_forced_refresh_and_peer_pair_does() {
    let h = Harness::new();
    seed(&h.store, NOW + 3600);
    let stamp = h.authorize().unwrap().stamp;
    // An unrelated pending-login mutation changes only the envelope revision.
    {
        let mut value = h.store.value.lock().unwrap();
        let env = value.as_mut().unwrap();
        env.bump().unwrap();
        env.fence().unwrap();
    }
    h.script.response(
        Endpoint::Token,
        200,
        tokens("synthetic-account", NOW + 7200, Some("synthetic-rotated")),
    );
    let fresh = h
        .auth
        .authorize_dispatch(Some(&stamp), &AtomicBool::new(false))
        .unwrap();
    assert_eq!(fresh.stamp.pair_revision, 2);
    let adopted = h
        .peer()
        .authorize_dispatch(Some(&stamp), &AtomicBool::new(false))
        .unwrap();
    assert_eq!(adopted.stamp.pair_revision, 2);
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 1);
    h.store
        .value
        .lock()
        .unwrap()
        .as_mut()
        .unwrap()
        .grant
        .as_mut()
        .unwrap()
        .id = random_id();
    assert_eq!(
        h.auth
            .authorize_dispatch(Some(&stamp), &AtomicBool::new(false))
            .unwrap_err()
            .code,
        AuthErrorCode::AccountChanged
    );
}

#[test]
fn peer_disconnect_and_replacement_fence_future_dispatch_and_completions() {
    let h = Harness::new();
    seed(&h.store, NOW + 3600);
    let stamp = h.authorize().unwrap().stamp;
    assert_eq!(completion(&h.auth, &stamp), CompletionStatus::Accepted);
    h.peer().disconnect().unwrap();
    assert_eq!(completion(&h.auth, &stamp), CompletionStatus::Invalidated);
    assert_eq!(h.authorize().unwrap_err().code, AuthErrorCode::NotConnected);
    let tombstone = h.store.value.lock().unwrap().clone().unwrap();
    assert!(tombstone.epoch > 0);
    assert!(tombstone.grant.is_none());
    seed(&h.store, NOW + 3600);
    assert_eq!(completion(&h.auth, &stamp), CompletionStatus::Invalidated);
}

#[test]
fn dispatch_and_completion_publish_observed_peer_changes_without_a_status_query() {
    for replacement in [false, true] {
        for completing in [false, true] {
            let h = Harness::new();
            seed(&h.store, NOW + 3600);
            let stamp = h.authorize().unwrap().stamp;
            let watch = h.auth.subscribe();
            let connected = h.auth.get_connection();
            assert_eq!(connected.state, ConnectionState::Connected);
            watch.latest();
            let peer = h.peer();
            if replacement {
                h.script.response(
                    Endpoint::DeviceCode,
                    200,
                    json!({"device_auth_id": "synthetic-id", "user_code": "SYN"}),
                );
                h.script.response(Endpoint::DevicePoll, 200, json!({"authorization_code": "synthetic-code", "code_verifier": "synthetic-verifier"}));
                let mut body = tokens(
                    "synthetic-replacement",
                    NOW + 3600,
                    Some("synthetic-new-refresh"),
                );
                let mut metadata = claims("synthetic-replacement", NOW + 3600);
                metadata["email"] = json!("replacement@example.test");
                metadata[AUTH_CLAIMS]["chatgpt_plan_type"] = json!("replacement-plan");
                body["access_token"] = json!(token(metadata));
                h.script.response(Endpoint::Token, 200, body);
                peer.start_login(LoginFlow::DeviceCode).unwrap();
                workers_done(&peer);
            } else {
                peer.disconnect().unwrap();
            }
            assert!(watch.latest().is_none());
            if completing {
                assert_eq!(completion(&h.auth, &stamp), CompletionStatus::Invalidated);
            } else {
                assert_eq!(
                    h.auth
                        .authorize_dispatch(Some(&stamp), &AtomicBool::new(false))
                        .unwrap_err()
                        .code,
                    if replacement {
                        AuthErrorCode::AccountChanged
                    } else {
                        AuthErrorCode::NotConnected
                    }
                );
            }
            let observed = watch
                .latest()
                .expect("authoritative read must publish peer change");
            assert!(observed.revision > connected.revision);
            assert_eq!(observed.session_generation, connected.session_generation);
            assert!(observed.pending_login.is_none());
            assert_eq!(
                observed.state,
                if replacement {
                    ConnectionState::Connected
                } else {
                    ConnectionState::Disconnected
                }
            );
            assert_eq!(
                observed.account_label.as_deref(),
                replacement.then_some("replacement@example.test")
            );
            assert_eq!(
                observed.plan_type.as_deref(),
                replacement.then_some("replacement-plan")
            );
            assert!(!serde_json::to_string(&observed)
                .unwrap()
                .contains("synthetic-new-refresh"));
        }
    }
}

#[test]
fn completion_busy_retains_the_same_response_until_normal_refresh_settles() {
    let h = Harness::new();
    seed(&h.store, NOW + 150);
    let stamp = h.authorize().unwrap().stamp;
    h.clock.now.store(NOW + 31, Ordering::Release);
    let (entered, release) = h.script.blocked(
        Endpoint::Token,
        200,
        tokens("synthetic-account", NOW + 3600, Some("synthetic-rotated")),
    );
    let peer = h.peer();
    let refresh =
        std::thread::spawn(move || peer.authorize_dispatch(None, &AtomicBool::new(false)));
    entered.recv_timeout(Duration::from_secs(2)).unwrap();
    let response = "successful synthetic inference response";
    let deadline = Instant::now() + Duration::from_secs(15);
    let started = Instant::now();
    assert_eq!(
        h.auth
            .check_completion(&stamp, &AtomicBool::new(false), deadline)
            .unwrap_err()
            .code,
        AuthErrorCode::AuthBusy
    );
    assert!(started.elapsed() >= LOCK_WAIT);
    release.send(()).unwrap();
    let fresh = refresh.join().unwrap().unwrap();
    assert_eq!(fresh.stamp.grant_id, stamp.grant_id);
    assert!(fresh.stamp.pair_revision > stamp.pair_revision);
    assert_eq!(
        h.auth
            .check_completion(&stamp, &AtomicBool::new(false), deadline)
            .unwrap(),
        CompletionStatus::Accepted
    );
    assert_eq!(response, "successful synthetic inference response");
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 1);
}

#[test]
fn completion_wait_is_cancellable_deadline_bounded_and_keeps_store_errors_typed() {
    let h = Harness::new();
    seed(&h.store, NOW + 3600);
    let stamp = h.authorize().unwrap().stamp;
    let lock = &h.auth.storage.as_ref().unwrap().lock;
    let transaction = lock.acquire(LOCK_WAIT, || false).unwrap();
    let deadline = Instant::now() + Duration::from_millis(100);
    assert_eq!(
        h.auth
            .check_completion(&stamp, &AtomicBool::new(false), deadline)
            .unwrap_err()
            .code,
        AuthErrorCode::RequestExpired
    );
    let cancel = Arc::new(AtomicBool::new(false));
    let signal = cancel.clone();
    let cancelled = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(100));
        signal.store(true, Ordering::Release);
    });
    let started = Instant::now();
    assert_eq!(
        h.auth
            .check_completion(&stamp, &cancel, Instant::now() + Duration::from_secs(10))
            .unwrap_err()
            .code,
        AuthErrorCode::Cancelled
    );
    cancelled.join().unwrap();
    assert!(started.elapsed() < Duration::from_secs(1));
    drop(transaction);
    *h.store.failure.lock().unwrap() = Some(GrantStoreError::Locked);
    assert_eq!(
        h.auth
            .check_completion(
                &stamp,
                &AtomicBool::new(false),
                Instant::now() + Duration::from_secs(10)
            )
            .unwrap_err()
            .code,
        AuthErrorCode::StoreLocked
    );
    *h.store.failure.lock().unwrap() = None;
    assert_eq!(completion(&h.auth, &stamp), CompletionStatus::Accepted);
    h.peer().disconnect().unwrap();
    assert_eq!(completion(&h.auth, &stamp), CompletionStatus::Invalidated);
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
}

#[test]
fn failed_disconnect_stays_locally_unavailable_until_durable_retry() {
    let h = Harness::new();
    seed(&h.store, NOW + 3600);
    let stamp = h.authorize().unwrap().stamp;
    h.store.fail_saves.lock().unwrap().insert(1);
    assert_eq!(
        h.auth.disconnect().unwrap_err().code,
        AuthErrorCode::DisconnectFailed
    );
    assert_eq!(completion(&h.auth, &stamp), CompletionStatus::Invalidated);
    assert_eq!(
        h.authorize().unwrap_err().code,
        AuthErrorCode::DisconnectFailed
    );
    assert_eq!(h.auth.get_connection().state, ConnectionState::Unavailable);
    h.auth.disconnect().unwrap();
    assert_eq!(h.auth.get_connection().state, ConnectionState::Disconnected);
}

#[test]
fn shutdown_waits_for_sent_refresh_to_settle_without_publishing_to_new_session() {
    let h = Harness::new();
    seed(&h.store, NOW + 1);
    let (entered, release) = h.script.blocked(
        Endpoint::Token,
        200,
        tokens("synthetic-account", NOW + 3600, Some("synthetic-rotated")),
    );
    let auth = h.auth.clone();
    let dispatch =
        std::thread::spawn(move || auth.authorize_dispatch(None, &AtomicBool::new(false)));
    entered.recv_timeout(Duration::from_secs(2)).unwrap();
    let auth = h.auth.clone();
    let shutdown = std::thread::spawn(move || auth.shutdown());
    eventually(|| h.auth.ended.load(Ordering::Acquire));
    assert!(!shutdown.is_finished());
    release.send(()).unwrap();
    shutdown.join().unwrap();
    assert_eq!(
        dispatch.join().unwrap().unwrap_err().code,
        AuthErrorCode::SessionEnded
    );
    let snapshot = h.auth.get_connection();
    assert_eq!(snapshot.state, ConnectionState::Unavailable);
    assert_eq!(snapshot.session_generation, 72);
    assert_eq!(h.peer().get_connection().state, ConnectionState::Connected);
    assert_eq!(
        h.store
            .value
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .grant
            .as_ref()
            .unwrap()
            .refresh
            .0,
        "synthetic-rotated"
    );
}

#[test]
fn shutdown_and_account_switch_cancel_login_and_never_commit_late_pair() {
    let h = Harness::new();
    h.script.response(
        Endpoint::DeviceCode,
        200,
        json!({"device_auth_id": "synthetic-id", "user_code": "SYN"}),
    );
    h.script.response(
        Endpoint::DevicePoll,
        200,
        json!({"authorization_code": "synthetic-code", "code_verifier": "synthetic-verifier"}),
    );
    let (entered, release) = h.script.blocked(
        Endpoint::Token,
        200,
        tokens("synthetic-account", NOW + 3600, Some("synthetic-rotated")),
    );
    h.auth.start_login(LoginFlow::DeviceCode).unwrap();
    entered.recv_timeout(Duration::from_secs(2)).unwrap();
    let auth = h.auth.clone();
    let shutdown = std::thread::spawn(move || auth.shutdown());
    eventually(|| h.auth.ended.load(Ordering::Acquire));
    release.send(()).unwrap();
    shutdown.join().unwrap();
    let env = h.store.value.lock().unwrap().clone().unwrap();
    assert!(env.grant.is_none());
    assert!(env.pending.is_none());
    assert_eq!(
        h.auth.start_login(LoginFlow::DeviceCode).unwrap_err().code,
        AuthErrorCode::SessionEnded
    );
    assert_eq!(
        h.peer().get_connection().state,
        ConnectionState::Disconnected
    );
}

#[test]
fn access_expiry_bounds_identity_conflicts_and_residency_fields() {
    let mut body = tokens("synthetic-account", NOW + 1800, Some("synthetic-refresh"));
    assert_eq!(
        parse_grant(&body, NOW, None).unwrap().expires_at,
        NOW + 1800
    );
    body["expires_in"] = json!(60);
    assert_eq!(parse_grant(&body, NOW, None).unwrap().expires_at, NOW + 60);
    body.as_object_mut().unwrap().remove("expires_in");
    assert_eq!(
        parse_grant(&body, NOW, None).unwrap().expires_at,
        NOW + 1800
    );
    let mut c = claims("synthetic-account", NOW + 1800);
    c.as_object_mut().unwrap().remove("exp");
    body["access_token"] = json!(token(c.clone()));
    assert_eq!(
        parse_grant(&body, NOW, None).err(),
        Some(AuthErrorCode::UnsupportedTokenShape)
    );
    body["expires_in"] = json!(60);
    assert!(parse_grant(&body, NOW, None).is_ok());
    c[AUTH_CLAIMS]["chatgpt_data_residency"] = json!("eu");
    c[AUTH_CLAIMS]["chatgpt_compute_residency"] = json!("us");
    body["access_token"] = json!(token(c.clone()));
    assert_eq!(
        parse_grant(&body, NOW, None).unwrap().residency.as_deref(),
        Some("eu")
    );
    c[AUTH_CLAIMS]
        .as_object_mut()
        .unwrap()
        .remove("chatgpt_data_residency");
    body["access_token"] = json!(token(c.clone()));
    assert_eq!(
        parse_grant(&body, NOW, None).unwrap().residency.as_deref(),
        Some("us")
    );
    c[AUTH_CLAIMS]["chatgpt_compute_residency"] = json!("eu");
    body["id_token"] = json!(token(c));
    assert_eq!(
        parse_grant(&body, NOW, None).err(),
        Some(AuthErrorCode::UnsupportedTokenShape)
    );
    body["id_token"] = json!(token(claims("different-account", NOW + 1800)));
    assert_eq!(
        parse_grant(&body, NOW, None).err(),
        Some(AuthErrorCode::UnsupportedTokenShape)
    );
    for field in ["iss", "sub"] {
        let mut c = claims("synthetic-account", NOW + 1800);
        c[field] = Value::Null;
        body.as_object_mut().unwrap().remove("id_token");
        body["access_token"] = json!(token(c));
        assert_eq!(
            parse_grant(&body, NOW, None).err(),
            Some(AuthErrorCode::UnsupportedTokenShape)
        );
    }
}

#[test]
fn malformed_store_wrong_slot_and_unsupported_schema_fail_closed() {
    for case in 0..4 {
        let h = Harness::new();
        seed(&h.store, NOW + 3600);
        {
            let mut value = h.store.value.lock().unwrap();
            let env = value.as_mut().unwrap();
            match case {
                0 => env.schema_version = 999,
                1 => {
                    env.slot = GrantSlot::new("other", "profile", "any-account", "device").unwrap()
                }
                2 => env.grant.as_mut().unwrap().expires_at = 0,
                _ => env.grant.as_mut().unwrap().principal.account_id = "wrong-account".into(),
            }
        }
        assert_eq!(h.authorize().unwrap_err().code, AuthErrorCode::InvalidStore);
        assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
    }
    assert!(GrantEnvelope::decode(&vec![b'x'; 131_073]).is_err());
}

#[test]
fn callback_protocol_is_exact_and_never_reflects_codes() {
    let good = "GET /auth/callback?code=synthetic-code&state=synthetic-state HTTP/1.1\r\nHost: localhost:1455\r\n\r\n";
    assert_eq!(
        parse_callback(good, "synthetic-state").unwrap(),
        "synthetic-code"
    );
    for bad in [
        good.replace("GET", "POST"),
        good.replace("/auth/callback?", "/auth/callback/extra?"),
        good.replace("synthetic-state", "wrong-state"),
        good.replace("&state=", "&state=duplicate&state="),
        good.replace("localhost:1455", "attacker.invalid"),
        good.replace(" HTTP/1.1", "#fragment HTTP/1.1"),
        good.replace("\r\n\r\n", "\r\nHost: localhost:1455\r\n\r\n"),
    ] {
        assert_eq!(
            parse_callback(&bad, "synthetic-state"),
            Err(AuthErrorCode::CallbackMismatch)
        );
    }
    assert_eq!(
        parse_callback(
            &good.replace("code=synthetic-code", "error=access_denied"),
            "synthetic-state"
        ),
        Err(AuthErrorCode::ConsentDenied)
    );
}

#[test]
fn browser_pkce_success_fixed_redirect_and_sanitized_callback_response() {
    let _serial = BROWSER_TEST.lock().unwrap();
    let h = Harness::new();
    h.clock.advance.store(false, Ordering::Release);
    h.script.add(Box::new(|endpoint, body| {
        assert_eq!(endpoint, Endpoint::Token);
        match body {
            AuthBody::Form(pairs) => {
                assert!(pairs.contains(&("redirect_uri", BROWSER_REDIRECT.into())));
                assert!(pairs.contains(&("code", "synthetic-browser-code".into())));
                assert_eq!(
                    pairs
                        .iter()
                        .find(|(k, _)| *k == "code_verifier")
                        .unwrap()
                        .1
                        .len(),
                    64
                );
            }
            _ => panic!("wrong protocol"),
        }
        Ok(AuthReply {
            status: 200,
            body: tokens(
                "synthetic-account",
                NOW + 3600,
                Some("synthetic-browser-refresh"),
            ),
            retry_after: None,
        })
    }));
    let pending = h.auth.start_login(LoginFlow::Browser).unwrap();
    let LoginPresentation::Browser { authorization_url } = pending.presentation else {
        panic!("wrong presentation");
    };
    let url = url::Url::parse(&authorization_url).unwrap();
    assert_eq!(url.origin().ascii_serialization(), ISSUER);
    assert_eq!(url.path(), "/oauth/authorize");
    let params: std::collections::BTreeMap<_, _> = url.query_pairs().collect();
    assert_eq!(params["redirect_uri"], BROWSER_REDIRECT);
    assert_eq!(params["code_challenge_method"], "S256");
    let mut stream = std::net::TcpStream::connect((std::net::Ipv4Addr::LOCALHOST, 1455)).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    stream.write_all(format!("GET /auth/callback?code=synthetic-browser-code&state={} HTTP/1.1\r\nHost: localhost:1455\r\n\r\n", params["state"]).as_bytes()).unwrap();
    let mut response = String::new();
    stream.read_to_string(&mut response).unwrap();
    assert!(response.starts_with("HTTP/1.1 200"));
    assert!(!response.contains("synthetic-browser-code"));
    assert!(response.contains("default-src 'none'"));
    workers_done(&h.auth);
    assert_eq!(h.auth.get_connection().state, ConnectionState::Connected);
    assert!(std::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 1455)).is_ok());
}

#[test]
fn browser_port_collision_does_not_touch_owner_and_cancel_releases_port() {
    let _serial = BROWSER_TEST.lock().unwrap();
    let owner = std::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 1455)).unwrap();
    let h = Harness::new();
    assert_eq!(
        h.auth.start_login(LoginFlow::Browser).unwrap_err().code,
        AuthErrorCode::PortBusy
    );
    workers_done(&h.auth);
    assert_eq!(owner.local_addr().unwrap().port(), 1455);
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
    drop(owner);
    let started = h.auth.start_login(LoginFlow::Browser).unwrap();
    assert_eq!(
        h.auth.open_login("arbitrary-url-or-id").unwrap_err().code,
        AuthErrorCode::NotCurrent
    );
    h.auth.cancel_login(&started.login_id).unwrap();
    workers_done(&h.auth);
    assert!(std::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 1455)).is_ok());
}

#[test]
fn cancelled_browser_releases_listener_while_pending_cleanup_waits_for_refresh() {
    let _serial = BROWSER_TEST.lock().unwrap();
    let h = Harness::new();
    seed(&h.store, NOW + 3600);
    h.clock.advance.store(false, Ordering::Release);
    let stamp = h.authorize().unwrap().stamp;
    let login = h.auth.start_login(LoginFlow::Browser).unwrap();
    let (sent, entered) = mpsc::channel();
    let (release, wait) = mpsc::channel();
    h.script.add(Box::new(move |endpoint, _| {
        assert_eq!(endpoint, Endpoint::Token);
        sent.send(()).unwrap();
        wait.recv_timeout(Duration::from_secs(20)).unwrap();
        Ok(AuthReply {
            status: 200,
            body: tokens(
                "synthetic-account",
                NOW + 3600,
                Some("synthetic-peer-refresh"),
            ),
            retry_after: None,
        })
    }));
    let peer = h.peer();
    let refreshing =
        std::thread::spawn(move || peer.authorize_dispatch(Some(&stamp), &AtomicBool::new(false)));
    entered.recv_timeout(Duration::from_secs(2)).unwrap();
    let started = Instant::now();
    assert_eq!(
        h.auth.cancel_login(&login.login_id).unwrap_err().code,
        AuthErrorCode::AuthBusy
    );
    let listener = std::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 1455)).unwrap();
    drop(listener);
    outwait_old_commit_and_cleanup_budget(started);
    assert_eq!(h.auth.session.lock().unwrap().login_workers, 1);
    release.send(()).unwrap();
    refreshing.join().unwrap().unwrap();
    workers_done(&h.auth);
    assert!(h
        .store
        .value
        .lock()
        .unwrap()
        .as_ref()
        .unwrap()
        .pending
        .is_none());
    assert_eq!(h.auth.get_connection().state, ConnectionState::Connected);
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 1);
}

#[test]
fn browser_expiry_and_mismatched_state_never_exchange() {
    let _serial = BROWSER_TEST.lock().unwrap();
    for expire in [false, true] {
        let h = Harness::new();
        h.clock.advance.store(false, Ordering::Release);
        let started = h.auth.start_login(LoginFlow::Browser).unwrap();
        if expire {
            h.clock.now.store(started.expires_at, Ordering::Release);
        } else {
            let mut stream =
                std::net::TcpStream::connect((std::net::Ipv4Addr::LOCALHOST, 1455)).unwrap();
            stream.write_all(b"GET /auth/callback?code=synthetic-code&state=wrong HTTP/1.1\r\nHost: localhost:1455\r\n\r\n").unwrap();
            let mut response = String::new();
            stream.read_to_string(&mut response).unwrap();
            assert!(response.starts_with("HTTP/1.1 400"));
        }
        workers_done(&h.auth);
        assert_eq!(
            h.auth.session.lock().unwrap().snapshot.error_code,
            Some(if expire {
                AuthErrorCode::LoginExpired
            } else {
                AuthErrorCode::CallbackMismatch
            })
        );
        assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
    }
}

#[test]
fn open_login_uses_only_the_current_stored_presentation_and_never_a_caller_url() {
    let _serial = BROWSER_TEST.lock().unwrap();
    struct Browser(Mutex<Vec<String>>);
    impl LoginBrowser for Browser {
        fn open(&self, url: &str) -> Result<(), BrowserOpenError> {
            self.0.lock().unwrap().push(url.into());
            Ok(())
        }
    }
    let mut h = Harness::new();
    h.clock.advance.store(false, Ordering::Release);
    let browser = Arc::new(Browser(Mutex::new(Vec::new())));
    Arc::get_mut(&mut h.auth).unwrap().browser = Some(browser.clone());
    let started = h.auth.start_login(LoginFlow::Browser).unwrap();
    h.auth.open_login(&started.login_id).unwrap();
    assert_eq!(
        h.auth
            .open_login("https://synthetic-attacker.invalid")
            .unwrap_err()
            .code,
        AuthErrorCode::NotCurrent
    );
    let LoginPresentation::Browser { authorization_url } = started.presentation else {
        panic!("wrong presentation")
    };
    assert_eq!(*browser.0.lock().unwrap(), vec![authorization_url]);
    h.auth.cancel_login(&started.login_id).unwrap();
    workers_done(&h.auth);
    assert_eq!(
        h.auth.open_login(&started.login_id).unwrap_err().code,
        AuthErrorCode::NotCurrent
    );
    assert_eq!(browser.0.lock().unwrap().len(), 1);
}

#[test]
fn cancelled_dispatch_does_not_read_store_or_send_auth() {
    let h = Harness::new();
    seed(&h.store, NOW + 1);
    assert_eq!(
        h.auth
            .authorize_dispatch(None, &AtomicBool::new(true))
            .unwrap_err()
            .code,
        AuthErrorCode::Cancelled
    );
    assert_eq!(h.store.reads.load(Ordering::Relaxed), 0);
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
}

#[test]
fn cancelled_sent_refresh_still_persists_pair_and_disconnect_then_tombstones_it() {
    for disconnect in [false, true] {
        let h = Harness::new();
        seed(&h.store, NOW + 1);
        let (entered, release) = h.script.blocked(
            Endpoint::Token,
            200,
            tokens("synthetic-account", NOW + 3600, Some("synthetic-rotated")),
        );
        let cancel = Arc::new(AtomicBool::new(false));
        let (auth, signal) = (h.auth.clone(), cancel.clone());
        let dispatch = std::thread::spawn(move || auth.authorize_dispatch(None, &signal));
        entered.recv_timeout(Duration::from_secs(2)).unwrap();
        let disconnecting = if disconnect {
            let auth = h.auth.clone();
            let worker = std::thread::spawn(move || auth.disconnect());
            eventually(|| h.auth.session.lock().unwrap().suspended);
            Some(worker)
        } else {
            cancel.store(true, Ordering::Release);
            None
        };
        release.send(()).unwrap();
        assert_eq!(
            dispatch.join().unwrap().unwrap_err().code,
            if disconnect {
                AuthErrorCode::DisconnectFailed
            } else {
                AuthErrorCode::Cancelled
            }
        );
        if let Some(worker) = disconnecting {
            worker.join().unwrap().unwrap();
        }
        let env = h.store.value.lock().unwrap().clone().unwrap();
        if disconnect {
            assert!(env.grant.is_none());
        } else {
            let grant = env.grant.unwrap();
            assert_eq!(grant.refresh.0, "synthetic-rotated");
            assert!(matches!(grant.state, GrantState::Ready));
            assert!(h.authorize().is_ok());
        }
        assert_eq!(h.script.calls.load(Ordering::Relaxed), 1);
    }
}

#[test]
fn local_monotonic_login_deadline_survives_wall_clock_rollback() {
    let h = Harness::new();
    h.script.response(
        Endpoint::DeviceCode,
        200,
        json!({"device_auth_id": "synthetic-id", "user_code": "SYN"}),
    );
    let (entered, release) = h.script.blocked(Endpoint::DevicePoll, 404, Value::Null);
    h.auth.start_login(LoginFlow::DeviceCode).unwrap();
    entered.recv_timeout(Duration::from_secs(2)).unwrap();
    let work = h.auth.session.lock().unwrap().work.clone().unwrap();
    h.clock.now.store(NOW - 3600, Ordering::Release);
    *work.deadline.lock().unwrap() = Instant::now();
    release.send(()).unwrap();
    workers_done(&h.auth);
    assert_eq!(
        h.auth.session.lock().unwrap().snapshot.error_code,
        Some(AuthErrorCode::LoginExpired)
    );
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 2);
}

struct SyntheticFileStore(PathBuf);
impl LocalGrantStore for SyntheticFileStore {
    fn namespace(&self) -> &str {
        "anybao.synthetic-process-test"
    }
    fn read(&self, _: &GrantSlot) -> Result<Option<GrantEnvelope>, GrantStoreError> {
        let bytes = std::fs::read(self.0.join("synthetic-envelope.json"))
            .map_err(|_| GrantStoreError::Unavailable)?;
        GrantEnvelope::decode(&bytes).map(Some)
    }
    fn replace(&self, _: &GrantSlot, envelope: &GrantEnvelope) -> Result<(), GrantStoreError> {
        let bytes = envelope.encode()?;
        let temp = self.0.join("synthetic-envelope.tmp");
        let mut file = std::fs::File::create(&temp).map_err(|_| GrantStoreError::Unavailable)?;
        file.write_all(&bytes)
            .and_then(|_| file.sync_all())
            .map_err(|_| GrantStoreError::Unavailable)?;
        std::fs::rename(temp, self.0.join("synthetic-envelope.json"))
            .map_err(|_| GrantStoreError::Unavailable)?;
        Ok(())
    }
}

#[test]
fn pending_browser_process_child() {
    let Ok(root) = std::env::var("ANYBAO_TEST_PENDING_ROOT") else {
        return;
    };
    let root = PathBuf::from(root);
    let clock = Arc::new(TestClock::new());
    clock.advance.store(false, Ordering::Release);
    let auth = service(
        &root,
        Arc::new(SyntheticFileStore(root.clone())),
        Arc::new(Script::default()),
        clock,
    );
    let watch = auth.subscribe();
    let stamp = auth
        .authorize_dispatch(None, &AtomicBool::new(false))
        .unwrap()
        .stamp;
    auth.start_login(LoginFlow::Browser).unwrap();
    watch.latest();
    std::fs::write(root.join("synthetic-browser-ready"), "ready").unwrap();
    workers_done(&auth);
    let observed = watch.latest().unwrap();
    assert_eq!(observed.state, ConnectionState::Connected);
    assert!(observed.pending_login.is_none());
    assert_eq!(completion(&auth, &stamp), CompletionStatus::Accepted);
}

#[test]
fn another_process_cancels_or_expires_pending_browser_and_owner_releases_listener() {
    let _serial = BROWSER_TEST.lock().unwrap();
    for expire in [false, true] {
        let dir = private_dir();
        let old = seed(&SyntheticStore::default(), NOW + 3600);
        let store = Arc::new(SyntheticFileStore(dir.path().into()));
        store.replace(&slot(), &old).unwrap();
        let mut child = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "chatgpt::tests::pending_browser_process_child",
                "--nocapture",
            ])
            .env("ANYBAO_TEST_PENDING_ROOT", dir.path())
            .spawn()
            .unwrap();
        eventually(|| dir.path().join("synthetic-browser-ready").exists());
        let clock = Arc::new(TestClock::new());
        clock.advance.store(false, Ordering::Release);
        let peer = service(
            dir.path(),
            store.clone(),
            Arc::new(Script::default()),
            clock.clone(),
        );
        let pending = peer.get_connection().pending_login.unwrap();
        let started = Instant::now();
        if expire {
            clock.now.store(pending.expires_at + 1, Ordering::Release);
            assert!(peer.get_connection().pending_login.is_none());
        } else {
            assert_eq!(
                peer.cancel_login(&pending.login_id).unwrap(),
                CancelOutcome::Cancelled
            );
        }
        eventually(|| child.try_wait().unwrap().is_some());
        assert!(child.wait().unwrap().success());
        assert!(started.elapsed() < Duration::from_secs(2));
        let stored = store.read(&slot()).unwrap().unwrap();
        assert_eq!(
            stored.grant.as_ref().unwrap().id,
            old.grant.as_ref().unwrap().id
        );
        assert!(stored.pending.is_none());
        // The former owner must release the fixed port, and its stale ID must
        // never cancel the new attempt now owned by this host.
        let next = peer.start_login(LoginFlow::Browser).unwrap();
        assert_eq!(
            peer.cancel_login(&pending.login_id).unwrap(),
            if expire {
                CancelOutcome::NotCurrent
            } else {
                CancelOutcome::AlreadyFinished
            }
        );
        assert_eq!(
            peer.get_connection().pending_login.unwrap().login_id,
            next.login_id
        );
        assert_eq!(
            peer.cancel_login(&next.login_id).unwrap(),
            CancelOutcome::Cancelled
        );
        workers_done(&peer);
    }
}

struct SyntheticProcessTransport {
    root: PathBuf,
    crash: bool,
}
impl AuthTransport for SyntheticProcessTransport {
    fn post(
        &self,
        endpoint: Endpoint,
        _: AuthBody,
        _: Duration,
    ) -> Result<AuthReply, TransportFailure> {
        assert_eq!(endpoint, Endpoint::Token);
        let counter = self.root.join("synthetic-refresh-count");
        let n: usize = std::fs::read_to_string(&counter)
            .unwrap_or_default()
            .parse()
            .unwrap_or(0);
        std::fs::write(counter, (n + 1).to_string()).unwrap();
        if self.crash {
            std::process::exit(23);
        }
        std::thread::sleep(Duration::from_millis(200));
        Ok(AuthReply {
            status: 200,
            body: tokens(
                "synthetic-account",
                NOW + 3600,
                Some("synthetic-process-rotated"),
            ),
            retry_after: None,
        })
    }
}

#[test]
fn refresh_process_child() {
    let Ok(root) = std::env::var("ANYBAO_TEST_REFRESH_ROOT") else {
        return;
    };
    let root = PathBuf::from(root);
    let store = Arc::new(SyntheticFileStore(root.clone()));
    let locks = GrantLockDirectory::new(&root).unwrap();
    let lock = locks.slot_lock(&slot(), store.namespace()).unwrap();
    let auth = ChatGptAuth::create(
        Some(Storage {
            slot: slot(),
            store,
            lock,
        }),
        "device".into(),
        1,
        None,
        Arc::new(SyntheticProcessTransport {
            root,
            crash: std::env::var_os("ANYBAO_TEST_REFRESH_CRASH").is_some(),
        }),
        Arc::new(TestClock::new()),
    );
    if std::env::var_os("ANYBAO_TEST_EXPECT_UNCERTAIN").is_some() {
        assert_eq!(
            auth.authorize_dispatch(None, &AtomicBool::new(false))
                .unwrap_err()
                .code,
            AuthErrorCode::RefreshUncertain
        );
    } else {
        assert_eq!(
            auth.authorize_dispatch(None, &AtomicBool::new(false))
                .unwrap()
                .stamp
                .pair_revision,
            2
        );
    }
}

#[test]
fn actual_processes_refresh_once_adopt_pair_and_never_reuse_after_crash() {
    for crash in [false, true] {
        let dir = private_dir();
        let synthetic = SyntheticStore::default();
        let env = seed(&synthetic, NOW + 1);
        SyntheticFileStore(dir.path().into())
            .replace(&slot(), &env)
            .unwrap();
        let command = || {
            let mut cmd = std::process::Command::new(std::env::current_exe().unwrap());
            cmd.args([
                "--exact",
                "chatgpt::tests::refresh_process_child",
                "--nocapture",
            ])
            .env("ANYBAO_TEST_REFRESH_ROOT", dir.path());
            cmd
        };
        if crash {
            assert_eq!(
                command()
                    .env("ANYBAO_TEST_REFRESH_CRASH", "1")
                    .status()
                    .unwrap()
                    .code(),
                Some(23)
            );
            assert!(command()
                .env("ANYBAO_TEST_EXPECT_UNCERTAIN", "1")
                .status()
                .unwrap()
                .success());
        } else {
            let mut first = command().spawn().unwrap();
            let mut second = command().spawn().unwrap();
            assert!(first.wait().unwrap().success());
            assert!(second.wait().unwrap().success());
        }
        assert_eq!(
            std::fs::read_to_string(dir.path().join("synthetic-refresh-count")).unwrap(),
            "1"
        );
    }
}

#[path = "inference_tests.rs"]
mod inference;

#[test]
fn readiness_cooldown_expires_on_local_status_read_without_refresh() {
    let h = Harness::new();
    let mut env = seed(&h.store, NOW + 3600);
    env.grant.as_mut().unwrap().retry_after = Some(NOW + 30);
    *h.store.value.lock().unwrap() = Some(env);
    let before = h.auth.get_connection();
    assert_eq!(crate::readiness::provider(&before)["state"], "cooldown");
    h.clock.now.store(NOW + 30, Ordering::Release);
    let after = h.auth.get_connection();
    assert_eq!(after.state, ConnectionState::Connected);
    assert_eq!(after.retry_after, None);
    assert_eq!(crate::readiness::provider(&after)["state"], "connected");
    assert!(after.revision > before.revision);
    assert_eq!(h.store.saves.load(Ordering::Relaxed), 0);
}
