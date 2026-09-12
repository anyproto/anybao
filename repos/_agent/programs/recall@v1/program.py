"""Recall over one space — one surface, three axes; read-only.

Semantic: `search` over the any index. Temporal: `by_period` merges
memory items, turns, chunks in a time range. Graph: `neighbors` walks
forward link properties plus server backlinks. Bind with
`recall(c, space, brain_object_id=…, chat_object_id=…)` (c = the
any@v1 module); `hydrate` turns search hits into full records in one
read."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# ADR-007 §5. The temporal sources live on different objects (memory
# items on the brain — the bao space's, memory's only home (ADR-017
# §0) — turns/chunks on the bound space's chat object); a None id
# just skips that source. ADR-028 §4: a memory item with `validTo` is
# closed (superseded) — every read here drops it unless asked.

_any = use("any@v1")  # noqa: F821 - guest global

DEFAULT_SCOPES = ("agent", "history", "basic", "email")

# Reserved property groups (`any`, the hidden built-ins' groups) are
# structural, not user graph edges — neighbors skips them.
RESERVED_GROUPS = {"any", "page", "miniapp", "bin", "dataview"}

# by_period sorts the merged records by each source's natural time field.
_TS_FIELD = {"memory": "validFrom", "turn": "createdAt", "chunk": "periodStart"}


def _ts(rec):
    v = rec.get(_TS_FIELD[rec["source"]]) or rec.get("createdAt")
    return ts_s(v) or 0  # noqa: F821 - guest global (ADR-019 §1)


class Recall:
    """Recall over one space. The temporal datasets live on different
    objects — memory items on the brain object, turns/chunks on the
    chat object — so both ids are constructor params; a None id skips
    that source in `by_period`."""

    def __init__(self, client, space, brain_object_id=None, chat_object_id=None):
        self._c = client
        self._space = space
        self._brain = brain_object_id
        self._chat = chat_object_id
        self._log = None   # the chat's log child, resolved lazily

    # --- semantic ----------------------------------------------------------
    @span(kind="getter")  # noqa: F821 - guest global
    def search(self, query, scopes=DEFAULT_SCOPES, limit=10):
        """Index search across scopes → a bare LIST of hits, no envelope.

        query must be non-empty — the index has no browse-all mode; to
        ENUMERATE memory, query the brain dataset instead (see the
        error text below). Each hit `{scope, objectId, dataset,
        recordId, score, …}` — already unwrapped from the `{hits,
        mode, vectorStatus}` envelope that any@v1 `search` returns, so
        `hits[0]` works and `hits["hits"]` does not. Default scopes
        ("agent", "history", "basic", "email"), limit 10."""
        if not (query or "").strip():
            raise ValueError(
                "search needs a non-empty query — the index has no "
                "browse-all mode. To enumerate memory items: "
                'a.query(a.bao_space(), a.get_brain()["objectId"], '
                '"agent_memory_items"); for history use '
                "history@v1 or by_period().")
        reply = self._c.search(self._space, query, scopes=list(scopes), limit=limit)
        return reply.get("hits") or []

    @span(kind="getter")  # noqa: F821 - guest global
    def hydrate(self, hits, include_expired=False):
        """Hit pointers → (hit, record) pairs, in one batched read.

        Hit order kept, missing records dropped; one `$in` query per
        (object, dataset). Closed memory items (`validTo` set —
        superseded, ADR-028 §4) are dropped too unless
        `include_expired=True` (history questions). The shared step
        under auto-recall rendering and the dedup judge."""
        wanted = {}
        for h in hits:
            wanted.setdefault((h["objectId"], h["dataset"]), []).append(h["recordId"])
        recs = {}
        for (obj, ds), ids in wanted.items():
            for r in self._c.query(self._space, obj, ds, filter={"id": {"$in": ids}}):
                recs[(ds, r.get("id"))] = r
        pairs = [(h, recs.get((h["dataset"], h["recordId"]))) for h in hits]
        return [(h, r) for h, r in pairs
                if r is not None and (include_expired or not r.get("validTo"))]

    # --- temporal ----------------------------------------------------------
    @span(kind="getter")  # noqa: F821 - guest global
    def by_period(self, from_ts, to_ts, include_expired=False):
        """Everything in [from_ts, to_ts] (unix seconds or ISO strings,
        inclusive).

        Memory items by validFrom, turns by createdAt, chunks by
        period overlap — one instant range scan per source (ADR-019
        §3; the bounds go through instant()). Merged, time-sorted (the
        records' own instants via ts_s), each record tagged with
        `source` ∈ memory/turn/chunk. Closed memory items (`validTo`
        set) are dropped unless `include_expired=True` — "what did I
        believe then" wants them, "what holds" does not. The memory
        source self-resolves
        via get_brain when the binder got no brain_object_id; raises
        when NO source is bound (a silent [] read as "nothing
        happened that week" — seen live, run_7318bebb61af44b0)."""
        if self._brain is None:
            try:   # the brain is the bao space's (ADR-017 §0)
                self._brain = (self._c.get_brain() or {}).get("objectId")
            except Exception:  # no bao space wired — source stays off
                self._brain = ""
        if not self._brain and not self._chat:
            raise ValueError(
                "by_period has no sources: no brain object in this space and "
                "no chat_object_id bound — bind with recall(c, space, "
                "chat_object_id=...) (a spaceConfig mapping with chatId "
                "binds it automatically)")
        out = []
        lo, hi = instant(from_ts), instant(to_ts)  # noqa: F821 - guest globals
        if self._brain:
            items = self._c.query(
                self._c.bao_space(), self._brain, "agent_memory_items",
                filter={"validFrom": {"$gte": lo, "$lte": hi}},
                sort=["validFrom"])
            out += [{**r, "source": "memory"} for r in items
                    if include_expired or not r.get("validTo")]
        if self._chat:
            if self._log is None:
                # turns/chunks live on the chat's log child (ADR-017)
                self._log = (self._c.chat_log(self._space, self._chat)
                             or {}).get("objectId")
            turns = self._c.query(
                self._space, self._log, "agent_turns",
                filter={"createdAt": {"$gte": lo, "$lte": hi}},
                sort=["createdAt"])
            out += [{**r, "source": "turn"} for r in turns]
            chunks = self._c.query(
                self._space, self._log, "agent_chunks",
                filter={"periodStart": {"$lte": hi}, "periodEnd": {"$gte": lo}},
                sort=["periodStart"])
            out += [{**r, "source": "chunk"} for r in chunks]
        out.sort(key=_ts)
        return out

    # --- graph -------------------------------------------------------------
    @span(kind="getter")  # noqa: F821 - guest global
    def neighbors(self, object_id):
        """1-hop neighborhood: {"forward": [...], "backlinks": [...]}.

        Forward = relation property values on the object's row
        (arrays of `any://…` URIs; edge label = property, targetId a
        bare object id); each `{type, prop, targetId}`. Backlinks =
        the server's reverse read (`…/backlinks`), one per edge:
        `{sourceId, kind}` plus `type` + `prop` for a relation edge
        (kind "relation") or `dataset` (+ `recordId`) for a block/
        message/record edge — index `type`/`prop` only after checking
        `kind`. type/prop are xKeys — content ids never surface here."""
        forward = []
        # normalize=False: graph edges are identified by raw type/prop ids
        # on the wire (ADR-006 §6); resolved to xKeys before returning.
        rows = self._c.query_objects(self._space, filter={"id": object_id},
                                     limit=1, normalize=False)
        if rows:
            try:
                type_xkey = {t.get("id"): t.get("xKey") or t.get("id")
                             for t in self._c.list_types(self._space)}
            except _any.AnyError:   # catalog unavailable — raw ids degrade
                type_xkey = {}
            for type_id, group in rows[0].items():
                if not isinstance(group, dict) or type_id in RESERVED_GROUPS:
                    continue
                link_props = self._link_props(type_id)
                for prop_id, value in group.items():
                    if prop_id not in link_props:
                        continue
                    targets = value if isinstance(value, list) else [value]
                    forward += [{"type": type_xkey.get(type_id, type_id),
                                 "prop": link_props[prop_id],
                                 "targetId": t.removeprefix("any://")}
                                for t in targets if isinstance(t, str) and t]
        # what links here: the link index's edges at the object itself
        # (ADR-027 §5) — a relation value names "type.prop", a block or
        # message names its collection
        raw = self._c.backlinks(self._space, object_id)
        backlinks = []
        for b in raw.get("object") or []:
            edge = {"sourceId": b["objectId"], "kind": b.get("kind")}
            if b.get("prop"):
                edge["type"], _, edge["prop"] = b["prop"].partition(".")
            elif b.get("key"):
                edge["dataset"] = b["key"]
            backlinks.append(edge)
        return {"forward": forward, "backlinks": backlinks}

    def _link_props(self, type_id):
        """{propId → prop xKey} for the type's relation properties (the
        object-reference descriptor, ADR-027 §4). A group key that
        isn't a queryable type (e.g. a dataset artifact) just yields no
        edges rather than failing the whole read — list_properties
        raises ValueError on unknown keys."""
        try:
            props = self._c.list_properties(self._space, type_id)
        except (_any.AnyError, ValueError):
            return {}
        return {p["id"]: p.get("xKey") or p.get("name") or p["id"]
                for p in props
                if ((p.get("xFormat") or {}).get("type") == "relation")}


@span(kind="setup")  # noqa: F821 - guest global
def recall(client, space, brain_object_id=None, chat_object_id=None):
    """Bind recall to one space over an any@v1 client — then `help(r)`.

    A bare bind is fully functional: `chat_object_id` defaults from a
    spaceConfig mapping's `chatId` (e.g. `baoSpaceConfig`), and
    `by_period` resolves the brain object itself on first use.
    Explicit ids override; a source that stays unresolved is skipped."""
    if chat_object_id is None and isinstance(space, dict):
        chat_object_id = space.get("chatId")
    return Recall(client, space, brain_object_id=brain_object_id,
                  chat_object_id=chat_object_id)
