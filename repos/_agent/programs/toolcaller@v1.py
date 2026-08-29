"""The conversation loop as a guest program (ADR-005).

One invocation = main(args) driving the whole turn cycle inside the
cage: boot window + auto-recall injection, the llm loop, model cells
executed via `subcell` (each wrapped in a `cell` span so the trace
groups its effects), progressive-disclosure digests, mailbox control
drained as a recorded effect, ceilings → wrap-up, replies and the turn
record written through the any module. Everything nondeterministic is
an effect, so a recorded conversation replays whole.

args: {space, chatId, userText, uiContext?, system?, agentName?, traceRef?,
maxTurns?, maxTokensTotal?, tier?, bootTokens?, quiet?}.

quiet (ADR-008 §5): a delegated sub-run — no chat bubbles, no boot
window/auto-recall, no persisted turn/ROI, and the parent's mailbox is
left alone; ceilings still bound the run and the replies return to the
caller (the subagent@v1 wrapper).
"""

import re

# markdown-link destinations in a reply: [Name](any://…) — the source
# of auto-attachments (chips in the UI without hand-built maps)
_ANY_LINK = re.compile(r"\(\s*(any://[^\s)]+)\s*\)")


def _auto_attachments(text):
    """any:// markdown-link destinations in a reply → chat attachments.

    Every object/file link bao writes gets a preview chip for free
    (first-occurrence order, deduped, wire cap 32). Mentions (m/) and
    space links (s/) stay text-only — a chip per mention is noise.
    Kind = first path segment; a segment longer than 4 chars is a
    legacy bare id, which always means an object (doc 19 back-compat)."""
    out, seen = {}, set()
    for uri in _ANY_LINK.findall(text or ""):
        kind = uri.removeprefix("any://").split("/", 1)[0]
        if kind in ("m", "s") or uri in seen:
            continue
        seen.add(uri)
        out[f"a{len(out)}"] = {"type": "link", "link": uri}
        if len(out) >= 32:
            break
    return out


RUN_CELL_TOOL = {
    "name": "run_cell",
    "description": (
        "Execute a Python cell in the persistent kernel. State (variables, "
        "imports, use() modules) persists across cells for the whole "
        "conversation. The result is a digest: print() output, the last "
        "expression, a side-effects summary; large values collapse to a "
        "values.get(...) stub that returns the stored value in a later "
        "cell. Reply with text only (no tool call) to end the turn."),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python source: top-level statements; the "
                               "last expression is captured as the value.",
            },
        },
        "required": ["code"],
    },
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


def _view(ctx):
    """The user's view when they SENT a message — the `context` group
    any-ui stamps on the chat message (ADR-005 §5), handed in by the
    host as the run's `uiContext` arg / an inject's `context`:
    `{spaceId, objectId?, view?}` or None. Feeds both the view line
    (_context_suffix) and the bound `currentUserSpace` cell global
    (ADR-010 §8). Nothing is fetched: the message is the record of
    where the user was."""
    if isinstance(ctx, dict) and ctx.get("spaceId"):
        return {k: ctx[k] for k in ("spaceId", "objectId", "view") if ctx.get(k)}
    return None


def _context_suffix(ctx):
    """ADR-005 §5: a user message closes with a timestamp + view suffix
    ('here'/'this page' resolve against the view line). A message the
    client sent without a view degrades to timestamp-only. The suffix
    rides the llm message only; the persisted turn keeps the raw
    userText."""
    epoch = now()  # noqa: F821 - guest global
    # the host's local zone with its offset spelled out (ADR-019 §8)
    stamp = fmt_ts(epoch, "%a %Y-%m-%d %H:%M")  # noqa: F821 - guest global
    line = f"\n\n[now: {stamp}"
    if ctx and ctx.get("spaceId"):
        line += (f" | user's view — space: {ctx['spaceId']}"
                 + (f", object: {ctx['objectId']}" if ctx.get("objectId") else "")
                 + (f", view: {ctx['view']}" if ctx.get("view") else ""))
    return line + "]"


# --- digest (progressive disclosure over subcell results) --------------------

def _render_value(cell_id, meta, i):
    if approx_tokens(meta["repr"]) <= INLINE_TOKEN_BUDGET:
        return meta["repr"]
    sel = f'values.get("{cell_id}", {i!r})'
    return (f"[{meta['size']} bytes, {meta['schema']} — {sel} returns the "
            f"STORED value: walk it (fields, slices), don't re-run the "
            f"producing call; printing it whole re-elides]")


def _op_name(e):
    """Display name for a digest row — a span's facade name (`any.
    create_object`) or a bare effect's name (`http.post`). effects_of
    returns both, as immediate children of the cell (ADR-001 §4d)."""
    return e.get("name") or e.get("effect")


def _side_effects(entries):
    entries = [e for e in entries
               if e.get("effect") not in ("trace.effects_of", "trace.effect_get",
                                          "trace.runs", "trace.stats", "trace.query")]
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


def _tally(stats, usage):
    # llm@v1 usage keys -> api.LLMStats keys (the agent_turns contract)
    for src, dst in (("in", "inTokens"), ("out", "outTokens"),
                     ("cacheRead", "cacheRead"), ("cacheWrite", "cacheWrite")):
        stats[dst] += usage.get(src, 0)


def _wrapup(messages, llm, system, tier, reason, stats):
    # A length-truncated assistant reply can carry a tool_call that
    # never ran; the provider rejects a tool_use with no tool_result at
    # the head of the next message (ADR-005 §2), so answer each
    # dangling call with a synthetic error result first.
    parts = []
    last = messages[-1] if messages else {}
    if last.get("role") == "assistant":
        parts = [{"type": "tool_result", "call_id": p["id"],
                  "content": f"not executed: {reason}", "is_error": True}
                 for p in last["parts"] if p["type"] == "tool_call"]
    parts.append({"type": "text", "text":
        f"[{reason}] No more cells. Summarize what you did, what is done, "
        f"and what is still pending."})
    messages.append({"role": "user", "parts": parts})
    reply = llm.chat(messages, system=system, tier=tier, tools=[])
    _tally(stats, reply.get("usage", {}))
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
        # the cell's failure rides its span-end record (ADR-003 §4b) —
        # type + message like @span; the traceback stays digest text
        err = cr["error"]
        effect("span.end", {"ok": cr["ok"],  # noqa: F821
                            "error": ({"type": err["type"], "message": err["message"]}
                                      if err else None)})
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
    "the tool's `Import:` line. Below, per tool: its description, then one "
    "`name(signature) [kind] — summary` line per method, rendered from the "
    "code itself. `[getter]` reads, `[mutator]` writes / side effects, "
    "`[setup]` is a binder you call once to get a handle (the handle's API: "
    "`help(handle)`). Full method doc — return shape, options — via "
    "`help(mod.method)`; describe before you call, don't guess shapes.")


def _tool_docs(c, space, code_space=None):
    """`## Tools` — each any_tool program rendered by `describe()` from
    its code (ADR-010 §3): module docstring + one `name(sig) [kind] —
    summary` line per public method. ONE renderer with `help()` — the
    prompt block and an interactive `help(mod)` show the same bytes.
    Two-tier (ADR-009 §2): shipped tools from the agent code overlay
    (imported `agent:<name>@vN`), user-space tools unqualified, merged
    by name — the working space wins. Sorted oldest-first (stable tools
    stay put, new tools append) so the cached prompt prefix doesn't
    churn. A tool whose source fails to load still lists — name +
    error line — instead of sinking the whole compose."""
    code_space = code_space or space
    sources = ([(code_space, "agent:"), (space, "")]
               if code_space != space else [(space, "")])
    tools = {}
    for sp, prefix in sources:
        # `program` is a user type deploy/programs@v1 declare (ADR-010
        # §5) — a space with none has no tools, not an error
        if not any((t.get("xKey") or t.get("key")) == "program"
                   for t in c.list_types(sp)):
            continue
        for p in c.query_objects(sp, filter={"program.any_tool": True}):
            prog = p.get("program") or {}
            name = prog.get("name") or "?"
            ver = prog.get("version") or "v1"
            spec = f"{prefix}{name}@{ver}"
            try:
                # module code resolves unqualified specs in ITS defining
                # space (ADR-004 §2.4), so working-space tools (agent-
                # authored, ADR-013) are rendered via a space-qualified
                # load; the displayed Import: line stays `spec` — the
                # form cell code should use, where it resolves locally
                load = spec if prefix else f"{sp}:{name}@{ver}"
                body = describe(use(load))  # noqa: F821 - guest globals
            except Exception as e:
                body = f"(unavailable: {type(e).__name__}: {e})"
            block = [f"### {name}", f'Import: `use("{spec}")`', body]
            # dict by name: a later source (the working space) shadows
            tools[name] = (ts_s(p.get("createdAt")) or 0, name,  # noqa: F821
                           "\n\n".join(b for b in block if b))
    if not tools:
        return ""
    rows = sorted(tools.values(), key=lambda t: (t[0], t[1]))
    return _TOOLS_INTRO + "\n\n" + "\n\n".join(b for _, _, b in rows)


def _user_skills(c, space):
    """`## User skills` — the user-authored agent_skill objects of the
    working space (names NOT `_`-prefixed): title + one-line
    description + id, so a matching turn can fetch the body
    (`get_markdown`) before planning. The `_meta_skill` skill teaches
    the flow; bodies stay out of the standing prompt."""
    type_id = next((t["id"] for t in c.list_types(space)
                    if (t.get("xKey") or t.get("key")) == "agent_skill"), None)
    if not type_id:
        return ""
    lines = []
    for o in c.query_objects(space, filter={"any.types": type_id}):
        meta = o.get("any") or {}
        name = meta.get("name") or ""
        if not name or name.startswith("_"):
            continue
        desc = (meta.get("description") or "").strip().splitlines()
        lines.append(f"- **{name}** (`{o['id']}`)"
                     + (f" — {desc[0]}" if desc else ""))
    if not lines:
        return ""
    return ("## User skills\n\n"
            "User-curated playbooks (`agent_skill` objects). When the "
            "turn matches one, fetch its body FIRST — "
            "`c.get_markdown(baoSpaceConfig, \"<id>\")` — and follow "
            "it.\n\n" + "\n".join(sorted(lines)))


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


def _repo_inventory(c, overlays, code_space=None):
    """`## Repos` — the configured overlays as name + first README line
    (ADR-009 §2), the agent code overlay included as the `agent` row.
    Repo CONTENTS stay out of context — the agent browses on demand
    with `list_programs`. The program-shadowing rule rides the intro
    (it belongs to the repo concept, not to Runtime context)."""
    rows = dict(overlays or {})
    if code_space:
        rows.setdefault("agent", code_space)
    lines = []
    for name, sid in sorted(rows.items()):
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
    shadowing = (
        " Shipped harness programs live in the `agent` repo (`use("
        '"agent:<name>@vN")`); programs in your own working space import '
        "unqualified and SHADOW shipped ones by name." if code_space else "")
    return ("## Repos\n\n"
            "Configured program overlays (package repositories). Import a "
            'repo\'s program with `use("<repo>:<name>@vN")`; list what a repo '
            f"offers with `list_programs(<spaceId>)` (any@v1).{shadowing}\n\n"
            + "\n".join(lines))


def compose_system(c, space, code_space=None, overlays=None):
    """The full system prompt loaded from the space(s): skills + tool
    docs (both two-tier: agent code overlay + working space, working
    wins) + repo inventory + memory categories. Guest-side — the host
    injects nothing."""
    parts = [_compose_skills(_load_system_skills(c, space, code_space)),
             _user_skills(c, space),
             _tool_docs(c, space, code_space),
             _repo_inventory(c, overlays,
                             code_space if code_space != space else None),
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

    c = use("any@v1")  # noqa: F821 - guest global
    llm = use("llm@v1")  # noqa: F821
    hist = use("history@v1")  # noqa: F821
    ar = use("autorecall@v1")  # noqa: F821

    # System prompt: composed guest-side from the space (skills + tool docs
    # + memory categories) — the agent loads its own context from `any`, the
    # host injects no prompt wording. Runtime context (the ids the model must
    # never guess) is appended; stable per instance.
    system = compose_system(c, space, code_space, overlays) + (
        "\n\n## Runtime context\n\n"
        f"- agent space: `{space}` (your chat, history, and brain live here)\n"
        f"- chat object: `{chat_id}`\n"
        f"- agent name: {agent_name}\n"
        "- bound cell globals (valid spaceConfig args): `currentUserSpace` — "
        "the user's view when they sent the message (`{spaceId, objectId?, "
        "view?}` or None; the same view rides the message as a "
        "`[now: … | user's view — …]` line) — and `baoSpaceConfig` "
        "(`{spaceId, chatId}` of this agent space)\n"
        "- other spaces: `list_spaces()` rows")
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

    ui_ctx = _view(args.get("uiContext"))
    # bound space globals (ADR-010 §8): cell code resolves "here" the
    # same way the prompt's view line does
    ctx_code = (f"currentUserSpace = {ui_ctx!r}\n"
                f"baoSpaceConfig = {{'spaceId': {space!r}, 'chatId': {chat_id!r}}}")
    if plan["messages"]:
        # the auto-recall injection is framed as a run_cell that bound
        # `rec` (kernel state persists across cells) — make that true,
        # or reusing the example's variable NameErrors
        ctx_code += ('\nrec = use("agent:recall@v1")'
                     '.recall(use("agent:any@v1"), baoSpaceConfig)')
    subcell(ctx_code, "_ctx")  # noqa: F821 - guest global
    messages = [*boot,
                {"role": "user",
                 "parts": [{"type": "text",
                            "text": user_text + _context_suffix(ui_ctx)}]},
                *plan["messages"]]

    def bubble(text, done):
        if text and not quiet:
            body = {"text": text,
                    "agent": {"name": agent_name, "done": done}}
            atts = _auto_attachments(text)
            if atts:
                body["attachments"] = atts
            c.chat_send(space, chat_id, body)

    stats = {"inTokens": 0, "outTokens": 0,
             "cacheRead": 0, "cacheWrite": 0, "cells": 0}
    tokens = 0
    turn = 0
    stop = "done"
    replies = []
    while True:
        wrapup_reason = None
        # a quiet run must not consume the parent's inject/break stream
        for msg in ([] if quiet else effect("mailbox.drain", {})["items"]):  # noqa: F821
            if msg["kind"] == "inject":
                # a mid-run message carries its own view: the suffix
                # and the bound global follow it (ADR-005 §5)
                inj_ctx = _view(msg.get("context"))
                if inj_ctx:
                    ui_ctx = inj_ctx
                    subcell(f"currentUserSpace = {ui_ctx!r}", "_ctx")  # noqa: F821
                messages.append({"role": "user",
                                 "parts": [{"type": "text",
                                            "text": msg["text"] + _context_suffix(inj_ctx)}]})
            elif msg["kind"] == "break":
                wrapup_reason = "user asked to wrap up"
        if turn >= max_turns:
            wrapup_reason = wrapup_reason or f"turn ceiling ({max_turns})"
        if tokens >= max_tokens:
            wrapup_reason = wrapup_reason or f"token ceiling ({max_tokens})"
        if wrapup_reason:
            replies = _wrapup(messages, llm, system, tier, wrapup_reason, stats)
            stop = "wrapup"
            bubble("\n".join(replies), True)
            break

        turn += 1
        reply = llm.chat(messages, system=system, tier=tier, tools=[RUN_CELL_TOOL])
        _tally(stats, reply.get("usage", {}))
        tokens = stats["inTokens"] + stats["outTokens"]
        messages.append({"role": "assistant", "parts": reply["parts"]})

        if reply["stop"] == "done":
            replies = _texts(reply["parts"])
            bubble("\n".join(replies), True)
            break
        if reply["stop"] == "length":
            replies = _wrapup(messages, llm, system, tier,
                              "response length limit", stats)
            stop = "wrapup"
            bubble("\n".join(replies), True)
            break
        if reply["stop"] != "tool":
            raise RuntimeError(f"unhandled stop reason: {reply['stop']}")

        for t in _texts(reply["parts"]):  # interim text = progress bubble
            bubble(t, False)
        results = []
        _run_model_cells(reply["parts"], results)
        stats["cells"] += len(results)
        messages.append({"role": "user", "parts": results})

    if not quiet:
        c.append_turn(space, chat_id, {
            "userText": user_text, "replies": replies, "interrupted": False,
            "traceRef": args.get("traceRef", ""), "fromAgent": agent_name,
            "llm": {"stopReason": stop, **stats}})
        if plan["injected"]:
            ar.log_roi(c, space, plan["injected"], replies, now())  # noqa: F821
    return {"stop": stop, "turns": turn, "tokens": tokens,
            "replies": replies, "injected": len(plan["injected"])}
