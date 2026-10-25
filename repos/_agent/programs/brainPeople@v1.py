"""Brain people: one catalog person per real correspondent in Bao's mail."""

# ADR-034. A cron job the brain's setup run writes (every 900 s), args
# {space, hub, datasets}. It reads mail that gmailSync synced into Bao's
# space, newer than its cursor, and keeps one catalog person — a `profile`
# filed under `person` — per address, also filed under the brain's
# `brain_person` collection with every address used, the names seen, when
# we last spoke and how often. It never reads a body: From / To / Cc only.
# Excluded people, blocklisted senders and domains, noise senders and the
# mailbox's own addresses never become people. Deterministic; no model.
#
# Resolution order for an address: a person whose brain_person.emails holds
# it, then one whose person.email equals it, else a new person. A run
# processes at most MAX_NEW unknown addresses and PER_MAILBOX messages per
# mailbox; the cursor advances only past messages fully handled, so a
# capped run resumes where it stopped. It then back-fills `personId` on
# promises whose recipient it now knows.

kit = use("brainKit@v1")  # noqa: F821 - guest global
_any = use("any@v1")  # noqa: F821 - guest global

MAX_NEW = 300
PER_MAILBOX = 200
PROMISE_BACKFILL = 200


class unconfigured(Exception):
    """The job's args do not name the brain hub: the brain is not set up."""


class cursor_corrupt(Exception):
    """config/people.cursor is not in its {mail: {mailboxId: ms}} shape."""


def _cursor(space, hub, datasets):
    value = kit.config_value(space, hub, datasets, "people.cursor")
    if value is None:
        return {}
    mail = value.get("mail") if isinstance(value, dict) else None
    if not isinstance(mail, dict) or not all(isinstance(v, (int, float)) for v in mail.values()):
        raise cursor_corrupt(f"people.cursor is {value!r}")
    return dict(mail)


class _People:
    """The space's brain persons, by address, loaded once per run."""

    def __init__(self, space):
        self.space = space
        self.by_email = {}
        self.rows = {}
        for r in _any.query_objects(space, filter={"any.collections": "brain_person"},
                                    limit=1000):
            self._index(r)

    def _index(self, row):
        self.rows[row["id"]] = row
        for e in ((row.get("brain_person") or {}).get("emails") or []):
            self.by_email[str(e).lower()] = row["id"]

    def find(self, addr):
        """(person id, filed under brain_person) or (None, False)."""
        if addr in self.by_email:
            return self.by_email[addr], True
        hits = _any.query_objects(self.space, filter={"person.email": addr}, limit=1)
        if hits:
            return hits[0]["id"], False
        return None, False


def _gather(space, boxes, cursor, own, excl, block, people):
    """Walk new mail → ({addr: seen}, new cursor, messages read)."""
    seen, read, unknown = {}, 0, set()
    for box_id, _addr in boxes:
        last = cursor.get(box_id, 0)
        rows = _any.query(space, box_id, "email_messages",
                          filter={"internalDate": {"$gt": last}},
                          sort=["internalDate"], limit=PER_MAILBOX)
        for row in rows:
            people_here = [(n, a) for n, a in kit.addresses(row.get("from"), row.get("to"),
                                                           row.get("cc"))
                           if a not in own and not kit.kept_out(a, excl, block)]
            fresh = {a for _, a in people_here
                     if a not in people.by_email and a not in seen and a not in unknown}
            if len(unknown) + len(fresh) > MAX_NEW:
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
            last = max(last, row.get("internalDate") or 0)
            read += 1
        cursor[box_id] = last
    return seen, cursor, read


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
        _any.update_object(space, pid, {"brain_person": _merged(current, addr, s)})
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
        pid = people.by_email.get(str(r.get("personEmail") or "").lower())
        if pid:
            kit.checked(_any.modify(space, {"objectId": hub, "dataset": datasets["promises"],
                        "records": [{"id": kit.plain_id(r["id"]), "ops": [
                            {"type": "$set", "path": "personId", "value": pid}]}]}),
                        "linking a promise to its person")
            linked += 1
    return linked


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
    seen, cursor, read = _gather(space, boxes, _cursor(space, hub, datasets), own, excl,
                                 block, people)
    minted, updated = _apply(space, people, seen, excl)
    kit.write_config(space, hub, datasets, "people.cursor", {"mail": cursor})
    linked = _link_promises(space, hub, datasets, people)
    return {"mailboxes": len(boxes), "messages": read, "minted": minted,
            "updated": updated, "promisesLinked": linked}
