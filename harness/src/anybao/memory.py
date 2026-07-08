"""memory — the M5 memory write facade (ADR-007 §1, §2).

Write-side sibling of Recall: add / evolve / delete over the brain's
`agent_memory_items` (any docs/11-agent-memory.md), the recall-side
accessCount bump (§4.3, ships ON), and `save_with_dedup` — the §2
search-before-save path. The dedup JUDGE and the recall used for
candidate retrieval are INJECTED (like the loop's llm transport): no
LLM call happens here, and there is no similarity threshold — the
judge decides merge | supersede | create.
"""

from __future__ import annotations

from collections.abc import Callable

from anybao.anyclient import AnyClient

# Post-create mutable fields (author-only evolve allow-list,
# docs/11-agent-memory.md) — the merge path evolves only these.
MUTABLE_FIELDS = ("salience", "accessCount", "confidence", "importance",
                  "context", "body", "tags", "edges")

_ACTIONS = ("merge", "supersede", "create")


def _require(fields: dict, what: str) -> None:
    for key in ("category", "context"):
        v = fields.get(key)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{what} requires a non-empty {key!r}")


class Memory:
    """Memory writes over one space's brain object. The server resolves
    the brain (deterministic derived id) — no object id param needed."""

    def __init__(self, client: AnyClient, space_id: str):
        self._c = client
        self._space = space_id

    def add(self, category: str, context: str, **fields) -> dict:
        """Create a memory item; `category` (lowercase slug) + `context`
        (one-liner) are required (ADR-007 §1). Returns `{"itemId": id}`
        — recordIds[0] of the ModifyResult."""
        body = {"category": category, "context": context, **fields}
        _require(body, "memory item")
        reply = self._c.create_memory(self._space, body)
        return {"itemId": reply["recordIds"][0]}

    def evolve(self, item_id: str, **fields) -> dict:
        """Evolve mutable fields (author-only; server bumps modifiedAt).
        Fields outside the allow-list raise here — never silently sent."""
        unknown = sorted(set(fields) - set(MUTABLE_FIELDS))
        if unknown:
            raise ValueError(f"immutable/unknown fields {unknown}; "
                             f"mutable: {list(MUTABLE_FIELDS)}")
        if not fields:
            raise ValueError("evolve needs at least one field")
        self._c.evolve_memory(self._space, item_id, fields)
        return {"itemId": item_id}

    def delete(self, item_id: str) -> dict:
        """Delete a memory item (author-only)."""
        self._c.delete_memory(self._space, item_id)
        return {"itemId": item_id}

    def bump_access(self, item_id: str, current_count: int) -> dict:
        """accessCount = current + 1 on recall — the ROI signal that
        tells extracted-but-never-recalled from earning-its-keep
        (ADR-007 §4.3; ships ON from day one)."""
        new = current_count + 1
        self.evolve(item_id, accessCount=new)
        return {"itemId": item_id, "accessCount": new}

    def save_with_dedup(self, candidate: dict, *,
                        recall, judge: Callable[[dict, list[dict]], dict]) -> dict:
        """The ADR-007 §2 save path: recall over scope `agent` supplies
        dedup candidates, the injected judge (classify-tier, wired later
        as an effect) returns `{"action": merge|supersede|create,
        "mergedInto"?: itemId}`. Merge is a SUCCESS, not an error:
        `{"deduplicated": True, "mergedInto", "action"}`."""
        _require(candidate, "dedup candidate")
        hits = recall.search(candidate["context"], scopes=["agent"])
        verdict = judge(candidate, hits)
        action = verdict.get("action")
        if action not in _ACTIONS:
            raise ValueError(f"judge returned unknown action {action!r}; "
                             f"expected one of {list(_ACTIONS)}")

        if action == "create":
            return {**self.add(**candidate), "action": "create"}

        merged_into = verdict.get("mergedInto")
        if not merged_into:
            raise ValueError(f"judge action {action!r} needs 'mergedInto' "
                             "(the existing item it matched)")

        if action == "merge":
            updates = {k: candidate[k] for k in MUTABLE_FIELDS if k in candidate}
            self.evolve(merged_into, **updates)
            return {"deduplicated": True, "mergedInto": merged_into, "action": "merge"}

        # supersede: new item carrying a `supersedes` edge to the old one
        fields = dict(candidate)
        fields["edges"] = [*(fields.get("edges") or []),
                           {"to": merged_into, "type": "supersedes"}]
        return {**self.add(**fields), "action": "supersede"}
