"""Background memory extraction from conversations (cron).

ADR-007 §1b: a
batched trigger job over newly persisted turns, off the hot path.

HIGH bar (the §1a audit): candidates only matching stable-fact shapes —
preference / decision / lesson / durable domain fact — never episodes
or session summaries (that is the history channel's job). Every
candidate goes through the §2 dedup judge (memory@v1 save_with_dedup);
survivors carry provenance (fromSeq) and capped confidence ≤ 6
(machine-derived never outranks user-stated). Cursor = an
agent_job_state record on the BRAIN object (where the server registers
harness bookkeeping); re-derivable (turns are append-only). args:
{space, chatId, batch?, tier?} — the brain resolves itself
(ADR-017); turns are read off the chat's log child.
"""

import json

SHAPES = ("preference", "decision", "lesson", "fact")
CONFIDENCE_CAP = 6
BATCH = 20
TIER = "classify"
STATE_DATASET = "agent_job_state"
STATE_ID = "extraction"

_SYSTEM = (
    "You extract durable memory candidates from an agent's conversation "
    "turns. ONLY stable facts worth remembering months later: category "
    "preference | decision (with the why) | lesson | fact. NEVER episodes, "
    "session summaries, greetings, meta-chatter, or restatements. Most "
    "turn batches yield ZERO candidates — that is the healthy default. "
    "Answer ONLY a JSON array (possibly empty): "
    '[{"category": "...", "context": "<one-line fact>", "body": "<detail>", '
    '"confidence": 1-10, "fromSeq": <turn seq it came from>}]')


def _first_json_array(text):
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError(f"extractor reply carried no JSON array: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def _state(c, space, brain):
    rows = c.query(space, brain, STATE_DATASET,
                   filter={"id": STATE_ID}, limit=1)
    return rows[0].get("lastSeq", 0) if rows else 0


def _save_state(c, space, brain, last_seq):
    c.upsert_record(space, brain, STATE_DATASET, STATE_ID,
                    {"lastSeq": last_seq})


def extract_candidates(turns, tier):
    lines = []
    for t in turns:
        replies = "\n".join(t.get("replies") or [])
        lines.append(f"turn {t.get('seq')}\nuser: {t.get('userText') or ''}\n"
                     f"agent: {replies}")
    reply = use("llm@v1").chat(  # noqa: F821 - guest global
        [{"role": "user",
          "parts": [{"type": "text", "text": "\n\n".join(lines)}]}],
        system=_SYSTEM, tier=tier, tools=[])
    text = " ".join(p["text"] for p in reply["parts"] if p["type"] == "text")
    return _first_json_array(text)


def normalize(candidate, max_seq):
    """§1b discipline enforced in code, not prompt trust: shape allow-list,
    capped confidence, provenance pointer."""
    if candidate.get("category") not in SHAPES:
        return None
    context = (candidate.get("context") or "").strip()
    if not context:
        return None
    out = {"category": candidate["category"], "context": context,
           "confidence": min(int(candidate.get("confidence", CONFIDENCE_CAP)),
                             CONFIDENCE_CAP),
           "source": "extraction",
           "provenance": {"fromSeq": int(candidate.get("fromSeq") or max_seq)}}
    if candidate.get("body"):
        out["body"] = candidate["body"]
    return out


def main(args):
    space, chat = args["space"], args["chatId"]
    c = use("any@v1")  # noqa: F821 - guest global
    brain = c.get_brain(space)["objectId"]
    log = c.chat_log(space, chat)["objectId"]
    last = _state(c, space, brain)
    turns = c.query(space, log, "agent_turns",
                    filter={"seq": {"$gt": last}}, sort=["seq"],
                    limit=args.get("batch", BATCH))
    if not turns:
        return {"scanned": 0, "saved": 0, "deduplicated": 0, "skipped": 0,
                "errors": 0}

    mem = use("memory@v1").memory(c, space)  # noqa: F821 - guest global
    rec = use("recall@v1").recall(c, space)  # noqa: F821 - guest global
    max_seq = max(t["seq"] for t in turns)
    saved = deduped = skipped = errors = 0
    for raw in extract_candidates(turns, args.get("tier", TIER)):
        candidate = normalize(raw, max_seq)
        if candidate is None:
            skipped += 1
            continue
        try:
            out = mem.save_with_dedup(candidate, recall=rec)
            if out.get("deduplicated"):
                deduped += 1
            else:
                saved += 1
        except Exception:
            errors += 1  # loud in the run record via counts; sweep continues
    _save_state(c, space, brain, max_seq)
    return {"scanned": len(turns), "saved": saved, "deduplicated": deduped,
            "skipped": skipped, "errors": errors}
