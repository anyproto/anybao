"""programs/brainChatScan@v1 — promises and asks from the chats the user
opted in, under the REAL guest kernel (ADR-039). Pinned: nothing is read
while no chat is watched; the read is a `_ver.id` range (the probe); the
user's own messages go to the shared promise extraction only with consent;
other people's messages become asks only when addressed to the user and
carrying a request phrase or a trailing "?"; agent posts and control
records are passed over; an excluded person's messages are skipped and
counted, an excluded one-to-one peer's chat never queried; the cursor
advances per chat and a re-run writes nothing; the pages flag gates every
page read; a provider failure keeps the cursor and an all-failed run
raises; one task_suggestion card a day; a completed task closes its ask."""

import json

import pytest
from brain_fakes import DATASETS, HUB, FakeSpace, job_args
from connectorenv import connector_kernel

THU = 1_792_670_400          # Thursday 2026-10-22 12:00 UTC
DAY = 86400
THU_DAY = THU // DAY * DAY
CONSENT = {"allowed": True, "requestedAt": "t", "by": "chat"}
EXCLUSIONS = {"version": 1, "personIds": [], "emails": [], "domains": []}


class Model:
    """The classify-tier extractor: one scripted reply per call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, messages, system="", tier=None, tools=None, max_tokens=None):
        self.prompts.append(messages[0]["parts"][0]["text"])
        reply = self.replies.pop(0) if self.replies else []
        if isinstance(reply, Exception):
            raise reply
        return {"parts": [{"type": "text", "text": reply if isinstance(reply, str)
                           else json.dumps(reply)}]}


def kernel(space, model=None, *, watched=("team", "dm"), consent=True, pages=None,
           datasets=None, now=THU):
    model = model or Model()
    cfg = {}
    if watched is not None:
        cfg["brain.chatScan.spaces"] = {"spaceIds": list(watched), "requestedAt": "t",
                                        "by": "chat"}
    if pages is not None:
        cfg["brain.chatScan.pages"] = {"enabled": pages, "requestedAt": "t", "by": "chat"}
    if consent is not None:
        cfg["brain.model"] = {**CONSENT, "allowed": consent}
    cfg["brain.status"] = {"state": "ready", "hub": {"objectId": HUB,
                                                     "datasets": dict(datasets or DATASETS)}}

    def eff(name, payload):
        if name == "time.now":
            return {"epoch": float(now)}
        if name == "config.get":
            if payload["key"] not in cfg:
                raise KeyError(f"no config value for {payload['key']!r}")
            return {"value": cfg[payload["key"]]}
        if name == "test.judge_available":
            return {"value": False}
        raise AssertionError(f"unexpected effect {name}")

    mod = connector_kernel(effect=eff, any_client=space, llm_chat=model).use(
        "brainChatScan@v1")
    mod._cfg = cfg
    return mod, model


def chats():
    """A team space (Ada, Bob) and a one-to-one with Bob, none read yet."""
    s = FakeSpace()
    s.seed_lists()
    s.add_space("team", members=[("ada-id", "Ada Lovelace"), ("bob-id", "Bob Stone")])
    s.add_space("dm", one_to_one=True, members=[("bob-id", "Bob Stone")])
    return s


def msg(s, sid, mid, creator, text, ver, at=THU - 3600, **extra):
    return s.add_message(sid, mid, creator, text, ver, at, **extra)


def asks(s):
    return sorted(s.store("asks").values(), key=lambda r: r["quote"])


def reads(s, dataset="chat_messages"):
    return [c for c in s.calls if c[0] == "query" and c[2] == dataset]


# --- nothing without opt-in -----------------------------------------------------

def test_nothing_is_read_while_no_chat_is_watched():
    for watched in (None, ()):
        s = chats()
        msg(s, "dm", "m1", "bob-id", "Can you send the deck?", "v1")
        mod, model = kernel(s, watched=watched)
        assert mod.main(job_args(s)) == {"skipped": "no chats"}
        assert s.calls == [] and model.prompts == [] and s.store("asks") == {}


def test_unconfigured_without_an_asks_store():
    s = chats()
    mod, _ = kernel(s)
    args = job_args(s)
    del args["datasets"]["asks"]
    with pytest.raises(Exception) as err:
        mod.main(args)
    assert type(err.value).__name__ == "unconfigured"


# --- the probe: what reaches the wire -----------------------------------------------

def _real_any_wire():
    from kernelenv import load_kernel
    bodies = []

    def effect(name, payload):
        if name == "runtime.get" and payload["key"] == "any.base_url":
            return {"value": "http://any"}
        if name.startswith("http."):
            bodies.append((payload["url"], payload.get("json")))
            return {"status": 200, "headers": {}, "body": json.dumps({"records": []})}
        raise AssertionError(f"unexpected effect {name}")

    return load_kernel(effect=effect).use("any@v1"), bodies


SID = "bafytestspace0000000000000000.tsuffix"


def test_probe_the_cursor_read_passes_any_v1_unchanged():
    # ADR-039 §2: chat_messages is a canonical collection and `_ver.id` is no
    # instant key, so the body the scan builds is the body the server gets
    mod, bodies = _real_any_wire()
    mod.query(SID, "chat1", "chat_messages", filter={"_ver.id": {"$gt": "v0042"}},
              sort=["_ver.id"], limit=200)
    mod.query(SID, "chat1", "chat_messages", sort=["-_ver.id"], limit=200)
    assert bodies == [
        (f"http://any/v1/spaces/{SID}/query",
         {"objectId": "chat1", "dataset": "chat_messages",
          "filter": {"_ver.id": {"$gt": "v0042"}}, "sort": ["_ver.id"], "limit": 200}),
        (f"http://any/v1/spaces/{SID}/query",
         {"objectId": "chat1", "dataset": "chat_messages", "sort": ["-_ver.id"],
          "limit": 200})]


def test_probe_a_created_at_cursor_needs_an_instant():
    # the alternative the probe ruled out: accepted only as an instant, and
    # unindexed + second-grained on the server (ADR-039 §2)
    mod, bodies = _real_any_wire()
    with pytest.raises(Exception, match="instant"):
        mod.query(SID, "chat1", "chat_messages", filter={"createdAt": {"$gt": 1_700_000_000}})
    assert bodies == []


def test_the_scan_reads_with_the_probed_shape():
    s = chats()
    seen = []
    real = s.query

    def spy(space, oid, ds, **kw):
        if ds == "chat_messages":
            seen.append({k: v for k, v in kw.items() if v is not None})
        return real(space, oid, ds, **kw)

    s.query = spy
    mod, _ = kernel(s, watched=("dm",))
    mod.main(job_args(s))                                   # first visit: newest page
    assert seen == [{"sort": ["-_ver.id"], "limit": 200}]
    s.set_config("chatScan.cursor", {"chat-dm": "v0"})
    mod.main(job_args(s))
    assert seen[-1] == {"filter": {"_ver.id": {"$gt": "v0"}}, "sort": ["_ver.id"],
                        "limit": 200}


# --- own messages → promises --------------------------------------------------------

def test_own_messages_become_chat_promises_in_english_and_german():
    s = chats()
    msg(s, "dm", "m1", "me-id", "Sure! I'll send you the deck tomorrow.", "v1")
    msg(s, "team", "m2", "me-id", "Ich schicke dir morgen den Bericht.", "v1")
    # the watched order: team, then the one-to-one
    model = Model([{"quote": "Ich schicke dir morgen den Bericht.", "due": None}],
                  [{"quote": "I'll send you the deck tomorrow.", "due": None}])
    mod, _ = kernel(s, model)
    out = mod.main(job_args(s))
    by_quote = {p["quote"]: p for p in s.store("promises").values()}
    deck = by_quote["I'll send you the deck tomorrow."]
    assert deck["channel"] == "chat" and deck["mailboxId"] == "chat-dm"
    assert deck["source"] == "any://o/dm/chat-dm/chat_messages/m1"
    assert deck["madeAt"] == {"$date": (THU - 3600) * 1000}
    assert deck["dueAt"] == {"$date": (THU_DAY + DAY) * 1000} and deck["status"] == "open"
    assert "personEmail" not in deck
    assert by_quote["Ich schicke dir morgen den Bericht."]["mailboxId"] == "chat-team"
    assert out["promises"] == 2 and out["own"] == 2 and out["modelCalls"] == 2
    assert s.store("asks") == {}


def test_a_one_to_one_promise_names_the_peers_person_when_it_resolves():
    s = chats()
    s.add_object("bob", "profile", ["person", "brain_person"], name="Bob Stone")
    msg(s, "dm", "m1", "me-id", "I'll call you on Monday.", "v1")
    mod, _ = kernel(s, Model([{"quote": "I'll call you on Monday.", "due": None}]))
    mod.main(job_args(s))
    (p,) = s.store("promises").values()
    assert p["personId"] == "bob"


def test_without_consent_no_own_message_reaches_the_model():
    s = chats()
    msg(s, "dm", "m1", "me-id", "I'll send you the deck.", "v1")
    msg(s, "dm", "m2", "bob-id", "Can you send it today?", "v2")
    for consent in (False, None):
        mod, model = kernel(s, Model([{"quote": "I'll send you the deck.", "due": None}]),
                            consent=consent)
        s.set_config("chatScan.cursor", {})
        out = mod.main(job_args(s))
        assert model.prompts == [] and s.store("promises") == {}
        assert out["noConsent"] == 1
    assert [a["quote"] for a in asks(s)] == ["Can you send it today?"]     # asks need no model


def test_a_message_that_already_has_a_promise_is_not_asked_again():
    s = chats()
    msg(s, "dm", "m1", "me-id", "I'll send you the deck.", "v1")
    model = Model([{"quote": "I'll send you the deck.", "due": None}],
                  [{"quote": "send you the deck.", "due": None}])
    mod, _ = kernel(s, model, watched=("dm",))
    mod.main(job_args(s))
    s.set_config("chatScan.cursor", {})
    out = mod.main(job_args(s))
    assert len(model.prompts) == 1 and out["alreadyExtracted"] == 1
    assert len(s.store("promises")) == 1


# --- others' messages → asks ----------------------------------------------------------

def test_each_request_phrase_makes_a_request_ask():
    s = chats()
    mod, _ = kernel(s, watched=("dm",))
    for i, phrase in enumerate(mod.REQUEST_PHRASES):
        msg(s, "dm", f"m{i}", "bob-id", f"Hi. {phrase.capitalize()} handle item {i}.",
            f"v{i:03d}")
    out = mod.main(job_args(s))
    got = asks(s)
    assert len(got) == len(mod.REQUEST_PHRASES) == out["asks"]
    assert {a["kind"] for a in got} == {"request"}
    assert all(not a["quote"].startswith("Hi") for a in got)        # the sentence, not the message
    for phrase in mod.REQUEST_PHRASES:
        assert mod.ask_kind(f"x {phrase} y") == "request", phrase
        assert mod.ask_kind(f"x {phrase}y") is None, phrase       # a whole phrase only


def test_a_trailing_question_mark_makes_a_question_and_plain_talk_nothing():
    s = chats()
    msg(s, "dm", "m1", "bob-id", "Lunch was great. Did the contract arrive?\nSee you", "v1")
    mod, _ = kernel(s, watched=("dm",))
    mod.main(job_args(s))
    (ask,) = asks(s)
    assert ask["kind"] == "question" and ask["quote"] == "Did the contract arrive?"
    assert ask["text"] == "Did the contract arrive?"
    assert ask["senderIdentity"] == "any://u/bob-id" and ask["spaceId"] == "dm"
    assert ask["chatId"] == "chat-dm" and ask["status"] == "open"
    assert ask["source"] == "any://o/dm/chat-dm/chat_messages/m1"
    assert ask["askedAt"] == {"$date": (THU - 3600) * 1000}
    assert ask["suggestedDeadline"] == {"$date": (THU_DAY + 5 * DAY) * 1000}  # +3 business
    assert ask["id"] == mod.ask_id("m1", "Did the contract arrive?")


def test_a_named_day_wins_for_the_suggested_deadline():
    s = chats()
    msg(s, "dm", "m1", "bob-id", "Can you review this by Friday?", "v1")
    mod, _ = kernel(s, watched=("dm",))
    mod.main(job_args(s))
    (ask,) = asks(s)
    assert ask["kind"] == "request"
    assert ask["suggestedDeadline"] == {"$date": (THU_DAY + DAY) * 1000}


def test_the_three_addressed_to_me_branches_and_the_negative():
    s = chats()
    mod, _ = kernel(s)
    plain = {"id": "x", "creator": "ada-id", "text": "Anyone free?"}
    assert mod.addressed_to_me(plain, "me-id", True) is True                 # one-to-one
    assert mod.addressed_to_me(plain, "me-id", False) is False
    assert mod.addressed_to_me({**plain, "mentions": ["me-id"]}, "me-id", False)  # mention
    linked = {**plain, "text": "[Me](any://u/me-id) anyone free?"}           # no mentions field
    assert mod.addressed_to_me(linked, "me-id", False)
    assert not mod.addressed_to_me({**linked, "mentions": ["ada-id"]}, "me-id", False)
    reply = {**plain, "replyToMessageId": "mine1"}
    assert mod.addressed_to_me(reply, "me-id", False, ["mine1"])               # a reply
    assert not mod.addressed_to_me(reply, "me-id", False, ["other"])
    # through the job: in a group chat only the addressed ones become asks
    msg(s, "team", "m1", "ada-id", "Anyone free for lunch?", "v1")
    msg(s, "team", "m2", "ada-id", "[Me](any://u/me-id) can you join?", "v2",
        mentions=["me-id"])
    msg(s, "team", "m3", "me-id", "Busy today, sorry.", "v3")
    msg(s, "team", "m4", "bob-id", "Could you take it over?", "v4",
        replyToMessageId="m3", mentions=["me-id"])
    msg(s, "team", "m5", "bob-id", "Could you ask Ada?", "v5", replyToMessageId="m1",
        mentions=["ada-id"])
    mod, _ = kernel(s, watched=("team",), consent=False)
    out = mod.main(job_args(s))
    assert [a["quote"] for a in asks(s)] == ["Could you take it over?", "Me can you join?"]
    assert out["ignored"] == 2 and out["addressed"] == 2


def test_agent_posts_and_control_records_are_passed_over():
    s = chats()
    msg(s, "dm", "m1", "bob-id", "Can you check this?", "v1", agent={"name": "bobs-bot",
                                                                        "done": True})
    msg(s, "dm", "m2", "bob-id", "", "v2", control={"kind": "break"})
    msg(s, "dm", "m3", "me-id", "Can you summarise?", "v3", agent={"name": "bao",
                                                                     "done": True})
    mod, model = kernel(s, watched=("dm",))
    out = mod.main(job_args(s))
    assert s.store("asks") == {} and model.prompts == [] and out["passedOver"] == 3
    assert s.config("chatScan.cursor") == {"chat-dm": "v3"}


# --- exclusions, before any text ------------------------------------------------------------

def test_an_excluded_sender_is_skipped_and_counted():
    s = chats()
    s.add_object("ada", "profile", ["person", "brain_person"], name="Ada Lovelace")
    s.set_config("exclusions", {**EXCLUSIONS, "personIds": ["ada"]})
    msg(s, "team", "m1", "ada-id", "Can you send me the file?", "v1", mentions=["me-id"])
    msg(s, "team", "m2", "bob-id", "Could you reply to Ada?", "v2", replyToMessageId="m1",
        mentions=["me-id", "ada-id"])
    msg(s, "team", "m3", "bob-id", "Can you review mine?", "v3", mentions=["me-id"])
    msg(s, "team", "m4", "me-id", "[Ada](any://u/ada-id) I'll send it tonight.", "v4",
        mentions=["ada-id"])
    mod, model = kernel(s, watched=("team",))
    out = mod.main(job_args(s))
    assert [a["quote"] for a in asks(s)] == ["Can you review mine?"]
    assert out["excludedSkipped"] == 3 and model.prompts == []


def test_a_name_two_persons_share_links_nobody_but_keeps_either_exclusion():
    s = chats()
    s.add_object("ada1", "profile", ["person"], name="Ada Lovelace")
    s.add_object("ada2", "profile", ["brain_person"], name="Ada Lovelace")
    msg(s, "team", "m1", "ada-id", "Can you call me?", "v1", mentions=["me-id"])
    mod, _ = kernel(s, watched=("team",))
    mod.main(job_args(s))
    (ask,) = asks(s)
    assert "personId" not in ask                     # ambiguous: no link
    s2 = chats()
    s2.add_object("ada1", "profile", ["person"], name="Ada Lovelace")
    s2.add_object("ada2", "profile", ["brain_person"], name="Ada Lovelace")
    s2.set_config("exclusions", {**EXCLUSIONS, "personIds": ["ada2"]})
    msg(s2, "team", "m1", "ada-id", "Can you call me?", "v1", mentions=["me-id"])
    mod, _ = kernel(s2, watched=("team",))
    assert mod.main(job_args(s2))["excludedSkipped"] == 1 and s2.store("asks") == {}


def test_a_unique_name_links_the_ask_to_the_person():
    s = chats()
    s.add_object("bob", "profile", ["person", "brain_person"], name="Robert",
                 brain_person={"aliases": ["Bob Stone"]})
    msg(s, "dm", "m1", "bob-id", "Can you call me?", "v1")
    mod, _ = kernel(s, watched=("dm",))
    mod.main(job_args(s))
    assert asks(s)[0]["personId"] == "bob"


def test_an_excluded_one_to_one_peer_s_chat_is_never_queried():
    s = chats()
    s.add_object("bob", "profile", ["person", "brain_person"], name="Bob Stone")
    s.set_config("exclusions", {**EXCLUSIONS, "personIds": ["bob"]})
    msg(s, "dm", "m1", "bob-id", "Can you call me?", "v1")
    mod, _ = kernel(s, watched=("dm",))
    out = mod.main(job_args(s))
    assert reads(s) == [] and out["excludedChats"] == 1 and s.store("asks") == {}


def test_an_unreadable_exclusion_list_fails_closed_before_any_read():
    s = chats()
    s.set_config("exclusions", None)
    msg(s, "dm", "m1", "bob-id", "Can you call me?", "v1")
    mod, _ = kernel(s)
    with pytest.raises(Exception) as err:
        mod.main(job_args(s))
    assert type(err.value).__name__ == "exclusions_unreadable" and reads(s) == []


def test_read_only_unknown_and_baos_own_spaces_are_skipped():
    s = chats()
    s.add_space("news", role="reader", members=[("ada-id", "Ada Lovelace")])
    s.add_space("bao-space")
    msg(s, "news", "m1", "ada-id", "Can you read this?", "v1")
    mod, _ = kernel(s, watched=("news", "gone", "bao-space"))
    out = mod.main(job_args(s))
    assert out["spacesSkipped"] == 3 and reads(s) == []


# --- cursor and idempotence ------------------------------------------------------------------

def test_the_cursor_advances_per_chat_and_a_re_run_writes_nothing():
    s = chats()
    msg(s, "dm", "m1", "bob-id", "Can you call me?", "v1")
    msg(s, "team", "m2", "ada-id", "Could you review it?", "v7", mentions=["me-id"])
    mod, _ = kernel(s, consent=False)
    first = mod.main(job_args(s))
    assert first["asks"] == 2
    assert s.config("chatScan.cursor") == {"chat-dm": "v1", "chat-team": "v7"}
    again = mod.main(job_args(s))
    assert again["asks"] == 0 and again["messages"] == 0
    msg(s, "dm", "m3", "bob-id", "Would you sign it?", "v2", at=THU - 60)
    third = mod.main(job_args(s))
    assert third["asks"] == 1 and third["messages"] == 1
    assert s.config("chatScan.cursor") == {"chat-dm": "v2", "chat-team": "v7"}
    # a cursor reset re-reads, and the stable ids write nothing twice
    s.set_config("chatScan.cursor", {})
    assert mod.main(job_args(s))["asks"] == 0 and len(s.store("asks")) == 3


def test_a_closed_or_deleted_ask_is_never_reopened():
    s = chats()
    msg(s, "dm", "m1", "bob-id", "Can you call me?", "v1")
    msg(s, "dm", "m2", "bob-id", "Could you sign?", "v2")
    mod, _ = kernel(s, watched=("dm",))
    mod.main(job_args(s))
    first, second = sorted(s.store("asks"))
    s.store("asks")[first]["status"] = "dismissed"
    s.store("asks").pop(second)
    s.refuse[second] = {"code": "upsert.record_deleted", "reason": "crdt: record is deleted"}
    s.set_config("chatScan.cursor", {})
    out = mod.main(job_args(s))
    assert out["asks"] == 0 and s.store("asks")[first]["status"] == "dismissed"


def test_a_first_visit_reads_only_the_last_seven_days():
    s = chats()
    msg(s, "dm", "old", "bob-id", "Can you call me?", "v1", at=THU - 10 * DAY)
    msg(s, "dm", "new", "bob-id", "Could you sign?", "v2", at=THU - DAY)
    mod, _ = kernel(s, watched=("dm",))
    mod.main(job_args(s))
    assert [a["quote"] for a in asks(s)] == ["Could you sign?"]
    assert s.config("chatScan.cursor") == {"chat-dm": "v2"}


def test_a_corrupt_cursor_raises():
    s = chats()
    s.set_config("chatScan.cursor", {"chat-dm": 42})
    mod, _ = kernel(s)
    with pytest.raises(Exception) as err:
        mod.main(job_args(s))
    assert type(err.value).__name__ == "cursor_corrupt"


# --- model failures and the call cap ----------------------------------------------------------

def test_a_provider_failure_keeps_the_cursor_and_an_all_failed_run_raises():
    s = chats()
    msg(s, "dm", "m1", "bob-id", "Can you call me?", "v1")
    msg(s, "dm", "m2", "me-id", "I'll call you tomorrow.", "v2")
    msg(s, "dm", "m3", "bob-id", "Could you sign?", "v3")
    model = Model(RuntimeError("provider down"))
    mod, _ = kernel(s, model, watched=("dm",))
    with pytest.raises(Exception) as err:
        mod.main(job_args(s))
    assert type(err.value).__name__ == "provider_failed"
    # what landed stays; the cursor waits before the failed message
    assert [a["quote"] for a in asks(s)] == ["Can you call me?"]
    assert s.config("chatScan.cursor") == {"chat-dm": "v1"}
    model.replies = [[{"quote": "I'll call you tomorrow.", "due": None}]]
    out = mod.main(job_args(s))
    assert out["promises"] == 1 and out["asks"] == 1
    assert s.config("chatScan.cursor") == {"chat-dm": "v3"}


def test_the_call_cap_counts_calls_and_leaves_the_rest_for_the_next_run():
    s = chats()
    for i in range(3):
        msg(s, "dm", f"m{i}", "me-id", f"I'll send part {i}.", f"v{i}")
    model = Model("prose", [{"quote": "I'll send part 0.", "due": None}],
                  *([{"quote": f"I'll send part {i}.", "due": None}] for i in (1, 2)))
    mod, _ = kernel(s, model, watched=("dm",))
    mod.MAX_CALLS = 4     # part 0 takes two (a retry), part 1 one; part 2 might need two
    out = mod.main(job_args(s))
    assert out["modelCalls"] == 3 and out["capped"] == 1 and out["promises"] == 2
    assert s.config("chatScan.cursor") == {"chat-dm": "v1"}
    out = mod.main(job_args(s))
    assert out["promises"] == 1 and s.config("chatScan.cursor") == {"chat-dm": "v2"}


# --- pages, behind the flag --------------------------------------------------------------------

def _page(s, pid, at, blocks):
    s.add_object(pid, "page", name=pid, modifiedAt={"$date": at * 1000},
                 modifiedBy="ada-id")
    for bid, text, checked in blocks:
        s.records.setdefault((pid, "editor_blocks"), {})[bid] = {
            "id": bid, "type": "check_list_item", "text": text,
            "style": {"checked": checked}}


def test_the_pages_flag_off_reads_no_page():
    s = chats()
    _page(s, "pg1", THU - 3600, [("b1", "Book the room", False)])
    for pages in (None, False):
        mod, _ = kernel(s, watched=("team",), pages=pages)
        assert mod.main(job_args(s))["pages"] == 0
    assert reads(s, "editor_blocks") == [] and s.store("asks") == {}


def test_the_pages_flag_on_makes_unchecked_items_checklist_asks():
    s = chats()
    _page(s, "pg1", THU - 3600, [("b1", "Book the room", False), ("b2", "Done one", True)])
    _page(s, "pg2", THU - 3 * DAY, [("b3", "Too old", False)])
    _page(s, "pg3", THU - 60, [("b4", "In the bin", False)])
    s.objects["pg3"]["any"]["collections"] = ["bin"]
    mod, _ = kernel(s, watched=("team",), pages=True)
    out = mod.main(job_args(s))
    (ask,) = asks(s)
    assert ask["kind"] == "checklist" and ask["quote"] == "Book the room"
    assert ask["source"] == "any://o/team/pg1/editor_blocks/b1" and ask["chatId"] == "pg1"
    assert ask["senderIdentity"] == "any://u/ada-id" and "suggestedDeadline" not in ask
    assert out["pages"] == 1 and out["checklist"] == 1
    assert s.config("chatScan.pagesCursor") == {"team": THU - 3600}


# --- tasks: the daily card and closing ----------------------------------------------------------

def test_one_task_suggestion_card_a_day():
    s = chats()
    msg(s, "dm", "m1", "bob-id", "Can you call me?", "v1")
    datasets = {**DATASETS, "cards": "hubtype_cards"}
    args = {**job_args(s), "datasets": datasets}
    mod, _ = kernel(s, watched=("dm",), datasets=datasets)
    out = mod.main(args)
    cid = mod.card_id("task_suggestion", "tasks:2026-10-22")
    assert out["card"] == cid and len(cid) == 64
    card = s.store("cards")[cid]
    assert card["kind"] == "task_suggestion" and card["subjectKey"] == "tasks:2026-10-22"
    assert card["title"] == "0 promises and 1 ask without a task"
    assert card["status"] == "open" and card["reason"]
    (item,) = card["items"]
    assert item["origin"] == "ask" and item["quote"] == "Can you call me?"
    assert item["suggestedDeadline"] == "2026-10-27"
    assert item["source"] == "any://o/dm/chat-dm/chat_messages/m1"
    assert card["personIds"] == []
    (sid, signal), = s.store("signals").items()
    assert signal["kind"] == "task_suggestion" and signal["decision"] == "card"
    assert signal["cardId"] == cid and card["signalId"] == sid
    assert sid == mod.signal_id(THU, "task_suggestion", "tasks:2026-10-22")
    msg(s, "dm", "m2", "bob-id", "Could you sign?", "v2")
    assert mod.main(args)["card"] is None and len(s.store("cards")) == 1


def test_no_card_without_a_cards_store_or_anything_to_suggest():
    s = chats()
    msg(s, "dm", "m1", "bob-id", "Can you call me?", "v1")
    mod, _ = kernel(s, watched=("dm",))
    assert mod.main(job_args(s))["card"] is None
    s2 = chats()
    datasets = {**DATASETS, "cards": "hubtype_cards"}
    mod, _ = kernel(s2, watched=("dm",), datasets=datasets)
    assert mod.main({**job_args(s2), "datasets": datasets})["card"] is None


def test_no_card_while_the_brain_is_mid_setup():
    s = chats()
    msg(s, "dm", "m1", "bob-id", "Can you call me?", "v1")
    datasets = {**DATASETS, "cards": "hubtype_cards"}
    mod, _ = kernel(s, watched=("dm",), datasets=datasets)
    mod._cfg["brain.status"] = {"state": "provisioning"}
    out = mod.main({**job_args(s), "datasets": datasets})
    assert out["card"] is None and out["asks"] == 1 and s.store("cards") == {}


def test_a_completed_task_closes_its_ask():
    s = chats()
    msg(s, "dm", "m1", "bob-id", "Can you call me?", "v1")
    mod, _ = kernel(s, watched=("dm",))
    mod.main(job_args(s))
    (rid,) = s.store("asks")
    s.store("asks")[rid]["taskId"] = "t1"
    s.add_object("t1", "task", ["brain_task"], task={"completed": True})
    out = mod.main(job_args(s))
    assert out["closed"] == 1 and s.store("asks")[rid]["status"] == "done"
    assert s.store("asks")[rid]["closedAt"] == {"$date": THU * 1000}


def test_stats_hold_counts_only():
    s = chats()
    msg(s, "dm", "m1", "bob-id", "Can you call me about the merger?", "v1")
    mod, _ = kernel(s, watched=("dm",))
    mod.main(job_args(s))
    stats = s.config("chatScan.stats")
    assert stats["asks"] == 1 and "merger" not in json.dumps(stats)
    assert HUB
