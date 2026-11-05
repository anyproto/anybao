//! Safe account-bus projection. Never serialize a ConnectionSnapshot here.
use crate::chatgpt::{ConnectionSnapshot, ConnectionState};
use crate::local_grant::AuthErrorCode;
use crate::model_configuration::ConfigurationVerdict;
use serde_json::{json, Value};
use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;

pub(crate) fn reserve_generation_file(path: &Path) -> Result<u64, AuthErrorCode> {
    let result = || -> std::io::Result<u64> {
        let current = match std::fs::symlink_metadata(path) {
            Ok(meta) => {
                if !meta.is_file() || meta.file_type().is_symlink() || meta.len() > 32 {
                    return Err(std::io::Error::other("invalid generation record"));
                }
                std::fs::read_to_string(path)?
                    .parse::<u64>()
                    .map_err(std::io::Error::other)?
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => 0,
            Err(e) => return Err(e),
        };
        // Decimal strings preserve full u64 ordering on every consumer.
        let next = current
            .checked_add(1)
            .ok_or_else(|| std::io::Error::other("generation exhausted"))?;
        let temporary = path.with_extension(format!("generation-{}.tmp", uuid::Uuid::new_v4()));
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let written = (|| {
            let mut file = options.open(&temporary)?;
            file.write_all(next.to_string().as_bytes())?;
            file.sync_all()?;
            commit_generation(&temporary, path)?;
            Ok(next)
        })();
        if written.is_err() {
            let _ = std::fs::remove_file(&temporary);
        }
        written
    };
    result().map_err(|_| AuthErrorCode::LocalStoreUnavailable)
}

// Every successful reservation must survive a process/power interruption.
// Failure (including an uncertain rename/flush) prevents publication.
#[cfg(not(windows))]
fn commit_generation(temporary: &Path, path: &Path) -> std::io::Result<()> {
    std::fs::rename(temporary, path)?;
    let parent = path
        .parent()
        .ok_or_else(|| std::io::Error::other("no parent"))?;
    std::fs::File::open(parent)?.sync_all()?;
    // The lock directory may have been created this boot inside the stable
    // private profile. Persist that directory entry as well as the counter.
    std::fs::File::open(
        parent
            .parent()
            .ok_or_else(|| std::io::Error::other("no profile"))?,
    )?
    .sync_all()
}
#[cfg(windows)]
fn commit_generation(temporary: &Path, path: &Path) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };
    let wide = |p: &Path| -> std::io::Result<Vec<u16>> {
        let mut text: Vec<_> = p.as_os_str().encode_wide().collect();
        if text.contains(&0) {
            return Err(std::io::Error::other("invalid generation path"));
        }
        text.push(0);
        Ok(text)
    };
    // The file has already been flushed. The write-through rename replaces
    // the previous reservation and waits for the filesystem commit.
    if unsafe {
        MoveFileExW(
            wide(temporary)?.as_ptr(),
            wide(path)?.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

pub(crate) fn provider(snapshot: &ConnectionSnapshot) -> Value {
    let state = match snapshot.state {
        ConnectionState::Disconnected | ConnectionState::Connecting => "connection_required",
        ConnectionState::Connected if snapshot.retry_after.is_some() => "cooldown",
        ConnectionState::Connected => "connected",
        ConnectionState::ReauthRequired => "reauth_required",
        ConnectionState::Unavailable => "unavailable",
    };
    let reason = match snapshot.error_code {
        Some(AuthErrorCode::AuthBusy) => "auth_busy",
        Some(AuthErrorCode::StoreLocked) => "store_locked",
        Some(AuthErrorCode::IdentityUnavailable | AuthErrorCode::SessionEnded) => {
            "identity_unavailable"
        }
        Some(AuthErrorCode::RefreshUncertain) => "refresh_uncertain",
        Some(AuthErrorCode::GrantRejected) => "grant_rejected",
        Some(AuthErrorCode::AccountChanged) => "account_changed",
        _ if state == "unavailable" => "local_store_unavailable",
        _ if state == "connection_required" => "not_connected",
        _ if state == "cooldown" => "auth_rate_limited",
        _ => "none",
    };
    json!({"state": state, "reason": reason})
}

pub(crate) fn extension(
    generation: u64,
    publisher_scope: &str,
    configuration: ConfigurationVerdict,
    status: Value,
) -> Value {
    json!({
        "schemaVersion": 1,
        "capability": "chatgpt-local-v1",
        "bootGeneration": generation.to_string(),
        "publisherScope": publisher_scope,
        "configuration": configuration.configuration,
        "applyId": configuration.apply_id,
        "configId": configuration.config_id,
        "chatgpt": configuration.chatgpt,
        "provider": status,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn durable_generation_survives_reopen_and_refuses_corruption() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("generation");
        assert_eq!(reserve_generation_file(&p).unwrap(), 1);
        assert_eq!(reserve_generation_file(&p).unwrap(), 2);
        std::fs::write(&p, "partial\n").unwrap();
        assert!(reserve_generation_file(&p).is_err());
        std::fs::write(&p, u64::MAX.to_string()).unwrap();
        assert!(reserve_generation_file(&p).is_err());
    }
    #[test]
    fn provider_projection_never_contains_local_presentation() {
        let snap = ConnectionSnapshot {
            state: ConnectionState::Connected,
            device_id: "device".into(),
            session_generation: 1,
            revision: 8,
            account_label: Some("private@example.test".into()),
            plan_type: Some("private-plan".into()),
            pending_login: Some(crate::chatgpt::PendingLogin {
                login_id: "private-login".into(),
                flow: crate::local_grant::LoginFlow::DeviceCode,
                expires_at: 100,
            }),
            error_code: None,
            retry_after: None,
        };
        assert_eq!(
            provider(&snap),
            json!({"state":"connected", "reason":"none"})
        );
        let mut cooldown = snap.clone();
        cooldown.retry_after = Some(50);
        assert_eq!(provider(&cooldown)["state"], "cooldown");
        let mut busy = snap;
        busy.state = ConnectionState::Unavailable;
        busy.error_code = Some(AuthErrorCode::AuthBusy);
        assert_eq!(
            provider(&busy),
            json!({"state":"unavailable", "reason":"auth_busy"})
        );
    }
    #[test]
    fn shared_safe_projection_fixture() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../docs/fixtures/chatgpt-readiness-v1.json"
        ))
        .unwrap();
        let expected = &fixture["extension"];
        let configuration = ConfigurationVerdict {
            configuration: "matched",
            apply_id: Some(expected["applyId"].as_str().unwrap().into()),
            config_id: Some(expected["configId"].as_str().unwrap().into()),
            chatgpt: true,
        };
        let mut actual = extension(
            9_007_199_254_740_993,
            &"a".repeat(64),
            configuration,
            json!({"state":"connected","reason":"none"}),
        );
        actual["sequence"] = json!("1");
        assert_eq!(actual, *expected);
    }
}
