//! The worker protocol mirror (ADR-034 §7): the B0 contract Kotlin freezes
//! in `WorkerProtocol.kt`, with the same bounds, so the native command
//! consumer (`session.rs`) and the JNI adapter speak exactly what main
//! sends. Pure data and validation; no thread, no I/O, no fence logic (the
//! fence is `grant.rs`). The one Rust-side addition is the calibration
//! bracket on Start: a worker-local input sampled in the `:bao` process
//! around the command's arrival, never a Binder field (N2 design Q1).
use super::grant::{ActivationIntent, CloseReason, GrantError, OpenGrant};

/// `MAX_GRANT_MS` in `WorkerProtocol.kt`: a grant spans at most 30 s.
pub const MAX_GRANT_MS: u64 = 30_000;
/// `CALLBACK_BUDGET_MS`: a callback for an accepted command is due within 5 s.
pub const CALLBACK_BUDGET_MS: u64 = 5_000;

/// A payload outside the frozen bounds, refused before any fence sees it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct InvalidPayload(pub &'static str);

impl std::fmt::Display for InvalidPayload {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "invalid worker payload: {}", self.0)
    }
}
impl std::error::Error for InvalidPayload {}

/// Kotlin `boundedId`: 1..=256 bytes, every byte printable ASCII (0x21..=0x7E).
pub fn bounded_id(value: &str) -> bool {
    (1..=256).contains(&value.len()) && value.bytes().all(|b| (0x21..=0x7E).contains(&b))
}

/// Kotlin's `baseUrl` rule: `http://127.0.0.1:<port>`, at most 64 bytes,
/// one to five digits, port 1..=65535.
pub fn loopback_origin(value: &str) -> bool {
    let Some(port) = value.strip_prefix("http://127.0.0.1:") else {
        return false;
    };
    value.len() <= 64
        && (1..=5).contains(&port.len())
        && port.bytes().all(|b| b.is_ascii_digit())
        && port.parse::<u32>().is_ok_and(|p| (1..=65535).contains(&p))
}

fn intent_valid(intent: &ActivationIntent) -> bool {
    intent.intent_revision > 0 && bounded_id(&intent.activation_id)
}

/// `WorkerLease`: what main validated (ADR-034 §4) and every command repeats.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WorkerLease {
    pub main_nonce: String,
    pub generation: u64,
    pub base_url: String,
    pub auth_epoch: u64,
    pub account_id: String,
    pub self_peer_id: String,
}

impl WorkerLease {
    pub fn validate(&self) -> Result<(), InvalidPayload> {
        if !bounded_id(&self.main_nonce)
            || !bounded_id(&self.account_id)
            || !bounded_id(&self.self_peer_id)
        {
            return Err(InvalidPayload("lease id out of bounds"));
        }
        if self.generation == 0 || self.auth_epoch == 0 {
            return Err(InvalidPayload(
                "lease generation/authEpoch must be positive",
            ));
        }
        if !loopback_origin(&self.base_url) {
            return Err(InvalidPayload(
                "lease baseUrl is not a literal loopback origin",
            ));
        }
        Ok(())
    }
}

/// `WorkerRequest`: request ids are unique per session and are correlation
/// ids, never an ordering (ADR-034 §7).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WorkerRequest {
    pub request_id: u64,
    pub session_id: String,
    pub lease: WorkerLease,
}

impl WorkerRequest {
    pub fn validate(&self) -> Result<(), InvalidPayload> {
        if self.request_id == 0 {
            return Err(InvalidPayload("requestId must be positive"));
        }
        if !bounded_id(&self.session_id) {
            return Err(InvalidPayload("sessionId out of bounds"));
        }
        self.lease.validate()
    }
}

/// Fixed APK resources only: never a caller-provided source, path or listener.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WorkerArtifact {
    AndroidPocV1,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WorkerStartOptions {
    pub artifact: WorkerArtifact,
    /// Android `elapsedRealtime` when main built the Start.
    pub elapsed_realtime_at_start: u64,
}

/// The worker-local clock bracket (ADR-034 §7 "calibrated against Android
/// elapsedRealtime at session start"): native CLOCK_BOOTTIME sampled
/// immediately before and after one Android `elapsedRealtime` read, all in
/// milliseconds, in the `:bao` process. `grant::CalibratedBootClock` refuses
/// an unbracketed or future sample; there is no adjustable offset.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ClockCalibration {
    pub native_before: u64,
    pub android_elapsed: u64,
    pub native_after: u64,
}

impl ClockCalibration {
    pub fn validate(&self) -> Result<(), InvalidPayload> {
        if self.native_before > self.android_elapsed || self.android_elapsed > self.native_after {
            return Err(InvalidPayload("calibration bracket is not ordered"));
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReleaseReason {
    UserStop,
    OwnershipLost,
    ActivationFailed,
    AccountTransition,
}

/// `AdmissionDecision`: Open carries the grant exactly as `grant::OpenGrant`
/// (intent pair, evidence sequence, elapsedRealtime window, grade); Close
/// carries its reason and, for a revoking reason only, the barrier.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AdmissionDecision {
    Open(OpenGrant),
    Close {
        reason: CloseReason,
        revoke_through_intent_revision: Option<u64>,
    },
}

impl AdmissionDecision {
    pub fn validate(&self) -> Result<(), InvalidPayload> {
        match self {
            AdmissionDecision::Open(grant) => {
                if grant.evidence_seq == 0 {
                    return Err(InvalidPayload("evidenceSeq must be positive"));
                }
                if !intent_valid(&grant.intent) {
                    return Err(InvalidPayload("open grant intent out of bounds"));
                }
                Ok(())
            }
            AdmissionDecision::Close {
                reason,
                revoke_through_intent_revision,
            } => {
                if reason.revokes_intent() != revoke_through_intent_revision.is_some() {
                    return Err(InvalidPayload(
                        "a revoking close carries a barrier, a non-revoking close none",
                    ));
                }
                Ok(())
            }
        }
    }
}

/// The six commands. `Start` carries the calibration bracket beside the
/// Kotlin fields; the opaque owner Binder stays in the JNI adapter.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum WorkerCommand {
    Start {
        request: WorkerRequest,
        options: WorkerStartOptions,
        calibration: ClockCalibration,
    },
    RequestStatus {
        request: WorkerRequest,
    },
    Activate {
        request: WorkerRequest,
        intent: ActivationIntent,
    },
    Release {
        request: WorkerRequest,
        reason: ReleaseReason,
        deadline_elapsed_realtime: u64,
        revoke_through_intent_revision: u64,
    },
    SetAdmission {
        request: WorkerRequest,
        policy_seq: u64,
        decision: AdmissionDecision,
    },
    Stop {
        request: WorkerRequest,
        deadline_elapsed_realtime: u64,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WorkerCommandKind {
    Start,
    Status,
    Activate,
    Release,
    SetAdmission,
    Stop,
}

impl WorkerCommand {
    pub fn request(&self) -> &WorkerRequest {
        match self {
            WorkerCommand::Start { request, .. }
            | WorkerCommand::RequestStatus { request }
            | WorkerCommand::Activate { request, .. }
            | WorkerCommand::Release { request, .. }
            | WorkerCommand::SetAdmission { request, .. }
            | WorkerCommand::Stop { request, .. } => request,
        }
    }

    pub fn kind(&self) -> WorkerCommandKind {
        match self {
            WorkerCommand::Start { .. } => WorkerCommandKind::Start,
            WorkerCommand::RequestStatus { .. } => WorkerCommandKind::Status,
            WorkerCommand::Activate { .. } => WorkerCommandKind::Activate,
            WorkerCommand::Release { .. } => WorkerCommandKind::Release,
            WorkerCommand::SetAdmission { .. } => WorkerCommandKind::SetAdmission,
            WorkerCommand::Stop { .. } => WorkerCommandKind::Stop,
        }
    }

    /// Close, Release and Stop ride the reserved control slot (ADR-034 §7):
    /// they never wait behind queued Open/renew/status traffic.
    pub fn is_control(&self) -> bool {
        matches!(
            self,
            WorkerCommand::Release { .. }
                | WorkerCommand::Stop { .. }
                | WorkerCommand::SetAdmission {
                    decision: AdmissionDecision::Close { .. },
                    ..
                }
        )
    }

    /// The Kotlin `init` checks, applied before the fence sees the command.
    pub fn validate(&self) -> Result<(), InvalidPayload> {
        self.request().validate()?;
        match self {
            WorkerCommand::Start { calibration, .. } => calibration.validate(),
            WorkerCommand::RequestStatus { .. } | WorkerCommand::Stop { .. } => Ok(()),
            WorkerCommand::Activate { intent, .. } => {
                if intent_valid(intent) {
                    Ok(())
                } else {
                    Err(InvalidPayload("activation intent out of bounds"))
                }
            }
            WorkerCommand::Release { .. } => Ok(()),
            WorkerCommand::SetAdmission {
                policy_seq,
                decision,
                ..
            } => {
                if *policy_seq == 0 {
                    return Err(InvalidPayload("policySeq must be positive"));
                }
                decision.validate()
            }
        }
    }
}

/// Every callback carries the correlated request, the worker's monotone
/// `stateSeq` (positive) and the highest applied policy sequence.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WorkerCallbackStamp {
    pub request: WorkerRequest,
    pub state_seq: u64,
    pub applied_policy_seq: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WorkerPhase {
    Preparing,
    Prepared,
    Opening,
    Open,
    Releasing,
    Quiescent,
    Stopping,
    Drained,
    Failed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WorkerFailure {
    NotReady,
    InvalidLease,
    InvalidSequence,
    InvalidGrant,
    TakeoverFailed,
    DeadlineExceeded,
    Internal,
}

/// The one map from the fence's refusal to the wire failure.
impl From<GrantError> for WorkerFailure {
    fn from(error: GrantError) -> Self {
        match error {
            GrantError::NotReady => WorkerFailure::NotReady,
            GrantError::Intent | GrantError::Evidence => WorkerFailure::InvalidGrant,
            GrantError::Sequence | GrantError::Terminal => WorkerFailure::InvalidSequence,
            GrantError::Takeover => WorkerFailure::TakeoverFailed,
            GrantError::Clock => WorkerFailure::Internal,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DrainResult {
    Drained,
    DeadlineExceeded,
    Failed,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WorkerStatus {
    pub phase: WorkerPhase,
    pub ready: bool,
    pub intent: Option<ActivationIntent>,
    pub open_grant: Option<OpenGrant>,
    pub failure: Option<WorkerFailure>,
}

/// The seven callbacks. `Status` correlates to its `RequestStatus`, or to
/// the accepted `Start` when spontaneous; `AdmissionApplied(Open)` is
/// AppliedOpen after checked takeover — `Accepted` never authorizes dispatch.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum WorkerCallback {
    Accepted {
        stamp: WorkerCallbackStamp,
        command: WorkerCommandKind,
    },
    Status {
        stamp: WorkerCallbackStamp,
        status: WorkerStatus,
    },
    ActivationReady {
        stamp: WorkerCallbackStamp,
        intent: ActivationIntent,
    },
    /// Execution quiescent; registry release and propagation are main's.
    Released { stamp: WorkerCallbackStamp },
    AdmissionApplied {
        stamp: WorkerCallbackStamp,
        decision: AdmissionDecision,
    },
    Drained {
        stamp: WorkerCallbackStamp,
        result: DrainResult,
    },
    Failed {
        stamp: WorkerCallbackStamp,
        failure: WorkerFailure,
    },
}

impl WorkerCallback {
    pub fn stamp(&self) -> &WorkerCallbackStamp {
        match self {
            WorkerCallback::Accepted { stamp, .. }
            | WorkerCallback::Status { stamp, .. }
            | WorkerCallback::ActivationReady { stamp, .. }
            | WorkerCallback::Released { stamp }
            | WorkerCallback::AdmissionApplied { stamp, .. }
            | WorkerCallback::Drained { stamp, .. }
            | WorkerCallback::Failed { stamp, .. } => stamp,
        }
    }
}

/// Where the consumer delivers callbacks: the JNI adapter's oneway Binder
/// sender in production, a channel in tests. Called on the consumer thread
/// only; it must never block on the worker's own state.
pub trait CallbackSink: Send + Sync {
    fn emit(&self, callback: WorkerCallback);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::managed::grant::EvidenceGrade;

    fn lease() -> WorkerLease {
        WorkerLease {
            main_nonce: "nonce-1".into(),
            generation: 1,
            base_url: "http://127.0.0.1:7001".into(),
            auth_epoch: 1,
            account_id: "acc".into(),
            self_peer_id: "peer".into(),
        }
    }

    fn request(id: u64) -> WorkerRequest {
        WorkerRequest {
            request_id: id,
            session_id: "s1".into(),
            lease: lease(),
        }
    }

    fn intent(rev: u64) -> ActivationIntent {
        ActivationIntent {
            activation_id: format!("intent-{rev}"),
            intent_revision: rev,
        }
    }

    #[test]
    fn ids_and_origins_follow_the_kotlin_bounds() {
        assert!(bounded_id("a"));
        assert!(bounded_id(&"x".repeat(256)));
        assert!(!bounded_id(""));
        assert!(!bounded_id(&"x".repeat(257)));
        assert!(!bounded_id("with space"));
        assert!(!bounded_id("tab\there"));
        assert!(!bounded_id("ü"));
        assert!(loopback_origin("http://127.0.0.1:1"));
        assert!(loopback_origin("http://127.0.0.1:65535"));
        for bad in [
            "http://127.0.0.1:0",
            "http://127.0.0.1:65536",
            "http://127.0.0.1:",
            "http://127.0.0.1:7001/",
            "https://127.0.0.1:7001",
            "http://localhost:7001",
            "http://[::1]:7001",
            "http://127.0.0.1:123456",
        ] {
            assert!(!loopback_origin(bad), "{bad}");
        }
    }

    #[test]
    fn lease_request_and_command_validation_mirror_the_init_blocks() {
        assert!(lease().validate().is_ok());
        let mut bad = lease();
        bad.generation = 0;
        assert!(bad.validate().is_err());
        let mut bad = lease();
        bad.auth_epoch = 0;
        assert!(bad.validate().is_err());
        let mut bad = lease();
        bad.base_url = "http://localhost:7001".into();
        assert!(bad.validate().is_err());
        let mut bad = request(1);
        bad.request_id = 0;
        assert!(bad.validate().is_err());
        let mut bad = request(1);
        bad.session_id = "has space".into();
        assert!(bad.validate().is_err());

        let start = WorkerCommand::Start {
            request: request(1),
            options: WorkerStartOptions {
                artifact: WorkerArtifact::AndroidPocV1,
                elapsed_realtime_at_start: 5,
            },
            calibration: ClockCalibration {
                native_before: 8,
                android_elapsed: 9,
                native_after: 10,
            },
        };
        assert!(start.validate().is_ok());
        assert_eq!(start.kind(), WorkerCommandKind::Start);
        assert!(!start.is_control());
        let unordered = WorkerCommand::Start {
            request: request(1),
            options: WorkerStartOptions {
                artifact: WorkerArtifact::AndroidPocV1,
                elapsed_realtime_at_start: 5,
            },
            calibration: ClockCalibration {
                native_before: 10,
                android_elapsed: 9,
                native_after: 10,
            },
        };
        assert!(unordered.validate().is_err());

        let mut zero_rev = intent(1);
        zero_rev.intent_revision = 0;
        assert!(WorkerCommand::Activate {
            request: request(2),
            intent: zero_rev,
        }
        .validate()
        .is_err());
        assert!(WorkerCommand::SetAdmission {
            request: request(3),
            policy_seq: 0,
            decision: AdmissionDecision::Close {
                reason: CloseReason::SyncLoss,
                revoke_through_intent_revision: None,
            },
        }
        .validate()
        .is_err());
    }

    #[test]
    fn close_barrier_consistency_and_control_classification() {
        let revoking_without_barrier = AdmissionDecision::Close {
            reason: CloseReason::OwnershipLost,
            revoke_through_intent_revision: None,
        };
        assert!(revoking_without_barrier.validate().is_err());
        let non_revoking_with_barrier = AdmissionDecision::Close {
            reason: CloseReason::SyncLoss,
            revoke_through_intent_revision: Some(1),
        };
        assert!(non_revoking_with_barrier.validate().is_err());
        let close = WorkerCommand::SetAdmission {
            request: request(4),
            policy_seq: 1,
            decision: AdmissionDecision::Close {
                reason: CloseReason::UserStop,
                revoke_through_intent_revision: Some(1),
            },
        };
        assert!(close.validate().is_ok());
        assert!(close.is_control());
        let open = WorkerCommand::SetAdmission {
            request: request(5),
            policy_seq: 1,
            decision: AdmissionDecision::Open(OpenGrant {
                intent: intent(1),
                evidence_seq: 1,
                observed_at: 0,
                expires_at: 30_000,
                grade: EvidenceGrade::Activation,
            }),
        };
        assert!(open.validate().is_ok());
        assert!(!open.is_control());
        let no_evidence = AdmissionDecision::Open(OpenGrant {
            intent: intent(1),
            evidence_seq: 0,
            observed_at: 0,
            expires_at: 30_000,
            grade: EvidenceGrade::Activation,
        });
        assert!(no_evidence.validate().is_err());
        assert!(WorkerCommand::Release {
            request: request(6),
            reason: ReleaseReason::UserStop,
            deadline_elapsed_realtime: 10_000,
            revoke_through_intent_revision: 1,
        }
        .is_control());
        assert!(WorkerCommand::Stop {
            request: request(7),
            deadline_elapsed_realtime: 10_000,
        }
        .is_control());
        assert!(!WorkerCommand::RequestStatus {
            request: request(8)
        }
        .is_control());
    }

    #[test]
    fn fence_errors_map_to_the_wire_failures() {
        assert_eq!(
            WorkerFailure::from(GrantError::NotReady),
            WorkerFailure::NotReady
        );
        assert_eq!(
            WorkerFailure::from(GrantError::Intent),
            WorkerFailure::InvalidGrant
        );
        assert_eq!(
            WorkerFailure::from(GrantError::Evidence),
            WorkerFailure::InvalidGrant
        );
        assert_eq!(
            WorkerFailure::from(GrantError::Sequence),
            WorkerFailure::InvalidSequence
        );
        assert_eq!(
            WorkerFailure::from(GrantError::Terminal),
            WorkerFailure::InvalidSequence
        );
        assert_eq!(
            WorkerFailure::from(GrantError::Takeover),
            WorkerFailure::TakeoverFailed
        );
        assert_eq!(
            WorkerFailure::from(GrantError::Clock),
            WorkerFailure::Internal
        );
    }
}
