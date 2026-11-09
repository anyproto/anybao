use super::*;
use crate::managed::{start_with, CancelToken, DrainOutcome};
use crate::testutil::{CallLog, StubTransport};
use serde_json::json;
use std::collections::BTreeMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

fn objects() -> FixtureObjects {
    FixtureObjects {
        config: ("config".into(), "t_config".into()),
        secrets: ("secrets".into(), "t_secrets".into()),
        triggers: ("triggers".into(), "t_triggers".into()),
        runs: ("runs".into(), "t_runs".into()),
        turns: ("turns".into(), "t_turns".into()),
        chat: "chat".into(),
        self_peer: "peer".into(),
    }
}

/// A gate over a stub that answers every admitted call; the owner only exists
/// so the gate has a live `Shared` to check cancellation against.
fn gated(
    stream: &[&str],
) -> (
    crate::managed::ManagedAgent,
    GatedTransport,
    PhaseGate,
    CallLog,
) {
    let (tx, rx) = std::sync::mpsc::channel();
    let agent = start_with(CancelToken::new(), move |ctx| {
        tx.send(Arc::downgrade(&ctx.shared)).unwrap();
        while !ctx.idle_until(Instant::now() + Duration::from_secs(300)) {}
        Ok(())
    });
    let shared = rx.recv_timeout(Duration::from_secs(5)).unwrap();
    let stub = StubTransport::new();
    let log = stub.log();
    for _ in 0..64 {
        stub.push(200, json!({"records": []}));
    }
    stub.set_stream(stream);
    let gate = PhaseGate::default();
    let transport = GatedTransport::new(
        Box::new(stub),
        shared,
        ["fixture".to_string(), "overlay".to_string()]
            .into_iter()
            .collect(),
        "fixture".into(),
        objects(),
        gate.clone(),
    );
    (agent, transport, gate, log)
}

fn modify(object: &str, dataset: &str, ops: Value) -> Value {
    json!({"objectId": object, "dataset": dataset, "records": [{"id": "r", "upsert": true, "ops": ops}]})
}
fn coll(name: &str) -> Value {
    json!({"coll": {"scope": "space", "spaceId": "fixture", "name": name}})
}
fn subscribe_body() -> Value {
    json!({"objectId": "chat", "dataset": "chat_messages", "sort": ["-createdAt"], "limit": 64})
}

#[test]
fn paths_are_matched_as_whole_canonical_segments_and_ids_may_carry_dots() {
    assert_eq!(
        segments("/v1/spaces/fixture/types?includeHidden=true").unwrap(),
        (
            vec!["v1", "spaces", "fixture", "types"],
            Some("includeHidden=true")
        )
    );
    for bad in [
        "v1/spaces",
        "/v1//spaces",
        "/v1/spaces/fixture/types/x/../../../../debug/pprof",
        "/v1/spaces/fixture/types/./properties",
        "/v1/spaces/fixture/types/%2e%2e/properties",
        "/v1/spaces/fixture/types/a%2fb/properties",
        "/v1/spaces/fixture\\types",
        "/v1/spaces/fixture#x",
        "/v1/spaces/fix\tture",
        "/v1/spaces/fixture\n",
        "/v1/spaces/fixture\r",
        "/v1/spaces/fix ture",
        "/v1/spaces/fixtur\u{e9}",
    ] {
        assert!(segments(bad).is_none(), "{bad:?}");
    }
    // real Any space ids carry a dot: <cid>.<suffix>
    let space = "bafyreigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi.2a1b3c4d";
    assert!(identifier(space));
    assert!(identifier("t-program_1"));
    for bad in ["", ".", "..", ".x", "x.", "a..b", "a/b", "a%2eb", "a b"] {
        assert!(!identifier(bad), "{bad:?}");
    }
    assert_eq!(
        segments(&format!("/v1/spaces/{space}/bundles")).unwrap().0[2],
        space
    );
}

#[test]
fn preparation_phase_permits_verification_reads_and_trace_creates_only() {
    let (mut agent, transport, _, log) = gated(&["event: ready", "data: {}", ""]);
    for (method, path) in [
        ("GET", "/v1/spaces/fixture"),
        ("GET", "/v1/spaces/overlay/bundles"),
        ("GET", "/v1/spaces/overlay/types?includeHidden=true"),
        ("GET", "/v1/spaces/overlay/types/t-program/properties"),
        ("GET", "/v1/spaces/overlay/types/t-program/datasets"),
        (
            "GET",
            "/v1/spaces/overlay/objects/skill-1/editor/editor_blocks/markdown",
        ),
        ("POST", "/v1/spaces/fixture/query"),
        ("POST", "/v1/spaces/overlay/objects/query"),
    ] {
        assert!(
            transport.send(method, path, None).is_ok(),
            "{method} {path}"
        );
    }
    assert!(transport
        .send(
            "PUT",
            "/v1/local/collections",
            Some(&json!({"scope": "space", "spaceId": "fixture", "name": "trace_runs"}))
        )
        .is_ok());
    // the device-local trace routes, retention's included, in every phase
    // (N2b-2 C3, design §10 R7)
    for route in ["upsert", "query", "delete", "aggregate"] {
        assert!(
            transport
                .send(
                    "POST",
                    &format!("/v1/local/{route}"),
                    Some(&coll("trace_records"))
                )
                .is_ok(),
            "{route}"
        );
    }
    let before = log.lock().unwrap().len();
    for (method, path, body) in [
        ("GET", "/v1/spaces/elsewhere", None),
        ("GET", "/v1/spaces/fixture/types", None),
        ("GET", "/v1/spaces/fixture/types?includeHidden=false", None),
        (
            "GET",
            "/v1/spaces/fixture/types/t/../../../../debug/pprof",
            None,
        ),
        ("GET", "/v1/spaces/fixture/types/%2e%2e/properties", None),
        ("GET", "/v1/spaces/fixture/properties/p", None),
        ("GET", "/v1/devices", None),
        ("GET", "/v1/auth", None),
        ("GET", "/v1/account", None),
        ("GET", "/v1/local/export?scope=space&spaceId=fixture", None),
        (
            "PATCH",
            "/v1/spaces/fixture/types/t-program",
            Some(json!({})),
        ),
        ("POST", "/v1/spaces/fixture/types", Some(json!({}))),
        (
            "POST",
            "/v1/spaces/fixture/bundles/b/children",
            Some(json!({})),
        ),
        (
            "POST",
            "/v1/spaces/fixture/modify",
            Some(modify("triggers", "t_triggers", json!([]))),
        ),
        (
            "POST",
            "/v1/spaces/fixture/objects/chat/chat/messages",
            Some(json!({"text": "x"})),
        ),
        (
            "POST",
            "/v1/events",
            Some(json!({"type": "bao.status", "scope": "account", "target": "peer"})),
        ),
        ("POST", "/v1/local/upsert", Some(coll("foreign"))),
        (
            "POST",
            "/v1/local/upsert",
            Some(
                json!({"coll": {"scope": "space", "spaceId": "overlay", "name": "trace_records"}}),
            ),
        ),
        (
            "PUT",
            "/v1/local/collections",
            Some(json!({"scope": "space", "spaceId": "fixture", "name": "foreign"})),
        ),
        ("PUT", "/v1/devices/me", Some(json!({"apps": {"bao": {}}}))),
    ] {
        let error = transport.send(method, path, body.as_ref()).unwrap_err();
        assert!(
            error.code == "preparation_write_denied" || error.code == "path_not_canonical",
            "{method} {path}: {}",
            error.code
        );
    }
    assert!(transport
        .open_stream(
            "/v1/spaces/fixture/query/subscribe",
            Some(&subscribe_body())
        )
        .is_err());
    assert_eq!(
        log.lock().unwrap().len(),
        before,
        "denied calls never reach the server"
    );
    assert_eq!(
        agent.drain_until(Instant::now() + Duration::from_secs(5)),
        DrainOutcome::Drained
    );
}

#[test]
fn execution_phase_adds_the_owned_writes_and_still_denies_registry_auth_hosts_and_secret_values() {
    let (mut agent, transport, gate, log) = gated(&["event: ready", "data: {}", ""]);
    gate.set(Phase::Execution);
    for (method, path, body) in [
        (
            "POST",
            "/v1/spaces/fixture/modify",
            Some(modify(
                "triggers",
                "t_triggers",
                json!([{"type": "$set", "path": "", "value": {"owner": "peer"}}]),
            )),
        ),
        (
            "POST",
            "/v1/spaces/fixture/modify",
            Some(modify("runs", "t_runs", json!([]))),
        ),
        (
            "POST",
            "/v1/spaces/fixture/modify",
            Some(modify("config", "t_config", json!([]))),
        ),
        (
            "POST",
            "/v1/spaces/fixture/modify",
            Some(modify(
                "secrets",
                "t_secrets",
                json!([{"type": "$set", "path": "key", "value": "k"}, {"type": "$set", "path": "secret", "value": true}, {"type": "$set", "path": "status", "value": "missing"}, {"type": "$set", "path": "label", "value": "L"}, {"type": "$unset", "path": "note"}, {"type": "$set", "path": "lastUsedAt", "value": "t"}]),
            )),
        ),
        (
            "POST",
            "/v1/spaces/fixture/objects/chat/chat/messages",
            Some(json!({"text": "Stopped."})),
        ),
        (
            "POST",
            "/v1/events",
            Some(json!({"type": "bao.status", "scope": "account", "target": "peer", "data": {}})),
        ),
        ("POST", "/v1/local/upsert", Some(coll("trace_records"))),
        ("POST", "/v1/local/query", Some(coll("trace_runs"))),
        ("POST", "/v1/local/delete", Some(coll("trace_blobs"))),
        ("POST", "/v1/spaces/overlay/query", None),
    ] {
        assert!(
            transport.send(method, path, body.as_ref()).is_ok(),
            "{method} {path}"
        );
    }
    assert!(transport
        .open_stream(
            "/v1/spaces/fixture/query/subscribe",
            Some(&subscribe_body())
        )
        .is_ok());
    // an event source: any chat object of the working space (N2b-2 C3)
    assert!(transport
        .open_stream(
            "/v1/spaces/fixture/query/subscribe",
            Some(
                &json!({"objectId": "bafy.events-chat", "dataset": "chat_messages",
                         "sort": ["-createdAt"], "limit": 64})
            )
        )
        .is_ok());
    let before = log.lock().unwrap().len();
    let secret_op = |op: Value| modify("secrets", "t_secrets", json!([op]));
    for (method, path, body) in [
        (
            "POST",
            "/v1/spaces/fixture/modify",
            Some(secret_op(
                json!({"type": "$set", "path": "value", "value": "k"}),
            )),
        ),
        (
            "POST",
            "/v1/spaces/fixture/modify",
            Some(secret_op(
                json!({"type": "$set", "path": "hosts", "value": ["a"]}),
            )),
        ),
        (
            "POST",
            "/v1/spaces/fixture/modify",
            Some(secret_op(
                json!({"type": "$set", "path": "", "value": {"key": "k", "value": "k"}}),
            )),
        ),
        (
            "POST",
            "/v1/spaces/fixture/modify",
            Some(secret_op(
                json!({"type": "$set", "path": "value.inner", "value": "k"}),
            )),
        ),
        (
            "POST",
            "/v1/spaces/fixture/modify",
            Some(secret_op(
                json!({"type": "$set", "path": "status.x", "value": "k"}),
            )),
        ),
        (
            "POST",
            "/v1/spaces/fixture/modify",
            Some(secret_op(
                json!({"type": "$push", "path": "note", "value": "k"}),
            )),
        ),
        (
            "POST",
            "/v1/spaces/fixture/modify",
            Some(
                json!({"objectId": "secrets", "dataset": "t_secrets", "records": [{"id": "r", "delete": true}]}),
            ),
        ),
        (
            "POST",
            "/v1/spaces/fixture/modify",
            Some(json!({"objectId": "secrets", "dataset": "t_secrets", "records": []})),
        ),
        (
            "POST",
            "/v1/spaces/fixture/modify",
            Some(modify(
                "secrets",
                "t_config",
                json!([{"type": "$set", "path": "status", "value": "missing"}]),
            )),
        ),
        (
            "POST",
            "/v1/spaces/fixture/modify",
            Some(modify("triggers", "t_runs", json!([]))),
        ),
        (
            "POST",
            "/v1/spaces/fixture/modify",
            Some(modify("other-object", "t_other", json!([]))),
        ),
        (
            "POST",
            "/v1/spaces/overlay/modify",
            Some(modify("triggers", "t_triggers", json!([]))),
        ),
        (
            "POST",
            "/v1/spaces/fixture/objects/other-chat/chat/messages",
            Some(json!({"text": "x"})),
        ),
        (
            "POST",
            "/v1/events",
            Some(json!({"type": "bao.host.status.v1", "scope": "account", "target": "peer"})),
        ),
        (
            "POST",
            "/v1/events",
            Some(json!({"type": "bao.status", "scope": "space", "target": "peer"})),
        ),
        (
            "POST",
            "/v1/events",
            Some(json!({"type": "bao.status", "scope": "account", "target": "other"})),
        ),
        (
            "POST",
            "/v1/local/upsert",
            Some(
                json!({"coll": {"scope": "space", "spaceId": "overlay", "name": "trace_records"}}),
            ),
        ),
        (
            "POST",
            "/v1/local/upsert",
            Some(json!({"coll": {"scope": "space", "spaceId": "fixture", "name": "foreign"}})),
        ),
        ("GET", "/v1/local/collections?scope=space", None),
        ("GET", "/v1/local/export?scope=space&spaceId=fixture", None),
        (
            "PUT",
            "/v1/devices/me",
            Some(json!({"apps": {"bao": null}})),
        ),
        ("POST", "/v1/devices/activate", Some(json!({}))),
        ("GET", "/v1/auth", None),
        ("GET", "/v1/account", None),
        (
            "POST",
            "/v1/spaces/fixture/bundles/b/children",
            Some(json!({})),
        ),
        ("PATCH", "/v1/spaces/fixture/types/t", Some(json!({}))),
        (
            "POST",
            "/v1/spaces/fixture/objects/chat/chat/messages/../../../../../devices/activate",
            Some(json!({})),
        ),
    ] {
        let error = transport.send(method, path, body.as_ref()).unwrap_err();
        assert!(
            error.code == "execution_route_denied" || error.code == "path_not_canonical",
            "{method} {path}: {}",
            error.code
        );
    }
    for (path, body) in [
        ("/v1/spaces/overlay/query/subscribe", subscribe_body()),
        ("/v1/devices/query/subscribe", subscribe_body()),
        ("/v1/spaces/fixture/query/subscribe?x=1", subscribe_body()),
        (
            "/v1/spaces/fixture/query/subscribe",
            json!({"objectId": "../other", "dataset": "chat_messages"}),
        ),
        (
            "/v1/spaces/fixture/query/subscribe",
            json!({"objectId": 7, "dataset": "chat_messages"}),
        ),
        (
            "/v1/spaces/fixture/query/subscribe",
            json!({"dataset": "chat_messages"}),
        ),
        (
            "/v1/spaces/fixture/query/subscribe",
            json!({"objectId": "chat", "dataset": "agent_triggers"}),
        ),
    ] {
        assert!(
            transport.open_stream(path, Some(&body)).is_err(),
            "{path} {body}"
        );
    }
    assert_eq!(log.lock().unwrap().len(), before);
    gate.set(Phase::Preparation);
    assert!(transport
        .send(
            "POST",
            "/v1/spaces/fixture/modify",
            Some(&modify("triggers", "t_triggers", json!([])))
        )
        .is_err());
    agent.cancel();
    assert!(transport.send("GET", "/v1/spaces/fixture", None).is_err());
    assert_eq!(
        agent.drain_until(Instant::now() + Duration::from_secs(5)),
        DrainOutcome::Drained
    );
    // the owner is gone: the gate's weak owner reference refuses everything
    assert!(transport.send("GET", "/v1/spaces/fixture", None).is_err());
}

/// N2b-2 C2: the one write on the setup-recorded log child is the ADR-005 §3
/// interrupted turn, exactly; anything else on it is refused before the
/// server, and the record reaches no other object.
#[test]
fn the_turns_log_child_takes_exactly_the_interrupted_turn() {
    let (mut agent, transport, gate, log) = gated(&["event: ready", "data: {}", ""]);
    gate.set(Phase::Execution);
    let turn = |id: &str, value: Value| {
        json!({"objectId": "turns", "dataset": "t_turns",
               "records": [{"id": id, "upsert": true,
                            "ops": [{"type": "$set", "path": "", "value": value}]}]})
    };
    let record = crate::chatwatch::interrupted_turn_record(7, "bao", "hi", "run_1");
    let path = "/v1/spaces/fixture/modify";
    assert!(transport
        .send("POST", path, Some(&turn("00000007", record.clone())))
        .is_ok());
    let before = log.lock().unwrap().len();
    let with = |key: &str, value: Value| {
        let mut r = record.clone();
        r[key] = value;
        r
    };
    let extra = with("note", json!("x"));
    let replied = with("replies", json!(["an answer"]));
    let finished = with("interrupted", json!(false));
    let stopped_otherwise = with("llm", json!({"stopReason": "end_turn"}));
    let mut missing = record.clone();
    missing.as_object_mut().unwrap().remove("searchText");
    let mut two = turn("00000007", record.clone());
    two["records"] = json!([two["records"][0], two["records"][0]]);
    let mut field_op = turn("00000007", record.clone());
    field_op["records"][0]["ops"][0]["path"] = json!("userText");
    let mut unset = turn("00000007", record.clone());
    unset["records"][0]["ops"][0]["type"] = json!("$unset");
    let mut no_upsert = turn("00000007", record.clone());
    no_upsert["records"][0]["upsert"] = json!(false);
    let mut elsewhere = turn("00000007", record.clone());
    elsewhere["dataset"] = json!("t_other");
    for body in [
        turn("0000007", record.clone()),
        turn("0000000a", record.clone()),
        turn("000000007", record.clone()),
        turn("00000007", extra),
        turn("00000007", replied),
        turn("00000007", finished),
        turn("00000007", stopped_otherwise),
        turn("00000007", missing),
        two,
        field_op,
        unset,
        no_upsert,
        elsewhere,
    ] {
        let error = transport.send("POST", path, Some(&body)).unwrap_err();
        assert_eq!(error.code, "execution_route_denied", "{body}");
    }
    assert_eq!(log.lock().unwrap().len(), before);
    gate.set(Phase::Preparation);
    assert!(transport
        .send("POST", path, Some(&turn("00000007", record)))
        .is_err());
    assert_eq!(
        agent.drain_until(Instant::now() + Duration::from_secs(5)),
        DrainOutcome::Drained
    );
}

#[test]
fn an_over_long_stream_line_ends_the_stream_as_a_visible_capacity_fault() {
    let long = "data: ".to_string() + &"x".repeat(MAX_STREAM_LINE_BYTES + 1);
    let (mut agent, transport, gate, _) =
        gated(&["event: ready", "data: {}", "", &long, "data: after"]);
    gate.set(Phase::Execution);
    let lines: Vec<String> = transport
        .open_stream(
            "/v1/spaces/fixture/query/subscribe",
            Some(&subscribe_body()),
        )
        .unwrap()
        .collect();
    assert_eq!(lines, vec!["event: ready", "data: {}", ""]);
    assert_eq!(agent.health().capacity_faults, 1);
    assert_eq!(
        agent.drain_until(Instant::now() + Duration::from_secs(5)),
        DrainOutcome::Drained
    );
}

/// Step B isolation review, item 3 precision (i): the gate's own sink check —
/// not only the trace store's pre-check — refuses an aggregate with a
/// `$out`/`$merge` stage anywhere, nested `$facet` included, before any call.
#[test]
fn the_gate_itself_refuses_an_aggregate_sink_stage() {
    let (mut agent, transport, gate, log) = gated(&[]);
    gate.set(Phase::Execution);
    let aggregate = |pipeline: Value| {
        let mut body = coll("trace_runs");
        body["pipeline"] = pipeline;
        transport.send("POST", "/v1/local/aggregate", Some(&body))
    };
    assert!(aggregate(json!([{"$match": {"program": "x"}}, {"$count": "n"}])).is_ok());
    let reached = log.lock().unwrap().len();
    for sink in [
        json!([{"$match": {}}, {"$out": "trace_copy"}]),
        json!([{"$merge": {"into": "trace_runs"}}]),
        json!([{"$facet": {"a": [{"$out": "x"}]}}]),
    ] {
        let error = aggregate(sink.clone()).unwrap_err();
        assert_eq!(error.code, "execution_route_denied", "{sink}");
    }
    assert_eq!(
        log.lock().unwrap().len(),
        reached,
        "no sink reached the store"
    );
    assert_eq!(
        agent.drain_until(Instant::now() + Duration::from_secs(5)),
        DrainOutcome::Drained
    );
}

// --- the gate against its real consumers (N2a-r2 items 3 and 4) -----------------

/// An in-memory local store behind the gate: the seven `/v1/local/*` routes the
/// trace store issues, with the filter forms it uses.
#[derive(Default)]
struct MemLocal {
    docs: Mutex<BTreeMap<String, BTreeMap<String, Value>>>,
    calls: CallLog,
}

fn field<'a>(doc: &'a Value, path: &str) -> Option<&'a Value> {
    path.split('.').try_fold(doc, |v, k| v.get(k))
}

fn cmp_json(a: &Value, b: &Value) -> Option<std::cmp::Ordering> {
    match (a.as_f64(), b.as_f64()) {
        (Some(x), Some(y)) => x.partial_cmp(&y),
        _ => a.as_str().zip(b.as_str()).map(|(x, y)| x.cmp(y)),
    }
}

fn matches(doc: &Value, filter: &Value) -> bool {
    let Some(filter) = filter.as_object() else {
        return true;
    };
    filter.iter().all(|(key, want)| match key.as_str() {
        "$and" => want
            .as_array()
            .is_some_and(|fs| fs.iter().all(|f| matches(doc, f))),
        "$or" => want
            .as_array()
            .is_some_and(|fs| fs.iter().any(|f| matches(doc, f))),
        _ => {
            let got = field(doc, key);
            match want.as_object() {
                Some(ops) if ops.keys().all(|k| k.starts_with('$')) => {
                    ops.iter().all(|(op, v)| match (op.as_str(), got) {
                        ("$exists", g) => g.is_some() == v.as_bool().unwrap_or(true),
                        ("$ne", g) => g != Some(v),
                        ("$in", Some(g)) => v.as_array().is_some_and(|a| a.contains(g)),
                        ("$gt", Some(g)) => cmp_json(g, v) == Some(std::cmp::Ordering::Greater),
                        ("$gte", Some(g)) => cmp_json(g, v) != Some(std::cmp::Ordering::Less),
                        ("$lt", Some(g)) => cmp_json(g, v) == Some(std::cmp::Ordering::Less),
                        _ => false,
                    })
                }
                _ => got == Some(want),
            }
        }
    })
}

impl Transport for MemLocal {
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
        let body = body.cloned().unwrap_or(Value::Null);
        let coll_key = |c: &Value| {
            format!(
                "{}/{}",
                c["spaceId"].as_str().unwrap_or(""),
                c["name"].as_str().unwrap_or("")
            )
        };
        let mut docs = self.docs.lock().unwrap();
        let op = path.trim_start_matches("/v1/local/");
        Ok(match (method, op) {
            ("PUT", "collections") => (200, json!({})),
            ("POST", "insert") | ("POST", "upsert") => {
                let coll = docs.entry(coll_key(&body["coll"])).or_default();
                let mut ids = Vec::new();
                for (i, d) in body["docs"]
                    .as_array()
                    .cloned()
                    .unwrap_or_default()
                    .into_iter()
                    .enumerate()
                {
                    let id = d["id"]
                        .as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| format!("minted-{}", coll.len() + i));
                    let mut d = d;
                    d["id"] = json!(id);
                    coll.insert(id.clone(), d);
                    ids.push(id);
                }
                (200, json!({"ids": ids}))
            }
            ("POST", "get") => {
                let coll = docs.entry(coll_key(&body["coll"])).or_default();
                match coll.get(body["id"].as_str().unwrap_or("")) {
                    Some(d) => (200, json!({"record": d})),
                    None => (
                        404,
                        json!({"error": {"code": "local.doc_not_found", "message": "no"}}),
                    ),
                }
            }
            ("POST", "query") => {
                let coll = docs.entry(coll_key(&body["coll"])).or_default();
                let limit = body["limit"].as_u64().unwrap_or(100) as usize;
                let records: Vec<Value> = coll
                    .values()
                    .filter(|d| matches(d, &body["filter"]))
                    .take(limit)
                    .cloned()
                    .collect();
                (200, json!({"records": records}))
            }
            ("POST", "aggregate") => {
                let coll = docs.entry(coll_key(&body["coll"])).or_default();
                (
                    200,
                    json!({"records": coll.values().cloned().collect::<Vec<_>>()}),
                )
            }
            ("POST", "update") => {
                let coll = docs.entry(coll_key(&body["coll"])).or_default();
                if let Some(d) = coll.get_mut(body["id"].as_str().unwrap_or("")) {
                    for (k, v) in body["modifier"]["$set"]
                        .as_object()
                        .cloned()
                        .unwrap_or_default()
                    {
                        d[k] = v;
                    }
                }
                (200, json!({}))
            }
            ("POST", "delete") => {
                let coll = docs.entry(coll_key(&body["coll"])).or_default();
                let before = coll.len();
                if let Some(ids) = body["ids"].as_array() {
                    coll.retain(|id, _| !ids.contains(&json!(id)));
                } else {
                    let filter = body["filter"].clone();
                    coll.retain(|_, d| !matches(d, &filter));
                }
                (200, json!({"deleted": before - coll.len()}))
            }
            _ => (404, json!({"error": {"code": "no_route", "message": path}})),
        })
    }
    fn open_stream(
        &self,
        _: &str,
        _: Option<&Value>,
    ) -> Result<Box<dyn Iterator<Item = String>>, AnyError> {
        Err(AnyError {
            status: 0,
            code: "stream".into(),
            message: "none".into(),
        })
    }
}

fn gated_over(
    inner: Box<dyn Transport + Send + Sync>,
) -> (crate::managed::ManagedAgent, GatedTransport, PhaseGate) {
    let (tx, rx) = std::sync::mpsc::channel();
    let agent = start_with(CancelToken::new(), move |ctx| {
        tx.send(Arc::downgrade(&ctx.shared)).unwrap();
        while !ctx.idle_until(Instant::now() + Duration::from_secs(300)) {}
        Ok(())
    });
    let shared = rx.recv_timeout(Duration::from_secs(5)).unwrap();
    let gate = PhaseGate::default();
    let transport = GatedTransport::new(
        inner,
        shared,
        ["fixture".to_string()].into_iter().collect(),
        "fixture".into(),
        objects(),
        gate.clone(),
    );
    (agent, transport, gate)
}

/// N2a-r2 item 3: the real `AnyTraceStore` streams, lands, reads, aggregates
/// and sweeps through the Execution gate — every route it issues is admitted.
#[test]
fn the_real_trace_store_works_through_the_execution_gate() {
    use crate::broker::Broker;
    use crate::tracestore::{AnyTraceStore, TraceStore};
    use std::sync::atomic::AtomicBool;
    let mem = Arc::new(MemLocal::default());
    struct Via(Arc<MemLocal>);
    impl Transport for Via {
        fn send(&self, m: &str, p: &str, b: Option<&Value>) -> Result<(u16, Value), AnyError> {
            self.0.send(m, p, b)
        }
        fn open_stream(
            &self,
            p: &str,
            b: Option<&Value>,
        ) -> Result<Box<dyn Iterator<Item = String>>, AnyError> {
            self.0.open_stream(p, b)
        }
    }
    let (mut agent, transport, gate) = gated_over(Box::new(Via(mem.clone())));
    gate.set(Phase::Execution);
    let client = Arc::new(crate::anyapi::Client::with_transport(Box::new(transport)));
    let store = AnyTraceStore::new(client, "fixture", Some("peer".into()), None).unwrap();
    let mut writer = crate::trace::TraceWriter::new(json!({"id": "run_gate", "program": "job@v1"}));
    writer.stream_to(&store).unwrap();
    let mut broker = Broker::new(
        writer,
        Default::default(),
        Default::default(),
        None,
        crate::routes::Classifier::new(Some("http://127.0.0.1:7001")),
    );
    broker.trace_store = Some(Arc::new(
        AnyTraceStore::new(
            Arc::new(crate::anyapi::Client::with_transport(Box::new(Via(
                mem.clone()
            )))),
            "fixture",
            None,
            None,
        )
        .unwrap(),
    ));
    broker.call("uuid4", json!({})).unwrap();
    let summary = broker
        .writer
        .dump(&store)
        .expect("the summary landed through the gate");
    assert_eq!(summary["id"], "run_gate");
    // reads: load (query), one record (get), the runs summary (query), aggregate
    let records = store.load("run_gate").unwrap();
    assert!(records.iter().any(|r| r["effect"] == "uuid4"));
    assert_eq!(store.record("run_gate", 1).unwrap()["effect"], "uuid4");
    assert!(
        store
            .find_runs(&json!({}), &Value::Null, 10)
            .unwrap()
            .unwrap()
            .len()
            == 1
    );
    assert!(store.query("records", &json!([{"$match": {}}])).unwrap()["records"].is_array());
    assert!(
        store.query("records", &json!([{"$out": "x"}])).is_err(),
        "a sink is refused"
    );
    // retention: delete + update through the gate
    let stop = AtomicBool::new(false);
    let now = crate::managed::service::epoch_s() + 1.0;
    assert_eq!(
        store
            .expire("agent:toolcaller@v1", None, Some(now), None, &stop)
            .unwrap(),
        1
    );
    assert!(store.load("run_gate").is_err(), "expired");
    let ops: std::collections::BTreeSet<String> = mem
        .calls
        .lock()
        .unwrap()
        .iter()
        .map(|(_, p, _)| p.trim_start_matches("/v1/local/").to_string())
        .collect();
    for op in [
        "collections",
        "insert",
        "upsert",
        "query",
        "get",
        "aggregate",
        "update",
        "delete",
    ] {
        assert!(ops.contains(op), "{op} reached the store");
    }
    assert_eq!(agent.health().capacity_faults, 0);
    assert_eq!(
        agent.drain_until(Instant::now() + Duration::from_secs(5)),
        DrainOutcome::Drained
    );
}

/// N2a-r2 item 4: the broker's own stamps and the takeover's reconcile pass the
/// secrets allowlist with their exact op lists; a `value` or a `hosts` write never does.
#[test]
fn the_secret_store_stamps_pass_the_gate_and_value_or_hosts_never_do() {
    use crate::broker::SecretSource;
    use crate::managed::takeover::{declared_stamp_patch, managed_descriptor_patch};
    use crate::stores::{persist_local_secret, upsert_secret_row, ServeSecretStore};
    let stub = StubTransport::new();
    let log = stub.log();
    for _ in 0..64 {
        stub.push(200, json!({"records": []}));
    }
    let (mut agent, transport, gate) = gated_over(Box::new(stub));
    gate.set(Phase::Execution);
    let client = Arc::new(crate::anyapi::Client::with_transport(Box::new(transport)));
    // the managed store: a stamp on a row that does not exist yet writes the
    // descriptor without `hosts` (the desktop store would write them and be refused)
    let store = crate::managed::service::ManagedSecretStore(ServeSecretStore {
        client: client.clone(),
        space: "fixture".into(),
        obj: "secrets".into(),
        dataset: "t_secrets".into(),
    });
    let about = json!({"label": "L", "hosts": ["h.example"], "help": "x"});
    let modifies = || {
        log.lock()
            .unwrap()
            .iter()
            .filter(|(_, p, _)| p.ends_with("/modify"))
            .count()
    };
    // the exact stamps the managed broker makes (each one is a modify that reaches the store)
    store.mark_missing("connector.key.github", &about, "run_1");
    assert_eq!(modifies(), 1);
    store.mark_rejected("llm.key.openai", &about, "run_1", 401);
    assert_eq!(modifies(), 2);
    store.mark_used("llm.key.openai");
    assert_eq!(modifies(), 3);
    let patch = managed_descriptor_patch(&about, None);
    upsert_secret_row(
        &client,
        "fixture",
        "secrets",
        "t_secrets",
        "google.key.gemini",
        &Value::Object(patch),
    )
    .unwrap();
    assert_eq!(modifies(), 4);
    // never a value, never a host binding — refused before the store sees them
    assert!(persist_local_secret(
        &client,
        "fixture",
        "secrets",
        "t_secrets",
        "value",
        "llm.key.openai",
        "sk-x"
    )
    .is_err());
    store.bind_hosts("local.key.weather", &about);
    let desktop = declared_stamp_patch(&about, None);
    assert!(upsert_secret_row(
        &client,
        "fixture",
        "secrets",
        "t_secrets",
        "k",
        &Value::Object(desktop)
    )
    .is_err());
    assert_eq!(modifies(), 4, "no value or hosts write reached the store");
    for (_, p, body) in log.lock().unwrap().iter() {
        if p.ends_with("/modify") {
            for rec in body.as_ref().unwrap()["records"].as_array().unwrap() {
                for op in rec["ops"].as_array().unwrap() {
                    let path = op["path"].as_str().unwrap();
                    assert!(DESCRIPTOR_FIELDS.contains(&path), "{path}");
                    assert!(path != "value" && path != "hosts");
                }
            }
        }
    }
    assert_eq!(
        agent.drain_until(Instant::now() + Duration::from_secs(5)),
        DrainOutcome::Drained
    );
}
