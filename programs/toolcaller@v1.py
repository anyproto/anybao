"""toolcaller@v1 — the conversation loop as a guest program (ADR-005).

One invocation = main(args) driving the whole turn cycle inside the
cage: boot window + auto-recall injection, the llm loop, model cells
executed via `subcell` (each wrapped in a `cell` span so the trace
groups its effects), progressive-disclosure digests, mailbox control
drained as a recorded effect, ceilings → wrap-up, replies and the turn
record written through the any module. Everything nondeterministic is
an effect, so a recorded conversation replays whole.

args: {space, chatId, userText, system?, agentName?, traceRef?,
maxTurns?, maxTokensTotal?, tier?, bootTokens?}.
"""

import datetime

RUN_CELL_TOOL = {
    "name": "run_cell",
    "description": "Execute a Python cell in the persistent kernel.",
}
MAX_TURNS = 108
MAX_TOKENS_TOTAL = 1_000_000
TIER = "codegen"
INLINE_TOKEN_BUDGET = 1000
MAX_SIDE_EFFECT_LINES = 12


def approx_tokens(text):
    return (len(text) + 3) // 4


def _fmt_age(sec):
    if sec < 120:
        return f"{int(sec)}s"
    if sec < 7200:
        return f"{int(sec // 60)}m"
    return f"{int(sec // 3600)}h"


def _context_suffix(c, space):
    """ADR-005 §5: the current user message closes the prompt with a
    timestamp + ui-context suffix ('here'/'this page' resolve against
    the view line). Best-effort — a missing or unreadable pointer
    degrades to timestamp-only. The suffix rides the llm message only;
    the persisted turn keeps the raw userText."""
    epoch = now()  # noqa: F821 - guest global
    stamp = datetime.datetime.fromtimestamp(
        int(epoch), datetime.UTC).strftime("%a %Y-%m-%d %H:%M UTC")
    line = f"\n\n[now: {stamp}"
    try:
        ctx = c.get_ui_context(space)
    except Exception:
        ctx = None
    if ctx and ctx.get("spaceId"):
        age = _fmt_age(max(0, epoch - ctx["updatedAt"] / 1000.0))
        line += (f" | user's view — space: {ctx['spaceId']}"
                 + (f", object: {ctx['objectId']}" if ctx.get("objectId") else "")
                 + (f", view: {ctx['view']}" if ctx.get("view") else "")
                 + f", {age} ago")
    return line + "]"


# --- digest (progressive disclosure over subcell results) --------------------

def _render_value(cell_id, meta, i):
    if approx_tokens(meta["repr"]) <= INLINE_TOKEN_BUDGET:
        return meta["repr"]
    sel = f'values.get("{cell_id}", {i!r})'
    return f"[{meta['size']} bytes, {meta['schema']} — {sel} to walk]"


def _side_effects(entries):
    entries = [e for e in entries
               if e["effect"] not in ("trace.effects_of", "trace.effect_get")]
    if not entries:
        return ""
    counts = {}
    mutations = []
    for e in entries:
        counts[e["effect"]] = counts.get(e["effect"], 0) + 1
        if e.get("class") == "mutate":
            mutations.append(e)
    lines = [f"{name} ×{n}" for name, n in sorted(counts.items())]
    for m in mutations[:MAX_SIDE_EFFECT_LINES]:
        lines.append(f"  mutate {m['effect']} #{m['seq']}")
    return "Side effects: " + ", ".join(lines[:MAX_SIDE_EFFECT_LINES])


def _hints(entries):
    counts = {}
    for e in entries:
        counts[e["effect"]] = counts.get(e["effect"], 0) + 1
    return [f"hint: {n}× sequential {name} — one round-trip via "
            f'effect("batch", {{"name": "{name}", "payloads": [...]}})'
            for name, n in counts.items() if n >= 4]


def render_digest(cell_id, cr, entries):
    parts = []
    if cr["prints"]:
        parts.append("Output:\n" + "\n".join(
            f"#{i} {_render_value(cell_id, m, i)}" for i, m in enumerate(cr["prints"])))
    if cr["last"] is not None:
        parts.append("Last value: " + _render_value(cell_id, cr["last"], "last"))
    se = _side_effects(entries)
    if se:
        parts.append(se)
    if cr["error"]:
        tb = "\n" + cr["error"].get("traceback", "") if cr["error"].get("traceback") else ""
        parts.append(f"Error: {cr['error']['type']}: {cr['error']['message']}{tb}")
    parts.extend(_hints(entries))
    return "\n\n".join(parts) or "(no output)"


# --- the loop -----------------------------------------------------------------

def _texts(parts):
    return [p["text"] for p in parts if p["type"] == "text"]


def _wrapup(messages, llm, system, tier, reason):
    messages.append({"role": "user", "parts": [{"type": "text", "text":
        f"[{reason}] No more cells. Summarize what you did, what is done, "
        f"and what is still pending."}]})
    reply = llm.chat(messages, system=system, tier=tier, tools=[])
    messages.append({"role": "assistant", "parts": reply["parts"]})
    return _texts(reply["parts"])


def _run_model_cells(parts, results):
    for part in parts:
        if part["type"] != "tool_call":
            continue
        cid = part["id"]
        sid = effect("span.begin",  # noqa: F821 - guest global
                     {"name": "cell", "input": {"cell": cid}})["span"]
        cr = subcell(part["args"].get("code", ""), cid)  # noqa: F821
        effect("span.end", {"ok": cr["ok"]})  # noqa: F821
        entries = effect("trace.effects_of",  # noqa: F821
                         {"span": sid})["records"]
        results.append({"type": "tool_result", "call_id": cid,
                        "content": render_digest(cid, cr, entries),
                        "is_error": not cr["ok"]})


def main(args):
    space, chat_id = args["space"], args["chatId"]
    user_text = args["userText"]
    tier = args.get("tier", TIER)
    max_turns = args.get("maxTurns", MAX_TURNS)
    max_tokens = args.get("maxTokensTotal", MAX_TOKENS_TOTAL)
    agent_name = args.get("agentName", "bao")
    # runtime context (ADR-005 §5): the ids the model must never guess.
    # Appended guest-side — the host composes no prompt wording. Stable
    # per instance, so the cached stable prefix is unaffected.
    system = args.get("system", "") + (
        "\n\n## Runtime context\n\n"
        f"- agent space: `{space}` (your chat, history, and brain live here)\n"
        f"- chat object: `{chat_id}`\n"
        f"- agent name: {agent_name}\n"
        "- other spaces: `c.list_spaces()`; the user's live view rides the "
        "newest user message as a `[now: … | user's view — …]` line")

    c = use("any@v1").client()  # noqa: F821 - guest global
    llm = use("llm@v1")  # noqa: F821
    hist = use("history@v1")  # noqa: F821
    ar = use("autorecall@v1")  # noqa: F821

    # boot window (recency channel) + auto-recall (topical channel)
    turns = list(reversed(hist.recent_turns(c, space, chat_id, 200)))
    chunks = {}
    for lvl in (1, 2, 3):
        got = list(reversed(hist.chunks_at_level(c, space, chat_id, lvl, 100)))
        if got:
            chunks[lvl] = got
    boot = hist.render_boot_window(turns, chunks,
                                   total_tokens=args.get("bootTokens", 40000))
    tail = hist.raw_tail(turns, total_tokens=args.get("bootTokens", 40000))
    boot_min_seq = tail[0].get("seq") if tail else None
    plan = ar.plan(c, space, user_text, boot_min_seq)

    messages = [*boot,
                {"role": "user",
                 "parts": [{"type": "text",
                            "text": user_text + _context_suffix(c, space)}]},
                *plan["messages"]]

    def bubble(text, done):
        if text:
            c.chat_send(space, chat_id, {"text": text,
                                         "agent": {"name": agent_name, "done": done}})

    tokens = 0
    turn = 0
    stop = "done"
    replies = []
    while True:
        wrapup_reason = None
        for msg in effect("mailbox.drain", {})["items"]:  # noqa: F821
            if msg["kind"] == "inject":
                messages.append({"role": "user",
                                 "parts": [{"type": "text", "text": msg["text"]}]})
            elif msg["kind"] == "break":
                wrapup_reason = "user asked to wrap up"
        if turn >= max_turns:
            wrapup_reason = wrapup_reason or f"turn ceiling ({max_turns})"
        if tokens >= max_tokens:
            wrapup_reason = wrapup_reason or f"token ceiling ({max_tokens})"
        if wrapup_reason:
            replies = _wrapup(messages, llm, system, tier, wrapup_reason)
            stop = "wrapup"
            bubble("\n".join(replies), True)
            break

        turn += 1
        reply = llm.chat(messages, system=system, tier=tier, tools=[RUN_CELL_TOOL])
        tokens += reply.get("usage", {}).get("in", 0) + reply.get("usage", {}).get("out", 0)
        messages.append({"role": "assistant", "parts": reply["parts"]})

        if reply["stop"] == "done":
            replies = _texts(reply["parts"])
            bubble("\n".join(replies), True)
            break
        if reply["stop"] == "length":
            replies = _wrapup(messages, llm, system, tier, "response length limit")
            stop = "wrapup"
            bubble("\n".join(replies), True)
            break
        if reply["stop"] != "tool":
            raise RuntimeError(f"unhandled stop reason: {reply['stop']}")

        for t in _texts(reply["parts"]):  # interim text = progress bubble
            bubble(t, False)
        results = []
        _run_model_cells(reply["parts"], results)
        messages.append({"role": "user", "parts": results})

    c.append_turn(space, chat_id, {
        "userText": user_text, "replies": replies, "interrupted": False,
        "traceRef": args.get("traceRef", ""), "fromAgent": agent_name,
        "llm": {"stopReason": stop, "tokensIn": tokens}})
    if plan["injected"]:
        ar.log_roi(c, space, plan["injected"], replies, now())  # noqa: F821
    return {"stop": stop, "turns": turn, "tokens": tokens,
            "replies": replies, "injected": len(plan["injected"])}
