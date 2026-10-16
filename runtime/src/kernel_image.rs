//! The precompiled kernel used where it sits (ADR-032 §3): the `.cwasm`
//! is assembled into an executable, read-only section of this binary,
//! so the code it runs is code the binary's signature covers. Nothing
//! copies it, relocates it or changes its protection.

use anyhow::Result;
use std::ptr::NonNull;
use std::sync::{Arc, Mutex};
use wasmtime::component::Component;
use wasmtime::{CustomCodeMemory, Engine};

use crate::runner::{engine_config, Platform};

macro_rules! cwasm_path {
    () => {
        concat!(env!("CARGO_MANIFEST_DIR"), "/../bin/kernel.cwasm")
    };
}

/// The image's length. Evaluated at compile time (the bytes are not
/// emitted twice); it also makes rustc rebuild when the file changes,
/// which `.incbin` alone would not.
const LEN: usize = include_bytes!(cwasm_path!()).len();

// 16 KiB alignment covers the `.text` offset inside a `.cwasm` (0x4000
// on Darwin, 0x10000 on Linux aarch64) and the 16 KiB pages of Apple
// silicon and recent Android devices.
#[cfg(target_vendor = "apple")]
std::arch::global_asm!(
    ".section __TEXT,__bao_kernel,regular,pure_instructions",
    ".p2align 14",
    ".globl _anyrt_kernel_image",
    ".private_extern _anyrt_kernel_image",
    "_anyrt_kernel_image:",
    concat!(".incbin \"", cwasm_path!(), "\""),
    ".text",
);
#[cfg(not(target_vendor = "apple"))]
std::arch::global_asm!(
    ".pushsection .text.bao_kernel,\"ax\",@progbits",
    ".p2align 14",
    ".globl anyrt_kernel_image",
    ".hidden anyrt_kernel_image",
    "anyrt_kernel_image:",
    concat!(".incbin \"", cwasm_path!(), "\""),
    ".popsection",
);

extern "C" {
    static anyrt_kernel_image: u8;
}

/// The embedded image, in place.
pub fn bytes() -> &'static [u8] {
    // SAFETY: the symbol labels LEN bytes assembled above; the section
    // is read-only for the life of the process.
    unsafe { std::slice::from_raw_parts(&raw const anyrt_kernel_image, LEN) }
}

/// Code memory that is already executable and must stay untouched: the
/// engine may only "publish" ranges inside the embedded image, and doing
/// so changes nothing. Any other range (code compiled at run time) is an
/// error, never a silent `mprotect`.
struct InPlace {
    start: usize,
    end: usize,
}

impl InPlace {
    fn check(&self, ptr: *const u8, len: usize) -> wasmtime::Result<()> {
        let (s, e) = (ptr as usize, ptr as usize + len);
        if s >= self.start && e <= self.end {
            Ok(())
        } else {
            Err(wasmtime::Error::msg(format!(
                "code at {s:#x}..{e:#x} is outside the embedded kernel image \
                 {:#x}..{:#x} (ADR-032 §3)",
                self.start, self.end
            )))
        }
    }
}

impl CustomCodeMemory for InPlace {
    fn required_alignment(&self) -> usize {
        // nothing changes protection; this only asserts the image layout
        4096
    }
    fn publish_executable(&self, ptr: *const u8, len: usize) -> wasmtime::Result<()> {
        self.check(ptr, len)
    }
    fn unpublish_executable(&self, ptr: *const u8, len: usize) -> wasmtime::Result<()> {
        self.check(ptr, len)
    }
}

/// Loaded once per process and shared: registering the same raw image
/// twice trips an assert in Wasmtime's module registry (49.0.3).
static LOADED: Mutex<Option<(Engine, Component)>> = Mutex::new(None);

/// The engine and component over the embedded image.
pub fn load(platform: Platform) -> Result<(Engine, Component)> {
    let mut slot = LOADED.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(loaded) = slot.as_ref() {
        return Ok(loaded.clone());
    }
    let image = bytes();
    let start = image.as_ptr() as usize;
    let mut cfg = engine_config(platform, None)?;
    cfg.with_custom_code_memory(Some(Arc::new(InPlace {
        start,
        end: start + image.len(),
    })));
    let engine = Engine::new(&cfg)?;
    // SAFETY: the image lives in this binary's read-only section for
    // the life of the process and is never modified; it is this build's
    // own `anyrt kernel compile` output, never input.
    let component =
        unsafe { Component::deserialize_raw(&engine, NonNull::from(image)) }.map_err(|e| {
            anyhow::anyhow!(
                "the embedded precompiled kernel does not fit this engine ({e}); \
                 rebuild bin/kernel.cwasm with this anyrt (ADR-032 §2)"
            )
        })?;
    *slot = Some((engine.clone(), component.clone()));
    Ok((engine, component))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_image_is_in_place_and_loads_once() {
        let image = bytes();
        assert_eq!(&image[..4], b"\x7fELF");
        assert_eq!(image.as_ptr() as usize % (16 << 10), 0, "16 KiB aligned");
        let (e1, _) = load(Platform::host()).unwrap();
        let (e2, _) = load(Platform::host()).unwrap();
        assert!(Engine::same(&e1, &e2), "one load per process");
    }

    #[test]
    fn publishing_outside_the_image_is_refused() {
        let image = bytes();
        let mem = InPlace {
            start: image.as_ptr() as usize,
            end: image.as_ptr() as usize + image.len(),
        };
        assert!(mem.publish_executable(image.as_ptr(), image.len()).is_ok());
        let outside = [0u8; 16];
        assert!(mem.publish_executable(outside.as_ptr(), 16).is_err());
        // SAFETY: one past the end, never dereferenced
        let tail = unsafe { image.as_ptr().add(image.len() - 8) };
        assert!(mem.publish_executable(tail, 16).is_err());
    }
}
