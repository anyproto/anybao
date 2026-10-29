"""programs/brainHeartbeat@v1 — the brain's decision loop and the reply
hand-off's sent detection (ADR-033 §4, §5), under the REAL guest kernel.
Pinned: the probes (unconfigured args raise with no writes; a missing or
unreadable exclusion list or blocklist writes one refused signal and raises;
a fresh tick writes no gap, an old one writes one heartbeat_gap signal; every
successful run records its tick); every mail decision branch writes one
signal with a decision and a reason; a message addressed to the user with no
reply becomes one reply card and one draft one-shot; mail from someone kept
out gets a refused signal and nothing else; the mail cursor is the record's
arrival order, so mail the backfill lands later is still carded, a same-
millisecond page boundary loses nothing, and the reply window bounds every
read; snoozed cards wake; a nudge is written once per thread; overdue
promises, meetings soon, calendar conflicts and fact conflicts each become
one card; one notice per run that made cards; a standup only when something
happened; cursors advance after the writes; a re-run with no new input
writes nothing; the heartbeat owns the attempt counter and every one-shot id
is fresh and existence-checked; the trigger lands before the counter moves;
a card whose source is gone is dismissed with its reason; drafting waits for
the user's consent and its budget. Sent detection: the user's message after
the approval carrying the approved text marks it — one sends row, the
approval consumed, the card sent, no chat message; what is not it marks
nothing; a reply sent as a new message to the same recipients is found; the
match survives the HTML-to-markdown round trip; a second run writes nothing;
seven days without a match open one send_check card, a later detection
answers it, and fifty approvals with check cards never starve a fresh one; a
recipient kept out means no mail is read; a half-written Mark sent is
completed without a second row."""

import hashlib

import pytest
from brain_fakes import ANCHOR, DATASETS, HUB, FakeSpace, job_args
from connectorenv import connector_kernel

NOW = 1_800_000_000
DAY = 86400
ME = "me@example.com"
ADA = "Ada Lovelace <ada@lab.org>"
CONSENT = {"allowed": True, "requestedAt": "t", "by": "chat"}
SUBJECT = "connectors:brainHeartbeat@v1"
MAIL_OF = "any://o/bao-space/mb1/email_messages/"


def kernel(space, *, consent=True):
    def eff(name, payload):
        if name == "time.now":
            return {"epoch": float(NOW)}
        if name == "config.get":
            if payload["key"] == "brain.model" and consent is not None:
                return {"value": {**CONSENT, "allowed": consent}}
            raise KeyError(f"no config value for {payload['key']!r}")
        raise AssertionError(f"unexpected effect {name}")

    return connector_kernel(effect=eff, any_client=space).use("brainHeartbeat@v1")


def args(space, **extra):
    return {**job_args(space), "tier": "brain.drafter", **extra}


def brain(*, people=True):
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    s.seed_lists()
    if people:
        s.add_object("ada", "profile", ["person", "brain_person"], name="Ada Lovelace",
                     person={"email": "ada@lab.org"}, brain_person={"emails": ["ada@lab.org"]})
    return s


def mail(s, mid, frm=ADA, to=ME, cc="", thread=None, at=NOW - 3600, subject="Hello", ver=None):
    s.add_mail("mb1", mid, ver=ver, **{"from": frm, "to": to, "cc": cc}, subject=subject,
               threadId=thread or f"t-{mid}", body="secret body", internalDate=at * 1000,
               participants=sorted({a.split("<")[-1].rstrip(">").strip().lower()
                                    for a in (frm, to, cc) if a}))


def ver_of(s, mid):
    return s.records[("mb1", "email_messages")][mid]["_ver"]["id"]


def signals(s):
    return list(s.store("signals").values())


def one_signal(s, **match):
    hits = [x for x in signals(s) if all(x.get(k) == v for k, v in match.items())]
    assert len(hits) == 1, (match, signals(s))
    return hits[0]


def cards(s, kind=None):
    return [c for c in s.store("cards").values() if kind is None or c["kind"] == kind]


def run(s, **kw):
    mod = kernel(s, **{k: v for k, v in kw.items() if k == "consent"})
    return mod, mod.main(args(s))


def email_filters(s):
    """Record the filters of every email_messages query on `s`."""
    filters = []
    real = s.query

    def query(space, oid, ds, filter=None, **kw):
        if ds == "email_messages":
            filters.append(filter or {})
        return real(space, oid, ds, filter=filter, **kw)

    s.query = query
    return filters


# --- the pure decision ----------------------------------------------------------

def test_decide_mail_enumerates_every_branch():
    mod = kernel(brain())
    quiet, bad = mod.quiet_patterns(["^news@", "("])
    assert bad == 1
    to_me = {"from": ADA, "to": ME, "cc": ""}
    cases = [
        (to_me, False, False, ("new_mail", "draft", "addressed to you, no reply yet", True)),
        (to_me, True, False, ("new_mail", "none", "already replied", False)),
        (to_me, False, True, ("new_mail", "refused", "kept out of the brain", False)),
        ({"from": "no-reply@shop.com", "to": ME}, False, False,
         ("new_mail", "quiet", "automated sender", False)),
        ({"from": "news@lab.org", "to": ME}, False, False,
         ("new_mail", "quiet", "automated sender", False)),
        ({"from": "hello@shop.com", "to": ME, "labelIds": ["INBOX", "CATEGORY_PROMOTIONS"]},
         False, False, ("new_mail", "quiet", "a Gmail bulk tab", False)),
        ({"from": ADA, "to": ME, "labelIds": ["CATEGORY_UPDATES"]}, False, False,
         ("new_mail", "quiet", "a Gmail bulk tab", False)),
        ({"from": ADA, "to": ME, "labelIds": ["INBOX", "CATEGORY_PERSONAL"]}, False, False,
         ("new_mail", "draft", "addressed to you, no reply yet", True)),
        ({"from": ADA, "to": "bob@lab.org", "cc": ME}, False, False,
         ("cc_thread", "quiet", "copied, not addressed", False)),
        ({"from": ADA, "to": "list@lab.org"}, False, False,
         ("new_mail", "quiet", "not addressed to you", False)),
        ({"from": f"Me <{ME}>", "to": "ada@lab.org"}, False, False, None),
    ]
    for message, replied, kept, want in cases:
        got = mod.decide_mail(message, ME, replied, quiet, kept)
        assert (tuple(got) if got else None) == want, (message, replied, kept)


def test_card_ids_are_sha256_of_kind_and_identity():
    mod = kernel(brain())
    assert mod.card_id("nudge", "t1") == hashlib.sha256(b"nudge|t1").hexdigest()
    assert mod.reply_card_id("t1", "m1") == hashlib.sha256(b"reply|t1|m1").hexdigest()
    assert [mod.draft_trigger_id("m1", n) for n in (1, 2, 3)] == [
        "brain-draft-m1", "brain-draft-m1-a2", "brain-draft-m1-a3"]


# --- the mail reader --------------------------------------------------------------

def test_mail_to_the_user_becomes_one_reply_card_one_signal_and_one_draft_one_shot():
    s = brain()
    mail(s, "m1", thread="t1")
    mod, out = run(s)
    (card,) = cards(s)
    cid = mod.reply_card_id("t1", "m1")
    assert out["cards"] == [cid] and card["id"] == cid
    assert card["kind"] == "reply" and card["title"] == "Hello" and card["status"] == "open"
    assert card["subjectKey"] == "t1" and card["personIds"] == ["ada"]
    assert card["sourceUri"] == f"{MAIL_OF}m1"
    assert card["reason"] == "addressed to you, no reply yet"
    assert card["draftAttempts"] == 1                       # the heartbeat counted it
    sig = one_signal(s, kind="new_mail")
    assert sig["decision"] == "draft" and sig["cardId"] == cid
    assert sig["id"] == card["signalId"] and sig["subject"] == card["sourceUri"]
    (trigger,) = s.triggers().values()
    assert trigger["id"] == "brain-draft-m1" and trigger["kind"] == "once"
    assert trigger["program"] == "connectors:brainDrafter@v1" and trigger["enabled"] is True
    assert trigger["spec"] == {"at": NOW} and "owner" not in trigger
    a = trigger["args"]
    assert a["cardId"] == cid and a["messageId"] == "m1" and a["mailboxId"] == "mb1"
    assert a["threadId"] == "t1" and a["recipients"] == ["ada@lab.org"]
    assert a["provenance"] == "thread" and a["tier"] == "brain.drafter"
    assert a["signalId"] == sig["id"] and a["datasets"]["drafts"] == DATASETS["drafts"]


def test_quiet_cc_already_replied_and_not_addressed_write_signals_and_no_card():
    s = brain()
    s.set_config("drafting", {"maxPerRun": 3, "replyWindowDays": 5, "meetingSoonHours": 24,
                              "quietSenders": ["^digest@"]})
    mail(s, "q1", frm="noreply@shop.com")
    mail(s, "q2", frm="digest@lab.org")
    mail(s, "c1", to="bob@lab.org", cc=ME)
    mail(s, "n1", to="all@lab.org")
    mail(s, "r1", thread="tr", at=NOW - 7200)
    mail(s, "r2", frm=f"Me <{ME}>", to=ADA, thread="tr", at=NOW - 3600)   # the user replied
    run(s)
    assert cards(s) == [] and s.triggers() == {}
    by_subject = {x["subject"].rsplit("/", 1)[-1]: x for x in signals(s)}
    assert sorted(by_subject) == ["c1", "n1", "q1", "q2", "r1"]      # own mail: no signal
    assert (by_subject["q1"]["decision"], by_subject["q1"]["reason"]) == ("quiet",
                                                                          "automated sender")
    assert by_subject["q2"]["decision"] == "quiet"
    assert by_subject["c1"]["kind"] == "cc_thread" and by_subject["c1"]["decision"] == "quiet"
    assert by_subject["n1"]["reason"] == "not addressed to you"
    assert (by_subject["r1"]["decision"], by_subject["r1"]["reason"]) == ("none",
                                                                          "already replied")
    assert all(x["reason"] for x in signals(s))


def test_mail_from_someone_kept_out_is_refused_and_never_carded():
    s = brain()
    s.set_config("exclusions", {"version": 1, "personIds": [], "emails": ["ada@lab.org"],
                                "domains": []})
    mail(s, "m1")
    filters = email_filters(s)
    run(s)
    assert cards(s) == [] and s.triggers() == {}
    sig = one_signal(s, kind="new_mail")
    assert sig["decision"] == "refused" and sig["reason"] == "kept out of the brain"
    # no thread was read for it: the decision came from the headers alone
    assert not [f for f in filters if "threadId" in f]


def test_only_the_newest_unanswered_message_of_a_thread_gets_the_card():
    s = brain()
    mail(s, "m1", thread="t1", at=NOW - 7200)
    mail(s, "m2", thread="t1", at=NOW - 3600)
    mod, _ = run(s)
    (card,) = cards(s)
    assert card["id"] == mod.reply_card_id("t1", "m2")
    older = one_signal(s, subject=f"{MAIL_OF}m1")
    assert older["decision"] == "none" and "newer message" in older["reason"]


def test_an_open_card_covers_a_new_message_and_a_closed_one_does_not():
    s = brain()
    mail(s, "m1", thread="t1", at=NOW - 3 * DAY)
    mod, _ = run(s)
    first = mod.reply_card_id("t1", "m1")
    mail(s, "m2", thread="t1", at=NOW - 60)
    run(s)
    assert [c["id"] for c in cards(s)] == [first]                  # covered
    covered = one_signal(s, subject=f"{MAIL_OF}m2")
    assert covered["decision"] == "card" and covered["cardId"] == first
    s.store("cards")[first]["status"] = "dismissed"
    mail(s, "m3", thread="t1", at=NOW - 30)
    run(s)
    assert sorted(c["id"] for c in cards(s)) == sorted([first, mod.reply_card_id("t1", "m3")])


def test_a_message_whose_own_card_is_closed_gets_a_signal_and_no_second_card():
    s = brain()
    mail(s, "m1", thread="t1")
    mod, _ = run(s)
    cid = mod.reply_card_id("t1", "m1")
    s.store("cards")[cid]["status"] = "dismissed"
    s.store("signals").clear()
    s.set_config("heartbeat.cursor", {"mail": {}, "stale": {}, "factConflicts": 0})
    run(s)                                     # the message is read again
    assert [c["id"] for c in cards(s)] == [cid]
    sig = one_signal(s, kind="new_mail")
    assert sig["decision"] == "none" and sig["cardId"] == cid
    assert sig["reason"] == "a closed reply card covers this thread"


def test_a_rerun_with_no_new_input_writes_nothing():
    s = brain()
    mail(s, "m1", thread="t1")
    run(s)
    snapshot = {k: dict(v) for k, v in s.records.items()}
    chats = len(s.chat)
    _, out = run(s)
    assert out["cards"] == [] and out["signals"] == 0 and out["standup"] is False
    assert len(s.chat) == chats
    for key in (DATASETS["cards"], DATASETS["signals"], DATASETS["standups"]):
        assert s.records[(HUB, key)] == snapshot[(HUB, key)]
    assert s.triggers() == snapshot[(ANCHOR, "agent_triggers")]


def test_the_mail_cursor_advances_only_after_the_writes_land():
    s = brain()
    mail(s, "m1", thread="t1")
    real = s.upsert_records

    def refuse_cards(space, oid, ds, records, page_size=None):
        if ds == DATASETS["cards"]:
            return {"rejections": [{"index": 0, "id": records[0]["id"], "reason": "nope"}]}
        return real(space, oid, ds, records, page_size)

    s.upsert_records = refuse_cards
    with pytest.raises(Exception, match="writing cards: nope"):
        run(s)
    assert s.config("heartbeat.cursor") is None
    s.upsert_records = real
    run(s)
    # the cursor is the record's arrival order (_ver.id), not its date
    assert s.config("heartbeat.cursor")["mail"] == {"mb1": ver_of(s, "m1")}
    assert len(cards(s)) == 1


def test_a_first_run_reads_only_the_reply_window():
    s = brain()
    mail(s, "old", thread="t-old", at=NOW - 6 * DAY)
    mail(s, "new", thread="t-new", at=NOW - DAY)
    mod, _ = run(s)
    assert [c["subjectKey"] for c in cards(s)] == ["t-new"]


def test_mail_the_backfill_lands_later_is_still_carded_and_old_mail_is_not():
    # the Gmail backfill writes newest first, in slices across its ticks: the
    # first tick sees today's mail, the next ones the rest of the window
    s = brain()
    mail(s, "today", thread="t-today", at=NOW - 3600, ver="v-0001")
    mod, _ = run(s)
    assert [c["subjectKey"] for c in cards(s)] == ["t-today"]
    assert s.config("heartbeat.cursor")["mail"] == {"mb1": "v-0001"}
    mail(s, "two-days", thread="t-two", at=NOW - 2 * DAY, ver="v-0002")   # older, landed later
    mail(s, "last-month", thread="t-month", at=NOW - 30 * DAY, ver="v-0003")
    _, out = run(s)
    assert sorted(c["subjectKey"] for c in cards(s)) == ["t-today", "t-two"]
    sig = one_signal(s, subject=f"{MAIL_OF}two-days")
    assert sig["decision"] == "draft" and sig["cardId"] == mod.reply_card_id("t-two", "two-days")
    assert out["counts"]["mails"] == 1                       # the month-old one was never read
    assert s.config("heartbeat.cursor")["mail"] == {"mb1": "v-0002"}


def test_a_same_millisecond_page_boundary_loses_no_mail():
    s = brain()
    for i in range(201):
        mail(s, f"m{i:03d}", to="all@lab.org", thread=f"t{i}", at=NOW - 3600)   # one ms
    _, out = run(s)
    assert out["counts"]["mails"] == 200                     # one page
    _, out = run(s)
    assert out["counts"]["mails"] == 1                       # the 201st, next run
    assert len({x["subject"] for x in signals(s)}) == 201
    _, out = run(s)
    assert out["counts"]["mails"] == 0


def test_the_mail_pass_reads_threads_covering_cards_and_persons_once_per_mailbox():
    s = brain()
    s.add_object("bob", "profile", ["person", "brain_person"], name="Bob",
                 brain_person={"emails": ["bob@lab.org"]})
    for i in range(4):
        mail(s, f"m{i}", frm=ADA if i % 2 else "Bob <bob@lab.org>", thread=f"t{i}",
             at=NOW - 3600 + i)
    before = len(s.calls)
    run(s)
    queries = [c for c in s.calls[before:] if c[0] == "query"]
    assert [c for c in queries if c[1:] == ("mb1", "email_messages")][1:2] == [
        ("query", "mb1", "email_messages")]                  # the page, then one thread read
    assert len([c for c in queries if c[1:] == ("mb1", "email_messages")]) == 2 + 4  # + _arm
    assert len([c for c in queries if c[1:] == (HUB, "cards")]) == 4   # snoozed, covering,
    assert len(cards(s)) == 4                                           # exists, open replies
    assert all(c["personIds"] for c in cards(s))


# --- drafting -----------------------------------------------------------------------

def test_no_consent_no_draft_and_the_card_says_why():
    s = brain()
    mail(s, "m1", thread="t1")
    _, out = run(s, consent=False)
    (card,) = cards(s)
    assert s.triggers() == {} and card["draftAttempts"] == 0 and out["armed"] == {}
    sig = one_signal(s, kind="new_mail")
    assert sig["decision"] == "card" and "consent" in sig["reason"]
    (standup,) = s.store("standups").values()
    assert any("model" in n for n in standup["needs"])
    run(s)                                    # consent given: the next run arms it
    assert list(s.triggers()) == ["brain-draft-m1"]


def test_the_draft_budget_caps_a_run_and_a_later_run_arms_the_rest():
    s = brain()
    for i in range(5):
        mail(s, f"m{i}", thread=f"t{i}", at=NOW - 3600 + i)
    _, out = run(s)
    assert len(out["armed"]) == 3 and len(s.triggers()) == 3
    budget = [x for x in signals(s) if x["kind"] == "new_mail" and x["decision"] == "card"]
    assert len(budget) == 2 and all("draft budget" in x["reason"] for x in budget)
    _, out = run(s)
    assert len(out["armed"]) == 2 and len(s.triggers()) == 5


def test_a_pending_one_shot_is_not_doubled_and_a_fired_one_is_never_rewritten():
    s = brain()
    mail(s, "m1", thread="t1")
    mod, _ = run(s)
    cid = mod.reply_card_id("t1", "m1")
    run(s)                                     # not fired yet: nothing new
    assert list(s.triggers()) == ["brain-draft-m1"]
    assert s.store("cards")[cid]["draftAttempts"] == 1
    s.triggers()["brain-draft-m1"]["lastRunAt"] = NOW    # fired, wrote no draft
    before = dict(s.triggers()["brain-draft-m1"])
    run(s)
    assert sorted(s.triggers()) == ["brain-draft-m1", "brain-draft-m1-a2"]
    assert s.triggers()["brain-draft-m1"] == before       # never re-upserted
    assert s.store("cards")[cid]["draftAttempts"] == 2
    s.triggers()["brain-draft-m1-a2"]["lastRunAt"] = NOW
    run(s)
    s.triggers()["brain-draft-m1-a3"]["lastRunAt"] = NOW
    run(s)
    assert sorted(s.triggers()) == ["brain-draft-m1", "brain-draft-m1-a2",
                                    "brain-draft-m1-a3"]
    assert s.store("cards")[cid]["draftAttempts"] == 3     # three attempts: it stays a card
    assert not [c for c in s.calls if c[0] == "upsert_record" and c[1] == "agent_triggers"
                and c[2] == "brain-draft-m1"][1:]


def test_the_trigger_lands_before_the_counter_moves():
    s = brain()
    mail(s, "m1", thread="t1")
    mod, _ = run(s, consent=False)
    cid = mod.reply_card_id("t1", "m1")
    real = s.upsert_record

    def refuse_trigger(space, oid, ds, rid, value):
        if ds == "agent_triggers":
            return {"recordIds": [], "rejections": [{"reason": "nope"}]}
        return real(space, oid, ds, rid, value)

    s.upsert_record = refuse_trigger
    with pytest.raises(Exception, match="arming brain-draft-m1: nope"):
        run(s)
    assert s.store("cards")[cid]["draftAttempts"] == 0      # the attempt was not spent
    s.upsert_record = real
    run(s)
    assert list(s.triggers()) == ["brain-draft-m1"]
    assert s.store("cards")[cid]["draftAttempts"] == 1
    order = [c for c in s.calls if (c[0] == "upsert_record" and c[1] == "agent_triggers")
             or (c[0] == "modify" and c[1] == "cards" and "draftAttempts" in c[3])]
    assert order[-2][0] == "upsert_record" and order[-1][0] == "modify"


def test_an_armed_id_the_counter_missed_is_caught_up_not_rewritten():
    s = brain()
    mail(s, "m1", thread="t1")
    mod, _ = run(s, consent=False)
    cid = mod.reply_card_id("t1", "m1")
    s.records.setdefault((ANCHOR, "agent_triggers"), {})["brain-draft-m1"] = {
        "id": "brain-draft-m1", "kind": "once", "lastRunAt": NOW}
    run(s)
    assert s.store("cards")[cid]["draftAttempts"] == 1
    assert ("upsert_record", "agent_triggers", "brain-draft-m1") not in s.calls


def test_a_card_with_a_draft_or_three_attempts_arms_nothing():
    s = brain()
    mail(s, "m1", thread="t1")
    mail(s, "m2", thread="t2")
    mod, _ = run(s, consent=False)
    s.store("cards")[mod.reply_card_id("t1", "m1")]["draftAttempts"] = 3
    s.store("cards")[mod.reply_card_id("t2", "m2")]["draftId"] = "d1"
    _, out = run(s)
    assert out["armed"] == {} and s.triggers() == {}


def test_a_source_message_kept_out_after_the_card_is_dismissed_not_drafted():
    s = brain()
    mail(s, "m1", thread="t1")
    mod, _ = run(s, consent=False)
    cid = mod.reply_card_id("t1", "m1")
    s.set_config("exclusions", {"version": 1, "personIds": [], "emails": [],
                                "domains": ["lab.org"]})
    _, out = run(s)
    assert out["armed"] == {} and s.triggers() == {}
    card = s.store("cards")[cid]
    assert card["status"] == "dismissed" and card["answer"] == "source gone or kept out"


def test_a_card_whose_source_is_gone_is_dismissed_once_with_its_reason():
    s = brain()
    mail(s, "m1", thread="t1")
    mod, _ = run(s, consent=False)
    cid = mod.reply_card_id("t1", "m1")
    del s.records[("mb1", "email_messages")]["m1"]
    _, out = run(s)
    card = s.store("cards")[cid]
    assert card["status"] == "dismissed" and card["answer"] == "source gone or kept out"
    assert out["armed"] == {} and s.triggers() == {}
    sig = one_signal(s, kind="new_mail", tick=NOW)
    assert sig["decision"] == "none" and sig["cardId"] == cid
    assert sig["reason"] == "source gone or kept out; card dismissed"
    n = len(s.calls)
    _, out = run(s)                                 # closed: not looked at again
    assert out["signals"] == 0 and not [c for c in s.calls[n:] if c[0] == "modify"]


# --- the other passes ---------------------------------------------------------------

def test_a_snoozed_card_wakes_when_its_time_has_come():
    s = brain()
    s.records.setdefault((HUB, DATASETS["cards"]), {}).update({
        "c1": {"id": "c1", "kind": "nudge", "status": "snoozed",
               "snoozedUntil": {"$date": (NOW - 60) * 1000}},
        "c2": {"id": "c2", "kind": "nudge", "status": "snoozed",
               "snoozedUntil": {"$date": (NOW + 60) * 1000}}})
    run(s)
    assert s.store("cards")["c1"]["status"] == "open"
    assert s.store("cards")["c2"]["status"] == "snoozed"
    sig = one_signal(s, kind="card_woken")
    assert sig["cardId"] == "c1" and sig["decision"] == "card"


def test_a_stale_thread_gets_one_nudge_and_a_dismissed_one_stays_dismissed():
    s = brain()
    s.add_mail("mb1", "s1", **{"from": f"Me <{ME}>", "to": ADA}, subject="Proposal",
               threadId="ts", internalDate=(NOW - 6 * DAY) * 1000)
    s.add_mail("mb1", "s2", **{"from": f"Me <{ME}>", "to": ADA}, subject="Answered",
               threadId="ta", internalDate=(NOW - 6 * DAY) * 1000)
    s.add_mail("mb1", "s3", **{"from": ADA, "to": ME}, subject="Re: Answered",
               threadId="ta", internalDate=(NOW - 5.5 * DAY) * 1000)
    mod, _ = run(s)
    (nudge,) = cards(s, "nudge")
    assert nudge["id"] == mod.card_id("nudge", "ts") and nudge["personIds"] == ["ada"]
    assert nudge["reason"] == "you wrote last, no answer for 6 days"
    assert one_signal(s, kind="stale_thread")["decision"] == "card"
    s.store("cards")[nudge["id"]]["status"] = "dismissed"
    s.set_config("heartbeat.cursor", {"mail": {}, "stale": {}, "factConflicts": 0})
    run(s)                                     # even a reset cursor writes no second one
    assert len(cards(s, "nudge")) == 1
    assert s.store("cards")[nudge["id"]]["status"] == "dismissed"


def test_a_stale_thread_to_no_known_person_gets_no_nudge():
    s = brain(people=False)
    s.add_mail("mb1", "s1", **{"from": f"Me <{ME}>", "to": "stranger@x.io"},
               threadId="ts", internalDate=(NOW - 6 * DAY) * 1000)
    run(s)
    assert cards(s, "nudge") == []


def test_an_overdue_promise_gets_one_card_and_a_kept_out_one_none():
    s = brain()
    promises = s.records.setdefault((HUB, DATASETS["promises"]), {})
    promises["p1"] = {"id": "p1", "text": "send the deck", "status": "open", "personId": "ada",
                      "source": "any://o/bao-space/mb1/email_messages/x1",
                      "dueAt": {"$date": (NOW - DAY) * 1000}}
    promises["p2"] = {"id": "p2", "text": "call back", "status": "open",
                      "personEmail": "eve@spy.org", "dueAt": {"$date": (NOW - DAY) * 1000}}
    promises["p3"] = {"id": "p3", "text": "later", "status": "open",
                      "dueAt": {"$date": (NOW + DAY) * 1000}}
    s.set_config("exclusions", {"version": 1, "personIds": [], "emails": ["eve@spy.org"],
                                "domains": []})
    mod, _ = run(s)
    (card,) = cards(s, "overdue_promise")
    assert card["id"] == mod.card_id("overdue_promise", "p1")
    assert card["reason"] == "due 2027-01-14" and card["personIds"] == ["ada"]
    assert card["sourceUri"] == "any://o/bao-space/mb1/email_messages/x1"
    run(s)
    assert len(cards(s, "overdue_promise")) == 1


def _calendar(s, events):
    ds = "hubtype_calendar_events"
    s.records.setdefault((HUB, ds), {}).update({e["id"]: e for e in events})
    return {**args(s), "datasets": {**job_args(s)["datasets"], "calendar_events": ds}}


def _event(eid, start_h, end_h, attendees=(), title=None, status=None, emails=()):
    e = {"id": eid, "title": title or eid, "attendeeIds": list(attendees),
         "attendees": list(emails),
         "start": {"$date": int((NOW + start_h * 3600) * 1000)},
         "end": {"$date": int((NOW + end_h * 3600) * 1000)}}
    if status:
        e["status"] = status
    return e


def test_a_meeting_soon_with_a_known_attendee_gets_a_card_with_brief_people():
    s = brain()
    a = _calendar(s, [_event("e1", 2, 3, ["ada"], "Sync"), _event("e2", 2, 3, [], "Solo"),
                      _event("e3", 30, 31, ["ada"]), _event("e4", 4, 5, ["ada"],
                                                            status="cancelled")])
    mod = kernel(s)
    mod.main(a)
    (card,) = cards(s, "meeting_soon")
    assert card["id"] == mod.card_id("meeting_soon", "e1")
    assert card["briefPersonIds"] == ["ada"] and card["personIds"] == ["ada"]
    assert card["reason"] == "with Ada Lovelace at 2027-01-15 10:00 UTC"


def test_overlapping_events_get_one_conflict_card_per_pair():
    s = brain()
    a = _calendar(s, [_event("e1", 2, 4, title="A"), _event("e2", 3, 5, title="B"),
                      _event("e3", 6, 7, title="C")])
    mod = kernel(s)
    mod.main(a)
    (card,) = cards(s, "calendar_conflict")
    assert card["subjectKey"] == "e1|e2" and card["id"] == mod.card_id("calendar_conflict",
                                                                       "e1|e2")
    mod.main(a)
    assert len(cards(s, "calendar_conflict")) == 1


def test_both_calendar_passes_share_one_read_and_drop_a_kept_out_attendee():
    s = brain()
    s.set_config("exclusions", {"version": 1, "personIds": [], "emails": ["eve@spy.org"],
                                "domains": []})
    a = _calendar(s, [_event("e1", 2, 3, ["ada"], "With Eve", emails=["eve@spy.org"]),
                      _event("e2", 2, 4, ["ada"], "Sync"), _event("e3", 3, 5, title="Other")])
    before = len(s.calls)
    kernel(s).main(a)
    assert len([c for c in s.calls[before:] if c[0] == "query" and c[2] == "calendar_events"]) == 1
    assert [c["subjectKey"] for c in cards(s, "meeting_soon")] == ["e2"]
    assert [c["subjectKey"] for c in cards(s, "calendar_conflict")] == ["e2|e3"]


def test_no_calendar_store_means_no_calendar_reads():
    s = brain()
    run(s)
    assert not [c for c in s.calls if c[0] == "query" and "calendar" in c[2]]


def test_a_fact_conflict_signal_becomes_one_card_and_no_second_signal():
    s = brain()
    s.records.setdefault((HUB, DATASETS["signals"]), {})["f1"] = {
        "id": "f1", "tick": NOW - 100, "kind": "fact_conflict",
        "subject": "any://o/bao-space/ada", "decision": "none",
        "reason": "preference.language: stated 'de' vs inferred 'en'"}
    mod, _ = run(s)
    (card,) = cards(s, "fact_conflict")
    assert card["id"] == mod.card_id("fact_conflict", "f1") and card["signalId"] == "f1"
    assert card["personIds"] == ["ada"] and "stated 'de'" in card["reason"]
    assert [x["kind"] for x in signals(s)] == ["fact_conflict"]
    assert s.config("heartbeat.cursor")["factConflicts"] == NOW - 100
    run(s)
    assert len(cards(s, "fact_conflict")) == 1


# --- sent detection (ADR-033 §5) ------------------------------------------------------

CARD = "c" * 64
AID = "a" * 64
BODY = "Hi Ada,\n\nThursday at 10 works - see you then.\n\nBest,\nMe\n"
# what gmailSync stores for that text once Gmail sent it as HTML: markdown
# escapes, the lines rewrapped, a quoted chain under it
SYNCED = ("Hi Ada,\n\nThursday at 10 works \\- see you\nthen.\n\nBest,\nMe\n\n"
          "> On Tue, Ada wrote:\n> Thursday?")


def store(s, key):
    return s.records.setdefault((HUB, DATASETS[key]), {})


def approved_reply(*, decided=NOW - 3600, status="approved", recipients=("ada@lab.org",),
                   body=BODY):
    s = brain()
    store(s, "cards")[CARD] = {"id": CARD, "kind": "reply", "status": "open",
                               "title": "Re: Meeting", "reason": "addressed to you",
                               "subjectKey": "t1", "draftId": "d1", "approvalId": AID,
                               "personIds": ["ada"]}
    store(s, "drafts")["d1"] = {"id": "d1", "cardId": CARD, "sender": "mb1",
                                "senderAddress": ME, "recipients": list(recipients),
                                "format": "text/plain", "body": body,
                                "subject": "Re: Meeting", "threadId": "t1"}
    store(s, "approvals")[AID] = {"id": AID, "draftId": "d1", "cardId": CARD,
                                  "status": status,
                                  "decidedAt": {"$date": int(decided * 1000)}}
    # the thread it answers, before the approval
    s.add_mail("mb1", "m1", **{"from": ADA, "to": ME}, threadId="t1", body="Thursday?",
               participants=["ada@lab.org", ME], internalDate=(decided - 600) * 1000)
    return s


def sent(s, mid="s1", *, frm=f"Me <{ME}>", to=ADA, thread="t1", at=NOW - 600, body=SYNCED):
    addr = to.split("<")[-1].rstrip(">").strip().lower()
    s.add_mail("mb1", mid, **{"from": frm, "to": to}, threadId=thread, body=body,
               participants=sorted({ME, addr}), internalDate=at * 1000)


def is_marked_sent(s):
    return (store(s, "approvals")[AID]["status"] == "consumed"
            and store(s, "cards")[CARD]["status"] == "sent" and AID in store(s, "sends"))


def test_the_sent_reply_marks_the_card_sent_silently():
    s = approved_reply()
    sent(s)
    _, out = run(s)
    (row,) = store(s, "sends").values()
    assert row == {"id": AID, "approvalId": AID, "cardId": CARD, "threadId": "t1",
                   "messageId": "s1", "how": "detected", "at": {"$date": NOW * 1000}}
    assert is_marked_sent(s)
    assert s.chat == [] and out["cards"] == []                # success is silent
    sig = one_signal(s, kind="reply_sent")
    assert sig["decision"] == "none" and sig["cardId"] == CARD
    (standup,) = store(s, "standups").values()
    assert "1 replies sent" in standup["did"]


def test_what_is_not_the_sent_reply_marks_nothing():
    cases = [dict(at=NOW - 2 * 3600),                                  # before approvedAt
             dict(frm="Ada Lovelace <ada@lab.org>", to=ME),             # not from the user
             dict(body="Something else entirely."),                     # other text
             dict(thread="t9", to="Bob <bob@lab.org>")]                 # other thread, people
    for case in cases:
        s = approved_reply()
        sent(s, **case)
        run(s)
        assert store(s, "sends") == {}, case
        assert store(s, "approvals")[AID]["status"] == "approved", case
        assert store(s, "cards")[CARD]["status"] == "open", case


def test_a_reply_sent_as_a_new_message_to_the_same_people_is_found():
    s = approved_reply()
    sent(s, thread="t-new")                     # Gmail filed it in a thread of its own
    run(s)
    assert is_marked_sent(s) and store(s, "sends")[AID]["threadId"] == "t-new"


def test_match_text_survives_the_markdown_round_trip():
    mod = kernel(approved_reply())
    assert mod.match_text(BODY) in mod.match_text(SYNCED)
    assert mod.match_text("A \\*bold\\* MOVE\n\n> quoted") == "a *bold* move"
    row = {"from": f"Me <{ME}>", "internalDate": (NOW - 1) * 1000, "body": SYNCED}
    assert mod.is_sent_reply(row, ME, BODY, (NOW - 10) * 1000)
    assert not mod.is_sent_reply(row, ME, BODY, NOW * 1000)
    assert not mod.is_sent_reply(row, ME, "", (NOW - 10) * 1000)


def test_a_second_run_writes_nothing_more():
    s = approved_reply()
    sent(s)
    run(s)

    def writes():
        # every run records its tick (config/heartbeat); nothing else may repeat
        return [c for c in s.calls if c[0] in ("upsert_record", "modify")
                and c[1:3] != ("config", "heartbeat")]

    before = len(writes())
    run(s)
    assert len(store(s, "sends")) == 1 and len(writes()) == before


def test_seven_days_without_a_send_open_one_check_card():
    s = approved_reply(decided=NOW - 6 * DAY)
    run(s)
    assert [c["kind"] for c in store(s, "cards").values()] == ["reply"]
    s = approved_reply(decided=NOW - 7 * DAY - 60)
    mod, out = run(s)
    check = mod.card_id("send_check", AID)
    assert out["cards"] == [check]
    card = store(s, "cards")[check]
    assert card["kind"] == "send_check" and card["status"] == "open"
    assert card["subjectKey"] == AID and card["approvalId"] == AID and card["draftId"] == "d1"
    assert card["sourceUri"] == f"any://o/bao-space/{HUB}/cards/{CARD}"
    assert card["personIds"] == ["ada"] and card["title"].startswith("Did you send this reply?")
    assert [x["decision"] for x in store(s, "signals").values()
            if x["kind"] == "send_check"] == ["card"]
    (standup,) = store(s, "standups").values()
    assert "1 send checks" in standup["did"]
    run(s)                                               # the eighth day: no second card
    assert len([c for c in store(s, "cards").values() if c["kind"] == "send_check"]) == 1


def test_a_send_found_after_the_check_answers_the_check_card():
    s = approved_reply(decided=NOW - 8 * DAY)
    mod, _ = run(s)
    check = mod.card_id("send_check", AID)
    sent(s, at=NOW - 60)
    run(s)
    assert is_marked_sent(s) and store(s, "cards")[check]["status"] == "answered"


def test_fifty_approvals_with_check_cards_do_not_starve_a_fresh_one():
    s = approved_reply()
    sent(s)
    mod = kernel(s)
    for i in range(50):
        aid = f"stale{i:02d}".ljust(64, "0")
        store(s, "drafts")[f"d-{i}"] = {"id": f"d-{i}", "cardId": f"card-{i}", "sender": "mb1",
                                        "senderAddress": ME, "recipients": ["ada@lab.org"],
                                        "format": "text/plain", "body": f"Other reply {i}.\n",
                                        "subject": f"Re: Old {i}", "threadId": f"old-{i}"}
        # a stale approval with no card of its own is read like any other
        store(s, "approvals")[aid] = {"id": aid, "draftId": f"d-{i}",
                                      "cardId": f"card-{i}" if i % 2 else None,
                                      "status": "approved",
                                      "decidedAt": {"$date": int((NOW - 10 * DAY) * 1000)}}
        store(s, "cards")[mod.card_id("send_check", aid)] = {
            "id": mod.card_id("send_check", aid), "kind": "send_check", "status": "open",
            "subjectKey": aid, "approvalId": aid}
    out = mod.main(args(s))
    assert is_marked_sent(s) and out["cards"] == []        # found; no new check card either
    assert len([c for c in store(s, "cards").values() if c["kind"] == "send_check"]) == 50


def test_a_recipient_kept_out_means_no_mail_is_read_for_it():
    s = approved_reply(decided=NOW - 8 * DAY)
    sent(s)
    s.set_config("exclusions", {"version": 1, "personIds": [], "emails": ["ada@lab.org"],
                                "domains": []})
    filters = email_filters(s)
    run(s)
    assert store(s, "sends") == {} and len(store(s, "cards")) == 1     # no check card either
    assert not [f for f in filters if "threadId" in f or "participants" in f]


def test_a_half_written_mark_sent_is_completed_without_a_second_row():
    s = approved_reply()
    store(s, "sends")[AID] = {"id": AID, "approvalId": AID, "cardId": CARD, "threadId": "t1",
                              "how": "marked", "at": {"$date": (NOW - 60) * 1000}}
    run(s)
    assert is_marked_sent(s) and store(s, "sends")[AID]["how"] == "marked"
    assert ("upsert_record", "sends", AID) not in s.calls


def test_only_approved_approvals_are_looked_at():
    for status in ("pending", "void", "rejected", "consumed"):
        s = approved_reply(status=status)
        sent(s)
        run(s)
        assert store(s, "sends") == {}, status


def test_the_other_passes_run_beside_sent_detection():
    s = approved_reply()
    s.add_mail("mb1", "m9", **{"from": "Bob <bob@lab.org>", "to": ME}, threadId="t9",
               subject="New", participants=["bob@lab.org", ME], internalDate=(NOW - 60) * 1000)
    _, out = run(s)
    assert len(out["cards"]) == 1
    assert store(s, "cards")[out["cards"][0]]["kind"] == "reply"
    s.set_config("heartbeat", {"lastTickAt": NOW - 5000})
    run(s)
    assert one_signal(s, kind="heartbeat_gap")["subject"] == SUBJECT


# --- notice, standup, probes ----------------------------------------------------------

def test_one_notice_per_run_that_made_cards_linking_the_newest():
    s = brain()
    mail(s, "m1", thread="t1", at=NOW - 7200, subject="First")
    mail(s, "m2", thread="t2", at=NOW - 3600, subject="Second")
    mod, out = run(s)
    (notice,) = s.chat
    assert notice["agent"] == {"name": "bao", "done": True}
    assert notice["text"] == "2 new cards: First; Second"
    newest = out["cards"][-1]
    assert notice["attachments"] == {"cards": {
        "type": "brain_cards", "link": f"any://o/bao-space/{HUB}/cards/{newest}"}}
    run(s)
    assert len(s.chat) == 1


def test_a_failed_notice_does_not_fail_the_run():
    s = brain()
    mail(s, "m1", thread="t1")

    def down(space, chat, body):
        raise RuntimeError("chat unavailable")

    s.chat_send = down
    _, out = run(s)
    assert out["counts"]["noticeFailed"] == 1 and len(cards(s)) == 1


def test_a_standup_row_only_when_something_happened():
    s = brain()
    _, out = run(s)
    assert out["standup"] is False and s.store("standups") == {}
    mail(s, "m1", thread="t1")
    s.records.setdefault((HUB, DATASETS["approvals"]), {})["h1"] = {"id": "h1",
                                                                    "status": "pending"}
    run(s)
    (row,) = s.store("standups").values()
    assert row["id"] == f"heartbeat:{NOW}" and row["job"] == "heartbeat" and row["tick"] == NOW
    assert "read 1 mails" in row["did"] and "1 reply cards" in row["did"]
    assert "asked for 1 drafts" in row["did"] and row["needs"] == ["approve 1 draft"]
    assert row["counts"]["replyCards"] == 1


def test_unconfigured_args_raise_with_no_writes():
    s = brain()
    for bad in ({"space": "bao-space"},
                {"space": "bao-space", "hub": HUB, "datasets": {"config": "x"}}):
        with pytest.raises(Exception) as err:
            kernel(s).main(bad)
        assert type(err.value).__name__ == "unconfigured"
    assert not [c for c in s.calls if c[0] in ("upsert_record", "upsert_records", "modify")]


def test_a_missing_exclusion_list_is_refused_and_raises():
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    mail(s, "m1")
    with pytest.raises(Exception) as err:
        kernel(s).main(args(s))
    assert type(err.value).__name__ == "exclusions_unreadable"
    (sig,) = signals(s)
    assert sig["kind"] == "exclusions_unreadable" and sig["decision"] == "refused"
    assert sig["subject"] == SUBJECT and sig["reason"] == "no exclusion list"
    assert cards(s) == [] and s.config("heartbeat") is None


def test_a_wrong_version_is_refused():
    s = brain()
    s.set_config("exclusions", {"version": 2, "personIds": [], "emails": [], "domains": []})
    with pytest.raises(Exception, match="exclusion list version 2"):
        run(s)
    (sig,) = signals(s)
    assert "version 2" in sig["reason"]


def test_an_unreadable_blocklist_is_refused_and_raises():
    s = brain()
    s.set_config("blocklist", {"senders": "ada@lab.org"})
    with pytest.raises(Exception) as err:
        run(s)
    assert type(err.value).__name__ == "blocklist_unreadable"
    (sig,) = signals(s)
    assert sig["kind"] == "blocklist_unreadable" and sig["decision"] == "refused"
    assert sig["subject"] == SUBJECT and "senders" in sig["reason"]
    assert s.config("heartbeat") is None


def test_a_first_tick_writes_no_gap_and_records_itself():
    s = brain()
    _, out = run(s)
    assert out["gap_s"] is None and signals(s) == []
    assert s.config("heartbeat") == {"lastTickAt": NOW}


def test_an_old_last_tick_writes_one_gap_signal():
    s = brain()
    s.set_config("heartbeat", {"lastTickAt": NOW - 1300})
    _, out = run(s)
    assert out["gap_s"] == 1300
    sig = one_signal(s, kind="heartbeat_gap")
    assert sig["decision"] == "none" and sig["reason"] == "no tick for 1300s"
    assert sig["subject"] == SUBJECT
    assert s.config("heartbeat") == {"lastTickAt": NOW}


def test_a_recent_last_tick_writes_none():
    s = brain()
    s.set_config("heartbeat", {"lastTickAt": NOW - 500})
    run(s)
    assert signals(s) == []


def test_a_rejected_tick_write_fails_the_run():
    s = brain()
    s.upsert_record = lambda *a: {"recordIds": [], "rejections": [{"reason": "nope"}]}
    with pytest.raises(Exception, match=r"writing config/heartbeat(\.cursor)?: nope"):
        run(s)


def test_a_corrupt_cursor_raises():
    s = brain()
    for bad in ({"mail": {"mb1": (NOW - 3600) * 1000}},        # the old date cursor
                {"mail": {}, "stale": {"mb1": "yesterday"}}, "a string"):
        s.set_config("heartbeat.cursor", bad)
        with pytest.raises(Exception) as err:
            run(s)
        assert type(err.value).__name__ == "cursor_corrupt", bad
