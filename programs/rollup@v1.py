"""rollup@v1 — hierarchical history rollup (ADR-006 §2), the cron
trigger job that keeps the boot window's decreasing-resolution pyramid
fed: uncovered turns → L1 chunks (batches of `batch`), uncovered L1
chunks → L2, … up to `maxLevel`. L2+ summaries come from CHILD
SUMMARIES ONLY (ADR-006 resolved Q3). Only COMPLETE batches roll up —
the partial tail stays raw until it fills.

Runs in the guest: all I/O through effects (any.query / llm.chat /
any.create_chunk), so every run is traced and replayable. args:
{space, chatId, batch?, tier?, maxLevel?}.
"""

BATCH = 10
TIER = "classify"
MAX_LEVEL = 2

_L1_SYSTEM = ("Summarize this conversation slice in 2-4 sentences: topics, "
              "decisions (with the why), and outcomes. No preamble.")
_LN_SYSTEM = ("Combine these period summaries into one 2-4 sentence summary "
              "covering the whole span: main threads, decisions, outcomes. "
              "No preamble.")


def _summarize(text, system, tier):
    reply = effect("llm.chat", {  # noqa: F821 - guest global
        "messages": [{"role": "user", "parts": [{"type": "text", "text": text}]}],
        "system": system, "tier": tier, "tools": []})
    return " ".join(p["text"] for p in reply["parts"]
                    if p["type"] == "text").strip()


def _query(space, chat, dataset, **kw):
    return effect("any.query", {  # noqa: F821 - guest global
        "space": space, "object_id": chat, "dataset": dataset, **kw})


def _covered(space, chat, level):
    """Max toSeq among level-N chunks — everything at or below is done."""
    top = _query(space, chat, "agent_chunks",
                 filter={"level": level}, sort=["-seq"], limit=1)
    return top[0]["toSeq"] if top else 0


def _turn_text(t):
    return "user: " + (t.get("userText") or "") + "\nagent: " + \
        "\n".join(t.get("replies") or [])


def _emit(space, chat, level, children, summary, period):
    body = {"level": level,
            "fromSeq": children[0]["seq"], "toSeq": children[-1]["seq"],
            "summary": summary, "periodStart": period[0], "periodEnd": period[1],
            "unitsCovered": len(children)}
    effect("any.create_chunk", {  # noqa: F821 - guest global
        "space": space, "object_id": chat, "body": body})
    return body


def _batches(items, size):
    return [items[i:i + size] for i in range(0, len(items) - size + 1, size)]


def rollup_l1(space, chat, batch, tier):
    covered = _covered(space, chat, 1)
    turns = _query(space, chat, "agent_turns",
                   filter={"seq": {"$gt": covered}}, sort=["seq"])
    out = []
    for group in _batches(turns, batch):
        summary = _summarize("\n\n".join(_turn_text(t) for t in group),
                             _L1_SYSTEM, tier)
        period = (min(t.get("createdAt", 0) for t in group),
                  max(t.get("createdAt", 0) for t in group))
        out.append(_emit(space, chat, 1, group, summary, period))
    return out


def rollup_ln(space, chat, level, batch, tier):
    covered = _covered(space, chat, level)
    children = _query(space, chat, "agent_chunks",
                      filter={"level": level - 1, "seq": {"$gt": covered}},
                      sort=["seq"])
    out = []
    for group in _batches(children, batch):
        summary = _summarize(
            "\n".join("- " + (c.get("summary") or "") for c in group),
            _LN_SYSTEM, tier)
        period = (min(c.get("periodStart", 0) for c in group),
                  max(c.get("periodEnd", 0) for c in group))
        out.append(_emit(space, chat, level, group, summary, period))
    return out


def main(args):
    space, chat = args["space"], args["chatId"]
    batch = args.get("batch", BATCH)
    tier = args.get("tier", TIER)
    created = rollup_l1(space, chat, batch, tier)
    for level in range(2, args.get("maxLevel", MAX_LEVEL) + 1):
        created += rollup_ln(space, chat, level, batch, tier)
    return {"created": len(created),
            "byLevel": {c["level"]: sum(1 for x in created if x["level"] == c["level"])
                        for c in created}}
