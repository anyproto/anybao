//! `kernel check` (ADR-032 §2): load the kernel and run one built-in
//! program with one effect, no server and no trace store. The CLI and
//! the C embedding (anyrt-ffi) report the same JSON.

use anyhow::Result;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::sync::atomic::AtomicBool;
use std::sync::Arc;

use crate::runner::{self, Cage};
use crate::{broker, resolver, routes, trace};

/// CPython work, stdlib imports and one effect: everything a run needs
/// but a server. The effect's reply is constant, so `fuelUsed` is too:
/// equal across backends (ADR-032 §1).
const PROGRAM: &str = r#"import hashlib, json, re, zlib


def main(args):
    blob = json.dumps([{"i": i, "s": str(i)} for i in range(1000)]).encode()
    return {"squares": sum(i * i for i in range(200000)),
            "sha256": hashlib.sha256(blob).hexdigest()[:16],
            "zlib": len(zlib.compress(blob)),
            "words": len(re.findall(r"\w+", "the kernel runs offline")),
            "mailbox": effect("mailbox.drain", {})}
"#;

/// The check serves its entry inline and refuses every other import:
/// nothing reaches a disk or a server.
struct NoImports;

impl resolver::ModuleResolver for NoImports {
    fn resolve(&mut self, spec: &str, _frm: Option<&str>) -> Result<Value, resolver::ResolveError> {
        Err(resolver::ResolveError::NotFound(spec.to_string()))
    }
}

/// One report per run, in order; stops after the first run that is not
/// ok. `precompiled` says whether `cage` is the embedded `.cwasm`.
/// Runs on the caller's thread, which needs a [`runner::RUN_THREAD_STACK`]
/// stack (ADR-032 §6).
pub fn check(cage: &Cage, load_ms: u128, runs: usize, precompiled: bool) -> Result<Vec<Value>> {
    let mut reports = Vec::new();
    for _ in 0..runs.max(1) {
        let (out, run_ms) = run_once(cage)?;
        let ok = out.status == "ok";
        reports.push(json!({"status": out.status, "loadMs": load_ms,
               "runMs": run_ms, "fuelUsed": out.fuel_used,
               "memPages": out.mem_pages,
               "precompiled": precompiled,
               "engine": cage.engine_meta(), "kernelSha256": cage.kernel_sha256,
               "value": out.value, "error": out.error}));
        if !ok {
            break;
        }
    }
    Ok(reports)
}

/// The embedded kernel, loaded and checked on a run thread.
pub fn check_embedded(runs: usize) -> Result<Vec<Value>> {
    runner::spawn_run_thread("kernel-check", move || {
        let t0 = std::time::Instant::now();
        let cage = Cage::embedded()?;
        let load_ms = t0.elapsed().as_millis();
        check(&cage, load_ms, runs, cfg!(feature = "precompiled-kernel"))
    })
    .join()
    .map_err(|_| anyhow::anyhow!("the kernel check thread panicked"))?
}

fn run_once(cage: &Cage) -> Result<(runner::RunOutcome, u128)> {
    let spec = "kernelcheck@v1";
    let mut broker = broker::Broker::new(
        trace::TraceWriter::new(json!({"id": "run_kernelcheck", "program": spec})),
        BTreeMap::new(),
        BTreeMap::new(),
        None,
        routes::Classifier::new(None),
    );
    broker.resolver = Some(Box::new(resolver::InlineResolver {
        spec: spec.into(),
        source: PROGRAM.into(),
        inner: Box::new(NoImports),
    }));
    let t1 = std::time::Instant::now();
    let out = runner::run_program(
        cage,
        broker,
        spec,
        &json!({}),
        Default::default(),
        Arc::new(AtomicBool::new(false)),
        60.0,
    )?;
    Ok((out, t1.elapsed().as_millis()))
}
