"""Brain helpers: checked writes, hub records, exclusions and mail addresses."""

__any_category__ = "plugin"  # the AI brain is a plugin of the connectors repo (ADR-033 §1.1)

# ADR-033 §1, §2. Shared by the brain's programs (setup's status, the
# heartbeat, people, promises, facts, recall, tasks, the chat scan); not a
# tool. So is the rule that links a chat identity to a person (ADR-033 §7.5).
# Three server rules every brain write and read depends on live here, once:
#   - modify / upsert answer HTTP 200 even when they refuse a record; the
#     refusal sits only in `rejections` (checked)
#   - the hub's stores are SHARED datasets: reads return, and `id` filters
#     compare, "<objectId>/<recordId>"; writes take the plain id
#     (hub_record, plain_id)
#   - the exclusion list and the blocklist are read fail-closed: a missing
#     or unreadable list stops every job that reads mail (exclusions,
#     blocklist)

import contextlib
import json
import re
from email.utils import getaddresses

_any = use("agent:any@v1")  # noqa: F821 - guest global

# how far back a job reads a mailbox it has no cursor for: a mailbox can hold
# years of mail, and promises from years ago are not open promises
BACKFILL_DAYS = 30

# local parts and hosts that are never a person
_NOISE = re.compile(
    r"^(no-?reply|do-?not-?reply|notifications?|mailer-daemon|postmaster|bounces?"
    r"|calendar-notification)([+._-].*)?@"
    r"|@([a-z0-9-]+\.)*googlegroups\.com$"
    r"|@(.+\.)?(mailchimp|sendgrid|mandrillapp|amazonses)\.(com|net)$",
    re.I)
# the message a promise's `source` opens: …/<mailboxId>/email_messages/<messageId>
_MAIL_LINK = re.compile(r"/([^/]+)/email_messages/([^/]+)$")


class write_rejected(Exception):
    """The server refused a record write; the reply's reason is the message."""


class exclusions_unreadable(Exception):
    """The exclusion list is missing or not in its version-1 shape."""


class not_set_up(Exception):
    """The brain has no hub yet: it is off or Bao has not set it up."""


class blocklist_unreadable(Exception):
    """The blocklist is not an object of senders, domains and labels lists."""


def ready_hub():
    """(hub object id, datasets) of a brain Bao has set up; else not_set_up.

    For the brain's tools, which run outside a job and so read the hub off
    `brain.status` rather than their trigger's args."""
    try:
        status = effect("config.get", {"key": "brain.status"})["value"]  # noqa: F821
    except EffectError:  # noqa: F821 - guest global
        status = None
    status = status if isinstance(status, dict) else {}
    hub = status.get("hub")
    if status.get("state") != "ready" or not isinstance(hub, dict) or not hub.get("objectId"):
        raise not_set_up(
            "The brain is off. Offer to switch it on — what it reads (the user's "
            "Gmail), what it keeps (people, promises, facts in Bao's space) — and "
            "call brainSetup.on() only after the user's yes.")
    return hub["objectId"], dict(hub.get("datasets") or {})


def checked(reply, what):
    """`reply`, or raise write_rejected on a per-record rejection."""
    rejected = (reply or {}).get("rejections") or []
    if rejected:
        first = rejected[0]
        reason = first.get("reason") or first.get("code") or "rejected"
        raise write_rejected(f"{what}: {reason}")
    return reply


def upsert(space, oid, dataset, rid, value, what):
    return checked(_any.upsert_record(space, oid, dataset, rid, value), what)


def plain_id(record_id):
    """A shared record's own id: "<objectId>/<recordId>" → "<recordId>"."""
    return record_id.rsplit("/", 1)[-1] if isinstance(record_id, str) else record_id


def hub_record(space, hub, collection, name):
    """One hub record by its plain name, or None."""
    rows = _any.query(space, hub, collection, filter={"id": f"{hub}/{name}"}, limit=1)
    return rows[0] if rows else None


def config_value(space, hub, datasets, name):
    """The `value` of a hub config record, or None when absent."""
    rec = hub_record(space, hub, datasets["config"], name)
    return (rec or {}).get("value")


def write_config(space, hub, datasets, name, value):
    upsert(space, hub, datasets["config"], name, {"value": value}, f"writing config/{name}")


def exclusions_problem(rec):
    """Why the exclusion record is unusable, or None when it is fine."""
    if rec is None:
        return "no exclusion list"
    value = rec.get("value")
    if not isinstance(value, dict):
        return "the exclusion list is not an object"
    if value.get("version") != 1:
        return f"exclusion list version {value.get('version')!r}, expected 1"
    for field in ("personIds", "emails", "domains"):
        if not isinstance(value.get(field), list):
            return f"exclusion list field {field!r} is not a list"
    if "identities" in value and not isinstance(value.get("identities"), list):
        return "exclusion list field 'identities' is not a list"
    stamps = value.get("includedAt")
    if stamps is not None and not (isinstance(stamps, dict) and all(
            isinstance(stamps.get(f) or {}, dict)
            and all(isinstance(v, (int, float)) for v in (stamps.get(f) or {}).values())
            for f in ("personIds", "emails", "domains"))):
        return "exclusion list field 'includedAt' is not {personIds, emails, domains: {item: ms}}"
    return None


def person_emails(row):
    """A person row's lowercased addresses: brain_person.emails and person.email."""
    bp = row.get("brain_person") or {}
    emails = {str(e).lower() for e in (bp.get("emails") or []) if e}
    own = (row.get("person") or {}).get("email")
    if own:
        emails.add(str(own).lower())
    return sorted(emails)


def exclusions(space, hub, datasets):
    """The exclusion list as sets; raises exclusions_unreadable (fail closed).

    `personEmails` are the addresses of the persons excluded by id, so a
    message that carries one is kept out for everyone in it, as a listed
    address is. `readFrom` is {address or domain: ms}: the moment each one
    was last let back in (`brainSetup.include`, the list's `includedAt`),
    a person id resolved to their addresses like an exclusion is; mail
    from before it was kept out at the time and stays unread
    (`included_after`)."""
    rec = hub_record(space, hub, datasets["config"], "exclusions")
    problem = exclusions_problem(rec)
    if problem is not None:
        raise exclusions_unreadable(problem)
    value = rec["value"]
    stamps = value.get("includedAt") or {}
    stamped_ids = {k: int(v) for k, v in (stamps.get("personIds") or {}).items()}
    ids = sorted(set(value["personIds"]))
    by_person, read_from = set(), {}
    for field in ("emails", "domains"):
        for key, at in (stamps.get(field) or {}).items():
            key = str(key).lower()
            read_from[key] = max(read_from.get(key, 0), int(at))
    want = sorted(set(ids) | set(stamped_ids))
    if want:
        try:
            rows = _any.query_objects(space, filter={"id": {"$in": want}}, limit=len(want))
        except _any.AnyError as e:
            raise exclusions_unreadable(f"the excluded persons could not be read: {e}") from e
        for row in rows:
            if row["id"] in ids:
                by_person.update(person_emails(row))
            if row["id"] in stamped_ids:
                for addr in person_emails(row):
                    read_from[addr] = max(read_from.get(addr, 0), stamped_ids[row["id"]])
    return {"personIds": set(ids),
            "personEmails": by_person,
            "emails": {e.lower() for e in value["emails"]},
            "domains": {d.lower() for d in value["domains"]},
            # chat account identities kept out by the user (brainSetup.exclude(identity=…)),
            # bare ids: an any://u/<id> link in the list is read as its id
            "identities": {identity_of(i) for i in value.get("identities") or [] if i},
            "readFrom": read_from}


def blocklist(space, hub, datasets):
    """The blocklist as sets of senders, domains and labels, trimmed and lowercased.

    `labels` are Gmail label ids, as on a synced message's `labelIds`. Setup
    seeds the list, so a missing one, or one in another shape, raises
    blocklist_unreadable (fail closed)."""
    rec = hub_record(space, hub, datasets["config"], "blocklist")
    value = (rec or {}).get("value")
    if not isinstance(value, dict):
        raise blocklist_unreadable("no blocklist" if rec is None else
                                   "the blocklist is not an object")
    out = {}
    for field in ("senders", "domains", "labels"):
        items = value.get(field, [])
        if not isinstance(items, list) or not all(isinstance(v, str) for v in items):
            raise blocklist_unreadable(f"blocklist field {field!r} is not a list of text")
        out[field] = {v.strip().lower() for v in items if v.strip()}
    return out


def addresses(*headers):
    """[(display name, lowercased address)] from raw From / To / Cc values."""
    out = []
    for name, addr in getaddresses([h for h in headers if h]):
        addr = (addr or "").strip().lower()
        if addr and "@" in addr:
            out.append(((name or "").strip(), addr))
    return out


def is_noise(addr):
    return bool(_NOISE.search(addr))


def domain_of(addr):
    return addr.rsplit("@", 1)[-1]


def kept_out(addr, excl, block):
    """True when an address is excluded, blocklisted or noise."""
    dom = domain_of(addr)
    return (addr in excl["emails"] or addr in excl["personEmails"] or dom in excl["domains"]
            or addr in block["senders"] or dom in block["domains"] or is_noise(addr))


def included_after(addr, excl, at_ms):
    """True when the address, or its domain, was let back into the brain only
    after `at_ms`: a message from before was kept out when it arrived, and
    including someone re-reads nothing already skipped."""
    read_from = excl.get("readFrom") or {}
    return max(read_from.get(addr, 0), read_from.get(domain_of(addr), 0)) > at_ms


def promise_kept_out(row, excl, block):
    """True when a promise was made to an excluded or blocklisted person."""
    if row.get("personId") in excl["personIds"]:
        return True
    email = row.get("personEmail")
    return bool(email) and kept_out(str(email).lower(), excl, block)


def participant_kept_out(row, excl, block):
    """True when a synced message is under a blocklisted label or carries an
    excluded or blocklisted participant (a noise sender is not one)."""
    if labelled_out(row, block):
        return True
    people = list(row.get("participants") or [])
    people += [a for _, a in addresses(row.get("from"), row.get("to"), row.get("cc"))]
    return any(kept_out(a, excl, block) and not is_noise(a)
               for a in (str(p).lower() for p in people))


def promises_kept_out(space, rows, excl, block):
    """Plain ids of the promises made to someone kept out, or whose message now
    carries someone kept out: an exclusion added after the promise was read."""
    out, by_box = set(), {}
    for r in rows:
        rid = plain_id(r["id"])
        if promise_kept_out(r, excl, block):
            out.add(rid)
            continue
        link = _MAIL_LINK.search(str(r.get("source") or ""))
        if link:
            box = r.get("mailboxId") or link.group(1)
            by_box.setdefault(box, {}).setdefault(link.group(2), []).append(rid)
    for box, by_message in by_box.items():
        mids = sorted(by_message)
        for msg in _any.query(space, box, "email_messages", filter={"id": {"$in": mids}},
                              limit=len(mids)):
            if participant_kept_out(msg, excl, block):
                out.update(by_message.get(plain_id(msg["id"]), []))
    return out


def labelled_out(row, block):
    """True when a synced message carries a blocklisted label."""
    return any(str(label).lower() in block["labels"] for label in (row.get("labelIds") or []))


def mailboxes(space):
    """[(mailbox object id, lowercased address)] in the space; [] before any mail sync."""
    # the mailbox type is installed by the first mail sync, and a query
    # naming a type the space lacks raises
    if not any(t.get("xKey") == "mailbox" for t in _any.list_types(space)):
        return []
    rows = _any.query_objects(space, filter={"any.type": "mailbox"}, limit=50)
    out = []
    for r in rows:
        addr = ((r.get("mailbox") or {}).get("address") or r.get("name") or "").strip().lower()
        if r.get("id"):
            out.append((r["id"], addr))
    return out


def model_allowed():
    """The user's consent to reading mail with a model (`brain.model`,
    ADR-033 §3.1): True only when the row says allowed."""
    try:
        value = effect("config.get", {"key": "brain.model"})["value"]  # noqa: F821
    except EffectError:  # noqa: F821 - guest global
        return False
    return isinstance(value, dict) and value.get("allowed") is True


def normalize_ws(text):
    """Whitespace collapsed to single spaces, trimmed — how a quote is matched."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def clean_body(body, limit):
    """A mail body as a model reads it: quoted (`>`) lines dropped, each line's
    whitespace collapsed, capped at `limit` chars."""
    lines = [" ".join(ln.split()) for ln in str(body or "").splitlines()
             if not ln.lstrip().startswith(">")]
    return "\n".join(ln for ln in lines if ln)[:limit]


def parse_json(text):
    """Tolerant JSON (enrich@v1's reader): strip a ``` fence, skip prose to the
    first [ or {, tolerate trailing prose, repair invalid \\ escapes, and close
    a length-truncated array. None when nothing parses."""
    if not text:
        return None
    s = str(text).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    start = re.search(r"[\[{]", s)
    if not start:
        return None
    s = s[start.start():]
    for candidate in (s, re.sub(r'\\(?!["\\/bfnrtu])', "", s)):
        try:
            return json.JSONDecoder().raw_decode(candidate)[0]
        except ValueError:
            continue
    if s[0] == "[" and "}" in s:
        with contextlib.suppress(ValueError):
            return json.loads(s[:s.rindex("}") + 1] + "]")
    return None


def civil_day(y, m, d):
    """A proleptic Gregorian date as the UTC day's epoch seconds (no datetime)."""
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return (era * 146097 + doe - 719468) * 86400


def backfill_floor_ms():
    """The internalDate a job reads a mailbox from when it has no cursor for it."""
    return int((now() - BACKFILL_DAYS * 86400) * 1000)  # noqa: F821 - guest global


def mail_link(space, mailbox_id, message_id):
    """The key-form link any-ui opens on the message (any://o/…/email_messages/…)."""
    return f"any://o/{space}/{mailbox_id}/email_messages/{message_id}"


def job_args(args, *needed):
    """(space, hub, datasets) from a brain job's args, or None when incomplete."""
    args = args or {}
    datasets = args.get("datasets") if isinstance(args.get("datasets"), dict) else {}
    if not args.get("space") or not args.get("hub"):
        return None
    if any(not datasets.get(k) for k in ("config",) + needed):
        return None
    return args["space"], args["hub"], datasets


# the six groups a person's facts are shown in, by kind prefix
FACT_GROUPS = (("tastes", "taste."), ("views", "view."), ("preferences", "preference."),
               ("personal", "personal."), ("work", "work."), ("history", "history."))


def _fact_view(fact):
    return {"id": plain_id(fact["id"]), "kind": fact.get("kind"), "value": fact.get("value"),
            "quote": fact.get("quote"), "source": fact.get("source"),
            "channel": fact.get("channel"), "origin": fact.get("origin"),
            "confidence": fact.get("confidence"),
            "observedAt": ts_s(fact.get("observedAt"))}  # noqa: F821 - guest global


def person_facts(space, hub, datasets, person_id, limit=200):
    """(current facts grouped by FACT_GROUPS, proposed facts) for one person,
    newest first; ({}, []) when the hub keeps no facts yet."""
    if not datasets.get("facts"):
        return {}, []
    rows = _any.query(space, hub, datasets["facts"], filter={"personId": person_id},
                      sort=["-observedAt"], limit=limit)
    grouped = {name: [] for name, _ in FACT_GROUPS}
    proposed = []
    for row in rows:
        if row.get("status") == "proposed":
            proposed.append(_fact_view(row))
            continue
        if row.get("status") != "current":
            continue
        kind = str(row.get("kind") or "")
        for name, prefix in FACT_GROUPS:
            if kind.startswith(prefix):
                grouped[name].append(_fact_view(row))
                break
    return grouped, proposed


# --- synced mail in arrival order (ADR-033 §4.4) --------------------------

def ver_id(record):
    """A record's creation version (`_ver.id`): set once, lex-monotonic, exact
    under a string `$gt` — the cursor unit for mail and chat. None if absent."""
    ver = record.get("_ver")
    if isinstance(ver, str):
        return ver or None
    if isinstance(ver, dict):
        return ver.get("id") or ver.get("*") or None
    return None


def mail_since(space, mailbox_id, cursor, limit, floor_ms=None, **filters):
    """Synced mail after `cursor`, in the order it landed → (rows, cursor).

    The cursor is the last record's `_ver.id`. An `internalDate` cursor
    would pass mail for good: the Gmail backfill writes newest first, in
    slices across its own ticks, so a job that cursors on the message date
    sets its cursor to today on the first tick and never sees the older
    mail that lands after it. `floor_ms` (internalDate, inclusive) bounds
    every read, with or without a cursor: the backfill lands mail older
    than the floor after the cursor too, and a job that read it would
    walk a year of mail. `filters` narrow the query (e.g. a sender). Rows
    come in arrival order, not by date: sort them when the date matters.
    The cursor comes back unchanged when nothing was read."""
    flt = dict(filters)
    if cursor:
        flt["_ver.id"] = {"$gt": cursor}
    if floor_ms is not None:
        flt["internalDate"] = {"$gte": int(floor_ms)}
    rows = _any.query(space, mailbox_id, "email_messages", filter=flt, sort=["_ver.id"],
                      limit=limit)
    last = cursor
    for row in rows:
        v = ver_id(row)
        if v and (not last or v > last):
            last = v
    return rows, last


# --- one chat notice (ADR-033 §4.8) -------------------------------------------

# brain notices post under the literal agent name, as remind@v1 does: the chat
# watcher skips its own name, so a notice starts no chat turn and raises one
# notification; the runtime exposes no configured name to guests (ADR-033 §1.6)
AGENT_NAME = "bao"


def notice(space, text, attachments=None, counts=None):
    """Post one notice to Bao's general chat under the agent's name; best-effort.

    A failure is printed and counted in `counts["noticeFailed"]`, never
    raised: the records the notice announces are already written."""
    body = {"text": text, "agent": {"name": AGENT_NAME, "done": True}}
    if attachments:
        body["attachments"] = attachments
    try:
        chat = _any.general_chat(space)
        _any.chat_send(space, chat, body)
        return True
    except Exception as e:  # noqa: BLE001 - the notice is a nicety
        print(f"brain notice failed: {type(e).__name__}: {e}")
        if counts is not None:
            counts["noticeFailed"] = counts.get("noticeFailed", 0) + 1
        return False


# --- identities: who wrote a chat message (ADR-033 §7.5) ----------------------

PERSON_PRELOAD = 1000


def identity_uri(identity):
    """An account identity as the link form asks carry: any://u/<identity>."""
    return f"any://u/{identity}"


def identity_of(uri):
    """The bare identity of an any://u/<identity> link (a bare id passes)."""
    value = str(uri or "")
    return value[len("any://u/"):] if value.startswith("any://u/") else value


def person_rows(space):
    """The catalog persons an identity can be: filed under person or
    brain_person, the first PERSON_PRELOAD of each."""
    rows = {}
    for collection in ("brain_person", "person"):
        for row in _any.query_objects(space, filter={"any.collections": collection},
                                      limit=PERSON_PRELOAD):
            rows.setdefault(row["id"], row)
    return list(rows.values())


def _names(row):
    names = {(row.get("any") or {}).get("name"), row.get("name"),
             *((row.get("brain_person") or {}).get("aliases") or [])}
    return {str(n).strip().casefold() for n in names if n and str(n).strip()}


def identity_candidates(persons, identity, name):
    """The persons an account identity may be → [person id].

    The one whose brain_person.identity names it; else every person with no
    identity whose name or an alias equals `name` (case folded). A link needs
    exactly one candidate; an exclusion holds when any is excluded."""
    uri = identity_uri(identity)
    linked = [r["id"] for r in persons if (r.get("brain_person") or {}).get("identity") == uri]
    if linked:
        return linked
    want = str(name or "").strip().casefold()
    if not want:
        return []
    return [r["id"] for r in persons
            if not (r.get("brain_person") or {}).get("identity") and want in _names(r)]


def identity_person(persons, identity, name):
    """The one person an identity resolves to, or None (none, or ambiguous)."""
    found = identity_candidates(persons, identity, name)
    return found[0] if len(found) == 1 else None


class Identities:
    """Display names of account identities for one run: a space's member
    rows first, then the account's identity directory."""

    def __init__(self):
        self.names = {}

    def add_members(self, members):
        for m in members or []:
            if m.get("identity") and m.get("name"):
                self.names.setdefault(m["identity"], m["name"])

    def name(self, identity):
        if identity not in self.names:
            row = _any.get_identity(identity)
            self.names[identity] = (row or {}).get("name")
        return self.names[identity]


class IdentityResolver:
    """Who a chat identity is, for one run: the one place the brain turns an
    account identity into a person or a verdict (ADR-033 §7.5).

    Built from the space and the exclusion list; the catalog persons are
    read on first use (`person_rows`), display names through `names` (an
    `Identities`: add a space's members to it before asking). `me` is the
    user's own identity, never kept out.

    - `kept_out(identity)`: True when the user excluded the identity itself
      (`exclusions.identities`, brainSetup.exclude(identity=…)), else when
      any person it may be (`identity_candidates`) is excluded.
    - `person(identity)`: the one person it resolves to, or None.
    - `candidates(identity)`: every person it may be → [person id]."""

    def __init__(self, space, excl, me=None, names=None):
        self.space, self.excl, self.me = space, excl, me
        self.names = names if names is not None else Identities()
        self._persons = None

    @property
    def persons(self):
        if self._persons is None:
            self._persons = person_rows(self.space)
        return self._persons

    def candidates(self, identity):
        if not identity:
            return []
        return identity_candidates(self.persons, identity, self.names.name(identity))

    def kept_out(self, identity):
        if not identity or identity == self.me:
            return False
        if identity in (self.excl.get("identities") or ()):
            return True
        if not self.excl["personIds"]:
            return False
        return any(p in self.excl["personIds"] for p in self.candidates(identity))

    def person(self, identity):
        if not identity:
            return None
        return identity_person(self.persons, identity, self.names.name(identity))
