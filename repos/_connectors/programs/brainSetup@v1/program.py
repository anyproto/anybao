"""Set up, switch off and repair the AI brain in Bao's own space.

The brain is off until the user asks for it. `status()` says how it
stands; `on()` (Google must be connected) and `off()` switch it after
the user's yes; `allow_model()` is the user's consent to read mail with
a model; `exclude()` / `include()` keep a person out of it, or let them
back in. A trigger run of this program does the setup.
"""

__any_category__ = "plugin"  # the AI brain is a plugin of the connectors repo (ADR-033 §1)

# ADR-033. The contract between the chat tools here, the setup run and
# any client that reads the answer is two agent_config rows and one
# trigger record:
#   brain.space   the request: {spaceId: <Bao's space> | None, requestedAt, by}
#                 — written only by on() / off() here, after the user's
#                 yes in chat; absent means off (nothing starts the brain
#                 on its own), None means switched off
#   brain.status  this program's answer, echoing the request's requestedAt
#   brain setup   a run-once agent_triggers record whoever writes a
#                 request re-arms; its run is `main` below. A whole-value
#                 $set MERGES into the record, so the fired once's
#                 lastRunAt survives a plain rewrite and the runtime reads
#                 it as consumed: a re-arm writes lastRunAt: None.
# Nothing here is ever deleted: a deleted record id can never be written
# again. The brain's jobs and the setup once are ordinary agent_triggers
# records found by PROGRAM (and kind), not by id, so a deleted one is
# rewritten under a fresh id — the user can delete the once from the
# Scheduled list like any schedule, and on()/off() must still work. A
# pause holds: the reconcile run only writes MISSING jobs and never
# re-enables one; an off run records which jobs it disabled (brain.jobs)
# and the next on run re-enables exactly those. A job the runtime stopped
# after repeated failures (lastStatus auto_disabled) — a brain job or the
# mail sync it set up — is not a pause: the brain stays ready and
# brain.status lists it under `stopped`, and only a NEW request (any-ui's
# Try again) restarts it; a setup run re-armed for the same request (a hub
# upgrade) leaves it.

__any_tool__ = True  # agent-callable (ADR-010 §4)

_any = use("agent:any@v1")  # noqa: F821 - guest global
kit = use("brainKit@v1")  # noqa: F821 - guest global

SPACE_KEY = "brain.space"
STATUS_KEY = "brain.status"
# the user's consent to reading mail with a model (ADR-035 §1): written
# only by allow_model() / deny_model() here, after a plain-words yes
MODEL_KEY = "brain.model"
JOBS_KEY = "brain.jobs"
STOPPED = "auto_disabled"
SETUP_TRIGGER_ID = "brain-setup"
PROGRAM = "connectors:brainSetup@v1"
HEARTBEAT = "connectors:brainHeartbeat@v2"
# every heartbeat version: the setup run rewrites an older one's program in
# place, keeping its `enabled` (ADR-036 §10)
HEARTBEAT_PREFIX = "connectors:brainHeartbeat@"
PEOPLE = "connectors:brainPeople@v1"
PROMISES = "connectors:brainPromises@v1"
FACTS = "connectors:brainFacts@v1"
GMAIL_SYNC = "connectors:gmailSync@v1"
MAIL_SYNC_ID = "brain-mail-sync"
# the hub's shape; a reconcile run that finds an older hub re-arms setup,
# which installs what the newer shape adds (1: Phase 0, 2: + people,
# promises and tasks, 3: + facts, 4: + cards, drafts, approvals, standups)
HUB_VERSION = 4
BUNDLE = "brain/v1"
HUB_SEED = "brain/hub/v1"
HUB_TYPE = "brain_hub"
TRIGGERS = "agent_triggers"
CATALOG_APPS = ("people", "meetings", "tasks")
# the brain's model tiers, seeded from these sources only when absent so
# an edit in Settings → Model always wins
# one row per brain job that calls a model; a job's row arrives with the
# job (the facts extractor, ADR-035 §3; the reply drafter, ADR-036 §6),
# never ahead of it
TIERS = (("llm.tier.brain.extractor", "llm.tier.codegen"),
         ("llm.tier.brain.drafter", "llm.tier.codegen"))
EXCLUSIONS_SEED = {"version": 1, "personIds": [], "emails": [], "domains": []}
# the heartbeat's drafting knobs (ADR-036 §5), seeded when absent and the
# user's to edit afterwards; quietSenders are regexes over a sender address
DRAFTING_SEED = {"maxPerRun": 3, "replyWindowDays": 5, "meetingSoonHours": 24,
                 "quietSenders": []}
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
    # what the user's correspondents said about themselves, each with its
    # verbatim sentence (ADR-035); the id is sha256(personId, kind,
    # normalized value), so a re-run writes nothing and a second source is
    # appended to moreSources. History is kept: a supersession is auditable.
    {"key": "facts", "displayName": "Brain facts", "shared": True,
     "idRule": "user", "deleteBy": "author",
     "search": {"text": ["value", "quote"], "scope": "facts"},
     "fields": [_f("personId", "string", required=True),
                _f("kind", "string", required=True),
                _f("value", "string", required=True),
                _f("quote", "string", required=True),
                _f("source", "string", required=True),
                _f("channel", "string", required=True),
                _f("observedAt", "datetime", required=True),
                _f("origin", "string", required=True),
                _f("confidence", "number", required=True),
                _f("model", "string"),
                _f("supersedes", "string", mutable=True),
                _f("status", "string", mutable=True),
                _f("supersededBy", "string", mutable=True),
                _f("moreSources", "array", mutable=True),
                _f("decidedAt", "datetime", mutable=True),
                _f("confirmedAt", "datetime", mutable=True),
                _CREATOR, _CREATED, _MODIFIED]},
    # --- Phase 3: the decision layer (ADR-036 §1) ---------------------------
    # what the brain asks the user; id = sha256(kind | identity), so a re-run
    # never duplicates a card. The heartbeat writes the card, the drafter its
    # draft pointers, the user its answer.
    {"key": "cards", "displayName": "Brain cards", "shared": True,
     "idRule": "user", "deleteBy": "author",
     "search": {"title": "title", "text": ["reason"], "scope": "cards"},
     "fields": [_f("kind", "string", required=True),
                _f("title", "string", required=True),
                _f("reason", "string", required=True),
                _f("subjectKey", "string", required=True),
                _f("sourceUri", "string"),
                _f("signalId", "string"),
                _f("personIds", "array"),
                _f("briefPersonIds", "array"),
                _f("status", "string", mutable=True),
                _f("snoozedUntil", "datetime", mutable=True),
                _f("answer", "string", mutable=True),
                _f("draftId", "string", mutable=True),
                _f("approvalId", "string", mutable=True),
                _f("draftAttempts", "number", mutable=True),
                _f("decidedAt", "datetime", mutable=True),
                _CREATOR, _CREATED, _MODIFIED]},
    # a reply the drafter (origin drafter) or the user (origin user, an edit)
    # wrote; write-once — an edit is a new draft that `supersedes` the old one
    {"key": "drafts", "displayName": "Brain drafts", "shared": True,
     "idRule": "user", "deleteBy": "author",
     "fields": [_f("cardId", "string", required=True),
                _f("sender", "string", required=True),
                _f("senderAddress", "string", required=True),
                _f("recipients", "array", required=True),
                _f("format", "string", required=True),
                _f("body", "string", required=True),
                _f("subject", "string", required=True),
                _f("inReplyTo", "string"),
                _f("threadId", "string"),
                _f("recipientProvenance", "string", required=True),
                _f("model", "string", required=True),
                _f("tier", "string", required=True),
                _f("origin", "string", required=True),
                _f("supersedes", "string"),
                _CREATOR, _CREATED, _MODIFIED]},
    # the user's decision on one draft's exact bytes: the id IS
    # hash(sender, recipients, format, body) (ADR-036 §7)
    {"key": "approvals", "displayName": "Brain approvals", "shared": True,
     "idRule": "user", "deleteBy": "author",
     "fields": [_f("draftId", "string", required=True),
                _f("cardId", "string", required=True),
                _f("sender", "string", required=True),
                _f("recipients", "array", required=True),
                _f("format", "string", required=True),
                _f("bodyHash", "string", required=True),
                _f("reason", "string", required=True),
                _f("status", "string", mutable=True),
                _f("decidedAt", "datetime", mutable=True),
                _f("consumedByRun", "string", mutable=True),
                _f("rejectedReason", "string", mutable=True),
                _f("editedTo", "string", mutable=True),
                _CREATOR, _CREATED, _MODIFIED]},
    # one row per brain run that did something — the per-agent standup,
    # kept out of the chat (ADR-036 §8)
    {"key": "standups", "displayName": "Brain standups", "shared": True,
     "idRule": "user", "deleteBy": "author",
     "fields": [_f("job", "string", required=True),
                _f("tick", "number", required=True),
                _f("did", "array", required=True),
                _f("needs", "array"),
                _f("counts", "object", required=True),
                _f("runId", "string"),
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


class unknown_person(Exception):
    """No object with that id in Bao's space."""


class google_not_connected(Exception):
    """The brain reads mail from Gmail: connect Google before switching it on."""


OFFER = ("The brain is off. Offer to switch it on — what it reads (the user's "
         "Gmail), what it keeps (people, promises, facts in Bao's space) — and "
         "call brainSetup.on() only after the user's yes.")


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


def _setup_once(bao, anchor):
    """The setup once's record — found by program and kind, not by id: a
    deleted record's id is burned, and the user can delete it from the
    Scheduled list like any schedule."""
    found = [rec for rec in _any.query(bao, anchor, TRIGGERS, limit=500)
             if rec.get("kind") == "once" and rec.get("program") == PROGRAM]
    return min(found, key=lambda r: r.get("id") or "") if found else None


def _arm(bao, request):
    """Re-arm the setup once for `request`; clearing lastRunAt is what re-arms it.

    The existing record is rewritten in place; a missing one is written
    under SETUP_TRIGGER_ID, or a fresh id when that one is burned."""
    anchor = _trigger_anchor(bao)
    record = {"name": "brain setup", "kind": "once", "spec": {"at": now()},  # noqa: F821
              "program": PROGRAM, "args": {"requestedAt": request["requestedAt"]},
              "enabled": True, "lastRunAt": None}
    existing = _setup_once(bao, anchor)
    if existing is not None:
        _upsert(bao, anchor, TRIGGERS, existing["id"], record, "arming brain setup")
        return existing["id"]
    return _write_job(bao, anchor, SETUP_TRIGGER_ID, record, set())


def _hub_record(bao, hub, collection, name):
    # the hub's stores are SHARED datasets: a read returns, and an `id`
    # filter compares, the whole "<objectId>/<recordId>"; writes take the
    # plain id (any docs/03-api.md § Shared datasets)
    rows = _any.query(bao, hub, collection, filter={"id": f"{hub}/{name}"}, limit=1)
    return rows[0] if rows else None


# --- the brain's jobs ------------------------------------------------------

def _heartbeat_args(bao, hub):
    # `tier` rides through to the reply drafts the heartbeat arms (ADR-036 §5)
    return {"space": bao, "hub": hub["objectId"], "datasets": dict(hub["datasets"]),
            "tier": "brain.drafter"}


def _wanted_jobs(bao, hub):
    """[(job key, base id, record)] — the jobs a set-up brain runs."""
    return [
        ((HEARTBEAT, None), "brain-heartbeat", {
            "name": "brain heartbeat", "kind": "cron", "spec": {"every_s": 600},
            "program": HEARTBEAT, "args": _heartbeat_args(bao, hub),
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
        # reads mail with a model only while brain.model allows it (ADR-035 §1)
        ((FACTS, None), "brain-facts", {
            "name": "brain facts", "kind": "cron", "spec": {"every_s": 21600},
            "program": FACTS,
            "args": {"space": bao, "hub": hub["objectId"], "datasets": hub["datasets"],
                     "tier": "brain.extractor"},
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
        if rec.get("kind") == "cron" and prog.startswith("connectors:brain"):
            out.setdefault(_job_key(rec), []).append(rec)
    return out


def _write_job(bao, anchor, base_id, record, taken):
    """Write a missing job (or the setup once) under base_id, or a fresh id
    when that one is used or burned."""
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


def _upgrade_heartbeats(bao, anchor, have, hub):
    """Point every heartbeat record at HEARTBEAT and the hub's datasets → [ids].

    The record is rewritten in place — `program` and `args` only, never
    `enabled` — so a heartbeat the user paused stays paused (ADR-036 §10).
    An older version is re-keyed in `have`, so the missing-job pass does
    not write a second heartbeat beside it."""
    wanted = _heartbeat_args(bao, hub)
    upgraded = []
    for key in [k for k in have if (k[0] or "").startswith(HEARTBEAT_PREFIX)]:
        for rec in have[key]:
            if rec.get("program") == HEARTBEAT and rec.get("args") == wanted:
                continue
            _checked(_any.modify(bao, {"objectId": anchor, "dataset": TRIGGERS, "records": [
                {"id": rec["id"], "ops": [
                    {"type": "$set", "path": "program", "value": HEARTBEAT},
                    {"type": "$set", "path": "args", "value": wanted}]}]}),
                f"upgrading job {rec['id']}")
            rec.update(program=HEARTBEAT, args=wanted)
            upgraded.append(rec["id"])
        if key != (HEARTBEAT, None):
            have.setdefault((HEARTBEAT, None), []).extend(have.pop(key))
    return upgraded


def _ensure_jobs(bao, hub):
    """Write the missing jobs and upgrade the heartbeat in place → (anchor,
    written, upgraded); never touch an existing job's `enabled`."""
    anchor = _trigger_anchor(bao)
    have = _brain_jobs(bao, anchor)
    taken = {r.get("id") for recs in have.values() for r in recs}
    upgraded = _upgrade_heartbeats(bao, anchor, have, hub)
    written = []
    for key, base_id, record in _wanted_jobs(bao, hub):
        if key not in have:
            written.append(_write_job(bao, anchor, base_id, record, taken))
    return anchor, written, upgraded


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


def _stopped_jobs(bao, anchor, mail_sync):
    """The brain's jobs, and the mail sync it set up, that the runtime stopped
    after repeated failures."""
    sync_id = (mail_sync or {}).get("triggerId")
    out = []
    for rec in _any.query(bao, anchor, TRIGGERS, limit=500):
        prog = rec.get("program") or ""
        ours = (rec.get("kind") == "cron" and prog.startswith("connectors:brain")) or (
            sync_id is not None and rec.get("id") == sync_id)
        if ours and not rec.get("enabled", True) and rec.get("lastStatus") == STOPPED:
            out.append(rec)
    return sorted(out, key=lambda r: r["id"])


def _warning(stopped):
    """brain.status's `stopped`: [{id, name}] — what status() and a client name."""
    return [{"id": r["id"], "name": r.get("name") or r["id"]} for r in stopped]


def _restart_stopped(bao, anchor, mail_sync):
    """Re-enable the stopped jobs; re-enabling resets the runtime's breaker."""
    back = []
    for rec in _stopped_jobs(bao, anchor, mail_sync):
        _set_enabled(bao, anchor, rec["id"], True)
        back.append(rec["id"])
    return back


# --- provisioning ----------------------------------------------------------

def _provision(bao):
    """Install or adopt everything the brain needs in Bao's space → the hub."""
    for usecase in CATALOG_APPS:
        _retry(lambda u=usecase: _any.setup_app(u, bao, show=False))   # definitions only
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
    for name, seed in (("exclusions", EXCLUSIONS_SEED), ("blocklist", BLOCKLIST_SEED),
                       ("drafting", DRAFTING_SEED)):
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
        # only on() / off() write the row: a run with no request (a stale
        # arm) sets nothing up and writes nothing
        return {"state": "off", "reason": "no request"}
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
        anchor, written, upgraded = _ensure_jobs(bao, hub)
        reenabled = _reenable_jobs(bao, anchor)
        mail_sync = _ensure_mail_sync(bao, anchor)
        # only a new request restarts: a same-request re-arm is not the user
        # asking to try again
        fresh = request["requestedAt"] != answered
        restarted = _restart_stopped(bao, anchor, mail_sync) if fresh else []
        stopped = _warning(_stopped_jobs(bao, anchor, mail_sync))
    except Exception as e:
        _write_status(request, "error",
                      error={"type": type(e).__name__, "message": str(e)[:500]})
        raise provision_failed(f"{type(e).__name__}: {e}") from e
    extra = {"stopped": stopped} if stopped else {}
    _write_status(request, "ready", hub=hub, mailSync=mail_sync, **extra)
    return {"state": "ready", "hub": hub, "written": written, "upgraded": upgraded,
            "reenabled": reenabled, "restarted": restarted, "mailSync": mail_sync}


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
    anchor, written, _upgraded = _ensure_jobs(bao, hub)
    stopped = _warning(_stopped_jobs(bao, anchor, status.get("mailSync")))
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
        return "off"
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
    `mailboxes` names the synced mailboxes' addresses there; `stopped` the
    jobs the runtime stopped after repeated failures, as [{id, name}]."""
    bao = _any.bao_space()
    request, current = _request(), _status()
    apps = sorted({a.get("usecase") for a in _any.list_apps(bao) if a.get("usecase")})
    stopped = _warning(_stopped_jobs(bao, _trigger_anchor(bao), (current or {}).get("mailSync")))
    model = _config_get(MODEL_KEY)
    return {"state": _state(request, current), "request": request, "status": current,
            "apps": apps,
            "mailboxes": [addr or box for box, addr in kit.mailboxes(bao)],
            "stopped": stopped,
            "modelAllowed": isinstance(model, dict) and model.get("allowed") is True}


def _switch(space_id):
    bao = _any.bao_space()
    request = {"spaceId": space_id, "requestedAt": _iso(now()), "by": "chat"}  # noqa: F821
    _config_set(SPACE_KEY, request)
    _arm(bao, request)
    return {"state": "waiting", "request": request}


@span("brainSetup.on", kind="mutator")  # noqa: F821 - guest global
def on():
    """Switch the brain on in Bao's space → {state: "waiting", request}.

    Only after the user said yes in this conversation. Raises
    google_not_connected while no Google account is connected: offer
    googleAuth.connect() first. Setup runs on the next tick; read
    `status()` next turn."""
    auth = use("googleAuth@v1")  # noqa: F821 - guest global; the same repo
    if not (auth.status() or {}).get("connected"):
        raise google_not_connected(
            "Google is not connected, and the brain reads the user's Gmail: "
            "offer googleAuth.connect() first, then on() again.")
    return _switch(_any.bao_space())


@span("brainSetup.off", kind="mutator")  # noqa: F821 - guest global
def off():
    """Switch the brain off → {state: "waiting", request}.

    Only after the user said yes. Its jobs stop; nothing it learned is
    deleted. Read `status()` next turn."""
    return _switch(None)


def _consent(allowed):
    row = {"allowed": allowed, "requestedAt": _iso(now()), "by": "chat"}  # noqa: F821
    _config_set(MODEL_KEY, row)
    return row


@span("brainSetup.allow_model", kind="mutator")  # noqa: F821 - guest global
def allow_model():
    """Record the user's consent to reading their mail with a model → the row.

    Only after a plain-words yes in this conversation to this question:
    may the brain read your mail with a model to keep facts about the
    people you write with? Until then the facts job reads nothing.
    Independent of on()/off()."""
    return _consent(True)


@span("brainSetup.deny_model", kind="mutator")  # noqa: F821 - guest global
def deny_model():
    """Withdraw that consent → the row.

    The jobs that read mail with a model stop; what they learned stays."""
    return _consent(False)


def _exclusion_list(bao, hub):
    """The exclusion list's value to edit, or the seed when there is none.

    A malformed list stops every job that reads mail; rewriting it whole
    would make it readable again with the damage inside, so it is refused."""
    rec = _hub_record(bao, hub["objectId"], hub["datasets"]["config"], "exclusions")
    if rec is None:
        return dict(EXCLUSIONS_SEED)        # its lists are replaced, never changed
    problem = kit.exclusions_problem(rec)
    if problem is not None:
        raise kit.exclusions_unreadable(f"{problem}: fix the list before editing it")
    return dict(rec["value"])


def _person_and_emails(bao, person, email, strict):
    """(person id, [lowercased addresses]) for an exclusion edit: a person's
    own addresses ride with their id. `strict` refuses an id that names no
    person; include() tolerates one, since the object may be gone."""
    person = (person or "").strip()
    emails = [email] if email else []
    if person:
        rows = _any.query_objects(bao, filter={"id": person}, limit=1)
        collections = set((rows[0].get("any") or {}).get("collections") or []) if rows else set()
        if collections & {"person", "brain_person"}:
            emails += kit.person_emails(rows[0])
        elif strict:
            raise unknown_person(f"no person {person!r} in Bao's space")
    return person, [(e or "").strip().lower() for e in emails]


def _included_at(value):
    """The list's `includedAt` to edit: {personIds, emails, domains: {item: ms}}
    — when each item was last let back in (kit.exclusions → readFrom)."""
    stamps = value.get("includedAt") or {}
    return {field: dict(stamps.get(field) or {}) for field in ("personIds", "emails", "domains")}


def _wanted(person, emails, domain):
    return {"personIds": [person], "emails": emails,
            "domains": [(domain or "").strip().lower().lstrip("@")]}


@span("brainSetup.exclude", kind="mutator")  # noqa: F821 - guest global
def exclude(person=None, email=None, domain=None):
    """Keep a person, an address or a domain out of the brain → the exclusion list.

    Appends; `include()` is the way back in. A person's addresses are
    added with them, so they stay out once the person is gone. Raises
    not_set_up while the brain has no hub, unknown_person for an id that
    names no person, exclusions_unreadable for a list in another shape."""
    bao = _any.bao_space()
    hub = _current_hub(_status())
    if hub is None:
        raise not_set_up(OFFER)
    person, emails = _person_and_emails(bao, person, email, strict=True)
    value = _exclusion_list(bao, hub)
    included = _included_at(value)
    for field, items in _wanted(person, emails, domain).items():
        for item in items:
            if item and item not in value[field]:
                value[field] = [*value[field], item]
            included[field].pop(item, None)      # kept out again
    value["version"] = 1
    value["includedAt"] = included
    _upsert(bao, hub["objectId"], hub["datasets"]["config"], "exclusions",
            {"value": value}, "updating the exclusion list")
    return value


@span("brainSetup.include", kind="mutator")  # noqa: F821 - guest global
def include(person=None, email=None, domain=None):
    """Let a person, an address or a domain back into the brain → the exclusion list.

    The inverse of exclude(), after the user asks: removes the id and,
    for a person that still exists, their addresses; an id that names
    no object any more is removed as is. Mail already kept out is not
    re-read: the list stamps the moment each item was let back in
    (`includedAt`), and the jobs skip mail from before it, however they
    cursor. Raises not_set_up while the brain has no hub,
    exclusions_unreadable for a list in another shape."""
    bao = _any.bao_space()
    hub = _current_hub(_status())
    if hub is None:
        raise not_set_up(OFFER)
    person, emails = _person_and_emails(bao, person, email, strict=False)
    value = _exclusion_list(bao, hub)
    included = _included_at(value)
    at = int(now() * 1000)  # noqa: F821 - guest global; ms, like a message's internalDate
    for field, items in _wanted(person, emails, domain).items():
        gone = {i for i in items if i}
        if gone:
            value[field] = [v for v in value[field] if v not in gone]
            for item in gone:
                included[field][item] = at
    value["version"] = 1
    value["includedAt"] = included
    _upsert(bao, hub["objectId"], hub["datasets"]["config"], "exclusions",
            {"value": value}, "updating the exclusion list")
    return value
