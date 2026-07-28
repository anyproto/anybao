"""The client for the `any` server — everything in a space is a
typed object; this is how you read and write it.

`c = client()`, then `space` is an explicit first argument on every
call (cross-space is normal). Types and properties are named by xKey
— the client resolves them to server content ids, and query rows come
back xKey-nested (never raw ids). Errors raise `AnyError` ({code,
message} from the wire). The full API: `help(c)`."""

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


class AnyError(Exception):
    """A >=400 reply, decoded from the server's error envelope
    `{"error": {"code", "message"}}`."""

    def __init__(self, status, code, message):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"{status} {code}: {message}")


# Builtin type namespaces whose group + property keys are already literal
# handles (`any.name`, `any.types`, `nav.parentId`, `program.name`). They are
# never reverse-mapped on read nor xKey-resolved on write — see the xKey
# normalization contract in ADR-006 §6.
_RESERVED_GROUPS = {"any", "nav", "program", "_ver"}


class Client:
    def __init__(self, base_url):
        self._base = base_url.rstrip("/")
        # Per-space type/property catalog, memoized for the client's lifetime
        # (one cell). Resolves xKey<->id both ways so the agent reads/writes
        # types and properties by their stable xKey slug, never raw content
        # ids — ADR-006 §6. Invalidated after create_type / add_property.
        self._types_cache = {}   # space -> {"by_id", "by_xkey", "rows"}
        self._props_cache = {}   # (space, type_id) -> [prop rows]

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

    # --- xKey catalog + resolution (ADR-006 §6) ------------------------------
    # The server stores and validates by content-id: a value lives at
    # record[typeId][propId], and create/query take those ids verbatim (it
    # does NOT resolve xKeys). This layer is the bobrik-watch anyHelper
    # catalog ported to the guest client: memoize the type list + each type's
    # property defs per space, resolve readable xKeys -> ids on write, and
    # reverse-map records -> xKey-nested on read. Builtins (id == xKey) pass
    # through untouched.
    def _catalog(self, space):
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
            props = self.list_properties(space, type_id)
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
        server's "typeId.propId". Keys whose head isn't a USER type (any.*,
        nav.*, program.*, bare `id`, already-resolved id pairs) pass
        through unchanged."""
        if not isinstance(path, str) or "." not in path:
            return path
        head, _, tail = path.partition(".")
        if head in _RESERVED_GROUPS:   # any.*/nav.*/program.* keys are literal
            return path
        tid = self._resolve_type_seg(space, head)
        row = self._catalog(space)["by_id"].get(tid or "")
        if not self._is_user_type(row):
            return path
        pid = self._resolve_prop_seg(space, tid, tail)
        return f"{tid}.{pid}" if pid else path

    def _resolve_type_value(self, space, v):
        """Resolve type xKeys appearing as an `any.types` filter VALUE
        (string, list, or operator dict like {$in:[...]}) so the agent can
        filter by type xKey. Non-resolving entries pass through."""
        if isinstance(v, str):
            return self._resolve_type_seg(space, v) or v
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
        (any/nav/program) and scalars (id, _ver, …) pass through verbatim —
        their keys are already literal handles."""
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
            row = cat["by_id"].get(k)
            if not self._is_user_type(row) or not isinstance(v, dict):
                out[k] = v
                continue
            label = {p["id"]: (p.get("xKey") or p.get("name") or p["id"])
                     for p in self._type_props(space, k) if p.get("id")}
            out[row.get("xKey") or k] = {label.get(pk, pk): pv
                                         for pk, pv in v.items()}
        return out

    # --- objects -------------------------------------------------------------
    @span("any.create_object", kind="mutator")  # noqa: F821 - guest global
    def create_object(self, space, body):
        """Create a typed object. `types` entries and `initialProperties`
        group + property keys are given as xKeys (or ids) and resolved to the
        content-ids the server writes by; reserved groups (any/nav) pass
        through literal. Unknown type/property keys error — ADR-006 §6."""
        body = dict(body or {})
        if isinstance(body.get("types"), list):
            body["types"] = [self._resolve_type_or_raise(space, t)
                             for t in body["types"]]
        if isinstance(body.get("initialProperties"), dict):
            body["initialProperties"] = self._resolve_prop_groups(
                space, body["initialProperties"])
        return self._call("post", f"/v1/spaces/{space}/objects", body)

    @span("any.update_object", kind="mutator")  # noqa: F821 - guest global
    def update_object(self, space, object_id, body):
        """Update an object's name / editor body / properties by xKey.

        `body`: {"name"?, "markdown"?/"body"?, "<typeXKey>": {prop:
        value}, …} — same nested type-group shape as create_object. Property
        keys resolve to ids; groups are resolved BEFORE any write so a bad
        key can't land a partial update. Returns {"objectId"}."""
        body = dict(body or {})
        markdown = body.pop("markdown", None)
        body_md = body.pop("body", None)
        if markdown is None:
            markdown = body_md
        name = body.pop("name", None)
        groups = self._resolve_prop_groups(space, body)   # raises before write
        if name is not None:
            groups.setdefault("any", {}).setdefault("name", name)
        if markdown is not None:
            self.put_markdown(space, object_id, markdown)
        for tid, patch in groups.items():
            if patch:
                self._call("post",
                          f"/v1/spaces/{space}/properties/{object_id}/set/{tid}",
                          {"patch": patch})
        return {"objectId": object_id}

    @span("any.query_objects", kind="getter")  # noqa: F821 - guest global
    def query_objects(self, space, *, normalize=True, **opts):
        # normalize is keyword-only: a positional dict here used to land
        # in `normalize` and silently drop the caller's filter — a query
        # for everything where a filtered query was intended.
        """Cross-object query over the per-space objects collection. `filter`
        / `sort` accept readable dotted xKey paths (`task.status`) and an
        `any.types` xKey value, resolved to the server's id paths. Records
        come back NORMALIZED (user-type groups keyed by type xKey, props by
        prop xKey) unless normalize=False — pass that when you need the raw
        content ids (e.g. graph edges) — ADR-006 §6."""
        if "filter" in opts:
            opts["filter"] = self._resolve_filter(space, opts["filter"])
        if "sort" in opts:
            opts["sort"] = self._resolve_sort(space, opts["sort"])
        recs = self._call("post", f"/v1/spaces/{space}/objects/query",
                          opts).get("records", [])
        return [self._normalize_record(space, r) for r in recs] if normalize \
            else recs

    @span("any.list_programs", kind="getter")  # noqa: F821 - guest global
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
            summary = (prog.get("summary") or "").strip()
            if not summary:
                # 2026-07-28 migration bridge (ADR-010 §4): until deploy
                # writes the summary property, fall back to the doc
                # dataset's first line. Delete with the datasets.
                desc = self.query(space, p["id"], "program_description",
                                  limit=1)
                text = (desc[0].get("text") or "") if desc else ""
                summary = next((ln.strip() for ln in text.splitlines()
                                if ln.strip()), "")
            out.append({"name": prog.get("name") or "",
                        "version": prog.get("version") or "",
                        "anyTool": bool(prog.get("any_tool")),
                        "summary": summary})
        return sorted(out, key=lambda r: (r["name"], r["version"]))

    @span("any.query", kind="getter")  # noqa: F821 - guest global
    def query(self, space, object_id, dataset, **opts):
        """Per-object dataset query (chat_messages, agent_turns, …).
        None-valued opts are dropped so callers can pass through
        optional filter/sort/limit unchecked."""
        body = {"objectId": object_id, "dataset": dataset}
        body.update({k: v for k, v in opts.items() if v is not None})
        return self._call("post", f"/v1/spaces/{space}/query", body).get("records", [])

    @span("any.modify", kind="mutator")  # noqa: F821 - guest global
    def modify(self, space, body):
        """Low-level dataset write; prefer upsert_record / create_object.

        `body`: {objectId, dataset, records: [{id, upsert?, ops:
        [{type, path, value}]}]} — for partial ops."""
        return self._call("post", f"/v1/spaces/{space}/modify", body)

    @span("any.upsert_record", kind="mutator")  # noqa: F821 - guest global
    def upsert_record(self, space, object_id, dataset, record_id, value):
        """Write one dataset record (whole-value $set, upsert).

        The generic path for plain (unregistered) datasets like
        agent_triggers."""
        return self.modify(space, {
            "objectId": object_id, "dataset": dataset,
            "records": [{"id": record_id, "upsert": True,
                         "ops": [{"type": "$set", "path": "", "value": value}]}]})

    @span("any.aggregate", kind="getter")  # noqa: F821 - guest global
    def aggregate(self, space, pipeline):
        """Run an aggregation pipeline over the space's objects.

        For counts / grouping when a plain query won't do."""
        return self._call("post", f"/v1/spaces/{space}/objects/aggregate",
                          {"pipeline": pipeline})

    # --- editor markdown (content, NOT markdown — wire landmine) --------------
    @span("any.get_markdown", kind="getter")  # noqa: F821 - guest global
    def get_markdown(self, space, object_id):
        """The object's editor body as markdown TEXT (a string, not a dict)."""
        r = self._call("get", f"/v1/spaces/{space}/objects/{object_id}/editor/markdown")
        return r.get("content", "")

    @span("any.put_markdown", kind="mutator")  # noqa: F821 - guest global
    def put_markdown(self, space, object_id, content):
        """Replace the object's editor body with `content` (markdown).
        Whole-body write — prefer append_markdown when adding."""
        return self._call("put",
                          f"/v1/spaces/{space}/objects/{object_id}/editor/markdown",
                          {"content": content})

    @span("any.append_markdown", kind="mutator")  # noqa: F821 - guest global
    def append_markdown(self, space, object_id, content):
        """Append to the editor body (server-side append-only fast path).

        No read-modify-write, so it can't clobber the body the way a
        get+put race can. Returns the api.MarkdownSetResponse dict."""
        return self._call(
            "post",
            f"/v1/spaces/{space}/objects/{object_id}/editor/markdown/append",
            {"content": content})

    # --- spaces & ui context ---------------------------------------------------
    @span("any.list_spaces", kind="getter")  # noqa: F821 - guest global
    def list_spaces(self):
        """Every space on the account as raw rows ({id, name, status, …}).

        Operate on status == "active" unless asked otherwise."""
        return self._call("get", "/v1/spaces").get("spaces", [])

    @span("any.create_space", kind="mutator")  # noqa: F821 - guest global
    def create_space(self, name, description=None):
        """Create a new top-level space; returns the full single-space row.

        `id` is the new space id, and `generalChatObjectId` its
        derived general chat (every space has exactly one; write chat
        there, never create chat objects). The space starts empty:
        resolve/create types against it before typed writes (types and
        xKeys are per-space). Check `list_spaces()` first — don't mint
        a duplicate of an existing active space."""
        body = {"name": name, "spaceType": "anytype.space"}
        if description:
            body["description"] = description
        return self._call("post", "/v1/spaces", body)

    @span("any.get_ui_context", kind="getter")  # noqa: F821 - guest global
    def get_ui_context(self, space):
        """The user's current view — the `ui_context` pointer any-ui keeps.

        Maintained in the agent space (xKey contract with any-ui:
        props space_id / object_id / view / updated_at). Returns
        {spaceId, objectId, view, updatedAt} — updatedAt is client ms,
        check freshness before trusting — or None when the UI has never
        reported (type or pointer absent)."""
        recs = self.query_objects(space, filter={"any.types": "ui_context"},
                                  limit=8)   # xKey-normalized (ADR-006 §6)
        latest, latest_at = None, 0
        for r in recs:
            group = r.get("ui_context") or {}
            at = group.get("updated_at", 0) or 0
            if latest is None or at > latest_at:
                latest, latest_at = group, at
        if latest is None:
            return None
        return {"spaceId": latest.get("space_id", ""),
                "objectId": latest.get("object_id", ""),
                "view": latest.get("view", ""),
                "updatedAt": latest_at}

    # --- types & properties (catalog source) ----------------------------------
    @span("any.list_types", kind="getter")  # noqa: F821 - guest global
    def list_types(self, space):
        """Every type in the space: rows of {id, xKey, name, …} (builtins included)."""
        return self._call("get", f"/v1/spaces/{space}/types").get("types", [])

    @span("any.list_properties", kind="getter")  # noqa: F821 - guest global
    def list_properties(self, space, type_id):
        """A type's property definitions: [{id, name, xKey, kind}].

        The xKey↔propId catalog map; property writes on custom types
        are keyed by these ids."""
        r = self._call("get", f"/v1/spaces/{space}/types/{type_id}/properties")
        return r.get("properties", r) if isinstance(r, dict) else r

    @span("any.create_type", kind="mutator")  # noqa: F821 - guest global
    def create_type(self, space, body):
        """Create a type, then add each property (composite ensure-type).

        bobrik-watch anyHelper semantics: the wire's POST /types takes
        NO inline properties (unknown fields are silently dropped), so
        properties are added one add_property call each. body: {"name", "xKey"?, "description"?,
        "properties"?: [{"name", "xKey"?, "kind"?, "meta"?}]}. xKeys
        default to a slug of the name ("Comic Book" -> "comic_book").
        Idempotent: an existing type (matched by xKey or builtin id) is
        reused and only MISSING properties (by xKey) are added.
        Returns {"typeId": str, "xKey": str, "created": bool,
        "addedProps": {xKey: propId}} — the agent references the type and
        its new properties by xKey afterwards, never the typeId."""
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
            have = {p.get("xKey") for p in self.list_properties(space, tid)}
            for p in props:
                pxkey = p.get("xKey") or _slugify_xkey(p.get("name") or "")
                if pxkey in have:
                    continue
                extra = {k: p[k] for k in ("kind", "meta") if k in p}
                extra["name"] = p.get("name") or pxkey
                extra["xKey"] = pxkey
                added[pxkey] = self.add_property(space, tid, extra)["propId"]
        self._cat_invalidate(space)   # freshly (re)shaped type -> refresh xKey map
        return {"typeId": tid, "xKey": xkey, "created": created,
                "addedProps": added}

    @span("any.add_property", kind="mutator")  # noqa: F821 - guest global
    def add_property(self, space, type_id, body):
        """POST one property onto a type. body: {"name", "xKey"?, "kind"?
        (default "string"), "meta"?}; xKey defaults to a slug of the
        name. Returns {"propId": str}."""
        body = dict(body or {})
        body.setdefault("xKey", _slugify_xkey(body.get("name") or ""))
        body.setdefault("kind", "string")
        res = self._call("post",
                         f"/v1/spaces/{space}/types/{type_id}/properties", body)
        self._cat_invalidate(space)   # new prop -> refresh the propId map
        return res

    # --- agent turns / chunks (server-assigned seq) ----------------------------
    @span("any.append_turn", kind="mutator")  # noqa: F821 - guest global
    def append_turn(self, space, chat_id, body):
        """Append an `agent_turns` record (server-assigned seq).
        Harness-level; conversations write these for you."""
        return self._call("post",
                          f"/v1/spaces/{space}/objects/{chat_id}/agent/turns", body)

    @span("any.create_chunk", kind="mutator")  # noqa: F821 - guest global
    def create_chunk(self, space, chat_id, body):
        """Append a compressed history chunk record (harness-level; rollup)."""
        return self._call("post",
                          f"/v1/spaces/{space}/objects/{chat_id}/agent/chunks", body)

    # --- chat messages ---------------------------------------------------------
    @span("any.chat_send", kind="mutator")  # noqa: F821 - guest global
    def chat_send(self, space, chat_id, body):
        """Post a message to a chat object. `body`: `{"text": ...}`.
        Use the space's general chat id."""
        return self._call("post",
                          f"/v1/spaces/{space}/objects/{chat_id}/chat/messages", body)

    # --- search & graph ----------------------------------------------------------
    @span("any.search", kind="getter")  # noqa: F821 - guest global
    def search(self, space, query, scopes=None, limit=None, mode=None,
               enrich=True):
        """Index search; returns the `{hits, mode, vectorStatus}` envelope.

        Each hit is a matched RECORD, not a resolved object:
        `{data (the matched text), dataset, objectId, recordId, scope,
        score}`. With enrich=True (default) every hit also gets `title`
        (the object's any.name) and `type` (its primary type's display
        name), resolved in ONE batch query — so you can read what was
        found without a follow-up lookup per hit. Pass enrich=False to
        skip the extra query when you only need objectIds."""
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

    @span("any.backlinks", kind="getter")  # noqa: F821 - guest global
    def backlinks(self, space, object_id):
        """Objects that reference object_id through a links-format property.

        Returns the `backlinks` list unwrapped from the
        envelope — each `{objectId, typeId, propId}` (never null)."""
        r = self._call("get", f"/v1/spaces/{space}/objects/{object_id}/backlinks")
        return r.get("backlinks") or []

    # --- agent memory (write path; reads go through /query on the brain) --------
    @span("any.get_brain", kind="getter")  # noqa: F821 - guest global
    def get_brain(self, space):
        """The per-space brain object id hosting agent_memory_items.

        `{objectId}` — derived, deterministic, no create race."""
        return self._call("get", f"/v1/spaces/{space}/agent/brain")

    @span("any.create_memory", kind="mutator")  # noqa: F821 - guest global
    def create_memory(self, space, fields):
        """Create a memory item (category + context required).

        Server resolves the brain object. Returns ModifyResult —
        recordIds[0] is the item id."""
        return self._call("post", f"/v1/spaces/{space}/agent/memory", fields)

    @span("any.evolve_memory", kind="mutator")  # noqa: F821 - guest global
    def evolve_memory(self, space, item_id, fields):
        """Evolve a memory item's mutable fields (author-only).

        modifiedAt is bumped server-side; the route is PATCH-only."""
        return self._call("patch", f"/v1/spaces/{space}/agent/memory/{item_id}", fields)

    @span("any.delete_memory", kind="mutator")  # noqa: F821 - guest global
    def delete_memory(self, space, item_id):
        """Delete a memory item by id (author-only)."""
        return self._call("delete", f"/v1/spaces/{space}/agent/memory/{item_id}")


def client(base_url=None):
    """Bind a Client to the server; the client's API: `help(c)`.

    Base url from config `any.base_url` unless given explicitly.
    Surface: typed objects (create_object / update_object /
    query_objects), per-object datasets (query / upsert_record /
    modify), editor bodies (get_markdown / put_markdown /
    append_markdown), the type catalog (list_types / list_properties /
    create_type / add_property), search, backlinks, the memory brain
    (get_brain / create_memory / evolve_memory), chat (chat_send /
    append_turn), spaces (list_spaces / create_space), programs
    (list_programs), and the user's live view (get_ui_context)."""
    if base_url is None:
        base_url = effect("config.get", {"key": "any.base_url"})["value"]  # noqa: F821
    return Client(base_url)
