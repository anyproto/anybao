"""Recall over one space — one surface, three axes; read-only.

Semantic: `search` over the any index. Temporal: `by_period` merges
memory items, turns, chunks in a time range. Graph: `neighbors` walks
forward link properties plus server backlinks. Bind with
`recall(c, space, brain_object_id=…, chat_object_id=…)` (c = the
any@v1 module); `hydrate` turns search hits into full records in one
read."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# ADR-007 §5. The temporal sources live on different objects (memory
# items on the brain, turns/chunks on the chat object) — a None id
# just skips that source.

_any = use("any@v1")  # noqa: F821 - guest global

DEFAULT_SCOPES = ("agent", "history", "basic")

# Reserved property groups (`any`, `nav`) are structural, not user
# graph edges — neighbors skips them.
RESERVED_GROUPS = {"any", "nav"}

# by_period sorts the merged records by each source's natural time field.
_TS_FIELD = {"memory": "validFrom", "turn": "createdAt", "chunk": "periodStart"}


def _ts(rec):
    v = rec.get(_TS_FIELD[rec["source"]]) or rec.get("createdAt") or 0
    return v if isinstance(v, (int, float)) else 0


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

    # --- semantic ----------------------------------------------------------
    @span("recall.search", kind="getter")  # noqa: F821 - guest global
    def search(self, query, scopes=DEFAULT_SCOPES, limit=10):
        """Index search across scopes → a bare LIST of hits, no envelope.

        Each `{scope, objectId, dataset, recordId, score, …}` —
        already unwrapped from the `{hits, mode, vectorStatus}`
        envelope that any@v1 `search` returns, so `hits[0]` works and
        `hits["hits"]` does not. Default scopes ("agent", "history",
        "basic"), limit 10."""
        reply = self._c.search(self._space, query, scopes=list(scopes), limit=limit)
        return reply.get("hits") or []

    @span("recall.hydrate", kind="getter")  # noqa: F821 - guest global
    def hydrate(self, hits):
        """Hit pointers → (hit, record) pairs, in one batched read.

        Hit order kept, missing records dropped; one `$in` query per
        (object, dataset). The shared step under auto-recall rendering
        and the dedup judge."""
        wanted = {}
        for h in hits:
            wanted.setdefault((h["objectId"], h["dataset"]), []).append(h["recordId"])
        recs = {}
        for (obj, ds), ids in wanted.items():
            for r in self._c.query(self._space, obj, ds, filter={"id": {"$in": ids}}):
                recs[(ds, r.get("id"))] = r
        pairs = [(h, recs.get((h["dataset"], h["recordId"]))) for h in hits]
        return [(h, r) for h, r in pairs if r is not None]

    # --- temporal ----------------------------------------------------------
    @span("recall.by_period", kind="getter")  # noqa: F821 - guest global
    def by_period(self, from_ts, to_ts):
        """Everything in [from_ts, to_ts] (unix seconds, inclusive).

        Memory items by validFrom, turns by createdAt, chunks by
        period overlap. Merged, time-sorted, each record tagged with
        `source` ∈ memory/turn/chunk. The memory source self-resolves
        via get_brain when the binder got no brain_object_id; raises
        when NO source is bound (a silent [] read as "nothing
        happened that week" — seen live, run_7318bebb61af44b0)."""
        if self._brain is None:
            try:
                self._brain = (self._c.get_brain(self._space) or {}).get("objectId")
            except Exception:  # no agent data in this space — source stays off
                self._brain = ""
        if not self._brain and not self._chat:
            raise ValueError(
                "by_period has no sources: no brain object in this space and "
                "no chat_object_id bound — bind with recall(c, space, "
                "chat_object_id=...) (a spaceConfig mapping with chatId "
                "binds it automatically)")
        out = []
        if self._brain:
            items = self._c.query(
                self._space, self._brain, "agent_memory_items",
                filter={"validFrom": {"$gte": from_ts, "$lte": to_ts}},
                sort=["validFrom"])
            out += [{**r, "source": "memory"} for r in items]
        if self._chat:
            turns = self._c.query(
                self._space, self._chat, "agent_turns",
                filter={"createdAt": {"$gte": from_ts, "$lte": to_ts}},
                sort=["createdAt"])
            out += [{**r, "source": "turn"} for r in turns]
            chunks = self._c.query(
                self._space, self._chat, "agent_chunks",
                filter={"periodStart": {"$lte": to_ts}, "periodEnd": {"$gte": from_ts}},
                sort=["periodStart"])
            out += [{**r, "source": "chunk"} for r in chunks]
        out.sort(key=_ts)
        return out

    # --- graph -------------------------------------------------------------
    @span("recall.neighbors", kind="getter")  # noqa: F821 - guest global
    def neighbors(self, object_id):
        """1-hop neighborhood: {"forward": [...], "backlinks": [...]}.

        Forward = links-format property values on the object's row
        (arrays of `any://<objectId>` URIs; edge label = property,
        targetId a bare object id); each `{type, prop, targetId}`.
        Backlinks = objects that reference it, from the server's
        reverse read (`…/backlinks`); each `{sourceId, type, prop}`.
        type/prop are xKeys — content ids never surface here."""
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
        try:
            raw = self._c.backlinks(self._space, object_id)
        except _any.AnyError as e:
            if e.code != "request.not_found":  # route absent = pre-backlinks server
                raise
            raw = []
        backlinks = [{"sourceId": b["objectId"], "type": b["type"],
                      "prop": b["prop"]} for b in raw]
        return {"forward": forward, "backlinks": backlinks}

    def _link_props(self, type_id):
        """{propId → prop xKey} for the type's links-format properties
        (the object-reference convention, docs/03-api.md § Backlinks).
        A group key that isn't a queryable type (e.g. a dataset
        artifact) just yields no edges rather than failing the whole
        read — list_properties raises ValueError on unknown keys."""
        try:
            props = self._c.list_properties(self._space, type_id)
        except (_any.AnyError, ValueError):
            return {}
        return {p["id"]: p.get("xKey") or p.get("name") or p["id"]
                for p in props
                if (p.get("format") or {}).get("type") == "links"}


@span("recall.recall", kind="setup")  # noqa: F821 - guest global
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
