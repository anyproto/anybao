"""memory.* effects + the judge-as-effect wiring (ADR-007 §2) — the
nested llm.chat judge call records in the same trace."""

import pytest
from anybao.anyclient import AnyClient
from anybao.memory import llm_judge, register_memory_effects
from anybao.recall import Recall
from anyrt import trace as tr
from anyrt.effects import Broker, Registry, effect

HIT = {"scope": "agent", "objectId": "brain1", "dataset": "agent_memory_items",
       "recordId": "m1", "score": 0.9}
EXISTING = {"id": "m1", "category": "preference", "context": "likes tea",
            "accessCount": 0}


def fake_any(capture, *, hits=(), items=()):
    def send(method, path, body):
        capture.append((method, path, body))
        if path.endswith("/search"):
            return 200, {"hits": list(hits), "mode": "hybrid", "vectorStatus": "used"}
        if path.endswith("/query"):
            ids = body.get("filter", {}).get("id", {}).get("$in", [])
            return 200, {"records": [r for r in items if r["id"] in ids]}
        if path.endswith("/agent/memory") and method == "POST":
            return 200, {"versionId": "v", "changeId": "c", "recordIds": ["new1"]}
        if "/agent/memory/" in path:
            return 200, {"versionId": "v", "changeId": "c", "recordIds": ["m1"]}
        return 404, {"error": {"code": "unknown", "message": path}}
    return send


def broker_with(judge_text, capture, **fake_kw):
    reg = Registry()
    client = AnyClient(fake_any(capture, **fake_kw))
    register_memory_effects(reg, client, space="s1")

    @effect("llm.chat", kind="read", registry=reg)
    def llm_chat(ctx, messages, system="", tier="codegen", tools=None):
        return {"parts": [{"type": "text", "text": judge_text}], "stop": "done",
                "usage": {"in": 1, "out": 1}}

    w = tr.TraceWriter(run={"id": "r"})
    return Broker(reg, w), w


def test_effects_registered_as_mutate_with_memory_cap():
    reg = Registry()
    register_memory_effects(reg, AnyClient(lambda m, p, b: (200, {})), space="s1")
    for name in ("memory.add", "memory.evolve", "memory.delete",
                 "memory.save_with_dedup", "memory.bump_access"):
        assert reg.get(name).kind == "mutate" and reg.get(name).cap == "memory.write"


def test_bump_access_effect_increments_from_current():
    cap = []
    b, _ = broker_with("unused", cap)
    out = b.call("memory.bump_access", {"item_id": "m1", "current_count": 4})
    assert out == {"itemId": "m1", "accessCount": 5}
    patch = [b_ for _, p, b_ in cap if p.endswith("/agent/memory/m1")]
    assert patch == [{"accessCount": 5}]


def test_save_with_dedup_merge_records_nested_judge_call():
    cap = []
    b, w = broker_with('{"action": "merge", "mergedInto": "m1"}', cap,
                       hits=[HIT], items=[EXISTING])
    out = b.call("memory.save_with_dedup", {"candidate": {
        "category": "preference", "context": "likes green tea"}})
    assert out == {"deduplicated": True, "mergedInto": "m1", "action": "merge"}
    effects = [r["effect"] for r in w.records if r["kind"] == "effect"]
    assert effects == ["llm.chat", "memory.save_with_dedup"]  # nested judge traced
    # merge evolved the existing item's mutable fields
    patch = [b_ for _, p, b_ in cap if p.endswith("/agent/memory/m1")]
    assert patch and patch[0]["context"] == "likes green tea"


def test_save_with_dedup_no_candidates_creates_without_judge():
    cap = []
    b, w = broker_with("SHOULD NOT BE CALLED", cap, hits=[], items=[])
    out = b.call("memory.save_with_dedup", {"candidate": {
        "category": "fact", "context": "sky is blue"}})
    assert out == {"itemId": "new1", "action": "create"}
    assert "llm.chat" not in [r["effect"] for r in w.records if r["kind"] == "effect"]


def test_judge_parses_json_embedded_in_prose_and_rejects_bad_action():
    calls = []

    def llm(payload):
        calls.append(payload)
        return {"parts": [{"type": "text",
                           "text": 'Verdict: {"action": "supersede", "mergedInto": "m1"}'}],
                "stop": "done", "usage": {}}

    recall = Recall(AnyClient(fake_any([], items=[EXISTING])), "s1")
    judge = llm_judge(llm, recall)
    v = judge({"category": "preference", "context": "likes coffee now"}, [HIT])
    assert v == {"action": "supersede", "mergedInto": "m1"}
    assert calls[0]["tier"] == "classify" and calls[0]["tools"] == []

    bad = llm_judge(lambda p: {"parts": [{"type": "text",
                                          "text": '{"action": "destroy"}'}],
                               "stop": "done", "usage": {}}, recall)
    with pytest.raises(ValueError, match="unknown action"):
        bad({"category": "x", "context": "y"}, [HIT])
