"""history@v1 — conversation turns/chunks composition (ADR-006 §1/§2).

`build_turn` shapes the agent_turns v2 payload; `raw_tail` /
`render_boot_window` compose the token-budgeted HIERARCHICAL boot
window — newest raw turns at full resolution, then chunk lines
ascending by level, so ALL history is present at decreasing
resolution. `recent_turns` / `chunks_at_level` are the thin reads over
the any@v1 client. Turns/chunks live in the USER space (the agent
overlay holds code only); rollup@v1 produces the chunk pyramid.
"""


def approx_tokens(text):
    """Cheap proxy tokenizer — ~4 chars/token, the standard rule of
    thumb."""
    return (len(text) + 3) // 4


def build_turn(*, user_text, outcome, think="", effects=None, message_ids=None,
               trace_ref="", user_name="", from_agent="", llm=None):
    """The agent_turns v2 payload — seq omitted (server-assigned).
    `outcome` is the loop outcome dict (`{"replies", "stop", …}`);
    `replies` = what the user saw; `think` = narration that did NOT go
    to chat (distinct, ADR-006 §1). `interrupted` + neutral stopReason
    come from the outcome."""
    body = {
        "userText": user_text,
        "replies": outcome["replies"],
        "interrupted": outcome.get("stop") == "break_hard",
    }
    if think:
        body["think"] = think
    if effects:
        body["effects"] = effects
    if message_ids:
        body["messageIds"] = message_ids
    if trace_ref:
        body["traceRef"] = trace_ref
    if user_name:
        body["userName"] = user_name
    if from_agent:
        body["fromAgent"] = from_agent
    llm = dict(llm or {})
    llm.setdefault("stopReason", outcome.get("stop"))
    body["llm"] = llm
    return body


# --- boot window (token-budgeted hierarchical composition) ------------------

def _turn_messages(turn):
    """A turn → a user message (userText) + an assistant message
    (replies)."""
    msgs = []
    ut = turn.get("userText", "")
    if ut:
        msgs.append({"role": "user", "parts": [{"type": "text", "text": ut}]})
    replies = turn.get("replies", [])
    if replies:
        msgs.append({"role": "assistant",
                     "parts": [{"type": "text", "text": "\n".join(replies)}]})
    return msgs


def _chunk_line(chunk):
    """A chunk → one line with its drill-down handle (seq + child range)."""
    lvl, seq = chunk.get("level", 1), chunk.get("seq")
    frm, to = chunk.get("fromSeq"), chunk.get("toSeq")
    unit = "turns" if lvl == 1 else f"L{lvl - 1} chunks"
    return f"— {chunk.get('summary', '')}  [chunk #{seq} (L{lvl}), {unit} {frm}–{to}]"


def raw_tail(raw_turns, total_tokens=40000, raw_tail_fraction=0.5):
    """Newest raw turns filling the raw-tail budget, oldest→newest — the
    full-resolution slice of the boot window. Its min seq is also the
    auto-recall deep-history guard boundary (ADR-007 §5)."""
    raw_budget = int(total_tokens * raw_tail_fraction)
    included = []
    spent = 0
    for turn in reversed(raw_turns):  # newest first
        cost = approx_tokens(turn.get("userText", "") + "\n".join(turn.get("replies", [])))
        if spent + cost > raw_budget and included:
            break
        included.append(turn)
        spent += cost
    included.reverse()
    return included


def render_boot_window(raw_turns, chunks_by_level, total_tokens=40000,
                       raw_tail_fraction=0.5):
    """Compose the boot context: newest raw turns (full resolution) up to
    the raw-tail budget, then chunks ascending by level filling the rest —
    all history at decreasing resolution, constant-size by budget.
    `raw_turns` ascending by seq; `chunks_by_level` = level → chunks
    ascending by seq. Skips chunks fully covered by the included raw tail
    (no double-cover). Returns neutral messages, oldest→newest, chunks as
    one leading compressed-context message."""
    included_turns = raw_tail(raw_turns, total_tokens, raw_tail_fraction)
    spent = sum(approx_tokens(t.get("userText", "") + "\n".join(t.get("replies", [])))
                for t in included_turns)
    covered_min_seq = included_turns[0].get("seq", 0) if included_turns else None

    # fill the remainder with chunks ascending level, newest-first per level,
    # skipping any chunk whose range is fully inside the included raw tail
    remaining = total_tokens - spent
    chunk_lines = []
    for level in sorted(chunks_by_level):
        for chunk in reversed(chunks_by_level[level]):
            if level == 1 and covered_min_seq is not None \
                    and chunk.get("toSeq", -1) >= covered_min_seq:
                continue  # already present at full resolution
            line = _chunk_line(chunk)
            cost = approx_tokens(line)
            if cost > remaining:
                break
            chunk_lines.append(line)
            remaining -= cost

    messages = []
    if chunk_lines:
        chunk_lines.reverse()  # oldest→newest
        body = ("[earlier context, compressed — expand a chunk by its #seq]\n"
                + "\n".join(chunk_lines))
        messages.append({"role": "user", "parts": [{"type": "text", "text": body}]})
    for turn in included_turns:
        messages.extend(_turn_messages(turn))
    return messages


# --- thin reads over the any@v1 client ---------------------------------------

@span("history.recent_turns", kind="getter")  # noqa: F821 - guest global
def recent_turns(client, space, chat_id, limit):
    """Newest turns first (descending seq)."""
    return client.query(space, chat_id, "agent_turns", sort=["-seq"], limit=limit)


@span("history.chunks_at_level", kind="getter")  # noqa: F821 - guest global
def chunks_at_level(client, space, chat_id, level, limit):
    """Newest chunks of one level first (descending seq)."""
    return client.query(space, chat_id, "agent_chunks",
                        filter={"level": level}, sort=["-seq"], limit=limit)
