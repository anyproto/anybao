//! Separate host-only compilation invocation (ADR-034 §2).
use anyhow::{bail, Context, Result};
use anyrt::engine_profile::{ArtifactManifestV1, BuildProvenance, EngineProfileV1};
use std::path::PathBuf;

fn main() -> Result<()> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.len() != 4 {
        bail!("usage: anyrt-precompile SOURCE.wasm ARTIFACT.cwasm MANIFEST.json PROVENANCE.json");
    }
    if std::env::vars_os().any(|(key, _)| key.to_string_lossy().starts_with("WASMTIME_")) {
        bail!("precompiler requires a WASMTIME_*-free environment; use scripts/precompile.py");
    }
    let paths: Vec<_> = args.iter().map(PathBuf::from).collect();
    let input = std::fs::read(&paths[0]).context("read source component")?;
    let source = wat::parse_bytes(&input)?.into_owned();
    if paths[0].extension().is_some_and(|ext| ext == "wat") {
        std::fs::write(paths[1].with_extension("wasm"), &source)?;
    }
    let provenance: BuildProvenance = serde_json::from_slice(&std::fs::read(&paths[3])?)?;
    if provenance.anyrt_revision.is_empty()
        || provenance.rust_version.is_empty()
        || provenance.ndk_version.is_empty()
        || provenance.any_version_pin.is_empty()
    {
        bail!("build provenance must contain every pin; compatibility must remain unclaimed until tested");
    }
    let profile = EngineProfileV1::android();
    let engine = wasmtime::Engine::new(&profile.config()?)?;
    let artifact = engine
        .precompile_component(&source)
        .map_err(|error| error.context("precompile component under EngineProfileV1"))?;
    let manifest = ArtifactManifestV1::new(profile, &engine, &source, &artifact, provenance);
    std::fs::write(&paths[1], artifact)?;
    std::fs::write(&paths[2], serde_json::to_vec_pretty(&manifest)?)?;
    println!(
        "artifact_sha256={} profile_sha256={} source_sha256={}",
        manifest.artifact_sha256, manifest.profile_sha256, manifest.source_sha256
    );
    Ok(())
}
