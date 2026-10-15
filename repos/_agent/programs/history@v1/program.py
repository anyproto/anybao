# ADR-017
"""Conversation history reads (turns/chunks) + the boot window.

Turns and chunks live on the chat's log child (`c.chat_log(space,
chat_id)`), never on the chat itself. `recent_turns` /
`chunks_at_level` take the any@v1 module as their first argument."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

import re  # noqa: E402 - the tool marker leads, as in every tool program

# Loop plumbing context (ADR-006 §1/§2): build_turn shapes the
# agent_turns v2 payload the loop persists; raw_tail /
# render_boot_window compose the hierarchical boot window (newest raw
# turns full-resolution, older history as chunk lines by level — all
# history at decreasing resolution). rollup@v1 produces the pyramid.


CHARS_PER_TOKEN = 2.5   # ADR-006 §2: count_tokens over 186 real turns, sonnet-5


def approx_tokens(text):
    """Estimated real tokens — len / 2.5, rounded up (ADR-006 §2).

    Chat history is ids, `any://` links and code: 2.5 chars/token
    measured, not the 4 of English prose. The window records its
    estimate as `llm.bootTokensEst` beside the real usage."""
    return (len(text) * 2 + 4) // 5


def build_turn(*, user_text, outcome, think="", effects=None, message_ids=None,
               trace_ref="", user_name="", from_agent="", llm=None):
    # ADR-006 §1
    """The agent_turns v2 payload — seq omitted (server-assigned).

    `outcome` is the loop outcome dict (`{"replies", "stop", …}`); `replies` =
    what the user saw; `think` = narration that did NOT go to chat
    (distinct). `interrupted` + neutral stopReason come from the outcome."""
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

MAX_EFFECT_NAMES = 8
_NOT_WORK = ("time.now", "trace.")   # ADR-005 §5: clock + trace reads are noise


def _effects_note(turn):
    """The turn's effect summary as one line — `[ran: <name>×<n>, … ·
    <run id>]`: what the reply below ran, by name and count; the calls
    themselves stay in the trace (how to read a run is taught in
    `_core`, not here). Clock and trace reads are dropped."""
    eff = [e.replace(" ×", "×") for e in turn.get("effects") or []
           if isinstance(e, str) and not e.startswith(_NOT_WORK)]
    if not eff:
        return ""
    shown = ", ".join(eff[:MAX_EFFECT_NAMES])
    if len(eff) > MAX_EFFECT_NAMES:
        shown += f", +{len(eff) - MAX_EFFECT_NAMES} more"
    ref = turn.get("traceRef")
    return f"[ran: {shown}{' · ' + ref if ref else ''}]"


_STAMP = re.compile(r"^\[now: (?P<day>[A-Za-z]{3} \d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2})"
                    r"(?P<off> [+-]\d{2}:\d{2})? \| (?P<view>.*)\]$")


def _stamp(context, prev):
    """A past turn's `[now: …]` line, compact (ADR-005 §5): the full
    `[<day> <date> <time> <offset> | <view>]` on the first turn and
    when the day or the view changed from the previous turn, else
    `[<time>]` (with ` | <view>` only when the view changed). Returns
    (line, (day, view)) — the state the next turn compares against; an
    unparseable line renders verbatim."""
    m = _STAMP.match(context or "")
    if not m:
        return context, prev
    day, view = m["day"], m["view"]
    pday, pview = prev or (None, None)
    when = f"{day} {m['time']}{m['off'] or ''}" if day != pday else m["time"]
    line = f"[{when} | {view}]" if view != pview else f"[{when}]"
    return line, (day, view)


def _turn_messages(turn, prev=None):
    """A turn → a user message (userText, the compact time + view line
    it was sent with, the effect summary of the reply) + an assistant
    message (replies). The summary rides the USER side: roles keep
    alternating and the model never sees it as its own text to
    imitate. `prev` = the previous turn's stamp state (`_stamp`)."""
    msgs = []
    ut = turn.get("userText", "")
    stamp, _state = _stamp(turn.get("context") or "", prev)
    extra = [x for x in (stamp, _effects_note(turn)) if x]
    if ut or extra:
        text = "\n\n".join([ut, *extra]) if ut else "\n\n".join(extra)
        msgs.append({"role": "user", "parts": [{"type": "text", "text": text}]})
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


HIGH_WATER = 25_000   # ADR-006 §2: the raw tail jumps past this …
LOW_WATER = 12_000    # … to the oldest L1 boundary under this


def water_marks(total_tokens):
    """(high, low) raw-tail marks for a window budget (ADR-006 §2).

    The defaults, scaled down for a small budget so summaries keep room."""
    high = min(HIGH_WATER, total_tokens * 5 // 8)
    return high, min(LOW_WATER, high * LOW_WATER // HIGH_WATER)


def _turn_cost(turn):
    return sum(approx_tokens(p["text"]) for m in _turn_messages(turn)
               for p in m["parts"])


def choose_anchor(raw_turns, l1_chunks, prev_anchor, high_water=HIGH_WATER,
                  low_water=LOW_WATER):
    # ADR-006 §2
    """The raw tail's first seq for this run — kept or jumped forward.

    `raw_turns` ascending by seq (the fetched newest), `l1_chunks` the
    L1 rollups, `prev_anchor` the previous turn's `bootAnchor`. The
    previous anchor stays while the tail from it is under `high_water`
    estimated tokens; past it (or when it predates the fetched turns)
    the anchor jumps forward, never back, to the oldest L1 boundary
    (`toSeq + 1`) that brings the tail under `low_water` — whole
    chunks, so the summaries can cover what the tail drops. With no
    boundary that fits (rollup lagging, one giant turn) the newest
    turns under `low_water` form the tail. None without turns."""
    if not raw_turns:
        return None
    oldest, newest = raw_turns[0].get("seq", 0), raw_turns[-1].get("seq", 0)

    def cost_from(a):
        return sum(_turn_cost(t) for t in raw_turns if t.get("seq", 0) >= a)

    if prev_anchor is not None and oldest <= prev_anchor <= newest \
            and cost_from(prev_anchor) <= high_water:
        return prev_anchor
    floor = prev_anchor + 1 if prev_anchor is not None else oldest
    boundaries = sorted({c.get("toSeq", -1) + 1 for c in l1_chunks} | {oldest})
    for b in boundaries:   # oldest first: the smallest jump that fits
        if max(floor, oldest) <= b <= newest and cost_from(b) <= low_water:
            return b
    anchor, spent = newest, 0
    for turn in reversed(raw_turns):
        cost = _turn_cost(turn)
        if spent + cost > low_water and turn.get("seq") != newest:
            break
        anchor, spent = turn.get("seq", 0), spent + cost
    return max(anchor, floor) if prev_anchor is not None else anchor


def raw_tail(raw_turns, anchor):
    # ADR-007 §5
    """The boot window's full-resolution slice: every turn from the
    anchor seq on, oldest→newest; the anchor is also the auto-recall
    deep-history guard boundary."""
    if anchor is None:
        return []
    return [t for t in raw_turns if t.get("seq", 0) >= anchor]


def render_boot_window(raw_turns, chunks_by_level, total_tokens=40000,
                       anchor=None):
    """Compose the boot context as neutral messages, oldest→newest.

    The raw tail from `anchor` (full resolution; `choose_anchor` when
    None — a fresh choice, no previous run), preceded by one
    compressed-context message of chunk lines for everything that ends
    before the anchor, ascending by level, newest first per level, no
    range twice (`_fill_chunks`). The summary budget is the window
    budget minus the high-water mark — fixed between anchor jumps, so
    the message is byte-stable while the tail grows (ADR-006 §2).
    `raw_turns` ascending by seq; `chunks_by_level` = level → chunks
    ascending by seq."""
    high, low = water_marks(total_tokens)
    if anchor is None:
        anchor = choose_anchor(raw_turns, chunks_by_level.get(1, []), None, high, low)
    included_turns = raw_tail(raw_turns, anchor)
    chunks = _fill_chunks(chunks_by_level, max(0, total_tokens - high), anchor)

    messages = []
    if chunks:
        body = ("[earlier context, compressed — expand a chunk by its #seq]\n"
                + "\n".join(_chunk_line(c) for c in chunks))
        messages.append({"role": "user", "parts": [{"type": "text", "text": body}]})
    state = None   # the previous turn's stamp (day, view) — compact stamps
    for turn in included_turns:
        messages.extend(_turn_messages(turn, state))
        state = _stamp(turn.get("context") or "", state)[1]
    return messages


def _fill_chunks(chunks_by_level, budget, covered_min_seq):
    """The summary message's chunks, oldest→newest, within `budget`
    tokens — no range covered twice (ADR-006 §2).

    Levels ascend, newest first per level: L1 chunks inside the raw
    tail (`toSeq >= covered_min_seq`) are skipped; a level-N chunk is
    rendered only where its level-N−1 children did not all fit, and
    then it replaces the ones that did — the newest material keeps the
    finest resolution, older material degrades whole."""
    chosen = {}      # level -> {seq: chunk} rendered at this level
    covered = {}     # level -> seqs present at this or a finer resolution
    remaining = budget
    for level in sorted(chunks_by_level):
        chosen[level], covered[level] = {}, set()
        for chunk in reversed(chunks_by_level[level]):
            seq = chunk.get("seq")
            if covered_min_seq is not None \
                    and _turn_span(chunk, chunks_by_level)[1] >= covered_min_seq:
                covered[level].add(seq)   # (partly) in the raw tail: never summarized
                continue
            if level > 1:
                kids = _children(chunk, chunks_by_level)
                if kids and all(k in covered[level - 1] for k in kids):
                    covered[level].add(seq)   # present whole, at finer resolution
                    continue
                # a partial cover is replaced whole: every rendered
                # descendant, at every level below, gives way
                remaining += _evict(chunk, chunks_by_level, chosen, covered)
            cost = approx_tokens(_chunk_line(chunk))
            if cost > remaining:
                break
            chosen[level][seq] = chunk
            covered[level].add(seq)
            remaining -= cost
    out = [c for lvl in chosen for c in chosen[lvl].values()]
    # reading order: by the oldest turn each chunk covers (fetch limits
    # can leave a higher chunk's children unfetched — older than any)
    out.sort(key=lambda c: (_turn_span(c, chunks_by_level)[0], c.get("level", 1)))
    return out


def _children(chunk, chunks_by_level):
    """The fetched level-N−1 chunk seqs a level-N chunk covers."""
    return [c.get("seq") for c in _child_chunks(chunk, chunks_by_level)]


def _child_chunks(chunk, chunks_by_level):
    lvl = chunk.get("level", 1)
    return [c for c in chunks_by_level.get(lvl - 1, [])
            if chunk.get("fromSeq", 0) <= c.get("seq", -1) <= chunk.get("toSeq", -1)]


def _evict(chunk, chunks_by_level, chosen, covered):
    """Drop every rendered descendant of `chunk` (all levels below);
    returns the tokens given back."""
    freed = 0
    lvl = chunk.get("level", 1)
    if lvl <= 1:
        return 0
    for kid in _child_chunks(chunk, chunks_by_level):
        gone = chosen.get(lvl - 1, {}).pop(kid.get("seq"), None)
        if gone is not None:
            freed += approx_tokens(_chunk_line(gone))
        covered.get(lvl - 1, set()).discard(kid.get("seq"))
        freed += _evict(kid, chunks_by_level, chosen, covered)
    return freed


def _turn_span(chunk, chunks_by_level):
    """(oldest, newest) TURN seq a chunk covers — L1 directly, higher
    levels through their fetched children; none fetched → (-1, -1),
    older than anything in hand."""
    lvl = chunk.get("level", 1)
    if lvl <= 1:
        return chunk.get("fromSeq", 0), chunk.get("toSeq", -1)
    spans = [_turn_span(c, chunks_by_level)
             for c in chunks_by_level.get(lvl - 1, [])
             if chunk.get("fromSeq", 0) <= c.get("seq", -1) <= chunk.get("toSeq", -1)]
    if not spans:
        return -1, -1
    return min(s[0] for s in spans), max(s[1] for s in spans)


# --- thin reads over the any@v1 client ---------------------------------------

@span(kind="getter")  # noqa: F821 - guest global
def recent_turns(client, space, chat_id, limit):
    # ADR-017
    """Newest AGENTLOG turns first — the agent's own turn records, NOT the chat.

    Descending seq. For what people said in a chat, read its messages:
    `client.query(space, chat_id, "chat_messages", sort=["-createdAt"],
    limit=n)`. Turns live on the chat's log child. An empty [] here
    just means this agent never logged turns on that chat."""
    log = client.chat_log(space, chat_id)["objectId"]
    return client.query(space, log, "agent_turns", sort=["-seq"], limit=limit)


@span(kind="getter")  # noqa: F821 - guest global
def chunks_at_level(client, space, chat_id, level, limit):
    """Newest chunks of one level first (descending seq)."""
    log = client.chat_log(space, chat_id)["objectId"]
    return client.query(space, log, "agent_chunks",
                        filter={"level": level}, sort=["-seq"], limit=limit)


@span(kind="getter")  # noqa: F821 - guest global
def activity(client, space, chat_id, unit="day", limit=90):
    # ADR-019 §3
    """Turns per calendar period, oldest first → [{period, turns}].

    Returns [{"period": <instant>, "turns": n}] — `unit` ∈ day | week | month.
    Native date arithmetic over agent_turns.createdAt ($dateTrunc);
    the periods are UTC instants — render with fmt_ts."""
    log = client.chat_log(space, chat_id)["objectId"]
    r = client.aggregate(space, [
        {"$group": {"_id": {"$dateTrunc": {"date": "$createdAt", "unit": unit}},
                    "turns": {"$count": {}}}},
        {"$sort": {"id": 1}},
        {"$limit": limit}],
        object_id=log, dataset="agent_turns")
    return [{"period": row.get("id"), "turns": row.get("turns")}
            for row in (r or {}).get("records", [])]
