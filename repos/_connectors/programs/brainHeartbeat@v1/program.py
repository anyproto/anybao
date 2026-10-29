"""Brain heartbeat: notice mail, stale threads, promises, meetings, sent replies; ask in cards."""

__any_category__ = "plugin"  # the AI brain is a plugin of the connectors repo (ADR-033 §1.1)

# ADR-033 §4 (the decision layer) and §5 (the reply hand-off). The cron
# job the brain's setup run writes (every 600 s), args {space, hub,
# datasets, tier}. Deterministic: it imports no llm@v1, no webSearch, no
# connector, and no mail body reaches a model — the only bodies it reads
# are the user's own sent messages, to match an approved draft (§5.3).
# Per run, in order:
#   0. the probes (§1.5): a missing or unreadable exclusion list or
#      blocklist writes one refused signal and raises; a tick more than
#      GAP_S after the last one writes a heartbeat_gap signal
#   1. snoozed cards whose snoozedUntil passed open again (card_woken)
#   2. new mail per mailbox since its cursor → one signal per inbound
#      message with a decision and a reason, and a `reply` card for mail
#      addressed to the user with no reply from them yet (§4.2, §4.4)
#   3. stale threads (the user wrote last, no answer for replyWindowDays)
#      → a `nudge` card, once per thread
#   4. open promises past their dueAt → an `overdue_promise` card
#   5. calendar events within meetingSoonHours with a known attendee, and
#      overlapping events in the next week → `meeting_soon` and
#      `calendar_conflict` cards (no-ops until calendar sync declares
#      calendar_events on the hub); one 7-day read serves both
#   6. fact_conflict signals the facts job wrote → a `fact_conflict` card
#   7. sent detection (§5.3, §5.5): for each approved approval with no
#      sends row, the user's message after the approval carrying the
#      approved text — in the draft's thread, then among messages to its
#      recipients — writes one sends row, consumes the approval and marks
#      the card sent, silently; seven days without one open a send_check
#      card, once. Approvals with a check card never crowd out the ones
#      without: the fresh ones are examined first, the checked ones with
#      what is left of PER_RUN. Nothing here sends: the user does, from Gmail
#   8. reply cards with no draft yet → a once trigger for brainDrafter@v1,
#      at most maxPerRun a run and MAX_ATTEMPTS a card, only while the user
#      allows reading mail with a model (§4.5); the heartbeat owns the
#      attempt counter and every trigger id is fresh and existence-checked.
#      A card whose source message is gone, or kept out now, is dismissed
#      with the reason as its answer, so it is not re-checked every tick
#   9. one chat notice when cards were created (§4.8), one standups row
#      when something was read or written, then the cursors
# Cursors (config/heartbeat.cursor): `mail` is {mailboxId: _ver.id}, the
# record's arrival order (kit.mail_since, §4.4) — the Gmail backfill lands
# mail newest first across its own ticks, so a date cursor would pass the
# older slices for good; the reply window still bounds every read by
# internalDate, so mail older than the window is never carded, whenever it
# lands. `stale` is {mailboxId: internalDate ms}: that pass judges the
# messages that cross the reply window, each once, so mail backfilled
# below its cursor is not nudged. `factConflicts` is a signal tick.
# Mail from an excluded or blocklisted participant gets one `refused`
# signal and nothing else: the drafter drops a thread message carrying
# such a signal, so mail that arrived while someone was excluded is never
# read later. A card's id is sha256(kind | identity), so a re-run writes
# no second card; a card that exists is never rewritten, since its status
# is the user's answer. Every per-row lookup (threads, covering cards,
# card existence, persons, triggers, drafts) is one `$in` query per pass.

import hashlib
import re

kit = use("brainKit@v1")  # noqa: F821 - guest global
_any = use("agent:any@v1")  # noqa: F821 - guest global

SUBJECT = "connectors:brainHeartbeat@v1"
DRAFTER = "connectors:brainDrafter@v1"
TRIGGERS = "agent_triggers"
GAP_S = 1200
DAY = 86400
PER_MAILBOX = 200          # messages read per mailbox and pass, per run
PER_PASS = 200             # promises, events, signals, approvals read per pass
THREAD_ROWS = 20           # newest messages kept per thread
IN_CHUNK = 50              # ids per `$in` query
MAX_ATTEMPTS = 3           # one-shots armed per reply card, ever
STALE_LOOKBACK_DAYS = 14   # how far behind the reply window a first run looks
CONFLICT_DAYS = 7
MAX_CONFLICTS = 20
NOTICE_TITLES = 5
TITLE_MAX = 200
PER_RUN = 50               # approvals settled per run: at most two mail queries each
SEARCH_ROWS = 20
MATCH_CHARS = 200
BODY_CHARS = 20000
SEND_CHECK_DAYS = 7
OPEN = ("open", "snoozed")
REPLY_REASON = "addressed to you, no reply yet"
GONE_REASON = "source gone or kept out"
# Gmail's own bulk tabs (and spam, trash): mail there is quiet whoever sent
# it, so a newsletter from a plain address never becomes a reply card
BULK_LABELS = frozenset(("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES",
                         "CATEGORY_FORUMS", "SPAM", "TRASH"))
DRAFTING = {"maxPerRun": 3, "replyWindowDays": 5, "meetingSoonHours": 24, "quietSenders": []}

_MAIL = re.compile(r"/([^/]+)/email_messages/([^/]+)$")
_PERSON = re.compile(r"^any://o/[^/]+/([^/]+)$")
_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")


class unconfigured(Exception):
    """The job's args do not name the brain hub and its card store."""


class cursor_corrupt(Exception):
    """config/heartbeat.cursor is not in its {mail, stale, factConflicts} shape."""


# --- pure decisions ---------------------------------------------------------

def card_id(kind, identity):
    """A card's id: sha256 of "<kind>|<identity>", hex (ADR-033 §4.3)."""
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
    if BULK_LABELS & set(message.get("labelIds") or []):
        return ("new_mail", "quiet", "a Gmail bulk tab", False)
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


def match_text(text):
    """Text as the sent match compares it: quoted lines dropped, markdown
    backslash escapes removed, whitespace collapsed, case folded."""
    return kit.normalize_ws(_ESCAPE.sub(r"\1", kit.clean_body(text, BODY_CHARS))).casefold()


def is_sent_reply(row, own_address, approved_body, approved_ms):
    """True when `row` is the user's message after the approval carrying the
    approved text's first MATCH_CHARS characters (ADR-033 §5.3)."""
    needle = match_text(approved_body)[:MATCH_CHARS]
    return (bool(needle) and sender_of(row) == own_address
            and (row.get("internalDate") or 0) > approved_ms
            and needle in match_text(row.get("body")))


def _chunks(items, size):
    items = list(items)
    return [items[i:i + size] for i in range(0, len(items), size)]


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
    if not isinstance(value, dict):
        raise cursor_corrupt(f"heartbeat.cursor is {value!r}")
    mail, stale = value.get("mail", {}), value.get("stale", {})
    # mail cursors are _ver.id strings; a number is the old date cursor, refused
    ok = (isinstance(mail, dict) and all(isinstance(v, str) for v in mail.values())
          and isinstance(stale, dict) and all(isinstance(v, (int, float)) for v in stale.values())
          and isinstance(value.get("factConflicts", 0), (int, float)))
    if not ok:
        raise cursor_corrupt(f"heartbeat.cursor is {value!r}")
    return {"mail": dict(mail or {}), "stale": dict(stale or {}),
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
                       "draftsArmed": 0, "badQuietSenders": 0, "noticeFailed": 0,
                       "sent": 0, "sendChecks": 0}
        self.needs = []
        self._cards = {}           # card id -> row or None, once looked up
        self._people = {}          # address -> person id or None
        self._threads = {}         # (mailbox, thread id) -> newest THREAD_ROWS rows
        self._events = None
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

    def load_cards(self, cids):
        """Look the cards up at once: one `id $in` query per IN_CHUNK ids."""
        want = sorted({c for c in cids if c} - set(self._cards) - set(self.new_cards))
        for chunk in _chunks(want, IN_CHUNK):
            rows = _any.query(self.space, self.hub, self.ds["cards"],
                              filter={"id": {"$in": [f"{self.hub}/{c}" for c in chunk]}},
                              limit=len(chunk))
            found = {kit.plain_id(r["id"]): r for r in rows}
            for c in chunk:
                self._cards[c] = found.get(c)

    def card_row(self, cid):
        """The stored card, or None; a card created this run counts as stored."""
        if cid in self.new_cards:
            return {"id": cid, **self.new_cards[cid]}
        if cid not in self._cards:
            self.load_cards([cid])
        return self._cards[cid]

    def card_exists(self, cid):
        return self.card_row(cid) is not None

    def add_card(self, cid, fields):
        fields.setdefault("status", "open")
        fields["title"] = str(fields.get("title") or "(no subject)")[:TITLE_MAX]
        self.new_cards[cid] = fields

    def load_people(self, addrs):
        """Resolve addresses to persons at once: one `brain_person.emails $in`
        query per IN_CHUNK addresses; an address with no person maps to None."""
        want = sorted({a for a in addrs if a} - set(self._people))
        for chunk in _chunks(want, IN_CHUNK):
            rows = _any.query_objects(self.space,
                                      filter={"brain_person.emails": {"$in": chunk}},
                                      limit=2 * len(chunk))
            for r in rows:
                for e in (r.get("brain_person") or {}).get("emails") or []:
                    if str(e) in chunk:
                        self._people.setdefault(str(e), r["id"])
            for a in chunk:
                self._people.setdefault(a, None)

    def person(self, addr):
        if addr not in self._people:
            self.load_people([addr])
        return self._people[addr]

    def load_threads(self, box, rows):
        """Read the threads of `rows` at once: one `threadId $in` query per
        IN_CHUNK threads, newest first, the newest THREAD_ROWS kept per thread."""
        tids = sorted({r.get("threadId") for r in rows if r.get("threadId")}
                      - {t for b, t in self._threads if b == box})
        for chunk in _chunks(tids, IN_CHUNK):
            found = _any.query(self.space, box, "email_messages",
                               filter={"threadId": {"$in": chunk}},
                               sort=["-internalDate"], limit=THREAD_ROWS * len(chunk))
            by = {t: [] for t in chunk}
            for r in found:
                kept = by.get(r.get("threadId"))
                if kept is not None and len(kept) < THREAD_ROWS:
                    kept.append(r)
            for t, kept in by.items():
                self._threads[(box, t)] = kept

    def thread(self, box, row):
        tid = row.get("threadId")
        if not tid:
            return [row]
        if (box, tid) not in self._threads:
            self.load_threads(box, [row])
        return self._threads[(box, tid)]

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
            # arrival order after the cursor, within the reply window whenever
            # it lands (ADR-033 §4.4); the rows are dated for the decisions
            rows, last = kit.mail_since(self.space, box, cursor.get(box), PER_MAILBOX,
                                        internalDate={"$gte": floor})
            rows.sort(key=_at)
            judged = []
            for row in rows:
                self.counts["mails"] += 1
                kept = kit.participant_kept_out(row, excl, block)
                judged.append((row, decide_mail(row, own, False, quiet, kept), kept))
            self.load_threads(box, [row for row, v, _ in judged if v is not None and v[3]])
            needing = []
            for row, verdict, kept in judged:
                if verdict is None:
                    continue
                if verdict[3] and replied_after(self.thread(box, row), row, own):
                    verdict = decide_mail(row, own, True, quiet, kept)
                kind, decision, reason, needs_card = verdict
                if needs_card:
                    needing.append(row)
                else:
                    self.signal(kind, kit.mail_link(self.space, box, kit.plain_id(row["id"])),
                                decision, reason)
            self._reply_cards(box, needing)
            if last:
                cursor[box] = last

    def _covering(self, tids):
        """{thread id: its open or snoozed reply card} for the threads given."""
        out = {}
        for chunk in _chunks(sorted(tids), IN_CHUNK):
            rows = _any.query(self.space, self.hub, self.ds["cards"],
                              filter={"kind": "reply", "subjectKey": {"$in": chunk}},
                              limit=THREAD_ROWS * len(chunk))
            for c in rows:
                if c.get("status") in OPEN:
                    out.setdefault(c.get("subjectKey"), c)
        return out

    def _reply_cards(self, box, rows):
        newest = {}
        for row in rows:
            tid = row.get("threadId") or kit.plain_id(row["id"])
            if tid not in newest or _at(row) > _at(newest[tid]):
                newest[tid] = row
        covering = self._covering(newest) if newest else {}
        wanted = {tid: reply_card_id(tid, kit.plain_id(row["id"]))
                  for tid, row in newest.items() if tid not in covering}
        self.load_cards(wanted.values())
        self.load_people([sender_of(newest[tid]) for tid, cid in wanted.items()
                          if not self.card_exists(cid)])
        for row in rows:
            mid = kit.plain_id(row["id"])
            tid = row.get("threadId") or mid
            link = kit.mail_link(self.space, box, mid)
            if newest[tid] is not row:
                self.signal("new_mail", link, "none",
                            "a newer message in the thread needs the reply")
                continue
            if tid in covering:
                self.signal("new_mail", link, "card", "an open reply card covers this thread",
                            kit.plain_id(covering[tid]["id"]))
                self.counts["covered"] += 1
                continue
            cid = wanted[tid]
            if self.card_exists(cid):
                # its own card, closed: the user answered this thread already
                self.signal("new_mail", link, "none", "a closed reply card covers this thread",
                            cid)
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
            mine = [row for row in rows
                    if sender_of(row) == own and not kit.participant_kept_out(row, excl, block)]
            self.load_people(a for row in mine for _, a in kit.addresses(row.get("to"))
                             if a != own)
            known = []
            for row in mine:
                to = [a for _, a in kit.addresses(row.get("to")) if a != own]
                pid = next((p for p in (self.person(a) for a in to) if p), None)
                if pid is not None:
                    known.append((row, pid))
            self.load_threads(box, [row for row, _ in known])
            self.load_cards(card_id("nudge", row.get("threadId") or kit.plain_id(row["id"]))
                            for row, _ in known)
            for row, pid in known:
                if not is_newest(self.thread(box, row), row):
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
        self.load_cards(card_id("overdue_promise", kit.plain_id(row["id"])) for row in rows)
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
    def events(self, excl, block, hours):
        """The non-cancelled events of the next max(hours, 7 days) with no
        kept-out attendee, read once a run and shared by both calendar passes
        (ADR-033 §4.5); each is {id, start, end, title, attendeeIds, source}."""
        if not self.ds.get("calendar_events"):
            return []
        if self._events is None:
            horizon = max(hours, CONFLICT_DAYS * 24) * 3600
            rows = _any.query(self.space, self.hub, self.ds["calendar_events"],
                              filter={"start": {"$gte": instant(self.tick),  # noqa: F821
                                                "$lte": instant(self.tick + horizon)}},  # noqa: F821
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
            self._events = out
        return [e for e in self._events if e["start"] <= self.tick + hours * 3600]

    def meetings_soon(self, excl, block, hours):
        events = self.events(excl, block, hours)
        known = {}
        wanted = sorted({i for e in events for i in e["attendeeIds"]})
        if wanted:
            for r in _any.query_objects(self.space, filter={"id": {"$in": wanted}},
                                        limit=len(wanted)):
                if any(kit.kept_out(e, excl, block) for e in kit.person_emails(r)):
                    continue        # a person kept out by address is no attendee here
                known[r["id"]] = (r.get("any") or {}).get("name") or r["id"]
        self.load_cards(card_id("meeting_soon", ev["id"]) for ev in events)
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
        pairs = overlapping(self.events(excl, block, CONFLICT_DAYS * 24))[:MAX_CONFLICTS]
        self.load_cards(card_id("calendar_conflict", "|".join(sorted((a["id"], b["id"]))))
                        for a, b in pairs)
        for a, b in pairs:
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
        self.load_cards(card_id("fact_conflict", kit.plain_id(row["id"])) for row in rows)
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

    # 7. sent detection (ADR-033 §5.3, §5.5)
    def _hub_rows(self, dataset, rids):
        """{plain id: row} for the hub records named, one `$in` query per chunk."""
        out = {}
        for chunk in _chunks(sorted({r for r in rids if r}), IN_CHUNK):
            for row in _any.query(self.space, self.hub, self.ds[dataset],
                                  filter={"id": {"$in": [f"{self.hub}/{r}" for r in chunk]}},
                                  limit=len(chunk)):
                out[kit.plain_id(row["id"])] = row
        return out

    def _set(self, dataset, rid, ops, what):
        kit.checked(_any.modify(self.space, {
            "objectId": self.hub, "dataset": self.ds[dataset], "records": [
                {"id": rid, "ops": [{"type": "$set", "path": p, "value": v}
                                    for p, v in ops]}]}), what)

    def _find_sent(self, draft, own, approved_ms, excl, block):
        """The user's sent reply for a draft, or None: its thread first, then
        any message to its recipients; a message carrying someone kept out is
        skipped before its body is read."""
        box = draft.get("sender")
        after = {"$gt": approved_ms}
        filters = []
        if draft.get("threadId"):
            filters.append({"threadId": draft["threadId"], "internalDate": after})
        if draft.get("recipients"):
            filters.append({"participants": {"$in": list(draft["recipients"])},
                            "internalDate": after})
        for flt in filters:
            for row in _any.query(self.space, box, "email_messages", filter=flt,
                                  sort=["-internalDate"], limit=SEARCH_ROWS):
                if kit.participant_kept_out(row, excl, block):
                    continue
                if is_sent_reply(row, own, draft.get("body"), approved_ms):
                    return row
        return None

    def _close_approval(self, aid, cid, card_status):
        """The approval consumed, its card sent, an open check card answered."""
        self._set("approvals", aid, [("status", "consumed")], "consuming the approval")
        if cid and card_status != "sent":
            self._set("cards", cid, [("status", "sent")], "marking the card sent")
        check = card_id("send_check", aid)
        row = self.card_row(check)
        if row is not None and row.get("status") in OPEN:
            self._set("cards", check, [("status", "answered")], "answering the send check")

    def _send_check(self, approval, draft, card, approved_at):
        aid = kit.plain_id(approval["id"])
        cid = card_id("send_check", aid)
        if self.card_exists(cid):
            return
        days = int((self.tick - approved_at) // DAY)
        reason = f"approved {days} days ago; no sent reply found in your mail"
        original = kit.plain_id((card or {}).get("id") or approval.get("cardId") or "")
        sid = self.signal("send_check", self.link("approvals", aid), "card", reason, cid)
        self.add_card(cid, {"kind": "send_check",
                            "title": f"Did you send this reply? {draft.get('subject') or ''}"
                                     .strip(),
                            "reason": reason, "subjectKey": aid, "approvalId": aid,
                            "draftId": kit.plain_id(draft["id"]),
                            "sourceUri": self.link("cards", original), "signalId": sid,
                            "personIds": list((card or {}).get("personIds") or [])})
        self.counts["sendChecks"] += 1

    def _settle(self, approval, draft, have_send, excl, block):
        """One approved approval: detected, completed, checked, or left."""
        aid = kit.plain_id(approval["id"])
        if draft is None:
            return
        card = self.card_row(approval.get("cardId") or draft.get("cardId") or "")
        cid = kit.plain_id(card["id"]) if card else None
        if have_send:
            # marked by hand, half written: finish it, write no second row
            self._close_approval(aid, cid, (card or {}).get("status"))
            return
        recipients = [str(x).lower() for x in draft.get("recipients") or []]
        if any(kit.kept_out(x, excl, block) for x in recipients):
            return                          # someone kept out: no mail is read for it
        approved_at = ts_s(approval.get("decidedAt")) or ts_s(approval.get("modifiedAt"))  # noqa: F821
        own = self.boxes.get(draft.get("sender")) or str(draft.get("senderAddress") or "").lower()
        if approved_at is None or not own:
            return
        row = self._find_sent(draft, own, int(approved_at * 1000), excl, block)
        if row is not None:
            mid = kit.plain_id(row["id"])
            kit.upsert(self.space, self.hub, self.ds["sends"], aid, {
                "approvalId": aid, "cardId": cid or approval.get("cardId"),
                "threadId": row.get("threadId") or draft.get("threadId") or mid,
                "messageId": mid, "how": "detected", "at": instant(self.tick)},  # noqa: F821
                "writing the send")
            self._close_approval(aid, cid, (card or {}).get("status"))
            self.signal("reply_sent", kit.mail_link(self.space, draft.get("sender"), mid), "none",
                        "sent from Gmail, detected", cid)
            self.counts["sent"] += 1
        elif self.tick - approved_at >= SEND_CHECK_DAYS * DAY:
            self._send_check(approval, draft, card, approved_at)

    def detect_sends(self, excl, block):
        if not all(self.ds.get(k) for k in ("approvals", "drafts", "sends")):
            return
        approvals = _any.query(self.space, self.hub, self.ds["approvals"],
                               filter={"status": "approved"}, sort=["decidedAt"], limit=PER_PASS)
        if not approvals:
            return
        # approvals the user was already asked about (a send_check card) wait
        # behind the ones without, so fifty stale approvals never starve a
        # fresh one; each group oldest decision first, PER_RUN in all
        checks = {kit.plain_id(a["id"]): card_id("send_check", kit.plain_id(a["id"]))
                  for a in approvals}
        self.load_cards(checks.values())
        fresh = [a for a in approvals if not self.card_exists(checks[kit.plain_id(a["id"])])]
        checked = [a for a in approvals if self.card_exists(checks[kit.plain_id(a["id"])])]
        window = (fresh + checked)[:PER_RUN]
        ids = [kit.plain_id(a["id"]) for a in window]
        sent = set(self._hub_rows("sends", ids))
        drafts = self._hub_rows("drafts", (a.get("draftId") for a in window))
        self.load_cards(a.get("cardId") or (drafts.get(a.get("draftId")) or {}).get("cardId")
                        for a in window)
        for approval in window:
            self._settle(approval, drafts.get(approval.get("draftId")),
                         kit.plain_id(approval["id"]) in sent, excl, block)

    # 8. drafting (ADR-033 §4.5)
    def _triggers(self, ids):
        out = {}
        for chunk in _chunks(ids, IN_CHUNK):
            rows = _any.query(self.space, self.anchor(), TRIGGERS, filter={"id": {"$in": chunk}},
                              limit=len(chunk))
            out.update({kit.plain_id(r["id"]): r for r in rows})
        return out

    def _message(self, box, mid):
        rows = _any.query(self.space, box, "email_messages", filter={"id": mid}, limit=1)
        return rows[0] if rows else None

    def arm_drafts(self, excl, block, budget):
        # the open reply cards from before this run, oldest first, then the
        # ones this run created (not re-read: the server may not show them yet)
        rows = _any.query(self.space, self.hub, self.ds["cards"],
                          filter={"kind": "reply", "status": "open"},
                          sort=["createdAt"], limit=PER_PASS)
        rows += [{"id": cid, **fields} for cid, fields in self.new_cards.items()
                 if fields.get("kind") == "reply"]
        waiting, seen = [], set()
        for card in rows:
            cid = kit.plain_id(card["id"])
            if cid in seen:
                continue
            seen.add(cid)
            if not card.get("draftId") and int(card.get("draftAttempts") or 0) < MAX_ATTEMPTS:
                waiting.append((cid, card))
        if not waiting:
            return
        if not kit.model_allowed():
            for cid, _ in waiting:
                self.not_drafted[cid] = "drafting needs your consent"
            self.needs.append("allow the brain to read mail with a model, to draft replies")
            return
        have = self._triggers(sorted({
            draft_trigger_id(src.group(2), k)
            for _, card in waiting
            for src in [_MAIL.search(str(card.get("sourceUri") or ""))] if src
            for k in range(1, int(card.get("draftAttempts") or 0) + 2)}))
        for cid, card in waiting:
            if self.counts["draftsArmed"] >= budget:
                self.not_drafted[cid] = "draft budget"
                continue
            self._arm(card, cid, excl, block, have)

    def _dismiss(self, card, cid, why):
        """Close a reply card the drafter could never serve, with the reason as
        its answer, so it is not re-checked every tick; its signal says why."""
        self.not_drafted[cid] = why
        self.modify_card(cid, [("status", "dismissed"), ("answer", why)],
                         "dismissing a reply card with no source")
        if cid not in self.reply_signal:
            self.signal("new_mail", str(card.get("sourceUri") or self.link("cards", cid)),
                        "none", f"{why}; card dismissed", cid)

    def _arm(self, card, cid, excl, block, have):
        src = _MAIL.search(str(card.get("sourceUri") or ""))
        if not src:
            return
        box, mid = src.group(1), src.group(2)
        n = int(card.get("draftAttempts") or 0)
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
            self._dismiss(card, cid, GONE_REASON)
            return
        to = sender_of(msg)
        if not to or to == own:
            return
        # the trigger first, the counter after: a refused upsert raises here
        # and spends no attempt; an upsert that landed without its count is
        # the catch-up branch above
        kit.upsert(self.space, self.anchor(), TRIGGERS, nxt, {
            "name": f"brain draft {mid}", "kind": "once", "spec": {"at": self.tick},
            "program": DRAFTER, "enabled": True,
            "args": {"space": self.space, "hub": self.hub, "datasets": dict(self.ds),
                     "tier": self.args.get("tier") or "brain.drafter", "cardId": cid,
                     "signalId": card.get("signalId"), "messageId": mid, "mailboxId": box,
                     "threadId": msg.get("threadId") or mid, "recipients": [to],
                     "provenance": "thread"}}, f"arming {nxt}")
        self.modify_card(cid, [("draftAttempts", n + 1)], "counting a draft attempt")
        self.armed[cid] = nxt
        self.counts["draftsArmed"] += 1

    # 9. writes
    def write_cards(self):
        if self.new_cards:
            kit.checked(_any.upsert_records(self.space, self.hub, self.ds["cards"], [
                {"id": cid, "fields": fields} for cid, fields in self.new_cards.items()]),
                "writing cards")

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
        """One chat notice for the cards this run created (ADR-033 §4.8); best-effort."""
        if not self.new_cards:
            return
        ids = list(self.new_cards)
        titles = [self.new_cards[c]["title"] for c in ids]
        n = len(ids)
        shown = "; ".join(titles[:NOTICE_TITLES]) + ("; …" if n > NOTICE_TITLES else "")
        kit.notice(self.space, f"{n} new card{'s' if n != 1 else ''}: {shown}",
                   attachments={"cards": {"type": "brain_cards",
                                          "link": self.link("cards", ids[-1])}},
                   counts=self.counts)

    def did(self):
        c = self.counts
        lines = [(c["mails"], "read {} mails"), (c["replyCards"], "{} reply cards"),
                 (c["nudges"], "{} nudges"), (c["overduePromises"], "{} overdue promises"),
                 (c["meetingsSoon"], "{} meetings soon"),
                 (c["conflicts"], "{} calendar conflicts"),
                 (c["factConflicts"], "{} fact conflicts"), (c["woken"], "woke {} cards"),
                 (c["sent"], "{} replies sent"), (c["sendChecks"], "{} send checks"),
                 (c["draftsArmed"], "asked for {} drafts"), (c["quiet"], "{} quiet"),
                 (c["refused"], "{} kept out")]
        return [fmt.format(n) for n, fmt in lines if n]

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


def _probe(space, hub, datasets, tick):
    """The probes (ADR-033 §1.5, §4.0): the exclusion list and the blocklist
    read fail-closed, each refusal signalled before it raises; the gap in
    seconds since the last tick, past GAP_S."""
    def refused(kind, problem):
        sid = signal_id(tick, kind, SUBJECT)
        kit.upsert(space, hub, datasets["signals"], sid, {
            "tick": tick, "kind": kind, "subject": SUBJECT, "decision": "refused",
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
    try:
        block = kit.blocklist(space, hub, datasets)
    except kit.blocklist_unreadable as e:
        refused("blocklist_unreadable", str(e))
        raise
    last = (kit.config_value(space, hub, datasets, "heartbeat") or {}).get("lastTickAt")
    gap = tick - last if isinstance(last, (int, float)) and tick - last > GAP_S else None
    return excl, block, gap


def main(args):
    parsed = kit.job_args(args, "signals", "cards")
    if parsed is None:
        raise unconfigured("the brain heartbeat needs space, hub and datasets with cards — "
                           "set the brain up first")
    space, hub, datasets = parsed
    tick = now()  # noqa: F821 - guest global
    excl, block, gap = _probe(space, hub, datasets, tick)
    settings = _drafting(kit.config_value(space, hub, datasets, "drafting"))
    quiet, bad = quiet_patterns(settings["quietSenders"])
    cursor = _cursor(space, hub, datasets)
    before = {k: dict(v) if isinstance(v, dict) else v for k, v in cursor.items()}
    r = Run(space, hub, datasets, tick, args)
    r.counts["badQuietSenders"] = bad
    if gap is not None:
        r.signal("heartbeat_gap", SUBJECT, "none", f"no tick for {int(gap)}s")
    r.wake_snoozed()
    r.read_mail(excl, block, cursor["mail"], quiet, settings["replyWindowDays"])
    r.stale_threads(excl, block, cursor["stale"], settings["replyWindowDays"])
    r.overdue_promises(excl, block)
    r.meetings_soon(excl, block, settings["meetingSoonHours"])
    r.calendar_conflicts(excl, block)
    r.fact_conflicts(excl, cursor)
    r.detect_sends(excl, block)
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
