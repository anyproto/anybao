"""Progress bars for long jobs — start/tick/done/fail, one bar per (space, job).

Programs and cells report progress ONLY through this module (ADR-014);
never hand-roll agent-progress objects. The transport (one
`agent-progress` object per (space, job), property ticks riding the
objects firehose into any-ui) is an implementation detail to be swapped
here when the any-native progress facility lands. Callers own the
throttle: tick per work chunk / percentage step, never per item — every
tick is a p2p-synced CRDT change.
"""

# TEMPORARY TRANSPORT (ADR-014 §2): the any server team is designing a
# native, generic progress facility. When it ships, ONLY this module's
# internals (and any-ui's ProgressSource) get rewritten against it —
# the start/tick/done/fail surface is the contract programs keep.
# Don't add transport-shaped features here in the meantime.

__any_tool__ = True  # agent-callable (ADR-010 §4; ADR-014 §1 amendment)

_any = use("any@v1")  # noqa: F821 - `use` is the guest global

PROGRESS_TYPE = {   # the agent_progress protocol — any-ui renders these
    "name": "Agent progress", "xKey": "agent-progress",
    "properties": [
        {"name": "job"}, {"name": "label"}, {"name": "status"},
        {"name": "current", "kind": "number"}, {"name": "total", "kind": "number"},
        {"name": "detail"}, {"name": "startedAt", "kind": "number"},
        {"name": "updatedAt", "kind": "number"}, {"name": "error"},
        {"name": "program"},
    ],
}


def _resolve(space, job):
    """Converge a job's objects to ONE → (objectId | None, carry-dict).

    Query-then-create races and p2p partitions mint duplicates
    (ADR-014 §3, the sync_state dance): freshest `modifiedAt` wins,
    stale rows are best-effort deleted, and the EARLIEST startedAt
    across all rows is returned as a carry to fold into the caller's
    next write — the true start survives the race; counters never
    merge (summing racers would double-count)."""
    rows = _any.query_objects(space, filter={"agent-progress.job": job},
                              limit=10)
    if not rows:
        return None, {}
    rows.sort(key=lambda r: r.get("modifiedAt") or 0, reverse=True)
    keep = rows[0]
    starts = [(r.get("agent-progress") or {}).get("startedAt") for r in rows]
    starts = [s for s in starts if s]
    carry = {}
    kept_start = (keep.get("agent-progress") or {}).get("startedAt")
    if starts and (not kept_start or min(starts) < kept_start):
        carry["startedAt"] = min(starts)
    for r in rows[1:]:
        try:  # noqa: SIM105 - best-effort, no contextlib in guest
            _any.delete_object(space, r["id"])
        except _any.AnyError:
            pass
    return keep["id"], carry


@span("progress.start", kind="mutator")  # noqa: F821 - guest global
def start(space, job, label, total=0, current=0, detail="", program=""):
    """Begin (or resume) a job's bar → objectId.

    Publishes the starting state BEFORE work begins (a bar that only
    appears at the first tick reads as a hang). Idempotent: an existing
    job object is converged (freshest wins, stale duplicates deleted,
    earliest startedAt kept) and rewritten in place, so resume/retry
    reuse the same bar. total <= 0 renders indeterminate."""
    _any.create_type(space, PROGRESS_TYPE)
    fields = {"job": job, "label": label, "status": "running",
              "current": int(current), "total": int(total),
              "detail": detail or "", "startedAt": now(),  # noqa: F821
              "updatedAt": now(), "error": "", "program": program or ""}  # noqa: F821
    oid, carry = _resolve(space, job)
    if oid:
        _any.update_object(space, oid, {"agent-progress": {**fields, **carry}})
        return oid
    made = _any.create_object(space, {
        "types": ["agent-progress"], "name": label or job,
        "initialProperties": {"agent-progress": fields}})
    return made["objectId"]


@span("progress.tick", kind="mutator")  # noqa: F821 - guest global
def tick(space, job, current, total=None, detail=None, label=None):
    """Advance the bar → objectId. Property writes only.

    Writes `status: running` every time — after a transient fail() the
    next tick reopens the job in place. Self-heals: a missing object
    (deleted, or never started) is recreated so a resumed chain never
    loses its bar. THROTTLE: per work chunk, never per item."""
    fields = {"current": int(current), "status": "running",
              "updatedAt": now()}  # noqa: F821
    if total is not None:
        fields["total"] = int(total)
    if detail is not None:
        fields["detail"] = detail
    if label is not None:
        fields["label"] = label
    oid, carry = _resolve(space, job)
    if oid:
        _any.update_object(space, oid, {"agent-progress": {**fields, **carry}})
        return oid
    return start(space, job, label or job, total=int(total or 0),
                 current=int(current), detail=detail or "")


@span("progress.done", kind="mutator")  # noqa: F821 - guest global
def done(space, job):
    """Finish + self-clean: DELETE the job's object(s) → count deleted.

    A finished bar leaves nothing in the space tree (ADR-014 §4);
    disappearance IS the success signal — fail() keeps its object, so a
    watcher that sees a running job vanish renders success. Want final
    counts in that last frame? tick() them just before done().
    Idempotent when nothing exists."""
    rows = _any.query_objects(space, filter={"agent-progress.job": job},
                              limit=10)
    n = 0
    for r in rows:
        try:  # noqa: SIM105 - best-effort, no contextlib in guest
            _any.delete_object(space, r["id"])
            n += 1
        except _any.AnyError:
            pass
    return n


@span("progress.jobs", kind="getter")  # noqa: F821 - guest global
def jobs(space):
    """Live bars in a space → [{job, label, status, current, total, …}].

    One row per job, freshest write wins (read-side §3, no pruning —
    this is a getter). Only running/failed rows exist by nature —
    done() deletes its object — so this IS the answer to "what's
    running?" / "did anything fail?"."""
    rows = _any.query_objects(space, filter={"any.types": "agent-progress"},
                              limit=100)
    best = {}
    for r in rows:
        p = dict(r.get("agent-progress") or {})
        job = p.get("job") or r["id"]
        at = r.get("modifiedAt") or 0
        if job not in best or at > best[job][0]:
            best[job] = (at, p)
    return [p for _, p in best.values()]


@span("progress.fail", kind="mutator")  # noqa: F821 - guest global
def fail(space, job, error, detail=None):
    """Mark the job failed → objectId. The object is KEPT.

    `status: failed` + error is the durable, visible record of what
    stopped — it stays until the user acts on it or a start()/tick()
    of the same job reopens it in place (the retry path)."""
    fields = {"status": "failed", "error": str(error or ""),
              "updatedAt": now()}  # noqa: F821
    if detail is not None:
        fields["detail"] = detail
    oid, carry = _resolve(space, job)
    if oid:
        _any.update_object(space, oid, {"agent-progress": {**fields, **carry}})
        return oid
    _any.create_type(space, PROGRESS_TYPE)
    made = _any.create_object(space, {
        "types": ["agent-progress"], "name": job,
        "initialProperties": {"agent-progress": {
            "job": job, "label": job, **fields}}})
    return made["objectId"]
