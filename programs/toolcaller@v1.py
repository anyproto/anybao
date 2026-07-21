"""toolcaller@v1 — the conversation loop as a guest program (ADR-005).

One invocation = main(args) driving the whole turn cycle inside the
cage: boot window + auto-recall injection, the llm loop, model cells
executed via `subcell` (each wrapped in a `cell` span so the trace
groups its effects), progressive-disclosure digests, mailbox control
drained as a recorded effect, ceilings → wrap-up, replies and the turn
record written through the any module. Everything nondeterministic is
an effect, so a recorded conversation replays whole.

args: {space, chatId, userText, system?, agentName?, traceRef?,
maxTurns?, maxTokensTotal?, tier?, bootTokens?, quiet?}.

quiet (ADR-008 §5): a delegated sub-run — no chat bubbles, no boot
window/auto-recall, no persisted turn/ROI, and the parent's mailbox is
left alone; ceilings still bound the run and the replies return to the
caller (the subagent@v1 wrapper).
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
# Fixed order of the built-in system skills in the prompt; unknown _-skills
# sort after these.
SYSTEM_SKILL_ORDER = ["_soul", "_core", "_any", "_memory", "_space_context",
                      "_meta_skill"]


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


def _op_name(e):
    """Display name for a digest row — a span's facade name (`any.
    create_object`) or a bare effect's name (`http.post`). effects_of
    returns both, as immediate children of the cell (ADR-001 §4d)."""
    return e.get("name") or e.get("effect")


def _side_effects(entries):
    entries = [e for e in entries
               if e.get("effect") not in ("trace.effects_of", "trace.effect_get")]
    if not entries:
        return ""
    counts = {}
    mutations = []
    failures = []
    for e in entries:
        name = _op_name(e)
        counts[name] = counts.get(name, 0) + 1
        # `class` is boundary truth for both rows: a raw mutate effect, or a
        # span whose inner effects mutated (meta.mutations) — not meta.kind.
        if e.get("class") == "mutate":
            mutations.append(e)
        if e.get("error"):
            failures.append(e)
    lines = [f"{name} ×{n}" for name, n in sorted(counts.items())]
    for m in mutations[:MAX_SIDE_EFFECT_LINES]:
        lines.append(f"  mutate {_op_name(m)} #{m['seq']}")
    for f in failures[:MAX_SIDE_EFFECT_LINES]:
        lines.append(f"  failed {_op_name(f)} #{f['seq']}: {f['error']}")
    return "Side effects: " + ", ".join(lines[:MAX_SIDE_EFFECT_LINES])


def _hints(entries):
    # Batch hint targets raw syscalls, not composite facade spans.
    counts = {}
    for e in entries:
        name = e.get("effect")
        if name:
            counts[name] = counts.get(name, 0) + 1
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


# --- system prompt: composed guest-side from the space ----------------------
# The agent loads its own context from `any` (the deployed _-prefixed
# agent_skill objects + tool docs + memory categories), never the host
# filesystem — isolation principle. The host injects no prompt wording.


def _skills_in(c, space):
    """`{name: markdown}` for the _-prefixed agent_skill objects in one
    space. Returns {} if the skill type isn't there yet (fresh space)."""
    type_id = next((t["id"] for t in c.list_types(space)
                    if (t.get("xKey") or t.get("key")) == "agent_skill"), None)
    if not type_id:
        return {}
    out = {}
    for o in c.query_objects(space, filter={"any.types": type_id}):
        name = (o.get("any") or {}).get("name") or ""
        if name.startswith("_"):
            out[name] = c.get_markdown(space, o["id"])
    return out


def _load_system_skills(c, space, code_space=None):
    """Two-tier skills (ADR-009 §3): shipped skills from the agent code
    overlay, user skills from the working space, merged by name — the
    working space wins (same shadowing doctrine as programs)."""
    code_space = code_space or space
    out = _skills_in(c, code_space)
    if code_space != space:
        out.update(_skills_in(c, space))
    return out


def _compose_skills(skills):
    """Fixed order (SYSTEM_SKILL_ORDER first, unknown _-skills sorted
    after), each trimmed, joined by blank lines."""
    known = [n for n in SYSTEM_SKILL_ORDER if n in skills]
    rest = sorted(n for n in skills if n not in SYSTEM_SKILL_ORDER)
    return "\n\n".join(skills[n].strip() for n in known + rest)


_TOOLS_INTRO = (
    "## Tools\n\n"
    "Each tool is a program reached with `use(...)` — the exact spec is on "
    "the tool's `Import:` line. Below: the "
    "tool's description + a compact method SIGNATURE list — argument NAMES "
    "only, no shapes (a bare `body`/`opts` hides real structure), each "
    "tagged `[getter]` (reads), `[mutator]` (writes / side effects), or "
    "`[setup]` (a binder you call once to get a handle). Read the method's "
    "`program_methods` record for its full doc; describe before you call, "
    "don't guess shapes.")


def _method_sig(m):
    """One method's line for the tool list: `name(sig) [kind]`, the
    authored heading form (ADR-005 §5) — the kind marks read/write/bind
    intent at the point of choice. Every deployed method carries a kind
    (deploy defaults it to getter), so the tag is always present."""
    name = m.get("name", "")
    kind = m.get("kind")
    return f"{name} [{kind}]" if kind else name


def _tool_docs(c, space, code_space=None):
    """`## Tools` — each any_tool program's description + a one-line method
    SIGNATURE list (kept short: there can be many tools, and the full
    per-method schema is retrievable from program_methods on demand).
    Two-tier (ADR-009 §2): shipped tools from the agent code overlay
    (imported `agent:<name>@vN`), user-space tools unqualified, merged
    by name — the working space wins. Sorted oldest-first (stable tools
    stay put, new tools append) so the cached prompt prefix doesn't
    churn."""
    code_space = code_space or space
    sources = ([(code_space, "agent:"), (space, "")]
               if code_space != space else [(space, "")])
    tools = {}
    for sp, prefix in sources:
        for p in c.query_objects(sp, filter={"program.any_tool": True}):
            oid, prog = p["id"], (p.get("program") or {})
            name = prog.get("name") or "?"
            desc = c.query(sp, oid, "program_description")
            methods = sorted(c.query(sp, oid, "program_methods"),
                             key=lambda m: m.get("pos") or 0)
            sigs = ", ".join(_method_sig(m) for m in methods)
            spec = f"{prefix}{name}@{prog.get('version') or 'v1'}"
            block = [f"### {name}", f'Import: `use("{spec}")`']
            if desc:
                block.append(desc[0].get("text") or "")
            if sigs:
                block.append(f"Methods: {sigs}")
            # dict by name: a later source (the working space) shadows
            tools[name] = (p.get("createdAt") or 0, name, "\n\n".join(block))
    if not tools:
        return ""
    rows = sorted(tools.values(), key=lambda t: (t[0], t[1]))
    return _TOOLS_INTRO + "\n\n" + "\n\n".join(b for _, _, b in rows)


def _memory_categories(c, space):
    """The category-name inventory in the brain — the write path's
    vocabulary anchor."""
    brain = c.get_brain(space)
    brain_id = brain.get("objectId") if isinstance(brain, dict) else None
    if not brain_id:
        return ""
    items = c.query(space, brain_id, "agent_memory_items", limit=500)
    cats = sorted({i.get("category") for i in items if i.get("category")})
    return ("Memory categories in use: " + ", ".join(cats)) if cats else ""


def _repo_inventory(c, overlays):
    """`## Repos` — the configured overlays as name + first README line
    (ADR-009 §2). Repo CONTENTS stay out of context — the agent browses
    on demand with `list_programs`."""
    lines = []
    for name, sid in sorted((overlays or {}).items()):
        desc = ""
        try:
            ro = c.query_objects(sid, filter={"any.name": "README"}, limit=1)
            if ro:
                md = (c.get_markdown(sid, ro[0]["id"]) or "").strip()
                desc = next((ln.lstrip("# ").strip()
                             for ln in md.splitlines() if ln.strip()), "")
        except Exception:
            desc = ""  # a repo with no README still lists
        lines.append(f"- `{name}` (space `{sid}`)" + (f" — {desc}" if desc else ""))
    if not lines:
        return ""
    return ("## Repos\n\n"
            "Configured program overlays (package repositories). Import a "
            'repo\'s program with `use("<repo>:<name>@vN")`; list what a repo '
            "offers with `c.list_programs(<spaceId>)`.\n\n" + "\n".join(lines))


def compose_system(c, space, code_space=None, overlays=None):
    """The full system prompt loaded from the space(s): skills + tool
    docs (both two-tier: agent code overlay + working space, working
    wins) + repo inventory + memory categories. Guest-side — the host
    injects nothing."""
    parts = [_compose_skills(_load_system_skills(c, space, code_space)),
             _tool_docs(c, space, code_space), _repo_inventory(c, overlays),
             _memory_categories(c, space)]
    return "\n\n".join(p for p in parts if p)


def main(args):
    space, chat_id = args["space"], args["chatId"]
    user_text = args["userText"]
    tier = args.get("tier", TIER)
    max_turns = args.get("maxTurns", MAX_TURNS)
    max_tokens = args.get("maxTokensTotal", MAX_TOKENS_TOTAL)
    agent_name = args.get("agentName", "bao")
    quiet = args.get("quiet", False)
    code_space = args.get("codeSpace") or space
    overlays = args.get("overlays") or {}

    c = use("any@v1").client()  # noqa: F821 - guest global
    llm = use("llm@v1")  # noqa: F821
    hist = use("history@v1")  # noqa: F821
    ar = use("autorecall@v1")  # noqa: F821

    # System prompt: composed guest-side from the space (skills + tool docs
    # + memory categories) — the agent loads its own context from `any`, the
    # host injects no prompt wording. Runtime context (the ids the model must
    # never guess) is appended; stable per instance.
    code_line = (
        f"- agent code space (the `agent:` overlay): `{code_space}` — "
        'shipped programs import as `use("agent:<name>@vN")`; your own '
        "programs in the working space import unqualified and SHADOW "
        "shipped ones by name\n") if code_space != space else ""
    system = compose_system(c, space, code_space, overlays) + (
        "\n\n## Runtime context\n\n"
        f"- agent space: `{space}` (your chat, history, and brain live here)\n"
        f"- chat object: `{chat_id}`\n"
        f"- agent name: {agent_name}\n"
        + code_line +
        "- other spaces: `c.list_spaces()`; the user's live view rides the "
        "newest user message as a `[now: … | user's view — …]` line")
    if quiet:
        system += (
            "\n\n## Subagent\n\nYou are running as a subagent on a delegated "
            "task. There is no interactive user on this thread: your final "
            "reply is returned verbatim to the delegating agent — make it a "
            "complete, self-contained report.")

    # boot window (recency channel) + auto-recall (topical channel);
    # a quiet run starts fresh — only the task text (ADR-008 §5)
    if quiet:
        boot, plan = [], {"messages": [], "injected": []}
    else:
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
        if text and not quiet:
            c.chat_send(space, chat_id, {"text": text,
                                         "agent": {"name": agent_name, "done": done}})

    tokens = 0
    turn = 0
    stop = "done"
    replies = []
    while True:
        wrapup_reason = None
        # a quiet run must not consume the parent's inject/break stream
        for msg in ([] if quiet else effect("mailbox.drain", {})["items"]):  # noqa: F821
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

    if not quiet:
        c.append_turn(space, chat_id, {
            "userText": user_text, "replies": replies, "interrupted": False,
            "traceRef": args.get("traceRef", ""), "fromAgent": agent_name,
            "llm": {"stopReason": stop, "tokensIn": tokens}})
        if plan["injected"]:
            ar.log_roi(c, space, plan["injected"], replies, now())  # noqa: F821
    return {"stop": stop, "turns": turn, "tokens": tokens,
            "replies": replies, "injected": len(plan["injected"])}
