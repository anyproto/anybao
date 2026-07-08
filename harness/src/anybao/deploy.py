"""Deploy tool — the generic program/skill publisher (plan §4, was the
bobrik-specific sync). Reads a source dir and writes to a TARGET space
(the agent overlay, a std overlay, any package space). Hash-gated:
unchanged programs skip all writes. This unwelds the old
bootstrapSystemFiles from its hardcoded space.

Program storage (mirrors internal/program): a `program`-typed object per
program, source in `program_source`/"main"/{code}, split tool docs in
`program_description`/"main"/{text} + one `program_methods` record per
method, `program.any_tool` = has description AND ≥1 method. A program is
`<name>@<version>` (the filename convention `name@vN.py`).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .anyclient import AnyClient
from .toolmd import split_tool_markdown

PROGRAM_TYPE = "program"
_NAME_VER = re.compile(r"^(?P<name>.+)@(?P<version>v\d+)$")


def _fingerprint(code: str, desc: str, method_tuples: list[tuple]) -> str:
    """Over the SPLIT form (code + description + sorted methods) so disk
    and in-space sides normalize identically — never over raw markdown
    (which wouldn't round-trip). method_tuples: (bare_name, name, kind, text)."""
    h = hashlib.sha256()
    h.update(code.encode())
    h.update(b"\x00d\x00")
    h.update(desc.encode())
    for bare, name, kind, text in sorted(method_tuples):
        h.update(f"\x00m\x00{bare}\x00{name}\x00{kind}\x00{text}".encode())
    return h.hexdigest()


@dataclass
class ProgramSource:
    name: str
    version: str
    code: str
    tool_md: str = ""   # optional tool-description markdown

    @property
    def spec(self) -> str:
        return f"{self.name}@{self.version}"

    def split(self) -> tuple[str, list]:
        return split_tool_markdown(self.tool_md) if self.tool_md else ("", [])

    def fingerprint(self) -> str:
        desc, methods = self.split()
        return _fingerprint(code=self.code, desc=desc,
                            method_tuples=[(m.bare_name, m.name, m.kind, m.text) for m in methods])


def load_programs(src_dir: Path) -> list[ProgramSource]:
    """Read `<name>@vN.py` program files (+ optional `<name>@vN.md` tool
    docs) from a directory."""
    out: list[ProgramSource] = []
    for py in sorted(src_dir.glob("*.py")):
        m = _NAME_VER.match(py.stem)
        if not m:
            continue
        md = py.with_suffix(".md")
        out.append(ProgramSource(
            name=m["name"], version=m["version"], code=py.read_text(),
            tool_md=md.read_text() if md.exists() else ""))
    return out


class FrozenVersionError(Exception):
    """Published overlay versions never mutate — edits bump `name@vN`."""


class Deployer:
    def __init__(self, client: AnyClient, *, space: str, frozen: bool = False):
        self._c = client
        self._space = space
        self._frozen = frozen

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
        return _fingerprint(code=code, desc=desc, method_tuples=method_tuples)

    def deploy_one(self, p: ProgramSource) -> str:
        """Create-or-update one program. Returns 'created' | 'updated' |
        'unchanged' (hash-gated)."""
        oid = self._find_program(p.name, p.version)
        if oid is not None and self._in_space_fingerprint(oid) == p.fingerprint():
            return "unchanged"
        if oid is not None and self._frozen:
            raise FrozenVersionError(
                f"{p.spec} is published in a frozen overlay — bump the version")

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
            for rec in self._c.query(self._space, oid, dataset):
                self._c.modify(self._space, {
                    "objectId": oid, "dataset": dataset,
                    "records": [{"id": rec["id"], "ops": [{"type": "$unset", "path": ""}]}]})

    def deploy_dir(self, src_dir: Path) -> dict[str, str]:
        """Deploy every program in a dir. Returns {spec: status}."""
        return {p.spec: self.deploy_one(p) for p in load_programs(src_dir)}
