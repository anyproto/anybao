//! anyrt's C embedding (`include/Anyrt/anyrt.h`). Every call returns an
//! owned JSON string that the caller frees with `anyrt_string_free`; a
//! panic or an error becomes `{"error": "..."}`, never an unwind into C.

use serde_json::{json, Value};
use std::ffi::{c_char, CString};
use std::panic::{catch_unwind, AssertUnwindSafe};

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
