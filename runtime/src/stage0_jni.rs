//! Embedded-only Android Stage-0 entry (ADR-034 §0, §2).
use crate::stage0::harness::Stage0Runtime;
use crate::stage0::protocol::{parse_request, response, Operation};
use anyhow::{bail, ensure, Context, Result};
use serde_json::{json, Value};
use std::ffi::c_void;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, LazyLock, Mutex};

const KERNEL: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/kernel.cwasm"));
const KERNEL_MANIFEST: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/kernel.manifest.json"));
const TRAPS: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/traps.cwasm"));
const TRAPS_MANIFEST: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/traps.manifest.json"));
static RUNTIME: Mutex<Option<Stage0Runtime>> = Mutex::new(None);
static CANCEL: LazyLock<Arc<AtomicBool>> = LazyLock::new(|| Arc::new(AtomicBool::new(false)));
static TERMINAL: AtomicBool = AtomicBool::new(false);
static RETIRED: AtomicBool = AtomicBool::new(false);

fn dispatch(text: &str) -> Result<Value> {
    ensure!(
        !TERMINAL.load(Ordering::Relaxed),
        "terminal native panic; retire worker"
    );
    ensure!(
        !RETIRED.load(Ordering::Relaxed),
        "worker retired; replace process"
    );
    let operation = parse_request(text)?;
    let mut state = RUNTIME.lock().map_err(|_| crate::stage0::RunThreadPanic)?;
    CANCEL.store(false, Ordering::Relaxed);
    match operation {
        Operation::Load {} => {
            ensure!(
                state.is_none(),
                "already loaded; fresh load requires process replacement"
            );
            // SAFETY: all native code and manifests are include_bytes! from the trusted build.
            let runtime = unsafe {
                Stage0Runtime::from_trusted_artifacts(
                    KERNEL,
                    serde_json::from_slice(KERNEL_MANIFEST)?,
                    TRAPS,
                    serde_json::from_slice(TRAPS_MANIFEST)?,
                )
            }
            .context("IncompatibleArtifact")?;
            let result = runtime.health();
            *state = Some(runtime);
            Ok(result)
        }
        Operation::Health {} => Ok(state
            .as_ref()
            .map_or(json!({"loaded": false}), Stage0Runtime::health)),
        Operation::Retire {} => {
            RETIRED.store(true, Ordering::Relaxed);
            *state = None;
            Ok(json!({"retired": true}))
        }
        operation => {
            let runtime = state.as_mut().context("load first")?;
            match operation {
                Operation::PrepareIdle {} => runtime.prepare_idle(),
                Operation::K2Case { index } => runtime.k2_case(index, CANCEL.clone()),
                Operation::PythonRecursionDiagnostic {} => {
                    runtime.python_recursion_diagnostic(CANCEL.clone())
                }
                Operation::Turn { request } => runtime.turn(*request, CANCEL.clone()),
                _ => bail!("invalid operation"),
            }
        }
    }
}

#[repr(C)]
pub struct BaoText {
    ptr: *mut u16,
    len: usize,
}

#[no_mangle]
unsafe extern "C" fn bao_stage0_request(input: *const u16, len: usize) -> BaoText {
    let guarded = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        // C owns this pinned UTF-16 view until the call returns.
        let reply = response(&TERMINAL, || {
            ensure!(len <= 4 * 1024 * 1024, "oversized UTF-16 request");
            let text = String::from_utf16(unsafe { std::slice::from_raw_parts(input, len) })
                .map_err(|_| anyhow::anyhow!("invalid UTF-16 request"))?;
            dispatch(&text)
        });
        owned_text(&reply)
    }));
    match guarded {
        Ok(text) => text,
        Err(_) => {
            TERMINAL.store(true, Ordering::Relaxed);
            owned_text(
                r#"{"protocol":1,"ok":false,"terminal_panic":true,"error":"native JNI panic; retire worker"}"#,
            )
        }
    }
}

fn owned_text(reply: &str) -> BaoText {
    let mut utf16 = reply.encode_utf16().collect::<Vec<_>>().into_boxed_slice();
    let result = BaoText {
        ptr: utf16.as_mut_ptr(),
        len: utf16.len(),
    };
    std::mem::forget(utf16);
    result
}

#[no_mangle]
unsafe extern "C" fn bao_stage0_free(text: BaoText) {
    drop(unsafe { Box::from_raw(std::ptr::slice_from_raw_parts_mut(text.ptr, text.len)) });
}

unsafe extern "C" {
    fn bao_stage0_jni_request(env: *mut c_void, input: *mut c_void) -> *mut c_void;
}

#[no_mangle]
unsafe extern "system" fn Java_com_anytypeio_anytype_feature_bao_poc_BaoPocNative_request(
    env: *mut c_void,
    _object: *mut c_void,
    input: *mut c_void,
) -> *mut c_void {
    unsafe { bao_stage0_jni_request(env, input) }
}

#[no_mangle]
extern "system" fn Java_com_anytypeio_anytype_feature_bao_poc_BaoPocNative_cancel(
    _env: *mut c_void,
    _object: *mut c_void,
) {
    CANCEL.store(true, Ordering::Relaxed);
}
