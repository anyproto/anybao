"""Brain caretaker: finds the brain's faults, repairs what it may, tells the rest.

A cron job the brain's setup run writes (every 900 s). Plain code grades the
brain's jobs, mail sync, lists and tiers from the records the UI shows, keeps
one incident per fault with one observation per tick, picks each incident's
one repair from a rule table, verifies every repair twice, and puts the rest
on the user's list. No model is involved: nothing here decides by model
whether something is broken or what to do about it.
"""

__any_category__ = "plugin"  # the AI brain is a plugin of the connectors repo (ADR-033 §1.1)

# ADR-033 §6. Args {space, hub, datasets}. Repairs are record writes only: a
# rerun once-trigger, `enabled: true` on a brain job the runtime stopped, a
# chat notice. Everything else is advice on the user's list. A repair is
# "tried, unverified" until the fault reads clean at verifyAt and again at
# the end of the soak; both windows are at least two of the job's own
# cadences, since the runtime shows a repair's effect only on the job's next
# run. Ages count from the later of their own base and `uptimeSince`, the
# first tick after a gap: at boot the runtime re-arms every cron strictly
# forward, so a job's first run is boot + every_s, and the pre-shutdown
# lastRunAt says nothing about whether it is late.

import hashlib

kit = use("brainKit@v1")  # noqa: F821 - guest global
_any = use("agent:any@v1")  # noqa: F821 - guest global

PROGRAM = "connectors:brainCaretaker@v1"
BRAIN = "connectors:brain"
PROMISES = "connectors:brainPromises@v1"
FACTS = "connectors:brainFacts@v1"
DRAFTER = "connectors:brainDrafter@v1"
SETUP_TRIGGER = "brain-setup"
SETUP_PROGRAM = "connectors:brainSetup@v1"
TRIGGERS = "agent_triggers"
RUNS = "agent_runs"
UNAVAILABLE = "unavailable"
OPEN_STATES = ("open", "repairing", "soaking", "escalated")
STOPPED = "auto_disabled"

EVERY_S = 900                 # this job's cadence when its own record does not say
OVERDUE_GRACE_S = 120
MAIL_STALE_S = 6 * 3600
SETUP_BLOCKED_S = 3600
BACKLOG_COUNT = 3
BACKLOG_AGE_S = 3600
DRAFT_ATTEMPTS = 3
VERIFY_S = 900                # the floors; a job's own cadence widens both (§6.7)
SOAK_S = 3600
ESCALATED_OBSERVE_S = 3600    # an escalated incident waits on the user: hourly rows
NOTICE_COOLDOWN_MIN = 60
NOTICE_LINES = 10
DAY = 86400

# the tier a brain job calls when its args name none
MODEL_TIERS = {PROMISES: "classify", FACTS: "brain.extractor"}
HUB_CONFIG = ("caretaker", "exclusions")
REPAIRS = ("run_job", "enable_job")   # the record repairs; "escalate" is not one


class unconfigured(Exception):
    """The job's args do not name the brain hub and its stores."""


class snapshot_failed(Exception):
    """A read the caretaker cannot grade or reconcile without failed."""


# --- small helpers ---------------------------------------------------------

def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _secs(v):
    """Seconds of a stamp, an instant or a number, as an int; None when unreadable."""
    s = ts_s(v)  # noqa: F821 - guest global
    return int(s) if s is not None else None


def _iso_secs(text):
    return _secs({"$date": text}) if isinstance(text, str) and text else None


def _sha(raw):
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def incident_id(fault_class, subject, opened_at):
    return _sha(f"{fault_class}|{subject}|{int(opened_at)}")


def card_id(kind, subject_key):
    # the cards contract (ADR-033 §4.6): id = sha256(kind | subjectKey), the
    # full hex digest, so the heartbeat and the caretaker name a card alike
    return hashlib.sha256(f"{kind}|{subject_key}".encode()).hexdigest()


def _config(key):
    """An agent_config row's value; None when the key has no row, UNAVAILABLE on any
    other failure."""
    try:
        return effect("config.get", {"key": key})["value"]  # noqa: F821 - guest global
    except EffectError as e:  # noqa: F821 - guest global
        return None if "no config value" in str(e) else UNAVAILABLE


def _read(fn):
    """fn(), or UNAVAILABLE when the read fails: a field the code could not read is
    passed on as "unavailable", never omitted (ADR-033 §6.2)."""
    try:
        return fn()
    except Exception:
        return UNAVAILABLE


def _live_datasets(hub, datasets, status):
    """The job's datasets, plus what the hub declares now: a job's args may predate
    a store the hub declares now."""
    live = status.get("hub") if isinstance(status, dict) else None
    if isinstance(live, dict) and live.get("objectId") == hub \
            and isinstance(live.get("datasets"), dict):
        return {**datasets, **live["datasets"]}
    return dict(datasets)


# --- the snapshot ----------------------------------------------------------

def _brain_jobs(triggers):
    return [t for t in triggers if t.get("kind") == "cron"
            and str(t.get("program") or "").startswith(BRAIN)]


def _enabled(t):
    return t.get("enabled", True) is not False


def _every_s(t):
    every = ((t or {}).get("spec") or {}).get("every_s")
    return every if _num(every) and every > 0 else None


def _job_tier(job):
    return (job.get("args") or {}).get("tier") or MODEL_TIERS.get(job.get("program"))


def _tiers_in_use(triggers, datasets):
    # the caretaker calls no model: a `tier` its own args may carry is not a tier in use
    tiers = {_job_tier(j) for j in _brain_jobs(triggers)
             if _enabled(j) and j.get("program") != PROGRAM}
    if datasets.get("drafts"):
        tiers.add("brain.drafter")
    return sorted(t for t in tiers if t)


def _mail_trigger_id(status):
    sync = status.get("mailSync") if isinstance(status, dict) else None
    if status == UNAVAILABLE:
        return UNAVAILABLE
    return sync.get("triggerId") if isinstance(sync, dict) else None


def _newest_run(space, runs_anchor, tid):
    rows = _any.query(space, runs_anchor, RUNS, filter={"triggerId": tid},
                      sort=["-startedAt"], limit=1)
    return rows[0] if rows else None


def _sync_state(space):
    # the sync_state type arrives with the first mail sync; a query naming a
    # type the space lacks raises
    if not any(t.get("xKey") == "sync_state" for t in _any.list_types(space)):
        return None
    rows = _any.query_objects(space, filter={"any.type": "sync_state"}, limit=10)
    return max(rows, key=lambda r: ts_s(r.get("modifiedAt")) or 0) if rows else None  # noqa: F821


def _credential():
    connected = (use("googleAuth@v1").status() or {}).get("connected")  # noqa: F821
    return connected if isinstance(connected, bool) else UNAVAILABLE


def _active_device():
    return (_any.list_devices().get("active") or {}).get("bao")


def _hub_config(space, hub, datasets):
    """{name: value} of the hub config records the caretaker reads, absent ones
    left out; raises snapshot_failed (the caretaker's own state is among them)."""
    ids = [f"{hub}/{name}" for name in HUB_CONFIG]
    try:
        rows = _any.query(space, hub, datasets["config"], filter={"id": {"$in": ids}},
                          limit=len(ids))
    except Exception as e:
        raise snapshot_failed(f"the hub's config could not be read: {e}") from e
    return {kit.plain_id(r["id"]): r for r in rows}


def own_every_s(triggers):
    """The caretaker's own cadence, from its record (fallback EVERY_S)."""
    mine = [t for t in _brain_jobs(triggers) if t.get("program") == PROGRAM]
    return next((_every_s(t) for t in mine if _every_s(t)), EVERY_S)


def uptime_since(state, triggers, tick):
    """The base every age counts from (ADR-033 §6.3): this tick when the caretaker
    never ticked, or when the gap since its last tick is longer than its own
    cadence plus grace — the device was down or asleep, and every job's next
    run is now boot + every_s; else the value the state carries."""
    last, since = state.get("lastTickAt"), state.get("uptimeSince")
    if not _num(last) or not _num(since) or tick - last > own_every_s(triggers) + OVERDUE_GRACE_S:
        return tick
    return since


def snapshot(space, hub, datasets, tick, status, records, state):
    """Everything the graders read, in one pass (ADR-033 §6.2)."""
    try:
        anchor = _any.bundle_child(space, "bao/v1", "bao/triggers/v1")["objectId"]
        triggers = _any.query(space, anchor, TRIGGERS, limit=500)
    except Exception as e:
        raise snapshot_failed(f"the brain's job records could not be read: {e}") from e
    runs_anchor = _read(lambda: _any.bundle_child(space, "bao/v1", "bao/runs/v1")["objectId"])
    mail_tid = _mail_trigger_id(status)
    watched = [j["id"] for j in _brain_jobs(triggers)]
    if isinstance(mail_tid, str) and mail_tid:
        watched.append(mail_tid)
    runs = {}
    for tid in watched:
        runs[tid] = (UNAVAILABLE if runs_anchor == UNAVAILABLE
                     else _read(lambda t=tid: _newest_run(space, runs_anchor, t)))
    cards = None
    if datasets.get("cards"):
        cards = _read(lambda: _any.query(space, hub, datasets["cards"],
                                         filter={"kind": "reply", "status": "open"}, limit=300))
    return {
        "now": tick, "space": space, "hub": hub, "datasets": datasets, "anchor": anchor,
        "uptimeSince": uptime_since(state, triggers, tick),
        "triggers": triggers, "runs": runs,
        "brainSpace": _config("brain.space"), "brainStatus": status,
        "exclusions": records.get("exclusions"),
        "tiers": {t: _config(f"llm.tier.{t}") for t in _tiers_in_use(triggers, datasets)},
        "syncState": _read(lambda: _sync_state(space)),
        "credential": _read(_credential),
        "activeDevice": _read(_active_device),
        "cards": cards,
    }


# --- the graders (pure over the snapshot) ----------------------------------

def _fault(fault_class, subject, evidence):
    return {"faultClass": fault_class, "subject": subject, "evidence": evidence}


def _trigger(snap, tid):
    return next((t for t in snap["triggers"] if t.get("id") == tid), None)


def _setup_once(snap):
    """Setup's once, found by program and kind as setup finds it: a deleted
    `brain-setup` is rewritten under a fresh id."""
    found = [t for t in snap["triggers"]
             if t.get("kind") == "once" and t.get("program") == SETUP_PROGRAM]
    return min(found, key=lambda t: t.get("id") or "") if found else None


def _status_at(snap):
    status = snap["brainStatus"]
    return _iso_secs(status.get("at")) if isinstance(status, dict) else None


def _age(snap, since):
    """Seconds since `since`, counted from uptimeSince when that is later: a stamp
    from before a gap says nothing about lateness now (ADR-033 §6.3)."""
    return snap["now"] - max(since, snap["uptimeSince"])


def _last_run(snap, job):
    """(seconds, status) of a job's newest run — its record's lastRunAt, its newest
    synced run, its caretaker reruns — or (None, None) when it never ran."""
    seen = []
    if _secs(job.get("lastRunAt")) is not None:
        seen.append((_secs(job.get("lastRunAt")), job.get("lastStatus")))
    run = snap["runs"].get(job["id"])
    if isinstance(run, dict) and _secs(run.get("startedAt")) is not None:
        seen.append((_secs(run.get("startedAt")), run.get("status")))
    prefix = f"brain-rerun-{job['id']}-"
    for t in snap["triggers"]:
        if str(t.get("id") or "").startswith(prefix) and _secs(t.get("lastRunAt")) is not None:
            seen.append((_secs(t.get("lastRunAt")), t.get("lastStatus")))
    return max(seen, key=lambda s: s[0]) if seen else (None, None)


def _graded_jobs(snap):
    # the caretaker does not grade itself: it is running
    return [j for j in _brain_jobs(snap["triggers"]) if j.get("program") != PROGRAM]


def grade_job_overdue(snap):
    out = []
    for job in _graded_jobs(snap):
        every = _every_s(job)
        if not _enabled(job) or every is None:
            continue
        last, status = _last_run(snap, job)
        since = last if last is not None else _status_at(snap)
        if since is None:
            continue
        overdue = _age(snap, since) - (2 * every + OVERDUE_GRACE_S)
        if overdue > 0:
            out.append(_fault("job_overdue", job["id"], {
                "lastRunAt": last, "lastStatus": status, "everyS": every,
                "overdueS": int(overdue), "owner": job.get("owner") or None,
                "activeDevice": snap["activeDevice"]}))
    return out


def grade_job_failing(snap):
    out = []
    for job in _graded_jobs(snap):
        fails = job.get("consecutiveFailures")
        stopped = job.get("lastStatus") == STOPPED
        if not stopped and (not _enabled(job) or not _num(fails) or fails < 2):
            continue                        # a paused job is the user's choice
        run = snap["runs"].get(job["id"])
        error = run.get("errorType") if isinstance(run, dict) else run
        out.append(_fault("job_failing", job["id"], {
            "consecutiveFailures": fails if _num(fails) else 0,
            "lastStatus": job.get("lastStatus"), "lastErrorType": error,
            "lastRunAt": _secs(job.get("lastRunAt")), "enabled": _enabled(job)}))
    return out


def _mail_paused(snap):
    """True when the user paused the mail sync: `enabled: false` without the
    runtime's stop. A pause holds (ADR-033 §1.4); mail is then stale by choice."""
    tid = _mail_trigger_id(snap["brainStatus"])
    trig = _trigger(snap, tid) if isinstance(tid, str) else None
    return trig is not None and not _enabled(trig) and trig.get("lastStatus") != STOPPED


def grade_mail_stale(snap):
    if _mail_paused(snap):
        return []
    sync, credential = snap["syncState"], snap["credential"]
    state = (sync.get("sync_state") or {}) if isinstance(sync, dict) else {}
    if sync == UNAVAILABLE:
        modified, since = UNAVAILABLE, None
    elif sync is None:
        modified, since = None, _status_at(snap)    # no mail sync has checkpointed yet
    else:
        modified = _secs(sync.get("modifiedAt"))
        since = modified
    by_age = since is not None and _age(snap, since) > MAIL_STALE_S
    if credential is not False and not by_age:
        return []
    tid = _mail_trigger_id(snap["brainStatus"])
    run = snap["runs"].get(tid) if isinstance(tid, str) else tid
    if isinstance(run, dict):
        last_status = run.get("status")
    elif run is None:
        trig = _trigger(snap, tid) if isinstance(tid, str) else None
        last_status = (trig or {}).get("lastStatus")
    else:
        last_status = UNAVAILABLE
    return [_fault("mail_stale", state.get("mailbox_id") or "mail", {
        "syncStateModifiedAt": modified, "credentialConnected": credential,
        "cursor": state.get("cursor") if isinstance(sync, dict) else sync,
        "skippedCount": state.get("skipped_count") if isinstance(sync, dict) else sync,
        "mailTriggerId": tid, "lastRunStatus": last_status})]


def grade_exclusions_unreadable(snap):
    rec = snap["exclusions"]
    if rec == UNAVAILABLE:
        return []
    problem = kit.exclusions_problem(rec)
    if problem is None:
        return []
    value = (rec or {}).get("value")
    return [_fault("exclusions_unreadable", "config/exclusions", {
        "present": rec is not None,
        "version": value.get("version") if isinstance(value, dict) else None,
        "parseError": problem})]


def grade_tier_unpinned(snap):
    out = []
    for tier, row in sorted(snap["tiers"].items()):
        if row == UNAVAILABLE or (isinstance(row, dict) and row.get("model")):
            continue
        row = row if isinstance(row, dict) else {}
        out.append(_fault("tier_unpinned", f"llm.tier.{tier}", {
            "present": bool(row), "provider": row.get("provider"),
            "model": row.get("model")}))
    return out


def _request_state(request, status):
    """brainSetup's reading of a request and its answer (ADR-033 §1.2)."""
    answered = isinstance(status, dict) and status.get("requestedAt") == request["requestedAt"]
    if request.get("spaceId") is None:
        return "off" if answered and status.get("state") == "off" else "switching_off"
    if not answered:
        return "waiting"
    return {"provisioning": "setting_up", "ready": "ready",
            "error": "error"}.get(status.get("state"), "waiting")


def grade_setup_blocked(snap):
    request, status = snap["brainSpace"], snap["brainStatus"]
    if not isinstance(request, dict) or not isinstance(request.get("requestedAt"), str) \
            or status == UNAVAILABLE:
        return []
    setup = _setup_once(snap)
    state = _request_state(request, status)
    answered_at = _iso_secs(status.get("at")) if isinstance(status, dict) else None
    age = None
    if state in ("waiting", "setting_up", "switching_off"):
        since = answered_at if state == "setting_up" else _iso_secs(request["requestedAt"])
        age = int(_age(snap, since)) if since is not None else None
    by_age = age is not None and age > SETUP_BLOCKED_S
    if setup is not None and not by_age:
        return []
    return [_fault("setup_blocked", SETUP_TRIGGER, {
        "requestState": state, "requestedAt": request["requestedAt"],
        "answeredAt": answered_at, "ageS": age, "setupTriggerPresent": setup is not None,
        "setupOwner": (setup or {}).get("owner") or None,
        "setupLastRunAt": _secs((setup or {}).get("lastRunAt")),
        "activeDevice": snap["activeDevice"]})]


def grade_draft_backlog(snap):
    cards = snap["cards"]
    if not isinstance(cards, list):
        return []
    now_s = snap["now"]
    waiting = [c for c in cards if c.get("kind") == "reply" and c.get("status") == "open"
               and not c.get("draftId")]
    old = [c for c in waiting if (_secs(c.get("createdAt")) or now_s) < now_s - BACKLOG_AGE_S]
    spent = [c for c in waiting if _num(c.get("draftAttempts"))
             and c["draftAttempts"] >= DRAFT_ATTEMPTS]
    if len(old) < BACKLOG_COUNT and not spent:
        return []
    drafters = [t for t in snap["triggers"] if t.get("program") == DRAFTER]
    newest = max(drafters, key=lambda t: _secs(t.get("lastRunAt")) or 0, default=None)
    return [_fault("draft_backlog", "cards", {
        "count": len(old),
        "oldestOpenAt": min((_secs(c.get("createdAt")) for c in old
                             if _secs(c.get("createdAt")) is not None), default=None),
        "attemptsExhausted": len(spent),
        "drafterLastStatus": (newest or {}).get("lastStatus")})]


GRADERS = (grade_job_overdue, grade_job_failing, grade_mail_stale,
           grade_exclusions_unreadable, grade_tier_unpinned, grade_setup_blocked,
           grade_draft_backlog)


def grade(snap):
    """Faults by (faultClass, subject)."""
    out = {}
    for g in GRADERS:
        for f in g(snap):
            out.setdefault((f["faultClass"], f["subject"]), f)
    return out


def _job_name(snap, tid):
    return (_trigger(snap, tid) or {}).get("name") or tid


def _hours(seconds):
    return f"{max(1, round(seconds / 3600))} h" if _num(seconds) else "a while"


def summary(fault_class, subject, evidence, snap):
    """One plain line saying what is wrong — the incident's, the card's and the
    notice's."""
    e = evidence or {}
    if fault_class == "job_overdue":
        return f"{_job_name(snap, subject)} is overdue by {_hours(e.get('overdueS'))}"
    if fault_class == "job_failing":
        if e.get("lastStatus") == STOPPED:
            return f"{_job_name(snap, subject)} was stopped after repeated failures"
        return f"{_job_name(snap, subject)} failed {e.get('consecutiveFailures')} times in a row"
    if fault_class == "mail_stale":
        if e.get("credentialConnected") is False:
            return "Google is not connected, so mail does not sync"
        return "mail has not synced for a long while"
    if fault_class == "exclusions_unreadable":
        return f"the exclusion list is unreadable ({e.get('parseError')})"
    if fault_class == "tier_unpinned":
        return f"no model is set for {subject}"
    if fault_class == "setup_blocked":
        if not e.get("setupTriggerPresent"):
            return ("the brain-setup job is gone; switching the brain on again "
                    "in chat makes a new one")
        return f"brain setup has not answered for {_hours(e.get('ageS'))}"
    if fault_class == "draft_backlog":
        return f"{e.get('count')} reply cards are waiting for a draft"
    return f"{fault_class}: {subject}"


# --- the rule table and the repairs ----------------------------------------

def _mail_job(snap):
    """The mail sync's record when brain.status.mailSync names one, else None."""
    tid = _mail_trigger_id(snap["brainStatus"])
    return _trigger(snap, tid) if isinstance(tid, str) else None


def _brain_job(snap, tid):
    job = _trigger(snap, tid)
    if job is None or job.get("kind") != "cron" \
            or not str(job.get("program") or "").startswith(BRAIN):
        return None
    return job


def repair_for(fault_class, subject, snap):
    """The rule table (ADR-033 §6.5): the one record repair for an incident, or
    "escalate" when none applies — a card and a notice line put it on the
    user's list. The target is the incident's subject, read from the snapshot."""
    if fault_class == "job_overdue" and _brain_job(snap, subject) is not None:
        return "run_job"
    if fault_class == "job_failing":
        job = _brain_job(snap, subject)
        if job is not None and job.get("lastStatus") == STOPPED and not _enabled(job):
            return "enable_job"
    if fault_class == "mail_stale":
        job = _mail_job(snap)
        if job is not None and _enabled(job):
            return "run_job"
    return "escalate"


def cadence_s(fault_class, subject, snap):
    """The every_s of the job an incident is about (the sync trigger's for
    mail_stale); None for an incident that is not about a job."""
    job = _mail_job(snap) if fault_class == "mail_stale" else _trigger(snap, subject)
    return _every_s(job)


def windows(every):
    """(verify, soak) in seconds for a job of cadence `every`: the floors, widened
    to two cadences plus grace — the runtime shows a repair's effect only on the
    job's next own run (ADR-033 §6.7)."""
    span = 2 * every + OVERDUE_GRACE_S if _num(every) else 0
    return max(VERIFY_S, span), max(SOAK_S, span)


def pending(incident, fault):
    """True while an enable_job repair waits for the job's next own run: the record
    still reads the runtime's stop, and nothing newer has run since the repair.
    Neither clean nor dirty (ADR-033 §6.7)."""
    if incident.get("action") != "enable_job" or not isinstance(fault, dict):
        return False
    e = fault.get("evidence") or {}
    acted = _secs(incident.get("actionAt"))
    last = e.get("lastRunAt")
    return (e.get("enabled") is True and e.get("lastStatus") == STOPPED and acted is not None
            and (last is None or (_num(last) and last < acted)))


def step(incident, now_s, clean, soak_s=SOAK_S):
    """The ladder (ADR-033 §6.4, §6.7) as a pure transition → the fields to set, in
    seconds, or None. `clean` is True, False, or None for a pending repair (no
    step). `verified: False` with `escalated` means a repair failed."""
    status = incident.get("status")
    due = (_secs(incident.get("verifyAt")) or 0) <= now_s
    if status in ("open", "escalated"):
        return {"status": "closed", "closedAt": now_s} if clean else None
    if clean is None:
        return None
    if status == "repairing":
        if not due:
            return None
        return ({"status": "soaking", "verifyAt": now_s + soak_s} if clean
                else {"status": "escalated", "verified": False})
    if status == "soaking":
        if not clean:
            return {"status": "escalated", "verified": False}
        return {"status": "closed", "closedAt": now_s, "verified": True} if due else None
    return None


# --- the run ---------------------------------------------------------------

_TIME_FIELDS = ("openedAt", "closedAt", "actionAt", "verifyAt")


class _Run:
    def __init__(self, space, hub, datasets, tick, state):
        self.space, self.hub, self.ds, self.tick = space, hub, datasets, tick
        self.state = dict(state)
        self.counts = {"faults": 0, "opened": 0, "observed": 0, "repaired": 0,
                       "verified": 0, "healed": 0, "escalated": 0, "refused": 0}
        self.did, self.needs = [], []
        self.incidents = {}
        self.snap = None
        self.faults = {}

    # writes
    def _patch(self, iid, fields):
        ops = [{"type": "$set", "path": k, "value": instant(v) if k in _TIME_FIELDS else v}  # noqa: F821
               for k, v in fields.items()]
        kit.checked(_any.modify(self.space, {"objectId": self.hub,
                                             "dataset": self.ds["incidents"],
                                             "records": [{"id": iid, "ops": ops}]}),
                    f"updating incident {iid}")
        self.incidents[iid].update(fields)

    def _signal(self, iid, reason):
        kit.upsert(self.space, self.hub, self.ds["signals"],
                   f"{int(self.tick)}-repair_refused-{iid}",
                   {"tick": int(self.tick), "kind": "repair_refused", "subject": iid,
                    "decision": "refused", "reason": reason[:500]},
                   "writing a repair_refused signal")
        self.counts["refused"] += 1

    def _queue_notice(self, iid, line):
        pending_lines = list(self.state.get("pending") or [])
        if not any(p.get("incidentId") == iid and p.get("line") == line for p in pending_lines):
            pending_lines.append({"incidentId": iid, "line": line[:300]})
        self.state["pending"] = pending_lines

    # reconcile
    def reconcile(self, snap, faults, live):
        self.snap, self.faults = snap, faults
        self.counts["faults"] = len(faults)
        for row in live:
            self.incidents[kit.plain_id(row["id"])] = {**row, "id": kit.plain_id(row["id"])}
        have = {(i.get("faultClass"), i.get("subject")) for i in self.incidents.values()}
        new = []
        for key, fault in faults.items():
            if key in have:
                continue
            iid = incident_id(fault["faultClass"], fault["subject"], self.tick)
            fields = {"faultClass": fault["faultClass"], "subject": fault["subject"],
                      "openedAt": self.tick, "status": "open",
                      "summary": summary(fault["faultClass"], fault["subject"],
                                         fault["evidence"], snap)[:300]}
            new.append({"id": iid, "fields": {**fields, "openedAt": instant(self.tick)}})  # noqa: F821
            self.incidents[iid] = {"id": iid, **fields}
        if new:
            kit.checked(_any.upsert_records(self.space, self.hub, self.ds["incidents"], new),
                        "opening incidents")
            self.counts["opened"] = len(new)
            self.did.append(f"opened {len(new)} incident{'s' * (len(new) != 1)}")
        self._observe()
        for iid, inc in list(self.incidents.items()):
            fault = faults.get((inc.get("faultClass"), inc.get("subject")))
            clean = None if pending(inc, fault) else fault is None
            _, soak = windows(cadence_s(inc.get("faultClass"), inc.get("subject"), snap))
            patch = step(inc, self.tick, clean, soak)
            if patch is None:
                continue
            if patch.get("status") == "escalated":
                self._escalate(iid, "the repair did not hold", patch)
                continue
            self._patch(iid, patch)
            if patch.get("verified") is True:
                self.counts["verified"] += 1
                self.did.append(f"verified the repair of {self._title(iid)}")
            elif patch.get("status") == "closed":
                self.counts["healed"] += 1
                self.did.append(f"closed {self._title(iid)}: clean again")

    def _observe(self):
        """One observation per incident this tick; an escalated one that is still
        dirty only hourly, so a fault the user never fixes does not grow the store
        by a row every tick. A pending repair's row says so."""
        records, seen = [], dict(self.state.get("observedAt") or {})
        for iid, inc in self.incidents.items():
            fault = self.faults.get((inc.get("faultClass"), inc.get("subject")))
            if fault and inc.get("status") == "escalated":
                last = seen.get(iid)
                if _num(last) and self.tick - last < ESCALATED_OBSERVE_S:
                    continue
                seen[iid] = self.tick
            evidence = {"clean": True} if not fault else (
                {**fault["evidence"], "pending": True} if pending(inc, fault)
                else fault["evidence"])
            records.append({"id": f"{iid}:{int(self.tick)}", "fields": {
                "incidentId": iid, "tick": int(self.tick), "evidence": evidence}})
        self.state["observedAt"] = {k: v for k, v in seen.items() if k in self.incidents}
        if records:
            kit.checked(_any.upsert_records(self.space, self.hub, self.ds["observations"],
                                            records), "writing observations")
        self.counts["observed"] = len(records)

    def _title(self, iid):
        inc = self.incidents[iid]
        return f"{inc.get('faultClass')}: {inc.get('subject')}"

    def _summary(self, iid):
        inc = self.incidents[iid]
        fault = self.faults.get((inc.get("faultClass"), inc.get("subject")))
        if fault is None:
            return inc.get("summary") or self._title(iid)
        return summary(inc.get("faultClass"), inc.get("subject"), fault.get("evidence"),
                       self.snap)

    # escalation
    def _card(self, iid, reason):
        """The incident's card in `cards` when the hub has it → its id, else None."""
        if not self.ds.get("cards"):
            return None
        cid = card_id("incident", iid)
        if kit.hub_record(self.space, self.hub, self.ds["cards"], cid) is None:
            # written only when absent: a card the user dismissed stays dismissed
            kit.upsert(self.space, self.hub, self.ds["cards"], cid, {
                "kind": "incident", "title": self._title(iid), "reason": reason[:500],
                "subjectKey": iid, "status": "open",
                "sourceUri": f"any://o/{self.space}/{self.hub}/incidents/{iid}"},
                "writing an incident card")
        return cid

    def _escalate(self, iid, why=None, fields=None):
        """Card (when the hub has `cards`) and a notice line, once per incident; the
        incident's own fields land in the same write."""
        fields = {**(fields or {}), "status": "escalated"}
        if not fields.get("action") and not self.incidents[iid].get("action"):
            fields["action"] = "escalate"   # a failed repair keeps the action it tried
            fields["actionAt"] = self.tick
        line = self._summary(iid) + (f" ({why})" if why else "")
        cid = self._card(iid, line)
        if cid:
            fields["cardId"] = cid
        self._patch(iid, fields)
        self._queue_notice(iid, line)
        self.counts["escalated"] += 1
        self.did.append(f"escalated {self._title(iid)}")
        self.needs.append(self._summary(iid))

    # the repairs: one per incident, from the rule table
    def repairs(self):
        """An incident that has had no repair asks the table each tick: an open one
        is repaired or escalated; an escalated one is repaired when the table now
        offers a repair (a job failing twice is escalated; the runtime's stop at
        the third failure then gets its enable_job). A tried repair, held or not,
        is the incident's only one."""
        for iid in sorted(self.incidents):
            inc = self.incidents[iid]
            if inc.get("status") not in ("open", "escalated") or inc.get("action") in REPAIRS:
                continue
            action = repair_for(inc.get("faultClass"), inc.get("subject"), self.snap)
            if action == "escalate":
                if inc.get("status") == "open":
                    self._escalate(iid)
                continue
            refusal = self._execute(iid, action)
            if refusal is not None:
                self._signal(iid, refusal)
                self._escalate(iid, refusal, {"action": action, "actionAt": self.tick})
                continue
            verify, _ = windows(cadence_s(inc.get("faultClass"), inc.get("subject"), self.snap))
            self._patch(iid, {"status": "repairing", "action": action, "actionAt": self.tick,
                              "verifyAt": self.tick + verify})
            self.counts["repaired"] += 1
            self.did.append(f"tried {action} on {self._title(iid)} (unverified)")

    # the target is read from the incident and the snapshot
    def _execute(self, iid, action):
        """Run one repair → None, or why it was refused."""
        inc = self.incidents[iid]
        try:
            if action == "run_job":
                return self._run_job(inc)
            if action == "enable_job":
                return self._enable_job(inc)
        except Exception as e:
            return f"{action} failed: {type(e).__name__}: {str(e)[:200]}"
        return f"unknown action {action!r}"

    def _job_of(self, inc):
        if inc.get("faultClass") == "mail_stale":
            return _mail_job(self.snap)
        return _brain_job(self.snap, inc.get("subject"))

    def _run_job(self, inc):
        job = self._job_of(inc)
        if job is None or not _enabled(job):
            return "run_job: no enabled job to run"
        taken = {t.get("id") for t in self.snap["triggers"]}
        rid = base = f"brain-rerun-{job['id']}-{int(self.tick)}"
        n = 1
        while rid in taken:                 # a fresh id, checked against what was read
            n += 1
            rid = f"{base}-{n}"
        kit.upsert(self.space, self.snap["anchor"], TRIGGERS, rid, {
            "name": f"rerun {job.get('name') or job['id']}", "kind": "once",
            "spec": {"at": self.tick}, "program": job["program"],
            "args": job.get("args") or {}, "enabled": True}, f"arming {rid}")
        self.snap["triggers"].append({"id": rid, "kind": "once"})
        return None

    def _enable_job(self, inc):
        job = _brain_job(self.snap, inc.get("subject"))
        if job is None:
            return "enable_job: only a brain job's own record"
        if _enabled(job) or job.get("lastStatus") != STOPPED:
            return "enable_job: the job was not stopped by the runtime"
        kit.checked(_any.modify(self.space, {
            "objectId": self.snap["anchor"], "dataset": TRIGGERS,
            "records": [{"id": job["id"], "ops": [
                {"type": "$set", "path": "enabled", "value": True}]}]}),
            f"enabling {job['id']}")
        return None

    # notices, the report, the standup
    def flush_notices(self):
        # a line whose incident closed meanwhile is news no longer
        pending_lines = [p for p in self.state.get("pending") or []
                         if (self.incidents.get(p.get("incidentId")) or {}).get("status")
                         not in (None, "closed")]
        self.state["pending"] = pending_lines
        if not pending_lines:
            return 0
        cooldown = self.state.get("noticeCooldownMin")
        cooldown = (cooldown if _num(cooldown) else NOTICE_COOLDOWN_MIN) * 60
        last = self.state.get("lastNoticeAt")
        if _num(last) and self.tick - last < cooldown:
            return 0
        lines = [f"• {p.get('line')}" for p in pending_lines[:NOTICE_LINES]]
        if len(pending_lines) > NOTICE_LINES:
            lines.append(f"…and {len(pending_lines) - NOTICE_LINES} more")
        text = "The brain needs a look:\n" + "\n".join(lines)
        if not kit.notice(self.space, text, counts=self.counts):
            return 0                         # kept for the next tick
        self.state["pending"], self.state["lastNoticeAt"] = [], self.tick
        return len(pending_lines)

    def report(self):
        """On the first tick of a UTC day: one line over the incidents opened or
        closed in the last 24 h (ADR-033 §6.8). An escalated incident that waits
        on the user for a week is reported the day it opened, not every day."""
        day = fmt_ts(self.tick, "%Y-%m-%d", 0)[:10]  # noqa: F821 - guest global
        if self.state.get("lastReportDay") == day:
            return
        self.state["lastReportDay"] = day
        if not (self.ds.get("standups") or self.ds.get("cards")):
            return
        floor = instant(self.tick - DAY)  # noqa: F821 - guest global
        closed = _read(lambda: _any.query(
            self.space, self.hub, self.ds["incidents"],
            filter={"status": "closed", "closedAt": {"$gt": floor}}, limit=500))
        recent = [i for i in self.incidents.values()
                  if (_secs(i.get("openedAt")) or 0) > self.tick - DAY]
        rows = recent + (closed if isinstance(closed, list) else [])
        seen, tally = set(), {"fixed": 0, "tried": 0, "escalated": 0, "healed": 0, "open": 0}
        for row in rows:
            iid = kit.plain_id(row.get("id"))
            if iid in seen:
                continue
            seen.add(iid)
            status, verified = row.get("status"), row.get("verified")
            if status == "closed":
                key = ("fixed" if verified is True
                       else "escalated" if verified is False else "healed")
            else:
                key = {"repairing": "tried", "soaking": "tried",
                       "escalated": "escalated"}.get(status, "open")
            tally[key] += 1
        if not seen:
            return                           # nothing happened: success is silent
        line = (f"{len(seen)} incidents: {tally['fixed']} fixed (verified), "
                f"{tally['tried']} tried, unverified, {tally['escalated']} escalated, "
                f"{tally['healed']} healed by themselves, {tally['open']} open")
        self.did.append(line)
        if self.ds.get("cards"):
            subject = f"caretaker:{day}"
            cid = card_id("info", subject)
            if kit.hub_record(self.space, self.hub, self.ds["cards"], cid) is None:
                kit.upsert(self.space, self.hub, self.ds["cards"], cid, {
                    "kind": "info", "title": f"Caretaker report {day}", "reason": line,
                    "subjectKey": subject, "status": "open"}, "writing the caretaker report")
        self.counts["reported"] = day

    def standup(self):
        if not self.ds.get("standups") or not self.did:
            return
        kit.upsert(self.space, self.hub, self.ds["standups"], f"caretaker:{int(self.tick)}", {
            "job": "caretaker", "tick": int(self.tick), "did": self.did[:20],
            "needs": self.needs[:20],
            "counts": {k: v for k, v in self.counts.items() if _num(v)}},
            "writing the caretaker's standup")

    def save_state(self):
        self.state["lastTickAt"] = self.tick
        self.state["uptimeSince"] = self.snap["uptimeSince"]
        self.state.setdefault("noticeCooldownMin", NOTICE_COOLDOWN_MIN)
        kit.write_config(self.space, self.hub, self.ds, "caretaker", self.state)


def _args(args):
    """(space, hub, datasets, brain.status) or raise unconfigured."""
    parsed = kit.job_args(args)
    if parsed is not None:
        space, hub, datasets = parsed
        status = _config("brain.status")
        datasets = _live_datasets(hub, datasets, status)
        if all(datasets.get(k) for k in ("incidents", "observations", "signals")):
            return space, hub, datasets, status
    raise unconfigured("the caretaker needs space, hub and the hub's incidents, "
                       "observations and signals — set the brain up first")


def main(args):
    space, hub, datasets, status = _args(args)
    tick = now()  # noqa: F821 - guest global
    records = _hub_config(space, hub, datasets)
    state = (records.get("caretaker") or {}).get("value")
    state = state if isinstance(state, dict) else {}
    run = _Run(space, hub, datasets, tick, state)
    snap = snapshot(space, hub, datasets, tick, status, records, state)
    faults = grade(snap)
    try:
        live = _any.query(space, hub, datasets["incidents"],
                          filter={"status": {"$in": list(OPEN_STATES)}}, limit=500)
    except Exception as e:
        raise snapshot_failed(f"the open incidents could not be read: {e}") from e
    run.reconcile(snap, faults, live)
    run.repairs()
    run.report()
    notices = run.flush_notices()
    run.save_state()
    run.standup()
    return {**run.counts, "notices": notices,
            "open": sorted(f"{c}:{s}" for c, s in faults)}
