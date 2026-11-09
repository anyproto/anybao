#!/usr/bin/env python3
"""Build an Android AOT artifact in an isolated host Cargo invocation (ADR-034 §2)."""

import argparse
import os
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("provenance", type=Path)
    parser.add_argument("--cargo", default="cargo")
    args = parser.parse_args()
    runtime = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith("WASMTIME_")}
    subprocess.run(
        [
            args.cargo,
            "run",
            "--locked",
            "--manifest-path",
            str(runtime / "Cargo.toml"),
            "--release",
            "--no-default-features",
            "--features",
            "precompiler",
            "--bin",
            "anyrt-precompile",
            "--",
            str(args.source.resolve()),
            str(args.artifact.resolve()),
            str(args.manifest.resolve()),
            str(args.provenance.resolve()),
        ],
        env=env,
        check=True,
    )


if __name__ == "__main__":
    main()
