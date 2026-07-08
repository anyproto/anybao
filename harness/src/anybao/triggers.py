"""Trigger subsystem — plan §4b + ADR-006 §4. Cron/event triggers that
run programs; single-owner, at-most-once, no fault tolerance by design.

This module is the PURE scheduler core (owner/arming/due/circuit-breaker/
run-rollup/event-match) — offline-testable with an injected clock. The
I/O layer (SSE event subscription, program execution, HTTP API, synced
trigger objects) wraps it. The watcher is trigger #1: a chat_messages
event trigger whose program is the conversation runner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from croniter import croniter

Kind = Literal["cron", "event"]
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class Trigger:
    id: str
    name: str
    kind: Kind
    spec: dict            # cron: {"cron": "0 * * * *"} | {"every_s": 3600}
                          # event: {"dataset", "objectId"?, "filter"?}
    program: str          # module spec to run (ADR-004)
    args: dict = field(default_factory=dict)
    owner: str = ""       # instanceId — runs ONLY on this instance
    enabled: bool = True
    limits: dict = field(default_factory=dict)  # {fuelPerRun, timeoutS, maxCostPerRun}
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES

    # observability rollup (ADR-006 §4)
    last_run_at: float | None = None
    last_status: str | None = None       # ok | error | auto_disabled
    last_duration_ms: int | None = None
    last_fuel: int | None = None
    last_cost_usd: float | None = None
    run_count: int = 0
    consecutive_failures: int = 0
    last_run_ref: str | None = None
    # cron scheduling state (from-now, never backward — cold-sync guard)
    next_due: float | None = None


@dataclass
class RunRecord:
    trigger_id: str
    ts: float
    status: str            # ok | error
    duration_ms: int
    error: str | None = None
    trace_ref: str | None = None
    fuel: int | None = None
    cost_usd: float | None = None


class Scheduler:
    """Owns instance identity + arming; decides what fires. Firing itself
    (running the program) is the caller's job — Scheduler stays pure."""

    def __init__(self, instance_id: str, *, now):
        self.instance_id = instance_id
        self._now = now
        self.armed = False  # boot DISARMED; arm() after /sync-status synced

    def arm(self) -> None:
        self.armed = True

    def owns(self, t: Trigger) -> bool:
        return t.owner == self.instance_id

    def runnable(self, t: Trigger) -> bool:
        """A trigger this instance may fire: armed, owned, enabled."""
        return self.armed and self.owns(t) and t.enabled

    # --- cron / interval ---------------------------------------------------
    def compute_next_due(self, t: Trigger, after: float | None = None) -> float:
        """Strictly forward from `after` (default now) — a missed
        occurrence while the owner was down simply does not exist
        (cold-sync guard; ADR-006 §4)."""
        base = after if after is not None else self._now()
        if "every_s" in t.spec:
            return base + float(t.spec["every_s"])
        if "cron" in t.spec:
            return croniter(t.spec["cron"], base).get_next(float)
        raise ValueError(f"cron trigger {t.id} has no every_s/cron spec")

    def cron_due(self, t: Trigger) -> bool:
        """True when a cron/interval trigger should fire now. Arms
        next_due on first check (from now, not backward)."""
        if t.kind != "cron" or not self.runnable(t):
            return False
        if t.next_due is None:
            t.next_due = self.compute_next_due(t)
            return False
        return self._now() >= t.next_due

    def advance_cron(self, t: Trigger) -> None:
        """After firing, schedule the next occurrence forward from now."""
        t.next_due = self.compute_next_due(t)

    # --- event matching ----------------------------------------------------
    def event_matches(self, t: Trigger, dataset: str, record: dict,
                      *, record_created_at: float | None = None,
                      boot_time: float, slack_s: float = 5.0) -> bool:
        """Event trigger fires on a post-connect delta matching its
        dataset/filter. Age guard: skip records older than boot_time -
        slack (CRDT cold-sync can surface history as live Added — belt
        to the snapshot-drop braces, ADR-006 §4)."""
        if t.kind != "event" or not self.runnable(t):
            return False
        if t.spec.get("dataset") != dataset:
            return False
        if record_created_at is not None and record_created_at < boot_time - slack_s:
            return False
        flt = t.spec.get("filter")
        if flt and not _matches_filter(flt, record):
            return False
        return True

    # --- run recording + circuit breaker -----------------------------------
    def record_run(self, t: Trigger, *, status: str, duration_ms: int,
                   error: str | None = None, trace_ref: str | None = None,
                   fuel: int | None = None, cost_usd: float | None = None) -> RunRecord:
        rec = RunRecord(trigger_id=t.id, ts=self._now(), status=status,
                        duration_ms=duration_ms, error=error, trace_ref=trace_ref,
                        fuel=fuel, cost_usd=cost_usd)
        t.last_run_at = rec.ts
        t.last_status = status
        t.last_duration_ms = duration_ms
        t.last_fuel = fuel
        t.last_cost_usd = cost_usd
        t.last_run_ref = trace_ref
        t.run_count += 1
        if status == "ok":
            t.consecutive_failures = 0
        else:
            t.consecutive_failures += 1
            if t.consecutive_failures >= t.max_consecutive_failures:
                t.enabled = False
                t.last_status = "auto_disabled"  # circuit breaker
        return rec


@dataclass
class RunResult:
    status: str                 # ok | error
    duration_ms: int
    trace_ref: str | None = None
    fuel: int | None = None
    cost_usd: float | None = None
    error: str | None = None


class TriggerRuntime:
    """Orchestration over the pure Scheduler — cron ticks + event
    dispatch → fire → record. I/O is injected: `run_program(trigger,
    event_record) -> RunResult` drives the executor/loop in prod (a fake
    in tests); `record_sink(trigger, RunRecord)` persists to trigger_runs
    (optional). The scheduler stays pure; this is where firing happens."""

    def __init__(self, scheduler: Scheduler, run_program, *, record_sink=None,
                 boot_time: float):
        self.sched = scheduler
        self._run = run_program
        self._record = record_sink
        self._boot_time = boot_time
        self.triggers: dict[str, Trigger] = {}

    def add(self, t: Trigger) -> None:
        self.triggers[t.id] = t

    def tick(self) -> list[RunResult]:
        """Fire every due cron/interval trigger. Called on a timer."""
        out = []
        for t in list(self.triggers.values()):
            if t.kind == "cron" and self.sched.cron_due(t):
                out.append(self._fire(t, None))
                self.sched.advance_cron(t)
        return out

    def on_event(self, dataset: str, record: dict,
                 *, created_at: float | None = None) -> list[RunResult]:
        """Dispatch a post-connect delta to matching event triggers."""
        out = []
        for t in list(self.triggers.values()):
            if self.sched.event_matches(t, dataset, record,
                                        record_created_at=created_at,
                                        boot_time=self._boot_time):
                out.append(self._fire(t, record))
        return out

    def _fire(self, t: Trigger, event_record: dict | None) -> RunResult:
        result = self._run(t, event_record)
        rec = self.sched.record_run(
            t, status=result.status, duration_ms=result.duration_ms,
            error=result.error, trace_ref=result.trace_ref,
            fuel=result.fuel, cost_usd=result.cost_usd)
        if self._record is not None:
            self._record(t, rec)
        return result


TRIGGER_TYPE = "agent_trigger"
DATASET_TRIGGERS = "agent_triggers"
DATASET_RUNS = "agent_trigger_runs"


def trigger_to_record(t: Trigger) -> dict:
    """The synced trigger record — carries the full definition AND the
    rollup, so a `list` query returns the monitoring view without opening
    runs (ADR-006 §4)."""
    return {
        "name": t.name, "kind": t.kind, "spec": t.spec, "program": t.program,
        "args": t.args, "owner": t.owner, "enabled": t.enabled, "limits": t.limits,
        "maxConsecutiveFailures": t.max_consecutive_failures,
        "lastRunAt": t.last_run_at, "lastStatus": t.last_status,
        "lastDurationMs": t.last_duration_ms, "lastFuel": t.last_fuel,
        "lastCostUsd": t.last_cost_usd, "runCount": t.run_count,
        "consecutiveFailures": t.consecutive_failures, "lastRunRef": t.last_run_ref,
    }


def trigger_from_record(rec: dict) -> Trigger:
    return Trigger(
        id=rec["id"], name=rec.get("name", ""), kind=rec.get("kind", "cron"),
        spec=rec.get("spec", {}), program=rec.get("program", ""),
        args=rec.get("args", {}), owner=rec.get("owner", ""),
        enabled=rec.get("enabled", True), limits=rec.get("limits", {}),
        max_consecutive_failures=rec.get("maxConsecutiveFailures",
                                         DEFAULT_MAX_CONSECUTIVE_FAILURES),
        last_run_at=rec.get("lastRunAt"), last_status=rec.get("lastStatus"),
        last_duration_ms=rec.get("lastDurationMs"), last_fuel=rec.get("lastFuel"),
        last_cost_usd=rec.get("lastCostUsd"), run_count=rec.get("runCount", 0),
        consecutive_failures=rec.get("consecutiveFailures", 0),
        last_run_ref=rec.get("lastRunRef"))


class TriggerStore:
    """Persists triggers + run records as dataset records on a per-space
    anchor object (plain datasets — no server handler in v2.0). The
    trigger record IS the monitoring rollup; runs go to a separate
    keep-last-N dataset."""

    def __init__(self, client, *, space: str, anchor_object_id: str):
        self._c = client
        self._space = space
        self._anchor = anchor_object_id

    def save(self, t: Trigger) -> None:
        self._c.upsert_record(self._space, self._anchor, DATASET_TRIGGERS,
                              t.id, trigger_to_record(t))

    def load_all(self) -> list[Trigger]:
        recs = self._c.query(self._space, self._anchor, DATASET_TRIGGERS)
        return [trigger_from_record(r) for r in recs]

    def record_run(self, t: Trigger, run: RunRecord, *, ts_ms: int) -> None:
        rid = f"{t.id}:{ts_ms:020d}"  # sortable per-trigger run id
        self._c.upsert_record(self._space, self._anchor, DATASET_RUNS, rid, {
            "triggerId": t.id, "ts": run.ts, "status": run.status,
            "durationMs": run.duration_ms, "error": run.error,
            "traceRef": run.trace_ref, "fuel": run.fuel, "costUsd": run.cost_usd})
        self.save(t)  # refresh the rollup on the trigger record

    def runs(self, trigger_id: str, limit: int = 20) -> list[dict]:
        return self._c.query(self._space, self._anchor, DATASET_RUNS,
                             filter={"triggerId": trigger_id}, sort=["-ts"], limit=limit)


def _matches_filter(flt: dict, record: dict) -> bool:
    """Minimal equality/`$in` filter over top-level record fields — the
    event-trigger match. Full query-filter parity is a later add."""
    for key, cond in flt.items():
        val = record.get(key)
        if isinstance(cond, dict):
            if "$in" in cond and val not in cond["$in"]:
                return False
            if "$eq" in cond and val != cond["$eq"]:
                return False
            if "$exists" in cond and (val is not None) != cond["$exists"]:
                return False
        elif val != cond:
            return False
    return True


def rollup(t: Trigger) -> dict:
    """The monitoring view a `list` returns — enough to keep background
    programs in sane order without opening traces (ADR-006 §4)."""
    fail_rate = t.consecutive_failures / t.run_count if t.run_count else 0.0
    return {
        "id": t.id, "name": t.name, "kind": t.kind, "owner": t.owner,
        "enabled": t.enabled, "lastRunAt": t.last_run_at, "lastStatus": t.last_status,
        "lastDurationMs": t.last_duration_ms, "lastFuel": t.last_fuel,
        "lastCostUsd": t.last_cost_usd, "runCount": t.run_count,
        "consecutiveFailures": t.consecutive_failures, "failureRate": fail_rate,
        "lastRunRef": t.last_run_ref, "limits": t.limits,
    }
