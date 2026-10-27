"""programs/brainPromises@v1 — promises from sent mail, under the REAL guest
kernel. Pinned: every phrase in PHRASES yields a promise in a plain sentence
and every exclusion refuses one; only the mailbox's own sent mail is read;
the quote is the verbatim sentence and the source opens the message; the
dueAt table; ids are stable so a re-run writes nothing new; mail to an
excluded or blocklisted recipient is skipped whole; a rejected write fails
the run; a completed task closes its promise; the exclusion list fails
closed."""

import pytest
from brain_fakes import HUB, FakeSpace, job_args
from kernelenv import load_kernel

ME = "me@example.com"
EXCLUSIONS = {"version": 1, "personIds": [], "emails": [], "domains": []}
# Thursday 2026-10-22 12:00 UTC, in ms
MADE_MS = 1_792_670_400_000
DAY = 86400


def kernel(space=None):
    space = space or FakeSpace()

    def eff(name, payload):
        if name == "time.now":
            return {"epoch": 1_800_000_000.0}
        raise AssertionError(f"unexpected effect {name}")

    return load_kernel(effect=eff, any_client=space).use("brainPromises@v1"), space


def mail_space(body, to="Ada <ada@lab.org>", sender=ME, mid="m1"):
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    s.set_config("exclusions", dict(EXCLUSIONS))
    s.add_mail("mb1", mid, **{"from": sender, "to": to, "body": body,
                              "internalDate": MADE_MS})
    return s


def test_every_phrase_makes_a_promise_and_every_exclusion_refuses_one():
    mod, _ = kernel()
    plain = {
        r"\bi will\b": "I will send the deck.", r"\bi'll\b": "I'll send the deck.",
        r"\bi’ll\b": "I’ll send the deck.",
        r"\blet me (send|share|check|get back)\b": "Let me send you the notes.",
        r"\bwill do\b": "Will do, thanks for the reminder.",
        r"\bi'm going to\b": "I'm going to draft the memo.",
        r"\bi am going to\b": "I am going to draft the memo.",
        r"\bich schicke\b": "Ich schicke dir das Deck.", r"\bich werde\b": "Ich werde das prüfen.",
        r"\bich melde mich\b": "Ich melde mich nächste Woche.",
        r"\bich sende\b": "Ich sende dir die Folien.", r"\bschicke ich\b": "Das schicke ich dir.",
        r"\bmelde ich mich\b": "Dann melde ich mich bei dir.",
        r"\bich leite\b.*\bweiter\b": "Ich leite die Mail weiter.",
        r"\bich stelle\b.*\bvor\b": "Ich stelle euch gerne vor.",
        r"\bich kümmere mich\b": "Ich kümmere mich darum.",
    }
    assert set(plain) == {p for ps in mod.PHRASES.values() for p in ps}
    for pattern, sentence in plain.items():
        assert mod.is_promise(sentence), (pattern, sentence)
    refused = ["Will you send the deck?", "I'll send it if they agree.",
               "I'll come unless it rains.", "I'll bring it in case we need it.",
               "I would send it.", "I could send it.", "Ich schicke es, falls nötig.",
               "Ich melde mich, wenn ich zurück bin.", "Ich schicke es, sobald es da ist.",
               "Ich könnte es schicken.", "Ich würde es schicken.", "I won't send it.",
               "I won’t send it.", "I will not send it.", "I'll not send it.",
               "Ich schicke es nicht."]
    assert len(refused) == len(mod.EXCLUSIONS)
    for sentence in refused:
        assert not mod.is_promise(sentence), sentence


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


def test_a_sent_promise_becomes_a_record_with_its_quote_and_source():
    s = mail_space("Thanks for the call.\nI'll send the deck tomorrow.\n> I will be there.")
    mod, _ = kernel(s)
    out = mod.main(job_args(s))
    (rec,) = s.store("promises").values()
    assert rec["quote"] == "I'll send the deck tomorrow." == rec["text"]
    assert rec["source"] == "any://o/bao-space/mb1/email_messages/m1"
    assert rec["mailboxId"] == "mb1" and rec["channel"] == "email" and rec["status"] == "open"
    assert rec["personEmail"] == "ada@lab.org" and "personId" not in rec
    assert rec["madeAt"] == {"$date": MADE_MS}
    assert rec["dueAt"] == {"$date": (int(MADE_MS / 1000 // DAY) * DAY + DAY) * 1000}
    assert rec["id"] == mod.promise_id("m1", "I'll send the deck tomorrow.")
    assert out == {"messages": 1, "promises": 1, "closed": 0}
    assert s.config("promises.cursor") == {"mb1": MADE_MS}


def test_received_mail_is_never_read_for_promises():
    s = mail_space("I'll send you the contract.", to=ME, sender="ada@lab.org")
    mod, _ = kernel(s)
    mod.main(job_args(s))
    assert s.store("promises") == {}


def test_the_person_is_linked_when_known():
    s = mail_space("I'll send the deck.")
    s.add_object("p1", "profile", ["person", "brain_person"],
                 brain_person={"emails": ["ada@lab.org"]})
    mod, _ = kernel(s)
    mod.main(job_args(s))
    assert next(iter(s.store("promises").values()))["personId"] == "p1"


def test_a_re_run_writes_nothing_new():
    s = mail_space("I'll send the deck.")
    mod, _ = kernel(s)
    mod.main(job_args(s))
    s.set_config("promises.cursor", {})       # re-read the same mail
    out = mod.main(job_args(s))
    assert len(s.store("promises")) == 1 and out["promises"] == 1


def test_mail_to_an_excluded_or_blocklisted_recipient_is_skipped():
    s = mail_space("I'll send the deck.")
    s.set_config("exclusions", {**EXCLUSIONS, "emails": ["ada@lab.org"]})
    mod, _ = kernel(s)
    mod.main(job_args(s))
    assert s.store("promises") == {}
    s2 = mail_space("I'll send the deck.")
    s2.set_config("blocklist", {"domains": ["lab.org"], "senders": [], "labels": []})
    mod2, _ = kernel(s2)
    mod2.main(job_args(s2))
    assert s2.store("promises") == {}


def test_mail_to_a_person_excluded_by_id_is_skipped():
    s = mail_space("I'll send the deck.")
    s.add_object("ada", "profile", ["person", "brain_person"],
                 brain_person={"emails": ["ada@lab.org"]})
    s.set_config("exclusions", {**EXCLUSIONS, "personIds": ["ada"]})
    mod, _ = kernel(s)
    mod.main(job_args(s))
    assert s.store("promises") == {}


def test_a_rejected_write_fails_the_run_and_keeps_the_cursor():
    s = mail_space("I'll send the deck tomorrow.")
    mod, _ = kernel(s)
    mod.main(job_args(s))
    # the same message re-read with a changed write-once field
    s.set_config("promises.cursor", {})
    rec = next(iter(s.store("promises").values()))
    rec["quote"] = "something else"
    with pytest.raises(Exception, match="1 of 1 rejected") as err:
        mod.main(job_args(s))
    assert type(err.value).__name__ == "promises_rejected"
    assert s.config("promises.cursor") == {}


def test_a_completed_task_closes_its_promise():
    s = mail_space("I'll send the deck.")
    mod, _ = kernel(s)
    mod.main(job_args(s))
    pid = next(iter(s.store("promises")))
    s.store("promises")[pid]["taskId"] = "t1"
    s.add_object("t1", "task", ["brain_task"], task={"completed": True})
    out = mod.main(job_args(s))
    assert out["closed"] == 1
    assert s.store("promises")[pid]["status"] == "done"
    assert s.store("promises")[pid]["reason"] == "task completed"
    assert s.store("promises")[pid]["closedAt"] == {"$date": 1_800_000_000_000}


def test_a_task_outside_brain_task_still_closes_its_promise_and_an_open_one_does_not():
    s = mail_space("I'll send the deck.\nI'll call you tomorrow.")
    mod, _ = kernel(s)
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
    mod, _ = kernel(s)
    with pytest.raises(Exception) as err:
        mod.main(job_args(s))
    assert type(err.value).__name__ == "exclusions_unreadable"
    assert s.store("promises") == {}


def test_unconfigured_without_a_promises_store():
    s = mail_space("I'll send the deck.")
    mod, _ = kernel(s)
    args = job_args(s)
    del args["datasets"]["promises"]
    with pytest.raises(Exception) as err:
        mod.main(args)
    assert type(err.value).__name__ == "unconfigured"
    assert HUB
