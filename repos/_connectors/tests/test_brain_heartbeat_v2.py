"""programs/brainHeartbeat@v2 — the brain's decision loop (ADR-036), under the
REAL guest kernel. Pinned: every mail decision branch writes one signal with
a decision and a reason; a message addressed to the user with no reply
becomes one reply card and one draft one-shot; mail from someone kept out
gets a refused signal and nothing else; snoozed cards wake; a nudge is
written once per thread; overdue promises, meetings soon, calendar conflicts
and fact conflicts each become one card; one notice per run that made cards;
a standup only when something happened; cursors advance after the writes;
a re-run with no new input writes nothing; the heartbeat owns the attempt
counter and every one-shot id is fresh and existence-checked; drafting waits
for the user's consent and its budget."""

import pytest
from brain_fakes import ANCHOR, DATASETS, HUB, FakeSpace, job_args
from connectorenv import connector_kernel

NOW = 1_800_000_000
DAY = 86400
ME = "me@example.com"
ADA = "Ada Lovelace <ada@lab.org>"
CONSENT = {"allowed": True, "requestedAt": "t", "by": "chat"}


def kernel(space, *, consent=True):
    def eff(name, payload):
        if name == "time.now":
            return {"epoch": float(NOW)}
        if name == "config.get":
            if payload["key"] == "brain.model" and consent is not None:
                return {"value": {**CONSENT, "allowed": consent}}
            raise KeyError(f"no config value for {payload['key']!r}")
        raise AssertionError(f"unexpected effect {name}")

    return connector_kernel(effect=eff, any_client=space).use("brainHeartbeat@v2")


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


def mail(s, mid, frm=ADA, to=ME, cc="", thread=None, at=NOW - 3600, subject="Hello"):
    s.add_mail("mb1", mid, **{"from": frm, "to": to, "cc": cc}, subject=subject,
               threadId=thread or f"t-{mid}", body="secret body", internalDate=at * 1000,
               participants=sorted({a.split("<")[-1].rstrip(">").strip().lower()
                                    for a in (frm, to, cc) if a}))


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
    import hashlib
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
    assert card["sourceUri"] == "any://o/bao-space/mb1/email_messages/m1"
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
    filters = []
    real = s.query

    def query(space, oid, ds, filter=None, **kw):
        filters.append(filter or {})
        return real(space, oid, ds, filter=filter, **kw)

    s.query = query
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
    older = one_signal(s, subject="any://o/bao-space/mb1/email_messages/m1")
    assert older["decision"] == "none" and "newer message" in older["reason"]


def test_an_open_card_covers_a_new_message_and_a_closed_one_does_not():
    s = brain()
    mail(s, "m1", thread="t1", at=NOW - 3 * DAY)
    mod, _ = run(s)
    first = mod.reply_card_id("t1", "m1")
    mail(s, "m2", thread="t1", at=NOW - 60)
    run(s)
    assert [c["id"] for c in cards(s)] == [first]                  # covered
    covered = one_signal(s, subject="any://o/bao-space/mb1/email_messages/m2")
    assert covered["decision"] == "card" and covered["cardId"] == first
    s.store("cards")[first]["status"] = "dismissed"
    mail(s, "m3", thread="t1", at=NOW - 30)
    run(s)
    assert sorted(c["id"] for c in cards(s)) == sorted([first, mod.reply_card_id("t1", "m3")])


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
    assert s.config("heartbeat.cursor")["mail"] == {"mb1": (NOW - 3600) * 1000}
    assert len(cards(s)) == 1


def test_a_first_run_reads_only_the_reply_window():
    s = brain()
    mail(s, "old", thread="t-old", at=NOW - 6 * DAY)
    mail(s, "new", thread="t-new", at=NOW - DAY)
    mod, _ = run(s)
    assert [c["subjectKey"] for c in cards(s)] == ["t-new"]


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


def test_a_source_message_kept_out_after_the_card_is_not_drafted():
    s = brain()
    mail(s, "m1", thread="t1")
    run(s, consent=False)
    s.set_config("exclusions", {"version": 1, "personIds": [], "emails": [],
                                "domains": ["lab.org"]})
    _, out = run(s)
    assert out["armed"] == {} and s.triggers() == {}


# --- the other passes ---------------------------------------------------------------

def test_a_snoozed_card_wakes_when_its_time_has_come():
    s = brain()
    s.store("cards")  # ensure the store exists
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


def _event(eid, start_h, end_h, attendees=(), title=None, status=None):
    e = {"id": eid, "title": title or eid, "attendeeIds": list(attendees),
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


def test_a_missing_exclusion_list_is_refused_and_raises():
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    mail(s, "m1")
    with pytest.raises(Exception) as err:
        kernel(s).main(args(s))
    assert type(err.value).__name__ == "exclusions_unreadable"
    (sig,) = signals(s)
    assert sig["kind"] == "exclusions_unreadable" and sig["decision"] == "refused"
    assert cards(s) == []


def test_unconfigured_args_raise():
    s = brain()
    with pytest.raises(Exception) as err:
        kernel(s).main({"space": "bao-space", "hub": HUB, "datasets": {"config": "x"}})
    assert type(err.value).__name__ == "unconfigured"


def test_a_corrupt_cursor_raises():
    s = brain()
    s.set_config("heartbeat.cursor", {"mail": {"mb1": "yesterday"}})
    with pytest.raises(Exception) as err:
        run(s)
    assert type(err.value).__name__ == "cursor_corrupt"


def test_an_old_last_tick_writes_one_gap_signal():
    s = brain()
    s.set_config("heartbeat", {"lastTickAt": NOW - 1300})
    _, out = run(s)
    assert out["gap_s"] == 1300
    assert one_signal(s, kind="heartbeat_gap")["reason"] == "no tick for 1300s"
    assert s.config("heartbeat") == {"lastTickAt": NOW}
