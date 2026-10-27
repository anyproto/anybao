"""programs/brainPeople@v1 — the people spine from synced mail, under the
REAL guest kernel. Pinned: one person per correspondent, minted as a catalog
profile filed under person + brain_person; the mailbox's own address, noise,
blocklisted and excluded addresses never become people; a person found by
person.email is filed under brain_person instead of duplicated; counts,
names and last contact accumulate across runs; the cursor resumes; a capped
run leaves the rest for the next one; an unreadable exclusion list fails
closed; promises get their personId back-filled."""

import pytest
from brain_fakes import HUB, FakeSpace, job_args
from kernelenv import load_kernel

ME = "me@example.com"
EXCLUSIONS = {"version": 1, "personIds": [], "emails": [], "domains": []}


def space_with_mail():
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    s.seed_lists()
    s.add_mail("mb1", "m1", **{"from": "Ada Lovelace <ada@lab.org>", "to": ME,
                               "cc": "Charles <charles@lab.org>", "internalDate": 1_000_000})
    s.add_mail("mb1", "m2", **{"from": ME, "to": "ada@lab.org, noreply@github.com",
                               "internalDate": 2_000_000})
    return s


# a day after the fixtures' mail, so all of it is inside the first run's window
FIXTURE_NOW = 86_400.0
DAY_MS = 86_400_000


def run(space, args=None, now=FIXTURE_NOW):
    def eff(name, payload):
        if name == "time.now":
            return {"epoch": now}
        raise AssertionError(f"unexpected effect {name}")

    mod = load_kernel(effect=eff, any_client=space).use("brainPeople@v1")
    return mod, mod.main(args if args is not None else job_args(space))


def people(space):
    return {r["person"]["email"]: r for r in space.objects.values()
            if r["any"]["type"] == "profile"}


def test_one_person_per_correspondent_and_never_myself_or_noise():
    s = space_with_mail()
    _, out = run(s)
    ps = people(s)
    assert sorted(ps) == ["ada@lab.org", "charles@lab.org"]
    ada = ps["ada@lab.org"]
    assert ada["any"]["collections"] == ["person", "brain_person"]
    assert ada["any"]["name"] == "Ada Lovelace"
    assert ada["brain_person"]["emails"] == ["ada@lab.org"]
    assert ada["brain_person"]["mailCount"] == 2
    assert ada["brain_person"]["lastContactAt"] == {"$date": 2_000_000}
    assert ada["brain_person"]["firstSeenAt"] == {"$date": 1_000_000}
    assert out == {"mailboxes": 1, "messages": 2, "minted": 2, "updated": 0,
                   "promisesLinked": 0, "massSkipped": 0}
    assert s.config("people.cursor") == {"mail": {"mb1": 2_000_000}}


def test_a_second_run_reads_only_new_mail_and_accumulates():
    s = space_with_mail()
    run(s)
    s.add_mail("mb1", "m3", **{"from": "Ada L. <ada@lab.org>", "to": ME,
                               "internalDate": 3_000_000})
    _, out = run(s)
    ada = people(s)["ada@lab.org"]
    assert out["messages"] == 1 and out["minted"] == 0 and out["updated"] == 1
    assert ada["brain_person"]["mailCount"] == 3
    assert ada["brain_person"]["aliases"] == ["Ada Lovelace", "Ada L."]
    assert ada["brain_person"]["lastContactAt"] == {"$date": 3_000_000}


def test_a_catalog_person_is_adopted_not_duplicated():
    s = space_with_mail()
    s.add_object("p1", "profile", ["person"], person={"email": "ada@lab.org"}, name="Ada")
    run(s)
    assert len([r for r in s.objects.values() if r["any"]["type"] == "profile"]) == 2
    ada = s.objects["p1"]
    assert "brain_person" in ada["any"]["collections"]
    assert ada["brain_person"]["emails"] == ["ada@lab.org"]


def test_excluded_and_blocklisted_addresses_never_become_people():
    s = space_with_mail()
    s.set_config("exclusions", {**EXCLUSIONS, "emails": ["charles@lab.org"]})
    s.set_config("blocklist", {"domains": [], "senders": [], "labels": []})
    run(s)
    assert sorted(people(s)) == ["ada@lab.org"]
    s2 = space_with_mail()
    s2.set_config("blocklist", {"domains": ["lab.org"], "senders": [], "labels": []})
    run(s2)
    assert people(s2) == {}


def test_an_excluded_person_id_is_left_alone():
    s = space_with_mail()
    s.add_object("p1", "profile", ["person", "brain_person"], person={"email": "ada@lab.org"},
                 brain_person={"emails": ["ada@lab.org"], "mailCount": 9})
    s.set_config("exclusions", {**EXCLUSIONS, "personIds": ["p1"]})
    run(s)
    assert s.objects["p1"]["brain_person"]["mailCount"] == 9


def test_an_unreadable_exclusion_list_fails_closed():
    s = space_with_mail()
    s.set_config("exclusions", {"version": 2})
    with pytest.raises(Exception, match="exclusion list version 2") as err:
        run(s)
    assert type(err.value).__name__ == "exclusions_unreadable"
    assert people(s) == {} and s.config("people.cursor") is None


def test_a_corrupt_cursor_raises():
    s = space_with_mail()
    s.set_config("people.cursor", {"mail": {"mb1": "yesterday"}})
    with pytest.raises(Exception) as err:
        run(s)
    assert type(err.value).__name__ == "cursor_corrupt"


def test_a_capped_run_resumes_where_it_stopped(monkeypatch):
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    s.seed_lists()
    for i in range(5):
        s.add_mail("mb1", f"m{i}", **{"from": f"p{i}@x.org", "to": ME,
                                      "internalDate": (i + 1) * 1000})
    mod, _ = None, None

    def eff(name, payload):
        if name == "time.now":
            return {"epoch": 1.0}
        raise AssertionError(name)

    mod = load_kernel(effect=eff, any_client=s).use("brainPeople@v1")
    mod.MAX_NEW = 2
    out = mod.main(job_args(s))
    assert out["minted"] == 2 and s.config("people.cursor") == {"mail": {"mb1": 2000}}
    out = mod.main(job_args(s))
    assert out["minted"] == 2 and s.config("people.cursor") == {"mail": {"mb1": 4000}}


def test_promises_get_their_person_back_filled():
    s = space_with_mail()
    s.records[(HUB, "hubtype_promises")] = {
        "pr1": {"id": "pr1", "personEmail": "ada@lab.org", "personId": None, "status": "open"},
        "pr2": {"id": "pr2", "personEmail": "nobody@else.org", "personId": None}}
    _, out = run(s)
    ada_id = people(s)["ada@lab.org"]["id"]
    assert s.store("promises")["pr1"]["personId"] == ada_id
    assert s.store("promises")["pr2"]["personId"] is None
    assert out["promisesLinked"] == 1


def test_unconfigured_args_raise():
    s = space_with_mail()
    with pytest.raises(Exception) as err:
        run(s, {"space": s.space})
    assert type(err.value).__name__ == "unconfigured"


def test_before_the_first_mail_sync_there_is_nothing_to_read():
    s = FakeSpace()                      # no mailbox type: no sync has run
    s.seed_lists()
    _, out = run(s)
    assert out == {"mailboxes": 0, "messages": 0, "minted": 0, "updated": 0,
                   "promisesLinked": 0, "massSkipped": 0}


def test_mail_under_a_blocklisted_label_makes_no_person_but_is_read_past():
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    s.seed_lists()
    s.set_config("blocklist", {"senders": [], "domains": [], "labels": ["Label_Medical"]})
    s.add_mail("mb1", "m1", **{"from": "Dr Who <who@clinic.org>", "to": ME},
               labelIds=["INBOX", "Label_Medical"], internalDate=1_000_000)
    s.add_mail("mb1", "m2", **{"from": "Ada <ada@lab.org>", "to": ME},
               labelIds=["INBOX"], internalDate=2_000_000)
    _, out = run(s)
    assert sorted(people(s)) == ["ada@lab.org"]
    assert out["messages"] == 2
    assert s.config("people.cursor") == {"mail": {"mb1": 2_000_000}}


def test_a_blocklist_field_that_is_not_a_list_fails_closed():
    s = space_with_mail()
    s.set_config("blocklist", {"senders": "ada@lab.org", "domains": [], "labels": []})
    with pytest.raises(Exception) as err:
        run(s)
    assert type(err.value).__name__ == "blocklist_unreadable"
    assert people(s) == {}


def test_a_person_past_the_first_thousand_is_merged_not_overwritten_or_duplicated():
    s = space_with_mail()
    for i in range(1000):
        s.add_object(f"p{i:04}", "profile", ["person", "brain_person"],
                     person={"email": f"x{i}@crowd.org"},
                     brain_person={"emails": [f"x{i}@crowd.org"]})
    s.add_object("ada", "profile", ["person", "brain_person"], name="Ada",
                 person={"email": "ada@lab.org"},
                 brain_person={"emails": ["ada@lab.org", "ada@home.net"], "aliases": ["Ada"],
                               "mailCount": 40})
    s.add_mail("mb1", "m3", **{"from": "Ada <ada@home.net>", "to": ME,
                               "internalDate": 3_000_000})
    run(s)
    ada = s.objects["ada"]["brain_person"]
    assert ada["emails"] == ["ada@lab.org", "ada@home.net"]
    assert ada["mailCount"] == 43 and ada["aliases"][0] == "Ada"
    assert "ada@home.net" not in people(s)
    assert [c for c in s.calls if c == ("add_to_collection", "ada", "brain_person")] == []


def test_a_mass_mailing_is_read_past_without_minting_anyone():
    # one message with more new addresses than a run may mint would stop the
    # cursor at it forever; it is a mailing list, not correspondents
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    s.seed_lists()
    cc = ", ".join(f"p{i}@crowd.org" for i in range(301))
    s.add_mail("mb1", "m1", **{"from": "News <news@lab.org>", "to": ME, "cc": cc},
               internalDate=1_000_000)
    s.add_mail("mb1", "m2", **{"from": "Ada <ada@lab.org>", "to": ME}, internalDate=2_000_000)
    _, out = run(s)
    assert sorted(people(s)) == ["ada@lab.org"]
    assert out["messages"] == 2 and out["massSkipped"] == 1
    assert s.config("people.cursor") == {"mail": {"mb1": 2_000_000}}


def test_a_promise_to_someone_past_the_preload_is_linked():
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    s.seed_lists()
    for i in range(1000):
        s.add_object(f"p{i:04}", "profile", ["person", "brain_person"],
                     person={"email": f"x{i}@crowd.org"},
                     brain_person={"emails": [f"x{i}@crowd.org"]})
    s.add_object("ada", "profile", ["person", "brain_person"], name="Ada",
                 person={"email": "ada@lab.org"}, brain_person={"emails": ["ada@lab.org"]})
    s.records[(HUB, "hubtype_promises")] = {
        "p1": {"id": "p1", "text": "Send the deck", "personEmail": "ada@lab.org"}}
    _, out = run(s)
    assert out["promisesLinked"] == 1
    assert s.store("promises")["p1"]["personId"] == "ada"


@pytest.mark.parametrize("blocklist", [
    None,                                                       # the row is gone
    {"senders": None, "domains": [], "labels": []},             # a field set to null
    {"senders": [None], "domains": [], "labels": []},           # an entry that is not text
])
def test_a_blocklist_that_cannot_be_read_fails_closed(blocklist):
    s = space_with_mail()
    if blocklist is None:
        del s.records[(HUB, "hubtype_config")]["blocklist"]
    else:
        s.set_config("blocklist", blocklist)
    with pytest.raises(Exception) as err:
        run(s)
    assert type(err.value).__name__ == "blocklist_unreadable"
    assert people(s) == {}


def test_blocklist_entries_are_matched_trimmed_and_in_any_case():
    s = space_with_mail()
    s.set_config("blocklist", {"senders": [" Ada@Lab.org "], "domains": [], "labels": []})
    run(s)
    assert sorted(people(s)) == ["charles@lab.org"]


def test_a_first_run_starts_thirty_days_back_not_at_the_oldest_mail():
    # a mailbox can hold years of mail; the people job reads the last month
    # first rather than walking forward from its first message
    now = 1_800_000_000.0
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    s.seed_lists()
    s.add_mail("mb1", "old", **{"from": "Old Friend <old@past.org>", "to": ME},
               internalDate=now * 1000 - 40 * DAY_MS)
    s.add_mail("mb1", "new", **{"from": "Ada <ada@lab.org>", "to": ME},
               internalDate=now * 1000 - 10 * DAY_MS)
    _, out = run(s, now=now)
    assert sorted(people(s)) == ["ada@lab.org"] and out["messages"] == 1
    assert s.config("people.cursor") == {"mail": {"mb1": now * 1000 - 10 * DAY_MS}}
