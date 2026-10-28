"""programs/brainDrafter@v1 — one reply draft and its pending approval
(ADR-036 §6), under the REAL guest kernel and the REAL llm@v1 (the model is
faked at its http.post). Pinned: nothing is read without consent; no tier
row → unpinned before any model call; the provenance check runs in plain
code before the model and refuses a recipient outside the source message
(mutation-checked: without it the outsider gets a draft); a thread message
kept out now or refused on arrival is never read; the draft, the approval
whose id is the hash of the draft's bytes, and the card's pointers are
written; the hash matches the pinned vector; a slow run is draft_timeout
after the writes; a provider failure on every call — LlmError or
EffectError — raises provider_failed and writes nothing."""

import hashlib
import json

import pytest
from brain_fakes import DATASETS, HUB, FakeSpace, job_args
from connectorenv import PROGRAMS_DIR, _agent_repo_source, kernelenv

NOW = 1_800_000_000
ME = "me@example.com"
ADA = "Ada Lovelace <ada@lab.org>"
TIER = {"provider": "openai-compat", "backend": "anyai",
        "base_url": "http://127.0.0.1:9/v1", "model": "codex/gpt-5.5", "api_key_ref": None}
# the pinned approval-hash vector (ADR-036 §7) — any-ui's hashApproval pins the same
VECTOR = ("mbx1", ["B@x.io", "a@x.io"], "text/plain", "Hi\n")
VECTOR_HASH = "fc45e7a45327b8d60fb22e052cbcc8c211d90c36750d610eb120c91035fc1bf3"
CARD = "c" * 64
REPLY = "Hi Ada,\n\nThanks — Thursday works for me.\n\nBest,\nMe"


class URLError(Exception):
    """What the host's http.post raises on a transport failure."""


class Model:
    """The provider behind the drafter's tier: canned replies, recorded requests.
    An int reply is that HTTP status; an Exception is raised (transport)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def answer(self, payload):
        self.requests.append(payload["json"])
        reply = self.replies.pop(0) if self.replies else REPLY
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, int):
            return {"status": reply, "headers": {}, "body": "provider says no"}
        return {"status": 200, "headers": {}, "body": json.dumps({
            "choices": [{"message": {"role": "assistant", "content": reply},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}})}

    def prompt(self, i=0):
        return self.requests[i]["messages"][-1]["content"]

    def system(self, i=0):
        return self.requests[i]["messages"][0]["content"]


def brain():
    s = FakeSpace()
    s.add_mailbox("mb1", ME)
    s.seed_lists()
    s.add_object("ada", "profile", ["person", "brain_person"], name="Ada Lovelace",
                 person={"email": "ada@lab.org"}, brain_person={"emails": ["ada@lab.org"]})
    s.add_mail("mb1", "m0", **{"from": f"Me <{ME}>", "to": ADA}, subject="Meeting",
               threadId="t1", body="Shall we meet next week?", date="Mon",
               participants=[ME, "ada@lab.org"], internalDate=(NOW - 7200) * 1000)
    s.add_mail("mb1", "m1", **{"from": ADA, "to": ME}, subject="Re: Meeting", threadId="t1",
               body="Thursday at 10 works for me.\n> Shall we meet next week?", date="Tue",
               participants=["ada@lab.org", ME], internalDate=(NOW - 3600) * 1000)
    s.records.setdefault((HUB, DATASETS["cards"]), {})[CARD] = {
        "id": CARD, "kind": "reply", "title": "Re: Meeting", "status": "open",
        "reason": "addressed to you, no reply yet", "subjectKey": "t1",
        "sourceUri": "any://o/bao-space/mb1/email_messages/m1", "personIds": ["ada"],
        "draftAttempts": 1}
    s.skills["brain-voice-email"] = "VOICE: greet by first name, sign off with Best."
    return s


def draft_args(space, **over):
    a = {**job_args(space), "tier": "brain.drafter", "cardId": CARD, "signalId": "s1",
         "messageId": "m1", "mailboxId": "mb1", "threadId": "t1",
         "recipients": ["ada@lab.org"], "provenance": "thread"}
    a.update(over)
    return a


def kernel(space, model, *, consent=True, tier=TIER, clock=None, source=None, on=True):
    clock = list(clock or [])
    config = {"llm.tier.brain.drafter": tier,
              "brain.space": {"spaceId": "bao-space" if on else None, "requestedAt": "t"},
              "brain.status": {"state": "ready", "hub": {"objectId": HUB,
                                                         "datasets": dict(DATASETS)}}}
    if consent is not None:
        config["brain.model"] = {"allowed": consent, "requestedAt": "t", "by": "chat"}

    def eff(name, payload):
        if name == "time.now":
            return {"epoch": float(clock.pop(0) if clock else NOW)}
        if name == "sleep":
            return {}
        if name == "config.get":
            if config.get(payload["key"]) is None:
                raise KeyError(f"no config value for {payload['key']!r}")
            return {"value": config[payload["key"]]}
        if name == "http.post":
            return model.answer(payload)
        raise AssertionError(f"unexpected effect {name}")

    def module_source(spec):
        if source is not None and spec == "brainDrafter@v1":
            return {"source": source, "marker": "mutant"}
        return _agent_repo_source(spec)

    app = kernelenv.load_kernel(effect=eff, any_client=space, programs_dir=PROGRAMS_DIR,
                                module_source=module_source)
    return app.use("brainDrafter@v1")


def raised(fn, *a):
    with pytest.raises(Exception) as err:
        fn(*a)
    return type(err.value).__name__, str(err.value)


def nothing_written(s):
    return s.store("drafts") == {} and s.store("approvals") == {} and \
        "draftId" not in s.store("cards")[CARD]


# --- the hash ---------------------------------------------------------------------

def test_the_approval_hash_matches_the_pinned_vector():
    mod = kernel(brain(), Model())
    assert mod.hash_approval(*VECTOR) == VECTOR_HASH
    joined = "mbx1\na@x.io,b@x.io\ntext/plain\nHi\n"
    assert hashlib.sha256(joined.encode()).hexdigest() == VECTOR_HASH
    # any change of byte, order of fields or recipient case is a different id only
    # where it should be: recipients are lowercased and sorted, nothing else moves
    assert mod.hash_approval("mbx1", ["a@x.io", "b@x.io"], "text/plain", "Hi\n") == VECTOR_HASH
    assert mod.hash_approval("mbx1", ["B@x.io", "a@x.io"], "text/plain", "Hi") != VECTOR_HASH
    assert mod.hash_approval("mbx2", ["B@x.io", "a@x.io"], "text/plain", "Hi\n") != VECTOR_HASH


# --- the gates before the model -----------------------------------------------------

def test_nothing_is_read_without_consent():
    for consent in (False, None):
        s, model = brain(), Model()
        n = len(s.calls)
        assert kernel(s, model, consent=consent).main(draft_args(s)) == {"skipped": "consent"}
        assert s.calls[n:] == [] and model.requests == [] and nothing_written(s)


def test_no_tier_row_is_unpinned_before_any_model_call():
    s, model = brain(), Model()
    kind, _ = raised(kernel(s, model, tier=None).main, draft_args(s))
    assert kind == "unpinned" and model.requests == [] and nothing_written(s)


def test_a_recipient_outside_the_source_message_is_refused_before_the_model():
    s, model = brain(), Model()
    kind, msg = raised(kernel(s, model).main, draft_args(s, recipients=["eve@spy.org"]))
    assert kind == "recipient_provenance" and "eve@spy.org" in msg
    assert model.requests == [] and nothing_written(s)


def test_the_mailbox_itself_and_another_thread_are_refused_too():
    for over in ({"recipients": [ME]}, {"threadId": "t-other"}, {"recipients": []}):
        s, model = brain(), Model()
        kind, _ = raised(kernel(s, model).main, draft_args(s, **over))
        assert kind == "recipient_provenance" and model.requests == [] and nothing_written(s)


def test_mutation_without_the_provenance_check_the_outsider_gets_a_draft():
    # the guard, not anything after it, is what keeps an outsider out
    src = kernelenv.local_source("brainDrafter@v1", PROGRAMS_DIR)
    call = '    check_provenance(message, recipients, own, args.get("threadId"))\n'
    assert src.count(call) == 1
    mutant = src.replace(call, "")
    s, model = brain(), Model()
    out = kernel(s, model, source=mutant).main(draft_args(s, recipients=["eve@spy.org"]))
    (draft,) = s.store("drafts").values()
    assert draft["recipients"] == ["eve@spy.org"] and out["draftId"] == draft["id"]


def test_a_source_kept_out_now_is_skipped_and_never_read():
    s, model = brain(), Model()
    s.set_config("exclusions", {"version": 1, "personIds": [], "emails": [],
                                "domains": ["lab.org"]})
    assert kernel(s, model).main(draft_args(s)) == {"skipped": "kept_out"}
    assert model.requests == [] and nothing_written(s)


def test_a_drafted_closed_or_missing_card_and_an_off_brain_are_skipped():
    s, model = brain(), Model()
    assert kernel(s, model, on=False).main(draft_args(s)) == {"skipped": "off"}
    s.store("cards")[CARD]["draftId"] = "d-old"
    assert kernel(s, model).main(draft_args(s)) == {"skipped": "drafted"}
    s.store("cards")[CARD].pop("draftId")
    s.store("cards")[CARD]["status"] = "dismissed"
    assert kernel(s, model).main(draft_args(s)) == {"skipped": "closed"}
    assert kernel(s, model).main(draft_args(s, cardId="d" * 64)) == {"skipped": "no card"}
    assert model.requests == []


# --- the draft ---------------------------------------------------------------------

def test_a_draft_its_pending_approval_and_the_card_pointers_are_written():
    s, model = brain(), Model()
    out = kernel(s, model).main(draft_args(s))
    (draft,) = s.store("drafts").values()
    body = "Hi Ada,\n\nThanks — Thursday works for me.\n\nBest,\nMe\n"
    assert draft["body"] == body and draft["format"] == "text/plain"
    assert draft["cardId"] == CARD and draft["sender"] == "mb1" and draft["senderAddress"] == ME
    assert draft["recipients"] == ["ada@lab.org"] and draft["recipientProvenance"] == "thread"
    assert draft["subject"] == "Re: Meeting" and draft["inReplyTo"] == "m1"
    assert draft["threadId"] == "t1" and draft["origin"] == "drafter"
    assert draft["model"] == "codex/gpt-5.5" and draft["tier"] == "brain.drafter"
    (approval,) = s.store("approvals").values()
    aid = kernel(s, model).hash_approval("mb1", ["ada@lab.org"], "text/plain", body)
    assert approval["id"] == aid == out["approvalId"]
    assert approval["status"] == "pending" and approval["draftId"] == draft["id"]
    assert approval["bodyHash"] == hashlib.sha256(body.encode()).hexdigest()
    assert approval["reason"] == "addressed to you, no reply yet"
    assert approval["sender"] == "mb1" and approval["recipients"] == ["ada@lab.org"]
    card = s.store("cards")[CARD]
    assert card["draftId"] == draft["id"] and card["approvalId"] == aid
    assert card["draftAttempts"] == 1                       # the counter is the heartbeat's
    (standup,) = s.store("standups").values()
    assert standup["job"] == "drafter" and standup["counts"]["modelCalls"] == 1
    assert out["calls"] == 1 and len(model.requests) == 1


def test_one_call_no_tools_the_voice_and_the_thread_in_order():
    s, model = brain(), Model()
    kernel(s, model).main(draft_args(s))
    req = model.requests[0]
    assert "tools" not in req
    assert 1200 in (req.get("max_tokens"), req.get("max_completion_tokens"))
    assert "VOICE: greet by first name" in model.system()
    prompt = model.prompt()
    assert prompt.index("Shall we meet next week?") < prompt.index("Thursday at 10")
    assert "> Shall we meet" not in prompt              # quoted chains stay out
    assert "Reply to: ada@lab.org" in prompt and "## Ada Lovelace" in prompt


def test_thread_mail_kept_out_or_refused_on_arrival_is_never_read():
    s, model = brain(), Model()
    s.add_mail("mb1", "m-bob", **{"from": "Bob <bob@x.io>", "to": ME, "cc": "ada@lab.org"},
               threadId="t1", body="BOB SECRET", participants=["bob@x.io", ME, "ada@lab.org"],
               internalDate=(NOW - 5000) * 1000)
    s.add_mail("mb1", "m-old", **{"from": "Cy <cy@y.io>", "to": ME}, threadId="t1",
               body="CY WHILE EXCLUDED", participants=["cy@y.io", ME],
               internalDate=(NOW - 6000) * 1000)
    s.add_mail("mb1", "m-later", **{"from": ADA, "to": ME}, threadId="t1",
               body="AFTER THE SOURCE", participants=["ada@lab.org", ME],
               internalDate=(NOW - 60) * 1000)
    s.set_config("exclusions", {"version": 1, "personIds": [], "emails": ["bob@x.io"],
                                "domains": []})
    # cy was excluded when m-old arrived (the heartbeat refused it) and is not now
    s.records.setdefault((HUB, DATASETS["signals"]), {})["sig-cy"] = {
        "id": "sig-cy", "kind": "new_mail", "decision": "refused", "tick": NOW - 6000,
        "subject": "any://o/bao-space/mb1/email_messages/m-old", "reason": "kept out"}
    kernel(s, model).main(draft_args(s))
    prompt = model.prompt()
    assert "BOB SECRET" not in prompt and "CY WHILE EXCLUDED" not in prompt
    assert "AFTER THE SOURCE" not in prompt and "Thursday at 10" in prompt


def test_a_missing_voice_skill_falls_back_to_the_built_in_voice():
    s, model = brain(), Model()
    s.skills.clear()
    kernel(s, model).main(draft_args(s))
    assert "greeting by first name" in model.system()


def test_a_slow_run_is_draft_timeout_after_the_draft_is_written():
    s, model = brain(), Model()
    kind, _ = raised(kernel(s, model, clock=[NOW, NOW + 100]).main, draft_args(s))
    assert kind == "draft_timeout"
    assert len(s.store("drafts")) == 1 and len(s.store("approvals")) == 1
    assert s.store("cards")[CARD]["draftId"]


# --- provider failures --------------------------------------------------------------

def test_two_llm_errors_raise_provider_failed_and_write_nothing():
    s, model = brain(), Model(400, 400)
    kind, _ = raised(kernel(s, model).main, draft_args(s))
    assert kind == "provider_failed" and len(model.requests) == 2 and nothing_written(s)


def test_effect_errors_count_as_provider_failures_too():
    # a transport failure crosses as EffectError (llm@v1 retries it twice, then raises)
    s = brain()
    model = Model(*[URLError("connection refused")] * 6)
    kind, msg = raised(kernel(s, model).main, draft_args(s))
    assert kind == "provider_failed" and "EffectError" in msg and nothing_written(s)


def test_one_failure_then_a_reply_writes_the_draft_counting_two_calls():
    s, model = brain(), Model(400, REPLY)
    out = kernel(s, model).main(draft_args(s))
    assert out["calls"] == 2 and len(s.store("drafts")) == 1
    (standup,) = s.store("standups").values()
    assert standup["counts"]["modelCalls"] == 2


def test_empty_replies_are_not_a_draft():
    s, model = brain(), Model("", "```\n```")
    kind, _ = raised(kernel(s, model).main, draft_args(s))
    assert kind == "empty_draft" and nothing_written(s)


def test_clean_reply_drops_a_fence_and_a_subject_and_ends_in_one_newline():
    mod = kernel(brain(), Model())
    assert mod.clean_reply("```text\nSubject: Re: x\n\nHello  \nthere\n```") == "Hello\nthere\n"
    assert mod.clean_reply("  ") == ""
    assert mod.reply_subject("AW: Termin") == "AW: Termin"
    assert mod.reply_subject("Termin") == "Re: Termin"
