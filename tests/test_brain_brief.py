"""programs/brainBrief@v1 — the card for one person, under the REAL guest
kernel. Pinned: current facts in six groups, newest first, with quote and
source; proposed facts apart and superseded ones gone; addressing from the
person's preferences; what the user promised them and what they asked for;
the latest mail; a markdown rendering that quotes every fact; an excluded
person raises."""

import pytest
from brain_fakes import HUB, FakeSpace
from kernelenv import load_kernel

NOW = 1_800_000_000
DAY = 86400
ME = "me@example.com"
EXCLUSIONS = {"version": 1, "personIds": [], "emails": [], "domains": []}
READY = {"state": "ready", "hub": {"objectId": HUB, "datasets": {
    "config": "hubtype_config", "promises": "hubtype_promises", "facts": "hubtype_facts"}}}


def fact(rid, kind, value, *, status="current", at=NOW - DAY, origin="inferred"):
    return {"id": rid, "personId": "ada", "kind": kind, "value": value,
            "quote": f"I said {value}.", "source": f"any://o/bao-space/mb1/email_messages/{rid}",
            "channel": "email", "origin": origin, "confidence": 0.7, "status": status,
            "observedAt": {"$date": at * 1000}}


def space():
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    s.seed_lists()
    s.add_object("ada", "profile", ["person", "brain_person"], name="Ada Lovelace",
                 person={"email": "ada@lab.org"},
                 brain_person={"emails": ["ada@lab.org"], "mailCount": 4,
                               "lastContactAt": {"$date": (NOW - DAY) * 1000}})
    s.add_mail("mb1", "m1", subject="Deck", snippet="the deck", **{"from": "ada@lab.org"},
               participants=["ada@lab.org", ME], internalDate=(NOW - DAY) * 1000)
    s.records[(HUB, "hubtype_facts")] = {f["id"]: f for f in (
        fact("f1", "taste.drink", "espresso"),
        fact("f2", "work.role", "CTO", at=NOW - 100),
        fact("f0", "work.role", "Head of Research", status="superseded"),
        fact("f3", "preference.address_form", "du"),
        fact("f4", "history.asked_for", "an intro to Grace"),
        fact("f5", "history.consent", "OK to share her deck", status="proposed"),
    )}
    s.records[(HUB, "hubtype_promises")] = {
        "p1": {"id": "p1", "text": "Send the deck", "quote": "I'll send the deck.",
               "source": "any://o/bao-space/mb1/email_messages/m2", "status": "open",
               "personId": "ada", "madeAt": {"$date": (NOW - 2 * DAY) * 1000}},
        "p2": {"id": "p2", "text": "Call", "quote": "I'll call.", "source": "x",
               "status": "done", "personId": "ada", "madeAt": {"$date": NOW * 1000}},
    }
    return s


def kernel(s):
    def eff(name, payload):
        if name == "time.now":
            return {"epoch": float(NOW)}
        if name == "config.get":
            return {"value": READY}
        raise AssertionError(f"unexpected effect {name}")

    return load_kernel(effect=eff, any_client=s).use("brainBrief@v1")


def test_the_card_groups_current_facts_and_sets_proposed_apart():
    card = kernel(space()).person("ada")
    facts = card["facts"]
    assert [f["value"] for f in facts["tastes"]] == ["espresso"]
    assert [f["value"] for f in facts["work"]] == ["CTO"]
    assert facts["work"][0]["quote"] == "I said CTO."
    assert facts["work"][0]["source"].endswith("/email_messages/f2")
    assert [f["value"] for f in card["proposed"]] == ["OK to share her deck"]
    assert card["person"]["addressForm"] == "du" and card["person"]["name"] == "Ada Lovelace"


def test_promises_both_ways_and_the_latest_mail():
    card = kernel(space()).person("ada")
    assert [p["id"] for p in card["promises"]["mine"]] == ["p1"]
    assert [f["value"] for f in card["promises"]["theirs"]] == ["an intro to Grace"]
    assert [t["subject"] for t in card["threads"]] == ["Deck"]


def test_the_markdown_quotes_every_fact_and_marks_the_unconfirmed():
    md = kernel(space()).person("ada")["markdown"]
    assert md.startswith("## Ada Lovelace")
    assert "“I said espresso.”" in md and "“I said CTO.”" in md
    assert "Head of Research" not in md
    assert "### Unconfirmed" in md and "OK to share her deck" in md
    assert "### You promised" in md and "Send the deck" in md


def test_an_excluded_person_raises():
    s = space()
    s.set_config("exclusions", {**EXCLUSIONS, "personIds": ["ada"]})
    with pytest.raises(Exception) as err:
        kernel(s).person("ada")
    assert type(err.value).__name__ == "person_excluded"
