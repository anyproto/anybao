"""TriggerRuntime orchestration + Watcher — offline with fakes."""

from anybao.triggers import RunResult, Scheduler, Trigger, TriggerRuntime
from anybao.watch import Watcher


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def runtime(clock, results=None, boot_time=0.0):
    s = Scheduler("inst-A", now=clock)
    s.arm()
    fired = []

    def run_program(trigger, event_record):
        fired.append((trigger.id, event_record))
        r = (results or {}).get(trigger.id)
        return r or RunResult(status="ok", duration_ms=5, trace_ref="run", fuel=100)

    records = []
    rt = TriggerRuntime(s, run_program, record_sink=lambda t, rec: records.append(rec),
                        boot_time=boot_time)
    return rt, fired, records


def cron(**kw):
    kw.setdefault("owner", "inst-A")
    return Trigger(id=kw.pop("id", "c1"), name="sweep", kind="cron",
                   spec=kw.pop("spec", {"every_s": 100}), program="p@v1", **kw)


def event(**kw):
    kw.setdefault("owner", "inst-A")
    return Trigger(id=kw.pop("id", "e1"), name="watch", kind="event",
                   spec=kw.pop("spec", {"dataset": "chat_messages"}),
                   program="runner@v1", **kw)


def test_cron_tick_fires_due_and_records():
    clk = Clock(1000.0)
    rt, fired, records = runtime(clk)
    rt.add(cron(spec={"every_s": 100}))
    assert rt.tick() == [] and not fired      # first tick arms next_due
    clk.t = 1100.0
    rt.tick()
    assert fired == [("c1", None)]
    assert len(records) == 1 and records[0].status == "ok"


def test_event_dispatch_fires_matching():
    rt, fired, _ = runtime(Clock(1000.0), boot_time=0.0)
    rt.add(event(spec={"dataset": "chat_messages", "filter": {"agent": {"$exists": False}}}))
    rt.on_event("chat_messages", {"id": "m1", "text": "hi"}, created_at=999.0)
    assert fired == [("e1", {"id": "m1", "text": "hi"})]
    # agent message doesn't fire
    rt.on_event("chat_messages", {"id": "m2", "agent": {"name": "bao"}}, created_at=999.0)
    assert len(fired) == 1


def test_failing_run_trips_breaker_after_threshold():
    rt, _, _ = runtime(Clock(),
                       results={"c1": RunResult(status="error", duration_ms=1, error="x")})
    t = cron(max_consecutive_failures=2)
    rt.add(t)
    # force two fires
    rt._fire(t, None)
    assert t.enabled is True
    rt._fire(t, None)
    assert t.enabled is False        # circuit breaker via record_run


# --- Watcher ---

def watcher():
    runs = []
    w = Watcher(run_conversation=lambda cid, text, mb: runs.append((cid, text, mb)))
    return w, runs


def test_watcher_starts_conversation_on_human_message():
    w, runs = watcher()
    assert w.on_message("chat1", {"id": "m1", "text": "hello"}) == "start"
    assert runs[0][0] == "chat1" and runs[0][1] == "hello"
    assert w.is_live("chat1")


def test_watcher_skips_agent_messages():
    w, runs = watcher()
    assert w.on_message("chat1", {"id": "m1", "text": "x", "agent": {"name": "bao"}}) == "skip"
    assert not runs


def test_watcher_dedups_redelivery():
    w, runs = watcher()
    w.on_message("chat1", {"id": "m1", "text": "hi"})
    assert w.on_message("chat1", {"id": "m1", "text": "hi"}) == "dup"
    assert len(runs) == 1


def test_watcher_injects_mid_run():
    w, runs = watcher()
    w.on_message("chat1", {"id": "m1", "text": "first"})   # starts, chat1 live
    mailbox = runs[0][2]
    assert w.on_message("chat1", {"id": "m2", "text": "also this"}) == "inject"
    drained = mailbox.drain()
    assert drained == [{"kind": "inject", "text": "also this"}]
    assert len(runs) == 1                                   # no second conversation


def test_watcher_fresh_after_done():
    w, runs = watcher()
    w.on_message("chat1", {"id": "m1", "text": "a"})
    w.conversation_done("chat1")
    assert not w.is_live("chat1")
    assert w.on_message("chat1", {"id": "m2", "text": "b"}) == "start"
    assert len(runs) == 2
