"""Conversation history — turns/chunks v2 (ADR-006 §1/§2).

Two pure pieces (offline-testable): build_turn (Outcome → agent_turns
payload) and render_boot_window (token-budgeted HIERARCHICAL composition
— newest raw turns, then L1 chunks, then L2, … until the budget fills,
so ALL history is present at decreasing resolution). Plus a thin History
wrapper over anyclient for the writes/reads. Turns/chunks live in the
USER space (agent: overlay holds code only). Rollup (turns→chunk) runs
as a cron trigger, off the completion path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .anyclient import AnyClient
from .digest import approx_tokens
from .loop import Outcome


def build_turn(
    *,
    user_text: str,
    outcome: Outcome,
    think: str = "",
    effects: list[str] | None = None,
    message_ids: list[str] | None = None,
    trace_ref: str = "",
    user_name: str = "",
    from_agent: str = "",
    llm: dict | None = None,
) -> dict:
    """The agent_turns v2 payload — seq omitted (server-assigned).
    `replies` = what the user saw; `think` = narration that did NOT go to
    chat (distinct now, ADR-006 §1). `interrupted` + neutral stopReason
    from the Outcome."""
    body: dict = {
        "userText": user_text,
        "replies": outcome.replies,
        "interrupted": outcome.stop == "break_hard",
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
    llm.setdefault("stopReason", outcome.stop)
    body["llm"] = llm
    return body


# --- boot window (token-budgeted hierarchical composition) ------------------

@dataclass
class BootWindowPolicy:
    total_tokens: int = 40_000
    raw_tail_fraction: float = 0.5     # reserve for newest raw turns
    tokenizer: Callable[[str], int] = staticmethod(approx_tokens)


def _turn_messages(turn: dict) -> list[dict]:
    """A turn → a user message (userText) + an assistant message (replies).
    Timestamp prefix is user-side only (stops the model mimicking it)."""
    msgs = []
    ut = turn.get("userText", "")
    if ut:
        msgs.append({"role": "user", "parts": [{"type": "text", "text": ut}]})
    replies = turn.get("replies", [])
    if replies:
        msgs.append({"role": "assistant",
                     "parts": [{"type": "text", "text": "\n".join(replies)}]})
    return msgs


def _chunk_line(chunk: dict) -> str:
    """A chunk → one line with its drill-down handle (seq + child range)."""
    lvl, seq = chunk.get("level", 1), chunk.get("seq")
    frm, to = chunk.get("fromSeq"), chunk.get("toSeq")
    unit = "turns" if lvl == 1 else f"L{lvl - 1} chunks"
    return f"— {chunk.get('summary', '')}  [chunk #{seq} (L{lvl}), {unit} {frm}–{to}]"


def render_boot_window(
    *,
    raw_turns: list[dict],           # ascending by seq (oldest→newest)
    chunks_by_level: dict[int, list[dict]],  # level → chunks ascending by seq
    policy: BootWindowPolicy | None = None,
) -> list[dict]:
    """Compose the boot context: newest raw turns (full resolution) up to
    the raw-tail budget, then chunks ascending by level filling the rest —
    all history at decreasing resolution, constant-size by budget.
    Skips chunks fully covered by the included raw tail (no double-cover).
    Returns neutral messages, oldest→newest, chunks as one leading
    compressed-context message."""
    policy = policy or BootWindowPolicy()
    tok = policy.tokenizer
    raw_budget = int(policy.total_tokens * policy.raw_tail_fraction)

    # newest raw turns until the raw budget fills
    included_turns: list[dict] = []
    spent = 0
    for turn in reversed(raw_turns):  # newest first
        cost = tok(turn.get("userText", "") + "\n".join(turn.get("replies", [])))
        if spent + cost > raw_budget and included_turns:
            break
        included_turns.append(turn)
        spent += cost
    included_turns.reverse()  # back to oldest→newest
    covered_min_seq = included_turns[0].get("seq", 0) if included_turns else None

    # fill the remainder with chunks ascending level, newest-first per level,
    # skipping any chunk whose range is fully inside the included raw tail
    remaining = policy.total_tokens - spent
    chunk_lines: list[str] = []
    for level in sorted(chunks_by_level):
        for chunk in reversed(chunks_by_level[level]):
            if level == 1 and covered_min_seq is not None \
                    and chunk.get("toSeq", -1) >= covered_min_seq:
                continue  # already present at full resolution
            line = _chunk_line(chunk)
            cost = tok(line)
            if cost > remaining:
                break
            chunk_lines.append(line)
            remaining -= cost

    messages: list[dict] = []
    if chunk_lines:
        chunk_lines.reverse()  # oldest→newest
        body = ("[earlier context, compressed — expand a chunk by its #seq]\n"
                + "\n".join(chunk_lines))
        messages.append({"role": "user", "parts": [{"type": "text", "text": body}]})
    for turn in included_turns:
        messages.extend(_turn_messages(turn))
    return messages


# --- I/O wrapper -------------------------------------------------------------

class History:
    def __init__(self, client: AnyClient, *, space: str, chat_id: str):
        self._c = client
        self._space = space
        self._chat = chat_id

    def append_turn(self, body: dict) -> dict:
        return self._c.append_turn(self._space, self._chat, body)

    def create_chunk(self, body: dict) -> dict:
        return self._c.create_chunk(self._space, self._chat, body)

    def recent_turns(self, limit: int) -> list[dict]:
        return self._c.query(self._space, self._chat, "agent_turns",
                             sort=["-seq"], limit=limit)

    def chunks_at_level(self, level: int, limit: int) -> list[dict]:
        return self._c.query(self._space, self._chat, "agent_chunks",
                             filter={"level": level}, sort=["-seq"], limit=limit)
