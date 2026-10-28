"""Brain caretaker: finds the brain's faults, repairs what it may, tells the rest.

A cron job the brain's setup run writes (every 900 s). It refuses to run
without its pinned model, grades the brain's jobs, mail sync, lists and
tiers in plain code, keeps one incident per fault with one observation
per tick, asks one model turn per incident for a hypothesis and one
allowed repair, verifies every repair twice, and puts the rest on the
user's list.
"""

__any_category__ = "plugin"  # the AI brain is a plugin of the connectors repo (ADR-033 §1)

# ADR-038. Args {space, hub, datasets, tier}. The model never decides
# whether something is broken (the graders below do) and never names the
# target of a repair (the code reads it from the incident). Repairs are
# record writes only: a rerun once-trigger, `enabled: true` on a stopped
# brain job, a brain cursor 24 h back, a chat notice. Everything else is
# advice. A repair is "tried, unverified" until the fault reads clean at
# verifyAt and again after the soak.

import hashlib
import json

kit = use("brainKit@v1")  # noqa: F821 - guest global
_any = use("agent:any@v1")  # noqa: F821 - guest global
llm = use("agent:llm@v1")  # noqa: F821 - guest global

TIER = "brain.caretaker"
PROGRAM = "connectors:brainCaretaker@v1"
BRAIN = "connectors:brain"
PEOPLE = "connectors:brainPeople@v1"
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

EVERY_S = 900                 # this job's cadence, for its own away check
OVERDUE_GRACE_S = 120
MAIL_STALE_S = 6 * 3600
SETUP_BLOCKED_S = 3600
STALL_RUNS = 2
BACKLOG_COUNT = 3
BACKLOG_AGE_S = 3600
DRAFT_ATTEMPTS = 3
VERIFY_S = 900
SOAK_S = 3600
TURN_EVERY_S = 3600
MAX_CALLS = 5                 # model calls per run; the rest wait for the next tick
MAX_TOKENS = 600
CONFIDENCE_MIN = 0.6
HISTORY = 5
ESCALATED_OBSERVE_S = 3600    # an escalated incident waits on the user: hourly rows
REWIND_S = 86400
HYPOTHESIS_MAX = 500
NOTICE_COOLDOWN_MIN = 60
NOTICE_LINES = 10
DAY = 86400

# the tier a brain job calls when its args name none
MODEL_TIERS = {PROMISES: "classify", FACTS: "brain.extractor", PROGRAM: TIER}
# the jobs that read mail with a model: their (stats, cursor) records in the hub's config
STALL_WATCH = {PROMISES: ("promises.stats", "promises.cursor"),
               FACTS: ("facts.stats", "facts.cursor")}
HUB_CONFIG = ("caretaker", "exclusions", "promises.stats", "promises.cursor",
              "facts.stats", "facts.cursor", "people.cursor")
REPAIRS = ("run_job", "enable_job", "rewind_cursor", "notice")

CARETAKER_SYS = (
    "You are the caretaker of a personal AI brain: background jobs that read the "
    "user's mail and keep people, promises and facts. One of its own checks found "
    "a fault. You get the fault class, its subject, a one-line summary, the "
    "evidence the check read (\"unavailable\" means the check could not read that "
    "value), the last observations, and the actions you may take. Say what most "
    "likely causes it and choose ONE action from `allowed`; choose \"escalate\" "
    "when no allowed action addresses the cause or you are unsure. Actions: "
    "run_job runs the job once now; enable_job switches back on a job the runtime "
    "stopped after repeated failures; rewind_cursor makes a job re-read its last "
    "day of mail; notice tells the user in chat what they need to do; escalate "
    "puts the problem on the user's list. Answer with ONLY a JSON object: "
    "{\"hypothesis\": one or two sentences, \"confidence\": a number from 0 to 1, "
    "\"evidence\": [the evidence keys your hypothesis rests on, copied from the "
    "input], \"action\": one of allowed, \"target\": the subject, copied}."
)


class unpinned(Exception):
    """No model is pinned for the caretaker (llm.tier.<tier>): it refuses to run."""


class unconfigured(Exception):
    """The job's args do not name the brain hub and its stores."""


class snapshot_failed(Exception):
    """A read the caretaker cannot grade or reconcile without failed."""


class provider_failed(Exception):
    """Every model call of the run failed."""


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
    # Phase 3's cards contract: id = sha256(kind | subjectKey), the full
    # hex digest, so the heartbeat and the caretaker name a card alike
    return hashlib.sha256(f"{kind}|{subject_key}".encode()).hexdigest()


def _digest(value):
    return _sha(json.dumps(value, sort_keys=True, default=str))


def _config(key):
    """An agent_config row's value; None when the key has no row, UNAVAILABLE on any
    other failure."""
    try:
        return effect("config.get", {"key": key})["value"]  # noqa: F821 - guest global
    except EffectError as e:  # noqa: F821 - guest global
        return None if "no config value" in str(e) else UNAVAILABLE


def _read(fn):
    """fn(), or UNAVAILABLE when the read fails: a field the code could not read is
    passed on as "unavailable", never omitted (ADR-038 §2)."""
    try:
        return fn()
    except Exception:
        return UNAVAILABLE


def _pinned(tier):
    """The tier row, or raise unpinned: a missing row, or one naming no model."""
    key = f"llm.tier.{tier}"
    try:
        row = effect("config.get", {"key": key})["value"]  # noqa: F821 - guest global
    except EffectError as e:  # noqa: F821 - guest global
        # the broker types every config failure ConfigError; only absence is unpinned
        if str(e).startswith("ConfigError: no config value for"):
            raise unpinned(f"no {key} row: pin the caretaker's model in Settings → Model") from e
        raise snapshot_failed(f"{key}: {e}") from e
    if not isinstance(row, dict) or not row.get("model"):
        raise unpinned(f"{key} names no model: pin it in Settings → Model")
    return row


def _agent_name():
    # the serving agent's own name: the chat watcher skips it, so a notice
    # starts no turn; a runtime without the key is `bao` (ADR-033)
    try:
        name = effect("runtime.get", {"key": "agent.name"})["value"]  # noqa: F821
    except EffectError:  # noqa: F821 - guest global
        return "bao"
    return name if isinstance(name, str) and name else "bao"


def _live_datasets(hub, datasets, status):
    """The job's datasets, plus what the hub declares now: a later hub version adds
    stores (cards, standups) the job's args were written without."""
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


def _job_tier(job):
    return (job.get("args") or {}).get("tier") or MODEL_TIERS.get(job.get("program"))


def _tiers_in_use(triggers, datasets):
    tiers = {_job_tier(j) for j in _brain_jobs(triggers) if _enabled(j)}
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


def snapshot(space, hub, datasets, tick, status, records):
    """Everything the graders read, in one pass (ADR-038 §2)."""
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
    config = {name: (rec or {}).get("value") for name, rec in records.items()}
    cards = None
    if datasets.get("cards"):
        cards = _read(lambda: _any.query(space, hub, datasets["cards"],
                                         filter={"kind": "reply", "status": "open"}, limit=300))
    return {
        "now": tick, "space": space, "hub": hub, "datasets": datasets, "anchor": anchor,
        "triggers": triggers, "runs": runs,
        "brainSpace": _config("brain.space"), "brainStatus": status,
        "exclusions": records.get("exclusions"),
        "tiers": {t: _config(f"llm.tier.{t}") for t in _tiers_in_use(triggers, datasets)},
        "config": config,
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


def _away(snap):
    """True when this job's own previous run is past its overdue limit: the device
    was asleep or off, and every age reads stale for that reason alone."""
    mine = [t for t in _brain_jobs(snap["triggers"]) if t.get("program") == PROGRAM]
    last = max((_secs(t.get("lastRunAt")) or 0 for t in mine), default=0)
    return last > 0 and snap["now"] - last > 2 * EVERY_S + OVERDUE_GRACE_S


def _graded_jobs(snap):
    # the caretaker does not grade itself: it is running
    return [j for j in _brain_jobs(snap["triggers"]) if j.get("program") != PROGRAM]


def grade_job_overdue(snap):
    if _away(snap):
        return []
    out = []
    for job in _graded_jobs(snap):
        every = (job.get("spec") or {}).get("every_s")
        if not _enabled(job) or not _num(every) or every <= 0:
            continue
        last, status = _last_run(snap, job)
        since = last if last is not None else _status_at(snap)
        if since is None:
            continue
        overdue = snap["now"] - since - (2 * every + OVERDUE_GRACE_S)
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


def grade_mail_stale(snap):
    sync, credential = snap["syncState"], snap["credential"]
    state = (sync.get("sync_state") or {}) if isinstance(sync, dict) else {}
    if sync == UNAVAILABLE:
        modified, since = UNAVAILABLE, None
    elif sync is None:
        modified, since = None, _status_at(snap)    # no mail sync has checkpointed yet
    else:
        modified = _secs(sync.get("modifiedAt"))
        since = modified
    by_age = (since is not None and not _away(snap)
              and snap["now"] - since > MAIL_STALE_S)
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


def _stall_entry(snap, job, prev):
    """This tick's watch entry for a model-reading job, or None when its stats or
    cursor could not be read."""
    stats_name, cursor_name = STALL_WATCH[job["program"]]
    stats, cursor = snap["config"].get(stats_name), snap["config"].get(cursor_name)
    if stats == UNAVAILABLE or cursor == UNAVAILABLE:
        return None
    failed = stats.get("providerFailed") if isinstance(stats, dict) else 0
    failed = failed if _num(failed) else 0
    last, status = _last_run(snap, job)
    entry = {"runAt": last, "runStatus": status, "providerFailed": failed,
             "cursor": _digest(cursor), "streak": 0, "delta": 0, "moved": False}
    if not isinstance(prev, dict):
        return entry
    entry.update(streak=prev.get("streak") or 0, delta=prev.get("delta") or 0,
                 moved=bool(prev.get("moved")))
    before = prev.get("runAt")
    if last is not None and (not _num(before) or last > before):
        # a run since the last look: did it fail the provider without progress?
        delta = failed - (prev.get("providerFailed") or 0)
        moved = entry["cursor"] != prev.get("cursor")
        entry.update(delta=delta, moved=moved,
                     streak=(entry["streak"] + 1) if delta > 0 and not moved else 0)
    return entry


def grade_model_stalled(snap, watch):
    """(faults, the new watch): a model-reading job whose runs report success while
    every one fails the provider and its cursor stands still."""
    out, new = [], {}
    for job in _graded_jobs(snap):
        if job.get("program") not in STALL_WATCH or not _enabled(job):
            continue
        prev = watch.get(job["id"])
        entry = _stall_entry(snap, job, prev)
        if entry is None:
            if isinstance(prev, dict):
                new[job["id"]] = prev
            continue
        new[job["id"]] = entry
        if entry["streak"] >= STALL_RUNS:
            out.append(_fault("model_stalled", job["id"], {
                "streak": entry["streak"], "providerFailedDelta": entry["delta"],
                "providerFailedTotal": entry["providerFailed"],
                "cursorMoved": entry["moved"], "lastRunAt": entry["runAt"],
                "lastRunStatus": entry["runStatus"]}))
    return out, new


def _request_state(request, status):
    """brainSetup's reading of a request and its answer (ADR-033 §2)."""
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
        age = int(snap["now"] - since) if since is not None else None
    by_age = age is not None and age > SETUP_BLOCKED_S and not _away(snap)
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


def grade(snap, watch):
    """(faults by (faultClass, subject), the new stall watch)."""
    faults = [f for g in GRADERS for f in g(snap)]
    stalled, watch = grade_model_stalled(snap, watch)
    out = {}
    for f in faults + stalled:
        out.setdefault((f["faultClass"], f["subject"]), f)
    return out, watch


def _job_name(snap, tid):
    return (_trigger(snap, tid) or {}).get("name") or tid


def _hours(seconds):
    return f"{max(1, round(seconds / 3600))} h" if _num(seconds) else "a while"


def summary(fault_class, subject, evidence, snap):
    """One plain line saying what is wrong — the card's and the notice's fallback."""
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
    if fault_class == "model_stalled":
        return (f"{_job_name(snap, subject)} reads nothing new: its model calls fail "
                f"({e.get('streak')} runs)")
    if fault_class == "setup_blocked":
        if not e.get("setupTriggerPresent"):
            return ("the brain-setup job is gone; switching the brain on again "
                    "in chat makes a new one")
        return f"brain setup has not answered for {_hours(e.get('ageS'))}"
    if fault_class == "draft_backlog":
        return f"{e.get('count')} reply cards are waiting for a draft"
    return f"{fault_class}: {subject}"


# --- the reply and the repairs ---------------------------------------------

def allowed_actions(fault_class, subject, snap):
    """The repairs that apply to this incident's subject (ADR-038 §6); escalate is
    always allowed besides them. Empty: nothing may repair it."""
    if fault_class == "setup_blocked":
        return []
    out = []
    if fault_class in ("job_overdue", "job_failing"):
        job = _trigger(snap, subject)
        if job is not None and str(job.get("program") or "").startswith(BRAIN):
            out.append("run_job")
            if fault_class == "job_failing" and job.get("lastStatus") == STOPPED \
                    and not _enabled(job):
                out.append("enable_job")
            if fault_class == "job_failing" and job.get("program") in REWIND:
                out.append("rewind_cursor")
    elif fault_class == "mail_stale":
        tid = _mail_trigger_id(snap["brainStatus"])
        if isinstance(tid, str) and _trigger(snap, tid) is not None:
            out.append("run_job")
    out.append("notice")
    return out


def _refused(reason):
    return {"accepted": False, "reason": reason}


def validate_reply(text, subject, evidence, allowed):
    """The model's reply checked by plain code → {accepted, action, hypothesis,
    confidence, evidence} or {accepted: False, reason} (ADR-038 §5)."""
    reply = kit.parse_json(text)
    if not isinstance(reply, dict):
        return _refused("unparsable reply")
    confidence = reply.get("confidence")
    if not _num(confidence) or not 0 <= confidence <= 1:
        confidence = 0                      # a word, a string, a boolean: no confidence
    cited = reply.get("evidence")
    known = set(evidence) | {"history"}
    if not isinstance(cited, list) or not all(isinstance(k, str) and k in known for k in cited):
        return _refused("the evidence names a key the input does not have")
    action = reply.get("action")
    if action != "escalate" and action not in allowed:
        return _refused(f"action {str(action)[:40]!r} is not allowed here")
    if reply.get("target") != subject:
        return _refused("the target is not the incident's subject")
    if action != "escalate" and not cited:
        return _refused("a repair must cite its evidence")
    hypothesis = reply.get("hypothesis")
    hypothesis = hypothesis.strip()[:HYPOTHESIS_MAX] if isinstance(hypothesis, str) else None
    if action != "escalate" and confidence < CONFIDENCE_MIN:
        action = "escalate"                 # an unsure turn never repairs
    return {"accepted": True, "action": action, "hypothesis": hypothesis or None,
            "confidence": confidence, "evidence": cited}


def _back_ms(value):
    if not isinstance(value, dict) or not value or not all(_num(v) for v in value.values()):
        return None
    return {k: v - REWIND_S * 1000 for k, v in value.items()}


def _rewind_people(value):
    mail = _back_ms(value.get("mail")) if isinstance(value, dict) else None
    return None if mail is None else {**value, "mail": mail}


def _rewind_facts(value):
    mark = value.get("watermark") if isinstance(value, dict) else None
    return {**value, "watermark": mark - REWIND_S} if _num(mark) else None


# the cursors rewind_cursor may move (ADR-038 §6); a token cursor (the mail
# sync's historyId, a calendar sync token) is not here and is refused
REWIND = {PEOPLE: ("people.cursor", _rewind_people),
          PROMISES: ("promises.cursor", _back_ms),
          FACTS: ("facts.cursor", _rewind_facts)}


def step(incident, now_s, clean):
    """The ladder (ADR-038 §4, §7) as a pure transition → the fields to set, in
    seconds, or None. `verified: False` with `escalated` means a repair failed."""
    status = incident.get("status")
    due = (_secs(incident.get("verifyAt")) or 0) <= now_s
    if status in ("open", "escalated"):
        return {"status": "closed", "closedAt": now_s} if clean else None
    if status == "repairing":
        if not due:
            return None
        return ({"status": "soaking", "verifyAt": now_s + SOAK_S} if clean
                else {"status": "escalated", "verified": False})
    if status == "soaking":
        if not clean:
            return {"status": "escalated", "verified": False}
        return {"status": "closed", "closedAt": now_s, "verified": True} if due else None
    return None


# --- the run ---------------------------------------------------------------

_TIME_FIELDS = ("openedAt", "closedAt", "actionAt", "verifyAt", "modelTurnAt")


class _Run:
    def __init__(self, space, hub, datasets, tick, tier, state):
        self.space, self.hub, self.ds, self.tick, self.tier = space, hub, datasets, tick, tier
        self.state = dict(state)
        self.calls = self.failed = 0
        self.counts = {"faults": 0, "opened": 0, "observed": 0, "repaired": 0,
                       "verified": 0, "healed": 0, "escalated": 0, "refused": 0,
                       "modelCalls": 0}
        self.did, self.needs = [], []
        self.incidents = {}
        self.snap = None
        self.faults = {}
        self._model_name = None

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
        pending = list(self.state.get("pending") or [])
        if not any(p.get("incidentId") == iid and p.get("line") == line for p in pending):
            pending.append({"incidentId": iid, "line": line[:300]})
        self.state["pending"] = pending

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
                      "openedAt": self.tick, "status": "open"}
            new.append({"id": iid, "fields": {**fields, "openedAt": instant(self.tick)}})  # noqa: F821
            self.incidents[iid] = {"id": iid, **fields}
        if new:
            kit.checked(_any.upsert_records(self.space, self.hub, self.ds["incidents"], new),
                        "opening incidents")
            self.counts["opened"] = len(new)
            self.did.append(f"opened {len(new)} incident{'s' * (len(new) != 1)}")
        self._observe()
        for iid, inc in list(self.incidents.items()):
            clean = (inc.get("faultClass"), inc.get("subject")) not in faults
            patch = step(inc, self.tick, clean)
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
        by a row every tick."""
        records, seen = [], dict(self.state.get("observedAt") or {})
        for iid, inc in self.incidents.items():
            fault = self.faults.get((inc.get("faultClass"), inc.get("subject")))
            if fault and inc.get("status") == "escalated":
                last = seen.get(iid)
                if _num(last) and self.tick - last < ESCALATED_OBSERVE_S:
                    continue
                seen[iid] = self.tick
            records.append({"id": f"{iid}:{int(self.tick)}", "fields": {
                "incidentId": iid, "tick": int(self.tick),
                "evidence": fault["evidence"] if fault else {"clean": True}}})
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
        fault = self.faults.get((inc.get("faultClass"), inc.get("subject"))) or {}
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
        hypothesis = fields.get("hypothesis") or self.incidents[iid].get("hypothesis")
        reason = hypothesis or f"caretaker could not decide — {self._summary(iid)}"
        if why:
            reason = f"{reason} ({why})"
        cid = self._card(iid, reason)
        if cid:
            fields["cardId"] = cid
        self._patch(iid, fields)
        line = self._summary(iid) + (f" — {hypothesis}" if hypothesis else "")
        self._queue_notice(iid, line)
        self.counts["escalated"] += 1
        self.did.append(f"escalated {self._title(iid)}")
        self.needs.append(self._summary(iid))

    # the model turn
    def _model(self):
        if self._model_name is None:
            self._model_name = _read(lambda: llm.profile(self.tier)["model"])
        return self._model_name

    def _history(self, iid):
        rows = _read(lambda: _any.query(self.space, self.hub, self.ds["observations"],
                                        filter={"incidentId": iid}, sort=["-tick"],
                                        limit=HISTORY))
        if rows == UNAVAILABLE:
            return UNAVAILABLE
        return [{"tick": r.get("tick"), "evidence": r.get("evidence")} for r in rows]

    def turns(self):
        for iid in sorted(self.incidents):
            inc = self.incidents[iid]
            last = _secs(inc.get("modelTurnAt"))
            if inc.get("status") != "open" or (last is not None
                                               and self.tick - last < TURN_EVERY_S):
                continue
            allowed = allowed_actions(inc.get("faultClass"), inc.get("subject"), self.snap)
            if not allowed:
                self._escalate(iid)          # nothing may repair it: no model turn
                continue
            if self.calls >= MAX_CALLS:
                continue                     # waits for the next tick
            self._turn(iid, inc, allowed)

    def _ask(self, payload):
        """(the reply's text, None) or (None, the failure)."""
        self.calls += 1
        self.counts["modelCalls"] = self.calls
        try:
            reply = llm.chat([{"role": "user", "parts": [{"type": "text",
                                                          "text": json.dumps(payload)}]}],
                             system=CARETAKER_SYS, tier=self.tier, tools=[],
                             max_tokens=MAX_TOKENS)
        except Exception as e:  # LlmError, EffectError (a timeout, a missing secret), …
            self.failed += 1
            return None, f"{type(e).__name__}: {str(e)[:200]}"
        return " ".join(p.get("text", "") for p in reply.get("parts") or []
                        if p.get("type") == "text"), None

    def _turn(self, iid, inc, allowed):
        fault = self.faults.get((inc.get("faultClass"), inc.get("subject"))) or {}
        evidence = fault.get("evidence") or {}
        payload = {"faultClass": inc.get("faultClass"), "subject": inc.get("subject"),
                   "summary": self._summary(iid), "evidence": evidence,
                   "history": self._history(iid), "allowed": [*allowed, "escalate"]}
        text, failure = self._ask(payload)
        turn = {"modelTurnAt": self.tick, "model": str(self._model())}
        if failure is not None:
            self._escalate(iid, f"the caretaker's model failed: {failure}",
                           {**turn, "action": "escalate"})
            return
        verdict = validate_reply(text, inc.get("subject"), evidence, allowed)
        if not verdict["accepted"]:
            self._signal(iid, verdict["reason"])
            self._escalate(iid, f"its reply was refused: {verdict['reason']}",
                           {**turn, "action": "escalate"})
            return
        decided = {**turn, "action": verdict["action"], "confidence": verdict["confidence"],
                   "evidence": verdict["evidence"]}
        if verdict["hypothesis"]:
            decided["hypothesis"] = verdict["hypothesis"]
        if verdict["action"] == "escalate":
            self._escalate(iid, None, decided)
            return
        self._patch(iid, decided)
        refusal = self._execute(iid, verdict["action"])
        if refusal is not None:
            self._signal(iid, refusal)
            self._escalate(iid, refusal, {"action": "escalate"})
            return
        self._patch(iid, {"status": "repairing", "actionAt": self.tick,
                          "verifyAt": self.tick + VERIFY_S})
        self.counts["repaired"] += 1
        self.did.append(f"tried {verdict['action']} on {self._title(iid)} (unverified)")

    # repairs: the target is read from the incident and the snapshot, never the reply
    def _execute(self, iid, action):
        """Run one allowed repair → None, or why it was refused."""
        inc = self.incidents[iid]
        try:
            if action == "run_job":
                return self._run_job(inc)
            if action == "enable_job":
                return self._enable_job(inc)
            if action == "rewind_cursor":
                return self._rewind(inc)
            if action == "notice":
                self._queue_notice(iid, f"{self._summary(iid)} — "
                                        f"{inc.get('hypothesis') or 'please take a look'}")
                return None
        except Exception as e:
            return f"{action} failed: {type(e).__name__}: {str(e)[:200]}"
        return f"unknown action {action!r}"

    def _job_of(self, inc):
        if inc.get("faultClass") == "mail_stale":
            tid = _mail_trigger_id(self.snap["brainStatus"])
            return _trigger(self.snap, tid) if isinstance(tid, str) else None
        job = _trigger(self.snap, inc.get("subject"))
        if job is None or not str(job.get("program") or "").startswith(BRAIN):
            return None
        return job

    def _run_job(self, inc):
        job = self._job_of(inc)
        if job is None:
            return "run_job: no brain job to run"
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
        job = _trigger(self.snap, inc.get("subject"))
        if job is None or job.get("kind") != "cron" \
                or not str(job.get("program") or "").startswith(BRAIN):
            return "enable_job: only a brain job's own record"
        if _enabled(job) or job.get("lastStatus") != STOPPED:
            return "enable_job: the job was not stopped by the runtime"
        kit.checked(_any.modify(self.space, {
            "objectId": self.snap["anchor"], "dataset": TRIGGERS,
            "records": [{"id": job["id"], "ops": [
                {"type": "$set", "path": "enabled", "value": True}]}]}),
            f"enabling {job['id']}")
        return None

    def _rewind(self, inc):
        job = _trigger(self.snap, inc.get("subject"))
        rule = REWIND.get((job or {}).get("program"))
        if rule is None:
            return "rewind_cursor: this job's cursor is not one the caretaker may move"
        name, back = rule
        value = self.snap["config"].get(name)
        moved = back(value) if value not in (None, UNAVAILABLE) else None
        if moved is None:
            return f"rewind_cursor: config/{name} is missing or unreadable"
        kit.write_config(self.space, self.hub, self.ds, name, moved)
        return None

    # notices, the report, the standup
    def flush_notices(self):
        # a line whose incident closed meanwhile is news no longer
        pending = [p for p in self.state.get("pending") or []
                   if (self.incidents.get(p.get("incidentId")) or {}).get("status")
                   not in (None, "closed")]
        self.state["pending"] = pending
        if not pending:
            return 0
        cooldown = self.state.get("noticeCooldownMin")
        cooldown = (cooldown if _num(cooldown) else NOTICE_COOLDOWN_MIN) * 60
        last = self.state.get("lastNoticeAt")
        if _num(last) and self.tick - last < cooldown:
            return 0
        lines = [f"• {p.get('line')}" for p in pending[:NOTICE_LINES]]
        if len(pending) > NOTICE_LINES:
            lines.append(f"…and {len(pending) - NOTICE_LINES} more")
        text = "The brain needs a look:\n" + "\n".join(lines)
        try:
            chat = _any.general_chat(self.space)
            _any.chat_send(self.space, chat, {"text": text,
                                              "agent": {"name": _agent_name(), "done": True}})
        except Exception as e:
            self.counts["noticeError"] = f"{type(e).__name__}: {str(e)[:200]}"
            return 0                         # kept for the next tick
        self.state["pending"], self.state["lastNoticeAt"] = [], self.tick
        return len(pending)

    def report(self):
        day = fmt_ts(self.tick, "%Y-%m-%d", 0)[:10]  # noqa: F821 - guest global
        if self.state.get("lastReportDay") == day:
            return
        self.state["lastReportDay"] = day
        if not (self.ds.get("standups") or self.ds.get("cards")):
            return
        closed = _read(lambda: _any.query(
            self.space, self.hub, self.ds["incidents"],
            filter={"status": "closed", "closedAt": {"$gt": instant(self.tick - DAY)}},  # noqa: F821
            limit=500))
        rows = list(self.incidents.values()) + (closed if isinstance(closed, list) else [])
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
    tier = (args or {}).get("tier") or TIER
    _pinned(tier)   # the first statement: no read, no write before the pin (ADR-038 §1)
    space, hub, datasets, status = _args(args)
    tick = now()  # noqa: F821 - guest global
    records = _hub_config(space, hub, datasets)
    state = (records.get("caretaker") or {}).get("value")
    run = _Run(space, hub, datasets, tick, tier, state if isinstance(state, dict) else {})
    snap = snapshot(space, hub, datasets, tick, status, records)
    faults, run.state["watch"] = grade(snap, run.state.get("watch") or {})
    try:
        live = _any.query(space, hub, datasets["incidents"],
                          filter={"status": {"$in": list(OPEN_STATES)}}, limit=500)
    except Exception as e:
        raise snapshot_failed(f"the open incidents could not be read: {e}") from e
    run.reconcile(snap, faults, live)
    run.turns()
    run.report()
    notices = run.flush_notices()
    run.save_state()
    run.standup()
    if run.calls and run.failed == run.calls:
        raise provider_failed(f"all {run.calls} model calls of the run failed")
    return {**run.counts, "notices": notices,
            "open": sorted(f"{c}:{s}" for c, s in faults)}
