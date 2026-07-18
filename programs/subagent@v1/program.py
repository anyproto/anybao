"""subagent@v1 — delegate a scoped task to a quiet toolcaller run
(ADR-008 §5).

A thin wrapper, not a second loop: `use("toolcaller@v1").main(...)`
with `quiet: True` — same space, full tool surface, fresh context (the
task text only). No bubbles, no boot window/auto-recall, no persisted
turn; the child's final replies come back as the report. Sequential
only; child cells share the kernel namespace with the parent's
(accepted, documented in the ADR).
"""

_MAX_TURNS = 30  # a delegated subtask, not a whole conversation


@span("subagent.delegate", kind="mutator")  # noqa: F821 - guest global
def delegate(space, task, opts=None):
    """Run `task` to completion in a quiet child loop; returns
    {report, stop, turns, tokens}. `opts` may override maxTurns /
    maxTokensTotal / tier / agentName and carries chatId through for
    the child's runtime-context section."""
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
