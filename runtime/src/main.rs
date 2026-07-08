//! anyrt — the anybao runtime: wasmtime cage + broker/trace + the
//! syscall surface (ADR-002). Guest modules (the whole agent) load
//! from program space; the binary carries no product logic.

mod anyapi;
mod broker;
mod caps;
mod deploy;
mod drift;
mod replay;
mod resolver;
mod routes;
mod runner;
mod serve;
mod stats;
#[cfg(test)]
mod testutil;
mod toolmd;
mod trace;
mod triggers;
mod view;

pub(crate) mod bindings {
    wasmtime::component::bindgen!({
        world: "kernel",
        path: "wit",
    });
}

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::Arc;

#[derive(Parser)]
#[command(name = "anyrt", about = "the anybao runtime")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// run one guest program's main(args) and exit
    Run {
        spec: String,
        #[arg(long, default_value = "{}")]
        args: String,
        #[arg(long, default_value = "bin/kernel.wasm")]
        kernel: PathBuf,
        #[arg(long, default_value = "programs")]
        programs: PathBuf,
        #[arg(long, default_value = "traces")]
        traces_dir: PathBuf,
        #[arg(long)]
        config: Option<PathBuf>,
        #[arg(long)]
        secrets: Option<PathBuf>,
        #[arg(long, default_value_t = 120.0)]
        timeout_s: f64,
    },
    /// the agent: watch a chat, run conversations + triggers
    Serve {
        #[arg(long, default_value = "http://127.0.0.1:7001")]
        addr: String,
        #[arg(long, default_value = "bao")]
        space: String,
        #[arg(long, default_value = "general")]
        chat_name: String,
        #[arg(long, default_value = "bao")]
        agent_name: String,
        #[arg(long, default_value = "programs")]
        programs: PathBuf,
        #[arg(long, default_value = "skills")]
        skills: PathBuf,
        #[arg(long, default_value = "bin/kernel.wasm")]
        kernel: PathBuf,
        #[arg(long, default_value = "traces")]
        traces_dir: PathBuf,
        #[arg(long, default_value_t = 7010)]
        control_port: u16,
        #[arg(long)]
        config: Option<PathBuf>,
        #[arg(long)]
        secrets: Option<PathBuf>,
    },
    /// trace tooling over device-local run files
    Trace {
        #[command(subcommand)]
        cmd: TraceCmd,
    },
    /// API-drift check: vendored swagger pin vs the coverage manifest
    Drift {
        #[arg(long, default_value = "api/swagger.vendored.json")]
        spec: PathBuf,
        #[arg(long, default_value = "api/coverage.json")]
        manifest: PathBuf,
    },
}

#[derive(Subcommand)]
enum TraceCmd {
    /// human-side render of one run (turns = llm.chat spans)
    Show {
        file: PathBuf,
        /// lift every clip limit (full text, code, outputs)
        #[arg(long)]
        full: bool,
        /// include the system prompt (turn 1's otherwise-invisible channel)
        #[arg(long)]
        system: bool,
        /// dump one record by seq, blob-resolved, pretty-printed
        #[arg(long)]
        seq: Option<i64>,
    },
    /// distributions + tuning suggestions over a traces directory
    Stats { dir: PathBuf },
}

fn load_map(path: &Option<PathBuf>) -> Result<BTreeMap<String, Value>> {
    match path {
        None => Ok(BTreeMap::new()),
        Some(p) => Ok(serde_json::from_str(&std::fs::read_to_string(p)?)?),
    }
}

fn load_secrets(path: &Option<PathBuf>) -> Result<BTreeMap<String, String>> {
    Ok(load_map(path)?
        .into_iter()
        .filter_map(|(k, v)| v.as_str().map(|s| (k, s.to_string())))
        .collect())
}

/// env bootstrap (first keys): ANTHROPIC_API_KEY + tier defaults land
/// in config/secrets when files don't provide them.
fn bootstrap(
    config: &mut BTreeMap<String, Value>,
    secrets: &mut BTreeMap<String, String>,
    addr: &str,
) {
    config
        .entry("any.base_url".into())
        .or_insert_with(|| json!(addr));
    if let Ok(key) = std::env::var("ANTHROPIC_API_KEY") {
        secrets.entry("llm.key.anthropic".into()).or_insert(key);
        for (tier, model) in [
            ("codegen", "claude-sonnet-5"),
            ("classify", "claude-haiku-4-5-20251001"),
        ] {
            config.entry(format!("llm.tier.{tier}")).or_insert_with(|| {
                json!({
                "provider": "anthropic", "model": model,
                "base_url": "https://api.anthropic.com",
                "api_key_ref": "llm.key.anthropic"})
            });
        }
    }
}

fn main() -> Result<()> {
    match Cli::parse().cmd {
        Cmd::Run {
            spec,
            args,
            kernel,
            programs,
            traces_dir,
            config,
            secrets,
            timeout_s,
        } => {
            let args: Value = serde_json::from_str(&args).context("--args JSON")?;
            let config = load_map(&config)?;
            let secrets = load_secrets(&secrets)?;
            let any_base = config
                .get("any.base_url")
                .and_then(|v| v.as_str())
                .map(str::to_string);
            let kernel_bytes = std::fs::read(&kernel)
                .with_context(|| format!("kernel at {}", kernel.display()))?;
            let cage = runner::Cage::new(&kernel_bytes)?;
            let run_id = format!("run_{}", &uuid::Uuid::new_v4().simple().to_string()[..16]);
            let mut writer = trace::TraceWriter::new(json!({
                "id": run_id, "program": spec, "host": "rust"}));
            let trace_path = traces_dir.join(format!("{run_id}.jsonl"));
            if let Err(e) = writer.stream_to(&trace_path) {
                eprintln!("trace streaming unavailable ({e}); will write at run end");
            }
            let broker = broker::Broker::new(
                writer,
                config,
                secrets,
                programs,
                routes::Classifier::new(any_base.as_deref()),
            );
            let out = runner::run_program(
                &cage,
                broker,
                &spec,
                &args,
                Default::default(),
                Arc::new(AtomicBool::new(false)),
                timeout_s,
            )?;
            out.broker.writer.dump(&trace_path)?;
            println!(
                "{}",
                json!({
                "status": out.status, "traceRef": run_id,
                "durationMs": out.duration_ms, "fuelUsed": out.fuel_used,
                "value": out.value, "error": out.error})
            );
            if out.status != "ok" {
                std::process::exit(1);
            }
            Ok(())
        }
        Cmd::Serve {
            addr,
            space,
            chat_name,
            agent_name,
            programs,
            skills,
            kernel,
            traces_dir,
            control_port,
            config,
            secrets,
        } => {
            let mut config = load_map(&config)?;
            let mut secrets = load_secrets(&secrets)?;
            bootstrap(&mut config, &mut secrets, &addr);
            serve::serve(serve::ServeConfig {
                addr,
                space_name: space,
                chat_name,
                agent_name,
                programs,
                skills,
                kernel,
                traces_dir,
                control_port,
                config,
                secrets,
            })
        }
        Cmd::Trace {
            cmd:
                TraceCmd::Show {
                    file,
                    full,
                    system,
                    seq,
                },
        } => {
            match seq {
                Some(n) => print!("{}", view::show_record(&file, n)?),
                None => print!("{}", view::render(&file, &view::ShowOpts { full, system })?),
            }
            Ok(())
        }
        Cmd::Trace {
            cmd: TraceCmd::Stats { dir },
        } => {
            print!("{}", stats::render(&dir)?);
            Ok(())
        }
        Cmd::Drift { spec, manifest } => {
            if drift::run(&spec, &manifest)? {
                Ok(())
            } else {
                std::process::exit(1)
            }
        }
    }
}
