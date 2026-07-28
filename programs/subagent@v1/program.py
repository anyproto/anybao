"""Delegate a self-contained subtask to a fresh agent loop; get its
report back as a value — nothing posted to chat, no history touched.

Delegate when intermediate steps would only clutter your context (a
survey, a batch transformation, a research errand). Write the task
like a good ticket: goal, inputs (ids, names), what the report must
contain — the child starts blank and knows nothing of this
conversation. Blocking, sequential; don't nest beyond one level."""

# Thin wrapper, not a second loop (ADR-008 §5):
# use("toolcaller@v1").main(quiet=True) — same space, full tool
# surface, fresh context. No bubbles, no boot window/auto-recall, no
# persisted turn. Child cells share the kernel namespace with the
# parent's (accepted, documented in the ADR).

_MAX_TURNS = 30  # a delegated subtask, not a whole conversation


@span("subagent.delegate", kind="mutator")  # noqa: F821 - guest global
def delegate(space, task, opts=None):
    """Run `task` in a quiet child loop → {report, stop, turns, tokens}.

    `task` is a self-contained instruction string. The child composes
    its system prompt from the same space (all tools available) but
    starts with fresh context — no chat history,
    no auto-recall, and it cannot post to chat; ceilings bound the
    run (default maxTurns 30). `opts`: {"maxTurns"?, "maxTokensTotal"?,
    "tier"?, "agentName"? (default "bao-sub"), "chatId"?}. `report`
    is the child's final reply text; `stop` is "done" or "wrapup" (a
    ceiling hit — the report is then a progress summary, not a
    completion)."""
    opts = opts or {}
    task = "" if task is None else str(task)
    if not task.strip():
        return {"report": "subagent.delegate: empty task — nothing to do",
                "stop": "error", "turns": 0, "tokens": 0}
    args = {"space": space, "chatId": opts.get("chatId", ""),
            "userText": task, "quiet": True,
            "agentName": opts.get("agentName", "bao-sub"),
            "maxTurns": opts.get("maxTurns", _MAX_TURNS)}
    for k in ("maxTokensTotal", "tier", "bootTokens", "traceRef"):
        if k in opts:
            args[k] = opts[k]
    out = use("toolcaller@v1").main(args)  # noqa: F821 - guest global
    return {"report": "\n".join(out["replies"]), "stop": out["stop"],
            "turns": out["turns"], "tokens": out["tokens"]}


def main(args):
    if args and args.get("space") and args.get("task"):
        return delegate(args["space"], args["task"], args)
    return {"report": "subagent — delegate(space, task, opts?) runs a "
                      "scoped task in a quiet child loop.",
            "stop": "error", "turns": 0, "tokens": 0}
