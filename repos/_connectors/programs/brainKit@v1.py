"""Brain helpers: checked writes, hub records, exclusions and mail addresses."""

__any_category__ = "plugin"  # the AI brain is a plugin of the connectors repo (ADR-033 §1)

# ADR-033 / ADR-034. Shared by the brain's programs (setup's status, the
# heartbeat, people, promises, facts, recall, tasks, the chat scan); not a
# tool. So is the rule that links a chat identity to a person (ADR-039 §5).
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
    address is."""
    rec = hub_record(space, hub, datasets["config"], "exclusions")
    problem = exclusions_problem(rec)
    if problem is not None:
        raise exclusions_unreadable(problem)
    value = rec["value"]
    ids = sorted(set(value["personIds"]))
    by_person = set()
    if ids:
        try:
            rows = _any.query_objects(space, filter={"id": {"$in": ids}}, limit=len(ids))
        except _any.AnyError as e:
            raise exclusions_unreadable(f"the excluded persons could not be read: {e}") from e
        for row in rows:
            by_person.update(person_emails(row))
    return {"personIds": set(ids),
            "personEmails": by_person,
            "emails": {e.lower() for e in value["emails"]},
            "domains": {d.lower() for d in value["domains"]}}


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
    ADR-035 §1): True only when the row says allowed."""
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


# --- identities: who wrote a chat message (ADR-039 §5) ----------------------

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
