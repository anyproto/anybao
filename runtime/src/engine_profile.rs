//! Trusted AOT build/load contract (ADR-034 §0, §2).

use anyhow::{ensure, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use wasmtime::{Config, Engine, InstanceAllocationStrategy, WasmBacktraceDetails, WasmFeatures};

pub const WASMTIME_VERSION: &str = "49.0.3";
pub const ANDROID_TARGET: &str = "aarch64-linux-android";
pub const BUILD_TARGET: &str = env!("ANYRT_BUILD_TARGET");
pub const WASM_STACK_BYTES: usize = 512 * 1024;
pub const ASYNC_STACK_BYTES: usize = 2 * 1024 * 1024;
pub const RUN_THREAD_STACK_BYTES: usize = 8 * 1024 * 1024;
pub const RUNTIME_FEATURES: &[&str] = &[
    "anyhow",
    "runtime",
    "std",
    "component-model",
    "async",
    "backtrace",
];
pub const PRECOMPILER_FEATURES: &[&str] = &["cranelift", "all-arch", "parallel-compilation"];
const ARM64_FLAGS: &[&str] = &[
    "has_lse",
    "has_pauth",
    "has_fp16",
    "has_dotprod",
    "has_i8mm",
    "sign_return_address_all",
    "sign_return_address",
    "sign_return_address_with_bkey",
    "use_bti",
    "use_csdb",
];

pub fn sha256(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EngineProfileV1 {
    pub target: String,
}

impl EngineProfileV1 {
    pub fn android() -> Self {
        Self {
            target: ANDROID_TARGET.into(),
        }
    }

    pub fn native() -> Self {
        Self {
            target: BUILD_TARGET.into(),
        }
    }

    fn proposals() -> WasmFeatures {
        WasmFeatures::MUTABLE_GLOBAL
            | WasmFeatures::SATURATING_FLOAT_TO_INT
            | WasmFeatures::SIGN_EXTENSION
            | WasmFeatures::FLOATS
            | WasmFeatures::BULK_MEMORY
            | WasmFeatures::MULTI_VALUE
            | WasmFeatures::MULTI_MEMORY
            | WasmFeatures::REFERENCE_TYPES
            | WasmFeatures::SIMD
            | WasmFeatures::COMPONENT_MODEL
            | WasmFeatures::EXTENDED_CONST
    }

    pub fn config(&self) -> Result<Config> {
        let mut cfg = Config::new();
        // Setting a target also disables inference of the build machine's CPU flags.
        cfg.target(&self.target)?;
        cfg.wasm_features(WasmFeatures::all(), false)
            .wasm_features(Self::proposals(), true)
            .consume_fuel(true)
            .epoch_interruption(true)
            .max_wasm_stack(WASM_STACK_BYTES)
            .async_stack_size(ASYNC_STACK_BYTES)
            .async_stack_zeroing(false)
            .concurrency_support(false)
            .allocation_strategy(InstanceAllocationStrategy::OnDemand)
            .memory_reservation(512 * 1024 * 1024)
            .memory_guard_size(64 * 1024)
            .memory_reservation_for_growth(0)
            .memory_may_move(true)
            .guard_before_linear_memory(true)
            .memory_init_cow(true)
            .memory_guaranteed_dense_image_size(16 * 1024 * 1024)
            .table_lazy_init(true)
            .signals_based_traps(true)
            .wasm_backtrace_details(WasmBacktraceDetails::Enable)
            .wasm_backtrace_max_frames(std::num::NonZeroUsize::new(20))
            .generate_address_map(true)
            .debug_info(false)
            .debug_symbols(false)
            .native_unwind_info(true)
            .wasm_branch_hinting(false)
            .relaxed_simd_deterministic(false)
            .metadata_for_internal_asserts(false);
        #[cfg(feature = "compiler")]
        {
            cfg.strategy(wasmtime::Strategy::Cranelift)
                .cranelift_opt_level(wasmtime::OptLevel::Speed)
                .cranelift_nan_canonicalization(false);
            if self.target.starts_with("aarch64-") {
                for flag in ARM64_FLAGS {
                    // Pinned 0.136.3 ARM64 ISA settings; no optional instructions enabled.
                    unsafe {
                        cfg.cranelift_flag_set(flag, "false");
                    }
                }
            }
        }
        Ok(cfg)
    }

    /// Complete profile values used for both the manifest and its integrity hash.
    pub fn resolved(&self, engine: &Engine) -> Value {
        let cpu_flags: std::collections::BTreeMap<_, _> = if self.target.starts_with("aarch64-") {
            ARM64_FLAGS.iter().map(|name| (*name, false)).collect()
        } else {
            std::collections::BTreeMap::new()
        };
        json!({
            "version": 1, "revision": 2, "target": self.target, "cpu_baseline": "explicit-target-no-host-inference",
            "cpu_flags": cpu_flags, "wasm_proposals": engine.get_wasm_features().iter_names().map(|(n, _)| n).collect::<Vec<_>>(),
            "fuel": engine.get_consume_fuel(), "epochs": engine.get_epoch_interruption(),
            "max_wasm_stack": engine.get_max_wasm_stack(), "async_stack_size": engine.get_async_stack_size(),
            "run_thread_stack": RUN_THREAD_STACK_BYTES, "async_stack_zeroing": engine.get_async_stack_zeroing(),
            "component_calls": "synchronous", "concurrency": engine.get_concurrency_support(),
            "allocation": "on-demand", "memory_reservation": engine.get_memory_reservation(),
            "memory_guard_size": engine.get_memory_guard_size(), "memory_reservation_for_growth": engine.get_memory_reservation_for_growth(),
            "memory_may_move": engine.get_memory_may_move(), "guard_before_linear_memory": engine.get_guard_before_linear_memory(),
            "memory_init_cow": engine.get_memory_init_cow(), "memory_guaranteed_dense_image_size": engine.get_memory_guaranteed_dense_image_size(),
            "table_lazy_init": engine.get_table_lazy_init(), "signals_based_traps": engine.get_signals_based_traps(),
            "wasm_backtrace": true, "wasm_backtrace_details": "enable", "wasm_backtrace_max_frames": engine.get_wasm_backtrace_max_frames(),
            "generate_address_map": engine.get_generate_address_map(), "debug_info": engine.get_debug_info(),
            "debug_symbols": engine.get_debug_symbols(), "native_unwind_info": true,
            "branch_hinting": engine.get_wasm_branch_hinting(), "relaxed_simd_deterministic": engine.get_relaxed_simd_deterministic(),
            "metadata_for_internal_asserts": engine.get_metadata_for_internal_asserts(),
            "compiler": "cranelift", "compiler_opt_level": "speed", "nan_canonicalization": false,
            "runtime_features": RUNTIME_FEATURES, "precompiler_extra_features": PRECOMPILER_FEATURES, "wasi_features": ["p2"]
        })
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BuildProvenance {
    pub anyrt_revision: String,
    pub rust_version: String,
    pub ndk_version: String,
    pub jni_protocol_version: Option<u32>,
    pub any_version_pin: String,
    /// Empty until the integration fixture actually passes on a specific version.
    pub any_versions_tested: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ArtifactManifestV1 {
    pub schema_version: u32,
    pub wasmtime_version: String,
    pub profile: EngineProfileV1,
    pub resolved_profile: Value,
    pub profile_sha256: String,
    pub source_sha256: String,
    pub artifact_sha256: String,
    pub provenance: BuildProvenance,
}

impl ArtifactManifestV1 {
    #[cfg(any(test, feature = "precompiler"))]
    pub fn new(
        profile: EngineProfileV1,
        engine: &Engine,
        source: &[u8],
        artifact: &[u8],
        provenance: BuildProvenance,
    ) -> Self {
        let resolved_profile = profile.resolved(engine);
        let profile_sha256 = sha256(crate::trace::canonical_json(&resolved_profile).as_bytes());
        Self {
            schema_version: 1,
            wasmtime_version: WASMTIME_VERSION.into(),
            profile,
            resolved_profile,
            profile_sha256,
            source_sha256: sha256(source),
            artifact_sha256: sha256(artifact),
            provenance,
        }
    }

    pub fn verify(&self, artifact: &[u8], engine: &Engine) -> Result<()> {
        ensure!(self.schema_version == 1, "unsupported manifest schema");
        ensure!(
            self.wasmtime_version == WASMTIME_VERSION,
            "Wasmtime version mismatch"
        );
        ensure!(
            self.profile.target == BUILD_TARGET,
            "artifact target mismatch"
        );
        ensure!(
            self.resolved_profile == self.profile.resolved(engine),
            "engine profile mismatch"
        );
        ensure!(
            self.profile_sha256
                == sha256(crate::trace::canonical_json(&self.resolved_profile).as_bytes()),
            "profile hash mismatch"
        );
        ensure!(
            self.artifact_sha256 == sha256(artifact),
            "artifact hash mismatch"
        );
        ensure!(
            self.source_sha256.len() == 64 && hex::decode(&self.source_sha256).is_ok(),
            "invalid source hash"
        );
        Ok(())
    }
}

/// A load failure never requests device compilation (ADR-034 §2).
#[derive(Debug)]
pub struct IncompatibleArtifact(pub String);

impl std::fmt::Display for IncompatibleArtifact {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "IncompatibleArtifact: {}", self.0)
    }
}
impl std::error::Error for IncompatibleArtifact {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn profile_is_explicit_without_a_compiler() {
        let profile = EngineProfileV1::native();
        let engine = Engine::new(&profile.config().unwrap()).unwrap();
        assert_eq!(engine.get_wasm_features(), EngineProfileV1::proposals());
        assert_eq!(engine.get_max_wasm_stack(), 512 * 1024);
        assert_eq!(engine.get_async_stack_size(), 2 * 1024 * 1024);
        assert!(engine.get_signals_based_traps());
        assert!(engine.get_consume_fuel());
        assert!(engine.get_epoch_interruption());
    }
}

#[cfg(all(test, feature = "compiler"))]
mod loading_tests {
    use super::*;
    use crate::runner::Cage;

    fn artifact() -> (Vec<u8>, ArtifactManifestV1) {
        let profile = EngineProfileV1::native();
        let engine = Engine::new(&profile.config().unwrap()).unwrap();
        let source = b"\0asm\x0d\0\x01\0"; // Empty binary component, no WAT/compiler fallback at load.
        let artifact = engine.precompile_component(source).unwrap();
        let resolved_profile = profile.resolved(&engine);
        let manifest = ArtifactManifestV1 {
            schema_version: 1,
            wasmtime_version: WASMTIME_VERSION.into(),
            profile,
            profile_sha256: sha256(crate::trace::canonical_json(&resolved_profile).as_bytes()),
            resolved_profile,
            source_sha256: sha256(source),
            artifact_sha256: sha256(&artifact),
            provenance: BuildProvenance {
                anyrt_revision: "test".into(),
                rust_version: "test".into(),
                ndk_version: "not-used-host-test".into(),
                jni_protocol_version: None,
                any_version_pin: "unqualified".into(),
                any_versions_tested: vec![],
            },
        };
        (artifact, manifest)
    }

    #[test]
    fn trusted_component_loads_and_preserves_source_identity() {
        let (artifact, manifest) = artifact();
        // SAFETY: this test just produced these exact bytes with the pinned compiler.
        let cage = unsafe { Cage::from_trusted_artifact(&artifact, &manifest) }.unwrap();
        assert_eq!(cage.kernel_sha256, manifest.source_sha256);
        assert_ne!(cage.kernel_sha256, manifest.artifact_sha256);
    }

    #[test]
    fn tampered_bytes_and_mismatched_profile_are_incompatible() {
        let (mut artifact, mut manifest) = artifact();
        artifact[0] ^= 1;
        // SAFETY: this is a deliberate integrity failure of test-produced bytes;
        // verification must return before deserialize is reached.
        let error = unsafe { Cage::from_trusted_artifact(&artifact, &manifest) }
            .err()
            .unwrap();
        assert!(error.downcast_ref::<IncompatibleArtifact>().is_some());
        assert!(error.to_string().contains("artifact hash mismatch"));
        artifact[0] ^= 1;
        manifest.resolved_profile["max_wasm_stack"] = json!(1);
        let error = unsafe { Cage::from_trusted_artifact(&artifact, &manifest) }
            .err()
            .unwrap();
        assert!(error.to_string().contains("engine profile mismatch"));
    }
}
