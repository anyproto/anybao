"""Mailbox — loop control as a syscall (ADR-005 §3).

The host pushes (watcher/operator, any thread); the guest toolcaller
drains between turns via the `mailbox.drain` effect. Because the drain
is a recorded effect, injected messages and soft breaks are IN THE
TRACE — a replayed conversation replays its interruptions.

Hard break stays host-side: `break_hard()` marks the mailbox AND the
caller interrupts the executor (a wedged cell can't be asked nicely).
"""

from __future__ import annotations

import queue

from anyrt.effects import Registry, effect


class Mailbox:
    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self.hard_break = False

    def inject(self, text: str) -> None:
        self._q.put({"kind": "inject", "text": text})

    def break_soft(self) -> None:
        self._q.put({"kind": "break"})

    def break_hard(self) -> None:
        self.hard_break = True

    def drain(self) -> list[dict]:
        out: list[dict] = []
        try:
            while True:
                out.append(self._q.get_nowait())
        except queue.Empty:
            return out


def register_mailbox_effect(registry: Registry, mailbox: Mailbox) -> None:
    @effect("mailbox.drain", kind="read", registry=registry, cap="mailbox.read")
    def mailbox_drain(ctx):
        return {"items": mailbox.drain()}
