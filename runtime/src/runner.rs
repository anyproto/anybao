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
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use wasmtime::component::{Component, Linker};
use wasmtime::{Config, Engine, Store, UpdateDeadline};
use wasmtime_wasi::p2::add_to_linker_sync;
use wasmtime_wasi::{HostMonotonicClock, HostWallClock, ResourceTable, WasiCtxBuilder};

// ---- the WASI floor (ADR-002 §4) ------------------------------------------
// The allowlist gates what cell code imports; a module's own ambient
// calls (zipfile's time.localtime, random's import-time seeding,
// os.urandom behind uuid4/secrets) reach the WASI clocks and entropy.
// These are what they reach: the run's recorded start and one recorded
// seed — deterministic by construction, replayed from the header.

/// Wall clock frozen at the header's `startedAt`.
struct FloorWallClock(Duration);

impl HostWallClock for FloorWallClock {
    fn resolution(&self) -> Duration {
        Duration::from_micros(1)
    }
    fn now(&self) -> Duration {
        self.0
    }
}

/// Monotonic clock: a counter advancing 1 µs per read.
struct FloorMonotonic(AtomicU64);

impl HostMonotonicClock for FloorMonotonic {
    fn resolution(&self) -> u64 {
        1_000
    }
    fn now(&self) -> u64 {
        self.0.fetch_add(1_000, Ordering::Relaxed) + 1_000
    }
}

/// xoshiro256** seeded by splitmix64 over (run seed, source tag) —
/// pinned here, not to a crate's StdRng, so a recorded run replays on
/// any anyrt version.
pub struct FloorRng {
    s: [u64; 4],
}

impl FloorRng {
    pub fn new(seed: &[u8; 32], tag: u64) -> Self {
        let mut x = tag;
        for chunk in seed.chunks(8) {
            let mut b = [0u8; 8];
            b[..chunk.len()].copy_from_slice(chunk);
            x ^= u64::from_le_bytes(b);
            x = x.wrapping_mul(0x9E37_79B9_7F4A_7C15);
        }
        let mut s = [0u64; 4];
        for w in s.iter_mut() {
            x = x.wrapping_add(0x9E37_79B9_7F4A_7C15);
            let mut z = x;
            z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
            z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
            *w = z ^ (z >> 31);
        }
        FloorRng { s }
    }

    fn next(&mut self) -> u64 {
        let s = &mut self.s;
        let result = s[1].wrapping_mul(5).rotate_left(7).wrapping_mul(9);
        let t = s[1] << 17;
        s[2] ^= s[0];
        s[3] ^= s[1];
        s[1] ^= s[2];
        s[0] ^= s[3];
        s[2] ^= t;
        s[3] = s[3].rotate_left(45);
        result
    }
}

impl rand::TryRng for FloorRng {
    type Error = std::convert::Infallible;
    fn try_next_u32(&mut self) -> Result<u32, Self::Error> {
        Ok((self.next() >> 32) as u32)
    }
    fn try_next_u64(&mut self) -> Result<u64, Self::Error> {
        Ok(self.next())
    }
    fn try_fill_bytes(&mut self, dst: &mut [u8]) -> Result<(), Self::Error> {
        for chunk in dst.chunks_mut(8) {
            let bytes = self.next().to_le_bytes();
            chunk.copy_from_slice(&bytes[..chunk.len()]);
        }
        Ok(())
    }
}

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
                // the end record's meta comes back (the toolcaller reads
                // `mockFilter` off its cell span, ADR-028 §5)
                "span.end" => self.broker.span_end(
                    p["ok"].as_bool().unwrap_or(false),
                    p.get("output").filter(|v| !v.is_null()).cloned(),
                    p.get("error").filter(|v| !v.is_null()).cloned(),
                ),
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
    // the run's interrupt flag is the broker's too: a host call that
    // blocks (sh.run, ADR-024 §1) polls it and kills its child, where
    // the epoch bump alone could not reach (ADR-003 §2 amendment)
    broker.interrupt = interrupt.clone();
    let floor = broker.writer.floor();
    let wasi = WasiCtxBuilder::new()
        .inherit_stderr() // guest tracebacks; no fs/net granted
        .env("PYTHONHASHSEED", "0") // determinism pin
        // the rest of the floor (ADR-002 §4): frozen wall clock,
        // counter monotonic, one recorded seed behind both entropy sources
        .wall_clock(FloorWallClock(Duration::from_secs_f64(
            floor.started_at.max(0.0),
        )))
        .monotonic_clock(FloorMonotonic(AtomicU64::new(0)))
        .secure_random(FloorRng::new(&floor.seed, 1))
        .insecure_random(FloorRng::new(&floor.seed, 2))
        .insecure_random_seed(u128::from_le_bytes(
            floor.seed[..16].try_into().expect("16 of 32 seed bytes"),
        ))
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

#[cfg(all(test, feature = "shell"))]
mod tests {
    use super::*;
    use crate::broker::Broker;
    use crate::routes::Classifier;
    use crate::trace::TraceWriter;
    use std::collections::BTreeMap;

    /// ADR-005 §3 / ADR-003 §2 amendment: the run's interrupt flag ends
    /// a run that is blocked inside `sh.run` — the syscall kills its
    /// child on the flag, the epoch callback traps the guest after,
    /// and the outcome is `interrupted`, long before the command's own
    /// timeout.
    #[test]
    fn interrupt_flag_ends_a_run_blocked_in_sh_run() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("slow@v1.py"),
            "def main(args):\n    r = effect(\"sh.run\", {\"cmd\": \"sleep 30\", \"timeout_s\": 25})\n    return r\n",
        )
        .unwrap();
        let cage = Cage::embedded().unwrap();
        let broker = Broker::new(
            TraceWriter::new(json!({"id": "brk"})),
            BTreeMap::new(),
            BTreeMap::new(),
            Some(dir.path().to_path_buf()),
            Classifier::new(None),
        );
        let interrupt = Arc::new(AtomicBool::new(false));
        let flag = interrupt.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(700));
            flag.store(true, Ordering::Relaxed);
        });
        let t0 = Instant::now();
        let out = run_program(
            &cage,
            broker,
            "slow@v1",
            &json!({}),
            Default::default(),
            interrupt,
            60.0,
        )
        .unwrap();
        assert!(
            t0.elapsed() < Duration::from_secs(10),
            "took {:?}",
            t0.elapsed()
        );
        assert_eq!(out.status, "interrupted");
    }
}

/// The WASI floor (ADR-002 §4): what a guest's ambient calls see is
/// derived from the header's `startedAt` + `seed`, so a run is
/// deterministic from its header alone — including module-internal
/// calls no proxy reaches (os.urandom behind uuid4/secrets, the
/// stdlib random's import-time seeding, time.gmtime()).
#[cfg(test)]
mod floor_tests {
    use super::*;
    use crate::broker::{Broker, Mode};
    use crate::replay::ReplayCursor;
    use crate::routes::Classifier;
    use crate::trace::TraceWriter;
    use std::collections::BTreeMap;

    const PROGRAM: &str = "import random\nimport secrets\nimport time\nimport uuid\n\n\
def main(args):\n    xs = list(range(20))\n    random.shuffle(xs)\n    \
return {\"r\": [random.random() for _ in range(3)], \"shuffled\": xs,\n            \
\"u\": str(uuid.uuid4()), \"tok\": secrets.token_hex(8),\n            \
\"gm\": list(time.gmtime()[:6]), \"lt\": list(time.localtime()[:6]),\n            \
\"pc\": time.perf_counter() > 0, \"rand\": rand()}\n";

    fn run(
        dir: &std::path::Path,
        cage: &Cage,
        header: Value,
        replay_of: Option<&[Value]>,
    ) -> RunOutcome {
        let mut broker = Broker::new(
            TraceWriter::new(header),
            BTreeMap::new(),
            BTreeMap::new(),
            Some(dir.to_path_buf()),
            Classifier::new(None),
        );
        if let Some(records) = replay_of {
            broker.mode = Mode::Replay;
            broker.cursor = Some(ReplayCursor::new(records));
        }
        run_program(
            cage,
            broker,
            "floor@v1",
            &json!({}),
            Default::default(),
            Arc::new(AtomicBool::new(false)),
            60.0,
        )
        .unwrap()
    }

    #[test]
    fn header_seeds_every_ambient_source_and_replays() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("floor@v1.py"), PROGRAM).unwrap();
        let cage = Cage::embedded().unwrap();

        let one = run(
            dir.path(),
            &cage,
            json!({"id": "fl1", "program": "floor@v1"}),
            None,
        );
        assert_eq!(one.status, "ok", "{:?}", one.error);
        let header = one.broker.writer.records[0]["run"].clone();
        assert_eq!(header["seed"].as_str().map(str::len), Some(64));
        let started = header["startedAt"].as_f64().unwrap();

        // the frozen wall clock IS the header's startedAt (UTC = local: no TZ)
        let t = chrono::DateTime::from_timestamp(started as i64, 0).unwrap();
        use chrono::{Datelike, Timelike};
        let expect = json!([
            t.year(),
            t.month(),
            t.day(),
            t.hour(),
            t.minute(),
            t.second()
        ]);
        assert_eq!(one.value["gm"], expect);
        assert_eq!(one.value["lt"], expect);
        assert_eq!(one.value["pc"], json!(true));
        assert_eq!(one.value["r"].as_array().unwrap().len(), 3);
        assert!(one.value["rand"].is_f64());
        assert!(
            !one.broker
                .writer
                .records
                .iter()
                .any(|r| r["effect"] == "random.random"),
            "no per-draw records"
        );

        // same header → same stream: uuid4, secrets, shuffle, random all agree
        let two = run(dir.path(), &cage, header.clone(), None);
        assert_eq!(two.status, "ok", "{:?}", two.error);
        assert_eq!(two.value, one.value);

        // a fresh header draws a fresh seed → different identities
        let three = run(
            dir.path(),
            &cage,
            json!({"id": "fl3", "program": "floor@v1"}),
            None,
        );
        assert_ne!(three.value["u"], one.value["u"]);
        assert_ne!(three.value["tok"], one.value["tok"]);

        // strict replay from the recorded log: every record matches, the
        // shuffle comes out identical
        let replayed = run(dir.path(), &cage, header, Some(&one.broker.writer.records));
        assert_eq!(replayed.status, "ok", "{:?}", replayed.error);
        assert_eq!(replayed.value, one.value);
    }

    #[test]
    fn floor_rng_is_pinned() {
        // the generator is part of the trace contract: a recorded seed
        // must yield these words on every anyrt version
        let mut r = FloorRng::new(&[7u8; 32], 1);
        let a = r.next();
        let mut r2 = FloorRng::new(&[7u8; 32], 1);
        assert_eq!(a, r2.next());
        let mut other = FloorRng::new(&[7u8; 32], 2);
        assert_ne!(a, other.next(), "source tags separate the streams");
        assert_eq!(FloorRng::new(&[0u8; 32], 0).next(), 0x99ec_5f36_cb75_f2b4);
    }
}

/// The admitted batteries under the real kernel (ADR-002 §4 table):
/// archive/codec/data modules work in wasm, and the floor makes their
/// ambient calls deterministic — a zip written in two cells of one run
/// is byte-identical, its entry stamp is the header's startedAt, and
/// two runs from one header produce the same archive bytes.
#[cfg(test)]
mod allowlist_tests {
    use super::*;
    use crate::broker::Broker;
    use crate::routes::Classifier;
    use crate::trace::TraceWriter;
    use std::collections::BTreeMap;

    const PROGRAM: &str = r#"
import csv
import gzip
import hashlib
import io
import mimetypes
import sqlite3
import struct
import tarfile
import urllib.parse
import zipfile


def build_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("a.txt", "hello")
    return buf.getvalue()


def refusal(name):
    try:
        __import__(name)
    except ImportError as e:
        return str(e)
    return "imported"


def main(args):
    z1, z2 = build_zip(), build_zip()
    with zipfile.ZipFile(io.BytesIO(z1)) as z:
        dt = list(z.getinfo("a.txt").date_time)
    tb = io.BytesIO()
    with tarfile.open(fileobj=tb, mode="w:gz") as t:
        ti = tarfile.TarInfo("b.txt")
        ti.size = 3
        t.addfile(ti, io.BytesIO(b"tar"))
    with tarfile.open(fileobj=io.BytesIO(tb.getvalue()), mode="r:gz") as t:
        tar_back = t.extractfile("b.txt").read().decode()
    g = gzip.compress(b"gz")
    out = io.StringIO()
    csv.writer(out).writerow(["a", "b,c"])
    row = next(csv.reader(io.StringIO(out.getvalue())))
    con = sqlite3.connect(":memory:")
    con.execute("create table t(x)")
    con.execute("insert into t values (1),(2)")
    return {
        "zip_same": z1 == z2,
        "zip_sha": hashlib.sha256(z1).hexdigest(),
        "tar_sha": hashlib.sha256(tb.getvalue()).hexdigest(),
        "gz_sha": hashlib.sha256(g).hexdigest(),
        "dt": dt, "tar": tar_back, "gz": gzip.decompress(g).decode(),
        "csv": row, "u32": struct.unpack("<I", struct.pack("<I", 7))[0],
        "sum": con.execute("select sum(x) from t").fetchone()[0],
        "mime": mimetypes.guess_type("photo.png")[0],
        "host": urllib.parse.urlparse("https://a.b/c?d=1").netloc,
        "refused": {
            "pathlib": refusal("pathlib"),
            "urllib.request": refusal("urllib.request"),
            "bz2": refusal("bz2"),
            "os.path": refusal("os.path"),
        },
    }
"#;

    fn run(dir: &std::path::Path, cage: &Cage, header: Value) -> RunOutcome {
        let broker = Broker::new(
            TraceWriter::new(header),
            BTreeMap::new(),
            BTreeMap::new(),
            Some(dir.to_path_buf()),
            Classifier::new(None),
        );
        run_program(
            cage,
            broker,
            "batteries@v1",
            &json!({}),
            Default::default(),
            Arc::new(AtomicBool::new(false)),
            60.0,
        )
        .unwrap()
    }

    #[test]
    fn batteries_work_in_wasm_and_archives_are_deterministic() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("batteries@v1.py"), PROGRAM).unwrap();
        let cage = Cage::embedded().unwrap();
        let one = run(
            dir.path(),
            &cage,
            json!({"id": "bt1", "program": "batteries@v1"}),
        );
        assert_eq!(one.status, "ok", "{:?}", one.error);
        let v = &one.value;
        assert_eq!(v["zip_same"], json!(true), "two cells, one archive");
        assert_eq!(v["tar"], "tar");
        assert_eq!(v["gz"], "gz");
        assert_eq!(v["csv"], json!(["a", "b,c"]));
        assert_eq!(v["u32"], 7);
        assert_eq!(v["sum"], 3);
        assert_eq!(v["mime"], "image/png");
        assert_eq!(v["host"], "a.b");
        for (name, pointer) in [
            ("pathlib", "ADR-024"),
            ("urllib.request", "http.get"),
            ("bz2", "not compiled into the kernel image"),
            ("os.path", "ADR-024"),
        ] {
            let msg = v["refused"][name].as_str().unwrap();
            assert!(msg.contains(pointer), "{name}: {msg}");
        }
        // the zip entry stamp is the frozen wall clock (DOS time: 2 s grain)
        let header = one.broker.writer.records[0]["run"].clone();
        let started = header["startedAt"].as_f64().unwrap() as i64;
        use chrono::{Datelike, Timelike};
        let t = chrono::DateTime::from_timestamp(started, 0).unwrap();
        assert_eq!(
            v["dt"],
            json!([
                t.year(),
                t.month(),
                t.day(),
                t.hour(),
                t.minute(),
                t.second() - t.second() % 2
            ])
        );
        // same header → same bytes, gzip mtime and tar stamps included
        let two = run(dir.path(), &cage, header);
        assert_eq!(two.status, "ok", "{:?}", two.error);
        assert_eq!(two.value["zip_sha"], v["zip_sha"]);
        assert_eq!(two.value["tar_sha"], v["tar_sha"]);
        assert_eq!(two.value["gz_sha"], v["gz_sha"]);
    }
}
