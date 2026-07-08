"""Trigger run SSE feed — the live half of the trigger monitoring
surface (ADR-006 §4 "API surface is a monitoring tool"; m4-notes "SSE
event feed"). Subscribes to the `agent_trigger_runs` dataset on the
trigger anchor object and yields NEW run records as TriggerStore
persists them — the streaming complement to the polling
`GET /triggers/{id}/runs`.

Drop-snapshot by design: the initial `snapshot` frame is discarded, so
a monitor sees only runs recorded AFTER it connected — at-most-once,
no historical replay on reconnect (the UI-command-channel philosophy;
history is what `runs()` answers). `closed` is terminal: the caller
opens a fresh feed to reconnect (docs/04-events.md contract — the new
subscription's snapshot, which we again drop, covers the gap for
pollers; a live monitor accepts the miss).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from .anyclient import AnyError
from .triggers import DATASET_RUNS

# The normalized run-event shape (ADR-006 §4 run record + metrics
# rollup). Everything else on the wire record (_ver, window plumbing)
# is dropped at this boundary.
_EVENT_FIELDS = ("triggerId", "ts", "status", "durationMs",
                 "fuel", "costUsd", "traceRef", "error")


class FeedProtocolError(AnyError):
    """The stream violated the documented frame contract
    (docs/04-events.md): an unknown frame type, a malformed `changes`
    payload, or EOF without a terminal `closed`. status 0 = not an
    HTTP status — the request succeeded, the stream broke."""

    def __init__(self, message: str):
        super().__init__(0, "trigger_feed.protocol", message)


def normalize_run(record_id: str, doc: dict) -> dict:
    """One wire run record → the monitoring event: the ADR-006 §4 run
    fields plus `runId` (the dataset record id, sortable per trigger)."""
    event = {k: doc.get(k) for k in _EVENT_FIELDS}
    event["runId"] = record_id
    return event


class TriggerEventFeed:
    """One feed = one subscription: `events()` yields normalized run
    events until the server closes the stream. Not reusable — build a
    fresh feed to reconnect. `closed_reason` carries the terminal
    frame's reason once `events()` returns.

    `trigger_id` narrows the feed to one trigger server-side;
    `on_live` fires once the snapshot has been dropped — everything
    after it is a post-connect run (the test/arming synchronization
    point). `window` bounds the subscription window; new runs sort to
    the top (`-ts`) so they always surface as `added`.
    """

    def __init__(self, client, *, space: str, anchor_object_id: str,
                 trigger_id: str | None = None, window: int = 64,
                 on_live: Callable[[], None] | None = None):
        self._c = client
        self._space = space
        self._anchor = anchor_object_id
        self._trigger_id = trigger_id
        self._window = window
        self._on_live = on_live
        self.closed_reason: str | None = None

    def events(self) -> Iterator[dict]:
        opts: dict = {"sort": ["-ts"], "limit": self._window}
        if self._trigger_id is not None:
            opts["filter"] = {"triggerId": self._trigger_id}
        for frame in self._c.subscribe_dataset(
                self._space, self._anchor, DATASET_RUNS, **opts):
            event, data = frame["event"], frame["data"]
            if event == "ready":
                continue
            if event == "snapshot":
                # Dropped by design (module docstring) — the feed goes
                # live here: every later change is a post-connect run.
                if self._on_live is not None:
                    self._on_live()
                continue
            if event == "changes":
                yield from _runs_in(data)
                continue
            if event == "closed":
                self.closed_reason = data.get("reason") if isinstance(data, dict) else None
                return
            raise FeedProtocolError(f"unexpected frame {event!r} on the trigger run feed")
        raise FeedProtocolError("stream ended without a closed frame")


def _runs_in(data: object) -> Iterator[dict]:
    """Normalized run events inside one `changes` frame. `removed` is
    window displacement / retention sweep, never a run happening —
    runs are append-only, so only arrivals are events."""
    if not isinstance(data, list):
        raise FeedProtocolError("changes frame data is not a batch list")
    for batch in data:
        for entry in (*batch.get("added", ()), *batch.get("updated", ())):
            yield normalize_run(entry.get("id", ""), entry.get("doc") or {})
