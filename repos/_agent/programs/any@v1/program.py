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
                "context", "body", "tags", "edges")
_MEM_CREATE_ONLY = ("fromAgent", "category", "entities", "keywords",
                    "validFrom", "chatId", "source", "provenance")
_MEM_DATASET = {
    "key": "agent_memory_items", "displayName": "Agent Memory",
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
    "key": "agent_job_state", "displayName": "Agent Job State",
    "idRule": "user", "deleteBy": "anyone", "skipHistory": True,
    "dynamic": True, "fields": [],
}
_ROI_DATASET = {
    # auto-recall injection log (autorecall@v1 §1b/§5 ROI metrics) —
    # free-form records keyed "<itemId>:<ts>", harness-owned shapes
    "key": "agent_roi_injections", "displayName": "Agent ROI Injections",
    "idRule": "user", "deleteBy": "anyone", "skipHistory": True,
    "dynamic": True, "fields": [],
}
_TURNS_DATASET = {
    "key": "agent_turns", "displayName": "Agent Turns",
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
    "key": "agent_chunks", "displayName": "Agent Chunks",
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


_DATASET_TIME_KEYS = {d["key"]: _datetime_keys(d) for d in
                      (_MEM_DATASET, _TURNS_DATASET, _CHUNKS_DATASET)}

# The module collections every space serves under their canonical names
# (ADR-027 §2): a dataset argument naming one passes through; every
# other dataset argument is a store KEY resolved against the host
# object's types to the collection the declaration reports.
_CANONICAL_COLLECTIONS = ("chat_messages", "editor_blocks", "objects")
# the catalog's general chat — the one chat a space has (ADR-027 §1)
_BAO_BUNDLE = "bao/v1"          # the harness bundle: bao space only (ADR-017 §0)
_GENERAL_CHAT_BUNDLE = "system:general-chat/v1"
# the catalog's wiki — the tree an object is in while it carries the
# wiki type with `parentId` / `pos` set (ADR-027 §3)
_WIKI_BUNDLE = "system:wiki/v1"


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
# space row: the general chat is the catalog's (`general_chat(space)`,
# ADR-027 §1), and the agent stores are `bao/v1` bundle children
# (`bundle_child`) — ADR-017 §0.
_SPACE_ROW_FIELDS = ("id", "name", "description", "status", "ownRole",
                     "spaceType", "createdAt")


def _trim_space_row(r):
    return {k: r[k] for k in _SPACE_ROW_FIELDS
            if r.get(k) not in (None, "", {})}


# Builtin group namespaces whose group + property keys are already
# literal handles (`any.name`, `any.types`). They are never reverse-
# mapped on read nor xKey-resolved on write — see the xKey
# normalization contract in ADR-006 §6. The hidden built-in TYPES
# (`page`, `miniapp`, `bin`, `dataview`) resolve by their literal id
# like every builtin; `program` and `mini_app` are harness-declared
# USER types (ADR-010 §5, ADR-008 §6): resolved by xKey like any other.
_RESERVED_GROUPS = {"any", "_ver"}

# Synthetic catalog rows: listed by GET /types in every space but not
# attachable — no object carries them, and the meta-type's only
# property (`type.xkey`) is writable solely on type rows.
_SYNTHETIC_TYPES = {"any", "spaceIndex", "type"}
# The one built-in every document carries: the shared editor body
# (`editor_blocks`) — attached by create_object / update_object on a
# body write, since no write attaches a type (ADR-027 §3).
_PAGE_TYPE = "page"
# The sidebar marker (ADR-027 §5): a root carrying it with `bundle` is
# an installed app; without one, an object the user pinned.
_MINIAPP_TYPE = "miniapp"
_BIN_TYPE = "bin"
# the hidden built-in types every space has: their id is their handle
_BUILTIN_TYPE_IDS = frozenset({_PAGE_TYPE, _MINIAPP_TYPE, _BIN_TYPE, "dataview"})

# --- ADR-027 §4: property descriptors (`xFormat`), handles, options ---------
# A definition is {name, xKey, kind, xFormat?, meta?}: `kind` is the
# storage guarantee (pinned); `xFormat` the descriptor bag — `type` (the
# slug), `options`, `relation`, `config`, `pos`, `icon`, `links`. The
# xKey is unique within its type on the server, so it IS the handle.
_KINDS = ("string", "number", "boolean", "null", "array", "object",
          "datetime")
# the kind a slug implies when a caller omits `kind` (any never derives
# it — the client does, so a bare {"xFormat": {"type": "date"}} works)
_KIND_OF_SLUG = {
    "choice": "array", "relation": "array",
    "date": "datetime", "datetime": "datetime",
    "checkbox": "boolean",
    "number": "number", "currency": "number", "percent": "number",
    "rating": "number", "duration": "number",
    "period": "object", "money": "object", "geo": "object",
}
# the option palette any-ui renders as swatches — `color` is an open
# string on the wire; these are the ten with a swatch
_OPTION_COLORS = ("grey", "yellow", "orange", "red", "pink", "purple",
                  "blue", "ice", "teal", "green")
# definition paths pinned at first write — a PATCH on them is 400
# property.immutable; refused client-side
_PINNED_PATHS = ("id", "kind", "scope", "items", "properties")
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
    """Stamp `handle` on every property row (ADR-027 §4): the xKey when
    present and unique on the type, else the name; a true duplicate is
    suffixed with its id so no two handles collide."""
    xk_count = {}
    for p in props:
        xk = p.get("xKey")
        if xk:
            xk_count[xk] = xk_count.get(xk, 0) + 1
    for p in props:
        xk = p.get("xKey")
        if xk and xk_count[xk] == 1:
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


def _xformat(pdef):
    return (pdef or {}).get("xFormat") or {}


def _slug(pdef):
    return _xformat(pdef).get("type")


def _multiple(pdef):
    return bool((_xformat(pdef).get("config") or {}).get("multiple"))


def _options_of(pdef):
    return _xformat(pdef).get("options") or {}


def _ordered_options(pdef):
    """[{key, name, color, pos}] by pos (byte compare), then key."""
    opts = _options_of(pdef)
    rows = [{"key": k, "name": (v or {}).get("name") or k,
             "color": (v or {}).get("color") or "grey",
             "pos": (v or {}).get("pos") or ""} for k, v in opts.items()]
    rows.sort(key=lambda r: (r["pos"] == "", r["pos"], r["key"]))
    return rows


def _prop_sort_key(p):
    pos = _xformat(p).get("pos") or ""
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
    def __init__(self, base_url, bao_space=None):
        self._base = base_url.rstrip("/")
        self._bao_space = bao_space   # the memory home (ADR-017 §0), runtime-wired
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
        self._ensured_stores = {}   # (space, xKey) -> {key: collection}
        self._stub_cache = {}   # space -> {objectId: {id, name, types}} (ADR-022 §3)
        self._type_datasets = {}   # (space, typeId) -> [dataset rows]
        self._object_types = {}    # (space, objectId) -> [type ids]
        self._chat_cache = {}      # space -> the general chat's root id
        self._wiki_cache = {}      # space -> {typeId, parentId, pos, folder}
        self._catalog_cache = None  # the server's usecase catalog
        self._collections_ready = set()   # spaces whose collections app is set up

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
                    continue
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
        # Builtins report xKey == id (page, miniapp, bin, dataview, any,
        # type, spaceIndex) and `builtIn`; user types have a CID id and
        # a slug xKey — only those are (reverse-)mapped.
        return (bool(row) and row.get("id") and row.get("id") != row.get("xKey")
                and not row.get("builtIn"))

    def _resolve_type_seg(self, space, seg, _retried=False):
        """A type xKey or id -> type id (or None). Refreshes the catalog once
        on miss so a freshly-created type resolves."""
        cat = self._catalog(space)
        if seg in cat["by_id"] or seg in _BUILTIN_TYPE_IDS:
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

    # --- dataset keys -> collections (ADR-027 §2) ----------------------------
    # A dataset's records live in the collection the server computed
    # at declaration (`<typeId>_<key>`, or a module's canonical
    # collection). The model and every program keep naming a store by
    # its KEY; this layer resolves the key against the types the host
    # object carries and never composes the string.
    def _datasets_of(self, space, type_id):
        key = (space, type_id)
        rows = self._type_datasets.get(key)
        if rows is None:
            try:
                r = self._call("get", f"/v1/spaces/{space}/types/{type_id}/datasets")
            except AnyError as e:
                if e.status != 404:
                    raise
                r = {}
            rows = [d for d in (r.get("datasets") or []) if isinstance(d, dict)]
            self._type_datasets[key] = rows
        return rows

    def _ds_invalidate(self, space, type_id=None, object_id=None):
        for k in [k for k in self._type_datasets
                  if k[0] == space and (type_id is None or k[1] == type_id)]:
            self._type_datasets.pop(k, None)
        for k in [k for k in self._object_types
                  if k[0] == space and (object_id is None or k[1] == object_id)]:
            self._object_types.pop(k, None)

    def _types_of_object(self, space, object_id):
        key = (space, object_id)
        types = self._object_types.get(key)
        if types is None:
            rows = self._call("post", f"/v1/spaces/{space}/objects/query",
                              {"filter": {"id": object_id}, "limit": 1}
                              ).get("records") or []
            anyg = (rows[0].get("any") if rows and isinstance(rows[0], dict)
                    else None) or {}
            types = [t for t in (anyg.get("types") or []) if isinstance(t, str)]
            self._object_types[key] = types
        return types

    def _is_collection(self, space, dataset):
        # a collection the server minted: `<typeId>_<key>` where the
        # prefix is a type of this space — passes through untouched
        if "_" not in dataset or not dataset.startswith("bafy"):
            return False
        return dataset.split("_", 1)[0] in self._catalog(space)["by_id"]

    def _collection(self, space, object_id, dataset, _retried=False):
        """The collection for a dataset argument on `object_id`: a
        canonical or already-resolved collection passes through; a
        store KEY resolves against the datasets the object's types
        declare — exactly one match, else an error naming the object's
        types and their keys (never first-match)."""
        if not isinstance(dataset, str) or not dataset:
            raise ValueError("dataset must be a non-empty string (a store key)")
        if dataset in _CANONICAL_COLLECTIONS or self._is_collection(space, dataset):
            return dataset
        types = self._types_of_object(space, object_id)
        hits = []
        for tid in types:
            for d in self._datasets_of(space, tid):
                if d.get("key") == dataset and d.get("collection"):
                    hits.append((tid, d["collection"]))
        if len(hits) == 1:
            return hits[0][1]
        if len(hits) > 1:
            cands = ", ".join(f"{self._dexify(space, t)} ({c})" for t, c in hits)
            raise ValueError(
                f'dataset "{dataset}" is declared by {len(hits)} types the '
                f"object carries — pass the collection: {cands}")
        if not _retried:
            self._ds_invalidate(space, object_id=object_id)
            return self._collection(space, object_id, dataset, True)
        keys = sorted({d.get("key") for t in types
                       for d in self._datasets_of(space, t) if d.get("key")})
        raise ValueError(
            f'object {object_id} carries no type declaring a dataset '
            f'"{dataset}" — its types: {self._dexify(space, types)}; '
            f"their datasets: {keys or 'none'}")

    @staticmethod
    def _collection_key(collection):
        """The store key inside a minted collection name (`<typeId>_<key>`
        → `<key>`); a canonical collection is its own key. The type id
        is recognised by SHAPE (a long alphanumeric content id, whatever
        its multibase prefix) — no catalog read, so hits from a foreign
        space (cross-space edges) resolve the same and the pre-wire
        instant guard stays offline."""
        if not isinstance(collection, str) or collection in _CANONICAL_COLLECTIONS \
                or "_" not in collection:
            return collection
        prefix, key = collection.split("_", 1)
        if len(prefix) >= 20 and prefix.isalnum():
            return key
        return collection

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
        shape the server writes by {typeId: {propId: val}}. The reserved
        builtin group (any) passes through with literal prop keys.
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
        """The wire value for `value` against its definition (ADR-027
        §4) — choice names → keys (minting missing options; always an
        array, one unless `config.multiple`), relation names/URIs →
        `["any://<id>"]`, dates → instants, scalars kind-checked; None
        ⇒ _UNSET. Raises ValueError naming the expected shape."""
        fmt = _slug(pdef)
        kind = pdef.get("kind")
        handle = pdef.get("handle") or pdef.get("name") or pdef.get("id")
        if value is None:
            return _UNSET
        if fmt == "choice":
            vals = value if isinstance(value, list) else [value]
            out = []
            for v in vals:
                k = self._option_key(space, tid, pdef, v, ctx)
                if k not in out:
                    out.append(k)
            if len(out) > 1 and not _multiple(pdef):
                raise ValueError(
                    f'"{handle}" is a single choice — pass one option, '
                    f"not {len(out)}")
            return out
        if fmt == "relation":
            vals = value if isinstance(value, list) else [value]
            out, explicit = [], []
            for v in vals:
                oid = _link_object_id(v)
                if oid is None:
                    if not isinstance(v, str) or not v.strip():
                        raise ValueError(
                            f'"{handle}" is a relation — pass object '
                            f"ids, any:// links, or object names, not "
                            f"{json.dumps(v)[:60]}")
                    oid = self._object_id_by_name(space, pdef, v.strip())
                elif oid not in explicit:
                    explicit.append(oid)
                uri = "any://" + oid
                if uri not in out:
                    out.append(uri)
            if len(out) > 1 and not _multiple(pdef):
                raise ValueError(
                    f'"{handle}" is a single relation — pass one object, '
                    f"not {len(out)}")
            # a relation value is the bare in-space id (no space
            # segment): an id from another space is unresolvable by
            # every reader, so an explicit id must exist HERE —
            # verified before any write, with the same batched lookup
            # hydration uses
            self._assert_links_in_space(space, handle, explicit)
            return out
        if fmt in ("date", "datetime"):
            return self._encode_instant(handle, fmt, value)
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
                "relations are in-space references; for an object in "
                "another space put a typed link in the body instead: "
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
        """Exact `any.name` match (then casefold-unique) within the
        relation's `relation.filter` (a JSON-text query condition) and
        `relation.targetTypes` (type xKeys); 0 or >1 hits error with
        candidates."""
        rel = _xformat(pdef).get("relation") or {}
        cand_filter = rel.get("filter")
        if isinstance(cand_filter, str) and cand_filter.strip():
            try:
                cand_filter = json.loads(cand_filter)
            except ValueError:
                cand_filter = None
        filt = {"any.name": name}
        clauses = [filt]
        if isinstance(cand_filter, dict) and cand_filter:
            clauses.append(cand_filter)
        targets = [t for t in rel.get("targetTypes") or []
                   if isinstance(t, str) and self._resolve_type_seg(space, t)]
        if targets:
            clauses.append({"any.types": {"$in": [
                self._resolve_type_seg(space, t) for t in targets]}})
        if len(clauses) > 1:
            filt = {"$and": clauses}
        rows = self._call("post", f"/v1/spaces/{space}/objects/query",
                          {"filter": filt, "limit": 5}).get("records") or []
        rows = [r for r in rows if isinstance(r, dict)]
        if len(rows) == 1:
            return rows[0]["id"]
        if not rows:
            raise ValueError(
                f'no object named "{name}" for relation '
                f'"{pdef.get("handle")}" — search(space, "{name}") for '
                "the id, or create the object first (relations never mint)")
        cands = "; ".join(
            f'{r["id"]} ({", ".join(self._dexify(space, (r.get("any") or {}).get("types") or []))})'
            for r in rows)
        raise ValueError(
            f'"{name}" names {len(rows)} objects — pass an id: {cands}')

    @staticmethod
    def _encode_instant(handle, fmt, value):
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
            secs = secs - (secs % 86400)   # a date lands on midnight UTC
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
                    sets[f"xFormat.options.{key}.{leaf}"] = o[leaf]
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
        server's "typeId.propId". Keys whose head is a builtin group
        (any.*, bare `id`) pass through unchanged. An
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
            if v == "__type__":
                return v   # the type-definition marker, not a type
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
        fmt = _slug(pdef)
        if fmt not in ("choice", "relation"):
            return cond
        ctx = self._write_ctx(create_options=False)

        def one(x):
            if fmt == "relation":
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
        record[typeId][propId] -> out[typeXKey][propXKey]. Builtin groups
        (any, the hidden built-in types) and scalars (id, …) pass through
        verbatim — their keys are already literal handles. `_ver` (CRDT
        version noise) is DROPPED — tokens the model can't use;
        normalize=False keeps it."""
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
        """Choice keys → option names (a dangling key passes through);
        relations stay `any://` strings here and are resolved in bulk
        by _hydrate_links. Instants stay instants (ADR-019)."""
        if _slug(pdef) != "choice":
            return v
        opts = _options_of(pdef)

        def name(k):
            o = opts.get(k) if isinstance(k, str) else None
            return (o or {}).get("name") or k
        return [name(k) for k in v] if isinstance(v, list) else name(v)

    def _hydrate_links(self, space, recs):
        """Replace relation values in normalized records with
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
                    if (_slug(p) == "relation"
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
    def create_object(self, space, body, create_options=True, parent=None,
                      folder=None):
        """Create a typed object; returns {"objectId", "resolved"?,
        "createdOptions"?, "warnings"?}.

        Top-level `name` / `description` route into the `any` group and
        `markdown` (alias `body`) becomes the editor body — one call
        creates the page: the built-in `page` type is added to `types`
        (the body lives on it; no write attaches a type) and the body
        is PUT after the create. `parent=` puts the object in the
        space's page tree (the wiki app): `""` for the top level or a
        parent object id — the object gains the wiki type with
        `parentId` and a position after the last sibling; `folder=True`
        marks it a folder. Without `parent` the object is outside every
        tree, reachable by search, links and queries. Everything else:
        `types` entries and `initialProperties` group + property keys
        are given as handles (or ids) and resolved to the content-ids
        the server writes by; the `any` group passes through literal.
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
        unknown = set(body) - {"types", "initialProperties",
                               "name", "description", "markdown", "body"}
        if unknown:
            raise ValueError(
                f"create_object: unknown top-level key(s) {sorted(unknown)} "
                "would be rejected by the wire (it accepts types/"
                "initialProperties). Properties go in initialProperties "
                'keyed by type xKey — {"any": {"name": ...}} — or pass '
                "name/description/markdown at top level; parent= places "
                "the object in the page tree.")
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
        types = list(body.get("types") or [])
        if markdown is not None and _PAGE_TYPE not in types \
                and not self._declares_body(space, types):
            types.append(_PAGE_TYPE)   # the body lives on `page` (ADR-027 §3)
        if parent is not None:
            wiki = self._wiki(space)
            if wiki["typeId"] not in types:
                types.append(wiki["typeId"])
            placement = {wiki["parentId"]: parent,
                         wiki["pos"]: self._next_tree_pos(space, wiki, parent)}
            if folder is not None:
                placement[wiki["folder"]] = bool(folder)
            groups = body.setdefault("initialProperties", {})
            groups[wiki["typeId"]] = {**groups.get(wiki["typeId"], {}), **placement}
        if types:
            body["types"] = types
        self._apply_option_patches(space, ctx)   # options before the value
        res = self._call("post", f"/v1/spaces/{space}/objects", body)
        object_id = res.get("objectId")
        if object_id:
            self._object_types[(space, object_id)] = list(types)
        if markdown is not None and object_id:
            self.put_markdown(space, object_id, markdown)
        return self._write_result(object_id, ctx)

    def _declares_body(self, space, type_ids):
        """Whether any of `type_ids` declares the shared editor body."""
        return any(d.get("collection") == "editor_blocks"
                   for tid in type_ids for d in self._datasets_of(space, tid))

    def _ensure_body(self, space, object_id):
        """A body write needs a declaring type on the object (no write
        attaches one): attach `page` unless a carried type declares
        the shared editor collection."""
        types = self._types_of_object(space, object_id)
        if _PAGE_TYPE in types or self._declares_body(space, types):
            return
        self._call("post",
                   f"/v1/spaces/{space}/properties/{object_id}/attach/{_PAGE_TYPE}")
        self._ds_invalidate(space, object_id=object_id)

    # --- the page tree (the catalog's wiki usecase, ADR-027 §3) --------------
    def _wiki(self, space):
        """The wiki type and its three property ids, set up once per
        space per run (the catalog setup is idempotent)."""
        w = self._wiki_cache.get(space)
        if w is None:
            r = self._call("post", "/v1/catalog/wiki/setup", {"spaceId": space})
            b = next((b for b in r.get("bundles") or []
                      if b.get("id") == _WIKI_BUNDLE), None) or {}
            props = b.get("properties") or {}
            if not b.get("typeId") or not all(k in props for k in ("parentId", "pos", "folder")):
                raise AnyError(500, "catalog.bad_reply",
                               f"wiki setup reply carries no type/properties: {r}")
            w = {"typeId": b["typeId"], **{k: props[k] for k in ("parentId", "pos", "folder")}}
            self._wiki_cache[space] = w
            self._cat_invalidate(space)   # the wiki type is new to the catalog
        return w

    def _next_tree_pos(self, space, wiki, parent):
        """A lexid after the last sibling under `parent` (the client
        allocates positions; the server orders nothing)."""
        rows = self._call("post", f"/v1/spaces/{space}/objects/query", {
            "filter": {f'{wiki["typeId"]}.{wiki["parentId"]}': parent},
            "sort": [f'-{wiki["typeId"]}.{wiki["pos"]}'], "limit": 1,
        }).get("records") or []
        last = ""
        if rows and isinstance(rows[0], dict):
            last = ((rows[0].get(wiki["typeId"]) or {}).get(wiki["pos"]) or "")
        return _lexid_after(last)

    def move_object(self, space, object_id, parent, folder=None):
        """Place an object in the page tree (or move it): `parent` is
        `""` for the top level or a parent object id; the object gains
        the wiki type if it lacks it, `parentId` is set and a position
        after the last sibling allocated; `folder=True/False` sets the
        folder flag. Returns {"objectId", "parentId", "pos"}."""
        wiki = self._wiki(space)
        if wiki["typeId"] not in self._types_of_object(space, object_id):
            self._call("post",
                       f"/v1/spaces/{space}/properties/{object_id}/attach/{wiki['typeId']}")
            self._ds_invalidate(space, object_id=object_id)
        pos = self._next_tree_pos(space, wiki, parent)
        patch = {wiki["parentId"]: parent, wiki["pos"]: pos}
        if folder is not None:
            patch[wiki["folder"]] = bool(folder)
        self._call("post",
                   f"/v1/spaces/{space}/properties/{object_id}/set/{wiki['typeId']}",
                   {"patch": patch})
        return {"objectId": object_id, "parentId": parent, "pos": pos}

    def list_children(self, space, parent=""):
        """The page tree under `parent` (`""` = the top level), in
        sidebar order → normalized rows (query_objects shape). A space
        without the wiki app has no tree: returns []."""
        if not any(b.get("id") == _WIKI_BUNDLE for b in self.list_bundles(space)):
            return []
        wiki = self._wiki(space)
        return self.query_objects(
            space,
            filter={f'{wiki["typeId"]}.{wiki["parentId"]}': parent},
            sort=[f'{wiki["typeId"]}.{wiki["pos"]}'])

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
        key would otherwise silently match nothing) — the builtin group
        (`any.*`) included. Derived `any` props
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
        for p in self.query_objects(
                space, filter={"$and": [{"any.types": "program"},
                                        {"any.types": {"$ne": "__type__"}},
                                        {"any.types": {"$nin": [_BIN_TYPE]}}]},
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
        # ADR-019 §4: stamps + declared datetime fields take instants
        # only — checked on the KEY before anything reaches the wire
        _guard_filter(opts.get("filter"),
                      _STAMP_KEYS | self._dataset_time_keys.get(
                          self._collection_key(dataset), set()))
        body = {"objectId": object_id,
                "dataset": self._collection(space, object_id, dataset)}
        body.update({k: v for k, v in opts.items() if v is not None})
        return self._call("post", f"/v1/spaces/{space}/query", body).get("records", [])

    def modify(self, space, body):
        """Low-level dataset write; prefer upsert_record / create_object.

        `body`: {objectId, dataset, records: [{id, upsert?, ops:
        [{type, path, value}]}]} — for partial ops. `dataset` is a
        store key (resolved against the object's types) or a
        collection."""
        body = dict(body or {})
        if body.get("objectId") and body.get("dataset"):
            body["dataset"] = self._collection(space, body["objectId"], body["dataset"])
        return self._call("post", f"/v1/spaces/{space}/modify", body)

    def upsert_record(self, space, object_id, dataset, record_id, value):
        """Write one dataset record (whole-value $set, upsert).

        `dataset` is the store KEY the owning program declared
        (agent_memory_items, email_messages, …), resolved against the
        types the host object carries — the object must carry the
        declaring type in `any.types` (400 dataset.not_declared
        otherwise; a key none of its types declare errors here with
        the list). A store that was never declared cannot be written:
        keep ad-hoc state as object properties. For bulk ingest into an
        idRule: user dataset prefer upsert_records (idempotent, diffs
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
        body = {"objectId": object_id,
                "dataset": self._collection(space, object_id, dataset),
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
                          {"objectId": object_id,
                           "dataset": self._collection(space, object_id, dataset),
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
        if bool(object_id) != bool(dataset):
            raise ValueError("aggregate over records takes BOTH object_id and "
                             "dataset (the object's dataset key); neither "
                             "aggregates the space's objects")
        if object_id and dataset:
            return self._call("post", f"/v1/spaces/{space}/aggregate",
                              {"objectId": object_id,
                               "dataset": self._collection(space, object_id, dataset),
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
    # The routes name the collection: the shared body `editor_blocks`
    # an object holds while it carries `page` or a type with a shared
    # editor part (ADR-027 §3). A write attaches `page` when the object
    # carries no declaring type — no write attaches one server-side.
    def _md_path(self, space, object_id, tail=""):
        return f"/v1/spaces/{space}/objects/{object_id}/editor/editor_blocks/markdown{tail}"

    def get_markdown(self, space, object_id):
        """The object's editor body as markdown TEXT (a string, not a dict)."""
        r = self._call("get", self._md_path(space, object_id))
        return r.get("content", "")

    def put_markdown(self, space, object_id, content):
        """Replace the object's editor body with `content` (markdown).
        Whole-body write — prefer append_markdown when adding."""
        self._ensure_body(space, object_id)
        return self._call("put", self._md_path(space, object_id),
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
        return self._call("patch", self._md_path(space, object_id),
                          {"edits": edits})

    def append_markdown(self, space, object_id, content):
        """Append to the editor body (server-side append-only fast path).

        No read-modify-write, so it can't clobber the body the way a
        get+put race can. Returns the api.MarkdownSetResponse dict."""
        self._ensure_body(space, object_id)
        return self._call("post", self._md_path(space, object_id, "/append"),
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
        """The space's canonical chat id — the catalog's general chat.

        Every space has exactly ONE chat: the server's `general-chat`
        usecase installs it on a root derived from the bundle id, the
        same on every device and member, so this call adopts or
        installs and always lands on the one chat (the chat module is
        reserved to it — no client can make another). Post there via
        `chat_send`; READ it via `query(space, chat_id,
        "chat_messages", sort=["-createdAt"], limit=n)` — never create
        a chat object or pick one from a query. Cached per run."""
        chat = self._chat_cache.get(space)
        if chat is None:
            chat = self._catalog_chat(space)
            self._chat_cache[space] = chat
        return chat

    def _catalog_chat(self, space):
        r = self._call("post", "/v1/catalog/general-chat/setup", {"spaceId": space})
        b = next((b for b in r.get("bundles") or []
                  if b.get("id") == _GENERAL_CHAT_BUNDLE), None) or {}
        row = b.get("bundle") or {}
        root = row.get("rootId")
        if not root:
            raise AnyError(500, "catalog.bad_reply",
                           f"general-chat setup reply carries no rootId: {r}")
        if row.get("derived") is not True:
            raise AnyError(409, "chat.not_derived",
                           f"the catalog's general chat in {space} is bound to "
                           f"non-derived object {root} — unsupported server")
        return root

    def create_space(self, name, description=None):
        """Create a new top-level space WITH its general chat → the
        (trimmed) space row + `generalChatId`.

        Right after the POST the catalog's `general-chat` usecase is
        set up (a derived root — the same id every member and device
        computes), so `generalChatId` is the chat everyone lands on —
        write chat there, never create chat objects (`general_chat(id)`
        returns the same id later). The space starts empty: resolve/
        create types against it before typed writes (types and xKeys
        are per-space). Check `list_spaces()` first — don't mint a
        duplicate of an existing active space."""
        # no spaceType: empty = server default on every vintage (the
        # literal "anytype.space" is rejected since SDK v0.0.10)
        body = {"name": name}
        if description:
            body["description"] = description
        r = self._call("post", "/v1/spaces", body)
        self._spaces_cache = None    # new space -> refresh the name catalog
        row = _trim_space_row(r)
        row["generalChatId"] = self.general_chat(row["id"])
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
        """Every type in the space: rows of {id, xKey, name, hidden?,
        builtIn?, weight?, layout?} — hidden ones included (the built-in
        `page`/`miniapp`/`bin`/`dataview`, the harness types, the
        catalog's hidden types such as the wiki)."""
        return self._call("get", f"/v1/spaces/{space}/types?includeHidden=true"
                          ).get("types", [])

    def list_properties(self, space, type_key):
        """A type's property definitions, in display order: [{handle,
        id, name, xKey, kind, scope, xFormat?, options?, meta?}].

        `handle` is THE key to read/write the property by (the xKey,
        else the name). `kind` is the storage shape (string | number |
        boolean | array | object | datetime); `xFormat` the descriptor
        — {"type": <slug>, "options"?, "relation"?: {"targetTypes",
        "filter"?}, "config"?: {"multiple"?, …}, "pos"?, "icon"?}.
        Slugs and what they take: `choice` (option NAME or key — a new
        name mints an option; one unless config.multiple; values store
        an ARRAY of keys), `relation` (object names/ids/any:// links;
        one unless config.multiple), `date`/`datetime` (instant()/ISO/
        epoch; a date lands on midnight UTC), `text`/`longtext`/
        `markdown`/`url`/`email`/`phone` (a string), `number`/
        `currency`/`percent`/`rating`/`duration` (a number),
        `checkbox` (a bool), `period`/`money`/`geo` (the object). A
        property without a descriptor is its plain kind. `options` is
        the ordered [{key, name, color}] of a choice. `scope` is the
        write/sync class (local-scope props exist only per-peer).
        `type_key` is the type's xKey (builtins: xKey == id); an
        unknown key ERRORS with the catalog — the server would answer
        a nonexistent id with a silent []."""
        tid = self._resolve_type_or_raise(space, type_key)
        self._props_cache.pop((space, tid), None)   # a listing reads fresh
        rows = []
        for p in sorted(self._type_props(space, tid), key=_prop_sort_key):
            row = dict(p)
            if _options_of(p):
                row["options"] = [{k: o[k] for k in ("key", "name", "color")}
                                  for o in _ordered_options(p)]
            rows.append(row)
        return rows

    def _fetch_props(self, space, type_id):
        # wire read by resolved id — internals (catalog, normalize) call
        # this directly so resolution can't recurse into itself
        r = self._call("get", f"/v1/spaces/{space}/types/{type_id}/properties")
        return r.get("properties", r) if isinstance(r, dict) else r

    def create_type(self, space, body):
        """Create a type, then add each property (composite ensure-type).

        body: {"name", "xKey"?, "description"?, "hidden"?, "weight"?,
        "layout"?, "properties"?: [{"name", "xKey"?, "kind"?,
        "xFormat"?}]}. kind ∈ string | number | boolean | array |
        object | datetime — NOTHING else ("text" and "date" are
        descriptor SLUGS, not kinds): {"xFormat": {"type": "date"}}
        (see add_property for the slug table); with a slug the kind
        may be omitted. xKeys default to a slug of the name.
        Idempotent: an existing USER type (by xKey) is reused, only
        MISSING properties are added. A name or xKey that collides with
        a builtin handle (any, spaceIndex, type, page, miniapp, bin,
        dataview) or a catalog type's ERRORS — those cannot be created
        or reshaped. `hidden: True` keeps the type out of pickers (the
        harness types are). Minting a listed type also sets up the
        space's `collections` app (the client's types feature switch)
        when it lacks one, so the type and its objects show in the UI.
        Returns {"typeId", "xKey", "created", "addedProps": {xKey:
        propId}} — reference everything by xKey afterwards."""
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
        if xkey in self._catalog_type_xkeys():
            raise ValueError(
                f'"{xkey}" is the type of a catalog app (`list_available_apps`) '
                "— catalog types cannot be created or reshaped; `setup_app` "
                "installs the app. Pick another name, or pass an explicit "
                'non-catalog "xKey".')
        tid = row["id"] if row else None
        created = False
        if tid is None:
            req = {k: body[k] for k in ("name", "description", "iconCid",
                                        "hidden", "weight", "layout")
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
                extra = {k: p[k] for k in ("kind", "meta", "xFormat", "scope",
                                           "description") if k in p}
                extra["name"] = p.get("name") or pxkey
                extra["xKey"] = pxkey
                added[pxkey] = self._post_property(space, tid, extra)["propId"]
        self._cat_invalidate(space)   # freshly (re)shaped type -> refresh xKey map
        if created and not body.get("hidden"):
            self._ensure_collections_app(space)
        return {"typeId": tid, "xKey": xkey, "created": created,
                "addedProps": added}

    def _ensure_collections_app(self, space):
        """A user type is invisible in the client until the space has
        the catalog's `collections` app (the types feature switch, a
        bare miniapp root — ADR-027 §5): set it up once per space per
        run when a listed type is minted. Idempotent on the server;
        best-effort here (a space that cannot install still holds the
        type)."""
        if space in self._collections_ready:
            return
        try:
            if not any(b.get("id") == "system:collections/v1"
                       for b in self.list_bundles(space)):
                self._call("post", "/v1/catalog/collections/setup", {"spaceId": space})
        except AnyError:
            return
        self._collections_ready.add(space)

    def list_datasets(self, space, type_key):
        """The datasets a type declares (by xKey) → [defs].

        Each def: {id, key, collection, module, shared?, partId,
        displayName?, idRule, idPattern?, deleteBy, skipHistory?,
        search?, fields: [{id, key, kind, scope, required?, mutableBy,
        stamp?, xFormat?}], invalid?, invalidReason?}. `collection` is
        where the records live — the server's name, read here and
        never composed; `key` is what query/upsert take. `invalid`
        marks a declaration that never registers or accepts data —
        remove it (remove_dataset) and re-declare."""
        tid = self._resolve_type_or_raise(space, type_key)
        self._ds_invalidate(space, type_id=tid)   # a listing reads fresh
        return list(self._datasets_of(space, tid))

    def create_dataset(self, space, type_key, draft):
        """Ensure a records dataset on a USER type — one part per store
        (ADR-016, ADR-027 §2). A module part instead — `{"module":
        "editor", "shared": true, "part"?: "body"}` — gives the type's
        objects the shared page body (`editor_blocks`).

        draft: {"key": "<store key>", "displayName"?, "idRule":
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
        records keep their stored scope until they re-index) →
        {"datasetDefId", "collection", "created", "patched"?: [paths]}.
        Records live per host object in `collection` (the server's
        `<typeId>_<key>`): write with upsert_records, read with
        query(space, object_id, "<key>") — the key resolves against
        the object's types; plain field keys in filter/sort.
        Registered built-in types refuse (400 type.registered)."""
        tid = self._resolve_type_or_raise(space, type_key)
        if "name" in (draft or {}):
            raise ValueError("create_dataset: \"name\" is not a dataset field — "
                             "the store is addressed by \"key\"")
        if (draft or {}).get("module") in ("editor", "chat"):
            # a module part: the shared canonical collection (`editor`
            # gives the type's objects the page body); `chat` is the
            # server's — the wire refuses it
            if not draft.get("shared"):
                raise ValueError("create_dataset: a module dataset is shared "
                                 "(the module's canonical collection)")
            canonical = "editor_blocks" if draft["module"] == "editor" else "chat_messages"
            for d in self.list_datasets(space, type_key):
                if d.get("collection") == canonical:
                    return {"datasetDefId": d.get("id"), "collection": canonical,
                            "created": False}
            self._call("post", f"/v1/spaces/{space}/types/{tid}/parts",
                       {"key": draft.get("part") or "body", "datasets": [
                           {"module": draft["module"], "shared": True}]})
            self._ds_invalidate(space, type_id=tid)
            d = next((d for d in self._datasets_of(space, tid)
                      if d.get("collection") == canonical), None) or {}
            return {"datasetDefId": d.get("id"), "collection": canonical,
                    "created": True}
        key = (draft or {}).get("key") or ""
        if not key:
            raise ValueError("create_dataset: the draft needs a \"key\" (the store key)")
        # ADR-019 §4: this run's queries guard the draft's dates
        self._dataset_time_keys.setdefault(key, set()).update(_datetime_keys(draft))
        for d in self.list_datasets(space, type_key):
            if d.get("key") == key:
                out = {"datasetDefId": d.get("id"),
                       "collection": d.get("collection"), "created": False}
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
                return out
        # one part per store, the dataset inline under the same key
        self._call("post", f"/v1/spaces/{space}/types/{tid}/parts",
                   {"key": key, "datasets": [draft]})
        self._ds_invalidate(space, type_id=tid)
        d = next((d for d in self._datasets_of(space, tid) if d.get("key") == key),
                 None)
        if d is None:
            raise AnyError(500, "dataset.not_listed",
                           f"dataset {key!r} declared on {type_key} but not listed")
        return {"datasetDefId": d.get("id"), "collection": d.get("collection"),
                "created": True}

    def remove_dataset(self, space, type_key, dataset_def_id):
        """Tombstone a runtime dataset definition; returns {} (wire: 204).

        dataset_def_id from list_datasets. Existing record data is NOT
        cleaned up; subsequent writes drop once peers apply; the
        search index evicts lazily."""
        tid = self._resolve_type_or_raise(space, type_key)
        r = self._call(
            "delete", f"/v1/spaces/{space}/types/{tid}/datasets/{dataset_def_id}")
        self._ds_invalidate(space, type_id=tid)
        return r

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
        "xFormat"?, "scope"?, "description"?, "meta"?}. kind ∈ string
        | number | boolean | array | object | datetime (default from
        the slug, else "string"). `xFormat` is the descriptor:
        {"type": <slug>, "options"?: {key: {name, color?}} (choice),
        "relation"?: {"targetTypes": [type xKeys], "filter"?: <query
        condition>} (relation), "config"?: {"multiple": true, …}}.
        Slugs: `text` `longtext` `markdown` `url` `email` `phone`
        (string), `choice` (array of option keys — one unless
        config.multiple), `relation` (array of any:// links — one
        unless config.multiple), `date` `datetime` (datetime, written
        as instant(…)), `number` `currency` `percent` `rating`
        `duration` (number), `checkbox` (boolean), `period` `money`
        `geo` (object). `meta` takes only `index` (a search scope, or
        "none"). `scope` ∈ synced (default) | account | local — pinned
        like kind. The property is appended to the type's display
        order (xFormat.pos). Returns {"propId"}."""
        tid = self._resolve_type_or_raise(space, type_key)
        return self._post_property(space, tid, body)

    def _post_property(self, space, type_id, body):
        body = dict(body or {})
        body.setdefault("xKey", _slugify_xkey(body.get("name") or ""))
        if "format" in body or "xKind" in body:
            raise ValueError(
                'a property carries "xFormat" ({"type": <slug>, …}) — '
                'there is no "format" or "xKind"')
        fmt = body.get("xFormat")
        if fmt is not None and not isinstance(fmt, dict):
            raise ValueError("xFormat must be an object")
        fmt = dict(fmt or {})
        slug = fmt.get("type")
        if body.get("kind") is None:
            body["kind"] = _KIND_OF_SLUG.get(slug, "string")
        if body["kind"] not in _KINDS:
            raise ValueError(
                f'kind must be one of {list(_KINDS)}, got {body["kind"]!r} — '
                'dates/relations/choices are descriptor SLUGS '
                '({"xFormat": {"type": …}})')
        opts = fmt.get("options")
        if isinstance(opts, dict):
            fmt["options"] = {}
            last = ""
            for k, o in opts.items():
                o = dict(o or {}) if isinstance(o, dict) else {"name": o}
                o.setdefault("name", k)
                o.setdefault("color", _pick_color(k))
                last = o.setdefault("pos", _lexid_after(last))
                fmt["options"][k] = o
        rel = fmt.get("relation")
        if isinstance(rel, dict) and isinstance(rel.get("filter"), dict):
            fmt["relation"] = {**rel, "filter": json.dumps(rel["filter"])}
        if "pos" not in fmt:   # appended to the type's display order
            last = max([_xformat(p).get("pos") or ""
                        for p in self._type_props(space, type_id)] or [""])
            fmt["pos"] = _lexid_after(last)
        body["xFormat"] = fmt
        meta = body.get("meta")
        if meta is not None:
            extra = set(meta) - {"index"}
            if extra:
                raise ValueError(
                    f"meta takes only \"index\" (a search scope or \"none\"); "
                    f"{sorted(extra)} belong under xFormat")
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
        """PATCH a property definition: `set` {path: leaf value} /
        `unset` [path]. Mutable paths: name, description, xKey,
        meta.index (a search scope or "none"), and every leaf under
        xFormat — xFormat.type (the slug moves within the pinned
        kind), xFormat.pos / .icon, xFormat.options.<key>.{name,color,
        pos}, xFormat.relation.targetTypes (a list) / .filter (a JSON
        text), xFormat.config.<k>. kind / scope / items / properties
        are pinned — refused here (define a new property instead). A
        `set` must hit a LEAF (never an object — containers are
        unset-only); `unset` may name a container (unsetting
        `xFormat.options.<key>` deletes the option). Returns {}."""
        tid = self._resolve_type_or_raise(space, type_key)
        pid = self._resolve_prop_or_raise(space, tid, prop_key)
        body = {}
        for path in list((set or {}).keys()) + list(unset or []):
            if path in _PINNED_PATHS or path.split(".")[0] in ("id", "key"):
                raise ValueError(
                    f'"{path}" is pinned at first write (400 '
                    "property.immutable) — kind/scope never change; add "
                    "a new property instead")
        if set:
            for k, v in set.items():
                if isinstance(v, dict):
                    raise ValueError(
                        f'"{k}": a set targets a leaf — containers are '
                        "unset-only; set each leaf (…options.<key>.name)")
            body["set"] = {k: (v if k.startswith("xFormat.") else str(v))
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
        """Create or update one option of a choice property. `option`
        is an existing key or name (matched like writes do), or a NEW
        name — then the key is minted (slug, uniquified) with `color`
        (one of grey yellow orange red pink purple blue ice teal green;
        default picked) and appended `pos`. Rename with `name=`,
        recolor with `color=`. Returns {"key", "name", "color",
        "created"}."""
        tid = self._resolve_type_or_raise(space, type_key)
        pid = self._resolve_prop_or_raise(space, tid, prop_key)
        pdef = self._prop_def(space, tid, pid) or {}
        if _slug(pdef) != "choice":
            raise ValueError(
                f'"{pdef.get("handle")}" is not a choice (options live on '
                "the choice slug only)")
        ctx = self._write_ctx(True)
        key = self._option_key(space, tid, pdef, option, ctx)
        created = bool(ctx["createdOptions"])
        cur = dict(_options_of(pdef).get(key) or
                   ctx["option_patches"].get((tid, pid), {}).get(key) or {})
        sets = {}
        if created:
            sets.update({f"xFormat.options.{key}.{leaf}": cur[leaf]
                         for leaf in ("name", "color", "pos")})
        if name is not None:
            sets[f"xFormat.options.{key}.name"] = name
        if color is not None:
            if color not in _OPTION_COLORS:
                raise ValueError(f"color must be one of {list(_OPTION_COLORS)}")
            sets[f"xFormat.options.{key}.color"] = color
        if pos is not None:
            sets[f"xFormat.options.{key}.pos"] = pos
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
                   {"unset": [f"xFormat.options.{key}"]})
        self._props_cache.pop((space, tid), None)
        return {"key": key}

    def reorder_property(self, space, type_key, prop_key, after=None):
        """Move a property in the type's display order: after the
        property `after` (a handle), or first when `after=""`; `None`
        = last. Re-expresses xFormat.pos for the whole type (sequential
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
            if _xformat(p).get("pos") != pos:
                self._call("patch",
                           f"/v1/spaces/{space}/types/{tid}/properties/{p['id']}",
                           {"set": {"xFormat.pos": pos}})
        self._props_cache.pop((space, tid), None)
        return {"order": [p["handle"] for p in rest]}

    def delete_property(self, space, type_key, prop_key):
        """PERMANENTLY tombstone a property definition (CRDT — the id
        never comes back; stored values stay as orphans). Confirm with
        the user first. Returns {}."""
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
        self._ds_invalidate(space, object_id=object_id)
        return {}

    def detach_type(self, space, object_id, type_key):
        """Remove a type from an object's `any.types`. Values under that
        type stay as orphans and come back on re-attach. Returns {}."""
        tid = self._resolve_type_seg(space, type_key) or type_key
        self._call("post",
                   f"/v1/spaces/{space}/properties/{object_id}/detach/{tid}")
        self._ds_invalidate(space, object_id=object_id)
        return {}

    # --- bundles (SYN-163 / ADR-017 §0) ----------------------------------------
    def _bundle_path(self, space, bundle_id, tail=""):
        # bundle ids carry a slash ("bao/v1") and the catalog's a colon
        # ("system:wiki/v1") — percent-encoded in path segments,
        # verbatim in bodies
        enc = bundle_id.replace("/", "%2F").replace(":", "%3A")
        return f"/v1/spaces/{space}/bundles/{enc}{tail}"

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
        permanence (a derived root is undeletable, so no uninstall).
        Ids under `system:` are the server's catalog (409
        bundle.reserved — `setup_app` installs those). Without it the root is
        created fresh and rootId is provisional until the space syncs.
        409 bundle.not_ready (a winner's tree hasn't landed on this
        device) is retryable. Ids are permanent — never reuse one for
        a successor install. `bao/v1` is the harness's own bundle:
        serve registers it in the bao space and nowhere else (memory
        lives there — `get_brain` takes no space); it is refused
        here."""
        if bundle_id == _BAO_BUNDLE:
            raise ValueError(
                f"{_BAO_BUNDLE} is the harness bundle — serve registers it in "
                "the bao space only; memory lives there (get_brain() / "
                "create_memory() take no space), never in a user space")
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
            if self._bundle_children[key] and types:
                # the child carries exactly the types it was derived with
                self._object_types[(space, self._bundle_children[key])] = list(types)
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
        chat itself. The chat must be a bundle root (the general chat
        is the catalog's `system:general-chat/v1`; §0a covers future
        non-bundle chats)."""
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
        `{data (the matched text), dataset (the collection), key (the
        store key inside it — `agent_memory_items`, `chat_messages`,
        …), objectId, recordId, scope, score}`. With enrich=True
        (default) every hit also gets `title`
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
        for h in result.get("hits") or []:
            if isinstance(h, dict) and isinstance(h.get("dataset"), str):
                h["key"] = self._collection_key(h["dataset"])
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
        owners = sorted({o for r in rows if r.get("module") == "records"
                         for o in (r.get("owners") or []) if isinstance(o, str)})
        for type_id in owners:
            for d in self._datasets_of(space, type_id):
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
            rows = self._catalog(space)["rows"]        # memoized per space
            type_name = {t["id"]: t.get("name") for t in rows}
            type_name.update({t.get("xKey"): t.get("name") for t in rows
                              if t.get("xKey")})
        except AnyError:
            return
        for h in hits:
            obj = by_id.get(h.get("objectId"))
            if not obj:
                continue
            meta = obj.get("any") or {}
            h["title"] = meta.get("name")
            types = meta.get("types") or []
            # primary type = the first listed user type; the hidden
            # built-ins (page, miniapp, bin) and `__type__` never win
            primary = next((t for t in types
                            if t not in (_PAGE_TYPE, _MINIAPP_TYPE, _BIN_TYPE,
                                         "dataview", "__type__")),
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

    def _edge(self, space, e):
        """One link-index edge → `{objectId, dataset?, key?, recordId?,
        kind, type?, prop?, spaceId?, target}`: the referencing object,
        where the reference sits (a block, a message, a record, or a
        property value: `prop` as "typeXKey.propXKey"), and the target
        URI. Ids read as xKeys where the catalog knows them."""
        src = e.get("source") or {}
        out = {"objectId": src.get("objectId"), "kind": e.get("kind"),
               "target": (e.get("target") or {}).get("uri")}
        if src.get("spaceId") and src.get("spaceId") != space:
            out["spaceId"] = src["spaceId"]
        ds = src.get("dataset")
        if ds and ds != "prop":
            out["dataset"] = ds
            out["key"] = self._collection_key(ds)
            if src.get("recordId"):
                out["recordId"] = src["recordId"]
        elif ds == "prop":
            tid, pid = src.get("typeId"), src.get("recordId")
            row = self._catalog(space)["by_id"].get(tid) if tid else None
            prop = pid
            if row:
                for p in self._type_props(space, tid):
                    if p.get("id") == pid:
                        prop = p.get("xKey") or p.get("name") or pid
                        break
            out["prop"] = f'{(row or {}).get("xKey") or tid}.{prop}'
        return out

    def backlinks(self, space, object_id):
        """What links HERE: the link index's edges pointing at the object
        (`object`) and at its records or property values (`parts`) →
        {"object": [edge], "parts": [edge], "truncated"?}. An edge:
        `{objectId, kind, target, dataset?/key?/recordId? (a block,
        message or record), prop? ("type.prop" for a relation value),
        spaceId? (when foreign)}`. Edges come from editor blocks, chat
        messages, and relation / markdown properties; the index
        catches up a few hundred ms after a write. 409 index.disabled
        when the search index is off."""
        r = self._call("get", f"/v1/spaces/{space}/objects/{object_id}/backlinks")
        out = {"object": [self._edge(space, e) for e in r.get("object") or []],
               "parts": [self._edge(space, e) for e in r.get("parts") or []]}
        if r.get("truncated"):
            out["truncated"] = True
        return out

    def links(self, space, object_id):
        """What this object links TO: the forward edges out of its
        blocks, messages, records and relation values → [edge] (the
        backlinks edge shape; `objectId` is this object)."""
        r = self._call("get", f"/v1/spaces/{space}/objects/{object_id}/links")
        return [self._edge(space, e) for e in r.get("links") or []]

    def backlinks_everywhere(self, target_uri):
        """Account-wide backlinks to one target across every space this
        device indexes → [{spaceId, object: [edge], parts: [edge]}].
        `target_uri` is the GLOBAL form: `any://o/<spaceId>/<objectId>`
        (also `any://m/…` for a member, `any://f/…` for a file)."""
        r = self._call("get", f"/v1/backlinks?target={_urlquote(target_uri)}")
        out = []
        for sp in r.get("spaces") or []:
            sid = sp.get("spaceId") or ""
            out.append({"spaceId": sid,
                        "object": [self._edge(sid, e) for e in sp.get("object") or []],
                        "parts": [self._edge(sid, e) for e in sp.get("parts") or []]})
        return out

    # --- apps: the sidebar, the registry, the catalog (ADR-027 §5) ------------
    def _catalog_usecases(self):
        if self._catalog_cache is None:
            self._catalog_cache = self._call("get", "/v1/catalog").get("usecases") or []
        return self._catalog_cache

    def _catalog_type_xkeys(self):
        """The xKeys of the types the catalog's apps bring (wiki, person,
        …) — reserved handles a user type may not take. One read per
        run; a server without a catalog reserves nothing."""
        try:
            usecases = self._catalog_usecases()
        except AnyError:
            return set()
        return {(b.get("type") or {}).get("xKey")
                for u in usecases for b in u.get("bundles") or []
                if (b.get("type") or {}).get("xKey")}

    def _usecase_of_bundle(self, bundle_id):
        for u in self._catalog_usecases():
            for b in u.get("bundles") or []:
                if b.get("id") == bundle_id:
                    return u, b
        return None, None

    def list_apps(self, space):
        """The apps a space has — what shows in its sidebar → [{name,
        bundleId?, rootId, usecase?, description, hidden, pinned}].

        Apps are DATA: every installed app is an object carrying the
        `miniapp` marker with `bundle` = the install's id (the wiki is
        `system:wiki/v1`, the chat `system:general-chat/v1`, contacts,
        CRM, …); a row without a bundle is an object the user pinned.
        `usecase` and `description` come from the server's catalog for
        its own apps (the root's own `any.description` wins when set).
        Never assume a wiki or contacts exists — read this. To offer
        more, `list_available_apps`; to install, `setup_app`."""
        rows = self.query_objects(
            space, normalize=False,
            filter={"$and": [{"any.types": _MINIAPP_TYPE},
                             {"any.types": {"$nin": [_BIN_TYPE]}}]},
            sort=[f"{_MINIAPP_TYPE}.pos"])
        out = []
        for r in rows:
            anyg = r.get("any") or {}
            mini = r.get(_MINIAPP_TYPE) or {}
            bundle = mini.get("bundle")
            row = {"name": anyg.get("name"), "rootId": r.get("id"),
                   "description": anyg.get("description") or "",
                   "hidden": bool(mini.get("hidden")), "pinned": not bundle}
            if bundle:
                row["bundleId"] = bundle
                u, b = self._usecase_of_bundle(bundle)
                if u:
                    row["usecase"] = u.get("id")
                    row["description"] = (row["description"] or b.get("description")
                                          or u.get("description") or "")
            out.append(row)
        return out

    def list_available_apps(self, space):
        """The server's catalog of well-known apps with what this space
        has → [{usecase, name, description, requires, installed}].
        `setup_app(space, usecase)` installs one (dependencies too)."""
        have = {b.get("id") for b in self.list_bundles(space)}
        out = []
        for u in self._catalog_usecases():
            ids = [b.get("id") for b in u.get("bundles") or []]
            out.append({"usecase": u.get("id"), "name": u.get("name"),
                        "description": u.get("description") or "",
                        "requires": list(u.get("requires") or []),
                        "installed": bool(ids) and all(i in have for i in ids)})
        return out

    def setup_app(self, space, usecase):
        """Install (or adopt) one of the catalog's apps in a space,
        dependencies first → [{usecase, bundleId, rootId, installed,
        typeId?, properties?}] — `properties` is the type's xKey →
        propId map. Idempotent: run it again and everything adopts.
        The user's call — offer, then install on a yes."""
        r = self._call("post", f"/v1/catalog/{usecase}/setup", {"spaceId": space})
        self._cat_invalidate(space)
        out = []
        for b in r.get("bundles") or []:
            row = {"usecase": b.get("usecase"), "bundleId": b.get("id"),
                   "rootId": (b.get("bundle") or {}).get("rootId"),
                   "installed": bool(b.get("installed"))}
            if b.get("typeId"):
                row["typeId"] = b["typeId"]
            if b.get("properties"):
                row["properties"] = b["properties"]
            out.append(row)
        return out

    # --- agent memory (write path; reads go through /query on the brain) --------
    def _ensure_store(self, space, xkey, name, datasets):
        """Lazily provision a guest-owned agent store (ADR-017 §1,
        ADR-027 §2): ensure the hidden user type by xKey + one part per
        dataset draft. Idempotent; the dataset ensure reconciles
        mutable search.* leaves. Cached per run → {key: collection}."""
        key = (space, xkey)
        if key in self._ensured_stores:
            return self._ensured_stores[key]
        self.create_type(space, {"name": name, "xKey": xkey, "hidden": True})
        colls = {}
        for d in datasets:
            colls[d["key"]] = self.create_dataset(space, xkey, d)["collection"]
        self._ensured_stores[key] = colls
        return colls

    def bao_space(self):
        """The bao space id — memory's only home (ADR-017 §0). Wired by
        the runtime (serve; `run --from-space` or a `bao.space` config
        key); raises when this run has none."""
        if not self._bao_space:
            raise ValueError(
                "no bao space wired for this run (runtime.get bao.space): "
                "memory lives in the bao space only — serve, or "
                "`anyrt run --from-space`")
        return self._bao_space

    def get_brain(self):
        """The brain object hosting agent_memory_items — the bao space's
        `bao/v1` bundle's `bao/brain/v1` child (ADR-017 §0), with the
        `agent_brain` type + datasets ensured lazily (guest-owned
        store). `{objectId}` — deterministic, no create race. Memory
        has ONE home: the bao space (`bao_space()`); there is no
        per-space brain, facts about a space go in `context`/`tags`."""
        space = self.bao_space()
        self._ensure_store(space, "agent_brain", "Agent Brain",
                           [_MEM_DATASET, _JOB_STATE_DATASET,
                            _ROI_DATASET])
        tid = self._resolve_type_or_raise(space, "agent_brain")
        return self.bundle_child(space, _BAO_BUNDLE, "bao/brain/v1", [tid])

    def collection(self, space, type_key, dataset_key):
        """The collection a type's dataset lives in (`<typeId>_<key>`),
        for callers that address the wire themselves — every any@v1
        call already takes the key. None when the type declares no
        such dataset."""
        tid = self._resolve_type_or_raise(space, type_key)
        return next((d.get("collection") for d in self._datasets_of(space, tid)
                     if d.get("key") == dataset_key), None)

    def create_memory(self, fields):
        """Create a memory item (category + context required) in the
        bao space's brain — memory's only home.

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
        brain = self.get_brain()["objectId"]
        ops = [{"type": "$set", "path": k, "value": v} for k, v in f.items()]
        return self.modify(self.bao_space(), {
            "objectId": brain, "dataset": "agent_memory_items",
            "records": [{"id": "", "upsert": True, "ops": ops}]})

    def evolve_memory(self, item_id, fields):
        """Evolve a memory item's mutable fields (author-only).

        modifiedAt is bumped by its stamp on apply. Mutable allow-list
        exactly `{salience, accessCount, confidence, importance,
        context, body, tags, edges}` — anything else (including
        source/provenance) raises; the declaration enforces the same
        for other identities."""
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
        self._mem_check_ranges(f)
        brain = self.get_brain()["objectId"]
        ops = [{"type": "$set", "path": k, "value": v} for k, v in f.items()]
        return self.modify(self.bao_space(), {
            "objectId": brain, "dataset": "agent_memory_items",
            "records": [{"id": item_id, "ops": ops}]})

    def delete_memory(self, item_id):
        """Delete a memory item by id (author-only — the dataset's
        deleteBy gate)."""
        brain = self.get_brain()["objectId"]
        return self.delete_records(self.bao_space(), brain,
                                   "agent_memory_items", [item_id])

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
        try:   # absent in a run without a bao space (memory is then off)
            bao = effect("runtime.get", {"key": "bao.space"})["value"]  # noqa: F821
        except Exception:  # noqa: BLE001 - the effect's KeyError, whatever its class
            bao = None
        _instance = _Client(base, bao)
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
def create_object(spaceConfig, body, create_options=True, parent=None, folder=None):
    return _c().create_object(_space(spaceConfig), body, create_options,
                              parent, folder)


@span(kind="mutator")  # noqa: F821 - guest global
def move_object(spaceConfig, object_id, parent, folder=None):
    return _c().move_object(_space(spaceConfig), object_id, parent, folder)


@span(kind="getter")  # noqa: F821 - guest global
def list_children(spaceConfig, parent=""):
    return _c().list_children(_space(spaceConfig), parent)


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
def list_properties(spaceConfig, type_key):
    return _c().list_properties(_space(spaceConfig), type_key)


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
def links(spaceConfig, object_id):
    return _c().links(_space(spaceConfig), object_id)


@span(kind="getter")  # noqa: F821 - guest global
def backlinks_everywhere(target_uri):
    return _c().backlinks_everywhere(target_uri)


@span(kind="getter")  # noqa: F821 - guest global
def list_apps(spaceConfig):
    return _c().list_apps(_space(spaceConfig))


@span(kind="getter")  # noqa: F821 - guest global
def list_available_apps(spaceConfig):
    return _c().list_available_apps(_space(spaceConfig))


@span(kind="mutator")  # noqa: F821 - guest global
def setup_app(spaceConfig, usecase):
    return _c().setup_app(_space(spaceConfig), usecase)


@span(kind="getter")  # noqa: F821 - guest global
def collection(spaceConfig, type_key, dataset_key):
    return _c().collection(_space(spaceConfig), type_key, dataset_key)


@span(kind="getter")  # noqa: F821 - guest global
def bao_space():
    return _c().bao_space()


@span(kind="getter")  # noqa: F821 - guest global
def get_brain():
    return _c().get_brain()


@span(kind="mutator")  # noqa: F821 - guest global
def create_memory(fields):
    return _c().create_memory(fields)


@span(kind="mutator")  # noqa: F821 - guest global
def evolve_memory(item_id, fields):
    return _c().evolve_memory(item_id, fields)


@span(kind="mutator")  # noqa: F821 - guest global
def delete_memory(item_id):
    return _c().delete_memory(item_id)


# lift the method docstrings onto the public functions — ONE authored
# copy (on _Client), rendered by describe()/help() from here
for _f in (create_object, move_object, list_children, update_object,
           delete_object, query_objects,
           list_programs, query, modify, upsert_record, upsert_records,
           delete_records, _list_datasets, _create_dataset, _remove_dataset,
           _add_dataset_field, _remove_dataset_field,
           aggregate, list_processes, cancel_process, list_devices,
           get_markdown, put_markdown, edit_markdown, list_files, file_content,
           list_search_scopes,
           append_markdown, list_spaces, get_space, general_chat,
           create_space, open_in_ui, list_types,
           list_properties, patch_property, set_option, remove_option,
           reorder_property, delete_property,
           attach_type, detach_type,
           create_type, add_property, append_turn, create_chunk,
           chat_send, search, backlinks, links, backlinks_everywhere,
           list_apps, list_available_apps, setup_app, collection,
           bao_space, get_brain, create_memory, evolve_memory, delete_memory):
    _f.__doc__ = getattr(_Client, _f.__name__.lstrip("_")).__doc__
del _f
