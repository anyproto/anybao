"""programs/brainRecall@v1 — what the brain knows, under the REAL guest
kernel. Pinned: one index search per scope; each hit is read back for its
date and reranked by age (mail decays, a promise does not); mail with an
excluded or blocklisted participant, a promise to an excluded person and an
excluded person's object never appear; a scope the index refuses is
skipped, not fatal; the person view joins brain columns, promises and the
latest mail across mailboxes; an excluded or unknown person raises; a brain
that is not set up refuses."""

import pytest
from brain_fakes import HUB, FakeSpace
from kernelenv import load_kernel

DAY = 86400
NOW = 1_800_000_000
ME = "me@example.com"
EXCLUSIONS = {"version": 1, "personIds": [], "emails": [], "domains": []}
READY = {"state": "ready", "hub": {"objectId": HUB, "datasets": {
    "config": "hubtype_config", "promises": "hubtype_promises"}}}


def kernel(space, status=READY):
    def eff(name, payload):
        if name == "time.now":
            return {"epoch": float(NOW)}
        if name == "config.get":
            if status is None:
                raise KeyError(f"no config value for {payload['key']!r}")
            return {"value": status}
        raise AssertionError(f"unexpected effect {name}")

    return load_kernel(effect=eff, any_client=space).use("brainRecall@v1")


def mail_space():
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    s.set_config("exclusions", dict(EXCLUSIONS))
    s.add_mail("mb1", "old", subject="Old deck", **{"from": "ada@lab.org", "to": ME},
               participants=["ada@lab.org", ME], internalDate=(NOW - 300 * DAY) * 1000)
    s.add_mail("mb1", "new", subject="New deck", **{"from": "ada@lab.org", "to": ME},
               participants=["ada@lab.org", ME], internalDate=(NOW - DAY) * 1000)
    s.add_mail("mb1", "secret", subject="Secret deck", **{"from": "eve@spy.org", "to": ME},
               participants=["eve@spy.org", ME], internalDate=NOW * 1000)
    s.records[(HUB, "hubtype_promises")] = {
        "p1": {"id": "p1", "text": "Send the deck", "quote": "I'll send the deck.",
               "source": "any://o/bao-space/mb1/email_messages/new", "status": "open",
               "personId": "ada", "personEmail": "ada@lab.org",
               "madeAt": {"$date": (NOW - 400 * DAY) * 1000}},
        "p2": {"id": "p2", "text": "Call Eve", "quote": "I'll call you.",
               "source": "any://o/bao-space/mb1/email_messages/secret", "status": "open",
               "personId": "eve", "personEmail": "eve@spy.org",
               "madeAt": {"$date": NOW * 1000}},
    }
    s.search_hits = {
        "email": [{"objectId": "mb1", "recordId": r, "score": 1.0, "data": "deck"}
                  for r in ("old", "new", "secret")],
        "promises": [{"objectId": HUB, "recordId": f"{HUB}/{r}", "score": 1.0}
                     for r in ("p1", "p2")],
        "basic": [{"objectId": "page1", "title": "Deck notes", "data": "deck", "score": 0.5},
                  {"objectId": "eve", "title": "Eve", "data": "deck", "score": 0.9}],
    }
    return s


def test_one_search_per_scope_newest_mail_first():
    s = mail_space()
    out = kernel(s).search("deck")
    assert [c[2] for c in s.calls if c[0] == "search"] == [("basic",), ("email",), ("promises",)]
    email = next(g for g in out["groups"] if g["scope"] == "email")
    assert [h["recordId"] for h in email["hits"]] == ["secret", "new", "old"]
    assert email["hits"][1]["link"] == "any://o/bao-space/mb1/email_messages/new"
    assert email["hits"][1]["title"] == "New deck"
    assert out["skipped"] == []


def test_kept_out_mail_promises_and_people_never_appear():
    s = mail_space()
    s.set_config("exclusions", {**EXCLUSIONS, "personIds": ["eve"], "emails": ["eve@spy.org"]})
    out = kernel(s).search("deck")
    by_scope = {g["scope"]: [h["recordId"] or h["objectId"] for h in g["hits"]]
                for g in out["groups"]}
    assert by_scope["email"] == ["new", "old"]
    assert by_scope["promises"] == ["p1"]
    assert by_scope["basic"] == ["page1"]
    s2 = mail_space()
    s2.set_config("blocklist", {"domains": ["spy.org"], "senders": [], "labels": []})
    email = next(g for g in kernel(s2).search("deck")["groups"] if g["scope"] == "email")
    assert [h["recordId"] for h in email["hits"]] == ["new", "old"]


def test_mail_from_a_person_excluded_only_by_id_never_appears():
    s = mail_space()
    s.add_object("eve", "profile", ["person", "brain_person"], name="Eve",
                 person={"email": "eve@spy.org"})
    s.set_config("exclusions", {**EXCLUSIONS, "personIds": ["eve"]})
    email = next(g for g in kernel(s).search("deck")["groups"] if g["scope"] == "email")
    assert [h["recordId"] for h in email["hits"]] == ["new", "old"]


def test_a_promise_does_not_decay_and_per_scope_caps():
    s = mail_space()
    out = kernel(s).search("deck", scopes=["promises", "email"], per_scope=1)
    promises = next(g for g in out["groups"] if g["scope"] == "promises")
    assert len(promises["hits"]) == 1
    assert promises["hits"][0]["rank"] == promises["hits"][0]["score"]
    assert [g["scope"] for g in out["groups"]] == ["promises", "email"]
    assert all(len(g["hits"]) == 1 for g in out["groups"])


def test_a_scope_the_index_refuses_is_skipped():
    s = mail_space()
    s.search_fails = {"promises"}
    out = kernel(s).search("deck", scopes=["promises", "email", "calendar"])
    assert [g["scope"] for g in out["groups"]] == ["email"]
    assert out["skipped"] == ["promises", "calendar"]


def test_the_person_view_joins_columns_promises_and_mail():
    s = mail_space()
    s.add_mailbox("mb2", "work@example.com")
    s.add_mail("mb2", "w1", subject="Work deck", **{"from": "ada@work.org", "to": ME},
               participants=["ada@work.org", "work@example.com"],
               internalDate=(NOW - 2 * DAY) * 1000)
    s.add_object("ada", "profile", ["person", "brain_person"], name="Ada Lovelace",
                 person={"email": "ada@lab.org"},
                 brain_person={"emails": ["ada@lab.org", "ada@work.org"],
                               "aliases": ["Ada L."], "mailCount": 3,
                               "lastContactAt": {"$date": (NOW - DAY) * 1000}})
    out = kernel(s).person("ada", limit=5)
    assert out["person"] == {"id": "ada", "name": "Ada Lovelace",
                             "emails": ["ada@lab.org", "ada@work.org"], "aliases": ["Ada L."],
                             "lastContactAt": NOW - DAY, "mailCount": 3}
    assert [p["id"] for p in out["promises"]] == ["p1"]
    assert out["promises"][0]["link"].endswith("/email_messages/new")
    assert [m["subject"] for m in out["mail"]] == ["New deck", "Work deck", "Old deck"]


def test_an_excluded_or_unknown_person_raises():
    s = mail_space()
    s.add_object("eve", "profile", ["person", "brain_person"], person={"email": "eve@spy.org"},
                 brain_person={"emails": ["eve@spy.org"]})
    s.set_config("exclusions", {**EXCLUSIONS, "personIds": ["eve"]})
    with pytest.raises(Exception) as err:
        kernel(s).person("eve")
    assert type(err.value).__name__ == "person_excluded"
    s.set_config("exclusions", {**EXCLUSIONS, "domains": ["spy.org"]})
    with pytest.raises(Exception) as err:
        kernel(s).person("eve")
    assert type(err.value).__name__ == "person_excluded"
    with pytest.raises(Exception) as err:
        kernel(s).person("nobody")
    assert type(err.value).__name__ == "unknown_person"


def test_a_brain_that_is_not_set_up_refuses():
    s = mail_space()
    with pytest.raises(Exception, match="not set up") as err:
        kernel(s, status={"state": "provisioning"}).search("deck")
    assert type(err.value).__name__ == "not_set_up"
    with pytest.raises(Exception) as err:
        kernel(s, status=None).person("ada")
    assert type(err.value).__name__ == "not_set_up"


def test_an_unreadable_exclusion_list_fails_closed():
    s = mail_space()
    s.set_config("exclusions", {"version": 9})
    with pytest.raises(Exception) as err:
        kernel(s).search("deck")
    assert type(err.value).__name__ == "exclusions_unreadable"


def test_a_person_before_the_first_mail_sync_has_no_mail():
    s = FakeSpace()                      # no mailbox type: no sync has run
    s.set_config("exclusions", dict(EXCLUSIONS))
    s.add_object("ada", "profile", ["person", "brain_person"], name="Ada",
                 person={"email": "ada@lab.org"}, brain_person={"emails": ["ada@lab.org"]})
    out = kernel(s).person("ada")
    assert out["person"]["emails"] == ["ada@lab.org"] and out["mail"] == []


def test_mail_under_a_blocklisted_label_is_never_recalled():
    s = mail_space()
    s.records[("mb1", "email_messages")]["new"]["labelIds"] = ["INBOX", "Label_Medical"]
    s.set_config("blocklist", {"senders": [], "domains": [], "labels": ["Label_Medical"]})
    s.add_object("ada", "profile", ["person", "brain_person"], name="Ada",
                 person={"email": "ada@lab.org"}, brain_person={"emails": ["ada@lab.org"]})
    out = kernel(s).search("deck", scopes=["email"])
    assert [h["recordId"] for h in out["groups"][0]["hits"]] == ["secret", "old"]
    assert [m["link"].rsplit("/", 1)[-1] for m in kernel(s).person("ada")["mail"]] == ["old"]


def test_a_person_kept_out_by_address_is_never_found_as_an_object():
    s = mail_space()
    s.add_object("eve", "profile", ["person", "brain_person"], name="Eve",
                 person={"email": "eve@spy.org"}, brain_person={"emails": ["eve@spy.org"]})
    s.add_object("page1", "page", name="Deck notes")
    for excl, block in (({**EXCLUSIONS, "emails": ["eve@spy.org"]}, None),
                        ({**EXCLUSIONS, "domains": ["spy.org"]}, None),
                        (dict(EXCLUSIONS), {"senders": [], "domains": ["spy.org"],
                                            "labels": []})):
        s.set_config("exclusions", excl)
        s.set_config("blocklist", block or {"senders": [], "domains": [], "labels": []})
        basic = kernel(s).search("deck", scopes=["basic"])["groups"][0]["hits"]
        assert [h["objectId"] for h in basic] == ["page1"], (excl, block)
