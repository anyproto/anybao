//! Injected local model generation and its lifecycle (ADR-030).

use crate::broker::EffectFailure;
use any_ai::{
    AiSettings, AiSettingsSnapshot, AiSettingsUpdate, AnyAi, CancelToken, GenerateRequest,
    GenerateResponse, ModelInfo, ModelSelection, ResolvedModel,
};
use serde_json::Value;
use std::collections::BTreeMap;
use std::sync::{atomic::AtomicBool, Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

/// An implementation must honor cancellation/deadlines and return only after
/// its provider processes have been reaped. Diagnostics must stay host-side.
pub trait AiService: Send + Sync {
    /// Host-local preferences only. Standalone/read-only hosts need not expose them.
    fn settings(&self) -> Result<AiSettingsSnapshot, EffectFailure> {
        Err(failure(
            "unsupported",
            "This host does not expose local AI settings",
        ))
    }

    fn models(
        &self,
        _harness: &str,
        _cancel: &CancelToken,
        _timeout: Duration,
    ) -> Result<Vec<ModelInfo>, EffectFailure> {
        Err(failure(
            "unsupported",
            "This host does not expose local AI models",
        ))
    }

    /// A committed write must return its committed result even on late cancel.
    fn select(
        &self,
        _update: &AiSettingsUpdate,
        _cancel: &CancelToken,
        _timeout: Duration,
    ) -> Result<AiSettingsSnapshot, EffectFailure> {
        Err(failure(
            "unsupported",
            "This host does not allow local AI settings changes",
        ))
    }

    /// Non-spending route resolution; record it before passing a frozen request.
    fn resolve(&self, selection: &ModelSelection) -> any_ai::Result<ResolvedModel> {
        AiSettings::default().resolve(selection)
    }

    fn generate(
        &self,
        request: &GenerateRequest,
        cancel: &CancelToken,
    ) -> any_ai::Result<GenerateResponse>;

    fn run(
        &self,
        operation: any_ai::Operation,
        request: &GenerateRequest,
        cancel: &CancelToken,
    ) -> any_ai::Result<GenerateResponse> {
        if operation == any_ai::Operation::Generate {
            self.generate(request, cancel)
        } else {
            Err(any_ai::Error::Unsupported {
                harness: request.harness.clone().unwrap_or_default(),
                capability: "media/search service",
            })
        }
    }
}

impl AiService for AnyAi {
    fn models(
        &self,
        harness: &str,
        cancel: &CancelToken,
        timeout: Duration,
    ) -> Result<Vec<ModelInfo>, EffectFailure> {
        self.models_with_timeout(harness, cancel, timeout)
            .map_err(provider_failure)
    }

    fn run(
        &self,
        operation: any_ai::Operation,
        request: &GenerateRequest,
        cancel: &CancelToken,
    ) -> any_ai::Result<GenerateResponse> {
        self.run_with(operation, request, cancel, |_| {})
    }
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
    fn permit(&self, cancel: CancelToken) -> Result<Permit<'_>, EffectFailure> {
        if cancel.is_cancelled() {
            return Err(provider_failure(any_ai::Error::Cancelled));
        }
        let mut state = self.state.0.lock().unwrap();
        if state.closed {
            return Err(failure("unavailable", "Local AI is shutting down"));
        }
        let id = state.next;
        state.next += 1;
        state.active.insert(id, cancel);
        Ok(Permit { runtime: self, id })
    }

    /// ADR-030: ordinary recorded effects; only the setter is a mutation.
    pub fn control(
        &self,
        name: &str,
        payload: &Value,
        interrupt: Arc<AtomicBool>,
        deadline: Option<Instant>,
    ) -> Result<Value, EffectFailure> {
        use serde::Deserialize;
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Models {
            harness: String,
        }
        let invalid = || failure("invalid_request", "Invalid local AI settings request");
        if serde_json::to_vec(payload).map_or(true, |bytes| bytes.len() > 16_384) {
            return Err(invalid());
        }
        let timeout = deadline
            .map_or(Duration::from_secs(12), |at| {
                at.saturating_duration_since(Instant::now())
            })
            .min(Duration::from_secs(12));
        if timeout.is_zero() {
            return Err(provider_failure(any_ai::Error::Timeout));
        }
        let started = Instant::now();
        let cancel = CancelToken::from(interrupt);
        let _permit = self.permit(cancel.clone())?;
        let service = self.service.as_ref().ok_or_else(|| {
            failure(
                "unavailable",
                "Local AI is not enabled on this execution device",
            )
        })?;
        let output = match name {
            "ai.settings.get" => {
                if !payload.as_object().is_some_and(serde_json::Map::is_empty) {
                    return Err(invalid());
                }
                let result = service.settings()?;
                if result.revision > 9_007_199_254_740_991 {
                    return Err(failure(
                        "invalid_output",
                        "Invalid local AI settings revision",
                    ));
                }
                result.settings.validate().map_err(provider_failure)?;
                serde_json::to_value(result)
            }
            "ai.models" => {
                let request: Models =
                    serde_json::from_value(payload.clone()).map_err(|_| invalid())?;
                if !matches!(request.harness.as_str(), "codex" | "claude") {
                    return Err(invalid());
                }
                serde_json::to_value(service.models(&request.harness, &cancel, timeout)?)
            }
            "ai.settings.set" => {
                let request: AiSettingsUpdate =
                    serde_json::from_value(payload.clone()).map_err(|_| invalid())?;
                if request.expected_revision > 9_007_199_254_740_991
                    || !matches!(request.selection.harness.as_str(), "codex" | "claude")
                {
                    return Err(invalid());
                }
                AiSettings::default()
                    .with_selection(&request.selection)
                    .map_err(provider_failure)?;
                // The host validates availability and CAS before committing.
                // No post-commit cancellation error: the recorded result must
                // describe the write that really happened.
                let result = service.select(&request, &cancel, timeout)?;
                if result.revision <= request.expected_revision
                    || result.revision > 9_007_199_254_740_991
                    || result
                        .settings
                        .resolve(&ModelSelection::default())
                        .ok()
                        .as_ref()
                        != Some(&request.selection)
                {
                    // The host may already have committed: never imply that a
                    // malformed receipt makes it safe to retry the mutation.
                    return Err(failure(
                        "settings_outcome_unknown",
                        "Settings save could not be verified; read settings before another change",
                    ));
                }
                serde_json::to_value(result)
            }
            _ => return Err(invalid()),
        }
        .map_err(|_| failure("invalid_output", "Invalid local AI settings result"))?;
        if serde_json::to_vec(&output).map_or(true, |bytes| bytes.len() > 1_048_576) {
            return Err(failure(
                "invalid_output",
                "Oversized local AI settings result",
            ));
        }
        if name != "ai.settings.set" {
            if cancel.is_cancelled() {
                return Err(provider_failure(any_ai::Error::Cancelled));
            }
            if started.elapsed() >= timeout {
                return Err(provider_failure(any_ai::Error::Timeout));
            }
        }
        Ok(output)
    }

    pub fn resolve(&self, payload: &Value) -> Result<Value, EffectFailure> {
        if serde_json::to_vec(payload).map_or(true, |b| b.len() > 1024) {
            return Err(failure(
                "invalid_request",
                "Local AI selection exceeds the input bound",
            ));
        }
        let selection: ModelSelection = serde_json::from_value(payload.clone())
            .map_err(|_| failure("invalid_request", "Invalid local AI selection"))?;
        if self.state.0.lock().unwrap().closed {
            return Err(failure("unavailable", "Local AI is shutting down"));
        }
        let service = self.service.as_ref().ok_or_else(|| {
            failure(
                "unavailable",
                "Local AI is not enabled on this execution device",
            )
        })?;
        let resolved = service.resolve(&selection).map_err(provider_failure)?;
        if self.state.0.lock().unwrap().closed {
            return Err(failure("unavailable", "Local AI is shutting down"));
        }
        // Validate the injected service result and preserve explicit choices.
        let checked = AiSettings::default()
            .resolve(&ModelSelection {
                harness: Some(resolved.harness.clone()),
                model: resolved.model.clone(),
                effort: resolved.effort,
                speed: Some(resolved.speed),
            })
            .map_err(provider_failure)?;
        if selection
            .harness
            .as_ref()
            .is_some_and(|h| h != &checked.harness)
            || selection
                .model
                .as_ref()
                .is_some_and(|m| Some(m) != checked.model.as_ref())
            || selection
                .effort
                .is_some_and(|effort| Some(effort) != checked.effort)
            || selection.speed.is_some_and(|speed| speed != checked.speed)
        {
            return Err(failure(
                "invalid_output",
                "Local AI changed an explicit selection",
            ));
        }
        serde_json::to_value(checked)
            .map_err(|_| failure("invalid_output", "Invalid local AI selection result"))
    }

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
        self.run(any_ai::Operation::Generate, payload, interrupt, deadline)
    }

    pub fn run(
        &self,
        operation: any_ai::Operation,
        payload: &Value,
        interrupt: Arc<AtomicBool>,
        deadline: Option<Instant>,
    ) -> Result<Value, EffectFailure> {
        // Bound the whole wire object before allocating another DTO copy.
        if serde_json::to_vec(payload).map_or(true, |b| b.len() > 20 * 1024 * 1024) {
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
        request
            .validate_operation(operation)
            .map_err(provider_failure)?;
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
        let permit = self.permit(cancel.clone())?;
        let service = self.service.as_ref().ok_or_else(|| {
            failure(
                "unavailable",
                "Local AI is not enabled on this execution device",
            )
        })?;
        let started = Instant::now();
        let response = service
            .run(operation, &request, &cancel)
            .map_err(provider_failure)?;
        response
            .validate_operation(&request, operation)
            .map_err(provider_failure)?;
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
        r["kind"] == "effect"
            && matches!(
                r["effect"].as_str(),
                Some("ai.generate" | "ai.image_generate" | "ai.search")
            )
            && r["meta"]["mocked"] != true
    })
}

fn provider_failure(error: any_ai::Error) -> EffectFailure {
    use any_ai::ErrorCode::*;
    // Never record adapter diagnostics: they can contain provider/user data.
    let message = match error.code() {
        InvalidRequest => "Invalid local AI request",
        SelectionRequired => "Choose a preferred local AI harness in Settings → Model",
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

    #[derive(Default)]
    struct SettingsService {
        snapshot: Mutex<AiSettingsSnapshot>,
        writes: AtomicUsize,
        late_cancel: bool,
        wrong_receipt: bool,
        wrong_speed_receipt: bool,
    }

    impl AiService for SettingsService {
        fn generate(
            &self,
            _: &GenerateRequest,
            _: &CancelToken,
        ) -> any_ai::Result<GenerateResponse> {
            panic!("settings must not perform inference")
        }
        fn settings(&self) -> Result<AiSettingsSnapshot, EffectFailure> {
            Ok(self.snapshot.lock().unwrap().clone())
        }
        fn models(
            &self,
            _: &str,
            _: &CancelToken,
            timeout: Duration,
        ) -> Result<Vec<ModelInfo>, EffectFailure> {
            assert!(timeout <= Duration::from_secs(12));
            Ok(vec![])
        }
        fn select(
            &self,
            update: &AiSettingsUpdate,
            cancel: &CancelToken,
            _: Duration,
        ) -> Result<AiSettingsSnapshot, EffectFailure> {
            let mut snapshot = self.snapshot.lock().unwrap();
            if snapshot.revision != update.expected_revision {
                return Err(failure("settings_conflict", "stale"));
            }
            snapshot.settings = snapshot.settings.with_selection(&update.selection).unwrap();
            snapshot.revision += 1;
            self.writes.fetch_add(1, Ordering::SeqCst);
            if self.late_cancel {
                cancel.cancel();
            }
            let mut result = snapshot.clone();
            if self.wrong_receipt {
                result.settings = AiSettings::default();
            }
            if self.wrong_speed_receipt {
                result.settings.speeds.clear();
            }
            Ok(result)
        }
    }

    fn selection_update() -> Value {
        json!({"expected_revision":0,"selection":{"harness":"codex","model":"test","effort":"high"}})
    }

    #[test]
    fn speed_setting_is_recorded_and_replayed_without_repeating_the_write() {
        let service = Arc::new(SettingsService::default());
        let mut live = broker(Some(service.clone()));
        let mut calls = Vec::new();
        for (revision, speed) in ["fast", "standard"].into_iter().enumerate() {
            let input = json!({"expected_revision":revision,
                "selection":{"harness":"codex","model":"test","speed":speed}});
            let output = live.call("ai.settings.set", input.clone()).unwrap();
            let settings: AiSettings = serde_json::from_value(output["settings"].clone()).unwrap();
            assert_eq!(
                serde_json::to_value(settings.resolve(&ModelSelection::default()).unwrap())
                    .unwrap()["speed"],
                speed
            );
            calls.push((input, output));
        }
        let mut replay = broker(None);
        replay.mode = Mode::Replay;
        replay.cursor = Some(ReplayCursor::new(&live.writer.records));
        for (input, expected) in calls {
            assert_eq!(replay.call("ai.settings.set", input).unwrap(), expected);
        }
        assert_eq!(service.writes.load(Ordering::SeqCst), 2);
        assert!(!attempted(&replay.writer.records));
    }

    #[test]
    fn speed_mismatch_in_a_save_receipt_reports_unknown_outcome_without_retry() {
        let service = Arc::new(SettingsService {
            wrong_speed_receipt: true,
            ..Default::default()
        });
        let mut live = broker(Some(service.clone()));
        let mut input = selection_update();
        input["selection"]["speed"] = json!("fast");
        assert_eq!(
            live.call("ai.settings.set", input.clone())
                .unwrap_err()
                .type_,
            "ai.settings_outcome_unknown"
        );
        assert_eq!(
            service.snapshot.lock().unwrap().settings.speeds["codex"]["test"],
            any_ai::Speed::Fast
        );
        let mut replay = broker(None);
        replay.mode = Mode::Replay;
        replay.cursor = Some(ReplayCursor::new(&live.writer.records));
        assert_eq!(
            replay.call("ai.settings.set", input).unwrap_err().type_,
            "ai.settings_outcome_unknown"
        );
        assert_eq!(service.writes.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn settings_wrong_receipt_reports_uncertainty_and_replay_does_not_retry() {
        let service = Arc::new(SettingsService {
            wrong_receipt: true,
            ..Default::default()
        });
        let mut live = broker(Some(service.clone()));
        let error = live
            .call("ai.settings.set", selection_update())
            .unwrap_err();
        assert_eq!(error.type_, "ai.settings_outcome_unknown");
        assert!(error.message.contains("read settings"));
        assert_eq!(live.mutations, 1);
        assert_eq!(service.snapshot.lock().unwrap().revision, 1);
        let mut replay = broker(None);
        replay.mode = Mode::Replay;
        replay.cursor = Some(ReplayCursor::new(&live.writer.records));
        assert_eq!(
            replay
                .call("ai.settings.set", selection_update())
                .unwrap_err()
                .type_,
            error.type_
        );
        assert_eq!(service.writes.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn settings_mock_requires_write_grant_and_never_touches_host() {
        let mut mock = broker(Some(Arc::new(Mock(
            |_: &GenerateRequest, _: &CancelToken| panic!("mock must not invoke host"),
        ))));
        mock.mode = Mode::Mock;
        mock.mock_index = Some(
            mock.build_mock_index(&json!({"records":[{
                "effect":"ai.settings.set", "output":{"revision":1}
            }]}))
            .unwrap(),
        );
        mock.grants = Some(GrantSet::of(["llm.chat"]));
        assert_eq!(
            mock.call("ai.settings.set", selection_update())
                .unwrap_err()
                .type_,
            "capability_denied"
        );
        mock.grants = Some(GrantSet::of(["ai.settings.write", "ai.settings.read"]));
        assert_eq!(
            mock.call("ai.settings.set", selection_update()).unwrap()["revision"],
            1
        );
        assert_eq!(
            mock.writer.records.last().unwrap()["meta"]["class"],
            "mutate"
        );
        assert_eq!(mock.writer.records.last().unwrap()["meta"]["mocked"], true);
        // No matching fixture must fail closed, not invoke settings().
        assert_eq!(
            mock.call("ai.settings.get", json!({})).unwrap_err().type_,
            "mock_unmatched"
        );
        assert_eq!(
            mock.call("ai.settings.set", selection_update())
                .unwrap_err()
                .type_,
            "mock_unmatched"
        );
    }

    #[test]
    fn settings_shutdown_cancels_active_controls_and_drains_before_returning() {
        struct BlockingSettings(mpsc::Sender<()>);
        impl AiService for BlockingSettings {
            fn generate(
                &self,
                _: &GenerateRequest,
                _: &CancelToken,
            ) -> any_ai::Result<GenerateResponse> {
                panic!("no inference")
            }
            fn models(
                &self,
                _: &str,
                cancel: &CancelToken,
                _: Duration,
            ) -> Result<Vec<ModelInfo>, EffectFailure> {
                self.0.send(()).unwrap();
                while !cancel.is_cancelled() {
                    std::thread::sleep(Duration::from_millis(1));
                }
                Err(provider_failure(any_ai::Error::Cancelled))
            }
            fn select(
                &self,
                _: &AiSettingsUpdate,
                cancel: &CancelToken,
                timeout: Duration,
            ) -> Result<AiSettingsSnapshot, EffectFailure> {
                self.models("codex", cancel, timeout)?;
                panic!("cancelled selection must not commit")
            }
        }
        let (started, ready) = mpsc::channel();
        let runtime = AiRuntime::new(Services {
            ai: Some(Arc::new(BlockingSettings(started))),
        });
        let workers: Vec<_> = [
            ("ai.models", json!({"harness":"codex"})),
            ("ai.settings.set", selection_update()),
        ]
        .into_iter()
        .map(|(name, input)| {
            let runtime = runtime.clone();
            std::thread::spawn(move || runtime.control(name, &input, Arc::default(), None))
        })
        .collect();
        for _ in 0..2 {
            ready.recv_timeout(Duration::from_secs(2)).unwrap();
        }
        runtime.shutdown(Duration::from_secs(2)).unwrap();
        assert!(runtime.state.0.lock().unwrap().active.is_empty());
        for worker in workers {
            assert_eq!(worker.join().unwrap().unwrap_err().type_, "ai.cancelled");
        }
        assert_eq!(
            runtime
                .control("ai.settings.set", &selection_update(), Arc::default(), None)
                .unwrap_err()
                .type_,
            "ai.unavailable"
        );
    }

    #[test]
    fn settings_effects_record_and_replay_without_host_and_write_has_distinct_capability() {
        let service = Arc::new(SettingsService::default());
        let mut live = broker(Some(service.clone()));
        live.grants = Some(GrantSet::of([
            "ai.settings.read",
            "ai.settings.write",
            "llm.chat",
        ]));
        let calls = [
            ("ai.settings.get", json!({})),
            ("ai.models", json!({"harness":"codex"})),
            ("ai.settings.set", selection_update()),
        ];
        let results: Vec<_> = calls
            .iter()
            .map(|(name, input)| live.call(name, input.clone()).unwrap())
            .collect();
        assert_eq!(results[2]["settings"]["models"]["codex"], "test");
        assert_eq!(
            live.writer.records.last().unwrap()["meta"]["class"],
            "mutate"
        );
        assert_eq!(live.mutations, 1);
        let mut replay = broker(None);
        replay.mode = Mode::Replay;
        replay.cursor = Some(ReplayCursor::new(&live.writer.records));
        for ((name, input), expected) in calls.iter().zip(results) {
            assert_eq!(replay.call(name, input.clone()).unwrap(), expected);
        }
        assert_eq!(service.writes.load(Ordering::SeqCst), 1);
        let mut denied = broker(Some(service.clone()));
        denied.grants = Some(GrantSet::of(["llm.chat"]));
        assert_eq!(
            denied
                .call("ai.settings.set", selection_update())
                .unwrap_err()
                .type_,
            "capability_denied"
        );
        assert_eq!(service.writes.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn settings_control_rejects_invalid_expired_cancelled_and_closed_work_without_writes() {
        let service = Arc::new(SettingsService::default());
        for input in [
            json!({}),
            json!({"expected_revision":0,"selection":{"harness":"other"}}),
            json!({"expected_revision":0,"selection":{"harness":"codex","effort":"high"}}),
            json!({"expected_revision":0,"selection":{"harness":"codex","speed":"fast"}}),
            json!({"expected_revision":0,"selection":{"harness":"codex","model":"test","speed":"ultrafast"}}),
            json!({"expected_revision":0,"selection":{"harness":"codex"},"allow_metered":true}),
        ] {
            assert!(broker(Some(service.clone()))
                .call("ai.settings.set", input)
                .is_err());
        }
        let mut expired = broker(Some(service.clone()));
        expired.deadline = Some(Instant::now());
        assert_eq!(
            expired
                .call("ai.settings.set", selection_update())
                .unwrap_err()
                .type_,
            "ai.timeout"
        );
        let mut cancelled = broker(Some(service.clone()));
        cancelled.interrupt.store(true, Ordering::SeqCst);
        assert_eq!(
            cancelled
                .call("ai.settings.set", selection_update())
                .unwrap_err()
                .type_,
            "ai.cancelled"
        );
        let mut closed = broker(Some(service.clone()));
        closed.ai.close();
        assert_eq!(
            closed
                .call("ai.settings.set", selection_update())
                .unwrap_err()
                .type_,
            "ai.unavailable"
        );
        assert_eq!(service.writes.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn settings_committed_write_is_not_reported_as_cancelled_and_stale_failure_is_recorded() {
        let service = Arc::new(SettingsService {
            late_cancel: true,
            ..Default::default()
        });
        let mut live = broker(Some(service.clone()));
        assert_eq!(
            live.call("ai.settings.set", selection_update()).unwrap()["revision"],
            1
        );
        assert!(live.interrupt.load(Ordering::SeqCst));
        let mut stale = broker(Some(service.clone()));
        assert_eq!(
            stale
                .call("ai.settings.set", selection_update())
                .unwrap_err()
                .type_,
            "ai.settings_conflict"
        );
        let mut replay = broker(None);
        replay.mode = Mode::Replay;
        replay.cursor = Some(ReplayCursor::new(&stale.writer.records));
        assert_eq!(
            replay
                .call("ai.settings.set", selection_update())
                .unwrap_err()
                .type_,
            "ai.settings_conflict"
        );
        assert_eq!(service.writes.load(Ordering::SeqCst), 1);
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
    fn ai_images_expand_only_for_execution_and_replay_keeps_blob_references() {
        let dir = tempfile::tempdir().unwrap();
        let blobs = crate::blob::BlobDir::create(dir.path()).unwrap();
        let bytes = b"\x89PNG\r\n\x1a\nfixture";
        let reference = blobs.put(bytes, "image/png").unwrap();
        let mut input = request();
        input["images"] = json!([{"mime":"image/png", "data": reference}]);
        let expected = any_ai::ImageInput::from_bytes("image/png", bytes).unwrap();
        let mut b = broker(Some(Arc::new(Mock(
            move |r: &GenerateRequest, _: &CancelToken| {
                assert_eq!(r.images, vec![expected.clone()]);
                Ok(response())
            },
        ))));
        b.writer.blob_dir = Some(blobs);
        let output = b.call("ai.generate", input.clone()).unwrap();
        let rec = b.writer.records.last().unwrap();
        assert_eq!(rec["input"]["images"][0]["data"], reference);
        assert!(rec["blobs"]
            .as_array()
            .unwrap()
            .contains(&reference["__blob"]));
        let mut replay = broker(None);
        replay.mode = Mode::Replay;
        replay.cursor = Some(ReplayCursor::new(&b.writer.records));
        // Replay needs neither the provider nor expansion/access to the bytes.
        assert_eq!(replay.call("ai.generate", input).unwrap(), output);
    }

    #[test]
    fn media_and_search_are_distinct_replayable_effects_with_blob_outputs() {
        struct Media(Arc<AtomicUsize>);
        impl AiService for Media {
            fn generate(
                &self,
                _: &GenerateRequest,
                _: &CancelToken,
            ) -> any_ai::Result<GenerateResponse> {
                panic!("media must not dispatch as ordinary generation")
            }
            fn run(
                &self,
                operation: any_ai::Operation,
                request: &GenerateRequest,
                _: &CancelToken,
            ) -> any_ai::Result<GenerateResponse> {
                self.0.fetch_add(1, Ordering::SeqCst);
                if operation == any_ai::Operation::Search {
                    assert_eq!(request.speed, Some(any_ai::Speed::Fast));
                }
                let mut r = response();
                r.content = match operation {
                    any_ai::Operation::ImageGenerate => GeneratedContent::Image {
                        image: any_ai::ImageInput::from_bytes(
                            "image/png",
                            b"\x89PNG\r\n\x1a\nfixture",
                        )
                        .unwrap(),
                    },
                    any_ai::Operation::Search => GeneratedContent::WebSearch {
                        text: "answer".into(),
                        sources: vec![any_ai::WebSource {
                            title: "Evidence".into(),
                            url: "https://example.org".into(),
                        }],
                    },
                    _ => panic!("wrong operation"),
                };
                Ok(r)
            }
        }
        let calls = Arc::new(AtomicUsize::new(0));
        let mut b = broker(Some(Arc::new(Media(calls.clone()))));
        let dir = tempfile::tempdir().unwrap();
        b.writer.blob_dir = Some(crate::blob::BlobDir::create(dir.path()).unwrap());
        b.grants = Some(GrantSet::of(["llm.chat"]));
        let image = b.call("ai.image_generate", request()).unwrap();
        let reference = &image["content"]["image"]["data"];
        assert!(crate::blob::is_raw_ref(reference));
        assert!(b.writer.records.last().unwrap()["blobs"]
            .as_array()
            .unwrap()
            .contains(&reference["__blob"]));
        let mut search_input = request();
        search_input["model"] = json!("test");
        search_input["speed"] = json!("fast");
        let search = b.call("ai.search", search_input.clone()).unwrap();
        assert_eq!(
            search["content"]["sources"][0]["url"],
            "https://example.org"
        );
        assert!(attempted(&b.writer.records));
        let mut replay = broker(None);
        replay.mode = Mode::Replay;
        replay.cursor = Some(ReplayCursor::new(&b.writer.records));
        assert_eq!(replay.call("ai.image_generate", request()).unwrap(), image);
        assert_eq!(replay.call("ai.search", search_input).unwrap(), search);
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        assert!(!attempted(&replay.writer.records));
        let mut denied = broker(None);
        denied.grants = Some(GrantSet::of(["data.read"]));
        assert_eq!(
            denied.call("ai.search", request()).unwrap_err().type_,
            "capability_denied"
        );
        let mut missing_storage = broker(Some(Arc::new(Media(calls.clone()))));
        assert_eq!(
            missing_storage
                .call("ai.image_generate", request())
                .unwrap_err()
                .type_,
            "ai.artifact_unavailable"
        );
        assert_eq!(calls.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn ai_image_reference_lies_fail_before_service() {
        let dir = tempfile::tempdir().unwrap();
        let blobs = crate::blob::BlobDir::create(dir.path()).unwrap();
        let reference = blobs.put(b"\x89PNG\r\n\x1a\nfixture", "image/png").unwrap();
        let mut b = broker(Some(Arc::new(Mock(
            |_: &GenerateRequest, _: &CancelToken| {
                panic!("invalid media must not reach the service")
            },
        ))));
        b.writer.blob_dir = Some(blobs);
        for field in ["bytes", "mime", "encoding", "__blob"] {
            let mut data = reference.clone();
            data[field] = match field {
                "bytes" => json!(1),
                "mime" => json!("image/jpeg"),
                "encoding" => json!("data-uri"),
                _ => json!("../../secret"),
            };
            let mut input = request();
            input["images"] = json!([{"mime":"image/png", "data":data}]);
            assert_eq!(
                b.call("ai.generate", input).unwrap_err().type_,
                "ai.invalid_request"
            );
        }
    }

    #[test]
    fn blocked_resolution_cannot_block_shutdown_or_publish_after_close() {
        struct Blocked {
            started: mpsc::Sender<()>,
            release: Mutex<mpsc::Receiver<()>>,
        }
        impl AiService for Blocked {
            fn resolve(&self, _: &ModelSelection) -> any_ai::Result<ResolvedModel> {
                self.started.send(()).unwrap();
                self.release.lock().unwrap().recv().unwrap();
                Ok(ResolvedModel {
                    harness: "codex".into(),
                    model: None,
                    effort: None,
                    speed: any_ai::Speed::Standard,
                })
            }
            fn generate(
                &self,
                _: &GenerateRequest,
                _: &CancelToken,
            ) -> any_ai::Result<GenerateResponse> {
                panic!("not generation")
            }
        }
        let (started, ready) = mpsc::channel();
        let (release, wait) = mpsc::channel();
        let runtime = AiRuntime::new(Services {
            ai: Some(Arc::new(Blocked {
                started,
                release: Mutex::new(wait),
            })),
        });
        let worker = runtime.clone();
        let job = std::thread::spawn(move || worker.resolve(&json!({})));
        ready.recv_timeout(Duration::from_secs(1)).unwrap();
        let (closed, receive) = mpsc::channel();
        let closer = std::thread::spawn(move || {
            closed
                .send(runtime.shutdown(Duration::from_millis(20)))
                .unwrap();
        });
        let outcome = receive.recv_timeout(Duration::from_millis(250));
        // Always release the worker, including on a regression, so this test
        // reports failure instead of stranding a background thread forever.
        release.send(()).unwrap();
        assert!(outcome.unwrap().is_ok());
        assert_eq!(job.join().unwrap().unwrap_err().type_, "ai.unavailable");
        closer.join().unwrap();
    }

    #[test]
    fn resolution_guards_and_missing_preference_errors_are_replayable() {
        let service = Arc::new(Mock(|_: &GenerateRequest, _: &CancelToken| {
            panic!("must not generate")
        }));
        let mut b = broker(Some(service));
        for payload in [json!({"unknown":true}), json!({"harness":"x".repeat(1025)})] {
            assert_eq!(
                b.call("ai.resolve", payload).unwrap_err().type_,
                "ai.invalid_request"
            );
        }
        let mut record = broker(Some(Arc::new(Mock(
            |_: &GenerateRequest, _: &CancelToken| panic!("must not generate"),
        ))));
        let error = record.call("ai.resolve", json!({})).unwrap_err();
        assert_eq!(error.type_, "ai.selection_required");
        assert!(error.message.contains("Settings"));
        let mut replay = broker(None);
        replay.mode = Mode::Replay;
        replay.cursor = Some(ReplayCursor::new(&record.writer.records));
        assert_eq!(
            replay.call("ai.resolve", json!({})).unwrap_err().type_,
            error.type_
        );
        assert_eq!(
            AiRuntime::default().resolve(&json!({})).unwrap_err().type_,
            "ai.unavailable"
        );
        b.ai.close();
        assert_eq!(
            b.ai.resolve(&json!({"harness":"codex"})).unwrap_err().type_,
            "ai.unavailable"
        );
        let mut mock = broker(None);
        mock.mode = Mode::Mock;
        mock.mock_index = Some(
            mock.build_mock_index(
                &json!({"records":[{"effect":"ai.resolve", "output":{"harness":"claude"}}]}),
            )
            .unwrap(),
        );
        assert_eq!(
            mock.call("ai.resolve", json!({})).unwrap(),
            json!({"harness":"claude"})
        );
    }

    #[test]
    fn resolution_rejects_invalid_service_output_and_changed_overrides() {
        struct Wrong(ResolvedModel);
        impl AiService for Wrong {
            fn resolve(&self, _: &ModelSelection) -> any_ai::Result<ResolvedModel> {
                Ok(self.0.clone())
            }
            fn generate(
                &self,
                _: &GenerateRequest,
                _: &CancelToken,
            ) -> any_ai::Result<GenerateResponse> {
                panic!("not generation")
            }
        }
        for (result, selection) in [
            (
                ResolvedModel {
                    harness: "claude".into(),
                    model: None,
                    effort: None,
                    speed: any_ai::Speed::Standard,
                },
                json!({"harness":"codex"}),
            ),
            (
                ResolvedModel {
                    harness: "codex".into(),
                    model: None,
                    effort: None,
                    speed: any_ai::Speed::Standard,
                },
                json!({"model":"explicit"}),
            ),
            (
                ResolvedModel {
                    harness: "".into(),
                    model: None,
                    effort: None,
                    speed: any_ai::Speed::Standard,
                },
                json!({}),
            ),
            (
                ResolvedModel {
                    harness: "codex".into(),
                    model: Some("--bad".into()),
                    effort: None,
                    speed: any_ai::Speed::Standard,
                },
                json!({}),
            ),
            (
                ResolvedModel {
                    harness: "codex".into(),
                    model: Some("explicit".into()),
                    effort: Some(any_ai::Effort::Low),
                    speed: any_ai::Speed::Standard,
                },
                json!({"model":"explicit", "effort":"high"}),
            ),
            (
                ResolvedModel {
                    harness: "codex".into(),
                    model: None,
                    effort: Some(any_ai::Effort::High),
                    speed: any_ai::Speed::Standard,
                },
                json!({}),
            ),
            (
                ResolvedModel {
                    harness: "codex".into(),
                    model: Some("explicit".into()),
                    effort: None,
                    speed: any_ai::Speed::Fast,
                },
                json!({"model":"explicit", "speed":"standard"}),
            ),
            (
                ResolvedModel {
                    harness: "codex".into(),
                    model: None,
                    effort: None,
                    speed: any_ai::Speed::Fast,
                },
                json!({}),
            ),
        ] {
            let runtime = AiRuntime::new(Services {
                ai: Some(Arc::new(Wrong(result))),
            });
            assert!(runtime.resolve(&selection).is_err());
        }
    }

    #[test]
    fn resolution_is_recorded_replayable_and_capability_checked() {
        let mut b = broker(Some(Arc::new(Mock(
            |_: &GenerateRequest, _: &CancelToken| panic!("resolution must not infer"),
        ))));
        b.grants = Some(GrantSet::of(["llm.chat"]));
        let input = json!({"harness":"claude"});
        let out = b.call("ai.resolve", input.clone()).unwrap();
        assert_eq!(out, json!({"harness":"claude","speed":"standard"}));
        assert!(!attempted(&b.writer.records));
        let mut replay = broker(None);
        replay.mode = Mode::Replay;
        replay.cursor = Some(ReplayCursor::new(&b.writer.records));
        assert_eq!(replay.call("ai.resolve", input.clone()).unwrap(), out);
        b.grants = Some(GrantSet::of(["ai.resolve"]));
        assert_eq!(
            b.call("ai.resolve", input).unwrap_err().type_,
            "capability_denied"
        );
    }

    #[test]
    fn effort_is_preserved_in_recorded_resolution_generation_and_strict_replay() {
        for effort in [
            "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
        ] {
            let expected: any_ai::Effort = serde_json::from_value(json!(effort)).unwrap();
            let mut b = broker(Some(Arc::new(Mock(
                move |request: &GenerateRequest, _: &CancelToken| {
                    assert_eq!(request.effort, Some(expected));
                    assert_eq!(request.model.as_deref(), Some("test-model"));
                    Ok(response())
                },
            ))));
            let selection = json!({"harness":"codex", "model":"test-model", "effort":effort});
            let resolved = b.call("ai.resolve", selection.clone()).unwrap();
            assert_eq!(resolved["effort"], selection["effort"]);
            assert_eq!(resolved["speed"], "standard");
            let mut input = request();
            input["model"] = resolved["model"].clone();
            input["effort"] = resolved["effort"].clone();
            let output = b.call("ai.generate", input.clone()).unwrap();
            let mut replay = broker(None);
            replay.mode = Mode::Replay;
            replay.cursor = Some(ReplayCursor::new(&b.writer.records));
            assert_eq!(replay.call("ai.resolve", selection).unwrap(), resolved);
            assert_eq!(replay.call("ai.generate", input).unwrap(), output);
            assert!(!attempted(&replay.writer.records));
        }
    }

    #[test]
    fn malformed_efforts_and_effort_without_resolved_model_never_generate() {
        let mut b = broker(Some(Arc::new(Mock(
            |_: &GenerateRequest, _: &CancelToken| panic!("invalid effort must not generate"),
        ))));
        for effort in [
            json!("ultracode"),
            json!("HIGH"),
            json!(""),
            json!(true),
            json!(1),
            json!([]),
            json!({}),
        ] {
            let selection = json!({"harness":"codex", "model":"test-model", "effort":effort});
            assert_eq!(
                b.call("ai.resolve", selection).unwrap_err().type_,
                "ai.invalid_request"
            );
            let mut input = request();
            input["model"] = json!("test-model");
            input["effort"] = effort;
            assert_eq!(
                b.call("ai.generate", input).unwrap_err().type_,
                "ai.invalid_request"
            );
        }
        let selection = json!({"harness":"codex", "effort":"high"});
        assert_eq!(
            b.call("ai.resolve", selection).unwrap_err().type_,
            "ai.invalid_request"
        );
        let mut input = request();
        input["effort"] = json!("high");
        assert_eq!(
            b.call("ai.generate", input).unwrap_err().type_,
            "ai.invalid_request"
        );
    }

    #[test]
    fn speed_is_preserved_in_recorded_resolution_generation_and_strict_replay() {
        for speed in ["standard", "fast"] {
            let expected: any_ai::Speed = serde_json::from_value(json!(speed)).unwrap();
            let mut live = broker(Some(Arc::new(Mock(
                move |request: &GenerateRequest, _: &CancelToken| {
                    assert_eq!(request.speed, Some(expected));
                    Ok(response())
                },
            ))));
            let selection = json!({"harness":"codex","model":"test","speed":speed});
            let resolved = live.call("ai.resolve", selection.clone()).unwrap();
            assert_eq!(resolved, selection);
            let mut input = request();
            input["model"] = resolved["model"].clone();
            input["speed"] = resolved["speed"].clone();
            let output = live.call("ai.generate", input.clone()).unwrap();
            let mut replay = broker(None);
            replay.mode = Mode::Replay;
            replay.cursor = Some(ReplayCursor::new(&live.writer.records));
            assert_eq!(replay.call("ai.resolve", selection).unwrap(), resolved);
            assert_eq!(replay.call("ai.generate", input).unwrap(), output);
            assert!(!attempted(&replay.writer.records));
        }
    }

    #[test]
    fn invalid_speed_and_fast_without_model_never_reach_the_service() {
        let mut live = broker(Some(Arc::new(Mock(
            |_: &GenerateRequest, _: &CancelToken| panic!("invalid speed must not generate"),
        ))));
        for speed in [
            json!("ultrafast"),
            json!("FAST"),
            json!(true),
            json!(1),
            json!([]),
            json!({}),
        ] {
            assert_eq!(
                live.call(
                    "ai.resolve",
                    json!({
                        "harness":"codex","model":"test","speed":speed
                    })
                )
                .unwrap_err()
                .type_,
                "ai.invalid_request"
            );
            let mut input = request();
            input["model"] = json!("test");
            input["speed"] = speed;
            assert_eq!(
                live.call("ai.generate", input).unwrap_err().type_,
                "ai.invalid_request"
            );
        }
        assert_eq!(
            live.call(
                "ai.resolve",
                json!({
                    "harness":"codex","speed":"fast"
                })
            )
            .unwrap_err()
            .type_,
            "ai.invalid_request"
        );
        let mut input = request();
        input["speed"] = json!("fast");
        assert_eq!(
            live.call("ai.generate", input).unwrap_err().type_,
            "ai.invalid_request"
        );
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
