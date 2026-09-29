//! ADR-030: the real WASM boundary and Bao's tool executor with a fake model.
//! No server, provider CLI, credentials, network request or deployment.

use any_ai::{CancelToken, FinishReason, GenerateRequest, GenerateResponse, GeneratedContent};
use anyrt::{
    ai::AiRuntime,
    broker::{Broker, Mode},
    replay::ReplayCursor,
    resolver::{InlineResolver, LocalDirResolver, ModuleResolver, ResolveError},
    routes::Classifier,
    trace::TraceWriter,
    AiService, Cage, Services,
};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc,
    },
};

const PROGRAM: &str = r#"
llm = use("local_ai:llm@v2")
loop = use("local_ai:toolcaller@v2")

def main(args):
    messages = [{"role": "user", "parts": [{"type": "text", "text": "Calculate 6 * 7"}]}]
    tools = [loop.RUN_CELL_TOOL]
    first = llm.chat(messages, tier="codegen", tools=tools)
    results = []
    malformed = loop._run_model_cells(first["parts"], results, {"run_cell"})
    messages.append({"role": "assistant", "parts": first["parts"]})
    messages.append({"role": "user", "parts": results})
    final = llm.chat(messages, tier="codegen", tools=tools)
    return {"first": first, "results": results, "malformed": malformed, "final": final}
"#;

struct ScriptedModel(AtomicUsize);

impl AiService for ScriptedModel {
    fn generate(
        &self,
        request: &GenerateRequest,
        _: &CancelToken,
    ) -> any_ai::Result<GenerateResponse> {
        assert_eq!(request.harness.as_deref(), Some("codex"));
        let value = match self.0.fetch_add(1, Ordering::SeqCst) {
            0 => {
                json!({"kind":"tools","text":"Calculating", "calls":[{"name":"run_cell", "arguments":"{\"code\":\"6 * 7\"}"}]})
            }
            1 => {
                let input: Value =
                    serde_json::from_str(&request.messages.last().unwrap().content).unwrap();
                assert_eq!(input["parts"][0]["type"], "tool_result");
                assert_eq!(input["parts"][0]["is_error"], false);
                assert!(input["parts"][0]["content"]
                    .as_str()
                    .unwrap()
                    .contains("42"));
                json!({"kind":"final","text":"42", "calls":[]})
            }
            _ => panic!("unexpected retry or extra model turn"),
        };
        Ok(GenerateResponse {
            harness: "codex".into(),
            model: Some("test-model".into()),
            content: GeneratedContent::Json { value },
            finish_reason: FinishReason::Other,
            usage: None,
        })
    }
}

// Keep test repos separate so an accidental unqualified shared import fails.
struct RepoResolver {
    agent: LocalDirResolver,
    local_ai: LocalDirResolver,
}

impl ModuleResolver for RepoResolver {
    fn resolve(&mut self, spec: &str, frm: Option<&str>) -> Result<Value, ResolveError> {
        let (alias, name) = spec.split_once(':').unwrap_or_else(|| {
            (
                frm.and_then(|id| id.split_once(':'))
                    .map_or("", |(alias, _)| alias),
                spec,
            )
        });
        let resolver = match alias {
            "agent" => &mut self.agent,
            "local_ai" => &mut self.local_ai,
            _ => return Err(ResolveError::UnknownAlias(alias.into())),
        };
        let mut result = resolver.resolve(name, None)?;
        result["spaceId"] = json!(alias);
        result["objectId"] = json!(format!("{alias}:{name}"));
        Ok(result)
    }
}

fn broker(header: Value, service: Option<Arc<dyn AiService>>) -> Broker {
    let mut b = Broker::new(
        TraceWriter::new(header),
        BTreeMap::from([(
            "llm.tier.codegen".into(),
            json!({"provider":"any-ai","harness":"codex"}),
        )]),
        BTreeMap::new(),
        None,
        Classifier::new(None),
    );
    b.resolver = Some(Box::new(InlineResolver {
        spec: "local-test@v1".into(),
        source: PROGRAM.into(),
        inner: Box::new(RepoResolver {
            agent: LocalDirResolver::new(
                PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../repos/_agent/programs"),
            ),
            local_ai: LocalDirResolver::new(
                PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../repos/_local_ai/programs"),
            ),
        }),
    }));
    b.ai = AiRuntime::new(Services { ai: service });
    b
}

#[test]
fn wasm_bao_tool_cycle_records_and_replays_without_ai_service() {
    let cage = Cage::embedded().unwrap();
    let model = Arc::new(ScriptedModel(AtomicUsize::new(0)));
    let live = anyrt::run_program(
        &cage,
        broker(
            json!({"id":"run_local","program":"local-test@v1"}),
            Some(model.clone()),
        ),
        "local-test@v1",
        &json!({}),
        Default::default(),
        Arc::new(AtomicBool::new(false)),
        60.0,
    )
    .unwrap();
    assert_eq!(live.status, "ok", "{:?}", live.error);
    assert_eq!(live.value["malformed"], 0);
    assert_eq!(live.value["first"]["stop"], "tool");
    assert_eq!(live.value["final"]["parts"][0]["text"], "42");
    assert_eq!(live.value["final"]["stop"], "done");
    assert_eq!(model.0.load(Ordering::SeqCst), 2);
    assert_eq!(
        live.broker
            .writer
            .records
            .iter()
            .filter(|r| r["effect"] == "ai.generate")
            .count(),
        2
    );
    assert!(!live
        .broker
        .writer
        .records
        .iter()
        .any(|r| r["effect"].as_str().is_some_and(|s| s.starts_with("http."))));

    let mut replay = broker(live.broker.writer.records[0]["run"].clone(), None);
    replay.mode = Mode::Replay;
    replay.cursor = Some(ReplayCursor::new(&live.broker.writer.records));
    replay.blobs = live.broker.writer.blobs.iter().cloned().collect();
    let out = anyrt::run_program(
        &cage,
        replay,
        "local-test@v1",
        &json!({}),
        Default::default(),
        Arc::new(AtomicBool::new(false)),
        60.0,
    )
    .unwrap();
    assert_eq!(out.status, "ok", "{:?}", out.error);
    assert_eq!(out.value, live.value);
    assert_eq!(model.0.load(Ordering::SeqCst), 2);
}

struct CancellableModel(std::sync::mpsc::Sender<()>);

impl AiService for CancellableModel {
    fn generate(
        &self,
        _: &GenerateRequest,
        cancel: &CancelToken,
    ) -> any_ai::Result<GenerateResponse> {
        self.0.send(()).unwrap();
        while !cancel.is_cancelled() {
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
        Err(any_ai::Error::Cancelled)
    }
}

#[test]
fn wasm_run_interrupt_reaches_the_injected_model_service() {
    let cage = Cage::embedded().unwrap();
    let (entered, wait) = std::sync::mpsc::channel();
    let interrupt = Arc::new(AtomicBool::new(false));
    let flag = interrupt.clone();
    let breaker = std::thread::spawn(move || {
        wait.recv_timeout(std::time::Duration::from_secs(10))
            .unwrap();
        flag.store(true, Ordering::Release);
    });
    let out = anyrt::run_program(
        &cage,
        broker(
            json!({"id":"run_cancel","program":"local-test@v1"}),
            Some(Arc::new(CancellableModel(entered))),
        ),
        "local-test@v1",
        &json!({}),
        Default::default(),
        interrupt,
        60.0,
    )
    .unwrap();
    breaker.join().unwrap();
    assert_eq!(out.status, "interrupted", "{:?}", out.error);
    assert!(out
        .broker
        .writer
        .records
        .iter()
        .any(|r| r["effect"] == "ai.generate" && r["error"]["type"] == "ai.cancelled"));
}
