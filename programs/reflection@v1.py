"""Reflection sweep over the memory store (cron).

ADR-007 §4.2: mechanism present,
trigger ships DISABLED — activation is gated on its eval.

Two moves over the never-recalled tail (accessCount 0, older than
`minAgeDays`): (a) synthesize per-category clusters (≥ `minCluster`
items) into ONE insight item — saved through the §2 dedup judge with
`source: reflection`, capped confidence, and derived_from edges back to
every source item; (b) detect contradictions → lower BOTH items'
confidence and write a `contradicts` edge. Everything the LLM proposes
is validated in code against the actual cluster ids. args: {space,
brainId, minAgeDays?, minCluster?, tier?}.
"""

import json

MIN_AGE_DAYS = 14
MIN_CLUSTER = 3
TIER = "classify"
CONFIDENCE_CAP = 6
DAY_S = 86400

_SYSTEM = (
    "You reflect over an agent's never-recalled memory items of one "
    "category. Reply ONLY a JSON object: "
    '{"insight": {"context": "<one-line synthesis>", "body": "<detail>"} '
    'or null if the items do not genuinely add up to one insight, '
    '"contradictions": [{"a": "<id>", "b": "<id>"}] for items that state '
    "conflicting facts (usually empty).}")


def _first_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"reflection reply carried no JSON object: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def reflect_cluster(category, items, tier):
    lines = [f"- id={i['id']} {i.get('context', '')}" for i in items]
    reply = use("llm@v1").chat(  # noqa: F821 - guest global
        [{"role": "user", "parts": [{"type": "text", "text":
          f"category: {category}\n" + "\n".join(lines)}]}],
        system=_SYSTEM, tier=tier, tools=[])
    return _first_json(" ".join(p["text"] for p in reply["parts"]
                                if p["type"] == "text"))


def main(args):
    space, brain = args["space"], args["brainId"]
    tier = args.get("tier", TIER)
    min_cluster = args.get("minCluster", MIN_CLUSTER)
    cutoff = now() - args.get("minAgeDays", MIN_AGE_DAYS) * DAY_S  # noqa: F821
    c = use("any@v1").client()  # noqa: F821 - guest global
    mem = use("memory@v1").memory(c, space)  # noqa: F821 - guest global
    rec = use("recall@v1").recall(c, space)  # noqa: F821 - guest global
    items = c.query(space, brain, "agent_memory_items",
                    filter={"accessCount": 0, "createdAt": {"$lt": cutoff}})

    clusters = {}
    for i in items:
        clusters.setdefault(i.get("category", "?"), []).append(i)

    insights = contradictions = errors = 0
    for category, members in sorted(clusters.items()):
        if len(members) < min_cluster:
            continue
        ids = {m["id"] for m in members}
        by_id = {m["id"]: m for m in members}
        try:
            verdict = reflect_cluster(category, members, tier)
        except Exception:
            errors += 1
            continue
        insight = verdict.get("insight")
        if insight and (insight.get("context") or "").strip():
            candidate = {
                "category": "insight", "context": insight["context"].strip(),
                "confidence": CONFIDENCE_CAP, "source": "reflection",
                "edges": [{"to": mid, "type": "derived_from"} for mid in sorted(ids)]}
            if insight.get("body"):
                candidate["body"] = insight["body"]
            mem.save_with_dedup(candidate, recall=rec)
            insights += 1
        for pair in verdict.get("contradictions") or []:
            a, b = pair.get("a"), pair.get("b")
            if a not in ids or b not in ids or a == b:
                continue  # LLM output validated against real cluster ids
            for mid, other in ((a, b), (b, a)):
                item = by_id[mid]
                mem.evolve(mid,
                           confidence=max(1, int(item.get("confidence", 5)) - 2),
                           edges=[*(item.get("edges") or []),
                                  {"to": other, "type": "contradicts"}])
            contradictions += 1
    return {"clusters": len(clusters), "insights": insights,
            "contradictions": contradictions, "errors": errors}
