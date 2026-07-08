"""Deploy tool — the generic program/skill publisher (plan §4, was the
bobrik-specific sync). Reads a source dir and writes to a TARGET space
(the agent overlay, a std overlay, any package space). Hash-gated:
unchanged programs skip all writes. This unwelds the old
bootstrapSystemFiles from its hardcoded space.

Program storage (mirrors internal/program): a `program`-typed object per
program, source in `program_source`/"main"/{code}, split tool docs in
`program_description`/"main"/{text} + one `program_methods` record per
method, `program.any_tool` = has description AND ≥1 method. A program is
`<name>@<version>` (the filename convention `name@vN.py`). Optional
capability manifest (sidecar `name@vN.manifest.json`) is stored in
`program_manifest`/"main" and mixed into the fingerprint — manifest =
request, grants bind to the content hash (anybao.caps, 00-plan
"Capabilities & trust").
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .anyclient import AnyClient
from .toolmd import split_tool_markdown

PROGRAM_TYPE = "program"
_NAME_VER = re.compile(r"^(?P<name>.+)@(?P<version>v\d+)$")


def _fingerprint(code: str, desc: str, method_tuples: list[tuple],
                 manifest: dict | None = None) -> str:
    """Over the SPLIT form (code + description + sorted methods) so disk
    and in-space sides normalize identically — never over raw markdown
    (which wouldn't round-trip). method_tuples: (bare_name, name, kind, text).
    The manifest is part of the hash (CapBAC: manifest = request; editing
    it MUST change the hash so grants stop matching — 00-plan
    "Capabilities & trust") but is only mixed in when present, keeping
    no-manifest fingerprints identical to the pre-manifest era."""
    h = hashlib.sha256()
    h.update(code.encode())
    h.update(b"\x00d\x00")
    h.update(desc.encode())
    for bare, name, kind, text in sorted(method_tuples):
        h.update(f"\x00m\x00{bare}\x00{name}\x00{kind}\x00{text}".encode())
    if manifest:
        h.update(b"\x00man\x00")
        h.update(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())
    return h.hexdigest()


@dataclass
class ProgramSource:
    name: str
    version: str
    code: str
    tool_md: str = ""   # optional tool-description markdown
    # optional capability manifest sidecar (CapBAC request half):
    # {"capabilities": [...], "publisher": ..., "attestation": {...}?}
    manifest: dict = field(default_factory=dict)

    @property
    def spec(self) -> str:
        return f"{self.name}@{self.version}"

    def split(self) -> tuple[str, list]:
        return split_tool_markdown(self.tool_md) if self.tool_md else ("", [])

    def fingerprint(self) -> str:
        desc, methods = self.split()
        return _fingerprint(code=self.code, desc=desc,
                            method_tuples=[(m.bare_name, m.name, m.kind, m.text) for m in methods],
                            manifest=self.manifest)


def load_programs(src_dir: Path) -> list[ProgramSource]:
    """Read `<name>@vN.py` program files (+ optional `<name>@vN.md` tool
    docs, optional `<name>@vN.manifest.json` capability manifests) from a
    directory."""
    out: list[ProgramSource] = []
    for py in sorted(src_dir.glob("*.py")):
        m = _NAME_VER.match(py.stem)
        if not m:
            continue
        md = py.with_suffix(".md")
        mf = py.with_suffix(".manifest.json")
        out.append(ProgramSource(
            name=m["name"], version=m["version"], code=py.read_text(),
            tool_md=md.read_text() if md.exists() else "",
            manifest=json.loads(mf.read_text()) if mf.exists() else {}))
    return out


class Deployer:
    def __init__(self, client: AnyClient, *, space: str):
        self._c = client
        self._space = space

    def _find_program(self, name: str, version: str) -> str | None:
        """Existing program object id by name+version, or None."""
        recs = self._c.query_objects(
            self._space,
            filter={f"{PROGRAM_TYPE}.name": name, f"{PROGRAM_TYPE}.version": version},
            limit=1)
        return recs[0]["id"] if recs else None

    def _in_space_fingerprint(self, object_id: str) -> str | None:
        src = self._c.query(self._space, object_id, "program_source")
        if not src:
            return None
        code = src[0].get("code", "")
        desc_recs = self._c.query(self._space, object_id, "program_description")
        desc = desc_recs[0].get("text", "") if desc_recs else ""
        methods = self._c.query(self._space, object_id, "program_methods")
        method_tuples = [(m.get("id", ""), m.get("name", ""), m.get("kind", "getter"),
                          m.get("text", "")) for m in methods]
        man_recs = self._c.query(self._space, object_id, "program_manifest")
        manifest = man_recs[0].get("manifest", {}) if man_recs else {}
        return _fingerprint(code=code, desc=desc, method_tuples=method_tuples, manifest=manifest)

    def deploy_one(self, p: ProgramSource) -> str:
        """Create-or-update one program. Returns 'created' | 'updated' |
        'unchanged' (hash-gated)."""
        oid = self._find_program(p.name, p.version)
        if oid is not None and self._in_space_fingerprint(oid) == p.fingerprint():
            return "unchanged"

        desc, methods = p.split()
        any_tool = bool(desc) and bool(methods)

        if oid is None:
            res = self._c.create_object(self._space, {
                "types": [PROGRAM_TYPE],
                "initialProperties": {
                    "any": {"name": p.name},
                    PROGRAM_TYPE: {"name": p.name, "version": p.version, "any_tool": any_tool},
                }})
            oid = res["objectId"]
            status = "created"
        else:
            self._c.set_properties(self._space, oid, PROGRAM_TYPE, {"any_tool": any_tool})
            status = "updated"

        self._c.upsert_record(self._space, oid, "program_source", "main", {"code": p.code})
        if p.manifest:
            self._c.upsert_record(self._space, oid, "program_manifest", "main",
                                  {"manifest": p.manifest})
        else:
            # a program that lost its manifest must not keep a stale one
            # (the in-space fingerprint would never converge)
            self._clear_dataset(oid, "program_manifest")
        # rewrite docs: clear then write (a program that lost its .md drops docs)
        self._clear_docs(oid)
        if desc:
            self._c.upsert_record(self._space, oid, "program_description", "main", {"text": desc})
        for m in methods:
            self._c.upsert_record(self._space, oid, "program_methods", m.bare_name, {
                "name": m.name, "kind": m.kind, "text": m.text, "pos": m.pos})
        return status

    def _clear_docs(self, oid: str) -> None:
        for dataset in ("program_description", "program_methods"):
            self._clear_dataset(oid, dataset)

    def _clear_dataset(self, oid: str, dataset: str) -> None:
        for rec in self._c.query(self._space, oid, dataset):
            self._c.modify(self._space, {
                "objectId": oid, "dataset": dataset,
                "records": [{"id": rec["id"], "ops": [{"type": "$unset", "path": ""}]}]})

    def deploy_dir(self, src_dir: Path) -> dict[str, str]:
        """Deploy every program in a dir. Returns {spec: status}."""
        return {p.spec: self.deploy_one(p) for p in load_programs(src_dir)}
