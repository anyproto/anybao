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
    "file.not_available": (
        "the file's bytes have not synced to this device yet (attach "
        "still in flight from the sender) — tell the user and read it "
        "again later; do not retry in a loop"),
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


# --- ADR-017: guest-owned agent stores (brain + chat logs) -------------------
# The writer ensures its store: these declarations live HERE because only
# guest code writes memory/turns/chunks. Host-written stores
# (config/secrets/triggers) are declared in anyrt.
_MEM_MUTABLE = ("salience", "accessCount", "confidence", "importance",
                "context", "body", "tags", "edges", "validTo")
_MEM_CREATE_ONLY = ("fromAgent", "category", "entities", "keywords",
                    "validFrom", "chatId", "source", "provenance")
_MEM_DATASET = {
    "name": "agent_memory_items", "displayName": "Agent Memory",
    "idRule": "auto", "deleteBy": "author",
    "search": {"title": "context", "text": "body", "scope": "agent"},
    "fields": [
        {"key": "category", "kind": "string", "required": True},
        {"key": "context", "kind": "string", "required": True,
         "mutableBy": "author"},
        {"key": "body", "kind": "string", "mutableBy": "author"},
        {"key": "tags", "kind": "array", "mutableBy": "author"},
        {"key": "edges", "kind": "array", "mutableBy": "author"},
        {"key": "confidence", "kind": "number", "mutableBy": "author"},
        {"key": "importance", "kind": "number", "mutableBy": "author"},
        {"key": "salience", "kind": "number", "mutableBy": "author"},
        {"key": "accessCount", "kind": "number", "mutableBy": "author"},
        {"key": "entities", "kind": "array"},
        {"key": "keywords", "kind": "array"},
        {"key": "validFrom", "kind": "datetime"},  # ADR-019 §2
        # ADR-027 §4: set when a newer item supersedes this one — the
        # closed fact stays for audit, recall reads live facts only
        {"key": "validTo", "kind": "datetime", "mutableBy": "author"},
        {"key": "chatId", "kind": "string"},
        {"key": "fromAgent", "kind": "string"},
        {"key": "source", "kind": "string"},
        {"key": "provenance", "kind": "object"},
        {"key": "creator", "stamp": "creator"},
        {"key": "createdAt", "stamp": "createTime"},
        {"key": "modifiedAt", "stamp": "modifyTime"},
    ],
}
_JOB_STATE_DATASET = {
    # free-form cursor records (lastSeq / lastModifiedAt / …), one per
    # job id — the dynamic keyspace is the contract, like triggers
    "name": "agent_job_state", "displayName": "Agent Job State",
    "idRule": "user", "deleteBy": "anyone", "skipHistory": True,
    "dynamic": True, "fields": [],
}
_ROI_DATASET = {
    # auto-recall injection log (autorecall@v1 §1b/§5 ROI metrics) —
    # free-form records keyed "<itemId>:<ts>", harness-owned shapes
    "name": "agent_roi_injections", "displayName": "Agent ROI Injections",
    "idRule": "user", "deleteBy": "anyone", "skipHistory": True,
    "dynamic": True, "fields": [],
}
_TURNS_DATASET = {
    "name": "agent_turns", "displayName": "Agent Turns",
    "idRule": "user", "deleteBy": "author", "skipHistory": True,
    "search": {"title": "userText", "text": "searchText",
               "scope": "history"},
    "fields": [
        {"key": "seq", "kind": "number"},
        {"key": "fromAgent", "kind": "string"},
        {"key": "userName", "kind": "string"},
        {"key": "userText", "kind": "string"},
        {"key": "think", "kind": "string"},
        {"key": "replies", "kind": "array"},
        {"key": "effects", "kind": "array"},
        {"key": "messageIds", "kind": "array"},
        {"key": "traceRef", "kind": "string"},
        {"key": "interrupted", "kind": "boolean"},
        {"key": "llm", "kind": "object"},
        {"key": "searchText", "kind": "string"},
        {"key": "creator", "stamp": "creator"},
        {"key": "createdAt", "stamp": "createTime"},
    ],
}
_CHUNKS_DATASET = {
    "name": "agent_chunks", "displayName": "Agent Chunks",
    "idRule": "user", "deleteBy": "author", "skipHistory": True,
    "search": {"text": "summary", "scope": "history"},
    "fields": [
        {"key": "seq", "kind": "number"},
        {"key": "level", "kind": "number"},
        {"key": "fromAgent", "kind": "string"},
        {"key": "summary", "kind": "string"},
        {"key": "periodStart", "kind": "datetime"},  # ADR-019 §2
        {"key": "periodEnd", "kind": "datetime"},
        {"key": "fromSeq", "kind": "number"},
        {"key": "toSeq", "kind": "number"},
        {"key": "unitsCovered", "kind": "number"},
        {"key": "creator", "stamp": "creator"},
        {"key": "createdAt", "stamp": "createTime"},
    ],
}


# --- ADR-019 §4: instant keys the client guards on ---------------------------
# Server stamps are instants on EVERY dataset; declared datetime fields
# come from the declarations this module owns (+ create_dataset drafts
# seen in this run). A bare number/string literal against one of these
# never errors server-side — it silently matches all or nothing.
_STAMP_KEYS = frozenset({"createdAt", "modifiedAt", "createTime", "modifyTime"})
_TIME_OPS = ("$eq", "$in", "$nin", "$gt", "$gte", "$lt", "$lte")


def _datetime_keys(decl):
    return {f["key"] for f in (decl or {}).get("fields") or []
            if f.get("kind") == "datetime"
            or f.get("stamp") in ("createTime", "modifyTime")}


_DATASET_TIME_KEYS = {d["name"]: _datetime_keys(d) for d in
                      (_MEM_DATASET, _TURNS_DATASET, _CHUNKS_DATASET)}


def _is_instant(v):
    return (isinstance(v, dict) and set(v) == {"$date"}
            and isinstance(v["$date"], (int, float, str))
            and not isinstance(v["$date"], bool))


def _check_time_literal(key, kind, cond):
    """Raise unless every comparable operand under `cond` is an instant.
    `cond` is a bare value or an operator dict; non-comparison
    operators ($exists, $regex, …) are the server's business."""
    def bad(v):
        raise ValueError(
            f'filter key "{key}" is {kind}: compare it to an instant '
            f'{{"$date": …}} — instant(seconds) / instant("<ISO>") — not '
            f'{json.dumps(v)[:60]}; a bare literal silently matches every '
            f"row ($gte/$lt) or none ($eq/$in) (ADR-019 §4)")
    if not isinstance(cond, dict) or _is_instant(cond):
        if cond is not None and not _is_instant(cond):
            bad(cond)
        return
    for op, v in cond.items():
        if op in ("$in", "$nin"):
            if not isinstance(v, list) or not all(_is_instant(x) for x in v):
                bad(v)
        elif op in _TIME_OPS and not _is_instant(v):
            bad(v)
        elif op == "$not":
            _check_time_literal(key, kind, v)


def _guard_filter(filt, time_keys, kind_of=None):
    """Walk a filter (through $and/$or/$nor) checking instant keys.
    `time_keys`: set of keys that are instants; `kind_of(key)` may
    add more (e.g. catalog-resolved datetime properties)."""
    if not isinstance(filt, dict):
        return
    for k, v in filt.items():
        if k in ("$and", "$or", "$nor") and isinstance(v, list):
            for sub in v:
                _guard_filter(sub, time_keys, kind_of)
        elif k in time_keys or (kind_of and kind_of(k)):
            _check_time_literal(k, "an instant (datetime)", v)


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
# never belongs in model context (ADR-010 §8). No chat id rides a
# space row: the general chat is the `general-chat/v1` bundle's root
# (`general_chat(space)`), and the agent stores are `bao/v1` bundle
# children (`bundle_child`) — ADR-006 §0, ADR-017 §0.
_SPACE_ROW_FIELDS = ("id", "name", "description", "status", "ownRole",
                     "spaceType", "createdAt")


def _trim_space_row(r):
    return {k: r[k] for k in _SPACE_ROW_FIELDS
            if r.get(k) not in (None, "", {})}


# Builtin type namespaces whose group + property keys are already literal
# handles (`any.name`, `any.types`, `nav.parentId`). They are never
# reverse-mapped on read nor xKey-resolved on write — see the xKey
# normalization contract in ADR-006 §6. `program` and `mini_app` are
# harness-declared USER types (ADR-010 §5, ADR-008 §6): resolved by xKey
# like any other.
_RESERVED_GROUPS = {"any", "nav", "_ver"}

# Synthetic catalog rows: listed by GET /types in every space but not
# attachable — no object carries them, and the meta-type's only
# property (`type.xkey`) is writable solely on type rows.
_SYNTHETIC_TYPES = {"any", "spaceIndex", "type"}

# --- ADR-022: property handles, formats, options -----------------------------
# any-ui stamps `xKey` as a KIND MARKER (`select` on every Select, `tags`
# on every Multiselect, …) and sends none for Text/Number/Check — so an
# xKey is a handle only when it is unique on its type and not a marker;
# otherwise the property resolves by name (ADR-022 §1). Bridge: drop
# once any-ui carries the marker in `xKind`.
_MARKER_XKEYS = frozenset({
    "select", "tags", "links", "relation", "date", "datetime", "url",
    "email", "longtext", "multiselect", "checkbox", "number", "text"})
_FORMATS = ("select", "multiselect", "links", "date", "datetime")
# Client conventions with NO server format (ADR-022 §1): a `kind:
# string` property whose `xKind` says how clients render/edit it —
# any-ui's STRING_XKINDS (api-core types.ts). Accepted here as
# `format.type` sugar and lowered to the marker; the wire never sees
# a format for them (the server would 400).
_XKIND_MARKERS = ("url", "email", "longtext")
# The marker any-ui stamps beside each server format, so a property
# the agent declares reads in the UI's picker/icons exactly like one
# the UI made (`datetime` shares `date` — the UI has one date kind).
_XKIND_OF_FORMAT = {"select": "select", "multiselect": "tags", "links": "links",
                    "date": "date", "datetime": "date"}
_KINDS = ("string", "number", "boolean", "null", "array", "object",
          "datetime")
# any-ui's option palette (optionPalette.ts) — colors are free strings on
# the wire; these are the ten the UI renders as swatches.
_OPTION_COLORS = ("grey", "yellow", "orange", "red", "pink", "purple",
                  "blue", "ice", "teal", "green")
# definition paths pinned at first write (property-lifecycle §2) —
# a PATCH on them is 400 property.immutable; refused client-side
_PINNED_PATHS = ("id", "key", "kind", "scope", "items", "properties",
                 "format", "format.type")
_ARCHIVED_META = "anyUiArchived"   # any-ui's removal marker (meta.<k>)
_LEXID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_UNSET = object()   # encoder sentinel: value None ⇒ $unset


def _lexid_after(pos):
    """A byte-compare-greater position after `pos` ("" ⇒ first)."""
    if not pos:
        return "a0"
    last = pos[-1]
    i = _LEXID_ALPHABET.find(last)
    if 0 <= i < len(_LEXID_ALPHABET) - 1:
        return pos[:-1] + _LEXID_ALPHABET[i + 1]
    return pos + "1"


def _assign_handles(props):
    """Stamp `handle` on every property row (ADR-022 §1): the xKey when
    unique on the type and not a kind marker, else the name; a true
    duplicate is suffixed with its id so no two handles collide."""
    xk_count = {}
    for p in props:
        xk = p.get("xKey")
        if xk:
            xk_count[xk] = xk_count.get(xk, 0) + 1
    for p in props:
        xk = p.get("xKey")
        if xk and xk not in _MARKER_XKEYS and xk_count[xk] == 1:
            p["handle"] = xk
        else:
            p["handle"] = p.get("name") or p.get("id")
    h_count = {}
    for p in props:
        h_count[p["handle"]] = h_count.get(p["handle"], 0) + 1
    for p in props:
        if h_count[p["handle"]] > 1:
            p["handle"] = f'{p["handle"]}~{p.get("id")}'
    return props


def _is_archived(p):
    return str((p.get("meta") or {}).get(_ARCHIVED_META) or "") == "1"


def _options_of(pdef):
    return ((pdef.get("format") or {}).get("options") or {})


def _ordered_options(pdef):
    """[{key, name, color, pos}] by pos (byte compare), then key."""
    opts = _options_of(pdef)
    rows = [{"key": k, "name": (v or {}).get("name") or k,
             "color": (v or {}).get("color") or "grey",
             "pos": (v or {}).get("pos") or ""} for k, v in opts.items()]
    rows.sort(key=lambda r: (r["pos"] == "", r["pos"], r["key"]))
    return rows


def _prop_sort_key(p):
    pos = (p.get("meta") or {}).get("pos") or ""
    return (pos == "", pos, (p.get("name") or "").casefold(), p.get("id") or "")


def _link_object_id(v):
    """The object id inside any accepted link spelling, or None: a bare
    id, `any://<id>`, legacy `any://<sid>/<id>`, typed `any://o/<sid>/
    <id>`, or a row/stub dict carrying `id`."""
    if isinstance(v, dict):
        return v.get("id") or v.get("objectId")
    if not isinstance(v, str):
        return None
    if v.startswith("any://"):
        segs = [s for s in v[6:].split("#")[0].split("/") if s]
        if len(segs) == 1:
            return segs[0]
        if len(segs) == 2:
            return segs[1]
        if len(segs) >= 3 and segs[0] == "o":
            return segs[2]
        return None
    return v if _looks_like_object_id(v) else None


def _looks_like_object_id(s):
    return (isinstance(s, str) and len(s) >= 40 and " " not in s
            and s.startswith("bafy"))


def _slugify_option_key(name):
    return _slugify_xkey(name).replace(".", "_") or "option"


def _pick_color(key):
    # deterministic (replay-safe) stand-in for any-ui's random pick
    return _OPTION_COLORS[sum(ord(ch) for ch in key) % len(_OPTION_COLORS)]


class _Client:
    def __init__(self, base_url):
        self._base = base_url.rstrip("/")
        # Per-space type/property catalog, memoized for the client's lifetime
        # (one cell). Resolves xKey<->id both ways so the agent reads/writes
        # types and properties by their stable xKey slug, never raw content
        # ids — ADR-006 §6. Invalidated after create_type / add_property.
        self._types_cache = {}   # space -> {"by_id", "by_xkey", "rows"}
        self._props_cache = {}   # (space, type_id) -> [prop rows]
        # dataset name -> instant keys (ADR-019 §4); drafts add to it
        self._dataset_time_keys = {k: set(v) for k, v in _DATASET_TIME_KEYS.items()}
        self._spaces_cache = None   # space rows for name resolution (§8)
        self._bundle_children = {}  # (space, bundleId, seed) -> objectId
        self._ensured_stores = set()  # (space, xKey) lazily provisioned
        self._stub_cache = {}   # space -> {objectId: {id, name, types}} (ADR-022 §3)

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
    @staticmethod
    def _check_space(space):
        # a non-string space (a list_spaces() row or the whole list)
        # otherwise surfaces frames deep as an unhashable-key TypeError
        if not isinstance(space, str) or not space:
            raise TypeError(
                f"space must be a space id string, got {type(space).__name__}"
                " — pick one id from list_spaces()")

    def _catalog(self, space):
        self._check_space(space)
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
        self._check_space(space)
        key = (space, type_id)
        props = self._props_cache.get(key)
        if props is None:
            props = _assign_handles([dict(p) for p in
                                     self._fetch_props(space, type_id)
                                     if isinstance(p, dict)])
            self._props_cache[key] = props
        return props

    def _prop_def(self, space, type_id, prop_id):
        return next((p for p in self._type_props(space, type_id)
                     if p.get("id") == prop_id), None)

    def _cat_invalidate(self, space):
        self._types_cache.pop(space, None)
        for k in [k for k in self._props_cache if k[0] == space]:
            self._props_cache.pop(k, None)

    @staticmethod
    def _is_user_type(row):
        # Builtins report xKey == id (chat, editor, nav, any); user types
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
        """A prop id, handle, name, or xKey under type_id -> prop id (or
        None). Tiers are tried in that order; a tier matching MORE than
        one property errors listing the candidates — never first-match
        (any-ui's marker xKeys make `select` name several, ADR-022 §1)."""
        props = self._type_props(space, type_id)
        for field in ("id", "handle", "name", "xKey"):
            hits = [p for p in props if seg and p.get(field) == seg]
            if len(hits) == 1:
                return hits[0].get("id")
            if len(hits) > 1:
                cands = ", ".join(
                    f'"{p["handle"]}" ({p.get("name")}, id {p.get("id")})'
                    for p in hits)
                raise ValueError(
                    f'property key "{seg}" is ambiguous on this type — it '
                    f"matches {len(hits)} properties by {field}: {cands}. "
                    "Use the handle.")
        if not _retried:
            self._cat_invalidate(space)
            return self._resolve_prop_seg(space, type_id, seg, True)
        return None

    def _prop_handles(self, space, type_id):
        return ", ".join(f'"{p["handle"]}"' for p in
                         self._type_props(space, type_id))

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

    def _resolve_prop_groups(self, space, groups, ctx=None):
        """Nested write groups {typeXKey: {propXKey: val}} -> the id-keyed
        shape the server writes by {typeId: {propId: val}}. Reserved builtin
        namespaces (any/nav) pass through with literal prop keys.
        Unknown type/property keys ERROR — never silently dropped (a
        misplaced key once lost a whole batch of writes). With a write
        `ctx` (ADR-022 §2) every value is encoded against its
        definition; None values are collected as unsets."""
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
                    raise ValueError(
                        f'unknown property "{pk}" on type "{gk}". Its '
                        f"properties: {self._prop_handles(space, tid)}")
                pdef = self._prop_def(space, tid, pid) or {}
                if ctx is None:
                    resolved[pid] = pv
                    continue
                wire = self._encode_value(space, tid, pdef, pv, ctx)
                if wire is _UNSET:
                    ctx["unsets"].append((tid, pdef))
                    continue
                if wire != pv:
                    ctx["resolved"][f'{gk}.{pdef.get("handle") or pk}'] = wire
                if _is_archived(pdef):
                    ctx["warnings"].append(
                        f'property "{pdef.get("handle")}" on "{gk}" is '
                        "archived in the UI (meta.anyUiArchived) — written "
                        "anyway")
                resolved[pid] = wire
            out[tid] = {**out.get(tid, {}), **resolved}
        return out

    # --- ADR-022 §2: definition-aware value encoding -------------------------
    @staticmethod
    def _write_ctx(create_options=True):
        return {"create_options": create_options, "resolved": {},
                "createdOptions": [], "warnings": [], "unsets": [],
                "option_patches": {}}

    def _encode_value(self, space, tid, pdef, value, ctx):
        """The wire value for `value` against its definition — option
        names → keys (minting missing options), object names/URIs →
        `any://<id>`, dates → instants, scalars kind-checked; None ⇒
        _UNSET. Raises ValueError naming the expected shape."""
        fmt = (pdef.get("format") or {}).get("type")
        kind = pdef.get("kind")
        handle = pdef.get("handle") or pdef.get("name") or pdef.get("id")
        if value is None:
            return _UNSET
        if fmt == "select":
            if isinstance(value, list):
                if len(value) != 1:
                    raise ValueError(
                        f'"{handle}" is a single select — pass one option, '
                        f"not {len(value)}")
                value = value[0]
            return self._option_key(space, tid, pdef, value, ctx)
        if fmt == "multiselect":
            vals = value if isinstance(value, list) else [value]
            out = []
            for v in vals:
                k = self._option_key(space, tid, pdef, v, ctx)
                if k not in out:
                    out.append(k)
            return out
        if fmt == "links":
            vals = value if isinstance(value, list) else [value]
            out, explicit = [], []
            for v in vals:
                oid = _link_object_id(v)
                if oid is None:
                    if not isinstance(v, str) or not v.strip():
                        raise ValueError(
                            f'"{handle}" is a links property — pass object '
                            f"ids, any:// links, or object names, not "
                            f"{json.dumps(v)[:60]}")
                    oid = self._object_id_by_name(space, pdef, v.strip())
                elif oid not in explicit:
                    explicit.append(oid)
                uri = "any://" + oid
                if uri not in out:
                    out.append(uri)
            # a links value is the bare in-space id (no space segment):
            # an id from another space is unresolvable by every reader,
            # so an explicit id must exist HERE — verified before any
            # write, with the same batched lookup hydration uses
            self._assert_links_in_space(space, handle, explicit)
            return out
        if fmt in ("date", "datetime"):
            return self._encode_instant(handle, fmt, kind, value)
        return self._encode_kind(handle, kind, value)

    def _assert_links_in_space(self, space, handle, ids):
        """Every explicit link id must be an object of `space` (links
        are in-space references). One `$in` query for the ids not
        already known to the stub cache; a miss raises before any
        write. Names never reach here — they resolved in-space."""
        stubs = self._stub_cache.setdefault(space, {})
        want = [i for i in ids if i not in stubs]
        if want:
            rows = self._call(
                "post", f"/v1/spaces/{space}/objects/query",
                {"filter": {"id": {"$in": want[:200]}},
                 "limit": 200}).get("records") or []
            for r in rows:
                if isinstance(r, dict) and r.get("id"):
                    anyg = r.get("any") or {}
                    stubs[r["id"]] = {
                        "id": r["id"], "name": anyg.get("name"),
                        "types": self._dexify(space, anyg.get("types") or [])}
        missing = [i for i in ids if i not in stubs]
        if missing:
            raise ValueError(
                f'"{handle}": object {missing[0]} is not in this space — '
                "links properties are in-space references; for an object "
                "in another space put a typed link in the body instead: "
                "[Name](any://o/<spaceId>/<objectId>)")

    def _option_key(self, space, tid, pdef, value, ctx):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f'"{pdef.get("handle")}" takes an option name or key '
                f"(string), not {json.dumps(value)[:60]}")
        value = value.strip()
        opts = _options_of(pdef)
        pending = ctx["option_patches"].get((tid, pdef.get("id")), {})
        if value in opts or value in pending:
            return value
        by_name = [k for k, o in list(opts.items()) + list(pending.items())
                   if (o or {}).get("name") == value]
        if not by_name:
            by_name = [k for k, o in list(opts.items()) + list(pending.items())
                       if ((o or {}).get("name") or "").casefold()
                       == value.casefold()]
        if len(by_name) == 1:
            return by_name[0]
        if len(by_name) > 1:
            raise ValueError(
                f'option "{value}" on "{pdef.get("handle")}" is ambiguous: '
                f"keys {by_name} — pass the key")
        if not ctx["create_options"]:
            names = ", ".join(f'"{o["name"]}"' for o in _ordered_options(pdef))
            raise ValueError(
                f'"{value}" is not an option of "{pdef.get("handle")}" '
                f"(create_options=False). Options: {names or 'none'}")
        base = _slugify_option_key(value)
        key, n = base, 1
        while key in opts or key in pending:
            n += 1
            key = f"{base}_{n}"
        last = max([o["pos"] for o in _ordered_options(pdef) if o["pos"]]
                   + [o["pos"] for o in pending.values()] or [""])
        pending[key] = {"name": value, "color": _pick_color(key),
                        "pos": _lexid_after(last)}
        ctx["option_patches"][(tid, pdef.get("id"))] = pending
        ctx["createdOptions"].append(
            {"property": pdef.get("handle"), "key": key, "name": value,
             "color": pending[key]["color"]})
        return key

    def _object_id_by_name(self, space, pdef, name):
        """Exact `any.name` match (then casefold-unique) within the links
        prop's `format.filter`; 0 or >1 hits error with candidates."""
        cand_filter = (pdef.get("format") or {}).get("filter")
        filt = {"any.name": name}
        if isinstance(cand_filter, dict) and cand_filter:
            filt = {"$and": [cand_filter, filt]}
        rows = self._call("post", f"/v1/spaces/{space}/objects/query",
                          {"filter": filt, "limit": 5}).get("records") or []
        rows = [r for r in rows if isinstance(r, dict)]
        if len(rows) == 1:
            return rows[0]["id"]
        if not rows:
            raise ValueError(
                f'no object named "{name}" for links property '
                f'"{pdef.get("handle")}" — search(space, "{name}") for '
                "the id, or create the object first (links never mint)")
        cands = "; ".join(
            f'{r["id"]} ({", ".join(self._dexify(space, (r.get("any") or {}).get("types") or []))})'
            for r in rows)
        raise ValueError(
            f'"{name}" names {len(rows)} objects — pass an id: {cands}')

    @staticmethod
    def _encode_instant(handle, fmt, kind, value):
        if _is_instant(value):
            secs = ts_s(value)  # noqa: F821 - guest global
        elif isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(
                f'"{handle}" is a {fmt}: pass instant(seconds) / an ISO '
                f"string / epoch seconds, not {json.dumps(value)[:60]}")
        else:
            try:
                secs = ts_s(instant(value))  # noqa: F821 - guest globals
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f'"{handle}" is a {fmt}: {value!r} is not a date '
                    f"({e})") from None
        if secs is None:
            raise ValueError(f'"{handle}": unreadable instant {value!r}')
        if fmt == "date":
            secs = secs - (secs % 86400)
        if kind == "string":   # legacy ISO-string convention
            import time
            t = time.gmtime(secs)
            return (time.strftime("%Y-%m-%d", t) if fmt == "date"
                    else time.strftime("%Y-%m-%dT%H:%M:%SZ", t))
        return instant(secs)  # noqa: F821 - guest global

    @staticmethod
    def _encode_kind(handle, kind, value):
        def bad(expect):
            raise ValueError(
                f'"{handle}" is kind {kind}: expected {expect}, got '
                f"{json.dumps(value)[:60]}")
        if kind == "number":
            if isinstance(value, bool):
                bad("a number")
            if isinstance(value, (int, float)):
                return value
            if isinstance(value, str):
                try:
                    f = float(value)
                except ValueError:
                    bad("a number")
                return int(f) if f.is_integer() and "." not in value else f
            bad("a number")
        if kind == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in (
                    "true", "false", "yes", "no"):
                return value.strip().lower() in ("true", "yes")
            bad("true/false")
        if kind == "string":
            if isinstance(value, str):
                return value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return str(value)
            bad("a string")
        if kind == "array":
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                bad("a list")
            return [value]
        if kind == "object":
            if isinstance(value, dict):
                return value
            bad("an object")
        return value

    def _apply_option_patches(self, space, ctx):
        for (tid, pid), pending in ctx["option_patches"].items():
            sets = {}
            for key, o in pending.items():
                for leaf in ("name", "color", "pos"):
                    sets[f"format.options.{key}.{leaf}"] = o[leaf]
            self._call("patch",
                       f"/v1/spaces/{space}/types/{tid}/properties/{pid}",
                       {"set": sets})
            self._props_cache.pop((space, tid), None)
        ctx["option_patches"] = {}

    def _apply_unsets(self, space, object_id, ctx):
        for tid, pdef in ctx["unsets"]:
            scope = pdef.get("scope") or "synced"
            if scope != "synced":
                raise ValueError(
                    f'"{pdef.get("handle")}" is scope {scope} — the server '
                    "has no unset route for non-synced properties; write "
                    "an empty value instead")
            self._call("post", f"/v1/spaces/{space}/modify", {
                "objectId": object_id, "dataset": "objects",
                "records": [{"id": object_id, "upsert": False, "ops": [
                    {"type": "$unset", "path": f'{tid}.{pdef.get("id")}'}]}]})
        ctx["unsets"] = []

    @staticmethod
    def _write_result(object_id, ctx):
        out = {"objectId": object_id}
        for k in ("resolved", "createdOptions", "warnings"):
            if ctx[k]:
                out[k] = ctx[k]
        return out

    def _resolve_path(self, space, path):
        """A readable dotted filter/sort key "typeXKey.propXKey" -> the
        server's "typeId.propId". Keys whose head is a builtin (any.*,
        nav.*, bare `id`) pass through unchanged. An
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

    def _prop_kind(self, space, resolved_key):
        """Kind of a RESOLVED "typeId.propId" key (None when unknown)."""
        if not isinstance(resolved_key, str) or "." not in resolved_key:
            return None
        tid, _, pid = resolved_key.partition(".")
        if tid in _RESERVED_GROUPS or tid == "_ver":
            return None
        return next((p.get("kind") for p in self._type_props(space, tid)
                     if p.get("id") == pid), None)

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
            if k in ("$and", "$or", "$nor") and isinstance(v, list):
                out[k] = [self._resolve_filter(space, sub) for sub in v]
                continue
            rk = self._resolve_path(space, k)
            if k == "any.types":
                v = self._resolve_type_value(space, v)
            else:
                v = self._encode_filter_value(space, rk, v)
            out[rk] = v
        return out

    def _encode_filter_value(self, space, resolved_key, cond):
        """Display forms in filter VALUES → wire (ADR-022 §3): option
        names → keys, object names/links → `any://<id>`. Never mints.
        Only comparison operands are touched ($exists/$regex/… pass)."""
        if not isinstance(resolved_key, str) or "." not in resolved_key:
            return cond
        tid, _, pid = resolved_key.partition(".")
        if tid in _RESERVED_GROUPS or tid == "_ver":
            return cond
        pdef = self._prop_def(space, tid, pid)
        fmt = ((pdef or {}).get("format") or {}).get("type")
        if fmt not in ("select", "multiselect", "links"):
            return cond
        ctx = self._write_ctx(create_options=False)

        def one(x):
            if fmt == "links":
                oid = _link_object_id(x)
                if oid is None and isinstance(x, str) and x.strip():
                    oid = self._object_id_by_name(space, pdef, x.strip())
                return "any://" + oid if oid else x
            return self._option_key(space, tid, pdef, x, ctx) \
                if isinstance(x, str) else x
        if isinstance(cond, dict):
            out = {}
            for op, v in cond.items():
                if op in ("$in", "$nin", "$all") and isinstance(v, list):
                    out[op] = [one(x) for x in v]
                elif op in ("$eq", "$ne"):
                    out[op] = one(v)
                else:
                    out[op] = v
            return out
        if isinstance(cond, list):
            return [one(x) for x in cond]
        return one(cond)

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
            defs = {p["id"]: p for p in self._type_props(space, k)
                    if p.get("id")}
            grp = {}
            for pk, pv in v.items():
                pdef = defs.get(pk)
                if pdef is None:
                    grp[pk] = pv
                    continue
                grp[pdef["handle"]] = self._display_value(pdef, pv)
            out[row.get("xKey") or k] = grp
        # `any.types` VALUES are type ids on the wire — speak xKeys
        # (builtins already are; unknown ids pass through)
        any_group = out.get("any")
        if isinstance(any_group, dict) and isinstance(any_group.get("types"), list):
            out["any"] = {**any_group, "types": [
                ((cat["by_id"].get(t) or {}).get("xKey") or t)
                for t in any_group["types"]]}
        return out

    # --- ADR-022 §3: hydrated reads -------------------------------------------
    @staticmethod
    def _display_value(pdef, v):
        """Select keys → option names (a dangling key passes through);
        links stay `any://` strings here and are resolved in bulk by
        _hydrate_links. Instants stay instants (ADR-019)."""
        fmt = (pdef.get("format") or {}).get("type")
        if fmt not in ("select", "multiselect"):
            return v
        opts = _options_of(pdef)

        def name(k):
            o = opts.get(k) if isinstance(k, str) else None
            return (o or {}).get("name") or k
        if fmt == "select":
            return name(v)
        return [name(k) for k in v] if isinstance(v, list) else v

    def _hydrate_links(self, space, recs):
        """Replace links-format values in normalized records with
        [{id, name, types}] stubs — ONE batched $in query per page
        (cap 200 ids; the tail stays raw), memoized per client."""
        cat = self._catalog(space)
        slots = []   # (group dict, key, [uris])
        for rec in recs:
            if not isinstance(rec, dict):
                continue
            for gk, gv in rec.items():
                tid = cat["by_xkey"].get(gk)
                if not tid or not isinstance(gv, dict):
                    continue
                for p in self._type_props(space, tid):
                    if ((p.get("format") or {}).get("type") == "links"
                            and isinstance(gv.get(p["handle"]), list)):
                        slots.append((gv, p["handle"], gv[p["handle"]]))
        want = []
        for _, _, uris in slots:
            for u in uris:
                oid = _link_object_id(u) if isinstance(u, str) else None
                if oid and oid not in self._stub_cache.get(space, {}) \
                        and oid not in want:
                    want.append(oid)
        stubs = self._stub_cache.setdefault(space, {})
        if want:
            rows = self._call(
                "post", f"/v1/spaces/{space}/objects/query",
                {"filter": {"id": {"$in": want[:200]}},
                 "limit": 200}).get("records") or []
            for r in rows:
                if isinstance(r, dict) and r.get("id"):
                    anyg = r.get("any") or {}
                    stubs[r["id"]] = {
                        "id": r["id"], "name": anyg.get("name"),
                        "types": self._dexify(space, anyg.get("types") or [])}
            for oid in want[:200]:
                stubs.setdefault(oid, {"id": oid, "name": None, "types": []})
        for grp, key, uris in slots:
            out = []
            for u in uris:
                oid = _link_object_id(u) if isinstance(u, str) else None
                out.append(stubs.get(oid, u) if oid else u)
            grp[key] = out

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
    def create_object(self, space, body, create_options=True):
        """Create a typed object; returns {"objectId", "resolved"?,
        "createdOptions"?, "warnings"?}.

        Top-level `name` / `description` route into the `any` group and
        `markdown` (alias `body`) becomes the editor body — one call
        creates the page (parity with update_object; the wire itself
        takes no body, so it is a create + put_markdown). Everything
        else: `types` entries
        and `initialProperties` group + property keys are given as
        handles (or ids) and resolved to the content-ids the server
        writes by; reserved groups (any/nav) pass through literal.
        Unknown type/property keys — and unknown TOP-LEVEL keys, which
        the wire would silently drop — error — ADR-006 §6.
        VALUES are encoded against each property's definition
        (ADR-022 §2): select/multiselect take option NAMES or keys — a
        missing option is created (any-ui style key/color/pos; pass
        `create_options=False` to refuse) and reported in
        `createdOptions`; links take object names, ids or any://
        links (a name must match exactly one object; links never
        create objects); date/datetime take instant()/ISO/epoch and
        land as instants (dates at midnight UTC); number/boolean/
        string are kind-checked. `resolved` echoes every value that
        changed on the way to the wire."""
        body = dict(body or {})
        unknown = set(body) - {"types", "initialProperties", "nav",
                               "name", "description", "markdown", "body"}
        if unknown:
            raise ValueError(
                f"create_object: unknown top-level key(s) {sorted(unknown)} "
                "would be dropped by the wire (it accepts types/"
                "initialProperties/nav). Properties go in initialProperties "
                'keyed by type xKey — {"any": {"name": ...}} — or pass '
                "name/description/markdown at top level.")
        markdown = body.pop("markdown", None)
        body_md = body.pop("body", None)
        if markdown is None:
            markdown = body_md
        name = body.pop("name", None)
        description = body.pop("description", None)
        if name is not None or description is not None:
            grp = body.setdefault("initialProperties", {}).setdefault("any", {})
            if name is not None:
                grp.setdefault("name", name)
            if description is not None:
                grp.setdefault("description", description)
        if isinstance(body.get("types"), list):
            bad = [t for t in body["types"] if t in _SYNTHETIC_TYPES]
            if bad:
                raise ValueError(
                    f"type(s) {bad} are synthetic catalog rows "
                    "(any/spaceIndex/type) — they describe the space and "
                    "are not attachable to objects. Use a user type, or "
                    "omit types for a plain object.")
            body["types"] = [self._resolve_type_or_raise(space, t)
                             for t in body["types"]]
        ctx = self._write_ctx(create_options)
        if isinstance(body.get("initialProperties"), dict):
            body["initialProperties"] = self._resolve_prop_groups(
                space, body["initialProperties"], ctx)
            # None at create = simply absent
            for g in list(body["initialProperties"]):
                if not body["initialProperties"][g]:
                    del body["initialProperties"][g]
        self._apply_option_patches(space, ctx)   # options before the value
        res = self._call("post", f"/v1/spaces/{space}/objects", body)
        object_id = res.get("objectId")
        if markdown is not None and object_id:
            self.put_markdown(space, object_id, markdown)
        return self._write_result(object_id, ctx)

    def update_object(self, space, object_id, body, create_options=True):
        """Update an object's name / editor body / properties by handle.

        `body`: {"name"?, "description"?, "markdown"?/"body"?,
        "<typeXKey>": {prop: value}, …} — same nested type-group shape as
        create_object (top-level name/description route into the `any`
        group, parity with create_object). Property keys resolve to ids;
        groups are resolved and every value encoded against its
        definition BEFORE any write so a bad key or value can't land a
        partial update (value rules: see create_object — option names,
        object names, dates; `None` CLEARS a property via $unset).
        Patches go one per (type, scope). Returns {"objectId",
        "resolved"?, "createdOptions"?, "warnings"?}."""
        body = dict(body or {})
        markdown = body.pop("markdown", None)
        body_md = body.pop("body", None)
        if markdown is None:
            markdown = body_md
        name = body.pop("name", None)
        description = body.pop("description", None)
        ctx = self._write_ctx(create_options)
        groups = self._resolve_prop_groups(space, body, ctx)   # raises before write
        if name is not None:
            groups.setdefault("any", {}).setdefault("name", name)
        if description is not None:
            groups.setdefault("any", {}).setdefault("description", description)
        if markdown is not None:
            self.put_markdown(space, object_id, markdown)
        self._apply_option_patches(space, ctx)
        self._apply_unsets(space, object_id, ctx)
        for tid, patch in groups.items():
            for scoped in self._split_by_scope(space, tid, patch):
                self._call("post",
                          f"/v1/spaces/{space}/properties/{object_id}/set/{tid}",
                          {"patch": scoped})
        return self._write_result(object_id, ctx)

    def _split_by_scope(self, space, tid, patch):
        """One patch per declared scope — the set route takes a single
        scope per call (data-types § Scopes)."""
        if not patch:
            return []
        if tid in _RESERVED_GROUPS:
            return [patch]
        by_scope = {}
        for pid, v in patch.items():
            pdef = self._prop_def(space, tid, pid) or {}
            by_scope.setdefault(pdef.get("scope") or "synced", {})[pid] = v
        return list(by_scope.values())

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
        content ids (e.g. graph edges) — ADR-006 §6. Time is an
        INSTANT: `createdAt`/`modifiedAt` and every `date`/`datetime`
        property read as `{"$date": "<RFC 3339>"}` (`ts_s(v)` → seconds)
        and a filter compares them only to `instant(seconds)` — a bare
        number raises here (server-side it would silently match every
        row) — ADR-019."""
        unknown = set(opts) - {"filter", "sort", "limit", "offset"}
        if unknown:
            # an unvisited key (e.g. `filters=`) would make the server
            # match EVERY object in the space as if it were intended
            raise ValueError(
                f"query_objects: unknown option(s) {sorted(unknown)}; "
                "the wire accepts filter/sort/limit/offset")
        if "filter" in opts:
            opts["filter"] = self._resolve_filter(space, opts["filter"])
            # ADR-019 §4: resolved keys — stamps are bare, user props
            # "typeId.propId" (kind from the catalog)
            _guard_filter(opts["filter"], _STAMP_KEYS,
                          lambda k: self._prop_kind(space, k) == "datetime")
        if "sort" in opts:
            opts["sort"] = self._resolve_sort(space, opts["sort"])
        recs = self._call("post", f"/v1/spaces/{space}/objects/query",
                          opts).get("records", [])
        if not normalize:
            return recs
        out = [self._normalize_record(space, r) for r in recs]
        self._hydrate_links(space, out)   # ADR-022 §3
        return out

    def list_programs(self, space, tools_only=False):
        """Programs deployed in a space: [{name, version, anyTool, summary}].

        An overlay/repo or your own working space (ADR-009 §2), sorted
        by name — `summary` is the program's one-liner (ADR-010 §4);
        for depth, `use()` it and `help(mod)`. Import one from another
        space with `use("<alias-or-spaceId>:<name>@<version>")`."""
        out = []
        if self._resolve_type_seg(space, "program") is None:
            return []   # no program type = nothing was ever deployed here
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
        optional filter/sort/limit unchecked. Stamps (`createdAt`,
        `modifiedAt`) and datetime fields (`validFrom`, `periodStart`/
        `periodEnd`) are instants `{"$date": …}`: read with `ts_s`,
        filter with `instant(seconds)` — a bare number raises
        (ADR-019 §4)."""
        body = {"objectId": object_id, "dataset": dataset}
        body.update({k: v for k, v in opts.items() if v is not None})
        # ADR-019 §4: stamps + declared datetime fields take instants only
        _guard_filter(body.get("filter"),
                      _STAMP_KEYS | self._dataset_time_keys.get(dataset, set()))
        return self._call("post", f"/v1/spaces/{space}/query", body).get("records", [])

    def modify(self, space, body):
        """Low-level dataset write; prefer upsert_record / create_object.

        `body`: {objectId, dataset, records: [{id, upsert?, ops:
        [{type, path, value}]}]} — for partial ops."""
        return self._call("post", f"/v1/spaces/{space}/modify", body)

    def upsert_record(self, space, object_id, dataset, record_id, value):
        """Write one dataset record (whole-value $set, upsert).

        Writable datasets: server-registered ones (agent_triggers,
        agent_memory_items, …) and runtime datasets declared by their
        owning program (ADR-016) — in both cases the host object must
        carry the dataset's owning type in `any.types`. An UNDECLARED
        dataset name 500s on write and reads as [] — store ad-hoc
        state as object properties instead (seen live 2026-08-12,
        progressbar probe). For bulk ingest into an idRule: user
        runtime dataset prefer upsert_records (idempotent, diffs
        mutable fields server-side)."""
        return self.modify(space, {
            "objectId": object_id, "dataset": dataset,
            "records": [{"id": record_id, "upsert": True,
                         "ops": [{"type": "$set", "path": "", "value": value}]}]})

    def upsert_records(self, space, object_id, dataset, records,
                       page_size=None):
        """Batch-ingest into an `idRule: user` runtime dataset (ADR-016).

        records: [{"id": "<caller id>", "fields": {…}}] — the id is
        the idempotency key: absent ids are created, existing ids get
        only their declared-MUTABLE fields diffed (identical records
        skip), a changed write-once field rejects that record. Field
        keys are the dataset's plain declared keys (never xKeys).
        Returns {created, updated, skipped, rejections: [{index, id,
        code, reason}], pages} — 200 even with rejections, so CHECK
        rejections. One CRDT change per page (page_size default 500).
        The owning program declares the dataset; put the owning
        type on the host object at create ({"types": [...]})."""
        body = {"objectId": object_id, "dataset": dataset,
                "records": records}
        if page_size:
            body["pageSize"] = int(page_size)
        return self._call("post", f"/v1/spaces/{space}/upsert", body)

    def delete_records(self, space, object_id, dataset, record_ids):
        """Delete dataset records by id → {versionId, changeId, recordIds}.

        Tombstones: a deleted id is consumed forever (re-upserting it
        rejects with upsert.record_deleted). On deleteBy: author
        datasets non-author deletes are dropped at apply."""
        return self._call("post", f"/v1/spaces/{space}/delete-records",
                          {"objectId": object_id, "dataset": dataset,
                           "recordIds": list(record_ids)})

    # --- processes (server registry over the event bus; ADR-014 §2) ----------
    def list_processes(self):
        """Live process view → [{identity, self, id, kind, title, scope,
        spaceId?, target?, state, done, total?, message?, error?,
        startedAt, updatedAt}].

        Account-level (no space): the server's last-event-wins registry
        of long-running work — bao jobs (kind "agent", published via
        agent:progress@v1) AND the server's own index.* producers
        (embed drain, fts pass, model download — the answer to "why is
        search incomplete right now"). Nothing is persisted: running
        rows expire 45s after their last heartbeat, terminal rows
        (done/failed/cancelled) linger 60s then vanish. A server
        without the facility 404s (request.not_found)."""
        return self._call("get", "/v1/processes").get("processes") or []

    def cancel_process(self, process_id, identity=None):
        """Ask a process's owner to stop → {subscribers}.

        Emits process.cancel at the owner, who reacts and finishes —
        never a state change by itself (the row stays until the owner's
        terminal event or expiry). 409 process.ambiguous means several
        identities share the id — pass `identity` to pick one. The
        index.* producers ignore cancels by design."""
        body = {"identity": identity} if identity else {}
        return self._call("post", f"/v1/processes/{process_id}/cancel", body)

    def list_devices(self):
        """The account's device registry (ADR-015) → {self, active,
        devices}.

        `self` = this server's peerId; `active` = {appSlug: peerId},
        the server-computed winner per app ("bao" is the agent; a
        client app registers under its own slug); `devices` = [{peerId,
        name, os, version, apps: {slug: presence}, activeClaims:
        {slug: {seq, at}}}] — every device that has registered, live
        or not (presence is the app's last heartbeat under `apps`).
        Read-only: the winner rule is server-side and a switch is
        MANUAL — the user activates bao on the device they want (the
        standby serves notice within one 10 s poll). Use it to tell
        the user which device answers right now, which others exist,
        and where to switch — never to claim from here. A server
        without the registry 404s (request.not_found)."""
        return self._call("get", "/v1/devices")

    def _process_register(self, body):
        # progress@v1 plumbing (ADR-014 §1: programs report progress
        # ONLY through agent:progress@v1, never these three directly)
        return self._call("post", "/v1/processes", body)

    def _process_progress(self, process_id, body):
        return self._call("post", f"/v1/processes/{process_id}/progress", body)

    def _process_finish(self, process_id, body):
        return self._call("post", f"/v1/processes/{process_id}/finish", body)

    def aggregate(self, space, pipeline, object_id=None, dataset=None):
        """Run a Mongo-style aggregation pipeline over the space's objects.

        Stages: $match, $group (_id + accumulators: {"$sum": 1},
        {"$count": {}}), $sort, $count. Field refs take xKeys like
        everywhere else — "$book.rating", "$any.types" — resolved in
        $match/$sort keys and $group refs; unknown ones error with the
        catalog. Avg rating per genre: [{"$group": {"_id":
        "$book.genre", "avg": {"$avg": "$book.rating"}}}]. Returns
        {"records": [...]} with ids mapped back to xKeys; unknown
        stages are a 400 (aggregate.bad_pipeline). With object_id +
        dataset the pipeline runs over that object's dataset records
        instead — field refs are then the dataset's PLAIN keys
        ("$internalDate"), no xKey resolution either way."""
        if object_id or dataset:
            return self._call("post", f"/v1/spaces/{space}/aggregate",
                              {"objectId": object_id, "dataset": dataset,
                               "pipeline": pipeline})
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
        """One space's row → {id, name, description, status, ownRole,
        spaceType, createdAt}; also THE explicit name resolver —
        `get_space("dev")` works.

        No chat id on the row: the space's chat is `general_chat(space)`.
        Sync internals are trimmed like list_spaces (`raw=True` for the
        wire row)."""
        r = self._call("get", f"/v1/spaces/{space}")
        return r if raw else _trim_space_row(r)

    def general_chat(self, space):
        """The space's canonical chat id — the `general-chat/v1`
        bundle's winning root.

        Every space has exactly ONE general chat, registered in the
        bundles registry: the side that creates a space installs it
        (`create_space` does, so does any-ui; the harness ensures the
        bao space's at serve boot). Post there via `chat_send`; READ it
        via `query(space, chat_id, "chat_messages",
        sort=["-createdAt"], limit=n)` — never create a chat object or
        pick one from a query: name-matched "general" chats are
        peer-made impostors that split the conversation.
        404 bundle.not_found = nobody ensured the chat yet (a space
        made outside these paths that no client has opened)."""
        return self.get_bundle(space, "general-chat/v1")["rootId"]

    def create_space(self, name, description=None):
        """Create a new top-level space WITH its general chat → the
        (trimmed) space row + `generalChatId`.

        Space create is a this-side-installs case (ADR-006 §0, same as
        any-ui): right after the POST the `general-chat/v1` bundle is
        ensured with `derived: true`, so `generalChatId` is the chat
        every member and device lands on — write chat there, never
        create chat objects (`general_chat(id)` returns the same id
        later). The space starts empty: resolve/create types against
        it before typed writes (types and xKeys are per-space). Check
        `list_spaces()` first — don't mint a duplicate of an existing
        active space."""
        # no spaceType: empty = server default on every vintage (the
        # literal "anytype.space" is rejected since SDK v0.0.10)
        body = {"name": name}
        if description:
            body["description"] = description
        r = self._call("post", "/v1/spaces", body)
        self._spaces_cache = None    # new space -> refresh the name catalog
        row = _trim_space_row(r)
        # derived install: id is a function of the bundle id, so a
        # fresh owned space needs no locked read and cannot 409
        chat = self.ensure_bundle(row["id"], "general-chat/v1", name="General",
                                  root_types=["chat"], derived=True)
        row["generalChatId"] = chat["bundle"]["rootId"]
        return row

    def open_in_ui(self, space, object_id=None):
        """Open a space — or one object in it — in the user's any-ui
        window on THIS device → {subscribers}.

        Publishes a `ui.open_space` / `ui.open_object` event on the
        device-scope event bus (the transient "show the user what I
        mean" navigation directive — at-most-once, nothing stored).
        Device scope on purpose (user decision 2026-08-19): the view
        changes only on the device this server runs on, never on the
        account's other machines. `subscribers: 0` simply means no UI
        window is connected right now — not an error, nothing is
        queued. Use for "open it / show me" asks; the READ side (where
        the user already is) is the `currentUserSpace` global — the
        view stamped on the message they sent."""
        data = {"spaceId": space, "source": "bao"}
        etype = "ui.open_space"
        if object_id:
            data["objectId"] = object_id
            etype = "ui.open_object"
        return self._call("post", "/v1/events",
                          {"type": etype, "scope": "device", "data": data})

    # --- types & properties (catalog source) ----------------------------------
    def list_types(self, space):
        """Every type in the space: rows of {id, xKey, name, …} (builtins included)."""
        return self._call("get", f"/v1/spaces/{space}/types").get("types", [])

    def list_properties(self, space, type_key, include_archived=False):
        """A type's property definitions, in display order: [{handle,
        id, name, xKey, xKind?, kind, scope, format?, options?, meta?}].

        `handle` is THE key to read/write the property by (the xKey
        when it is a real slug, else the name — any-ui stamps
        `xKey: "select"` on every Select, so xKeys alone collide).
        `format` is the value convention when declared — {"type":
        "date"|"datetime"|"links"|"select"|"multiselect", "ui"?,
        "filter"?, "options"?}; `options` is the ordered
        [{key, name, color}] of a select/multiselect (values store the
        KEY; write by name or key, a new name mints an option).
        `xKind` is the CLIENT convention when there is no format: a
        string prop marked `url` / `email` / `longtext` takes a plain
        string and the UI renders it as a link / textarea.
        A links prop takes object names/ids/any:// links; dates take
        instant()/ISO/epoch; a prop without format is a plain kind
        (write the JSON shape). `scope` is the write/sync class
        (local-scope props exist only per-peer — the chat filter
        trap). Rows the UI archived (meta.anyUiArchived) are hidden
        unless `include_archived=True`. `type_key` is the type's xKey
        (builtins: xKey == id); an unknown key ERRORS with the catalog
        — the server would answer a nonexistent id with a silent []."""
        tid = self._resolve_type_or_raise(space, type_key)
        self._props_cache.pop((space, tid), None)   # a listing reads fresh
        rows = []
        for p in sorted(self._type_props(space, tid), key=_prop_sort_key):
            if _is_archived(p) and not include_archived:
                continue
            row = dict(p)
            if _options_of(p):
                row["options"] = [{k: o[k] for k in ("key", "name", "color")}
                                  for o in _ordered_options(p)]
            if _is_archived(p):
                row["archived"] = True
            rows.append(row)
        return rows

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
        links, select, multiselect) with kind omitted — the server
        derives it. URL / e-mail / long text are the same spelling —
        {"format": {"type": "url" | "email" | "longtext"}} — but are
        client conventions (a string property marked `xKind`), not
        server formats: values are plain strings, the UI renders them
        as links / a textarea. xKeys default to a slug of the name.
        Idempotent: an existing USER type (by xKey, or by name for a
        pre-metatype type listed without one — its handle is re-claimed
        in place) is reused, only MISSING properties are added. A name
        or xKey that collides with a builtin handle (any, spaceIndex,
        type, chat, page, …) ERRORS — builtins cannot be created or
        reshaped. Returns {"typeId", "xKey", "created",
        "addedProps": {xKey: propId}} — reference everything by xKey
        afterwards."""
        body = dict(body or {})
        props = body.pop("properties", None) or []
        xkey = body.get("xKey") or _slugify_xkey(body.get("name") or "")
        rows = self.list_types(space)
        row = next((t for t in rows
                    if t.get("xKey") == xkey or t.get("id") == xkey), None)
        if row is not None and not self._is_user_type(row):
            raise ValueError(
                f'"{xkey}" is the handle of the builtin type '
                f'"{row.get("name")}" — builtins cannot be created or '
                "reshaped. Pick another name, or pass an explicit "
                'non-reserved "xKey".')
        tid = row["id"] if row else None
        if tid is None:
            # a type from before the server's meta-type xkey move (any
            # PR #176) lists with no xKey — re-claim its handle in
            # place (one type.xkey write) instead of duplicating it
            legacy = next(
                (t for t in rows
                 if not t.get("xKey") and self._is_user_type(t)
                 and _slugify_xkey(t.get("name") or "") == xkey), None)
            if legacy is not None:
                self._call(
                    "post",
                    f"/v1/spaces/{space}/properties/{legacy['id']}/set/type",
                    {"patch": {"xkey": xkey}})
                self._cat_invalidate(space)
                tid = legacy["id"]
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
                extra = {k: p[k] for k in ("kind", "meta", "format", "scope",
                                           "description", "xKind") if k in p}
                extra["name"] = p.get("name") or pxkey
                extra["xKey"] = pxkey
                added[pxkey] = self._post_property(space, tid, extra)["propId"]
        self._cat_invalidate(space)   # freshly (re)shaped type -> refresh xKey map
        return {"typeId": tid, "xKey": xkey, "created": created,
                "addedProps": added}

    def list_datasets(self, space, type_key):
        """Runtime dataset definitions on a user type (by xKey) → [defs].

        Each def: {id, name, displayName?, idRule, idPattern?,
        deleteBy, skipHistory?, search?, fields: [{id, key, kind,
        scope, required?, mutableBy, stamp?}], invalid?,
        invalidReason?}. `invalid` marks a declaration that never
        registers or accepts data — remove it (remove_dataset) and
        re-declare."""
        tid = self._resolve_type_or_raise(space, type_key)
        r = self._call("get", f"/v1/spaces/{space}/types/{tid}/datasets")
        return r.get("datasets") or []

    def create_dataset(self, space, type_key, draft):
        """Ensure a runtime dataset schema on a USER type (ADR-016).

        draft: {"name": "<collection>", "displayName"?, "idRule":
        "auto"|"user", "deleteBy": "anyone"|"author", "skipHistory"?,
        "search"?: {"title": "<field key>", "text": "<field key>" |
        ["<field key>", ...], "scope"?: "<index scope slug>"},
        "fields": [{"key", "kind"?: string|number|boolean|array|
        object, "required"?, "mutableBy"?: "author"|"any", "stamp"?:
        "creator"|"createTime"|"modifyTime"}]}. Fields default
        write-once; author gates (mutableBy/deleteBy "author") need a
        {"stamp": "creator"} field; idRule "user" = caller-supplied
        record ids (the upsert idempotency key). search.scope picks
        the index scope the records land under (absent = "basic");
        recall must query that scope to see them. search.text may name
        SEVERAL fields (SYN-179) — the indexer joins their values in
        mapping order; the server stores a single-element array as the
        bare string. Behavioral parts pin
        first-write — to change them remove and re-declare.
        Idempotent by collection name: an existing def is reused, but
        the draft stays authoritative for the mutable search.* leaves
        — a drifted title/text/scope is PATCHed back (already-indexed
        records keep their stored scope until they re-index) — and for
        the FIELD SET, additively: a declared field the live def lacks
        is added (add_dataset_field, ADR-017 §1; never removed) →
        {"datasetDefId", "created", "patched"?: [paths], "added"?:
        [keys]}. Records live
        per host object: write with upsert_records, read with
        query(space, object_id, "<name>") — plain field keys in
        filter/sort. Registered built-in types refuse (400
        type.registered)."""
        tid = self._resolve_type_or_raise(space, type_key)
        name = (draft or {}).get("name") or ""
        if name:   # ADR-019 §4: this run's queries guard the draft's dates
            self._dataset_time_keys.setdefault(name, set()).update(
                _datetime_keys(draft))
        for d in self.list_datasets(space, type_key):
            if d.get("name") == name:
                out = {"datasetDefId": d.get("id"), "created": False}
                want = (draft or {}).get("search") or {}
                have = d.get("search") or {}

                def _text_norm(v):
                    # search.text is string-or-array (SYN-179); the
                    # server stores single-element arrays as the bare
                    # string, so drift-compare normalized lists
                    return v if isinstance(v, list) else ([v] if v else [])

                set_ops, unset = {}, []
                for leaf in ("title", "text", "scope"):
                    w = want.get(leaf) or ""
                    h = have.get(leaf) or ""
                    same = (_text_norm(w) == _text_norm(h)
                            if leaf == "text" else w == h)
                    if not same:
                        if w:
                            set_ops[f"search.{leaf}"] = w
                        else:
                            unset.append(f"search.{leaf}")
                if set_ops or unset:
                    body = {}
                    if set_ops:
                        body["set"] = set_ops
                    if unset:
                        body["unset"] = unset
                    self._call(
                        "patch",
                        f"/v1/spaces/{space}/types/{tid}/datasets/{d.get('id')}",
                        body)
                    out["patched"] = sorted(list(set_ops) + unset)
                if isinstance(d.get("fields"), list):   # additive evolution
                    live = {fd.get("key") for fd in d["fields"]}
                    added = [fd["key"] for fd in (draft or {}).get("fields") or []
                             if fd.get("key") and fd["key"] not in live]
                    for key in added:
                        fd = next(x for x in draft["fields"] if x.get("key") == key)
                        self.add_dataset_field(space, type_key, d.get("id"), fd)
                    if added:
                        out["added"] = added
                return out
        r = self._call("post", f"/v1/spaces/{space}/types/{tid}/datasets",
                       draft)
        return {"datasetDefId": r.get("datasetDefId"), "created": True}

    def remove_dataset(self, space, type_key, dataset_def_id):
        """Tombstone a runtime dataset definition; returns {} (wire: 204).

        dataset_def_id from list_datasets. Existing record data is NOT
        cleaned up; subsequent writes drop once peers apply; the
        search index evicts lazily."""
        tid = self._resolve_type_or_raise(space, type_key)
        return self._call(
            "delete", f"/v1/spaces/{space}/types/{tid}/datasets/{dataset_def_id}")

    def add_dataset_field(self, space, type_key, dataset_def_id, field):
        """Add ONE field to an existing dataset definition (additive
        evolution, ADR-017 §1) → {fieldDefId}.

        field: {"key", "kind"?: string|number|boolean|array|object,
        "required"?, "mutableBy"?: "author"|"any", "stamp"?:
        "creator"|"createTime"|"modifyTime", "scope"?: "local",
        "name"?, "shape"?}. Existing records simply lack the key
        (a `required` field only gates writes from now on). The
        alternative — remove + re-declare — drops the declaration's
        pinned behaviour; adding a field keeps it. dataset_def_id from
        list_datasets. 404 dataset.not_found when the def is gone."""
        tid = self._resolve_type_or_raise(space, type_key)
        r = self._call(
            "post",
            f"/v1/spaces/{space}/types/{tid}/datasets/{dataset_def_id}/fields",
            field)
        return {"fieldDefId": r.get("fieldDefId")}

    def remove_dataset_field(self, space, type_key, dataset_def_id, field_def_id):
        """Remove ONE field definition from a dataset (wire: 204) →
        {}.

        field_def_id = the `id` inside list_datasets' `fields`. Stored
        values under that key are NOT cleaned up — the key becomes an
        undeclared (any-typed) field for readers; a later add under
        the same key re-declares it."""
        tid = self._resolve_type_or_raise(space, type_key)
        return self._call(
            "delete",
            f"/v1/spaces/{space}/types/{tid}/datasets/{dataset_def_id}/fields/{field_def_id}")

    def add_property(self, space, type_key, body):
        """POST one property onto a type (named by xKey — unknown keys
        error with the catalog). body: {"name", "xKey"?, "kind"?,
        "format"?, "scope"?, "description"?, "meta"?}. kind ∈ string |
        number | boolean | null | array | object (default "string");
        dates/links/selects go via {"format": {"type": "date" |
        "datetime" | "links" | "select" | "multiselect", "options"?:
        {key: {name, color?}}, "filter"?: <objects-query filter that
        narrows a links prop's candidates>}} with kind omitted (server
        derives it — date/datetime ⇒ `datetime`, written as
        `instant(...)`; "tags" is reserved). {"format": {"type": "url"
        | "email" | "longtext"}} declares a CLIENT convention: a string
        property with that `xKind` marker and no server format — plain
        string values, rendered by the UI as a link / textarea. `scope`
        ∈ synced (default) | account | local — pinned like kind. The
        property is appended to the type's display order (meta.pos).
        Returns {"propId"}."""
        tid = self._resolve_type_or_raise(space, type_key)
        return self._post_property(space, tid, body)

    def _post_property(self, space, type_id, body):
        body = dict(body or {})
        body.setdefault("xKey", _slugify_xkey(body.get("name") or ""))
        fmt = body.get("format")
        if isinstance(fmt, dict) and fmt.get("type") in _XKIND_MARKERS:
            # client convention (ADR-022 §1): string kind + xKind
            # marker, NO format on the wire
            if body.get("kind") not in (None, "string"):
                raise ValueError(
                    f'format.type {fmt["type"]!r} is a string convention — '
                    f'kind must be "string" (or omitted), got {body["kind"]!r}')
            body.pop("format")
            body["kind"] = "string"
            body.setdefault("xKind", fmt["type"])
            fmt = None
        elif fmt is None:    # with a format, the server derives
            body.setdefault("kind", "string")   # kind (links⇒array, date⇒datetime)
        elif isinstance(fmt, dict):
            if fmt.get("type") not in _FORMATS:
                raise ValueError(
                    f'format.type must be one of {list(_FORMATS)} (server '
                    f'formats) or {list(_XKIND_MARKERS)} (client conventions), '
                    f'got {fmt.get("type")!r} ("tags" is reserved server-side)')
            body.setdefault("xKind", _XKIND_OF_FORMAT[fmt["type"]])
            opts = fmt.get("options")
            if isinstance(opts, dict):
                fmt = dict(fmt)
                fmt["options"] = {}
                last = ""
                for k, o in opts.items():
                    o = dict(o or {}) if isinstance(o, dict) else {"name": o}
                    o.setdefault("name", k)
                    o.setdefault("color", _pick_color(k))
                    last = o.setdefault("pos", _lexid_after(last))
                    fmt["options"][k] = o
                body["format"] = fmt
        if body.get("kind") not in (None,) + _KINDS:
            raise ValueError(
                f'kind must be one of {list(_KINDS)}, got {body["kind"]!r} — '
                "dates/links/selects are FORMATS ({\"format\": {\"type\": …}})")
        meta = dict(body.get("meta") or {})
        if "pos" not in meta:
            last = max([(p.get("meta") or {}).get("pos") or ""
                        for p in self._type_props(space, type_id)] or [""])
            meta["pos"] = _lexid_after(last)
        body["meta"] = {k: str(v) for k, v in meta.items()}
        res = self._call("post",
                         f"/v1/spaces/{space}/types/{type_id}/properties", body)
        self._cat_invalidate(space)   # new prop -> refresh the propId map
        return res

    # --- ADR-022 §4: definition surface ---------------------------------------
    def _resolve_prop_or_raise(self, space, tid, prop_key):
        pid = self._resolve_prop_seg(space, tid, prop_key)
        if not pid:
            raise ValueError(
                f'unknown property "{prop_key}" — properties: '
                f"{self._prop_handles(space, tid)}")
        return pid

    def patch_property(self, space, type_key, prop_key, set=None, unset=None):
        """PATCH a property definition: `set` {path: string} / `unset`
        [path]. Mutable paths: name, description, xKey, xKind,
        meta.<k> (pos = display order lexid, index = search scope or
        "none", icon), format.ui, format.filter (a filter object),
        format.meta.<k>, format.options.<key>.{name,color,pos}.
        kind / scope / format.type / an option's KEY are pinned —
        refused here (define a new property instead). `set` must hit a
        scalar leaf; `unset` may name a container (unsetting
        `format.options.<key>` deletes the option). Returns {}."""
        tid = self._resolve_type_or_raise(space, type_key)
        pid = self._resolve_prop_or_raise(space, tid, prop_key)
        body = {}
        for path in list((set or {}).keys()) + list(unset or []):
            if path in _PINNED_PATHS or path.split(".")[0] in ("id", "key"):
                raise ValueError(
                    f'"{path}" is pinned at first write (400 '
                    "property.immutable) — kind/scope/format.type never "
                    "change; add a new property instead")
        if set:
            body["set"] = {k: (v if k == "format.filter" else str(v))
                           for k, v in set.items()}
        if unset:
            body["unset"] = list(unset)
        if not body:
            raise ValueError("patch_property: nothing to set or unset")
        self._call("patch",
                   f"/v1/spaces/{space}/types/{tid}/properties/{pid}", body)
        self._props_cache.pop((space, tid), None)
        return {}

    def set_option(self, space, type_key, prop_key, option, name=None,
                   color=None, pos=None):
        """Create or update one select/multiselect option. `option` is
        an existing key or name (matched like writes do), or a NEW name
        — then the key is minted (slug, uniquified) with `color` (one
        of grey yellow orange red pink purple blue ice teal green;
        default picked) and appended `pos`. Rename with `name=`,
        recolor with `color=`. Returns {"key", "name", "color",
        "created"}."""
        tid = self._resolve_type_or_raise(space, type_key)
        pid = self._resolve_prop_or_raise(space, tid, prop_key)
        pdef = self._prop_def(space, tid, pid) or {}
        if (pdef.get("format") or {}).get("type") not in ("select", "multiselect"):
            raise ValueError(
                f'"{pdef.get("handle")}" is not a select/multiselect '
                "(options live on those formats only)")
        ctx = self._write_ctx(True)
        key = self._option_key(space, tid, pdef, option, ctx)
        created = bool(ctx["createdOptions"])
        cur = dict(_options_of(pdef).get(key) or
                   ctx["option_patches"].get((tid, pid), {}).get(key) or {})
        sets = {}
        if created:
            sets.update({f"format.options.{key}.{leaf}": cur[leaf]
                         for leaf in ("name", "color", "pos")})
        if name is not None:
            sets[f"format.options.{key}.name"] = name
        if color is not None:
            if color not in _OPTION_COLORS:
                raise ValueError(f"color must be one of {list(_OPTION_COLORS)}")
            sets[f"format.options.{key}.color"] = color
        if pos is not None:
            sets[f"format.options.{key}.pos"] = pos
        if sets:
            self._call("patch",
                       f"/v1/spaces/{space}/types/{tid}/properties/{pid}",
                       {"set": sets})
            self._props_cache.pop((space, tid), None)
        return {"key": key, "name": name or cur.get("name") or key,
                "color": color or cur.get("color"), "created": created}

    def remove_option(self, space, type_key, prop_key, option):
        """Delete an option (by key or name). Values still holding the
        key stay as dangling keys — by design; rewrite them first if
        that matters. Returns {"key"}."""
        tid = self._resolve_type_or_raise(space, type_key)
        pid = self._resolve_prop_or_raise(space, tid, prop_key)
        pdef = self._prop_def(space, tid, pid) or {}
        ctx = self._write_ctx(False)
        key = self._option_key(space, tid, pdef, option, ctx)
        self._call("patch",
                   f"/v1/spaces/{space}/types/{tid}/properties/{pid}",
                   {"unset": [f"format.options.{key}"]})
        self._props_cache.pop((space, tid), None)
        return {"key": key}

    def reorder_property(self, space, type_key, prop_key, after=None):
        """Move a property in the type's display order: after the
        property `after` (a handle), or first when `after=""`; `None`
        = last. Re-expresses meta.pos for the whole type (sequential
        writes — the server serializes schema edits). Returns {"order":
        [handles]}."""
        tid = self._resolve_type_or_raise(space, type_key)
        pid = self._resolve_prop_or_raise(space, tid, prop_key)
        rows = sorted(self._type_props(space, tid), key=_prop_sort_key)
        target = next(p for p in rows if p["id"] == pid)
        rest = [p for p in rows if p["id"] != pid]
        if after is None:
            rest.append(target)
        elif after == "":
            rest.insert(0, target)
        else:
            aid = self._resolve_prop_or_raise(space, tid, after)
            idx = next(i for i, p in enumerate(rest) if p["id"] == aid)
            rest.insert(idx + 1, target)
        pos = ""
        for p in rest:
            pos = _lexid_after(pos)
            if (p.get("meta") or {}).get("pos") != pos:
                self._call("patch",
                           f"/v1/spaces/{space}/types/{tid}/properties/{p['id']}",
                           {"set": {"meta.pos": pos}})
        self._props_cache.pop((space, tid), None)
        return {"order": [p["handle"] for p in rest]}

    def archive_property(self, space, type_key, prop_key, restore=False):
        """Hide a property the way the UI removes one: the reversible
        `meta.anyUiArchived` marker (values untouched; `restore=True`
        clears it). Prefer this over delete_property. Returns {}."""
        tid = self._resolve_type_or_raise(space, type_key)
        pid = self._resolve_prop_or_raise(space, tid, prop_key)
        body = ({"unset": [f"meta.{_ARCHIVED_META}"]} if restore
                else {"set": {f"meta.{_ARCHIVED_META}": "1"}})
        self._call("patch",
                   f"/v1/spaces/{space}/types/{tid}/properties/{pid}", body)
        self._props_cache.pop((space, tid), None)
        return {}

    def delete_property(self, space, type_key, prop_key):
        """PERMANENTLY tombstone a property definition (CRDT — the id
        never comes back; stored values stay as orphans). The UI never
        does this; use archive_property unless the user insists.
        Returns {}."""
        tid = self._resolve_type_or_raise(space, type_key)
        pid = self._resolve_prop_or_raise(space, tid, prop_key)
        self._call("delete",
                   f"/v1/spaces/{space}/types/{tid}/properties/{pid}")
        self._props_cache.pop((space, tid), None)
        return {}

    def attach_type(self, space, object_id, type_key):
        """Add a type to an object (`any.types`) — the membership route
        the UI uses to put an object in a collection. Idempotent;
        unknown type/object 404. Returns {}."""
        tid = self._resolve_type_or_raise(space, type_key)
        self._call("post",
                   f"/v1/spaces/{space}/properties/{object_id}/attach/{tid}")
        return {}

    def detach_type(self, space, object_id, type_key):
        """Remove a type from an object's `any.types`. Values under that
        type stay as orphans and come back on re-attach. Returns {}."""
        tid = self._resolve_type_seg(space, type_key) or type_key
        self._call("post",
                   f"/v1/spaces/{space}/properties/{object_id}/detach/{tid}")
        return {}

    # --- bundles (SYN-163 / ADR-017 §0) ----------------------------------------
    def _bundle_path(self, space, bundle_id, tail=""):
        # bundle ids carry a slash ("bao/v1") — percent-encoded in path
        # segments, verbatim in bodies
        return f"/v1/spaces/{space}/bundles/{bundle_id.replace('/', '%2F')}{tail}"

    def ensure_bundle(self, space, bundle_id, name=None, root_types=None,
                      root_properties=None, derived=False):
        """Adopt-or-install a bundle → {bundle: {id, name, rootId,
        roots, losers, derived}, installed}.

        A bundle is one install: one root object registered under a
        permanent id in the space's bundles registry. With a winner
        already registered this is a local read (installed: False);
        otherwise the root is minted with root_types attached and
        registered in one change. derived=True installs on the root
        DERIVED from the bundle id — the same id on every device,
        computed offline, so the install can never fork; the price is
        permanence (a derived root is undeletable, so no uninstall —
        the general-chat/v1 convention). Without it the root is
        created fresh and rootId is provisional until the space syncs.
        409 bundle.not_ready (a winner's tree hasn't landed on this
        device) is retryable. Ids are permanent — never reuse one for
        a successor install."""
        body = {"id": bundle_id}
        if name:
            body["name"] = name
        if root_types:
            body["rootTypes"] = list(root_types)
        if root_properties:
            body["rootProperties"] = root_properties
        if derived:
            body["derived"] = True
        return self._call("post", f"/v1/spaces/{space}/bundles", body)

    def list_bundles(self, space):
        """The space's bundles registry rows → [{id, name, rootId,
        roots, losers?}]. Read-only; non-empty `losers` = a resolved
        concurrent install whose losing root may hold content."""
        r = self._call("get", f"/v1/spaces/{space}/bundles")
        return r.get("bundles") or []

    def get_bundle(self, space, bundle_id):
        """One registry row → {id, name, rootId, roots, losers?,
        derived, synced}; 404 bundle.not_found when nobody ensured it
        yet. The wire is a locked read `{bundle, synced}` — `synced`
        False means the registry may still be arriving from peers."""
        r = self._call("get", self._bundle_path(space, bundle_id))
        row = r.get("bundle") if isinstance(r.get("bundle"), dict) else r
        return {**row, "synced": r.get("synced", True)}

    def bundle_child(self, space, bundle_id, seed, types=None):
        """Derive a setup object under the bundle's winner → {objectId}.

        Deterministic per (space, root, seed) — the same id on every
        device, materialized on first call, cascade-deleted with the
        root. Seeds are permanent. 409 bundle.not_ready until the
        winner's tree is local (retryable). Cached per run."""
        key = (space, bundle_id, seed)
        if key not in self._bundle_children:
            body = {"seed": seed}
            if types:
                body["types"] = list(types)
            r = self._call("post",
                           self._bundle_path(space, bundle_id, "/children"),
                           body)
            self._bundle_children[key] = r.get("objectId") or ""
        return {"objectId": self._bundle_children[key]}

    def resolve_loser(self, space, bundle_id, loser_root_id):
        """Cascade-delete a losing bundle root after merging what
        matters out of it → {} (idempotent). 409 bundle.loser_not_ready
        until the loser's tree has settled (retry); 409
        bundle.not_loser for the winner or an unclaimed root. The
        server never merges — merge first, resolve second."""
        return self._call("post", self._bundle_path(space, bundle_id, "/resolve"),
                          {"loserRootId": loser_root_id})

    # --- agent turns / chunks (client-assigned seq, ADR-017 §2) ----------------
    def chat_log(self, space, chat_id):
        """The chat's log object hosting agent_turns + agent_chunks →
        {objectId}.

        The `bao/log/v1` child of the chat's own bundle (ADR-017 §0):
        deterministic, ensured with the agent_log type + datasets on
        first use. Query turns/chunks on THIS object, never on the
        chat itself. The chat must be a bundle root (every served chat
        is — general-chat/v1 etc.; §0a covers future non-bundle
        chats)."""
        return {"objectId": self._chat_log(space, chat_id)}

    def _chat_log(self, space, chat_id):
        key = (space, "log-of", chat_id)
        if key not in self._bundle_children:
            row = next((b for b in self.list_bundles(space)
                        if b.get("rootId") == chat_id), None)
            if row is None:
                raise AnyError(404, "bundle.not_found",
                               f"chat {chat_id} is not a bundle root — "
                               "no log child (ADR-017 §0a)")
            self._ensure_store(space, "agent_log", "Agent Log",
                               [_TURNS_DATASET, _CHUNKS_DATASET])
            tid = self._resolve_type_or_raise(space, "agent_log")
            child = self.bundle_child(space, row["id"], "bao/log/v1", [tid])
            self._bundle_children[key] = child["objectId"]
        return self._bundle_children[key]

    def _next_seq(self, space, host, dataset):
        # ADR-017 §2: the next free id is one past the highest id EVER
        # written, tombstones included — a deleted id never reuses
        # (upsert.record_deleted), and the live maximum drops below the
        # burned ones as soon as anything was deleted. Tombstones carry
        # no `seq` (content wiped), so the probe sorts on the record id,
        # which IS the zero-padded seq. One primary-key read.
        rows = self.query(space, host, dataset, includeDeleted=True,
                          sort=["-id"], limit=1)
        return (int(rows[0]["id"]) if rows else 0) + 1

    def append_turn(self, space, chat_id, body):
        """Append an `agent_turns` record on the chat's log child.
        Harness-level; conversations write these for you. Fields:
        `{seq?, fromAgent?, userName?, userText?, think?, replies?,
        effects?, messageIds?, traceRef?, interrupted?, llm?}` — llm
        subkeys `{stopReason, inTokens, outTokens, cacheRead,
        cacheWrite, model, costUsd, fuelUsed, cells}`. seq absent →
        one past the highest id ever written, deleted rows included
        (client-assigned; safe under the ADR-015 single active writer,
        a duplicate seq write rejects). Returns {recordIds, seq}."""
        return self._append_log(space, chat_id, "agent_turns", body,
                                search_text=True)

    def create_chunk(self, space, chat_id, body):
        """Append a compressed history chunk record (harness-level;
        rollup). Fields: `{seq?, level?, fromAgent?, summary,
        periodStart, periodEnd, fromSeq, toSeq, unitsCovered?}`. seq
        absent → max+1. Returns {recordIds, seq}."""
        return self._append_log(space, chat_id, "agent_chunks", body)

    def _append_log(self, space, chat_id, dataset, body, search_text=False):
        host = self._chat_log(space, chat_id)
        f = dict(body or {})
        seq = int(f.get("seq") or 0) or self._next_seq(space, host, dataset)
        f["seq"] = seq
        if search_text:
            # client-materialized index text (ADR-017 §1): the
            # conversational content, question first
            parts = [f.get("userText") or ""] + list(f.get("replies") or [])
            f["searchText"] = " ".join(p for p in parts if p).strip()
        rid = f"{seq:08d}"
        r = self.upsert_records(space, host, dataset,
                                [{"id": rid, "fields": f}])
        rej = r.get("rejections") or []
        if rej:
            raise AnyError(409, "log.seq_collision",
                           f"{dataset} seq {seq} rejected: {rej[0]}")
        return {"recordIds": [rid], "seq": seq}

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

    # established scopes every space has (docs/13-index.md); dataset
    # declarations mint the rest (`search.scope`) — the set is open
    _FIXED_SCOPES = ("basic", "chat", "props")

    def list_search_scopes(self, space):
        """The search scopes this space's index can answer → sorted
        list, e.g. ["agent", "basic", "chat", "email", "history",
        "props"]. `basic` (object names + editor text), `chat`
        (messages) and `props` (property values, FTS-only) always;
        every runtime dataset declared with a `search.scope` adds its
        own (`email` for synced mail, `agent`/`history` for bao's
        memory and turns). `search(space, q)` with no `scopes` covers
        ALL of them; pass a subset to narrow. Costs one datasets
        listing + one call per declaring type."""
        scopes = set(self._FIXED_SCOPES)
        rows = self._call("get", f"/v1/spaces/{space}/datasets").get("datasets", [])
        for type_id in sorted({r.get("typeId") for r in rows
                               if r.get("typeId") and "." in str(r.get("typeId"))
                               or (r.get("typeId") or "").startswith("bafy")}):
            try:
                defs = self._call("get", f"/v1/spaces/{space}/types/{type_id}/datasets")
            except AnyError:
                continue
            for d in defs.get("datasets", defs if isinstance(defs, list) else []):
                sc = (d.get("search") or {}).get("scope")
                if sc:
                    scopes.add(sc)
        return sorted(scopes)

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
    def _ensure_store(self, space, xkey, name, datasets):
        """Lazily provision a guest-owned agent store (ADR-017 §1):
        ensure the user type by xKey + its dataset declarations.
        Idempotent; the dataset ensure reconciles mutable search.*
        leaves. Cached per run."""
        key = (space, xkey)
        if key in self._ensured_stores:
            return
        self.create_type(space, {"name": name, "xKey": xkey})
        for d in datasets:
            self.create_dataset(space, xkey, d)
        self._ensured_stores.add(key)

    def get_brain(self, space):
        """The per-space brain object hosting agent_memory_items — the
        `bao/v1` bundle's `bao/brain/v1` child (ADR-017 §0), with the
        `agent_brain` type + dataset ensured lazily (guest-owned
        store). `{objectId}` — deterministic, no create race; requires
        the harness to have registered `bao/v1` (serve boot)."""
        self._ensure_store(space, "agent_brain", "Agent Brain",
                           [_MEM_DATASET, _JOB_STATE_DATASET,
                            _ROI_DATASET])
        tid = self._resolve_type_or_raise(space, "agent_brain")
        return self.bundle_child(space, "bao/v1", "bao/brain/v1", [tid])

    def create_memory(self, space, fields):
        """Create a memory item (category + context required).

        Writes the brain child's agent_memory_items dataset. Returns
        ModifyResult — recordIds[0] is the item id. Fields:
        `{fromAgent?, category, context, body?, tags?, entities?,
        keywords?, confidence?, importance?, salience?, validFrom?,
        edges?, chatId?, source?, provenance?}` — `source` is a
        lowercase slug ("extraction", "user", …), `provenance` exactly
        `{fromSeq}`; both create-only (not evolvable). Stray keys
        raise. Scoring defaults when absent: confidence 5, importance
        5, salience 10, accessCount 0, validFrom now. `validFrom` is an
        instant — `instant(seconds)` (ADR-019 §2)."""
        f = dict(fields or {})
        allowed = set(_MEM_MUTABLE) | set(_MEM_CREATE_ONLY)
        unknown = sorted(set(f) - allowed)
        if unknown:
            raise AnyError(400, "request.unknown_field",
                           f"unknown memory fields {unknown}; accepted: "
                           f"{sorted(allowed)}")
        for req in ("category", "context"):
            if not (isinstance(f.get(req), str) and f[req].strip()):
                raise AnyError(400, "request.missing_field",
                               f"{req} required (non-empty string)")
        self._mem_check_ranges(f)
        f.setdefault("confidence", 5)
        f.setdefault("importance", 5)
        f.setdefault("salience", 10)
        f.setdefault("accessCount", 0)
        f.setdefault("validFrom", instant(now()))  # noqa: F821 - guest globals
        if not _is_instant(f["validFrom"]):
            raise AnyError(400, "request.invalid_field",
                           "validFrom must be an instant — instant(seconds) "
                           "(ADR-019 §2)")
        brain = self.get_brain(space)["objectId"]
        ops = [{"type": "$set", "path": k, "value": v} for k, v in f.items()]
        return self.modify(space, {
            "objectId": brain, "dataset": "agent_memory_items",
            "records": [{"id": "", "upsert": True, "ops": ops}]})

    def evolve_memory(self, space, item_id, fields):
        """Evolve a memory item's mutable fields (author-only).

        modifiedAt is bumped by its stamp on apply. Mutable allow-list
        exactly `{salience, accessCount, confidence, importance,
        context, body, tags, edges, validTo}` — anything else
        (including source/provenance) raises; the declaration enforces
        the same for other identities. `validTo` (an instant) closes
        the item: recall reads live facts only (ADR-027 §4)."""
        f = dict(fields or {})
        unknown = sorted(set(f) - set(_MEM_MUTABLE))
        if unknown:
            raise AnyError(400, "request.unknown_field",
                           f"not evolvable: {unknown}; mutable: "
                           f"{sorted(_MEM_MUTABLE)}")
        if "context" in f and not (isinstance(f["context"], str)
                                   and f["context"].strip()):
            raise AnyError(400, "request.invalid_field",
                           "context must stay a non-empty string")
        if "validTo" in f and not _is_instant(f["validTo"]):
            raise AnyError(400, "request.invalid_field",
                           "validTo must be an instant — instant(seconds) "
                           "(ADR-019 §2)")
        self._mem_check_ranges(f)
        brain = self.get_brain(space)["objectId"]
        ops = [{"type": "$set", "path": k, "value": v} for k, v in f.items()]
        return self.modify(space, {
            "objectId": brain, "dataset": "agent_memory_items",
            "records": [{"id": item_id, "ops": ops}]})

    def delete_memory(self, space, item_id):
        """Delete a memory item by id (author-only — the dataset's
        deleteBy gate)."""
        brain = self.get_brain(space)["objectId"]
        return self.delete_records(space, brain, "agent_memory_items",
                                   [item_id])

    @staticmethod
    def _mem_check_ranges(f):
        for k, lo, hi in (("confidence", 0, 10), ("salience", 0, 10),
                          ("importance", 1, 10)):
            if k in f:
                v = f[k]
                if not isinstance(v, (int, float)) or not lo <= v <= hi:
                    raise AnyError(400, "request.invalid_field",
                                   f"{k} must be a number in {lo}..{hi}")
        if "accessCount" in f:
            v = f["accessCount"]
            if not isinstance(v, (int, float)) or v < 0:
                raise AnyError(400, "request.invalid_field",
                               "accessCount must be a number >= 0")

    # --- files (files v2; ADR-020 §2) -----------------------------------------
    @staticmethod
    def _file_ref(space, file):
        """(space, fileId, query) from an `any://f/<sid>/<fid>[?…]` URI or
        a bare fileId (space stays the given one)."""
        if isinstance(file, dict):
            file = file.get("link") or file.get("fileId") or file.get("id") or ""
        if not isinstance(file, str) or not file:
            raise TypeError(f"file must be an any://f/ URI or a fileId, got {file!r}")
        query = ""
        if "?" in file:
            file, query = file.split("?", 1)
        if file.startswith("any://f/"):
            parts = file[len("any://f/"):].split("/")
            if len(parts) != 2 or not all(parts):
                raise AnyError(400, "request.invalid_field",
                               f"malformed file URI {file!r} (any://f/<spaceId>/<fileId>)")
            space, file = parts
        return space, file, query

    def list_files(self, space, object_id=None):
        """Files in the space — `[{fileId, objectId, name, mime, size, …}]`;
        `object_id` narrows to one object's attachments. Files are
        addressed `any://f/<spaceId>/<fileId>` (chat attachments arrive
        as such lines); read one with file_content / llm.read, add one
        with attach_file."""
        q = f"?objectId={object_id}" if object_id else ""
        return self._call("get", f"/v1/spaces/{space}/files{q}").get("files", [])

    def file_content(self, space, file):
        """The file as a Blob — `{fileId, mime, size, blob}` (ADR-026 §5).
        `file` is an `any://f/<spaceId>/<fileId>` URI (a chat
        `[attachment …]` line; `?variant=thumb` passes through) or a
        bare fileId in `space`. `blob` is a handle, zero bytes in the
        cell: pass it to `llm.read`, a File part, `attach_file`, an
        http `body=`; `bytes(blob)` / `blob.text()` pull the payload
        in only when you must."""
        space, file_id, query = self._file_ref(space, file)
        url = self._base + f"/v1/spaces/{space}/files/{file_id}/content"
        if query:
            url += "?" + query
        reply = effect("http.get", {"url": url})  # noqa: F821
        body = reply.get("body")
        if reply["status"] >= 400:
            try:
                data = json.loads(body) if isinstance(body, str) and body else {}
            except ValueError:
                data = {}
            err = data.get("error", {}) if isinstance(data, dict) else {}
            raise AnyError(reply["status"], err.get("code", "unknown"),
                           err.get("message", ""))
        mime = (reply.get("headers") or {}).get("content-type", "application/octet-stream")
        mime = mime.split(";", 1)[0].strip()
        # a text/* file comes back as text (ADR-026 §3) — still a handle
        b = (Blob.from_ref(body) if Blob.is_ref(body)  # noqa: F821 - guest globals
             else blob.from_bytes(body or "", mime))  # noqa: F821
        return {"fileId": file_id, "mime": b.mime, "size": b.size, "blob": b}

    def attach_file(self, space, object_id, name, data, mime=None):
        """Attach a file to an object — the write half (ADR-026 §5).
        `data`: a Blob (an `http.get(...).blob`, `file_content(...)["blob"]`,
        a `tempfile` writer's `.blob`) or `bytes`/`str` (wrapped into
        one); `mime` defaults to the Blob's. One raw upload — the host
        streams the bytes, the trace keeps the ref. Returns the
        server's FileInfo plus `uri` (`any://f/<sid>/<fileId>`): put
        that in markdown (`![alt](<uri>)` — the editor renders images
        from any://f/ links only) or in `chat_send` attachments. There
        is no file without an object: to "create a file", pick or
        create the object it belongs to first."""
        b = data if isinstance(data, Blob) else blob.of(data)  # noqa: F821 - guest globals
        if not isinstance(data, Blob) and mime:  # noqa: F821
            b = blob.from_bytes(bytes(b), mime)  # noqa: F821
        mime = mime or b.mime
        url = (self._base + f"/v1/spaces/{space}/objects/{object_id}/files"
               f"?name={_urlquote(name)}")
        reply = effect("http.post", {"url": url, "body": b,  # noqa: F821 - guest global
                                     "headers": {"Content-Type": mime}})
        raw = reply.get("body") or ""
        if reply["status"] >= 400:
            try:
                data = json.loads(raw) if isinstance(raw, str) and raw else {}
            except ValueError:
                data = {}
            err = data.get("error", {}) if isinstance(data, dict) else {}
            raise AnyError(reply["status"], err.get("code", "unknown"),
                           err.get("message", ""))
        info = json.loads(raw) if isinstance(raw, str) and raw else {}
        if info.get("fileId"):
            info["uri"] = f"any://f/{space}/{info['fileId']}"
        return info


def _urlquote(s):
    """Percent-encode one query value (RFC 3986 unreserved kept)."""
    out = []
    for ch in str(s).encode("utf-8"):
        c = chr(ch)
        if c.isalnum() and ch < 128 or c in "-._~":
            out.append(c)
        else:
            out.append(f"%{ch:02X}")
    return "".join(out)


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
        base = effect("runtime.get", {"key": "any.base_url"})["value"]  # noqa: F821
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


@span(kind="mutator")  # noqa: F821 - guest global
def create_object(spaceConfig, body, create_options=True):
    return _c().create_object(_space(spaceConfig), body, create_options)


@span(kind="mutator")  # noqa: F821 - guest global
def update_object(spaceConfig, object_id, body, create_options=True):
    return _c().update_object(_space(spaceConfig), object_id, body,
                              create_options)


@span(kind="mutator")  # noqa: F821 - guest global
def delete_object(spaceConfig, object_id):
    return _c().delete_object(_space(spaceConfig), object_id)


@span(kind="getter")  # noqa: F821 - guest global
def query_objects(spaceConfig, *, normalize=True, **opts):
    return _c().query_objects(_space(spaceConfig), normalize=normalize, **opts)


@span(kind="getter")  # noqa: F821 - guest global
def list_programs(spaceConfig, tools_only=False):
    return _c().list_programs(_space(spaceConfig), tools_only)


@span(kind="getter")  # noqa: F821 - guest global
def query(spaceConfig, object_id, dataset, **opts):
    return _c().query(_space(spaceConfig), object_id, dataset, **opts)


@span(kind="mutator")  # noqa: F821 - guest global
def modify(spaceConfig, body):
    return _c().modify(_space(spaceConfig), body)


@span(kind="mutator")  # noqa: F821 - guest global
def upsert_record(spaceConfig, object_id, dataset, record_id, value):
    return _c().upsert_record(_space(spaceConfig), object_id, dataset,
                              record_id, value)


@span(kind="mutator")  # noqa: F821 - guest global
def upsert_records(spaceConfig, object_id, dataset, records, page_size=None):
    return _c().upsert_records(_space(spaceConfig), object_id, dataset,
                               records, page_size)


@span(kind="mutator")  # noqa: F821 - guest global
def delete_records(spaceConfig, object_id, dataset, record_ids):
    return _c().delete_records(_space(spaceConfig), object_id, dataset,
                               record_ids)


# `_`-private (hidden from the tool inventory, ADR-010 §1): dataset
# DECLARATION is program plumbing — a program that owns a store ensures
# its type + datasets (ADR-017 §1); the chat agent only reads/writes
# records. Records stay public: query / upsert_record(s) / delete_records.
def _list_datasets(spaceConfig, type_key):
    return _c().list_datasets(_space(spaceConfig), type_key)


def _create_dataset(spaceConfig, type_key, draft):
    return _c().create_dataset(_space(spaceConfig), type_key, draft)


def _remove_dataset(spaceConfig, type_key, dataset_def_id):
    return _c().remove_dataset(_space(spaceConfig), type_key, dataset_def_id)


def _add_dataset_field(spaceConfig, type_key, dataset_def_id, field):
    return _c().add_dataset_field(_space(spaceConfig), type_key, dataset_def_id, field)


def _remove_dataset_field(spaceConfig, type_key, dataset_def_id, field_def_id):
    return _c().remove_dataset_field(_space(spaceConfig), type_key, dataset_def_id,
                                     field_def_id)


@span(kind="getter")  # noqa: F821 - guest global
def aggregate(spaceConfig, pipeline, object_id=None, dataset=None):
    return _c().aggregate(_space(spaceConfig), pipeline, object_id, dataset)


@span(kind="getter")  # noqa: F821 - guest global
def list_processes():
    return _c().list_processes()


@span(kind="mutator")  # noqa: F821 - guest global
def cancel_process(process_id, identity=None):
    return _c().cancel_process(process_id, identity)


@span(kind="getter")  # noqa: F821 - guest global
def list_devices():
    return _c().list_devices()


# `_`-private (hidden from the tool inventory): progress@v1's transport
def _process_register(body):
    return _c()._process_register(body)


def _process_progress(process_id, body):
    return _c()._process_progress(process_id, body)


def _process_finish(process_id, body):
    return _c()._process_finish(process_id, body)


@span(kind="getter")  # noqa: F821 - guest global
def get_markdown(spaceConfig, object_id):
    return _c().get_markdown(_space(spaceConfig), object_id)


@span(kind="getter")  # noqa: F821 - guest global
def list_search_scopes(spaceConfig):
    return _c().list_search_scopes(_space(spaceConfig))


@span(kind="getter")  # noqa: F821 - guest global
def list_files(spaceConfig, object_id=None):
    return _c().list_files(_space(spaceConfig), object_id)


@span(kind="getter")  # noqa: F821 - guest global
def file_content(spaceConfig, file):
    return _c().file_content(_space(spaceConfig), file)


@span(kind="mutator")  # noqa: F821 - guest global
def attach_file(spaceConfig, object_id, name, data, mime=None):
    return _c().attach_file(_space(spaceConfig), object_id, name, data, mime)


@span(kind="mutator")  # noqa: F821 - guest global
def put_markdown(spaceConfig, object_id, content):
    return _c().put_markdown(_space(spaceConfig), object_id, content)


@span(kind="mutator")  # noqa: F821 - guest global
def edit_markdown(spaceConfig, object_id, edits):
    return _c().edit_markdown(_space(spaceConfig), object_id, edits)


@span(kind="mutator")  # noqa: F821 - guest global
def append_markdown(spaceConfig, object_id, content):
    return _c().append_markdown(_space(spaceConfig), object_id, content)


@span(kind="getter")  # noqa: F821 - guest global
def list_spaces(raw=False):
    return _c().list_spaces(raw)


@span(kind="getter")  # noqa: F821 - guest global
def get_space(spaceConfig, raw=False):
    return _c().get_space(_space(spaceConfig), raw)


@span(kind="getter")  # noqa: F821 - guest global
def general_chat(spaceConfig):
    return _c().general_chat(_space(spaceConfig))


@span(kind="mutator")  # noqa: F821 - guest global
def ensure_bundle(spaceConfig, bundle_id, name=None, root_types=None,
                  root_properties=None, derived=False):
    return _c().ensure_bundle(_space(spaceConfig), bundle_id, name,
                              root_types, root_properties, derived)


@span(kind="getter")  # noqa: F821 - guest global
def list_bundles(spaceConfig):
    return _c().list_bundles(_space(spaceConfig))


@span(kind="getter")  # noqa: F821 - guest global
def get_bundle(spaceConfig, bundle_id):
    return _c().get_bundle(_space(spaceConfig), bundle_id)


@span(kind="mutator")  # noqa: F821 - guest global
def bundle_child(spaceConfig, bundle_id, seed, types=None):
    return _c().bundle_child(_space(spaceConfig), bundle_id, seed, types)


@span(kind="mutator")  # noqa: F821 - guest global
def resolve_loser(spaceConfig, bundle_id, loser_root_id):
    return _c().resolve_loser(_space(spaceConfig), bundle_id, loser_root_id)


@span(kind="getter")  # noqa: F821 - guest global
def chat_log(spaceConfig, chat_id):
    return _c().chat_log(_space(spaceConfig), chat_id)


@span(kind="mutator")  # noqa: F821 - guest global
def create_space(name, description=None):
    return _c().create_space(name, description)


@span(kind="mutator")  # noqa: F821 - guest global
def open_in_ui(spaceConfig, object_id=None):
    return _c().open_in_ui(_space(spaceConfig), object_id)


@span(kind="getter")  # noqa: F821 - guest global
def list_types(spaceConfig):
    return _c().list_types(_space(spaceConfig))


@span(kind="getter")  # noqa: F821 - guest global
def list_properties(spaceConfig, type_key, include_archived=False):
    return _c().list_properties(_space(spaceConfig), type_key, include_archived)


# --- ADR-022 §4: definition surface -----------------------------------------
@span(kind="mutator")  # noqa: F821 - guest global
def patch_property(spaceConfig, type_key, prop_key, set=None, unset=None):
    return _c().patch_property(_space(spaceConfig), type_key, prop_key, set, unset)


@span(kind="mutator")  # noqa: F821 - guest global
def set_option(spaceConfig, type_key, prop_key, option, name=None, color=None,
               pos=None):
    return _c().set_option(_space(spaceConfig), type_key, prop_key, option,
                           name, color, pos)


@span(kind="mutator")  # noqa: F821 - guest global
def remove_option(spaceConfig, type_key, prop_key, option):
    return _c().remove_option(_space(spaceConfig), type_key, prop_key, option)


@span(kind="mutator")  # noqa: F821 - guest global
def reorder_property(spaceConfig, type_key, prop_key, after=None):
    return _c().reorder_property(_space(spaceConfig), type_key, prop_key, after)


@span(kind="mutator")  # noqa: F821 - guest global
def archive_property(spaceConfig, type_key, prop_key, restore=False):
    return _c().archive_property(_space(spaceConfig), type_key, prop_key, restore)


@span(kind="mutator")  # noqa: F821 - guest global
def delete_property(spaceConfig, type_key, prop_key):
    return _c().delete_property(_space(spaceConfig), type_key, prop_key)


@span(kind="mutator")  # noqa: F821 - guest global
def attach_type(spaceConfig, object_id, type_key):
    return _c().attach_type(_space(spaceConfig), object_id, type_key)


@span(kind="mutator")  # noqa: F821 - guest global
def detach_type(spaceConfig, object_id, type_key):
    return _c().detach_type(_space(spaceConfig), object_id, type_key)


@span(kind="mutator")  # noqa: F821 - guest global
def create_type(spaceConfig, body):
    return _c().create_type(_space(spaceConfig), body)


@span(kind="mutator")  # noqa: F821 - guest global
def add_property(spaceConfig, type_key, body):
    return _c().add_property(_space(spaceConfig), type_key, body)


@span(kind="mutator")  # noqa: F821 - guest global
def append_turn(spaceConfig, chat_id, body):
    return _c().append_turn(_space(spaceConfig), chat_id, body)


@span(kind="mutator")  # noqa: F821 - guest global
def create_chunk(spaceConfig, chat_id, body):
    return _c().create_chunk(_space(spaceConfig), chat_id, body)


@span(kind="mutator")  # noqa: F821 - guest global
def chat_send(spaceConfig, chat_id, body):
    return _c().chat_send(_space(spaceConfig), chat_id, body)


@span(kind="getter")  # noqa: F821 - guest global
def search(spaceConfig, query, scopes=None, limit=None, mode=None,
           enrich=True, **kw):
    if kw:   # A18: the guessed types= kwarg gets a redirect, not a bare TypeError
        raise TypeError(
            f"search() got unexpected keyword(s) {sorted(kw)} — search has "
            "no type filter. List objects of a type with "
            "query_objects(spaceConfig, filter={'any.types': '<xKey>'}), "
            "or post-filter hits on h['type'].")
    return _c().search(_space(spaceConfig), query, scopes, limit, mode, enrich)


@span(kind="getter")  # noqa: F821 - guest global
def backlinks(spaceConfig, object_id):
    return _c().backlinks(_space(spaceConfig), object_id)


@span(kind="getter")  # noqa: F821 - guest global
def get_brain(spaceConfig):
    return _c().get_brain(_space(spaceConfig))


@span(kind="mutator")  # noqa: F821 - guest global
def create_memory(spaceConfig, fields):
    return _c().create_memory(_space(spaceConfig), fields)


@span(kind="mutator")  # noqa: F821 - guest global
def evolve_memory(spaceConfig, item_id, fields):
    return _c().evolve_memory(_space(spaceConfig), item_id, fields)


@span(kind="mutator")  # noqa: F821 - guest global
def delete_memory(spaceConfig, item_id):
    return _c().delete_memory(_space(spaceConfig), item_id)


# lift the method docstrings onto the public functions — ONE authored
# copy (on _Client), rendered by describe()/help() from here
for _f in (create_object, update_object, delete_object, query_objects,
           list_programs, query, modify, upsert_record, upsert_records,
           delete_records, _list_datasets, _create_dataset, _remove_dataset,
           _add_dataset_field, _remove_dataset_field,
           aggregate, list_processes, cancel_process, list_devices,
           get_markdown, put_markdown, edit_markdown, list_files, file_content,
           list_search_scopes,
           append_markdown, list_spaces, get_space, general_chat,
           create_space, open_in_ui, list_types,
           list_properties, patch_property, set_option, remove_option,
           reorder_property, archive_property, delete_property,
           attach_type, detach_type,
           create_type, add_property, append_turn, create_chunk,
           chat_send, search, backlinks, get_brain, create_memory,
           evolve_memory, delete_memory):
    _f.__doc__ = getattr(_Client, _f.__name__.lstrip("_")).__doc__
del _f
