use super::*;
use std::process::Command;

fn slot() -> GrantSlot {
    GrantSlot::new("bao/test", "profile", "account", "device").unwrap()
}

fn private_dir() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    }
    dir
}

#[test]
fn reserved_namespace_is_exact_and_includes_all_dot_descendants() {
    for key in [
        CHATGPT_REF,
        "connector.oauth.openai-codex.refresh",
        "connector.oauth.openai-codex.a.b",
    ] {
        assert!(is_reserved_ref(key));
        assert!(reject_reserved_ref(key).is_err());
    }
    for key in [
        "connector.oauth.google",
        "connector.oauth.openai-codex2",
        "connector.oauth.openai-codex-other",
    ] {
        assert!(!is_reserved_ref(key));
    }
}

#[test]
fn slot_keys_are_canonical_and_identity_scoped() {
    let baseline = slot().storage_key("vault").unwrap();
    for (app, profile, account, device, vault) in [
        ("bao/other", "profile", "account", "device", "vault"),
        ("bao/test", "other", "account", "device", "vault"),
        ("bao/test", "profile", "other", "device", "vault"),
        ("bao/test", "profile", "account", "other", "vault"),
        ("bao/test", "profile", "account", "device", "other"),
    ] {
        assert_ne!(
            baseline,
            GrantSlot::new(app, profile, account, device)
                .unwrap()
                .storage_key(vault)
                .unwrap()
        );
    }
    assert!(GrantSlot::new("bao", "", "account", "device").is_err());
    assert!(GrantSlot::new("bao", "profile", "account\n", "device").is_err());
    assert!(slot().storage_key("").is_err());
    assert_ne!(
        GrantSlot::new("a/b", "c", "d", "e")
            .unwrap()
            .storage_key("vault")
            .unwrap(),
        GrantSlot::new("a", "b/c", "d", "e")
            .unwrap()
            .storage_key("vault")
            .unwrap()
    );
}

#[test]
fn process_local_gate_is_shared_between_independent_instances() {
    let dir = private_dir();
    let locks = GrantLockDirectory::new(dir.path()).unwrap();
    let first = locks.slot_lock(&slot(), "vault").unwrap();
    let second = locks.slot_lock(&slot(), "vault").unwrap();
    let _held = first.acquire(Duration::from_secs(1), || false).unwrap();
    assert!(matches!(
        second.acquire(Duration::from_millis(60), || false),
        Err(AuthErrorCode::AuthBusy)
    ));
    assert!(matches!(
        second.acquire(Duration::from_secs(1), || true),
        Err(AuthErrorCode::Cancelled)
    ));
}

#[test]
fn os_lock_child() {
    let Ok(root) = std::env::var("ANYBAO_TEST_LOCK_ROOT") else {
        return;
    };
    let root = PathBuf::from(root);
    let locks = GrantLockDirectory::new(&root).unwrap();
    let lock = locks.slot_lock(&slot(), "vault").unwrap();
    let _held = lock.acquire(Duration::from_secs(3), || false).unwrap();
    std::fs::write(root.join("held"), "synthetic").unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while !root.join("release").exists() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn actual_child_process_lock_contention_timeout_cancellation_and_release() {
    let dir = private_dir();
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "local_grant::tests::os_lock_child",
            "--nocapture",
        ])
        .env("ANYBAO_TEST_LOCK_ROOT", dir.path())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while !dir.path().join("held").exists() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    let locks = GrantLockDirectory::new(dir.path()).unwrap();
    let lock = locks.slot_lock(&slot(), "vault").unwrap();
    assert!(matches!(
        lock.acquire(Duration::from_millis(70), || false),
        Err(AuthErrorCode::AuthBusy)
    ));
    let start = Instant::now();
    assert!(matches!(
        lock.acquire(Duration::from_secs(2), || start.elapsed()
            > Duration::from_millis(30)),
        Err(AuthErrorCode::Cancelled)
    ));
    std::fs::write(dir.path().join("release"), "synthetic").unwrap();
    assert!(child.wait().unwrap().success());
    let _available = lock.acquire(Duration::from_secs(1), || false).unwrap();
}

#[cfg(unix)]
#[test]
fn lock_directory_must_be_private_and_lock_file_cannot_be_a_symlink() {
    use std::os::unix::fs::{symlink, PermissionsExt};
    let dir = private_dir();
    std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o755)).unwrap();
    assert!(matches!(
        GrantLockDirectory::new(dir.path()),
        Err(AuthErrorCode::InvalidStore)
    ));
    std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    let locks = GrantLockDirectory::new(dir.path()).unwrap();
    let lock = locks.slot_lock(&slot(), "vault").unwrap();
    symlink(dir.path().join("outside"), &lock.path).unwrap();
    assert!(matches!(
        lock.acquire(Duration::from_secs(1), || false),
        Err(AuthErrorCode::InvalidStore)
    ));
}

#[test]
fn generation_child() {
    let Ok(root) = std::env::var("ANYBAO_TEST_GENERATION_ROOT") else {
        return;
    };
    let locks = GrantLockDirectory::new(&root).unwrap();
    let lock = locks.slot_lock(&slot(), "vault").unwrap();
    for _ in 0..20 {
        lock.reserve_generation().unwrap();
    }
}

#[test]
fn two_process_generations_are_durable_ordered_and_scoped() {
    let dir = private_dir();
    let spawn = || {
        Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "local_grant::tests::generation_child"])
            .env("ANYBAO_TEST_GENERATION_ROOT", dir.path())
            .spawn()
            .unwrap()
    };
    let mut first = spawn();
    let mut second = spawn();
    assert!(first.wait().unwrap().success());
    assert!(second.wait().unwrap().success());
    let locks = GrantLockDirectory::new(dir.path()).unwrap();
    assert_eq!(
        locks
            .slot_lock(&slot(), "vault")
            .unwrap()
            .reserve_generation()
            .unwrap(),
        41
    );
    assert_eq!(
        locks
            .slot_lock(&slot(), "other-store")
            .unwrap()
            .reserve_generation()
            .unwrap(),
        1
    );
    drop(locks);
    let reopened = GrantLockDirectory::new(dir.path()).unwrap();
    assert_eq!(
        reopened
            .slot_lock(&slot(), "vault")
            .unwrap()
            .reserve_generation()
            .unwrap(),
        42
    );
}
