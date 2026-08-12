"""The `any` server client — read and write everything in a space.

Everything is a typed object. Flat surface: every space-scoped
function takes `spaceConfig` FIRST (cross-space is normal) — a space
NAME or id string (names resolve against the live space list; an
unknown or ambiguous name errors listing every space), or a mapping
with `spaceId`/`id` (a `list_spaces()` row, the bound
`currentUserSpace` / `baoSpaceConfig` cell globals). Account-level
calls (`list_spaces`, `create_space`) take none. Types and properties
are named by xKey — resolved to content ids both ways; rows come back
xKey-nested. Errors raise `AnyError` ({code, message} from the
wire); a bad spaceConfig is a TypeError naming the accepted forms."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# Built on the http syscall: JSON transport, error-envelope mapping
# (AnyError), the NUL write guard, typed per-route calls.

import json


def _sanitize_nuls(obj):
    """Strip NUL bytes from strings before any write — anyenc/fastjson
    rejects \\x00 in JSON strings, so we guard at the write boundary.
    Binary-ish HTTP bodies are the realistic source."""
    if isinstance(obj, str):
        return obj.replace("\x00", "�") if "\x00" in obj else obj
    if isinstance(obj, dict):
        return {k: _sanitize_nuls(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_nuls(v) for v in obj]
    return obj


def _slugify_xkey(name):
    """Stable snake_case programmatic key from a display name:
    "Agent Memory" -> "agent_memory", "ComicBook" -> "comic_book"."""
    out = []
    prev_lower = False
    for ch in str(name):
        if ch.isalnum():
            if ch.isupper() and prev_lower:
                out.append("_")
            out.append(ch.lower())
            prev_lower = ch.islower() or ch.isdigit()
        else:
            out.append("_")
            prev_lower = False
    slug = "".join(out)
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_")


# Recovery hints appended to known error codes. The server message
# states the fault (and for these codes enumerates the fix material —
# accepted fields / the unset variable); the hint adds the client-side
# idiom that resolves it. Keep entries to codes where a local action
# exists — never paraphrase the server message itself.
_HINTS = {
    "request.unknown_field": (
        "strict body — resend with only the accepted fields named in "
        "the message; stray keys are rejected, never silently dropped"),
    "object.id_required": (
        "an id variable was unset — re-query the id in this same cell "
        "instead of retyping or interpolating a stale one"),
    "filter.unknown_operator": (
        "the filter GRAMMAR is wrong (operator token), not your keys — "
        "readable xKey paths are fine, they resolve client-side; pick "
        "an operator from the list in the message"),
    "filter.invalid": (
        "the filter GRAMMAR is wrong at the named path (operand type / "
        "array shape), not your keys — readable xKey paths are fine, "
        "they resolve client-side"),
}


class AnyError(Exception):
    """A >=400 reply, decoded from the server's error envelope
    `{"error": {"code", "message"}}` — message relayed verbatim, plus
    a client-side recovery hint for known codes."""

    def __init__(self, status, code, message):
        self.status = status
        self.code = code
        self.message = message
        text = f"{status} {code}: {message}"
        if code in _HINTS:
            text += f" (hint: {_HINTS[code]})"
        super().__init__(text)


# Space-row fields the model can use; the rest (push key material,
# settings, index pointers, icon, author hash) is sync plumbing — it
# never belongs in model context (ADR-010 §8). Single-space extras
# (generalChat/agentConfig/agentSecrets ids) survive the trim.
_SPACE_ROW_FIELDS = ("id", "name", "description", "status", "ownRole",
                     "spaceType", "createdAt", "generalChatObjectId",
                     "agentConfigObjectId", "agentSecretsObjectId")


def _trim_space_row(r):
    return {k: r[k] for k in _SPACE_ROW_FIELDS
            if r.get(k) not in (None, "", {})}


def _ui_context_rank(rec):
    group = rec.get("ui_context") or {}
    return (group.get("updated_at") or 0, rec.get("modifiedAt") or 0)


def _ui_context_pointer(rec):
    if rec is None:
        return None
    group = rec.get("ui_context") or {}
    return {"spaceId": group.get("space_id", ""),
            "objectId": group.get("object_id", ""),
            "view": group.get("view", ""),
            "updatedAt": group.get("updated_at") or 0}


# Builtin type namespaces whose group + property keys are already literal
# handles (`any.name`, `any.types`, `nav.parentId`, `program.name`). They are
# never reverse-mapped on read nor xKey-resolved on write — see the xKey
# normalization contract in ADR-006 §6.
_RESERVED_GROUPS = {"any", "nav", "program", "_ver"}


class _Client:
    def __init__(self, base_url):
        self._base = base_url.rstrip("/")
        # Per-space type/property catalog, memoized for the client's lifetime
        # (one cell). Resolves xKey<->id both ways so the agent reads/writes
        # types and properties by their stable xKey slug, never raw content
        # ids — ADR-006 §6. Invalidated after create_type / add_property.
        self._types_cache = {}   # space -> {"by_id", "by_xkey", "rows"}
        self._props_cache = {}   # (space, type_id) -> [prop rows]
        self._spaces_cache = None   # space rows for name resolution (§8)

    def _call(self, verb, path, body=None):
        payload = {"url": self._base + path}
        if body is not None:
            payload["json"] = _sanitize_nuls(body)   # write guard
        reply = effect("http." + verb, payload)  # noqa: F821 - guest global
        raw = reply.get("body") or ""
        if reply["status"] >= 400:
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                data = {}
            err = data.get("error", {}) if isinstance(data, dict) else {}
            raise AnyError(reply["status"], err.get("code", "unknown"),
                           err.get("message", ""))
        return json.loads(raw) if raw else {}

    # --- space-name resolution (ADR-010 §8) ----------------------------------
    # Spaces resolve by NAME the way types/props resolve by xKey: a
    # memoized list_spaces catalog, refresh-once-on-miss, loud errors
    # carrying the space list. Id-shaped strings skip the catalog.
    def _spaces(self):
        if self._spaces_cache is None:
            self._spaces_cache = self._call("get", "/v1/spaces").get("spaces", [])
        return self._spaces_cache

    def _resolve_space(self, sid, _retried=False):
        if "." in sid and " " not in sid and len(sid) >= 20:
            return sid                        # id-shaped: pass through
        active = [r for r in self._spaces() if r.get("status") == "active"]
        hits = [r for r in active if r.get("name") == sid]
        if not hits:
            hits = [r for r in active
                    if (r.get("name") or "").casefold() == sid.casefold()]
        if len(hits) == 1:
            return hits[0]["id"]
        if len(hits) > 1:
            raise ValueError(
                f'space name "{sid}" is ambiguous: '
                + ", ".join(f'"{h.get("name")}" ({h["id"]})' for h in hits)
                + " — pass the id")
        if not _retried:
            self._spaces_cache = None         # fresh space? refresh once
            return self._resolve_space(sid, True)
        if not active:
            return sid    # degenerate env (no listable spaces): server decides
        names = ", ".join(f'"{r.get("name") or r["id"]}"' for r in active)
        raise ValueError(f'no space named "{sid}" — spaces: {names}')

    # --- xKey catalog + resolution (ADR-006 §6) ------------------------------
    # The server stores and validates by content-id: a value lives at
    # record[typeId][propId], and create/query take those ids verbatim (it
    # does NOT resolve xKeys). This layer is the bobrik-watch anyHelper
    # catalog ported to the guest client: memoize the type list + each type's
    # property defs per space, resolve readable xKeys -> ids on write, and
    # reverse-map records -> xKey-nested on read. Builtins (id == xKey) pass
    # through untouched.
    def _catalog(self, space):
        # a non-string space (a list_spaces() row or the whole list)
        # otherwise surfaces frames deep as an unhashable-key TypeError
        if not isinstance(space, str) or not space:
            raise TypeError(
                f"space must be a space id string, got {type(space).__name__}"
                " — pick one id from list_spaces()")
        cat = self._types_cache.get(space)
        if cat is None:
            by_id, by_xkey, rows = {}, {}, []
            for t in self.list_types(space):
                tid = t.get("id")
                if not tid or tid in by_id:
                    continue           # dedup (nav is listed twice)
                by_id[tid] = t
                if t.get("xKey"):
                    by_xkey.setdefault(t["xKey"], tid)
                rows.append(t)
            cat = {"by_id": by_id, "by_xkey": by_xkey, "rows": rows}
            self._types_cache[space] = cat
        return cat

    def _type_props(self, space, type_id):
        key = (space, type_id)
        props = self._props_cache.get(key)
        if props is None:
            props = self._fetch_props(space, type_id)
            self._props_cache[key] = props
        return props

    def _cat_invalidate(self, space):
        self._types_cache.pop(space, None)
        for k in [k for k in self._props_cache if k[0] == space]:
            self._props_cache.pop(k, None)

    @staticmethod
    def _is_user_type(row):
        # Builtins report xKey == id (chat, program, nav, any); user types
        # have a CID id and a slug xKey — only those are (reverse-)mapped.
        return bool(row) and row.get("id") and row.get("id") != row.get("xKey")

    def _resolve_type_seg(self, space, seg, _retried=False):
        """A type xKey or id -> type id (or None). Refreshes the catalog once
        on miss so a freshly-created type resolves."""
        cat = self._catalog(space)
        if seg in cat["by_id"]:
            return seg
        tid = cat["by_xkey"].get(seg)
        if tid:
            return tid
        if not _retried:
            self._cat_invalidate(space)
            return self._resolve_type_seg(space, seg, True)
        return None

    def _resolve_prop_seg(self, space, type_id, seg, _retried=False):
        """A prop id, xKey, or name under type_id -> prop id (or None)."""
        for p in self._type_props(space, type_id):
            if seg in (p.get("id"), p.get("xKey"), p.get("name")):
                return p.get("id")
        if not _retried:
            self._cat_invalidate(space)
            return self._resolve_prop_seg(space, type_id, seg, True)
        return None

    def _type_handles(self, space):
        return ", ".join(f'"{t.get("xKey") or t.get("id")}" ({t.get("name")})'
                         for t in self._catalog(space)["rows"])

    def _resolve_type_or_raise(self, space, seg):
        tid = self._resolve_type_seg(space, seg)
        if not tid:
            raise ValueError(
                f'type "{seg}" doesn\'t exist. Available types: '
                f"{self._type_handles(space)}")
        return tid

    def _resolve_prop_groups(self, space, groups):
        """Nested write groups {typeXKey: {propXKey: val}} -> the id-keyed
        shape the server writes by {typeId: {propId: val}}. Reserved builtin
        namespaces (any/nav/program) pass through with literal prop keys.
        Unknown type/property keys ERROR — never silently dropped (a
        misplaced key once lost a whole batch of writes)."""
        out = {}
        for gk, gv in groups.items():
            if gk in _RESERVED_GROUPS:
                out[gk] = gv
                continue
            tid = self._resolve_type_or_raise(space, gk)
            if not isinstance(gv, dict):
                raise ValueError(
                    f'value for type group "{gk}" must be a {{prop: value}} '
                    f"object, got {type(gv).__name__}")
            resolved = {}
            for pk, pv in gv.items():
                pid = self._resolve_prop_seg(space, tid, pk)
                if not pid:
                    raise ValueError(f'unknown property "{pk}" on type "{gk}"')
                resolved[pid] = pv
            out[tid] = {**out.get(tid, {}), **resolved}
        return out

    def _resolve_path(self, space, path):
        """A readable dotted filter/sort key "typeXKey.propXKey" -> the
        server's "typeId.propId". Keys whose head is a builtin (any.*,
        nav.*, program.*, bare `id`) pass through unchanged. An
        unresolvable head or prop ERRORS — the store answers a typo'd
        key with a silent empty set, never an error (ADR-006 §6 makes
        both halves of that contract ours, reads like writes)."""
        if not isinstance(path, str) or "." not in path:
            return path
        head, _, tail = path.partition(".")
        if head == "_ver":
            return path
        if head in _RESERVED_GROUPS:
            # Builtin groups carry real property catalogs too (A19): an
            # unvalidated tail would ride the wire and silently match
            # nothing. `any` props with scope "derived" live TOP-LEVEL
            # on records — rewrite to the bare key (any.id -> id).
            props = self._type_props(space, head)
            p = next((q for q in props
                      if tail in (q.get("id"), q.get("xKey"), q.get("name"))),
                     None)
            if p is None:
                handles = ", ".join(f'"{q.get("id")}"' for q in props)
                raise ValueError(
                    f'unknown property "{tail}" on builtin group "{head}" '
                    f'(filter/sort key "{path}"). Available: {handles or "?"}')
            if head == "any" and p.get("scope") == "derived":
                return p["id"]
            return f"{head}.{p['id']}"
        tid = self._resolve_type_seg(space, head)
        if tid is None:
            raise ValueError(
                f'type "{head}" doesn\'t exist (filter/sort key "{path}"). '
                f"Available types: {self._type_handles(space)}")
        row = self._catalog(space)["by_id"].get(tid)
        if not self._is_user_type(row):
            return path
        pid = self._resolve_prop_seg(space, tid, tail)
        if pid is None:
            raise ValueError(
                f'unknown property "{tail}" on type "{head}" '
                f'(filter/sort key "{path}")')
        return f"{tid}.{pid}"

    def _resolve_type_value(self, space, v):
        """Resolve type xKeys appearing as an `any.types` filter VALUE
        (string, list, or operator dict like {$in:[...]}) so the agent can
        filter by type xKey. An unknown handle ERRORS with the catalog —
        forwarding it would only ever match the empty set."""
        if isinstance(v, str):
            return self._resolve_type_or_raise(space, v)
        if isinstance(v, list):
            return [self._resolve_type_value(space, x) for x in v]
        if isinstance(v, dict):
            return {op: self._resolve_type_value(space, iv)
                    for op, iv in v.items()}
        return v

    def _resolve_filter(self, space, filt):
        if not isinstance(filt, dict):
            return filt
        out = {}
        for k, v in filt.items():
            out[self._resolve_path(space, k)] = (
                self._resolve_type_value(space, v) if k == "any.types" else v)
        return out

    def _resolve_sort(self, space, sort):
        if not isinstance(sort, list):
            return sort
        out = []
        for e in sort:
            if isinstance(e, str) and e.startswith("-"):
                out.append("-" + self._resolve_path(space, e[1:]))
            elif isinstance(e, str):
                out.append(self._resolve_path(space, e))
            else:
                out.append(e)
        return out

    def _normalize_record(self, space, rec):
        """Reverse-map a raw wire record to the readable xKey-nested shape:
        record[typeId][propId] -> out[typeXKey][propXKey]. Builtin namespaces
        (any/nav/program) and scalars (id, …) pass through verbatim — their
        keys are already literal handles. `_ver` (CRDT version noise) is
        DROPPED — tokens the model can't use; normalize=False keeps it."""
        if not isinstance(rec, dict):
            return rec
        cat = self._catalog(space)
        # Refresh once if a group key references a type id we don't know yet
        # (catalog stale after an out-of-band create).
        for k, v in rec.items():
            if (isinstance(v, dict) and k not in cat["by_id"]
                    and k not in _RESERVED_GROUPS):
                self._cat_invalidate(space)
                cat = self._catalog(space)
                break
        out = {}
        for k, v in rec.items():
            if k == "_ver":
                continue  # B5: version-vector noise, raw via normalize=False
            row = cat["by_id"].get(k)
            if not self._is_user_type(row) or not isinstance(v, dict):
                out[k] = v
                continue
            label = {p["id"]: (p.get("xKey") or p.get("name") or p["id"])
                     for p in self._type_props(space, k) if p.get("id")}
            out[row.get("xKey") or k] = {label.get(pk, pk): pv
                                         for pk, pv in v.items()}
        # `any.types` VALUES are type ids on the wire — speak xKeys
        # (builtins already are; unknown ids pass through)
        any_group = out.get("any")
        if isinstance(any_group, dict) and isinstance(any_group.get("types"), list):
            out["any"] = {**any_group, "types": [
                ((cat["by_id"].get(t) or {}).get("xKey") or t)
                for t in any_group["types"]]}
        return out

    def _dexify(self, space, v):
        """Deep-map USER-type ids -> xKeys anywhere in a result payload
        (strings, list items, dict keys/values). Builtins are already
        their own xKey; unknown strings pass through untouched."""
        if isinstance(v, str):
            row = self._catalog(space)["by_id"].get(v)
            return (row.get("xKey") or v) if self._is_user_type(row) else v
        if isinstance(v, list):
            return [self._dexify(space, x) for x in v]
        if isinstance(v, dict):
            return {self._dexify(space, k) if isinstance(k, str) else k:
                    self._dexify(space, x) for k, x in v.items()}
        return v

    # --- objects -------------------------------------------------------------
    def create_object(self, space, body):
        """Create a typed object; returns {"objectId"}.

        Top-level `name` / `description` route into the `any` group
        (parity with update_object). Everything else: `types` entries
        and `initialProperties` group + property keys are given as
        xKeys (or ids) and resolved to the content-ids the server
        writes by; reserved groups (any/nav) pass through literal.
        Unknown type/property keys — and unknown TOP-LEVEL keys, which
        the wire would silently drop — error — ADR-006 §6."""
        body = dict(body or {})
        unknown = set(body) - {"types", "initialProperties", "nav",
                               "name", "description"}
        if unknown:
            raise ValueError(
                f"create_object: unknown top-level key(s) {sorted(unknown)} "
                "would be dropped by the wire (it accepts types/"
                "initialProperties/nav). Properties go in initialProperties "
                'keyed by type xKey — {"any": {"name": ...}} — or pass '
                "name/description at top level.")
        name = body.pop("name", None)
        description = body.pop("description", None)
        if name is not None or description is not None:
            grp = body.setdefault("initialProperties", {}).setdefault("any", {})
            if name is not None:
                grp.setdefault("name", name)
            if description is not None:
                grp.setdefault("description", description)
        if isinstance(body.get("types"), list):
            body["types"] = [self._resolve_type_or_raise(space, t)
                             for t in body["types"]]
        if isinstance(body.get("initialProperties"), dict):
            body["initialProperties"] = self._resolve_prop_groups(
                space, body["initialProperties"])
        return self._call("post", f"/v1/spaces/{space}/objects", body)

    def update_object(self, space, object_id, body):
        """Update an object's name / editor body / properties by xKey.

        `body`: {"name"?, "description"?, "markdown"?/"body"?,
        "<typeXKey>": {prop: value}, …} — same nested type-group shape as
        create_object (top-level name/description route into the `any`
        group, parity with create_object). Property keys resolve to ids;
        groups are resolved BEFORE any write so a bad key can't land a
        partial update. Returns {"objectId"}."""
        body = dict(body or {})
        markdown = body.pop("markdown", None)
        body_md = body.pop("body", None)
        if markdown is None:
            markdown = body_md
        name = body.pop("name", None)
        description = body.pop("description", None)
        groups = self._resolve_prop_groups(space, body)   # raises before write
        if name is not None:
            groups.setdefault("any", {}).setdefault("name", name)
        if description is not None:
            groups.setdefault("any", {}).setdefault("description", description)
        if markdown is not None:
            self.put_markdown(space, object_id, markdown)
        for tid, patch in groups.items():
            if patch:
                self._call("post",
                          f"/v1/spaces/{space}/properties/{object_id}/set/{tid}",
                          {"patch": patch})
        return {"objectId": object_id}

    def delete_object(self, space, object_id):
        """Delete an object permanently; returns {} (wire: 204).

        Removes the whole object — for records inside another object's
        dataset use modify/delete-records instead. No undo; confirm
        the id (query/search) before deleting."""
        return self._call("delete", f"/v1/spaces/{space}/objects/{object_id}")

    def query_objects(self, space, *, normalize=True, **opts):
        # normalize is keyword-only: a positional dict here used to land
        # in `normalize` and silently drop the caller's filter — a query
        # for everything where a filtered query was intended.
        """Cross-object query over the per-space objects collection. `filter`
        / `sort` accept readable dotted xKey paths (`task.status`) and an
        `any.types` xKey value, resolved to the server's id paths; an
        UNKNOWN type or property key errors with the catalog (a typo'd
        key would otherwise silently match nothing) — builtin groups
        (`any.*`, `nav.*`, `program.*`) included. Derived `any` props
        resolve to the bare top-level record keys (`any.id` → `id`,
        `any.createdAt` → `createdAt`). Records come back
        NORMALIZED (user-type groups keyed by type xKey, props by prop
        xKey) unless normalize=False — pass that when you need the raw
        content ids (e.g. graph edges) — ADR-006 §6."""
        unknown = set(opts) - {"filter", "sort", "limit", "offset"}
        if unknown:
            # an unvisited key (e.g. `filters=`) would make the server
            # match EVERY object in the space as if it were intended
            raise ValueError(
                f"query_objects: unknown option(s) {sorted(unknown)}; "
                "the wire accepts filter/sort/limit/offset")
        if "filter" in opts:
            opts["filter"] = self._resolve_filter(space, opts["filter"])
        if "sort" in opts:
            opts["sort"] = self._resolve_sort(space, opts["sort"])
        recs = self._call("post", f"/v1/spaces/{space}/objects/query",
                          opts).get("records", [])
        return [self._normalize_record(space, r) for r in recs] if normalize \
            else recs

    def list_programs(self, space, tools_only=False):
        """Programs deployed in a space: [{name, version, anyTool, summary}].

        An overlay/repo or your own working space (ADR-009 §2), sorted
        by name — `summary` is the program's one-liner (ADR-010 §4);
        for depth, `use()` it and `help(mod)`. Import one from another
        space with `use("<alias-or-spaceId>:<name>@<version>")`."""
        out = []
        for p in self.query_objects(space, filter={"any.types": "program"},
                                    limit=200):
            prog = p.get("program") or {}
            if tools_only and not prog.get("any_tool"):
                continue
            out.append({"name": prog.get("name") or "",
                        "version": prog.get("version") or "",
                        "anyTool": bool(prog.get("any_tool")),
                        "summary": (prog.get("summary") or "").strip()})
        return sorted(out, key=lambda r: (r["name"], r["version"]))

    def query(self, space, object_id, dataset, **opts):
        """Per-object dataset query (chat_messages, agent_turns, …).
        None-valued opts are dropped so callers can pass through
        optional filter/sort/limit unchecked."""
        body = {"objectId": object_id, "dataset": dataset}
        body.update({k: v for k, v in opts.items() if v is not None})
        return self._call("post", f"/v1/spaces/{space}/query", body).get("records", [])

    def modify(self, space, body):
        """Low-level dataset write; prefer upsert_record / create_object.

        `body`: {objectId, dataset, records: [{id, upsert?, ops:
        [{type, path, value}]}]} — for partial ops."""
        return self._call("post", f"/v1/spaces/{space}/modify", body)

    def upsert_record(self, space, object_id, dataset, record_id, value):
        """Write one dataset record (whole-value $set, upsert).

        Only REGISTERED datasets are writable (agent_triggers,
        agent_memory_items, agent_roi_injections, …— server-side Go
        handlers), and the host object must carry the dataset's type
        in `any.types`. There is no generic path: an unregistered
        dataset name 500s on write and reads as [] — store ad-hoc
        state as object properties instead (seen live 2026-08-12,
        progressbar probe)."""
        return self.modify(space, {
            "objectId": object_id, "dataset": dataset,
            "records": [{"id": record_id, "upsert": True,
                         "ops": [{"type": "$set", "path": "", "value": value}]}]})

    def aggregate(self, space, pipeline):
        """Run a Mongo-style aggregation pipeline over the space's objects.

        Stages: $match, $group (_id + accumulators: {"$sum": 1},
        {"$count": {}}), $sort, $count. Field refs take xKeys like
        everywhere else — "$book.rating", "$any.types" — resolved in
        $match/$sort keys and $group refs; unknown ones error with the
        catalog. Avg rating per genre: [{"$group": {"_id":
        "$book.genre", "avg": {"$avg": "$book.rating"}}}]. Returns
        {"records": [...]} with ids mapped back to xKeys; unknown
        stages are a 400 (aggregate.bad_pipeline)."""
        r = self._call("post", f"/v1/spaces/{space}/objects/aggregate",
                       {"pipeline": self._resolve_pipeline(space, pipeline)})
        if isinstance(r, dict) and isinstance(r.get("records"), list):
            r["records"] = self._dexify(space, r["records"])
        return r

    def _resolve_pipeline(self, space, pipeline):
        """xKey field refs -> wire ids, only in the grammatically
        unambiguous positions: $match bodies resolve like query filters
        (keys + any.types values, unknown keys error — A4), $sort dict
        keys via _resolve_path, "$type.prop" strings under $group.
        Value-position literals are never touched ($literal ambiguity)."""
        if not isinstance(pipeline, list):
            return pipeline
        out = []
        for stage in pipeline:
            if not isinstance(stage, dict):
                out.append(stage)
                continue
            st = {}
            for op, body in stage.items():
                if op == "$match":
                    st[op] = self._resolve_filter(space, body)
                elif op == "$sort" and isinstance(body, dict):
                    st[op] = {self._resolve_path(space, k): v
                              for k, v in body.items()}
                elif op == "$group":
                    st[op] = self._resolve_field_refs(space, body)
                else:
                    st[op] = body
            out.append(st)
        return out

    def _resolve_field_refs(self, space, v):
        """'$typeXKey.propXKey' -> '$typeId.propId' inside $group values
        (dicts/lists recursed); reserved heads ("$any.types") and
        single-segment refs ("$creator") pass through literal."""
        if isinstance(v, str) and v.startswith("$") and "." in v:
            return "$" + self._resolve_path(space, v[1:])
        if isinstance(v, dict):
            return {k: self._resolve_field_refs(space, x) for k, x in v.items()}
        if isinstance(v, list):
            return [self._resolve_field_refs(space, x) for x in v]
        return v

    # --- editor markdown (content, NOT markdown — wire landmine) --------------
    def get_markdown(self, space, object_id):
        """The object's editor body as markdown TEXT (a string, not a dict)."""
        r = self._call("get", f"/v1/spaces/{space}/objects/{object_id}/editor/markdown")
        return r.get("content", "")

    def put_markdown(self, space, object_id, content):
        """Replace the object's editor body with `content` (markdown).
        Whole-body write — prefer append_markdown when adding."""
        return self._call("put",
                          f"/v1/spaces/{space}/objects/{object_id}/editor/markdown",
                          {"content": content})

    def edit_markdown(self, space, object_id, edits):
        """Surgical text edits on the editor body — THE point-edit path
        (never get→replace→put, which clobbers concurrent edits).

        `edits`: [{"oldText", "newText", "replaceAll"?}] — matched
        server-side against current state (exact, then whole-line
        fuzzy). All-or-nothing; oldText must be unique unless
        replaceAll. Tick a checkbox: [{"oldText": "- [ ] Buy milk",
        "newText": "- [x] Buy milk"}]. Typed 400s say what to fix:
        markdown.no_match / ambiguous_match (add surrounding lines to
        disambiguate) / overlapping_edits. Returns PUT's {inserted,
        updated, deleted, unchanged}; a no-op edit is a clean 200."""
        return self._call(
            "patch",
            f"/v1/spaces/{space}/objects/{object_id}/editor/markdown",
            {"edits": edits})

    def append_markdown(self, space, object_id, content):
        """Append to the editor body (server-side append-only fast path).

        No read-modify-write, so it can't clobber the body the way a
        get+put race can. Returns the api.MarkdownSetResponse dict."""
        return self._call(
            "post",
            f"/v1/spaces/{space}/objects/{object_id}/editor/markdown/append",
            {"content": content})

    # --- spaces & ui context ---------------------------------------------------
    def list_spaces(self, raw=False):
        """Every space on the account: `{id, name, description?, status,
        ownRole, spaceType, createdAt}` rows.

        Sync internals (push key material, settings, index pointers) are
        TRIMMED — `raw=True` returns the wire rows. Operate on
        `status == "active"` unless asked otherwise."""
        rows = self._call("get", "/v1/spaces").get("spaces", [])
        self._spaces_cache = rows       # doubles as the name catalog (§8)
        return rows if raw else [_trim_space_row(r) for r in rows]

    def get_space(self, space, raw=False):
        """One space's row → {id, name, generalChatObjectId, …}; also THE
        explicit name resolver — `get_space("dev")` works.

        The single-space GET is the only read that carries
        `generalChatObjectId` (+ agentConfig/agentSecrets ids) —
        `list_spaces()` rows omit them by design. Sync internals are
        trimmed like list_spaces (`raw=True` for the wire row)."""
        r = self._call("get", f"/v1/spaces/{space}")
        return r if raw else _trim_space_row(r)

    def general_chat(self, space):
        """The space's canonical chat id (its `generalChatObjectId`).

        Every space derives exactly ONE general chat from a fixed
        seed. Post there via `chat_send`; READ it via `query(space,
        chat_id, "chat_messages", sort=["-createdAt"], limit=n)` —
        never create a chat object or pick one from a query:
        name-matched "general" chats are peer-made impostors that
        split the conversation (the derived chat carries no
        name/nav)."""
        return self.get_space(space)["generalChatObjectId"]

    def create_space(self, name, description=None):
        """Create a new top-level space; returns its (trimmed) row.

        `id` is the new space id, and `generalChatObjectId` its
        derived general chat (every space has exactly one; write chat
        there, never create chat objects). The space starts empty:
        resolve/create types against it before typed writes (types and
        xKeys are per-space). Check `list_spaces()` first — don't mint
        a duplicate of an existing active space."""
        body = {"name": name, "spaceType": "anytype.space"}
        if description:
            body["description"] = description
        r = self._call("post", "/v1/spaces", body)
        self._spaces_cache = None    # new space -> refresh the name catalog
        return _trim_space_row(r)

    def get_ui_context(self, space):
        """The user's current view — the `ui_context` pointer any-ui keeps.

        Maintained in the agent space (xKey contract with any-ui:
        props space_id / object_id / view / updated_at). Returns
        {spaceId, objectId, view, updatedAt} — updatedAt is client ms,
        check freshness before trusting — or None when the UI has never
        reported (type or pointer absent)."""
        recs = self._ui_context_recs(space)
        return _ui_context_pointer(recs[0] if recs else None)

    # TODO: ui-context protocol rework pending — this last-modified-wins +
    # delete-stale is a stopgap (user, 2026-08-12)
    def _ui_context_recs(self, space):
        """Every ui_context pointer object, freshest first.

        any-ui's ensurePointerObject queries the pointer by name and
        creates one when the query comes back empty, so racing clients
        (and index lag right after a create) leave the space with
        several pointers, each client then writing only to its own.
        Rank: the protocol's own `updated_at` (client ms), server
        `modifiedAt` breaking ties for pointers written before the
        prop existed."""
        try:
            recs = self.query_objects(space, filter={"any.types": "ui_context"},
                                      limit=50)   # xKey-normalized (ADR-006 §6)
        except ValueError:
            return []   # type absent = UI never reported in this space
        return sorted(recs, key=_ui_context_rank, reverse=True)

    def _prune_ui_contexts(self, space):
        """Delete every ui_context pointer but the freshest; returns its
        pointer (get_ui_context shape) or None.

        The duplicate stopgap above, as a mutation — kept out of
        get_ui_context so that stays a pure getter (ADR-001 §7: a
        declared getter whose span mutates is an inconsistency). A
        delete that fails is not worth failing a run over: the survivor
        is returned either way."""
        recs = self._ui_context_recs(space)
        for r in recs[1:]:
            try:  # noqa: SIM105 - contextlib is one more guest import for a stopgap
                self.delete_object(space, r.get("id"))
            except AnyError:
                pass
        return _ui_context_pointer(recs[0] if recs else None)

    # --- types & properties (catalog source) ----------------------------------
    def list_types(self, space):
        """Every type in the space: rows of {id, xKey, name, …} (builtins included)."""
        return self._call("get", f"/v1/spaces/{space}/types").get("types", [])

    def list_properties(self, space, type_key):
        """A type's property definitions: [{id, name, xKey, kind}].

        `type_key` is the type's xKey (builtins: xKey == id); an
        unknown key ERRORS with the available catalog — the server
        would answer a nonexistent id with a silent []. Reference
        properties by xKey everywhere; writes resolve through it."""
        tid = self._resolve_type_or_raise(space, type_key)
        return self._fetch_props(space, tid)

    def _fetch_props(self, space, type_id):
        # wire read by resolved id — internals (catalog, normalize) call
        # this directly so resolution can't recurse into itself
        r = self._call("get", f"/v1/spaces/{space}/types/{type_id}/properties")
        return r.get("properties", r) if isinstance(r, dict) else r

    def create_type(self, space, body):
        """Create a type, then add each property (composite ensure-type).

        body: {"name", "xKey"?, "description"?, "properties"?:
        [{"name", "xKey"?, "kind"?, "format"?}]}. kind ∈ string |
        number | boolean | null | array | object — NOTHING else ("text"
        and "date" are 400s). Dates/links/selects are FORMATS, not
        kinds: {"format": {"type": "date"}} (types: date, datetime,
        links, select, multiselect) with kind omitted — the
        server derives it. xKeys default to a slug of the name.
        Idempotent: an existing type (by xKey) is reused, only MISSING
        properties are added. Returns {"typeId", "xKey", "created",
        "addedProps": {xKey: propId}} — reference everything by xKey
        afterwards."""
        body = dict(body or {})
        props = body.pop("properties", None) or []
        xkey = body.get("xKey") or _slugify_xkey(body.get("name") or "")
        tid = next((t["id"] for t in self.list_types(space)
                    if t.get("xKey") == xkey or t.get("id") == xkey), None)
        created = False
        if tid is None:
            req = {k: body[k] for k in ("name", "description", "iconCid")
                   if k in body}
            req["xKey"] = xkey
            tid = self._call("post", f"/v1/spaces/{space}/types", req)["typeId"]
            created = True
        added = {}
        if props:
            have = {p.get("xKey") for p in self._fetch_props(space, tid)}
            for p in props:
                pxkey = p.get("xKey") or _slugify_xkey(p.get("name") or "")
                if pxkey in have:
                    continue
                extra = {k: p[k] for k in ("kind", "meta") if k in p}
                extra["name"] = p.get("name") or pxkey
                extra["xKey"] = pxkey
                added[pxkey] = self._post_property(space, tid, extra)["propId"]
        self._cat_invalidate(space)   # freshly (re)shaped type -> refresh xKey map
        return {"typeId": tid, "xKey": xkey, "created": created,
                "addedProps": added}

    def add_property(self, space, type_key, body):
        """POST one property onto a type (named by xKey — unknown keys
        error with the catalog). body: {"name", "xKey"?, "kind"?,
        "format"?}. kind ∈ string | number | boolean | null | array |
        object (default "string"); dates/links/selects go via
        {"format": {"type": "date" | "datetime" | "links" | "select" |
        "multiselect"}} with kind omitted (server derives it;
        "tags" is reserved). Returns {"propId": str}."""
        tid = self._resolve_type_or_raise(space, type_key)
        return self._post_property(space, tid, body)

    def _post_property(self, space, type_id, body):
        body = dict(body or {})
        body.setdefault("xKey", _slugify_xkey(body.get("name") or ""))
        if "format" not in body:      # with a format, the server derives
            body.setdefault("kind", "string")   # kind (links⇒array, date⇒string)
        res = self._call("post",
                         f"/v1/spaces/{space}/types/{type_id}/properties", body)
        self._cat_invalidate(space)   # new prop -> refresh the propId map
        return res

    # --- agent turns / chunks (server-assigned seq) ----------------------------
    def append_turn(self, space, chat_id, body):
        """Append an `agent_turns` record (server-assigned seq).
        Harness-level; conversations write these for you. Strict body:
        `{seq?, fromAgent?, userName?, userText?, think?, replies?,
        effects?, messageIds?, traceRef?, interrupted?, llm?}` — llm
        subkeys `{stopReason, inTokens, outTokens, cacheRead,
        cacheWrite, model, costUsd, fuelUsed, cells}`; any other key
        (nested too) is a 400 request.unknown_field."""
        return self._call("post",
                          f"/v1/spaces/{space}/objects/{chat_id}/agent/turns", body)

    def create_chunk(self, space, chat_id, body):
        """Append a compressed history chunk record (harness-level; rollup).
        Strict body: `{seq?, level?, fromAgent?, summary, periodStart,
        periodEnd, fromSeq, toSeq, unitsCovered?}` — stray keys 400."""
        return self._call("post",
                          f"/v1/spaces/{space}/objects/{chat_id}/agent/chunks", body)

    # --- chat messages ---------------------------------------------------------
    def chat_send(self, space, chat_id, body):
        """Post a message to a chat object. `body`: `{"text": ...}` —
        accepted fields exactly `{text, replyToMessageId?, agent?,
        attachments?}`; the body passes through verbatim and the server
        rejects any other key (400 request.unknown_field naming the
        set). The chat id for a space's conversation is
        `general_chat(space)` — never a queried or created chat. To
        READ messages: `query(space, chat_id, "chat_messages",
        sort=["-createdAt"], limit=n)` (agent_turns is the agentlog,
        not the conversation)."""
        return self._call("post",
                          f"/v1/spaces/{space}/objects/{chat_id}/chat/messages", body)

    # --- search & graph ----------------------------------------------------------
    def search(self, space, query, scopes=None, limit=None, mode=None,
               enrich=True):
        """Index search; returns the `{hits, mode, vectorStatus}` envelope.

        Each hit is a matched RECORD, not a resolved object:
        `{data (the matched text), dataset, objectId, recordId, scope,
        score}`. With enrich=True (default) every hit also gets `title`
        (the object's any.name) and `type` (its primary type's display
        name) — and prop-dataset hits gain `prop` ("book.author": which
        property matched, as xKeys) — resolved in ONE batch query. Pass
        enrich=False to skip the extra query when you only need
        objectIds."""
        body = {"query": query}
        if scopes:
            body["scopes"] = scopes
        if limit:
            body["limit"] = limit
        if mode:
            body["mode"] = mode
        result = self._call("post", f"/v1/spaces/{space}/search", body)
        if enrich:
            self._enrich_hits(space, result.get("hits") or [])
        return result

    def _enrich_hits(self, space, hits):
        """Add `title` + `type` to each search hit in place, best-effort:
        one $in query resolves object names/types, list_types maps the
        primary type id to its display name. Never raises — enrichment is
        additive, a failure leaves the raw hits untouched."""
        ids = list({h["objectId"] for h in hits if h.get("objectId")})
        if not ids:
            return
        try:
            objs = self.query_objects(space, filter={"id": {"$in": ids}})
            by_id = {o["id"]: o for o in objs}
            type_name = {t["id"]: t.get("name") for t in self.list_types(space)}
        except AnyError:
            return
        for h in hits:
            obj = by_id.get(h.get("objectId"))
            if not obj:
                continue
            meta = obj.get("any") or {}
            h["title"] = meta.get("name")
            types = meta.get("types") or []
            # primary type = first non-structural (skip nav/editor builtins)
            primary = next((t for t in types if t not in ("nav", "editor")),
                           types[0] if types else None)
            h["type"] = type_name.get(primary, primary)
            # prop-dataset hits carry a raw propId as recordId — name the
            # matched property as "typeXKey.propXKey" (builtin name/
            # description recordIds are already readable)
            rid = h.get("recordId")
            if h.get("dataset") == "prop" and rid not in ("name",
                                                          "description"):
                for tkey in types:   # normalized rows carry xKeys (A3)
                    tid = self._resolve_type_seg(space, tkey)
                    row = self._catalog(space)["by_id"].get(tid or "")
                    if not self._is_user_type(row):
                        continue
                    p = next((p for p in self._type_props(space, tid)
                              if p.get("id") == rid), None)
                    if p:
                        h["prop"] = (f'{row.get("xKey") or tid}.'
                                     f'{p.get("xKey") or p.get("name") or rid}')
                        break

    def backlinks(self, space, object_id):
        """Objects that reference object_id through a links-format property.

        Returns the `backlinks` list unwrapped from the envelope —
        each `{objectId, type, prop}` where type/prop are xKeys (a raw
        id only when unresolvable), never content ids."""
        r = self._call("get", f"/v1/spaces/{space}/objects/{object_id}/backlinks")
        out = []
        for b in r.get("backlinks") or []:
            tid, prop = b.get("typeId"), b.get("propId")
            row = self._catalog(space)["by_id"].get(tid)
            if row:
                for p in self._type_props(space, tid):
                    if p.get("id") == prop:
                        prop = p.get("xKey") or p.get("name") or prop
                        break
            out.append({"objectId": b.get("objectId"),
                        "type": (row or {}).get("xKey") or tid,
                        "prop": prop})
        return out

    # --- agent memory (write path; reads go through /query on the brain) --------
    def get_brain(self, space):
        """The per-space brain object id hosting agent_memory_items.

        `{objectId}` — derived, deterministic, no create race."""
        return self._call("get", f"/v1/spaces/{space}/agent/brain")

    def create_memory(self, space, fields):
        """Create a memory item (category + context required).

        Server resolves the brain object. Returns ModifyResult —
        recordIds[0] is the item id. Strict body: `{fromAgent?,
        category, context, body?, tags?, entities?, keywords?,
        confidence?, importance?, salience?, validFrom?, edges?,
        chatId?, source?, provenance?}` — `source` is a lowercase slug
        ("extraction", "user", …), `provenance` exactly `{fromSeq}`;
        both create-only (not evolvable). Stray keys 400."""
        return self._call("post", f"/v1/spaces/{space}/agent/memory", fields)

    def evolve_memory(self, space, item_id, fields):
        """Evolve a memory item's mutable fields (author-only).

        modifiedAt is bumped server-side; the route is PATCH-only.
        Mutable allow-list exactly `{salience, accessCount, confidence,
        importance, context, body, tags, edges}` — anything else
        (including source/provenance) is a 400."""
        return self._call("patch", f"/v1/spaces/{space}/agent/memory/{item_id}", fields)

    def delete_memory(self, space, item_id):
        """Delete a memory item by id (author-only)."""
        return self._call("delete", f"/v1/spaces/{space}/agent/memory/{item_id}")


# --- flat module surface (ADR-010 §8) ----------------------------------------
# One private _Client instance carries the connection + per-space xKey
# catalog caches; the public API is these module functions, so the whole
# surface renders into the `## Tools` inventory (describe() lists module
# functions only). Docstrings live ONCE, on the _Client methods, and are
# lifted onto the wrappers below — help(search) shows the method doc.

_instance = None


def _c():
    global _instance
    if _instance is None:
        base = effect("config.get", {"key": "any.base_url"})["value"]  # noqa: F821
        _instance = _Client(base)
    return _instance


def _sid(sc):
    # Shape guard only — string content (id vs name) is judged by
    # _resolve_space against the live space list, so names may carry
    # whitespace. Rejected here: what cannot be a space ref at all.
    if isinstance(sc, str) and sc.strip():
        return sc
    if isinstance(sc, dict):
        sid = sc.get("spaceId") or sc.get("id")
        if isinstance(sid, str) and sid:
            return sid
    raise TypeError(
        "spaceConfig (the FIRST argument) must name a space: a space "
        "NAME or id string, a list_spaces() row, or a bound cell global "
        f"(currentUserSpace, baoSpaceConfig) — got {sc!r}"[:300])


def _space(sc):
    return _c()._resolve_space(_sid(sc))


@span("any.create_object", kind="mutator")  # noqa: F821 - guest global
def create_object(spaceConfig, body):
    return _c().create_object(_space(spaceConfig), body)


@span("any.update_object", kind="mutator")  # noqa: F821 - guest global
def update_object(spaceConfig, object_id, body):
    return _c().update_object(_space(spaceConfig), object_id, body)


@span("any.delete_object", kind="mutator")  # noqa: F821 - guest global
def delete_object(spaceConfig, object_id):
    return _c().delete_object(_space(spaceConfig), object_id)


@span("any.query_objects", kind="getter")  # noqa: F821 - guest global
def query_objects(spaceConfig, *, normalize=True, **opts):
    return _c().query_objects(_space(spaceConfig), normalize=normalize, **opts)


@span("any.list_programs", kind="getter")  # noqa: F821 - guest global
def list_programs(spaceConfig, tools_only=False):
    return _c().list_programs(_space(spaceConfig), tools_only)


@span("any.query", kind="getter")  # noqa: F821 - guest global
def query(spaceConfig, object_id, dataset, **opts):
    return _c().query(_space(spaceConfig), object_id, dataset, **opts)


@span("any.modify", kind="mutator")  # noqa: F821 - guest global
def modify(spaceConfig, body):
    return _c().modify(_space(spaceConfig), body)


@span("any.upsert_record", kind="mutator")  # noqa: F821 - guest global
def upsert_record(spaceConfig, object_id, dataset, record_id, value):
    return _c().upsert_record(_space(spaceConfig), object_id, dataset,
                              record_id, value)


@span("any.aggregate", kind="getter")  # noqa: F821 - guest global
def aggregate(spaceConfig, pipeline):
    return _c().aggregate(_space(spaceConfig), pipeline)


@span("any.get_markdown", kind="getter")  # noqa: F821 - guest global
def get_markdown(spaceConfig, object_id):
    return _c().get_markdown(_space(spaceConfig), object_id)


@span("any.put_markdown", kind="mutator")  # noqa: F821 - guest global
def put_markdown(spaceConfig, object_id, content):
    return _c().put_markdown(_space(spaceConfig), object_id, content)


@span("any.edit_markdown", kind="mutator")  # noqa: F821 - guest global
def edit_markdown(spaceConfig, object_id, edits):
    return _c().edit_markdown(_space(spaceConfig), object_id, edits)


@span("any.append_markdown", kind="mutator")  # noqa: F821 - guest global
def append_markdown(spaceConfig, object_id, content):
    return _c().append_markdown(_space(spaceConfig), object_id, content)


@span("any.list_spaces", kind="getter")  # noqa: F821 - guest global
def list_spaces(raw=False):
    return _c().list_spaces(raw)


@span("any.get_space", kind="getter")  # noqa: F821 - guest global
def get_space(spaceConfig, raw=False):
    return _c().get_space(_space(spaceConfig), raw)


@span("any.general_chat", kind="getter")  # noqa: F821 - guest global
def general_chat(spaceConfig):
    return _c().general_chat(_space(spaceConfig))


@span("any.create_space", kind="mutator")  # noqa: F821 - guest global
def create_space(name, description=None):
    return _c().create_space(name, description)


@span("any.get_ui_context", kind="getter")  # noqa: F821 - guest global
def get_ui_context(spaceConfig):
    return _c().get_ui_context(_space(spaceConfig))


# `_`-private: describe() hides it from the `## Tools` inventory — the
# duplicate-pointer stopgap is the loop's business (toolcaller calls it
# once per run), not a tool the model should reach for.
@span("any.prune_ui_contexts", kind="mutator")  # noqa: F821 - guest global
def _prune_ui_contexts(spaceConfig):
    return _c()._prune_ui_contexts(_space(spaceConfig))


@span("any.list_types", kind="getter")  # noqa: F821 - guest global
def list_types(spaceConfig):
    return _c().list_types(_space(spaceConfig))


@span("any.list_properties", kind="getter")  # noqa: F821 - guest global
def list_properties(spaceConfig, type_key):
    return _c().list_properties(_space(spaceConfig), type_key)


@span("any.create_type", kind="mutator")  # noqa: F821 - guest global
def create_type(spaceConfig, body):
    return _c().create_type(_space(spaceConfig), body)


@span("any.add_property", kind="mutator")  # noqa: F821 - guest global
def add_property(spaceConfig, type_key, body):
    return _c().add_property(_space(spaceConfig), type_key, body)


@span("any.append_turn", kind="mutator")  # noqa: F821 - guest global
def append_turn(spaceConfig, chat_id, body):
    return _c().append_turn(_space(spaceConfig), chat_id, body)


@span("any.create_chunk", kind="mutator")  # noqa: F821 - guest global
def create_chunk(spaceConfig, chat_id, body):
    return _c().create_chunk(_space(spaceConfig), chat_id, body)


@span("any.chat_send", kind="mutator")  # noqa: F821 - guest global
def chat_send(spaceConfig, chat_id, body):
    return _c().chat_send(_space(spaceConfig), chat_id, body)


@span("any.search", kind="getter")  # noqa: F821 - guest global
def search(spaceConfig, query, scopes=None, limit=None, mode=None,
           enrich=True, **kw):
    if kw:   # A18: the guessed types= kwarg gets a redirect, not a bare TypeError
        raise TypeError(
            f"search() got unexpected keyword(s) {sorted(kw)} — search has "
            "no type filter. List objects of a type with "
            "query_objects(spaceConfig, filter={'any.types': '<xKey>'}), "
            "or post-filter hits on h['type'].")
    return _c().search(_space(spaceConfig), query, scopes, limit, mode, enrich)


@span("any.backlinks", kind="getter")  # noqa: F821 - guest global
def backlinks(spaceConfig, object_id):
    return _c().backlinks(_space(spaceConfig), object_id)


@span("any.get_brain", kind="getter")  # noqa: F821 - guest global
def get_brain(spaceConfig):
    return _c().get_brain(_space(spaceConfig))


@span("any.create_memory", kind="mutator")  # noqa: F821 - guest global
def create_memory(spaceConfig, fields):
    return _c().create_memory(_space(spaceConfig), fields)


@span("any.evolve_memory", kind="mutator")  # noqa: F821 - guest global
def evolve_memory(spaceConfig, item_id, fields):
    return _c().evolve_memory(_space(spaceConfig), item_id, fields)


@span("any.delete_memory", kind="mutator")  # noqa: F821 - guest global
def delete_memory(spaceConfig, item_id):
    return _c().delete_memory(_space(spaceConfig), item_id)


# lift the method docstrings onto the public functions — ONE authored
# copy (on _Client), rendered by describe()/help() from here
for _f in (create_object, update_object, delete_object, query_objects,
           list_programs, query, modify, upsert_record, aggregate,
           get_markdown, put_markdown, edit_markdown, append_markdown,
           list_spaces, get_space, general_chat, create_space,
           get_ui_context, list_types, list_properties, create_type,
           add_property, append_turn, create_chunk, chat_send, search,
           backlinks, get_brain, create_memory, evolve_memory,
           delete_memory):
    _f.__doc__ = getattr(_Client, _f.__name__).__doc__
del _f
