"""Loop core, M0 skeleton — ADR-005's turn cycle over the neutral
message model. `llm.chat` is an effect (the whole conversation replays
deterministically). M2 adds ceilings/mailbox/digest budgets; here the
digest is v0 (prints/last/error, deterministic — no timings in model-
facing content, so replay keys stay stable).
"""

from __future__ import annotations

from typing import Any

from anyrt.effects import Broker
from anyrt.executor import Executor

from . import digest


def run_conversation(
    user_text: str,
    *,
    broker: Broker,
    executor: Executor,
    system: str = "",
    tier: str = "codegen",
    max_turns: int = 16,
) -> list[str]:
    """One invocation: user message -> cells -> final replies."""
    messages: list[dict[str, Any]] = [
        {"role": "user", "parts": [{"type": "text", "text": user_text}]}
    ]
    for _ in range(max_turns):
        reply = broker.call(
            "llm.chat", {"messages": messages, "system": system, "tier": tier}
        )
        messages.append({"role": "assistant", "parts": reply["parts"]})
        if reply["stop"] == "done":
            return [p["text"] for p in reply["parts"] if p["type"] == "text"]
        if reply["stop"] != "tool":
            raise RuntimeError(f"unhandled stop reason: {reply['stop']}")
        results = []
        for part in reply["parts"]:
            if part["type"] != "tool_call":
                continue
            cr = executor.run_cell(part["args"]["code"], cell_id=part["id"])
            content = digest.render(
                cr, broker.writer.records, hints=digest.teaching_hints(cr, broker.writer.records)
            )
            results.append(
                {
                    "type": "tool_result",
                    "call_id": part["id"],
                    "content": content,
                    "is_error": not cr.ok,
                }
            )
        messages.append({"role": "user", "parts": results})
    raise RuntimeError("max_turns ceiling hit without done")
