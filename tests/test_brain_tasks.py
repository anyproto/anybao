"""programs/brainTasks@v1 — promises into tasks, under the REAL guest kernel.
Pinned: one case per deadline rule, never earlier than today; suggest lists
open promises without a task, newest first; create makes a catalog task in
the Tasks app filed under brain_task and links the promise; creating twice
is a no-op; create_all does every suggestion; a brain that is not set up
refuses."""

import pytest
from brain_fakes import HUB, FakeSpace
from kernelenv import load_kernel

DAY = 86400
THU = 1_792_670_400          # Thursday 2026-10-22 12:00 UTC
THU_DAY = THU // DAY * DAY
READY = {"state": "ready", "hub": {"objectId": HUB, "datasets": {
    "config": "hubtype_config", "promises": "hubtype_promises"}}}


EXCLUSIONS = {"version": 1, "personIds": [], "emails": [], "domains": []}


def kernel(space=None, now=THU, status=READY):
    if space is None:
        space = FakeSpace()
        space.set_config("exclusions", dict(EXCLUSIONS))

    def eff(name, payload):
        if name == "time.now":
            return {"epoch": float(now)}
        if name == "config.get":
            if status is None:
                raise KeyError(f"no config value for {payload['key']!r}")
            return {"value": status}
        raise AssertionError(f"unexpected effect {name}")

    return load_kernel(effect=eff, any_client=space).use("brainTasks@v1"), space


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
        assert mod.suggested_deadline(text, made, due, THU) == want, text
    # never earlier than today: an old promise with a past suggestion
    assert mod.suggested_deadline("I'll send it tomorrow.", THU - 30 * DAY, None,
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
        assert mod.suggested_deadline(text, THU, None, THU) == want, text


def test_rules_count_from_the_day_the_promise_was_made():
    mod, _ = kernel()
    mon, wed = THU - 3 * DAY, THU - DAY
    mon_day, wed_day = THU_DAY - 3 * DAY, THU_DAY - DAY
    assert mod.suggested_deadline("I'll do it this week.", mon, None, wed) == mon_day + 4 * DAY
    assert mod.suggested_deadline("I'll send it next week.", mon, None, wed) == mon_day + 11 * DAY
    assert mod.suggested_deadline("I'll send it on Thursday.", mon, None, wed) == mon_day + 3 * DAY
    assert mod.suggested_deadline("I'll send it.", mon, None, wed) == wed_day
    # made the week before: that week's Friday has passed, so today
    assert mod.suggested_deadline("I'll do it this week.", mon - 7 * DAY, None, wed) == wed_day


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
    with pytest.raises(Exception, match="not set up yet"):
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
