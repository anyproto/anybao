"""The v3 stack end-to-end, OFFLINE: real WasiEngine, all guest modules
via use(), the llm as a credentialed http call answered by a fake wire.
One conversation exercises: boot window, auto-recall injection, model
cell via subcell (span-grouped), digest, chat bubbles, turn persistence,
ROI log — every hop through the recorded http syscall. Skips without
bin/kernel.wasm (repo convention)."""

import json
import tempfile
from pathlib import Path

import pytest
from anybao.anyclient import AnyClient
from anybao.config import Config, DictConfigStore
from anybao.mailbox import Mailbox
from anybao.runner import Runner
from anyrt.builtin_effects import DictResolver

ROOT = Path(__file__).resolve().parents[2]
KERNEL = ROOT / "bin" / "kernel.wasm"
PROGRAMS = ROOT / "programs"
ANY = "http://anyserver.test"

pytestmark = pytest.mark.skipif(not KERNEL.exists(),
                                reason="bin/kernel.wasm missing — run `make kernel`")


def resolver():
    return DictResolver({p.stem: p.read_text()
                         for p in PROGRAMS.glob("*.py")})


class FakeWire:
    """Answers BOTH backends at the http layer: the any server (space
    data) and the anthropic endpoint (scripted replies)."""

    def __init__(self, llm_replies, memory_items=()):
        self.llm_replies = list(llm_replies)
        self.llm_requests = []
        self.turns = []
        self.chat_posts = []
        self.datasets = {"agent_turns": self.turns, "agent_chunks": [],
                         "agent_memory_items": list(memory_items),
                         "agent_roi_injections": []}
        self.search_hits = []

    def __call__(self, method, url, *, params=None, headers=None,
                 json_body=None, body=None, timeout=None):
        if url.endswith("/v1/messages"):
            assert headers.get("x-api-key") == "sk-live", "credential not injected"
            self.llm_requests.append(json_body)
            return self._ok(self.llm_replies.pop(0))
        assert url.startswith(ANY), url
        path = url[len(ANY):]
        if path.endswith("/search"):
            return self._ok({"hits": self.search_hits, "mode": "hybrid"})
        if path.endswith("/query"):
            rows = self.datasets.get(json_body["dataset"], [])
            flt = json_body.get("filter") or {}
            if "id" in flt:
                ids = flt["id"]["$in"] if isinstance(flt["id"], dict) else [flt["id"]]
                rows = [r for r in rows if r.get("id") in ids]
            return self._ok({"records": rows})
        if path.endswith("/agent/turns"):
            self.turns.append({**json_body, "seq": len(self.turns)})
            return self._ok({"seq": len(self.turns) - 1})
        if path.endswith("/chat/messages"):
            self.chat_posts.append(json_body)
            return self._ok({"recordIds": [f"m{len(self.chat_posts)}"]})
        if "/agent/memory/" in path and method == "PATCH":
            return self._ok({"versionId": "v", "changeId": "c",
                             "recordIds": [path.rsplit("/", 1)[1]]})
        if path.endswith("/modify"):
            rec = json_body["records"][0]
            self.datasets.setdefault(json_body["dataset"], []).append(
                {"id": rec["id"], **rec["ops"][0]["value"]})
            return self._ok({"versionId": "v", "changeId": "c",
                             "recordIds": [rec["id"]]})
        return {"status": 404, "headers": {},
                "body": json.dumps({"error": {"code": "unknown", "message": path}})}

    @staticmethod
    def _ok(payload):
        return {"status": 200, "headers": {}, "body": json.dumps(payload)}


def make_runner(wire):
    cfg = Config(DictConfigStore({
        "any.base_url": {"value": ANY},
        "llm.tier.codegen": {"value": {"provider": "anthropic", "model": "t",
                                       "base_url": "https://api.anthropic.com",
                                       "api_key_ref": "llm.key"}},
        "llm.tier.classify": {"value": {"provider": "anthropic", "model": "t",
                                        "base_url": "https://api.anthropic.com",
                                        "api_key_ref": "llm.key"}},
        "llm.key": {"localValue": "sk-live"},
    }))
    return Runner(AnyClient(lambda m, p, b: (200, {})), cfg,
                  kernel_wasm=KERNEL, traces_dir=Path(tempfile.mkdtemp()),
                  resolver=resolver(), user_space="s1", any_base=ANY,
                  http_request=wire)


MEM = {"id": "m1", "category": "preference", "context": "prefers dark roast",
       "confidence": 8, "validFrom": 1751328000, "accessCount": 0}


def test_full_conversation_through_the_kernel():
    wire = FakeWire(
        llm_replies=[
            {"content": [{"type": "tool_use", "id": "t1", "name": "run_cell",
                          "input": {"code": "x = 40 + 2\nprint(x)\nx"}}],
             "stop_reason": "tool_use",
             "usage": {"input_tokens": 10, "output_tokens": 5}},
            {"content": [{"type": "text", "text": "It is 42; you like dark roast."}],
             "stop_reason": "end_turn",
             "usage": {"input_tokens": 20, "output_tokens": 6}},
        ],
        memory_items=[MEM])
    wire.search_hits = [{"scope": "agent", "objectId": "brain1",
                         "dataset": "agent_memory_items", "recordId": "m1",
                         "score": 0.03}]
    runner = make_runner(wire)
    result = runner.run_conversation("chat1", "what's 40+2, coffee fan?")

    assert result.status == "ok", result.error
    assert result.value and result.value["stop"] == "done"
    assert result.value["replies"] == ["It is 42; you like dark roast."]
    assert result.value["injected"] == 1

    # auto-recall reached the provider wire as a synthetic tool pair
    first = wire.llm_requests[0]["messages"]
    blocks = [b for m in first for b in m["content"]]
    assert any(b.get("type") == "tool_use" and b.get("name") == "recall"
               for b in blocks)
    assert any("dark roast" in str(b.get("content", ""))
               for b in blocks if b.get("type") == "tool_result")
    # the model's cell ran in the kernel: digest carries the print
    cell_results = [b for m in wire.llm_requests[1]["messages"]
                    for b in m["content"] if b.get("type") == "tool_result"
                    if b.get("tool_use_id") == "t1"]
    assert "#0 42" in str(cell_results[0]["content"])
    # reply bubble + turn persisted guest-side
    assert wire.chat_posts[-1]["agent"]["done"] is True
    assert wire.turns[0]["userText"] == "what's 40+2, coffee fan?"
    assert wire.turns[0]["llm"]["stopReason"] == "done"
    # ROI log written through /modify
    assert wire.datasets["agent_roi_injections"]

    # the trace shows the syscall surface only: http + span/cell + mailbox
    trace = runner._traces_dir / f"{result.trace_ref}.jsonl"
    records = [json.loads(ln) for ln in trace.read_text().splitlines()]
    effects = {r["effect"] for r in records if r.get("kind") == "effect"}
    assert any(e.startswith("http.") for e in effects)
    assert "mailbox.drain" in effects
    syscalls = ("http.", "mailbox.", "module.", "kernel.", "trace.",
                "config.", "time.", "random.", "env.", "uuid4", "sleep", "batch")
    assert all(e.startswith(syscalls) for e in effects), effects
    spans = [r for r in records if r.get("kind") == "span"]
    assert any(r.get("name") == "cell" for r in spans)
    assert any(r.get("name") == "llm.chat" for r in spans)


def test_hard_break_interrupts_and_reports_stopped():
    wire = FakeWire(llm_replies=[])  # llm never answers — we interrupt first
    infra_posts = []

    def infra(method, path, body):
        infra_posts.append((path, body))
        return 200, {"recordIds": ["x"]}

    runner = make_runner(wire)
    runner._client = AnyClient(infra)
    mb = Mailbox()
    mb.break_hard()   # flag set before the run: watchdog fires immediately
    result = runner.run_conversation("chat1", "hi", mailbox=mb)
    assert result.status in ("interrupted", "error")
    assert any("Stopped." in str(b) or "broke mid-run" in str(b)
               for _, b in infra_posts)
