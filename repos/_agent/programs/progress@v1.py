"""Progress bars for long jobs — start/tick/done/fail, one bar per (space, job).

Programs and cells report progress ONLY through this module (ADR-014);
never hand-roll process events. The transport — the server's process
registry (`/v1/processes`, any PR #163): `process.*` events over the
event bus, rendered GLOBALLY by any-ui regardless of the open space —
is an implementation detail owned here. Callers own the throttle: tick
per work chunk / percentage step, never per item. Nothing is
persisted: a finished bar lingers ~60s in the view, a failed one too —
the durable record of an outcome is your notify message / job state.
"""

# TRANSPORT (ADR-014 §2, swapped 2026-08-19): register/progress/finish
# on /v1/processes — id "<job>.<spaceId>", kind "agent", scope
# "account" (the account's devices see every bar — the global-bar
# property), target = the subject space id, detail rides `message`.
# The pre-swap agent-progress OBJECT transport is retired with NO
# fallback (the no-backward-compat rule): a server without the
# facility fails these calls loudly — upgrade the server, don't mask.

__any_tool__ = True  # agent-callable (ADR-010 §4; ADR-014 §1 amendment)

_any = use("any@v1")  # noqa: F821 - `use` is the guest global

_KIND = "agent"
_sids = {}   # space ref -> space id, memoized for the run (one cell)


def _sid(space):
    key = str(space)
    if key not in _sids:
        sc = space
        if isinstance(sc, dict):
            sc = sc.get("spaceId") or sc.get("id") or ""
        _sids[key] = _any.get_space(sc)["id"]
    return _sids[key]


def _pid(space, job):
    # (identity, id) is the registry key and identity is the whole
    # account — the space id suffix keeps two spaces' same-named jobs
    # apart. job must fit the event-target grammar [A-Za-z0-9._-].
    return f"{job}.{_sid(space)}"


def _register(space, job, title):
    _any._process_register({"id": _pid(space, job), "kind": _KIND,
                            "title": title or job, "scope": "account",
                            "target": _sid(space)})


@span("progress.start", kind="mutator")  # noqa: F821 - guest global
def start(space, job, label, total=0, current=0, detail="", program=""):
    """Begin (or resume) a job's bar → process id.

    Publishes the starting state BEFORE work begins (a bar that only
    appears at the first tick reads as a hang). Idempotent:
    re-registering the same job restarts its row in place, so
    resume/retry reuse the same bar. total <= 0 renders indeterminate.
    `program` is accepted for compatibility and unused — the process
    row identifies the publisher by account identity."""
    pid = _pid(space, job)
    _register(space, job, label)
    _any._process_progress(pid, {"done": int(current),
                                 "total": int(total),
                                 "message": detail or ""})
    return pid


@span("progress.tick", kind="mutator")  # noqa: F821 - guest global
def tick(space, job, current, total=None, detail=None, label=None):
    """Advance the bar → process id. Doubles as the liveness heartbeat.

    Absent fields keep their current values. A tick after fail()
    reopens the row as running (the retry path). Self-heals: an
    expired or never-started process (404 process.not_found — running
    rows expire 45s after the last tick) is re-registered, so a
    resumed chain never loses its bar. THROTTLE: per work chunk, never
    per item — and at least one tick per 45s keeps a slow job's bar
    alive."""
    pid = _pid(space, job)
    body = {"done": int(current)}
    if total is not None:
        body["total"] = int(total)
    if detail is not None:
        body["message"] = detail
    try:
        _any._process_progress(pid, body)
    except _any.AnyError as e:
        if e.code != "process.not_found":
            raise
        return start(space, job, label or job, total=int(total or 0),
                     current=int(current), detail=detail or "")
    return pid


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


def _own_row(pid):
    try:
        return next((r for r in _any.list_processes()
                     if r.get("id") == pid and r.get("self")), {})
    except _any.AnyError:
        return {}


@span("progress.done", kind="mutator")  # noqa: F821 - guest global
def done(space, job, notify=None):
    """Finish the bar → 1 if a live row was finished, else 0.

    Emits the terminal `process.done` frame; the row lingers ~60s in
    the view as the success signal, then expires — nothing is left
    behind (ADR-014 §4). Want final counts in that linger? tick() them
    just before done(). Idempotent when nothing is live. `notify`
    (§6): pass `baoSpaceConfig` (or a `{spaceId, chatId}` dict / agent
    space) to post a visible `trigger:<job>` chat message that
    triggers the loop — the agent reports completion in that chat with
    history in context. For detached (trigger-driven) jobs; pointless
    for work you run inline in your own turn."""
    pid = _pid(space, job)
    last, finished = {}, 0
    try:
        if notify is not None:
            last = _own_row(pid)
        _any._process_finish(pid, {"status": "done"})
        finished = 1
    except _any.AnyError as e:
        if e.code != "process.not_found":
            raise
    if notify is not None:
        cur, tot = last.get("done"), last.get("total")
        counts = f" — {cur}/{tot}" if cur is not None and tot else ""
        detail = f" ({last['message']})" if last.get("message") else ""
        label = f" ({last['title']})" if last.get("title") else ""
        _arm_notify(notify, space, job, _NUDGE.format(
            job=job, label=label, space=space,
            outcome=f"is DONE{counts}{detail}"))
    return finished


@span("progress.jobs", kind="getter")  # noqa: F821 - guest global
def jobs(space):
    """Bars for a space → [{job, label, status, current, total, …}].

    Reads the server process view filtered to bao jobs (kind "agent")
    whose subject is this space. status ∈ running | done | failed |
    cancelled — terminal rows appear during their ~60s linger, then
    expire; "what's running?" is the running rows."""
    sid = _sid(space)
    rows = _any.list_processes()
    suffix = "." + sid
    out = []
    for r in rows:
        if r.get("kind") != _KIND or r.get("target") != sid:
            continue
        rid = r.get("id") or ""
        out.append({
            "job": rid[:-len(suffix)] if rid.endswith(suffix) else rid,
            "label": r.get("title") or "", "status": r.get("state") or "",
            "current": int(r.get("done") or 0),
            "total": int(r.get("total") or 0),
            "detail": r.get("message") or "",
            "error": (r.get("error") or {}).get("message") or ""})
    return out


@span("progress.fail", kind="mutator")  # noqa: F821 - guest global
def fail(space, job, error, detail=None, notify=None):
    """Mark the job failed → process id.

    Emits the terminal `process.failed` frame with the error; the row
    lingers ~60s as the visible outcome, then expires — the DURABLE
    record of a failure is the notify chat message and the job's own
    state, not the bar (ADR-014 §4 as amended). A start()/tick() of
    the same job reopens it (the retry path). `notify` (§6): same as
    done() — a visible `trigger:<job>` chat message triggers the loop
    so the agent tells the user what broke."""
    pid = _pid(space, job)
    err = {"message": str(error or "")}
    try:
        if detail is not None:
            _any._process_progress(pid, {"message": detail})
        _any._process_finish(pid, {"status": "failed", "error": err})
    except _any.AnyError as e:
        if e.code != "process.not_found":
            raise
        # expired or never started: materialize, then fail it
        _register(space, job, job)
        if detail is not None:
            _any._process_progress(pid, {"message": detail})
        _any._process_finish(pid, {"status": "failed", "error": err})
    if notify is not None:
        d = f" ({detail})" if detail else ""
        _arm_notify(notify, space, job, _NUDGE.format(
            job=job, label="", space=space,
            outcome=f"FAILED: {err['message']}{d}"))
    return pid
