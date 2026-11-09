#!/usr/bin/env python3
"""Bind the frozen kernel to JNI v1 and build the trusted trap pair (ADR-034 §0)."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel-artifact", type=Path, required=True)
    parser.add_argument("--kernel-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cargo", default="cargo")
    args = parser.parse_args()
    manifest = json.loads(args.kernel_manifest.read_text())
    if manifest["source_sha256"] != (
        "81c7324e57bc924bd8b8f17adba9fe45c9261294fc102c0d5efb57eba82b7cf8"
    ):
        raise SystemExit("not the frozen locked kernel")
    if hashlib.sha256(args.kernel_artifact.read_bytes()).hexdigest() != manifest["artifact_sha256"]:
        raise SystemExit("kernel artifact hash mismatch")
    manifest["provenance"]["jni_protocol_version"] = 1
    args.output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.kernel_artifact, args.output / "kernel.cwasm")
    (args.output / "kernel.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    provenance = args.output / "provenance.json"
    provenance.write_text(json.dumps(manifest["provenance"], indent=2) + "\n")
    runtime = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith("WASMTIME_")}
    subprocess.run(
        [
            args.cargo,
            "run",
            "--locked",
            "--manifest-path",
            str(runtime / "Cargo.toml"),
            "--no-default-features",
            "--features",
            "precompiler",
            "--bin",
            "anyrt-precompile",
            "--",
            str(runtime / "fixtures/stage0-traps.wat"),
            str(args.output.resolve() / "traps.cwasm"),
            str(args.output.resolve() / "traps.manifest.json"),
            str(provenance.resolve()),
        ],
        env=env,
        check=True,
    )


if __name__ == "__main__":
    main()
