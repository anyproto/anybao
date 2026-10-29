"""Brain chat scan: promises and asks from the chats the user opted in."""

__any_category__ = "plugin"  # the AI brain is a plugin of the connectors repo (ADR-033 §1.1)

# ADR-033 §7. A cron job the brain's setup run writes (every 1800 s, off
# until a chat is watched), args {space, hub, datasets, tier?}. It reads
# only the general chats of the spaces named in `brain.chatScan.spaces` —
# written by brainSetup's watch_chats() / stop_watching_chats() after the
# user's yes — and, behind `brain.chatScan.pages`, the unchecked checklist
# items of their recently edited pages. Nothing is read while the row
# names nothing.
#
# Per chat, after its cursor (the last `_ver.id` handled — indexed and
# exact, where createdAt is neither, ADR-033 §7.2): agent posts and
# control records are passed over; a message whose sender, reply target
# or any mentioned identity is kept out — the identity itself, or any
# person it may be, on the exclusion list (brainKit.IdentityResolver) —
# is counted and its text never looked at (fail closed: the exclusion
# list is read first). The user's own messages go through brainPromises'
# extract() — only with the model consent (`brain.model`), capped by
# model calls — and land in `promises` with channel "chat". Other people's
# messages addressed to the user (a one-to-one space, a mention, a reply
# to the user) become `asks`, one per sentence with a request phrase or a
# trailing "?": deterministic, no model. Only ids the store lacks are
# written; the cursor passes only messages fully handled. A chat's first
# visit reads its newest page back to a 7-day floor, and the floor holds
# for every later read of that chat (`config/chatScan.floor`): a message
# synced in later from before it is passed, never read. Once a UTC day it
# writes one task_suggestion card when the hub keeps `cards`.

import hashlib
import re

kit = use("brainKit@v1")  # noqa: F821 - guest global
promises = use("brainPromises@v1")  # noqa: F821 - guest global
tasks = use("brainTasks@v1")  # noqa: F821 - guest global
_any = use("agent:any@v1")  # noqa: F821 - guest global
llm = use("agent:llm@v1")  # noqa: F821 - guest global

SPACES_KEY = "brain.chatScan.spaces"
PAGES_KEY = "brain.chatScan.pages"
PER_CHAT = 200
FIRST_VISIT_DAYS = 7      # a chat with no cursor: its newest page, this far back
MAX_CALLS = 40            # model calls per run; a judge, a call and a retry are three
MAX_FAILURES = 2          # provider failures after which the run calls no model
MESSAGE_CHARS = 2400
QUOTE_MAX = 240
PAGES_PER_SPACE = 20
PAGE_WINDOW_S = 86400     # a space with no page cursor: pages edited this recently
BLOCKS_PER_PAGE = 500
OPEN_CHECKLIST = 500      # open checklist asks read back per page batch
CARD_ITEMS = 20
DAY = 86400
ONE_TO_ONE = "any.onetoone"
# the roles that may write — a reader, a guest, or "none" (also: not loaded yet) is skipped
WRITABLE = {"owner", "admin", "writer"}

# what makes a sentence addressed to the user a request (ADR-033 §7.3)
REQUEST_PHRASES = ("can you", "could you", "would you", "please", "let me know",
                   "send me", "need you to", "remind me",
                   "kannst du", "könntest du", "bitte", "schick mir", "brauche", "sag mir")
_REQUEST = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(p) for p in REQUEST_PHRASES)
                      + r")(?!\w)", re.I)
_QUESTION = re.compile(r"\?[\s\"')\]»”]*$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_LINK = re.compile(r"\[([^\]\n]*)\]\((any://[^)\s]*)\)")
_IDENTITY_LINK = re.compile(r"any://u/([^\s)\]/?#]+)")
# a checklist ask's `source`: …/<page>/editor_blocks/<blockId>
_BLOCK_LINK = re.compile(r"/editor_blocks/([^/\s]+)$")


class unconfigured(Exception):
    """The job's args do not name the brain hub and its asks and promises stores."""


class cursor_corrupt(Exception):
    """config/chatScan.cursor is not in its {chatId: _ver.id} shape."""


class asks_rejected(Exception):
    """The server refused ask records; the first reason is the message."""


class promises_rejected(Exception):
    """The server refused promise records; the first reason is the message."""


class cards_rejected(Exception):
    """The server refused the task_suggestion card; the reason is the message."""


class provider_failed(Exception):
    """Every model call of the run failed: the provider is down or unset."""


# --- pure rules ---------------------------------------------------------------

def chat_text(text):
    """A chat message as the user sees it: links reduced to their label, each
    line's whitespace collapsed, empty lines dropped, MESSAGE_CHARS at most."""
    shown = _LINK.sub(lambda m: m.group(1), str(text or ""))
    lines = [" ".join(ln.split()) for ln in shown.splitlines()]
    return "\n".join(ln for ln in lines if ln)[:MESSAGE_CHARS]


def sentences(text):
    """The sentences of a shown message, verbatim: split at line ends and at
    . ! ? followed by whitespace (a URL's dots stay put)."""
    out = []
    for line in str(text or "").splitlines():
        out += [s.strip() for s in _SENTENCE_END.split(line) if s.strip()]
    return out


def ask_kind(sentence):
    """`request` for a sentence with a request phrase, `question` for one
    ending in "?", else None."""
    if _REQUEST.search(sentence):
        return "request"
    if _QUESTION.search(sentence):
        return "question"
    return None


def mentioned(message):
    """The identities a message mentions: the server's derived `mentions`
    (text mentions and the replied-to author), else the text's any://u/ links."""
    found = message.get("mentions")
    if isinstance(found, list):
        return [str(i) for i in found if i]
    return _IDENTITY_LINK.findall(str(message.get("text") or ""))


def addressed_to_me(message, me, space_is_one_to_one, my_message_ids=()):
    """True when a message is addressed to the user: a one-to-one space, a
    mention of the user (or a reply to them — the server folds the replied-to
    author into `mentions`), or a reply to a message of theirs we read."""
    if space_is_one_to_one:
        return True
    if me in mentioned(message):
        return True
    return message.get("replyToMessageId") in set(my_message_ids or ())


def ver_id(message):
    """A message's creation version (`_ver.id`), the cursor's unit; None if absent."""
    return kit.ver_id(message)


def is_passed_over(message):
    """An agent's post (Bao's or another agent's) or a control record."""
    if message.get("agent"):
        return True
    control = message.get("control")
    return isinstance(control, dict) and bool(control.get("kind"))


def ask_id(message_id, text):
    return hashlib.sha256(f"{message_id}|{text}".encode()).hexdigest()[:32]


def card_id(kind, subject_key):
    """A card's deterministic id: sha256(kind | subjectKey), 64 hex."""
    return hashlib.sha256(f"{kind}|{subject_key}".encode()).hexdigest()


def signal_id(tick, kind, subject):
    """A signal's id, one per (tick, kind, subject) — the heartbeat's form."""
    return f"{int(tick)}-{kind}-{hashlib.sha256(subject.encode()).hexdigest()[:12]}"


def record_link(space, object_id, dataset, record_id):
    return f"any://o/{space}/{object_id}/{dataset}/{record_id}"


def block_of(source):
    """The block id a checklist ask's `source` link names, or None."""
    m = _BLOCK_LINK.search(str(source or ""))
    return m.group(1) if m else None


# --- config ---------------------------------------------------------------------

def _row(key):
    try:
        return effect("config.get", {"key": key})["value"]  # noqa: F821 - guest global
    except EffectError:  # noqa: F821 - guest global
        return None


def watched_spaces():
    """The space ids `brain.chatScan.spaces` names; [] when it names none."""
    value = _row(SPACES_KEY)
    ids = value.get("spaceIds") if isinstance(value, dict) else None
    return [s for s in ids if isinstance(s, str) and s] if isinstance(ids, list) else []


def pages_enabled():
    value = _row(PAGES_KEY)
    return isinstance(value, dict) and value.get("enabled") is True


def _cursor(space, hub, datasets, name, kinds=(str, int, float)):
    value = kit.config_value(space, hub, datasets, name)
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(v, kinds) for v in value.values()):
        raise cursor_corrupt(f"{name} is {value!r}")
    return dict(value)


def _new_stats():
    return {"spaces": 0, "spacesSkipped": 0, "chats": 0, "messages": 0, "passedOver": 0,
            "own": 0, "noConsent": 0, "alreadyExtracted": 0, "capped": 0,
            "providerFailed": 0, "modelCalls": 0, "addressed": 0, "ignored": 0,
            "excludedSkipped": 0, "excludedChats": 0, "promises": 0, "asks": 0,
            "pages": 0, "checklist": 0, "checked": 0, "closed": 0, "cards": 0}


def _merge_stats(space, hub, datasets, stats):
    """Add this run's counts to config/chatScan.stats (counts only, never text)."""
    total = kit.config_value(space, hub, datasets, "chatScan.stats") or {}
    for key, value in stats.items():
        if isinstance(value, dict):
            merged = dict(total.get(key) or {})
            for reason, n in value.items():
                merged[reason] = merged.get(reason, 0) + n
            total[key] = merged
        elif isinstance(value, (int, float)):
            total[key] = (total.get(key) or 0) + value
    kit.write_config(space, hub, datasets, "chatScan.stats", total)


def _write_new(space, hub, dataset, records, error):
    """Write the records the store lacks → how many landed. A deleted id is the
    user's answer (upsert.record_deleted); any other refusal raises `error`."""
    by_id = {}
    for r in records:
        by_id.setdefault(r["id"], r)
    if not by_id:
        return 0
    ids = [f"{hub}/{rid}" for rid in by_id]
    have = {kit.plain_id(r["id"]) for r in
            _any.query(space, hub, dataset, filter={"id": {"$in": ids}}, limit=len(ids))}
    fresh = [r for rid, r in by_id.items() if rid not in have]
    if not fresh:
        return 0
    reply = _any.upsert_records(space, hub, dataset, fresh) or {}
    rejections = reply.get("rejections") or []
    deleted = [r for r in rejections if r.get("code") == "upsert.record_deleted"]
    refused = [r for r in rejections if r not in deleted]
    if refused:
        raise error(f"{len(refused)} of {len(fresh)} rejected: "
                    f"{refused[0].get('reason') or refused[0].get('code')}")
    return len(fresh) - len(deleted)


def _close(space, hub, dataset, record, what):
    """Mark one ask done, now."""
    kit.checked(_any.modify(space, {"objectId": hub, "dataset": dataset,
                "records": [{"id": kit.plain_id(record["id"]), "ops": [
                    {"type": "$set", "path": "status", "value": "done"},
                    {"type": "$set", "path": "closedAt",
                     "value": instant(now())}]}]}),  # noqa: F821 - guest globals
                what)


# --- one run --------------------------------------------------------------------

class _Run:
    def __init__(self, space, hub, datasets, tier):
        self.space, self.hub, self.datasets, self.tier = space, hub, datasets, tier
        self.stats = _new_stats()
        self.today = now()  # noqa: F821 - guest global
        self.excl = kit.exclusions(space, hub, datasets)          # fail closed
        self.me = _any.me()["identity"]
        self.who = kit.IdentityResolver(space, self.excl, me=self.me)
        self.consent = kit.model_allowed()
        self._gate = None
        self.calls = self.failures = self.answered = 0
        self.cursor = _cursor(space, hub, datasets, "chatScan.cursor", (str,))
        self.floor = _cursor(space, hub, datasets, "chatScan.floor", (int, float))

    def gate(self):
        if self._gate is None:
            self._gate = bool(llm.judge_available())
        return self._gate

    # --- one space ------------------------------------------------------------
    def scan_space(self, row, pages):
        sid = row["id"]
        members = _any.list_members(sid)
        self.who.names.add_members(members)
        one_to_one = row.get("spaceType") == ONE_TO_ONE
        peer = None
        if one_to_one:
            others = [m["identity"] for m in members
                      if m.get("identity") and m["identity"] != self.me]
            peer = others[0] if others else None
            if self.who.kept_out(peer):
                self.stats["excludedChats"] += 1          # never queried
                return
        self.stats["spaces"] += 1
        self.scan_chat(sid, _any.general_chat(sid), one_to_one, peer)
        if pages:
            self.scan_pages(sid)

    # --- one chat -------------------------------------------------------------
    def _read(self, sid, chat):
        """(messages to handle, ascending; the newest _ver.id read).

        A chat with no cursor is read from its newest page back to a floor,
        FIRST_VISIT_DAYS ago, which `config/chatScan.floor` then keeps for
        the chat: later reads pass over anything created before it (a
        message synced in late), so a visit a stop cut short cannot read
        past the window the first one set."""
        last = self.cursor.get(chat)
        if last:
            rows = _any.query(sid, chat, "chat_messages", filter={"_ver.id": {"$gt": last}},
                              sort=["_ver.id"], limit=PER_CHAT)
        else:
            rows = list(reversed(_any.query(sid, chat, "chat_messages", sort=["-_ver.id"],
                                            limit=PER_CHAT)))
            self.floor[chat] = self.today - FIRST_VISIT_DAYS * DAY
            kit.write_config(self.space, self.hub, self.datasets, "chatScan.floor", self.floor)
        newest = max((v for v in (ver_id(r) for r in rows) if v), default=None)
        floor = self.floor.get(chat)
        if floor is not None:
            rows = [r for r in rows if (ts_s(r.get("createdAt")) or 0) > floor]  # noqa: F821
        return rows, newest

    def scan_chat(self, sid, chat, one_to_one, peer):
        rows, newest = self._read(sid, chat)
        self.stats["chats"] += 1
        by_id = {r["id"]: r for r in rows if r.get("id")}
        mine = [r["id"] for r in rows if r.get("creator") == self.me]
        own_links = [record_link(sid, chat, "chat_messages", rid) for rid in mine]
        done = promises.extracted(self.space, self.hub, self.datasets["promises"], own_links) \
            if self.consent and own_links else set()
        ctx = {"sid": sid, "chat": chat, "one_to_one": one_to_one, "peer": peer,
               "by_id": by_id, "mine": mine, "done": done, "asks": [], "promises": []}
        last, finished = self.cursor.get(chat), True
        for row in rows:
            if self.message(row, ctx) == "stop":
                finished = False
                break
            vid = ver_id(row)
            if vid and (last is None or vid > last):
                last = vid
            self.stats["messages"] += 1
        if finished and newest and (last is None or newest > last):
            last = newest
        self.stats["asks"] += _write_new(self.space, self.hub, self.datasets["asks"],
                                         ctx["asks"], asks_rejected)
        self.stats["promises"] += _write_new(self.space, self.hub, self.datasets["promises"],
                                             ctx["promises"], promises_rejected)
        if last and last != self.cursor.get(chat):
            self.cursor[chat] = last
            kit.write_config(self.space, self.hub, self.datasets, "chatScan.cursor",
                             self.cursor)

    def _involved(self, row, ctx):
        people = {row.get("creator"), *mentioned(row)}
        reply = row.get("replyToMessageId")
        if reply:
            target = ctx["by_id"].get(reply)
            if target is None:
                hits = _any.query(ctx["sid"], ctx["chat"], "chat_messages",
                                  filter={"id": reply}, limit=1)
                target = hits[0] if hits else {}
            people.add(target.get("creator"))
        return {p for p in people if p}

    def message(self, row, ctx):
        """Handle one message → "next", or "stop" to leave the rest of the chat
        (and the cursor before this message) for the next run."""
        if is_passed_over(row):
            self.stats["passedOver"] += 1
            return "next"
        if any(self.who.kept_out(p) for p in self._involved(row, ctx)):
            self.stats["excludedSkipped"] += 1        # its text is never looked at
            return "next"
        text = chat_text(row.get("text"))
        if not text:
            return "next"
        if row.get("creator") == self.me:
            return self.own(row, text, ctx)
        if not addressed_to_me(row, self.me, ctx["one_to_one"], ctx["mine"]):
            self.stats["ignored"] += 1
            return "next"
        self.stats["addressed"] += 1
        ctx["asks"].extend(self.asks_of(row, text, ctx))
        return "next"

    def own(self, row, text, ctx):
        self.stats["own"] += 1
        if not self.consent:
            self.stats["noConsent"] += 1
            return "next"
        link = record_link(ctx["sid"], ctx["chat"], "chat_messages", row["id"])
        if link in ctx["done"]:
            self.stats["alreadyExtracted"] += 1
            return "next"
        if self.failures >= MAX_FAILURES:
            return "stop"
        if self.calls + (3 if self.gate() else 2) > MAX_CALLS:
            self.stats["capped"] += 1
            return "stop"
        try:
            items, used = promises.extract(text, self.tier, self.gate(), self.stats)
        except promises.model_failed as e:
            self.failures += 1
            self.calls += getattr(e, "calls", 1)      # what the failed extract spent
            self.stats["providerFailed"] += 1
            return "stop"
        self.calls += used
        self.answered += 1
        at = ts_s(row.get("createdAt")) or self.today  # noqa: F821 - guest global
        pid = self.who.person(ctx["peer"]) if ctx["one_to_one"] else None
        for it in items or []:
            ctx["promises"].append(self._promise(row, it, link, at, pid, ctx))
        return "next"

    def _promise(self, row, item, link, at, person_id, ctx):
        quote = item["quote"]
        fields = {"text": quote, "quote": quote, "source": link, "mailboxId": ctx["chat"],
                  "channel": "chat", "madeAt": instant(at),  # noqa: F821 - guest global
                  "status": "open"}
        due = promises.due_at(quote, at)
        if due is None:
            due = promises.due_hint(item.get("due"))
        if due is not None:
            fields["dueAt"] = instant(due)  # noqa: F821 - guest global
        if person_id:
            fields["personId"] = person_id
        return {"id": promises.promise_id(row["id"], quote), "fields": fields}

    def asks_of(self, row, text, ctx):
        """One ask per sentence with a request phrase or a trailing "?". The
        stored `suggestedDeadline` is the rule table's for the run's day, so
        it is never already past when it lands (write-once)."""
        creator = row.get("creator") or ""
        at = ts_s(row.get("createdAt")) or self.today  # noqa: F821 - guest global
        link = record_link(ctx["sid"], ctx["chat"], "chat_messages", row["id"])
        out = []
        for sentence in sentences(text):
            kind = ask_kind(sentence)
            if kind is None:
                continue
            quote = sentence[:QUOTE_MAX].strip()
            norm = kit.normalize_ws(quote)
            fields = self._ask_fields(creator, ctx, kind, norm, quote, link, at)
            deadline = tasks.deadline_for("ask", norm, at, today=self.today)
            if deadline is not None:
                fields["suggestedDeadline"] = instant(deadline)  # noqa: F821 - guest global
            out.append({"id": ask_id(row["id"], norm), "fields": fields})
        return out

    def _ask_fields(self, creator, ctx, kind, text, quote, link, at):
        fields = {"senderIdentity": kit.identity_uri(creator), "spaceId": ctx["sid"],
                  "chatId": ctx["chat"], "kind": kind, "text": text, "quote": quote,
                  "source": link, "askedAt": instant(at),  # noqa: F821 - guest global
                  "status": "open"}
        pid = self.who.person(creator)
        if pid:
            fields["personId"] = pid
        return fields

    # --- pages (behind brain.chatScan.pages) --------------------------------------
    def scan_pages(self, sid):
        cursor = _cursor(self.space, self.hub, self.datasets, "chatScan.pagesCursor")
        since = cursor.get(sid)
        if not isinstance(since, (int, float)):
            since = self.today - PAGE_WINDOW_S
        pages = _any.query_objects(sid, filter={
            "any.type": "page", "any.collections": {"$nin": ["bin"]},
            "modifiedAt": {"$gt": instant(since)}},  # noqa: F821 - guest global
            sort=["modifiedAt"], limit=PAGES_PER_SPACE)
        records, checked, last = [], {}, since
        for page in pages:
            last = max(last, ts_s(page.get("modifiedAt")) or last)  # noqa: F821 - guest global
            self.stats["pages"] += 1
            editor = page.get("modifiedBy") or page.get("author") or self.me
            if self.who.kept_out(editor):
                self.stats["excludedSkipped"] += 1
                continue
            fresh, ticked = self._checklist(sid, page, editor)
            records.extend(fresh)
            checked[page["id"]] = ticked
        self.stats["checklist"] += _write_new(self.space, self.hub, self.datasets["asks"],
                                              records, asks_rejected)
        self.stats["checked"] += self._close_checked(checked)
        if last != since:
            cursor[sid] = last
            kit.write_config(self.space, self.hub, self.datasets, "chatScan.pagesCursor",
                             cursor)

    def _checklist(self, sid, page, editor):
        """(the checklist asks a page's unchecked items make, the ids of its
        checked blocks)."""
        at = ts_s(page.get("modifiedAt")) or self.today  # noqa: F821 - guest global
        out, ticked = [], set()
        for block in _any.query(sid, page["id"], "editor_blocks",
                                filter={"type": "check_list_item"}, limit=BLOCKS_PER_PAGE):
            if (block.get("style") or {}).get("checked") is True:
                ticked.add(block["id"])
                continue
            quote = chat_text(block.get("text"))[:QUOTE_MAX].strip()
            if not quote:
                continue
            norm = kit.normalize_ws(quote)
            link = record_link(sid, page["id"], "editor_blocks", block["id"])
            fields = {"senderIdentity": kit.identity_uri(editor), "spaceId": sid,
                      "chatId": page["id"], "kind": "checklist", "text": norm,
                      "quote": quote, "source": link,
                      "askedAt": instant(at),  # noqa: F821 - guest global
                      "status": "open"}
            out.append({"id": ask_id(block["id"], norm), "fields": fields})
        return out, ticked

    def _close_checked(self, checked):
        """Mark done the open checklist asks of the pages read whose block the
        user has ticked since (ADR-033 §7.8) → how many."""
        pages = sorted(p for p, blocks in checked.items() if blocks)
        if not pages:
            return 0
        rows = _any.query(self.space, self.hub, self.datasets["asks"],
                          filter={"kind": "checklist", "status": "open",
                                  "chatId": {"$in": pages}}, limit=OPEN_CHECKLIST)
        closed = 0
        for r in rows:
            if block_of(r.get("source")) in checked.get(r.get("chatId"), ()):
                _close(self.space, self.hub, self.datasets["asks"], r,
                       "closing a checklist ask whose item is checked")
                closed += 1
        return closed


# --- after the scan ---------------------------------------------------------------

def _close_completed(space, hub, datasets):
    """Mark done the open asks whose task the user completed."""
    rows = _any.query(space, hub, datasets["asks"], filter={"status": "open"}, limit=500)
    linked = sorted({r["taskId"] for r in rows if r.get("taskId")})
    if not linked:
        return 0
    found = _any.query_objects(space, filter={"id": {"$in": linked}}, limit=len(linked))
    done = {t["id"] for t in found if (t.get("task") or {}).get("completed") is True}
    closed = 0
    for r in rows:
        if r.get("taskId") in done:
            _close(space, hub, datasets["asks"], r, "closing an ask whose task is done")
            closed += 1
    return closed


def _plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def suggestion_card(space, hub, datasets, today):
    """The day's task_suggestion card when the hub keeps cards and the day has
    none → its id, or None (ADR-033 §7.7). The title counts every open
    suggestion; the card carries the newest CARD_ITEMS of them."""
    if not datasets.get("cards"):
        return None
    day = fmt_ts(today, "%Y-%m-%d", 0)[:10]  # noqa: F821 - guest global
    subject = f"tasks:{day}"
    cid = card_id("task_suggestion", subject)
    if kit.hub_record(space, hub, datasets["cards"], cid) is not None:
        return None
    try:
        every = tasks.suggest(limit=None)
    except kit.not_set_up:
        return None                         # mid-setup: brain.status is not ready
    if not every:
        return None
    items = every[:CARD_ITEMS]
    n = sum(1 for i in every if i.get("origin") == "promise")
    title = f"{_plural(n, 'promise')} and {_plural(len(every) - n, 'ask')} without a task"
    sid = signal_id(today, "task_suggestion", subject)
    card = {"kind": "task_suggestion", "title": title,
            "reason": "Things you owe that have no task yet; pick the ones to make tasks.",
            "subjectKey": subject, "status": "open",
            "personIds": sorted({i["personId"] for i in items if i.get("personId")}),
            "items": [{k: v for k, v in i.items() if v is not None} for i in items]}
    if datasets.get("signals"):
        card["signalId"] = sid
    if not _write_new(space, hub, datasets["cards"], [{"id": cid, "fields": card}],
                      cards_rejected):
        return None                         # the user deleted today's card
    if datasets.get("signals"):
        kit.upsert(space, hub, datasets["signals"], sid, {
            "tick": today, "kind": "task_suggestion", "subject": subject,
            "decision": "card", "reason": title, "cardId": cid},
            "writing a task_suggestion signal")
    return cid


def main(args):
    parsed = kit.job_args(args, "asks", "promises")
    if parsed is None:
        raise unconfigured("the brain chat scan needs space, hub and datasets with asks "
                           "and promises — set the brain up")
    space, hub, datasets = parsed
    watched = watched_spaces()
    if not watched:
        return {"skipped": "no chats"}
    run = _Run(space, hub, datasets, (args or {}).get("tier") or promises.TIER)
    pages = pages_enabled()
    rows = {r.get("id"): r for r in _any.list_spaces()}
    for sid in watched:
        row = rows.get(sid)
        if (row is None or sid == space or row.get("status") != "active"
                or row.get("ownRole") not in WRITABLE):
            run.stats["spacesSkipped"] += 1
            continue
        run.scan_space(row, pages)
    run.stats["modelCalls"] = run.calls
    run.stats["closed"] = _close_completed(space, hub, datasets)
    card = suggestion_card(space, hub, datasets, run.today)
    run.stats["cards"] = 1 if card else 0
    _merge_stats(space, hub, datasets, run.stats)
    if run.failures and not run.answered:
        raise provider_failed(f"every model call of the run failed ({run.failures})")
    return {**run.stats, "card": card}
