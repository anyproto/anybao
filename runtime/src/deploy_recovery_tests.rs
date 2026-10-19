//! Frozen publication recovery fault tests (ADR-009 §2).
use crate::anyapi::{AnyError, Client, Transport};
use crate::deploy::{Deployer, FrozenVersionError, ProgramSource};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

const SPACE: &str = "recovery";
const TYPE: &str = "program-type";
const SOURCE: &str = "program-source";
const MANIFEST: &str = "program-manifest";
const MARKER: &str = "p_deployment_fingerprint";
const KEYS: [&str; 6] = [
    "name",
    "version",
    "summary",
    "any_tool",
    "credentials",
    "deployment_fingerprint",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Stage {
    Create,
    Source,
    Manifest,
    Publish,
}
#[derive(Clone, Copy, Debug)]
enum Fault {
    Before(Stage),
    After(Stage),
    Rejection(Stage),
    EditCode(Stage),
    EditManifest(Stage),
    EditMetadata(Stage),
    DeleteSource(Stage),
}
#[derive(Default)]
struct State {
    object: Option<Value>,
    records: BTreeMap<String, Value>,
    seq: u64,
    creates: usize,
    committed: Vec<Stage>,
    fault: Option<Fault>,
    legacy_server: bool,
}
impl State {
    fn stamp(&mut self) -> u64 {
        self.seq += 1;
        if let Some(object) = &mut self.object {
            object["_applySeq"] = json!(self.seq);
        }
        self.seq
    }
    fn edit_code(&mut self, code: &str) {
        let seq = self.stamp();
        self.records.insert(
            SOURCE.into(),
            json!({"id":"main", "code":code, "_applySeq":seq}),
        );
    }
    fn tombstone_source(&mut self) {
        let seq = self.stamp();
        self.records.insert(
            SOURCE.into(),
            json!({"id":"main", "_deletedAt": {"$date": "2026-10-19T00:00:00Z"}, "_applySeq":seq}),
        );
    }
}
#[derive(Clone, Default)]
struct Harness(Arc<Mutex<State>>);
impl Harness {
    fn client(&self) -> Client {
        Client::with_transport(Box::new(self.clone()))
    }
    fn fault(&self, fault: Fault) {
        self.0.lock().unwrap().fault = Some(fault);
    }
    fn snapshot(&self) -> (Option<Value>, BTreeMap<String, Value>, usize) {
        let s = self.0.lock().unwrap();
        (s.object.clone(), s.records.clone(), s.creates)
    }
    fn resume(&self, source: &ProgramSource) -> anyhow::Result<&'static str> {
        Deployer::new(&self.client(), SPACE)
            .frozen(true)
            .deploy_one(source)
    }
}
fn unavailable() -> AnyError {
    AnyError {
        status: 503,
        code: "test.interrupted".into(),
        message: "controlled response loss or disconnect".into(),
    }
}
fn error(status: u16, code: &str) -> (u16, Value) {
    (status, json!({"error":{"code":code,"message":code}}))
}
fn stage(path: &str, body: &Value) -> Option<Stage> {
    if path.ends_with("/objects") {
        Some(Stage::Create)
    } else if path.ends_with("/modify") {
        match body["dataset"].as_str() {
            Some(SOURCE) => Some(Stage::Source),
            Some(MANIFEST) => Some(Stage::Manifest),
            Some("objects") => Some(Stage::Publish),
            _ => None,
        }
    } else if path.contains("/set/") {
        Some(Stage::Publish)
    } else {
        None
    }
}
fn set_path(value: &mut Value, path: &str, replacement: Option<&Value>) {
    let (head, tail) = path.split_once('.').unwrap_or((path, ""));
    if !tail.is_empty() {
        if value.get(head).is_none() {
            value[head] = json!({});
        }
        set_path(&mut value[head], tail, replacement);
    } else if let Some(replacement) = replacement {
        value[head] = replacement.clone();
    } else if let Some(object) = value.as_object_mut() {
        object.remove(head);
    }
}
fn apply_ops(value: &mut Value, ops: &Value) {
    for op in ops.as_array().expect("ops array") {
        let path = op["path"].as_str().unwrap_or("");
        match op["type"].as_str() {
            Some("$set") if path.is_empty() => {
                for (key, v) in op["value"].as_object().expect("multi-field set") {
                    set_path(value, key, Some(v));
                }
            }
            Some("$set") => set_path(value, path, Some(&op["value"])),
            Some("$unset") => set_path(value, path, None),
            other => panic!("unsupported recovery op {other:?}"),
        }
    }
}
impl Transport for Harness {
    fn send(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
    ) -> Result<(u16, Value), AnyError> {
        let body = body.cloned().unwrap_or(json!({}));
        let mut s = self.0.lock().unwrap();
        let current = stage(path, &body);
        let matches_stage = |target| current == Some(target);
        let mut lose_response = false;
        if let Some(fault) = s.fault {
            let target = match fault {
                Fault::Before(t)
                | Fault::After(t)
                | Fault::Rejection(t)
                | Fault::EditCode(t)
                | Fault::EditManifest(t)
                | Fault::EditMetadata(t)
                | Fault::DeleteSource(t) => t,
            };
            if matches_stage(target) {
                s.fault = None;
                match fault {
                    Fault::Before(_) => return Err(unavailable()),
                    Fault::After(_) => lose_response = true,
                    Fault::Rejection(_) => {
                        return Ok((
                            200,
                            json!({"rejections":[{"recordId":"main","recordIndex":0,"opIndex":0,"reason":"controlled handler rejection"}]}),
                        ));
                    }
                    Fault::EditCode(_) => s.edit_code("# user edit must survive\n"),
                    Fault::EditManifest(_) => {
                        let seq = s.stamp();
                        s.records.insert(MANIFEST.into(), json!({"id":"main", "manifest":{"capabilities":["user.changed"]}, "_applySeq":seq}));
                    }
                    Fault::EditMetadata(_) => {
                        s.object.as_mut().unwrap()[TYPE]["p_summary"] =
                            json!("User-authored summary");
                        s.stamp();
                    }
                    Fault::DeleteSource(_) => s.tombstone_source(),
                }
            }
        }
        let reply = match (method, path) {
            ("GET", "/v1/health") => (
                200,
                if s.legacy_server {
                    json!({})
                } else {
                    json!({"capabilities": ["conditional-writes"]})
                },
            ),
            ("GET", "/v1/spaces/recovery/types?includeHidden=true") => (
                200,
                json!({"types":[{"id":TYPE,"xKey":"program","hidden":false}]}),
            ),
            ("GET", "/v1/spaces/recovery/types/program-type/properties") => (
                200,
                json!({"properties":KEYS.map(|key|json!({"id":format!("p_{key}"),"xKey":key}))}),
            ),
            ("GET", "/v1/spaces/recovery/types/program-type/datasets") => (
                200,
                json!({"datasets":[{"key":"editor_blocks","collection":"editor_blocks"},{"key":"program_source","collection":SOURCE},{"key":"program_manifest","collection":MANIFEST}]}),
            ),
            ("POST", "/v1/spaces/recovery/objects/query") => {
                (200, json!({"records":s.object.iter().collect::<Vec<_>>()}))
            }
            ("GET", "/v1/spaces/recovery/objects/program")
            | ("GET", "/v1/spaces/recovery/properties/program") => {
                (200, json!({"record":s.object}))
            }
            ("POST", "/v1/spaces/recovery/objects") => {
                assert!(
                    s.object.is_none(),
                    "retry must not create a duplicate program"
                );
                s.creates += 1;
                let mut object = body["initialProperties"].clone();
                object["id"] = json!("program");
                object["any"]["type"] = json!(TYPE);
                s.object = Some(object);
                s.stamp();
                s.committed.push(Stage::Create);
                (201, json!({"objectId":"program"}))
            }
            ("POST", "/v1/spaces/recovery/query") => {
                assert_eq!(body["objectId"], "program");
                let dataset = body["dataset"].as_str().unwrap();
                let rows = s
                    .records
                    .get(dataset)
                    .filter(|r| body["includeDeleted"] == true || r.get("_deletedAt").is_none())
                    .into_iter()
                    .collect::<Vec<_>>();
                (200, json!({"records":rows}))
            }
            ("POST", "/v1/spaces/recovery/modify") => {
                assert_eq!(body["objectId"], "program");
                let dataset = body["dataset"].as_str().unwrap();
                let object_seq = s.object.as_ref().unwrap()["_applySeq"].as_u64().unwrap();
                let dataset_seq = if dataset == "objects" {
                    object_seq
                } else {
                    s.records
                        .get(dataset)
                        .and_then(|r| r["_applySeq"].as_u64())
                        .unwrap_or(0)
                };
                if body["ifUnchangedSince"]
                    .as_u64()
                    .is_some_and(|expected| dataset_seq > expected)
                    || body["ifObjectUnchangedSince"]
                        .as_u64()
                        .is_some_and(|expected| object_seq > expected)
                {
                    return Ok(error(409, "write.precondition_failed"));
                }
                if dataset != "objects"
                    && s.records
                        .get(dataset)
                        .is_some_and(|r| r.get("_deletedAt").is_some())
                {
                    return Ok((
                        200,
                        json!({"rejections":[{"recordId":"main","recordIndex":0,"opIndex":-1,"reason":"record deleted"}]}),
                    ));
                }
                let records = body["records"].as_array().unwrap();
                assert_eq!(records.len(), 1);
                if dataset == "objects" {
                    apply_ops(s.object.as_mut().unwrap(), &records[0]["ops"]);
                    s.stamp();
                } else {
                    let mut value = s
                        .records
                        .get(dataset)
                        .cloned()
                        .unwrap_or(json!({"id":"main"}));
                    apply_ops(&mut value, &records[0]["ops"]);
                    value["_applySeq"] = json!(s.stamp());
                    s.records.insert(dataset.into(), value);
                }
                s.committed.push(current.unwrap());
                (
                    200,
                    json!({"recordIds":[records[0]["id"]],"applySeq":s.seq,"rejections":[]}),
                )
            }
            ("POST", "/v1/spaces/recovery/properties/program/set/program-type") => {
                for (key, value) in body["patch"].as_object().unwrap() {
                    s.object.as_mut().unwrap()[TYPE][key] = value.clone();
                }
                s.stamp();
                s.committed.push(Stage::Publish);
                (200, json!({"ok":true}))
            }
            _ => panic!("unexpected recovery request {method} {path}: {body}"),
        };
        if lose_response {
            Err(unavailable())
        } else {
            Ok(reply)
        }
    }
    fn open_stream(
        &self,
        _: &str,
        _: Option<&Value>,
    ) -> Result<Box<dyn Iterator<Item = String>>, AnyError> {
        panic!("deploy never streams")
    }
}
fn source() -> ProgramSource {
    ProgramSource::new(
        "browser",
        "v4",
        "\"\"\"Browser bridge.\"\"\"\n__any_tool__ = True\n@span(\"main\")\ndef main(args):\n    return 1\n",
    )
    .with_manifest(json!({"capabilities":["net.http"]}))
}
fn assert_complete(h: &Harness, p: &ProgramSource) {
    let (object, records, creates) = h.snapshot();
    assert_eq!(creates, 1);
    assert_eq!(records[SOURCE]["code"], p.code);
    assert_eq!(records[MANIFEST]["manifest"], p.manifest);
    assert_eq!(object.as_ref().unwrap()[TYPE]["p_any_tool"], p.any_tool());
    assert!(
        object.unwrap()[TYPE][MARKER]
            .as_str()
            .unwrap_or("")
            .is_empty(),
        "publication clears the pending marker"
    );
    assert_eq!(h.resume(p).unwrap(), "unchanged");
}

#[test]
fn interrupted_frozen_install_resumes_without_duplicate_or_rewrite() {
    for stage in [Stage::Source, Stage::Manifest, Stage::Publish] {
        let h = Harness::default();
        let p = source();
        h.fault(Fault::Before(stage));
        assert!(h.resume(&p).is_err(), "fault at {stage:?} must interrupt");
        assert!(
            h.snapshot().0.unwrap()[TYPE][MARKER]
                .as_str()
                .is_some_and(|s| !s.is_empty()),
            "new frozen installs carry durable ownership before dataset writes"
        );
        h.resume(&p)
            .unwrap_or_else(|error| panic!("resume after {stage:?}: {error:#}"));
        assert_complete(&h, &p);
        let state = h.0.lock().unwrap();
        assert_eq!(
            state
                .committed
                .iter()
                .filter(|s| **s == Stage::Source)
                .count(),
            1,
            "source never rewritten"
        );
        assert_eq!(
            state
                .committed
                .iter()
                .filter(|s| **s == Stage::Manifest)
                .count(),
            1,
            "manifest never rewritten"
        );
    }
}
#[test]
fn lost_success_responses_are_recovered_idempotently() {
    for stage in [
        Stage::Create,
        Stage::Source,
        Stage::Manifest,
        Stage::Publish,
    ] {
        let h = Harness::default();
        let p = source();
        h.fault(Fault::After(stage));
        assert!(h.resume(&p).is_err());
        h.resume(&p)
            .unwrap_or_else(|error| panic!("lost {stage:?} response: {error:#}"));
        assert_complete(&h, &p);
    }
}
#[test]
fn recovery_preserves_user_edits_already_present() {
    for metadata in [false, true] {
        let h = Harness::default();
        let p = source();
        h.fault(Fault::Before(Stage::Manifest));
        assert!(h.resume(&p).is_err());
        {
            let mut state = h.0.lock().unwrap();
            if metadata {
                state.object.as_mut().unwrap()[TYPE]["p_summary"] = json!("User-authored summary");
                state.stamp();
            } else {
                state.edit_code("# user edit must survive\n");
            }
        }
        let before = h.snapshot();
        assert!(h.resume(&p).is_err());
        assert_eq!(h.snapshot(), before, "frozen recovery must preserve edits");
    }
}
#[test]
fn user_edit_between_recovery_read_and_write_wins() {
    for fault in [
        Fault::EditCode(Stage::Source),
        Fault::EditManifest(Stage::Manifest),
        Fault::EditMetadata(Stage::Source),
        Fault::EditCode(Stage::Publish),
        Fault::EditManifest(Stage::Publish),
        Fault::EditMetadata(Stage::Publish),
    ] {
        let h = Harness::default();
        let p = source();
        h.fault(fault);
        assert!(
            h.resume(&p).is_err(),
            "concurrent edit cannot publish: {fault:?}"
        );
        let state = h.0.lock().unwrap();
        match fault {
            Fault::EditCode(_) => {
                assert_eq!(state.records[SOURCE]["code"], "# user edit must survive\n")
            }
            Fault::EditManifest(_) => assert_eq!(
                state.records[MANIFEST]["manifest"],
                json!({"capabilities":["user.changed"]})
            ),
            Fault::EditMetadata(_) => assert_eq!(
                state.object.as_ref().unwrap()[TYPE]["p_summary"],
                "User-authored summary"
            ),
            _ => unreachable!(),
        }
        assert!(!state.committed.contains(&Stage::Publish));
    }
}
#[test]
fn deleted_source_is_never_resurrected() {
    let h = Harness::default();
    let p = source();
    h.fault(Fault::Before(Stage::Source));
    assert!(h.resume(&p).is_err());
    h.0.lock().unwrap().tombstone_source();
    let before = h.snapshot();
    assert!(h.resume(&p).is_err());
    assert_eq!(h.snapshot(), before);
    let h = Harness::default();
    h.fault(Fault::DeleteSource(Stage::Source));
    assert!(h.resume(&p).is_err());
    assert!(h.snapshot().1[SOURCE].get("_deletedAt").is_some());
}
#[test]
fn successful_http_with_rejected_write_is_not_publication() {
    for stage in [Stage::Source, Stage::Manifest, Stage::Publish] {
        let h = Harness::default();
        let p = source();
        h.fault(Fault::Rejection(stage));
        assert!(
            h.resume(&p).is_err(),
            "HTTP 200 with rejected {stage:?} must fail"
        );
        assert!(!h.0.lock().unwrap().committed.contains(&Stage::Publish));
        h.resume(&p).unwrap();
        assert_complete(&h, &p);
    }
}
#[test]
fn unmarked_legacy_partial_object_is_not_adopted() {
    let h = Harness::default();
    let p = source();
    h.fault(Fault::Before(Stage::Source));
    assert!(h.resume(&p).is_err());
    h.0.lock().unwrap().object.as_mut().unwrap()[TYPE]
        .as_object_mut()
        .unwrap()
        .remove(MARKER);
    let before = h.snapshot();
    let error = h.resume(&p).unwrap_err();
    assert!(error.downcast_ref::<FrozenVersionError>().is_some());
    assert_eq!(h.snapshot(), before);
}

#[test]
fn legacy_server_without_guard_capability_cannot_start_frozen_install() {
    let h = Harness::default();
    h.0.lock().unwrap().legacy_server = true;
    assert!(h.resume(&source()).is_err());
    assert_eq!(h.snapshot(), (None, BTreeMap::new(), 0));
}

#[test]
fn existing_manifest_edits_and_deletes_are_preserved() {
    for delete in [false, true] {
        let h = Harness::default();
        let p = source();
        h.fault(Fault::Before(Stage::Publish));
        assert!(h.resume(&p).is_err());
        {
            let mut state = h.0.lock().unwrap();
            let seq = state.stamp();
            let record = if delete {
                json!({"id":"main", "_deletedAt": {"$date":"2026-10-19T00:00:00Z"}, "_applySeq":seq})
            } else {
                json!({"id":"main", "manifest":{"capabilities":["user.changed"]}, "_applySeq":seq})
            };
            state.records.insert(MANIFEST.into(), record);
        }
        let before = h.snapshot();
        assert!(h.resume(&p).is_err());
        assert_eq!(h.snapshot(), before);
    }
}
