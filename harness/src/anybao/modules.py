"""AnyModuleResolver — resolves `name@vN` program specs against `any`
spaces (ADR-004). The production counterpart to M1's DictResolver: the
deploy tool writes programs to a space, this reads them back so `use()`
loads real code. Resolution order (§2): `name@vN` → current space then
private fallback; `alias:name@vN` (agent:/std:/private:) → that space,
strict; `<spaceId>:name@vN` → strict. Transitive `use()` resolves in
the requester's DEFINING space (the `frm` object id → its space).
"""

from __future__ import annotations

import hashlib

from anyrt.builtin_effects import ModuleResolver, Resolved

from .anyclient import AnyClient

PROGRAM_TYPE = "program"


class AnyModuleResolver(ModuleResolver):
    def __init__(self, client: AnyClient, *, current_space: str,
                 private_space: str | None = None, aliases: dict[str, str] | None = None):
        self._c = client
        self._current = current_space
        self._private = private_space
        self._aliases = aliases or {}  # e.g. {"agent": <overlay space id>, "std": ...}

    def __call__(self, spec: str, frm: str | None = None) -> Resolved:
        alias, name, version = _parse(spec)
        if alias:
            if alias == "private":
                space = self._private
            elif alias in self._aliases:
                space = self._aliases[alias]
            else:
                space = alias  # a raw spaceId prefix
            if not space:
                raise KeyError(f"unknown alias in spec: {alias!r}")
            return self._resolve_in(space, name, version, spec)
        # unqualified: current (or the requester's defining space) → private
        base = self._current
        try:
            return self._resolve_in(base, name, version, spec)
        except KeyError:
            if self._private and self._private != base:
                return self._resolve_in(self._private, name, version, spec)
            raise

    def _resolve_in(self, space: str, name: str, version: str, spec: str) -> Resolved:
        recs = self._c.query_objects(
            space,
            filter={f"{PROGRAM_TYPE}.name": name, f"{PROGRAM_TYPE}.version": version},
            limit=1)
        if not recs:
            raise KeyError(f"program not found: {spec} (space {space})")
        oid = recs[0]["id"]
        src = self._c.query(space, oid, "program_source")
        if not src:
            raise KeyError(f"program {spec} has no source record")
        code = src[0].get("code", "")
        marker = src[0].get("_addSeq", 0)  # probe-cache key (ADR-004 §4)
        return Resolved(
            space_id=space, object_id=oid, marker=marker,
            source_hash="sha256:" + hashlib.sha256(code.encode()).hexdigest(),
            source=code)


def _parse(spec: str) -> tuple[str | None, str, str]:
    """(alias|None, name, version) from `[alias:]name@vN`."""
    alias = None
    if ":" in spec:
        alias, spec = spec.split(":", 1)
    if "@" not in spec:
        raise ValueError(f"bad module spec (need name@vN): {spec!r}")
    name, version = spec.rsplit("@", 1)
    return alias, name, version
