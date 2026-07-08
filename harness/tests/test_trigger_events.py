"""Trigger run SSE feed — offline: fake subscribe frames through
TriggerEventFeed (snapshot dropped, changes normalized, closed
terminal, contract violations loud) plus the GET /triggers/events
bridge on the control server."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
from anybao.trigger_control import TriggerService, start_server
from anybao.trigger_events import FeedProtocolError, TriggerEventFeed, normalize_run

RUN_DOC = {"triggerId": "t1", "ts": 1000.0, "status": "ok", "durationMs": 42,
           "error": None, "traceRef": "run_local_1", "fuel": 1234, "costUsd": 0.003,
           "_ver": {"x": 1}}
NORMALIZED = {"runId": "t1:001", "triggerId": "t1", "ts": 1000.0, "status": "ok",
              "durationMs": 42, "error": None, "traceRef": "run_local_1",
              "fuel": 1234, "costUsd": 0.003}


def ready():
    return {"event": "ready", "data": {}}


def snapshot(records=()):
    return {"event": "snapshot", "data": {"records": list(records), "total": len(records)}}


def changes(added=(), updated=(), removed=()):
    return {"event": "changes", "data": [{"versionId": "v1", "added": list(added),
                                          "updated": list(updated), "removed": list(removed)}]}


def closed(reason):
    return {"event": "closed", "data": {"reason": reason}}


def entry(rid, doc):
    return {"id": rid, "doc": doc, "ops": []}


class FakeClient:
    """subscribe_dataset stand-in yielding pre-parsed SSE frames."""

    def __init__(self, frames):
        self._frames = frames
        self.calls = []

    def subscribe_dataset(self, space, object_id, dataset, **opts):
        self.calls.append((space, object_id, dataset, opts))
        yield from self._frames


def feed_over(frames, **kw):
    client = FakeClient(frames)
    return client, TriggerEventFeed(client, space="s1", anchor_object_id="anchor1", **kw)


# --- normalization -------------------------------------------------------------

def test_normalize_run_projects_the_monitoring_fields():
    assert normalize_run("t1:001", RUN_DOC) == NORMALIZED  # _ver etc. dropped


def test_snapshot_dropped_only_live_runs_yield():
    _, f = feed_over([
        ready(),
        snapshot([{"id": "t1:000", "triggerId": "t1", "status": "ok"}]),  # history
        changes(added=[entry("t1:001", RUN_DOC)]),
        closed("sdk_closed"),
    ])
    events = list(f.events())
    assert events == [NORMALIZED]          # the snapshot run never replayed
    assert f.closed_reason == "sdk_closed"


def test_updated_yields_and_removed_is_not_an_event():
    upd = dict(RUN_DOC, status="error", error="boom")
    _, f = feed_over([
        ready(), snapshot(),
        changes(added=[entry("t1:001", RUN_DOC)],
                updated=[entry("t1:001", upd)],
                removed=[{"id": "t1:000", "reason": "displaced"}]),
        closed("server_shutdown"),
    ])
    events = list(f.events())
    assert [e["status"] for e in events] == ["ok", "error"]
    assert events[1]["error"] == "boom"


def test_on_live_fires_after_snapshot_before_first_event():
    seen = []
    _, f = feed_over([
        ready(), snapshot(),
        changes(added=[entry("t1:001", RUN_DOC)]),
        closed("sdk_closed"),
    ], on_live=lambda: seen.append("live"))
    for ev in f.events():
        seen.append(ev["runId"])
    assert seen == ["live", "t1:001"]


def test_closed_is_terminal_later_frames_unread():
    _, f = feed_over([
        ready(), snapshot(),
        closed("overflow"),
        changes(added=[entry("t1:009", RUN_DOC)]),  # after closed — must not surface
    ])
    assert list(f.events()) == []
    assert f.closed_reason == "overflow"


# --- subscription body ----------------------------------------------------------

def test_subscribe_targets_runs_dataset_with_window_sort():
    client, f = feed_over([ready(), snapshot(), closed("sdk_closed")], window=8)
    list(f.events())
    assert client.calls == [("s1", "anchor1", "agent_trigger_runs",
                             {"sort": ["-ts"], "limit": 8})]


def test_trigger_id_narrows_the_feed_server_side():
    client, f = feed_over([ready(), snapshot(), closed("sdk_closed")], trigger_id="t1")
    list(f.events())
    assert client.calls[0][3]["filter"] == {"triggerId": "t1"}


# --- contract violations are loud ------------------------------------------------

def test_eof_without_closed_raises():
    _, f = feed_over([ready(), snapshot()])
    with pytest.raises(FeedProtocolError, match="without a closed frame"):
        list(f.events())


def test_unknown_frame_raises():
    _, f = feed_over([ready(), {"event": "lagged", "data": {"total": 3}}])
    with pytest.raises(FeedProtocolError, match="unexpected frame 'lagged'"):
        list(f.events())


def test_malformed_changes_payload_raises():
    _, f = feed_over([ready(), snapshot(), {"event": "changes", "data": {"added": []}}])
    with pytest.raises(FeedProtocolError, match="not a batch list"):
        list(f.events())


# --- GET /triggers/events bridge --------------------------------------------------

class _EmptyStore:
    def save(self, t):
        pass

    def load_all(self):
        return []

    def record_run(self, t, run, *, ts_ms):
        pass

    def runs(self, trigger_id, limit=20):
        return []


def _sse_frames(raw: str) -> list[tuple[str, dict]]:
    out = []
    for block in raw.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_http_events_route_streams_runs_then_closed():
    def factory():
        _, f = feed_over([
            ready(), snapshot(),
            changes(added=[entry("t1:001", RUN_DOC)]),
            closed("sdk_closed"),
        ])
        return f

    srv = start_server(TriggerService(_EmptyStore(), owner="inst-A"), feed_factory=factory)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/triggers/events"
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "text/event-stream"
            frames = _sse_frames(resp.read().decode())
        assert frames == [("run", NORMALIZED), ("closed", {"reason": "sdk_closed"})]
    finally:
        srv.shutdown()
        srv.server_close()


def test_http_events_route_501_without_feed():
    srv = start_server(TriggerService(_EmptyStore(), owner="inst-A"))
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/triggers/events"
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(url, timeout=5)
        assert exc.value.code == 501
        assert json.loads(exc.value.read())["error"]["code"] == "feed.unavailable"
    finally:
        srv.shutdown()
        srv.server_close()
