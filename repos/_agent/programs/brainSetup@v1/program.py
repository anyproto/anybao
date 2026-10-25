"""Set up, switch off and repair the AI brain in Bao's own space.

The brain is on by default. `status()` says how it stands; `on()` and
`off()` switch it after the user's yes; `exclude()` keeps a person's
mail out of it for good. A trigger run of this program does the setup.
"""

# ADR-033. The contract with any-ui's Settings → Brain is two
# agent_config rows and one trigger record:
#   brain.space   the request: {spaceId: <Bao's space> | None, requestedAt, by}
#                 — absent means ON (the default), None means off
#   brain.status  this program's answer, echoing the request's requestedAt
#   brain-setup   a run-once agent_triggers record whoever writes a
#                 request re-arms; its run is `main` below. A whole-value
#                 $set MERGES into the record, so the fired once's
#                 lastRunAt survives a plain rewrite and the runtime reads
#                 it as consumed: a re-arm writes lastRunAt: None.
# Nothing here is ever deleted: a deleted record id can never be written
# again. The brain's jobs are ordinary agent_triggers records found by
# PROGRAM, not by id, so a deleted job is rewritten under a fresh id. A
# pause holds: the reconcile run only writes MISSING jobs and never
# re-enables one; an off run records which jobs it disabled (brain.jobs)
# and the next on run re-enables exactly those. A job the runtime stopped
# after repeated failures (lastStatus auto_disabled) is not a pause: the
# brain stays ready and brain.status lists it under `stopped`, and only a
# NEW request (any-ui's Try again) restarts it; a setup run re-armed for
# the same request (the chat-scan list) leaves it.

__any_tool__ = True  # agent-callable (ADR-010 §4)

_any = use("any@v1")  # noqa: F821 - guest global

SPACE_KEY = "brain.space"
STATUS_KEY = "brain.status"
JOBS_KEY = "brain.jobs"
STOPPED = "auto_disabled"
CHAT_SPACES_KEY = "brain.chatScan.spaces"
SETUP_TRIGGER_ID = "brain-setup"
PROGRAM = "agent:brainSetup@v1"
HEARTBEAT = "agent:brainHeartbeat@v1"
PEOPLE = "agent:brainPeople@v1"
PROMISES = "agent:brainPromises@v1"
GMAIL_SYNC = "connectors:gmailSync@v1"
MAIL_SYNC_ID = "brain-mail-sync"
# the hub's shape; a reconcile run that finds an older hub re-arms setup,
# which installs what the newer shape adds (1: Phase 0, 2: + people,
# promises and tasks)
HUB_VERSION = 2
BUNDLE = "brain/v1"
HUB_SEED = "brain/hub/v1"
HUB_TYPE = "brain_hub"
TRIGGERS = "agent_triggers"
CATALOG_APPS = ("people", "meetings", "tasks")
# the brain's model tiers, seeded from these sources only when absent so
# an edit in Settings → Model always wins
TIERS = (("llm.tier.brain.drafter", "llm.tier.codegen"),
         ("llm.tier.brain.extractor", "llm.tier.codegen"),
         ("llm.tier.brain.caretaker", "llm.tier.classify"))
EXCLUSIONS_SEED = {"version": 1, "personIds": [], "emails": [], "domains": []}
# senders, domains and labels whose mail the people and promises jobs skip
BLOCKLIST_SEED = {"domains": [], "senders": [], "labels": []}
NOT_READY_TRIES = 5
NOT_READY_SLEEP_S = 2

_CREATOR = {"key": "creator", "stamp": "creator"}
_CREATED = {"key": "createdAt", "stamp": "createTime"}
_MODIFIED = {"key": "modifiedAt", "stamp": "modifyTime"}


def _f(key, kind, required=False, mutable=False):
    field = {"key": key, "kind": kind}
    if required:
        field["required"] = True
    if mutable:
        field["mutableBy"] = "author"
    return field


# The hub's stores. Later phases ADD datasets; each one's
# full field set lands the first time it is declared, because required
# fields can only be declared then.
DATASETS = (
    {"key": "signals", "displayName": "Brain signals", "shared": True,
     "idRule": "user", "deleteBy": "author",
     "search": {"title": "subject", "text": "reason", "scope": "signals"},
     "fields": [_f("tick", "number", required=True),
                _f("kind", "string", required=True),
                _f("subject", "string", required=True),
                _f("decision", "string", required=True),
                _f("reason", "string", required=True),
                _f("cardId", "string"),
                _CREATOR, _CREATED]},
    {"key": "config", "displayName": "Brain config", "shared": True,
     "idRule": "user", "deleteBy": "author",
     "fields": [_f("value", "object", mutable=True),
                {"key": "updatedAt", "stamp": "modifyTime"},
                _CREATOR]},
    {"key": "incidents", "displayName": "Brain incidents", "shared": True,
     "idRule": "user", "deleteBy": "author",
     "fields": [_f("faultClass", "string", required=True),
                _f("subject", "string", required=True),
                _f("openedAt", "datetime", required=True),
                _f("status", "string", mutable=True),
                _f("closedAt", "datetime", mutable=True),
                _f("hypothesis", "string", mutable=True),
                _f("confidence", "number", mutable=True),
                _f("evidence", "array", mutable=True),
                _f("action", "string", mutable=True),
                _f("actionAt", "datetime", mutable=True),
                _f("verifyAt", "datetime", mutable=True),
                _f("verified", "boolean", mutable=True),
                _f("modelTurnAt", "datetime", mutable=True),
                _f("model", "string", mutable=True),
                _f("cardId", "string", mutable=True),
                _CREATOR, _CREATED, _MODIFIED]},
    {"key": "observations", "displayName": "Brain observations", "shared": True,
     "idRule": "user", "deleteBy": "author",
     "fields": [_f("incidentId", "string", required=True),
                _f("tick", "number", required=True),
                _f("evidence", "object", required=True),
                _CREATOR, _CREATED]},
    # what the user committed to in sent mail (later also chat); the id is
    # sha256(messageId, normalized sentence), so a re-run writes nothing
    {"key": "promises", "displayName": "Brain promises", "shared": True,
     "idRule": "user", "deleteBy": "author",
     "search": {"text": ["text", "quote"], "scope": "promises"},
     "fields": [_f("personId", "string", mutable=True),
                _f("personEmail", "string"),
                _f("text", "string", required=True),
                _f("quote", "string", required=True),
                _f("source", "string", required=True),
                _f("mailboxId", "string", required=True),
                _f("channel", "string", required=True),
                _f("madeAt", "datetime", required=True),
                _f("dueAt", "datetime"),
                _f("status", "string", mutable=True),
                _f("reason", "string", mutable=True),
                _f("closedAt", "datetime", mutable=True),
                _f("taskId", "string", mutable=True),
                _CREATOR, _CREATED, _MODIFIED]},
)

# The brain's columns on catalog objects: a person (`profile` filed under
# `person`) also filed under `brain_person`, a catalog `task` also filed
# under `brain_task`. Collections are idempotent by xKey and never touch
# the catalog's own definitions.
COLLECTIONS = (
    {"name": "Brain person", "xKey": "brain_person", "hidden": True, "properties": [
        {"name": "Emails", "xKey": "emails", "kind": "array"},
        {"name": "Preferred name", "xKey": "preferredName", "xFormat": {"type": "text"}},
        {"name": "Address form", "xKey": "addressForm",
         "xFormat": {"type": "choice", "options": ["du", "sie", "unknown"]}},
        {"name": "Language", "xKey": "language", "xFormat": {"type": "text"}},
        {"name": "Last contact", "xKey": "lastContactAt", "xFormat": {"type": "datetime"}},
        {"name": "First seen", "xKey": "firstSeenAt", "xFormat": {"type": "datetime"}},
        {"name": "Mails", "xKey": "mailCount", "xFormat": {"type": "number"}},
        {"name": "Meetings", "xKey": "meetingCount", "xFormat": {"type": "number"}},
        {"name": "Names seen", "xKey": "aliases", "kind": "array"}]},
    {"name": "Brain task", "xKey": "brain_task", "hidden": True, "properties": [
        {"name": "Source", "xKey": "sourceUri", "xFormat": {"type": "url"}},
        {"name": "Person", "xKey": "personId",
         "xFormat": {"type": "relation", "relation": {"targetTypes": ["profile"]}}},
        {"name": "Origin", "xKey": "origin",
         "xFormat": {"type": "choice", "options": ["promise", "ask", "checklist"]}},
        {"name": "Suggested deadline", "xKey": "suggestedDeadline",
         "xFormat": {"type": "date"}}]},
)


class provision_failed(Exception):
    """Setting the brain up hit an error; brain.status carries it."""


class config_unreadable(Exception):
    """A brain config row could not be read for a reason other than absence."""


class not_set_up(Exception):
    """The brain has no hub yet: it is off or Bao has not set it up."""


class write_rejected(Exception):
    """The server refused a record write; the reply's reason is the message."""


# --- small helpers ---------------------------------------------------------

def _iso(seconds):
    # fmt_ts renders an instant; offset 0 + the fixed format is UTC ISO
    return fmt_ts(seconds, "%Y-%m-%dT%H:%M:%S", 0)[:19] + "Z"  # noqa: F821 - guest global


def _config_get(key):
    """The row's value, or None when the key has no row at all."""
    try:
        return effect("config.get", {"key": key})["value"]  # noqa: F821 - guest global
    except EffectError as e:  # noqa: F821 - guest global
        if "no config value" in str(e):
            return None
        raise config_unreadable(f"{key}: {e}") from e


def _config_set(key, value):
    effect("config.set", {"key": key, "value": value})  # noqa: F821 - guest global


def _request():
    """brain.space as a dict, or None when absent or not in the contract's shape."""
    raw = _config_get(SPACE_KEY)
    if not isinstance(raw, dict) or "spaceId" not in raw:
        return None
    if not isinstance(raw.get("requestedAt"), str) or not raw["requestedAt"]:
        return None
    return raw


def _status():
    raw = _config_get(STATUS_KEY)
    return raw if isinstance(raw, dict) else None


def _write_status(request, state, **extra):
    value = {"spaceId": request.get("spaceId"), "requestedAt": request["requestedAt"],
             "state": state, "at": _iso(now())}  # noqa: F821 - guest global
    value.update(extra)
    _config_set(STATUS_KEY, value)
    return value


def _checked(reply, what):
    """Raise on a per-record rejection: modify and upsert answer HTTP 200 even
    when they refuse a record, with the refusal only in `rejections`."""
    rejected = (reply or {}).get("rejections") or []
    if rejected:
        reason = rejected[0].get("reason") or rejected[0].get("code") or "rejected"
        raise write_rejected(f"{what}: {reason}")
    return reply


def _upsert(bao, oid, dataset, rid, value, what):
    return _checked(_any.upsert_record(bao, oid, dataset, rid, value), what)


def _not_ready(e):
    return getattr(e, "status", 0) == 409 and getattr(e, "code", "") == "bundle.not_ready"


def _retry(fn):
    """fn(), retrying a 409 bundle.not_ready (the tree still arriving)."""
    for attempt in range(NOT_READY_TRIES):
        try:
            return fn()
        except Exception as e:
            if not _not_ready(e) or attempt == NOT_READY_TRIES - 1:
                raise
            effect("sleep", {"seconds": NOT_READY_SLEEP_S})  # noqa: F821 - guest global
    return None


def _trigger_anchor(bao):
    # the `bao/triggers/v1` bundle child the runtime polls (ADR-017 §0)
    return _any.bundle_child(bao, "bao/v1", "bao/triggers/v1")["objectId"]


def _arm(bao, request):
    """Re-arm brain-setup for `request`; clearing lastRunAt is what re-arms it."""
    _upsert(bao, _trigger_anchor(bao), TRIGGERS, SETUP_TRIGGER_ID, {
        "name": "brain setup", "kind": "once", "spec": {"at": now()},  # noqa: F821
        "program": PROGRAM, "args": {"requestedAt": request["requestedAt"]},
        "enabled": True, "lastRunAt": None}, "arming brain-setup")


def _hub_record(bao, hub, collection, name):
    # the hub's stores are SHARED datasets: a read returns, and an `id`
    # filter compares, the whole "<objectId>/<recordId>"; writes take the
    # plain id (any docs/03-api.md § Shared datasets)
    rows = _any.query(bao, hub, collection, filter={"id": f"{hub}/{name}"}, limit=1)
    return rows[0] if rows else None


# --- the brain's jobs ------------------------------------------------------

def _wanted_jobs(bao, hub):
    """[(job key, base id, record)] — the jobs a set-up brain runs."""
    return [
        ((HEARTBEAT, None), "brain-heartbeat", {
            "name": "brain heartbeat", "kind": "cron", "spec": {"every_s": 600},
            "program": HEARTBEAT,
            "args": {"space": bao, "hub": hub["objectId"],
                     "datasets": hub["datasets"]},
            "enabled": True}),
        ((PROGRAM, "reconcile"), "brain-reconcile", {
            "name": "brain reconcile", "kind": "cron", "spec": {"every_s": 3600},
            "program": PROGRAM, "args": {"mode": "reconcile"},
            "enabled": True}),
        ((PEOPLE, None), "brain-people", {
            "name": "brain people", "kind": "cron", "spec": {"every_s": 900},
            "program": PEOPLE,
            "args": {"space": bao, "hub": hub["objectId"], "datasets": hub["datasets"]},
            "enabled": True}),
        ((PROMISES, None), "brain-promises", {
            "name": "brain promises", "kind": "cron", "spec": {"every_s": 1800},
            "program": PROMISES,
            "args": {"space": bao, "hub": hub["objectId"], "datasets": hub["datasets"]},
            "enabled": True}),
    ]


def _ensure_mail_sync(bao, anchor):
    """Adopt or write the cron that syncs mail into Bao's space → the verdict.

    The brain reads only Bao's copy of the mail. A gmailSync cron the user
    already runs into Bao's space is adopted; crons into other spaces are
    named and left alone. Mail sync is not a brain job: switching the brain
    off leaves it running, since the mailbox is useful without the brain."""
    mine, other = None, []
    for rec in _any.query(bao, anchor, TRIGGERS, limit=500):
        if rec.get("program") != GMAIL_SYNC or rec.get("kind") != "cron":
            continue
        target = (rec.get("args") or {}).get("space")
        if target == bao:
            mine = mine or rec
        else:
            other.append({"triggerId": rec.get("id"), "space": target})
    if mine is not None:
        return {"mode": "existing", "triggerId": mine.get("id"), "other": other}
    record = {"name": "mail sync", "kind": "cron", "spec": {"every_s": 900},
              "program": GMAIL_SYNC, "args": {"space": bao}, "enabled": True}
    rid = _write_job(bao, anchor, MAIL_SYNC_ID, record, set())
    return {"mode": "created", "triggerId": rid, "other": other}


def _job_key(rec):
    return (rec.get("program"), (rec.get("args") or {}).get("mode"))


def _brain_jobs(bao, anchor):
    """The brain's job records, by job key (a cron whose program is a brain program)."""
    out = {}
    for rec in _any.query(bao, anchor, TRIGGERS, limit=500):
        prog = rec.get("program") or ""
        if rec.get("kind") == "cron" and prog.startswith("agent:brain"):
            out.setdefault(_job_key(rec), []).append(rec)
    return out


def _write_job(bao, anchor, base_id, record, taken):
    """Write a missing job under base_id, or a fresh id when that one is used."""
    rid = base_id if base_id not in taken else f"{base_id}-{int(now())}"  # noqa: F821
    try:
        _upsert(bao, anchor, TRIGGERS, rid, record, f"writing job {rid}")
    except write_rejected as e:
        # a deleted record's id is burned for good: write a fresh one
        if "deleted" not in str(e):
            raise
        rid = f"{base_id}-{int(now())}"  # noqa: F821
        _upsert(bao, anchor, TRIGGERS, rid, record, f"writing job {rid}")
    return rid


def _set_enabled(bao, anchor, rid, enabled):
    _checked(_any.modify(bao, {"objectId": anchor, "dataset": TRIGGERS, "records": [
        {"id": rid, "ops": [{"type": "$set", "path": "enabled", "value": enabled}]}]}),
        f"{'enabling' if enabled else 'disabling'} job {rid}")


def _ensure_jobs(bao, hub):
    """Write the missing jobs; never touch an existing one's `enabled`."""
    anchor = _trigger_anchor(bao)
    have = _brain_jobs(bao, anchor)
    taken = {r.get("id") for recs in have.values() for r in recs}
    written = []
    for key, base_id, record in _wanted_jobs(bao, hub):
        if key not in have:
            written.append(_write_job(bao, anchor, base_id, record, taken))
    return anchor, written


class jobs_unreadable(Exception):
    """brain.jobs is not in its {offDisabled: [job id]} shape."""


def _off_disabled():
    # an agent_config row, not a hub record: an off run after a failed
    # setup has no hub to write to
    value = _config_get(JOBS_KEY)
    if value is None:
        return []
    ids = value.get("offDisabled") if isinstance(value, dict) else None
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise jobs_unreadable(f"brain.jobs is {value!r}")
    return list(ids)


def _disable_jobs(bao):
    """Disable every enabled brain job; remember which, for the next on run.

    The list is written first, so a run cut short leaves no disabled job
    the next on run would not bring back."""
    anchor = _trigger_anchor(bao)
    enabled = [rec["id"] for recs in _brain_jobs(bao, anchor).values() for rec in recs
               if rec.get("enabled", True)]
    if enabled:
        _config_set(JOBS_KEY, {"offDisabled": sorted(set(_off_disabled()) | set(enabled))})
    for rid in enabled:
        _set_enabled(bao, anchor, rid, False)
    return enabled


def _reenable_jobs(bao, anchor):
    """Re-enable exactly the jobs the last off run disabled.

    A remembered job this device cannot see yet (its record has not synced)
    stays remembered for the next on run."""
    remembered = _off_disabled()
    if not remembered:
        return []
    live = {r["id"]: r for recs in _brain_jobs(bao, anchor).values() for r in recs}
    back = []
    for rid in remembered:
        if rid in live and not live[rid].get("enabled", True):
            _set_enabled(bao, anchor, rid, True)
            back.append(rid)
    _config_set(JOBS_KEY, {"offDisabled": [rid for rid in remembered if rid not in live]})
    return back


def _stopped_jobs(bao, anchor):
    """The brain's jobs that the runtime stopped after repeated failures."""
    out = []
    for rec in _any.query(bao, anchor, TRIGGERS, limit=500):
        prog = rec.get("program") or ""
        ours = rec.get("kind") == "cron" and prog.startswith("agent:brain")
        if ours and not rec.get("enabled", True) and rec.get("lastStatus") == STOPPED:
            out.append(rec)
    return sorted(out, key=lambda r: r["id"])


def _warning(stopped):
    """brain.status's `stopped`: [{id, name}] — what Settings names."""
    return [{"id": r["id"], "name": r.get("name") or r["id"]} for r in stopped]


def _restart_stopped(bao, anchor):
    """Re-enable the stopped jobs; re-enabling resets the runtime's breaker."""
    back = []
    for rec in _stopped_jobs(bao, anchor):
        _set_enabled(bao, anchor, rec["id"], True)
        back.append(rec["id"])
    return back


# --- provisioning ----------------------------------------------------------

def _provision(bao):
    """Install or adopt everything the brain needs in Bao's space → the hub."""
    for usecase in CATALOG_APPS:
        _retry(lambda u=usecase: _any.setup_app(bao, u))
    _retry(lambda: _any.ensure_bundle(bao, BUNDLE, name="Brain", root_type="page"))
    _any.create_type(bao, {"name": "Brain hub", "xKey": HUB_TYPE, "hidden": True,
                           "body": False})
    datasets = {}
    for draft in DATASETS:
        # `_create_dataset` is any@v1's harness-plumbing export, the one
        # enrich@v1 and applet@v1 declare their stores with
        datasets[draft["key"]] = _any._create_dataset(bao, HUB_TYPE, draft)["collection"]
    for draft in COLLECTIONS:
        _any.create_collection(bao, draft)
    hub_id = _retry(lambda: _any.bundle_child(bao, BUNDLE, HUB_SEED, type_key=HUB_TYPE))
    hub = {"objectId": hub_id["objectId"], "datasets": datasets, "version": HUB_VERSION}
    for name, seed in (("exclusions", EXCLUSIONS_SEED), ("blocklist", BLOCKLIST_SEED)):
        if _hub_record(bao, hub["objectId"], datasets["config"], name) is None:
            _upsert(bao, hub["objectId"], datasets["config"], name,
                    {"value": dict(seed)}, f"seeding the {name}")
    for key, source in TIERS:
        if _config_get(key) is None:
            row = _config_get(source)
            if row is not None:
                _config_set(key, row)
    return hub


def _current_hub(status):
    hub = (status or {}).get("hub")
    if isinstance(hub, dict) and hub.get("objectId") and isinstance(hub.get("datasets"), dict):
        return hub
    return None


# --- the trigger runs ------------------------------------------------------

def _setup():
    bao = _any.bao_space()
    answered = (_status() or {}).get("requestedAt")
    request = _request()
    if request is None:
        request = {"spaceId": bao, "requestedAt": _iso(now()), "by": "default"}  # noqa: F821
        _config_set(SPACE_KEY, request)
    if request.get("spaceId") is None:
        try:
            turned_off = _disable_jobs(bao)
        except Exception as e:
            _write_status(request, "error",
                          error={"type": type(e).__name__, "message": str(e)[:500]})
            raise provision_failed(f"{type(e).__name__}: {e}") from e
        _write_status(request, "off")
        return {"state": "off", "disabled": turned_off}
    if request["spaceId"] != bao:
        message = "The brain lives in Bao's own space; the request named another."
        _write_status(request, "error", error={"type": "wrong_space", "message": message})
        raise provision_failed(message)
    _write_status(request, "provisioning")
    try:
        hub = _provision(bao)
        anchor, written = _ensure_jobs(bao, hub)
        reenabled = _reenable_jobs(bao, anchor)
        mail_sync = _ensure_mail_sync(bao, anchor)
        # only a new request restarts: a same-request re-arm is not the user
        # asking to try again
        fresh = request["requestedAt"] != answered
        restarted = _restart_stopped(bao, anchor) if fresh else []
        stopped = _warning(_stopped_jobs(bao, anchor))
    except Exception as e:
        _write_status(request, "error",
                      error={"type": type(e).__name__, "message": str(e)[:500]})
        raise provision_failed(f"{type(e).__name__}: {e}") from e
    extra = {"stopped": stopped} if stopped else {}
    _write_status(request, "ready", hub=hub, mailSync=mail_sync, **extra)
    return {"state": "ready", "hub": hub, "written": written, "reenabled": reenabled,
            "restarted": restarted, "mailSync": mail_sync}


def _reconcile():
    bao = _any.bao_space()
    request = _request()
    status = _status()
    if request is None:
        return {"reconciled": False, "reason": "no request yet"}
    hub = _current_hub(status)
    if request.get("spaceId") is None:
        return {"reconciled": True, "disabled": _disable_jobs(bao)}
    answered = (status or {}).get("requestedAt") == request["requestedAt"]
    if hub is None or not answered or (status or {}).get("state") != "ready":
        return {"reconciled": False, "reason": "not set up"}
    if int(hub.get("version") or 1) < HUB_VERSION:
        # an older hub: setup installs what this version adds
        _arm(bao, request)
        return {"reconciled": True, "upgrading": True}
    anchor, written = _ensure_jobs(bao, hub)
    stopped = _warning(_stopped_jobs(bao, anchor))
    if stopped != (status.get("stopped") or []):
        # the warning changes, the answer does not: `at` and the hub stay
        value = {k: v for k, v in status.items() if k != "stopped"}
        _config_set(STATUS_KEY, {**value, "stopped": stopped} if stopped else value)
    return {"reconciled": True, "written": written, "stopped": [w["id"] for w in stopped]}


def main(args):
    # trigger entry (ADR-006 §4): the brain-setup once, or the hourly reconcile
    if (args or {}).get("mode") == "reconcile":
        return _reconcile()
    return _setup()


# --- chat tools ------------------------------------------------------------

def _state(request, status):
    if request is None:
        return "waiting"
    answered = status is not None and status.get("requestedAt") == request["requestedAt"]
    if request.get("spaceId") is None:
        return "off" if answered and status.get("state") == "off" else "switching_off"
    if not answered:
        return "waiting"
    return {"provisioning": "setting_up", "ready": "ready",
            "error": "error"}.get(status.get("state"), "waiting")


@span("brainSetup.status", kind="getter")  # noqa: F821 - guest global
def status():
    """How the brain stands → {state, request, status, apps, mailboxes, stopped}.

    state ∈ waiting | setting_up | ready | error | switching_off | off.
    `apps` names the catalog apps Bao's space lists (meetings and tasks
    among them; people installs the person collection, not a listed app);
    `mailboxes` names the synced mailboxes there; `stopped` the jobs the
    runtime stopped after repeated failures, as [{id, name}]."""
    bao = _any.bao_space()
    request, current = _request(), _status()
    apps = sorted({a.get("usecase") for a in _any.list_apps(bao) if a.get("usecase")})
    boxes = _any.query_objects(bao, normalize=False, filter={"any.type": "mailbox"},
                               limit=20)
    stopped = _warning(_stopped_jobs(bao, _trigger_anchor(bao)))
    return {"state": _state(request, current), "request": request, "status": current,
            "apps": apps,
            "mailboxes": [((b.get("any") or {}).get("name") or b.get("id")) for b in boxes],
            "stopped": stopped}


def _switch(space_id):
    bao = _any.bao_space()
    request = {"spaceId": space_id, "requestedAt": _iso(now()), "by": "chat"}  # noqa: F821
    _config_set(SPACE_KEY, request)
    _arm(bao, request)
    return {"state": "waiting", "request": request}


@span("brainSetup.on", kind="mutator")  # noqa: F821 - guest global
def on():
    """Switch the brain on in Bao's space → {state: "waiting", request}.

    Only after the user said yes in this conversation. Setup runs on the
    next tick; read `status()` next turn."""
    return _switch(_any.bao_space())


@span("brainSetup.off", kind="mutator")  # noqa: F821 - guest global
def off():
    """Switch the brain off → {state: "waiting", request}.

    Only after the user said yes. Its jobs stop; nothing it learned is
    deleted. Read `status()` next turn."""
    return _switch(None)


@span("brainSetup.exclude", kind="mutator")  # noqa: F821 - guest global
def exclude(person=None, email=None, domain=None):
    """Keep a person, an address or a domain out of the brain → the exclusion list.

    Appends only: removing an exclusion is Settings-only. Raises
    not_set_up while the brain has no hub."""
    bao = _any.bao_space()
    hub = _current_hub(_status())
    if hub is None:
        raise not_set_up("The brain is not set up yet; switch it on first.")
    rec = _hub_record(bao, hub["objectId"], hub["datasets"]["config"], "exclusions")
    value = dict((rec or {}).get("value") or EXCLUSIONS_SEED)
    for field, item in (("personIds", person), ("emails", email), ("domains", domain)):
        if item:
            items = list(value.get(field) or [])
            norm = item.strip().lower() if field != "personIds" else item.strip()
            if norm and norm not in items:
                items.append(norm)
            value[field] = items
    value["version"] = 1
    _upsert(bao, hub["objectId"], hub["datasets"]["config"], "exclusions",
            {"value": value}, "updating the exclusion list")
    return value


@span("brainSetup.set_chat_spaces", kind="mutator")  # noqa: F821 - guest global
def set_chat_spaces(spaces):
    """Name the spaces whose chats the brain may read → {spaces}.

    Empty means none (the default). Re-arms setup so the chat-scan job
    follows the list."""
    if not isinstance(spaces, (list, tuple)) or not all(isinstance(s, str) and s
                                                        for s in spaces):
        raise ValueError("spaces: a list of space ids")
    bao = _any.bao_space()
    chosen = sorted(set(spaces))
    _config_set(CHAT_SPACES_KEY, chosen)
    request = _request()
    if request is not None:
        _arm(bao, request)
    return {"spaces": chosen}
