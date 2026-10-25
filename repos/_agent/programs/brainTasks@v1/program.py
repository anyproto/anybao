"""Turn the user's open promises into tasks, each with a suggested deadline.

`suggest()` lists open promises not yet made into tasks, with the
deadline the rule table proposes; `create(promise_id)` makes one into a
catalog task in Bao's Tasks app; `create_all()` does every suggestion.
Create only after the user said yes; show the deadline and let them change it.
"""

# ADR-034. Deadlines come from one deterministic rule table, never a model
# (design record §4.15), in this order:
#   1. the promise's own dueAt (brainPromises parsed it from the sentence)
#   2. an ISO date in the sentence (2026-11-03)
#   3. today / asap / heute / sofort → today
#   4. tomorrow / morgen → the day after it was made
#   5. this week / end of the week / diese Woche / Ende der Woche → Friday
#   6. next week / nächste Woche → next week's Friday
#   7. a weekday name → its next occurrence after the day it was made
#   8. otherwise two business days after it was made
# and never earlier than today. Days are UTC. A task is a catalog `task`
# (planning: inbox) also filed under the brain's `brain_task` collection,
# which keeps the source link, the person, the origin and the suggestion
# even after the user moves the deadline; the promise gets the task's id.

__any_tool__ = True  # agent-callable (ADR-010 §4)

import re

kit = use("brainKit@v1")  # noqa: F821 - guest global
_any = use("any@v1")  # noqa: F821 - guest global

DAY = 86400
_ISO = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
             "saturday": 5, "sunday": 6, "montag": 0, "dienstag": 1, "mittwoch": 2,
             "donnerstag": 3, "freitag": 4, "samstag": 5, "sonntag": 6}
_MAX_LIST = 300


class not_set_up(Exception):
    """The brain has no hub yet: it is off or Bao has not set it up."""


class unknown_promise(Exception):
    """No promise with that id."""


def _day(seconds):
    return int(seconds // DAY) * DAY


def _weekday(day):
    return int((day // DAY + 3) % 7)          # 1970-01-01 was a Thursday


def _iso_date(day):
    return fmt_ts(day, "%Y-%m-%d", 0)[:10]  # noqa: F821 - guest global


def _days_from_civil(y, m, d):
    # days since 1970-01-01 for a proleptic Gregorian date (no datetime import)
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _business_days_after(day, n):
    while n > 0:
        day += DAY
        if _weekday(day) < 5:
            n -= 1
    return day


def suggested_deadline(text, made_at, due_at=None, today=None):
    """The rule table's deadline (UTC day start, seconds) for a promise."""
    made = _day(made_at)
    floor = _day(today if today is not None else made_at)
    low = (text or "").lower()
    wd = _weekday(made)
    if due_at:
        out = _day(due_at)
    elif _ISO.search(low):
        y, m, d = (int(g) for g in _ISO.search(low).groups())
        out = _days_from_civil(y, m, d) * DAY
    elif re.search(r"\b(today|asap|heute|sofort)\b", low):
        out = floor
    elif re.search(r"\btomorrow\b|(?<!guten )\bmorgen\b", low):
        out = made + DAY
    elif re.search(r"\b(this week|end of (the )?week|diese woche|ende der woche)\b", low):
        out = made + max(0, 4 - wd) * DAY
    elif re.search(r"\b(next week|nächste woche|naechste woche)\b", low):
        out = made + (7 - wd + 4) * DAY
    else:
        out = None
        for name, target in _WEEKDAYS.items():
            if re.search(rf"\b{name}\b", low):
                out = made + (((target - wd) % 7) or 7) * DAY
                break
        if out is None:
            out = _business_days_after(made, 2)
    return max(out, floor)


def _notes(r):
    # the task's notes are its Markdown body: the sentence, then a link to the mail
    link = f"[Open the mail]({r['source']})" if r.get("source") else ""
    return f"{r.get('quote') or ''}\n\n{link}".strip()


def _hub():
    try:
        status = effect("config.get", {"key": "brain.status"})["value"]  # noqa: F821
    except EffectError:  # noqa: F821 - guest global
        status = None
    hub = (status or {}).get("hub") if isinstance(status, dict) else None
    if (not isinstance(hub, dict) or (status or {}).get("state") != "ready"
            or not (hub.get("datasets") or {}).get("promises")):
        raise not_set_up("The brain is not set up yet; switch it on first.")
    return hub["objectId"], hub["datasets"]["promises"]


def _open_promises(bao, hub, promises):
    rows = _any.query(bao, hub, promises, filter={"status": "open"},
                      sort=["-madeAt"], limit=_MAX_LIST)
    return [r for r in rows if not r.get("taskId")]


def _row(r, today):
    made = ts_s(r.get("madeAt")) or today  # noqa: F821 - guest global
    due = ts_s(r.get("dueAt"))  # noqa: F821 - guest global
    return {"id": kit.plain_id(r["id"]), "text": r.get("text"), "quote": r.get("quote"),
            "personEmail": r.get("personEmail"), "personId": r.get("personId"),
            "source": r.get("source"),
            "suggestedDeadline": _iso_date(suggested_deadline(r.get("text"), made, due, today))}


@span("brainTasks.suggest", kind="getter")  # noqa: F821 - guest global
def suggest(limit=20):
    """Open promises not yet made into tasks, newest first, with suggested deadlines.

    → [{id, text, quote, personEmail, personId, source, suggestedDeadline}];
    `suggestedDeadline` is an ISO date from the rule table in this
    program's notes. Offer them to the user; create only after a yes."""
    bao = _any.bao_space()
    hub, promises = _hub()
    today = now()  # noqa: F821 - guest global
    return [_row(r, today) for r in _open_promises(bao, hub, promises)[:max(0, int(limit))]]


@span("brainTasks.create", kind="mutator")  # noqa: F821 - guest global
def create(promise_id, deadline=None):
    """Make one promise into a task → {taskId, created, deadline}.

    `deadline`: an ISO date the user chose; omitted = the suggestion.
    A promise already made into a task returns its task, unchanged."""
    bao = _any.bao_space()
    hub, promises = _hub()
    rid = kit.plain_id(promise_id)
    rows = _any.query(bao, hub, promises, filter={"id": f"{hub}/{rid}"}, limit=1)
    if not rows:
        raise unknown_promise(f"no promise {rid!r}")
    r = rows[0]
    if r.get("taskId"):
        return {"taskId": r["taskId"], "created": False, "deadline": None}
    today = now()  # noqa: F821 - guest global
    suggested = _row(r, today)["suggestedDeadline"]
    chosen = deadline or suggested
    brain_task = {"sourceUri": r.get("source"), "origin": "promise",
                  "suggestedDeadline": suggested}
    if r.get("personId"):
        brain_task["personId"] = [r["personId"]]
    made = _any.create_object(bao, {
        "type": "task", "collections": ["brain_task"], "name": (r.get("text") or "")[:200],
        "initialProperties": {
            "task": {"deadline": chosen, "planning": "inbox",
                     "notes": _notes(r)},
            "brain_task": brain_task}})
    task_id = made["objectId"]
    kit.checked(_any.modify(bao, {"objectId": hub, "dataset": promises, "records": [
        {"id": rid, "ops": [{"type": "$set", "path": "taskId", "value": task_id}]}]}),
        "linking the promise to its task")
    return {"taskId": task_id, "created": True, "deadline": chosen}


@span("brainTasks.create_all", kind="mutator")  # noqa: F821 - guest global
def create_all(limit=20):
    """Make every suggestion into a task → [{id, taskId, deadline}].

    Only after the user said yes to the list `suggest()` showed."""
    out = []
    for s in suggest(limit=limit):
        made = create(s["id"])
        out.append({"id": s["id"], "taskId": made["taskId"], "deadline": made["deadline"]})
    return out
