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

import hashlib
import json
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
            "mockref": {
                "type": "string",
                "description": "Run this cell against the effects recorded in "
                               "that past run (a run id from effects.runs) "
                               "instead of live ones: same inputs → recorded "
                               "outputs, nothing executed; a call the run never "
                               "made fails. Sugar for mock={\"from\": ref}.",
            },
            "mock": {
                "type": "object",
                "description": "Recorded/scripted effects for this cell only: "
                               "{from: run id(s), only: [globs], except: [globs], "
                               "records: [{effect, input?, output|error, repeat?}], "
                               "unmatched: fail|live}. Globs match effect names "
                               "(http.get, time.now) AND the facade the call runs "
                               "under (any.*, github.*): except: [\"any.*\"] keeps "
                               "every any call live. Outside only/except a call "
                               "runs live. The result is NOT a live verification "
                               "— it says so.",
            },
        },
        "required": ["code"],
    },
}
# `prompt_style: "compact"` (ADR-005 §1.3 / §5): the same tool, said
# shorter — for models that follow a short instruction better
RUN_CELL_TOOL_COMPACT = {
    **RUN_CELL_TOOL,
    "description": (
        "Run a Python cell. Variables and use() modules persist across "
        "cells. Returns printed output, the last expression, and a "
        "side-effects summary. Reply with text only (no tool call) to end."),
}
# The second tool, offered only when the runtime has shell effects
# (ADR-024 §4, ADR-005 §2 amendment): one command, raw output, the
# result bound in the kernel for run_cell.
BASH_TOOL = {
    "name": "bash",
    "description": (
        "Run ONE bash command line on the machine bao runs on (bash -c, in "
        "the user's login environment) and read its output raw: stdout, then stderr, "
        "then an [exit N] line only when non-zero (a timeout is reported, "
        "not raised). The result is also bound in the kernel as `sh.last` "
        "(and as `as`, when given) so a following run_cell can process it — "
        "never paste output back into a cell. Absolute paths: no working "
        "directory carries over between calls. Keep output bounded (`| head "
        "-100`, `rg` rather than `cat`); default timeout 120 s. Long-running "
        "or interactive work belongs in a tmux session driven from here."),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string",
                        "description": "The command line, exactly as typed in a terminal."},
            "cwd": {"type": "string",
                    "description": "Absolute working directory for this command."},
            "timeout_s": {"type": "number",
                          "description": "Seconds before the command is killed (default 120)."},
            "as": {"type": "string",
                   "description": "Also bind the result to this variable name for run_cell."},
        },
        "required": ["command"],
    },
}
MAX_TURNS = 300
MAX_TOKENS_TOTAL = 1_000_000
TIER = "codegen"
# a call whose input exceeds this share of the profile's context window
# ends the run with a wrap-up — before the next call fails (§1.3)
CONTEXT_FULL_SHARE = 0.85
INLINE_TOKEN_BUDGET = 1000
MAX_SIDE_EFFECT_LINES = 12
# bash tool results are raw text, not values: a wider inline budget,
# head + tail past it (the whole text stays on sh.last.out)
BASH_HEAD_CHARS = 12000
BASH_TAIL_CHARS = 4000
BASH_STDERR_CHARS = 3000
# Fixed order of the built-in system skills in the prompt; unknown _-skills
# sort after these. `_coding` is composed only with the shell feature.
# `_soul` is not in the band: it is the identity, rendered verbatim as
# the first bytes of the system block (ADR-005 §5).
SYSTEM_SKILL_ORDER = ["_core", "_any", "_coding", "_memory",
                      "_space_context", "_meta_skill"]
IDENTITY_SKILL = "_soul"
# a pasted essay must not eat the prompt: head kept, a marker names the cut
IDENTITY_TOKEN_CAP = 2000
# The bash tool's `as=` pre-check only; the kernel is the authority
# (`_KERNEL_NAMES`, ADR-003 §3) and refuses any cell that rebinds one.
_RESERVED_NAMES = {"sh", "fs", "values", "effects", "use", "http", "print",
                   "effect", "subcell", "span", "help"}


def _shell_info():
    """`runtime.get("shell")` → `{cwd, home, shell, os}` when the binary
    has shell effects, null without (ADR-024 §4/§6) → None: decides the
    tool set and whether `_coding` is composed. One recorded read per
    run, a clean one either way."""
    try:
        return effect("runtime.get", {"key": "shell"}).get("value") or None  # noqa: F821
    except Exception:
        return None


def approx_tokens(text):
    return (len(text) + 3) // 4


APPS_CAP = 15


def _apps_lines(c, space, ui_ctx):
    """The installed apps of the agent space and of the user's current
    space (ADR-027 §5) — one `name (usecase): description` per app,
    capped; beyond the cap the model calls `list_apps`. Apps are data:
    the line is what lets the model know a wiki or a CRM exists
    without guessing. A space whose list cannot be read contributes
    nothing (the model can still ask)."""
    out = []
    seen = []
    for label, sid in (("agent space", space),
                       ("user's space", (ui_ctx or {}).get("spaceId"))):
        if not sid or sid in seen:
            continue
        seen.append(sid)
        try:
            apps = [a for a in c.list_apps(sid) if not a.get("hidden")]
        except Exception:  # noqa: BLE001 — an unreadable list is not a run failure
            continue
        if not apps:
            out.append(f"- apps in the {label}: none installed (`list_available_apps`)")
            continue
        items = []
        for a in apps[:APPS_CAP]:
            item = a.get("name") or a.get("bundleId") or "?"
            if a.get("usecase"):
                item += f" ({a['usecase']})"
            if a.get("description"):
                item += f": {a['description']}"
            items.append(item)
        more = f"; +{len(apps) - APPS_CAP} more (`list_apps`)" if len(apps) > APPS_CAP else ""
        out.append(f"- apps in the {label}: " + "; ".join(items) + more)
    return "".join(line + "\n" for line in out)


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


_TRACE_VIEWS = ("trace.effects_of", "trace.effect_get", "trace.runs", "trace.stats",
                "trace.query")


def _mock_state(e):
    """A digest row's mock state (ADR-028 §5): an effect row is `mocked`
    or `live` (its `mocked` flag); a facade span row is `mocked` when
    every inner effect was served, `live` when none, else `mixed`, from
    the served count `mocked` over `effects`."""
    if e.get("effect"):
        return "mocked" if e.get("mocked") is True else "live"
    served = e.get("mocked") or 0
    total = e.get("effects") or 0
    if not served:
        return "live"
    return "mocked" if served >= total else "mixed"


def _mock_suffix(e):
    st = _mock_state(e)
    if st == "mixed":
        served = e.get("mocked") or 0
        return f" (mixed: {served} mocked, {(e.get('effects') or 0) - served} live)"
    return f" ({st})"


def _side_effects(entries, mocked=False):
    entries = [e for e in entries if e.get("effect") not in _TRACE_VIEWS]
    if not entries:
        return ""
    counts = {}
    states = {}
    mutations = []
    failures = []
    for e in entries:
        name = _op_name(e)
        counts[name] = counts.get(name, 0) + 1
        states.setdefault(name, set()).add(_mock_state(e))
        # `class` is boundary truth for both rows: a raw mutate effect, or a
        # span whose inner effects mutated (meta.mutations) — not meta.kind.
        if e.get("class") == "mutate":
            mutations.append(e)
        if e.get("error"):
            failures.append(e)
    if mocked:
        # one suffix per op: a lone row says its own state (a facade's
        # mixed count included); several rows agree, or the op is mixed
        def suffix(name):
            rows = [e for e in entries if _op_name(e) == name]
            if len(rows) == 1:
                return _mock_suffix(rows[0])
            st = states[name]
            return f" ({st.pop()})" if len(st) == 1 else " (mixed)"
        lines = [f"{name} ×{n}{suffix(name)}" for name, n in sorted(counts.items())]
    else:
        lines = [f"{name} ×{n}" for name, n in sorted(counts.items())]
    for m in mutations[:MAX_SIDE_EFFECT_LINES]:
        if mocked and _mock_state(m) != "live":
            # a mocked mutation was served, not executed — never `mutate`
            lines.append(f"  would mutate {_op_name(m)} #{m['seq']} (mocked: NOT executed)")
        else:
            lines.append(f"  mutate {_op_name(m)} #{m['seq']}")
    for f in failures[:MAX_SIDE_EFFECT_LINES]:
        lines.append(f"  failed {_op_name(f)} #{f['seq']}: {f['error']}")
    return "Side effects: " + ", ".join(lines[:MAX_SIDE_EFFECT_LINES])


def _mock_header(entries, mock, filter_hits=None):
    """The line a mocked cell's digest ALWAYS opens with (ADR-028 §5) —
    including `0 of N`: a wrong glob or stale keys must be visible. A
    second line WARNS when an only/except glob matched no call in the
    cell (`filter_hits` = the span end's `mockFilter` counts): a
    narrowing glob that matches nothing silently widens to everything."""
    rows = [e for e in entries if e.get("effect") not in _TRACE_VIEWS]
    served = [e for e in rows if _mock_state(e) == "mocked"]
    n_served = sum((e.get("mocked") or 0) if not e.get("effect") else 1
                   for e in rows if _mock_state(e) != "live")
    n_total = sum((e.get("effects") or 0) if not e.get("effect") else 1 for e in rows)
    sources = []
    if mock.get("from"):
        refs = mock["from"] if isinstance(mock["from"], list) else [mock["from"]]
        sources.append(", ".join(refs))
    if mock.get("records"):
        sources.append(f"{len(mock['records'])} inline record(s)")
    counts = {}
    for e in served:
        counts[_op_name(e)] = counts.get(_op_name(e), 0) + 1
    by_op = ", ".join(f"{k} ×{v}" for k, v in sorted(counts.items()))
    line = (f"[MOCK] {n_served} of {n_total} effects served from "
            f"{' + '.join(sources) or 'nothing'}" + (f" ({by_op})" if by_op else ""))
    live = [e for e in rows if e.get("effect") and e.get("unmatched")]
    if live:
        line += "; " + f"{len(live)} live: " + ", ".join(
            f"{_op_name(e)} #{e['seq']}" for e in live[:MAX_SIDE_EFFECT_LINES])
    hits = filter_hits or {}
    for key in ("only", "except"):
        if mock.get(key) and n_total and hits.get(key) == 0:
            line += (f"\nWARNING: {key}: {json.dumps(mock[key])} matched no call in this "
                     f"cell — " + ("every effect ran live" if key == "only"
                                   else "nothing was kept live by it")
                     + " (globs match effect names and facade names like any.*)")
    return line


MOCK_GUARD = ("Values above came from recorded effects, not live data. Nothing marked "
              "mocked was executed or written. Re-run without `mock` to do it for real.")


class MockArgError(ValueError):
    """The tool call's mock arguments are malformed — an is_error
    result, never a silent live cell (ADR-028 §5)."""


def _mock_spec(args):
    """`mock` (the spec) or `mockref` (sugar for {from: ref}); None when
    neither — the tool's ordinary live cell. A `mock` that is not an
    object (a JSON string, a list) or a `mockref` that is not a string
    raises MockArgError: dropping it would run the cell live under a
    request for recorded effects."""
    mock = args.get("mock")
    ref = args.get("mockref")
    if mock is not None and not isinstance(mock, dict):
        raise MockArgError(f"mock must be a JSON object, got {type(mock).__name__}"
                           + (" (a JSON string — pass the object itself)"
                              if isinstance(mock, str) else ""))
    if ref is not None and not isinstance(ref, str):
        raise MockArgError(f"mockref must be a run id string, got {type(ref).__name__}")
    if mock is None and ref:
        mock = {"from": ref}
    return mock or None


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


def _clip(text, head, tail, where):
    if len(text) <= head + tail:
        return text
    gone = len(text) - head - tail
    return (text[:head] + f"\n[… {gone} chars elided — the full text is on {where} …]\n"
            + text[-tail:])


def _bash_code(a):
    """The cell a bash tool call runs: `sh(command, cwd=, timeout_s=)`,
    optionally bound to `as` (an identifier that isn't a kernel name);
    the result is the cell's last value either way."""
    kw = "".join(f", {k}={a[k]!r}" for k in ("cwd", "timeout_s") if a.get(k) is not None)
    call = f"sh({a.get('command', '')!r}{kw})"
    name = (a.get("as") or "").strip()
    if name.isidentifier() and name not in _RESERVED_NAMES:
        return f"{name} = {call}\n{name}", name
    return call, None


def render_bash(cr, res, bound):
    """A bash tool result reads like a terminal (ADR-024 §4): stdout,
    then stderr, an exit/timeout line only when there is one, and the
    footer naming the kernel binding so the model reaches for
    `sh.last` instead of re-pasting."""
    if cr["error"]:
        tb = "\n" + cr["error"].get("traceback", "") if cr["error"].get("traceback") else ""
        return f"Error: {cr['error']['type']}: {cr['error']['message']}{tb}"
    parts = []
    out = (getattr(res, "out", "") or "").rstrip("\n")
    err = (getattr(res, "err", "") or "").rstrip("\n")
    if out:
        parts.append(_clip(out, BASH_HEAD_CHARS, BASH_TAIL_CHARS, "sh.last.out"))
    if err:
        parts.append("[stderr]\n" + _clip(err, BASH_STDERR_CHARS, BASH_STDERR_CHARS,
                                          "sh.last.err"))
    if getattr(res, "timed_out", False):
        parts.append(f"[timed out after {getattr(res, 'duration_ms', '?')} ms — "
                     "partial output above]")
    elif getattr(res, "interrupted", False):
        parts.append("[interrupted — partial output above]")
    elif getattr(res, "code", 0) != 0:
        parts.append(f"[exit {getattr(res, 'code', None)}]")
    if getattr(res, "truncated", False):
        parts.append("[output over the 1 MiB capture cap — head and tail kept]")
    if not parts:
        parts.append("(no output)")
    parts.append("→ sh.last" + (f" (also `{bound}`)" if bound else ""))
    return "\n".join(parts)


def render_digest(cell_id, cr, entries, mock=None, filter_hits=None):
    parts = []
    if mock is not None:
        parts.append(_mock_header(entries, mock, filter_hits))
    if cr["prints"]:
        parts.append("Output:\n" + "\n".join(
            f"#{i} {_render_value(cell_id, m, i)}" for i, m in enumerate(cr["prints"])))
    if cr["last"] is not None:
        parts.append("Last value: " + _render_value(cell_id, cr["last"], "last"))
    se = _side_effects(entries, mocked=mock is not None)
    if se:
        parts.append(se)
    if cr["error"]:
        tb = "\n" + cr["error"].get("traceback", "") if cr["error"].get("traceback") else ""
        parts.append(f"Error: {cr['error']['type']}: {cr['error']['message']}{tb}")
    parts.extend(_hints(entries))
    if mock is not None:
        parts.append(MOCK_GUARD)
    return "\n\n".join(parts) or "(no output)"


# --- the loop -----------------------------------------------------------------

def _texts(parts):
    return [p["text"] for p in parts if p["type"] == "text"]


def _tally(stats, usage):
    # llm@v1 usage keys -> api.LLMStats keys (the agent_turns contract)
    for src, dst in (("in", "inTokens"), ("out", "outTokens"),
                     ("cacheRead", "cacheRead"), ("cacheWrite", "cacheWrite")):
        stats[dst] += usage.get(src, 0)


def _dangling(messages, reason):
    """Synthetic error results for tool calls in the last assistant
    message that never ran — the provider rejects a tool_use with no
    tool_result at the head of the next message (ADR-005 §2)."""
    last = messages[-1] if messages else {}
    if last.get("role") != "assistant":
        return []
    return [{"type": "tool_result", "call_id": p["id"],
             "content": f"not executed: {reason}", "is_error": True}
            for p in last["parts"] if p["type"] == "tool_call"]


def _wrapup(messages, llm, system, tier, reason, stats, tools):
    # The wrap-up call keeps the SAME tool list as every other turn:
    # the tools are part of the cached prompt prefix, and dropping them
    # here made the biggest prompt of the run — the last one — a full
    # cache miss (105k uncached tokens, 7% of a 109-turn run's cost).
    # Text-only is asked for, not enforced by the wire; a model that
    # answers with a tool call anyway gets one more, tool-less call.
    parts = _dangling(messages, reason)
    parts.append({"type": "text", "text":
        f"[{reason}] No more cells. Summarize what you did, what is done, "
        f"and what is still pending."})
    messages.append({"role": "user", "parts": parts})
    reply = llm.chat(messages, system=system, tier=tier, tools=tools)
    _tally(stats, reply.get("usage", {}))
    messages.append({"role": "assistant", "parts": reply["parts"]})
    texts = _texts(reply["parts"])
    if not texts and any(p["type"] == "tool_call" for p in reply["parts"]):
        parts = _dangling(messages, reason)
        parts.append({"type": "text", "text": "Text only — no tool calls. Summarize."})
        messages.append({"role": "user", "parts": parts})
        reply = llm.chat(messages, system=system, tier=tier, tools=[])
        _tally(stats, reply.get("usage", {}))
        messages.append({"role": "assistant", "parts": reply["parts"]})
        texts = _texts(reply["parts"])
    return texts


def _run_model_cells(parts, results):
    """Run each tool_call part as a cell; a call llm@v1 flagged as
    malformed (`error`: unparseable arguments) is answered with an
    is_error result instead of a cell — the model gets to retry.
    Returns the number of malformed calls."""
    malformed = 0
    for part in parts:
        if part["type"] != "tool_call":
            continue
        cid = part["id"]
        if part.get("error"):
            malformed += 1
            results.append({"type": "tool_result", "call_id": cid,
                            "content": f"Error: {part['error']}",
                            "is_error": True})
            continue
        if part.get("name") == "bash":
            # a subcell in the same kernel (ADR-005 §2 amendment): the
            # result lands in the namespace as sh.last / `as`; rendered
            # raw from the stored object, not as a value digest
            code, bound = _bash_code(part["args"])
            effect("span.begin",  # noqa: F821 - guest global
                   {"name": "bash", "input": {"cell": cid,
                                              "command": part["args"].get("command", "")}})
            cr = subcell(code, cid)  # noqa: F821
            err = cr["error"]
            effect("span.end", {"ok": cr["ok"],  # noqa: F821
                                "error": ({"type": err["type"], "message": err["message"]}
                                          if err else None)})
            res = values.get(cid, "last") if cr["ok"] and cr["last"] is not None else None  # noqa: F821
            results.append({"type": "tool_result", "call_id": cid,
                            "content": render_bash(cr, res, bound),
                            "is_error": not cr["ok"]})
            continue
        code = part["args"].get("code", "")
        # `preview` rides into presence beats (ADR-025 §1 run.cell): the
        # status surfaces show what the cell is doing, one collapsed line
        span_input = {"cell": cid, "preview": " ".join(code.split())[:48]}
        # a mock spec rides the cell span (ADR-028 §3): the host installs
        # the index for the span's lifetime; a bad spec fails the begin
        # itself — the model gets the error, no cell runs
        try:
            mock = _mock_spec(part["args"])
        except MockArgError as e:
            results.append({"type": "tool_result", "call_id": cid,
                            "content": f"Error: mock spec rejected — {e}",
                            "is_error": True})
            continue
        if mock is not None:
            span_input["mock"] = mock
        try:
            sid = effect("span.begin",  # noqa: F821 - guest global
                         {"name": "cell", "input": span_input})["span"]
        except EffectError as e:  # noqa: F821 - guest global
            results.append({"type": "tool_result", "call_id": cid,
                            "content": f"Error: mock spec rejected — {e}",
                            "is_error": True})
            continue
        cr = subcell(code, cid)  # noqa: F821
        # the cell's failure rides its span-end record (ADR-003 §4b) —
        # type + message like @span; the traceback stays digest text
        err = cr["error"]
        # the end record's meta comes back: `mockFilter` = only/except hit
        # counts for the zero-match warning (ADR-028 §5)
        end = effect("span.end", {"ok": cr["ok"],  # noqa: F821
                                  "error": ({"type": err["type"], "message": err["message"]}
                                            if err else None)}) or {}
        entries = effect("trace.effects_of",  # noqa: F821
                         {"span": sid})["records"]
        results.append({"type": "tool_result", "call_id": cid,
                        "content": render_digest(cid, cr, entries, mock,
                                                 end.get("mockFilter")),
                        "is_error": not cr["ok"]})
    return malformed


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
    working space wins (same shadowing doctrine as programs). A BLANK
    working-space body does not shadow (ADR-005 §5): an emptied `_soul`
    falls back to the shipped identity instead of composing none."""
    code_space = code_space or space
    out = _skills_in(c, code_space)
    if code_space != space:
        out.update({n: md for n, md in _skills_in(c, space).items()
                    if (md or "").strip()})
    return out


def _identity(skills):
    """ADR-005 §5: the `_soul` body is the identity, not a skill — popped
    out of the band and rendered verbatim as the FIRST bytes of the
    system block (no heading, nothing before it). Free text: the
    harness reads no structure out of it. Capped at IDENTITY_TOKEN_CAP
    tokens, head kept. '' when the space ships no soul."""
    body = (skills.pop(IDENTITY_SKILL, "") or "").strip()
    if approx_tokens(body) > IDENTITY_TOKEN_CAP:
        body = (body[:IDENTITY_TOKEN_CAP * 4].rstrip()
                + f"\n\n[_soul cut at {IDENTITY_TOKEN_CAP} tokens — shorten the object]")
    return body


def _fingerprint(text):
    """16 hex chars of sha256 — prompt provenance (ADR-005 §5): which
    identity / which system block produced a reply is a turn-row read."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _compose_skills(skills, has_shell=False):
    """Fixed order (SYSTEM_SKILL_ORDER first, unknown _-skills sorted
    after), each trimmed, joined by blank lines. `_coding` rides only
    with the shell feature (ADR-024 §4) — no prompt tax for tools the
    binary lacks."""
    if not has_shell:
        skills = {n: s for n, s in skills.items() if n != "_coding"}
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


_TOOLS_INTRO_COMPACT = (
    "## Tools\n\n"
    "Programs reached with `use(...)` (spec on each `Import:` line). Per "
    "tool: description, then `name(signature) [kind] — summary` per method "
    "(`[getter]` reads, `[mutator]` writes, `[setup]` returns a handle). "
    "`help(mod.method)` shows the full doc — check before calling.")


def _tool_docs(c, space, code_space=None, style="full"):
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
    intro = _TOOLS_INTRO_COMPACT if style == "compact" else _TOOLS_INTRO
    return intro + "\n\n" + "\n\n".join(b for _, _, b in rows)


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
    brain = c.get_brain()   # the bao space's (ADR-017 §0) — `space` here IS it
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


def compose_system(c, space, code_space=None, overlays=None, style="full",
                   has_shell=False, identity=True):
    """The full system prompt loaded from the space(s): identity (the
    `_soul` body, first, verbatim) + skills + tool docs (both two-tier:
    agent code overlay + working space, working wins) + repo inventory
    + memory categories. Guest-side — the host injects nothing. `style`
    is the profile's `prompt_style`; `identity=False` (quiet runs,
    ADR-008 §5) composes without the soul. Returns `(system, soul)` —
    the soul body separately so the run can fingerprint it."""
    skills = _load_system_skills(c, space, code_space)
    soul = _identity(skills)          # always popped: never in the band
    if not identity:
        soul = ""
    parts = [soul,
             _compose_skills(skills, has_shell),
             _user_skills(c, space),
             _tool_docs(c, space, code_space, style),
             _repo_inventory(c, overlays,
                             code_space if code_space != space else None),
             _memory_categories(c, space)]
    return "\n\n".join(p for p in parts if p), soul


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

    # the tier's model profile (ADR-005 §1.3): the loop budgets from its
    # loop-facing traits and never sees provider or wire
    traits = llm.profile(tier)["traits"]
    run_cell = RUN_CELL_TOOL_COMPACT if traits["prompt_style"] == "compact" else RUN_CELL_TOOL
    # shell effects (ADR-024): the second tool + the _coding skill + a
    # runtime-context line exist only when the binary has them
    shell = _shell_info()
    tools = [run_cell] + ([BASH_TOOL] if shell else [])
    context_window = traits["context_window"]
    boot_tokens = min(args.get("bootTokens", 40000), context_window // 4)

    # System prompt: composed guest-side from the space (skills + tool docs
    # + memory categories) — the agent loads its own context from `any`, the
    # host injects no prompt wording. Runtime context (the ids the model must
    # never guess) is appended; stable per instance.
    system, soul = compose_system(c, space, code_space, overlays,
                                  traits["prompt_style"], bool(shell),
                                  identity=not quiet)
    ui_ctx = _view(args.get("uiContext"))
    runtime_ctx = (
        "\n\n## Runtime context\n\n"
        f"- agent space: `{space}` (your chat, history, and brain live here)\n"
        f"- chat object: `{chat_id}`\n"
        f"- agent name: {agent_name}\n"
        + _apps_lines(c, space, ui_ctx) +
        "- bound cell globals (valid spaceConfig args): `currentUserSpace` — "
        "the user's view when they sent the message (`{spaceId, objectId?, "
        "view?}` or None; the same view rides the message as a "
        "`[now: … | user's view — …]` line) — and `baoSpaceConfig` "
        "(`{spaceId, chatId}` of this agent space)\n"
        "- other spaces: `list_spaces()` rows")
    # `instructions_at: "last_user"` (§1.3): the ids ride the tail of the
    # user message for models that weight recency over the system block
    if traits["instructions_at"] == "system":
        system += runtime_ctx
        user_suffix = ""
    else:
        user_suffix = runtime_ctx
    if shell:
        system += (
            f"\n- shell: this device (`{shell.get('os')}`), serve cwd "
            f"`{shell.get('cwd')}`, home `{shell.get('home')}` — the `bash` tool "
            "and the `sh`/`fs` cell globals run here")
    if quiet:
        system += (
            "\n\n## Subagent\n\nYou are running as a subagent on a delegated "
            "task. There is no interactive user on this thread: your final "
            "reply is returned verbatim to the delegating agent — make it a "
            "complete, self-contained report.")

    # prompt provenance (ADR-005 §5): recorded on the persisted turn
    prompt_fp = _fingerprint(system)
    soul_fp = _fingerprint(soul) if soul else ""

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
        boot = hist.render_boot_window(turns, chunks, total_tokens=boot_tokens)
        tail = hist.raw_tail(turns, total_tokens=boot_tokens)
        boot_min_seq = tail[0].get("seq") if tail else None
        plan = ar.plan(c, space, user_text, boot_min_seq)

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
                            "text": user_text + _context_suffix(ui_ctx) + user_suffix}]},
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
    last_in = 0        # prompt tokens of the latest call — the context in use
    malformed = 0      # unparseable tool calls so far (traits.malformed_retries)
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
        if last_in >= CONTEXT_FULL_SHARE * context_window:
            wrapup_reason = wrapup_reason or (
                f"context window nearly full ({last_in}/{context_window})")
        if malformed > traits["malformed_retries"]:
            wrapup_reason = wrapup_reason or f"{malformed} malformed tool calls"
        if wrapup_reason:
            replies = _wrapup(messages, llm, system, tier, wrapup_reason, stats, tools)
            stop = "wrapup"
            bubble("\n".join(replies), True)
            break

        turn += 1
        reply = llm.chat(messages, system=system, tier=tier, tools=tools)
        _tally(stats, reply.get("usage", {}))
        tokens = stats["inTokens"] + stats["outTokens"]
        # usage.in is the UNCACHED prompt; the context the model holds is
        # the whole prompt — cached reads/writes included (ADR-005 §1)
        u = reply.get("usage", {})
        last_in = u.get("in", 0) + u.get("cacheRead", 0) + u.get("cacheWrite", 0)
        messages.append({"role": "assistant", "parts": reply["parts"]})

        if reply["stop"] == "done":
            replies = _texts(reply["parts"])
            bubble("\n".join(replies), True)
            break
        if reply["stop"] == "length":
            replies = _wrapup(messages, llm, system, tier,
                              "response length limit", stats, tools)
            stop = "wrapup"
            bubble("\n".join(replies), True)
            break
        if reply["stop"] != "tool":
            raise RuntimeError(f"unhandled stop reason: {reply['stop']}")

        for t in _texts(reply["parts"]):  # interim text = progress bubble
            bubble(t, False)
        results = []
        malformed += _run_model_cells(reply["parts"], results)
        stats["cells"] += len(results)
        messages.append({"role": "user", "parts": results})

    out = {"stop": stop, "turns": turn, "tokens": tokens,
           "replies": replies, "injected": len(plan["injected"])}
    if not quiet:
        # ADR-005 §3: the log append is bookkeeping AFTER the reply
        # landed — its failure (a tombstoned seq, an unreachable store)
        # is not the run's. The failed effect is in the trace; the
        # result names it so the run stays `ok` with a visible warning
        # instead of a second "Something broke" bubble for work that
        # succeeded.
        try:
            c.append_turn(space, chat_id, {
                "userText": user_text, "replies": replies, "interrupted": False,
                "traceRef": args.get("traceRef", ""), "fromAgent": agent_name,
                "llm": {"stopReason": stop, **stats,
                        "promptFingerprint": prompt_fp,
                        **({"soulFingerprint": soul_fp} if soul_fp else {})}})
            if plan["injected"]:
                ar.log_roi(c, space, plan["injected"], replies, now())  # noqa: F821
        except Exception as e:  # noqa: BLE001 - any store failure, named in the result
            out["logError"] = f"{type(e).__name__}: {e}"
    return out
