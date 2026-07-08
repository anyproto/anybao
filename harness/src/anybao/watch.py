"""The chat watcher — trigger #1 (plan §4b). A chat_messages event
trigger whose program is the conversation runner. It owns the fragility
the old hand-rolled SSE loop lacked: cursor+dedup, agent-message skip,
and mid-run routing (a message arriving while a conversation runs is
INJECTED into that conversation's mailbox, not queued as a new run —
ADR-005 loop control).

Feeding (drop-initial-snapshot SSE, cursor) is the I/O layer; this
component is the pure decision logic, offline-testable with a fake
conversation runner.
"""

from __future__ import annotations

from collections.abc import Callable

from .mailbox import Mailbox


class Watcher:
    def __init__(self, *, run_conversation: Callable[[str, str, Mailbox], object],
                 start_conversation: Callable[[str, str, Mailbox], None] | None = None):
        """`run_conversation(chat_id, user_text, mailbox)` starts a
        conversation (in prod on its own task); the watcher tracks which
        chats have a live conversation so it can route mid-run messages.
        `start_conversation` is the async spawn hook (prod); default runs
        inline (tests)."""
        self._run = run_conversation
        self._start = start_conversation or self._inline_start
        self._seen: set[str] = set()          # dedup by message id
        self._live: dict[str, Mailbox] = {}    # chat_id → mailbox of a running conversation

    def _inline_start(self, chat_id: str, text: str, mailbox: Mailbox) -> None:
        self._run(chat_id, text, mailbox)

    def on_message(self, chat_id: str, record: dict) -> str:
        """Handle one chat_messages delta. Returns the action taken:
        'skip' | 'dup' | 'inject' | 'start'."""
        msg_id = record.get("id", "")
        if msg_id and msg_id in self._seen:
            return "dup"                        # cursor/reconnect re-delivery
        if msg_id:
            self._seen.add(msg_id)
        if record.get("agent") is not None:
            return "skip"                       # agent-authored — never self-trigger
        text = record.get("text", "")

        live = self._live.get(chat_id)
        if live is not None:
            live.inject(text)                   # mid-run → mailbox (loop control)
            return "inject"

        mailbox = Mailbox()
        self._live[chat_id] = mailbox
        self._start(chat_id, text, mailbox)
        return "start"

    def conversation_done(self, chat_id: str) -> None:
        """The runner calls this when a conversation ends, so the next
        message starts fresh (or, if one is queued, is picked up)."""
        self._live.pop(chat_id, None)

    def is_live(self, chat_id: str) -> bool:
        return chat_id in self._live
