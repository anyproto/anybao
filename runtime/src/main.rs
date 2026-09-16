//! anyrt — the CLI over the anyrt library (ADR-009 §6): clap parsing,
//! env-var bootstrap, the fmt log subscriber, and process exit codes
//! live here; everything else is `anyrt::*`.

use anyhow::{Context, Result};
use anyrt::tracestore::TraceStore as _;
use anyrt::{anyapi, config, deploy, drift, oauth, resolver, runner, serve, stats, trace, view};
use anyrt::{broker, routes};
use clap::{Parser, Subcommand};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;
use std::sync::Arc;

#[derive(Parser)]
#[command(name = "anyrt", about = "the anybao runtime")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

/// The run/replay plumbing (kernel, programs, trace landing, config).
#[derive(clap::Args)]
struct RunOpts {
    /// local kernel override (dev) [default: the embedded kernel]
    #[arg(long)]
    kernel: Option<PathBuf>,
    #[arg(long, default_value = "repos/_agent/programs")]
    programs: PathBuf,
    /// raw-blob directory (ADR-026 §1); the trace itself lands in
    /// the local store of the run's space on --addr (ADR-023 §1)
    #[arg(long, default_value = "traces")]
    traces_dir: PathBuf,
    #[arg(long)]
    config: Option<PathBuf>,
    /// dotenv-style secrets, `ref=value` lines like .connectors.env
    /// (which is also read, from the config file's dir / cwd; the
    /// flag wins on duplicate refs)
    #[arg(long)]
    secrets_file: Option<PathBuf>,
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
}

#[derive(Subcommand)]
enum Cmd {
    /// run one guest program's main(args) and exit
    Run {
        spec: String,
        #[arg(long, default_value = "{}")]
        args: String,
        /// serve effects from recorded ones (ADR-028 §4): a run id
        /// (sugar for {"from": id}) or a path to a spec JSON
        /// {from, only, except, records, unmatched}
        #[arg(long)]
        mock: Option<String>,
        /// mockable set, effect-name globs (`http.*`); repeatable
        #[arg(long = "mock-only")]
        mock_only: Vec<String>,
        /// subtract from the mockable set; repeatable
        #[arg(long = "mock-except")]
        mock_except: Vec<String>,
        /// a miss inside the mockable set: fail (default) | live
        #[arg(long = "mock-unmatched")]
        mock_unmatched: Option<String>,
        #[command(flatten)]
        opts: RunOpts,
    },
    /// strict replay of a recorded run (ADR-001 §5, ADR-028 §2): the
    /// program + args come from its header, every effect must match
    /// the next record in sequence — a divergence is the finding
    Replay {
        /// run id (from `trace ls`), on --addr's store
        run: String,
        #[command(flatten)]
        opts: RunOpts,
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
        /// dotenv-style HARD seeds, `ref=value` lines keyed by secret
        /// ref — rewrites the device-local store like .connectors.env
        /// and any-ui's .env import (empty value = delete); the flag
        /// wins over .connectors.env on duplicate refs
        #[arg(long)]
        secrets_file: Option<PathBuf>,
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
    /// trace tooling over a serve's any local store (ADR-023 §7)
    Trace {
        #[command(subcommand)]
        cmd: TraceCmd,
    },
    /// API-drift check: vendored OpenAPI 3.1 pin vs the coverage manifest
    Drift {
        #[arg(long, default_value = "api/openapi.vendored.json")]
        spec: PathBuf,
        #[arg(long, default_value = "api/coverage.json")]
        manifest: PathBuf,
        /// rewrite fingerprints of already-triaged endpoints in place
        /// (new/removed stay human-triaged)
        #[arg(long)]
        refresh: bool,
    },
}

#[derive(Subcommand)]
enum TraceCmd {
    /// list runs, newest first: status, duration, turns, turn-1 title
    Ls {
        /// the any server whose local store holds the traces (ADR-023)
        #[arg(long, default_value = config::DEFAULT_ADDR)]
        addr: String,
        /// bao space name or id on --addr
        #[arg(long, default_value = "bao")]
        space: String,
        /// only runs whose program contains this (e.g. "toolcaller")
        #[arg(long)]
        program: Option<String>,
        /// max rows, 0 = all
        #[arg(short = 'n', long, default_value_t = 30)]
        limit: usize,
    },
    /// human-side render of one run (turns = llm.chat spans)
    Show {
        /// run id (from `trace ls`)
        run: String,
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
        /// the traceDiff view (ADR-028 §6): what a mocked run executed
        /// live inside its mockable set
        #[arg(long)]
        unmocked: bool,
        #[arg(long, default_value = config::DEFAULT_ADDR)]
        addr: String,
        #[arg(long, default_value = "bao")]
        space: String,
        /// the serve's traces dir — raw blobs resolve from its blobs/
        /// (ADR-026 §7); elsewhere a raw ref renders as its stub
        #[arg(long, default_value = "traces")]
        traces_dir: PathBuf,
    },
    /// effect-level diff of two runs by (effect, input key): calls only
    /// one made, shared calls whose outcome differs (ADR-028 §6)
    Diff {
        a: String,
        b: String,
        #[arg(long, default_value = config::DEFAULT_ADDR)]
        addr: String,
        #[arg(long, default_value = "bao")]
        space: String,
        #[arg(long, default_value = "traces")]
        traces_dir: PathBuf,
    },
    /// live-render a run as records land (show's line format); exits
    /// when the run completes
    Follow {
        /// run id [default: the newest run on --addr]
        run: Option<String>,
        /// with no run: only runs whose program contains this
        #[arg(long)]
        program: Option<String>,
        #[arg(long, default_value = config::DEFAULT_ADDR)]
        addr: String,
        #[arg(long, default_value = "bao")]
        space: String,
    },
    /// write one raw blob's bytes (ADR-026 §7) — to stdout, or -o file
    Blob {
        /// `sha256:<hex>` (or bare hex) from a trace record's ref
        hash: String,
        /// the traces dir whose blobs/ holds it
        #[arg(long, default_value = "traces")]
        dir: PathBuf,
        #[arg(short, long)]
        out: Option<PathBuf>,
    },
    /// distributions + tuning suggestions over a server's local store
    Stats {
        #[arg(long, default_value = config::DEFAULT_ADDR)]
        addr: String,
        #[arg(long, default_value = "bao")]
        space: String,
    },
}

/// How a run consults effects (ADR-028 §2): live, loose mock from a
/// spec, or strict replay of a recorded run.
enum RunHow {
    Live,
    Mock(Value),
    Replay(String),
}

/// `--mock` + its overlay flags → the spec value the broker parses: a
/// readable path is a spec JSON file, anything else a run id.
fn mock_spec_value(
    mock: &str,
    only: Vec<String>,
    except: Vec<String>,
    unmatched: Option<String>,
) -> Result<Value> {
    let path = Path::new(mock);
    let mut spec: Value = if path.is_file() {
        serde_json::from_str(&std::fs::read_to_string(path)?)
            .with_context(|| format!("--mock {mock}: spec JSON"))?
    } else {
        json!({"from": mock})
    };
    if !spec.is_object() {
        anyhow::bail!("--mock {mock}: the spec must be a JSON object");
    }
    let extend = |spec: &mut Value, key: &str, items: Vec<String>| {
        if items.is_empty() {
            return;
        }
        let mut arr: Vec<Value> = spec[key].as_array().cloned().unwrap_or_default();
        arr.extend(items.into_iter().map(Value::String));
        spec[key] = Value::Array(arr);
    };
    extend(&mut spec, "only", only);
    extend(&mut spec, "except", except);
    if let Some(u) = unmatched {
        spec["unmatched"] = json!(u);
    }
    Ok(spec)
}

fn run_cmd(spec: String, args: Value, opts: RunOpts, how: RunHow) -> Result<()> {
    let RunOpts {
        kernel,
        programs,
        traces_dir,
        config,
        secrets_file,
        timeout_s,
        from_space,
        addr,
        config_file,
    } = opts;
    // host config supplies overlays/cache/addr default for
    // --from-space parity with serve (ADR-009 §2)
    let host = config::Config::load(config_file.as_deref())?;
    let addr = addr.unwrap_or_else(|| host.addr.clone());
    let mut config = load_map(&config)?;
    // the explicit --config keys: with --from-space they shadow
    // the space store's READS for this run (never its rows)
    let mut config_overrides = config.clone();
    config_overrides.remove("any.base_url");
    config_overrides.remove("bao.space");
    // Secrets come from .connectors.env (picked up by
    // Config::load) + --secrets-file only — env is not read.
    // run has no device-local store, so every seed is just
    // this run's in-memory map; empty values (a delete in the
    // serve store) are dropped.
    let mut secrets = host.secret_overrides.clone();
    secrets.append(&mut load_secrets_file(&secrets_file)?);
    secrets.retain(|_, v| !v.is_empty());
    // Managed OAuth (ADR-011): run has no device-local store —
    // seeded oauth refs work this run only (degraded mode)
    let oauth_state = Arc::new(oauth::OauthState::new(oauth::builtin_providers(), None));
    oauth_state.seed(&mut secrets);
    // bootstrap parity with serve (closes dev D3): defaults
    // seed under any --config file (or_insert — the file
    // wins), so a scratch run needs no hand-built config;
    // any.base_url comes from --addr, never the space (you
    // can't read the space without already knowing the url).
    config::bootstrap_maps(&mut config, &addr);
    // runtime wiring (ADR-006 §3): a --config file may still
    // carry any.base_url (it wins over --addr); it moves to the
    // runtime namespace, never the agent-config seeds
    let any_base = config
        .remove("any.base_url")
        .and_then(|v| v.as_str().map(str::to_string))
        .unwrap_or_else(|| addr.clone());
    let mut runtime: BTreeMap<String, Value> = BTreeMap::new();
    runtime.insert("any.base_url".into(), Value::String(any_base.clone()));
    // `bao.space` (ADR-017 §0): the memory home. A --config key
    // wins; --from-space is that space by definition (below)
    if let Some(v) = config.remove("bao.space") {
        runtime.insert("bao.space".into(), v);
    }
    runtime.insert("shell".into(), anyrt::shell_runtime_value());
    // --from-space: serve's composition, one-shot (ADR-004 §6) —
    // space-backed resolver, no disk
    let from: Option<(Arc<anyapi::Client>, String)> = match &from_space {
        Some(space) => {
            let client = Arc::new(anyapi::Client::new(&any_base));
            let space_id = serve::find_space(&client, space)?;
            Some((client, space_id))
        }
        None => None,
    };
    // --from-space parity with serve: seed the guest-visible
    // alias map for the programs@v1 shadow guard (ADR-013 §1)
    if let Some((_, space_id)) = &from {
        let aliases = serve::alias_map(&host.overlays, space_id);
        runtime.insert("overlays.aliases".into(), serde_json::to_value(&aliases)?);
        runtime
            .entry("bao.space".into())
            .or_insert_with(|| Value::String(space_id.clone()));
    }
    let resolver: Option<Box<dyn resolver::ModuleResolver + Send>> =
        from.as_ref().map(|(client, space_id)| {
            Box::new(resolver::AnyModuleResolver::new(
                client.clone(),
                space_id,
                None,
                serve::alias_map(&host.overlays, space_id),
            )) as Box<dyn resolver::ModuleResolver + Send>
        });
    // the trace lands where serve's would (ADR-023 §1): the local
    // store of the run's space — --from-space, else bao.space
    let trace_space = match (&from, runtime.get("bao.space").and_then(Value::as_str)) {
        (Some((_, sid)), _) => sid.clone(),
        (None, Some(sid)) => sid.to_string(),
        (None, None) => anyhow::bail!(
            "run: the trace lands in a space's local store (ADR-023 §1) — \
             pass --from-space, or bao.space in --config"
        ),
    };
    let trace_client = match &from {
        Some((client, _)) => client.clone(),
        None => Arc::new(anyapi::Client::new(&any_base)),
    };
    let store = Arc::new(
        anyrt::tracestore::AnyTraceStore::new(
            trace_client,
            &trace_space,
            None,
            Some(anyrt::blob::BlobDir::create(&traces_dir)?),
        )
        .with_context(|| format!("trace store: space {trace_space} on {any_base}"))?,
    );
    let any_base = Some(any_base);
    // kernel is embedded (ADR-009 §4); --kernel is a dev override
    let cage = match &kernel {
        Some(path) => runner::Cage::new(
            &std::fs::read(path).with_context(|| format!("kernel at {}", path.display()))?,
        )?,
        None => runner::Cage::embedded()?,
    };
    let run_id = format!("run_{}", &uuid::Uuid::new_v4().simple().to_string()[..16]);
    // the header names the program AND its args (a replay reads both
    // back, ADR-028 §4); a mocked run records its spec; a replay keeps
    // the original header's seed/startedAt (the WASI floor derives from
    // them, ADR-002 §4) and names what it replays
    let (spec, args, mut header, replay_records) = match &how {
        RunHow::Live => (
            spec.clone(),
            args.clone(),
            json!({"program": spec, "host": "rust", "args": args}),
            None,
        ),
        RunHow::Mock(spec_v) => (
            spec.clone(),
            args.clone(),
            json!({"program": spec, "host": "rust", "args": args, "mock": spec_v}),
            None,
        ),
        RunHow::Replay(of) => {
            let records = store
                .load_resolved(of)
                .with_context(|| format!("replay: load {of}"))?;
            let mut header = records[0]["run"].clone();
            let program = header["program"].as_str().unwrap_or("").to_string();
            let args = header.get("args").cloned().unwrap_or(Value::Null);
            if program.is_empty() || args.is_null() {
                anyhow::bail!("replay: {of} predates program/args in the run header");
            }
            header["replayOf"] = json!(of);
            (program, args, header, Some(records))
        }
    };
    header["id"] = json!(run_id);
    let mut writer = trace::TraceWriter::new(header);
    if let Err(e) = writer.stream_to(store.as_ref()) {
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
    broker.runtime = runtime;
    broker.oauth = Some(oauth_state);
    broker.trace_store = Some(store.clone());
    match &how {
        RunHow::Live => {}
        RunHow::Mock(spec_v) => {
            let idx = broker
                .build_mock_index(spec_v)
                .map_err(|e| anyhow::anyhow!("--mock: {e}"))?;
            broker.mode = broker::Mode::Mock;
            broker.mock_index = Some(idx);
        }
        RunHow::Replay(_) => {
            broker.mode = broker::Mode::Replay;
            broker.cursor = Some(anyrt::replay::ReplayCursor::new(
                replay_records.as_deref().unwrap_or(&[]),
            ));
        }
    }
    // --from-space parity (ADR-004 §6): the space's agent_config
    // store, read through + written like serve; a space with no
    // bao/v1 bundle binds nothing (seeds only, config.set refused)
    if let Some((client, space_id)) = &from {
        match serve::run_config_store(client, space_id, config_overrides)? {
            Some(cs) => broker.config_store = Some(cs),
            None => eprintln!(
                "config store: none — {space_id} carries no bao/v1 bundle; \
                 config.get reads this run's seeds, config.set is refused"
            ),
        }
    }
    let mut out = runner::run_program(
        &cage,
        broker,
        &spec,
        &args,
        Default::default(),
        Arc::new(AtomicBool::new(false)),
        timeout_s,
    )?;
    let _summary = out.broker.writer.dump(store.as_ref())?;
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

/// The trace store a `trace` subcommand reads (ADR-023 §7): the
/// local-store collections of `space` on `addr`. `traces_dir` is the
/// serve's directory beside that server, whose blobs/ resolves raw
/// refs (ADR-026 §7); None = raw refs render as stubs.
fn trace_store(
    addr: &str,
    space: &str,
    traces_dir: Option<&Path>,
) -> Result<Box<dyn anyrt::tracestore::TraceStore>> {
    let client = Arc::new(anyapi::Client::new(addr));
    let blob_dir = traces_dir.map(anyrt::blob::BlobDir::new);
    match serve::find_space(&client, space) {
        Ok(sid) => Ok(Box::new(anyrt::tracestore::AnyTraceStore::new(
            client, &sid, None, blob_dir,
        )?)),
        // an imported store (`any local import` of a reporter's export,
        // docs/debugging.md § A reporter's export): the trace
        // collections are on --addr, the space is not — a raw id whose
        // run summaries exist there names that store, attached without
        // the ensure (a write the server would refuse for that space)
        Err(e) => {
            let imported = client
                .local_collections(Some("space"), Some(space))
                .unwrap_or_default()
                .iter()
                .any(|c| c["name"] == anyrt::tracestore::RUNS_COLL);
            if !imported {
                return Err(e.context(format!(
                    "trace store: space {space:?} on {addr} (neither a space here nor an imported trace store)"
                )));
            }
            Ok(Box::new(anyrt::tracestore::AnyTraceStore::attach(
                client, space, blob_dir,
            )))
        }
    }
}

fn load_map(path: &Option<PathBuf>) -> Result<BTreeMap<String, Value>> {
    match path {
        None => Ok(BTreeMap::new()),
        Some(p) => Ok(serde_json::from_str(&std::fs::read_to_string(p)?)?),
    }
}

/// `--secrets-file`: same dotenv `ref=value` format as
/// [`config::SECRETS_ENV_FILE`]. An explicit path that can't be read
/// is an error (unlike the optional sibling file).
fn load_secrets_file(path: &Option<PathBuf>) -> Result<BTreeMap<String, String>> {
    match path {
        None => Ok(BTreeMap::new()),
        Some(p) => Ok(config::parse_secrets_env(
            &std::fs::read_to_string(p)
                .with_context(|| format!("secrets file at {}", p.display()))?,
        )),
    }
}

fn main() -> Result<()> {
    // bin-only: lib embedders install their own subscriber (or none).
    // Logs go to STDERR — `run`'s stdout is the one-JSON-line result
    // contract, and fmt's default stdout writer polluted it.
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .with_target(false)
        .with_writer(std::io::stderr)
        .init();
    match Cli::parse().cmd {
        Cmd::Run {
            spec,
            args,
            mock,
            mock_only,
            mock_except,
            mock_unmatched,
            opts,
        } => {
            let args: Value = serde_json::from_str(&args).context("--args JSON")?;
            let how = match mock {
                None => RunHow::Live,
                Some(m) => {
                    RunHow::Mock(mock_spec_value(&m, mock_only, mock_except, mock_unmatched)?)
                }
            };
            run_cmd(spec, args, opts, how)
        }
        Cmd::Replay { run, opts } => run_cmd(String::new(), Value::Null, opts, RunHow::Replay(run)),
        Cmd::Serve {
            addr,
            space,
            agent_name,
            kernel,
            traces_dir,
            control_port,
            config_file,
            config,
            secrets_file,
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
                cfg.config.insert(k.clone(), v.clone());
                cfg.config_overrides.insert(k, v);
            }
            // --secrets-file = HARD seeds, the CLI twin of any-ui's
            // .env import: merged over .connectors.env (flag wins),
            // rewriting the device-local store in bootstrap_secrets
            cfg.secret_overrides
                .append(&mut load_secrets_file(&secrets_file)?);
            config::bootstrap(&mut cfg);
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
                    program,
                    limit,
                    addr,
                    space,
                },
        } => {
            let store = trace_store(&addr, &space, None)?;
            print!("{}", view::list(store.as_ref(), program.as_deref(), limit)?);
            Ok(())
        }
        Cmd::Trace {
            cmd:
                TraceCmd::Show {
                    run: id,
                    full,
                    system,
                    stats,
                    boot,
                    seq,
                    unmocked,
                    addr,
                    space,
                    traces_dir,
                },
        } => {
            let store = trace_store(&addr, &space, Some(&traces_dir))?;
            let store = store.as_ref();
            match (seq, stats, unmocked) {
                (Some(n), _, _) => print!("{}", view::show_record(store, &id, n)?),
                (None, true, _) => print!("{}", view::stats(store, &id)?),
                (None, false, true) => print!("{}", view::unmocked(store, &id)?),
                (None, false, false) => print!(
                    "{}",
                    view::render(store, &id, &view::ShowOpts { full, system, boot })?
                ),
            }
            Ok(())
        }
        Cmd::Trace {
            cmd:
                TraceCmd::Diff {
                    a,
                    b,
                    addr,
                    space,
                    traces_dir,
                },
        } => {
            let store = trace_store(&addr, &space, Some(&traces_dir))?;
            print!("{}", view::diff(store.as_ref(), &a, &b)?);
            Ok(())
        }
        Cmd::Trace {
            cmd:
                TraceCmd::Follow {
                    run,
                    program,
                    addr,
                    space,
                },
        } => {
            let store = trace_store(&addr, &space, None)?;
            let id = match run {
                Some(id) => id,
                None => {
                    let mut waited = false;
                    loop {
                        if let Some(p) = view::latest_run(store.as_ref(), program.as_deref()) {
                            break p;
                        }
                        if !waited {
                            eprintln!("waiting for a run on {addr}…");
                            waited = true;
                        }
                        std::thread::sleep(std::time::Duration::from_millis(500));
                    }
                }
            };
            view::follow(store.as_ref(), &id)
        }
        Cmd::Trace {
            cmd: TraceCmd::Blob { hash, dir, out },
        } => {
            let store = anyrt::blob::BlobDir::new(&dir);
            let bytes = store
                .read(&hash)?
                .ok_or_else(|| anyhow::anyhow!("no blob {hash} in {}", store.dir().display()))?;
            match out {
                Some(path) => {
                    std::fs::write(&path, &bytes)?;
                    eprintln!("{} bytes → {}", bytes.len(), path.display());
                }
                None => {
                    use std::io::Write as _;
                    std::io::stdout().write_all(&bytes)?;
                }
            }
            Ok(())
        }
        Cmd::Trace {
            cmd: TraceCmd::Stats { addr, space },
        } => {
            let store = trace_store(&addr, &space, None)?;
            print!("{}", stats::render(store.as_ref())?);
            Ok(())
        }
        Cmd::Drift {
            spec,
            manifest,
            refresh,
        } => {
            let clean = if refresh {
                drift::refresh(&spec, &manifest)?
            } else {
                drift::run(&spec, &manifest)?
            };
            if clean {
                Ok(())
            } else {
                std::process::exit(1)
            }
        }
    }
}
