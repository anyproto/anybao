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
