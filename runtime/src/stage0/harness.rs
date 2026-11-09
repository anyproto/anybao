//! Stage-0 only: immutable components, two idle stores, fixed K2 cases (ADR-034 §0).
use super::{
    on_run_thread, FuelSample, IdleStore, NativeFailure, RecordingLimiter, ResourceSample,
};
use crate::broker::Broker;
use crate::engine_profile::{sha256, ArtifactManifestV1};
use crate::runner::{Cage, ExecutionStop, FUEL_PER_CELL};
use anyhow::{ensure, Result};
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use wasmtime::component::{Component, Linker};
use wasmtime::{Store, UpdateDeadline};

pub const PROTOCOL: u32 = 1;
pub const LOCKED_KERNEL_SHA256: &str =
    "81c7324e57bc924bd8b8f17adba9fe45c9261294fc102c0d5efb57eba82b7cf8";
pub const FIXTURE_MANIFEST_SHA256: &str =
    "sha256:55d8528d3ac597c2a5c2d2cb3ea344d4a8bb480a0f163f4e9d5a903fb503514a";
pub const K2_CASE_COUNT: u32 = 200;

pub struct Stage0Runtime {
    pub cage: Arc<Cage>,
    trap: Component,
    kernel_manifest: ArtifactManifestV1,
    trap_manifest: ArtifactManifestV1,
    idle: Vec<IdleStore>,
    load_ms: u128,
}

fn empty_broker(id: &str) -> Broker {
    Broker::new(
        crate::trace::TraceWriter::new(json!({"id": id, "mocked": false})),
        Default::default(),
        Default::default(),
        None,
        crate::routes::Classifier::new(None),
    )
}

impl Stage0Runtime {
    /// # Safety
    /// Both artifact/manifest pairs must be immutable bytes from the trusted build.
    /// The JNI adapter calls this only with include_bytes!, never request paths.
    pub unsafe fn from_trusted_artifacts(
        kernel: &[u8],
        kernel_manifest: ArtifactManifestV1,
        trap: &[u8],
        trap_manifest: ArtifactManifestV1,
    ) -> Result<Self> {
        let start = Instant::now();
        ensure!(kernel_manifest.provenance.jni_protocol_version == Some(PROTOCOL));
        ensure!(trap_manifest.provenance.jni_protocol_version == Some(PROTOCOL));
        // SAFETY: the caller established build provenance for both pairs.
        let cage = unsafe { Cage::from_trusted_artifact(kernel, &kernel_manifest)? };
        trap_manifest.verify(trap, &cage.engine)?;
        let trap = unsafe { Component::deserialize(&cage.engine, trap)? };
        Ok(Self {
            cage,
            trap,
            kernel_manifest,
            trap_manifest,
            idle: Vec::new(),
            load_ms: start.elapsed().as_millis(),
        })
    }

    pub fn health(&self) -> Value {
        json!({
            "loaded": true, "idle_store_count": self.idle.len(),
            "native_load_ms": self.load_ms,
            "kernel_manifest": self.kernel_manifest, "trap_manifest": self.trap_manifest,
            "fixture_manifest_sha256": FIXTURE_MANIFEST_SHA256,
            "k2_case_count": K2_CASE_COUNT,
            "oob_signal_evidence": "requires_artifact_fault_mapping_and_device_run",
            "phone_qualification": "unmeasured"
        })
    }

    pub fn prepare_idle(&mut self) -> Result<Value> {
        ensure!(self.idle.is_empty(), "idle stores already prepared");
        let start = Instant::now();
        let first = IdleStore::initialize(&self.cage, empty_broker("stage0-idle-1"))?;
        let second = IdleStore::initialize(&self.cage, empty_broker("stage0-idle-2"))?;
        self.idle = vec![first, second];
        Ok(
            json!({"idle_store_count": 2, "prepare_ms": start.elapsed().as_millis(),
            "idle_resources": self.idle_resources(), "aggregate_resources": self.aggregate(Default::default())}),
        )
    }

    pub fn idle_resources(&self) -> Vec<ResourceSample> {
        self.idle.iter().map(IdleStore::resources).collect()
    }

    pub fn aggregate(&self, chat: ResourceSample) -> ResourceSample {
        self.idle.iter().fold(chat, |sum, store| {
            let sample = store.resources();
            ResourceSample {
                memory_bytes: sum.memory_bytes + sample.memory_bytes,
                peak_memory_bytes: sum.peak_memory_bytes + sample.peak_memory_bytes,
                table_elements: sum.table_elements + sample.table_elements,
                peak_table_elements: sum.peak_table_elements + sample.peak_table_elements,
            }
        })
    }

    pub fn require_idle(&self) -> Result<()> {
        ensure!(
            self.idle.len() == 2,
            "prepare exactly two idle stores before real turns"
        );
        Ok(())
    }

    pub fn turn(
        &self,
        request: crate::stage0_fixture::TurnRequest,
        cancel: Arc<AtomicBool>,
    ) -> Result<Value> {
        let started = Instant::now();
        self.require_idle()?;
        ensure!(
            request.manifest_sha256 == FIXTURE_MANIFEST_SHA256,
            "untrusted fixture manifest"
        );
        let prepared = crate::stage0_fixture::prepare_turn(request)?;
        let outcome = super::run_program(
            &self.cage,
            prepared.broker,
            prepared.spec,
            &prepared.args,
            Default::default(),
            cancel,
        )?;
        let aggregate = self.aggregate(outcome.resources);
        let mut result =
            crate::stage0_fixture::finish_turn(outcome, prepared.trace_store, &prepared.reply_url)?;
        result["aggregate_resources"] = serde_json::to_value(aggregate)?;
        result["idle_resources"] = serde_json::to_value(self.idle_resources())?;
        result["native_turn_elapsed_ms"] = json!(started.elapsed().as_millis());
        Ok(result)
    }

    pub fn k2_case(&mut self, index: u32, cancel: Arc<AtomicBool>) -> Result<Value> {
        ensure!(index < K2_CASE_COUNT, "K2 index must be 0..199");
        on_run_thread(move || self.k2_case_on_thread(index, cancel))
    }

    pub fn python_recursion_diagnostic(&mut self, cancel: Arc<AtomicBool>) -> Result<Value> {
        on_run_thread(move || {
            let (mut store, kernel) = crate::runner::prepare_store(
                &self.cage,
                empty_broker("python-recursion-diagnostic"),
                Default::default(),
                cancel,
                5.0,
            )?;
            let raw = kernel.call_run_cell(
                &mut store,
                "def recurse():\n    return recurse()\nrecurse()",
                "python-recursion",
            );
            let native_failure = raw.as_ref().err().map(NativeFailure::from_error);
            Ok(json!({"diagnostic_only": true, "included_in_k2": false,
                "python_reply": raw.ok(), "native_failure": native_failure}))
        })
    }

    fn k2_case_on_thread(&self, index: u32, cancel: Arc<AtomicBool>) -> Result<Value> {
        let group = index / 40;
        let repetition = index % 40;
        let (name, expected) = [
            ("python_success", "Success"),
            ("guard_page_oob", "MemoryOutOfBounds"),
            ("fuel_loop", "OutOfFuel"),
            ("epoch_loop", "Interrupted"),
            ("non_tail_recursion", "StackOverflow"),
        ][group as usize];
        let fuel = if group == 2 { 1_000_000 } else { FUEL_PER_CELL };
        let code = format!("sum(range({}))", repetition + 1);
        let input = json!({"index": index, "group": name, "repetition": repetition,
            "python_cell": if group == 0 {Some(code.as_str())} else {None},
            "address": if group == 1 {Some(65_536 + 4 * repetition)} else {None},
            "recursion_input": if group == 4 {Some(repetition)} else {None},
            "fuel": fuel, "epoch_tick_ms": 10, "epoch_interrupt_requested_ms": if group == 3 {Some(100)} else {None},
            "owner_watchdog_ms": 5000});
        let start = Instant::now();
        let (actual, raw_failure, resources, fuel_sample, epoch_actual_ms) = if group == 0 {
            let (mut store, kernel) = crate::runner::prepare_store(
                &self.cage,
                empty_broker("k2-python"),
                Default::default(),
                cancel,
                5.0,
            )?;
            let raw = kernel.call_run_cell(&mut store, &code, "k2-python");
            store.data_mut().resources.commit_pending();
            let failure = raw.as_ref().err().map(NativeFailure::from_error);
            let actual = match &raw {
                Ok(reply) if serde_json::from_str::<Value>(reply)?["ok"] == true => {
                    "Success".to_string()
                }
                Ok(_) => "PythonError".into(),
                Err(_) => failure.as_ref().unwrap().cause.clone(),
            };
            (
                actual,
                failure,
                store.data().resources.sample(),
                FuelSample::after_call(fuel, store.get_fuel().ok(), raw.is_ok()),
                None,
            )
        } else {
            let mut store = Store::new(&self.cage.engine, RecordingLimiter::default());
            store.limiter(|data| data);
            store.set_fuel(fuel)?;
            store.set_epoch_deadline(1);
            let interrupt = Arc::new(AtomicBool::new(false));
            let requested_ms = Arc::new(AtomicU64::new(0));
            let flag = interrupt.clone();
            store.epoch_deadline_callback(move |_| {
                if cancel.load(Ordering::Relaxed) || flag.load(Ordering::Relaxed) {
                    return Err(wasmtime::Error::new(ExecutionStop::Interrupted));
                }
                if start.elapsed() >= Duration::from_secs(5) {
                    return Err(wasmtime::Error::new(ExecutionStop::Timeout));
                }
                Ok(UpdateDeadline::Continue(1))
            });
            let linker = Linker::new(&self.cage.engine);
            let instance = linker.instantiate(&mut store, &self.trap)?;
            let timer = (group == 3).then(|| {
                let requested_ms = requested_ms.clone();
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_millis(100));
                    requested_ms.store(start.elapsed().as_millis() as u64, Ordering::Relaxed);
                    interrupt.store(true, Ordering::Relaxed);
                })
            });
            let raw = match group {
                1 => instance
                    .get_typed_func::<(u32,), (u32,)>(&mut store, "oob")?
                    .call(&mut store, (65_536 + 4 * repetition,))
                    .map(|_| ()),
                2 | 3 => instance
                    .get_typed_func::<(), ()>(&mut store, "spin")?
                    .call(&mut store, ()),
                4 => instance
                    .get_typed_func::<(u32,), (u32,)>(&mut store, "recurse")?
                    .call(&mut store, (repetition,))
                    .map(|_| ()),
                _ => unreachable!(),
            };
            if let Some(timer) = timer {
                timer.join().map_err(|_| super::RunThreadPanic)?;
            }
            store.data_mut().commit_pending();
            let failure = raw.as_ref().err().map(NativeFailure::from_error);
            let actual = failure
                .as_ref()
                .map_or("Success", |f| f.cause.as_str())
                .to_string();
            let epoch_actual = (group == 3).then(|| requested_ms.load(Ordering::Relaxed));
            (
                actual,
                failure,
                store.data().sample(),
                FuelSample::after_call(fuel, store.get_fuel().ok(), raw.is_ok()),
                epoch_actual,
            )
        };
        let manifest = if group == 0 {
            &self.kernel_manifest
        } else {
            &self.trap_manifest
        };
        let mut result = json!({"index": index, "input": input,
            "input_sha256": sha256(crate::trace::canonical_json(&input).as_bytes()),
            "source_sha256": manifest.source_sha256, "artifact_sha256": manifest.artifact_sha256,
            "expected": expected, "actual": actual, "classification_matches": actual == expected,
            "raw_failure": raw_failure, "duration_ms": start.elapsed().as_millis(),
            "epoch_interrupt_actual_ms": epoch_actual_ms, "resources": resources,
            "aggregate_resources": self.aggregate(resources),
            "signal_path_verified_on_device": Value::Null,
            "k2_verdict": "requires_all_cases_external_watchdog_and_signal_evidence"});
        result.as_object_mut().unwrap().extend(
            serde_json::to_value(fuel_sample)?
                .as_object()
                .unwrap()
                .clone(),
        );
        Ok(result)
    }
}

#[cfg(all(test, feature = "desktop", feature = "precompiler"))]
mod tests {
    use super::*;
    use crate::engine_profile::{BuildProvenance, EngineProfileV1};

    #[test]
    fn trusted_components_complete_all_200_cases_with_two_idle_stores() {
        let profile = EngineProfileV1::native();
        let engine = wasmtime::Engine::new(&profile.config().unwrap()).unwrap();
        let provenance = BuildProvenance {
            anyrt_revision: "host-harness-test".into(),
            rust_version: "test".into(),
            ndk_version: "not-used-host-test".into(),
            jni_protocol_version: Some(PROTOCOL),
            any_version_pin: "v0.3.5".into(),
            any_versions_tested: vec![],
        };
        let locked_source = std::env::var("ANYRT_STAGE0_LOCKED_KERNEL")
            .ok()
            .map(|path| {
                let bytes = std::fs::read(path).unwrap();
                assert_eq!(sha256(&bytes), LOCKED_KERNEL_SHA256);
                bytes
            });
        let kernel_source = locked_source
            .as_deref()
            .unwrap_or(crate::runner::EMBEDDED_KERNEL);
        let trap_source = wat::parse_str(include_str!("../../fixtures/stage0-traps.wat")).unwrap();
        let kernel = engine.precompile_component(kernel_source).unwrap();
        let trap = engine.precompile_component(&trap_source).unwrap();
        let kernel_manifest = ArtifactManifestV1::new(
            profile.clone(),
            &engine,
            kernel_source,
            &kernel,
            provenance.clone(),
        );
        let trap_manifest =
            ArtifactManifestV1::new(profile, &engine, &trap_source, &trap, provenance);
        // SAFETY: the test just compiled these exact bytes on this trusted host.
        let mut runtime = unsafe {
            Stage0Runtime::from_trusted_artifacts(&kernel, kernel_manifest, &trap, trap_manifest)
        }
        .unwrap();
        assert_eq!(runtime.health()["idle_store_count"], 0);
        runtime.prepare_idle().unwrap();
        assert!(runtime.prepare_idle().is_err());
        runtime.require_idle().unwrap();
        let mut records = Vec::new();
        for index in 0..K2_CASE_COUNT {
            let result = runtime
                .k2_case(index, Arc::new(AtomicBool::new(false)))
                .unwrap();
            assert_eq!(result["classification_matches"], true, "{result}");
            if index < 40 {
                assert_eq!(result["fuel_accounting"], "exact");
                assert_eq!(result["fuel_used"], result["fuel_snapshot_delta"]);
                assert!(result["fuel_used"].as_u64().unwrap() > 0);
            } else {
                assert_eq!(result["fuel_accounting"], "snapshot_only");
                assert!(result["fuel_used"].is_null());
                assert_eq!(
                    result["fuel_snapshot_delta"].as_u64().unwrap(),
                    result["fuel_initial"].as_u64().unwrap()
                        - result["fuel_remaining"].as_u64().unwrap()
                );
            }
            assert_eq!(result["input_sha256"].as_str().unwrap().len(), 64);
            assert!(
                result["aggregate_resources"]["memory_bytes"]
                    .as_u64()
                    .unwrap()
                    > result["resources"]["memory_bytes"].as_u64().unwrap()
            );
            if (120..160).contains(&index) {
                assert!(result["fuel_remaining"].as_u64().unwrap() > 0);
                assert!(result["epoch_interrupt_actual_ms"].as_u64().unwrap() >= 100);
            }
            records.push(result);
        }
        assert!(runtime
            .k2_case(200, Arc::new(AtomicBool::new(false)))
            .is_err());
        let diagnostic = runtime
            .python_recursion_diagnostic(Arc::new(AtomicBool::new(false)))
            .unwrap();
        assert_eq!(diagnostic["included_in_k2"], false);
        assert!(diagnostic["native_failure"].is_null());
        assert!(diagnostic["python_reply"]
            .as_str()
            .unwrap()
            .contains("RecursionError"));
        // The same runner used by real turns must preserve abrupt-trap fuel
        // precision in both the returned outcome and its exported cell record.
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("spin@v1.py"),
            "def main(args):\n    while True:\n        pass\n",
        )
        .unwrap();
        let mut broker = empty_broker("failed-turn-fuel");
        broker.resolver = Some(Box::new(crate::resolver::LocalDirResolver::new(
            dir.path().into(),
        )));
        let outcome = on_run_thread(|| {
            crate::runner::run_program(
                &runtime.cage,
                broker,
                "spin@v1",
                &json!({}),
                Default::default(),
                Arc::new(AtomicBool::new(false)),
                1.0,
            )
        })
        .unwrap();
        assert_eq!(outcome.status, "error");
        assert_eq!(outcome.native_failure.as_ref().unwrap().cause, "Timeout");
        assert_eq!(outcome.fuel.fuel_accounting, "snapshot_only");
        assert!(outcome.fuel.fuel_used.is_none());
        let snapshot = outcome.fuel.fuel_snapshot_delta;
        let failed_turn = crate::stage0_fixture::finish_turn(
            outcome,
            Arc::new(crate::tracestore::MemTraceStore::bounded(1_048_576)),
            "unused",
        )
        .unwrap();
        assert_eq!(failed_turn["fuel_accounting"], "snapshot_only");
        assert!(failed_turn["fuel_used"].is_null());
        assert_eq!(failed_turn["fuel_snapshot_delta"], json!(snapshot));
        let cell = failed_turn["trace"]["runs"]["failed-turn-fuel"]
            .as_array()
            .unwrap()
            .iter()
            .find(|record| record["kind"] == "cell")
            .unwrap();
        assert_eq!(cell["metrics"]["fuel_accounting"], "snapshot_only");
        assert!(cell["metrics"]["fuel_used"].is_null());
        if let Ok(path) = std::env::var("ANYRT_STAGE0_HOST_EVIDENCE") {
            std::fs::write(
                path,
                serde_json::to_vec_pretty(&json!({
                    "scope": "host_only_not_android_K2", "health": runtime.health(),
                    "cases": records, "python_recursion_diagnostic": diagnostic,
                    "failed_turn_fuel": failed_turn
                }))
                .unwrap(),
            )
            .unwrap();
        }
    }
}
