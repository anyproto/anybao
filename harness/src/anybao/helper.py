"""anyHelper — the curated, agent-ergonomic surface over anyclient.

Ported from the JS anyHelper per docs/helper-style.md. The nested
type-group read/write shape, per-space catalog resolution (xKey↔propId),
and the no-silent-drop rule are the load-bearing conventions. This is
the surface the cell `any` facade wraps and the coverage manifest maps
to.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .anyclient import AnyClient

RESERVED_KEYS = {"name", "body", "markdown", "types", "space"}
CATALOG_TTL_S = 30.0


class HelperError(Exception):
    pass


@dataclass
class _SpaceCatalog:
    """Per-space type/prop resolution, TTL-bounded (style guide §4)."""

    types_by_key: dict[str, str]              # xKey/name → typeId
    label_by_type: dict[str, str]             # typeId → readable label (xKey preferred)
    props_by_type: dict[str, dict[str, str]]  # typeId → {xKey → propId}
    prop_xkey_by_id: dict[str, dict[str, str]]  # typeId → {propId → xKey}
    fetched_at: float = field(default_factory=lambda: 0.0)


class Helper:
    def __init__(self, client: AnyClient, *, default_space: str, now=time.monotonic):
        self._c = client
        self._default_space = default_space
        self._now = now
        self._catalogs: dict[str, _SpaceCatalog] = {}

    # --- catalog -----------------------------------------------------------
    def _catalog(self, space: str) -> _SpaceCatalog:
        cat = self._catalogs.get(space)
        if cat and (self._now() - cat.fetched_at) < CATALOG_TTL_S:
            return cat
        types_by_key: dict[str, str] = {}
        label_by_type: dict[str, str] = {}
        props_by_type: dict[str, dict[str, str]] = {}
        prop_xkey_by_id: dict[str, dict[str, str]] = {}
        for t in self._c.list_types(space):
            tid = t.get("id") or t.get("typeId")
            if not tid:
                continue
            for k in (t.get("key"), t.get("xKey"), t.get("name"), tid):
                if k:
                    types_by_key[k] = tid
            label_by_type[tid] = t.get("xKey") or t.get("key") or t.get("name") or tid
            byx, byid = {}, {}
            for p in self._c.list_properties(space, tid):
                pid, xk = p.get("id"), p.get("xKey") or p.get("name")
                if pid and xk:
                    byx[xk] = pid
                    byid[pid] = xk
            props_by_type[tid] = byx
            prop_xkey_by_id[tid] = byid
        cat = _SpaceCatalog(types_by_key, label_by_type, props_by_type,
                            prop_xkey_by_id, self._now())
        self._catalogs[space] = cat
        return cat

    def invalidate(self, space: str | None = None) -> None:
        if space is None:
            self._catalogs.clear()
        else:
            self._catalogs.pop(space, None)

    def resolve_type(self, key: str, space: str | None = None) -> str:
        space = space or self._default_space
        tid = self._catalog(space).types_by_key.get(key)
        if not tid:
            raise HelperError(f"type not found: {key!r} (space {space})")
        return tid

    # --- write shape (nested type groups; no silent drop) ------------------
    def _resolve_groups(self, space: str, data: dict) -> dict[str, dict]:
        """Map {typeKey: {xKey: value}} → {typeId: {propId: value}}.
        Any non-reserved top-level key that is not a resolvable type
        RAISES (style guide §2 — closes the silent-drop hole)."""
        cat = self._catalog(space)
        groups: dict[str, dict] = {}
        for key, val in data.items():
            if key in RESERVED_KEYS:
                continue
            if "." in key:
                raise HelperError(
                    f"dotted key {key!r} is read syntax only — nest writes "
                    f"under the type key: {{{key.split('.')[0]}: {{...}}}}")
            tid = cat.types_by_key.get(key)
            if not tid:
                raise HelperError(
                    f"unknown data key {key!r}: not reserved and not a type "
                    f"(reserved: {sorted(RESERVED_KEYS)})")
            if not isinstance(val, dict):
                raise HelperError(f"property group {key!r} must be a dict of {{prop: value}}")
            byx = cat.props_by_type.get(tid, {})
            resolved = {}
            for xk, v in val.items():
                pid = byx.get(xk)
                if not pid:
                    raise HelperError(f"property {xk!r} not found on type {key!r} "
                                      f"(valid: {sorted(byx)})")
                resolved[pid] = v
            groups[tid] = resolved
        return groups

    def create_object(self, type_key: str, data: dict | None = None,
                      space: str | None = None) -> dict:
        data = data or {}
        space = data.get("space") or space or self._default_space
        type_ids = [self.resolve_type(type_key, space)]
        for extra in data.get("types", []):
            tid = self.resolve_type(extra, space)
            if tid not in type_ids:
                type_ids.append(tid)

        init_props: dict[str, dict] = {}
        if data.get("name"):
            init_props["any"] = {"name": data["name"]}
        for tid, group in self._resolve_groups(space, data).items():
            if group:
                init_props.setdefault(tid, {}).update(group)

        body: dict = {"types": type_ids}
        if init_props:
            body["initialProperties"] = init_props
        res = self._c.create_object(space, body)
        oid = res.get("objectId")
        if not oid:
            raise HelperError(f"create_object: no objectId in response: {res}")

        markdown = data.get("body") or data.get("markdown")
        if markdown:
            self._c.put_markdown(space, oid, markdown)
        return {"id": oid, "name": data.get("name", "")}

    def update_object(self, object_id: str, data: dict, space: str | None = None) -> dict:
        space = data.get("space") or space or self._default_space
        if data.get("name") is not None:
            self._c.set_properties(space, object_id, "any", {"name": data["name"]})
        markdown = data.get("body") or data.get("markdown")
        if markdown is not None:
            self._c.put_markdown(space, object_id, markdown)
        for tid, group in self._resolve_groups(space, data).items():
            if group:
                self._c.set_properties(space, object_id, tid, group)
        return {"id": object_id}

    def delete_object(self, object_id: str, space: str | None = None) -> None:
        self._c.delete_object(space or self._default_space, object_id)

    # --- reads (normalized to the nested shape) ----------------------------
    def get_object(self, object_id: str, space: str | None = None) -> dict:
        space = space or self._default_space
        recs = self._c.query_objects(space, filter={"id": object_id}, limit=1)
        if not recs:
            raise HelperError(f"object not found: {object_id}")
        return self._normalize(space, recs[0])

    def _normalize(self, space: str, rec: dict) -> dict:
        """Re-key raw record property namespaces (typeId → propId) back to
        readable (typeKey-ish → xKey), leaving `any`/`nav` reserved
        groups as-is."""
        cat = self._catalog(space)
        out: dict = {}
        for k, v in rec.items():
            if isinstance(v, dict) and k in cat.prop_xkey_by_id:
                byid = cat.prop_xkey_by_id[k]
                label = cat.label_by_type.get(k, k)
                out[label] = {byid.get(pid, pid): val for pid, val in v.items()}
            else:
                out[k] = v
        return out

    # --- passthroughs ------------------------------------------------------
    def list_spaces(self, status: str | None = None) -> list[dict]:
        return self._c.list_spaces(status)

    def search(self, query: str, *, scopes: list[str] | None = None,
               space: str | None = None, **kw) -> dict:
        return self._c.search(space or self._default_space, query, scopes=scopes, **kw)
