//! Host calibration of the same Stage-0 execution path, never built on Android.
use anyhow::{ensure, Context, Result};
use anyrt::engine_profile::{sha256, ArtifactManifestV1, BuildProvenance, EngineProfileV1};
use anyrt::stage0::harness::{Stage0Runtime, LOCKED_KERNEL_SHA256, PROTOCOL};
use serde_json::json;
use std::io::{BufRead, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

fn main() -> Result<()> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    ensure!(
        args.len() == 1,
        "usage: anyrt-stage0 LOCKED_KERNEL.wasm < requests.jsonl"
    );
    ensure!(
        !std::env::vars_os().any(|(key, _)| key.to_string_lossy().starts_with("WASMTIME_")),
        "host calibration builder requires a WASMTIME_*-free environment"
    );
    let source = std::fs::read(&args[0]).context("read locked kernel")?;
    ensure!(
        sha256(&source) == LOCKED_KERNEL_SHA256,
        "not the frozen locked kernel source"
    );
    let profile = EngineProfileV1::native();
    let engine = wasmtime::Engine::new(&profile.config()?)?;
    let provenance = BuildProvenance {
        anyrt_revision: "local-stage0-calibration-build".into(),
        rust_version: env!("ANYRT_BUILD_RUSTC_VERSION").into(),
        ndk_version: "host-only; Android comparison NDK28.1.13356709".into(),
        jni_protocol_version: Some(PROTOCOL),
        any_version_pin: "v0.3.5".into(),
        any_versions_tested: vec![],
    };
    let kernel = engine.precompile_component(&source)?;
    let kernel_manifest = ArtifactManifestV1::new(
        profile.clone(),
        &engine,
        &source,
        &kernel,
        provenance.clone(),
    );
    let trap_source = wat::parse_str(include_str!("../fixtures/stage0-traps.wat"))?;
    let traps = engine.precompile_component(&trap_source)?;
    let trap_manifest = ArtifactManifestV1::new(profile, &engine, &trap_source, &traps, provenance);
    // SAFETY: these exact bytes were just compiled locally from hash-pinned sources.
    let mut runtime = unsafe {
        Stage0Runtime::from_trusted_artifacts(&kernel, kernel_manifest, &traps, trap_manifest)
    }?;
    let idle = runtime.prepare_idle()?;
    println!(
        "{}",
        json!({"protocol": PROTOCOL, "event": "ready", "scope": "host_calibration",
        "health": runtime.health(), "idle": idle})
    );
    std::io::stdout().flush()?;
    let terminal = AtomicBool::new(false);
    for line in std::io::stdin().lock().lines() {
        let line = line?;
        let reply = anyrt::stage0::protocol::response(&terminal, || {
            let request =
                serde_json::from_str(&line).map_err(|_| anyhow::anyhow!("invalid TurnRequest"))?;
            runtime.turn(request, Arc::new(AtomicBool::new(false)))
        });
        println!("{reply}");
        std::io::stdout().flush()?;
        if terminal.load(Ordering::Relaxed) {
            break;
        }
    }
    Ok(())
}
