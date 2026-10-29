"""programs/brainTasks@v1 — promises into tasks, under the REAL guest kernel.
Pinned: one case per deadline rule, never earlier than today; suggest lists
open promises without a task, newest first (all of them for limit None);
create makes a catalog task in the Tasks app filed under brain_task and
links the promise; creating twice is a no-op; create_all does every
suggestion; a brain that is not set up refuses; an ask from an excluded
person or an excluded identity is never offered."""

import pytest
from brain_fakes import HUB, FakeSpace
from connectorenv import connector_kernel

DAY = 86400
THU = 1_792_670_400          # Thursday 2026-10-22 12:00 UTC
THU_DAY = THU // DAY * DAY
READY = {"state": "ready", "hub": {"objectId": HUB, "datasets": {
    "config": "hubtype_config", "promises": "hubtype_promises"}}}


EXCLUSIONS = {"version": 1, "personIds": [], "emails": [], "domains": []}


def kernel(space=None, now=THU, status=READY):
    if space is None:
        space = FakeSpace()
        space.seed_lists()

    def eff(name, payload):
        if name == "time.now":
            return {"epoch": float(now)}
        if name == "config.get":
            if status is None:
                raise KeyError(f"no config value for {payload['key']!r}")
            return {"value": status}
        raise AssertionError(f"unexpected effect {name}")

    return connector_kernel(effect=eff, any_client=space).use("brainTasks@v1"), space


def add_promise(space, rid, text, made=THU, due=None, task=None, person=None, status="open"):
    rec = {"id": rid, "text": text, "quote": text, "status": status,
           "source": f"any://o/bao-space/mb1/email_messages/{rid}",
           "madeAt": {"$date": made * 1000}, "personEmail": "ada@lab.org"}
    if due is not None:
        rec["dueAt"] = {"$date": due * 1000}
    if task:
        rec["taskId"] = task
    if person:
        rec["personId"] = person
    space.records.setdefault((HUB, "hubtype_promises"), {})[rid] = rec


def test_one_case_per_deadline_rule():
    mod, _ = kernel()
    fri = THU_DAY + DAY
    cases = [
        ("I'll send it.", THU, THU_DAY + 4 * DAY, None),        # 2 business days: Mon
        ("I'll send it today.", THU, THU_DAY, None),
        ("Ich schicke es sofort.", THU, THU_DAY, None),
        ("I'll send it tomorrow.", THU, THU_DAY + DAY, None),
        ("I'll do it this week.", THU, fri, None),
        ("Ich melde mich diese Woche.", THU, fri, None),
        ("I'll send it next week.", THU, THU_DAY + 8 * DAY, None),
        ("Ich schicke es nächste Woche.", THU, THU_DAY + 8 * DAY, None),
        ("I'll send it on Tuesday.", THU, THU_DAY + 5 * DAY, None),
        ("I'll send it by 2026-11-03.", THU, THU_DAY + 12 * DAY, None),
        ("I'll send the deck.", THU, THU_DAY + 7 * DAY, THU + 7 * DAY),   # its own dueAt
    ]
    for text, made, want, due in cases:
        assert mod._suggested_deadline(text, made, due, THU) == want, text
    # never earlier than today: an old promise with a past suggestion
    assert mod._suggested_deadline("I'll send it tomorrow.", THU - 30 * DAY, None,
                                  THU) == THU_DAY


def test_each_alternative_of_each_rule():
    mod, _ = kernel()
    fri = THU_DAY + DAY
    for text, want in [("I will send it asap.", THU_DAY), ("Ich schicke es heute.", THU_DAY),
                       ("Ich schicke es morgen.", THU_DAY + DAY),
                       ("I'll do it by the end of the week.", fri),
                       ("Ich schicke es bis Ende der Woche.", fri),
                       ("Ich schicke es naechste Woche.", THU_DAY + 8 * DAY),
                       ("Guten Morgen, ich schicke es.", THU_DAY + 4 * DAY)]:
        assert mod._suggested_deadline(text, THU, None, THU) == want, text


def test_rules_count_from_the_day_the_promise_was_made():
    mod, _ = kernel()
    mon, wed = THU - 3 * DAY, THU - DAY
    mon_day, wed_day = THU_DAY - 3 * DAY, THU_DAY - DAY
    assert mod._suggested_deadline("I'll do it this week.", mon, None, wed) == mon_day + 4 * DAY
    assert mod._suggested_deadline("I'll send it next week.", mon, None, wed) == mon_day + 11 * DAY
    assert mod._suggested_deadline("I'll send it on Thursday.", mon, None, wed) == mon_day + 3 * DAY
    assert mod._suggested_deadline("I'll send it.", mon, None, wed) == wed_day
    # made the week before: that week's Friday has passed, so today
    assert mod._suggested_deadline("I'll do it this week.", mon - 7 * DAY, None, wed) == wed_day


def test_suggest_lists_open_promises_without_a_task_newest_first():
    mod, s = kernel()
    add_promise(s, "a", "I'll send the deck.", made=THU - DAY)
    add_promise(s, "b", "I'll send the memo tomorrow.", made=THU)
    add_promise(s, "c", "I'll call.", task="t9")
    add_promise(s, "d", "I'll write.", status="done")
    out = mod.suggest()
    assert [r["id"] for r in out] == ["b", "a"]
    assert out[0]["suggestedDeadline"] == "2026-10-23"
    assert out[0]["source"].endswith("/email_messages/b")


def test_create_makes_a_task_in_the_tasks_app_and_links_the_promise():
    mod, s = kernel()
    add_promise(s, "b", "I'll send the memo tomorrow.", person="p1")
    out = mod.create("hub/b")
    task = s.objects[out["taskId"]]
    assert task["any"]["type"] == "task" and task["any"]["collections"] == ["brain_task"]
    assert task["any"]["name"] == "I'll send the memo tomorrow."
    assert task["task"]["deadline"] == "2026-10-23" and task["task"]["planning"] == "inbox"
    assert task["task"]["notes"].endswith("[Open the mail](any://o/bao-space/mb1/email_messages/b)")
    assert task["brain_task"] == {"sourceUri": "any://o/bao-space/mb1/email_messages/b",
                                  "origin": "promise", "suggestedDeadline": "2026-10-23",
                                  "personId": ["p1"]}
    assert s.store("promises")["b"]["taskId"] == out["taskId"]
    assert out == {"taskId": out["taskId"], "created": True, "deadline": "2026-10-23"}


def test_a_chosen_deadline_wins_and_the_suggestion_is_kept():
    mod, s = kernel()
    add_promise(s, "b", "I'll send the memo tomorrow.")
    out = mod.create("b", deadline="2026-11-01")
    task = s.objects[out["taskId"]]
    assert task["task"]["deadline"] == "2026-11-01"
    assert task["brain_task"]["suggestedDeadline"] == "2026-10-23"


def test_creating_twice_is_a_no_op():
    mod, s = kernel()
    add_promise(s, "b", "I'll send it.")
    first = mod.create("b")
    again = mod.create("b")
    assert again == {"taskId": first["taskId"], "created": False, "deadline": None}
    assert len([o for o in s.objects.values() if o["any"]["type"] == "task"]) == 1


def test_create_all_makes_every_suggestion():
    mod, s = kernel()
    add_promise(s, "a", "I'll send the deck.")
    add_promise(s, "b", "I'll send the memo.")
    out = mod.create_all()
    assert sorted(r["id"] for r in out) == ["a", "b"]
    assert mod.suggest() == []


def test_an_unknown_promise_raises():
    mod, _ = kernel()
    with pytest.raises(Exception) as err:
        mod.create("nope")
    assert type(err.value).__name__ == "unknown_promise"


def test_a_brain_that_is_not_set_up_refuses():
    mod, _ = kernel(status=None)
    with pytest.raises(Exception) as err:
        mod.suggest()
    assert type(err.value).__name__ == "not_set_up"
    mod2, _ = kernel(status={"state": "provisioning"})
    with pytest.raises(Exception, match="brain is off"):
        mod2.create("x")


def test_a_promise_to_someone_kept_out_is_never_offered_or_made_a_task():
    mod, s = kernel()
    add_promise(s, "p-ada", "I'll send the deck.", person="ada")
    add_promise(s, "p-eve", "I'll call you.", made=THU - DAY, person="eve")
    s.records[(HUB, "hubtype_promises")]["p-eve"]["personEmail"] = "eve@spy.org"
    add_promise(s, "p-bob", "I'll ping Bob.", made=THU - 2 * DAY, person="bob")
    s.records[(HUB, "hubtype_promises")]["p-bob"]["personEmail"] = "bob@rival.com"
    s.set_config("exclusions", {**EXCLUSIONS, "personIds": ["ada"], "emails": ["eve@spy.org"]})
    s.set_config("blocklist", {"senders": [], "domains": ["rival.com"], "labels": []})
    assert mod.suggest() == []
    for rid in ("p-ada", "p-eve", "p-bob"):
        with pytest.raises(Exception) as err:
            mod.create(rid)
        assert type(err.value).__name__ == "person_excluded"
    assert not [c for c in s.calls if c[0] == "create_object"]


def test_an_unreadable_exclusion_list_fails_closed():
    mod, s = kernel()
    add_promise(s, "p1", "I'll send the deck.")
    s.set_config("exclusions", {"version": 1, "personIds": []})
    with pytest.raises(Exception) as err:
        mod.suggest()
    assert type(err.value).__name__ == "exclusions_unreadable"


def test_a_promise_from_mail_that_copied_in_someone_excluded_later_stays_out():
    mod, s = kernel()
    add_promise(s, "p1", "I'll send the deck.", person="ada")
    s.add_mail("mb1", "p1", **{"from": "me@example.com", "to": "ada@lab.org",
                               "cc": "Eve <eve@spy.org>"},
               participants=["me@example.com", "ada@lab.org", "eve@spy.org"])
    assert [r["id"] for r in mod.suggest()] == ["p1"]
    s.set_config("exclusions", {**EXCLUSIONS, "emails": ["eve@spy.org"]})
    assert mod.suggest() == []
    with pytest.raises(Exception) as err:
        mod.create("p1")
    assert type(err.value).__name__ == "person_excluded"


# --- asks (ADR-033 §7.6) ---------------------------------------------------------

WITH_ASKS = {"state": "ready", "hub": {"objectId": HUB, "datasets": {
    "config": "hubtype_config", "promises": "hubtype_promises", "asks": "hubtype_asks"}}}


def add_ask(space, rid, text, asked=THU, kind="request", task=None, person=None,
            sender="peer-id", status="open"):
    rec = {"id": rid, "text": text, "quote": text, "kind": kind, "status": status,
           "senderIdentity": f"any://u/{sender}", "spaceId": "team", "chatId": "chat-team",
           "source": f"any://o/team/chat-team/chat_messages/{rid}",
           "askedAt": {"$date": asked * 1000}}
    if task:
        rec["taskId"] = task
    if person:
        rec["personId"] = person
    space.records.setdefault((HUB, "hubtype_asks"), {})[rid] = rec


def test_deadline_for_counts_by_origin():
    mod, _ = kernel()
    assert mod.deadline_for("promise", "I'll send it.", THU) == THU_DAY + 4 * DAY    # Mon
    assert mod.deadline_for("ask", "Can you review it?", THU) == THU_DAY + 5 * DAY   # Tue
    assert mod.deadline_for("ask", "Could you review it by Friday?", THU) == THU_DAY + DAY
    assert mod.deadline_for("ask", "Kannst du das heute machen?", THU) == THU_DAY
    assert mod.deadline_for("checklist", "Send the deck tomorrow", THU) is None
    # never earlier than today
    assert mod.deadline_for("ask", "Can you check?", THU - 30 * DAY, today=THU) == THU_DAY


def test_suggest_lists_promises_and_asks_together_newest_first():
    mod, s = kernel(status=WITH_ASKS)
    add_promise(s, "p1", "I'll send the deck.", made=THU - DAY)
    add_ask(s, "a1", "Can you review this by Friday?", asked=THU)
    add_ask(s, "a2", "Buy milk", asked=THU - 2 * DAY, kind="checklist")
    add_ask(s, "a3", "Could you call?", task="t1")
    add_ask(s, "a4", "Would you sign?", status="dismissed")
    out = mod.suggest()
    assert [(r["id"], r["origin"]) for r in out] == [("a1", "ask"), ("p1", "promise"),
                                                     ("a2", "checklist")]
    assert out[0]["suggestedDeadline"] == "2026-10-23" and out[0]["kind"] == "request"
    assert out[0]["senderIdentity"] == "any://u/peer-id"
    assert out[2]["suggestedDeadline"] is None


def test_create_from_ask_makes_a_task_and_links_the_ask():
    mod, s = kernel(status=WITH_ASKS)
    add_ask(s, "a1", "Can you review this?", person="p1")
    out = mod.create_from_ask("hub/a1")
    task = s.objects[out["taskId"]]
    assert task["any"]["type"] == "task" and task["any"]["collections"] == ["brain_task"]
    assert task["any"]["name"] == "Can you review this?"
    assert task["task"]["deadline"] == "2026-10-27" and task["task"]["planning"] == "inbox"
    assert task["task"]["notes"].endswith(
        "[Open the chat](any://o/team/chat-team/chat_messages/a1)")
    assert task["brain_task"] == {"sourceUri": "any://o/team/chat-team/chat_messages/a1",
                                  "origin": "ask", "suggestedDeadline": "2026-10-27",
                                  "personId": ["p1"]}
    assert s.store("asks")["a1"]["taskId"] == out["taskId"]
    assert mod.create_from_ask("a1") == {"taskId": out["taskId"], "created": False,
                                         "deadline": None}


def test_a_checklist_ask_becomes_a_task_without_a_deadline():
    mod, s = kernel(status=WITH_ASKS)
    add_ask(s, "c1", "Book the room", kind="checklist")
    out = mod.create_from_ask("c1")
    task = s.objects[out["taskId"]]
    assert "deadline" not in task["task"] and out["deadline"] is None
    assert task["brain_task"]["origin"] == "checklist"
    assert "suggestedDeadline" not in task["brain_task"]


def test_create_all_covers_promises_and_asks():
    mod, s = kernel(status=WITH_ASKS)
    add_promise(s, "p1", "I'll send the deck.")
    add_ask(s, "a1", "Can you review this?")
    out = mod.create_all()
    assert sorted((r["id"], r["origin"]) for r in out) == [("a1", "ask"), ("p1", "promise")]
    assert mod.suggest() == []


def test_an_ask_from_someone_kept_out_is_never_offered_or_made_a_task():
    mod, s = kernel(status=WITH_ASKS)
    s.add_object("ada", "profile", ["person", "brain_person"], name="Ada Lovelace",
                 brain_person={"emails": ["ada@lab.org"]})
    add_ask(s, "a1", "Can you call me?", person="ada")
    # not linked yet, but the directory names the sender as the excluded person
    add_ask(s, "a2", "Could you send it?", sender="ada-id")
    s.directory = {"ada-id": "Ada Lovelace"}
    add_ask(s, "a3", "Would you sign?", sender="bob-id")
    s.set_config("exclusions", {**EXCLUSIONS, "personIds": ["ada"]})
    assert [r["id"] for r in mod.suggest()] == ["a3"]
    for rid in ("a1", "a2"):
        with pytest.raises(Exception) as err:
            mod.create_from_ask(rid)
        assert type(err.value).__name__ == "person_excluded"


def test_an_ask_from_an_excluded_identity_is_never_offered_or_made_a_task():
    # the identity itself is on the list (brainSetup.exclude(identity=…)): no
    # person to match, and a link to a person who is not excluded does not help
    mod, s = kernel(status=WITH_ASKS)
    s.add_object("bob", "profile", ["person", "brain_person"], name="Robert Stone")
    add_ask(s, "a1", "Can you call me?", sender="bob-id")
    add_ask(s, "a2", "Could you send it?", sender="bob-id", person="bob")
    add_ask(s, "a3", "Would you sign?", sender="eve-id")
    s.set_config("exclusions", {**EXCLUSIONS, "identities": ["any://u/bob-id"]})
    assert [r["id"] for r in mod.suggest()] == ["a3"]
    for rid in ("a1", "a2"):
        with pytest.raises(Exception) as err:
            mod.create_from_ask(rid)
        assert type(err.value).__name__ == "person_excluded"
    assert [c for c in s.calls if c[0] == "get_identity"] == []   # no name lookup needed


def test_suggest_without_a_limit_lists_everything():
    mod, s = kernel()
    for i in range(25):
        add_promise(s, f"p{i:02d}", f"I'll send part {i}.", made=THU - i)
    assert len(mod.suggest()) == 20 and len(mod.suggest(limit=None)) == 25
    assert mod.suggest(limit=None)[0]["id"] == "p00"


def test_an_unknown_ask_raises():
    mod, _ = kernel(status=WITH_ASKS)
    with pytest.raises(Exception) as err:
        mod.create_from_ask("nope")
    assert type(err.value).__name__ == "unknown_ask"
