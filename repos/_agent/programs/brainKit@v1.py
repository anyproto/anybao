"""Brain helpers: checked writes, hub records, exclusions and mail addresses."""

# ADR-033 / ADR-034. Shared by the brain's programs (setup's status, the
# heartbeat, people, promises, recall, tasks); not a tool. Three server
# rules every brain write and read depends on live here, once:
#   - modify / upsert answer HTTP 200 even when they refuse a record; the
#     refusal sits only in `rejections` (checked)
#   - the hub's stores are SHARED datasets: reads return, and `id` filters
#     compare, "<objectId>/<recordId>"; writes take the plain id
#     (hub_record, plain_id)
#   - the exclusion list and the blocklist are read fail-closed: a missing
#     or unreadable list stops every job that reads mail (exclusions,
#     blocklist)

import re
from email.utils import getaddresses

_any = use("any@v1")  # noqa: F821 - guest global

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
        raise not_set_up("The brain is not set up yet; switch it on first.")
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
