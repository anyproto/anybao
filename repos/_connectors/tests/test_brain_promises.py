"""programs/brainPromises@v1 — promises from sent mail by a model, grounded,
under the REAL guest kernel. Pinned: nothing is read without consent; only
the mailbox's own sent mail goes to the model, cleaned of quoted lines; a
quote is kept only when it is a verbatim substring of that body, within
length, else dropped and counted; the dueAt rule table first, the model's
date as the fallback; a judge gates the extraction call when configured;
a message that already has a promise is not re-extracted; ids are stable so
a re-run writes nothing new and never reopens a closed promise; excluded,
blocklisted and labelled mail never reaches the model; a provider failure
leaves the cursor before its message and two stop the run; the call cap;
a rejected write fails the run; a completed task closes its promise; the
cursor is the `_ver.id` of the last message handled, so sent mail the
backfill lands later, below the dates already read, is still extracted."""

import json

import pytest
from brain_fakes import HUB, FakeSpace, job_args
from connectorenv import connector_kernel

ME = "me@example.com"
EXCLUSIONS = {"version": 1, "personIds": [], "emails": [], "domains": []}
# Thursday 2026-10-22 12:00 UTC, in ms
MADE_MS = 1_792_670_400_000
DAY = 86400
# two days after the fixtures' mail, inside the first run's 30-day window
NOW = MADE_MS / 1000 + 2 * 86400
CONSENT = {"allowed": True, "requestedAt": "t", "by": "chat"}


class Model:
    """The classify-tier extractor: one scripted JSON reply per call, the
    prompts kept. A reply that is an Exception is raised (provider down)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, messages, system="", tier=None, tools=None, max_tokens=None):
        self.prompts.append((tier, messages[0]["parts"][0]["text"]))
        reply = self.replies.pop(0) if self.replies else []
        if isinstance(reply, Exception):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return {"parts": [{"type": "text", "text": text}]}


def kernel(space=None, model=None, *, consent=True, judge=None):
    """`judge`: None = no judge model; else a function body -> P(commits)."""
    space = space or FakeSpace()
    model = model or Model()
    judged = []

    def eff(name, payload):
        if name == "time.now":
            return {"epoch": NOW}
        if name == "config.get":
            if payload["key"] == "brain.model" and consent is not None:
                return {"value": {**CONSENT, "allowed": consent}}
            raise KeyError(f"no config value for {payload['key']!r}")
        if name == "test.judge_available":
            return {"value": judge is not None}
        if name == "test.judge":
            assert judge is not None and "commits" in payload["questions"]
            judged.append(payload["state"])
            return {"answers": {"commits": {"type": "noul", "noul": judge(payload["state"])}},
                    "model": "jev-test", "usage": {"in": 1, "out": 0}}
        raise AssertionError(f"unexpected effect {name}")

    mod = connector_kernel(effect=eff, any_client=space, llm_chat=model).use("brainPromises@v1")
    mod._judged = judged
    return mod, space


def mail_space(body, to="Ada <ada@lab.org>", sender=ME, mid="m1"):
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    s.seed_lists()
    s.add_mail("mb1", mid, **{"from": sender, "to": to, "body": body,
                              "internalDate": MADE_MS})
    return s


def quotes(space):
    return sorted(p["quote"] for p in space.store("promises").values())


def ver(space, mid, mailbox="mb1"):
    """The `_ver.id` the fake stamped on a message: the cursor's unit."""
    return space.records[(mailbox, "email_messages")][mid]["_ver"]["id"]


def test_nothing_is_read_or_asked_without_consent():
    s = mail_space("I'll send the deck tomorrow.")
    for consent in (False, None):
        model = Model([{"quote": "I'll send the deck tomorrow.", "due": None}])
        mod, _ = kernel(s, model, consent=consent)
        assert mod.main(job_args(s)) == {"skipped": "consent"}
        assert model.prompts == [] and s.store("promises") == {}
        assert not [c for c in s.calls if c[0] == "query" and c[2] == "email_messages"]


def test_a_grounded_quote_becomes_a_record_and_a_paraphrase_is_dropped():
    body = "Thanks for the call.\nI'll send the deck tomorrow.\n> I will be there.\nBest, me"
    model = Model([{"quote": "I'll send the deck tomorrow.", "due": None},
                   {"quote": "I will send you the deck", "due": None},      # paraphrased
                   {"quote": "I will be there.", "due": None},             # a quoted line
                   {"quote": "Best", "due": None},                         # too short
                   "not an object"])
    s = mail_space(body)
    mod, _ = kernel(s, model)
    out = mod.main(job_args(s))
    (rec,) = s.store("promises").values()
    assert rec["quote"] == "I'll send the deck tomorrow." == rec["text"]
    assert rec["source"] == "any://o/bao-space/mb1/email_messages/m1"
    assert rec["mailboxId"] == "mb1" and rec["channel"] == "email" and rec["status"] == "open"
    assert rec["personEmail"] == "ada@lab.org" and "personId" not in rec
    assert rec["madeAt"] == {"$date": MADE_MS}
    assert rec["dueAt"] == {"$date": (int(MADE_MS / 1000 // DAY) * DAY + DAY) * 1000}
    assert rec["id"] == mod.promise_id("m1", "I'll send the deck tomorrow.")
    assert out["promises"] == 1 and out["messages"] == 1 and out["closed"] == 0
    assert out["dropped"] == {"notVerbatim": 2, "badLength": 1, "badShape": 1}
    assert s.config("promises.cursor") == {"mb1": ver(s, "m1")}
    # the model saw the cleaned body: no quoted line, on the classify tier
    ((tier, prompt),) = model.prompts
    assert tier == "classify" and "> I will be there." not in prompt
    assert "I'll send the deck tomorrow." in prompt
    stats = s.config("promises.stats")
    assert stats["asked"] == 1 and stats["grounded"] == 1 and stats["written"] == 1


def test_the_rule_table_first_then_the_models_date():
    made = MADE_MS / 1000                     # a Thursday
    day = int(made // DAY) * DAY
    body = ("I'll send it next week.\nI'll send the deck.\nI'll call you.\n"
            "I'll write the memo.")
    model = Model([{"quote": "I'll send it next week.", "due": "2026-10-23"},   # table wins
                   {"quote": "I'll send the deck.", "due": "2026-11-03"},      # the hint
                   {"quote": "I'll call you.", "due": "soon"},                 # no date
                   {"quote": "I'll write the memo.", "due": "2026-13-40"}])    # invalid
    s = mail_space(body)
    mod, _ = kernel(s, model)
    mod.main(job_args(s))
    by_quote = {p["quote"]: p.get("dueAt") for p in s.store("promises").values()}
    assert by_quote["I'll send it next week."] == {"$date": (day + 8 * DAY) * 1000}
    assert by_quote["I'll send the deck."] == {"$date": mod.kit.civil_day(2026, 11, 3) * 1000}
    assert by_quote["I'll call you."] is None and by_quote["I'll write the memo."] is None


def test_due_at_table():
    mod, _ = kernel()
    made = MADE_MS / 1000            # a Thursday
    day = int(made // DAY) * DAY
    cases = {"I'll send it today.": day, "I'll send it tomorrow.": day + DAY,
             "Ich schicke es morgen.": day + DAY, "I'll do it by the end of the week.": day + DAY,
             "I'll send it next week.": day + 8 * DAY,
             "Ich melde mich nächste Woche.": day + 8 * DAY,
             "I'll send it on Monday.": day + 4 * DAY, "Ich schicke es bis Freitag.": day + DAY,
             "I'll send it Thursday.": day + 7 * DAY, "I'll send the deck.": None,
             "Guten Morgen, ich schicke es.": None}
    for sentence, want in cases.items():
        assert mod.due_at(sentence, made) == want, sentence


def test_received_mail_is_never_sent_to_the_model():
    s = mail_space("I'll send you the contract.", to=ME, sender="ada@lab.org")
    model = Model([{"quote": "I'll send you the contract.", "due": None}])
    mod, _ = kernel(s, model)
    out = mod.main(job_args(s))
    assert model.prompts == [] and s.store("promises") == {}
    assert out["messages"] == 1 and out["sent"] == 0
    assert s.config("promises.cursor") == {"mb1": ver(s, "m1")}


def test_a_judge_gates_the_extraction_call():
    s = mail_space("Thanks, see you Monday.")
    s.add_mail("mb1", "m2", **{"from": ME, "to": "Ada <ada@lab.org>",
                               "body": "I'll send the deck tomorrow.",
                               "internalDate": MADE_MS + 1000})
    model = Model([{"quote": "I'll send the deck tomorrow.", "due": None}])
    mod, _ = kernel(s, model, judge=lambda body: 0.9 if "send" in body else 0.05)
    out = mod.main(job_args(s))
    assert mod._judged == ["Thanks, see you Monday.", "I'll send the deck tomorrow."]
    assert [p for _, p in model.prompts] == ["I'll send the deck tomorrow."]
    assert quotes(s) == ["I'll send the deck tomorrow."]
    assert out["judged"] == 2 and out["skippedByJudge"] == 1 and out["asked"] == 1


def test_without_a_judge_every_sent_message_is_extracted():
    s = mail_space("Thanks, see you Monday.")
    model = Model([])
    mod, _ = kernel(s, model)
    out = mod.main(job_args(s))
    assert mod._judged == [] and len(model.prompts) == 1
    assert out["judged"] == 0 and out["asked"] == 1 and s.store("promises") == {}


def test_the_person_is_linked_when_known():
    s = mail_space("I'll send the deck.")
    s.add_object("p1", "profile", ["person", "brain_person"],
                 brain_person={"emails": ["ada@lab.org"]})
    mod, _ = kernel(s, Model([{"quote": "I'll send the deck.", "due": None}]))
    mod.main(job_args(s))
    assert next(iter(s.store("promises").values()))["personId"] == "p1"


def test_a_message_with_a_promise_is_never_re_extracted():
    # a re-run (the cursor reset) asks the model nothing for a message that
    # already has a record, so a differently cut quote cannot land twice
    s = mail_space("I'll send the deck.")
    model = Model([{"quote": "I'll send the deck.", "due": None}],
                  [{"quote": "send the deck.", "due": None}])
    mod, _ = kernel(s, model)
    mod.main(job_args(s))
    s.set_config("promises.cursor", {})
    out = mod.main(job_args(s))
    assert len(model.prompts) == 1 and len(s.store("promises")) == 1
    assert out["promises"] == 0 and out["alreadyExtracted"] == 1


def test_re_reading_a_message_never_reopens_a_closed_promise():
    s = mail_space("I'll send the deck tomorrow.")
    reply = [{"quote": "I'll send the deck tomorrow.", "due": None}]
    mod, _ = kernel(s, Model(reply, reply))
    mod.main(job_args(s))
    (rid,) = s.store("promises")
    s.store("promises")[rid].update(status="done", reason="task completed")
    s.store("promises")[rid]["source"] = "elsewhere"        # forces a re-ask
    s.set_config("promises.cursor", {})
    mod.main(job_args(s))
    assert s.store("promises")[rid]["status"] == "done"


def test_mail_to_an_excluded_or_blocklisted_recipient_never_reaches_the_model():
    for lists in ({"exclusions": {**EXCLUSIONS, "emails": ["ada@lab.org"]}},
                  {"blocklist": {"domains": ["lab.org"], "senders": [], "labels": []}}):
        s = mail_space("I'll send the deck.")
        for name, value in lists.items():
            s.set_config(name, value)
        model = Model([{"quote": "I'll send the deck.", "due": None}])
        mod, _ = kernel(s, model)
        out = mod.main(job_args(s))
        assert model.prompts == [] and s.store("promises") == {}
        assert out["excludedSkipped"] == 1


def test_mail_to_a_person_excluded_by_id_is_skipped():
    s = mail_space("I'll send the deck.")
    s.add_object("ada", "profile", ["person", "brain_person"],
                 brain_person={"emails": ["ada@lab.org"]})
    s.set_config("exclusions", {**EXCLUSIONS, "personIds": ["ada"]})
    model = Model([{"quote": "I'll send the deck.", "due": None}])
    mod, _ = kernel(s, model)
    mod.main(job_args(s))
    assert model.prompts == [] and s.store("promises") == {}


def test_sent_mail_under_a_blocklisted_label_is_never_read():
    s = mail_space("I'll send the lab results tomorrow.")
    s.records[("mb1", "email_messages")]["m1"]["labelIds"] = ["SENT", "Label_Medical"]
    s.set_config("blocklist", {"senders": [], "domains": [], "labels": ["label_medical"]})
    model = Model([{"quote": "I'll send the lab results tomorrow.", "due": None}])
    mod, _ = kernel(s, model)
    out = mod.main(job_args(s))
    assert model.prompts == [] and s.store("promises") == {} and out["messages"] == 1


def test_a_provider_failure_with_one_mailbox_fails_the_run_and_keeps_the_cursor():
    # seen live: with one mailbox the old "two failures stop the run" rule
    # never fired, so a provider that was down passed, run after run, for
    # a job that read nothing — and the brain stayed "ready"
    s = mail_space("I'll send the deck.")
    s.add_mail("mb1", "m2", **{"from": ME, "to": "Ada <ada@lab.org>",
                               "body": "I'll call you.", "internalDate": MADE_MS + 1000})
    model = Model(RuntimeError("provider says no"))
    mod, _ = kernel(s, model)
    with pytest.raises(Exception) as err:
        mod.main(job_args(s))
    assert type(err.value).__name__ == "provider_failed"
    assert len(model.prompts) == 1                           # the mailbox stopped at m1
    assert s.store("promises") == {} and s.config("promises.cursor") is None
    # the next run picks both up
    model.replies = [[{"quote": "I'll send the deck.", "due": None}],
                     [{"quote": "I'll call you.", "due": None}]]
    out = mod.main(job_args(s))
    assert out["promises"] == 2 and s.config("promises.cursor") == {"mb1": ver(s, "m2")}


def test_a_provider_failure_beside_a_handled_mailbox_only_holds_that_mailbox():
    s = mail_space("I'll send the deck.")
    s.add_mailbox("mb2", "two@example.com")
    s.add_mail("mb2", "m9", **{"from": "two@example.com", "to": "Bob <bob@x.io>",
                               "body": "I'll call you.", "internalDate": MADE_MS})
    model = Model(RuntimeError("provider says no"), [{"quote": "I'll call you.", "due": None}])
    mod, _ = kernel(s, model)
    out = mod.main(job_args(s))
    assert out["providerFailed"] == 1 and out["promises"] == 1
    assert s.config("promises.cursor") == {"mb2": ver(s, "m9", "mb2")}   # mb1: no cursor yet


def test_two_provider_failures_stop_the_run():
    s = mail_space("I'll send the deck.")
    s.add_mailbox("mb2", "two@example.com")
    s.add_mail("mb2", "m9", **{"from": "two@example.com", "to": "Bob <bob@x.io>",
                               "body": "I'll call you.", "internalDate": MADE_MS})
    model = Model(RuntimeError("down"), RuntimeError("still down"))
    mod, _ = kernel(s, model)
    with pytest.raises(Exception) as err:
        mod.main(job_args(s))
    assert type(err.value).__name__ == "provider_failed"
    assert s.config("promises.cursor") is None and s.store("promises") == {}


def test_an_unparsable_reply_is_retried_once_then_counted():
    s = mail_space("I'll send the deck.")
    model = Model("no json here", "still prose")
    mod, _ = kernel(s, model)
    out = mod.main(job_args(s))
    assert len(model.prompts) == 2 and out["parseFailed"] == 1
    assert s.store("promises") == {} and s.config("promises.cursor") == {"mb1": ver(s, "m1")}


def test_the_call_cap_leaves_the_rest_for_the_next_run():
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    s.seed_lists()
    for i in range(3):
        s.add_mail("mb1", f"m{i}", **{"from": ME, "to": "Ada <ada@lab.org>",
                                      "body": f"I'll send part {i}.",
                                      "internalDate": MADE_MS + i * 1000})
    model = Model(*([{"quote": f"I'll send part {i}.", "due": None}] for i in range(3)))
    mod, _ = kernel(s, model)
    mod.MAX_CALLS = 2
    out = mod.main(job_args(s))
    assert out["promises"] == 2 and s.config("promises.cursor") == {"mb1": ver(s, "m1")}
    out = mod.main(job_args(s))
    assert out["promises"] == 1 and s.config("promises.cursor") == {"mb1": ver(s, "m2")}


def test_a_rejected_write_fails_the_run_and_keeps_the_cursor():
    s = mail_space("I'll send the deck tomorrow.")
    mod, _ = kernel(s, Model([{"quote": "I'll send the deck tomorrow.", "due": None}]))
    s.refuse[mod.promise_id("m1", "I'll send the deck tomorrow.")] = "field madeAt: invalid"
    with pytest.raises(Exception, match="1 of 1 rejected: field madeAt: invalid") as err:
        mod.main(job_args(s))
    assert type(err.value).__name__ == "promises_rejected"
    assert s.config("promises.cursor") is None


def test_a_promise_whose_record_was_deleted_is_not_a_failure():
    s = mail_space("I'll send the deck tomorrow.")
    mod, _ = kernel(s, Model([{"quote": "I'll send the deck tomorrow.", "due": None}]))
    s.refuse[mod.promise_id("m1", "I'll send the deck tomorrow.")] = {
        "code": "upsert.record_deleted", "reason": "crdt: record is deleted"}
    out = mod.main(job_args(s))
    assert out["promises"] == 0
    assert s.config("promises.cursor") == {"mb1": ver(s, "m1")}


def test_a_completed_task_closes_its_promise():
    s = mail_space("I'll send the deck.")
    mod, _ = kernel(s, Model([{"quote": "I'll send the deck.", "due": None}]))
    mod.main(job_args(s))
    pid = next(iter(s.store("promises")))
    s.store("promises")[pid]["taskId"] = "t1"
    s.add_object("t1", "task", ["brain_task"], task={"completed": True})
    out = mod.main(job_args(s))
    assert out["closed"] == 1
    assert s.store("promises")[pid]["status"] == "done"
    assert s.store("promises")[pid]["reason"] == "task completed"
    assert s.store("promises")[pid]["closedAt"] == {"$date": NOW * 1000}


def test_a_task_outside_brain_task_still_closes_its_promise_and_an_open_one_does_not():
    s = mail_space("I'll send the deck.\nI'll call you tomorrow.")
    mod, _ = kernel(s, Model([{"quote": "I'll send the deck.", "due": None},
                              {"quote": "I'll call you tomorrow.", "due": None}]))
    mod.main(job_args(s))
    first, second = sorted(s.store("promises"))
    s.store("promises")[first]["taskId"] = "t1"
    s.store("promises")[second]["taskId"] = "t2"
    s.add_object("t1", "task", [], task={"completed": True})
    s.add_object("t2", "task", ["brain_task"], task={"completed": False})
    out = mod.main(job_args(s))
    assert out["closed"] == 1
    assert s.store("promises")[first]["status"] == "done"
    assert s.store("promises")[second]["status"] == "open"


def test_an_unreadable_exclusion_list_fails_closed():
    s = mail_space("I'll send the deck.")
    s.set_config("exclusions", None)
    model = Model([{"quote": "I'll send the deck.", "due": None}])
    mod, _ = kernel(s, model)
    with pytest.raises(Exception) as err:
        mod.main(job_args(s))
    assert type(err.value).__name__ == "exclusions_unreadable"
    assert model.prompts == [] and s.store("promises") == {}


def test_unconfigured_without_a_promises_store():
    s = mail_space("I'll send the deck.")
    mod, _ = kernel(s)
    args = job_args(s)
    del args["datasets"]["promises"]
    with pytest.raises(Exception) as err:
        mod.main(args)
    assert type(err.value).__name__ == "unconfigured"
    assert HUB


def test_before_the_first_mail_sync_there_is_nothing_to_read():
    s = FakeSpace()                      # no mailbox type: no sync has run
    s.seed_lists()
    mod, _ = kernel(s)
    out = mod.main(job_args(s))
    assert out["messages"] == 0 and out["promises"] == 0 and out["closed"] == 0


def test_a_first_run_starts_thirty_days_back_not_at_the_oldest_mail():
    s = mail_space("I'll send the deck tomorrow.")
    s.add_mail("mb1", "old", **{"from": ME, "to": "Ada <ada@lab.org>",
                                "body": "I'll send the old report on Monday."},
               internalDate=NOW * 1000 - 40 * 86_400_000)
    model = Model([{"quote": "I'll send the deck tomorrow.", "due": None}])
    mod, _ = kernel(s, model)
    out = mod.main(job_args(s))
    assert quotes(s) == ["I'll send the deck tomorrow."]
    assert out["messages"] == 1 and len(model.prompts) == 1


def test_extract_grounds_and_counts_every_model_call():
    # the helper the chat scan shares: a judge, a call and a retry are three
    model = Model("prose, no json", [{"quote": "I'll send the deck tomorrow.", "due": None},
                                     {"quote": "I will send it", "due": None}])
    mod, _ = kernel(model=model, judge=lambda body: 0.9)
    stats = {}
    items, calls = mod.extract("Thanks!\nI'll send the deck tomorrow.", stats=stats)
    assert items == [{"quote": "I'll send the deck tomorrow.", "due": None}]
    assert calls == 3 and stats["judged"] == 1 and stats["asked"] == 2
    assert stats["dropped"] == {"notVerbatim": 1} and stats["grounded"] == 1
    assert mod.extract("See you.", gate=False)[0] == []


def test_a_failing_judge_is_a_provider_failure_not_a_crash_or_a_pass():
    # an EffectError from llm@v1 (here the judge's) is a provider failure:
    # the mailbox stops before the message instead of reading it as empty,
    # and a run in which no call succeeded fails
    s = mail_space("I'll send the deck.")

    def broken(_body):
        raise RuntimeError("judge timed out")

    mod, _ = kernel(s, Model([{"quote": "I'll send the deck.", "due": None}]), judge=broken)
    with pytest.raises(Exception) as failed:
        mod.main(job_args(s))
    assert type(failed.value).__name__ == "provider_failed"
    assert s.store("promises") == {}
    assert "mb1" not in (s.config("promises.cursor") or {})
    with pytest.raises(Exception) as err:
        mod.extract("I'll send the deck.")
    assert type(err.value).__name__ == "model_failed"


def test_a_failed_extract_says_how_many_calls_it_made():
    # the chat scan charges its call budget with what a failed extract spent:
    # the judge that answered plus the call that failed are two, a retry
    # that failed after an unparsable reply two as well
    model = Model(RuntimeError("down"))
    mod, _ = kernel(model=model, judge=lambda body: 0.9)
    with pytest.raises(Exception) as err:
        mod.extract("I'll send the deck.")
    assert type(err.value).__name__ == "model_failed" and err.value.calls == 2
    mod, _ = kernel(model=Model("prose", RuntimeError("down")))
    with pytest.raises(Exception) as err:
        mod.extract("I'll send the deck.")
    assert err.value.calls == 2
    mod, _ = kernel(model=Model(RuntimeError("down")))
    with pytest.raises(Exception) as err:
        mod.extract("I'll send the deck.")
    assert err.value.calls == 1


def test_a_corrupt_cursor_raises():
    s = mail_space("I'll send the deck.")
    s.set_config("promises.cursor", {"mb1": MADE_MS})          # a date, not a _ver.id
    mod, _ = kernel(s, Model([{"quote": "I'll send the deck.", "due": None}]))
    with pytest.raises(Exception) as err:
        mod.main(job_args(s))
    assert type(err.value).__name__ == "cursor_corrupt"
    assert s.store("promises") == {}


def test_sent_mail_the_backfill_lands_later_is_still_extracted():
    # the Gmail backfill writes newest first, in slices across its ticks: a
    # run between two slices sets the cursor on today's mail, and the older
    # sent mail of the next slice lands with a HIGHER _ver.id but an OLDER
    # date — a date cursor would pass it for good (review A1)
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    s.seed_lists()
    s.add_mail("mb1", "today", ver="v-0001", **{"from": ME, "to": "Ada <ada@lab.org>",
                                                "body": "I'll send the deck tomorrow.",
                                                "internalDate": MADE_MS})
    model = Model([{"quote": "I'll send the deck tomorrow.", "due": None}],
                  [{"quote": "I'll call you on Monday.", "due": None}])
    mod, _ = kernel(s, model)
    out = mod.main(job_args(s))
    assert out["promises"] == 1 and s.config("promises.cursor") == {"mb1": "v-0001"}
    s.add_mail("mb1", "older", ver="v-0002", **{"from": ME, "to": "Bob <bob@x.io>",
                                                "body": "I'll call you on Monday.",
                                                "internalDate": MADE_MS - 20 * DAY * 1000})
    out = mod.main(job_args(s))
    assert out["promises"] == 1 and out["messages"] == 1
    assert quotes(s) == ["I'll call you on Monday.", "I'll send the deck tomorrow."]
    assert s.config("promises.cursor") == {"mb1": "v-0002"}
    older = next(p for p in s.store("promises").values() if p["personEmail"] == "bob@x.io")
    assert older["madeAt"] == {"$date": MADE_MS - 20 * DAY * 1000}


def test_mail_older_than_the_floor_that_lands_after_the_cursor_is_not_read():
    # the backfill lands a year of mail, oldest last: the floor holds with a
    # cursor too, or every old commitment would be listed open and overdue
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    s.seed_lists()
    s.add_mail("mb1", "today", ver="v-0001", **{"from": ME, "to": "Ada <ada@lab.org>",
                                                "body": "I'll send the deck tomorrow.",
                                                "internalDate": MADE_MS})
    model = Model([{"quote": "I'll send the deck tomorrow.", "due": None}])
    mod, _ = kernel(s, model)
    assert mod.main(job_args(s))["promises"] == 1
    s.add_mail("mb1", "old", ver="v-0002", **{"from": ME, "to": "Bob <bob@x.io>",
                                              "body": "I'll send the old report on Monday.",
                                              "internalDate": MADE_MS - 40 * DAY * 1000})
    out = mod.main(job_args(s))
    assert out["messages"] == 0 and out["promises"] == 0 and len(model.prompts) == 1
    assert quotes(s) == ["I'll send the deck tomorrow."]
    assert s.config("promises.cursor") == {"mb1": "v-0001"}
