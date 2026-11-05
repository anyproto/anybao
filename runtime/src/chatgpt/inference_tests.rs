//! Synthetic service + actual broker/replay integration (docs/chatgpt-local-oauth.md §6).
use super::*;
use crate::broker::chatgpt::{Control, Transport, WireReply, ENDPOINT};
use crate::broker::{Broker, EffectFailure, Mode};
use crate::caps::GrantSet;
use crate::replay::ReplayCursor;
use crate::routes::Classifier;
use crate::trace::TraceWriter;
use crate::tracestore::{MemTraceStore, TraceStore};
use reqwest::header::HeaderMap;
use std::collections::BTreeMap;

type InferenceStep =
    Box<dyn FnOnce(&str, HeaderMap, &Control<'_>) -> Result<WireReply, EffectFailure> + Send>;
#[derive(Default)]
struct InferenceScript(Mutex<VecDeque<InferenceStep>>, AtomicUsize);
impl InferenceScript {
    fn add(
        &self,
        step: impl FnOnce(&str, HeaderMap, &Control<'_>) -> Result<WireReply, EffectFailure>
            + Send
            + 'static,
    ) {
        self.0.lock().unwrap().push_back(Box::new(step));
    }
    fn reply(&self, status: u16, body: String) {
        self.add(move |_, _, _| {
            Ok(WireReply {
                status,
                body,
                retry_after: None,
                streaming: status == 200,
            })
        });
    }
}
impl Transport for InferenceScript {
    fn post(
        &self,
        body: &str,
        headers: HeaderMap,
        control: &Control<'_>,
        _: Duration,
    ) -> Result<WireReply, EffectFailure> {
        control.check()?;
        self.1.fetch_add(1, Ordering::Relaxed);
        self.0
            .lock()
            .unwrap()
            .pop_front()
            .expect("unexpected inference")(body, headers, control)
    }
}
fn payload() -> Value {
    json!({"url": ENDPOINT, "credential": {"ref": crate::local_grant::CHATGPT_REF},
        "headers": {"Content-Type": "application/json", "Accept": "text/event-stream"}, "stream": true,
        "timeout": {"total": 10, "idle": 2},
        "json": {"model": "gpt-synthetic", "instructions": "synthetic", "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
        "tools": [], "store": false, "stream": true, "include": ["reasoning.encrypted_content"]}})
}
fn success(text: &str) -> String {
    let item = json!({"id": "lookup_msg", "type": "message", "role": "assistant", "status": "completed", "phase": "final_answer", "content": [{"type": "output_text", "text": text}]});
    [json!({"type": "response.created", "response": {"id": "r1", "status": "in_progress"}}),
        json!({"type": "response.output_item.added", "output_index": 0, "item": item}),
        json!({"type": "response.output_item.done", "output_index": 0, "item": item}),
        json!({"type": "response.completed", "response": {"id": "r1", "status": "completed", "output": [item]}})]
        .iter().map(|v| format!("data: {v}\n\n")).collect()
}
fn rejected() -> String {
    json!({"error": {"code": "token_expired", "message": "REFLECTED-ACCESS-SENTINEL"}}).to_string()
}
fn broker(auth: Arc<ChatGptAuth>, script: Arc<InferenceScript>) -> Broker {
    let mut b = Broker::new(
        TraceWriter::new(
            json!({"id": "run_inference", "program": "toolcaller@v1", "host": "rust"}),
        ),
        BTreeMap::new(),
        BTreeMap::new(),
        None,
        Classifier::new(None),
    );
    b.chatgpt_auth = Some(auth);
    b.chatgpt_transport = Some(script);
    b
}
fn setup() -> (Harness, Arc<InferenceScript>, Broker) {
    let h = Harness::new();
    seed(&h.store, NOW + 3600);
    let script = Arc::new(InferenceScript::default());
    let b = broker(h.auth.clone(), script.clone());
    (h, script, b)
}

fn run_guest(mut b: Broker, source: &str) -> crate::runner::RunOutcome {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(
        dir.path().join("llm@v1.py"),
        include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../repos/_agent/programs/llm@v1/program.py"
        )),
    )
    .unwrap();
    std::fs::write(dir.path().join("probe@v1.py"), source).unwrap();
    b.programs_dir = Some(dir.path().to_path_buf());
    b.config.entry("llm.tier.codegen".into()).or_insert_with(|| json!({"provider":"openai-responses", "backend":"openai-codex", "model":"gpt-synthetic", "base_url":"https://chatgpt.com/backend-api/codex", "api_key_ref":crate::local_grant::CHATGPT_REF, "timeout":30}));
    let cage = crate::runner::Cage::embedded().unwrap();
    let interrupt = b.interrupt.clone();
    crate::runner::run_program(
        &cage,
        b,
        "probe@v1",
        &json!({}),
        Default::default(),
        interrupt,
        60.0,
    )
    .unwrap()
}

const ONE_CHAT: &str = "def main(args):\n    return use('llm@v1').chat([{'role':'user','parts':[{'type':'text','text':'private history'}]}])\n";

#[test]
fn backend_completed_items_cross_sanitizer_real_guest_and_replay() {
    // Shared synthetic lifecycle, not captured provider traffic. In particular,
    // item.added carries skeletons; terminal output can be a redundant omission.
    let fixture: Vec<Value> = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../tests/fixtures/responses-codex-lifecycle.json"
    )))
    .unwrap();
    for mode in [
        "full",
        "missing",
        "null",
        "empty",
        "wrong_type",
        "conflict",
        "unfinished",
    ] {
        let (h, script, b) = setup();
        let mut events = fixture.clone();
        let terminal = &mut events.last_mut().unwrap()["response"];
        match mode {
            "missing" | "unfinished" => {
                terminal.as_object_mut().unwrap().remove("output");
            }
            "null" => terminal["output"] = Value::Null,
            "empty" => terminal["output"] = json!([]),
            "wrong_type" => terminal["output"] = json!("untrusted-provider-string"),
            "conflict" => terminal["output"][2]["arguments"] = json!("{}"),
            _ => (),
        }
        if mode == "unfinished" {
            events
                .retain(|e| !(e["type"] == "response.output_item.done" && e["output_index"] == 2));
        }
        let invalid = matches!(mode, "wrong_type" | "conflict" | "unfinished");
        let body = wire_events(&events);
        for _ in 0..if invalid { 3 } else { 1 } {
            script.reply(200, body.clone());
        }
        let result = run_guest(b, ONE_CHAT);
        if invalid {
            assert_eq!(result.status, "error", "{mode}");
            assert!(result
                .error
                .as_ref()
                .unwrap()
                .to_string()
                .contains("IncompleteReply"));
            assert!(!result
                .error
                .as_ref()
                .unwrap()
                .to_string()
                .contains("untrusted-provider-string"));
        } else {
            assert_eq!(result.status, "ok", "{mode}: {:?}", result.error);
            assert_eq!(result.value["stop"], "tool");
            assert_eq!(
                result.value["parts"][0]["provider_state"]["encrypted_content"],
                "OPAQUE-DO-NOT-DECODE"
            );
            assert_eq!(
                result.value["parts"][1],
                json!({"type":"text", "text":"working", "phase":"commentary"})
            );
            assert_eq!(
                result.value["parts"][2],
                json!({"type":"tool_call", "id":"call_actual", "name":"run_cell", "args":{"code":"1+1"}})
            );
            assert_eq!(
                result.value["parts"][3],
                json!({"type":"text", "text":"answer", "phase":"final_answer"})
            );
            assert_eq!(
                result.value["usage"],
                json!({"in":5, "out":12, "cacheRead":20, "cacheWrite":0, "billingSource":"chatgpt-subscription"})
            );
        }
        assert_eq!(
            script.1.load(Ordering::Relaxed),
            if invalid { 3 } else { 1 }
        );
        assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
        // The host inspected the complete raw lifecycle without repairing its
        // terminal; the guest is responsible for completed-item assembly.
        let record = result
            .broker
            .writer
            .records
            .iter()
            .find(|r| r["effect"] == "http.post")
            .unwrap();
        assert_eq!(record["output"]["body"], body);
        replay_guest(&h, &result, ONE_CHAT);
    }
}

fn replay_guest(h: &Harness, result: &crate::runner::RunOutcome, source: &str) {
    let mut replay = broker(h.auth.clone(), Arc::new(InferenceScript::default()));
    replay.writer = TraceWriter::new(result.broker.writer.records[0]["run"].clone());
    replay.mode = Mode::Replay;
    replay.cursor = Some(ReplayCursor::new(&result.broker.writer.records));
    replay.blobs = result.broker.writer.blobs.iter().cloned().collect();
    h.store.panic_reads.store(true, Ordering::Relaxed);
    let replay = run_guest(replay, source);
    assert_eq!(replay.status, result.status, "{:?}", replay.error);
    assert_eq!(replay.value, result.value);
    assert_eq!(replay.error, result.error);
    h.store.panic_reads.store(false, Ordering::Relaxed);
}

// Commit-time observation lets tests change synthetic custody exactly after
// the real guest backoff, without timers or modifying production guest code.
type BackoffAction = Arc<Mutex<Option<Box<dyn FnOnce() + Send>>>>;
struct BackoffStore {
    inner: MemTraceStore,
    action: BackoffAction,
}
struct BackoffSink {
    inner: Box<dyn crate::tracestore::TraceSink>,
    action: BackoffAction,
}
impl crate::tracestore::TraceSink for BackoffSink {
    fn append(&mut self, r: &Value) -> anyhow::Result<()> {
        self.inner.append(r)?;
        if r["effect"] == "sleep" {
            if let Some(action) = self.action.lock().unwrap().take() {
                action();
            }
        }
        Ok(())
    }
    fn append_blob(&mut self, h: &str, data: &str) -> anyhow::Result<()> {
        self.inner.append_blob(h, data)
    }
    fn close(&mut self) -> anyhow::Result<()> {
        self.inner.close()
    }
}
impl TraceStore for BackoffStore {
    fn open_sink(&self, run: &str) -> anyhow::Result<Box<dyn crate::tracestore::TraceSink>> {
        Ok(Box::new(BackoffSink {
            inner: self.inner.open_sink(run)?,
            action: self.action.clone(),
        }))
    }
    fn write_run(
        &self,
        run: &str,
        records: &[Value],
        blobs: &[(String, String)],
    ) -> anyhow::Result<()> {
        self.inner.write_run(run, records, blobs)
    }
    fn list(&self) -> anyhow::Result<Vec<crate::tracestore::RunMeta>> {
        self.inner.list()
    }
    fn load(&self, run: &str) -> anyhow::Result<Vec<Value>> {
        self.inner.load(run)
    }
    fn blobs(&self, run: &str) -> anyhow::Result<BTreeMap<String, String>> {
        self.inner.blobs(run)
    }
}
fn after_backoff(b: &mut Broker, action: impl FnOnce() + Send + 'static) {
    b.writer
        .stream_to(&BackoffStore {
            inner: MemTraceStore::new(),
            action: Arc::new(Mutex::new(Some(Box::new(action)))),
        })
        .unwrap();
}

fn logical_spans(result: &crate::runner::RunOutcome) -> usize {
    result
        .broker
        .writer
        .records
        .iter()
        .filter(|r| r["kind"] == "span" && r["phase"] == "begin" && r["name"] == "llm.chat")
        .count()
}

#[test]
fn real_logical_call_keeps_grant_binding_and_new_call_can_use_replacement() {
    let (h, script, mut b) = setup();
    script.reply(502, json!({"error": {"code": "server_error"}}).to_string());
    let store = h.store.clone();
    after_backoff(&mut b, move || {
        store.value.lock().unwrap().as_mut().unwrap().grant = Some(
            parse_grant(
                &tokens(
                    "different-synthetic-account",
                    NOW + 7200,
                    Some("replacement"),
                ),
                NOW,
                None,
            )
            .unwrap(),
        );
    });
    script.add(|body, headers, _| {
        assert!(body.contains("private history"));
        assert_eq!(headers["chatgpt-account-id"], "different-synthetic-account");
        Ok(WireReply {
            status: 200,
            body: success("new call accepted"),
            retry_after: None,
            streaming: true,
        })
    });
    let source = "def main(args):\n    llm = use('llm@v1')\n    messages = [{'role':'user','parts':[{'type':'text','text':'private history'}]}]\n    try:\n        llm.chat(messages)\n    except Exception as e:\n        first = str(e)\n    else:\n        first = 'unexpected success'\n    return [first, llm.chat(messages)]\n";
    let result = run_guest(b, source);
    assert_eq!(result.status, "ok", "{:?}", result.error);
    assert!(result.value[0]
        .as_str()
        .unwrap()
        .contains("account_changed"));
    assert_eq!(result.value[1]["parts"][0]["text"], "new call accepted");
    assert_eq!(logical_spans(&result), 2);
    assert_eq!(script.1.load(Ordering::Relaxed), 2);
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
    let records = &result.broker.writer.records;
    assert_eq!(
        records
            .iter()
            .filter(|r| r["effect"] == "http.post")
            .count(),
        3
    );
    let grant_id = h
        .store
        .value
        .lock()
        .unwrap()
        .as_ref()
        .unwrap()
        .grant
        .as_ref()
        .unwrap()
        .id
        .clone();
    assert!(!json!(records).to_string().contains(&grant_id));
    replay_guest(&h, &result, source);
}

#[test]
fn real_logical_call_shares_one_auth_recovery_across_transient_retries() {
    let (h, script, b) = setup();
    script.reply(401, rejected());
    script.reply(502, json!({"error": {"code": "server_error"}}).to_string());
    script.reply(401, rejected());
    h.script.response(
        Endpoint::Token,
        200,
        tokens("synthetic-account", NOW + 7200, Some("rotated")),
    );
    let result = run_guest(b, ONE_CHAT);
    assert_eq!(result.status, "error");
    assert!(result
        .error
        .as_ref()
        .unwrap()
        .to_string()
        .contains("grant_rejected"));
    assert_eq!(logical_spans(&result), 1);
    assert_eq!(script.1.load(Ordering::Relaxed), 3);
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 1);
    let attempts: Vec<_> = result
        .broker
        .writer
        .records
        .iter()
        .filter(|r| r["effect"] == "chatgpt.inference")
        .map(|r| r["input"]["attempt"].as_u64().unwrap())
        .collect();
    assert_eq!(attempts, [1, 2, 3]);
    assert_eq!(
        h.auth.get_connection().state,
        ConnectionState::ReauthRequired
    );
    replay_guest(&h, &result, ONE_CHAT);
}

#[test]
fn real_logical_call_allows_same_grant_peer_rotation_during_backoff() {
    let (h, script, mut b) = setup();
    h.store
        .value
        .lock()
        .unwrap()
        .as_mut()
        .unwrap()
        .grant
        .as_mut()
        .unwrap()
        .residency = Some("us".into());
    script.reply(502, json!({"error": {"code": "server_error"}}).to_string());
    let store = h.store.clone();
    after_backoff(&mut b, move || {
        let mut env = store.value.lock().unwrap();
        let old = env.as_ref().unwrap().grant.as_ref().unwrap();
        env.as_mut().unwrap().grant = Some(
            parse_grant(
                &tokens("synthetic-account", NOW + 7200, Some("peer-pair")),
                NOW,
                Some(old),
            )
            .unwrap(),
        );
    });
    script.reply(200, success("us and eu are ordinary words"));
    let result = run_guest(b, ONE_CHAT);
    assert_eq!(result.status, "ok", "{:?}", result.error);
    assert_eq!(logical_spans(&result), 1);
    assert_eq!(script.1.load(Ordering::Relaxed), 2);
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
    replay_guest(&h, &result, ONE_CHAT);
}

#[test]
fn real_logical_call_aborts_disconnect_session_end_or_stop_after_backoff() {
    for change in ["disconnect", "session", "stop"] {
        let (h, script, mut b) = setup();
        script.reply(502, json!({"error": {"code": "server_error"}}).to_string());
        let auth = h.auth.clone();
        let interrupt = b.interrupt.clone();
        after_backoff(&mut b, move || match change {
            "disconnect" => {
                auth.disconnect().unwrap();
            }
            "session" => auth.shutdown(),
            _ => interrupt.store(true, Ordering::Release),
        });
        let result = run_guest(b, ONE_CHAT);
        assert_ne!(result.status, "ok");
        assert_eq!(script.1.load(Ordering::Relaxed), 1);
        assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
        if change != "stop" {
            replay_guest(&h, &result, ONE_CHAT);
        }
    }
}

#[test]
fn invalid_requests_never_touch_custody_or_transport_and_real_capability_gate() {
    let (h, script, mut b) = setup();
    let reads = h.store.reads.load(Ordering::Relaxed);
    h.store.panic_reads.store(true, Ordering::Relaxed);
    let mut bad = Vec::new();
    for url in [
        "https://chatgpt.com/backend-api/codex/../codex/responses",
        "https://chatgpt.com/backend-api/codex%2fresponses",
        "https://chatgpt.com/backend-api/codex/responses?x=1",
        "https://chatgpt.com/backend-api/codex/responses#x",
        "http://chatgpt.com/backend-api/codex/responses",
        "https://chatgpt.com:444/backend-api/codex/responses",
        "https://u@chatgpt.com/backend-api/codex/responses",
        "https://chatgpt.com.evil.test/backend-api/codex/responses",
        "https://chatgpt.com/backend-api/./codex/responses",
    ] {
        let mut p = payload();
        p["url"] = json!(url);
        bad.push(p);
    }
    for (field, value) in [
        ("redirects", json!(1)),
        ("params", json!({})),
        ("body", json!("x")),
        ("stream", json!(false)),
        ("form", json!({})),
        (
            "credential",
            json!({"ref": crate::local_grant::CHATGPT_REF, "prefix": "Bearer "}),
        ),
    ] {
        let mut p = payload();
        p[field] = value;
        bad.push(p);
    }
    for (field, value) in [
        ("store", json!(true)),
        ("stream", json!(false)),
        ("previous_response_id", json!("r")),
        ("conversation", json!("c")),
        ("background", json!(true)),
        ("max_output_tokens", json!(1)),
        ("messages", json!([])),
        ("tools", json!([{"type": "web_search"}])),
        ("input", json!([{"type": "item_reference", "id": "x"}])),
        ("text", json!({"format": {}})),
    ] {
        let mut p = payload();
        p["json"][field] = value;
        bad.push(p);
    }
    for header in [
        "Authorization",
        "aUtHoRiZaTiOn",
        "ChatGPT-Account-ID",
        "x-openai-internal-codex-residency",
        "User-Agent",
        "originator",
        "Cookie",
        "Host",
        "accept",
    ] {
        let mut p = payload();
        p["headers"][header] = json!("synthetic");
        bad.push(p);
    }
    for item in [
        json!({"type": "reasoning", "id": "lookup", "summary": [], "encrypted_content": "opaque"}),
        json!({"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "https://evil.test/image"}]}),
        json!({"type": "function_call_output", "call_id": "orphan", "output": "no"}),
        json!({"type": "message", "role": "assistant", "phase": "wrong", "content": [{"type": "output_text", "text": "x"}]}),
    ] {
        let mut p = payload();
        p["json"]["input"] = json!([item]);
        bad.push(p);
    }
    for p in bad {
        assert_eq!(
            b.call("http.post", p).unwrap_err().type_,
            "chatgpt_request_invalid"
        );
    }
    assert_eq!(
        b.call("http.get", payload()).unwrap_err().type_,
        "chatgpt_request_invalid"
    );
    assert_eq!(h.store.reads.load(Ordering::Relaxed), reads);
    assert_eq!(script.1.load(Ordering::Relaxed), 0);
    b.grants = Some(GrantSet::of(["net.http"]));
    assert_eq!(
        b.call("http.post", payload()).unwrap_err().type_,
        "capability_denied"
    );
    assert_eq!(h.store.reads.load(Ordering::Relaxed), reads);
    h.store.panic_reads.store(false, Ordering::Relaxed);
    b.grants = Some(GrantSet::of(["llm.chat"]));
    script.reply(200, success("ok"));
    assert_eq!(b.call("http.post", payload()).unwrap()["status"], 200);
}

#[test]
fn headers_are_sensitive_truthful_and_every_attempt_is_recorded_replay_is_offline() {
    let (h, script, mut b) = setup();
    let original = h
        .store
        .value
        .lock()
        .unwrap()
        .as_ref()
        .unwrap()
        .grant
        .as_ref()
        .unwrap()
        .access
        .0
        .clone();
    script.add(move |body, headers, _| {
        assert_eq!(headers["authorization"], format!("Bearer {original}"));
        assert_eq!(headers["chatgpt-account-id"], "synthetic-account");
        assert!(
            headers["authorization"].is_sensitive() && headers["chatgpt-account-id"].is_sensitive()
        );
        let debug = format!("{headers:?}");
        assert!(!debug.contains(&original) && !debug.contains("synthetic-account"));
        assert_eq!(headers["originator"], "anybao");
        assert!(headers["user-agent"]
            .to_str()
            .unwrap()
            .starts_with("anybao/"));
        assert!(!body.contains("synthetic-account"));
        Ok(WireReply {
            status: 401,
            body: rejected(),
            retry_after: None,
            streaming: false,
        })
    });
    h.script.response(
        Endpoint::Token,
        200,
        tokens(
            "synthetic-account",
            NOW + 7200,
            Some("synthetic-refresh-rotated"),
        ),
    );
    script.reply(200, success("complete"));
    let out = b.call("http.post", payload()).unwrap();
    assert_eq!(out["billingSource"], "chatgpt-subscription");
    assert_eq!(script.1.load(Ordering::Relaxed), 2);
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 1);
    let records = b.writer.records.clone();
    assert_eq!(
        records
            .iter()
            .filter(|r| r["effect"] == "chatgpt.inference")
            .count(),
        2
    );
    assert_eq!(
        records
            .iter()
            .filter(|r| r["effect"] == "http.post")
            .count(),
        1
    );
    assert_eq!(
        records.last().unwrap()["meta"]["billingSource"],
        "chatgpt-subscription"
    );
    let encoded = json!(records).to_string();
    for sentinel in [
        "REFLECTED-ACCESS-SENTINEL",
        "synthetic-account",
        "synthetic-refresh",
        "synthetic-signature",
    ] {
        assert!(!encoded.contains(sentinel));
    }
    let reads = h.store.reads.load(Ordering::Relaxed);
    h.store.panic_reads.store(true, Ordering::Relaxed);
    let mut replay = broker(h.auth.clone(), Arc::new(InferenceScript::default()));
    replay.mode = Mode::Replay;
    replay.cursor = Some(ReplayCursor::new(&records));
    assert_eq!(replay.call("http.post", payload()).unwrap(), out);
    assert_eq!(h.store.reads.load(Ordering::Relaxed), reads);
    assert_eq!(replay.writer.records.len(), records.len());
    assert_eq!(
        replay.call("http.post", payload()).unwrap_err().type_,
        "DivergenceError"
    );
}

#[test]
fn second_rejection_blocks_same_grant_and_failure_replays_without_authorization() {
    let (h, script, mut b) = setup();
    script.reply(401, rejected());
    script.reply(401, rejected());
    h.script.response(
        Endpoint::Token,
        200,
        tokens(
            "synthetic-account",
            NOW + 7200,
            Some("synthetic-refresh-rotated"),
        ),
    );
    assert!(b.call("http.post", payload()).unwrap()["body"]
        .as_str()
        .unwrap()
        .contains("grant_rejected"));
    assert_eq!(
        h.auth.get_connection().state,
        ConnectionState::ReauthRequired
    );
    assert_eq!(script.1.load(Ordering::Relaxed), 2);
    let reads = h.store.reads.load(Ordering::Relaxed);
    h.store.panic_reads.store(true, Ordering::Relaxed);
    let mut replay = broker(h.auth.clone(), Arc::new(InferenceScript::default()));
    replay.mode = Mode::Replay;
    replay.cursor = Some(ReplayCursor::new(&b.writer.records));
    assert_eq!(replay.call("http.post", payload()).unwrap()["status"], 401);
    assert_eq!(reads, h.store.reads.load(Ordering::Relaxed));
}

#[test]
fn quota_policy_unknown_auth_and_stream_auth_never_refresh_or_reflect() {
    for (status, code, expected, streaming) in [
        (401, "unknown", 401, false),
        (403, "policy_violation", 403, false),
        (429, "insufficient_quota", 402, false),
        (403, "residency_mismatch", 403, false),
        (429, "rate_limit_exceeded", 429, false),
        (401, "token_expired", 401, true),
    ] {
        let (h, script, mut b) = setup();
        script.add(move |_, _, _| {
            Ok(WireReply {
                status,
                streaming,
                retry_after: Some(u64::MAX),
                body: json!({"error": {"code": code, "message": "REFLECTED-SENTINEL"}}).to_string(),
            })
        });
        let out = b.call("http.post", payload()).unwrap();
        assert_eq!(out["status"], expected);
        assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
        assert_eq!(h.auth.get_connection().state, ConnectionState::Connected);
        assert!(!json!(b.writer.records)
            .to_string()
            .contains("REFLECTED-SENTINEL"));
        assert_eq!(out["headers"]["retry-after"], "60");
    }
    for kind in ["error", "response.failed", "response.incomplete"] {
        let (h, script, mut b) = setup();
        let raw = json!({"code": "policy_violation", "message": "REFLECTED-SENTINEL"});
        script.reply(200, format!("data: {}\n\n", json!({"type": kind, "error": raw, "response": {"error": raw, "incomplete_details": {"reason": "policy"}}})));
        assert_eq!(b.call("http.post", payload()).unwrap()["status"], 403);
        assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
        assert!(!json!(b.writer.records)
            .to_string()
            .contains("REFLECTED-SENTINEL"));
    }
}

#[test]
fn peer_rotation_adopts_pair_but_replacement_never_gets_a_resend() {
    for replace in [false, true] {
        let (h, script, mut b) = setup();
        let store = h.store.clone();
        script.add(move |_, _, _| {
            let mut slot = store.value.lock().unwrap();
            let old = slot.as_ref().unwrap().grant.as_ref().unwrap().clone();
            let next = parse_grant(
                &tokens("synthetic-account", NOW + 7200, Some("peer-refresh")),
                NOW,
                if replace { None } else { Some(&old) },
            )
            .unwrap();
            slot.as_mut().unwrap().grant = Some(next);
            Ok(WireReply {
                status: 401,
                body: rejected(),
                retry_after: None,
                streaming: false,
            })
        });
        if replace {
            assert_eq!(
                b.call("http.post", payload()).unwrap_err().type_,
                "account_changed"
            );
            assert_eq!(script.1.load(Ordering::Relaxed), 1);
        } else {
            script.reply(200, success("adopted"));
            assert_eq!(b.call("http.post", payload()).unwrap()["status"], 200);
            assert_eq!(script.1.load(Ordering::Relaxed), 2);
        }
        assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
    }
}

#[test]
fn late_stop_session_end_and_deadline_discard_success_with_one_send() {
    for which in ["stop", "session", "deadline"] {
        let (h, script, mut b) = setup();
        let auth = h.auth.clone();
        let signal = b.interrupt.clone();
        let mut p = payload();
        if which == "deadline" {
            p["timeout"]["total"] = json!(0.02);
        }
        script.add(move |_, _, _| {
            match which {
                "stop" => signal.store(true, Ordering::Release),
                "session" => auth.shutdown(),
                _ => std::thread::sleep(Duration::from_millis(35)),
            }
            Ok(WireReply {
                status: 200,
                body: success("LATE-SENTINEL"),
                retry_after: None,
                streaming: true,
            })
        });
        assert!(b.call("http.post", p).is_err());
        assert_eq!(script.1.load(Ordering::Relaxed), 1);
        assert!(!json!(b.writer.records)
            .to_string()
            .contains("LATE-SENTINEL"));
    }
}

#[test]
fn expired_and_stopped_requests_do_not_start_authorization_or_inference() {
    for stop in [true, false] {
        let (h, script, mut b) = setup();
        let reads = h.store.reads.load(Ordering::Relaxed);
        if stop {
            b.interrupt.store(true, Ordering::Release);
        } else {
            b.deadline = Some(Instant::now());
        }
        assert!(b.call("http.post", payload()).is_err());
        assert_eq!(script.1.load(Ordering::Relaxed), 0);
        assert_eq!(reads, h.store.reads.load(Ordering::Relaxed));
    }
}

#[test]
fn reflected_errors_never_spill_into_the_trace_store() {
    let (_h, script, mut b) = setup();
    let store = MemTraceStore::new();
    b.writer.stream_to(&store).unwrap();
    script.reply(
        401,
        json!({"error": {"message": "REFLECTED-SENTINEL".repeat(10000)}}).to_string(),
    );
    let mut p = payload();
    p["json"]["instructions"] = json!("safe".repeat(30000));
    b.call("http.post", p).unwrap();
    b.writer.dump(&store).unwrap();
    let records = store.load_resolved("run_inference").unwrap();
    assert!(!json!(records).to_string().contains("REFLECTED-SENTINEL"));
    for blob in store.blobs("run_inference").unwrap().values() {
        assert!(!blob.contains("REFLECTED-SENTINEL"));
    }
}

#[test]
fn completion_lock_contention_retains_success_without_resending() {
    let (h, script, mut b) = setup();
    let auth = h.auth.clone();
    script.add(move |_, _, _| {
        let (sent, wait) = mpsc::channel();
        std::thread::spawn(move || {
            let storage = auth.storage().unwrap();
            let _held = storage
                .lock
                .acquire(Duration::from_secs(1), || false)
                .unwrap();
            sent.send(()).unwrap();
            std::thread::sleep(Duration::from_millis(5200));
        });
        wait.recv_timeout(Duration::from_secs(2)).unwrap();
        Ok(WireReply {
            status: 200,
            body: success("retained"),
            retry_after: None,
            streaming: true,
        })
    });
    let out = b.call("http.post", payload()).unwrap();
    assert!(out["body"].as_str().unwrap().contains("retained"));
    assert_eq!(script.1.load(Ordering::Relaxed), 1);
}

#[test]
fn refresh_that_crosses_deadline_settles_without_inference() {
    let (h, script, mut b) = setup();
    seed(&h.store, NOW + 1);
    h.script.add(Box::new(|endpoint, _| {
        assert_eq!(endpoint, Endpoint::Token);
        std::thread::sleep(Duration::from_millis(40));
        Ok(AuthReply {
            status: 200,
            body: tokens("synthetic-account", NOW + 7200, Some("late-refresh-pair")),
            retry_after: None,
        })
    }));
    let mut p = payload();
    p["timeout"]["total"] = json!(0.02);
    assert_eq!(b.call("http.post", p).unwrap_err().type_, "request_expired");
    assert_eq!(script.1.load(Ordering::Relaxed), 0);
    let value = h.store.value.lock().unwrap();
    let grant = value.as_ref().unwrap().grant.as_ref().unwrap();
    assert_eq!(grant.refresh.0, "late-refresh-pair");
    assert!(matches!(grant.state, GrantState::Ready));
}

#[test]
fn actual_dependency_trace_logging_never_renders_sensitive_header_values() {
    struct Capture(Mutex<Vec<String>>);
    static LOGS: Capture = Capture(Mutex::new(Vec::new()));
    impl log::Log for Capture {
        fn enabled(&self, _: &log::Metadata<'_>) -> bool {
            true
        }
        fn log(&self, record: &log::Record<'_>) {
            if record.target().starts_with("reqwest") || record.target().starts_with("hyper") {
                self.0
                    .lock()
                    .unwrap()
                    .push(format!("{} {}", record.target(), record.args()));
            }
        }
        fn flush(&self) {}
    }
    log::set_logger(&LOGS).unwrap();
    log::set_max_level(log::LevelFilter::Trace);
    let (h, script, mut b) = setup();
    {
        let mut env = h.store.value.lock().unwrap();
        env.as_mut().unwrap().grant.as_mut().unwrap().residency =
            Some("RESIDENCY-HEADER-SENTINEL".into());
    }
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let target = format!("http://{}", server.server_addr());
    let receive = std::thread::spawn(move || {
        let request = server.recv().unwrap();
        let header = |name: &str| {
            request
                .headers()
                .iter()
                .find(|h| h.field.as_str().as_str().eq_ignore_ascii_case(name))
                .unwrap()
                .value
                .as_str()
        };
        assert_eq!(header("ChatGPT-Account-ID"), "synthetic-account");
        assert_eq!(
            header("x-openai-internal-codex-residency"),
            "RESIDENCY-HEADER-SENTINEL"
        );
        let response = tiny_http::Response::from_string("REFLECTED-BODY-SENTINEL").with_header(
            tiny_http::Header::from_bytes("x-reflection", "REFLECTED-HEADER-SENTINEL").unwrap(),
        );
        request.respond(response).unwrap();
    });
    script.add(move |body, headers, _| {
        let debug = format!("{headers:?}");
        assert!(!debug.contains("RESIDENCY-HEADER-SENTINEL"));
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(async {
            // Private synthetic transport only; production always posts ENDPOINT over HTTPS.
            let client = crate::broker::chatgpt::client_builder()
                .https_only(false)
                .build()
                .unwrap();
            let response = client
                .post(target)
                .headers(headers)
                .body(body.to_owned())
                .send()
                .await
                .unwrap();
            assert_eq!(response.text().await.unwrap(), "REFLECTED-BODY-SENTINEL");
        });
        Ok(WireReply {
            status: 200,
            body: success("logged safely"),
            retry_after: None,
            streaming: true,
        })
    });
    b.call("http.post", payload()).unwrap();
    receive.join().unwrap();
    let logs = LOGS.0.lock().unwrap().join("\n");
    assert!(
        !logs.is_empty(),
        "dependency logging must actually be enabled"
    );
    for forbidden in [
        "synthetic-account",
        "synthetic-signature",
        "RESIDENCY-HEADER-SENTINEL",
        "REFLECTED-HEADER-SENTINEL",
        "REFLECTED-BODY-SENTINEL",
    ] {
        assert!(!logs.contains(forbidden), "transport logged {forbidden}");
    }
}

#[test]
fn hosted_records_keep_span_counters_and_mocked_subscription_metadata() {
    let (h, script, mut b) = setup();
    script.reply(200, success("one call"));
    b.try_span_begin("llm.chat", None, json!({})).unwrap();
    let out = b.call("http.post", payload()).unwrap();
    let meta = b.span_end(true, Some(out.clone()), None).unwrap();
    assert_eq!(meta["effects"], 3);
    let mut replay = broker(h.auth.clone(), Arc::new(InferenceScript::default()));
    h.store.panic_reads.store(true, Ordering::Relaxed);
    replay.mode = Mode::Replay;
    replay.cursor = Some(ReplayCursor::new(&b.writer.records));
    replay.try_span_begin("llm.chat", None, json!({})).unwrap();
    assert_eq!(replay.call("http.post", payload()).unwrap(), out);
    assert_eq!(
        replay.span_end(true, Some(out), None).unwrap()["effects"],
        3
    );
    let record = b
        .writer
        .records
        .iter()
        .find(|r| r["effect"] == "http.post")
        .unwrap();
    let mut mocked = broker(h.auth.clone(), Arc::new(InferenceScript::default()));
    mocked.mode = Mode::Mock;
    mocked.mock_index = Some(
        mocked
            .build_mock_index(&json!({"records": [record]}))
            .unwrap(),
    );
    mocked.call("http.post", payload()).unwrap();
    assert_eq!(
        mocked.writer.records.last().unwrap()["meta"]["billingSource"],
        "chatgpt-subscription"
    );
}

#[test]
fn success_shaped_secret_reflection_is_rejected_before_trace_or_blob() {
    let (_h, script, mut b) = setup();
    script.add(|_, headers, _| {
        Ok(WireReply {
            status: 200,
            body: success(
                headers["authorization"]
                    .to_str()
                    .unwrap()
                    .strip_prefix("Bearer ")
                    .unwrap(),
            ),
            retry_after: None,
            streaming: true,
        })
    });
    assert_eq!(
        b.call("http.post", payload()).unwrap_err().type_,
        "chatgpt_response_rejected"
    );
    assert!(!json!(b.writer.records)
        .to_string()
        .contains("synthetic-signature"));
}

#[test]
fn escaped_reflections_and_duplicate_keys_never_reach_guest_or_spilled_trace() {
    for variant in [
        "access",
        "account",
        "key",
        "duplicate",
        "arguments",
        "argument_duplicate",
    ] {
        let (h, script, mut b) = setup();
        let store = MemTraceStore::new();
        b.writer.stream_to(&store).unwrap();
        let captured = Arc::new(Mutex::new(None));
        let capture = captured.clone();
        script.add(move |_, headers, _| {
            let secret = if variant == "account" {
                headers["chatgpt-account-id"].to_str().unwrap()
            } else {
                headers["authorization"].to_str().unwrap().strip_prefix("Bearer ").unwrap()
            };
            let escaped: String = secret.chars().map(|ch| format!("\\u{:04x}", ch as u32)).collect();
            let body = match variant {
                "key" => success("safe").replacen("data: {", &format!("data: {{\"{escaped}\":false,"), 1),
                "duplicate" => success("safe").replacen("data: {", &format!("data: {{\"shadow\":\"{escaped}\",\"shadow\":null,"), 1),
                "arguments" | "argument_duplicate" => {
                    let arguments = if variant == "arguments" {
                        format!("{{\"code\":\"{escaped}\"}}")
                    } else {
                        format!("{{\"code\":\"{escaped}\",\"code\":\"safe\"}}")
                    };
                    let item = json!({"type":"function_call", "status":"completed", "id":"lookup_call", "call_id":"real_call", "name":"run_cell", "arguments":arguments});
                    [json!({"type":"response.created", "response":{"id":"r1", "status":"in_progress"}}),
                        json!({"type":"response.output_item.added", "output_index":0, "item":item}),
                        json!({"type":"response.output_item.done", "output_index":0, "item":item}),
                        json!({"type":"response.completed", "response":{"id":"r1", "status":"completed", "output":[item]}})]
                        .iter().map(|v| format!("data: {v}\n\n")).collect()
                }
                _ => success(&format!("{} {secret}", "benign text ".repeat(10000))).replace(secret, &escaped),
            };
            assert!(!body.contains(secret));
            *capture.lock().unwrap() = Some((secret.to_owned(), escaped));
            Ok(WireReply { status:200, body, retry_after:None, streaming:true })
        });
        let source = ONE_CHAT.replace("'private history'", "'private history' * 8000");
        let mut result = run_guest(b, &source);
        assert_eq!(result.status, "error", "{variant}: {:?}", result.value);
        assert!(
            result
                .error
                .as_ref()
                .unwrap()
                .to_string()
                .contains("chatgpt_response_rejected"),
            "{variant}: {:?}",
            result.error
        );
        assert_eq!(script.1.load(Ordering::Relaxed), 1);
        result.broker.writer.dump(&store).unwrap();
        assert!(store.text_blob_count() > 0, "exercise actual spill storage");
        let records = store.load_resolved("run_inference").unwrap();
        for name in ["chatgpt.inference", "http.post"] {
            let record = records.iter().find(|r| r["effect"] == name).unwrap();
            assert!(record["output"].is_null());
            assert_eq!(record["error"]["type"], "chatgpt_response_rejected");
        }
        let (secret, escaped) = captured.lock().unwrap().clone().unwrap();
        let escaped_json = serde_json::to_string(&escaped).unwrap();
        for raw in store
            .blobs("run_inference")
            .unwrap()
            .values()
            .chain(std::iter::once(&json!(records).to_string()))
        {
            assert!(!raw.contains(&secret));
            assert!(!raw.contains(&escaped));
            assert!(!raw.contains(&escaped_json[1..escaped_json.len() - 1]));
        }
        replay_guest(&h, &result, &source);
    }
}

#[test]
fn short_residency_codes_do_not_reject_normal_assistant_text() {
    let (h, script, b) = setup();
    h.store
        .value
        .lock()
        .unwrap()
        .as_mut()
        .unwrap()
        .grant
        .as_mut()
        .unwrap()
        .residency = Some("us".into());
    script.reply(200, success("Discuss us and eu options."));
    let result = run_guest(b, ONE_CHAT);
    assert_eq!(result.status, "ok", "{:?}", result.error);
    assert_eq!(
        result.value["parts"][0]["text"],
        "Discuss us and eu options."
    );
}

#[test]
fn multiline_sse_reasoning_is_hidden_in_loose_and_nested_semantic_views() {
    let body = "event: response.output_item.done\r\ndata: {\"type\":\"response.output_item.done\",\r\ndata: \"item\":{\"type\":\"reasoning\",\r\ndata: \"encrypted_content\":\"OPAQUE-MULTILINE-SENTINEL\",\"summary\":[]}}\r\n\r\n";
    for nested in [false, true] {
        let (_h, script, mut b) = setup();
        let store = MemTraceStore::new();
        b.writer.stream_to(&store).unwrap();
        if nested {
            b.try_span_begin("utility.inspect", None, json!({}))
                .unwrap();
        }
        // Legal SSE comments and retry hints can precede the first data frame.
        let body = if nested {
            format!(": keepalive\r\nretry: 1000\r\n\r\n{body}")
        } else {
            body.into()
        };
        script.reply(200, body.clone());
        b.call("http.post", payload()).unwrap();
        if nested {
            b.span_end(true, Some(json!({})), None).unwrap();
        }
        b.writer.dump(&store).unwrap();
        let rendered = crate::view::render(
            &store,
            "run_inference",
            &crate::view::ShowOpts {
                full: true,
                ..Default::default()
            },
        )
        .unwrap();
        assert!(
            !rendered.contains("OPAQUE-MULTILINE-SENTINEL"),
            "{rendered}"
        );
        assert!(rendered.contains("[opaque reasoning]"));
        let records = store.load_resolved("run_inference").unwrap();
        let record = records.iter().find(|r| r["effect"] == "http.post").unwrap();
        assert_eq!(record["output"]["body"], body);
        assert!(
            crate::view::show_record(&store, "run_inference", record["seq"].as_i64().unwrap())
                .unwrap()
                .contains("OPAQUE-MULTILINE-SENTINEL")
        );
    }
}

fn wire_events(events: &[Value]) -> String {
    events
        .iter()
        .map(|e| format!("event: {}\ndata: {e}\n\n", e["type"].as_str().unwrap()))
        .collect()
}

fn limit_events(arguments: &str, done: bool, text: &str) -> Vec<Value> {
    let message = json!({"id":"limit_message", "type":"message", "role":"assistant", "status":"completed", "phase":"final_answer", "content":[{"type":"output_text", "text":text}]});
    let added = json!({"id":"limit_call", "type":"function_call", "status":"in_progress", "call_id":"call_actual", "name":"run_cell", "arguments":""});
    let mut partial = added.clone();
    partial["status"] = json!("incomplete");
    partial["arguments"] = json!(arguments);
    let mut events = vec![
        json!({"type":"response.created", "response":{"id":"limit_response", "status":"in_progress"}}),
        json!({"type":"response.output_item.added", "output_index":0, "item":message}),
        json!({"type":"response.output_item.done", "output_index":0, "item":message}),
        json!({"type":"response.output_item.added", "output_index":1, "item":added}),
        json!({"type":"response.function_call_arguments.delta", "output_index":1, "item_id":"limit_call", "delta":arguments}),
    ];
    if done {
        events.push(json!({"type":"response.function_call_arguments.done", "output_index":1, "item_id":"limit_call", "arguments":arguments}));
        events.push(json!({"type":"response.output_item.done", "output_index":1, "item":partial}));
    }
    events.push(json!({"type":"response.incomplete", "response":{"id":"limit_response", "status":"incomplete", "incomplete_details":{"reason":"max_output_tokens"}, "output":[message,partial], "usage":{"input_tokens":25, "output_tokens":12, "input_tokens_details":{"cached_tokens":20}}}}));
    events
}

fn assert_limit_once(
    h: &Harness,
    script: &InferenceScript,
    result: &crate::runner::RunOutcome,
    text: &str,
) {
    assert_eq!(result.status, "ok", "{:?}", result.error);
    assert_eq!(logical_spans(result), 1);
    assert_eq!(result.value["stop"], "length");
    assert_eq!(
        result.value["parts"],
        json!([{"type":"text", "text":text, "phase":"final_answer"}])
    );
    assert_eq!(
        result.value["usage"],
        json!({"in":5, "out":12, "cacheRead":20, "cacheWrite":0, "billingSource":"chatgpt-subscription"})
    );
    assert_eq!(script.1.load(Ordering::Relaxed), 1);
    assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
    assert!(!result
        .broker
        .writer
        .records
        .iter()
        .any(|r| r["effect"] == "sleep"));
}

#[test]
fn real_guest_partial_output_limit_keeps_text_usage_without_retry_or_lifted_tools() {
    for (profile, done, text) in [
        ("generic", false, "legitimate partial answer"),
        (
            "fenced",
            true,
            "```cell\nraise RuntimeError('must not execute')\n```",
        ),
        (
            "xml",
            false,
            "<tool_call>{\"name\":\"run_cell\",\"args\":{\"code\":\"explode()\"}}</tool_call>",
        ),
    ] {
        let (h, script, mut b) = setup();
        let mut events = limit_events("{\"code\":\"PARTIAL-ARGUMENT-SENTINEL", done, text);
        events[4]["response"] = json!(7); // unrelated malformed extra field must not panic normalization
        script.reply(200, wire_events(&events));
        let config = json!({"provider":"openai-responses", "backend":"openai-codex", "model":"gpt-synthetic", "base_url":"https://chatgpt.com/backend-api/codex", "api_key_ref":crate::local_grant::CHATGPT_REF, "timeout":30, "profile":"generic"});
        b.config.insert("llm.tier.codegen".into(), config);
        let source = format!("def main(args):\n    llm = use('llm@v1')\n    reply = llm.chat([{{'role':'user','parts':[{{'type':'text','text':'private history'}}]}}], tools=[{{'name':'run_cell','description':'synthetic','input_schema':{{'type':'object'}}}}])\n    return llm._lift(reply, {{**llm.profile()['traits'], 'tool_mode': '{profile}'}})\n");
        let result = run_guest(b, &source);
        assert_limit_once(&h, &script, &result, text);
        assert!(!json!(result.broker.writer.records)
            .to_string()
            .contains("PARTIAL-ARGUMENT-SENTINEL"));
        replay_guest(&h, &result, &source);
    }
}

#[test]
fn discarded_limit_arguments_never_reach_real_guest_or_spilled_trace() {
    for (account, shape) in [(false, "complete"), (true, "partial"), (false, "duplicate")] {
        let (h, script, mut b) = setup();
        let store = MemTraceStore::new();
        b.writer.stream_to(&store).unwrap();
        let captured = Arc::new(Mutex::new(None));
        let capture = captured.clone();
        script.add(move |_, headers, _| {
            let secret = if account {
                headers["chatgpt-account-id"].to_str().unwrap()
            } else {
                headers["authorization"]
                    .to_str()
                    .unwrap()
                    .strip_prefix("Bearer ")
                    .unwrap()
            };
            let escaped: String = secret
                .chars()
                .map(|ch| format!("\\u{:04x}", ch as u32))
                .collect();
            let arguments = match shape {
                "partial" => format!("{{\"code\":\"{escaped}"),
                "duplicate" => format!("{{\"code\":\"{escaped}\",\"code\":\"safe\"}}"),
                _ => format!("{{\"code\":\"{escaped}\"}}"),
            };
            let mut events = limit_events("", false, "kept safe text");
            events[0]["response"]["padding"] = json!("safe ".repeat(20000));
            // Split inside the known argument JSON layer, including escape sequences.
            let split = arguments.len() / 2 + 1;
            events[4]["delta"] = json!(&arguments[..split]);
            let mut second = events[4].clone();
            second["delta"] = json!(&arguments[split..]);
            events.insert(5, second);
            let body = wire_events(&events);
            assert!(!body.contains(secret));
            *capture.lock().unwrap() = Some((secret.to_owned(), escaped));
            Ok(WireReply {
                status: 200,
                body,
                retry_after: None,
                streaming: true,
            })
        });
        let mut result = run_guest(b, ONE_CHAT);
        assert_limit_once(&h, &script, &result, "kept safe text");
        result.broker.writer.dump(&store).unwrap();
        assert!(store.text_blob_count() > 0);
        let records = store.load_resolved("run_inference").unwrap();
        for name in ["chatgpt.inference", "http.post"] {
            let record = records.iter().find(|r| r["effect"] == name).unwrap();
            let body = record["output"]["body"].as_str().unwrap();
            for event in body
                .lines()
                .filter_map(|l| l.strip_prefix("data: "))
                .map(|s| serde_json::from_str::<Value>(s).unwrap())
            {
                if event["type"] == "response.function_call_arguments.delta" {
                    assert_eq!(event["delta"], "");
                }
                if event["item"]["type"] == "function_call" {
                    assert_eq!(event["item"]["arguments"], "");
                }
                if event["type"] == "response.incomplete" {
                    assert_eq!(event["response"]["output"][1]["arguments"], "");
                }
            }
        }
        let (secret, escaped) = captured.lock().unwrap().clone().unwrap();
        for raw in store
            .blobs("run_inference")
            .unwrap()
            .values()
            .chain(std::iter::once(&json!(records).to_string()))
        {
            assert!(!raw.contains(&secret));
            assert!(!raw.contains(&escaped));
            assert!(
                !raw.contains("\\u0065"),
                "discard the encoded fragments too"
            );
        }
        replay_guest(&h, &result, ONE_CHAT);
    }
}

#[test]
fn retained_split_text_and_refusal_credentials_are_rejected_before_guest_or_spills() {
    for (field, account) in [
        ("output_text", false),
        ("output_text", true),
        ("refusal", false),
        ("refusal", true),
    ] {
        let (h, script, mut b) = setup();
        let store = MemTraceStore::new();
        b.writer.stream_to(&store).unwrap();
        script.add(move |_, headers, _| {
            let secret = if account { headers["chatgpt-account-id"].to_str().unwrap() } else {
                headers["authorization"].to_str().unwrap().strip_prefix("Bearer ").unwrap()
            };
            let mut events = limit_events("", false, "safe terminal text");
            let split = secret.len()/2;
            let pieces = [&secret[..split], &secret[split..]];
            for (i, piece) in pieces.iter().enumerate() {
                events.insert(2+i, json!({"type":format!("response.{field}.delta"), "output_index":0, "content_index":0, "item_id":"limit_message", "delta":piece}));
            }
            // Incomplete terminals need not repeat earlier fields, and the original
            // codec accepts no item.done here. The earlier fragments still need inspection.
            events.remove(4);
            let mut body = wire_events(&events);
            for piece in pieces {
                let escaped: String = piece.chars().map(|ch| format!("\\u{:04x}", ch as u32)).collect();
                body = body.replace(&serde_json::to_string(piece).unwrap(), &format!("\"{escaped}\""));
            }
            assert!(!body.contains(secret));
            Ok(WireReply { status:200, body, retry_after:None, streaming:true })
        });
        let source = ONE_CHAT.replace("'private history'", "'private history' * 8000");
        let mut result = run_guest(b, &source);
        assert_eq!(result.status, "error");
        assert!(result
            .error
            .as_ref()
            .unwrap()
            .to_string()
            .contains("chatgpt_response_rejected"));
        assert_eq!(script.1.load(Ordering::Relaxed), 1);
        assert_eq!(h.script.calls.load(Ordering::Relaxed), 0);
        result.broker.writer.dump(&store).unwrap();
        assert!(store.text_blob_count() > 0);
        let records = store.load_resolved("run_inference").unwrap();
        for name in ["chatgpt.inference", "http.post"] {
            let record = records.iter().find(|r| r["effect"] == name).unwrap();
            assert!(record["output"].is_null());
            assert_eq!(record["error"]["type"], "chatgpt_response_rejected");
        }
        assert!(!json!(records).to_string().contains("synthetic-signature"));
        replay_guest(&h, &result, &source);
    }
}

#[test]
fn limit_normalization_does_not_repair_argument_equality_or_wire_identity() {
    for mutation in [
        "field_done",
        "item_done",
        "item_id",
        "item_order",
        "terminal",
        "event_name",
        "duplicate_key",
    ] {
        let (_h, script, mut b) = setup();
        let mut events = limit_events("{\"code\":\"partial", true, "safe text");
        match mutation {
            "field_done" => events[5]["arguments"] = json!("different"),
            "item_done" => events[6]["item"]["arguments"] = json!("different"),
            "item_id" => events[4]["item_id"] = json!("foreign"),
            "item_order" => events[3]["output_index"] = json!(5),
            "terminal" => events.push(events.last().unwrap().clone()),
            _ => (),
        }
        let mut body = wire_events(&events);
        if mutation == "event_name" {
            body = body.replacen("event: response.created", "event: foreign", 1);
        }
        if mutation == "duplicate_key" {
            body = body.replacen("data: {", "data: {\"x\":1,\"x\":2,", 1);
        }
        script.reply(200, body);
        assert!(b.call("http.post", payload()).is_err(), "{mutation}");
    }
}

#[test]
fn split_reasoning_summary_reflection_is_rejected_before_recording() {
    let (h, script, mut b) = setup();
    let grant = h
        .store
        .value
        .lock()
        .unwrap()
        .as_ref()
        .unwrap()
        .grant
        .as_ref()
        .unwrap()
        .clone();
    let access = &grant.access.0;
    let half = access.len() / 2;
    let events = vec![
        json!({"type":"response.created","response":{"id":"r","status":"in_progress"}}),
        json!({"type":"response.output_item.added","output_index":0,"item":{"id":"reason","type":"reasoning","summary":[]}}),
        json!({"type":"response.reasoning_summary_text.delta","output_index":0,"item_id":"reason","summary_index":0,"delta":&access[..half]}),
        json!({"type":"response.reasoning_summary_text.delta","output_index":0,"item_id":"reason","summary_index":0,"delta":&access[half..]}),
    ];
    script.reply(200, wire_events(&events));
    assert_eq!(
        b.call("http.post", payload()).unwrap_err().type_,
        "chatgpt_response_rejected"
    );
    let trace = json!(b.writer.records).to_string();
    assert!(!trace.contains(&access[..half]));
    assert!(!trace.contains(&access[half..]));
}
