use super::{ManagedFailure, ManagedTimeContext};
use crate::engine_profile::ArtifactManifestV1;
use crate::guest_egress::AndroidPocEgress;
use crate::Config;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ControlApi {
    Disabled,
    #[cfg(feature = "desktop")]
    Loopback(u16),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ElectionPolicy {
    DesktopDefault,
    HostControlled,
}

/// Artifact bytes belong to the trusted application build, not a runtime path.
#[derive(Clone)]
pub struct TrustedArtifact {
    bytes: &'static [u8],
    manifest: ArtifactManifestV1,
}

impl TrustedArtifact {
    /// # Safety
    /// Both inputs must be packaged by the trusted build. Hashes alone do not
    /// establish provenance. Never pass downloaded, user or writable-cache data.
    pub unsafe fn from_build(bytes: &'static [u8], manifest: ArtifactManifestV1) -> Self {
        Self { bytes, manifest }
    }

    pub fn bytes(&self) -> &'static [u8] {
        self.bytes
    }

    pub fn manifest(&self) -> &ArtifactManifestV1 {
        &self.manifest
    }
}

/// Concrete Android PoC options; no desktop defaults or optional guest policy.
/// The managed Broker factory applies this policy to every guest broker before
/// any effect, preparation checks every configured model/MCP endpoint and
/// API-key credential, and OAuth/ChatGPT/shell capabilities are rejected before
/// reading grants (ADR-034 §2). The time context is the embedder's zone sample
/// for `time.now`; the runtime never consults `TZ`/`HOME` for it.
#[derive(Clone)]
pub struct ManagedOptions {
    leased_origin: String,
    artifact: TrustedArtifact,
    guest_egress: AndroidPocEgress,
    time: ManagedTimeContext,
    pub(super) fixture: Option<super::PreseededFixture>,
}

impl ManagedOptions {
    pub fn android_poc(
        leased_origin: &str,
        artifact: TrustedArtifact,
        time: ManagedTimeContext,
    ) -> Result<Self, ManagedFailure> {
        let guest_egress = AndroidPocEgress::new(leased_origin)
            .map_err(|_| ManagedFailure::InvalidConfiguration)?;
        Ok(Self {
            leased_origin: leased_origin.into(),
            artifact,
            guest_egress,
            time,
            fixture: None,
        })
    }

    pub fn control_api(&self) -> ControlApi {
        ControlApi::Disabled
    }

    /// Supplied from the trusted fixture setup manifest, never from Binder paths
    /// or guest data. Preparation reads these exact existing objects (ADR-034 §5).
    pub fn with_preseeded_fixture(
        mut self,
        fixture: super::PreseededFixture,
    ) -> Result<Self, ManagedFailure> {
        fixture.validate()?;
        self.fixture = Some(fixture);
        Ok(self)
    }

    pub fn election_policy(&self) -> ElectionPolicy {
        ElectionPolicy::HostControlled
    }

    pub fn artifact(&self) -> &TrustedArtifact {
        &self.artifact
    }

    pub fn guest_egress(&self) -> &AndroidPocEgress {
        &self.guest_egress
    }

    pub fn leased_origin(&self) -> &str {
        &self.leased_origin
    }

    /// The same shared sample the embedder updates at startup, on timezone
    /// changes and at the zone's next offset transition (ADR-034 §2): each
    /// sample carries its validity window, and `time.now` refuses outside it.
    pub fn time_context(&self) -> &ManagedTimeContext {
        &self.time
    }

    pub(super) fn validate_config(&self, cfg: &Config) -> Result<(), ManagedFailure> {
        if cfg.addr != self.leased_origin
            || !cfg.traces_dir.is_absolute()
            || cfg
                .traces_dir
                .components()
                .any(|c| matches!(c, std::path::Component::ParentDir))
            || cfg.kernel.is_some()
            || cfg.consent_hook.is_some()
            || !cfg.secrets.is_empty()
            || !cfg.secret_overrides.is_empty()
            || !cfg.config_overrides.is_empty()
            || self
                .fixture
                .as_ref()
                .is_some_and(|fixture| !cfg.traces_dir.starts_with(&fixture.account_root))
        {
            return Err(ManagedFailure::InvalidConfiguration);
        }
        Ok(())
    }
}
