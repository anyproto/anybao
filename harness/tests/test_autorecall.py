"""Auto-recall injection (ADR-007 §5) — offline: fake transport into a
real AnyClient, asserting framing, guards, caps, and the accessCount
bump."""

from anybao.anyclient import AnyClient
from anybao.autorecall import AutoRecall, AutoRecallPolicy, covered_by_boot
from anybao.loop import run_conversation
from anyrt import trace as tr
from anyrt.effects import Broker, Registry, effect
from anyrt.executor import FakeExecutor

MEM_HIT = {"scope": "agent", "objectId": "brain1", "dataset": "agent_memory_items",
           "recordId": "m1", "score": 0.033}
TURN_HIT = {"scope": "history", "objectId": "chat1", "dataset": "agent_turns",
            "recordId": "t1", "score": 0.032}
CHUNK_HIT = {"scope": "history", "objectId": "chat1", "dataset": "agent_chunks",
             "recordId": "c1", "score": 0.017}

MEM_REC = {"id": "m1", "category": "preference", "context": "prefers dark roast",
           "confidence": 8, "validFrom": 1751328000, "accessCount": 2}   # 2025-07-01
TURN_REC = {"id": "t1", "seq": 4, "userText": "let's plan the trip\nmore",
            "createdAt": 1751328000}
CHUNK_REC = {"id": "c1", "seq": 1, "level": 1, "fromSeq": 1, "toSeq": 3,
             "summary": "planned the trip", "periodStart": 1751328000}


def fake_any(capture, hits, records_by_dataset):
    def send(method, path, body):
        capture.append((method, path, body))
        if path.endswith("/search"):
            return 200, {"hits": hits, "mode": "hybrid", "vectorStatus": "used"}
        if path.endswith("/query"):
            recs = records_by_dataset.get(body["dataset"], [])
            ids = body.get("filter", {}).get("id", {}).get("$in", [])
            return 200, {"records": [r for r in recs if r["id"] in ids]}
        if "/agent/memory/" in path:  # evolve (the accessCount bump)
            return 200, {"versionId": "v", "changeId": "c", "recordIds": ["m1"]}
        return 404, {"error": {"code": "unknown", "message": path}}
    return send


def autorecall(capture, hits, recs=None, policy=None):
    client = AnyClient(fake_any(capture, hits, recs or {
        "agent_memory_items": [MEM_REC], "agent_turns": [TURN_REC],
        "agent_chunks": [CHUNK_REC]}))
    return AutoRecall(client, "s1", policy=policy)


def test_injects_tool_framed_pair_with_both_sections():
    cap = []
    msgs = autorecall(cap, [MEM_HIT, TURN_HIT, CHUNK_HIT]).messages_for("trip?")
    assert [m["role"] for m in msgs] == ["assistant", "user"]
    call, result = msgs[0]["parts"][0], msgs[1]["parts"][0]
    assert call["type"] == "tool_call" and call["name"] == "recall"
    assert call["args"]["query"] == "trip?"
    assert result["type"] == "tool_result" and result["call_id"] == call["id"]
    assert "Memories:" in result["content"] and "Related history:" in result["content"]
    assert "[preference] prefers dark roast (saved 2025-07-01, confidence 8)" \
        in result["content"]
    assert "[chunk #1 (L1), turns 1–3]" in result["content"]
    assert "“let's plan the trip” [turn #4]" in result["content"]


def test_generic_message_below_threshold_injects_nothing():
    cap = []
    weak = [{**MEM_HIT, "score": 0.008}, {**TURN_HIT, "score": 0.011}]  # deep-rank RRF noise
    assert autorecall(cap, weak).messages_for("hi") == []
    # threshold cut happens before any hydration round-trip
    assert all(not p.endswith("/query") for _, p, _ in cap)


def test_injected_memory_items_bump_access_count():
    cap = []
    autorecall(cap, [MEM_HIT]).messages_for("coffee?")
    bump = [(m, p, b) for m, p, b in cap if p.endswith("/agent/memory/m1")]
    assert len(bump) == 1
    assert bump[0][2] == {"accessCount": 3}  # 2 + 1


def test_deep_history_guard_skips_boot_covered_hits():
    cap = []
    ar = autorecall(cap, [TURN_HIT, CHUNK_HIT])
    # raw tail starts at seq 4 → turn #4 covered; chunk 1–3 is deeper, stays
    msgs = ar.messages_for("trip?", boot_min_seq=4)
    content = msgs[1]["parts"][0]["content"]
    assert "turn #4" not in content and "chunk #1" in content
    # chunk fully inside the tail is covered too
    assert covered_by_boot(CHUNK_REC, "agent_chunks", boot_min_seq=1)


def test_memory_cap_and_budget_respected():
    hits = [{**MEM_HIT, "recordId": f"m{i}"} for i in range(8)]
    recs = {"agent_memory_items": [
        {**MEM_REC, "id": f"m{i}", "context": f"fact {i}"} for i in range(8)]}
    cap = []
    msgs = autorecall(cap, hits, recs).messages_for("q?")
    content = msgs[1]["parts"][0]["content"]
    assert content.count("- [preference]") == 5  # max_memory
    tiny = AutoRecallPolicy(token_budget=15)
    cap2 = []
    msgs2 = autorecall(cap2, hits, recs, policy=tiny).messages_for("q?")
    assert msgs2[1]["parts"][0]["content"].count("- [preference]") == 1


def test_search_failure_fails_open():
    def send(method, path, body):
        raise OSError("network down")
    assert AutoRecall(AnyClient(send), "s1").messages_for("q") == []


def test_loop_places_boot_and_injection_around_user_message():
    reg = Registry()
    seen = []

    @effect("llm.chat", kind="read", registry=reg)
    def llm_chat(ctx, messages, system="", tier="codegen", tools=None):
        seen.append(list(messages))
        return {"parts": [{"type": "text", "text": "ok"}], "stop": "done",
                "usage": {"in": 1, "out": 1}}

    boot = [{"role": "user", "parts": [{"type": "text", "text": "[earlier context]"}]}]
    inject = [{"role": "assistant", "parts": [{"type": "tool_call", "id": "a0",
                                               "name": "recall", "args": {}}]},
              {"role": "user", "parts": [{"type": "tool_result", "call_id": "a0",
                                          "content": "Memories:", "is_error": False}]}]
    run_conversation("go", broker=Broker(reg, tr.TraceWriter(run={"id": "t"})),
                     executor=FakeExecutor([]), boot_messages=boot,
                     recall_inject=lambda q: inject)
    msgs = seen[0]
    assert msgs[0]["parts"][0]["text"] == "[earlier context]"
    assert msgs[1]["parts"][0]["text"] == "go"
    assert msgs[2]["parts"][0]["type"] == "tool_call"
    assert msgs[3]["parts"][0]["type"] == "tool_result"
