"""Brain facts: what correspondents say about themselves, each with its sentence."""

__any_category__ = "plugin"  # the AI brain is a plugin of the connectors repo (ADR-033 §1)

# ADR-035. A cron job the brain's setup run writes (every 21600 s), args
# {space, hub, datasets, tier}. It is the one brain program that reads mail
# with a model, and it does nothing until the user allowed that in chat
# (`brain.model`, written by brainSetup.allow_model()). Then, per person
# with new mail: one
# model call over their newest mail, each message tagged [M1], [M2]…, and
# plain-code grounding of every proposed fact — its kind in KINDS, its ref a
# tagged message, its quote a verbatim substring of that message. A model
# that paraphrases loses the fact. Exclusions are read fail-closed before
# any mail; a message with an excluded or blocklisted participant is out for
# everyone in it. Facts are idempotent by id; a single-valued kind
# supersedes an inferred fact and never a stated one.

import hashlib
import re

kit = use("brainKit@v1")  # noqa: F821 - guest global
_any = use("agent:any@v1")  # noqa: F821 - guest global
llm = use("agent:llm@v1")  # noqa: F821 - guest global

CONSENT_KEY = "brain.model"
PERSON_PAGE = 200
MAX_PERSONS = 40
MAILS_PER_MAILBOX = 30
SOURCE_CHARS = 8000
MESSAGE_CHARS = 2400
QUOTE_MAX = 240
VALUE_MAX = 200
CONFIDENCE_CAP = 0.8
CONFIDENCE_MIN = 0.6
MORE_SOURCES_MAX = 5
MAX_TOKENS = 4000
DAY = 86400

KINDS = (
    "taste.food", "taste.drink", "taste.sport", "taste.team", "taste.pet",
    "view.opinion", "view.reading",
    "preference.channel", "preference.address_form", "preference.language",
    "preference.name", "preference.schedule",
    "personal.family", "personal.location", "personal.origin", "personal.language",
    "personal.date",
    "work.role", "work.company", "work.role_change", "work.board_seat",
    "work.invests_in", "work.expertise",
    "history.how_met", "history.worked_on", "history.asked_for", "history.consent",
)
# one current value per person; a new one supersedes the old
SINGLE_VALUED = frozenset({
    "preference.address_form", "preference.language", "preference.name",
    "preference.channel", "personal.location", "work.role", "work.company"})
PROPOSED_KINDS = frozenset({"history.consent"})

EXTRACT_SYS = (
    "You read a person's email and list durable facts about THAT person, for "
    "the user's private address book. Answer with ONLY a JSON array, no prose. "
    "Each item: {\"kind\": one of " + ", ".join(KINDS) + ", \"value\": the "
    "bare value, a few words and never a sentence (Lisbon, CTO, espresso, Ada), "
    "\"quote\": the exact sentence from the message that states it, copied "
    "character for character, \"ref\": the tag of that message such as "
    "\"M2\", \"confidence\": 0 to 1}. "
    "Only facts the person states about themselves, or the user states about "
    "them. Never paraphrase or fix the quote. A known fact that still holds "
    "keeps its value word for word. A new role, company, location, name, "
    "channel, language or form of address goes under that kind with the new "
    "value, which replaces the old one; work.role_change is only for the move "
    "itself, beside it. Skip greetings, signatures, scheduling chatter and "
    "anything about other people. No fact, no item: an empty array is a fine "
    "answer."
)


class unconfigured(Exception):
    """The job's args do not name the brain's facts store: the brain is not set up."""


class unpinned(Exception):
    """No model tier is configured for the extractor (llm.tier.<tier>)."""


class provider_failed(Exception):
    """The model provider failed twice in a row."""


def normalize(value):
    """A fact value as its identity compares it."""
    return re.sub(r"\s+", " ", str(value)).strip().lower().rstrip(".,;:!?")


def fact_id(person_id, kind, value):
    raw = f"{person_id}|{kind}|{normalize(value)}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def _ws(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _drop(stats, reason):
    stats["dropped"][reason] = stats["dropped"].get(reason, 0) + 1


def ground(items, sources, stats):
    """The proposed items that survive grounding, each with its source."""
    kept = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            _drop(stats, "badShape")
            continue
        kind, ref = it.get("kind"), str(it.get("ref") or "").strip("[] ")
        quote, value = _ws(it.get("quote")), _ws(it.get("value"))
        if kind not in KINDS:
            _drop(stats, "badKind")
        elif ref not in sources:
            _drop(stats, "badSource")
        elif not quote:
            _drop(stats, "noQuote")
        elif len(quote) > QUOTE_MAX or quote not in _ws(sources[ref]["text"]):
            _drop(stats, "notVerbatim")
        elif not value or len(value) > VALUE_MAX:
            _drop(stats, "badValue")
        else:
            try:
                confidence = min(float(it.get("confidence")), CONFIDENCE_CAP)
            except (TypeError, ValueError):
                confidence = -1.0
            if confidence < CONFIDENCE_MIN:
                _drop(stats, "lowConfidence")
                continue
            kept.append({"kind": kind, "value": value, "quote": quote,
                         "confidence": round(confidence, 2), "source": sources[ref]})
    stats["grounded"] += len(kept)
    return kept


def _pinned(tier):
    try:
        effect("config.get", {"key": f"llm.tier.{tier}"})  # noqa: F821 - guest global
    except EffectError as e:  # noqa: F821 - guest global
        raise unpinned(f"no llm.tier.{tier} row: set the extractor's model in "
                       "Settings → Model") from e


def _persons(space, excl, block, floor_s):
    """The persons with mail since `floor_s`, oldest contact first, minus the excluded.

    At most MAX_PERSONS, but never a cut inside a group that shares a
    contact time: the watermark moves to the last one read and the next
    run starts after it, so a cut there would pass the rest of the group."""
    # asked of the server in contact order, then id, a page at a time: a cap
    # on an unordered read drops people, and ties need a stable order
    out, skipped, offset = [], 0, 0
    since = {"$gt": instant(floor_s)}  # noqa: F821 - guest global
    while True:
        rows = _any.query_objects(space, filter={"any.collections": "brain_person",
                                                 "brain_person.lastContactAt": since},
                                  sort=["brain_person.lastContactAt", "id"],
                                  limit=PERSON_PAGE, offset=offset)
        for row in rows:
            bp = row.get("brain_person") or {}
            last = ts_s(bp.get("lastContactAt")) or 0  # noqa: F821 - guest global
            if len(out) >= MAX_PERSONS and last != out[-1]["last"]:
                return out, skipped
            emails = sorted({str(e).lower() for e in (bp.get("emails") or []) if e})
            if row["id"] in excl["personIds"] or any(kit.kept_out(e, excl, block)
                                                     for e in emails):
                skipped += 1
                continue
            if emails:
                out.append({"id": row["id"], "name": (row.get("any") or {}).get("name") or "",
                            "emails": emails, "last": last})
        if len(rows) < PERSON_PAGE:
            return out, skipped
        offset += PERSON_PAGE


def _sources(space, boxes, person, floor_s, excl, block, stats):
    """{ref: {text, header, link, observedAt}} — the person's newest mail."""
    mail = []
    for box_id, _addr in boxes:
        for row in _any.query(space, box_id, "email_messages",
                              filter={"participants": {"$in": person["emails"]},
                                      "internalDate": {"$gt": floor_s * 1000}},
                              sort=["-internalDate"], limit=MAILS_PER_MAILBOX):
            people = list(row.get("participants") or [])
            people += [a for _, a in kit.addresses(row.get("from"), row.get("to"),
                                                   row.get("cc"))]
            if kit.labelled_out(row, block) or any(kit.kept_out(str(a).lower(), excl, block)
                                                   for a in people):
                stats["excludedSkipped"] += 1
                continue
            mail.append((box_id, row))
    mail.sort(key=lambda m: m[1].get("internalDate") or 0, reverse=True)
    sources, used = {}, 0
    for box_id, row in mail:
        body = str(row.get("body") or row.get("snippet") or "")[:MESSAGE_CHARS]
        if not body.strip() or used + len(body) > SOURCE_CHARS:
            continue
        used += len(body)
        ref = f"M{len(sources) + 1}"
        sources[ref] = {
            "text": body,
            "header": (f"From: {row.get('from') or ''}\nTo: {row.get('to') or ''}\n"
                       f"Subject: {row.get('subject') or ''}"),
            "link": kit.mail_link(space, box_id, kit.plain_id(row["id"])),
            "observedAt": (row.get("internalDate") or 0) / 1000}
    return sources


def _prompt(person, current, sources):
    lines = [f"Person: {person['name'] or person['emails'][0]} "
             f"<{', '.join(person['emails'])}>", "Known facts:"]
    lines += [f"- {f['kind']}: {f['value']}" for f in current] or ["- none"]
    lines.append("Messages:")
    for ref, src in sources.items():
        lines += [f"[{ref}]", src["header"], "", src["text"], ""]
    return "\n".join(lines)


def _ask(prompt, tier):
    """The model's proposed items, or None after two unparsable replies."""
    for _attempt in range(2):
        reply = llm.chat([{"role": "user", "parts": [{"type": "text", "text": prompt}]}],
                         system=EXTRACT_SYS, tier=tier, tools=[], max_tokens=MAX_TOKENS)
        text = " ".join(p.get("text", "") for p in reply.get("parts") or []
                        if p.get("type") == "text")
        items = kit.parse_json(text)
        if isinstance(items, list):
            return items
    return None


def _counts_as_stated(fact):
    return fact.get("origin") == "stated" or bool(fact.get("confirmedAt"))


class _Writer:
    """Writes one person's grounded facts under the identity and supersession rules."""

    def __init__(self, space, hub, datasets, model, stats):
        self.space, self.hub, self.model, self.stats = space, hub, model, stats
        self.ds = datasets["facts"]
        self.signals = datasets.get("signals")

    def _modify(self, rid, ops, what):
        kit.checked(_any.modify(self.space, {"objectId": self.hub, "dataset": self.ds,
                                             "records": [{"id": rid, "ops": ops}]}), what)

    def _conflict(self, person, kind, stated, inferred):
        if not self.signals:
            return
        tick = now()  # noqa: F821 - guest global
        fid = fact_id(person["id"], kind, inferred)
        kit.upsert(self.space, self.hub, self.signals, f"{int(tick)}-fact_conflict-{fid[:8]}",
                   {"tick": tick, "kind": "fact_conflict",
                    "subject": f"any://o/{self.space}/{person['id']}", "decision": "none",
                    "reason": f"{kind}: stated '{stated}' vs inferred '{inferred}'"},
                   "writing a fact_conflict signal")
        self.stats["conflicts"] += 1

    def _more(self, have, item):
        src = item["source"]
        more = list(have.get("moreSources") or [])
        if have.get("source") == src["link"] or any(m.get("source") == src["link"]
                                                   for m in more):
            return False
        if len(more) >= MORE_SOURCES_MAX:
            return False
        more.append({"quote": item["quote"], "source": src["link"], "channel": "email",
                     "observedAt": instant(src["observedAt"])})  # noqa: F821
        self._modify(kit.plain_id(have["id"]), [
            {"type": "$set", "path": "moreSources", "value": more}], "adding a source")
        self.stats["moreSources"] += 1
        return True

    def write(self, person, items, existing):
        """existing: the person's facts by plain id."""
        current = {}
        for f in existing.values():
            if f.get("status") == "current":
                current.setdefault(f.get("kind"), []).append(f)
        written = 0
        for item in items:
            fid = fact_id(person["id"], item["kind"], item["value"])
            if fid in existing:
                written += self._more(existing[fid], item)
                continue
            replaced = None
            if item["kind"] in SINGLE_VALUED:
                olds = current.get(item["kind"]) or []
                stated = next((f for f in olds if _counts_as_stated(f)), None)
                if stated is not None:
                    self._conflict(person, item["kind"], stated.get("value"), item["value"])
                    continue
                replaced = next(iter(olds), None)
            src = item["source"]
            record = {"personId": person["id"], "kind": item["kind"], "value": item["value"],
                      "quote": item["quote"], "source": src["link"], "channel": "email",
                      "observedAt": instant(src["observedAt"]),  # noqa: F821
                      "origin": "inferred", "confidence": item["confidence"],
                      "model": self.model,
                      "status": "proposed" if item["kind"] in PROPOSED_KINDS else "current"}
            if replaced is not None:
                record["supersedes"] = kit.plain_id(replaced["id"])
            kit.upsert(self.space, self.hub, self.ds, fid, record, "writing a fact")
            existing[fid] = {**record, "id": fid}
            written += 1
            self.stats["written"] += 1
            if replaced is not None:
                self._modify(kit.plain_id(replaced["id"]), [
                    {"type": "$set", "path": "status", "value": "superseded"},
                    {"type": "$set", "path": "supersededBy", "value": fid}],
                    "superseding a fact")
                current[item["kind"]] = [existing[fid]]
                self.stats["superseded"] += 1
            elif item["kind"] in SINGLE_VALUED:
                current[item["kind"]] = [existing[fid]]
        return written


def _existing(space, hub, ds, person_id):
    rows = _any.query(space, hub, ds, filter={"personId": person_id}, limit=500)
    return {kit.plain_id(r["id"]): r for r in rows}


def _new_stats():
    return {"persons": 0, "grounded": 0, "written": 0, "superseded": 0, "moreSources": 0,
            "conflicts": 0, "excludedSkipped": 0, "parseFailed": 0, "providerFailed": 0,
            "dropped": {}}


def _merge_stats(space, hub, datasets, stats):
    """Add this run's counts to config/facts.stats (counts only, never names)."""
    total = kit.config_value(space, hub, datasets, "facts.stats") or {}
    for key, value in stats.items():
        if key == "dropped":
            dropped = dict(total.get("dropped") or {})
            for reason, n in value.items():
                dropped[reason] = dropped.get(reason, 0) + n
            total["dropped"] = dropped
        elif isinstance(value, (int, float)):
            total[key] = (total.get(key) or 0) + value
    kit.write_config(space, hub, datasets, "facts.stats", total)


def main(args):
    if not kit.model_allowed():
        return {"skipped": "consent"}
    parsed = kit.job_args(args, "facts")
    if parsed is None:
        raise unconfigured("brain facts needs space, hub and datasets — set the brain up")
    space, hub, datasets = parsed
    tier = (args or {}).get("tier") or "brain.extractor"
    excl = kit.exclusions(space, hub, datasets)          # fail closed, before any mail
    block = kit.blocklist(space, hub, datasets)
    _pinned(tier)
    cursor = kit.config_value(space, hub, datasets, "facts.cursor") or {}
    floor_s = cursor.get("watermark")
    if not isinstance(floor_s, (int, float)):
        floor_s = now() - kit.BACKFILL_DAYS * DAY  # noqa: F821 - guest global
    stats = _new_stats()
    persons, stats["excludedSkipped"] = _persons(space, excl, block, floor_s)
    boxes = kit.mailboxes(space)
    writer = _Writer(space, hub, datasets, llm.profile(tier)["model"], stats)
    watermark, blocked, failures = floor_s, False, 0
    try:
        for person in persons:
            sources = _sources(space, boxes, person, floor_s, excl, block, stats)
            done = True
            if sources:
                existing = _existing(space, hub, datasets["facts"], person["id"])
                current = [f for f in existing.values() if f.get("status") == "current"]
                try:
                    items = _ask(_prompt(person, current, sources), tier)
                    failures = 0
                except llm.LlmError as e:
                    failures += 1
                    stats["providerFailed"] += 1
                    if failures >= 2:
                        raise provider_failed("the model provider failed twice in a row") from e
                    items, done = None, False
                if items is None and done:
                    stats["parseFailed"] += 1
                if items is not None:
                    writer.write(person, ground(items, sources, stats), existing)
            stats["persons"] += 1
            # the watermark only passes persons whose mail was read through
            if done and not blocked:
                watermark = max(watermark, person["last"])
            else:
                blocked = True
    finally:
        kit.write_config(space, hub, datasets, "facts.cursor", {"watermark": watermark})
        _merge_stats(space, hub, datasets, stats)
    return stats
