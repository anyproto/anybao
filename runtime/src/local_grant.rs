//! Host-only durable custody and slot transactions (docs/chatgpt-local-oauth.md §2–4).

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock, TryLockError, Weak};
use std::time::{Duration, Instant};

pub const CHATGPT_REF: &str = "connector.oauth.openai-codex";
pub const CHATGPT_PROVIDER: &str = "openai-codex";
const MAX_ENVELOPE_BYTES: usize = 131_072;

pub fn is_reserved_ref(key: &str) -> bool {
    key == CHATGPT_REF
        || key
            .strip_prefix(CHATGPT_REF)
            .is_some_and(|s| s.starts_with('.'))
}

pub(crate) fn reject_reserved_ref(key: &str) -> anyhow::Result<()> {
    if is_reserved_ref(key) {
        anyhow::bail!("host_only: ChatGPT credentials require native local authentication");
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AuthErrorCode {
    LocalStoreUnavailable,
    StoreLocked,
    StoreAccessDenied,
    StoreAccessUnsupported,
    StoreKeyMissing,
    StoreCorrupt,
    StoreSchemaUnsupported,
    InvalidStore,
    IdentityUnavailable,
    AuthBusy,
    RequestExpired,
    SessionEnded,
    Cancelled,
    NotConnected,
    LoginInProgress,
    LoginExpired,
    ConsentDenied,
    DeviceFlowDisabled,
    AuthNetwork,
    AuthRateLimited,
    TokenExchangeFailed,
    UnsupportedTokenShape,
    PortBusy,
    CallbackMismatch,
    BrowserUnavailable,
    NotCurrent,
    RefreshUncertain,
    GrantRejected,
    AccountChanged,
    DisconnectFailed,
}

impl AuthErrorCode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::LocalStoreUnavailable => "local_store_unavailable",
            Self::StoreLocked => "store_locked",
            Self::StoreAccessDenied => "store_access_denied",
            Self::StoreAccessUnsupported => "store_access_unsupported",
            Self::StoreKeyMissing => "store_key_missing",
            Self::StoreCorrupt => "store_corrupt",
            Self::StoreSchemaUnsupported => "store_schema_unsupported",
            Self::InvalidStore => "invalid_store",
            Self::IdentityUnavailable => "identity_unavailable",
            Self::AuthBusy => "auth_busy",
            Self::RequestExpired => "request_expired",
            Self::SessionEnded => "session_ended",
            Self::Cancelled => "cancelled",
            Self::NotConnected => "not_connected",
            Self::LoginInProgress => "login_in_progress",
            Self::LoginExpired => "login_expired",
            Self::ConsentDenied => "consent_denied",
            Self::DeviceFlowDisabled => "device_flow_disabled",
            Self::AuthNetwork => "auth_network",
            Self::AuthRateLimited => "auth_rate_limited",
            Self::TokenExchangeFailed => "token_exchange_failed",
            Self::UnsupportedTokenShape => "unsupported_token_shape",
            Self::PortBusy => "port_busy",
            Self::CallbackMismatch => "callback_mismatch",
            Self::BrowserUnavailable => "browser_unavailable",
            Self::NotCurrent => "not_current",
            Self::RefreshUncertain => "refresh_uncertain",
            Self::GrantRejected => "grant_rejected",
            Self::AccountChanged => "account_changed",
            Self::DisconnectFailed => "disconnect_failed",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LoginFlow {
    DeviceCode,
    Browser,
}

/// Constructed only from the authenticated native session (docs/chatgpt-local-oauth.md §3).
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GrantSlot {
    application: String,
    profile: String,
    account: String,
    device: String,
}

impl std::fmt::Debug for GrantSlot {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("GrantSlot([host identity])")
    }
}

impl GrantSlot {
    pub fn new(
        application_namespace_and_channel: &str,
        stable_profile_id: &str,
        any_account_id: &str,
        device_id: &str,
    ) -> Result<Self, AuthErrorCode> {
        if ![
            application_namespace_and_channel,
            stable_profile_id,
            any_account_id,
            device_id,
        ]
        .iter()
        .all(|s| valid_identifier(s))
        {
            return Err(AuthErrorCode::IdentityUnavailable);
        }
        Ok(Self {
            application: application_namespace_and_channel.into(),
            profile: stable_profile_id.into(),
            account: any_account_id.into(),
            device: device_id.into(),
        })
    }

    /// A versioned, unambiguous native-store and OS-lock key, never a token hash.
    pub fn storage_key(&self, store_namespace: &str) -> Result<String, AuthErrorCode> {
        if !valid_identifier(store_namespace) {
            return Err(AuthErrorCode::InvalidStore);
        }
        let tuple = serde_json::to_vec(&(
            "anybao.local-grant.v1",
            store_namespace,
            &self.application,
            &self.profile,
            &self.account,
            &self.device,
            CHATGPT_PROVIDER,
        ))
        .map_err(|_| AuthErrorCode::InvalidStore)?;
        Ok(hex::encode(Sha256::digest(tuple)))
    }

    pub(crate) fn device_id(&self) -> &str {
        &self.device
    }
}

fn valid_identifier(s: &str) -> bool {
    !s.trim().is_empty() && s.len() <= 256 && !s.chars().any(char::is_control)
}

/// Store errors intentionally carry no platform diagnostic or record contents.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GrantStoreError {
    Locked,
    AccessDenied,
    AccessUnsupported,
    MissingKey,
    CorruptRecord,
    UnsupportedSchema,
    Cancelled,
    NotCurrent,
    Unavailable,
    InvalidRecord,
}

impl From<GrantStoreError> for AuthErrorCode {
    fn from(value: GrantStoreError) -> Self {
        match value {
            GrantStoreError::Locked => Self::StoreLocked,
            GrantStoreError::AccessDenied => Self::StoreAccessDenied,
            GrantStoreError::AccessUnsupported => Self::StoreAccessUnsupported,
            GrantStoreError::MissingKey => Self::StoreKeyMissing,
            GrantStoreError::CorruptRecord => Self::StoreCorrupt,
            GrantStoreError::UnsupportedSchema => Self::StoreSchemaUnsupported,
            GrantStoreError::Cancelled => Self::Cancelled,
            GrantStoreError::NotCurrent => Self::NotCurrent,
            GrantStoreError::Unavailable => Self::LocalStoreUnavailable,
            GrantStoreError::InvalidRecord => Self::InvalidStore,
        }
    }
}

/// Native implementations must use device-local secure custody. Ordinary read
/// and replace never prompt and return within a bounded time. Replacement is atomic
/// and durable; even an error must leave a complete old or new envelope. Only
/// ChatGptAuth calls these methods, under its slot transaction (docs/chatgpt-local-oauth.md §3).
pub trait LocalGrantStore: Send + Sync {
    fn namespace(&self) -> &str;
    fn read(&self, slot: &GrantSlot) -> Result<Option<GrantEnvelope>, GrantStoreError>;
    fn replace(&self, slot: &GrantSlot, envelope: &GrantEnvelope) -> Result<(), GrantStoreError>;
    /// Explicit native user action only. Preparation may prompt with a bounded,
    /// cancellable lifetime, but must not modify the canonical record. The core
    /// holds the slot transaction and commits under session/cancellation gates.
    fn prepare_access(
        &self,
        _slot: &GrantSlot,
        _empty: &GrantEnvelope,
        _cancelled: &(dyn Fn() -> bool + Sync),
    ) -> Result<Box<dyn PreparedGrantAccess>, GrantStoreError> {
        Err(GrantStoreError::AccessUnsupported)
    }
    /// Identify only a recognized, demonstrably unreadable current record.
    /// Unknown formats and readable grants must never yield a reset candidate.
    fn recovery(&self, _slot: &GrantSlot) -> Result<StoreRecovery, GrantStoreError> {
        Err(GrantStoreError::Unavailable)
    }
    /// Recheck exact bytes AND unrecoverability under the same slot lock. A
    /// failure must leave a complete old record or an identifiable reset marker.
    fn reset(&self, _slot: &GrantSlot, _expected: &StoreRecovery) -> Result<(), GrantStoreError> {
        Err(GrantStoreError::Unavailable)
    }
}

/// A prepared encrypted candidate; secrets and paths never leave native custody.
pub trait PreparedGrantAccess: Send {
    fn commit(self: Box<Self>) -> Result<(), GrantStoreError>;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecoveryReason {
    MissingKey,
    CorruptRecord,
}

/// Host-only exact-record identity. Never serialize it or expose it through IPC.
#[derive(Clone, PartialEq, Eq)]
pub struct StoreRecovery {
    pub fingerprint: [u8; 32],
    pub reason: RecoveryReason,
}

#[derive(Clone, Serialize, Deserialize)]
pub(crate) struct Secret(pub(crate) String);

impl std::fmt::Debug for Secret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("[REDACTED]")
    }
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Principal {
    pub issuer: String,
    pub subject: String,
    pub account_id: String,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(tag = "state", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum GrantState {
    Ready,
    RefreshInflight { attempt_id: String },
    ReauthRequired { reason: AuthErrorCode },
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Grant {
    pub id: String,
    pub pair_revision: u64,
    pub access: Secret,
    pub refresh: Secret,
    pub expires_at: u64,
    pub principal: Principal,
    pub account_label: Option<String>,
    pub plan_type: Option<String>,
    pub residency: Option<String>,
    pub state: GrantState,
    pub retry_after: Option<u64>,
}

impl Grant {
    pub(crate) fn block(&mut self, reason: AuthErrorCode) {
        self.state = GrantState::ReauthRequired { reason };
        self.access.0.clear();
        self.refresh.0.clear();
        self.retry_after = None;
    }
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PendingGrantLogin {
    pub id: String,
    pub owner_instance: String,
    pub epoch: u64,
    pub flow: LoginFlow,
    pub expires_at: u64,
}

/// Opaque host-only record. Encode/decode exist exclusively for native custody;
/// this type must never be bound to a renderer command or diagnostic (docs/chatgpt-local-oauth.md §3).
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GrantEnvelope {
    pub(crate) schema_version: u32,
    pub(crate) slot: GrantSlot,
    pub(crate) revision: u64,
    pub(crate) epoch: u64,
    pub(crate) pending: Option<PendingGrantLogin>,
    pub(crate) grant: Option<Grant>,
}

impl std::fmt::Debug for GrantEnvelope {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("GrantEnvelope([REDACTED])")
    }
}

impl GrantEnvelope {
    pub fn decode(bytes: &[u8]) -> Result<Self, GrantStoreError> {
        if bytes.len() > MAX_ENVELOPE_BYTES {
            return Err(GrantStoreError::InvalidRecord);
        }
        serde_json::from_slice(bytes).map_err(|_| GrantStoreError::InvalidRecord)
    }

    pub fn encode(&self) -> Result<Vec<u8>, GrantStoreError> {
        let bytes = serde_json::to_vec(self).map_err(|_| GrantStoreError::InvalidRecord)?;
        if bytes.len() > MAX_ENVELOPE_BYTES {
            return Err(GrantStoreError::InvalidRecord);
        }
        Ok(bytes)
    }

    pub(crate) fn empty(slot: &GrantSlot) -> Self {
        Self {
            schema_version: 1,
            slot: slot.clone(),
            revision: 0,
            epoch: 0,
            pending: None,
            grant: None,
        }
    }

    pub(crate) fn bump(&mut self) -> Result<(), AuthErrorCode> {
        self.revision = self
            .revision
            .checked_add(1)
            .ok_or(AuthErrorCode::InvalidStore)?;
        Ok(())
    }

    pub(crate) fn fence(&mut self) -> Result<(), AuthErrorCode> {
        self.epoch = self
            .epoch
            .checked_add(1)
            .ok_or(AuthErrorCode::InvalidStore)?;
        Ok(())
    }
}

/// One private, stable application directory shared by every process using the
/// native store. Lock files contain no credentials and must never be deleted or
/// replaced while the application is installed (docs/chatgpt-local-oauth.md §3).
#[derive(Clone, Debug)]
pub struct GrantLockDirectory(PathBuf);

impl GrantLockDirectory {
    pub fn new(path: impl AsRef<Path>) -> Result<Self, AuthErrorCode> {
        let path = path.as_ref();
        if !path.is_absolute() {
            return Err(AuthErrorCode::InvalidStore);
        }
        let mut builder = std::fs::DirBuilder::new();
        builder.recursive(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            builder.mode(0o700);
        }
        builder
            .create(path)
            .map_err(|_| AuthErrorCode::LocalStoreUnavailable)?;
        let metadata =
            std::fs::symlink_metadata(path).map_err(|_| AuthErrorCode::LocalStoreUnavailable)?;
        if !metadata.is_dir() || metadata.file_type().is_symlink() {
            return Err(AuthErrorCode::InvalidStore);
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if metadata.permissions().mode() & 0o077 != 0 {
                return Err(AuthErrorCode::InvalidStore);
            }
        }
        Ok(Self(
            std::fs::canonicalize(path).map_err(|_| AuthErrorCode::InvalidStore)?,
        ))
    }

    pub(crate) fn slot_lock(
        &self,
        slot: &GrantSlot,
        namespace: &str,
    ) -> Result<SlotLock, AuthErrorCode> {
        let path = self
            .0
            .join(format!("{}.lock", slot.storage_key(namespace)?));
        type Gates = Mutex<BTreeMap<PathBuf, Weak<Mutex<()>>>>;
        static GATES: OnceLock<Gates> = OnceLock::new();
        let mut gates = GATES
            .get_or_init(Default::default)
            .lock()
            .map_err(|_| AuthErrorCode::LocalStoreUnavailable)?;
        gates.retain(|_, gate| gate.strong_count() > 0);
        let gate = gates.get(&path).and_then(Weak::upgrade).unwrap_or_default();
        gates.insert(path.clone(), Arc::downgrade(&gate));
        Ok(SlotLock { path, gate })
    }
}

pub(crate) struct SlotLock {
    path: PathBuf,
    gate: Arc<Mutex<()>>,
}

pub(crate) struct SlotTransaction<'a> {
    _gate: MutexGuard<'a, ()>,
    _file: File,
}

impl SlotLock {
    /// Reserve before a readiness-bearing runtime starts. The counter shares
    /// the canonical slot lock and scope, but contains no credential data.
    pub(crate) fn reserve_generation(&self) -> Result<u64, AuthErrorCode> {
        let _transaction = self.acquire(Duration::from_secs(5), || false)?;
        crate::readiness::reserve_generation_file(&self.path.with_extension("generation"))
    }

    pub(crate) fn acquire(
        &self,
        timeout: Duration,
        cancelled: impl Fn() -> bool,
    ) -> Result<SlotTransaction<'_>, AuthErrorCode> {
        let deadline = Instant::now() + timeout;
        let check = || {
            if cancelled() {
                Err(AuthErrorCode::Cancelled)
            } else if Instant::now() >= deadline {
                Err(AuthErrorCode::AuthBusy)
            } else {
                Ok(())
            }
        };
        let gate = loop {
            check()?;
            match self.gate.try_lock() {
                Ok(gate) => break gate,
                Err(TryLockError::WouldBlock) => std::thread::sleep(Duration::from_millis(20)),
                Err(TryLockError::Poisoned(_)) => return Err(AuthErrorCode::LocalStoreUnavailable),
            }
        };
        if let Ok(metadata) = std::fs::symlink_metadata(&self.path) {
            if !metadata.is_file() || metadata.file_type().is_symlink() {
                return Err(AuthErrorCode::InvalidStore);
            }
        }
        let mut options = OpenOptions::new();
        options.create(true).truncate(false).read(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let file = options
            .open(&self.path)
            .map_err(|_| AuthErrorCode::LocalStoreUnavailable)?;
        loop {
            check()?;
            match file.try_lock() {
                Ok(()) => {
                    return Ok(SlotTransaction {
                        _gate: gate,
                        _file: file,
                    })
                }
                Err(std::fs::TryLockError::WouldBlock) => {
                    std::thread::sleep(Duration::from_millis(20));
                }
                Err(std::fs::TryLockError::Error(_)) => {
                    return Err(AuthErrorCode::LocalStoreUnavailable)
                }
            }
        }
    }
}

#[cfg(test)]
mod tests;
