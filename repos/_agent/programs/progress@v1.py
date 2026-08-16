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
        {"name": "detail"}, {"name": "started_at", "kind": "number"},
        {"name": "updated_at", "kind": "number"}, {"name": "error"},
        {"name": "program"},
    ],
}
# Property names are snake_case ON PURPOSE (ADR-014 §2 amendment): the
# client derives xKeys by snake-casing names, and normalized READS key
# by xKey — camelCase names made the read shape differ from the write
# shape (and silently broke the started_at carry in _resolve, which
# read the camel key against snake-keyed rows). name == xKey, always.


def _resolve(space, job):
    """Converge a job's objects to ONE → (objectId | None, carry-dict).

    Query-then-create races and p2p partitions mint duplicates
    (ADR-014 §3, the sync_state dance): freshest `modifiedAt` wins,
    stale rows are best-effort deleted, and the EARLIEST started_at
    across all rows is returned as a carry to fold into the caller's
    next write — the true start survives the race; counters never
    merge (summing racers would double-count)."""
    rows = _any.query_objects(space, filter={"agent-progress.job": job},
                              limit=10)
    if not rows:
        return None, {}
    rows.sort(key=lambda r: r.get("modifiedAt") or 0, reverse=True)
    keep = rows[0]
    starts = [(r.get("agent-progress") or {}).get("started_at") for r in rows]
    starts = [s for s in starts if s]
    carry = {}
    kept_start = (keep.get("agent-progress") or {}).get("started_at")
    if starts and (not kept_start or min(starts) < kept_start):
        carry["started_at"] = min(starts)
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
    earliest started_at kept) and rewritten in place, so resume/retry
    reuse the same bar. total <= 0 renders indeterminate."""
    _any.create_type(space, PROGRESS_TYPE)
    fields = {"job": job, "label": label, "status": "running",
              "current": int(current), "total": int(total),
              "detail": detail or "", "started_at": now(),  # noqa: F821
              "updated_at": now(), "error": "", "program": program or ""}  # noqa: F821
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
              "updated_at": now()}  # noqa: F821
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


def _arm_notify(notify, space, job, summary):
    """Best-effort visible nudge (ADR-014 §6): post the outcome into
    the agent chat as a `trigger:<job>` agent message. The watcher's
    name-scoped self-skip (ADR-009 §8 amendment) treats a foreign
    agent name as user-side input, so the message triggers the loop
    naturally — visible in chat, not from the user, answered with the
    chat's history in context. Never raises."""
    try:
        if isinstance(notify, dict):
            agent_space = notify.get("spaceId") or notify.get("space")
            chat = notify.get("chatId")
        else:
            agent_space, chat = notify, None
        if not agent_space:
            return None
        if not chat:
            chat = _any.general_chat(agent_space)
        _any.chat_send(agent_space, chat, {
            "text": summary, "agent": {"name": f"trigger:{job}", "done": True}})
        return chat
    except Exception:
        return None


_NUDGE = ("[trigger: progress] Job '{job}'{label} in space '{space}' "
          "{outcome}. Report this briefly to the user in this chat — reply "
          "normally, do not chat_send.")


@span("progress.done", kind="mutator")  # noqa: F821 - guest global
def done(space, job, notify=None):
    """Finish + self-clean: DELETE the job's object(s) → count deleted.

    A finished bar leaves nothing in the space tree (ADR-014 §4);
    disappearance IS the success signal — fail() keeps its object, so a
    watcher that sees a running job vanish renders success. Want final
    counts in that last frame? tick() them just before done().
    Idempotent when nothing exists. `notify` (TENTATIVE, §6): pass
    `baoSpaceConfig` (or a `{spaceId, chatId}` dict / agent space) to
    post a visible `trigger:<job>` chat message that triggers the loop
    naturally — the agent reports completion in that chat with history
    in context. For detached (trigger-driven) jobs; pointless for work
    you run inline in your own turn."""
    rows = _any.query_objects(space, filter={"agent-progress.job": job},
                              limit=10)
    rows.sort(key=lambda r: r.get("modifiedAt") or 0, reverse=True)
    last = dict(rows[0].get("agent-progress") or {}) if rows else {}
    n = 0
    for r in rows:
        try:  # noqa: SIM105 - best-effort, no contextlib in guest
            _any.delete_object(space, r["id"])
            n += 1
        except _any.AnyError:
            pass
    if notify is not None:
        cur, tot = last.get("current"), last.get("total")
        counts = f" — {cur}/{tot}" if cur is not None else ""
        detail = f" ({last['detail']})" if last.get("detail") else ""
        label = f" ({last['label']})" if last.get("label") else ""
        _arm_notify(notify, space, job, _NUDGE.format(
            job=job, label=label, space=space,
            outcome=f"is DONE{counts}{detail}"))
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
def fail(space, job, error, detail=None, notify=None):
    """Mark the job failed → objectId. The object is KEPT.

    `status: failed` + error is the durable, visible record of what
    stopped — it stays until the user acts on it or a start()/tick()
    of the same job reopens it in place (the retry path). `notify`
    (TENTATIVE, §6): same as done() — a visible `trigger:<job>` chat
    message triggers the loop so the agent tells the user what broke,
    with the chat's history in context."""
    fields = {"status": "failed", "error": str(error or ""),
              "updated_at": now()}  # noqa: F821
    if detail is not None:
        fields["detail"] = detail
    oid, carry = _resolve(space, job)
    if oid:
        _any.update_object(space, oid, {"agent-progress": {**fields, **carry}})
    else:
        _any.create_type(space, PROGRESS_TYPE)
        made = _any.create_object(space, {
            "types": ["agent-progress"], "name": job,
            "initialProperties": {"agent-progress": {
                "job": job, "label": job, **fields}}})
        oid = made["objectId"]
    if notify is not None:
        d = f" ({detail})" if detail else ""
        _arm_notify(notify, space, job, _NUDGE.format(
            job=job, label="", space=space,
            outcome=f"FAILED: {fields['error']}{d}"))
    return oid
