"""recall — the M5 recall primitives: one surface, three axes (ADR-007 §5).

Semantic (`search` over the any index), temporal (`by_period` fan-out
across memory items / turns / chunks), graph (`neighbors` — forward
links-format property refs + the server's `/backlinks` reverse read,
plan §4c). All reads go through the injected AnyClient; nothing here
writes.
"""

from __future__ import annotations

from anybao.anyclient import AnyClient, AnyError

DEFAULT_SCOPES = ("agent", "history", "basic")

# Reserved property groups (`any`, `nav`) are structural, not user
# graph edges — neighbors skips them.
RESERVED_GROUPS = {"any", "nav"}

# by_period sorts the merged records by each source's natural time field.
_TS_FIELD = {"memory": "validFrom", "turn": "createdAt", "chunk": "periodStart"}


def _ts(rec: dict) -> float:
    v = rec.get(_TS_FIELD[rec["source"]]) or rec.get("createdAt") or 0
    return v if isinstance(v, (int, float)) else 0


class Recall:
    """Recall over one space. The temporal datasets live on different
    objects — memory items on the brain object, turns/chunks on the chat
    object — so both ids are constructor params; a None id skips that
    source in `by_period`."""

    def __init__(self, client: AnyClient, space_id: str, *,
                 brain_object_id: str | None = None,
                 chat_object_id: str | None = None):
        self._c = client
        self._space = space_id
        self._brain = brain_object_id
        self._chat = chat_object_id

    # --- semantic ----------------------------------------------------------
    def search(self, query: str, scopes: tuple[str, ...] | list[str] = DEFAULT_SCOPES,
               limit: int = 10) -> list[dict]:
        """Index search across scopes; returns the server hits
        (`{scope, objectId, dataset, recordId, score, …}`) unwrapped from
        the `{hits, mode, vectorStatus}` envelope."""
        reply = self._c.search(self._space, query, scopes=list(scopes), limit=limit)
        return reply.get("hits") or []

    def hydrate(self, hits: list[dict]) -> list[tuple[dict, dict]]:
        """Hit pointers → (hit, record) pairs (hit order kept, missing
        dropped), one `$in` query per (object, dataset). The shared step
        under auto-recall rendering and the dedup judge."""
        wanted: dict[tuple[str, str], list[str]] = {}
        for h in hits:
            wanted.setdefault((h["objectId"], h["dataset"]), []).append(h["recordId"])
        recs: dict[tuple[str, str], dict] = {}
        for (obj, ds), ids in wanted.items():
            for r in self._c.query(self._space, obj, ds, filter={"id": {"$in": ids}}):
                recs[(ds, r.get("id"))] = r
        pairs = [(h, recs.get((h["dataset"], h["recordId"]))) for h in hits]
        return [(h, r) for h, r in pairs if r is not None]

    # --- temporal ----------------------------------------------------------
    def by_period(self, from_ts: int, to_ts: int) -> list[dict]:
        """Everything that happened in [from_ts, to_ts] (unix seconds,
        inclusive): memory items by validFrom, turns by createdAt, chunks
        by period overlap. Merged, time-sorted, each record tagged with
        `source` ∈ memory/turn/chunk."""
        out: list[dict] = []
        if self._brain:
            items = self._c.query(
                self._space, self._brain, "agent_memory_items",
                filter={"validFrom": {"$gte": from_ts, "$lte": to_ts}}, sort=["validFrom"])
            out += [{**r, "source": "memory"} for r in items]
        if self._chat:
            turns = self._c.query(
                self._space, self._chat, "agent_turns",
                filter={"createdAt": {"$gte": from_ts, "$lte": to_ts}}, sort=["createdAt"])
            out += [{**r, "source": "turn"} for r in turns]
            chunks = self._c.query(
                self._space, self._chat, "agent_chunks",
                filter={"periodStart": {"$lte": to_ts}, "periodEnd": {"$gte": from_ts}},
                sort=["periodStart"])
            out += [{**r, "source": "chunk"} for r in chunks]
        out.sort(key=_ts)
        return out

    # --- graph -------------------------------------------------------------
    def neighbors(self, object_id: str) -> dict:
        """1-hop graph neighbors of an object. Forward = links-format
        property values on its row (arrays of `any://<objectId>` URIs;
        edge label = property, targetId returned as a bare object id).
        Backlinks = objects that reference it, from the server's reverse
        read (`…/backlinks`); each `{sourceId, typeId, propId}`."""
        forward: list[dict] = []
        rows = self._c.query_objects(self._space, filter={"id": object_id}, limit=1)
        if rows:
            for type_id, group in rows[0].items():
                if not isinstance(group, dict) or type_id in RESERVED_GROUPS:
                    continue
                link_props = self._link_props(type_id)
                for prop_id, value in group.items():
                    if prop_id not in link_props:
                        continue
                    targets = value if isinstance(value, list) else [value]
                    forward += [{"typeId": type_id, "propId": prop_id,
                                 "propName": link_props[prop_id],
                                 "targetId": t.removeprefix("any://")}
                                for t in targets if isinstance(t, str) and t]
        try:
            raw = self._c.backlinks(self._space, object_id)
        except AnyError as e:
            if e.code != "request.not_found":  # route absent = pre-backlinks server
                raise
            raw = []
        backlinks = [{"sourceId": b["objectId"], "typeId": b["typeId"], "propId": b["propId"]}
                     for b in raw]
        return {"forward": forward, "backlinks": backlinks}

    def _link_props(self, type_id: str) -> dict[str, str]:
        """{propId → name} for the type's links-format properties (the
        object-reference convention, docs/03-api.md § Backlinks). A group
        key that isn't a queryable type (e.g. a dataset artifact) just
        yields no edges rather than failing the whole read."""
        try:
            props = self._c.list_properties(self._space, type_id)
        except AnyError:
            return {}
        return {p["id"]: p.get("name", p["id"]) for p in props
                if (p.get("format") or {}).get("type") == "links"}
