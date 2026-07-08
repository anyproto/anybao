"""Runner end-to-end, OFFLINE: real WasiEngine + scripted llm transport
+ fake anyclient. The composition test the integration suite runs live,
now runnable in CI — boot window, auto-recall injection, guest effect
calls, turn persistence, ROI log, and the trigger program path all in
one pass. Skips when bin/kernel.wasm is missing (repo convention)."""

import json
import tempfile
from pathlib import Path

import pytest
from anybao.anyclient import AnyClient
from anybao.config import Config, DictConfigStore
from anybao.runner import Runner
from anyrt.builtin_effects import DictResolver

KERNEL = Path(__file__).resolve().parents[2] / "bin" / "kernel.wasm"
PROGRAMS = Path(__file__).resolve().parents[2] / "programs"

pytestmark = pytest.mark.skipif(not KERNEL.exists(),
                                reason="bin/kernel.wasm missing — run `make kernel`")

MEM_REC = {"id": "m1", "category": "preference", "context": "prefers dark roast",
           "confidence": 8, "validFrom": 1751328000, "accessCount": 0}


class FakeAnySpace:
    """Enough of the any wire for a full conversation + program run."""

    def __init__(self, *, hits=(), turns=(), memory=(MEM_REC,)):
        self.hits = list(hits)
        self.datasets = {"agent_turns": list(turns), "agent_chunks": [],
                         "agent_memory_items": list(memory),
                         "agent_roi_injections": []}
        self.appended_turns = []
        self.created_chunks = []
        self.bumps = []
        self.modifies = []
        self.chat_posts = []

    def __call__(self, method, path, body):
        if path.endswith("/search"):
            return 200, {"hits": self.hits, "mode": "hybrid", "vectorStatus": "used"}
        if path.endswith("/query"):
            rows = self.datasets.get(body["dataset"], [])
            flt = body.get("filter") or {}
            if "id" in flt and "$in" in flt["id"]:
                rows = [r for r in rows if r["id"] in flt["id"]["$in"]]
            if "seq" in flt and "$gt" in flt["seq"]:
                rows = [r for r in rows if r.get("seq", 0) > flt["seq"]["$gt"]]
            if "level" in flt:
                rows = [r for r in rows if r.get("level") == flt["level"]]
            for key in reversed(body.get("sort") or []):
                rows = sorted(rows, key=lambda r: r.get(key.lstrip("-"), 0),
                              reverse=key.startswith("-"))
            lim = body.get("limit")
            return 200, {"records": rows[:lim] if lim else rows}
        if path.endswith("/agent/turns"):
            self.appended_turns.append(body)
            return 200, {"seq": len(self.appended_turns) - 1}
        if path.endswith("/agent/chunks"):
            self.created_chunks.append(body)
            return 200, {"seq": len(self.created_chunks) - 1}
        if "/agent/memory/" in path:
            self.bumps.append((path.rsplit("/", 1)[1], body))
            return 200, {"versionId": "v", "changeId": "c", "recordIds": ["m1"]}
        if path.endswith("/chat/messages"):
            self.chat_posts.append(body)
            return 200, {"recordIds": [f"msg{len(self.chat_posts)}"]}
        if path.endswith("/modify"):
            self.modifies.append(body)
            return 200, {"versionId": "v", "changeId": "c",
                         "recordIds": [body["records"][0]["id"]]}
        return 404, {"error": {"code": "unknown", "message": path}}


def scripted_anthropic(responses):
    calls = []

    def transport(prov, req):
        calls.append(req)
        return responses[min(len(calls), len(responses)) - 1]
    return transport, calls


CFG = {
    "llm.tier.codegen": {"value": {"provider": "anthropic", "model": "t",
                                   "base_url": "http://x", "api_key_ref": "llm.key"}},
    "llm.tier.classify": {"value": {"provider": "anthropic", "model": "t",
                                    "base_url": "http://x", "api_key_ref": "llm.key"}},
    "llm.key": {"localValue": "fake"},
}


def make_runner(fake, transport, resolver=None):
    return Runner(AnyClient(fake), Config(DictConfigStore(dict(CFG))),
                  kernel_wasm=KERNEL, traces_dir=Path(tempfile.mkdtemp()),
                  resolver=resolver or DictResolver({}),
                  user_space="s1", llm_transport=transport)


def test_conversation_end_to_end_with_injection_and_guest_effect():
    fake = FakeAnySpace(hits=[{"scope": "agent", "objectId": "brain1",
                               "dataset": "agent_memory_items",
                               "recordId": "m1", "score": 0.9}])
    cell = ("rows = effect('any.query', {'space': 's1', 'object_id': 'chat1',"
            " 'dataset': 'agent_memory_items', 'limit': 5})\nlen(rows)")
    transport, calls = scripted_anthropic([
        {"content": [{"type": "tool_use", "id": "t1", "name": "run_cell",
                      "input": {"code": cell}}],
         "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}},
        {"content": [{"type": "text", "text": "you like dark roast."}],
         "stop_reason": "end_turn", "usage": {"input_tokens": 20, "output_tokens": 3}},
    ])
    runner = make_runner(fake, transport)
    result = runner.run_conversation("chat1", "what coffee do I like?")

    assert result.outcome.stop == "done"
    # auto-recall entered the FIRST request as a synthetic tool pair
    first_msgs = calls[0]["messages"]
    blocks = [b for m in first_msgs for b in m["content"]]
    assert any(b.get("type") == "tool_use" and b.get("name") == "recall"
               for b in blocks)
    tool_results = [b for b in blocks if b.get("type") == "tool_result"]
    assert any("dark roast" in str(b.get("content")) for b in tool_results)
    # the injected item's accessCount bumped
    assert ("m1", {"accessCount": 1}) in fake.bumps
    # the guest cell's any.query effect reached the fake wire
    assert result.outcome.replies == ["you like dark roast."]
    # turn persisted with the local trace ref; trace file parses as JSONL
    assert fake.appended_turns[0]["userText"] == "what coffee do I like?"
    trace_path = runner._traces_dir / f"{result.trace_ref}.jsonl"
    records = [json.loads(line) for line in trace_path.read_text().splitlines()]
    effects = [r["effect"] for r in records if r.get("kind") == "effect"]
    assert "llm.chat" in effects and "any.query" in effects
    # ROI injection log written (best-effort path exercised)
    roi_writes = [m for m in fake.modifies
                  if m.get("dataset") == "agent_roi_injections"]
    assert len(roi_writes) == 1
    assert roi_writes[0]["records"][0]["ops"][0]["value"]["referenced"] is True


def test_same_conversation_twice_traces_identically():
    """Loop purity (ADR-005): same inputs + same effect answers ⇒ the
    trace's effect sequence (names + input keys) is bit-identical —
    the property strict replay stands on."""
    def one_run():
        fake = FakeAnySpace()
        transport, _ = scripted_anthropic([
            {"content": [{"type": "text", "text": "hi."}],
             "stop_reason": "end_turn",
             "usage": {"input_tokens": 5, "output_tokens": 1}}])
        runner = make_runner(fake, transport)
        result = runner.run_conversation("chat1", "hello")
        trace = runner._traces_dir / f"{result.trace_ref}.jsonl"
        return [(r["effect"], r["key"]) for r in
                (json.loads(ln) for ln in trace.read_text().splitlines())
                if r.get("kind") == "effect"]

    assert one_run() == one_run()


def test_rollup_program_runs_in_the_real_guest():
    turns = [{"id": f"t{i}", "seq": i, "userText": f"u{i}", "replies": [f"r{i}"],
              "createdAt": 1000 + i} for i in range(1, 11)]
    fake = FakeAnySpace(turns=turns)
    transport, calls = scripted_anthropic([
        {"content": [{"type": "text", "text": "ten turns about coffee."}],
         "stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 5}},
    ])
    resolver = DictResolver({"rollup@v1": (PROGRAMS / "rollup@v1.py").read_text()})
    runner = make_runner(fake, transport, resolver=resolver)

    res = runner.run_program("rollup@v1", {"space": "s1", "chatId": "chat1"})
    assert res.status == "ok", res.error
    assert res.fuel and res.fuel > 0
    assert len(fake.created_chunks) == 1
    chunk = fake.created_chunks[0]
    assert (chunk["level"], chunk["fromSeq"], chunk["toSeq"]) == (1, 1, 10)
    assert chunk["summary"] == "ten turns about coffee."
    assert calls[0]["messages"][0]["content"][0]["text"].startswith("user: u1")
