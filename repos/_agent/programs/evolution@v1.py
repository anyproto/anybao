"""Neighbor context/keyword refresh for memory items (cron).

(ADR-007 §4.2,
A-MEM's evolve step): mechanism present, trigger ships DISABLED —
activation is gated on its eval.

For items linked since the last sweep (edges present, modifiedAt past
the cursor), show the LLM the item plus its edge neighbors and let it
refresh ONLY `context` and `tags` (the mutable descriptive fields —
never category/body/confidence from here). No-change replies are
skipped. Cursor = agent_job_state record on the brain object. args:
{space, batch?, tier?} — the brain resolves itself (ADR-017).
"""

import json

BATCH = 20
TIER = "classify"
STATE_DATASET = "agent_job_state"
STATE_ID = "evolution"

_SYSTEM = (
    "You refresh a memory item's one-line context and tags now that it is "
    "linked to neighbors. Keep the fact identical — only sharpen wording "
    "and tags using the neighbor context. Reply ONLY JSON: "
    '{"context": "<one-line>", "tags": ["..."]} or null if no improvement.')


def _first_json(text):
    text = text.strip()
    if text.lower().startswith("null"):
        return None
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"evolution reply carried no JSON object: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def _state(c, space, brain):
    rows = c.query(space, brain, STATE_DATASET,
                   filter={"id": STATE_ID}, limit=1)
    # newest swept modifiedAt instant, verbatim (ADR-019 §2)
    return instant(rows[0].get("lastModifiedAt") or 0) if rows else instant(0)  # noqa: F821


def _save_state(c, space, brain, last_ts):
    c.upsert_record(space, brain, STATE_DATASET, STATE_ID,
                    {"lastModifiedAt": last_ts})


def refresh(item, neighbors, tier):
    lines = [f"- [{n.get('category', '?')}] {n.get('context', '')}"
             for n in neighbors]
    prompt = (f"ITEM: [{item.get('category')}] {item.get('context')}\n"
              f"tags: {item.get('tags') or []}\n\nNEIGHBORS:\n" + "\n".join(lines))
    reply = use("llm@v1").chat(  # noqa: F821 - guest global
        [{"role": "user", "parts": [{"type": "text", "text": prompt}]}],
        system=_SYSTEM, tier=tier, tools=[])
    return _first_json(" ".join(p["text"] for p in reply["parts"]
                                if p["type"] == "text"))


def main(args):
    space = args["space"]
    tier = args.get("tier", TIER)
    c = use("any@v1")  # noqa: F821 - guest global
    brain = c.get_brain()["objectId"]   # the bao space's (ADR-017 §0)
    last = _state(c, space, brain)
    items = c.query(space, brain, "agent_memory_items",
                    filter={"modifiedAt": {"$gt": last}}, sort=["modifiedAt"],
                    limit=args.get("batch", BATCH))
    linked = [i for i in items if i.get("edges")]
    if not items:
        return {"swept": 0, "refreshed": 0, "errors": 0}

    mem = use("memory@v1").memory(c)  # noqa: F821 - guest global
    by_id = {i["id"]: i for i in items}
    refreshed = errors = 0
    for item in linked:
        neighbor_ids = [e.get("to") for e in item["edges"] if e.get("to")]
        neighbors = [by_id[n] for n in neighbor_ids if n in by_id]
        if not neighbors:
            hydrated = c.query(space, brain, "agent_memory_items",
                               filter={"id": {"$in": neighbor_ids}})
            neighbors = list(hydrated)
        if not neighbors:
            continue
        try:
            update = refresh(item, neighbors, tier)
        except Exception:
            errors += 1
            continue
        if not update:
            continue
        fields = {}
        if (update.get("context") or "").strip() and \
                update["context"].strip() != item.get("context"):
            fields["context"] = update["context"].strip()
        if isinstance(update.get("tags"), list) and \
                update["tags"] != (item.get("tags") or []):
            fields["tags"] = [str(t) for t in update["tags"]]
        if fields:  # ONLY context/tags — enforced here, not prompt trust
            mem.evolve(item["id"], **fields)
            refreshed += 1
    newest = max(items, key=lambda i: ts_s(i.get("modifiedAt")) or 0)  # noqa: F821
    _save_state(c, space, brain, newest.get("modifiedAt"))
    return {"swept": len(linked), "refreshed": refreshed, "errors": errors}
