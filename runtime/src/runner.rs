//! The program runner — one shared Engine/Component, one Store per
//! run (ADR-003 §2). Interrupt rides the epoch callback: a global
//! ticker bumps the engine every 10ms; each run's callback checks its
//! interrupt flag + wall deadline and either extends by one tick or
//! traps. Hard break from any thread = flip the flag.

use crate::bindings;
use crate::broker::{Broker, SharedMailbox};
use crate::trace::canonical_json;
use anyhow::Result;
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use wasmtime::component::{Component, Linker};
use wasmtime::{Config, Engine, Store, UpdateDeadline};
use wasmtime_wasi::p2::add_to_linker_sync;
use wasmtime_wasi::{ResourceTable, WasiCtxBuilder};

pub const EPOCH_TICK_MS: u64 = 10;
/// Per-run compute budget (ADR-003 §2 fuel; bumped 5B→50B 2026-08-11:
/// ≈20s of pure compute — data jobs like mail sync parse megabytes of
/// JSON per tick and 5B starved a ~300-message batch run while still
/// being hours away from the runaway-loop ceiling fuel exists for).
pub const FUEL_PER_CELL: u64 = 50_000_000_000;

pub struct Host {
    pub wasi: wasmtime_wasi::WasiCtx,
    pub table: ResourceTable,
    pub broker: Broker,
}

pub struct HostData;

impl wasmtime::component::HasData for HostData {
    type Data<'a> = &'a mut Host;
}

impl wasmtime_wasi::WasiView for Host {
    fn ctx(&mut self) -> wasmtime_wasi::WasiCtxView<'_> {
        wasmtime_wasi::WasiCtxView {
            ctx: &mut self.wasi,
            table: &mut self.table,
        }
    }
}

impl crate::bindings::KernelImports for &mut Host {
    fn host_effect(&mut self, name: String, payload: String) -> String {
        let reply = (|| -> Result<Value, crate::broker::EffectFailure> {
            let p: Value =
                serde_json::from_str(&payload).map_err(|e| crate::broker::EffectFailure {
                    type_: "ValueError".into(),
                    message: e.to_string(),
                })?;
            match name.as_str() {
                "span.begin" => {
                    let n = p["name"].as_str().unwrap_or("").to_string();
                    let input = p.get("input").cloned().unwrap_or(json!({}));
                    let kind = p.get("kind").and_then(|k| k.as_str()).map(String::from);
                    Ok(json!({"span": self.broker.try_span_begin(&n, kind, input)?}))
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

/// The componentized CPython guest, compiled INTO the binary (ADR-009
/// §4): binary + kernel are one artifact — no space publish, no
/// artifact cache, no load-order question (the wasmtime *compile*
/// cache in `Cage::new` is a different thing). `make kernel` must run
/// before the crate builds (the Makefile runtime targets depend on it).
pub const EMBEDDED_KERNEL: &[u8] =
    include_bytes!(concat!(env!("CARGO_MANIFEST_DIR"), "/../bin/kernel.wasm"));

/// Process-wide cage: compile once, instantiate per run.
pub struct Cage {
    pub engine: Engine,
    pub component: Component,
    pub linker: Linker<Host>,
    pub kernel_sha256: String,
    ticker_stop: Arc<AtomicBool>,
    ticker: std::sync::Mutex<Option<std::thread::JoinHandle<()>>>,
}

impl Cage {
    /// The embedded kernel — the normal path; `new` stays for dev
    /// overrides (`--kernel <path>`) and embedders with custom builds.
    pub fn embedded() -> Result<Arc<Self>> {
        Cage::new(EMBEDDED_KERNEL)
    }

    pub fn new(kernel_bytes: &[u8]) -> Result<Arc<Self>> {
        use sha2::{Digest, Sha256};
        let mut cfg = Config::new();
        cfg.consume_fuel(true);
        cfg.epoch_interruption(true);
        // On-disk compile cache (ADR-009 §4): keyed by engine config +
        // wasm hash, so a rebuilt kernel invalidates itself. Compilation
        // latency only — execution and the trace are untouched.
        // Best-effort: an unusable cache dir must not fail the boot.
        match wasmtime::Cache::from_file(None) {
            Ok(cache) => {
                cfg.cache(Some(cache));
            }
            Err(e) => tracing::warn!("wasmtime compile cache unavailable ({e}); compiling cold"),
        }
        let engine = Engine::new(&cfg)?;
        let component = Component::from_binary(&engine, kernel_bytes)?;
        let mut linker: Linker<Host> = Linker::new(&engine);
        add_to_linker_sync(&mut linker)?;
        bindings::Kernel::add_to_linker::<Host, HostData>(&mut linker, |h| h)?;
        let ticker_stop = Arc::new(AtomicBool::new(false));
        let (ticker_engine, stop) = (engine.clone(), ticker_stop.clone());
        let ticker = std::thread::spawn(move || {
            while !stop.load(Ordering::Relaxed) {
                std::thread::sleep(Duration::from_millis(EPOCH_TICK_MS));
                ticker_engine.increment_epoch();
            }
        });
        Ok(Arc::new(Cage {
            engine,
            component,
            linker,
            kernel_sha256: hex::encode(Sha256::digest(kernel_bytes)),
            ticker_stop,
            ticker: std::sync::Mutex::new(Some(ticker)),
        }))
    }
}

impl Drop for Cage {
    /// Embedders create/drop cages — the epoch ticker must not leak
    /// (ADR-009 §6). Join is bounded by one tick.
    fn drop(&mut self) {
        self.ticker_stop.store(true, Ordering::Relaxed);
        if let Some(t) = self.ticker.lock().unwrap().take() {
            let _ = t.join();
        }
    }
}

pub struct RunOutcome {
    pub status: String, // ok | error | interrupted
    pub duration_ms: i64,
    pub fuel_used: u64,
    pub value: Value,
    pub error: Option<Value>,
    pub broker: Broker, // carries the finished trace
}

/// Run `spec`'s main(args) in a fresh store. `interrupt` may be
/// flipped from any thread (the watcher's hard break).
pub fn run_program(
    cage: &Cage,
    mut broker: Broker,
    spec: &str,
    args: &Value,
    mailbox: SharedMailbox,
    interrupt: Arc<AtomicBool>,
    timeout_s: f64,
) -> Result<RunOutcome> {
    broker.mailbox = mailbox;
    let wasi = WasiCtxBuilder::new()
        .inherit_stderr() // guest tracebacks; no fs/net granted
        .env("PYTHONHASHSEED", "0") // determinism pin
        .build();
    let mut store = Store::new(
        &cage.engine,
        Host {
            wasi,
            table: ResourceTable::new(),
            broker,
        },
    );
    store.set_fuel(FUEL_PER_CELL)?;
    // Live fuel gauge for the `fuel.state` syscall: host fns can't
    // reach the store, but the epoch callback can — it refreshes the
    // shared gauge every tick (≤EPOCH_TICK_MS staleness, fine for
    // checkpoint-before-exhaustion decisions).
    let gauge = store.data().broker.fuel_gauge.clone();
    gauge.store(FUEL_PER_CELL, Ordering::Relaxed);
    store.set_epoch_deadline(1);
    let deadline = Instant::now() + Duration::from_secs_f64(timeout_s);
    store.data_mut().broker.deadline = Some(deadline); // sh.run clamps to it (ADR-024 §1)
    let flag = interrupt.clone();
    store.epoch_deadline_callback(move |ctx| {
        if let Ok(fuel) = ctx.get_fuel() {
            gauge.store(fuel, Ordering::Relaxed);
        }
        if flag.load(Ordering::Relaxed) {
            return Err(wasmtime::Error::msg("interrupted"));
        }
        if Instant::now() > deadline {
            return Err(wasmtime::Error::msg("timeout"));
        }
        Ok(UpdateDeadline::Continue(1))
    });

    let kernel = bindings::Kernel::instantiate(&mut store, &cage.component, &cage.linker)?;
    store
        .data_mut()
        .broker
        .call(
            "kernel.boot",
            json!({"kernel_sha256": cage.kernel_sha256, "hashseed": "0",
                   "trace_schema": 2}),
        )
        .ok();

    // the driver cell: main()'s return rides back as the LAST PRINT
    let args_lit = canonical_json(&json!(canonical_json(args)));
    let cell = format!(
        "import json\n_a = json.loads({args_lit})\n_r = use({spec:?}).main(_a)\n\
         print(json.dumps(_r))\n_r"
    );
    store.data_mut().broker.current_cell = Some("main".into());
    let t0 = Instant::now();
    let fuel_before = store.get_fuel()?;
    let raw = kernel.call_run_cell(&mut store, &cell, "main");
    let duration_ms = t0.elapsed().as_millis() as i64;
    let fuel_used = fuel_before.saturating_sub(store.get_fuel().unwrap_or(0));

    // interrupted = the trap actually landed. A flag raised after the
    // guest already returned Ok (the soft-break timer racing the final
    // syscalls) must not rewrite a completed run (review).
    let interrupted = raw.is_err() && interrupt.load(Ordering::Relaxed);
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
                // Out-of-fuel is deterministic (same cell + inputs =
                // same trap): retrying unchanged can never succeed, so
                // the message teaches the one recovery that works.
                if matches!(
                    e.downcast_ref::<wasmtime::Trap>(),
                    Some(wasmtime::Trap::OutOfFuel)
                ) {
                    json!({"type": "FuelExhausted",
                           "message": format!(
                               "run exceeded its compute budget ({FUEL_PER_CELL} fuel). \
                                Retrying the same work will hit the same wall — split it \
                                into smaller chunks (fewer items per call/batch) and, in \
                                long loops, check effect(\"fuel.state\") to checkpoint \
                                and stop before the budget runs out.")})
                } else {
                    json!({"type": "Trap", "message": e.to_string()})
                }
            })
        });
    let mut host = store.into_data();
    host.broker.cell_done(
        "main",
        ok,
        error.clone(),
        interrupted,
        json!({"duration_ms": duration_ms, "fuel_used": fuel_used}),
    );
    let value = reply["prints"]
        .as_array()
        .and_then(|p| p.last())
        .and_then(|m| m["repr"].as_str())
        .and_then(|s| serde_json::from_str::<Value>(s).ok())
        .unwrap_or(Value::Null);
    Ok(RunOutcome {
        status: if interrupted {
            "interrupted".into()
        } else if ok {
            "ok".into()
        } else {
            "error".into()
        },
        duration_ms,
        fuel_used,
        value,
        error,
        broker: host.broker,
    })
}
