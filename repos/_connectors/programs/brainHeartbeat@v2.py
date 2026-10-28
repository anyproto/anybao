"""Brain heartbeat: notice mail, stale threads, promises, meetings; ask in cards."""

__any_category__ = "plugin"  # the AI brain is a plugin of the connectors repo (ADR-033 §1)

# ADR-036. The cron job the brain's setup run points at this program
# (every 600 s, the same record v1 ran in), args {space, hub, datasets,
# tier}. Deterministic: it imports no llm@v1, no webSearch, no connector,
# and reads no mail body. Per run, in order:
#   0. the v1 probes: a missing or unreadable exclusion list writes one
#      refused signal and raises; a tick more than GAP_S after the last
#      one writes a heartbeat_gap signal
#   1. snoozed cards whose snoozedUntil passed open again (card_woken)
#   2. new mail per mailbox since its cursor → one signal per inbound
#      message with a decision and a reason, and a `reply` card for mail
#      addressed to the user with no reply from them yet
#   3. stale threads (the user wrote last, no answer for replyWindowDays)
#      → a `nudge` card, once per thread
#   4. open promises past their dueAt → an `overdue_promise` card
#   5. calendar events within meetingSoonHours with a known attendee, and
#      overlapping events in the next week → `meeting_soon` and
#      `calendar_conflict` cards (no-ops until calendar sync declares
#      calendar_events on the hub)
#   6. fact_conflict signals the facts job wrote → a `fact_conflict` card
#   7. reply cards with no draft yet → a once trigger for brainDrafter@v1,
#      at most maxPerRun a run and MAX_ATTEMPTS a card, only while the user
#      allows reading mail with a model; the heartbeat owns the attempt
#      counter and every trigger id is fresh and existence-checked
#   8. one chat notice under the agent's name when cards were created, one
#      standups row when something was read or written, then the cursors
# Mail from an excluded or blocklisted participant gets one `refused`
# signal and nothing else: the drafter drops a thread message carrying
# such a signal, so mail that arrived while someone was excluded is never
# read later. A card's id is sha256(kind | identity), so a re-run writes
# no second card; a card that exists is never rewritten, since its status
# is the user's answer.

import hashlib
import re

kit = use("brainKit@v1")  # noqa: F821 - guest global
_any = use("agent:any@v1")  # noqa: F821 - guest global

SUBJECT = "connectors:brainHeartbeat@v2"
DRAFTER = "connectors:brainDrafter@v1"
TRIGGERS = "agent_triggers"
GAP_S = 1200
DAY = 86400
PER_MAILBOX = 200          # messages read per mailbox and pass, per run
PER_PASS = 200             # promises, events, signals read per pass
THREAD_ROWS = 20
MAX_ATTEMPTS = 3           # one-shots armed per reply card, ever
STALE_LOOKBACK_DAYS = 14   # how far behind the reply window a first run looks
CONFLICT_DAYS = 7
MAX_CONFLICTS = 20
NOTICE_TITLES = 5
TITLE_MAX = 200
# brain notices post under the literal agent name, as remind@v1 does: the
# watcher skips its own name, so the notice starts no chat turn (ADR-033)
AGENT_NAME = "bao"
OPEN = ("open", "snoozed")
REPLY_REASON = "addressed to you, no reply yet"
DRAFTING = {"maxPerRun": 3, "replyWindowDays": 5, "meetingSoonHours": 24, "quietSenders": []}

_MAIL = re.compile(r"/([^/]+)/email_messages/([^/]+)$")
_PERSON = re.compile(r"^any://o/[^/]+/([^/]+)$")


class unconfigured(Exception):
    """The job's args do not name the brain hub and its card store."""


class cursor_corrupt(Exception):
    """config/heartbeat.cursor is not in its {mail, stale, factConflicts} shape."""


# --- pure decisions ---------------------------------------------------------

def card_id(kind, identity):
    """A card's id: sha256 of "<kind>|<identity>", hex (ADR-036 §3)."""
    return hashlib.sha256(f"{kind}|{identity}".encode()).hexdigest()


def reply_card_id(thread_id, message_id):
    # a reply card's identity is its thread and the message it answers, so a
    # later message in a thread whose card is closed gets a card of its own
    return card_id("reply", f"{thread_id}|{message_id}")


def signal_id(tick, kind, subject):
    """A signal's id: one per (tick, kind, subject)."""
    return f"{int(tick)}-{kind}-{hashlib.sha256(subject.encode()).hexdigest()[:12]}"


def draft_trigger_id(message_id, attempt):
    """The once trigger for a reply's nth draft attempt: brain-draft-<id>, -a2, -a3."""
    return f"brain-draft-{message_id}" if attempt <= 1 else f"brain-draft-{message_id}-a{attempt}"


def sender_of(row):
    found = kit.addresses(row.get("from"))
    return found[0][1] if found else ""


def quiet_patterns(values):
    """(compiled regexes, how many entries did not compile) from quietSenders."""
    out, bad = [], 0
    for v in values if isinstance(values, list) else []:
        try:
            out.append(re.compile(str(v), re.I))
        except re.error:
            bad += 1
    return out, bad


def decide_mail(message, own_address, replied, quiet, kept_out=False):
    """One message in the user's mailbox → (signal kind, decision, reason,
    needs a reply card), or None for the user's own mail.

    `replied`: a newer message in the thread is from the user. `quiet`:
    compiled quietSenders. `kept_out`: an excluded or blocklisted
    participant, or a blocklisted label."""
    if kept_out:
        return ("new_mail", "refused", "kept out of the brain", False)
    sender = sender_of(message)
    if sender == own_address:
        return None
    if not sender or kit.is_noise(sender) or any(q.search(sender) for q in quiet):
        return ("new_mail", "quiet", "automated sender", False)
    to = {a for _, a in kit.addresses(message.get("to"))}
    cc = {a for _, a in kit.addresses(message.get("cc"))}
    if own_address in to:
        if replied:
            return ("new_mail", "none", "already replied", False)
        return ("new_mail", "draft", REPLY_REASON, True)
    if own_address in cc:
        return ("cc_thread", "quiet", "copied, not addressed", False)
    return ("new_mail", "quiet", "not addressed to you", False)


def _at(row):
    return row.get("internalDate") or 0


def replied_after(thread, message, own_address):
    """True when the thread holds a message from the user newer than `message`."""
    return any(_at(r) > _at(message) and sender_of(r) == own_address for r in thread)


def is_newest(thread, message):
    """True when no message in the thread is newer than `message`."""
    return not any(_at(r) > _at(message) for r in thread)


def overlapping(events):
    """[(a, b)] of events whose [start, end) overlap; a starts first. Each event
    is {id, start, end} in seconds."""
    ordered = sorted(events, key=lambda e: (e["start"], e["id"]))
    pairs = []
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if b["start"] >= a["end"]:
                break
            pairs.append((a, b))
    return pairs


# --- the run ------------------------------------------------------------------

def _drafting(value):
    out = dict(DRAFTING)
    for key, default in DRAFTING.items():
        v = (value or {}).get(key) if isinstance(value, dict) else None
        if isinstance(default, int) and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[key] = max(0, int(v))
        elif isinstance(default, list) and isinstance(v, list):
            out[key] = list(v)
    return out


def _cursor(space, hub, datasets):
    value = kit.config_value(space, hub, datasets, "heartbeat.cursor")
    if value is None:
        return {"mail": {}, "stale": {}, "factConflicts": 0}
    ok = isinstance(value, dict) and all(
        isinstance(value.get(k, {}), dict)
        and all(isinstance(v, (int, float)) for v in value.get(k, {}).values())
        for k in ("mail", "stale")) and isinstance(value.get("factConflicts", 0), (int, float))
    if not ok:
        raise cursor_corrupt(f"heartbeat.cursor is {value!r}")
    return {"mail": dict(value.get("mail") or {}), "stale": dict(value.get("stale") or {}),
            "factConflicts": value.get("factConflicts") or 0}


def _day(seconds):
    return fmt_ts(seconds, "%Y-%m-%d", 0)[:10]  # noqa: F821 - guest global


def _when(seconds):
    return fmt_ts(seconds, "%Y-%m-%d %H:%M", 0)[:16] + " UTC"  # noqa: F821 - guest global


class Run:
    """One heartbeat tick: what it saw, the cards and signals it will write."""

    def __init__(self, space, hub, datasets, tick, args):
        self.space, self.hub, self.ds, self.tick = space, hub, datasets, tick
        self.args = args or {}
        self.signals = {}          # id -> fields
        self.new_cards = {}        # id -> fields, in creation order
        self.reply_signal = {}     # new reply card id -> its signal id
        self.not_drafted = {}      # reply card id -> why no draft this run
        self.armed = {}            # reply card id -> trigger id
        self.counts = {"mails": 0, "quiet": 0, "refused": 0, "none": 0, "covered": 0,
                       "replyCards": 0, "nudges": 0, "overduePromises": 0,
                       "meetingsSoon": 0, "conflicts": 0, "factConflicts": 0, "woken": 0,
                       "draftsArmed": 0, "badQuietSenders": 0, "noticeFailed": 0}
        self.needs = []
        self.extra_did = []        # standup lines a later version's passes add
        self._exists = {}
        self._people = {}
        self._threads = {}
        self._anchor = None
        self.boxes = dict(kit.mailboxes(space))

    # helpers
    def link(self, dataset, rid):
        return f"any://o/{self.space}/{self.hub}/{dataset}/{rid}"

    def signal(self, kind, subject, decision, reason, card=None):
        sid = signal_id(self.tick, kind, subject)
        fields = {"tick": self.tick, "kind": kind, "subject": subject,
                  "decision": decision, "reason": reason}
        if card:
            fields["cardId"] = card
        self.signals[sid] = fields
        if decision in self.counts:
            self.counts[decision] += 1
        return sid

    def card_exists(self, cid):
        if cid in self.new_cards:
            return True
        if cid not in self._exists:
            rows = _any.query(self.space, self.hub, self.ds["cards"],
                              filter={"id": f"{self.hub}/{cid}"}, limit=1)
            self._exists[cid] = bool(rows)
        return self._exists[cid]

    def add_card(self, cid, fields):
        fields.setdefault("status", "open")
        fields["title"] = str(fields.get("title") or "(no subject)")[:TITLE_MAX]
        self.new_cards[cid] = fields

    def person(self, addr):
        if addr not in self._people:
            hits = _any.query_objects(self.space, filter={"brain_person.emails": addr}, limit=1)
            self._people[addr] = hits[0]["id"] if hits else None
        return self._people[addr]

    def thread(self, box, row):
        tid = row.get("threadId")
        if not tid:
            return [row]
        key = (box, tid)
        if key not in self._threads:
            self._threads[key] = _any.query(self.space, box, "email_messages",
                                            filter={"threadId": tid},
                                            sort=["-internalDate"], limit=THREAD_ROWS)
        return self._threads[key]

    def modify_card(self, cid, ops, what):
        kit.checked(_any.modify(self.space, {"objectId": self.hub, "dataset": self.ds["cards"],
                                             "records": [{"id": cid, "ops": [
                                                 {"type": "$set", "path": p, "value": v}
                                                 for p, v in ops]}]}), what)

    def anchor(self):
        if self._anchor is None:
            self._anchor = _any.bundle_child(self.space, "bao/v1", "bao/triggers/v1")["objectId"]
        return self._anchor

    # 1. standing decisions
    def wake_snoozed(self):
        rows = _any.query(self.space, self.hub, self.ds["cards"],
                          filter={"status": "snoozed",
                                  "snoozedUntil": {"$lte": instant(self.tick)}},  # noqa: F821
                          limit=PER_PASS)
        for row in rows:
            cid = kit.plain_id(row["id"])
            self.modify_card(cid, [("status", "open")], "waking a snoozed card")
            until = ts_s(row.get("snoozedUntil"))  # noqa: F821 - guest global
            self.signal("card_woken", self.link("cards", cid), "card",
                        f"snoozed until {_when(until)}" if until else "snooze over", cid)
            self.counts["woken"] += 1

    # 2. the mail reader
    def read_mail(self, excl, block, cursor, quiet, window_days):
        floor = int((self.tick - window_days * DAY) * 1000)
        for box, own in self.boxes.items():
            last = cursor.get(box, floor)
            rows = _any.query(self.space, box, "email_messages",
                              filter={"internalDate": {"$gt": last}},
                              sort=["internalDate"], limit=PER_MAILBOX)
            needing = []
            for row in rows:
                last = max(last, _at(row))
                self.counts["mails"] += 1
                link = kit.mail_link(self.space, box, kit.plain_id(row["id"]))
                kept = kit.participant_kept_out(row, excl, block)
                verdict = decide_mail(row, own, False, quiet, kept)
                if verdict is not None and verdict[3] and replied_after(
                        self.thread(box, row), row, own):
                    verdict = decide_mail(row, own, True, quiet, kept)
                if verdict is None:
                    continue
                kind, decision, reason, needs_card = verdict
                if needs_card:
                    needing.append(row)
                else:
                    self.signal(kind, link, decision, reason)
            self._reply_cards(box, needing)
            cursor[box] = last

    def _reply_cards(self, box, rows):
        newest = {}
        for row in rows:
            tid = row.get("threadId") or kit.plain_id(row["id"])
            if tid not in newest or _at(row) > _at(newest[tid]):
                newest[tid] = row
        for row in rows:
            mid = kit.plain_id(row["id"])
            tid = row.get("threadId") or mid
            link = kit.mail_link(self.space, box, mid)
            if newest[tid] is not row:
                self.signal("new_mail", link, "none",
                            "a newer message in the thread needs the reply")
                continue
            cards = _any.query(self.space, self.hub, self.ds["cards"],
                               filter={"kind": "reply", "subjectKey": tid}, limit=THREAD_ROWS)
            covering = [c for c in cards if c.get("status") in OPEN]
            if covering:
                self.signal("new_mail", link, "card", "an open reply card covers this thread",
                            kit.plain_id(covering[0]["id"]))
                self.counts["covered"] += 1
                continue
            cid = reply_card_id(tid, mid)
            if self.card_exists(cid):
                continue
            sid = self.signal("new_mail", link, "draft", REPLY_REASON, cid)
            pid = self.person(sender_of(row))
            self.add_card(cid, {"kind": "reply", "title": row.get("subject"),
                                "reason": REPLY_REASON, "subjectKey": tid, "sourceUri": link,
                                "signalId": sid, "personIds": [pid] if pid else [],
                                "draftAttempts": 0})
            self.reply_signal[cid] = sid
            self.counts["replyCards"] += 1

    # 3. stale threads
    def stale_threads(self, excl, block, cursor, window_days):
        upto = int((self.tick - window_days * DAY) * 1000)
        floor = int((self.tick - (window_days + STALE_LOOKBACK_DAYS) * DAY) * 1000)
        for box, own in self.boxes.items():
            last = cursor.get(box, floor)
            if last >= upto:
                continue
            rows = _any.query(self.space, box, "email_messages",
                              filter={"internalDate": {"$gt": last, "$lte": upto}},
                              sort=["internalDate"], limit=PER_MAILBOX)
            for row in rows:
                last = max(last, _at(row))
                if sender_of(row) != own or kit.participant_kept_out(row, excl, block):
                    continue
                to = [a for _, a in kit.addresses(row.get("to")) if a != own]
                pid = next((p for p in (self.person(a) for a in to) if p), None)
                if pid is None or not is_newest(self.thread(box, row), row):
                    continue
                mid = kit.plain_id(row["id"])
                tid = row.get("threadId") or mid
                cid = card_id("nudge", tid)
                if self.card_exists(cid):
                    continue
                days = int((self.tick - _at(row) / 1000) // DAY)
                reason = f"you wrote last, no answer for {days} days"
                link = kit.mail_link(self.space, box, mid)
                sid = self.signal("stale_thread", link, "card", reason, cid)
                self.add_card(cid, {"kind": "nudge", "title": row.get("subject"),
                                    "reason": reason, "subjectKey": tid, "sourceUri": link,
                                    "signalId": sid, "personIds": [pid]})
                self.counts["nudges"] += 1
            cursor[box] = last

    # 4. overdue promises
    def overdue_promises(self, excl, block):
        if not self.ds.get("promises"):
            return
        rows = _any.query(self.space, self.hub, self.ds["promises"],
                          filter={"status": "open",
                                  "dueAt": {"$lt": instant(self.tick)}},  # noqa: F821
                          limit=PER_PASS)
        kept = kit.promises_kept_out(self.space, rows, excl, block) if rows else set()
        for row in rows:
            pid = kit.plain_id(row["id"])
            cid = card_id("overdue_promise", pid)
            if pid in kept or self.card_exists(cid):
                continue
            due = ts_s(row.get("dueAt"))  # noqa: F821 - guest global
            reason = f"due {_day(due)}" if due else "past its deadline"
            sid = self.signal("overdue_promise", self.link("promises", pid), "card", reason, cid)
            person = row.get("personId")
            self.add_card(cid, {"kind": "overdue_promise", "title": row.get("text"),
                                "reason": reason, "subjectKey": pid,
                                "sourceUri": row.get("source") or self.link("promises", pid),
                                "signalId": sid, "personIds": [person] if person else []})
            self.counts["overduePromises"] += 1

    # 5. calendar
    def _events(self, excl, block, hours):
        if not self.ds.get("calendar_events"):
            return []
        rows = _any.query(self.space, self.hub, self.ds["calendar_events"],
                          filter={"start": {"$gte": instant(self.tick),  # noqa: F821
                                            "$lte": instant(self.tick + hours * 3600)}},  # noqa: F821
                          sort=["start"], limit=PER_PASS)
        out = []
        for row in rows:
            if str(row.get("status") or "").lower() == "cancelled":
                continue
            emails = [str(e).lower() for e in row.get("attendees") or []]
            ids = [str(i) for i in row.get("attendeeIds") or []]
            if any(i in excl["personIds"] for i in ids) or any(
                    kit.kept_out(e, excl, block) and not kit.is_noise(e) for e in emails):
                continue
            start = ts_s(row.get("start"))  # noqa: F821 - guest global
            end = ts_s(row.get("end")) or start  # noqa: F821 - guest global
            if start is None:
                continue
            out.append({"id": kit.plain_id(row["id"]), "start": start, "end": end,
                        "title": row.get("title") or "(no title)", "attendeeIds": ids,
                        "source": row.get("source")})
        return out

    def meetings_soon(self, excl, block, hours):
        events = self._events(excl, block, hours)
        known = {}
        wanted = sorted({i for e in events for i in e["attendeeIds"]})
        if wanted:
            for r in _any.query_objects(self.space, filter={"id": {"$in": wanted}},
                                        limit=len(wanted)):
                if any(kit.kept_out(e, excl, block) for e in kit.person_emails(r)):
                    continue        # a person kept out by address is no attendee here
                known[r["id"]] = (r.get("any") or {}).get("name") or r["id"]
        for ev in events:
            people = [i for i in ev["attendeeIds"] if i in known]
            cid = card_id("meeting_soon", ev["id"])
            if not people or self.card_exists(cid):
                continue
            names = ", ".join(known[i] for i in people)
            reason = f"with {names} at {_when(ev['start'])}"
            link = self.link("calendar_events", ev["id"])
            sid = self.signal("meeting_soon", link, "card", reason, cid)
            self.add_card(cid, {"kind": "meeting_soon", "title": ev["title"], "reason": reason,
                                "subjectKey": ev["id"], "sourceUri": ev["source"] or link,
                                "signalId": sid, "personIds": people,
                                "briefPersonIds": people})
            self.counts["meetingsSoon"] += 1

    def calendar_conflicts(self, excl, block):
        events = self._events(excl, block, CONFLICT_DAYS * 24)
        for a, b in overlapping(events)[:MAX_CONFLICTS]:
            key = "|".join(sorted((a["id"], b["id"])))
            cid = card_id("calendar_conflict", key)
            if self.card_exists(cid):
                continue
            reason = f"“{a['title']}” and “{b['title']}” overlap at {_when(b['start'])}"
            sid = self.signal("calendar_conflict", self.link("calendar_events", key), "card",
                              reason, cid)
            self.add_card(cid, {"kind": "calendar_conflict",
                                "title": f"{a['title']} / {b['title']}", "reason": reason,
                                "subjectKey": key, "signalId": sid,
                                "personIds": sorted(set(a["attendeeIds"] + b["attendeeIds"]))})
            self.counts["conflicts"] += 1

    # 6. fact conflicts the facts job wrote
    def fact_conflicts(self, excl, cursor):
        last = cursor.get("factConflicts") or 0
        rows = _any.query(self.space, self.hub, self.ds["signals"],
                          filter={"kind": "fact_conflict", "tick": {"$gt": last}},
                          sort=["tick"], limit=PER_PASS)
        for row in rows:
            last = max(last, row.get("tick") or 0)
            sid = kit.plain_id(row["id"])
            person = _PERSON.match(str(row.get("subject") or ""))
            pid = person.group(1) if person else None
            cid = card_id("fact_conflict", sid)
            if (pid and pid in excl["personIds"]) or self.card_exists(cid):
                continue
            self.add_card(cid, {"kind": "fact_conflict", "title": "Conflicting facts",
                                "reason": row.get("reason") or "a stated fact and an inferred "
                                                               "one disagree",
                                "subjectKey": sid, "signalId": sid,
                                "sourceUri": row.get("subject"),
                                "personIds": [pid] if pid else []})
            self.counts["factConflicts"] += 1
        cursor["factConflicts"] = last

    # 7. drafting
    def _triggers(self, ids):
        rows = _any.query(self.space, self.anchor(), TRIGGERS, filter={"id": {"$in": ids}},
                          limit=len(ids))
        return {kit.plain_id(r["id"]): r for r in rows}

    def _message(self, box, mid):
        rows = _any.query(self.space, box, "email_messages", filter={"id": mid}, limit=1)
        return rows[0] if rows else None

    def arm_drafts(self, excl, block, budget):
        rows = _any.query(self.space, self.hub, self.ds["cards"],
                          filter={"kind": "reply", "status": "open"},
                          sort=["createdAt"], limit=PER_PASS)
        waiting = [r for r in rows if not r.get("draftId")
                   and int(r.get("draftAttempts") or 0) < MAX_ATTEMPTS]
        if not waiting:
            return
        if not kit.model_allowed():
            for r in waiting:
                self.not_drafted[kit.plain_id(r["id"])] = "drafting needs your consent"
            self.needs.append("allow the brain to read mail with a model, to draft replies")
            return
        for card in waiting:
            cid = kit.plain_id(card["id"])
            if self.counts["draftsArmed"] >= budget:
                self.not_drafted[cid] = "draft budget"
                continue
            self._arm(card, cid, excl, block)

    def _arm(self, card, cid, excl, block):
        src = _MAIL.search(str(card.get("sourceUri") or ""))
        if not src:
            return
        box, mid = src.group(1), src.group(2)
        n = int(card.get("draftAttempts") or 0)
        have = self._triggers([draft_trigger_id(mid, k) for k in range(1, n + 2)])
        prev = have.get(draft_trigger_id(mid, n)) if n else None
        if prev is not None and not prev.get("lastRunAt"):
            self.not_drafted[cid] = "a draft is on its way"
            return
        nxt = draft_trigger_id(mid, n + 1)
        if nxt in have:
            # armed before the counter landed: catch the counter up, never rewrite
            self.modify_card(cid, [("draftAttempts", n + 1)], "counting a draft attempt")
            return
        own = self.boxes.get(box)
        msg = self._message(box, mid)
        if own is None or msg is None or kit.participant_kept_out(msg, excl, block):
            self.not_drafted[cid] = "the message is gone or kept out"
            return
        to = sender_of(msg)
        if not to or to == own:
            return
        self.modify_card(cid, [("draftAttempts", n + 1)], "counting a draft attempt")
        kit.upsert(self.space, self.anchor(), TRIGGERS, nxt, {
            "name": f"brain draft {mid}", "kind": "once", "spec": {"at": self.tick},
            "program": DRAFTER, "enabled": True,
            "args": {"space": self.space, "hub": self.hub, "datasets": dict(self.ds),
                     "tier": self.args.get("tier") or "brain.drafter", "cardId": cid,
                     "signalId": card.get("signalId"), "messageId": mid, "mailboxId": box,
                     "threadId": msg.get("threadId") or mid, "recipients": [to],
                     "provenance": "thread"}}, f"arming {nxt}")
        self.armed[cid] = nxt
        self.counts["draftsArmed"] += 1

    # 8. writes
    def write_cards(self):
        if self.new_cards:
            kit.checked(_any.upsert_records(self.space, self.hub, self.ds["cards"], [
                {"id": cid, "fields": fields} for cid, fields in self.new_cards.items()]),
                "writing cards")
            self._exists.update(dict.fromkeys(self.new_cards, True))

    def settle_reply_signals(self):
        for cid, sid in self.reply_signal.items():
            if cid not in self.armed:
                why = self.not_drafted.get(cid) or "drafting waits for a later run"
                self.signals[sid].update(decision="card", reason=f"{REPLY_REASON}; {why}")

    def write_signals(self):
        if self.signals:
            kit.checked(_any.upsert_records(self.space, self.hub, self.ds["signals"], [
                {"id": sid, "fields": fields} for sid, fields in self.signals.items()]),
                "writing signals")

    def notice(self):
        """One chat notice for the cards this run created; best-effort."""
        if not self.new_cards:
            return
        ids = list(self.new_cards)
        titles = [self.new_cards[c]["title"] for c in ids]
        n = len(ids)
        shown = "; ".join(titles[:NOTICE_TITLES]) + ("; …" if n > NOTICE_TITLES else "")
        text = f"{n} new card{'s' if n != 1 else ''}: {shown}"
        try:
            chat = _any.general_chat(self.space)
            _any.chat_send(self.space, chat, {
                "text": text, "agent": {"name": AGENT_NAME, "done": True},
                "attachments": {"cards": {"type": "brain_cards",
                                          "link": self.link("cards", ids[-1])}}})
        except Exception as e:  # noqa: BLE001 - the cards are written; the notice is a nicety
            print(f"brain notice failed: {type(e).__name__}: {e}")
            self.counts["noticeFailed"] += 1

    def did(self):
        c = self.counts
        lines = [(c["mails"], "read {} mails"), (c["replyCards"], "{} reply cards"),
                 (c["nudges"], "{} nudges"), (c["overduePromises"], "{} overdue promises"),
                 (c["meetingsSoon"], "{} meetings soon"),
                 (c["conflicts"], "{} calendar conflicts"),
                 (c["factConflicts"], "{} fact conflicts"), (c["woken"], "woke {} cards"),
                 (c["draftsArmed"], "asked for {} drafts"), (c["quiet"], "{} quiet"),
                 (c["refused"], "{} kept out")]
        return [fmt.format(n) for n, fmt in lines if n] + self.extra_did

    def standup(self):
        """One standups row when the run read or wrote something."""
        if not (self.counts["mails"] or self.new_cards or self.signals or self.armed):
            return False
        if self.ds.get("approvals"):
            pending = _any.query(self.space, self.hub, self.ds["approvals"],
                                 filter={"status": "pending"}, limit=100)
            if pending:
                self.needs.append(f"approve {len(pending)} draft"
                                  f"{'s' if len(pending) != 1 else ''}")
        if self.ds.get("standups"):
            kit.upsert(self.space, self.hub, self.ds["standups"], f"heartbeat:{int(self.tick)}",
                       {"job": "heartbeat", "tick": self.tick, "did": self.did(),
                        "needs": self.needs, "counts": dict(self.counts)},
                       "writing the standup")
        return True


def _probe(space, hub, datasets, tick, subject):
    """The v1 probes: (exclusions, blocklist) read fail-closed, the gap in seconds."""
    def refused(kind, problem):
        sid = signal_id(tick, kind, subject)
        kit.upsert(space, hub, datasets["signals"], sid, {
            "tick": tick, "kind": kind, "subject": subject, "decision": "refused",
            "reason": problem}, f"writing a {kind} signal")

    problem = kit.exclusions_problem(kit.hub_record(space, hub, datasets["config"], "exclusions"))
    if problem is not None:
        refused("exclusions_unreadable", problem)
        raise kit.exclusions_unreadable(problem)
    try:
        excl = kit.exclusions(space, hub, datasets)
    except kit.exclusions_unreadable as e:
        refused("exclusions_unreadable", str(e))
        raise
    block = kit.blocklist(space, hub, datasets)
    last = (kit.config_value(space, hub, datasets, "heartbeat") or {}).get("lastTickAt")
    gap = tick - last if isinstance(last, (int, float)) and tick - last > GAP_S else None
    return excl, block, gap


def run(args, passes=(), subject=SUBJECT):
    """The heartbeat tick; `passes` are later versions' extra steps, each
    called with (run, excl, block) after the cards pass and before drafting."""
    parsed = kit.job_args(args, "signals", "cards")
    if parsed is None:
        raise unconfigured("the brain heartbeat needs space, hub and datasets with cards — "
                           "set the brain up first")
    space, hub, datasets = parsed
    tick = now()  # noqa: F821 - guest global
    excl, block, gap = _probe(space, hub, datasets, tick, subject)
    settings = _drafting(kit.config_value(space, hub, datasets, "drafting"))
    quiet, bad = quiet_patterns(settings["quietSenders"])
    cursor = _cursor(space, hub, datasets)
    before = {k: dict(v) if isinstance(v, dict) else v for k, v in cursor.items()}
    r = Run(space, hub, datasets, tick, args)
    r.counts["badQuietSenders"] = bad
    if gap is not None:
        r.signal("heartbeat_gap", subject, "none", f"no tick for {int(gap)}s")
    r.wake_snoozed()
    r.read_mail(excl, block, cursor["mail"], quiet, settings["replyWindowDays"])
    r.stale_threads(excl, block, cursor["stale"], settings["replyWindowDays"])
    r.overdue_promises(excl, block)
    r.meetings_soon(excl, block, settings["meetingSoonHours"])
    r.calendar_conflicts(excl, block)
    r.fact_conflicts(excl, cursor)
    for extra in passes:
        extra(r, excl, block)
    r.write_cards()
    r.arm_drafts(excl, block, settings["maxPerRun"])
    r.settle_reply_signals()
    r.write_signals()
    r.notice()
    wrote_standup = r.standup()
    if cursor != before:
        kit.write_config(space, hub, datasets, "heartbeat.cursor", cursor)
    kit.write_config(space, hub, datasets, "heartbeat", {"lastTickAt": tick})
    return {"ok": True, "gap_s": gap, "cards": list(r.new_cards), "armed": dict(r.armed),
            "signals": len(r.signals), "standup": wrote_standup, "counts": dict(r.counts)}


def main(args):
    return run(args)
