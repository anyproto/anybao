"""Brain promises: what you committed to in sent mail, quoted verbatim."""

# ADR-034. A cron job the brain's setup run writes (every 1800 s), args
# {space, hub, datasets}. It reads the user's SENT mail in Bao's space —
# messages whose From is the mailbox's own address — newer than its cursor,
# splits each cleaned body into sentences and keeps the ones that commit
# the user to something (PHRASES, minus EXCLUSIONS). Each becomes one
# `promises` record whose `quote` is the verbatim sentence and whose
# `source` opens the message. Deterministic; no model. A message to an
# excluded or blocklisted recipient, or under a blocklisted label, is
# skipped whole. The record id is sha256(messageId, normalized sentence),
# and only ids the store lacks are written: `status` is mutable, so
# re-sending a promise a re-read message yields would reopen it.
#
# It also closes the loop with tasks: an open promise whose `taskId` names
# a catalog task the user completed is marked done.

import hashlib
import re

kit = use("brainKit@v1")  # noqa: F821 - guest global
_any = use("any@v1")  # noqa: F821 - guest global

PER_MAILBOX = 200
MAX_SENTENCE = 300
MIN_SENTENCE = 8
MAX_QUOTE = 240
DAY = 86400

# A promise names the writer as the one acting. Patterns are matched on a
# lowercased sentence at word boundaries.
PHRASES = {
    "en": [r"\bi will\b", r"\bi'll\b", r"\bi’ll\b", r"\blet me (send|share|check|get back)\b",
           r"\bwill do\b", r"\bi'm going to\b", r"\bi am going to\b"],
    "de": [r"\bich schicke\b", r"\bich werde\b", r"\bich melde mich\b", r"\bich sende\b",
           r"\bschicke ich\b", r"\bmelde ich mich\b", r"\bich leite\b.*\bweiter\b",
           r"\bich stelle\b.*\bvor\b", r"\bich kümmere mich\b"],
}
# A sentence with any of these is not a promise: a question, a condition,
# a hypothetical, or a negation anywhere in it (stricter than a negation
# next to the verb: a missed promise is cheap, a false one is not).
EXCLUSIONS = [r"\?\s*$", r"\bif\b", r"\bunless\b", r"\bin case\b", r"\bwould\b", r"\bcould\b",
              r"\bfalls\b", r"\bwenn\b", r"\bsobald\b", r"\bkönnte\b", r"\bwürde\b",
              r"\bwon't\b", r"\bwon’t\b", r"\bwill not\b", r"\bi'll not\b", r"\bnicht\b"]
_PHRASES = [re.compile(p) for ps in PHRASES.values() for p in ps]
_EXCLUSIONS = [re.compile(p) for p in EXCLUSIONS]
_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
             "saturday": 5, "sunday": 6, "montag": 0, "dienstag": 1, "mittwoch": 2,
             "donnerstag": 3, "freitag": 4, "samstag": 5, "sonntag": 6}


class unconfigured(Exception):
    """The job's args do not name the brain hub and its promises store."""


class cursor_corrupt(Exception):
    """config/promises.cursor is not in its {mailboxId: ms} shape."""


class promises_rejected(Exception):
    """The server refused promise records; the first reason is the message."""


def sentences(body):
    """The candidate sentences of a cleaned body, quoted lines dropped."""
    lines = [ln for ln in (body or "").splitlines() if not ln.lstrip().startswith(">")]
    out = []
    for part in _SPLIT.split("\n".join(lines)):
        s = " ".join(part.split())
        if MIN_SENTENCE <= len(s) <= MAX_SENTENCE:
            out.append(s)
    return out


def is_promise(sentence):
    low = sentence.lower()
    return any(p.search(low) for p in _PHRASES) and not any(p.search(low) for p in _EXCLUSIONS)


def _day(seconds):
    return int(seconds // DAY) * DAY


def due_at(sentence, made_at):
    """A deterministic deadline from the sentence, in UTC days, or None."""
    low = sentence.lower()
    day = _day(made_at)
    weekday = int((day // DAY + 3) % 7)          # 1970-01-01 was a Thursday
    if re.search(r"\b(today|tonight|heute)\b", low):
        return day
    if re.search(r"\btomorrow\b|(?<!guten )\bmorgen\b", low):
        return day + DAY
    if re.search(r"\b(end of (the )?week|ende der woche)\b", low):
        return day + max(0, 4 - weekday) * DAY               # this week's Friday
    if re.search(r"\b(next week|nächste woche|naechste woche)\b", low):
        return day + (7 - weekday + 4) * DAY                  # next week's Friday
    for name, target in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", low):
            return day + (((target - weekday) % 7) or 7) * DAY
    return None


def promise_id(message_id, text):
    return hashlib.sha256(f"{message_id}\n{text.lower()}".encode()).hexdigest()[:32]


def _cursor(space, hub, datasets):
    value = kit.config_value(space, hub, datasets, "promises.cursor")
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(v, (int, float)) for v in value.values()):
        raise cursor_corrupt(f"promises.cursor is {value!r}")
    return dict(value)


def _person_of(space, addr, cache):
    if addr not in cache:
        hits = _any.query_objects(space, filter={"brain_person.emails": addr}, limit=1)
        cache[addr] = hits[0]["id"] if hits else None
    return cache[addr]


def _records_of(space, box_id, box_addr, row, excl, block, people):
    """The promise records one sent message yields (may be none)."""
    sender = kit.addresses(row.get("from"))
    if not sender or sender[0][1] != box_addr or kit.labelled_out(row, block):
        return []
    recipients = [a for _, a in kit.addresses(row.get("to"), row.get("cc")) if a != box_addr]
    if any(kit.kept_out(a, excl, block) and not kit.is_noise(a) for a in recipients):
        return []                       # excluded or blocklisted: never read
    made_at = (row.get("internalDate") or 0) / 1000
    to = [a for _, a in kit.addresses(row.get("to")) if a != box_addr]
    person_email = to[0] if to else (recipients[0] if recipients else None)
    out = []
    for s in sentences(row.get("body")):
        if not is_promise(s):
            continue
        fields = {"text": s, "quote": s[:MAX_QUOTE],
                  "source": kit.mail_link(space, box_id, row["id"]),
                  "mailboxId": box_id, "channel": "email",
                  "madeAt": instant(made_at),  # noqa: F821 - guest global
                  "status": "open"}
        due = due_at(s, made_at)
        if due is not None:
            fields["dueAt"] = instant(due)  # noqa: F821 - guest global
        if person_email:
            fields["personEmail"] = person_email
            pid = _person_of(space, person_email, people)
            if pid:
                fields["personId"] = pid
        out.append({"id": promise_id(row["id"], s), "fields": fields})
    return out


def _unwritten(space, hub, dataset, records):
    """The records whose id the store does not hold yet."""
    ids = [f"{hub}/{r['id']}" for r in records]
    rows = _any.query(space, hub, dataset, filter={"id": {"$in": ids}}, limit=len(ids))
    have = {kit.plain_id(r["id"]) for r in rows}
    return [r for r in records if r["id"] not in have]


def _close_completed(space, hub, datasets):
    """Mark done the open promises whose task the user completed.

    The tasks are found by the ids the promises hold, wherever they are filed:
    a task made from the Promises view is not filed under brain_task."""
    rows = _any.query(space, hub, datasets["promises"], filter={"status": "open"}, limit=500)
    linked = sorted({r["taskId"] for r in rows if r.get("taskId")})
    if not linked:
        return 0
    tasks = _any.query_objects(space, filter={"id": {"$in": linked}}, limit=len(linked))
    done_ids = {t["id"] for t in tasks if (t.get("task") or {}).get("completed") is True}
    closed = 0
    for r in rows:
        if r.get("taskId") in done_ids:
            kit.checked(_any.modify(space, {"objectId": hub, "dataset": datasets["promises"],
                        "records": [{"id": kit.plain_id(r["id"]), "ops": [
                            {"type": "$set", "path": "status", "value": "done"},
                            {"type": "$set", "path": "reason", "value": "task completed"},
                            {"type": "$set", "path": "closedAt",
                             "value": instant(now())}]}]}),  # noqa: F821
                        "closing a promise whose task is done")
            closed += 1
    return closed


def main(args):
    parsed = kit.job_args(args, "promises")
    if parsed is None:
        raise unconfigured("brain promises needs space, hub and datasets — set the brain up")
    space, hub, datasets = parsed
    excl = kit.exclusions(space, hub, datasets)          # fail closed
    block = kit.blocklist(space, hub, datasets)
    cursor = _cursor(space, hub, datasets)
    people, found, read = {}, 0, 0
    for box_id, box_addr in kit.mailboxes(space):
        last = cursor.get(box_id, 0)
        rows = _any.query(space, box_id, "email_messages",
                          filter={"internalDate": {"$gt": last}},
                          sort=["internalDate"], limit=PER_MAILBOX)
        records = []
        for row in rows:
            records.extend(_records_of(space, box_id, box_addr, row, excl, block, people))
            last = max(last, row.get("internalDate") or 0)
            read += 1
        records = _unwritten(space, hub, datasets["promises"], records) if records else []
        if records:
            reply = _any.upsert_records(space, hub, datasets["promises"], records) or {}
            rejected = reply.get("rejections") or []
            if rejected:
                raise promises_rejected(
                    f"{len(rejected)} of {len(records)} rejected: "
                    f"{rejected[0].get('reason') or rejected[0].get('code')}")
            found += len(records)
        cursor[box_id] = last
    kit.write_config(space, hub, datasets, "promises.cursor", cursor)
    closed = _close_completed(space, hub, datasets)
    return {"messages": read, "promises": found, "closed": closed}
