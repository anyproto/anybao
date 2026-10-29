"""Turn what the user owes into tasks, each with a suggested deadline.

`suggest()` lists open promises and asks not yet made into tasks, with
the deadline the rule table proposes; `create(promise_id)` and
`create_from_ask(ask_id)` make one into a catalog task in Bao's Tasks
app; `create_all()` does every suggestion. Create only after the user
said yes; show the deadline and let them change it.
"""

__any_category__ = "plugin"  # the AI brain is a plugin of the connectors repo (ADR-033 §1.1)

# ADR-033 §2.5. Deadlines come from one deterministic rule table, never a model
# (design record §4.15), in this order:
#   1. the promise's own dueAt (brainPromises parsed it from the sentence)
#   2. an ISO date in the sentence (2026-11-03)
#   3. today / asap / heute / sofort → today
#   4. tomorrow / morgen → the day after it was made
#   5. this week / end of the week / diese Woche / Ende der Woche → Friday
#   6. next week / nächste Woche → next week's Friday
#   7. a weekday name → its next occurrence after the day it was made
#   8. otherwise two business days after it was made — three for an ask,
#      none for a checklist item (ADR-033 §7.6)
# and never earlier than today. Days are UTC. A task is a catalog `task`
# (planning: inbox) also filed under the brain's `brain_task` collection,
# which keeps the source link, the person, the origin and the suggestion
# even after the user moves the deadline; the promise gets the task's id.
# A promise made to someone the exclusion list or the blocklist keeps out,
# or read from a message that now carries such a person (an exclusion added
# later), is never offered and refuses create; both lists are read
# fail-closed. Asks — what other people asked the user for in the chats
# the user opted in (brainChatScan@v1) — are offered and made the same way;
# an ask from an excluded person, from an identity the user excluded, or
# from one that may be an excluded person (brainKit.IdentityResolver), is
# never offered and refuses create_from_ask.

__any_tool__ = True  # agent-callable (ADR-010 §4)

import re

kit = use("brainKit@v1")  # noqa: F821 - guest global
_any = use("agent:any@v1")  # noqa: F821 - guest global

DAY = 86400
_ISO = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
             "saturday": 5, "sunday": 6, "montag": 0, "dienstag": 1, "mittwoch": 2,
             "donnerstag": 3, "freitag": 4, "samstag": 5, "sonntag": 6}
_MAX_LIST = 300
# business days a deadline defaults to without a date in the sentence, by
# origin; None: no suggestion (ADR-033 §7.6)
DEFAULT_DAYS = {"promise": 2, "ask": 3, "checklist": None}


class unknown_promise(Exception):
    """No promise with that id."""


class person_excluded(Exception):
    """The promise was made to, or the ask came from, someone the user keeps
    out of the brain."""


class unknown_ask(Exception):
    """No ask with that id."""


def _day(seconds):
    return int(seconds // DAY) * DAY


def _weekday(day):
    return int((day // DAY + 3) % 7)          # 1970-01-01 was a Thursday


def _iso_date(day):
    return fmt_ts(day, "%Y-%m-%d", 0)[:10]  # noqa: F821 - guest global


def _business_days_after(day, n):
    while n > 0:
        day += DAY
        if _weekday(day) < 5:
            n -= 1
    return day


def _suggested_deadline(text, made_at, due_at=None, today=None, default_days=2):
    """The rule table's deadline (UTC day start, seconds) for a promise."""
    made = _day(made_at)
    floor = _day(today if today is not None else made_at)
    low = (text or "").lower()
    wd = _weekday(made)
    if due_at:
        out = _day(due_at)
    elif _ISO.search(low):
        y, m, d = (int(g) for g in _ISO.search(low).groups())
        out = kit.civil_day(y, m, d)
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
            out = _business_days_after(made, default_days)
    return max(out, floor)


@span("brainTasks.deadline_for", kind="getter")  # noqa: F821 - guest global
def deadline_for(origin, text, at, due_at=None, today=None):
    """The rule table's deadline for one item → a UTC day start (seconds), or None.

    `origin` ∈ promise | ask | checklist; `at` is when it was made or
    asked (seconds). Without a date in the sentence a promise gets two
    business days, an ask three, a checklist item none; never earlier
    than `today` (default: the day of `at`)."""
    days = DEFAULT_DAYS.get(origin, DEFAULT_DAYS["promise"])
    if days is None:
        return None
    return _suggested_deadline(text, at, due_at, today, days)


def _notes(r, what="mail"):
    # the task's notes are its Markdown body: the sentence, then a link to its source
    link = f"[Open the {what}]({r['source']})" if r.get("source") else ""
    return f"{r.get('quote') or ''}\n\n{link}".strip()


def _hub(bao):
    """(hub, datasets, the rules of who is kept out) of a set-up brain."""
    hub, datasets = kit.ready_hub()
    if not datasets.get("promises"):
        raise kit.not_set_up("The brain is not set up yet; switch it on first.")
    rules = (kit.exclusions(bao, hub, datasets), kit.blocklist(bao, hub, datasets))
    return hub, datasets, rules


def _open_promises(bao, hub, promises, rules):
    rows = [r for r in _any.query(bao, hub, promises, filter={"status": "open"},
                                  sort=["-madeAt"], limit=_MAX_LIST) if not r.get("taskId")]
    kept_out = kit.promises_kept_out(bao, rows, *rules)
    return [r for r in rows if kit.plain_id(r["id"]) not in kept_out]


def _row(r, today):
    made = ts_s(r.get("madeAt")) or today  # noqa: F821 - guest global
    due = ts_s(r.get("dueAt"))  # noqa: F821 - guest global
    return {"id": kit.plain_id(r["id"]), "origin": "promise",
            "text": r.get("text"), "quote": r.get("quote"),
            "personEmail": r.get("personEmail"), "personId": r.get("personId"),
            "source": r.get("source"),
            "suggestedDeadline": _iso_date(_suggested_deadline(r.get("text"), made, due, today))}


def _ask_origin(r):
    return "checklist" if r.get("kind") == "checklist" else "ask"


def _ask_kept_out(r, who):
    """True when an ask came from someone kept out (ADR-033 §7.5): its person,
    its sender identity itself, or — unlinked — any person that identity
    may be. `who` is the run's brainKit.IdentityResolver."""
    excl = who.excl
    if r.get("personId") in excl["personIds"]:
        return True
    identity = kit.identity_of(r.get("senderIdentity"))
    if identity in excl["identities"]:
        return True
    if r.get("personId"):
        return False                        # linked to a person who is not kept out
    return who.kept_out(identity)


def _open_asks(bao, hub, datasets, rules):
    if not datasets.get("asks"):
        return []
    rows = [r for r in _any.query(bao, hub, datasets["asks"], filter={"status": "open"},
                                  sort=["-askedAt"], limit=_MAX_LIST) if not r.get("taskId")]
    if not rows:
        return []
    who = kit.IdentityResolver(bao, rules[0])
    return [r for r in rows if not _ask_kept_out(r, who)]


def _ask_row(r, today):
    origin = _ask_origin(r)
    asked = ts_s(r.get("askedAt")) or today  # noqa: F821 - guest global
    due = deadline_for(origin, r.get("text"), asked, today=today)
    return {"id": kit.plain_id(r["id"]), "origin": origin, "kind": r.get("kind"),
            "text": r.get("text"), "quote": r.get("quote"),
            "personId": r.get("personId"), "senderIdentity": r.get("senderIdentity"),
            "source": r.get("source"),
            "suggestedDeadline": _iso_date(due) if due is not None else None}


def _suggestions(bao, hub, datasets, rules, today):
    """Open promises and asks without a task, newest first."""
    promises = [(ts_s(r.get("madeAt")) or 0, _row(r, today))  # noqa: F821 - guest global
                for r in _open_promises(bao, hub, datasets["promises"], rules)]
    asks = [(ts_s(r.get("askedAt")) or 0, _ask_row(r, today))  # noqa: F821 - guest global
            for r in _open_asks(bao, hub, datasets, rules)]
    return [row for _, row in sorted(promises + asks, key=lambda p: p[0], reverse=True)]


@span("brainTasks.suggest", kind="getter")  # noqa: F821 - guest global
def suggest(limit=20):
    """Open promises and asks not yet made into tasks, newest first, with deadlines.

    → [{id, origin, text, quote, personId, source, suggestedDeadline, …}];
    `origin` ∈ promise | ask | checklist — a promise row adds
    `personEmail`, an ask row `kind` and `senderIdentity` (any://u/…).
    `suggestedDeadline` is an ISO date from the rule table in this
    program's notes (None for a checklist item). `limit` None lists them
    all. Offer them to the user; create only after a yes: `create(id)` for
    a promise, `create_from_ask(id)` for an ask."""
    bao = _any.bao_space()
    hub, datasets, rules = _hub(bao)
    rows = _suggestions(bao, hub, datasets, rules, now())  # noqa: F821 - guest global
    return rows if limit is None else rows[:max(0, int(limit))]


@span("brainTasks.create", kind="mutator")  # noqa: F821 - guest global
def create(promise_id, deadline=None):
    """Make one promise into a task → {taskId, created, deadline}.

    `deadline`: an ISO date the user chose; omitted = the suggestion.
    A promise already made into a task returns its task, unchanged."""
    bao = _any.bao_space()
    hub, datasets, rules = _hub(bao)
    promises = datasets["promises"]
    rid = kit.plain_id(promise_id)
    rows = _any.query(bao, hub, promises, filter={"id": f"{hub}/{rid}"}, limit=1)
    if not rows:
        raise unknown_promise(f"no promise {rid!r}")
    r = rows[0]
    if kit.promises_kept_out(bao, [r], *rules):
        raise person_excluded("This promise was made to someone kept out of the brain.")
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
                     "notes": _notes(r, "chat" if r.get("channel") == "chat" else "mail")},
            "brain_task": brain_task}})
    task_id = made["objectId"]
    kit.checked(_any.modify(bao, {"objectId": hub, "dataset": promises, "records": [
        {"id": rid, "ops": [{"type": "$set", "path": "taskId", "value": task_id}]}]}),
        "linking the promise to its task")
    return {"taskId": task_id, "created": True, "deadline": chosen}


@span("brainTasks.create_from_ask", kind="mutator")  # noqa: F821 - guest global
def create_from_ask(ask_id, deadline=None):
    """Make one ask into a task → {taskId, created, deadline}.

    `deadline`: an ISO date the user chose; omitted = the suggestion (a
    checklist item has none). An ask already made into a task returns its
    task, unchanged. Raises person_excluded for an ask from someone kept
    out of the brain."""
    bao = _any.bao_space()
    hub, datasets, rules = _hub(bao)
    if not datasets.get("asks"):
        raise unknown_ask(f"no ask {ask_id!r}: the brain keeps no asks yet")
    rid = kit.plain_id(ask_id)
    rows = _any.query(bao, hub, datasets["asks"], filter={"id": f"{hub}/{rid}"}, limit=1)
    if not rows:
        raise unknown_ask(f"no ask {rid!r}")
    r = rows[0]
    if _ask_kept_out(r, kit.IdentityResolver(bao, rules[0])):
        raise person_excluded("This ask came from someone kept out of the brain.")
    if r.get("taskId"):
        return {"taskId": r["taskId"], "created": False, "deadline": None}
    suggested = _ask_row(r, now())["suggestedDeadline"]  # noqa: F821 - guest global
    chosen = deadline or suggested
    origin = _ask_origin(r)
    brain_task = {"sourceUri": r.get("source"), "origin": origin}
    if suggested:
        brain_task["suggestedDeadline"] = suggested
    if r.get("personId"):
        brain_task["personId"] = [r["personId"]]
    task = {"planning": "inbox",
            "notes": _notes(r, "page" if origin == "checklist" else "chat")}
    if chosen:
        task["deadline"] = chosen
    made = _any.create_object(bao, {
        "type": "task", "collections": ["brain_task"], "name": (r.get("text") or "")[:200],
        "initialProperties": {"task": task, "brain_task": brain_task}})
    task_id = made["objectId"]
    kit.checked(_any.modify(bao, {"objectId": hub, "dataset": datasets["asks"], "records": [
        {"id": rid, "ops": [{"type": "$set", "path": "taskId", "value": task_id}]}]}),
        "linking the ask to its task")
    return {"taskId": task_id, "created": True, "deadline": chosen}


@span("brainTasks.create_all", kind="mutator")  # noqa: F821 - guest global
def create_all(limit=20):
    """Make every suggestion into a task → [{id, origin, taskId, deadline}].

    Only after the user said yes to the list `suggest()` showed: promises
    and asks alike."""
    out = []
    for s in suggest(limit=limit):
        made = create(s["id"]) if s["origin"] == "promise" else create_from_ask(s["id"])
        out.append({"id": s["id"], "origin": s["origin"], "taskId": made["taskId"],
                    "deadline": made["deadline"]})
    return out
