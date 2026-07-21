//! anyrt — the CLI over the anyrt library (ADR-009 §6): clap parsing,
//! env-var bootstrap, the fmt log subscriber, and process exit codes
//! live here; everything else is `anyrt::*`.

use anyhow::{Context, Result};
use anyrt::{anyapi, config, deploy, drift, resolver, runner, serve, stats, trace, view};
use anyrt::{broker, routes};
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
        /// local kernel override (dev) [default: the embedded kernel]
        #[arg(long)]
        kernel: Option<PathBuf>,
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
        /// resolve use() from this space's deployed programs (name or
        /// id) instead of the local dir — serve's resolver, one-shot
        #[arg(long)]
        from_space: Option<String>,
        /// any server base url for --from-space (a --config
        /// any.base_url wins over this) [default: config addr]
        #[arg(long)]
        addr: Option<String>,
        /// anybao.toml host config for --from-space overlays/cache
        /// [default: ./anybao.toml when present]
        #[arg(long)]
        config_file: Option<PathBuf>,
    },
    /// the agent: watch a chat, run conversations + triggers
    /// (defaults come from anybao.toml, ADR-009 §1; flags override)
    Serve {
        /// any server base url [default: config addr]
        #[arg(long)]
        addr: Option<String>,
        /// working space name [default: config agent.space]
        #[arg(long)]
        space: Option<String>,
        #[arg(long)]
        agent_name: Option<String>,
        /// local kernel override (dev) [default: the embedded kernel]
        #[arg(long)]
        kernel: Option<PathBuf>,
        /// [default: config paths.traces]
        #[arg(long)]
        traces_dir: Option<PathBuf>,
        /// [default: config agent.control_port]
        #[arg(long)]
        control_port: Option<u16>,
        /// anybao.toml host config [default: ./anybao.toml when present]
        #[arg(long)]
        config_file: Option<PathBuf>,
        #[arg(long)]
        config: Option<PathBuf>,
        #[arg(long)]
        secrets: Option<PathBuf>,
    },
    /// publish a repo folder (programs/, skills/, README.md) to a
    /// space (hash-gated), so a running serve picks changes up on its
    /// next run — no restart (ADR-009 §2)
    Deploy {
        /// repo folder: <src>/programs/, <src>/skills/, README.md
        #[arg(long, default_value = ".")]
        source: PathBuf,
        /// target space id, or an overlay name from [overlays];
        /// strict — never creates [default: the working space]
        #[arg(long)]
        target: Option<String>,
        /// any server base url [default: config addr]
        #[arg(long)]
        addr: Option<String>,
        /// working space name for the no-target default [default:
        /// config agent.space]
        #[arg(long)]
        space: Option<String>,
        /// anybao.toml host config [default: ./anybao.toml when present]
        #[arg(long)]
        config_file: Option<PathBuf>,
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
    /// list runs, newest first: status, duration, turns, turn-1 title
    Ls {
        #[arg(default_value = "traces")]
        dir: PathBuf,
        /// only runs whose program contains this (e.g. "toolcaller")
        #[arg(long)]
        program: Option<String>,
        /// max rows, 0 = all
        #[arg(short = 'n', long, default_value_t = 30)]
        limit: usize,
    },
    /// human-side render of one run (turns = llm.chat spans)
    Show {
        /// trace file, or a bare run id resolved against traces/
        file: PathBuf,
        /// lift every clip limit (full text, code, outputs)
        #[arg(long)]
        full: bool,
        /// include the system prompt (turn 1's otherwise-invisible channel)
        #[arg(long)]
        system: bool,
        /// per-turn metrics table (tokens/cache/cells/effects/costUsd)
        #[arg(long)]
        stats: bool,
        /// dump the boot window verbatim (the messages the loop fed the
        /// model before turn 1's user text)
        #[arg(long)]
        boot: bool,
        /// dump one record by seq, blob-resolved, pretty-printed
        #[arg(long)]
        seq: Option<i64>,
    },
    /// live-render a run as records land (show's line format); exits
    /// when the run completes
    Follow {
        /// trace file, or a bare run id resolved against traces/
        /// [default: the newest run in --dir]
        file: Option<PathBuf>,
        #[arg(long, default_value = "traces")]
        dir: PathBuf,
        /// with no file: only runs whose program contains this
        #[arg(long)]
        program: Option<String>,
    },
    /// distributions + tuning suggestions over a traces directory
    Stats { dir: PathBuf },
}

/// `trace show run_x` — a bare run id resolves against the default
/// traces dir, so `trace ls` output feeds straight into `show`.
fn resolve_trace(file: PathBuf) -> PathBuf {
    if file.exists() {
        return file;
    }
    let candidate = PathBuf::from("traces").join(format!("{}.jsonl", file.display()));
    if candidate.exists() {
        candidate
    } else {
        file
    }
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

/// Config defaults (ADR-006 §3): the harness's DEFAULT layer, sourced
/// from `config_defaults.json` (embedded at build — data, not Rust
/// literals; edit the json to change model/tier defaults). Sits under
/// any `--config` file and the space-scope override read off the config
/// object at serve start. These are behavior settings, not secrets, so
/// they seed unconditionally — `config.get("llm.tier.codegen")` resolves
/// even with no API key and no config object yet (a fresh space just
/// works). API keys are the only env-gated bits (ADR-008 §1): device-
/// local secrets that never enter config, only `secrets`.
const CONFIG_DEFAULTS: &str = include_str!("config_defaults.json");

fn bootstrap(
    config: &mut BTreeMap<String, Value>,
    secrets: &mut BTreeMap<String, String>,
    addr: &str,
) {
    config
        .entry("any.base_url".into())
        .or_insert_with(|| json!(addr));
    let defaults: BTreeMap<String, Value> =
        serde_json::from_str(CONFIG_DEFAULTS).expect("config_defaults.json is valid JSON");
    for (key, value) in defaults {
        config.entry(key).or_insert(value);
    }
    if let Ok(key) = std::env::var("ANTHROPIC_API_KEY") {
        secrets.entry("llm.key.anthropic".into()).or_insert(key);
    }
    if let Ok(key) = std::env::var("GEMINI_API_KEY") {
        secrets.entry("google.key.gemini".into()).or_insert(key);
    }
    if let Ok(key) = std::env::var("TOGETHER_API_KEY") {
        secrets.entry("llm.key.together".into()).or_insert(key);
    }
}

fn main() -> Result<()> {
    // bin-only: lib embedders install their own subscriber (or none)
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .with_target(false)
        .init();
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
            from_space,
            addr,
            config_file,
        } => {
            let args: Value = serde_json::from_str(&args).context("--args JSON")?;
            // host config supplies overlays/cache/addr default for
            // --from-space parity with serve (ADR-009 §2)
            let host = config::Config::load(config_file.as_deref())?;
            let addr = addr.unwrap_or_else(|| host.addr.clone());
            let mut config = load_map(&config)?;
            let mut secrets = load_secrets(&secrets)?;
            // bootstrap parity with serve (closes dev D3): defaults +
            // env keys seed under any --config file (or_insert — the
            // file wins), so a scratch run needs no hand-built config;
            // any.base_url comes from --addr, never the space (you
            // can't read the space without already knowing the url)
            bootstrap(&mut config, &mut secrets, &addr);
            // --from-space: serve's composition, one-shot (ADR-004 §6) —
            // space-backed resolver, no disk
            let from: Option<(Arc<anyapi::Client>, String)> = match &from_space {
                Some(space) => {
                    let base = config["any.base_url"].as_str().unwrap_or(&addr).to_string();
                    let client = Arc::new(anyapi::Client::new(&base));
                    let space_id = serve::find_space(&client, space)?;
                    Some((client, space_id))
                }
                None => None,
            };
            let resolver: Option<Box<dyn resolver::ModuleResolver + Send>> =
                from.as_ref().map(|(client, space_id)| {
                    Box::new(resolver::AnyModuleResolver::new(
                        client.clone(),
                        space_id,
                        None,
                        serve::alias_map(&host.overlays, space_id),
                    )) as Box<dyn resolver::ModuleResolver + Send>
                });
            let any_base = config
                .get("any.base_url")
                .and_then(|v| v.as_str())
                .map(str::to_string);
            // kernel is embedded (ADR-009 §4); --kernel is a dev override
            let cage = match &kernel {
                Some(path) => runner::Cage::new(
                    &std::fs::read(path)
                        .with_context(|| format!("kernel at {}", path.display()))?,
                )?,
                None => runner::Cage::embedded()?,
            };
            let run_id = format!("run_{}", &uuid::Uuid::new_v4().simple().to_string()[..16]);
            let mut writer = trace::TraceWriter::new(json!({
                "id": run_id, "program": spec, "host": "rust"}));
            let trace_path = traces_dir.join(format!("{run_id}.jsonl"));
            if let Err(e) = writer.stream_to(&trace_path) {
                eprintln!("trace streaming unavailable ({e}); will write at run end");
            }
            let mut broker = broker::Broker::new(
                writer,
                config,
                secrets,
                resolver.is_none().then_some(programs),
                routes::Classifier::new(any_base.as_deref()),
            );
            broker.resolver = resolver;
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
            agent_name,
            kernel,
            traces_dir,
            control_port,
            config_file,
            config,
            secrets,
        } => {
            let mut cfg = config::Config::load(config_file.as_deref())?;
            cfg.apply(config::CliOverrides {
                addr,
                space,
                agent_name,
                control_port,
                traces_dir,
            });
            cfg.kernel = kernel;
            // guest cascade (ADR-009 §1): the [config] table already
            // seeded cfg.config; the --config JSON file shadows it
            for (k, v) in load_map(&config)? {
                cfg.config.insert(k, v);
            }
            cfg.secrets = load_secrets(&secrets)?;
            bootstrap(&mut cfg.config, &mut cfg.secrets, &cfg.addr);
            serve::serve(cfg)
        }
        Cmd::Deploy {
            source,
            target,
            addr,
            space,
            config_file,
        } => {
            let mut cfg = config::Config::load(config_file.as_deref())?;
            cfg.apply(config::CliOverrides {
                addr,
                space,
                ..Default::default()
            });
            let client = anyapi::Client::new(&cfg.addr);
            let space_id = match &target {
                // overlay name from config, else a raw space id — strict
                Some(t) => {
                    let id = cfg
                        .overlays
                        .get(t)
                        .map(|o| o.space.clone())
                        .unwrap_or_else(|| t.clone());
                    serve::find_space(&client, &id)?
                }
                None => serve::ensure_space(&client, &cfg.agent_space)?,
            };
            let repo = deploy::deploy_repo(&client, &space_id, &source)?;
            println!("deploy → {:?}", repo.programs);
            println!("skills → {:?}", repo.skills);
            if let Some(st) = repo.readme {
                println!("readme → {st}");
            }
            Ok(())
        }
        Cmd::Trace {
            cmd:
                TraceCmd::Ls {
                    dir,
                    program,
                    limit,
                },
        } => {
            print!("{}", view::list(&dir, program.as_deref(), limit)?);
            Ok(())
        }
        Cmd::Trace {
            cmd:
                TraceCmd::Show {
                    file,
                    full,
                    system,
                    stats,
                    boot,
                    seq,
                },
        } => {
            let file = resolve_trace(file);
            match (seq, stats) {
                (Some(n), _) => print!("{}", view::show_record(&file, n)?),
                (None, true) => print!("{}", view::stats(&file)?),
                (None, false) => print!(
                    "{}",
                    view::render(&file, &view::ShowOpts { full, system, boot })?
                ),
            }
            Ok(())
        }
        Cmd::Trace {
            cmd: TraceCmd::Follow { file, dir, program },
        } => {
            let path = match file {
                Some(f) => resolve_trace(f),
                None => {
                    let mut waited = false;
                    loop {
                        if let Some(p) = view::latest_run(&dir, program.as_deref()) {
                            break p;
                        }
                        if !waited {
                            eprintln!("waiting for a run in {}…", dir.display());
                            waited = true;
                        }
                        std::thread::sleep(std::time::Duration::from_millis(500));
                    }
                }
            };
            view::follow(&path)
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
