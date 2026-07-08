"""Trigger scheduler core — pure, injected clock, offline."""

from anybao.triggers import Scheduler, Trigger, rollup


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def sched(clock, instance="inst-A"):
    s = Scheduler(instance, now=clock)
    s.arm()
    return s


def cron_trigger(**kw):
    kw.setdefault("owner", "inst-A")
    return Trigger(id="t1", name="sweep", kind="cron",
                   spec=kw.pop("spec", {"every_s": 100}), program="p@v1", **kw)


def event_trigger(**kw):
    kw.setdefault("owner", "inst-A")
    return Trigger(id="e1", name="watch", kind="event",
                   spec=kw.pop("spec", {"dataset": "chat_messages"}),
                   program="runner@v1", **kw)


# --- ownership + arming -------------------------------------------------------

def test_runs_only_own_triggers():
    clk = Clock()
    s = sched(clk)
    mine, theirs = cron_trigger(owner="inst-A"), cron_trigger(owner="inst-B")
    assert s.owns(mine) and not s.owns(theirs)
    assert s.runnable(mine) and not s.runnable(theirs)


def test_boot_disarmed_until_armed():
    clk = Clock()
    s = Scheduler("inst-A", now=clk)  # not armed
    t = cron_trigger()
    assert not s.runnable(t)
    s.arm()
    assert s.runnable(t)


def test_disabled_not_runnable():
    s = sched(Clock())
    assert not s.runnable(cron_trigger(enabled=False))


# --- cron / interval, from-now ------------------------------------------------

def test_cron_due_arms_forward_not_backward():
    clk = Clock(1000.0)
    s = sched(clk)
    t = cron_trigger(spec={"every_s": 100})
    assert s.cron_due(t) is False       # first check arms next_due=1100
    assert t.next_due == 1100.0
    clk.t = 1099.0
    assert s.cron_due(t) is False
    clk.t = 1100.0
    assert s.cron_due(t) is True
    s.advance_cron(t)
    assert t.next_due == 1200.0         # forward from now, no backfill


def test_cron_expr_next_due():
    clk = Clock(0.0)  # epoch
    s = sched(clk)
    t = cron_trigger(spec={"cron": "0 * * * *"})  # top of every hour
    nd = s.compute_next_due(t)
    assert nd == 3600.0  # next top-of-hour after epoch


# --- events + age guard -------------------------------------------------------

def test_event_matches_dataset_and_filter():
    s = sched(Clock(1000.0))
    t = event_trigger(spec={"dataset": "chat_messages", "filter": {"agent": {"$exists": False}}})
    human = {"text": "hi"}                      # no agent field
    agent = {"text": "hi", "agent": {"name": "bao"}}
    assert s.event_matches(t, "chat_messages", human, boot_time=0.0)
    assert not s.event_matches(t, "chat_messages", agent, boot_time=0.0)
    assert not s.event_matches(t, "other_ds", human, boot_time=0.0)  # wrong dataset


def test_event_age_guard_skips_historical():
    s = sched(Clock(1000.0))
    t = event_trigger()
    # record created long before boot → cold-sync historical, skip
    assert not s.event_matches(t, "chat_messages", {"text": "old"},
                               record_created_at=10.0, boot_time=500.0)
    # recent record → fires
    assert s.event_matches(t, "chat_messages", {"text": "new"},
                           record_created_at=999.0, boot_time=500.0)


# --- run recording + circuit breaker ------------------------------------------

def test_run_rollup_updates():
    clk = Clock(1000.0)
    s = sched(clk)
    t = cron_trigger()
    s.record_run(t, status="ok", duration_ms=88, fuel=1234, cost_usd=0.01, trace_ref="run1")
    assert t.run_count == 1 and t.last_status == "ok" and t.last_run_ref == "run1"
    assert rollup(t)["lastFuel"] == 1234


def test_circuit_breaker_auto_disables():
    s = sched(Clock())
    t = cron_trigger(max_consecutive_failures=3)
    for _ in range(2):
        s.record_run(t, status="error", duration_ms=1, error="boom")
    assert t.enabled is True                    # under threshold
    s.record_run(t, status="error", duration_ms=1, error="boom")
    assert t.enabled is False                   # tripped
    assert t.last_status == "auto_disabled"


def test_success_resets_failure_streak():
    s = sched(Clock())
    t = cron_trigger()
    s.record_run(t, status="error", duration_ms=1)
    s.record_run(t, status="ok", duration_ms=1)
    assert t.consecutive_failures == 0 and t.enabled is True
