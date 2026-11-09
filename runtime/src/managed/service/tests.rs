use super::*;
#[cfg(feature = "compiler")]
use crate::anyapi::{AnyError, Transport};
#[cfg(feature = "compiler")]
use crate::managed::preparation::PreparedData;
#[cfg(feature = "compiler")]
use crate::managed::transport::PhaseGate;
#[cfg(feature = "compiler")]
use crate::managed::{FrozenPins, FrozenProgram, StoreAddress, TrustedArtifact};
#[cfg(feature = "compiler")]
use crate::testutil::CallLog;
#[cfg(feature = "compiler")]
use std::sync::Mutex;

/// Replies by path suffix (the first match wins), `{}` otherwise; FIFO would let
/// the trace store's own writes consume a reply meant for a later read.
#[cfg(feature = "compiler")]
struct RoutedTransport {
    calls: CallLog,
    replies: Mutex<Vec<(&'static str, Value)>>,
}
#[cfg(feature = "compiler")]
impl Transport for RoutedTransport {
    fn send(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
    ) -> Result<(u16, Value), AnyError> {
        self.calls
            .lock()
            .unwrap()
            .push((method.into(), path.into(), body.cloned()));
        let replies = self.replies.lock().unwrap();
        let reply = replies
            .iter()
            .find(|(suffix, _)| path.ends_with(suffix))
            .map(|(_, reply)| reply.clone())
            .unwrap_or_else(|| json!({}));
        Ok((200, reply))
    }
    fn open_stream(
        &self,
        _: &str,
        _: Option<&Value>,
    ) -> Result<Box<dyn Iterator<Item = String>>, AnyError> {
        Err(AnyError {
            status: 0,
            code: "stream".into(),
            message: "no streams in this fake".into(),
        })
    }
}

#[cfg(feature = "compiler")]
fn synthetic_cage() -> Arc<Cage> {
    let engine = wasmtime::Engine::new(
        &crate::engine_profile::EngineProfileV1::native()
            .config()
            .unwrap(),
    )
    .unwrap();
    let component =
        wasmtime::component::Component::from_binary(&engine, b"\0asm\x0d\0\x01\0").unwrap();
    let artifact: &'static [u8] = Box::leak(component.serialize().unwrap().into_boxed_slice());
    let manifest = crate::engine_profile::ArtifactManifestV1::new(
        crate::engine_profile::EngineProfileV1::native(),
        &engine,
        b"\0asm\x0d\0\x01\0",
        artifact,
        crate::engine_profile::BuildProvenance {
            anyrt_revision: "synthetic-test".into(),
            rust_version: "test".into(),
            ndk_version: "none".into(),
            jni_protocol_version: None,
            any_version_pin: "v0.3.5".into(),
            any_versions_tested: vec![],
        },
    );
    unsafe { Cage::from_trusted_artifact_managed(artifact, &manifest) }.unwrap()
}

#[cfg(feature = "compiler")]
fn service(replies: Vec<(&'static str, Value)>) -> (PreparedService, CallLog, ManagedTimeContext) {
    service_with(replies, synthetic_cage(), Default::default())
}

#[cfg(feature = "compiler")]
fn service_with(
    replies: Vec<(&'static str, Value)>,
    cage: Arc<Cage>,
    content: crate::managed::frozen::FrozenContent,
) -> (PreparedService, CallLog, ManagedTimeContext) {
    let log: CallLog = Default::default();
    let transport = RoutedTransport {
        calls: log.clone(),
        replies: Mutex::new(replies),
    };
    let client = Arc::new(Client::with_transport(Box::new(transport)));
    let dir = tempfile::tempdir().unwrap();
    let cfg = crate::Config {
        addr: "http://127.0.0.1:7001".into(),
        agent_space: "fixture".into(),
        traces_dir: dir.path().join("traces"),
        ..Default::default()
    };
    let store = |name: &str| StoreAddress {
        object_id: name.into(),
        collection: format!("type_{name}"),
    };
    let fixture = PreseededFixture {
        account_root: dir.path().into(),
        account_id: "account".into(),
        self_peer_id: "peer".into(),
        bao_root: "bao-root".into(),
        chat: "chat".into(),
        config: store("config"),
        secrets: store("secrets"),
        triggers: store("triggers"),
        runs: store("runs"),
        turns: store("turns"),
        frozen: FrozenPins {
            source_revision: "repos-test".into(),
            anyrt_revision: "synthetic-test".into(),
            trace_schema: crate::runner::TRACE_SCHEMA,
            kernel_source_sha256: "0".repeat(64),
            profile_sha256: "0".repeat(64),
            overlays: vec!["agent".into()],
            programs: vec![FrozenProgram {
                alias: "agent".into(),
                name: "llm".into(),
                version: "v1".into(),
                fingerprint: "0".repeat(64),
                credentials_sha256: "0".repeat(64),
                manifest: false,
            }],
            skills: vec![],
        },
    };
    let zone = ManagedTimeContext::new("Europe/Berlin", 3_600, None, None).unwrap();
    let manifest = crate::engine_profile::ArtifactManifestV1 {
        schema_version: 1,
        wasmtime_version: crate::engine_profile::WASMTIME_VERSION.into(),
        profile: crate::engine_profile::EngineProfileV1::android(),
        resolved_profile: Value::Null,
        profile_sha256: String::new(),
        source_sha256: String::new(),
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
    let options = ManagedOptions::android_poc(
        &cfg.addr,
        unsafe { TrustedArtifact::from_build(b"unused", manifest) },
        zone.clone(),
    )
    .unwrap()
    .with_preseeded_fixture(fixture.clone())
    .unwrap();
    let blobs = crate::blob::BlobDir::create(&cfg.traces_dir).unwrap();
    let traces =
        AnyTraceStore::new(client.clone(), "fixture", Some("peer".into()), Some(blobs)).unwrap();
    let mut frozen = content;
    frozen.aliases.insert("agent".into(), "fixture".into());
    let prepared = PreparedData {
        traces,
        declared: crate::managed::frozen::FrozenDeclared::new(Default::default()),
        readiness: Readiness::default(),
        frozen,
    };
    let service = PreparedService::new(
        &cfg,
        &options,
        &fixture,
        cage,
        client,
        PhaseGate::default(),
        prepared,
    );
    std::mem::forget(dir);
    (service, log, zone)
}

#[test]
fn zone_samples_are_validated_and_updates_reach_later_reads() {
    let future = 4.0e9;
    assert!(ManagedTimeContext::new("", 0, None, None).is_err());
    assert!(ManagedTimeContext::new("Europe/Berlin", 19 * 3_600, None, None).is_err());
    assert!(ManagedTimeContext::new("Europe/Berlin;rm", 0, None, None).is_err());
    assert!(ManagedTimeContext::new("Europe/Berlin", 0, None, Some(0.0)).is_err());
    // a validity end already in the past is never accepted; an inverted window neither
    assert!(ManagedTimeContext::new("Europe/Berlin", 0, None, Some(1.0)).is_err());
    assert!(ManagedTimeContext::new("Europe/Berlin", 0, Some(future), Some(future - 1.0)).is_err());
    // custom ids as Android reports them are valid (N1-R5)
    assert!(ManagedTimeContext::new("GMT+05:30", 19_800, None, None).is_ok());
    let zone = ManagedTimeContext::new("Europe/Berlin", 3_600, None, None).unwrap();
    let shared = zone.clone();
    shared
        .update("Asia/Kolkata", 19_800, Some(1.0), Some(future))
        .unwrap();
    assert_eq!(
        zone.sample(),
        Some(ZoneSample {
            zone_id: "Asia/Kolkata".into(),
            offset_s: 19_800,
            valid_from_s: Some(1.0),
            valid_until_s: Some(future),
        })
    );
    assert!(!zone.is_stale());
    let now = zone.now().unwrap();
    assert_eq!(now["tz"], "Asia/Kolkata");
    assert_eq!(now["offset_s"], 19_800);
    assert!(now["epoch"].as_f64().unwrap() > 1.0e9);
}

#[test]
fn a_not_yet_valid_absent_or_failed_zone_sample_is_a_typed_refusal_never_a_stale_offset() {
    // the wall clock was set back before the sample's window: stale by the sample itself
    let zone = ManagedTimeContext::new("Europe/Berlin", 7_200, Some(4.0e9), None).unwrap();
    assert!(zone.is_stale());
    assert_eq!(zone.now(), Err(TimeContextStale));
    // a valid refresh clears it; a failed update discards the sample rather than keeping it
    zone.update("Europe/Berlin", 3_600, None, Some(4.0e9))
        .unwrap();
    assert!(!zone.is_stale());
    assert!(zone.now().is_ok());
    assert!(zone
        .update("Europe/Berlin", 99 * 3_600, None, None)
        .is_err());
    assert_eq!(zone.sample(), None);
    assert!(zone.is_stale());
    assert_eq!(zone.now(), Err(TimeContextStale));
}

/// N2a-r2 I-9: the validity window is one word, rounded to fail closed, and
/// `health()`'s staleness and `time.now` apply the same rule to it.
#[test]
fn the_zone_window_is_one_word_and_health_and_time_now_agree() {
    let sample = |from: Option<f64>, until: Option<f64>| ZoneSample {
        zone_id: "UTC".into(),
        offset_s: 0,
        valid_from_s: from,
        valid_until_s: until,
    };
    assert_eq!(pack_window(None), NO_SAMPLE);
    assert!(!window_valid(NO_SAMPLE, 1.0e9));
    let word = pack_window(Some(&sample(Some(10.2), Some(20.7))));
    assert_eq!((word >> 32, word & u64::from(u32::MAX)), (11, 20));
    assert!(!window_valid(word, 10.9), "from rounds up");
    assert!(window_valid(word, 11.0));
    assert!(window_valid(word, 19.99));
    assert!(!window_valid(word, 20.0), "until rounds down");
    assert!(window_valid(pack_window(Some(&sample(None, None))), 1.0e9));
    // ISO-2: a bound above u32::MAX seconds is unrepresentable, never clamped:
    // the sample is always stale — before its window, inside it, after it
    let past_u32 = f64::from(u32::MAX);
    for word in [
        pack_window(Some(&sample(
            Some(past_u32 + 705.0),
            Some(past_u32 + 1_705.0),
        ))),
        pack_window(Some(&sample(None, Some(past_u32 + 1.0)))),
    ] {
        assert_eq!(word, NO_SAMPLE);
        for now in [past_u32 - 0.5, past_u32 + 1_000.0, 1.0e9] {
            assert!(!window_valid(word, now), "{now}");
        }
    }
    // the largest representable bounds still pack
    let edge = pack_window(Some(&sample(Some(past_u32 - 1.0), Some(past_u32))));
    assert_eq!(
        (edge >> 32, edge & u64::from(u32::MAX)),
        (u64::from(u32::MAX - 1), u64::from(u32::MAX))
    );
    let now = epoch_s();
    for (from, until) in [
        (None, Some(now + 1_000.0)),
        (Some(now + 1_000.0), None),
        (Some(now - 1_000.0), Some(now + 1_000.0)),
    ] {
        let zone = ManagedTimeContext::new("UTC", 0, from, until).unwrap();
        assert_eq!(zone.is_stale(), zone.now().is_err(), "{from:?} {until:?}");
    }
}

#[cfg(feature = "compiler")]
#[test]
fn factory_installs_egress_first_and_refuses_consent_grants_and_shell_without_any_request() {
    let (service, log, _) = service(vec![]);
    let before = log.lock().unwrap().len();
    let mut broker = service.broker("agent:toolcaller@v1", "run_test");
    for effect in [
        "oauth.connect",
        "oauth.refresh",
        "oauth.status",
        "sh.run",
        "fs.read",
    ] {
        let error = broker
            .call(
                effect,
                json!({"provider": "google", "cmd": "id", "path": "/"}),
            )
            .unwrap_err();
        assert_eq!(error.type_, "UnsupportedCapability", "{effect}");
    }
    assert!(broker.oauth.is_none());
    let error = broker
        .call(
            "http.get",
            json!({"url": "http://10.0.0.1:7001/v1/spaces/fixture"}),
        )
        .unwrap_err();
    assert_eq!(error.type_, "egress_denied");
    let error = broker
        .call(
            "http.post",
            json!({"url": "http://127.0.0.1:7001/v1/devices/me", "json": {}}),
        )
        .unwrap_err();
    assert_eq!(error.type_, "egress_denied");
    // The only traffic a refused effect leaves is the trace sink's own device-local
    // record writes; no space, registry, auth, consent or provider request.
    assert!(log.lock().unwrap()[before..]
        .iter()
        .all(|(method, path, _)| method == "POST" && path.starts_with("/v1/local/")));
}

#[cfg(feature = "compiler")]
#[test]
fn time_now_reports_the_embedder_zone_and_never_touches_tz_or_home() {
    let tz = std::env::var_os("TZ");
    let home = std::env::var_os("HOME");
    let (service, _, zone) = service(vec![]);
    let mut broker = service.broker("agent:toolcaller@v1", "run_test");
    let now = broker.call("time.now", json!({})).unwrap();
    assert_eq!(now["tz"], "Europe/Berlin");
    assert_eq!(now["offset_s"], 3_600);
    zone.update("UTC", 0, None, None).unwrap();
    let later = broker.call("time.now", json!({})).unwrap();
    assert_eq!(later["tz"], "UTC");
    assert_eq!(later["offset_s"], 0);
    zone.update("UTC", 0, Some(4.0e9), None).unwrap();
    let error = broker.call("time.now", json!({})).unwrap_err();
    assert_eq!(error.type_, "TimeContextStale");
    assert_eq!(std::env::var_os("TZ"), tz);
    assert_eq!(std::env::var_os("HOME"), home);
}

/// ADR-034 §2 / N2a-r2 item 10 / isolation N-1: an agent-authored program on
/// the frozen managed resolver is the guest's ordinary `KeyError`, listed in
/// the run's `refused_programs` (never `missing_programs`, which would re-run
/// the message); the lanes add it to `health().refused_programs`.
#[cfg(feature = "compiler")]
#[test]
fn an_unpinned_program_is_a_key_error_listed_as_refused_never_as_missing() {
    let (service, log, _) = service(vec![]);
    let mut broker = service.broker("agent:toolcaller@v1", "run_test");
    let before = log.lock().unwrap().len();
    let error = broker
        .call("module.resolve", json!({"spec": "agent:mine@v1"}))
        .unwrap_err();
    assert_eq!(error.type_, "KeyError");
    broker
        .call("module.resolve", json!({"spec": "agent:mine@v1"}))
        .unwrap_err();
    assert_eq!(
        broker.refused_programs,
        vec![("fixture".to_string(), "mine@v1".to_string())],
        "listed once"
    );
    assert!(broker.missing_programs.is_empty());
    // the only traffic is the trace sink's device-local record writes
    assert!(log.lock().unwrap()[before..]
        .iter()
        .all(|(_, path, _)| path.starts_with("/v1/local/")));
}

#[cfg(feature = "compiler")]
#[test]
fn factory_wires_the_shared_stores_space_resolver_guards_and_runtime_keys() {
    let (service, log, _) = service(vec![(
        "/v1/spaces/fixture/query",
        json!({"records": [{"key": "llm.tier.codegen", "value": {"provider": "anthropic"}}]}),
    )]);
    let mut broker = service.broker("agent:toolcaller@v1", "run_test");
    assert_eq!(broker.secrets_guard.as_deref(), Some("secrets"));
    assert_eq!(broker.secrets_collection.as_deref(), Some("type_secrets"));
    assert!(broker.programs_dir.is_none());
    assert!(broker.resolver.is_some());
    let before = log.lock().unwrap().len();
    let resolver = broker.resolver.as_mut().unwrap();
    assert!(resolver.resolve("agent:llm@v1", None).is_err());
    assert!(resolver.resolve("fixture:llm@v1", None).is_err());
    assert_eq!(
        log.lock().unwrap().len(),
        before,
        "managed use() makes no HTTP"
    );
    assert!(broker.trace_store.is_some());
    assert!(broker.secret_store.is_some());
    assert!(broker.config_store.is_some());
    assert!(broker.declared.is_some());
    assert!(broker.grants.is_none());
    assert!(broker.secrets.is_empty());
    assert!(broker.config.is_empty());
    assert_eq!(broker.runtime["any.base_url"], "http://127.0.0.1:7001");
    assert_eq!(broker.runtime["bao.space"], "fixture");
    assert_eq!(broker.runtime["overlays.aliases"]["agent"], "fixture");
    let value = broker
        .call("config.get", json!({"key": "llm.tier.codegen"}))
        .unwrap();
    assert_eq!(value["value"]["provider"], "anthropic");
    let last = log.lock().unwrap().last().cloned().unwrap();
    assert_eq!(last.0, "POST");
    assert_eq!(last.1, "/v1/spaces/fixture/query");
    assert_eq!(last.2.as_ref().unwrap()["objectId"], "config");
}

#[cfg(feature = "desktop")]
#[test]
fn the_real_lane_runner_runs_a_pinned_program_under_the_permit_and_lands_its_trace() {
    use crate::managed::admission::{Admission, Lane, LanePool};
    use crate::managed::frozen::{FrozenContent, VerifiedProgram};
    use crate::managed::lanes::{LaneJob, LaneRunner};
    use std::sync::atomic::AtomicBool;
    let mut content = FrozenContent::default();
    content.programs.insert(
        ("fixture".into(), "smoke".into(), "v1".into()),
        VerifiedProgram {
            object_id: "obj-smoke".into(),
            code: "def main(args):\n    return 7\n".into(),
            marker: 1,
        },
    );
    let (service, log, _) = service_with(vec![], Cage::embedded().unwrap(), content);
    let pool = LanePool::opened();
    let permit = match pool.acquire(Lane::Scheduled) {
        Admission::Admitted(p) => p,
        Admission::CapacityDeferred => unreachable!(),
    };
    let job = LaneJob {
        lane: Lane::Scheduled,
        work_id: "smoke".into(),
        run_id: "run_smoke".into(),
        spec: "agent:smoke@v1".into(),
        args: json!({}),
        trigger: Some("heartbeat".into()),
        mailbox: Default::default(),
        interrupt: Arc::new(AtomicBool::new(false)),
    };
    let before = log.lock().unwrap().len();
    let outcome = service.run(&job, Default::default(), permit);
    let result = outcome.result.expect("the run path succeeded");
    assert_eq!(result.status, "ok");
    assert_eq!(result.trace_ref.as_deref(), Some("run_smoke"));
    assert!(
        outcome.summary.is_some(),
        "the trace landed in the device-local store"
    );
    assert_eq!(pool.live_stores(), 0, "the permit dropped with the store");
    assert_eq!(pool.occupied_lanes(), 0);
    // module resolution made no HTTP; the only traffic is the trace store's local writes
    assert!(log.lock().unwrap()[before..]
        .iter()
        .all(|(method, path, _)| method == "POST" && path.starts_with("/v1/local/")));
}
