"""Loop core — ADR-005 §2/§3. The successor of toolcall_core's loop as
harness code. Pure over (history, policy, mailbox) with ALL externality
through effects (llm.chat, executor cells, chat.send) — so a recorded
conversation replays the whole loop deterministically.

M2 scope: turn cycle, ceilings→wrap-up, mailbox inject/break, progress
bubbles. Turn-record persistence is M4 (needs anyclient/agentlog).
"""

from __future__ import annotations

import queue
from dataclasses import dataclass, field
from typing import Any

from anyrt.effects import Broker
from anyrt.executor import Executor

from . import digest

RUN_CELL_TOOL = {
    "name": "run_cell",
    "description": "Execute a Python cell in the persistent kernel.",
}


@dataclass
class LoopPolicy:
    max_turns: int = 108
    max_tokens_total: int = 1_000_000
    max_cost_total: float | None = None
    tier: str = "codegen"


@dataclass
class Mailbox:
    """External control (ADR-005 §3). Thread-safe: the watcher/operator
    pushes from another thread; the loop drains between turns and after
    each cell."""

    _q: queue.Queue = field(default_factory=queue.Queue)

    def inject(self, text: str) -> None:
        self._q.put({"kind": "inject", "text": text})

    def break_soft(self) -> None:
        self._q.put({"kind": "break", "hard": False})

    def break_hard(self) -> None:
        self._q.put({"kind": "break", "hard": True})

    def drain(self) -> list[dict]:
        out = []
        try:
            while True:
                out.append(self._q.get_nowait())
        except queue.Empty:
            pass
        return out


@dataclass
class Outcome:
    replies: list[str]
    stop: str          # done | wrapup | break_hard
    turns: int
    tokens: int


def _text(parts: list[dict]) -> list[str]:
    return [p["text"] for p in parts if p["type"] == "text"]


def run_conversation(
    user_text: str,
    *,
    broker: Broker,
    executor: Executor,
    system: str = "",
    policy: LoopPolicy | None = None,
    mailbox: Mailbox | None = None,
) -> Outcome:
    policy = policy or LoopPolicy()
    mailbox = mailbox or Mailbox()
    messages: list[dict[str, Any]] = [
        {"role": "user", "parts": [{"type": "text", "text": user_text}]}
    ]
    tokens = 0
    have_chat = _has_effect(broker, "chat.send")

    def bubble(text: str, done: bool) -> None:
        if have_chat and text:
            broker.call("chat.send", {"text": text, "done": done})

    turn = 0
    while True:
        # drain mailbox between turns (ADR-005 §3)
        wrapup_reason = None
        for msg in mailbox.drain():
            if msg["kind"] == "inject":
                messages.append({"role": "user", "parts": [{"type": "text", "text": msg["text"]}]})
            elif msg["kind"] == "break" and msg["hard"]:
                executor.interrupt()
                bubble("Stopped.", done=True)
                return Outcome(["Stopped."], "break_hard", turn, tokens)
            elif msg["kind"] == "break":
                wrapup_reason = "user asked to wrap up"

        # ceilings (ADR-005 §3): hit -> wrap-up turn, never a silent cut
        if turn >= policy.max_turns:
            wrapup_reason = wrapup_reason or f"turn ceiling ({policy.max_turns})"
        if tokens >= policy.max_tokens_total:
            wrapup_reason = wrapup_reason or f"token ceiling ({policy.max_tokens_total})"

        if wrapup_reason:
            replies = _wrapup(messages, broker, system, policy, wrapup_reason)
            bubble("\n".join(replies), done=True)
            return Outcome(replies, "wrapup", turn, tokens)

        turn += 1
        reply = broker.call(
            "llm.chat",
            {"messages": messages, "system": system, "tier": policy.tier,
             "tools": [RUN_CELL_TOOL]},
        )
        tokens += reply.get("usage", {}).get("in", 0) + reply.get("usage", {}).get("out", 0)
        messages.append({"role": "assistant", "parts": reply["parts"]})

        if reply["stop"] == "done":
            replies = _text(reply["parts"])
            bubble("\n".join(replies), done=True)
            return Outcome(replies, "done", turn, tokens)

        if reply["stop"] == "length":
            # dangling tool calls -> is_error; ask for text-only wrap-up
            replies = _wrapup(messages, broker, system, policy, "response length limit")
            bubble("\n".join(replies), done=True)
            return Outcome(replies, "wrapup", turn, tokens)

        if reply["stop"] != "tool":
            raise RuntimeError(f"unhandled stop reason: {reply['stop']}")

        # interim assistant text = progress bubble (v1 behavior)
        for t in _text(reply["parts"]):
            bubble(t, done=False)

        results = []
        for part in reply["parts"]:
            if part["type"] != "tool_call":
                continue
            cr = executor.run_cell(part["args"]["code"], cell_id=part["id"])
            content = digest.render(
                cr, broker.writer.records, hints=digest.teaching_hints(cr, broker.writer.records)
            )
            results.append(
                {"type": "tool_result", "call_id": part["id"],
                 "content": content, "is_error": not cr.ok}
            )
        messages.append({"role": "user", "parts": results})


def _wrapup(
    messages: list[dict], broker: Broker, system: str, policy: LoopPolicy, reason: str
) -> list[str]:
    """One final constrained call — no cells, summarize state (ADR-005
    §3 no-lossy-truncation: caps produce a wrap-up, not a cut)."""
    messages.append(
        {"role": "user", "parts": [{"type": "text", "text":
            f"[{reason}] No more cells. Summarize what you did, what is done, "
            f"and what is still pending."}]}
    )
    reply = broker.call(
        "llm.chat",
        {"messages": messages, "system": system, "tier": policy.tier, "tools": []},
    )
    messages.append({"role": "assistant", "parts": reply["parts"]})
    return _text(reply["parts"])


def _has_effect(broker: Broker, name: str) -> bool:
    try:
        broker.registry.get(name)
        return True
    except Exception:
        return False
