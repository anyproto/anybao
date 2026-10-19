//! anyrt's C embedding (`include/Anyrt/anyrt.h`). Every call returns an
//! owned JSON string that the caller frees with `anyrt_string_free`; a
//! panic or an error becomes `{"error": "..."}`, never an unwind into C.

use anyrt::config::{self, Config, ServeMode};
use anyrt::AgentHandle;
use serde_json::{json, Value};
use std::ffi::{c_char, CStr, CString};
use std::io::Write;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

/// The host config the published bao uses: the agent space and the
/// overlays with their invites.
const ANYBAO_TOML: &str = include_str!("../../../anybao.toml");

/// The one serve this process runs.
static AGENT: Mutex<Option<AgentHandle>> = Mutex::new(None);

/// Receives each runtime log line, without the trailing newline.
pub type AnyrtLogFn = extern "C" fn(line: *const c_char);

fn to_c(value: Value) -> *mut c_char {
    // serde_json escapes NUL, so the text has no interior NUL
    CString::new(value.to_string()).map_or(std::ptr::null_mut(), CString::into_raw)
}

fn guarded(f: impl FnOnce() -> anyhow::Result<Value>) -> *mut c_char {
    let value = match catch_unwind(AssertUnwindSafe(f)) {
        Ok(Ok(value)) => value,
        Ok(Err(e)) => json!({"error": format!("{e:#}")}),
        Err(_) => json!({"error": "anyrt panicked"}),
    };
    to_c(value)
}

/// # Safety
/// `s` is NULL or a NUL-terminated string.
unsafe fn text(s: *const c_char, what: &str) -> anyhow::Result<String> {
    anyhow::ensure!(!s.is_null(), "{what} is NULL");
    Ok(CStr::from_ptr(s).to_str()?.to_string())
}

/// `anyrt kernel check` on the embedded kernel: `{"runs": [...]}`, one
/// report per run, each the CLI's JSON line. Blocks for the load and
/// the runs; call it off the main thread.
#[no_mangle]
pub extern "C" fn anyrt_kernel_check(runs: u32) -> *mut c_char {
    guarded(|| {
        let reports = anyrt::kernel_check::check_embedded(runs as usize)?;
        Ok(json!({ "runs": reports }))
    })
}

/// One log line per event: to `log` and appended to `<data_dir>/agent.log`.
struct LineSink {
    log: Option<AnyrtLogFn>,
    file: PathBuf,
    buf: Vec<u8>,
}

impl Write for LineSink {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.buf.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl Drop for LineSink {
    fn drop(&mut self) {
        if let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.file)
        {
            let _ = f.write_all(&self.buf);
        }
        let line = String::from_utf8_lossy(&self.buf);
        if let (Some(log), Ok(c)) = (self.log, CString::new(line.trim_end())) {
            log(c.as_ptr());
        }
    }
}

fn install_logging(log: Option<AnyrtLogFn>, data_dir: &Path) {
    let file = data_dir.join("agent.log");
    let _ = tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::new(
            "info,wasmtime=warn,cranelift=warn",
        ))
        .with_ansi(false)
        .with_target(false)
        .with_writer(move || LineSink {
            log,
            file: file.clone(),
            buf: Vec::new(),
        })
        .try_init();
}

/// The phone's serve over the `any` server at `addr`: chat only, no
/// control socket, traces and code cache under `data_dir` (absolute).
fn phone_config(addr: &str, data_dir: &Path) -> anyhow::Result<Config> {
    let mut cfg = Config::from_toml(ANYBAO_TOML)?;
    cfg.addr = addr.to_string();
    cfg.control = false;
    cfg.mode = ServeMode::ChatOnly;
    cfg.traces_dir = data_dir.join("traces");
    cfg.code_cache_dir = Some(data_dir.join("cache").join("code"));
    config::bootstrap(&mut cfg);
    Ok(cfg)
}

/// Starts the agent against the `any` server at `addr` (e.g.
/// `http://127.0.0.1:52011`), with its files under `data_dir`, an
/// absolute path. Blocks until boot is done, which waits on the
/// server; call it off the main thread. `log` receives each runtime
/// log line from any thread, or NULL. `{"status": {...}}` (the
/// presence beat) or `{"error": ...}`.
///
/// # Safety
/// `addr` and `data_dir` are NUL-terminated UTF-8 strings.
#[no_mangle]
pub unsafe extern "C" fn anyrt_start(
    addr: *const c_char,
    data_dir: *const c_char,
    log: Option<AnyrtLogFn>,
) -> *mut c_char {
    guarded(|| {
        let addr = text(addr, "addr")?;
        let data_dir = PathBuf::from(text(data_dir, "data_dir")?);
        anyhow::ensure!(data_dir.is_absolute(), "data_dir must be absolute");
        std::fs::create_dir_all(data_dir.join("cache").join("code"))?;
        install_logging(log, &data_dir);
        anyhow::ensure!(
            AGENT.lock().unwrap().is_none(),
            "the agent is already running"
        );
        let handle = anyrt::start(phone_config(&addr, &data_dir)?)?;
        let status = handle.status()?;
        *AGENT.lock().unwrap() = Some(handle);
        Ok(json!({ "status": status }))
    })
}

/// `GET /status` of the running agent: `{"status": {...}}`.
#[no_mangle]
pub extern "C" fn anyrt_status() -> *mut c_char {
    guarded(|| {
        let agent = AGENT.lock().unwrap();
        let handle = agent
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("not running"))?;
        Ok(json!({ "status": handle.status()? }))
    })
}

/// Drains and stops the agent within `deadline_ms` (ADR-032 §8):
/// `{"drained": n, "interrupted": n, "revoked": n}`.
#[no_mangle]
pub extern "C" fn anyrt_stop_with_drain(deadline_ms: u32) -> *mut c_char {
    guarded(|| {
        let handle = AGENT
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| anyhow::anyhow!("not running"))?;
        let report = handle.stop_with_drain(Duration::from_millis(deadline_ms.into()));
        Ok(
            json!({"drained": report.drained, "interrupted": report.interrupted,
                  "revoked": report.revoked}),
        )
    })
}

/// Frees a string that an `anyrt_*` call returned. NULL is a no-op.
///
/// # Safety
/// `s` is NULL or a pointer an `anyrt_*` call returned, freed once.
#[no_mangle]
pub unsafe extern "C" fn anyrt_string_free(s: *mut c_char) {
    if !s.is_null() {
        drop(CString::from_raw(s));
    }
}
