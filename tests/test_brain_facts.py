"""programs/brainFacts@v1 — facts about people from their mail, under the REAL
guest kernel and the REAL llm@v1 (the model is faked at its http.post, the
way the local AI proxy answers). Pinned: nothing is read without consent;
the exclusion list fails closed before any mail; an excluded person and any
message with an excluded participant stay out; grounding keeps only
verbatim quotes of tagged messages with known kinds and enough confidence;
ids make a re-run write nothing and a new source append; a single-valued
kind supersedes an inferred fact but never a stated one; history.consent
lands proposed; the tier must be configured; a bad reply is retried once;
two provider failures stop the run with the cursor kept."""

import json

import pytest
from brain_fakes import HUB, FakeAnyError, FakeSpace, job_args
from kernelenv import load_kernel

NOW = 1_800_000_000
DAY = 86400
ME = "me@example.com"
EXCLUSIONS = {"version": 1, "personIds": [], "emails": [], "domains": []}
TIER = {"provider": "openai-compat", "backend": "anyai",
        "base_url": "http://127.0.0.1:9/v1", "model": "codex/gpt-5.5", "api_key_ref": None}
ADA_MAIL = ("Hi! I moved to Lisbon last month, so mornings work best for me.\n"
            "By the way, I'm now Head of Research at Lab Co.")


def facts_args(space):
    args = job_args(space)
    args["datasets"]["facts"] = "hubtype_facts"
    args["tier"] = "brain.extractor"
    return args


def space_with(body=ADA_MAIL, *, sender="Ada Lovelace <ada@lab.org>", to=ME,
               participants=("ada@lab.org", ME), mid="m1", at=NOW - DAY):
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    s.seed_lists()
    s.add_object("ada", "profile", ["person", "brain_person"], name="Ada Lovelace",
                 person={"email": "ada@lab.org"},
                 brain_person={"emails": ["ada@lab.org"], "lastContactAt": {"$date": at * 1000}})
    s.add_mail("mb1", mid, **{"from": sender, "to": to}, subject="Hello", body=body,
               participants=list(participants), internalDate=at * 1000)
    return s


def item(kind, value, quote, ref="M1", confidence=0.7):
    return {"kind": kind, "value": value, "quote": quote, "ref": ref,
            "confidence": confidence}


LISBON = item("personal.location", "Lisbon", "I moved to Lisbon last month")
ROLE = item("work.role", "Head of Research",
            "By the way, I'm now Head of Research at Lab Co.")


class Model:
    """The provider behind the extractor's tier: canned replies, recorded prompts."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def answer(self, payload):
        self.prompts.append(payload["json"]["messages"])
        reply = self.replies.pop(0)
        if isinstance(reply, int):
            return {"status": reply, "headers": {}, "body": "provider says no"}
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return {"status": 200, "headers": {}, "body": json.dumps({
            "choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}})}


def run(space, model, *, consent=True, tier=TIER, args=None):
    def eff(name, payload):
        if name == "time.now":
            return {"epoch": float(NOW)}
        if name == "sleep":
            return {}
        if name == "config.get":
            key = payload["key"]
            if key == "brain.model" and consent is not None:
                return {"value": {"allowed": consent, "requestedAt": "t", "by": "any-ui"}}
            if key == "llm.tier.brain.extractor" and tier is not None:
                return {"value": tier}
            raise KeyError(f"no config value for {key!r}")
        if name == "http.post":
            return model.answer(payload)
        raise AssertionError(f"unexpected effect {name}")

    mod = load_kernel(effect=eff, any_client=space).use("brainFacts@v1")
    return mod, mod.main(args if args is not None else facts_args(space))


def mail_reads(space):
    return [c for c in space.calls if c[0] == "query" and c[2] == "email_messages"]


def test_nothing_is_read_or_asked_without_consent():
    s, model = space_with(), Model()
    for consent in (False, None):
        _, out = run(s, model, consent=consent)
        assert out == {"skipped": "consent"}
    assert model.prompts == [] and s.store("facts") == {}


def test_a_grounded_fact_is_written_with_its_quote_source_and_model():
    s, model = space_with(), Model([LISBON, ROLE])
    mod, out = run(s, model)
    facts = {f["kind"]: f for f in s.store("facts").values()}
    assert sorted(facts) == ["personal.location", "work.role"]
    lisbon = facts["personal.location"]
    assert lisbon["id"] == mod.fact_id("ada", "personal.location", "Lisbon")
    assert lisbon["quote"] == "I moved to Lisbon last month"
    assert lisbon["source"] == "any://o/bao-space/mb1/email_messages/m1"
    assert lisbon["status"] == "current" and lisbon["origin"] == "inferred"
    assert lisbon["model"] == "codex/gpt-5.5" and lisbon["confidence"] == 0.7
    assert lisbon["observedAt"] == {"$date": (NOW - DAY) * 1000}
    assert out["written"] == 2 and out["persons"] == 1
    prompt = model.prompts[0][-1]["content"]
    assert "[M1]" in prompt and "Lisbon" in prompt and "Ada Lovelace" in prompt


def test_grounding_drops_what_is_not_verbatim_known_tagged_or_confident():
    s = space_with()
    paraphrase = item("personal.location", "Lisbon", "She lives in Lisbon now")
    bad_kind = item("personal.shoe_size", "42", "I moved to Lisbon last month")
    bad_ref = item("personal.location", "Lisbon", "I moved to Lisbon last month", ref="M9")
    timid = item("personal.location", "Lisbon", "I moved to Lisbon last month",
                 confidence=0.5)
    no_quote = item("work.role", "Head of Research", "")
    _, out = run(s, Model([paraphrase, bad_kind, bad_ref, timid, no_quote]))
    assert s.store("facts") == {}
    assert out["dropped"] == {"notVerbatim": 1, "badKind": 1, "badSource": 1,
                              "lowConfidence": 1, "noQuote": 1}
    assert s.config("facts.stats")["dropped"]["notVerbatim"] == 1


def test_whitespace_differences_do_not_break_a_verbatim_quote():
    s = space_with(body="I moved to\n   Lisbon last month.")
    _, out = run(s, Model([LISBON]))
    assert out["written"] == 1


def test_confidence_is_capped():
    s = space_with()
    run(s, Model([{**LISBON, "confidence": 0.99}]))
    assert next(iter(s.store("facts").values()))["confidence"] == 0.8


def test_an_unreadable_exclusion_list_fails_before_any_mail():
    s = space_with()
    s.set_config("exclusions", {"version": 2})
    with pytest.raises(Exception) as err:
        run(s, Model([LISBON]))
    assert type(err.value).__name__ == "exclusions_unreadable"
    assert mail_reads(s) == []


def test_an_excluded_person_gets_no_facts_and_is_counted():
    s = space_with()
    s.set_config("exclusions", {**EXCLUSIONS, "personIds": ["ada"]})
    model = Model([LISBON])
    _, out = run(s, model)
    assert model.prompts == [] and s.store("facts") == {}
    assert out["excludedSkipped"] == 1


def test_a_message_with_an_excluded_participant_is_out_for_everyone():
    s = space_with(to=f"{ME}, eve@spy.org", participants=("ada@lab.org", ME, "eve@spy.org"))
    s.set_config("exclusions", {**EXCLUSIONS, "domains": ["spy.org"]})
    model = Model([LISBON])
    _, out = run(s, model)
    assert model.prompts == [] and out["excludedSkipped"] == 1


def test_a_message_carrying_a_person_excluded_by_id_is_out_for_everyone():
    s = space_with(to=f"{ME}, grace@navy.mil", participants=("ada@lab.org", ME))
    s.add_object("grace", "profile", ["person", "brain_person"], name="Grace Hopper",
                 brain_person={"emails": ["grace@navy.mil"]})
    s.set_config("exclusions", {**EXCLUSIONS, "personIds": ["grace"]})
    model = Model([LISBON])
    _, out = run(s, model)
    assert model.prompts == [] and s.store("facts") == {}
    assert out["excludedSkipped"] == 1


def test_excluded_persons_that_cannot_be_read_fail_closed():
    class Refusing(FakeSpace):
        def query_objects(self, space, normalize=True, filter=None, limit=None, **kw):
            if "id" in (filter or {}):
                raise FakeAnyError("503 unavailable")
            return super().query_objects(space, normalize, filter, limit, **kw)

    s = space_with()
    s.__class__ = Refusing
    s.set_config("exclusions", {**EXCLUSIONS, "personIds": ["grace"]})
    with pytest.raises(Exception) as err:
        run(s, Model([LISBON]))
    assert type(err.value).__name__ == "exclusions_unreadable"
    assert mail_reads(s) == []


def test_a_re_run_writes_nothing_and_a_new_source_is_appended():
    s = space_with()
    run(s, Model([LISBON]))
    s.set_config("facts.cursor", None)
    _, out = run(s, Model([LISBON]))
    assert out["written"] == 0 and out["moreSources"] == 0
    s.add_mail("mb1", "m2", **{"from": "ada@lab.org", "to": ME}, subject="Again",
               body="Yes, I moved to Lisbon last month!", participants=["ada@lab.org", ME],
               internalDate=(NOW - 1000) * 1000)
    s.set_config("facts.cursor", None)
    _, out = run(s, Model([item("personal.location", "lisbon.",
                                "I moved to Lisbon last month", ref="M1")]))
    (fact,) = s.store("facts").values()
    assert out["moreSources"] == 1
    assert fact["moreSources"][0]["source"].endswith("/email_messages/m2")


def test_a_single_valued_kind_supersedes_an_inferred_fact():
    s = space_with()
    run(s, Model([ROLE]))
    old_id = next(iter(s.store("facts")))
    s.add_mail("mb1", "m2", **{"from": "ada@lab.org", "to": ME}, subject="News",
               body="Big news: I am now CTO at Lab Co.", participants=["ada@lab.org", ME],
               internalDate=(NOW - 100) * 1000)
    s.objects["ada"]["brain_person"]["lastContactAt"] = {"$date": (NOW - 100) * 1000}
    _, out = run(s, Model([item("work.role", "CTO", "I am now CTO at Lab Co.")]))
    store = s.store("facts")
    new = next(f for f in store.values() if f["value"] == "CTO")
    assert new["supersedes"] == old_id and new["status"] == "current"
    assert store[old_id]["status"] == "superseded"
    assert store[old_id]["supersededBy"] == new["id"]
    assert out["superseded"] == 1


def test_a_stated_fact_is_never_superseded_and_the_conflict_is_a_signal():
    s = space_with()
    s.records[(HUB, "hubtype_facts")] = {"stated1": {
        "id": "stated1", "personId": "ada", "kind": "work.role", "value": "Advisor",
        "origin": "stated", "status": "current"}}
    _, out = run(s, Model([ROLE]))
    assert [f["value"] for f in s.store("facts").values()] == ["Advisor"]
    (signal,) = s.store("signals").values()
    assert signal["kind"] == "fact_conflict" and "stated 'Advisor'" in signal["reason"]
    assert out["conflicts"] == 1


def test_a_consent_fact_lands_proposed():
    s = space_with(body="Happy for you to introduce me to your investors.")
    run(s, Model([item("history.consent", "OK to introduce to investors",
                       "Happy for you to introduce me to your investors.")]))
    assert next(iter(s.store("facts").values()))["status"] == "proposed"


def test_no_tier_row_raises_unpinned():
    s = space_with()
    with pytest.raises(Exception) as err:
        run(s, Model(), tier=None)
    assert type(err.value).__name__ == "unpinned"


def test_an_unparsable_reply_is_retried_once_then_counted():
    s = space_with()
    _, out = run(s, Model("I could not find anything.", "still prose"))
    assert out["parseFailed"] == 1 and s.store("facts") == {}
    s2 = space_with()
    _, out = run(s2, Model("sorry", "```json\n" + json.dumps([LISBON]) + "\n```"))
    assert out["written"] == 1


def test_two_provider_failures_stop_the_run_and_keep_the_cursor():
    s = space_with()
    s.add_object("bob", "profile", ["person", "brain_person"], name="Bob",
                 brain_person={"emails": ["bob@x.org"],
                               "lastContactAt": {"$date": (NOW - 50) * 1000}})
    s.add_mail("mb1", "m2", **{"from": "bob@x.org", "to": ME}, subject="Hi", body="Hello there",
               participants=["bob@x.org", ME], internalDate=(NOW - 50) * 1000)
    with pytest.raises(Exception) as err:
        run(s, Model(400, 400))
    assert type(err.value).__name__ == "provider_failed"
    # neither person's mail was read through, so the watermark stays put
    assert s.config("facts.cursor") == {"watermark": NOW - 30 * DAY}


def test_the_watermark_passes_a_person_once_their_mail_is_read():
    s = space_with()
    run(s, Model([LISBON]))
    assert s.config("facts.cursor") == {"watermark": NOW - DAY}
    model = Model()
    _, out = run(s, model)
    assert model.prompts == [] and out["persons"] == 0


def test_the_fact_id_is_stable_across_spelling_and_space():
    mod, _ = run(space_with(), Model([]))
    assert mod.fact_id("ada", "personal.location", "  Lisbon. ") == \
        mod.fact_id("ada", "personal.location", "lisbon")
    # pinned: any-ui computes the same id for a stated fact
    assert mod.fact_id("person-1", "preference.name", "Ëmilie  Dû\tBois!") == \
        "1706df506564ca6856b8cdbc0782b407"


def test_unconfigured_without_a_facts_store():
    s = space_with()
    args = facts_args(s)
    del args["datasets"]["facts"]
    with pytest.raises(Exception) as err:
        run(s, Model(), args=args)
    assert type(err.value).__name__ == "unconfigured"


def test_before_the_first_mail_sync_nothing_is_read_or_asked():
    s, model = space_with(), Model()
    del s.objects["mb1"]                 # no mailbox type: no sync has run
    s.types.discard("mailbox")
    _, out = run(s, model)
    assert model.prompts == [] and out["written"] == 0


def test_mail_under_a_blocklisted_label_never_reaches_the_model():
    s, model = space_with(), Model()
    s.records[("mb1", "email_messages")]["m1"]["labelIds"] = ["INBOX", "Label_Medical"]
    s.set_config("blocklist", {"senders": [], "domains": [], "labels": ["Label_Medical"]})
    _, out = run(s, model)
    assert model.prompts == [] and out["excludedSkipped"] == 1
