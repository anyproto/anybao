//! The build's target triple, for `kernel.boot`'s `meta.engine.target`
//! (ADR-032 §2): a trace says what ran it.
fn main() {
    let target = std::env::var("TARGET").unwrap_or_default();
    println!("cargo:rustc-env=ANYRT_TARGET={target}");
    println!("cargo:rerun-if-changed=build.rs");
}
