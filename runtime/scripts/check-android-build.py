#!/usr/bin/env python3
"""Check/build only the Android runtime graph, in a separate invocation (ADR-034 §2)."""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

NDK_VERSION = "28.1.13356709"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--ndk", required=True, type=Path)
    parser.add_argument(
        "--build", action="store_true", help="build the embedded-only JNI shared library"
    )
    parser.add_argument(
        "--artifacts",
        required=True,
        type=Path,
        help="trusted build directory: kernel/traps .cwasm and .manifest.json",
    )
    parser.add_argument("--output", type=Path, help="copy .so and symbol/build evidence here")
    parser.add_argument(
        "--kernel-source",
        type=Path,
        help="locked source bytes to verify they are absent from the .so",
    )
    args = parser.parse_args()
    properties = dict(
        line.split("=", 1)
        for line in (args.ndk / "source.properties").read_text().splitlines()
        if "=" in line
    )
    revision = next(
        (value.strip() for key, value in properties.items() if key.strip() == "Pkg.Revision"), ""
    )
    if revision != NDK_VERSION:
        raise SystemExit(f"Expected NDK {NDK_VERSION}, found {revision!r}")
    host = {"Darwin": "darwin-x86_64", "Linux": "linux-x86_64"}.get(platform.system())
    if host is None:
        raise SystemExit("This local build helper supports macOS and Linux hosts")
    toolchain = args.ndk.resolve() / "toolchains/llvm/prebuilt" / host / "bin"
    clang = toolchain / "aarch64-linux-android28-clang"
    if not clang.is_file():
        raise SystemExit(f"Missing pinned API-28 compiler: {clang}")
    env = os.environ.copy()
    env.update(
        {
            "CC_aarch64_linux_android": str(clang),
            "AR_aarch64_linux_android": str(toolchain / "llvm-ar"),
            "CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER": str(clang),
            "CARGO_TARGET_AARCH64_LINUX_ANDROID_RUSTFLAGS": (
                "-C link-arg=-Wl,--build-id=sha1 -C link-arg=-Wl,-z,max-page-size=16384"
            ),
            "ANYRT_STAGE0_ARTIFACT_DIR": str(args.artifacts.resolve()),
        }
    )
    for name in ("kernel", "traps"):
        artifact = (args.artifacts / f"{name}.cwasm").read_bytes()
        manifest = json.loads((args.artifacts / f"{name}.manifest.json").read_text())
        if hashlib.sha256(artifact).hexdigest() != manifest["artifact_sha256"]:
            raise SystemExit(f"{name}: artifact hash mismatch")
        if manifest["provenance"]["jni_protocol_version"] != 1:
            raise SystemExit(f"{name}: JNI protocol must be 1")
        if name == "kernel" and manifest["source_sha256"] != (
            "81c7324e57bc924bd8b8f17adba9fe45c9261294fc102c0d5efb57eba82b7cf8"
        ):
            raise SystemExit("kernel source differs from the frozen locked build")
    runtime = Path(__file__).resolve().parents[1]
    common = [
        "--locked",
        "--manifest-path",
        str(runtime / "Cargo.toml"),
        "--target",
        "aarch64-linux-android",
        "--no-default-features",
        "--features",
        "android-spike",
    ]
    graph = subprocess.check_output(
        [args.cargo, "tree", *common, "-e", "normal,build"], text=True, env=env
    )
    forbidden = [
        name
        for name in ("cranelift-codegen", "wasmtime-internal-cache", "tiny_http")
        if name in graph
    ]
    if forbidden:
        raise SystemExit(f"Forbidden Android dependencies: {', '.join(forbidden)}")
    for package, expected in (
        # Wasmtime 49.0.3 std expands these two implicit dependency features.
        (
            "wasmtime",
            {
                "anyhow",
                "runtime",
                "std",
                "component-model",
                "async",
                "backtrace",
                "once_cell",
                "wasmtime-jit-icache-coherence",
            },
        ),
        ("wasmtime-wasi", {"p2"}),
    ):
        feature_tree = subprocess.check_output(
            [args.cargo, "tree", *common, "-e", "features", "-i", package],
            text=True,
            env=env,
        )
        actual = set(re.findall(rf'{package} feature "([^"]+)"', feature_tree))
        if actual != expected:
            raise SystemExit(
                f"Unexpected {package} features: {sorted(actual)}; expected {sorted(expected)}"
            )
        print(f"{package} feature allowlist verified: {', '.join(sorted(actual))}", flush=True)
    subprocess.run(
        [args.cargo, "build" if args.build else "check", *common, "--profile", "android", "--lib"],
        env=env,
        check=True,
    )
    if args.build and args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        library = runtime / "target/aarch64-linux-android/android/libanyrt.so"
        packaged = args.output / "libanyrt.so"
        shutil.copy2(library, packaged)
        for name, flags in (
            ("elf.txt", ["--file-header", "--program-headers", "--sections", "--notes"]),
            ("dynamic-symbols.txt", ["--dyn-symbols"]),
            ("unwind.txt", ["--unwind"]),
        ):
            evidence = subprocess.check_output(
                [str(toolchain / "llvm-readelf"), *flags, str(packaged)], text=True
            )
            (args.output / name).write_text(evidence)
        symbols = (args.output / "dynamic-symbols.txt").read_text()
        for operation in ("request", "cancel"):
            if (
                f"Java_com_anytypeio_anytype_feature_bao_poc_BaoPocNative_{operation}"
                not in symbols
            ):
                raise SystemExit(f"missing JNI {operation} symbol")
        elf = (args.output / "elf.txt").read_text()
        for required in (".eh_frame", ".debug_line", "Build ID:"):
            if required not in elf:
                raise SystemExit(f"missing native evidence: {required}")
        build_id = re.search(r"Build ID: ([0-9a-f]+)", elf).group(1)
        (args.output / "dependency-tree.txt").write_text(graph)
        source_paths = [runtime / "Cargo.toml", runtime / "Cargo.lock", runtime / "build.rs"]
        source_paths += list((runtime / "src").rglob("*.rs"))
        source_paths += list((runtime / "src").rglob("*.c"))
        (args.output / "source-sha256.json").write_text(
            json.dumps(
                {
                    str(path.relative_to(runtime)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted(source_paths)
                },
                indent=2,
            )
            + "\n"
        )
        raw = packaged.read_bytes()
        for name in ("kernel", "traps"):
            if raw.count((args.artifacts / f"{name}.cwasm").read_bytes()) != 1:
                raise SystemExit(f"{name}: expected exactly one embedded artifact")
        raw_kernel_present = None
        if args.kernel_source:
            source = args.kernel_source.read_bytes()
            kernel_manifest = json.loads((args.artifacts / "kernel.manifest.json").read_text())
            if hashlib.sha256(source).hexdigest() != kernel_manifest["source_sha256"]:
                raise SystemExit("source kernel hash mismatch")
            raw_kernel_present = source in raw
            if raw_kernel_present:
                raise SystemExit("raw source kernel must not be embedded in Android .so")
        load_alignments = [
            int(line.split()[-1], 16)
            for line in elf.splitlines()
            if line.strip().startswith("LOAD ")
        ]
        if not load_alignments or any(value < 16384 for value in load_alignments):
            raise SystemExit("ELF load alignment is below 16 KiB")
        (args.output / "library.json").write_text(
            json.dumps(
                {
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "build_id": build_id,
                    "bytes": len(raw),
                    "ndk": NDK_VERSION,
                    "api": 28,
                    "target": "aarch64-linux-android",
                    "profile": "android",
                    "jni_protocol": 1,
                    "link_page_alignment": 16384,
                    "actual_load_segment_alignments": load_alignments,
                    "raw_kernel_present": raw_kernel_present,
                    "embedded_artifact_copies": 1,
                    "symbolization_on_device": None,
                    "page_size_loading_on_device": None,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"JNI library and matching symbols/evidence: {args.output}", flush=True)


if __name__ == "__main__":
    main()
