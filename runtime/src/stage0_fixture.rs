//! Frozen fixture construction and evidence export for ADR-034 §0.
use crate::broker::{Broker, SpikeDataRoute, SpikePolicy};
use crate::resolver::{FrozenBundleManifest, FrozenBundleResolver};
use crate::runner::RunOutcome;
use crate::trace::TraceWriter;
use crate::tracestore::MemTraceStore;
use anyhow::{ensure, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;

pub const FIXTURE_SPEC: &str = "android-fixture@v1";
pub const MANIFEST_SHA256: &str =
    "sha256:55d8528d3ac597c2a5c2d2cb3ea344d4a8bb480a0f163f4e9d5a903fb503514a";

// Deliberately neither Debug nor Serialize: the key is transient worker input.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TurnRequest {
    pub format: u32,
    pub run_id: String,
    pub fixture_dir: PathBuf,
    pub manifest_sha256: String,
    pub base_url: String,
    pub fixture_space: String,
    pub provider_url: String,
    pub provider_credential: String,
    pub provider_key: String,
    pub config: BTreeMap<String, Value>,
    pub args: Value,
    pub data_routes: Vec<SpikeDataRoute>,
    pub trace_limit_bytes: usize,
}

pub struct PreparedTurn {
    pub broker: Broker,
    pub spec: &'static str,
    pub args: Value,
    pub trace_store: Arc<MemTraceStore>,
    pub reply_url: String,
}

pub fn prepare_turn(request: TurnRequest) -> Result<PreparedTurn> {
    ensure!(request.format == 1, "unsupported fixture request format");
    ensure!(
        !request.run_id.is_empty()
            && request
                .run_id
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c)),
        "invalid run ID"
    );
    ensure!(
        request.manifest_sha256 == MANIFEST_SHA256,
        "untrusted fixture manifest"
    );
    ensure!(
        !request.provider_key.trim().is_empty(),
        "local test provider key required"
    );
    ensure!(
        (1_048_576..=67_108_864).contains(&request.trace_limit_bytes),
        "trace limit must be 1–64 MiB"
    );
    ensure!(
        request.config.keys().all(|k| k == "llm.tier.codegen"),
        "only the frozen codegen tier may be seeded"
    );
    let tier = request
        .config
        .get("llm.tier.codegen")
        .ok_or_else(|| anyhow::anyhow!("explicit codegen tier required"))?;
    ensure!(
        tier["model"].as_str().is_some_and(|s| !s.is_empty()),
        "explicit model required"
    );
    ensure!(
        tier["profile"].as_str().is_some_and(|s| !s.is_empty()),
        "explicit model profile required"
    );
    ensure!(
        tier["api_key_ref"] == request.provider_credential,
        "tier credential mismatch"
    );
    ensure!(
        request.provider_url == "https://api.openai.com/v1/chat/completions"
            && request.provider_credential == "llm.key.openai"
            && tier["provider"] == "openai-compat"
            && tier["backend"] == "openai"
            && tier["base_url"] == "https://api.openai.com/v1",
        "Stage-0 requires the selected OpenAI API endpoint"
    );
    ensure!(
        request
            .data_routes
            .iter()
            .any(|r| r.method == "GET" && r.path == "/v1/health" && r.response_sha256.is_some()),
        "pinned server generation preflight missing"
    );
    ensure!(
        request.args["space"] == request.fixture_space,
        "turn space mismatch"
    );
    ensure!(
        request.args["chatId"]
            .as_str()
            .is_some_and(|s| !s.is_empty()),
        "turn chat required"
    );
    ensure!(
        request.args["userText"]
            .as_str()
            .is_some_and(|s| !s.is_empty()),
        "turn text required"
    );
    ensure!(
        request
            .args
            .as_object()
            .is_some_and(|a| a.keys().all(|k| matches!(
                k.as_str(),
                "space" | "chatId" | "userText" | "codeSpace" | "traceRef"
            ))),
        "unexpected turn argument"
    );
    ensure!(
        request
            .args
            .get("codeSpace")
            .is_none_or(|s| s == &request.args["space"]),
        "turn code space mismatch"
    );
    let manifest_bytes = std::fs::read(request.fixture_dir.join("manifest.json"))?;
    ensure!(
        format!("sha256:{:x}", Sha256::digest(&manifest_bytes)) == MANIFEST_SHA256,
        "fixture manifest bytes changed"
    );
    let manifest: FrozenBundleManifest = serde_json::from_slice(&manifest_bytes)?;
    let resolver = FrozenBundleResolver::new(request.fixture_dir.join("programs"), manifest)?;
    let reply_path = format!(
        "/v1/spaces/{}/objects/{}/chat/messages",
        request.fixture_space,
        request.args["chatId"].as_str().unwrap()
    );
    ensure!(
        request
            .data_routes
            .iter()
            .any(|route| route.method == "POST" && route.path == reply_path),
        "turn chat reply route missing"
    );
    let reply_url = format!("{}{}", request.base_url.trim_end_matches('/'), reply_path);
    let policy = SpikePolicy::new(
        &request.base_url,
        &request.fixture_space,
        &request.provider_url,
        &request.provider_credential,
        request.data_routes,
    )?;
    let store = Arc::new(MemTraceStore::bounded(request.trace_limit_bytes));
    let mut writer = TraceWriter::new(json!({"id": request.run_id, "program": FIXTURE_SPEC,
        "meta": {"mocked": false, "stage0": true, "manifest_sha256": MANIFEST_SHA256}}));
    writer.stream_to(store.as_ref())?;
    let mut broker = Broker::new(
        writer,
        request.config,
        BTreeMap::from([(request.provider_credential, request.provider_key)]),
        None,
        crate::routes::Classifier::new(None),
    );
    broker.trace_store = Some(store.clone());
    broker.runtime.insert("shell".into(), Value::Null);
    broker.set_spike_policy(policy, resolver)?;
    let health = broker
        .call(
            "http.get",
            json!({"url": format!("{}/v1/health", request.base_url.trim_end_matches('/'))}),
        )
        .map_err(|e| anyhow::anyhow!("server preflight: {}", e.message))?;
    let health: Value = serde_json::from_str(health["body"].as_str().unwrap_or(""))?;
    ensure!(
        health["version"]
            .as_str()
            .is_some_and(|s| s.starts_with("any v0.3.5 (commit e7ea90ea,"))
            && health["status"] == "ok"
            && health["crdtVersion"]["supported"] == 2
            && health["crdtVersion"]["stored"] == 2
            && health["crdtVersion"]["newer"] == false,
        "unverified Any version/CRDT state"
    );
    Ok(PreparedTurn {
        broker,
        spec: FIXTURE_SPEC,
        args: request.args,
        trace_store: store,
        reply_url,
    })
}

pub fn finish_turn(
    mut outcome: RunOutcome,
    store: Arc<MemTraceStore>,
    reply_url: &str,
) -> Result<Value> {
    let secrets: Vec<_> = outcome.broker.secrets.values().cloned().collect();
    // Preserve partial evidence even if dump/streaming has exhausted its bound.
    let dump_error = outcome
        .broker
        .writer
        .dump(store.as_ref())
        .err()
        .map(|e| e.to_string());
    let mut result = serde_json::to_value(outcome.fuel)?;
    result.as_object_mut().unwrap().extend(
        json!({
        "status": outcome.status, "value": outcome.value,
        "error": outcome.error, "duration_ms": outcome.duration_ms, "resources": outcome.resources,
        "native_failure": outcome.native_failure, "trace_error": dump_error})
        .as_object()
        .unwrap()
        .clone(),
    );
    drop(outcome.broker);
    let exported = store.drain_export();
    let records: Vec<_> = exported
        .runs
        .values()
        .flatten()
        .map(|r| {
            let mut record = r.clone();
            // TraceWriter spills the fields, not the enclosing record (ADR-001 §7).
            for field in ["input", "output"] {
                if let Some(value) = record.get_mut(field) {
                    *value = crate::replay::resolve_blobs(value.take(), &exported.text_blobs);
                }
            }
            record
        })
        .collect();
    let provider_records: Vec<_> = records
        .iter()
        .filter(|r| {
            r["effect"] == "http.post"
                && r.pointer("/input/url").and_then(Value::as_str)
                    == Some("https://api.openai.com/v1/chat/completions")
        })
        .collect();
    let intervals: Vec<_> = provider_records
        .iter()
        .filter_map(|r| {
            r.pointer("/meta/stage0IntervalMs")
                .and_then(Value::as_array)
        })
        .filter_map(|a| Some((a.first()?.as_f64()?, a.get(1)?.as_f64()?)))
        .collect();
    result["provider_wait_ms"] = json!(interval_union_ms(intervals));
    result["provider_requests"] = json!(provider_records.iter().map(|r| {
        let (response_id, usage) = provider_metadata(r["output"]["body"].as_str().unwrap_or(""));
        json!({"status": r["output"]["status"], "headers": r["output"]["headers"],
            "response_id": response_id, "usage": usage, "interval_ms": r["meta"]["stage0IntervalMs"],
            "error": r["error"]})
    }).collect::<Vec<_>>());
    result["resolved_sources"] = json!(records
        .iter()
        .filter(|r| r["effect"] == "module.resolve")
        .map(|r| json!({"spec": r["input"]["spec"], "source_sha256": r["output"]["sourceHash"]}))
        .collect::<Vec<_>>());
    let anomalies: Vec<_> = records
        .iter()
        .filter(|r| {
            r.get("error").is_some_and(|e| !e.is_null())
                || r.pointer("/output/status")
                    .and_then(Value::as_u64)
                    .is_some_and(|s| s >= 400)
                || r.pointer("/output/status_code")
                    .and_then(Value::as_u64)
                    .is_some_and(|s| s >= 400)
        })
        .cloned()
        .collect();
    let reply = result["value"]["replies"]
        .as_array()
        .and_then(|parts| parts.iter().map(Value::as_str).collect::<Option<Vec<_>>>())
        .map(|parts| parts.join("\n"));
    let nonblank = reply.as_ref().is_some_and(|text| !text.trim().is_empty());
    let reply_post_seq = records.iter().find_map(|r| {
        (nonblank
            && r["kind"] == "effect"
            && r["effect"] == "http.post"
            && r["input"]["url"] == reply_url
            && r["input"]["json"]["text"].as_str() == reply.as_deref()
            && r["input"]["json"]["agent"]["done"] == true
            && r["error"].is_null()
            && r["output"]["status"]
                .as_u64()
                .is_some_and(|s| (200..300).contains(&s))
            && r["meta"]["mocked"] == false
            && r.pointer("/meta/stage0IntervalMs").is_some())
        .then(|| r["seq"].as_i64())
        .flatten()
    });
    result["reply_nonblank"] = json!(nonblank);
    result["reply_post_seq"] = json!(reply_post_seq);
    result["evidence_valid"] = json!(
        result["status"] == "ok"
            && result["trace_error"].is_null()
            && !exported.overflowed
            && anomalies.is_empty()
            && !provider_records.is_empty()
            && provider_records
                .iter()
                .all(|r| r.pointer("/meta/stage0IntervalMs").is_some())
            && result["value"]["logError"].is_null()
            && reply_post_seq.is_some()
    );
    result["anomalies"] = json!(anomalies);
    result["trace"] = serde_json::to_value(exported)?;
    let redactions = redact_secrets(&mut result, &secrets);
    result["credential_redactions"] = json!(redactions);
    if redactions > 0 {
        result["evidence_valid"] = json!(false);
    }
    Ok(result)
}

/// The existing Chat Completions client records the full JSON or SSE body.
/// Only supplied metadata is extracted; interrupted streams can lack usage.
fn provider_metadata(body: &str) -> (Value, Value) {
    let (mut id, mut usage) = (Value::Null, Value::Null);
    let mut read = |data: &str| {
        if let Ok(value) = serde_json::from_str::<Value>(data) {
            if id.is_null() && value["id"].is_string() {
                id = value["id"].clone();
            }
            if value["usage"].is_object() {
                usage = value["usage"].clone();
            }
        }
    };
    if serde_json::from_str::<Value>(body).is_ok() {
        read(body);
    } else {
        let mut data = String::new();
        for line in body.lines().chain(std::iter::once("")) {
            if line.is_empty() {
                read(&data);
                data.clear();
            } else if let Some(value) = line.strip_prefix("data:") {
                data.push_str(value.strip_prefix(' ').unwrap_or(value));
                data.push('\n');
            }
        }
    }
    (id, usage)
}

fn redact_secrets(value: &mut Value, secrets: &[String]) -> usize {
    match value {
        Value::String(text) => {
            let mut count = 0;
            for secret in secrets.iter().filter(|s| !s.is_empty()) {
                count += text.matches(secret.as_str()).count();
                *text = text.replace(secret, "[REDACTED]");
            }
            count
        }
        Value::Array(values) => values.iter_mut().map(|v| redact_secrets(v, secrets)).sum(),
        Value::Object(values) => values
            .values_mut()
            .map(|v| redact_secrets(v, secrets))
            .sum(),
        _ => 0,
    }
}

fn interval_union_ms(mut intervals: Vec<(f64, f64)>) -> f64 {
    intervals.sort_by(|a, b| a.0.total_cmp(&b.0));
    let (mut total, mut until) = (0.0, 0.0_f64);
    for (start, end) in intervals {
        total += (end - start.max(until)).max(0.0);
        until = until.max(end);
    }
    total
}

#[cfg(test)]
mod tests {
    use super::*;

    const PROVIDER_BODY: &str = concat!(
        "data: {\"id\":\"chatcmpl-fixture\",\"usage\":null,\"choices\":[{\"delta\":{\"content\":\"Hello\"}}]}\r\n\r\n",
        "data: {\"id\":\"chatcmpl-fixture\",\"choices\":[],\r\n",
        "data: \"usage\":{\"prompt_tokens\":10,\"completion_tokens\":2,\"total_tokens\":12}}\r\n\r\n",
        "data: [DONE]\r\n\r\n"
    );

    fn outcome(replies: Value) -> RunOutcome {
        let mut writer = TraceWriter::new(json!({"id": "fixture-admission-test"}));
        // Offline provider receipt fixture, never a network provider call.
        writer.effect(
            "http.post",
            Some("main"),
            json!({"url": "https://api.openai.com/v1/chat/completions"}),
            "provider",
            Some(json!({"status": 200, "body": PROVIDER_BODY})),
            None,
            json!({"mocked": false, "stage0IntervalMs": [1.0, 2.0]}),
            None,
        );
        RunOutcome {
            status: "ok".into(),
            duration_ms: 3,
            fuel_used: 4,
            value: json!({"replies": replies}),
            error: None,
            broker: Broker::new(
                writer,
                Default::default(),
                Default::default(),
                None,
                crate::routes::Classifier::new(None),
            ),
            resources: Default::default(),
            native_failure: None,
            fuel: crate::stage0::FuelSample::after_call(100, Some(96), true),
        }
    }

    #[test]
    fn provider_metadata_extracts_json_and_sse_final_usage() {
        let expected = (
            json!("chatcmpl-fixture"),
            json!({"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}),
        );
        assert_eq!(provider_metadata(PROVIDER_BODY), expected);
        assert_eq!(
            provider_metadata(&json!({"id": expected.0, "usage": expected.1}).to_string()),
            expected
        );
    }

    #[test]
    fn interrupted_stream_preserves_missing_usage() {
        let body =
            "data: {\"id\":\"chatcmpl-partial\",\"usage\":null}\n\ndata: {\"id\":\"unfinished";
        assert_eq!(
            provider_metadata(body),
            (json!("chatcmpl-partial"), Value::Null)
        );
        assert_eq!(provider_metadata(""), (Value::Null, Value::Null));
        assert_eq!(
            provider_metadata(r#"{"error":{"message":"rejected"}}"#),
            (Value::Null, Value::Null)
        );
    }

    #[test]
    fn spilled_fields_preserve_receipts_and_reject_failed_http_effects() {
        let url = "http://127.0.0.1:1234/v1/spaces/s/objects/c/chat/messages";
        let large_text = "x".repeat(70_000);
        // Exercise inline and spilled provider inputs independently of the output spill.
        for large_input in [false, true] {
            for (provider_status, post_status) in [(200, 201), (429, 201), (200, 500)] {
                let mut out = outcome(json!([large_text]));
                out.broker.writer = TraceWriter::new(json!({"id": "spilled-receipts"}));
                let provider_body = format!(
                    "data: {}\n\n{PROVIDER_BODY}",
                    json!({"id":"chatcmpl-fixture", "choices":[{"delta":{"content":large_text}}]})
                );
                out.broker.writer.effect(
                    "http.post",
                    Some("main"),
                    json!({"url":"https://api.openai.com/v1/chat/completions",
                        "json":{"messages": if large_input { large_text.as_str() } else { "small" }}}),
                    "provider",
                    Some(json!({"status":provider_status, "body":provider_body,
                        "headers":{"x-request-id":"req-fixture"}})),
                    None,
                    json!({"mocked":false,"stage0IntervalMs":[1.0,2.0]}),
                    None,
                );
                let post_seq = out.broker.writer.effect(
                    "http.post",
                    Some("main"),
                    json!({"url":url,"json":{"text":large_text,"agent":{"done":true}}}),
                    "reply",
                    Some(json!({"status":post_status,"body":large_text})),
                    None,
                    json!({"mocked":false,"stage0IntervalMs":[2.0,3.0]}),
                    None,
                );
                out.broker.writer.effect(
                    "module.resolve",
                    Some("main"),
                    json!({"spec":"fixture@v1"}),
                    "source",
                    Some(json!({"source":large_text,"sourceHash":"sha256:fixture"})),
                    None,
                    json!({}),
                    None,
                );
                let result =
                    finish_turn(out, Arc::new(MemTraceStore::bounded(1_048_576)), url).unwrap();
                let provider = &result["provider_requests"][0];
                assert_eq!(provider["status"], provider_status);
                assert_eq!(provider["headers"]["x-request-id"], "req-fixture");
                assert_eq!(provider["response_id"], "chatcmpl-fixture");
                assert_eq!(provider["usage"]["total_tokens"], 12);
                assert_eq!(result["provider_wait_ms"], 1.0);
                assert_eq!(
                    result["resolved_sources"][0]["source_sha256"],
                    "sha256:fixture"
                );
                assert_eq!(
                    result["reply_post_seq"],
                    if post_status == 201 {
                        json!(post_seq)
                    } else {
                        Value::Null
                    }
                );
                let success = provider_status == 200 && post_status == 201;
                assert_eq!(result["evidence_valid"], success);
                assert_eq!(
                    result["anomalies"].as_array().unwrap().len(),
                    usize::from(!success)
                );
                if !success {
                    let failed_status = if provider_status == 429 { 429 } else { 500 };
                    assert_eq!(result["anomalies"][0]["output"]["status"], failed_status);
                }
                let raw = &result["trace"]["runs"]["spilled-receipts"];
                for (index, field) in [(1, "output"), (2, "input"), (2, "output")] {
                    let reference = &raw[index][field];
                    assert!(reference["bytes"].as_u64().unwrap() > 65_536);
                    let hash = reference["__blob"].as_str().unwrap();
                    let text = result["trace"]["text_blobs"][hash].as_str().unwrap();
                    assert_eq!(text.len() as u64, reference["bytes"].as_u64().unwrap());
                    assert_eq!(format!("sha256:{:x}", Sha256::digest(text)), hash);
                }
                assert_eq!(raw[1]["input"]["__blob"].is_string(), large_input);
            }
        }
    }

    #[test]
    fn empty_and_refusal_only_outputs_cannot_be_real_turn_evidence() {
        for replies in [
            json!([]),
            json!([""]),
            json!([" \n\t"]),
            Value::Null,
            json!([null]),
        ] {
            let result = finish_turn(
                outcome(replies),
                Arc::new(MemTraceStore::bounded(1_048_576)),
                "http://127.0.0.1:1234/v1/spaces/s/objects/c/chat/messages",
            )
            .unwrap();
            assert_eq!(result["reply_nonblank"], false);
            assert!(result["reply_post_seq"].is_null());
            assert_eq!(result["evidence_valid"], false);
        }
    }

    #[test]
    fn nonblank_reply_requires_matching_live_successful_final_post() {
        let url = "http://127.0.0.1:1234/v1/spaces/s/objects/c/chat/messages";
        let good = json!({"kind":"effect", "effect":"http.post", "seq":2,
            "input":{"url":url,"json":{"text":"Hello","agent":{"done":true}}},
            "output":{"status":200},"error":null,
            "meta":{"mocked":false,"stage0IntervalMs":[2.0,3.0]}});
        // Each superficially successful alternative must still fail admission.
        let mutations = [
            (
                "/input/url",
                json!("http://127.0.0.1:1234/v1/spaces/s/objects/other/chat/messages"),
            ),
            ("/input/json/text", json!("different reply")),
            ("/input/json/agent/done", json!(false)),
            ("/output/status", json!(500)),
            ("/output/status", Value::Null),
            ("/error", json!({"type":"spike_denied"})),
            ("/meta/mocked", json!(true)),
        ];
        for (path, value) in mutations {
            let mut out = outcome(json!(["Hello"]));
            let mut post = good.clone();
            *post.pointer_mut(path).unwrap() = value;
            out.broker.writer.records.push(post);
            let result =
                finish_turn(out, Arc::new(MemTraceStore::bounded(1_048_576)), url).unwrap();
            assert_eq!(result["reply_nonblank"], true);
            assert!(result["reply_post_seq"].is_null(), "{path}");
            assert_eq!(result["evidence_valid"], false, "{path}");
        }
        let result = finish_turn(
            outcome(json!(["Hello"])),
            Arc::new(MemTraceStore::bounded(1_048_576)),
            url,
        )
        .unwrap();
        assert_eq!(result["evidence_valid"], false);
    }

    #[test]
    fn allowed_chat_post_receipt_controls_admission_and_retains_raw_sse() {
        let dir = tempfile::tempdir().unwrap();
        let source = "def main(args): return None\n";
        std::fs::write(dir.path().join("fixture@v1.py"), source).unwrap();
        let manifest = FrozenBundleManifest {
            format: 1,
            modules: BTreeMap::from([(
                "fixture@v1".into(),
                format!("sha256:{:x}", Sha256::digest(source)),
            )]),
            specs: BTreeMap::from([("fixture@v1".into(), "fixture@v1".into())]),
        };
        for status in [201, 500] {
            let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
            let base = format!("http://{}", server.server_addr());
            let path = "/v1/spaces/s/objects/c/chat/messages";
            let url = format!("{base}{path}");
            let handle = std::thread::spawn(move || {
                let mut request = server
                    .recv_timeout(std::time::Duration::from_secs(5))
                    .unwrap()
                    .unwrap();
                assert_eq!(request.method(), &tiny_http::Method::Post);
                assert_eq!(request.url(), path);
                let body: Value = serde_json::from_reader(request.as_reader()).unwrap();
                assert_eq!(body["text"], "Hello");
                request
                    .respond(
                        tiny_http::Response::from_string("{\"messageId\":\"posted\"}")
                            .with_status_code(status),
                    )
                    .unwrap();
            });
            let mut out = outcome(json!(["Hello"]));
            out.broker
                .set_spike_policy(
                    SpikePolicy::new(
                        &base,
                        "s",
                        "https://api.openai.com/v1/chat/completions",
                        "llm.key.openai",
                        vec![SpikeDataRoute {
                            method: "POST".into(),
                            path: path.into(),
                            json_matches: Default::default(),
                            json_exact: None,
                            response_sha256: None,
                        }],
                    )
                    .unwrap(),
                    FrozenBundleResolver::new(dir.path().into(), manifest.clone()).unwrap(),
                )
                .unwrap();
            let response = out
                .broker
                .call(
                    "http.post",
                    json!({"url":url,
                "json":{"text":"Hello","agent":{"done":true}}}),
                )
                .unwrap();
            assert_eq!(response["status"], status);
            handle.join().unwrap();
            let result =
                finish_turn(out, Arc::new(MemTraceStore::bounded(1_048_576)), &url).unwrap();
            assert_eq!(result["evidence_valid"], status == 201);
            assert_eq!(!result["reply_post_seq"].is_null(), status == 201);
            assert_eq!(
                result["provider_requests"][0]["response_id"],
                "chatcmpl-fixture"
            );
            assert_eq!(result["provider_requests"][0]["usage"]["total_tokens"], 12);
            let raw = &result["trace"]["runs"]["fixture-admission-test"][1]["output"]["body"];
            assert_eq!(raw, PROVIDER_BODY);
        }
    }
    #[test]
    fn transient_key_cannot_escape_even_when_server_echoes_it() {
        let mut value =
            json!({"error": "bad test-secret", "trace": {"blob": "header=test-secret"}});
        assert_eq!(redact_secrets(&mut value, &["test-secret".into()]), 2);
        assert!(!value.to_string().contains("test-secret"));
    }
    #[test]
    fn provider_wait_uses_union_not_sum() {
        assert_eq!(
            interval_union_ms(vec![(20.0, 40.0), (0.0, 30.0), (50.0, 60.0)]),
            50.0
        );
    }
}
