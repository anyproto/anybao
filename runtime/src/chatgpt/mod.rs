//! Session-scoped native ChatGPT authentication (docs/chatgpt-local-oauth.md §1–5).

mod custody;
mod protocol;
pub use crate::local_grant::{AuthErrorCode, LoginFlow};
use crate::local_grant::{
    Grant, GrantEnvelope, GrantLockDirectory, GrantSlot, GrantState, LocalGrantStore,
    PendingGrantLogin, Principal, Secret, SlotLock,
};
pub use custody::{CustodyOperation, RecoveryRequest};
use protocol::*;
use serde::Serialize;
use serde_json::json;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, Weak};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const LOCK_WAIT: Duration = Duration::from_secs(5);
const LOGIN_OBSERVE_EVERY: Duration = Duration::from_millis(250);
const LOGIN_OBSERVE_LOCK_WAIT: Duration = Duration::from_millis(100);
// A terminal login can wait out a normal 30-second refresh plus one lock budget.
const LOGIN_SETTLE_WAIT: Duration = Duration::from_secs(35);

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ConnectionState {
    Disconnected,
    Connecting,
    Connected,
    ReauthRequired,
    Unavailable,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PendingLogin {
    pub login_id: String,
    pub flow: LoginFlow,
    pub expires_at: u64,
}

impl From<&PendingGrantLogin> for PendingLogin {
    fn from(p: &PendingGrantLogin) -> Self {
        Self {
            login_id: p.id.clone(),
            flow: p.flow,
            expires_at: p.expires_at,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ConnectionSnapshot {
    pub state: ConnectionState,
    pub device_id: String,
    pub session_generation: u64,
    pub revision: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub account_label: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub plan_type: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pending_login: Option<PendingLogin>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error_code: Option<AuthErrorCode>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retry_after: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AuthError {
    pub code: AuthErrorCode,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pending_login: Option<PendingLogin>,
}

impl From<AuthErrorCode> for AuthError {
    fn from(code: AuthErrorCode) -> Self {
        Self {
            code,
            pending_login: None,
        }
    }
}
impl std::fmt::Display for AuthError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.code.as_str())
    }
}
impl std::error::Error for AuthError {}

/// Transient initiating-local-UI result only; never a shared event (docs/chatgpt-local-oauth.md §5).
#[derive(Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum LoginPresentation {
    DeviceCode {
        #[serde(rename = "verificationUrl")]
        verification_url: String,
        #[serde(rename = "userCode")]
        user_code: String,
    },
    Browser {
        #[serde(rename = "authorizationUrl")]
        authorization_url: String,
    },
}

impl std::fmt::Debug for LoginPresentation {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("LoginPresentation([REDACTED])")
    }
}

impl LoginPresentation {
    fn url(&self) -> &str {
        match self {
            Self::DeviceCode {
                verification_url, ..
            } => verification_url,
            Self::Browser { authorization_url } => authorization_url,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LoginStart {
    pub login_id: String,
    pub presentation: LoginPresentation,
    pub expires_at: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CancelOutcome {
    Cancelled,
    AlreadyFinished,
    NotCurrent,
}

/// Trusted native browser integration; errors cannot carry URLs or OS output.
pub trait LoginBrowser: Send + Sync {
    fn open(&self, url: &str) -> Result<(), BrowserOpenError>;
}
#[derive(Debug)]
pub struct BrowserOpenError;

struct Watch {
    value: Mutex<Option<ConnectionSnapshot>>,
    changed: Condvar,
}

/// A bounded latest-snapshot subscription. Subscribe, then get_connection.
pub struct ConnectionSubscription(Arc<Watch>);
impl ConnectionSubscription {
    pub fn latest(&self) -> Option<ConnectionSnapshot> {
        self.0.value.lock().unwrap().take()
    }
    pub fn wait_timeout(&self, timeout: Duration) -> Option<ConnectionSnapshot> {
        let value = self.0.value.lock().unwrap();
        let (mut value, _) = self
            .0
            .changed
            .wait_timeout_while(value, timeout, |v| v.is_none())
            .unwrap();
        value.take()
    }
}

trait Clock: Send + Sync {
    fn now(&self) -> u64;
    fn wait(&self, duration: Duration, cancel: &AtomicBool);
}
struct SystemClock;
impl Clock for SystemClock {
    fn now(&self) -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs()
    }
    fn wait(&self, duration: Duration, cancel: &AtomicBool) {
        let deadline = Instant::now() + duration;
        while !cancel.load(Ordering::Acquire) && Instant::now() < deadline {
            std::thread::sleep(
                deadline
                    .saturating_duration_since(Instant::now())
                    .min(Duration::from_millis(25)),
            );
        }
    }
}

struct Storage {
    slot: GrantSlot,
    store: Arc<dyn LocalGrantStore>,
    lock: SlotLock,
}
struct LoginWork {
    id: String,
    epoch: u64,
    generation: u64,
    flow: LoginFlow,
    expires_at: AtomicU64,
    deadline: Mutex<Instant>,
    cancel: AtomicBool,
    presentation: Mutex<Option<Result<LoginStart, AuthError>>>,
    ready: Condvar,
}
impl LoginWork {
    fn complete_start(&self, value: Result<LoginStart, AuthError>) {
        let mut initial = self.presentation.lock().unwrap();
        if initial.is_none() {
            *initial = Some(value);
            self.ready.notify_all();
        }
    }
    fn wait_start(&self) -> Result<LoginStart, AuthError> {
        let mut initial = self.presentation.lock().unwrap();
        loop {
            if self.cancel.load(Ordering::Acquire) {
                return Err(AuthErrorCode::Cancelled.into());
            }
            if let Some(result) = &*initial {
                return result.clone();
            }
            initial = self
                .ready
                .wait_timeout(initial, Duration::from_millis(50))
                .unwrap()
                .0;
        }
    }
}

#[derive(Clone, PartialEq, Eq)]
struct LoginCleanup {
    id: String,
    owner_instance: String,
    epoch: u64,
    generation: u64,
}

struct Session {
    generation: u64,
    fence: u64,
    suspended: bool,
    work: Option<Arc<LoginWork>>,
    finished: Option<String>,
    cleanup: Option<LoginCleanup>,
    snapshot: ConnectionSnapshot,
    watches: Vec<Weak<Watch>>,
    refreshes: usize,
    login_workers: usize,
}

/// Create one instance per authenticated Any session and share it with native
/// commands and runtime config. No CLI/degraded store is created (docs/chatgpt-local-oauth.md §2).
pub struct ChatGptAuth {
    storage: Option<Storage>,
    instance: String,
    // Custody remains scoped to this original session after its UI generation ends.
    // This identity permits pending cleanup only, never installation/dispatch.
    owner_generation: u64,
    browser: Option<Arc<dyn LoginBrowser>>,
    transport: Arc<dyn AuthTransport>,
    clock: Arc<dyn Clock>,
    session: Mutex<Session>,
    ended: AtomicBool,
    refresh_settled: Condvar,
}

impl std::fmt::Debug for ChatGptAuth {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("ChatGptAuth([host-only])")
    }
}

fn random_id() -> String {
    uuid::Uuid::new_v4().to_string()
}

impl ChatGptAuth {
    pub fn new(
        slot: GrantSlot,
        session_generation: u64,
        store: Arc<dyn LocalGrantStore>,
        locks: GrantLockDirectory,
        browser: Option<Arc<dyn LoginBrowser>>,
    ) -> Result<Arc<Self>, AuthError> {
        let lock = locks.slot_lock(&slot, store.namespace())?;
        let device = slot.device_id().to_string();
        Ok(Self::create(
            Some(Storage { slot, store, lock }),
            device,
            session_generation,
            browser,
            Arc::new(FixedTransport),
            Arc::new(SystemClock),
        ))
    }

    /// Used by a native host with unavailable custody; it cannot initiate login.
    pub fn unavailable(device_id: &str, session_generation: u64) -> Arc<Self> {
        Self::create(
            None,
            device_id
                .chars()
                .filter(|c| !c.is_control())
                .take(256)
                .collect(),
            session_generation,
            None,
            Arc::new(FixedTransport),
            Arc::new(SystemClock),
        )
    }

    fn create(
        storage: Option<Storage>,
        device_id: String,
        generation: u64,
        browser: Option<Arc<dyn LoginBrowser>>,
        transport: Arc<dyn AuthTransport>,
        clock: Arc<dyn Clock>,
    ) -> Arc<Self> {
        let snapshot = ConnectionSnapshot {
            state: ConnectionState::Unavailable,
            device_id,
            session_generation: generation,
            revision: 0,
            account_label: None,
            plan_type: None,
            pending_login: None,
            error_code: Some(AuthErrorCode::LocalStoreUnavailable),
            retry_after: None,
        };
        Arc::new(Self {
            storage,
            instance: random_id(),
            owner_generation: generation,
            browser,
            transport,
            clock,
            session: Mutex::new(Session {
                generation,
                fence: 0,
                suspended: false,
                work: None,
                finished: None,
                cleanup: None,
                snapshot,
                watches: Vec::new(),
                refreshes: 0,
                login_workers: 0,
            }),
            ended: AtomicBool::new(false),
            refresh_settled: Condvar::new(),
        })
    }

    pub fn subscribe(&self) -> ConnectionSubscription {
        let watch = Arc::new(Watch {
            value: Mutex::new(None),
            changed: Condvar::new(),
        });
        self.session
            .lock()
            .unwrap()
            .watches
            .push(Arc::downgrade(&watch));
        ConnectionSubscription(watch)
    }

    fn storage(&self) -> Result<&Storage, AuthErrorCode> {
        self.storage
            .as_ref()
            .ok_or(AuthErrorCode::LocalStoreUnavailable)
    }
    fn live(&self, session: &Session) -> Result<(), AuthErrorCode> {
        if self.ended.load(Ordering::Acquire) {
            Err(AuthErrorCode::SessionEnded)
        } else if session.suspended {
            Err(AuthErrorCode::DisconnectFailed)
        } else {
            Ok(())
        }
    }
    fn save(&self, env: &mut GrantEnvelope) -> Result<(), AuthErrorCode> {
        env.bump()?;
        let storage = self.storage()?;
        storage
            .store
            .replace(&storage.slot, env)
            .map_err(Into::into)
    }
    fn read_envelope(&self) -> Result<GrantEnvelope, AuthErrorCode> {
        let storage = self.storage()?;
        let env = storage
            .store
            .read(&storage.slot)
            .map_err(AuthErrorCode::from)?
            .unwrap_or_else(|| GrantEnvelope::empty(&storage.slot));
        validate_envelope(&env, &storage.slot)?;
        Ok(env)
    }
    fn load(&self) -> Result<GrantEnvelope, AuthErrorCode> {
        self.load_preserving_login(None)
    }
    fn cleanup_matches(&self, env: &GrantEnvelope, cleanup: &LoginCleanup) -> bool {
        cleanup.owner_instance == self.instance
            && cleanup.generation == self.owner_generation
            && env.epoch == cleanup.epoch
            && env.pending.as_ref().is_some_and(|pending| {
                pending.id == cleanup.id
                    && pending.owner_instance == cleanup.owner_instance
                    && pending.epoch == cleanup.epoch
            })
    }
    fn clear_matching_pending(
        &self,
        env: &mut GrantEnvelope,
        cleanup: &LoginCleanup,
    ) -> Result<bool, AuthErrorCode> {
        if !self.cleanup_matches(env, cleanup) {
            return Ok(false);
        }
        env.pending = None;
        env.fence()?;
        Ok(true)
    }
    fn load_preserving_login(
        &self,
        login_id: Option<&str>,
    ) -> Result<GrantEnvelope, AuthErrorCode> {
        let result = self.read_normalized(login_id);
        if let Err(code) = &result {
            self.record_error(*code);
        }
        result
    }
    fn read_normalized(&self, login_id: Option<&str>) -> Result<GrantEnvelope, AuthErrorCode> {
        let mut env = self.read_envelope()?;
        let mut changed = false;
        let cleanup = self
            .session
            .lock()
            .unwrap()
            .cleanup
            .clone()
            .filter(|cleanup| {
                cleanup.owner_instance == self.instance
                    && cleanup.generation == self.owner_generation
            });
        if let Some(cleanup) = &cleanup {
            changed = self.clear_matching_pending(&mut env, cleanup)?;
        }
        if let Some(grant) = &mut env.grant {
            if matches!(grant.state, GrantState::RefreshInflight { .. }) {
                grant.block(AuthErrorCode::RefreshUncertain);
                changed = true;
            }
        }
        if env
            .pending
            .as_ref()
            // The active worker settles its own expiry in finish_login, keeping
            // the explicit login_expired result with the pending-state commit.
            .is_some_and(|p| Some(p.id.as_str()) != login_id && p.expires_at <= self.clock.now())
        {
            env.pending = None;
            env.fence()?;
            changed = true;
        }
        if changed {
            self.save(&mut env)?;
        }
        let mut session = self.session.lock().unwrap();
        if let Some(cleanup) = cleanup {
            if session.cleanup.as_ref() == Some(&cleanup) {
                session.cleanup = None;
            }
        }
        // Every normalized authoritative read publishes recovery/peer changes,
        // including callers that subsequently return early. Failed saves never do.
        self.publish(&mut session, Some(&env), None);
        Ok(env)
    }

    fn publish(
        &self,
        session: &mut Session,
        env: Option<&GrantEnvelope>,
        error: Option<AuthErrorCode>,
    ) {
        let mut next = ConnectionSnapshot {
            state: ConnectionState::Unavailable,
            device_id: session.snapshot.device_id.clone(),
            session_generation: session.generation,
            revision: session.snapshot.revision,
            account_label: None,
            plan_type: None,
            pending_login: None,
            error_code: error,
            retry_after: None,
        };
        if self.ended.load(Ordering::Acquire) {
            next.error_code = Some(AuthErrorCode::SessionEnded);
        } else if session.suspended {
            next.error_code = Some(error.unwrap_or(AuthErrorCode::DisconnectFailed));
        } else if let Some(env) = env {
            next.pending_login = env.pending.as_ref().map(Into::into);
            next.state = if next.pending_login.is_some() {
                ConnectionState::Connecting
            } else {
                ConnectionState::Disconnected
            };
            if let Some(grant) = &env.grant {
                match grant.state {
                    GrantState::Ready => {
                        next.state = ConnectionState::Connected;
                        next.account_label.clone_from(&grant.account_label);
                        next.plan_type.clone_from(&grant.plan_type);
                        next.retry_after =
                            grant.retry_after.filter(|until| *until > self.clock.now());
                    }
                    GrantState::ReauthRequired { reason } => {
                        if next.pending_login.is_none() {
                            next.state = ConnectionState::ReauthRequired;
                        }
                        next.error_code = Some(reason);
                    }
                    GrantState::RefreshInflight { .. } => {
                        next.error_code = Some(AuthErrorCode::RefreshUncertain);
                    }
                }
            }
        }
        if next != session.snapshot {
            next.revision = session.snapshot.revision.saturating_add(1);
            session.snapshot = next.clone();
            session.watches.retain(|weak| {
                if let Some(watch) = weak.upgrade() {
                    *watch.value.lock().unwrap() = Some(next.clone());
                    watch.changed.notify_all();
                    true
                } else {
                    false
                }
            });
        }
    }
    fn record_error(&self, code: AuthErrorCode) {
        self.publish(&mut self.session.lock().unwrap(), None, Some(code));
    }

    /// Each runtime incarnation reserves once, independently of this auth
    /// service's lifetime. Neither credentials nor wall time order beats.
    pub(crate) fn reserve_readiness_generation(
        &self,
        device: &str,
    ) -> Result<(u64, String), AuthErrorCode> {
        let session = self.session.lock().unwrap();
        self.live(&session)?;
        if session.snapshot.device_id != device || device.is_empty() {
            return Err(AuthErrorCode::IdentityUnavailable);
        }
        drop(session);
        let storage = self.storage()?;
        Ok((
            storage.lock.reserve_generation()?,
            storage
                .slot
                .storage_key(&format!("bao-readiness-v1:{}", storage.store.namespace()))?,
        ))
    }

    /// Authoritative local read; no refresh or network (docs/chatgpt-local-oauth.md §5).
    pub fn get_connection(&self) -> ConnectionSnapshot {
        let read = || -> Result<(), AuthErrorCode> {
            if self.ended.load(Ordering::Acquire) {
                return Err(AuthErrorCode::SessionEnded);
            }
            let storage = self.storage()?;
            let _transaction = storage
                .lock
                .acquire(LOCK_WAIT, || self.ended.load(Ordering::Acquire))?;
            // A failed disconnect stays suspended for inference, but the
            // connection surface must still diagnose recoverable custody.
            let suspended = self.session.lock().unwrap().suspended;
            let env = if suspended {
                self.read_envelope()?
            } else {
                self.load()?
            };
            let mut session = self.session.lock().unwrap();
            self.publish(&mut session, Some(&env), None);
            Ok(())
        };
        if let Err(code) = read() {
            self.record_error(code);
        }
        self.session.lock().unwrap().snapshot.clone()
    }

    /// Polling/listening runs on a host worker; only code acquisition is awaited.
    pub fn start_login(self: &Arc<Self>, flow: LoginFlow) -> Result<LoginStart, AuthError> {
        let result = self.begin_login(flow);
        match result {
            Ok(work) => work.wait_start(),
            Err(error) => {
                if error.code != AuthErrorCode::LoginInProgress {
                    self.record_error(error.code);
                }
                Err(error)
            }
        }
    }
    fn begin_login(self: &Arc<Self>, flow: LoginFlow) -> Result<Arc<LoginWork>, AuthError> {
        self.live(&self.session.lock().unwrap())?;
        let storage = self.storage()?;
        let _transaction = storage
            .lock
            .acquire(LOCK_WAIT, || self.ended.load(Ordering::Acquire))?;
        let mut env = self.load()?;
        let mut session = self.session.lock().unwrap();
        self.live(&session)?;
        if let Some(pending) = &env.pending {
            if pending.owner_instance == self.instance && pending.flow == flow {
                if let Some(work) = &session.work {
                    if work.id == pending.id && !work.cancel.load(Ordering::Acquire) {
                        return Ok(work.clone());
                    }
                }
            }
            self.publish(&mut session, Some(&env), None);
            return Err(AuthError {
                code: AuthErrorCode::LoginInProgress,
                pending_login: Some(pending.into()),
            });
        }
        env.fence()?;
        let expires_at = self.clock.now().saturating_add(match flow {
            LoginFlow::DeviceCode => 900,
            LoginFlow::Browser => 300,
        });
        let work = Arc::new(LoginWork {
            id: random_id(),
            epoch: env.epoch,
            generation: session.generation,
            flow,
            expires_at: AtomicU64::new(expires_at),
            deadline: Mutex::new(
                Instant::now()
                    + Duration::from_secs(match flow {
                        LoginFlow::DeviceCode => 900,
                        LoginFlow::Browser => 300,
                    }),
            ),
            cancel: AtomicBool::new(false),
            presentation: Mutex::new(None),
            ready: Condvar::new(),
        });
        env.pending = Some(PendingGrantLogin {
            id: work.id.clone(),
            owner_instance: self.instance.clone(),
            epoch: work.epoch,
            flow,
            expires_at,
        });
        if let Err(code) = self.save(&mut env) {
            self.remember_login_cleanup(&mut session, &work);
            session.finished = Some(work.id.clone());
            return Err(code.into());
        }
        session.work = Some(work.clone());
        session.login_workers += 1;
        self.publish(&mut session, Some(&env), None);
        drop(session);
        drop(_transaction);
        let (auth, worker) = (self.clone(), work.clone());
        if std::thread::Builder::new()
            .name("chatgpt-login".into())
            .spawn(move || {
                let _worker = LoginSettlement(&auth);
                let result = auth.login_worker(&worker);
                auth.finish_login(&worker, result);
            })
            .is_err()
        {
            self.finish_login(&work, Err(AuthErrorCode::AuthNetwork));
            self.session.lock().unwrap().login_workers -= 1;
            self.refresh_settled.notify_all();
        }
        Ok(work)
    }

    fn check_work(&self, work: &LoginWork) -> Result<(), AuthErrorCode> {
        if self.ended.load(Ordering::Acquire) {
            return Err(AuthErrorCode::SessionEnded);
        }
        if work.cancel.load(Ordering::Acquire) {
            return Err(AuthErrorCode::Cancelled);
        }
        if self.clock.now() >= work.expires_at.load(Ordering::Acquire)
            || Instant::now() >= *work.deadline.lock().unwrap()
        {
            return Err(AuthErrorCode::LoginExpired);
        }
        Ok(())
    }
    fn matches_work(&self, env: &GrantEnvelope, work: &LoginWork, session: &Session) -> bool {
        self.live(session).is_ok()
            && session.generation == work.generation
            && env.epoch == work.epoch
            && env.pending.as_ref().is_some_and(|p| {
                p.id == work.id
                    && p.owner_instance == self.instance
                    && p.epoch == work.epoch
                    && p.expires_at > self.clock.now()
            })
    }

    // docs/chatgpt-local-oauth.md §4: another host can cancel this attempt without deleting its grant.
    fn observe_login(&self, work: &LoginWork) -> Result<bool, AuthErrorCode> {
        self.check_work(work)?;
        let storage = self.storage()?;
        let transaction = storage.lock.acquire(LOGIN_OBSERVE_LOCK_WAIT, || {
            work.cancel.load(Ordering::Acquire) || self.ended.load(Ordering::Acquire)
        });
        let _transaction = match transaction {
            Ok(transaction) => transaction,
            Err(AuthErrorCode::AuthBusy) => return Ok(false),
            Err(code) => return Err(code),
        };
        let env = self.load_preserving_login(Some(&work.id))?;
        let mut session = self.session.lock().unwrap();
        self.publish(&mut session, Some(&env), None);
        self.check_work(work)?;
        if !self.matches_work(&env, work, &session) {
            work.cancel.store(true, Ordering::Release);
            work.ready.notify_all();
            if session
                .work
                .as_ref()
                .is_some_and(|current| current.id == work.id)
            {
                session.work = None;
                session.finished = Some(work.id.clone());
            }
            return Err(AuthErrorCode::NotCurrent);
        }
        self.check_work(work)?;
        Ok(true)
    }

    fn await_current_login(&self, work: &LoginWork) -> Result<(), AuthErrorCode> {
        while !self.observe_login(work)? {
            self.clock.wait(Duration::from_millis(25), &work.cancel);
        }
        Ok(())
    }

    fn wait_login(&self, work: &LoginWork, duration: Duration) -> Result<(), AuthErrorCode> {
        let mut remaining = duration;
        while !remaining.is_zero() {
            self.observe_login(work)?;
            let slice = remaining.min(LOGIN_OBSERVE_EVERY);
            self.clock.wait(slice, &work.cancel);
            remaining = remaining.saturating_sub(slice);
        }
        self.check_work(work)
    }

    fn present(
        &self,
        work: &LoginWork,
        presentation: LoginPresentation,
        expires_at: u64,
    ) -> Result<(), AuthErrorCode> {
        self.check_work(work)?;
        let storage = self.storage()?;
        let _transaction = storage.lock.acquire(LOCK_WAIT, || {
            work.cancel.load(Ordering::Acquire) || self.ended.load(Ordering::Acquire)
        })?;
        let mut env = self.load()?;
        let mut session = self.session.lock().unwrap();
        if !self.matches_work(&env, work, &session) {
            return Err(AuthErrorCode::NotCurrent);
        }
        if expires_at <= self.clock.now() {
            return Err(AuthErrorCode::LoginExpired);
        }
        env.pending.as_mut().unwrap().expires_at = expires_at;
        self.save(&mut env)?;
        work.expires_at.store(expires_at, Ordering::Release);
        let mut deadline = work.deadline.lock().unwrap();
        *deadline = (*deadline)
            .min(Instant::now() + Duration::from_secs(expires_at.saturating_sub(self.clock.now())));
        drop(deadline);
        work.complete_start(Ok(LoginStart {
            login_id: work.id.clone(),
            presentation,
            expires_at,
        }));
        self.publish(&mut session, Some(&env), None);
        Ok(())
    }

    fn post_login(
        &self,
        work: &LoginWork,
        endpoint: Endpoint,
        body: AuthBody,
    ) -> Result<AuthReply, AuthErrorCode> {
        self.await_current_login(work)?;
        let remaining = work
            .expires_at
            .load(Ordering::Acquire)
            .saturating_sub(self.clock.now());
        if remaining == 0 {
            return Err(AuthErrorCode::LoginExpired);
        }
        let timeout = Duration::from_secs(remaining.min(30)).min(
            work.deadline
                .lock()
                .unwrap()
                .saturating_duration_since(Instant::now()),
        );
        if timeout.is_zero() {
            return Err(AuthErrorCode::LoginExpired);
        }
        let result = self.transport.post(endpoint, body, timeout);
        self.await_current_login(work)?;
        result.map_err(|_| AuthErrorCode::AuthNetwork)
    }

    fn login_worker(&self, work: &LoginWork) -> Result<Grant, AuthErrorCode> {
        self.check_work(work)?;
        let (code, verifier, redirect) = match work.flow {
            LoginFlow::DeviceCode => {
                let response = self.post_login(
                    work,
                    Endpoint::DeviceCode,
                    AuthBody::Json(json!({"client_id": CLIENT_ID})),
                )?;
                self.check_work(work)?;
                let device = device_code(
                    &response,
                    self.clock.now(),
                    work.expires_at.load(Ordering::Acquire),
                )?;
                self.present(
                    work,
                    LoginPresentation::DeviceCode {
                        verification_url: DEVICE_VERIFY.into(),
                        user_code: device.code.0.clone(),
                    },
                    device.expires_at,
                )?;
                let mut interval = device.interval;
                let mut delay = interval;
                loop {
                    let remaining = device.expires_at.saturating_sub(self.clock.now());
                    let wait = Duration::from_secs(delay.min(remaining)).min(
                        work.deadline
                            .lock()
                            .unwrap()
                            .saturating_duration_since(Instant::now()),
                    );
                    self.wait_login(work, wait)?;
                    let response = match self.post_login(
                        work,
                        Endpoint::DevicePoll,
                        AuthBody::Json(
                            json!({"device_auth_id": device.id.0, "user_code": device.code.0}),
                        ),
                    ) {
                        Ok(response) => response,
                        Err(AuthErrorCode::AuthNetwork) => {
                            // Polling does not consume the one-time authorization code.
                            // Keep the original deadlines and sliced cancellation checks.
                            delay = delay.saturating_mul(2).clamp(3, 60).max(interval);
                            continue;
                        }
                        Err(code) => return Err(code),
                    };
                    self.check_work(work)?;
                    match (response.status, error_code(&response.body)) {
                        (400 | 429, "slow_down") => {
                            // Defensive support; this is not a claimed private-endpoint contract.
                            interval = interval.saturating_add(5).min(900);
                            delay = delay.saturating_add(5).min(900).max(interval);
                            continue;
                        }
                        (
                            _,
                            "access_denied"
                            | "authorization_declined"
                            | "expired_token"
                            | "device_auth_disabled"
                            | "device_auth_not_allowed",
                        ) if response.status != 200 => return Err(login_error(&response)),
                        (403 | 404, _) => {
                            delay = interval;
                            continue;
                        }
                        (429 | 500..=599, _) => {
                            delay = response
                                .retry_after
                                .unwrap_or(delay.saturating_mul(2).clamp(3, 60))
                                .clamp(3, 60)
                                .max(interval);
                            continue;
                        }
                        _ => {
                            let (code, verifier) = poll_code(&response)?;
                            break (code, verifier, DEVICE_REDIRECT);
                        }
                    }
                }
            }
            LoginFlow::Browser => {
                let (listener, state, verifier, url) = browser_start()?;
                let deadline = work.expires_at.load(Ordering::Acquire);
                self.present(
                    work,
                    LoginPresentation::Browser {
                        authorization_url: url,
                    },
                    deadline,
                )?;
                let monotonic_deadline = *work.deadline.lock().unwrap();
                let mut next_observation = Instant::now();
                let code = receive_browser(
                    listener,
                    &state.0,
                    deadline,
                    monotonic_deadline,
                    self.clock.as_ref(),
                    &work.cancel,
                    || {
                        if Instant::now() >= next_observation {
                            self.observe_login(work)?;
                            next_observation = Instant::now() + LOGIN_OBSERVE_EVERY;
                        }
                        Ok(())
                    },
                )?;
                (code, verifier.0, BROWSER_REDIRECT)
            }
        };
        self.check_work(work)?;
        let response = self.post_login(
            work,
            Endpoint::Token,
            authorization_form(code, verifier, redirect),
        )?;
        self.check_work(work)?;
        if response.status != 200 {
            return Err(login_error(&response));
        }
        parse_grant(&response.body, self.clock.now(), None)
    }

    fn retire_login(session: &mut Session, work: &LoginWork) {
        if session
            .work
            .as_ref()
            .is_some_and(|current| current.id == work.id)
        {
            session.work = None;
            session.finished = Some(work.id.clone());
        }
    }

    fn remember_login_cleanup(&self, session: &mut Session, work: &LoginWork) {
        if session.cleanup.as_ref().is_none_or(|cleanup| {
            (cleanup.generation, cleanup.epoch) <= (work.generation, work.epoch)
        }) {
            session.cleanup = Some(LoginCleanup {
                id: work.id.clone(),
                owner_instance: self.instance.clone(),
                epoch: work.epoch,
                generation: work.generation,
            });
        }
    }

    fn defer_login_cleanup(&self, work: &LoginWork, code: AuthErrorCode) {
        work.complete_start(Err(code.into()));
        let mut session = self.session.lock().unwrap();
        Self::retire_login(&mut session, work);
        // docs/chatgpt-local-oauth.md §4: retain only a fenced cleanup request, never the parsed pair.
        // A later authoritative read resolves this before start_login examines pending.
        self.remember_login_cleanup(&mut session, work);
        self.publish(&mut session, None, Some(code));
    }

    fn finish_login(&self, work: &LoginWork, mut result: Result<Grant, AuthErrorCode>) {
        let mut cleanup_deadline = result.is_err().then(|| Instant::now() + LOGIN_SETTLE_WAIT);
        let custody_error = result.as_ref().err().copied().filter(|code| {
            matches!(
                code,
                AuthErrorCode::LocalStoreUnavailable
                    | AuthErrorCode::StoreLocked
                    | AuthErrorCode::StoreAccessDenied
                    | AuthErrorCode::StoreKeyMissing
                    | AuthErrorCode::StoreCorrupt
                    | AuthErrorCode::StoreSchemaUnsupported
                    | AuthErrorCode::InvalidStore
                    | AuthErrorCode::AuthBusy
            )
        });
        let mut commit_error = None;
        let mut failed_grant_write: Option<(String, u64)> = None;
        loop {
            // The original deadline/cancellation fences installation. Discard the
            // pair on terminal transition, but allow bounded token-free cleanup.
            if let Err(code) = self.check_work(work) {
                result = Err(code);
                cleanup_deadline.get_or_insert_with(|| Instant::now() + LOGIN_SETTLE_WAIT);
            }
            if let Err(code) = result {
                work.complete_start(Err(code.into()));
            }
            let settling = result.is_err();
            let deadline = cleanup_deadline.unwrap_or_else(|| *work.deadline.lock().unwrap());
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                if settling {
                    self.defer_login_cleanup(
                        work,
                        commit_error
                            .or(custody_error)
                            .unwrap_or(AuthErrorCode::AuthBusy),
                    );
                    return;
                }
                result = Err(AuthErrorCode::LoginExpired);
                cleanup_deadline = Some(Instant::now() + LOGIN_SETTLE_WAIT);
                continue;
            }
            let storage = match self.storage() {
                Ok(storage) => storage,
                Err(code) => {
                    self.defer_login_cleanup(work, code);
                    return;
                }
            };
            // No session gate is held during these bounded waits. AuthBusy retains
            // the result; it never starts another OAuth request or abandons pending.
            let transaction = storage
                .lock
                .acquire(remaining.min(LOGIN_OBSERVE_LOCK_WAIT), || {
                    !settling
                        && (work.cancel.load(Ordering::Acquire)
                            || self.ended.load(Ordering::Acquire))
                });
            let transaction = match transaction {
                Ok(transaction) => transaction,
                Err(AuthErrorCode::AuthBusy | AuthErrorCode::Cancelled) => continue,
                Err(code) => {
                    self.defer_login_cleanup(work, code);
                    return;
                }
            };
            let mut commit = || -> Result<(), AuthErrorCode> {
                let mut env = self.read_envelope()?;
                let mut session = self.session.lock().unwrap();
                if env.epoch != work.epoch
                    || !env.pending.as_ref().is_some_and(|pending| {
                        pending.id == work.id
                            && pending.owner_instance == self.instance
                            && pending.epoch == work.epoch
                    })
                {
                    Self::retire_login(&mut session, work);
                    if failed_grant_write.as_ref().is_some_and(|(id, revision)| {
                        env.revision == *revision
                            && env.grant.as_ref().is_some_and(|grant| grant.id == *id)
                    }) {
                        self.publish(&mut session, None, commit_error);
                    } else {
                        self.publish(&mut session, Some(&env), None);
                    }
                    return Ok(());
                }
                // docs/chatgpt-local-oauth.md §4: the session fence and final durable save share this gate.
                if let Err(code) = self.live(&session).and_then(|()| {
                    if session.generation != work.generation {
                        Err(AuthErrorCode::SessionEnded)
                    } else {
                        self.check_work(work)
                    }
                }) {
                    result = Err(code);
                }
                env.pending = None;
                env.fence()?;
                let error = result.as_ref().err().copied();
                let replaced = result.is_ok();
                if let Ok(grant) = &result {
                    env.grant = Some(grant.clone());
                }
                if let Err(code) = self.save(&mut env) {
                    if replaced {
                        failed_grant_write =
                            Some((env.grant.as_ref().unwrap().id.clone(), env.revision));
                    }
                    return Err(code);
                }
                if replaced {
                    session.fence = session.fence.saturating_add(1);
                }
                Self::retire_login(&mut session, work);
                if let Some(code) = commit_error.or(custody_error) {
                    self.publish(&mut session, None, Some(code));
                } else {
                    self.publish(&mut session, Some(&env), error);
                }
                Ok(())
            };
            let committed = commit();
            drop(transaction);
            match committed {
                Ok(()) => {
                    work.complete_start(Err(result
                        .err()
                        .unwrap_or(AuthErrorCode::NotCurrent)
                        .into()));
                    return;
                }
                Err(code) => {
                    result = Err(code);
                    if commit_error.is_some() {
                        self.defer_login_cleanup(work, code);
                        return;
                    }
                    // A failed save may have committed. Reread and clean up only
                    // this attempt, preserving both an old and an ambiguously saved grant.
                    commit_error = Some(code);
                    cleanup_deadline.get_or_insert_with(|| Instant::now() + LOGIN_SETTLE_WAIT);
                }
            }
        }
    }

    pub fn open_login(&self, login_id: &str) -> Result<(), AuthError> {
        let storage = self.storage()?;
        let _transaction = storage
            .lock
            .acquire(LOCK_WAIT, || self.ended.load(Ordering::Acquire))?;
        let env = self.load()?;
        let session = self.session.lock().unwrap();
        self.live(&session)?;
        let work = session
            .work
            .as_ref()
            .filter(|w| w.id == login_id)
            .ok_or(AuthErrorCode::NotCurrent)?;
        self.check_work(work)?;
        if !self.matches_work(&env, work, &session) {
            return Err(AuthErrorCode::NotCurrent.into());
        }
        let initial = work.presentation.lock().unwrap();
        let presentation = initial
            .as_ref()
            .and_then(|r| r.as_ref().ok())
            .ok_or(AuthErrorCode::NotCurrent)?;
        self.browser
            .as_ref()
            .ok_or(AuthErrorCode::BrowserUnavailable)?
            .open(presentation.presentation.url())
            .map_err(|_| AuthErrorCode::BrowserUnavailable.into())
    }

    pub fn cancel_login(&self, login_id: &str) -> Result<CancelOutcome, AuthError> {
        self.cancel_login_scoped(login_id, false)
    }

    /// Automatic caller cleanup must never cancel another host's pending login.
    /// Explicit user cancellation retains the cross-host `cancel_login` behavior.
    pub fn cancel_login_if_owned(&self, login_id: &str) -> Result<CancelOutcome, AuthError> {
        self.cancel_login_scoped(login_id, true)
    }

    fn cancel_login_scoped(
        &self,
        login_id: &str,
        only_owned: bool,
    ) -> Result<CancelOutcome, AuthError> {
        let owned = {
            let session = self.session.lock().unwrap();
            self.live(&session)?;
            let work = session
                .work
                .as_ref()
                .filter(|work| work.id == login_id && work.generation == session.generation);
            if only_owned && work.is_none() {
                return Ok(CancelOutcome::NotCurrent);
            }
            if let Some(work) = work {
                // Interrupt only this exact local worker before waiting for custody.
                work.cancel.store(true, Ordering::Release);
                work.ready.notify_all();
            }
            work.cloned()
        };
        let result = self.clear_login(login_id, only_owned.then_some(owned.as_deref()).flatten());
        if let Err(error) = &result {
            self.record_error(error.code);
        }
        result
    }
    fn clear_login(
        &self,
        login_id: &str,
        owned: Option<&LoginWork>,
    ) -> Result<CancelOutcome, AuthError> {
        let storage = self.storage()?;
        let _transaction = storage
            .lock
            .acquire(LOCK_WAIT, || self.ended.load(Ordering::Acquire))?;
        // Scoped cleanup must not normalize/expire an unrelated pending record.
        let mut env = if owned.is_some() {
            self.read_envelope()?
        } else {
            self.load()?
        };
        let mut session = self.session.lock().unwrap();
        self.live(&session)?;
        if env.pending.as_ref().is_none_or(|p| p.id != login_id) {
            return Ok(if session.finished.as_deref() == Some(login_id) {
                CancelOutcome::AlreadyFinished
            } else {
                CancelOutcome::NotCurrent
            });
        }
        if let Some(work) = owned {
            let pending = env.pending.as_ref().unwrap();
            if pending.owner_instance != self.instance
                || pending.epoch != work.epoch
                || env.epoch != work.epoch
                || work.generation != session.generation
            {
                return Ok(CancelOutcome::NotCurrent);
            }
        }
        env.pending = None;
        env.fence()?;
        self.save(&mut env)?;
        session.work = None;
        session.finished = Some(login_id.into());
        self.publish(&mut session, Some(&env), None);
        Ok(CancelOutcome::Cancelled)
    }

    /// Durable tombstone; a failed deletion suspends all use in this host.
    pub fn disconnect(&self) -> Result<(), AuthError> {
        {
            let mut session = self.session.lock().unwrap();
            if self.ended.load(Ordering::Acquire) {
                return Err(AuthErrorCode::SessionEnded.into());
            }
            session.suspended = true;
            session.fence = session.fence.saturating_add(1);
            if let Some(work) = session.work.take() {
                work.cancel.store(true, Ordering::Release);
                work.ready.notify_all();
                session.finished = Some(work.id.clone());
            }
            self.publish(&mut session, None, Some(AuthErrorCode::DisconnectFailed));
        }
        let remove = || -> Result<(), AuthErrorCode> {
            let storage = self.storage()?;
            let _transaction = storage
                .lock
                .acquire(LOCK_WAIT, || self.ended.load(Ordering::Acquire))?;
            let mut env = self.load()?;
            let mut session = self.session.lock().unwrap();
            if self.ended.load(Ordering::Acquire) {
                return Err(AuthErrorCode::SessionEnded);
            }
            env.fence()?;
            env.grant = None;
            env.pending = None;
            self.save(&mut env)?;
            session.suspended = false;
            self.publish(&mut session, Some(&env), None);
            Ok(())
        };
        remove().map_err(|_| AuthErrorCode::DisconnectFailed.into())
    }

    /// Revoke all new work before native command ownership is retired. This
    /// immediate fence does not wait for already-sent refresh/login settlement.
    pub fn revoke_session(&self) {
        let mut session = self.session.lock().unwrap();
        let already_ended = self.ended.swap(true, Ordering::AcqRel);
        if !already_ended {
            session.generation = session.generation.saturating_add(1);
            session.fence = session.fence.saturating_add(1);
        }
        if let Some(work) = session.work.take() {
            work.cancel.store(true, Ordering::Release);
            work.ready.notify_all();
            self.remember_login_cleanup(&mut session, &work);
        }
        self.publish(&mut session, None, Some(AuthErrorCode::SessionEnded));
    }

    /// End an Any session. Already-sent refreshes settle in its old slot first;
    /// native logout/account switch must call this before discarding the service.
    pub fn shutdown(&self) {
        self.revoke_session();
        let mut session = self.session.lock().unwrap();
        while session.refreshes > 0 || session.login_workers > 0 {
            session = self.refresh_settled.wait(session).unwrap();
        }
        // Workers can retire into cleanup after shutdown advances the visible
        // generation. Capture their latest token-free request under its original
        // owner identity, including when no active work existed at shutdown entry.
        let cleanup = session.cleanup.clone();
        drop(session);
        if let Some(cleanup) = cleanup {
            self.finish_shutdown_cleanup(&cleanup);
        }
    }

    fn finish_shutdown_cleanup(&self, cleanup: &LoginCleanup) {
        if cleanup.owner_instance != self.instance || cleanup.generation != self.owner_generation {
            return;
        }
        let Ok(storage) = self.storage() else {
            return;
        };
        let deadline = Instant::now() + LOGIN_SETTLE_WAIT;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return; // Retain metadata so repeated shutdown can retry recovered custody.
            }
            let settle = || -> Result<(), AuthErrorCode> {
                let _transaction = storage
                    .lock
                    .acquire(remaining.min(LOGIN_OBSERVE_LOCK_WAIT), || false)?;
                let mut env = self.read_envelope()?;
                if self.clear_matching_pending(&mut env, cleanup)? {
                    self.save(&mut env)?;
                }
                let mut session = self.session.lock().unwrap();
                if session.cleanup.as_ref() == Some(cleanup) {
                    session.cleanup = None;
                }
                self.publish(&mut session, Some(&env), None);
                Ok(())
            };
            match settle() {
                Ok(()) => return,
                Err(AuthErrorCode::AuthBusy) => {}
                Err(_) => std::thread::sleep(
                    LOGIN_OBSERVE_LOCK_WAIT.min(deadline.saturating_duration_since(Instant::now())),
                ),
            }
        }
    }

    #[cfg(test)]
    pub(crate) fn authorize_dispatch(
        &self,
        rejected: Option<&DispatchStamp>,
        cancel: &AtomicBool,
    ) -> Result<DispatchCredential, AuthError> {
        self.authorize_dispatch_until(
            None,
            rejected,
            cancel,
            Instant::now() + Duration::from_secs(120),
        )
    }

    // docs/chatgpt-local-oauth.md §4/§6: policy is validated before this custody seam.
    pub(crate) fn authorize_dispatch_until(
        &self,
        original: Option<&DispatchStamp>,
        rejected: Option<&DispatchStamp>,
        cancel: &AtomicBool,
        request_deadline: Instant,
    ) -> Result<DispatchCredential, AuthError> {
        let result = self.authorize(original, rejected, cancel, request_deadline);
        if let Err(code) = result {
            if !matches!(
                code,
                AuthErrorCode::NotConnected
                    | AuthErrorCode::GrantRejected
                    | AuthErrorCode::RefreshUncertain
                    | AuthErrorCode::AccountChanged
                    | AuthErrorCode::AuthRateLimited
            ) {
                self.record_error(code);
            }
        }
        result.map_err(Into::into)
    }
    fn authorize(
        &self,
        original: Option<&DispatchStamp>,
        rejected: Option<&DispatchStamp>,
        cancel: &AtomicBool,
        request_deadline: Instant,
    ) -> Result<DispatchCredential, AuthErrorCode> {
        if Instant::now() >= request_deadline {
            return Err(AuthErrorCode::RequestExpired);
        }
        self.live(&self.session.lock().unwrap())?;
        if original.is_some_and(|stamp| !self.dispatch_live(stamp)) {
            return Err(AuthErrorCode::AccountChanged);
        }
        let storage = self.storage()?;
        let _transaction = storage.lock.acquire(
            LOCK_WAIT.min(request_deadline.saturating_duration_since(Instant::now())),
            || {
                cancel.load(Ordering::Acquire)
                    || self.ended.load(Ordering::Acquire)
                    || Instant::now() >= request_deadline
            },
        )?;
        if Instant::now() >= request_deadline {
            return Err(AuthErrorCode::RequestExpired);
        }
        let mut env = self.load()?;
        self.publish(&mut self.session.lock().unwrap(), Some(&env), None);
        let grant = env.grant.as_ref().ok_or(AuthErrorCode::NotConnected)?;
        if let GrantState::ReauthRequired { reason } = grant.state {
            return Err(reason);
        }
        // Recheck the original logical call binding under the slot lock, before
        // any refresh. A guest transient retry may not adopt a replacement login.
        if original.is_some_and(|stamp| stamp.grant_id != grant.id || !self.dispatch_live(stamp))
            || rejected.is_some_and(|stamp| stamp.grant_id != grant.id)
        {
            return Err(AuthErrorCode::AccountChanged);
        }
        if grant
            .retry_after
            .is_some_and(|until| until > self.clock.now())
        {
            return Err(AuthErrorCode::AuthRateLimited);
        }
        let force = rejected.is_some_and(|stamp| stamp.pair_revision == grant.pair_revision);
        let outcome = if force || grant.expires_at <= self.clock.now().saturating_add(120) {
            self.refresh(&mut env, cancel, request_deadline)?;
            "refreshed"
        } else if rejected.is_some() {
            "adopted"
        } else {
            "cached"
        };
        let mut session = self.session.lock().unwrap();
        self.live(&session)?;
        if original.is_some_and(|stamp| {
            stamp.session_generation != session.generation || stamp.fence != session.fence
        }) {
            return Err(AuthErrorCode::AccountChanged);
        }
        if cancel.load(Ordering::Acquire) {
            return Err(AuthErrorCode::Cancelled);
        }
        self.publish(&mut session, Some(&env), None);
        let grant = env.grant.as_ref().ok_or(AuthErrorCode::NotConnected)?;
        if Instant::now() >= request_deadline {
            return Err(AuthErrorCode::RequestExpired);
        }
        Ok(DispatchCredential {
            outcome,
            access: grant.access.clone(),
            account_id: grant.principal.account_id.clone(),
            residency: grant.residency.clone(),
            stamp: DispatchStamp {
                grant_id: grant.id.clone(),
                pair_revision: grant.pair_revision,
                session_generation: session.generation,
                fence: session.fence,
            },
        })
    }

    fn refresh(
        &self,
        env: &mut GrantEnvelope,
        cancel: &AtomicBool,
        request_deadline: Instant,
    ) -> Result<(), AuthErrorCode> {
        if Instant::now() >= request_deadline {
            return Err(AuthErrorCode::RequestExpired);
        }
        let old = env
            .grant
            .as_ref()
            .ok_or(AuthErrorCode::NotConnected)?
            .clone();
        {
            let mut session = self.session.lock().unwrap();
            self.live(&session)?;
            if cancel.load(Ordering::Acquire) {
                return Err(AuthErrorCode::Cancelled);
            }
            env.grant.as_mut().unwrap().state = GrantState::RefreshInflight {
                attempt_id: random_id(),
            };
            // docs/chatgpt-local-oauth.md §4: durable before send; failed replacement authorizes nothing.
            self.save(env)?;
            session.refreshes += 1;
        }
        let _settle = RefreshSettlement(self);
        let result = if cancel.load(Ordering::Acquire)
            || Instant::now() >= request_deadline
            || self.live(&self.session.lock().unwrap()).is_err()
        {
            env.grant = Some(old);
            self.save(env)?;
            return Err(AuthErrorCode::Cancelled);
        } else {
            self.transport.post(
                Endpoint::Token,
                AuthBody::Form(vec![
                    ("grant_type", "refresh_token".into()),
                    ("client_id", CLIENT_ID.into()),
                    ("refresh_token", old.refresh.0.clone()),
                ]),
                Duration::from_secs(30),
            )
        };
        let outcome = match result {
            Ok(reply) if reply.status == 200 => {
                parse_grant(&reply.body, self.clock.now(), Some(&old)).map_err(|code| {
                    if code == AuthErrorCode::AccountChanged {
                        code
                    } else {
                        AuthErrorCode::RefreshUncertain
                    }
                })
            }
            Ok(reply) if rejected_grant(&reply) => Err(AuthErrorCode::GrantRejected),
            Err(TransportFailure::BeforeSend) => {
                env.grant = Some(old);
                self.save(env)?;
                return Err(AuthErrorCode::AuthNetwork);
            }
            _ => Err(AuthErrorCode::RefreshUncertain),
        };
        let error = outcome.as_ref().err().copied();
        match outcome {
            Ok(grant) => env.grant = Some(grant),
            Err(reason) => env.grant.as_mut().unwrap().block(reason),
        }
        // Settles even after session shutdown; publication/dispatch still check its fence.
        self.save(env)?;
        self.publish(&mut self.session.lock().unwrap(), Some(env), None);
        error.map_or(Ok(()), Err)
    }

    pub(crate) fn dispatch_live(&self, stamp: &DispatchStamp) -> bool {
        let session = self.session.lock().unwrap();
        self.live(&session).is_ok()
            && session.generation == stamp.session_generation
            && session.fence == stamp.fence
    }

    pub(crate) fn reject_dispatch(
        &self,
        stamp: &DispatchStamp,
        cancel: &AtomicBool,
        deadline: Instant,
    ) -> Result<(), AuthError> {
        let storage = self.storage()?;
        let _transaction = storage.lock.acquire(
            LOCK_WAIT.min(deadline.saturating_duration_since(Instant::now())),
            || {
                cancel.load(Ordering::Acquire)
                    || Instant::now() >= deadline
                    || self.ended.load(Ordering::Acquire)
            },
        )?;
        if !self.dispatch_live(stamp) {
            return Err(AuthErrorCode::SessionEnded.into());
        }
        let mut env = self.load()?;
        if let Some(grant) = env.grant.as_mut() {
            if grant.id == stamp.grant_id && grant.pair_revision == stamp.pair_revision {
                grant.block(AuthErrorCode::GrantRejected);
                self.save(&mut env)?;
                self.publish(&mut self.session.lock().unwrap(), Some(&env), None);
            }
        }
        Ok(())
    }

    pub(crate) fn check_completion(
        &self,
        stamp: &DispatchStamp,
        cancel: &AtomicBool,
        request_deadline: Instant,
    ) -> Result<CompletionStatus, AuthError> {
        // docs/chatgpt-local-oauth.md §4: on AuthBusy retain the response and recheck this stamp with
        // the same deadline; a retry here never authorizes another network send.
        let check_request = || {
            if cancel.load(Ordering::Acquire) {
                Err(AuthErrorCode::Cancelled)
            } else if Instant::now() >= request_deadline {
                Err(AuthErrorCode::RequestExpired)
            } else {
                Ok(())
            }
        };
        let check = || -> Result<CompletionStatus, AuthErrorCode> {
            check_request()?;
            {
                let session = self.session.lock().unwrap();
                if self.live(&session).is_err()
                    || session.generation != stamp.session_generation
                    || session.fence != stamp.fence
                {
                    return Ok(CompletionStatus::Invalidated);
                }
            }
            let storage = self.storage()?;
            let transaction = storage.lock.acquire(
                LOCK_WAIT.min(request_deadline.saturating_duration_since(Instant::now())),
                || cancel.load(Ordering::Acquire) || self.ended.load(Ordering::Acquire),
            );
            check_request()?;
            if self.ended.load(Ordering::Acquire) {
                return Ok(CompletionStatus::Invalidated);
            }
            let _transaction = transaction?;
            let env = self.load()?;
            let mut session = self.session.lock().unwrap();
            self.publish(&mut session, Some(&env), None);
            check_request()?;
            let current = self.live(&session).is_ok()
                && session.generation == stamp.session_generation
                && session.fence == stamp.fence
                && env.grant.as_ref().is_some_and(|g| {
                    g.id == stamp.grant_id && matches!(g.state, GrantState::Ready)
                });
            Ok(if current {
                CompletionStatus::Accepted
            } else {
                CompletionStatus::Invalidated
            })
        };
        match check() {
            Ok(status) => Ok(status),
            Err(code) => {
                if !matches!(
                    code,
                    AuthErrorCode::Cancelled | AuthErrorCode::RequestExpired
                ) {
                    self.record_error(code);
                }
                Err(code.into())
            }
        }
    }
}

struct RefreshSettlement<'a>(&'a ChatGptAuth);
impl Drop for RefreshSettlement<'_> {
    fn drop(&mut self) {
        self.0.session.lock().unwrap().refreshes -= 1;
        self.0.refresh_settled.notify_all();
    }
}

struct LoginSettlement<'a>(&'a ChatGptAuth);
impl Drop for LoginSettlement<'_> {
    fn drop(&mut self) {
        self.0.session.lock().unwrap().login_workers -= 1;
        self.0.refresh_settled.notify_all();
    }
}

pub(crate) struct DispatchCredential {
    pub outcome: &'static str,
    pub access: Secret,
    pub account_id: String,
    pub residency: Option<String>,
    pub stamp: DispatchStamp,
}
impl std::fmt::Debug for DispatchCredential {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("DispatchCredential([REDACTED])")
    }
}
#[derive(Clone)]
pub(crate) struct DispatchStamp {
    grant_id: String,
    pair_revision: u64,
    session_generation: u64,
    fence: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum CompletionStatus {
    Accepted,
    Invalidated,
}

#[cfg(test)]
mod tests;
