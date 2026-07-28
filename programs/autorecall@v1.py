"""Auto-recall injection — loop plumbing, not an agent tool.

ADR-007 §5: the recall-side
STRUCTURAL mechanism (memory behavior must not depend on model
initiative).

`plan` runs recall@v1 search over the user message — index-backed, no
LLM call — and frames the top hits as a synthetic recall tool call +
tool result: evidence the model weighs and can discount as stale, never
prompt truth. Memory hits render as distilled facts (provenance date +
confidence); history hits as *related past discussion* pointers (chunk
drill-down handles, bodies on demand). Guards: deep-history only (hits
inside the boot window's raw tail are skipped — auto-recall is the
TOPICAL channel, the window owns RECENCY), relevance threshold (generic
messages inject nothing), token budget. accessCount bumps on every
injected memory item (§4.3, ON from day one); `log_roi` writes the
per-injection ROI records the §1b metrics read.
"""

import re

INJECT_SCOPES = ("agent", "history")
_HISTORY_DATASETS = ("agent_turns", "agent_chunks")
ROI_DATASET = "agent_roi_injections"
_WORD = re.compile(r"[a-zA-Z0-9]{5,}")

# min_score is the relevance gate — below it, inject nothing. The
# server's hybrid search scores are RRF (k=60): rank-1 in ONE leg
# ≈ 0.0164, rank-1 in both ≈ 0.033 (live-calibrated 2026-07-08). 0.015
# admits near-top hits from either leg and drops deep-rank noise; a 0-1
# similarity scale would need a very different constant (ADR-007 §7:
# scale normalization is an upstream question). max_memory: §5 "top 3–5
# hits"; max_history: §5 "capped at 2–3".
POLICY = {"min_score": 0.015, "max_memory": 5, "max_history": 3,
          "token_budget": 1500}


def approx_tokens(text):
    """Cheap proxy tokenizer — ~4 chars/token."""
    return (len(text) + 3) // 4


def _civil(days):
    """Days since 1970-01-01 → (year, month, day), proleptic Gregorian
    (the standard civil-from-days algorithm)."""
    days += 719468
    era = days // 146097
    doe = days - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + 3 if mp < 10 else mp - 9
    return yoe + era * 400 + (1 if month <= 2 else 0), month, day


def _date(ts):
    if not isinstance(ts, (int, float)) or not ts:
        return "undated"
    y, m, d = _civil(int(ts) // 86400)
    return f"{y:04d}-{m:02d}-{d:02d}"


def memory_line(item):
    prov = _date(item.get("validFrom") or item.get("createdAt"))
    conf = item.get("confidence")
    suffix = f"(saved {prov}" + (f", confidence {conf}" if conf is not None else "") + ")"
    return f"- [{item.get('category', '?')}] {item.get('context', '')} {suffix}"


def history_line(rec, dataset):
    if dataset == "agent_chunks":
        return (f"- related past discussion, {_date(rec.get('periodStart'))}: "
                f"{rec.get('summary', '')} [chunk #{rec.get('seq')} "
                f"(L{rec.get('level', 1)}), turns {rec.get('fromSeq')}–{rec.get('toSeq')}]")
    first = (rec.get("userText") or "").splitlines() or [""]
    return (f"- related past discussion, {_date(rec.get('createdAt'))}: "
            f"“{first[0][:100]}” [turn #{rec.get('seq')}]")


def covered_by_boot(rec, dataset, boot_min_seq):
    """Deep-history guard: a turn in the boot window's raw tail, or a
    chunk fully inside it, is already visible at full resolution."""
    if boot_min_seq is None:
        return False
    if dataset == "agent_turns":
        return rec.get("seq", -1) >= boot_min_seq
    if dataset == "agent_chunks":
        return rec.get("fromSeq", -1) >= boot_min_seq
    return False


def frame_messages(query, memory_lines, history_lines):
    """Tool-result framing (§5): a synthetic recall call + its result,
    exactly the shape an explicit recall would produce."""
    sections = []
    if memory_lines:
        sections.append("Memories:\n" + "\n".join(memory_lines))
    if history_lines:
        sections.append("Related history:\n" + "\n".join(history_lines))
    if not sections:
        return []
    call_id = "autorecall_0"
    return [
        {"role": "assistant",
         "parts": [{"type": "tool_call", "id": call_id, "name": "recall",
                    "args": {"query": query, "scopes": list(INJECT_SCOPES)}}]},
        {"role": "user",
         "parts": [{"type": "tool_result", "call_id": call_id,
                    "content": "\n\n".join(sections), "is_error": False}]},
    ]


@span("autorecall.plan")  # noqa: F821 - guest global
def plan(client, space, user_text, boot_min_seq=None, policy=None):
    """The injection plan for one turn: `{"messages": [...], "injected":
    [(hit, record), ...]}` — messages ready to splice before the user
    message, injected pairs feeding `log_roi`. Bumps accessCount on
    every injected memory item. Fail-open: recall must never break the
    turn."""
    try:
        return _plan(client, space, user_text, boot_min_seq, policy)
    except Exception:
        return {"messages": [], "injected": []}


def _plan(client, space, user_text, boot_min_seq, policy):
    p = dict(POLICY)
    p.update(policy or {})
    rec = use("recall@v1").recall(client, space)  # noqa: F821 - guest global
    hits = rec.search(user_text, scopes=list(INJECT_SCOPES),
                      limit=(p["max_memory"] + p["max_history"]) * 2)
    relevant = [h for h in hits if h.get("score", 0) >= p["min_score"]]
    mem_hits = [h for h in relevant if h.get("dataset") == "agent_memory_items"]
    hist_hits = [h for h in relevant if h.get("dataset") in _HISTORY_DATASETS]
    if not mem_hits and not hist_hits:
        return {"messages": [], "injected": []}

    pairs = rec.hydrate(mem_hits + hist_hits)
    budget = p["token_budget"]

    injected, mem_lines, bumped = [], [], []
    for h, r in pairs:
        if h["dataset"] != "agent_memory_items" or len(mem_lines) >= p["max_memory"]:
            continue
        line = memory_line(r)
        cost = approx_tokens(line)
        if cost > budget:
            break
        mem_lines.append(line)
        budget -= cost
        bumped.append(r)
        injected.append((h, r))

    hist_lines = []
    for h, r in pairs:
        if h["dataset"] not in _HISTORY_DATASETS or len(hist_lines) >= p["max_history"] \
                or covered_by_boot(r, h["dataset"], boot_min_seq):
            continue
        line = history_line(r, h["dataset"])
        cost = approx_tokens(line)
        if cost > budget:
            break
        hist_lines.append(line)
        budget -= cost

    msgs = frame_messages(user_text, mem_lines, hist_lines)
    if msgs:
        mem = use("memory@v1").memory(client, space)  # noqa: F821 - guest global
        for r in bumped:  # injected = recalled (§4.3)
            try:  # noqa: SIM105 - no contextlib in the guest; best-effort bump
                mem.bump_access(r["id"], r.get("accessCount", 0))
            except Exception:
                pass
    return {"messages": msgs, "injected": injected}


# --- ROI logging (ADR-007 §1b/§5: features earn their keep by measurement) ---

def referenced(context, replies):
    """Did any significant context word surface in the replies? A cheap
    drift signal, not a verdict."""
    text = " ".join(replies).lower()
    return any(w.lower() in text for w in _WORD.findall(context or ""))


def log_roi(client, space, injected, replies, ts):
    """One agent_roi_injections record per injected (hit, item) pair;
    the brain object comes from the hit pointer. Returns the number of
    records written."""
    n = 0
    for hit, item in injected:
        client.upsert_record(space, hit["objectId"], ROI_DATASET,
                             f"{item['id']}:{ts}",
                             {"itemId": item["id"], "ts": ts,
                              "referenced": referenced(item.get("context", ""),
                                                       replies)})
        n += 1
    return n
