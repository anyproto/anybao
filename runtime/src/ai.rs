//! Injected local model generation and its lifecycle (ADR-030).

use crate::broker::EffectFailure;
use any_ai::{AnyAi, CancelToken, GenerateRequest, GenerateResponse};
use serde_json::Value;
use std::collections::BTreeMap;
use std::sync::{atomic::AtomicBool, Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

/// An implementation must honor cancellation/deadlines and return only after
/// its provider processes have been reaped. Diagnostics must stay host-side.
pub trait AiService: Send + Sync {
    fn generate(
        &self,
        request: &GenerateRequest,
        cancel: &CancelToken,
    ) -> any_ai::Result<GenerateResponse>;
}

impl AiService for AnyAi {
    fn generate(
        &self,
        request: &GenerateRequest,
        cancel: &CancelToken,
    ) -> any_ai::Result<GenerateResponse> {
        self.generate_with(request, cancel, |_| {})
    }
}

#[derive(Clone, Default)]
pub struct Services {
    pub ai: Option<Arc<dyn AiService>>,
}

#[derive(Default)]
struct State {
    closed: bool,
    next: u64,
    active: BTreeMap<u64, CancelToken>,
}

/// One admission gate per embedded agent; clones track the same in-flight calls.
#[derive(Clone, Default)]
pub struct AiRuntime {
    service: Option<Arc<dyn AiService>>,
    state: Arc<(Mutex<State>, Condvar)>,
}

struct Permit<'a> {
    runtime: &'a AiRuntime,
    id: u64,
}

impl Drop for Permit<'_> {
    fn drop(&mut self) {
        self.runtime.state.0.lock().unwrap().active.remove(&self.id);
        self.runtime.state.1.notify_all();
    }
}

impl AiRuntime {
    pub fn new(services: Services) -> Self {
        Self {
            service: services.ai,
            ..Default::default()
        }
    }

    pub fn close(&self) {
        let mut state = self.state.0.lock().unwrap();
        state.closed = true;
        for cancel in state.active.values() {
            cancel.cancel();
        }
    }

    /// Close admission, cancel accepted work (including provider-slot waiters),
    /// and wait for process ownership to return. A broken service fails loudly.
    pub fn shutdown(&self, timeout: Duration) -> Result<(), EffectFailure> {
        self.close();
        let state = self.state.0.lock().unwrap();
        let (state, _) = self
            .state
            .1
            .wait_timeout_while(state, timeout, |s| !s.active.is_empty())
            .unwrap();
        if !state.active.is_empty() {
            return Err(failure(
                "shutdown_timeout",
                "Local AI did not stop within the shutdown deadline",
            ));
        }
        Ok(())
    }

    pub fn generate(
        &self,
        payload: &Value,
        interrupt: Arc<AtomicBool>,
        deadline: Option<Instant>,
    ) -> Result<Value, EffectFailure> {
        // Bound the whole wire object before allocating another DTO copy.
        if serde_json::to_vec(payload).map_or(true, |b| b.len() > 2 * 1024 * 1024) {
            return Err(failure(
                "invalid_request",
                "Local AI request exceeds the input bound",
            ));
        }
        let mut request: GenerateRequest = serde_json::from_value(payload.clone())
            .map_err(|_| failure("invalid_request", "Invalid local AI request fields"))?;
        if request.harness.as_ref().is_none_or(|h| {
            h.is_empty()
                || h.len() > 128
                || !h
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        }) {
            return Err(failure(
                "invalid_request",
                "An explicit local harness id is required",
            ));
        }
        request.validate().map_err(provider_failure)?;
        let cancel = CancelToken::from(interrupt);
        if cancel.is_cancelled() {
            return Err(provider_failure(any_ai::Error::Cancelled));
        }
        if let Some(deadline) = deadline {
            let remaining = deadline
                .saturating_duration_since(Instant::now())
                .as_millis();
            if remaining < 100 {
                return Err(provider_failure(any_ai::Error::Timeout));
            }
            request.limits.timeout_ms = request.limits.timeout_ms.min(remaining as u64);
        }
        let permit = {
            let mut state = self.state.0.lock().unwrap();
            if state.closed {
                return Err(failure("unavailable", "Local AI is shutting down"));
            }
            let id = state.next;
            state.next += 1;
            state.active.insert(id, cancel.clone());
            Permit { runtime: self, id }
        };
        let service = self.service.as_ref().ok_or_else(|| {
            failure(
                "unavailable",
                "Local AI is not enabled on this execution device",
            )
        })?;
        let started = Instant::now();
        let response = service
            .generate(&request, &cancel)
            .map_err(provider_failure)?;
        response.validate_for(&request).map_err(provider_failure)?;
        let output = serde_json::to_value(response)
            .map_err(|_| failure("invalid_output", "Local AI returned an invalid response"))?;
        if cancel.is_cancelled() {
            return Err(provider_failure(any_ai::Error::Cancelled));
        }
        if started.elapsed() >= Duration::from_millis(request.limits.timeout_ms) {
            return Err(provider_failure(any_ai::Error::Timeout));
        }
        drop(permit);
        Ok(output)
    }
}

fn failure(code: &str, message: &str) -> EffectFailure {
    EffectFailure {
        type_: format!("ai.{code}"),
        message: message.into(),
    }
}

/// A local inference attempt may consume quota even without a model reply.
/// Mock/replay effects never consumed local provider work (ADR-030).
pub(crate) fn attempted(records: &[Value]) -> bool {
    records.iter().any(|r| {
        r["kind"] == "effect" && r["effect"] == "ai.generate" && r["meta"]["mocked"] != true
    })
}

fn provider_failure(error: any_ai::Error) -> EffectFailure {
    use any_ai::ErrorCode::*;
    // Never record adapter diagnostics: they can contain provider/user data.
    let message = match error.code() {
        InvalidRequest => "Invalid local AI request",
        HarnessNotFound | NoReadyHarness => {
            "The selected AI harness is unavailable on this execution device"
        }
        AuthenticationRequired => "Sign in using the selected harness on this execution device",
        HarnessDisabled => "The selected AI harness is disabled by host policy",
        IncompatibleHarness => "Update the selected AI harness to a compatible version",
        Unsupported => "The selected harness does not support this request",
        Cancelled => "Local AI generation was cancelled",
        Timeout => "Local AI generation reached its deadline",
        InvalidOutput => "The selected harness returned invalid output",
        ProviderFailed | Protocol | Io => "The selected harness failed; inspect host diagnostics",
    };
    failure(error.code().as_str(), message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        broker::{Broker, Mode},
        caps::GrantSet,
        replay::ReplayCursor,
        routes::Classifier,
        trace::TraceWriter,
    };
    use any_ai::{FinishReason, GeneratedContent};
    use serde_json::json;
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        mpsc,
    };

    struct Mock<F>(F);
    impl<F> AiService for Mock<F>
    where
        F: Fn(&GenerateRequest, &CancelToken) -> any_ai::Result<GenerateResponse> + Send + Sync,
    {
        fn generate(
            &self,
            r: &GenerateRequest,
            c: &CancelToken,
        ) -> any_ai::Result<GenerateResponse> {
            (self.0)(r, c)
        }
    }

    fn request() -> Value {
        json!({"harness":"codex", "messages":[{"role":"user","content":"hello"}],
            "limits":{"timeout_ms":1000,"max_output_bytes":1024}})
    }

    fn response() -> GenerateResponse {
        GenerateResponse {
            harness: "codex".into(),
            model: Some("test".into()),
            content: GeneratedContent::Text {
                text: "hello".into(),
            },
            finish_reason: FinishReason::Stop,
            usage: None,
        }
    }

    fn broker(service: Option<Arc<dyn AiService>>) -> Broker {
        let mut b = Broker::new(
            TraceWriter::new(json!({"id":"run_ai","program":"test@v1"})),
            BTreeMap::new(),
            BTreeMap::new(),
            None,
            Classifier::new(None),
        );
        b.ai = AiRuntime::new(Services { ai: service });
        b
    }

    #[test]
    fn ai_success_is_recorded_and_replays_without_service() {
        let calls = Arc::new(AtomicUsize::new(0));
        let count = calls.clone();
        let mut b = broker(Some(Arc::new(Mock(
            move |_: &GenerateRequest, _: &CancelToken| {
                count.fetch_add(1, Ordering::SeqCst);
                Ok(response())
            },
        ))));
        b.grants = Some(GrantSet::of(["llm.chat"]));
        let out = b.call("ai.generate", request()).unwrap();
        assert_eq!(out["content"]["text"], "hello");
        assert_eq!(b.writer.records.last().unwrap()["meta"]["class"], "read");
        let mut replay = broker(None);
        replay.mode = Mode::Replay;
        replay.cursor = Some(ReplayCursor::new(&b.writer.records));
        assert_eq!(replay.call("ai.generate", request()).unwrap(), out);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        assert!(attempted(&b.writer.records));
        assert!(!attempted(&replay.writer.records));
    }

    #[test]
    fn ai_capability_denial_precedes_mock_and_mock_needs_no_service() {
        let mut b = broker(None);
        b.mode = Mode::Mock;
        b.mock_index = Some(
            b.build_mock_index(&json!({"records":[{"effect":"ai.generate", "output":response()}]}))
                .unwrap(),
        );
        b.grants = Some(GrantSet::of(["ai.generate"]));
        assert_eq!(
            b.call("ai.generate", request()).unwrap_err().type_,
            "capability_denied"
        );
        b.grants = Some(GrantSet::of(["llm.chat"]));
        assert_eq!(
            b.call("ai.generate", request()).unwrap()["harness"],
            "codex"
        );
        assert_eq!(b.writer.records.last().unwrap()["meta"]["mocked"], true);
    }

    #[test]
    fn ai_errors_are_safe_recorded_and_replayable() {
        let mut b = broker(Some(Arc::new(Mock(
            |_: &GenerateRequest, _: &CancelToken| {
                Err(any_ai::Error::ProviderFailed {
                    harness: "private".into(),
                    message: "secret-token".into(),
                })
            },
        ))));
        let err = b.call("ai.generate", request()).unwrap_err();
        assert_eq!(err.type_, "ai.provider_failed");
        assert!(!serde_json::to_string(&b.writer.records)
            .unwrap()
            .contains("secret-token"));
        let mut replay = broker(None);
        replay.mode = Mode::Replay;
        replay.cursor = Some(ReplayCursor::new(&b.writer.records));
        assert_eq!(
            replay.call("ai.generate", request()).unwrap_err().type_,
            err.type_
        );
        let mut missing = broker(None);
        assert_eq!(
            missing.call("ai.generate", request()).unwrap_err().type_,
            "ai.unavailable"
        );
    }

    #[test]
    fn ai_invalid_input_and_expired_or_cancelled_work_never_reaches_service() {
        let service = Arc::new(Mock(
            |_: &GenerateRequest, _: &CancelToken| -> any_ai::Result<GenerateResponse> {
                panic!("must not invoke")
            },
        ));
        let mut b = broker(Some(service));
        for payload in [
            json!({"messages":[]}),
            json!({"harness":"codex","messages":[],"executable":"private"}),
            json!({"harness":"codex","messages":[{"role":"user","content":"hi"}],"limits":{"timeout_ms":0,"max_output_bytes":1}}),
        ] {
            assert_eq!(
                b.call("ai.generate", payload).unwrap_err().type_,
                "ai.invalid_request"
            );
        }
        b.deadline = Some(Instant::now());
        assert_eq!(
            b.call("ai.generate", request()).unwrap_err().type_,
            "ai.timeout"
        );
        b.deadline = None;
        b.interrupt.store(true, Ordering::Release);
        assert_eq!(
            b.call("ai.generate", request()).unwrap_err().type_,
            "ai.cancelled"
        );
    }

    #[test]
    fn ai_deadline_is_clamped_and_bad_output_is_rejected() {
        let mut b = broker(Some(Arc::new(Mock(
            |r: &GenerateRequest, _: &CancelToken| {
                assert!((100..=500).contains(&r.limits.timeout_ms));
                let mut out = response();
                out.harness = "claude".into();
                Ok(out)
            },
        ))));
        b.deadline = Some(Instant::now() + Duration::from_millis(500));
        assert_eq!(
            b.call("ai.generate", request()).unwrap_err().type_,
            "ai.protocol_error"
        );
    }

    #[test]
    fn ai_schema_is_checked_at_the_boundary_even_for_injected_services() {
        let mut b = broker(Some(Arc::new(Mock(
            |_: &GenerateRequest, _: &CancelToken| {
                let mut out = response();
                out.content = GeneratedContent::Json {
                    value: json!({"code":"wrong"}),
                };
                Ok(out)
            },
        ))));
        let mut req = request();
        req["output"] = json!({"type":"json_schema", "schema":{"type":"object","required":["text"],"properties":{"text":{"type":"string"}},"additionalProperties":false}});
        assert_eq!(
            b.call("ai.generate", req).unwrap_err().type_,
            "ai.invalid_output"
        );
    }

    #[test]
    fn ai_shutdown_cancels_active_calls_and_closes_admission_on_all_clones() {
        let (tx, rx) = mpsc::channel();
        let runtime = AiRuntime::new(Services {
            ai: Some(Arc::new(Mock(
                move |_: &GenerateRequest, cancel: &CancelToken| {
                    tx.send(()).unwrap();
                    while !cancel.is_cancelled() {
                        std::thread::sleep(Duration::from_millis(1));
                    }
                    Err(any_ai::Error::Cancelled)
                },
            ))),
        });
        let workers: Vec<_> = (0..2)
            .map(|_| {
                let ai = runtime.clone();
                std::thread::spawn(move || {
                    ai.generate(&request(), Arc::new(AtomicBool::new(false)), None)
                })
            })
            .collect();
        for _ in 0..2 {
            rx.recv_timeout(Duration::from_secs(2)).unwrap();
        }
        runtime.shutdown(Duration::from_secs(2)).unwrap();
        for worker in workers {
            assert_eq!(worker.join().unwrap().unwrap_err().type_, "ai.cancelled");
        }
        assert_eq!(
            runtime
                .generate(&request(), Arc::new(AtomicBool::new(false)), None)
                .unwrap_err()
                .type_,
            "ai.unavailable"
        );
    }

    #[test]
    fn ai_noncooperative_service_shutdown_is_bounded_and_reported() {
        let (entered, waiting) = mpsc::channel();
        let (release, released) = mpsc::channel();
        let released = Mutex::new(released);
        let ai = AiRuntime::new(Services {
            ai: Some(Arc::new(Mock(
                move |_: &GenerateRequest, _: &CancelToken| {
                    entered.send(()).unwrap();
                    released.lock().unwrap().recv().unwrap();
                    Ok(response())
                },
            ))),
        });
        let runtime = ai.clone();
        let worker = std::thread::spawn(move || {
            runtime.generate(&request(), Arc::new(AtomicBool::new(false)), None)
        });
        waiting.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(
            ai.shutdown(Duration::from_millis(1)).unwrap_err().type_,
            "ai.shutdown_timeout"
        );
        release.send(()).unwrap();
        assert_eq!(worker.join().unwrap().unwrap_err().type_, "ai.cancelled");
        ai.shutdown(Duration::from_secs(1)).unwrap();
    }
}
