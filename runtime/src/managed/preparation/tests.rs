use super::*;
use crate::config::Overlay;
use crate::managed::frozen::{sha256_hex, WORKING_ALIAS};
use crate::managed::preparation::SetupReason;
use crate::testutil::{CallLog, StubTransport};

const CODE: &str = "def main(args):\n    return args\n";
const HOSTS: &str = "provider.example";
const INVALID_SETUP: PreparationStatus = PreparationStatus::NeedsSetup(SetupReason::Invalid);
const NOT_SYNCED_SETUP: PreparationStatus = PreparationStatus::NeedsSetup(SetupReason::NotSynced);
const BUNDLES: &str = r#"{"synced":true,"bundles":[{"id":"bao/v1","rootId":"bao-root"},{"id":"system:general-chat/v1","rootId":"chat","derived":true}]}"#;
const OBJECTS: &str = r#"{"records":[{"id":"config"},{"id":"secrets"},{"id":"triggers"},{"id":"runs"},{"id":"turns"},{"id":"chat"}]}"#;

fn shared() -> Arc<Shared> {
    Arc::new(Shared::new(
        super::super::CancelToken::new(),
        Instant::now(),
        None,
    ))
}
fn credentials(hosts: &str) -> String {
    format!("[{{\"ref\":\"llm.key.fixture\",\"about\":{{\"hosts\":[\"{hosts}\"]}}}}]")
}
fn pinned(alias: &str, name: &str, code: &str, hosts: &str) -> FrozenProgram {
    FrozenProgram {
        alias: alias.into(),
        name: name.into(),
        version: "v1".into(),
        fingerprint: crate::deploy::fingerprint(code, "LLM", false, "", &json!({})),
        credentials_sha256: sha256_hex(&credentials(hosts)),
        manifest: false,
    }
}
fn pins() -> FrozenPins {
    FrozenPins {
        source_revision: "repos-test".into(),
        anyrt_revision: "synthetic-test".into(),
        trace_schema: crate::runner::TRACE_SCHEMA,
        kernel_source_sha256: "1".repeat(64),
        profile_sha256: "2".repeat(64),
        overlays: vec!["agent".into()],
        programs: vec![pinned("agent", "llm", CODE, HOSTS)],
        skills: vec![],
    }
}
fn fixture(root: &std::path::Path) -> (crate::Config, PreseededFixture) {
    let store = |name: &str| StoreAddress {
        object_id: name.into(),
        collection: format!("type_{name}"),
    };
    (
        crate::Config {
            agent_space: "fixture".into(),
            traces_dir: root.join("traces"),
            overlays: BTreeMap::from([(
                "agent".to_string(),
                Overlay {
                    space: "overlay".into(),
                    invite: None,
                },
            )]),
            ..Default::default()
        },
        PreseededFixture {
            account_root: root.into(),
            account_id: "account".into(),
            self_peer_id: "peer".into(),
            bao_root: "bao-root".into(),
            chat: "chat".into(),
            config: store("config"),
            secrets: store("secrets"),
            triggers: store("triggers"),
            runs: store("runs"),
            turns: store("turns"),
            frozen: pins(),
        },
    )
}
fn configuration() -> Vec<Value> {
    ["codegen","vision","classify"].into_iter().map(|tier|json!({"key":format!("llm.tier.{tier}"),"value":{"provider":"anthropic","model":"synthetic","base_url":"https://provider.example","api_key_ref":"llm.key.fixture"}})).collect()
}
fn program_row(name: &str, hosts: &str) -> Value {
    json!({"id": format!("obj-{name}"), "t-program": {"p-name": name, "p-version": "v1", "p-any_tool": false, "p-summary": "LLM",
        "p-credentials": credentials(hosts)}})
}
/// The schema lookup `ProgramSchema::lookup` performs: types, properties, datasets.
fn schema_replies() -> Vec<Value> {
    vec![
        json!({"types":[{"id":"t-program","xKey":"program"}]}),
        json!({"properties":[{"xKey":"name","id":"p-name"},{"xKey":"version","id":"p-version"},{"xKey":"any_tool","id":"p-any_tool"},{"xKey":"summary","id":"p-summary"},{"xKey":"credentials","id":"p-credentials"}]}),
        json!({"datasets":[{"key":"program_source","collection":"t-program_program_source"},{"key":"program_manifest","collection":"t-program_program_manifest"}]}),
    ]
}
fn v(text: &str) -> Value {
    serde_json::from_str(text).unwrap()
}
/// One space's verification replies: schema + rows + per-program source/manifest,
/// then the skill-type listing (none here).
fn space_with_programs(transport: &StubTransport, rows: Vec<Value>, code: &str) {
    for reply in schema_replies() {
        transport.push(200, reply);
    }
    let n = rows.len();
    transport.push(200, json!({"records": rows}));
    for _ in 0..n {
        transport.push(200, json!({"records":[{"code": code}]}));
        transport.push(200, json!({"records":[]}));
    }
    transport.push(200, json!({"types":[{"id":"t-program","xKey":"program"}]}));
}
/// An empty space: no program type, no skill type.
fn empty_space(transport: &StubTransport) {
    transport.push(200, json!({"types":[]}));
    transport.push(200, json!({"types":[]}));
}
fn tail(transport: &StubTransport, secret: Value) {
    transport.push(200, json!({"records":[secret]}));
    transport.push(200, json!({"records":[]}));
    transport.push(200, json!({"records":[]}));
    for _ in 0..3 {
        transport.push(200, json!({}));
    }
}
fn head(transport: &StubTransport, spaces: &[&str], rows: Vec<Value>) {
    for space in spaces {
        transport.push(200, json!({"id": space}));
    }
    transport.push(200, v(BUNDLES));
    transport.push(200, v(OBJECTS));
    transport.push(200, json!({"records": rows}));
}
struct Script {
    rows: Vec<Value>,
    programs: Vec<Value>,
    code: &'static str,
    /// 200, or the status of a failed source read (a transient 500, never a drift)
    source_status: u16,
    /// the agent_skill objects the overlay lists: (name, markdown)
    skills: Vec<(&'static str, &'static str)>,
    secret: Value,
    /// the `program_manifest` record each program answers with (None = no record)
    manifest: Option<Value>,
}
impl Default for Script {
    fn default() -> Self {
        Script {
            rows: configuration(),
            programs: vec![program_row("llm", HOSTS)],
            code: CODE,
            source_status: 200,
            skills: vec![],
            secret: json!({"key":"llm.key.fixture","value":"fake-only","status":"set"}),
            manifest: None,
        }
    }
}
/// Replies in the exact order preparation reads: working + overlay space, bundles,
/// objects, config, the working space (empty), the overlay's programs (schema +
/// rows + per-program source/manifest) and skills (types + rows + bodies),
/// secrets, triggers/runs, trace creates. The declared table is built from the
/// program rows above — no second overlay read (N1-I-4).
fn script(s: Script) -> (StubTransport, CallLog) {
    let transport = StubTransport::new();
    let log = transport.log();
    head(&transport, &["fixture", "overlay"], s.rows);
    empty_space(&transport);
    for reply in schema_replies() {
        transport.push(200, reply);
    }
    transport.push(200, json!({"records": s.programs.clone()}));
    for _ in &s.programs {
        if s.source_status == 200 {
            transport.push(200, json!({"records":[{"code": s.code}]}));
            match &s.manifest {
                Some(m) => transport.push(200, json!({"records":[{"manifest": m}]})),
                None => transport.push(200, json!({"records":[]})),
            }
        } else {
            transport.push(
                s.source_status,
                json!({"error":{"code":"unavailable","message":"not yet"}}),
            );
        }
    }
    if s.skills.is_empty() {
        transport.push(200, json!({"types":[{"id":"t-program","xKey":"program"}]}));
    } else {
        transport.push(200, json!({"types":[{"id":"t-program","xKey":"program"},{"id":"t-skill","xKey":"agent_skill"}]}));
        let rows: Vec<Value> = s
            .skills
            .iter()
            .map(|(name, _)| json!({"id": format!("skill-{name}"), "any": {"name": name}}))
            .collect();
        transport.push(200, json!({"records": rows}));
        for (_, body) in &s.skills {
            transport.push(200, json!({"content": body}));
        }
    }
    tail(&transport, s.secret);
    (transport, log)
}
fn public(_: &str, _: u16) -> Result<Vec<IpAddr>, PreparationStatus> {
    Ok(vec!["93.184.216.34".parse().unwrap()])
}
fn run(
    cfg: &crate::Config,
    fixture: &PreseededFixture,
    transport: StubTransport,
) -> Result<PreparedData, PreparationStatus> {
    // the gate holds a weak owner reference: keep the owner alive for the read
    let state = shared();
    let (c, _) = client(cfg, &state, Box::new(transport), fixture.objects());
    prepare_data(cfg, fixture, &c, &super::super::CancelToken::new(), public)
}
fn prepare_fixture(
    cfg: &crate::Config,
    fixture: &PreseededFixture,
    s: Script,
) -> (Result<PreparedData, PreparationStatus>, CallLog) {
    let (transport, log) = script(s);
    (run(cfg, fixture, transport), log)
}
fn prepare(dir: &std::path::Path, s: Script) -> (Result<PreparedData, PreparationStatus>, CallLog) {
    let (cfg, fixture) = fixture(dir);
    prepare_fixture(&cfg, &fixture, s)
}
fn secret_reads(log: &CallLog) -> usize {
    log.lock()
        .unwrap()
        .iter()
        .filter(|(_, path, body)| {
            path.ends_with("/query") && body.as_ref().is_some_and(|b| b["objectId"] == "secrets")
        })
        .count()
}

#[test]
fn real_preparation_reads_preseeded_data_and_only_creates_three_local_collections() {
    let dir = tempfile::tempdir().unwrap();
    let (prepared, log) = prepare(dir.path(), Script::default());
    let prepared = prepared.unwrap();
    assert_eq!(
        prepared.readiness.programs,
        BTreeMap::from([(
            "agent:llm@v1".to_string(),
            pinned("agent", "llm", CODE, HOSTS).fingerprint
        )])
    );
    assert_eq!(
        prepared.readiness.tiers,
        BTreeSet::from([
            "llm.tier.codegen".to_string(),
            "llm.tier.vision".into(),
            "llm.tier.classify".into()
        ])
    );
    assert_eq!(
        prepared.readiness.credential_refs,
        BTreeSet::from(["llm.key.fixture".to_string()])
    );
    assert_eq!(prepared.readiness.source_revision, "repos-test");
    assert!(prepared.readiness.skills.is_empty());
    assert!(prepared.declared.snapshot().contains_key("llm.key.fixture"));
    assert_eq!(prepared.frozen.programs.len(), 1);
    assert_eq!(prepared.frozen.aliases[WORKING_ALIAS], "fixture");
    let log = log.lock().unwrap();
    let writes: Vec<_> = log
        .iter()
        .filter(|(method, _, _)| method == "PUT")
        .collect();
    assert_eq!(writes.len(), 3);
    assert!(writes
        .iter()
        .all(|(_, path, body)| path == "/v1/local/collections"
            && body.as_ref().unwrap()["spaceId"] == "fixture"));
    assert!(log.iter().all(|(method, path, _)| method == "GET"
        || path.ends_with("/query")
        || path == "/v1/local/collections"));
    assert!(!log.iter().any(|(_, path, _)| path.contains("/devices")
        || path.contains("/auth")
        || path.contains("/events")));
    assert_eq!(log.len(), 20);
    assert!(dir.path().join("traces/blobs").is_dir());
}
#[test]
fn unsupported_capability_is_rejected_before_any_secret_value_read_or_write() {
    for unsupported in ["oauth.gmail", "chatgpt.local"] {
        let dir = tempfile::tempdir().unwrap();
        let mut rows = configuration();
        rows[2]["value"]["api_key_ref"] = json!(unsupported);
        let (result, log) = prepare(
            dir.path(),
            Script {
                rows,
                ..Default::default()
            },
        );
        assert_eq!(result.err(), Some(PreparationStatus::UnsupportedCapability));
        assert_eq!(log.lock().unwrap().len(), 5);
        assert!(!dir.path().join("traces").exists());
    }
}
#[test]
fn missing_or_private_configuration_cannot_become_ready() {
    let mut rows = configuration();
    rows.pop();
    assert_eq!(
        inspect_configuration(&rows, public).err(),
        Some(INVALID_SETUP)
    );
    let mut rows = configuration();
    rows[0]["value"]["base_url"] = json!("https://127.0.0.1");
    assert_eq!(
        inspect_configuration(&rows, public).err(),
        Some(PreparationStatus::UnsupportedCapability)
    );
    assert_eq!(
        inspect_configuration(&configuration(), |_, _| Ok(vec!["10.0.0.1"
            .parse()
            .unwrap()]))
        .err(),
        Some(PreparationStatus::UnsupportedCapability)
    );
    let mut rows = configuration();
    rows.push(json!({"key":"mcp.server.fixture","value":{"url":"https://mcp.example","transport":"stdio"}}));
    assert_eq!(
        inspect_configuration(&rows, public).err(),
        Some(PreparationStatus::UnsupportedCapability)
    );
}
#[test]
fn providers_are_checked_per_row_kind_like_the_pinned_guests() {
    let mut rows = configuration();
    rows[0]["value"]["provider"] = json!("gemini");
    assert_eq!(
        inspect_configuration(&rows, public).err(),
        Some(PreparationStatus::UnsupportedCapability)
    );
    let mut rows = configuration();
    rows.push(json!({"key":"search.provider.web","value":{"provider":"anthropic","model":"x","base_url":"https://search.example","api_key_ref":"llm.key.fixture"}}));
    assert_eq!(
        inspect_configuration(&rows, public).err(),
        Some(PreparationStatus::UnsupportedCapability)
    );
    let mut rows = configuration();
    rows.push(json!({"key":"search.provider.web","value":{"provider":"gemini","model":"x","base_url":"https://search.example","api_key_ref":"llm.key.fixture"}}));
    rows.push(json!({"key":"llm.tier.judge","value":{"provider":"typesafe","model":"x","base_url":"https://judge.example","api_key_ref":"llm.key.fixture"}}));
    assert_eq!(inspect_configuration(&rows, public).unwrap().len(), 5);
}
#[test]
fn frozen_program_drift_unpinned_or_credentials_change_is_invalid_before_secret_reads() {
    let drifted = Script {
        code: "def main(args):\n    return None\n",
        ..Default::default()
    };
    let unpinned = Script {
        programs: vec![program_row("llm", HOSTS), program_row("extra", HOSTS)],
        ..Default::default()
    };
    // same code, same deploy fingerprint, a changed credentials declaration (N1-R3)
    let credentials_only = Script {
        programs: vec![program_row("llm", "attacker.example")],
        ..Default::default()
    };
    for script in [drifted, unpinned, credentials_only] {
        let dir = tempfile::tempdir().unwrap();
        let (result, log) = prepare(dir.path(), script);
        assert_eq!(result.err(), Some(INVALID_SETUP));
        assert_eq!(secret_reads(&log), 0);
        assert!(!dir.path().join("traces").exists());
    }
}
#[test]
fn sync_lag_and_failed_reads_are_typed_not_synced_and_never_retried_natively() {
    let missing = Script {
        programs: vec![],
        ..Default::default()
    };
    let failed_source = Script {
        source_status: 500,
        ..Default::default()
    };
    for script in [missing, failed_source] {
        let dir = tempfile::tempdir().unwrap();
        let (result, log) = prepare(dir.path(), script);
        assert_eq!(result.err(), Some(NOT_SYNCED_SETUP));
        assert_eq!(secret_reads(&log), 0);
        assert!(!dir.path().join("traces").exists());
    }
    for (bundles, expected) in [
        (json!({"synced":false,"bundles":[]}), NOT_SYNCED_SETUP),
        (json!({"synced":true}), INVALID_SETUP),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let (cfg, fixture) = fixture(dir.path());
        let transport = StubTransport::new();
        transport.push(200, json!({"id":"fixture"}));
        transport.push(200, json!({"id":"overlay"}));
        transport.push(200, bundles);
        assert_eq!(run(&cfg, &fixture, transport).err(), Some(expected));
    }
    // a failed device-local collection create is the same typed reason (N2a-R5)
    let dir = tempfile::tempdir().unwrap();
    let (cfg, fixture) = fixture(dir.path());
    let transport = StubTransport::new();
    head(&transport, &["fixture", "overlay"], configuration());
    empty_space(&transport);
    space_with_programs(&transport, vec![program_row("llm", HOSTS)], CODE);
    transport.push(
        200,
        json!({"records":[json!({"key":"llm.key.fixture","value":"fake-only","status":"set"})]}),
    );
    transport.push(200, json!({"records":[]}));
    transport.push(200, json!({"records":[]}));
    transport.push(
        500,
        json!({"error":{"code":"local.unavailable","message":"x"}}),
    );
    assert_eq!(run(&cfg, &fixture, transport).err(), Some(NOT_SYNCED_SETUP));
}
#[test]
fn every_configured_overlay_is_verified_and_an_unpinned_overlay_must_be_empty() {
    let dir = tempfile::tempdir().unwrap();
    let (mut cfg, mut fixture) = fixture(dir.path());
    cfg.overlays.insert(
        "connectors".into(),
        Overlay {
            space: "conn".into(),
            invite: None,
        },
    );
    // pins that do not cover the configured overlay: a setup mismatch before any overlay read
    let transport = StubTransport::new();
    let log = transport.log();
    head(&transport, &["fixture", "overlay", "conn"], configuration());
    assert_eq!(run(&cfg, &fixture, transport).err(), Some(INVALID_SETUP));
    assert_eq!(log.lock().unwrap().len(), 6);
    fixture.frozen.overlays.push("connectors".into());
    // the connectors space holds programs nobody pinned: refused (N1-I-1)
    let transport = StubTransport::new();
    let log = transport.log();
    head(&transport, &["fixture", "overlay", "conn"], configuration());
    empty_space(&transport);
    space_with_programs(&transport, vec![program_row("llm", HOSTS)], CODE);
    for reply in schema_replies() {
        transport.push(200, reply);
    }
    transport.push(200, json!({"records":[program_row("mail", HOSTS)]}));
    assert_eq!(run(&cfg, &fixture, transport).err(), Some(INVALID_SETUP));
    assert!(log
        .lock()
        .unwrap()
        .iter()
        .any(|(_, path, _)| path.contains("/spaces/conn/")));
    // an empty connectors overlay (no program type, no skill type) is fine
    let transport = StubTransport::new();
    head(&transport, &["fixture", "overlay", "conn"], configuration());
    empty_space(&transport);
    space_with_programs(&transport, vec![program_row("llm", HOSTS)], CODE);
    empty_space(&transport);
    tail(
        &transport,
        json!({"key":"llm.key.fixture","value":"fake-only","status":"set"}),
    );
    let prepared = run(&cfg, &fixture, transport).unwrap();
    assert_eq!(prepared.frozen.aliases.len(), 3); // working + agent + connectors
}
#[test]
fn working_space_programs_are_pinned_like_an_overlay_and_resolve_unqualified() {
    use crate::resolver::{ModuleResolver, ResolveError};
    let dir = tempfile::tempdir().unwrap();
    let (cfg, mut fixture) = fixture(dir.path());
    let heartbeat = "def main(args):\n    return 'beat'\n";
    fixture.frozen.programs.push(pinned(
        WORKING_ALIAS,
        "android-poc-heartbeat",
        heartbeat,
        HOSTS,
    ));
    let transport = StubTransport::new();
    head(&transport, &["fixture", "overlay"], configuration());
    space_with_programs(
        &transport,
        vec![program_row("android-poc-heartbeat", HOSTS)],
        heartbeat,
    );
    space_with_programs(&transport, vec![program_row("llm", HOSTS)], CODE);
    tail(
        &transport,
        json!({"key":"llm.key.fixture","value":"fake-only","status":"set"}),
    );
    let prepared = run(&cfg, &fixture, transport).unwrap();
    assert!(prepared
        .readiness
        .programs
        .contains_key("working:android-poc-heartbeat@v1"));
    let mut resolver = crate::managed::frozen::FrozenResolver::new(Arc::new(prepared.frozen));
    assert_eq!(
        resolver.resolve("android-poc-heartbeat@v1", None).unwrap()["source"],
        heartbeat
    );
    // an agent-authored program is refused typed, never a sync wait
    assert!(matches!(
        resolver.resolve("mine@v1", None),
        Err(ResolveError::NotPinned { .. })
    ));
    // the working space cannot be listed as an overlay; a working-space program
    // present without a pin is drift
    let mut listed = fixture.clone();
    listed.frozen.overlays.push(WORKING_ALIAS.into());
    assert!(listed.validate().is_err());
    let mut unpinned = fixture.clone();
    unpinned.frozen.programs.pop();
    let transport = StubTransport::new();
    head(&transport, &["fixture", "overlay"], configuration());
    for reply in schema_replies() {
        transport.push(200, reply);
    }
    transport.push(200, json!({"records":[program_row("mine", HOSTS)]}));
    assert_eq!(run(&cfg, &unpinned, transport).err(), Some(INVALID_SETUP));
}
/// N2a-r2 item 1 / ADR-021 §8.1: a working-space program's `credentials`
/// declaration never enters the declared table — an overlay's declaration wins
/// for a shared ref and a working-only ref stays undeclared.
#[test]
fn working_space_programs_declare_no_credentials() {
    let dir = tempfile::tempdir().unwrap();
    let (cfg, mut fixture) = fixture(dir.path());
    let heartbeat = "def main(args):\n    return 'beat'\n";
    // the working-space program re-declares the overlay's ref toward another host
    fixture.frozen.programs.push(pinned(
        WORKING_ALIAS,
        "android-poc-heartbeat",
        heartbeat,
        "collector.example",
    ));
    let transport = StubTransport::new();
    head(&transport, &["fixture", "overlay"], configuration());
    space_with_programs(
        &transport,
        vec![program_row("android-poc-heartbeat", "collector.example")],
        heartbeat,
    );
    space_with_programs(&transport, vec![program_row("llm", HOSTS)], CODE);
    tail(
        &transport,
        json!({"key":"llm.key.fixture","value":"fake-only","status":"set"}),
    );
    let prepared = run(&cfg, &fixture, transport).unwrap();
    assert_eq!(
        prepared.frozen.declared["llm.key.fixture"]["hosts"],
        json!([HOSTS]),
        "the overlay's declaration, never the working space's"
    );
    assert_eq!(prepared.frozen.declared.len(), 1);
    // a ref only the working space declares is undeclared on the managed path
    let mut only_working = fixture.clone();
    only_working.frozen.programs = vec![
        FrozenProgram {
            credentials_sha256: sha256_hex(""),
            ..pinned("agent", "llm", CODE, HOSTS)
        },
        pinned(WORKING_ALIAS, "android-poc-heartbeat", heartbeat, HOSTS),
    ];
    let transport = StubTransport::new();
    head(&transport, &["fixture", "overlay"], configuration());
    space_with_programs(
        &transport,
        vec![program_row("android-poc-heartbeat", HOSTS)],
        heartbeat,
    );
    let mut undeclared = program_row("llm", HOSTS);
    undeclared["t-program"]
        .as_object_mut()
        .unwrap()
        .remove("p-credentials");
    space_with_programs(&transport, vec![undeclared], CODE);
    tail(
        &transport,
        json!({"key":"llm.key.fixture","value":"fake-only","status":"set",
               "hosts":[HOSTS]}),
    );
    let prepared = run(&cfg, &only_working, transport).unwrap();
    assert!(prepared.frozen.declared.is_empty());
}

/// N2a-r2 R2-4: a pinned part that has not arrived is `NotSynced`, a part the
/// pin does not expect is drift.
#[test]
fn a_pinned_manifest_or_skill_body_not_yet_here_is_not_synced_an_unexpected_one_is_drift() {
    let manifest = json!({"capabilities": ["net.http"]});
    let with_manifest = |manifest_expected: bool| {
        let mut p = pinned("agent", "llm", CODE, HOSTS);
        p.fingerprint = crate::deploy::fingerprint(CODE, "LLM", false, "", &manifest);
        p.manifest = manifest_expected;
        p
    };
    // the manifest is pinned but its record is not on the device yet
    let dir = tempfile::tempdir().unwrap();
    let (cfg, mut fixture) = fixture(dir.path());
    fixture.frozen.programs = vec![with_manifest(true)];
    let (transport, _) = script(Script::default());
    assert_eq!(run(&cfg, &fixture, transport).err(), Some(NOT_SYNCED_SETUP));
    // …and verified once it is here
    let (transport, _) = script(Script {
        manifest: Some(manifest.clone()),
        ..Default::default()
    });
    assert!(run(&cfg, &fixture, transport).is_ok());
    // a manifest the pin does not expect is drift, as is a changed one
    fixture.frozen.programs = vec![pinned("agent", "llm", CODE, HOSTS)];
    let (transport, _) = script(Script {
        manifest: Some(manifest.clone()),
        ..Default::default()
    });
    assert_eq!(run(&cfg, &fixture, transport).err(), Some(INVALID_SETUP));
    fixture.frozen.programs = vec![with_manifest(true)];
    let (transport, _) = script(Script {
        manifest: Some(json!({"capabilities": []})),
        ..Default::default()
    });
    assert_eq!(run(&cfg, &fixture, transport).err(), Some(INVALID_SETUP));
    // a pinned skill whose body has not arrived (empty) is not synced; a
    // changed body is drift
    fixture.frozen.programs = vec![pinned("agent", "llm", CODE, HOSTS)];
    fixture.frozen.skills.push(FrozenSkill {
        alias: "agent".into(),
        name: "_core".into(),
        sha256: sha256_hex("# core\n"),
    });
    let (transport, _) = script(Script {
        skills: vec![("_core", "")],
        ..Default::default()
    });
    assert_eq!(run(&cfg, &fixture, transport).err(), Some(NOT_SYNCED_SETUP));
    let (transport, _) = script(Script {
        skills: vec![("_core", "# edited\n")],
        ..Default::default()
    });
    assert_eq!(run(&cfg, &fixture, transport).err(), Some(INVALID_SETUP));
}

#[test]
fn skills_are_pinned_verified_and_drift_or_extras_are_invalid() {
    let dir = tempfile::tempdir().unwrap();
    let (cfg, mut fixture) = fixture(dir.path());
    fixture.frozen.skills.push(FrozenSkill {
        alias: "agent".into(),
        name: "_core".into(),
        sha256: sha256_hex("# core\n"),
    });
    let (prepared, _) = prepare_fixture(
        &cfg,
        &fixture,
        Script {
            skills: vec![("_core", "# core\n")],
            ..Default::default()
        },
    );
    assert_eq!(
        prepared.unwrap().readiness.skills,
        BTreeMap::from([("agent:_core".to_string(), sha256_hex("# core\n"))])
    );
    for (skills, expected) in [
        (vec![("_core", "# changed\n")], INVALID_SETUP),
        (
            vec![("_core", "# core\n"), ("_soul", "# soul\n")],
            INVALID_SETUP,
        ),
        (vec![], NOT_SYNCED_SETUP),
    ] {
        let (result, log) = prepare_fixture(
            &cfg,
            &fixture,
            Script {
                skills,
                ..Default::default()
            },
        );
        assert_eq!(result.err(), Some(expected));
        assert_eq!(secret_reads(&log), 0);
    }
}
#[test]
fn protocol_pins_must_name_the_running_artifact_kernel_profile_and_schema() {
    let manifest = |source: &str, profile: &str| crate::engine_profile::ArtifactManifestV1 {
        schema_version: 1,
        wasmtime_version: crate::engine_profile::WASMTIME_VERSION.into(),
        profile: crate::engine_profile::EngineProfileV1::android(),
        resolved_profile: Value::Null,
        profile_sha256: profile.into(),
        source_sha256: source.into(),
        artifact_sha256: String::new(),
        provenance: crate::engine_profile::BuildProvenance {
            anyrt_revision: "synthetic-test".into(),
            rust_version: "test".into(),
            ndk_version: "none".into(),
            jni_protocol_version: None,
            any_version_pin: "v0.3.5".into(),
            any_versions_tested: vec![],
        },
    };
    let dir = tempfile::tempdir().unwrap();
    let (_, fixture) = fixture(dir.path());
    let good = manifest(&"1".repeat(64), &"2".repeat(64));
    assert!(protocol_compatible(&fixture.frozen, &good));
    assert!(!protocol_compatible(
        &fixture.frozen,
        &manifest(&"f".repeat(64), &"2".repeat(64))
    ));
    assert!(!protocol_compatible(
        &fixture.frozen,
        &manifest(&"1".repeat(64), &"f".repeat(64))
    ));
    let mut other_kernel = fixture.frozen.clone();
    other_kernel.trace_schema += 1;
    assert!(!protocol_compatible(&other_kernel, &good));
    let mut other_runtime = fixture.frozen.clone();
    other_runtime.anyrt_revision = "moved".into();
    assert!(!protocol_compatible(&other_runtime, &good));
    let mut unpinned = fixture.clone();
    unpinned.frozen.programs.clear();
    assert!(unpinned.validate().is_err());
    let mut bad_hash = fixture.clone();
    bad_hash.frozen.programs[0].fingerprint = "xyz".into();
    assert!(bad_hash.validate().is_err());
    let mut duplicate = fixture.clone();
    duplicate
        .frozen
        .programs
        .push(duplicate.frozen.programs[0].clone());
    assert!(duplicate.validate().is_err());
    let mut foreign_alias = fixture.clone();
    foreign_alias.frozen.programs[0].alias = "connectors".into();
    assert!(foreign_alias.validate().is_err());
    let mut no_agent = fixture.clone();
    no_agent.frozen.overlays = vec!["connectors".into()];
    assert!(no_agent.validate().is_err());
    let mut bad_account = fixture.clone();
    bad_account.account_id = "bad id".into();
    assert!(bad_account.validate().is_err());
    // real-shaped Any ids carry a dot: accepted by validation and by the gate
    let mut dotted = fixture.clone();
    dotted.chat = "bafyreigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi.2a1b".into();
    dotted.bao_root = "bafyreigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi.3c4d".into();
    assert!(dotted.validate().is_ok());
}
#[test]
fn rejected_unbound_undeclared_or_hostless_credentials_are_not_usable() {
    let rejected = Script {
        secret: json!({"key":"llm.key.fixture","value":"fake-only","status":"rejected"}),
        ..Default::default()
    };
    let mut rows = configuration();
    for row in &mut rows {
        row["value"]["api_key_ref"] = json!("connector.key.fixture");
    }
    let undeclared = Script {
        rows,
        secret: json!({"key":"connector.key.fixture","value":"fake-only","status":"set"}),
        ..Default::default()
    };
    for script in [rejected, undeclared] {
        let dir = tempfile::tempdir().unwrap();
        let (result, _) = prepare(dir.path(), script);
        assert_eq!(result.err(), Some(INVALID_SETUP));
        assert!(!dir.path().join("traces").exists());
    }
    // declared hosts that do not cover the endpoint (the pin follows the row text)
    let dir = tempfile::tempdir().unwrap();
    let (cfg, mut fixture) = fixture(dir.path());
    fixture.frozen.programs = vec![pinned("agent", "llm", CODE, "other.example")];
    let (result, _) = prepare_fixture(
        &cfg,
        &fixture,
        Script {
            programs: vec![program_row("llm", "other.example")],
            ..Default::default()
        },
    );
    assert_eq!(result.err(), Some(INVALID_SETUP));
    // An open ref binds through its stored row's hosts, like the broker's injection;
    // a row with no hosts anywhere is NOT usable: managed never binds on first use (N1-R4).
    let open_rows = || {
        let mut rows = configuration();
        for row in &mut rows {
            row["value"]["api_key_ref"] = json!("local.key.fixture");
        }
        rows
    };
    let dir = tempfile::tempdir().unwrap();
    let (result, _) = prepare(
        dir.path(),
        Script {
            rows: open_rows(),
            secret: json!({"key":"local.key.fixture","value":"fake-only","status":"set","hosts":["provider.example"]}),
            ..Default::default()
        },
    );
    assert!(result.is_ok());
    let dir = tempfile::tempdir().unwrap();
    let (result, _) = prepare(
        dir.path(),
        Script {
            rows: open_rows(),
            secret: json!({"key":"local.key.fixture","value":"fake-only","status":"set"}),
            ..Default::default()
        },
    );
    assert_eq!(result.err(), Some(INVALID_SETUP));
}
#[test]
fn transport_refuses_all_synced_writes_registry_streams_and_post_cancel_calls() {
    let dir = tempfile::tempdir().unwrap();
    let (cfg, fixture) = fixture(dir.path());
    let state = shared();
    let transport = StubTransport::new();
    let log = transport.log();
    let (client, _) = client(&cfg, &state, Box::new(transport), fixture.objects());
    assert!(client.activate_device("bao").is_err());
    assert!(client.upsert_self_device(&json!({})).is_err());
    assert!(client.create_space("forbidden").is_err());
    assert!(client
        .ensure_bundle("fixture", "bao/v1", "bao", "page", &[], false)
        .is_err());
    assert!(client
        .upsert_record("fixture", "config", "type_config", "key", &json!({}))
        .is_err());
    assert!(client
        .patch_type("fixture", "t-program", &json!({"hidden": false}))
        .is_err());
    assert!(client.list_types("elsewhere").is_err());
    assert!(client
        .list_properties("fixture", "t-program/../../debug")
        .is_err());
    assert!(client
        .local_ensure(&Client::local_coll("fixture", "foreign"), &[])
        .is_err());
    assert!(client
        .subscribe_dataset("fixture", "chat", "chat_messages", &json!({}))
        .is_err());
    state.cancel.cancel();
    assert!(client.get_space("fixture").is_err());
    assert!(log.lock().unwrap().is_empty());
}

#[cfg(feature = "compiler")]
fn synthetic(revision: &str) -> (crate::engine_profile::ArtifactManifestV1, &'static [u8]) {
    use crate::engine_profile::{ArtifactManifestV1, BuildProvenance, EngineProfileV1};
    let profile = EngineProfileV1::native();
    let engine = wasmtime::Engine::new(&profile.config().unwrap()).unwrap();
    // Immutable test-only build artifact; no phone/raw-kernel loading path.
    let source = b"\0asm\x0d\0\x01\0";
    let component = wasmtime::component::Component::from_binary(&engine, source).unwrap();
    let artifact: &'static [u8] = Box::leak(component.serialize().unwrap().into_boxed_slice());
    let manifest = ArtifactManifestV1::new(
        profile,
        &engine,
        source,
        artifact,
        BuildProvenance {
            anyrt_revision: revision.into(),
            rust_version: "test".into(),
            ndk_version: "none".into(),
            jni_protocol_version: None,
            any_version_pin: "v0.3.5".into(),
            any_versions_tested: vec![],
        },
    );
    (manifest, artifact)
}

#[cfg(feature = "compiler")]
fn owner_options(cfg: &crate::Config, fixture: PreseededFixture, revision: &str) -> ManagedOptions {
    use super::super::{ManagedTimeContext, TrustedArtifact};
    let (manifest, artifact) = synthetic(revision);
    let mut fixture = fixture;
    fixture.frozen.kernel_source_sha256 = manifest.source_sha256.clone();
    fixture.frozen.profile_sha256 = manifest.profile_sha256.clone();
    ManagedOptions::android_poc(
        &cfg.addr,
        unsafe { TrustedArtifact::from_build(artifact, manifest) },
        ManagedTimeContext::new("Europe/Berlin", 3_600, None, None).unwrap(),
    )
    .unwrap()
    .with_preseeded_fixture(fixture)
    .unwrap()
}

#[cfg(feature = "compiler")]
#[test]
fn trusted_synthetic_artifact_and_preparation_run_under_the_real_owner_without_opening() {
    use super::super::{start_with, CancelToken, DrainOutcome, ManagedPhase};
    let dir = tempfile::tempdir().unwrap();
    let (cfg, fixture) = fixture(dir.path());
    let options = owner_options(&cfg, fixture, "synthetic-test");
    let (transport, log) = script(Script::default());
    let mut agent = start_with(CancelToken::new(), move |ctx| {
        prepare_with(cfg, options, ctx, Box::new(transport), public)
    });
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let health = agent.health();
        assert!(health.failures.is_empty(), "{:?}", health.failures);
        if health.preparation_status == PreparationStatus::Prepared {
            break;
        }
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(2));
    }
    assert_eq!(agent.health().phase, ManagedPhase::Preparing);
    assert!(!agent.health().admission_open);
    assert_eq!(agent.health().live_stores, 0);
    assert!(!agent.health().time_context_stale);
    assert_eq!(log.lock().unwrap().len(), 20);
    // The prepared resources live in the owner while it idles, and leave with its task.
    let service = agent.prepared_service().expect("prepared service retained");
    assert_eq!(service.readiness.programs.len(), 1);
    assert_eq!(service.readiness.tiers.len(), 3);
    drop(service);
    assert_eq!(agent.drain_until(deadline), DrainOutcome::Drained);
    assert!(agent.health().all_threads_joined);
    assert!(agent.prepared_service().is_none());
}

#[cfg(feature = "compiler")]
#[test]
fn an_unsynced_prerequisite_is_typed_needs_setup_once_with_no_native_retry() {
    use super::super::{start_with, CancelToken, DrainOutcome, ManagedPhase};
    let dir = tempfile::tempdir().unwrap();
    let (cfg, fixture) = fixture(dir.path());
    let options = owner_options(&cfg, fixture, "synthetic-test");
    let (transport, log) = script(Script {
        source_status: 500,
        ..Default::default()
    });
    let mut agent = start_with(CancelToken::new(), move |ctx| {
        prepare_with(cfg, options, ctx, Box::new(transport), public)
    });
    let deadline = Instant::now() + Duration::from_secs(5);
    while agent.health().preparation_status != NOT_SYNCED_SETUP {
        assert!(agent.health().failures.is_empty());
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(2));
    }
    assert_eq!(agent.health().phase, ManagedPhase::Preparing);
    assert!(agent.prepared_service().is_none());
    // no second attempt: the request count stays where the failed read left it
    let reads = log.lock().unwrap().len();
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(log.lock().unwrap().len(), reads);
    assert_eq!(agent.drain_until(deadline), DrainOutcome::Drained);
}

#[cfg(feature = "compiler")]
#[test]
fn the_service_slot_is_released_on_every_task_exit_including_panic() {
    use super::super::{start_with, CancelToken, DrainOutcome, ManagedFailure, ManagedPhase};
    let dir = tempfile::tempdir().unwrap();
    let (cfg, fixture) = fixture(dir.path());
    let options = owner_options(&cfg, fixture.clone(), "synthetic-test");
    let (transport, _) = script(Script::default());
    let mut agent = start_with(CancelToken::new(), move |ctx| {
        let _slot = ServiceSlot(ctx.shared.clone());
        let cage = unsafe {
            crate::runner::Cage::from_trusted_artifact_managed(
                options.artifact().bytes(),
                options.artifact().manifest(),
            )
        }
        .unwrap();
        let (c, gate) = client(&cfg, &ctx.shared, Box::new(transport), fixture.objects());
        let prepared = prepare_data(&cfg, &fixture, &c, &ctx.shared.cancel, public).unwrap();
        let service = PreparedService::new(&cfg, &options, &fixture, cage, c, gate, prepared);
        *ctx.shared.service.lock().unwrap() = Some(Arc::new(service));
        panic!("after the slot was filled");
    });
    let deadline = Instant::now() + Duration::from_secs(5);
    while agent.health().phase != ManagedPhase::Failed {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(2));
    }
    assert_eq!(
        agent.drain_until(deadline),
        DrainOutcome::Failed(vec![ManagedFailure::PreparationPanicked])
    );
    assert!(agent.prepared_service().is_none());
    assert!(agent.readiness().is_none());
}

#[cfg(feature = "compiler")]
#[test]
fn incompatible_protocol_pins_fail_before_any_http() {
    use super::super::{start_with, CancelToken, DrainOutcome};
    let dir = tempfile::tempdir().unwrap();
    let (cfg, fixture) = fixture(dir.path());
    let options = owner_options(&cfg, fixture, "another-runtime");
    let (transport, log) = script(Script::default());
    let mut agent = start_with(CancelToken::new(), move |ctx| {
        prepare_with(cfg, options, ctx, Box::new(transport), public)
    });
    let deadline = Instant::now() + Duration::from_secs(5);
    while agent.health().preparation_status != PreparationStatus::UnsupportedCapability {
        assert!(agent.health().failures.is_empty());
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(2));
    }
    assert!(log.lock().unwrap().is_empty());
    assert!(agent.prepared_service().is_none());
    assert_eq!(agent.drain_until(deadline), DrainOutcome::Drained);
}
