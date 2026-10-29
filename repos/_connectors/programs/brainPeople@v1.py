"""Brain people: one catalog person per real correspondent in Bao's mail."""

__any_category__ = "plugin"  # the AI brain is a plugin of the connectors repo (ADR-033 §1.1)

# ADR-033 §2.3. A cron job the brain's setup run writes (every 900 s), args
# {space, hub, datasets}. It reads mail that gmailSync synced into Bao's
# space that landed after its cursor — the last `_ver.id` handled per
# mailbox (kit.mail_since: the backfill lands older mail later, which a
# date cursor would pass for good; only mail dated within BACKFILL_DAYS
# is read, cursor or not) — and keeps one catalog person — a `profile`
# filed under `person` — per address, also filed under the brain's
# `brain_person` collection with every address used, the names seen, when
# we last spoke and how often. It never reads a body: From / To / Cc only.
# Excluded people, blocklisted senders and domains, noise senders and the
# mailbox's own addresses never become people; mail under a blocklisted
# label is read past. Deterministic; no model.
#
# Resolution order for an address: a person whose brain_person.emails holds
# it, then one whose person.email equals it, else a new person. A run
# processes at most MAX_NEW unknown addresses and PER_MAILBOX messages per
# mailbox, starting a mailbox it has no cursor for BACKFILL_DAYS back; the
# cursor advances only past messages fully handled, in the order they
# landed, so a capped run resumes where it stopped. A single message with more new
# addresses than MAX_NEW is a mailing list, not correspondents: it is read
# past and nobody on it is minted. It then back-fills `personId` on
# promises whose recipient it now knows, past the preload included.
#
# Chats (ADR-033 §7.5): the account identities behind unlinked asks and the
# peers of one-to-one chats with unlinked promises are linked to a person
# only when brainKit's IdentityResolver finds exactly one candidate — written as
# brain_person.identity, never over another — and asks and chat promises
# then get their personId back-filled. A bare name never mints a person.

import re

kit = use("brainKit@v1")  # noqa: F821 - guest global
_any = use("agent:any@v1")  # noqa: F821 - guest global

PRELOAD = 1000
MAX_NEW = 300
PER_MAILBOX = 200
PROMISE_BACKFILL = 200
CHAT_BACKFILL = 200
_CHAT_SOURCE = re.compile(r"^any://o/([^/]+)/[^/]+/chat_messages/")


class unconfigured(Exception):
    """The job's args do not name the brain hub: the brain is not set up."""


class cursor_corrupt(Exception):
    """config/people.cursor is not in its {mail: {mailboxId: _ver.id}} shape."""


def _cursor(space, hub, datasets):
    value = kit.config_value(space, hub, datasets, "people.cursor")
    if value is None:
        return {}
    mail = value.get("mail") if isinstance(value, dict) else None
    if not isinstance(mail, dict) or not all(isinstance(v, str) and v for v in mail.values()):
        raise cursor_corrupt(f"people.cursor is {value!r}")
    return dict(mail)


class _People:
    """The space's brain persons by address: the first PRELOAD up front, the
    rest found as their addresses turn up, so a person past the preload is
    merged into, never overwritten or duplicated."""

    def __init__(self, space):
        self.space = space
        self.by_email = {}
        self.rows = {}
        for r in _any.query_objects(space, filter={"any.collections": "brain_person"},
                                    limit=PRELOAD):
            self._index(r)

    def _index(self, row):
        self.rows[row["id"]] = row
        for e in ((row.get("brain_person") or {}).get("emails") or []):
            self.by_email[str(e).lower()] = row["id"]

    def find(self, addr):
        """(person id, filed under brain_person) or (None, False)."""
        if addr in self.by_email:
            return self.by_email[addr], True
        for path in ("brain_person.emails", "person.email"):
            hits = _any.query_objects(self.space, filter={path: addr}, limit=1)
            if hits:
                row = hits[0]
                filed = "brain_person" in ((row.get("any") or {}).get("collections") or [])
                if filed:
                    self._index(row)
                return row["id"], filed
        return None, False


def _gather(space, boxes, cursor, own, excl, block, people):
    """Walk the mail that landed since the cursor → ({addr: seen}, new cursor,
    messages read, mass mailings). Arrival order (ADR-033 §4.4); a
    person's first / last contact are the min / max dates seen, so the
    order does not matter here."""
    seen, read, unknown, mass = {}, 0, set(), 0
    floor = kit.backfill_floor_ms()
    for box_id, _addr in boxes:
        # the date floor holds on every read: the backfill lands older mail
        # after the cursor too
        last = cursor.get(box_id)
        rows, _newest = kit.mail_since(space, box_id, last, PER_MAILBOX, floor_ms=floor)
        for row in rows:
            people_here = [] if kit.labelled_out(row, block) else [
                (n, a) for n, a in kit.addresses(row.get("from"), row.get("to"), row.get("cc"))
                if a not in own and not kit.kept_out(a, excl, block)]
            fresh = {a for _, a in people_here
                     if a not in people.by_email and a not in seen and a not in unknown}
            if len(fresh) > MAX_NEW:
                # more new addresses than a run may mint: a mailing list, not
                # correspondents; it would stop the cursor here for good
                people_here, fresh = [], set()
                mass += 1
            elif len(unknown) + len(fresh) > MAX_NEW:
                break            # capped: this message waits for the next run
            unknown |= fresh
            at = (row.get("internalDate") or 0) / 1000
            for name, addr in people_here:
                s = seen.setdefault(addr, {"names": set(), "first": at, "last": 0.0,
                                           "count": 0})
                if name and name.lower() != addr:
                    s["names"].add(name)
                s["first"] = min(s["first"], at)
                s["last"] = max(s["last"], at)
                s["count"] += 1
            ver = kit.ver_id(row)
            if ver and (not last or ver > last):
                last = ver
            read += 1
        if last:
            cursor[box_id] = last
    return seen, cursor, read, mass


def _merged(current, addr, s):
    """The brain_person values after this run's sightings of `addr`."""
    emails = list(current.get("emails") or [])
    if addr not in emails:
        emails.append(addr)
    aliases = list(current.get("aliases") or [])
    for n in sorted(s["names"]):
        if n not in aliases:
            aliases.append(n)
    before = ts_s(current.get("lastContactAt")) or 0  # noqa: F821 - guest global
    out = {"emails": emails, "aliases": aliases,
           "mailCount": int(current.get("mailCount") or 0) + s["count"]}
    if s["last"] > before:
        out["lastContactAt"] = instant(s["last"])  # noqa: F821 - guest global
    return out


def _apply(space, people, seen, excl):
    minted = updated = 0
    for addr in sorted(seen):
        s = seen[addr]
        pid, filed = people.find(addr)
        if pid is not None and pid in excl["personIds"]:
            continue
        if pid is None:
            name = sorted(s["names"])[0] if s["names"] else addr
            made = _any.create_object(space, {
                "type": "profile", "collections": ["person", "brain_person"], "name": name,
                "initialProperties": {
                    "person": {"email": addr},
                    "brain_person": {"emails": [addr], "aliases": sorted(s["names"]),
                                     "firstSeenAt": instant(s["first"]),  # noqa: F821
                                     "lastContactAt": instant(s["last"]),  # noqa: F821
                                     "mailCount": s["count"]}}})
            people.by_email[addr] = made["objectId"]
            people.rows[made["objectId"]] = {"id": made["objectId"], "brain_person": {
                "emails": [addr], "aliases": sorted(s["names"]), "mailCount": s["count"],
                "lastContactAt": s["last"]}}
            minted += 1
            continue
        if not filed:
            _any.add_to_collection(space, pid, "brain_person")
        current = (people.rows.get(pid) or {}).get("brain_person") or {}
        merged = _merged(current, addr, s)
        _any.update_object(space, pid, {"brain_person": merged})
        # a person seen under two addresses in one run merges twice
        people.rows.setdefault(pid, {"id": pid})["brain_person"] = {**current, **merged}
        people.by_email[addr] = pid
        updated += 1
    return minted, updated


def _link_promises(space, hub, datasets, people):
    """Back-fill personId on promises whose recipient is now a person."""
    if not datasets.get("promises"):
        return 0
    rows = _any.query(space, hub, datasets["promises"], filter={"personId": None},
                      limit=PROMISE_BACKFILL)
    linked = 0
    for r in rows:
        email = str(r.get("personEmail") or "").lower()
        pid = people.by_email.get(email) or (people.find(email)[0] if email else None)
        if pid:
            kit.checked(_any.modify(space, {"objectId": hub, "dataset": datasets["promises"],
                        "records": [{"id": kit.plain_id(r["id"]), "ops": [
                            {"type": "$set", "path": "personId", "value": pid}]}]}),
                        "linking a promise to its person")
            linked += 1
    return linked


def _chat_datasets(datasets):
    """The job's datasets, with the asks store the hub names when the job was
    written before the hub had one (its args never change)."""
    if datasets.get("asks"):
        return datasets
    try:
        _hub, live = kit.ready_hub()
    except kit.not_set_up:
        return datasets
    return {**live, **datasets}


def _peers(space_ids, names):
    """{space id: the other participant's identity} for the one-to-one spaces
    among `space_ids`; their members' names go into `names`."""
    rows = {r.get("id"): r for r in _any.list_spaces()}
    out, me = {}, None
    for sid in sorted(space_ids):
        if (rows.get(sid) or {}).get("spaceType") != "any.onetoone":
            continue
        me = me or _any.me()["identity"]
        members = _any.list_members(sid)
        names.add_members(members)
        others = [m["identity"] for m in members if m.get("identity") not in (None, me)]
        if others:
            out[sid] = others[0]
    return out


def _persist_link(space, persons, pid, identity):
    """Write brain_person.identity on the person (filed under brain_person
    first when needed), never over another identity → True when written."""
    row = next((r for r in persons if r["id"] == pid), None) or {}
    current = (row.get("brain_person") or {}).get("identity")
    uri = kit.identity_uri(identity)
    if current:
        return False
    if "brain_person" not in ((row.get("any") or {}).get("collections") or []):
        _any.add_to_collection(space, pid, "brain_person")
    _any.update_object(space, pid, {"brain_person": {"identity": uri}})
    row.setdefault("brain_person", {})["identity"] = uri
    return True


def _set_person(space, hub, dataset, record, pid, what):
    kit.checked(_any.modify(space, {"objectId": hub, "dataset": dataset, "records": [
        {"id": kit.plain_id(record["id"]), "ops": [
            {"type": "$set", "path": "personId", "value": pid}]}]}), what)


def _link_identities(space, hub, datasets, excl):
    """Link chat identities to persons and back-fill personId on asks and chat
    promises → {identitiesLinked, asksLinked, chatPromisesLinked}."""
    out = {"identitiesLinked": 0, "asksLinked": 0, "chatPromisesLinked": 0}
    datasets = _chat_datasets(datasets)
    if not datasets.get("asks"):
        return out                      # the hub keeps no chat records yet
    asks = _any.query(space, hub, datasets["asks"], filter={"personId": None},
                      limit=CHAT_BACKFILL)
    chat_promises = _any.query(space, hub, datasets["promises"],
                               filter={"personId": None, "channel": "chat"},
                               limit=CHAT_BACKFILL) if datasets.get("promises") else []
    if not asks and not chat_promises:
        return out
    promise_spaces = {m.group(1) for m in (_CHAT_SOURCE.match(str(p.get("source") or ""))
                                           for p in chat_promises) if m}
    who = kit.IdentityResolver(space, excl)
    peers = _peers(promise_spaces, who.names) if promise_spaces else {}
    wanted = {kit.identity_of(a.get("senderIdentity")) for a in asks} | set(peers.values())
    wanted.discard("")
    if not wanted:
        return out
    links = {}
    for identity in sorted(wanted):
        pid = who.person(identity)
        if pid:
            links[identity] = pid
            out["identitiesLinked"] += _persist_link(space, who.persons, pid, identity)
    for a in asks:
        pid = links.get(kit.identity_of(a.get("senderIdentity")))
        if pid:
            _set_person(space, hub, datasets["asks"], a, pid, "linking an ask to its person")
            out["asksLinked"] += 1
    for p in chat_promises:
        m = _CHAT_SOURCE.match(str(p.get("source") or ""))
        pid = links.get(peers.get(m.group(1))) if m else None
        if pid:
            _set_person(space, hub, datasets["promises"], p, pid,
                        "linking a chat promise to its person")
            out["chatPromisesLinked"] += 1
    return out


def main(args):
    parsed = kit.job_args(args)
    if parsed is None:
        raise unconfigured("brain people needs space, hub and datasets — set the brain up")
    space, hub, datasets = parsed
    excl = kit.exclusions(space, hub, datasets)          # fail closed
    block = kit.blocklist(space, hub, datasets)
    boxes = kit.mailboxes(space)
    own = {addr for _, addr in boxes if addr}
    people = _People(space)
    seen, cursor, read, mass = _gather(space, boxes, _cursor(space, hub, datasets), own,
                                       excl, block, people)
    minted, updated = _apply(space, people, seen, excl)
    kit.write_config(space, hub, datasets, "people.cursor", {"mail": cursor})
    linked = _link_promises(space, hub, datasets, people)
    return {"mailboxes": len(boxes), "messages": read, "minted": minted,
            "updated": updated, "promisesLinked": linked, "massSkipped": mass,
            **_link_identities(space, hub, datasets, excl)}
