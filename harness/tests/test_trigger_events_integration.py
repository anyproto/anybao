"""Trigger run feed against a real any server (`-m integration`): a run
recorded via TriggerStore surfaces live on the SSE feed, and the run
that predates the connection stays in the dropped snapshot."""

import queue
import threading

import pytest
from anybao.trigger_events import TriggerEventFeed
from anybao.triggers import Scheduler, Trigger, TriggerStore

pytestmark = pytest.mark.integration


def test_live_run_surfaces_on_feed(client, fresh_space):
    # the anchor object carries the agent_trigger type so its datasets
    # (agent_triggers / agent_trigger_runs) are writable
    anchor = client.create_object(fresh_space, {"types": ["agent_trigger"]})["objectId"]
    store = TriggerStore(client, space=fresh_space, anchor_object_id=anchor)

    t = Trigger(id="feed1", name="feed test", kind="cron",
                spec={"every_s": 60}, program="noop@v1", owner="inst-A")
    store.save(t)
    # a PRE-connect run — must land in the dropped snapshot, not the feed
    sched = Scheduler("inst-A", now=lambda: 1000.0)
    store.record_run(t, sched.record_run(t, status="ok", duration_ms=1,
                                         trace_ref="run_old"), ts_ms=1_000_000)

    live = threading.Event()
    events: queue.Queue = queue.Queue()
    feed = TriggerEventFeed(client, space=fresh_space, anchor_object_id=anchor,
                            on_live=live.set)

    def pump():
        try:
            for ev in feed.events():
                events.put(ev)
        except Exception as e:  # surfaced through the queue, never swallowed
            events.put(e)

    threading.Thread(target=pump, name="feed-pump", daemon=True).start()
    assert live.wait(10), "feed never went live (no snapshot frame)"

    sched2 = Scheduler("inst-A", now=lambda: 2000.0)
    store.record_run(t, sched2.record_run(t, status="error", duration_ms=7,
                                          error="boom", trace_ref="run_new",
                                          fuel=55, cost_usd=0.001), ts_ms=2_000_000)

    got = events.get(timeout=10)
    assert not isinstance(got, Exception), got
    assert got["triggerId"] == "feed1" and got["runId"] == f"feed1:{2_000_000:020d}"
    assert got["status"] == "error" and got["error"] == "boom"
    assert got["durationMs"] == 7 and got["fuel"] == 55 and got["costUsd"] == 0.001
    assert got["traceRef"] == "run_new"
    # deliveries are ordered, so run_old replaying would have preceded
    # run_new — an empty queue proves the snapshot was dropped
    assert events.qsize() == 0
