"""The memory WRITE facade over the brain (agent_memory_items).

Bind `m = memory(c)` (c = the any@v1 module), then save through
`m.save_with_dedup(candidate, rec)` (rec = recall@v1) — recall
supplies lookalikes, a fast-tier judge decides merge | supersede |
create. Raw `add` skips dedup; use only when the fact is known new.
Reads go through recall@v1 or the brain's dataset, not here."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# ADR-007 §1/§2 (any docs/11-agent-memory.md): search-before-save
# dedup with a classify-tier judge, no similarity threshold; the
# humble merge keeps machine re-sightings from blurring user-stated
# text or lowering confidence. accessCount bump = §4.3, ships ON.
# ADR-028 §4: supersede CLOSES the old item (validTo = new validFrom)
# instead of leaving two live facts — nothing is deleted.
# Policy only — all I/O flows through the injected any@v1 client,
# the recall@v1 object, and the llm@v1 chat.

import json
import re

# Post-create mutable fields (author-only evolve allow-list,
# docs/11-agent-memory.md) — the merge path evolves only these.
MUTABLE_FIELDS = ("salience", "accessCount", "confidence", "importance",
                  "context", "body", "tags", "edges", "validTo")

_ACTIONS = ("merge", "supersede", "create")
_MACHINE_SOURCES = ("extraction", "reflection")

JUDGE_TIER = "classify"

_JUDGE_SYSTEM = (
    "You deduplicate an agent's memory store. Given a CANDIDATE fact and "
    "EXISTING items, decide: merge (same fact — evolve the existing item), "
    "supersede (candidate replaces a now-outdated item), or create (new "
    "fact). Answer ONLY a JSON object: "
    '{"action": "merge"|"supersede"|"create", "mergedInto": "<existing id>"} '
    "(mergedInto required unless action is create).")


def _humble_merge(old, updates, machine):
    """ADR-007 §2 machine humility on the MERGE path (live-caught: an
    extraction re-sighting blurred a user-stated fact and dropped its
    confidence): machine-derived text never overwrites stored text, and
    a merge never LOWERS confidence — a duplicate sighting is
    corroboration, not doubt. Tags/edges union instead of replace."""
    if machine:
        updates.pop("context", None)
        updates.pop("body", None)
    if "confidence" in updates and isinstance(old.get("confidence"), (int, float)):
        updates["confidence"] = max(old["confidence"], updates["confidence"])
        if updates["confidence"] == old["confidence"]:
            del updates["confidence"]
    if "tags" in updates and old.get("tags"):
        updates["tags"] = [*old["tags"],
                           *(t for t in updates["tags"] if t not in old["tags"])]
    if "edges" in updates and old.get("edges"):
        seen = {(e.get("to"), e.get("type")) for e in old["edges"]}
        updates["edges"] = [*old["edges"],
                            *(e for e in updates["edges"]
                              if (e.get("to"), e.get("type")) not in seen)]
    return updates


def _require(fields, what):
    for key in ("category", "context"):
        v = fields.get(key)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{what} requires a non-empty {key!r}")


def _first_json(text):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"judge reply carried no JSON object: {text!r}")
    return json.loads(m.group(0))


class Memory:
    """Memory writes over THE brain — the bao space's (ADR-017 §0);
    memory has one home, so no space is bound. The server resolves the
    brain (deterministic derived id) — no object id param needed."""

    def __init__(self, client, llm_chat):
        self._c = client
        self._chat = llm_chat

    @span(kind="mutator")  # noqa: F821 - guest global
    def add(self, category, context, **fields):
        """Create an item unconditionally; prefer `save_with_dedup`.

        `category` (lowercase slug) + `context` (one-liner) are
        required (ADR-007 §1). Returns `{"itemId": id}` — recordIds[0]
        of the ModifyResult."""
        body = {"category": category, "context": context, **fields}
        _require(body, "memory item")
        reply = self._c.create_memory(body)
        return {"itemId": reply["recordIds"][0]}

    @span(kind="mutator")  # noqa: F821 - guest global
    def evolve(self, item_id, **fields):
        """Evolve mutable fields (author-only; server bumps modifiedAt).
        Fields outside the allow-list raise here — never silently sent."""
        unknown = sorted(set(fields) - set(MUTABLE_FIELDS))
        if unknown:
            raise ValueError(f"immutable/unknown fields {unknown}; "
                             f"mutable: {list(MUTABLE_FIELDS)}")
        if not fields:
            raise ValueError("evolve needs at least one field")
        self._c.evolve_memory(item_id, fields)
        return {"itemId": item_id}

    @span(kind="mutator")  # noqa: F821 - guest global
    def delete(self, item_id):
        """Delete a memory item (author-only)."""
        self._c.delete_memory(item_id)
        return {"itemId": item_id}

    @span(kind="mutator")  # noqa: F821 - guest global
    def bump_access(self, item_id, current_count):
        """accessCount = current + 1 on recall — the memory ROI signal.

        Tells extracted-but-never-recalled from earning-its-keep
        (ADR-007 §4.3; ships ON from day one)."""
        new = current_count + 1
        self.evolve(item_id, accessCount=new)
        return {"itemId": item_id, "accessCount": new}

    def _judge(self, candidate, hits, recall):
        """The ADR-007 §2 dedup judge: hydrate the recall hits, ask the
        classify tier same-fact?, return the verdict dict. No hydrated
        items short-circuits to create — no model call."""
        items = [r for _, r in recall.hydrate(hits)
                 if r.get("category") or r.get("context")]
        if not items:
            return {"action": "create"}
        existing = "\n".join(
            f"- id={r['id']} [{r.get('category', '?')}] {r.get('context', '')}"
            for r in items)
        prompt = (f"CANDIDATE: [{candidate.get('category')}] "
                  f"{candidate.get('context')}\n"
                  f"{candidate.get('body', '')}\n\nEXISTING:\n{existing}")
        reply = self._chat(
            [{"role": "user", "parts": [{"type": "text", "text": prompt}]}],
            system=_JUDGE_SYSTEM, tier=JUDGE_TIER, tools=[])
        verdict = _first_json(" ".join(
            p["text"] for p in reply["parts"] if p["type"] == "text"))
        if verdict.get("action") not in _ACTIONS:
            raise ValueError(f"judge returned unknown action: {verdict!r}")
        return verdict

    @span(kind="mutator")  # noqa: F821 - guest global
    def save_with_dedup(self, candidate, recall):
        """The default save path — dedup via recall + judge.

        `candidate`: item fields (`category` + `context` required,
        plus `body`, `tags`, `edges`, `confidence`, `source`,
        `validFrom`, …); `recall`: a recall@v1 object over the same
        space. Recall over scope `agent` supplies dedup candidates
        (live items only — closed ones never compete), the
        classify-tier judge decides merge | supersede | create
        (ADR-007 §2). Merge is a SUCCESS, not an error:
        `{"deduplicated": True, "mergedInto", "action"}`; otherwise
        `{"itemId", "action"}`. Supersede closes the old item —
        `validTo` = the new item's `validFrom` (ADR-028 §4)."""
        _require(candidate, "dedup candidate")
        hits = recall.search(candidate["context"], scopes=["agent"])
        verdict = self._judge(candidate, hits, recall)
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
            old = next((r for _, r in recall.hydrate(hits)
                        if r.get("id") == merged_into), None)
            if old is not None:
                updates = _humble_merge(old, updates,
                                        candidate.get("source") in _MACHINE_SOURCES)
            if updates:
                self.evolve(merged_into, **updates)
            return {"deduplicated": True, "mergedInto": merged_into, "action": "merge"}

        # supersede: new item carrying a `supersedes` edge to the old
        # one, which closes at the instant the new fact holds from
        fields = dict(candidate)
        fields["edges"] = [*(fields.get("edges") or []),
                           {"to": merged_into, "type": "supersedes"}]
        fields.setdefault("validFrom", instant(now()))  # noqa: F821 - guest globals
        out = {**self.add(**fields), "action": "supersede"}
        self.evolve(merged_into, validTo=fields["validFrom"])
        return out


@span(kind="setup")  # noqa: F821 - guest global
def memory(client, llm_chat=None):
    """Bind the memory facade to the brain — then `help(m)`.

    Memory lives in the bao space only (ADR-017 §0): no space to pick,
    a fact ABOUT a space goes in `context`/`tags`. The brain is
    server-resolved, no object id needed.

    `client` is an any@v1 client (create_memory/evolve_memory/
    delete_memory); the judge's model call defaults to llm@v1 chat
    and is injectable for tests."""
    if llm_chat is None:
        llm_chat = use("llm@v1").chat  # noqa: F821 - guest global
    return Memory(client, llm_chat)
