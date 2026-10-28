"""programs/brainHeartbeat@v3 — v2 plus the reply hand-off's sent detection
(ADR-037), under the REAL guest kernel. Pinned: the user's message after the
approval carrying the approved text marks it — one sends row, the approval
consumed, the card sent, no chat message; a message before the approval,
from another address, with other text, or to other people in another thread
is not it; a reply sent as a new message to the same recipients is; the
match survives the HTML-to-markdown round trip; a second run writes nothing;
seven days without a match open one send_check card, and a later detection
answers it; a recipient kept out means no mail is read; a half-written Mark
sent is completed without a second row; v2's passes still run."""

from brain_fakes import DATASETS, HUB, FakeSpace, job_args
from connectorenv import connector_kernel

NOW = 1_800_000_000
DAY = 86400
ME = "me@example.com"
ADA = "Ada Lovelace <ada@lab.org>"
CARD = "c" * 64
AID = "a" * 64
BODY = "Hi Ada,\n\nThursday at 10 works - see you then.\n\nBest,\nMe\n"
# what gmailSync stores for that text once Gmail sent it as HTML: markdown
# escapes, the lines rewrapped, a quoted chain under it
SYNCED = ("Hi Ada,\n\nThursday at 10 works \\- see you\nthen.\n\nBest,\nMe\n\n"
          "> On Tue, Ada wrote:\n> Thursday?")


def kernel(space):
    def eff(name, payload):
        if name == "time.now":
            return {"epoch": float(NOW)}
        if name == "config.get":
            raise KeyError(f"no config value for {payload['key']!r}")
        raise AssertionError(f"unexpected effect {name}")

    return connector_kernel(effect=eff, any_client=space).use("brainHeartbeat@v3")


def args(space):
    return {**job_args(space), "tier": "brain.drafter"}


def store(s, key):
    return s.records.setdefault((HUB, DATASETS[key]), {})


def approved_reply(*, decided=NOW - 3600, status="approved", recipients=("ada@lab.org",),
                   body=BODY):
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    s.seed_lists()
    s.add_object("ada", "profile", ["person", "brain_person"], name="Ada Lovelace",
                 brain_person={"emails": ["ada@lab.org"]})
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


def run(s):
    return kernel(s).main(args(s))


def is_marked_sent(s):
    return (store(s, "approvals")[AID]["status"] == "consumed"
            and store(s, "cards")[CARD]["status"] == "sent" and AID in store(s, "sends"))


def test_the_sent_reply_marks_the_card_sent_silently():
    s = approved_reply()
    sent(s)
    out = run(s)
    (row,) = store(s, "sends").values()
    assert row == {"id": AID, "approvalId": AID, "cardId": CARD, "threadId": "t1",
                   "messageId": "s1", "how": "detected", "at": {"$date": NOW * 1000}}
    assert is_marked_sent(s)
    assert s.chat == [] and out["cards"] == []                # success is silent
    sig = [x for x in store(s, "signals").values() if x["kind"] == "reply_sent"]
    assert len(sig) == 1 and sig[0]["decision"] == "none" and sig[0]["cardId"] == CARD
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
    mod = kernel(s)
    out = mod.main(args(s))
    check = mod.hb.card_id("send_check", AID)
    assert out["cards"] == [check]
    card = store(s, "cards")[check]
    assert card["kind"] == "send_check" and card["status"] == "open"
    assert card["subjectKey"] == AID and card["approvalId"] == AID and card["draftId"] == "d1"
    assert card["sourceUri"] == f"any://o/bao-space/{HUB}/cards/{CARD}"
    assert card["personIds"] == ["ada"] and card["title"].startswith("Did you send this reply?")
    assert [x["decision"] for x in store(s, "signals").values()
            if x["kind"] == "send_check"] == ["card"]
    mod.main(args(s))                                    # the eighth day: no second card
    assert len([c for c in store(s, "cards").values() if c["kind"] == "send_check"]) == 1


def test_a_send_found_after_the_check_answers_the_check_card():
    s = approved_reply(decided=NOW - 8 * DAY)
    mod = kernel(s)
    mod.main(args(s))
    check = mod.hb.card_id("send_check", AID)
    sent(s, at=NOW - 60)
    mod.main(args(s))
    assert is_marked_sent(s) and store(s, "cards")[check]["status"] == "answered"


def test_a_recipient_kept_out_means_no_mail_is_read_for_it():
    s = approved_reply(decided=NOW - 8 * DAY)
    sent(s)
    s.set_config("exclusions", {"version": 1, "personIds": [], "emails": ["ada@lab.org"],
                                "domains": []})
    filters = []
    real = s.query

    def query(space, oid, ds, filter=None, **kw):
        if ds == "email_messages":
            filters.append(filter or {})
        return real(space, oid, ds, filter=filter, **kw)

    s.query = query
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


def test_v2s_passes_still_run_under_v3():
    s = approved_reply()
    s.add_mail("mb1", "m9", **{"from": "Bob <bob@lab.org>", "to": ME}, threadId="t9",
               subject="New", participants=["bob@lab.org", ME], internalDate=(NOW - 60) * 1000)
    out = run(s)
    assert len(out["cards"]) == 1
    assert store(s, "cards")[out["cards"][0]]["kind"] == "reply"
    s.set_config("heartbeat", {"lastTickAt": NOW - 5000})
    run(s)
    gap = [x for x in store(s, "signals").values() if x["kind"] == "heartbeat_gap"]
    assert gap and gap[0]["subject"] == "connectors:brainHeartbeat@v3"
