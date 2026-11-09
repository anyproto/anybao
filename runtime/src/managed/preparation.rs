//! The worker's permitted read/local-trace phase, never mutable desktop boot
//! (ADR-034 §5). Fixture addresses and frozen pins come from explicit setup's
//! manifest; readiness here is a report, never a Ready/Open assertion.
use super::frozen::{self, FrozenContent, FrozenDeclared, Verify, WORKING_ALIAS};
use super::service::{PreparedService, Readiness};
use super::transport::{identifier, FixtureObjects, GatedTransport, PhaseGate};
use super::{ManagedFailure, ManagedOptions, Shared, TaskContext, WaitClass};
use crate::anyapi::{Client, HttpTransport, Transport};
use crate::broker::{host_allowed, hosts_of, LOCAL_KEY_PREFIX, MCP_KEY_PREFIX};
use crate::engine_profile::ArtifactManifestV1;
use crate::guest_egress::{validate_api_key_credential, validate_public_endpoint};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::net::{IpAddr, ToSocketAddrs};
use std::path::PathBuf;
use std::sync::{atomic::Ordering, Arc};
use std::time::{Duration, Instant};

/// Why preparation needs setup (ADR-034 §5 "Missing synced prerequisites in
/// preparation mean Needs setup"): the two reasons main shows differently.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SetupReason {
    /// A read failed, a pinned object or prerequisite is not on this device
    /// (yet), the working bundle is unsynced, or the trace collections could
    /// not be created. Main's monitor re-requests preparation; Rust never polls.
    NotSynced,
    /// Verified drift, an unsupported shape or a malformed prerequisite.
    Invalid,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PreparationStatus {
    Initializing,
    NeedsSetup(SetupReason),
    UnsupportedCapability,
    Prepared,
}
impl PreparationStatus {
    pub(super) fn from_atomic(value: u64) -> Self {
        match value {
            1 => Self::NeedsSetup(SetupReason::Invalid),
            2 => Self::UnsupportedCapability,
            3 => Self::Prepared,
            4 => Self::NeedsSetup(SetupReason::NotSynced),
            _ => Self::Initializing,
        }
    }
    pub(super) fn atomic(self) -> u64 {
        match self {
            Self::Initializing => 0,
            Self::NeedsSetup(SetupReason::Invalid) => 1,
            Self::UnsupportedCapability => 2,
            Self::Prepared => 3,
            Self::NeedsSetup(SetupReason::NotSynced) => 4,
        }
    }
}
const INVALID: PreparationStatus = PreparationStatus::NeedsSetup(SetupReason::Invalid);
const NOT_SYNCED: PreparationStatus = PreparationStatus::NeedsSetup(SetupReason::NotSynced);

#[derive(Clone)]
pub struct StoreAddress {
    pub object_id: String,
    pub collection: String,
}

/// One deployed program of the frozen test overlay pair (ADR-034 §2): its
/// resolver alias, spec, `deploy::fingerprint` over code, derived properties
/// and manifest, plus the managed-side digest of its raw `credentials`
/// property text (SHA-256 of "" when absent), as recorded by explicit setup.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FrozenProgram {
    pub alias: String,
    pub name: String,
    pub version: String,
    pub fingerprint: String,
    pub credentials_sha256: String,
    /// Whether the program has a `program_manifest` record (deploy writes one
    /// only for a truthy manifest sidecar), so a pinned manifest that has not
    /// arrived yet reads as not synced, not as drift (N2a-r2 R2-4).
    pub manifest: bool,
}

/// One deployed skill (`agent_skill` object): alias, name and the SHA-256 of
/// its markdown body as the data route serves it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FrozenSkill {
    pub alias: String,
    pub name: String,
    pub sha256: String,
}

/// The frozen overlay pins: every program and skill the overlays may hold, and
/// the runtime, kernel and protocol they were validated against. A changed
/// overlay, runtime, kernel or protocol is a setup/support failure, never
/// followed; a managed run resolves modules from the verified snapshot only.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FrozenPins {
    /// The overlay sources' revision these fingerprints were deployed from; recorded
    /// separately from the runtime revision (ADR-034 §2), not verifiable at runtime.
    pub source_revision: String,
    /// `BuildProvenance::anyrt_revision` of the trusted artifact these pins were validated with.
    pub anyrt_revision: String,
    /// `runner::TRACE_SCHEMA` the programs were validated against.
    pub trace_schema: u32,
    /// `ArtifactManifestV1::source_sha256`: the kernel identity (N1-R8).
    pub kernel_source_sha256: String,
    /// `ArtifactManifestV1::profile_sha256`: the engine profile (N1-R8).
    pub profile_sha256: String,
    /// Every configured overlay alias; one without programs or skills below
    /// must be empty of both on the device.
    pub overlays: Vec<String>,
    pub programs: Vec<FrozenProgram>,
    pub skills: Vec<FrozenSkill>,
}

#[derive(Clone)]
pub struct PreseededFixture {
    pub account_root: PathBuf,
    /// The account explicit setup recorded; a lease naming another is refused (N2b).
    pub account_id: String,
    pub self_peer_id: String,
    pub bao_root: String,
    pub chat: String,
    pub config: StoreAddress,
    pub secrets: StoreAddress,
    pub triggers: StoreAddress,
    pub runs: StoreAddress,
    /// The general chat's `bao/log/v1` child and its `agent_turns`
    /// collection (explicit setup, ADR-034 §5): where the host logs the turn
    /// an interrupted run could not (ADR-005 §3).
    pub turns: StoreAddress,
    pub frozen: FrozenPins,
}
fn sha256_hex_text(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}
impl PreseededFixture {
    pub(super) fn validate(&self) -> Result<(), ManagedFailure> {
        let pins = &self.frozen;
        let overlays: BTreeSet<&str> = pins.overlays.iter().map(String::as_str).collect();
        let mut specs = BTreeSet::new();
        let mut skills = BTreeSet::new();
        if !self.account_root.is_absolute()
            || self
                .account_root
                .components()
                .any(|c| matches!(c, std::path::Component::ParentDir))
            || [
                &self.account_id,
                &self.self_peer_id,
                &self.bao_root,
                &self.chat,
            ]
            .iter()
            .any(|s| !identifier(s))
            || self
                .stores()
                .iter()
                .any(|s| !identifier(&s.object_id) || !identifier(&s.collection))
            || pins.anyrt_revision.is_empty()
            || pins.source_revision.is_empty()
            || !sha256_hex_text(&pins.kernel_source_sha256)
            || !sha256_hex_text(&pins.profile_sha256)
            || overlays.len() != pins.overlays.len()
            || !overlays.contains("agent")
            || overlays.contains(WORKING_ALIAS)
            || pins.overlays.iter().any(|a| !identifier(a))
            || pins.programs.is_empty()
            || pins.programs.iter().any(|p| {
                (p.alias != WORKING_ALIAS && !overlays.contains(p.alias.as_str()))
                    || !identifier(&p.name)
                    || !identifier(&p.version)
                    || !sha256_hex_text(&p.fingerprint)
                    || !sha256_hex_text(&p.credentials_sha256)
                    || !specs.insert((&p.alias, &p.name, &p.version))
            })
            || pins.skills.iter().any(|s| {
                (s.alias != WORKING_ALIAS && !overlays.contains(s.alias.as_str()))
                    || s.name.is_empty()
                    || s.name.len() > 256
                    || !sha256_hex_text(&s.sha256)
                    || !skills.insert((&s.alias, &s.name))
            })
        {
            return Err(ManagedFailure::InvalidConfiguration);
        }
        Ok(())
    }
    fn stores(&self) -> [&StoreAddress; 5] {
        [
            &self.config,
            &self.secrets,
            &self.triggers,
            &self.runs,
            &self.turns,
        ]
    }
    pub(super) fn objects(&self) -> FixtureObjects {
        let pair = |s: &StoreAddress| (s.object_id.clone(), s.collection.clone());
        FixtureObjects {
            config: pair(&self.config),
            secrets: pair(&self.secrets),
            triggers: pair(&self.triggers),
            runs: pair(&self.runs),
            turns: pair(&self.turns),
            chat: self.chat.clone(),
            self_peer: self.self_peer_id.clone(),
        }
    }
}

/// The frozen pins must name the running artifact, kernel, profile and
/// protocol (ADR-034 §2); checked before any HTTP.
pub(super) fn protocol_compatible(pins: &FrozenPins, manifest: &ArtifactManifestV1) -> bool {
    pins.trace_schema == crate::runner::TRACE_SCHEMA
        && pins.anyrt_revision == manifest.provenance.anyrt_revision
        && pins.kernel_source_sha256 == manifest.source_sha256
        && pins.profile_sha256 == manifest.profile_sha256
}

/// The gated host client for this owner; the gate starts in Preparation.
fn client(
    cfg: &crate::Config,
    shared: &Arc<Shared>,
    inner: Box<dyn Transport + Send + Sync>,
    objects: FixtureObjects,
) -> (Arc<Client>, PhaseGate) {
    let mut spaces: BTreeSet<_> = cfg
        .overlays
        .values()
        .map(|overlay| overlay.space.clone())
        .collect();
    spaces.insert(cfg.agent_space.clone());
    let gate = PhaseGate::default();
    let transport = GatedTransport::new(
        inner,
        Arc::downgrade(shared),
        spaces,
        cfg.agent_space.clone(),
        objects,
        gate.clone(),
    );
    (Arc::new(Client::with_transport(Box::new(transport))), gate)
}

fn required_string<'a>(row: &'a Value, key: &str) -> Result<&'a str, PreparationStatus> {
    row[key].as_str().filter(|s| !s.is_empty()).ok_or(INVALID)
}

/// The providers each row kind's pinned guest accepts (N1-R10): chat tiers
/// `llm@v1` (anthropic/openai-compat), judge tiers (typesafe), web search
/// `webSearch@v1` (gemini/openai-compat).
fn allowed_providers(key: &str) -> &'static [&'static str] {
    if key.starts_with("llm.tier.judge") {
        &["typesafe"]
    } else if key.starts_with("llm.tier.") {
        &["anthropic", "openai-compat"]
    } else {
        &["gemini", "openai-compat"]
    }
}

/// A configured model tier, search provider or MCP server after its shape,
/// endpoint and credential kind passed; its stored key is checked later.
struct ConfiguredEndpoint {
    key: String,
    url: String,
    reference: Option<String>,
}

fn inspect_configuration(
    rows: &[Value],
    mut resolve: impl FnMut(&str, u16) -> Result<Vec<IpAddr>, PreparationStatus>,
) -> Result<Vec<ConfiguredEndpoint>, PreparationStatus> {
    let mut endpoints = Vec::new();
    let mut tiers = BTreeSet::new();
    for row in rows {
        let key = required_string(row, "key")?;
        let value = &row["value"];
        if key.starts_with("llm.tier.")
            || key.starts_with("search.provider.")
            || key.starts_with("mcp.server.")
        {
            let mcp = key.starts_with("mcp.server.");
            if mcp {
                if value.get("transport").is_some_and(|v| v != "http") {
                    return Err(PreparationStatus::UnsupportedCapability);
                }
            } else {
                let provider = required_string(value, "provider")?;
                if !allowed_providers(key).contains(&provider) {
                    return Err(PreparationStatus::UnsupportedCapability);
                }
                required_string(value, "model")?;
                if key.starts_with("llm.tier.") {
                    tiers.insert(key);
                }
            }
            if value.get("auth_mode").is_some_and(|v| v != "api-key") {
                return Err(PreparationStatus::UnsupportedCapability);
            }
            let raw = required_string(value, if mcp { "url" } else { "base_url" })?;
            let endpoint = validate_public_endpoint(raw)
                .map_err(|_| PreparationStatus::UnsupportedCapability)?;
            let reference = if mcp && value["api_key_ref"].is_null() {
                None
            } else {
                Some(required_string(value, "api_key_ref")?)
            };
            if let Some(reference) = reference {
                validate_api_key_credential(reference)
                    .map_err(|_| PreparationStatus::UnsupportedCapability)?;
                if reference.starts_with("chatgpt.") || reference.starts_with("oauth.") {
                    return Err(PreparationStatus::UnsupportedCapability);
                }
            }
            let host = endpoint
                .url()
                .host_str()
                .ok_or(INVALID)?
                .trim_matches(['[', ']']);
            let addresses = if let Ok(ip) = host.parse() {
                vec![ip]
            } else {
                resolve(host, endpoint.url().port_or_known_default().unwrap_or(443))?
            };
            endpoint
                .validate_addresses(&addresses)
                .map_err(|_| PreparationStatus::UnsupportedCapability)?;
            endpoints.push(ConfiguredEndpoint {
                key: key.to_owned(),
                url: raw.to_owned(),
                reference: reference.map(str::to_owned),
            });
        }
    }
    for tier in ["llm.tier.codegen", "llm.tier.vision", "llm.tier.classify"] {
        if !tiers.contains(tier) {
            return Err(INVALID);
        }
    }
    Ok(endpoints)
}

/// ADR-034 §2 "all configured model tiers verified usable": the credential
/// binding the broker applies at injection (ADR-021 §8.1), decided from the
/// frozen declared table and the stored row without a provider call. A
/// rejected, unbound or hostless key fails here instead of during a turn; the
/// managed path never binds a key from a guest request (N1-R4).
fn usable(
    endpoint: &ConfiguredEndpoint,
    declared: &BTreeMap<String, Value>,
    row: Option<&Value>,
) -> Result<(), PreparationStatus> {
    let Some(reference) = &endpoint.reference else {
        return Ok(());
    };
    let row = row.ok_or(INVALID)?;
    if row["status"] == "rejected" {
        return Err(INVALID);
    }
    let open = reference.starts_with(LOCAL_KEY_PREFIX)
        || reference.starts_with(MCP_KEY_PREFIX)
        || reference.starts_with("llm.key.");
    let declared_hosts = declared.get(reference).map(hosts_of);
    if declared_hosts.is_none() && !open {
        return Err(INVALID);
    }
    let hosts = declared_hosts
        .filter(|hosts| !hosts.is_empty())
        .unwrap_or_else(|| hosts_of(row));
    if hosts.is_empty() || !host_allowed(&endpoint.url, &hosts) {
        return Err(INVALID);
    }
    Ok(())
}

pub(crate) struct PreparedData {
    pub(crate) traces: crate::tracestore::AnyTraceStore,
    pub(crate) declared: FrozenDeclared,
    pub(crate) readiness: Readiness,
    pub(crate) frozen: FrozenContent,
}

fn syncing<T>(_: T) -> PreparationStatus {
    NOT_SYNCED
}

fn prepare_data(
    cfg: &crate::Config,
    fixture: &PreseededFixture,
    client: &Arc<Client>,
    cancel: &super::CancelToken,
    resolve: impl FnMut(&str, u16) -> Result<Vec<IpAddr>, PreparationStatus>,
) -> Result<PreparedData, PreparationStatus> {
    for space in std::iter::once(&cfg.agent_space).chain(cfg.overlays.values().map(|o| &o.space)) {
        if !identifier(space) {
            return Err(INVALID);
        }
        if client.get_space(space).map_err(syncing)?["id"] != *space {
            return Err(NOT_SYNCED); // not joined/visible here yet
        }
    }
    let bundles = client.list_bundles(&cfg.agent_space).map_err(syncing)?;
    if bundles["synced"] != true {
        return Err(NOT_SYNCED);
    }
    let bundles = bundles["bundles"].as_array().ok_or(INVALID)?;
    for (id, root) in [
        ("bao/v1", &fixture.bao_root),
        ("system:general-chat/v1", &fixture.chat),
    ] {
        if !bundles.iter().any(|bundle| {
            bundle["id"] == id
                && bundle["rootId"] == *root
                && (id == "bao/v1" || bundle["derived"] == true)
        }) {
            return Err(INVALID);
        }
    }
    let ids: Vec<_> = fixture
        .stores()
        .iter()
        .map(|s| s.object_id.clone())
        .chain([fixture.chat.clone()])
        .collect();
    let objects = client
        .query_objects(
            &cfg.agent_space,
            &json!({"filter":{"id":{"$in":ids}},"limit":ids.len()}),
        )
        .map_err(syncing)?;
    if ids
        .iter()
        .any(|id| !objects.iter().any(|object| object["id"] == *id))
    {
        return Err(INVALID);
    }
    let rows = client
        .query(
            &cfg.agent_space,
            &fixture.config.object_id,
            &fixture.config.collection,
            &json!({"limit":1000}),
        )
        .map_err(syncing)?;
    if rows.len() >= 1000 {
        return Err(INVALID);
    }
    // Check all credential kinds before any secret-value read.
    let endpoints = inspect_configuration(&rows, resolve)?;
    // Frozen overlay content, captured once (reads only); the declared table
    // is built from the verified rows, never re-read (N1-R1, N1-I-4).
    let content = frozen::verify(client, cfg, &fixture.frozen).map_err(|e| match e {
        Verify::NeedsSetup => INVALID,
        Verify::Syncing | Verify::Missing => NOT_SYNCED,
    })?;
    let table = content.declared.clone();
    let declared = FrozenDeclared::new(table.clone());
    let refs: BTreeSet<String> = endpoints
        .iter()
        .filter_map(|e| e.reference.clone())
        .collect();
    let mut secret_rows = BTreeMap::new();
    for reference in &refs {
        let secret = client
            .query(
                &cfg.agent_space,
                &fixture.secrets.object_id,
                &fixture.secrets.collection,
                &json!({"filter":{"key":reference},"limit":2}),
            )
            .map_err(syncing)?;
        if secret.len() != 1
            || secret[0]["key"] != *reference
            || secret[0]["value"].as_str().is_none_or(|v| v.is_empty())
        {
            return Err(INVALID);
        }
        secret_rows.insert(reference.clone(), secret[0].clone());
    }
    let mut tiers = BTreeSet::new();
    for endpoint in &endpoints {
        usable(
            endpoint,
            &table,
            endpoint
                .reference
                .as_ref()
                .and_then(|reference| secret_rows.get(reference)),
        )?;
        if endpoint.key.starts_with("llm.tier.") {
            tiers.insert(endpoint.key.clone());
        }
    }
    // Existing trigger/run collections must be readable; never install them here.
    for store in [&fixture.triggers, &fixture.runs] {
        client
            .query(
                &cfg.agent_space,
                &store.object_id,
                &store.collection,
                &json!({"limit":1}),
            )
            .map_err(syncing)?;
    }
    if cancel.is_cancelled() {
        return Err(NOT_SYNCED);
    }
    let blobs = crate::blob::BlobDir::create(&cfg.traces_dir).map_err(|_| INVALID)?;
    let traces = crate::tracestore::AnyTraceStore::new(
        client.clone(),
        &cfg.agent_space,
        Some(fixture.self_peer_id.clone()),
        Some(blobs),
    )
    .map_err(|_| NOT_SYNCED)?; // the device-local collection creates: a prerequisite not met
    Ok(PreparedData {
        traces,
        declared,
        readiness: Readiness {
            source_revision: fixture.frozen.source_revision.clone(),
            programs: content.fingerprints.clone(),
            skills: content.skills.clone(),
            tiers,
            credential_refs: refs,
        },
        frozen: content,
    })
}

pub(super) fn prepare_host_controlled(
    cfg: crate::Config,
    options: ManagedOptions,
    ctx: TaskContext,
) -> Result<(), ManagedFailure> {
    // the stream-line bound is enforced at the read (N2a-r2 item 7); the gate's
    // `BoundedLines` then refuses the over-long line as a capacity fault
    let transport = Box::new(HttpTransport::managed(
        &cfg.addr,
        super::admission::MAX_STREAM_LINE_BYTES,
    ));
    prepare_with(cfg, options, ctx, transport, |host, port| {
        (host, port)
            .to_socket_addrs()
            .map(|addresses| addresses.map(|a| a.ip()).collect())
            .map_err(|_| NOT_SYNCED)
    })
}

/// Empties the owner's service slot on every exit of the preparation task —
/// return, cancel, panic — so the retained client/cage never outlive it (N1-R6).
struct ServiceSlot(Arc<Shared>);
impl Drop for ServiceSlot {
    fn drop(&mut self) {
        self.0
            .service
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take();
    }
}

fn prepare_with(
    cfg: crate::Config,
    options: ManagedOptions,
    ctx: TaskContext,
    transport: Box<dyn Transport + Send + Sync>,
    mut resolve: impl FnMut(&str, u16) -> Result<Vec<IpAddr>, PreparationStatus>,
) -> Result<(), ManagedFailure> {
    if !identifier(&cfg.agent_space)
        || cfg.overlays.values().any(|o| !identifier(&o.space))
        || !cfg.overlays.contains_key("agent")
    {
        return Err(ManagedFailure::InvalidConfiguration);
    }
    // SAFETY: ManagedOptions only accepts TrustedArtifact's explicit provenance contract.
    let cage = unsafe {
        crate::runner::Cage::from_trusted_artifact_managed(
            options.artifact().bytes(),
            options.artifact().manifest(),
        )
    }
    .map_err(|_| ManagedFailure::IncompatibleArtifact)?;
    if ctx.shared.cancel.is_cancelled() {
        return Ok(());
    }
    let _slot = ServiceSlot(ctx.shared.clone());
    let publish = |status: PreparationStatus| {
        ctx.shared
            .preparation_status
            .store(status.atomic(), Ordering::Release)
    };
    match &options.fixture {
        None => publish(INVALID),
        Some(fixture) if !protocol_compatible(&fixture.frozen, options.artifact().manifest()) => {
            publish(PreparationStatus::UnsupportedCapability)
        }
        Some(fixture) => {
            let (client, gate) = client(&cfg, &ctx.shared, transport, fixture.objects());
            // One attempt (ADR-034 §5): a missing or unsynced prerequisite is Needs
            // setup; main's monitor re-requests preparation, Rust never polls.
            let attempt = prepare_data(&cfg, fixture, &client, &ctx.shared.cancel, |host, port| {
                if ctx.shared.cancel.is_cancelled() {
                    return Err(NOT_SYNCED);
                }
                ctx.wait(WaitClass::Dns, None);
                resolve(host, port)
            });
            match attempt {
                Ok(prepared) => {
                    // Retained by the owner, not this stack: the run service takes it from here.
                    let service = PreparedService::new(
                        &cfg,
                        &options,
                        fixture,
                        cage.clone(),
                        client.clone(),
                        gate.clone(),
                        prepared,
                    );
                    *ctx.shared.service.lock().unwrap_or_else(|e| e.into_inner()) =
                        Some(Arc::new(service));
                    publish(PreparationStatus::Prepared);
                }
                Err(status) => publish(status),
            }
        }
    }
    // The service is retained; readiness and admission are the session's
    // (`managed::session`): nothing opens without checked takeover (ADR-034 §5).
    while !ctx.shared.cancel.is_cancelled() {
        let deadline = Instant::now() + Duration::from_secs(300);
        ctx.idle_until(deadline);
    }
    drop(cage);
    Ok(())
}

#[cfg(test)]
mod tests;
