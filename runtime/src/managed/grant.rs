//! Native consumer of the B0 intent/policy/evidence fence (ADR-034 §7).
//! The production managed service does not expose Open until takeover exists.
use std::sync::Arc;

pub trait BootClock: Send + Sync {
    fn now_ms(&self) -> Result<u64, GrantError>;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GrantError {
    Clock,
    NotReady,
    Terminal,
    Intent,
    Sequence,
    Evidence,
    Takeover,
}

/// Calibration uses a native/Android/native sample bracket, all in milliseconds.
/// There is no adjustable offset: both clocks must describe the same boot.
/// Cloned into the execution service at every Open (N2b-2), which checks
/// the grant's freshness against it at each admission (ADR-034 §7).
#[derive(Clone)]
pub struct CalibratedBootClock {
    clock: Arc<dyn BootClock>,
}
impl CalibratedBootClock {
    pub fn new(
        clock: Arc<dyn BootClock>,
        native_before: u64,
        android_elapsed: u64,
        native_after: u64,
    ) -> Result<Self, GrantError> {
        if native_before > android_elapsed
            || android_elapsed > native_after
            || native_after > clock.now_ms()?
        {
            return Err(GrantError::Clock);
        }
        Ok(Self { clock })
    }
    pub(crate) fn now_ms(&self) -> Result<u64, GrantError> {
        self.clock.now_ms()
    }
}

/// Android elapsedRealtime and Linux CLOCK_BOOTTIME both include suspend.
#[cfg(target_os = "android")]
pub struct NativeBootClock;
#[cfg(target_os = "android")]
impl BootClock for NativeBootClock {
    fn now_ms(&self) -> Result<u64, GrantError> {
        let mut ts = libc::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        // SAFETY: ts is valid writable storage for clock_gettime.
        if unsafe { libc::clock_gettime(libc::CLOCK_BOOTTIME, &mut ts) } != 0
            || ts.tv_sec < 0
            || !(0..1_000_000_000).contains(&ts.tv_nsec)
        {
            return Err(GrantError::Clock);
        }
        (ts.tv_sec as u64)
            .checked_mul(1000)
            .and_then(|v| v.checked_add(ts.tv_nsec as u64 / 1_000_000))
            .ok_or(GrantError::Clock)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActivationIntent {
    pub activation_id: String,
    pub intent_revision: u64,
}
impl ActivationIntent {
    fn valid(&self) -> bool {
        self.intent_revision > 0
            && self.activation_id.len() <= 256
            && !self.activation_id.is_empty()
            && self.activation_id.bytes().all(|b| (33..=126).contains(&b))
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EvidenceGrade {
    Activation,
    Renewal,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CloseReason {
    SyncLoss,
    ResourcePressure,
    HealthFailure,
    OwnershipLost,
    Deauthorized,
    Pruned,
    UserStop,
}
impl CloseReason {
    pub fn revokes_intent(self) -> bool {
        matches!(
            self,
            Self::OwnershipLost | Self::Deauthorized | Self::Pruned | Self::UserStop
        )
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OpenGrant {
    pub intent: ActivationIntent,
    pub evidence_seq: u64,
    pub observed_at: u64,
    pub expires_at: u64,
    pub grade: EvidenceGrade,
}
impl OpenGrant {
    /// The one freshness rule (ADR-034 §7: expiresAt at most observedAt + 30 s),
    /// shared by the fence and the session's expiry tick (N2b1-I-6).
    pub(crate) fn fresh(&self, now: u64) -> bool {
        self.observed_at <= now
            && now < self.expires_at
            && self
                .expires_at
                .checked_sub(self.observed_at)
                .is_some_and(|ms| ms > 0 && ms <= super::protocol::MAX_GRANT_MS)
    }
}

/// Owned by the future serialized native command consumer, never by JNI callers.
pub struct GrantFence {
    clock: CalibratedBootClock,
    intent: Option<ActivationIntent>,
    revoked_through: u64,
    policy_seq: u64,
    applied_policy_seq: u64,
    evidence_seq: u64,
    grant: Option<OpenGrant>,
    terminal: bool,
}

/// Capability to finish exactly one accepted Open after owned takeover work.
/// Close/Stop can still be consumed while that work waits on HTTP.
pub struct PendingOpen {
    policy_seq: u64,
    grant: OpenGrant,
    pub requires_takeover: bool,
}
impl GrantFence {
    pub fn new(clock: CalibratedBootClock) -> Self {
        Self {
            clock,
            intent: None,
            revoked_through: 0,
            policy_seq: 0,
            applied_policy_seq: 0,
            evidence_seq: 0,
            grant: None,
            terminal: false,
        }
    }
    pub fn activate(&mut self, intent: ActivationIntent, ready: bool) -> Result<(), GrantError> {
        if self.terminal {
            return Err(GrantError::Terminal);
        }
        if !ready {
            return Err(GrantError::NotReady);
        }
        if !intent.valid()
            || intent.intent_revision <= self.revoked_through
            || self.intent.as_ref().is_some_and(|current| {
                current.intent_revision > self.revoked_through && current != &intent
            })
        {
            return Err(GrantError::Intent);
        }
        self.intent = Some(intent);
        Ok(())
    }
    /// Only a checked successful takeover may produce AppliedOpen. Failure also
    /// consumes the sequences so retry requires fresh evidence/policy, not replay.
    pub fn begin_open(
        &mut self,
        policy_seq: u64,
        grant: OpenGrant,
    ) -> Result<PendingOpen, GrantError> {
        if self.terminal {
            return Err(GrantError::Terminal);
        }
        if policy_seq <= self.policy_seq {
            return Err(GrantError::Sequence);
        }
        if Some(&grant.intent) != self.intent.as_ref()
            || grant.intent.intent_revision <= self.revoked_through
        {
            return Err(GrantError::Intent);
        }
        let now = self.clock.now_ms()?;
        let was_open = self.grant.as_ref().is_some_and(|old| old.fresh(now));
        if grant.evidence_seq <= self.evidence_seq
            || !grant.fresh(now)
            || (!was_open && grant.grade != EvidenceGrade::Activation)
        {
            return Err(GrantError::Evidence);
        }
        self.policy_seq = policy_seq;
        self.evidence_seq = grant.evidence_seq;
        self.grant = None;
        Ok(PendingOpen {
            policy_seq,
            grant,
            requires_takeover: !was_open,
        })
    }

    pub fn complete_open(
        &mut self,
        pending: PendingOpen,
        takeover: Result<(), GrantError>,
    ) -> Result<(), GrantError> {
        if self.terminal {
            return Err(GrantError::Terminal);
        }
        if pending.policy_seq != self.policy_seq {
            return Err(GrantError::Sequence);
        }
        if self.intent.as_ref() != Some(&pending.grant.intent)
            || pending.grant.intent.intent_revision <= self.revoked_through
        {
            return Err(GrantError::Intent);
        }
        takeover?;
        if !pending.grant.fresh(self.clock.now_ms()?) {
            return Err(GrantError::Evidence);
        }
        self.grant = Some(pending.grant);
        self.applied_policy_seq = pending.policy_seq;
        Ok(())
    }

    #[cfg(test)]
    fn apply_open(
        &mut self,
        policy_seq: u64,
        grant: OpenGrant,
        takeover: impl FnOnce() -> Result<(), GrantError>,
    ) -> Result<(), GrantError> {
        let pending = self.begin_open(policy_seq, grant)?;
        let outcome = if pending.requires_takeover {
            takeover()
        } else {
            Ok(())
        };
        self.complete_open(pending, outcome)
    }
    fn revoke(&mut self, barrier: u64) -> Result<(), GrantError> {
        if barrier < self.revoked_through
            || self
                .intent
                .as_ref()
                .is_some_and(|intent| barrier < intent.intent_revision)
        {
            return Err(GrantError::Intent);
        }
        self.revoked_through = barrier;
        Ok(())
    }
    pub fn close(
        &mut self,
        policy_seq: u64,
        reason: CloseReason,
        revoke_through: Option<u64>,
    ) -> Result<(), GrantError> {
        if self.terminal {
            return Err(GrantError::Terminal);
        }
        if policy_seq <= self.policy_seq {
            return Err(GrantError::Sequence);
        }
        if reason.revokes_intent() != revoke_through.is_some() {
            return Err(GrantError::Intent);
        }
        if let Some(barrier) = revoke_through {
            self.revoke(barrier)?;
        }
        self.policy_seq = policy_seq;
        self.applied_policy_seq = policy_seq;
        self.grant = None;
        Ok(())
    }
    pub fn applied_policy_seq(&self) -> u64 {
        self.applied_policy_seq
    }
    /// The session's calibrated clock, handed to the execution service with
    /// each AppliedOpen (N2b-2).
    pub(crate) fn clock(&self) -> &CalibratedBootClock {
        &self.clock
    }
    pub fn release(&mut self, barrier: u64) -> Result<(), GrantError> {
        if self.terminal {
            return Err(GrantError::Terminal);
        }
        self.revoke(barrier)?;
        self.grant = None;
        Ok(())
    }
    pub fn stop(&mut self) {
        self.terminal = true;
        self.grant = None;
    }
    /// Call at every admission, including after a suspend or a resource closure.
    pub fn permits_dispatch(
        &mut self,
        resources_healthy: bool,
        active: bool,
        owns_chat: bool,
        chat: bool,
    ) -> Result<bool, GrantError> {
        let now = match self.clock.now_ms() {
            Ok(now) => now,
            Err(e) => {
                self.grant = None;
                return Err(e);
            }
        };
        if self.grant.as_ref().is_some_and(|grant| !grant.fresh(now)) {
            self.grant = None;
        }
        Ok(!self.terminal
            && self.grant.is_some()
            && resources_healthy
            && active
            && (!chat || owns_chat))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    #[derive(Default)]
    struct Clock(AtomicU64);
    impl BootClock for Clock {
        fn now_ms(&self) -> Result<u64, GrantError> {
            Ok(self.0.load(Ordering::Relaxed))
        }
    }
    fn intent(revision: u64) -> ActivationIntent {
        ActivationIntent {
            activation_id: format!("intent-{revision}"),
            intent_revision: revision,
        }
    }
    fn setup() -> (GrantFence, Arc<Clock>) {
        let clock = Arc::new(Clock::default());
        let mut fence = GrantFence::new(CalibratedBootClock::new(clock.clone(), 0, 0, 0).unwrap());
        fence.activate(intent(1), true).unwrap();
        (fence, clock)
    }
    fn grant(seq: u64, grade: EvidenceGrade) -> OpenGrant {
        OpenGrant {
            intent: intent(1),
            evidence_seq: seq,
            observed_at: 0,
            expires_at: 30_000,
            grade,
        }
    }
    #[test]
    fn takeover_failure_never_opens_and_requires_fresh_sequence() {
        let (mut fence, _) = setup();
        assert_eq!(
            fence.apply_open(1, grant(1, EvidenceGrade::Activation), || Err(
                GrantError::Takeover
            )),
            Err(GrantError::Takeover)
        );
        assert!(!fence.permits_dispatch(true, true, true, true).unwrap());
        assert_eq!(
            fence.apply_open(1, grant(2, EvidenceGrade::Activation), || Ok(())),
            Err(GrantError::Sequence)
        );
        fence
            .apply_open(2, grant(2, EvidenceGrade::Activation), || Ok(()))
            .unwrap();
        assert!(fence.permits_dispatch(true, true, true, true).unwrap());
        assert!(!fence.permits_dispatch(false, true, true, true).unwrap());
        assert!(!fence.permits_dispatch(true, true, false, true).unwrap());
    }
    #[test]
    fn suspended_expiry_during_takeover_and_reopening_needs_activation() {
        let (mut fence, clock) = setup();
        assert_eq!(
            fence.apply_open(1, grant(1, EvidenceGrade::Activation), || {
                clock.0.store(30_000, Ordering::Relaxed);
                Ok(())
            }),
            Err(GrantError::Evidence)
        );
        let mut fresh = grant(2, EvidenceGrade::Renewal);
        fresh.observed_at = 30_000;
        fresh.expires_at = 60_000;
        assert_eq!(
            fence.apply_open(2, fresh.clone(), || Ok(())),
            Err(GrantError::Evidence)
        );
        fresh.grade = EvidenceGrade::Activation;
        fence.apply_open(2, fresh, || Ok(())).unwrap();
        clock.0.store(60_000, Ordering::Relaxed);
        assert!(!fence.permits_dispatch(true, true, true, false).unwrap());
    }
    #[test]
    fn barriers_cover_unseen_activations_and_cannot_close_newer_intents() {
        let (mut fence, _) = setup();
        fence.release(4).unwrap();
        assert_eq!(fence.activate(intent(4), true), Err(GrantError::Intent));
        fence.activate(intent(5), true).unwrap();
        assert_eq!(fence.release(4), Err(GrantError::Intent));
        let mut alias = intent(5);
        alias.activation_id = "other".into();
        assert_eq!(fence.activate(alias, true), Err(GrantError::Intent));
        assert_eq!(fence.activate(intent(6), true), Err(GrantError::Intent));
        fence.stop();
        assert_eq!(fence.activate(intent(5), true), Err(GrantError::Terminal));
    }
    #[test]
    fn stale_future_long_and_mismatched_grants_are_rejected() {
        let (mut fence, _) = setup();
        for (observed, expires) in [(1, 30_001), (0, 30_001), (0, 0)] {
            let mut g = grant(1, EvidenceGrade::Activation);
            g.observed_at = observed;
            g.expires_at = expires;
            assert_eq!(
                fence.apply_open(1, g, || panic!("invalid")),
                Err(GrantError::Evidence)
            );
        }
        let mut mismatch = grant(1, EvidenceGrade::Activation);
        mismatch.intent = intent(2);
        assert_eq!(
            fence.apply_open(1, mismatch, || panic!("invalid")),
            Err(GrantError::Intent)
        );
        fence
            .apply_open(1, grant(1, EvidenceGrade::Activation), || Ok(()))
            .unwrap();
        assert_eq!(
            fence.apply_open(2, grant(1, EvidenceGrade::Renewal), || panic!("invalid")),
            Err(GrantError::Evidence)
        );
    }
    #[test]
    fn calibration_refuses_unbracketed_or_future_samples() {
        let clock = Arc::new(Clock(AtomicU64::new(10)));
        assert!(CalibratedBootClock::new(clock.clone(), 8, 9, 10).is_ok());
        assert!(CalibratedBootClock::new(clock.clone(), 8, 7, 10).is_err());
        assert!(CalibratedBootClock::new(clock, 8, 9, 11).is_err());
    }
    #[test]
    fn close_or_release_during_takeover_cannot_be_undone_by_late_completion() {
        let (mut fence, _) = setup();
        let pending = fence
            .begin_open(1, grant(1, EvidenceGrade::Activation))
            .unwrap();
        assert!(pending.requires_takeover);
        assert_eq!(fence.applied_policy_seq(), 0);
        fence.close(2, CloseReason::SyncLoss, None).unwrap();
        assert_eq!(fence.applied_policy_seq(), 2);
        assert_eq!(
            fence.complete_open(pending, Ok(())),
            Err(GrantError::Sequence)
        );
        let pending = fence
            .begin_open(3, grant(2, EvidenceGrade::Activation))
            .unwrap();
        fence.release(1).unwrap();
        assert_eq!(
            fence.complete_open(pending, Ok(())),
            Err(GrantError::Intent)
        );
        assert!(!fence.permits_dispatch(true, true, true, true).unwrap());
    }
    #[test]
    fn revoking_close_requires_its_barrier_and_rejects_stale_barriers() {
        let (mut fence, _) = setup();
        assert_eq!(
            fence.close(1, CloseReason::OwnershipLost, None),
            Err(GrantError::Intent)
        );
        assert_eq!(
            fence.close(1, CloseReason::SyncLoss, Some(1)),
            Err(GrantError::Intent)
        );
        assert_eq!(
            fence.close(1, CloseReason::OwnershipLost, Some(0)),
            Err(GrantError::Intent)
        );
        assert_eq!(fence.applied_policy_seq(), 0);
        fence.close(1, CloseReason::OwnershipLost, Some(1)).unwrap();
        assert_eq!(fence.applied_policy_seq(), 1);
        assert_eq!(fence.activate(intent(1), true), Err(GrantError::Intent));
    }
}
