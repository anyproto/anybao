"""The runtime binary end-to-end, OFFLINE: `anyrt run toolcaller@v1`
as a subprocess against a stdlib fake serving BOTH backends (any server
+ anthropic) on localhost. No project imports — the binary's CLI and
the wire are the whole contract. Skips without the binary or kernel."""

import json
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KERNEL = ROOT / "bin" / "kernel.wasm"


def rt_binary():
    for profile in ("release", "debug"):
        p = ROOT / "runtime" / "target" / profile / "anyrt"
        if p.exists():
            return p
    return None


pytestmark = pytest.mark.skipif(
    rt_binary() is None or not KERNEL.exists(),
    reason="needs runtime/target/*/anyrt (make runtime) + bin/kernel.wasm")


class FakeBackends(BaseHTTPRequestHandler):
    """One localhost server answering /llm/* as anthropic and /v1/* as
    the any server (state on the class: fresh per test via reset)."""

    llm_replies: list = []
    llm_requests: list = []
    turns: list = []
    chat_posts: list = []
    datasets: dict = {}

    @classmethod
    def reset(cls, llm_replies):
        cls.llm_replies = list(llm_replies)
        cls.llm_requests, cls.turns, cls.chat_posts = [], [], []
        cls.datasets = {"agent_turns": cls.turns, "agent_chunks": [],
                        "agent_memory_items": [], "agent_roi_injections": []}

    def log_message(self, *a):
        pass

    def _reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # compose_system reads the space at boot: type catalog + brain
        if self.path.endswith("/v1/spaces"):
            # empty catalog -> space-name resolution passes strings
            # through (degenerate-env rule, ADR-010 §8)
            return self._reply({"spaces": []})
        if self.path.endswith("/types"):
            return self._reply({"types": []})
        if self.path.endswith("/properties"):
            # builtin-group filter paths resolve against these (A19);
            # program.any_tool rides the compose's tool query
            return self._reply({"properties": [
                {"id": "name"}, {"id": "types"}, {"id": "any_tool"}]})
        if self.path.endswith("/agent/brain"):
            return self._reply({"objectId": "brain1"})
        return self._reply({"error": {"code": "unknown", "message": self.path}}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        cls = type(self)
        if self.path.endswith("/v1/messages"):
            assert self.headers.get("x-api-key") == "sk-test", "credential missing"
            cls.llm_requests.append(body)
            return self._reply(cls.llm_replies.pop(0))
        if self.path.endswith("/search"):
            return self._reply({"hits": [], "mode": "fts"})
        if self.path.endswith("/query"):
            rows = cls.datasets.get(body.get("dataset", ""), [])
            return self._reply({"records": rows})
        if self.path.endswith("/agent/turns"):
            cls.turns.append(body)
            return self._reply({"seq": len(cls.turns) - 1})
        if self.path.endswith("/chat/messages"):
            cls.chat_posts.append(body)
            return self._reply({"recordIds": [f"m{len(cls.chat_posts)}"]})
        if self.path.endswith("/modify"):
            rec = body["records"][0]
            cls.datasets.setdefault(body["dataset"], []).append(
                {"id": rec["id"], **rec["ops"][0]["value"]})
            return self._reply({"versionId": "v", "changeId": "c",
                                "recordIds": [rec["id"]]})
        return self._reply({"error": {"code": "unknown", "message": self.path}}, 404)


@pytest.fixture
def backends():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBackends)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def run_rt(base, spec, args):
    scratch = Path(tempfile.mkdtemp())
    (scratch / "config.json").write_text(json.dumps({
        "any.base_url": base,
        "llm.tier.codegen": {"provider": "anthropic", "model": "t",
                             "base_url": f"{base}/llm",
                             "api_key_ref": "llm.key"},
        "llm.tier.classify": {"provider": "anthropic", "model": "t",
                              "base_url": f"{base}/llm",
                              "api_key_ref": "llm.key"},
    }))
    (scratch / "secrets.env").write_text("llm.key=sk-test\n")
    proc = subprocess.run(
        [rt_binary(), "run", spec, "--args", json.dumps(args),
         "--kernel", KERNEL, "--programs", ROOT / "repos" / "_agent" / "programs",
         "--traces-dir", scratch / "traces",
         "--config", scratch / "config.json",
         "--secrets-file", scratch / "secrets.env"],
        capture_output=True, text=True, timeout=300)
    out = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    return proc, out, scratch


def test_full_conversation_through_the_binary(backends):
    FakeBackends.reset([
        {"content": [{"type": "tool_use", "id": "t1", "name": "run_cell",
                      "input": {"code": "x = 40 + 2\nprint(x)\nx"}}],
         "stop_reason": "tool_use",
         "usage": {"input_tokens": 10, "output_tokens": 5}},
        {"content": [{"type": "text", "text": "It is 42."}],
         "stop_reason": "end_turn",
         "usage": {"input_tokens": 20, "output_tokens": 6}},
    ])
    proc, out, scratch = run_rt(backends, "toolcaller@v1",
                                {"space": "s1", "chatId": "c1",
                                 "userText": "what is 40+2?"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["status"] == "ok"
    assert out["value"]["stop"] == "done"
    assert out["value"]["replies"] == ["It is 42."]
    # the model's cell ran in the kernel — its digest reached request 2
    results = [b for m in FakeBackends.llm_requests[1]["messages"]
               for b in m["content"] if b.get("type") == "tool_result"]
    assert any("#0 42" in str(b.get("content")) for b in results)
    # turn + done bubble persisted through the any side
    assert FakeBackends.turns[0]["userText"] == "what is 40+2?"
    assert FakeBackends.chat_posts[-1]["agent"]["done"] is True
    # the trace exists and speaks only syscalls
    trace = next((scratch / "traces").glob("run_*.jsonl"))
    effects = {json.loads(ln)["effect"] for ln in trace.read_text().splitlines()
               if json.loads(ln).get("kind") == "effect"}
    assert all(e.split(".")[0] in
               ("http", "mailbox", "module", "kernel", "trace", "config",
                "time", "random", "env", "uuid4", "sleep", "batch")
               for e in effects), effects


def test_error_status_on_guest_failure(backends):
    FakeBackends.reset([])  # llm immediately fails → toolcaller errors
    proc, out, _ = run_rt(backends, "toolcaller@v1",
                          {"space": "s1", "chatId": "c1", "userText": "hi"})
    assert proc.returncode == 1
    assert out["status"] == "error"
