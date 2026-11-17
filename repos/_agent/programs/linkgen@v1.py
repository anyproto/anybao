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
NEIGHBOR_CHARS = 300
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
    # the cursor is the newest swept item's createdAt instant, stored
    # verbatim (ADR-019 §2); a pre-instant seconds cursor still wraps
    return instant(rows[0].get("lastCreatedAt") or 0) if rows else instant(0)  # noqa: F821


def _save_state(c, space, brain, last_ts):
    c.upsert_record(space, brain, STATE_DATASET, STATE_ID,
                    {"lastCreatedAt": last_ts})


def _neighbor_text(hit):
    """The matched text of a search hit, on one line and capped. The
    server carries it in `data`; a hit without it shows its id."""
    text = " ".join(str(hit.get("data") or "").split())
    return text[:NEIGHBOR_CHARS] or hit.get("recordId")


def _target(hit):
    """What an edge to this hit points at. A memory item or a history
    record: its record id, unique in its store. Any other hit: the
    OBJECT that holds the matched record, as its link — a property or
    block record id (`name`, a window id) names nothing by itself."""
    if hit.get("scope") in ("agent", "history"):
        return hit.get("recordId")
    return hit.get("uri")


def neighbors_of(hits, item_id):
    """The hits as link candidates, best first: `{key, target, scope,
    text}`. One candidate per target (two records of one object are one
    neighbor), never the item itself; `key` (`n1`, `n2`, …) is the
    short id the judge answers with."""
    out, seen = [], {item_id}
    for h in hits:
        target = _target(h)
        if not target or target in seen:
            continue
        seen.add(target)
        title = " ".join(str(h.get("title") or "").split())
        text = _neighbor_text(h)
        if title and h.get("scope") not in ("agent", "history"):
            text = f"{title}: {text}"
        out.append({"key": f"n{len(out) + 1}", "target": target,
                    "scope": h.get("scope"), "text": text})
    return out


def propose_links(item, neighbors, tier):
    lines = [f"- id={n['key']} [{n['scope']}] {n['text']}" for n in neighbors]
    prompt = (f"ITEM: [{item.get('category')}] {item.get('context')}\n"
              f"{item.get('body') or ''}\n\nNEIGHBORS:\n" + "\n".join(lines))
    reply = use("llm@v1").chat(  # noqa: F821 - guest global
        [{"role": "user", "parts": [{"type": "text", "text": prompt}]}],
        system=_SYSTEM, tier=tier, tools=[])
    text = " ".join(p["text"] for p in reply["parts"] if p["type"] == "text")
    return _first_json_array(text)


def valid_links(proposals, targets, existing_edges, max_links):
    """Vocabulary + target allow-lists enforced in code; dedup against
    edges already on the item. `targets` maps a neighbor key to the
    edge target it stands for."""
    seen = {(e.get("to"), e.get("type")) for e in existing_edges}
    out = []
    for p in proposals:
        to, typ = targets.get(p.get("to")), p.get("type")
        if typ not in EDGE_TYPES or to is None or (to, typ) in seen:
            continue
        seen.add((to, typ))
        out.append({"to": to, "type": typ})
        if len(out) >= max_links:
            break
    return out


def main(args):
    space = args["space"]
    c = use("any@v1")  # noqa: F821 - guest global
    brain = c.get_brain()["objectId"]   # the bao space's (ADR-017 §0)
    last = _state(c, space, brain)
    items = c.query(space, brain, "agent_memory_items",
                    filter={"createdAt": {"$gt": last}}, sort=["createdAt"],
                    limit=args.get("batch", BATCH))
    if not items:
        return {"swept": 0, "linked": 0, "errors": 0}

    mem = use("memory@v1").memory(c)  # noqa: F821 - guest global
    tier = args.get("tier", TIER)
    max_links = args.get("maxLinks", MAX_LINKS)
    linked = errors = 0
    for item in items:
        try:
            hits = c.search(space, item.get("context", ""),
                            scopes=["agent", "history", "basic"], limit=8)
            neighbors = neighbors_of(hits.get("hits", []), item["id"])
            if not neighbors:
                continue
            proposals = propose_links(item, neighbors, tier)
            links = valid_links(proposals, {n["key"]: n["target"] for n in neighbors},
                                item.get("edges") or [], max_links)
            if links:
                mem.evolve(item["id"],
                           edges=[*(item.get("edges") or []), *links])
                linked += 1
        except Exception:
            errors += 1  # counted loud; the sweep continues
    newest = max(items, key=lambda i: ts_s(i.get("createdAt")) or 0)  # noqa: F821
    _save_state(c, space, brain, newest.get("createdAt"))
    return {"swept": len(items), "linked": linked, "errors": errors}
