"""Hourly link-generation sweep over memory items (cron).

ADR-007 §4.1:
A-MEM's write-time linking run async over memory items created since
the last sweep. Seed-search neighbors (scopes agent+history+basic) →
classify-tier LLM proposes typed links from the CURATED edge vocabulary
→ written as edges via memory@v1 evolve. New edge types are never
invented here (vocabulary drift is the known traversal killer — §3).
Cursor = a plain agent_job_state record on the brain object. args:
{space, batch?, tier?, maxLinks?} — the brain resolves itself
(ADR-017).
"""

import json

EDGE_TYPES = ("relates_to", "caused_by", "supersedes", "decided_in",
              "part_of", "owned_by", "discussed_in")
BATCH = 20
TIER = "classify"
MAX_LINKS = 3
STATE_DATASET = "agent_job_state"
STATE_ID = "linkgen"

_SYSTEM = (
    f"You link a new memory item to its true neighbors. Given the ITEM and "
    f"NEIGHBOR candidates, propose 0-{MAX_LINKS} typed links — only where "
    f"the relation is real and useful for later traversal; propose NOTHING "
    f"for mere topical similarity. Types allowed: {', '.join(EDGE_TYPES)}. "
    'Answer ONLY a JSON array: [{"to": "<neighbor id>", "type": "<type>"}]')


def _first_json_array(text):
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError(f"linkgen reply carried no JSON array: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def _state(c, space, brain):
    rows = c.query(space, brain, STATE_DATASET,
                   filter={"id": STATE_ID}, limit=1)
    return rows[0].get("lastCreatedAt", 0) if rows else 0


def _save_state(c, space, brain, last_ts):
    c.upsert_record(space, brain, STATE_DATASET, STATE_ID,
                    {"lastCreatedAt": last_ts})


def propose_links(item, neighbors, tier):
    lines = [f"- id={n.get('recordId')} [{n.get('scope')}] "
             f"{n.get('snippet') or n.get('recordId')}" for n in neighbors]
    prompt = (f"ITEM: [{item.get('category')}] {item.get('context')}\n"
              f"{item.get('body') or ''}\n\nNEIGHBORS:\n" + "\n".join(lines))
    reply = use("llm@v1").chat(  # noqa: F821 - guest global
        [{"role": "user", "parts": [{"type": "text", "text": prompt}]}],
        system=_SYSTEM, tier=tier, tools=[])
    text = " ".join(p["text"] for p in reply["parts"] if p["type"] == "text")
    return _first_json_array(text)


def valid_links(proposals, neighbor_ids, existing_edges, max_links):
    """Vocabulary + target allow-lists enforced in code; dedup against
    edges already on the item."""
    seen = {(e.get("to"), e.get("type")) for e in existing_edges}
    out = []
    for p in proposals:
        to, typ = p.get("to"), p.get("type")
        if typ not in EDGE_TYPES or to not in neighbor_ids or (to, typ) in seen:
            continue
        seen.add((to, typ))
        out.append({"to": to, "type": typ})
        if len(out) >= max_links:
            break
    return out


def main(args):
    space = args["space"]
    c = use("any@v1")  # noqa: F821 - guest global
    brain = c.get_brain(space)["objectId"]
    last = _state(c, space, brain)
    items = c.query(space, brain, "agent_memory_items",
                    filter={"createdAt": {"$gt": last}}, sort=["createdAt"],
                    limit=args.get("batch", BATCH))
    if not items:
        return {"swept": 0, "linked": 0, "errors": 0}

    mem = use("memory@v1").memory(c, space)  # noqa: F821 - guest global
    tier = args.get("tier", TIER)
    max_links = args.get("maxLinks", MAX_LINKS)
    linked = errors = 0
    for item in items:
        try:
            hits = c.search(space, item.get("context", ""),
                            scopes=["agent", "history", "basic"], limit=8)
            neighbors = [h for h in hits.get("hits", [])
                         if h.get("recordId") != item["id"]]
            if not neighbors:
                continue
            proposals = propose_links(item, neighbors, tier)
            links = valid_links(proposals, {n["recordId"] for n in neighbors},
                                item.get("edges") or [], max_links)
            if links:
                mem.evolve(item["id"],
                           edges=[*(item.get("edges") or []), *links])
                linked += 1
        except Exception:
            errors += 1  # counted loud; the sweep continues
    _save_state(c, space, brain, max(i.get("createdAt", 0) for i in items))
    return {"swept": len(items), "linked": linked, "errors": errors}
