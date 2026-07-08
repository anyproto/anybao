//! anybao-host — the thin host as a static binary (ADR-002): wasmtime
//! cage + broker/trace + the syscall surface. Guest modules (the whole
//! agent) load from program space; the Python reference host's traces
//! define this binary's contract.

mod broker;
mod routes;
mod trace;

use anyhow::{Context, Result};
use broker::Broker;
use clap::Parser;
use routes::Classifier;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::path::PathBuf;
use trace::TraceWriter;
use wasmtime::component::{Component, Linker};
use wasmtime::{Config, Engine, Store};
use wasmtime_wasi::{ResourceTable, WasiCtx, WasiCtxBuilder, WasiCtxView, WasiView};

mod bindings {
    wasmtime::component::bindgen!({
        world: "kernel",
        path: "../runtime/wit",
    });
}

const EPOCH_TICK_MS: u64 = 10;
const FUEL_PER_CELL: u64 = 5_000_000_000;

#[derive(Parser)]
#[command(name = "anybao-host", about = "run a guest program in the cage")]
struct Cli {
    /// program spec, e.g. toolcaller@v1
    spec: String,
    /// JSON args for the program's main()
    #[arg(long, default_value = "{}")]
    args: String,
    #[arg(long, default_value = "bin/kernel.wasm")]
    kernel: PathBuf,
    #[arg(long, default_value = "programs")]
    programs: PathBuf,
    #[arg(long, default_value = "traces")]
    traces_dir: PathBuf,
    /// config JSON file ({key: value}) — any.base_url, llm.tier.*, …
    #[arg(long)]
    config: Option<PathBuf>,
    /// secrets JSON file ({ref: value}) — credential injection only
    #[arg(long)]
    secrets: Option<PathBuf>,
    #[arg(long, default_value_t = 120.0)]
    timeout_s: f64,
}

struct Host {
    wasi: WasiCtx,
    table: ResourceTable,
    broker: Broker,
}

struct HostData;

impl wasmtime::component::HasData for HostData {
    type Data<'a> = &'a mut Host;
}

impl WasiView for Host {
    fn ctx(&mut self) -> WasiCtxView<'_> {
        WasiCtxView {
            ctx: &mut self.wasi,
            table: &mut self.table,
        }
    }
}

impl bindings::KernelImports for &mut Host {
    fn host_effect(&mut self, name: String, payload: String) -> String {
        let reply = (|| -> Result<Value, broker::EffectFailure> {
            let p: Value = serde_json::from_str(&payload).map_err(|e| broker::EffectFailure {
                type_: "ValueError".into(),
                message: e.to_string(),
            })?;
            match name.as_str() {
                "span.begin" => {
                    let n = p["name"].as_str().unwrap_or("").to_string();
                    let input = p.get("input").cloned().unwrap_or(json!({}));
                    Ok(json!({"span": self.broker.span_begin(&n, input)}))
                }
                "span.end" => {
                    self.broker.span_end(
                        p["ok"].as_bool().unwrap_or(false),
                        p.get("output").filter(|v| !v.is_null()).cloned(),
                        p.get("error").filter(|v| !v.is_null()).cloned(),
                    )?;
                    Ok(Value::Null)
                }
                _ => self.broker.call(&name, p),
            }
        })();
        match reply {
            Ok(output) => json!({"ok": true, "output": output}).to_string(),
            Err(e) => json!({"ok": false, "error": {
                "type": e.type_, "message": e.message}})
            .to_string(),
        }
    }
}

fn load_map(path: &Option<PathBuf>) -> Result<BTreeMap<String, Value>> {
    match path {
        None => Ok(BTreeMap::new()),
        Some(p) => Ok(serde_json::from_str(&std::fs::read_to_string(p)?)?),
    }
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let args: Value = serde_json::from_str(&cli.args).context("--args must be JSON")?;
    let config = load_map(&cli.config)?;
    let secrets: BTreeMap<String, String> = load_map(&cli.secrets)?
        .into_iter()
        .filter_map(|(k, v)| v.as_str().map(|s| (k, s.to_string())))
        .collect();
    let any_base = config
        .get("any.base_url")
        .and_then(|v| v.as_str())
        .map(str::to_string);

    let kernel_bytes = std::fs::read(&cli.kernel)
        .with_context(|| format!("kernel at {}", cli.kernel.display()))?;
    let kernel_sha = hex::encode(Sha256::digest(&kernel_bytes));

    let run_id = format!("run_{}", &uuid::Uuid::new_v4().simple().to_string()[..16]);
    let writer = TraceWriter::new(json!({"id": run_id, "program": cli.spec,
                                          "host": "rust"}));
    let broker = Broker::new(
        writer,
        config,
        secrets,
        cli.programs.clone(),
        Classifier::new(any_base.as_deref()),
    );

    // the cage: fuel + epoch, same knobs as the reference host
    let mut cfg = Config::new();
    cfg.consume_fuel(true);
    cfg.epoch_interruption(true);
    let engine = Engine::new(&cfg)?;
    let component = Component::from_binary(&engine, &kernel_bytes)?;

    let mut linker: Linker<Host> = Linker::new(&engine);
    wasmtime_wasi::p2::add_to_linker_sync(&mut linker)?;
    bindings::Kernel::add_to_linker::<Host, HostData>(&mut linker, |h| h)?;

    let wasi = WasiCtxBuilder::new()
        .inherit_stderr() // guest tracebacks; no fs/net granted
        .env("PYTHONHASHSEED", "0") // determinism pin, as the reference host
        .build();
    let mut store = Store::new(
        &engine,
        Host {
            wasi,
            table: ResourceTable::new(),
            broker,
        },
    );
    store.set_fuel(FUEL_PER_CELL)?;
    store.set_epoch_deadline((cli.timeout_s * 1000.0 / EPOCH_TICK_MS as f64) as u64);
    {
        let eng = engine.clone();
        std::thread::spawn(move || loop {
            std::thread::sleep(std::time::Duration::from_millis(EPOCH_TICK_MS));
            eng.increment_epoch();
        });
    }

    let kernel = bindings::Kernel::instantiate(&mut store, &component, &linker)?;

    // determinism pins as a recorded effect (ADR-003 kernel.boot)
    store
        .data_mut()
        .broker
        .call(
            "kernel.boot",
            json!({
                "kernel_sha256": kernel_sha, "hashseed": "0", "trace_schema": 2,
            }),
        )
        .ok();

    // the driver cell — identical to the reference host's
    let args_lit = trace::canonical_json(&json!(trace::canonical_json(&args)));
    let cell = format!(
        "import json\n_a = json.loads({args_lit})\n_r = use({spec:?}).main(_a)\n\
         print(json.dumps(_r))\n_r",
        spec = cli.spec
    );

    store.data_mut().broker.current_cell = Some("main".into());
    let t0 = std::time::Instant::now();
    let fuel_before = store.get_fuel()?;
    let raw = kernel.call_run_cell(&mut store, &cell, "main");
    let dur_ms = t0.elapsed().as_millis() as i64;
    let fuel_used = fuel_before.saturating_sub(store.get_fuel().unwrap_or(0));

    let (ok, reply): (bool, Value) = match &raw {
        Ok(text) => {
            let v: Value = serde_json::from_str(text).unwrap_or(Value::Null);
            (v["ok"].as_bool().unwrap_or(false), v)
        }
        Err(_) => (false, Value::Null),
    };
    let error = reply
        .get("error")
        .filter(|e| !e.is_null())
        .cloned()
        .or_else(|| {
            raw.as_ref().err().map(|e| {
                json!({
            "type": "Trap", "message": e.to_string()})
            })
        });
    store.data_mut().broker.cell_done(
        "main",
        ok,
        error.clone(),
        false,
        json!({"duration_ms": dur_ms, "fuel_used": fuel_used}),
    );

    std::fs::create_dir_all(&cli.traces_dir)?;
    let trace_path = cli.traces_dir.join(format!("{run_id}.jsonl"));
    store.data().broker.writer.dump(&trace_path)?;

    // main()'s return rides the driver cell's last print (json)
    let value = reply["prints"]
        .as_array()
        .and_then(|p| p.last())
        .and_then(|m| m["repr"].as_str())
        .and_then(|s| serde_json::from_str::<Value>(s).ok())
        .unwrap_or(Value::Null);
    println!(
        "{}",
        json!({
            "status": if ok { "ok" } else { "error" },
            "traceRef": run_id, "durationMs": dur_ms, "fuelUsed": fuel_used,
            "value": value, "error": error,
        })
    );
    if !ok {
        std::process::exit(1);
    }
    Ok(())
}
