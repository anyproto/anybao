"""memory — the M5 memory write facade (ADR-007 §1, §2).

Write-side sibling of Recall: add / evolve / delete over the brain's
`agent_memory_items` (any docs/11-agent-memory.md), the recall-side
accessCount bump (§4.3, ships ON), and `save_with_dedup` — the §2
search-before-save path. The dedup JUDGE and the recall used for
candidate retrieval are INJECTED (like the loop's llm transport): no
LLM call happens here, and there is no similarity threshold — the
judge decides merge | supersede | create.

`register_memory_effects` is the promised judge-as-effect wiring: the
`memory.*` effects guest programs (extraction, the agent's addMemory
tool) call; `memory.save_with_dedup` runs the classify-tier judge via
a NESTED broker call, so the judge's llm.chat records in the trace.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from anyrt.effects import Registry, effect

from anybao.anyclient import AnyClient
from anybao.recall import Recall
from anybao.triggers import Trigger

# Post-create mutable fields (author-only evolve allow-list,
# docs/11-agent-memory.md) — the merge path evolves only these.
MUTABLE_FIELDS = ("salience", "accessCount", "confidence", "importance",
                  "context", "body", "tags", "edges")

_ACTIONS = ("merge", "supersede", "create")
_MACHINE_SOURCES = ("extraction", "reflection")


def _humble_merge(old: dict, updates: dict, *, machine: bool) -> dict:
    """§1b machine humility on the MERGE path (live-caught: an
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
            old = next((r for _, r in recall.hydrate(hits)
                        if r.get("id") == merged_into), None)
            if old is not None:
                updates = _humble_merge(old, updates,
                                        machine=candidate.get("source")
                                        in _MACHINE_SOURCES)
            if updates:
                self.evolve(merged_into, **updates)
            return {"deduplicated": True, "mergedInto": merged_into, "action": "merge"}

        # supersede: new item carrying a `supersedes` edge to the old one
        fields = dict(candidate)
        fields["edges"] = [*(fields.get("edges") or []),
                           {"to": merged_into, "type": "supersedes"}]
        return {**self.add(**fields), "action": "supersede"}


# --- background cognition triggers (ADR-007 §1b / §4.1) ----------------------

def extraction_trigger(*, space: str, chat_id: str, brain_id: str,
                       owner: str = "", every_s: int = 900,
                       trigger_id: str = "extraction") -> Trigger:
    """programs/extraction@v1 — batched sweep over newly persisted
    turns; cursor lives on the brain (agent_job_state)."""
    return Trigger(id=trigger_id, name="memory extraction", kind="cron",
                   spec={"every_s": every_s}, program="extraction@v1",
                   args={"space": space, "chatId": chat_id, "brainId": brain_id},
                   owner=owner)


def linkgen_trigger(*, space: str, brain_id: str, owner: str = "",
                    every_s: int = 3600, trigger_id: str = "linkgen") -> Trigger:
    """programs/linkgen@v1 — the HOURLY link sweep (§4 resolved Q2)."""
    return Trigger(id=trigger_id, name="memory link generation", kind="cron",
                   spec={"every_s": every_s}, program="linkgen@v1",
                   args={"space": space, "brainId": brain_id}, owner=owner)


# --- §4.2 mechanisms: present, DISABLED, each behind an eval ------------------
# Flip enabled=True only after the matching eval passes (test_evals_gated
# mechanisms are the offline gate; live-quality runs extend them). The
# amemory lesson stands: these were implemented-but-disabled there too,
# and never earned activation — evals decide, not enthusiasm.

def decay_trigger(*, space: str, brain_id: str, owner: str = "",
                  every_s: int = 86400, trigger_id: str = "decay") -> Trigger:
    return Trigger(id=trigger_id, name="salience decay", kind="cron",
                   spec={"every_s": every_s}, program="decay@v1",
                   args={"space": space, "brainId": brain_id}, owner=owner,
                   enabled=False)


def reflection_trigger(*, space: str, brain_id: str, owner: str = "",
                       every_s: int = 86400, trigger_id: str = "reflection") -> Trigger:
    return Trigger(id=trigger_id, name="reflection", kind="cron",
                   spec={"every_s": every_s}, program="reflection@v1",
                   args={"space": space, "brainId": brain_id}, owner=owner,
                   enabled=False)


def evolution_trigger(*, space: str, brain_id: str, owner: str = "",
                      every_s: int = 21600, trigger_id: str = "evolution") -> Trigger:
    return Trigger(id=trigger_id, name="memory evolution", kind="cron",
                   spec={"every_s": every_s}, program="evolution@v1",
                   args={"space": space, "brainId": brain_id}, owner=owner,
                   enabled=False)


# --- the §2 judge + effect wiring --------------------------------------------

_JUDGE_SYSTEM = (
    "You deduplicate an agent's memory store. Given a CANDIDATE fact and "
    "EXISTING items, decide: merge (same fact — evolve the existing item), "
    "supersede (candidate replaces a now-outdated item), or create (new "
    "fact). Answer ONLY a JSON object: "
    '{"action": "merge"|"supersede"|"create", "mergedInto": "<existing id>"} '
    "(mergedInto required unless action is create).")


def _first_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"judge reply carried no JSON object: {text!r}")
    return json.loads(m.group(0))


def llm_judge(llm_call: Callable[[dict], dict], recall: Recall,
              tier: str = "classify") -> Callable[[dict, list[dict]], dict]:
    """The ADR-007 §2 dedup judge: hydrate the recall hits, ask the
    classify tier same-fact?, return the verdict dict. `llm_call` is
    payload -> llm.chat reply (broker-bound in production)."""

    def judge(candidate: dict, hits: list[dict]) -> dict:
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
        reply = llm_call({
            "messages": [{"role": "user", "parts": [{"type": "text", "text": prompt}]}],
            "system": _JUDGE_SYSTEM, "tier": tier, "tools": []})
        verdict = _first_json(" ".join(
            p["text"] for p in reply["parts"] if p["type"] == "text"))
        if verdict.get("action") not in _ACTIONS:
            raise ValueError(f"judge returned unknown action: {verdict!r}")
        return verdict

    return judge


def register_memory_effects(registry: Registry, client: AnyClient, *,
                            space: str, judge_tier: str = "classify") -> None:
    """`memory.*` effects over one space's brain. Reads happen through
    recall/`any.query`; these are the writes (cap memory.write)."""
    mem = Memory(client, space)
    rec = Recall(client, space)

    @effect("memory.add", kind="mutate", registry=registry, cap="memory.write")
    def memory_add(ctx, category, context, **fields):
        return mem.add(category, context, **fields)

    @effect("memory.evolve", kind="mutate", registry=registry, cap="memory.write")
    def memory_evolve(ctx, item_id, **fields):
        return mem.evolve(item_id, **fields)

    @effect("memory.delete", kind="mutate", registry=registry, cap="memory.write")
    def memory_delete(ctx, item_id):
        return mem.delete(item_id)

    @effect("memory.bump_access", kind="mutate", registry=registry, cap="memory.write")
    def memory_bump_access(ctx, item_id, current_count=0):
        """§4.3 bump for the agent's EXPLICIT recall path (auto-recall
        bumps host-side; a deliberate dig bumps through this)."""
        return mem.bump_access(item_id, current_count)

    @effect("memory.save_with_dedup", kind="mutate", registry=registry,
            cap="memory.write")
    def memory_save_with_dedup(ctx, candidate):
        judge = llm_judge(lambda payload: ctx.call("llm.chat", payload),
                          rec, tier=judge_tier)
        return mem.save_with_dedup(candidate, recall=rec, judge=judge)
