//! Bounded native spike execution and recording-only guest accounting (ADR-034 §0).
use crate::broker::{Broker, SharedMailbox};
use crate::engine_profile::RUN_THREAD_STACK_BYTES;
use crate::runner::{Cage, Host, RunOutcome};
use anyhow::{anyhow, Result};
use serde::Serialize;
use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use wasmtime::{ResourceLimiter, Store};

pub mod harness;
pub mod protocol;

#[derive(Debug)]
pub struct RunThreadPanic;

impl std::fmt::Display for RunThreadPanic {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Stage-0 native run thread panicked; retire the worker")
    }
}
impl std::error::Error for RunThreadPanic {}

/// Run every spike wasm call, including instantiation, on the pinned native stack.
/// The background JNI caller joins this thread; Rust never calls into the JVM.
pub fn on_run_thread<T: Send>(f: impl FnOnce() -> Result<T> + Send) -> Result<T> {
    std::thread::scope(|scope| {
        std::thread::Builder::new()
            .name("bao-spike-run".into())
            .stack_size(RUN_THREAD_STACK_BYTES)
            .spawn_scoped(scope, f)?
            .join()
            .map_err(|_| anyhow!(RunThreadPanic))?
    })
}

/// JNI/calibration run entry: includes instantiation on the explicit Rust stack.
pub fn run_program(
    cage: &Cage,
    broker: Broker,
    spec: &str,
    args: &serde_json::Value,
    mailbox: SharedMailbox,
    interrupt: Arc<AtomicBool>,
) -> Result<RunOutcome> {
    on_run_thread(|| {
        crate::runner::run_program(cage, broker, spec, args, mailbox, interrupt, 120.0)
    })
}

#[derive(Debug, Serialize)]
pub struct NativeFailure {
    pub cause: String,
    pub detail: String,
}

impl NativeFailure {
    pub(crate) fn from_anyhow(error: &anyhow::Error) -> Self {
        let cause = if let Some(trap) = error.downcast_ref::<wasmtime::Trap>() {
            format!("{trap:?}")
        } else if let Some(stop) = error.downcast_ref::<crate::runner::ExecutionStop>() {
            format!("{stop:?}")
        } else {
            "Other".into()
        };
        Self {
            cause,
            detail: format!("{error:#}"),
        }
    }

    pub(crate) fn from_error(error: &wasmtime::Error) -> Self {
        let cause = if let Some(trap) = error.downcast_ref::<wasmtime::Trap>() {
            format!("{trap:?}")
        } else if let Some(stop) = error.downcast_ref::<crate::runner::ExecutionStop>() {
            format!("{stop:?}")
        } else {
            "Other".into()
        };
        Self {
            cause,
            detail: format!("{error:#}"),
        }
    }
}

/// Wasmtime's fuel register need not be flushed to the store after an abrupt
/// trap. Preserve its raw snapshots without treating their difference as exact.
#[derive(Clone, Copy, Debug, Serialize)]
pub struct FuelSample {
    pub fuel_initial: u64,
    pub fuel_remaining: Option<u64>,
    pub fuel_snapshot_delta: Option<u64>,
    pub fuel_used: Option<u64>,
    pub fuel_accounting: &'static str,
}

impl FuelSample {
    pub(crate) fn after_call(initial: u64, remaining: Option<u64>, returned: bool) -> Self {
        let delta = remaining.map(|remaining| initial.saturating_sub(remaining));
        Self {
            fuel_initial: initial,
            fuel_remaining: remaining,
            fuel_snapshot_delta: delta,
            fuel_used: delta.filter(|_| returned),
            fuel_accounting: match (remaining, returned) {
                (None, _) => "unavailable",
                (Some(_), false) => "snapshot_only",
                (Some(_), true) => "exact",
            },
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize)]
pub struct ResourceSample {
    pub memory_bytes: usize,
    pub peak_memory_bytes: usize,
    pub table_elements: usize,
    pub peak_table_elements: usize,
}

/// This observes guest allocations only; native/host RSS and PSS need OS samples.
#[derive(Default)]
pub struct RecordingLimiter {
    sample: ResourceSample,
    pending_memory: usize,
    pending_table: usize,
}

impl RecordingLimiter {
    // Wasmtime has a failure callback but no success callback. Commit at the
    // next callback/host boundary or after the wasm call returns, never while
    // a requested allocation could still fail.
    pub(crate) fn commit_pending(&mut self) {
        self.sample.memory_bytes += std::mem::take(&mut self.pending_memory);
        self.sample.table_elements += std::mem::take(&mut self.pending_table);
        self.sample.peak_memory_bytes = self.sample.peak_memory_bytes.max(self.sample.memory_bytes);
        self.sample.peak_table_elements = self
            .sample
            .peak_table_elements
            .max(self.sample.table_elements);
    }

    pub fn sample(&self) -> ResourceSample {
        self.sample
    }
}

impl ResourceLimiter for RecordingLimiter {
    fn instances(&self) -> usize {
        usize::MAX
    }
    fn memories(&self) -> usize {
        usize::MAX
    }
    fn tables(&self) -> usize {
        usize::MAX
    }

    fn memory_growing(
        &mut self,
        current: usize,
        desired: usize,
        _maximum: Option<usize>,
    ) -> wasmtime::Result<bool> {
        self.commit_pending();
        self.pending_memory = desired.saturating_sub(current);
        Ok(true)
    }

    fn memory_grow_failed(&mut self, _error: wasmtime::Error) -> wasmtime::Result<()> {
        self.pending_memory = 0;
        Ok(())
    }

    fn table_growing(
        &mut self,
        current: usize,
        desired: usize,
        _maximum: Option<usize>,
    ) -> wasmtime::Result<bool> {
        self.commit_pending();
        self.pending_table = desired.saturating_sub(current);
        Ok(true)
    }

    fn table_grow_failed(&mut self, _error: wasmtime::Error) -> wasmtime::Result<()> {
        self.pending_table = 0;
        Ok(())
    }
}

/// Keep two initialized stores in the run-thread closure throughout measured turns.
/// Dropping this value releases the store; it never starts serve or a trace store.
pub struct IdleStore(Store<Host>);

impl IdleStore {
    pub fn initialize(cage: &Cage, broker: Broker) -> Result<Self> {
        on_run_thread(|| Self::initialize_on_thread(cage, broker))
    }

    fn initialize_on_thread(cage: &Cage, broker: Broker) -> Result<Self> {
        let (mut store, kernel) = crate::runner::prepare_store(
            cage,
            broker,
            Default::default(),
            Arc::new(AtomicBool::new(false)),
            120.0,
        )?;
        let reply = kernel.call_run_cell(&mut store, "None", "stage0-idle")?;
        let reply: serde_json::Value = serde_json::from_str(&reply)?;
        anyhow::ensure!(
            reply["ok"] == true,
            "idle kernel initialization failed: {reply}"
        );
        store.data_mut().resources.commit_pending();
        Ok(Self(store))
    }

    pub fn resources(&self) -> ResourceSample {
        self.0.data().resources.sample()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn failed_growth_never_inflates_successful_peak() {
        let mut limiter = RecordingLimiter::default();
        limiter.memory_growing(0, 65_536, None).unwrap();
        limiter.table_growing(0, 20, None).unwrap();
        limiter.memory_growing(65_536, 131_072, None).unwrap();
        limiter
            .memory_grow_failed(wasmtime::Error::msg("allocation failed"))
            .unwrap();
        limiter.table_growing(20, 30, None).unwrap();
        limiter
            .table_grow_failed(wasmtime::Error::msg("allocation failed"))
            .unwrap();
        limiter.commit_pending();
        assert_eq!(
            limiter.sample(),
            ResourceSample {
                memory_bytes: 65_536,
                peak_memory_bytes: 65_536,
                table_elements: 20,
                peak_table_elements: 20
            }
        );
    }

    #[test]
    fn wasmtime_growth_records_success_and_rolls_back_failure() {
        let profile = crate::engine_profile::EngineProfileV1::native();
        let engine = wasmtime::Engine::new(&profile.config().unwrap()).unwrap();
        let mut store = Store::new(&engine, RecordingLimiter::default());
        store.limiter(|limiter| limiter);
        let memory =
            wasmtime::Memory::new(&mut store, wasmtime::MemoryType::new(1, Some(2))).unwrap();
        memory.grow(&mut store, 1).unwrap();
        assert!(memory.grow(&mut store, 1).is_err());
        let _second =
            wasmtime::Memory::new(&mut store, wasmtime::MemoryType::new(1, Some(1))).unwrap();
        store.data_mut().commit_pending();
        assert_eq!(store.data().sample().memory_bytes, 3 * 65_536);
        assert_eq!(store.data().sample().peak_memory_bytes, 3 * 65_536);
    }

    #[cfg(feature = "desktop")]
    #[test]
    fn trusted_kernel_runs_with_two_initialized_idle_stores() {
        use crate::engine_profile::{ArtifactManifestV1, BuildProvenance, EngineProfileV1};
        let profile = EngineProfileV1::native();
        let engine = wasmtime::Engine::new(&profile.config().unwrap()).unwrap();
        let source = crate::runner::EMBEDDED_KERNEL;
        let artifact = engine.precompile_component(source).unwrap();
        let manifest = ArtifactManifestV1::new(
            profile,
            &engine,
            source,
            &artifact,
            BuildProvenance {
                anyrt_revision: "test".into(),
                rust_version: "test".into(),
                ndk_version: "not-used-host-test".into(),
                jni_protocol_version: None,
                any_version_pin: "unqualified".into(),
                any_versions_tested: vec![],
            },
        );
        // SAFETY: these bytes were produced immediately above by this trusted build.
        let cage = unsafe { Cage::from_trusted_artifact(&artifact, &manifest) }.unwrap();
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("smoke@v1.py"),
            "def main(args):\n    return 7\n",
        )
        .unwrap();
        let broker = || {
            Broker::new(
                crate::trace::TraceWriter::new(serde_json::json!({"id": "native-smoke"})),
                Default::default(),
                Default::default(),
                Some(dir.path().to_path_buf()),
                crate::routes::Classifier::new(None),
            )
        };
        let idle_one = IdleStore::initialize(&cage, broker()).unwrap();
        let idle_two = IdleStore::initialize(&cage, broker()).unwrap();
        let outcome = run_program(
            &cage,
            broker(),
            "smoke@v1",
            &serde_json::json!({}),
            Default::default(),
            Arc::new(AtomicBool::new(false)),
        )
        .unwrap();
        assert_eq!(outcome.status, "ok", "{:?}", outcome.error);
        assert_eq!(outcome.value, serde_json::json!(7));
        assert!(idle_one.resources().memory_bytes > 0);
        assert!(idle_two.resources().memory_bytes > 0);
        assert!(outcome.resources.peak_memory_bytes > 0);
        assert!(outcome.native_failure.is_none());
    }

    #[test]
    fn run_thread_contains_panics_and_returns_results() {
        assert_eq!(on_run_thread(|| Ok(42)).unwrap(), 42);
        assert!(on_run_thread::<()>(|| panic!("test panic")).is_err());
    }
}
