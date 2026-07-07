"""API drift detector — plan §4 "anyHelper drift flow".

The helper is curated (never autogen), but the `any` API moves fast.
This makes drift EVENT-DRIVEN: vendor a pinned swagger.json + a coverage
manifest (endpoint → helper method | excluded), each endpoint carrying a
fingerprint of its contract. `make api-drift` diffs a fresh spec against
the manifest and reports three classes:

  new         — endpoint in spec, absent from manifest (wrap or exclude?)
  removed     — manifest entry whose endpoint is gone (stale helper)
  changed     — fingerprint mismatch (params/body/response drifted under
                an existing mapping) — the interesting one

On drift the mini-skill (M4+) drafts helper/doc/manifest diffs for human
review. This module is the mechanism only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

METHODS = ("get", "post", "put", "delete", "patch")


def endpoint_fingerprint(op: dict) -> str:
    """Hash the contract-bearing parts of one operation: parameters,
    requestBody schema, response schema refs. Ignores descriptions /
    summaries / tags (prose churn is not drift)."""
    material = {
        "parameters": _norm_params(op.get("parameters", [])),
        "requestBody": _norm_schema(op.get("parameters", []), op.get("requestBody")),
        "responses": sorted(op.get("responses", {}).keys()),
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def _norm_params(params: list) -> list:
    out = []
    for p in params:
        if p.get("in") == "body":
            continue  # body handled separately
        out.append({"name": p.get("name"), "in": p.get("in"),
                    "required": p.get("required", False),
                    "type": p.get("type") or (p.get("schema") or {}).get("$ref")})
    return sorted(out, key=lambda x: (x.get("in") or "", x.get("name") or ""))


def _norm_schema(params: list, request_body: dict | None) -> Any:
    # swagger 2.0 puts the body in parameters (in: body); openapi 3 in
    # requestBody. Handle both, reduce to the schema $ref/shape.
    for p in params:
        if p.get("in") == "body":
            return (p.get("schema") or {}).get("$ref") or p.get("schema")
    if request_body:
        content = request_body.get("content", {})
        for _, media in content.items():
            return (media.get("schema") or {}).get("$ref")
    return None


def extract_endpoints(spec: dict) -> dict[str, str]:
    """{'METHOD /path': fingerprint} for every operation."""
    out = {}
    for path, ops in spec.get("paths", {}).items():
        for method, op in ops.items():
            if method in METHODS:
                out[f"{method.upper()} {path}"] = endpoint_fingerprint(op)
    return out


@dataclass
class Drift:
    new: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)

    def clean(self) -> bool:
        return not (self.new or self.removed or self.changed)


def diff(spec_endpoints: dict[str, str], manifest: dict) -> Drift:
    covered = manifest.get("endpoints", {})
    d = Drift()
    for ep, fp in sorted(spec_endpoints.items()):
        if ep not in covered:
            d.new.append(ep)
        elif covered[ep].get("fingerprint") != fp:
            d.changed.append(ep)
    for ep in sorted(covered):
        if ep not in spec_endpoints:
            d.removed.append(ep)
    return d


def build_skeleton(spec_endpoints: dict[str, str]) -> dict:
    """Initial manifest: every endpoint uncovered, fingerprint pinned."""
    return {
        "note": "endpoint -> {helper: <method>} | {excluded: <reason>}; "
                "fingerprint pins the contract (make api-drift checks it)",
        "endpoints": {
            ep: {"helper": None, "excluded": None, "fingerprint": fp}
            for ep, fp in sorted(spec_endpoints.items())
        },
    }


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {"endpoints": {}}


def save_manifest(path: Path, manifest: dict) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def render(d: Drift) -> str:
    if d.clean():
        return "api-drift: clean — manifest matches the vendored spec."
    lines = ["api-drift: DRIFT DETECTED"]
    for label, items in (("new (uncovered)", d.new),
                         ("removed (stale mapping)", d.removed),
                         ("changed (contract drift)", d.changed)):
        if items:
            lines.append(f"\n  {label}:")
            lines.extend(f"    {ep}" for ep in items)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="anyHelper API-drift detector")
    ap.add_argument("--spec", default="api/swagger.vendored.json")
    ap.add_argument("--manifest", default="api/coverage.json")
    ap.add_argument("--init", action="store_true", help="write a fresh manifest skeleton")
    args = ap.parse_args(argv)

    spec = json.loads(Path(args.spec).read_text())
    endpoints = extract_endpoints(spec)
    manifest_path = Path(args.manifest)

    if args.init:
        save_manifest(manifest_path, build_skeleton(endpoints))
        print(f"wrote manifest skeleton: {manifest_path} "
              f"({len(endpoints)} endpoints, all uncovered)")
        return 0

    d = diff(endpoints, load_manifest(manifest_path))
    print(render(d))
    return 0 if d.clean() else 1


if __name__ == "__main__":
    raise SystemExit(main())
