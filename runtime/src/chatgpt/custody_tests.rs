use super::*;

fn blocked_access(
    h: &Harness,
    operation: Arc<CustodyOperation>,
) -> (
    mpsc::Sender<()>,
    std::thread::JoinHandle<Result<(), AuthError>>,
) {
    let (entered, wait) = mpsc::channel();
    let (release, blocked) = mpsc::channel();
    *h.store.access_hook.lock().unwrap() = Some(Box::new(move || {
        entered.send(()).unwrap();
        blocked.recv_timeout(Duration::from_secs(5)).unwrap();
    }));
    let auth = h.auth.clone();
    let worker = std::thread::spawn(move || auth.authorize_store(&operation));
    wait.recv_timeout(Duration::from_secs(5)).unwrap();
    (release, worker)
}

#[test]
fn explicit_access_preserves_the_grant_and_background_checks_never_authorize() {
    let h = Harness::new();
    let original = seed(&h.store, NOW + 3600).encode().unwrap();
    *h.store.failure.lock().unwrap() = Some(GrantStoreError::Locked);
    assert_eq!(
        h.auth.get_connection().error_code,
        Some(AuthErrorCode::StoreLocked)
    );
    assert_eq!(h.authorize().unwrap_err().code, AuthErrorCode::StoreLocked);
    assert_eq!(h.store.access_commits.load(Ordering::SeqCst), 0);
    h.auth.authorize_store(&CustodyOperation::new()).unwrap();
    assert_eq!(h.auth.get_connection().state, ConnectionState::Connected);
    assert_eq!(
        h.store
            .value
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .encode()
            .unwrap(),
        original
    );
    assert_eq!(h.script.calls.load(Ordering::SeqCst), 0);
}

#[test]
fn cancelled_or_retired_access_cannot_commit_after_the_prompt_returns() {
    for retirement in [false, true] {
        let h = Harness::new();
        seed(&h.store, NOW + 3600);
        *h.store.failure.lock().unwrap() = Some(GrantStoreError::Locked);
        let operation = Arc::new(CustodyOperation::new());
        let (release, worker) = blocked_access(&h, operation.clone());
        if retirement {
            h.auth.revoke_session();
        } else {
            operation.cancel();
        }
        release.send(()).unwrap();
        assert!(worker.join().unwrap().is_err());
        assert_eq!(h.store.access_commits.load(Ordering::SeqCst), 0);
        assert_eq!(
            *h.store.failure.lock().unwrap(),
            Some(GrantStoreError::Locked)
        );
    }
}

#[test]
fn failed_disconnect_diagnoses_custody_and_access_finishes_the_disconnect() {
    let h = Harness::new();
    seed(&h.store, NOW + 3600);
    *h.store.failure.lock().unwrap() = Some(GrantStoreError::Locked);
    assert_eq!(
        h.auth.disconnect().unwrap_err().code,
        AuthErrorCode::DisconnectFailed
    );
    assert_eq!(
        h.auth.get_connection().error_code,
        Some(AuthErrorCode::StoreLocked)
    );
    assert_eq!(
        h.authorize().unwrap_err().code,
        AuthErrorCode::DisconnectFailed
    );
    h.auth.authorize_store(&CustodyOperation::new()).unwrap();
    assert_eq!(h.auth.get_connection().state, ConnectionState::Disconnected);
    assert_eq!(h.authorize().unwrap_err().code, AuthErrorCode::NotConnected);
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

#[test]
fn reset_is_confirmed_exact_session_scoped_and_stale_requests_preserve_new_grants() {
    let h = Harness::new();
    seed(&h.store, NOW + 3600);
    *h.store.failure.lock().unwrap() = Some(GrantStoreError::MissingKey);
    let request = h.auth.prepare_recovery().unwrap();
    assert!(
        h.store.value.lock().unwrap().is_some(),
        "preparation never resets"
    );
    assert_eq!(
        h.peer()
            .reset_store(&request, &CustodyOperation::new())
            .unwrap_err()
            .code,
        AuthErrorCode::NotCurrent
    );
    let cancelled = CustodyOperation::new();
    cancelled.cancel();
    assert_eq!(
        h.auth.reset_store(&request, &cancelled).unwrap_err().code,
        AuthErrorCode::Cancelled
    );
    assert!(h.store.value.lock().unwrap().is_some());
    h.auth
        .reset_store(&request, &CustodyOperation::new())
        .unwrap();
    assert_eq!(h.auth.get_connection().state, ConnectionState::Disconnected);
    let newer = seed(&h.store, NOW + 3600).encode().unwrap();
    assert_eq!(
        h.auth
            .reset_store(&request, &CustodyOperation::new())
            .unwrap_err()
            .code,
        AuthErrorCode::NotCurrent
    );
    assert_eq!(
        h.store
            .value
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .encode()
            .unwrap(),
        newer
    );
}

#[test]
fn reset_after_failed_disconnect_recovers_without_unsuspending_the_old_grant() {
    let h = Harness::new();
    seed(&h.store, NOW + 3600);
    *h.store.failure.lock().unwrap() = Some(GrantStoreError::MissingKey);
    assert!(h.auth.disconnect().is_err());
    assert_eq!(
        h.auth.get_connection().error_code,
        Some(AuthErrorCode::StoreKeyMissing)
    );
    let request = h.auth.prepare_recovery().unwrap();
    h.auth
        .reset_store(&request, &CustodyOperation::new())
        .unwrap();
    assert_eq!(h.auth.get_connection().state, ConnectionState::Disconnected);
    assert_eq!(h.authorize().unwrap_err().code, AuthErrorCode::NotConnected);
}

#[test]
fn reset_cannot_discard_a_peer_restored_or_replaced_record() {
    let h = Harness::new();
    seed(&h.store, NOW + 3600);
    *h.store.failure.lock().unwrap() = Some(GrantStoreError::MissingKey);
    let request = h.auth.prepare_recovery().unwrap();
    // Another process changed the exact record while the confirmation was open.
    h.store.value.lock().unwrap().as_mut().unwrap().revision += 1;
    assert_eq!(
        h.auth
            .reset_store(&request, &CustodyOperation::new())
            .unwrap_err()
            .code,
        AuthErrorCode::NotCurrent
    );
    *h.store.failure.lock().unwrap() = None;
    assert_eq!(
        h.auth
            .reset_store(&request, &CustodyOperation::new())
            .unwrap_err()
            .code,
        AuthErrorCode::NotCurrent
    );
    assert_eq!(h.auth.get_connection().state, ConnectionState::Connected);
}

#[test]
fn reset_fences_a_peer_login_even_when_the_new_envelope_epoch_restarts() {
    let h = Harness::new();
    h.clock.advance.store(false, Ordering::Release);
    h.script.response(
        Endpoint::DeviceCode,
        200,
        json!({"device_auth_id":"synthetic-device-id","user_code":"SYN-CODE","interval":1}),
    );
    let (entered, release) = h.script.blocked(
        Endpoint::DevicePoll,
        200,
        json!({"authorization_code":"synthetic-code","code_verifier":"synthetic-verifier"}),
    );
    let start = h.auth.start_login(LoginFlow::DeviceCode).unwrap();
    entered.recv_timeout(Duration::from_secs(5)).unwrap();
    *h.store.failure.lock().unwrap() = Some(GrantStoreError::MissingKey);
    let peer = h.peer();
    let request = peer.prepare_recovery().unwrap();
    peer.reset_store(&request, &CustodyOperation::new())
        .unwrap();
    let mut newer = seed(&h.store, NOW + 3600);
    newer.epoch = 1;
    *h.store.value.lock().unwrap() = Some(newer.clone());
    release.send(()).unwrap();
    // The old worker's pending ID cannot match a new grant at epoch 1.
    workers_done(&h.auth);
    assert_eq!(
        h.store
            .value
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .encode()
            .unwrap(),
        newer.encode().unwrap()
    );
    assert_ne!(newer.grant.as_ref().unwrap().id, start.login_id);
    assert_eq!(peer.get_connection().state, ConnectionState::Connected);
}

/// Plain synthetic envelopes only; never a production or OS custody adapter.
struct RecoveryFileStore(SyntheticFileStore);
impl LocalGrantStore for RecoveryFileStore {
    fn namespace(&self) -> &str {
        self.0.namespace()
    }
    fn read(&self, slot: &GrantSlot) -> Result<Option<GrantEnvelope>, GrantStoreError> {
        if self.0 .0.join("synthetic-key-missing").exists() {
            return Err(GrantStoreError::MissingKey);
        }
        self.0.read(slot)
    }
    fn replace(&self, slot: &GrantSlot, envelope: &GrantEnvelope) -> Result<(), GrantStoreError> {
        self.0.replace(slot, envelope)
    }
    fn recovery(
        &self,
        _: &GrantSlot,
    ) -> Result<crate::local_grant::StoreRecovery, GrantStoreError> {
        use sha2::{Digest, Sha256};
        if !self.0 .0.join("synthetic-key-missing").exists() {
            return Err(GrantStoreError::NotCurrent);
        }
        Ok(crate::local_grant::StoreRecovery {
            fingerprint: Sha256::digest(
                std::fs::read(self.0 .0.join("synthetic-envelope.json")).unwrap(),
            )
            .into(),
            reason: crate::local_grant::RecoveryReason::MissingKey,
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
        self.0.replace(slot, &GrantEnvelope::empty(slot))?;
        std::fs::remove_file(self.0 .0.join("synthetic-key-missing")).unwrap();
        Ok(())
    }
}

#[test]
fn recovery_process_child() {
    let Ok(root) = std::env::var("ANYBAO_TEST_CUSTODY_RECOVERY_ROOT") else {
        return;
    };
    let root = PathBuf::from(root);
    let store = Arc::new(RecoveryFileStore(SyntheticFileStore(root.clone())));
    let auth = service(
        &root,
        store.clone(),
        Arc::new(Script::default()),
        Arc::new(TestClock::new()),
    );
    let request = auth.prepare_recovery().unwrap();
    auth.reset_store(&request, &CustodyOperation::new())
        .unwrap();
    let synthetic = SyntheticStore::default();
    let newer = seed(&synthetic, NOW + 3600);
    let storage = auth.storage().unwrap();
    let _transaction = storage.lock.acquire(LOCK_WAIT, || false).unwrap();
    store.replace(&slot(), &newer).unwrap();
}

#[test]
fn a_real_second_process_recovery_and_replacement_invalidates_the_first_ticket() {
    let dir = private_dir();
    let store = Arc::new(RecoveryFileStore(SyntheticFileStore(dir.path().into())));
    let synthetic = SyntheticStore::default();
    store
        .replace(&slot(), &seed(&synthetic, NOW + 3600))
        .unwrap();
    std::fs::write(dir.path().join("synthetic-key-missing"), b"synthetic").unwrap();
    let auth = service(
        dir.path(),
        store.clone(),
        Arc::new(Script::default()),
        Arc::new(TestClock::new()),
    );
    let request = auth.prepare_recovery().unwrap();
    let child = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "chatgpt::tests::custody_tests::recovery_process_child",
            "--nocapture",
        ])
        .env("ANYBAO_TEST_CUSTODY_RECOVERY_ROOT", dir.path())
        .status()
        .unwrap();
    assert!(child.success());
    let newer = store.read(&slot()).unwrap().unwrap().encode().unwrap();
    assert_eq!(
        auth.reset_store(&request, &CustodyOperation::new())
            .unwrap_err()
            .code,
        AuthErrorCode::NotCurrent
    );
    assert_eq!(
        store.read(&slot()).unwrap().unwrap().encode().unwrap(),
        newer
    );
    assert_eq!(auth.get_connection().state, ConnectionState::Connected);
}
