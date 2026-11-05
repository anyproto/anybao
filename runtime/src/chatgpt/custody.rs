//! Explicit local custody operations. Background paths never call this module.
use super::*;
use crate::local_grant::{RecoveryReason, StoreRecovery};

const INTERACTION_LIMIT: Duration = Duration::from_secs(120);

/// The native owner registers this before queueing work. Cancellation and the
/// final record commit share a gate; cancellation cannot return while a commit
/// is still able to install. Dropping a window cancels its exact operation.
pub struct CustodyOperation {
    cancelled: Mutex<bool>,
    deadline: Instant,
}
impl Default for CustodyOperation {
    fn default() -> Self {
        Self::new()
    }
}
impl CustodyOperation {
    pub fn new() -> Self {
        Self {
            cancelled: Mutex::new(false),
            deadline: Instant::now() + INTERACTION_LIMIT,
        }
    }
    pub fn cancel(&self) {
        *self.cancelled.lock().unwrap() = true;
    }
    pub fn is_cancelled(&self) -> bool {
        *self.cancelled.lock().unwrap() || Instant::now() >= self.deadline
    }
    fn commit<T>(
        &self,
        commit: impl FnOnce() -> Result<T, AuthErrorCode>,
    ) -> Result<T, AuthErrorCode> {
        let cancelled = self.cancelled.lock().unwrap();
        if *cancelled {
            return Err(AuthErrorCode::Cancelled);
        }
        if Instant::now() >= self.deadline {
            return Err(AuthErrorCode::RequestExpired);
        }
        commit()
    }
}

/// Nonserializable, session-bound proof used by the native confirmation owner.
pub struct RecoveryRequest {
    record: StoreRecovery,
    instance: String,
    generation: u64,
    fence: u64,
    deadline: Instant,
}
impl RecoveryRequest {
    pub fn reason(&self) -> RecoveryReason {
        self.record.reason
    }
}

impl ChatGptAuth {
    fn custody_current(&self, generation: u64, fence: u64) -> Result<(), AuthErrorCode> {
        let session = self.session.lock().unwrap();
        if self.ended.load(Ordering::Acquire) {
            return Err(AuthErrorCode::SessionEnded);
        }
        if session.generation != generation || session.fence != fence {
            return Err(AuthErrorCode::NotCurrent);
        }
        Ok(())
    }

    /// May prompt only because a registered native user action invoked it.
    /// Migration preserves the grant and never refreshes or signs in. If an
    /// earlier disconnect failed, finish that intent before releasing suspension.
    pub fn authorize_store(&self, operation: &CustodyOperation) -> Result<(), AuthError> {
        let result = (|| -> Result<(), AuthErrorCode> {
            let (generation, fence) = {
                let session = self.session.lock().unwrap();
                if self.ended.load(Ordering::Acquire) {
                    return Err(AuthErrorCode::SessionEnded);
                }
                (session.generation, session.fence)
            };
            let cancelled =
                || operation.is_cancelled() || self.custody_current(generation, fence).is_err();
            let storage = self.storage()?;
            let _transaction = storage.lock.acquire(LOCK_WAIT, cancelled)?;
            if cancelled() {
                return Err(AuthErrorCode::Cancelled);
            }
            let prepared = storage
                .store
                .prepare_access(
                    &storage.slot,
                    &GrantEnvelope::empty(&storage.slot),
                    &cancelled,
                )
                .map_err(AuthErrorCode::from)?;
            let mut session = self.session.lock().unwrap();
            if self.ended.load(Ordering::Acquire) {
                return Err(AuthErrorCode::SessionEnded);
            }
            if session.generation != generation || session.fence != fence {
                return Err(AuthErrorCode::NotCurrent);
            }
            operation.commit(|| {
                prepared.commit().map_err(AuthErrorCode::from)?;
                let mut env = self.read_envelope()?;
                if session.suspended {
                    env.fence()?;
                    env.grant = None;
                    env.pending = None;
                    self.save(&mut env)?;
                    session.suspended = false;
                }
                self.publish(&mut session, Some(&env), None);
                Ok(())
            })
        })();
        if let Err(code) = result {
            if !matches!(
                code,
                AuthErrorCode::Cancelled | AuthErrorCode::NotCurrent | AuthErrorCode::SessionEnded
            ) {
                self.record_error(code);
            }
        }
        result.map_err(Into::into)
    }

    pub fn prepare_recovery(&self) -> Result<RecoveryRequest, AuthError> {
        let storage = self.storage()?;
        let _transaction = storage
            .lock
            .acquire(LOCK_WAIT, || self.ended.load(Ordering::Acquire))?;
        let record = storage
            .store
            .recovery(&storage.slot)
            .map_err(AuthErrorCode::from)?;
        let session = self.session.lock().unwrap();
        if self.ended.load(Ordering::Acquire) {
            return Err(AuthErrorCode::SessionEnded.into());
        }
        Ok(RecoveryRequest {
            record,
            instance: self.instance.clone(),
            generation: session.generation,
            fence: session.fence,
            deadline: Instant::now() + Duration::from_secs(300),
        })
    }

    /// Explicit confirmation for the exact unreadable record. It cannot reset a
    /// valid grant, an unknown format, another session, or a changed ciphertext.
    pub fn reset_store(
        &self,
        request: &RecoveryRequest,
        operation: &CustodyOperation,
    ) -> Result<(), AuthError> {
        let storage = self.storage()?;
        let _transaction = storage.lock.acquire(LOCK_WAIT, || {
            operation.is_cancelled() || self.ended.load(Ordering::Acquire)
        })?;
        let mut session = self.session.lock().unwrap();
        if self.ended.load(Ordering::Acquire) {
            return Err(AuthErrorCode::SessionEnded.into());
        }
        if request.instance != self.instance
            || request.generation != session.generation
            || request.fence != session.fence
        {
            return Err(AuthErrorCode::NotCurrent.into());
        }
        if Instant::now() >= request.deadline {
            return Err(AuthErrorCode::RequestExpired.into());
        }
        operation
            .commit(|| {
                // Store validation happens before mutation; a stale request must not
                // suspend a healthy grant. A failed atomic reset still fails closed.
                storage
                    .store
                    .reset(&storage.slot, &request.record)
                    .map_err(AuthErrorCode::from)?;
                if let Some(work) = session.work.take() {
                    work.cancel.store(true, Ordering::Release);
                    work.ready.notify_all();
                    session.finished = Some(work.id.clone());
                }
                session.fence = session.fence.saturating_add(1);
                session.cleanup = None;
                session.suspended = false;
                self.publish(
                    &mut session,
                    Some(&GrantEnvelope::empty(&storage.slot)),
                    None,
                );
                Ok(())
            })
            .map_err(Into::into)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn expired_operation_cannot_enter_the_commit_boundary() {
        let operation = CustodyOperation {
            cancelled: Mutex::new(false),
            deadline: Instant::now(),
        };
        let committed = AtomicBool::new(false);
        assert_eq!(
            operation.commit(|| {
                committed.store(true, Ordering::SeqCst);
                Ok(())
            }),
            Err(AuthErrorCode::RequestExpired)
        );
        assert!(!committed.load(Ordering::SeqCst));
        assert!(operation.is_cancelled());
    }
}
