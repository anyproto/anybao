"""TriggerService + TriggerRuntime sharing one registry — a control-API
patch must reach the running scheduler (live-caught in the gate walk)."""

from anybao.trigger_control import TriggerService
from anybao.triggers import RunResult, Scheduler, Trigger, TriggerRuntime


class FakeStore:
    def __init__(self):
        self.saved = {}

    def save(self, t):
        self.saved[t.id] = t

    def load_all(self):
        return []

    def record_run(self, t, run, *, ts_ms):
        self.save(t)

    def runs(self, trigger_id, limit=20):
        return []


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_patch_through_service_reschedules_the_running_trigger():
    clock = Clock()
    fired = []
    sched = Scheduler("inst1", now=clock)
    sched.arm()
    runtime = TriggerRuntime(
        sched, lambda t, ev: (fired.append(t.spec), RunResult("ok", 1))[1],
        boot_time=clock.t)
    runtime.add(Trigger(id="x", name="x", kind="cron", spec={"every_s": 3600},
                        program="p@v1", owner="inst1"))
    service = TriggerService(FakeStore(), owner="inst1",
                             registry=runtime.triggers)

    runtime.tick()                       # arms next_due at +3600
    clock.t += 20
    assert runtime.tick() == []          # far from due

    service.patch("x", {"spec": {"every_s": 8}})
    runtime.tick()                       # re-arms on the new schedule
    clock.t += 9
    assert len(runtime.tick()) == 1      # fires 9s later
    assert fired == [{"every_s": 8}]

    # disable through the service stops the shared trigger too
    service.disable("x")
    clock.t += 20
    assert runtime.tick() == []


def test_service_fills_registry_gaps_from_store_but_memory_wins():
    live = Trigger(id="a", name="live", kind="cron", spec={"every_s": 5},
                   program="p@v1", owner="i")

    class Store(FakeStore):
        def load_all(self):
            return [Trigger(id="a", name="stale", kind="cron",
                            spec={"every_s": 999}, program="p@v1", owner="i"),
                    Trigger(id="b", name="persisted-only", kind="cron",
                            spec={"every_s": 1}, program="q@v1", owner="i")]

    registry = {"a": live}
    service = TriggerService(Store(), owner="i", registry=registry)
    assert registry["a"].name == "live"          # in-memory wins
    assert registry["b"].name == "persisted-only"
    assert {r["id"] for r in service.list()} == {"a", "b"}
