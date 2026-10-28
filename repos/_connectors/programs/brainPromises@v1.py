"""Brain promises: what you committed to in sent mail, quoted verbatim."""

__any_category__ = "plugin"  # the AI brain is a plugin of the connectors repo (ADR-033 §1)

# ADR-034 §4. A cron job the brain's setup run writes (every 1800 s), args
# {space, hub, datasets, tier?}. It reads the user's SENT mail in Bao's
# space — messages whose From is the mailbox's own address — newer than
# its cursor (BACKFILL_DAYS back for a mailbox it has none for), and asks
# a model (the `classify` tier) for the commitments in each message:
# `[{quote, due}]`, the quote copied character for character. Plain code
# keeps only a quote that is a verbatim substring of the cleaned body
# (QUOTE_MIN..QUOTE_MAX chars): a paraphrase is dropped, never repaired.
# When a judge model is configured (ADR-030) one noul question per
# message gates the extraction call. The user's consent to reading mail
# with a model (`brain.model`, ADR-035 §1) covers this job: without it,
# nothing is read.
#
# A message to an excluded or blocklisted recipient, or under a
# blocklisted label, is never sent to the model. A message that already
# has a promise record (by `source`) is not re-extracted, so a re-run
# cannot mint a second quote for it. The record id is
# sha256(messageId, normalized quote), and only ids the store lacks are
# written: `status` is mutable, so re-sending a promise would reopen one
# the user or a completed task closed. The cursor passes only messages
# fully handled: a provider failure (LlmError or an EffectError — a
# timeout, a refused connection, a missing secret) stops the mailbox
# before its message, and a run in which no model call succeeded fails
# (provider_failed) — with one mailbox that is the first failure — so a
# provider that is down trips the runtime's breaker instead of passing,
# run after run, for a job that read nothing.
#
# `extract(text)` — the judge gate, the call, the retry and the grounding —
# is the one extraction path: this job's sent mail and brainChatScan@v1's
# own chat messages both go through it (ADR-039 §3).
#
# It also closes the loop with tasks: an open promise whose `taskId` names
# a catalog task the user completed is marked done.

import hashlib
import re

kit = use("brainKit@v1")  # noqa: F821 - guest global
_any = use("agent:any@v1")  # noqa: F821 - guest global
llm = use("agent:llm@v1")  # noqa: F821 - guest global

TIER = "classify"
PER_MAILBOX = 200
MAX_CALLS = 60          # model calls per run; the rest waits for the next run
MESSAGE_CHARS = 2400
QUOTE_MIN = 8
QUOTE_MAX = 240
MAX_TOKENS = 1200
JUDGE_MIN = 0.3         # P(commits) below this: the message is not extracted
DAY = 86400

EXTRACT_SYS = (
    "You read ONE email the user sent and list the commitments the user makes "
    "in it: things the user says they themselves will do, send, check, forward, "
    "introduce, arrange, call. Answer with ONLY a JSON array, no prose. Each "
    "item: {\"quote\": the exact sentence from the message that makes the "
    "commitment, copied character for character, never shortened or fixed, "
    "\"due\": \"YYYY-MM-DD\" when that sentence names a date or a day, else "
    "null}. Not a commitment: a question, a condition (if, unless, in case), "
    "a hypothetical (would, could), a negation, something already done, "
    "something another person will do, scheduling chatter, greetings, a "
    "signature. Any language. No commitment, no item: an empty array is a "
    "fine answer."
)
JUDGE_QUESTIONS = {
    "commits": {
        "type": "noul",
        "instructions": "Does the writer of this email commit to doing something "
                        "themselves later (send, check, forward, introduce, arrange, call)?",
        "criteria": {
            "true": "the writer promises a future action of their own",
            "false": "no commitment by the writer: questions, conditions, other "
                     "people's actions, pleasantries, things already done"}}}

_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
             "saturday": 5, "sunday": 6, "montag": 0, "dienstag": 1, "mittwoch": 2,
             "donnerstag": 3, "freitag": 4, "samstag": 5, "sonntag": 6}
_ISO_DAY = re.compile(r"^\s*(20\d\d)-(\d\d)-(\d\d)")


class unconfigured(Exception):
    """The job's args do not name the brain hub and its promises store."""


class cursor_corrupt(Exception):
    """config/promises.cursor is not in its {mailboxId: ms} shape."""


class promises_rejected(Exception):
    """The server refused promise records; the first reason is the message."""


class provider_failed(Exception):
    """The model provider failed, and no call of the run succeeded."""


class model_failed(Exception):
    """The provider failed one model call: llm@v1's LlmError, or an
    EffectError — a timeout, a refused connection, a missing secret."""


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


def due_hint(value):
    """The model's `due` ("YYYY-MM-DD") as a UTC day, or None for anything else."""
    m = _ISO_DAY.match(str(value or ""))
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return kit.civil_day(y, mo, d)


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


def _new_stats():
    return {"messages": 0, "sent": 0, "judged": 0, "skippedByJudge": 0, "asked": 0,
            "grounded": 0, "written": 0, "excludedSkipped": 0, "alreadyExtracted": 0,
            "parseFailed": 0, "providerFailed": 0, "dropped": {}}


def _is_sent(row, box_addr):
    sender = kit.addresses(row.get("from"))
    return bool(sender) and sender[0][1] == box_addr


def _readable(row, box_addr, excl, block):
    """True when a sent message may go to the model: not under a blocklisted
    label, and to nobody the exclusion list or the blocklist keeps out."""
    if kit.labelled_out(row, block):
        return False
    recipients = [a for _, a in kit.addresses(row.get("to"), row.get("cc")) if a != box_addr]
    return not any(kit.kept_out(a, excl, block) and not kit.is_noise(a) for a in recipients)


def _extracted(space, hub, dataset, links):
    """The `source` links that already have a promise record: not re-extracted,
    so a re-run cannot mint a second quote for the same message."""
    if not links:
        return set()
    rows = _any.query(space, hub, dataset, filter={"source": {"$in": links}}, limit=len(links))
    return {r.get("source") for r in rows}


def _count(stats, key, n=1):
    if stats is not None:
        stats[key] = stats.get(key, 0) + n


def _model(fn, *args, **kwargs):
    """One model call; any provider failure is model_failed, so it never reads
    as "no commitments"."""
    try:
        return fn(*args, **kwargs)
    except (llm.LlmError, EffectError) as e:  # noqa: F821 - guest global
        raise model_failed(f"{type(e).__name__}: {e}") from e


def _ask(body, gate, tier, stats):
    """(the model's raw items — [] when the judge says no, None when
    unparsable twice), model calls made. Raises model_failed."""
    used = 0
    if gate:
        answers = _model(llm.judge, body, JUDGE_QUESTIONS)
        used += 1
        _count(stats, "judged")
        p = ((answers.get("answers") or {}).get("commits") or {}).get("noul")
        if isinstance(p, (int, float)) and p < JUDGE_MIN:
            _count(stats, "skippedByJudge")
            return [], used
    for _attempt in range(2):
        reply = _model(llm.chat, [{"role": "user", "parts": [{"type": "text", "text": body}]}],
                       system=EXTRACT_SYS, tier=tier, tools=[], max_tokens=MAX_TOKENS)
        used += 1
        _count(stats, "asked")
        text = " ".join(p.get("text", "") for p in reply.get("parts") or []
                        if p.get("type") == "text")
        items = kit.parse_json(text)
        if isinstance(items, list):
            return items, used
    _count(stats, "parseFailed")
    return None, used


def _grounded(body, items, stats):
    """The items whose quote is a verbatim substring of `body`, within length,
    once each → [{quote, due}]; every drop counted by reason."""
    norm = kit.normalize_ws(body)
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            _drop(stats, "badShape")
            continue
        quote = kit.normalize_ws(it.get("quote"))
        if not quote:
            _drop(stats, "noQuote")
        elif not QUOTE_MIN <= len(quote) <= QUOTE_MAX:
            _drop(stats, "badLength")
        elif quote not in norm:
            _drop(stats, "notVerbatim")
        elif quote.lower() in seen:
            _drop(stats, "duplicate")
        else:
            seen.add(quote.lower())
            out.append({"quote": quote, "due": it.get("due")})
    _count(stats, "grounded", len(out))
    return out


def extract(text, tier=TIER, gate=None, stats=None):
    """The commitments the writer of `text` makes, grounded → (items, calls).

    `items` is [{quote, due}] — each quote a verbatim substring of `text`
    (whitespace normalized), QUOTE_MIN..QUOTE_MAX chars, once; `due` the
    model's date hint, unparsed — [] when the judge says no or nothing
    commits, None when the reply was unparsable twice. `calls` counts
    every model call, the judge's too. `gate` None asks llm@v1 whether a
    judge is configured. The caller checks the consent
    (kit.model_allowed()) first. Raises model_failed; `stats` gets the
    promise job's counters."""
    if gate is None:
        gate = bool(llm.judge_available())
    items, used = _ask(text, gate, tier, stats)
    if not items:
        return items, used
    return _grounded(text, items, stats), used


def _drop(stats, reason):
    if stats is not None:
        dropped = stats.setdefault("dropped", {})
        dropped[reason] = dropped.get(reason, 0) + 1


def _records(space, box_id, box_addr, row, items, people):
    """The promise records one message's grounded items yield."""
    recipients = [a for _, a in kit.addresses(row.get("to"), row.get("cc")) if a != box_addr]
    to = [a for _, a in kit.addresses(row.get("to")) if a != box_addr]
    person_email = to[0] if to else (recipients[0] if recipients else None)
    made_at = (row.get("internalDate") or 0) / 1000
    out = []
    for it in items:
        quote = it["quote"]
        fields = {"text": quote, "quote": quote,
                  "source": kit.mail_link(space, box_id, row["id"]),
                  "mailboxId": box_id, "channel": "email",
                  "madeAt": instant(made_at),  # noqa: F821 - guest global
                  "status": "open"}
        due = due_at(quote, made_at)
        if due is None:
            due = due_hint(it.get("due"))
        if due is not None:
            fields["dueAt"] = instant(due)  # noqa: F821 - guest global
        if person_email:
            fields["personEmail"] = person_email
            pid = _person_of(space, person_email, people)
            if pid:
                fields["personId"] = pid
        out.append({"id": promise_id(row["id"], quote), "fields": fields})
    return out


def _unwritten(space, hub, dataset, records):
    """The records whose id the store does not hold yet."""
    ids = [f"{hub}/{r['id']}" for r in records]
    rows = _any.query(space, hub, dataset, filter={"id": {"$in": ids}}, limit=len(ids))
    have = {kit.plain_id(r["id"]) for r in rows}
    return [r for r in records if r["id"] not in have]


def _write(space, hub, dataset, records):
    """Write the new records → how many landed; a refusal fails the run,
    except a deleted id, which is the user's answer."""
    records = _unwritten(space, hub, dataset, records) if records else []
    if not records:
        return 0
    reply = _any.upsert_records(space, hub, dataset, records) or {}
    deleted = [r for r in reply.get("rejections") or []
               if r.get("code") == "upsert.record_deleted"]
    rejected = [r for r in reply.get("rejections") or [] if r not in deleted]
    if rejected:
        raise promises_rejected(
            f"{len(rejected)} of {len(records)} rejected: "
            f"{rejected[0].get('reason') or rejected[0].get('code')}")
    return len(records) - len(deleted)


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


def _merge_stats(space, hub, datasets, stats):
    """Add this run's counts to config/promises.stats (counts only, never text)."""
    total = kit.config_value(space, hub, datasets, "promises.stats") or {}
    for key, value in stats.items():
        if key == "dropped":
            dropped = dict(total.get("dropped") or {})
            for reason, n in value.items():
                dropped[reason] = dropped.get(reason, 0) + n
            total["dropped"] = dropped
        elif isinstance(value, (int, float)):
            total[key] = (total.get(key) or 0) + value
    kit.write_config(space, hub, datasets, "promises.stats", total)


def main(args):
    if not kit.model_allowed():
        return {"skipped": "consent"}
    parsed = kit.job_args(args, "promises")
    if parsed is None:
        raise unconfigured("brain promises needs space, hub and datasets — set the brain up")
    space, hub, datasets = parsed
    tier = (args or {}).get("tier") or TIER
    excl = kit.exclusions(space, hub, datasets)          # fail closed
    block = kit.blocklist(space, hub, datasets)
    gate = bool(llm.judge_available())
    cursor = _cursor(space, hub, datasets)
    stats = _new_stats()
    people, calls, failures = {}, 0, []
    floor = kit.backfill_floor_ms()
    for box_id, box_addr in kit.mailboxes(space):
        last = cursor.get(box_id, floor)
        rows = _any.query(space, box_id, "email_messages",
                          filter={"internalDate": {"$gt": last}},
                          sort=["internalDate"], limit=PER_MAILBOX)
        sent = [r for r in rows if _is_sent(r, box_addr)]
        done = _extracted(space, hub, datasets["promises"],
                          [kit.mail_link(space, box_id, r["id"]) for r in sent])
        records = []
        for row in rows:
            if calls >= MAX_CALLS:
                break                       # the rest waits for the next run
            if _is_sent(row, box_addr):
                stats["sent"] += 1
                if not _readable(row, box_addr, excl, block):
                    stats["excludedSkipped"] += 1
                elif kit.mail_link(space, box_id, row["id"]) in done:
                    stats["alreadyExtracted"] += 1
                else:
                    body = kit.clean_body(row.get("body"), MESSAGE_CHARS)
                    if body:
                        try:
                            items, used = extract(body, tier, gate, stats)
                        except model_failed as e:
                            failures.append(e)
                            stats["providerFailed"] += 1
                            break               # the cursor stays before this message
                        calls += used
                        if items:
                            records.extend(_records(space, box_id, box_addr, row,
                                                    items, people))
            last = max(last, row.get("internalDate") or 0)
            stats["messages"] += 1
        stats["written"] += _write(space, hub, datasets["promises"], records)
        cursor[box_id] = last
    if failures and not (stats["asked"] or stats["judged"]):
        # nothing was handled: the cursor stays where the last run left it
        raise provider_failed(
            "the model provider failed and no call of this run succeeded") from failures[-1]
    kit.write_config(space, hub, datasets, "promises.cursor", cursor)
    _merge_stats(space, hub, datasets, stats)
    closed = _close_completed(space, hub, datasets)
    return {**stats, "promises": stats["written"], "closed": closed}
