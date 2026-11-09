#!/usr/bin/env python3
"""Check the guest allowlist against the pinned Any spec AND registered routes."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--any-source", required=True, type=Path)
    args = parser.parse_args()
    source = args.any_source
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    assert revision == "e7ea90eab16952b61c3513b0464708812c86d655", revision
    assert not subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain"], text=True
    ), "pinned source is dirty"
    server = source / "internal/server"
    spec = (server / "docs/swagger.json").read_bytes()
    assert hashlib.sha256(spec).hexdigest() == (
        "a9bb908d56bead8a3bcfdb2f3a979c53a55aa06dc3f0c9cf02a351d9f86eee3c"
    )
    documented = {
        f"{method.upper()} /v1{path}"
        for path, operations in json.loads(spec)["paths"].items()
        for method in operations
    }
    registered = set()
    for file in [server / "routes.go", *server.glob("handlers_*.go")]:
        for method, path in re.findall(
            r'\b(?:g|v1)\.(GET|POST|PUT|PATCH|DELETE)\("([^"]+)"',
            file.read_text(),
        ):
            path = re.sub(r":([A-Za-z]+)", r"{\1}", path)
            registered.add(f"{method} /v1{path}")
    fixture = Path(__file__).parent.parent / "fixtures/android-guest-data-routes.txt"
    allowed = [line for line in fixture.read_text().splitlines() if not line.startswith("#")]
    assert len(allowed) == len(set(allowed)), "duplicate allowlist entries"
    assert set(allowed) <= documented, set(allowed) - documented
    assert set(allowed) <= registered, set(allowed) - registered
    print(f"PASS: {len(allowed)} explicit method/path rows match pinned OpenAPI and handlers")


if __name__ == "__main__":
    main()
