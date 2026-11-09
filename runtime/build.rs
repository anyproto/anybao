fn main() {
    let rustc = std::process::Command::new(std::env::var_os("RUSTC").unwrap())
        .arg("--version")
        .output()
        .expect("read pinned compiler identity");
    assert!(rustc.status.success());
    println!(
        "cargo:rustc-env=ANYRT_BUILD_RUSTC_VERSION={}",
        String::from_utf8(rustc.stdout).unwrap().trim()
    );
    println!(
        "cargo:rustc-env=ANYRT_BUILD_TARGET={}",
        std::env::var("TARGET").unwrap()
    );
    println!("cargo:rerun-if-changed=build.rs");
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("android")
        && std::env::var_os("CARGO_FEATURE_ANDROID_SPIKE").is_some()
    {
        build_android_spike();
    }
}

fn build_android_spike() {
    use std::path::PathBuf;
    use std::process::Command;
    println!("cargo:rerun-if-env-changed=ANYRT_STAGE0_ARTIFACT_DIR");
    let artifacts = PathBuf::from(
        std::env::var_os("ANYRT_STAGE0_ARTIFACT_DIR")
            .expect("Android spike requires the trusted build's ANYRT_STAGE0_ARTIFACT_DIR"),
    );
    let out = PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    for name in [
        "kernel.cwasm",
        "kernel.manifest.json",
        "traps.cwasm",
        "traps.manifest.json",
    ] {
        let source = artifacts.join(name);
        println!("cargo:rerun-if-changed={}", source.display());
        std::fs::copy(&source, out.join(name)).expect("copy trusted Stage-0 build artifact");
    }
    let compiler = std::env::var_os("CC_aarch64_linux_android")
        .expect("use the pinned Android build helper (missing NDK compiler)");
    let ar = std::env::var_os("AR_aarch64_linux_android").expect("missing NDK archiver");
    let object = out.join("stage0-jni.o");
    assert!(
        Command::new(compiler)
            .args(["-c", "-fPIC", "-g", "-O2", "src/stage0/jni.c", "-o"])
            .arg(&object)
            .status()
            .unwrap()
            .success(),
        "compile JNI bridge"
    );
    assert!(
        Command::new(ar)
            .arg("crs")
            .arg(out.join("libstage0_jni.a"))
            .arg(object)
            .status()
            .unwrap()
            .success(),
        "archive JNI bridge"
    );
    println!("cargo:rerun-if-changed=src/stage0/jni.c");
    println!("cargo:rustc-link-search=native={}", out.display());
    println!("cargo:rustc-link-lib=static=stage0_jni");
}
