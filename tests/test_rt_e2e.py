"""The runtime binary end-to-end, OFFLINE: `anyrt run toolcaller@v1`
as a subprocess against a stdlib fake serving BOTH backends (any server
+ anthropic, the local store included — the trace lands there, ADR-023
§1) on localhost. No project imports — the binary's CLI and
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
    local: dict = {}          # local-store collections: name -> {id: doc}

    @classmethod
    def reset(cls, llm_replies):
        cls.llm_replies = list(llm_replies)
        cls.llm_requests, cls.turns, cls.chat_posts = [], [], []
        cls.local = {}
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
        # compose_system reads the space at boot: type catalog + the
        # ADR-017 store plumbing (bundles + dataset defs)
        if self.path.endswith("/v1/spaces"):
            # empty catalog -> space-name resolution passes strings
            # through (degenerate-env rule, ADR-010 §8)
            return self._reply({"spaces": []})
        path = self.path.partition("?")[0]
        if "/types/" in path and path.endswith("/datasets"):
            # defs pre-exist with matching search leaves — no PATCH;
            # the collection is the server's (ADR-027 §2): `<typeId>_<key>`
            tid = path.rsplit("/types/", 1)[1].split("/")[0]

            def ds(n, key, **rest):
                return {"id": n, "key": key, "collection": f"{tid}_{key}",
                        "module": "records", **rest}
            return self._reply({"datasets": [
                ds("d1", "agent_memory_items",
                   search={"title": "context", "text": "body", "scope": "agent"}),
                ds("d2", "agent_job_state"), ds("d5", "agent_roi_injections"),
                ds("d3", "agent_turns",
                   search={"title": "userText", "text": "searchText",
                           "scope": "history"}),
                ds("d4", "agent_chunks", search={"text": "summary", "scope": "history"})]})
        if path.endswith("/types"):
            return self._reply({"types": [
                {"id": "br", "name": "Agent Brain", "xKey": "agent_brain", "hidden": True},
                {"id": "lg", "name": "Agent Log", "xKey": "agent_log", "hidden": True}]})
        if path.endswith("/v1/catalog"):
            return self._reply({"usecases": []})
        if self.path.endswith("/properties"):
            # builtin-group filter paths resolve against these (A19);
            # program.any_tool rides the compose's tool query
            return self._reply({"properties": [
                {"id": "name"}, {"id": "types"}, {"id": "any_tool"}]})
        if self.path.endswith("/bundles"):
            # the test chat c1 is the catalog's chat root (ADR-027 §1)
            return self._reply({"bundles": [
                {"id": "system:general-chat/v1", "rootId": "c1", "derived": True}],
                "synced": True})
        return self._reply({"error": {"code": "unknown", "message": self.path}}, 404)

    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if self.path.endswith("/v1/local/collections"):
            return self._reply({})      # ensure: idempotent, nothing to say
        return self._reply({"error": {"code": "unknown", "message": self.path}}, 404)

    def _local(self, body):
        """/v1/local/* — enough of the any local store (ADR-023 §2) for
        a run to stream its trace and land its summary."""
        cls = type(self)
        coll = cls.local.setdefault(body["coll"]["name"], {})
        op = self.path.rsplit("/", 1)[1]
        if op == "upsert":
            for d in body.get("docs") or []:
                coll[d["id"]] = d
            return self._reply({"ids": [d["id"] for d in body.get("docs") or []]})
        if op == "get":
            doc = coll.get(body["id"])
            if doc is None:
                return self._reply({"error": {"code": "local.doc_not_found",
                                              "message": body["id"]}}, 404)
            return self._reply({"record": doc})
        if op == "query":
            flt = body.get("filter") or {}
            rows = [d for d in coll.values()
                    if all(d.get(k) == v for k, v in flt.items()
                           if not isinstance(v, dict))]
            for key in reversed(body.get("sort") or []):
                rows.sort(key=lambda d: d.get(key.lstrip("-")) or 0,
                          reverse=key.startswith("-"))
            off = body.get("offset") or 0
            return self._reply({"records": rows[off:off + (body.get("limit") or len(rows))]})
        if op == "aggregate":
            return self._reply({"records": []})
        return self._reply({"error": {"code": "unknown", "message": self.path}}, 404)

    @staticmethod
    def _store(collection):
        # the guest addresses a store by its collection `<typeId>_<key>`;
        # the fake keeps records by key
        return collection.split("_", 1)[1] if "_" in collection else collection

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        cls = type(self)
        if "/v1/local/" in self.path:
            return self._local(body)
        if self.path.endswith("/v1/messages"):
            assert self.headers.get("x-api-key") == "sk-test", "credential missing"
            cls.llm_requests.append(body)
            return self._reply(cls.llm_replies.pop(0))
        if self.path.endswith("/search"):
            return self._reply({"hits": [], "mode": "fts"})
        if self.path.endswith("/objects/query"):
            return self._reply({"records": []})
        if self.path.endswith("/query"):
            rows = cls.datasets.get(self._store(body.get("dataset", "")), [])
            return self._reply({"records": rows})
        if "/types/" in self.path and self.path.endswith("/parts"):
            return self._reply({"partId": "pX"}, 201)
        if self.path.endswith("/children"):
            seed = body.get("seed", "")
            oid = {"bao/brain/v1": "brain1", "bao/log/v1": "log1"}.get(
                seed, f"child-{seed}")
            return self._reply({"objectId": oid})
        if self.path.endswith("/upsert"):
            recs = [dict(r.get("fields") or {}, id=r["id"])
                    for r in body.get("records") or []]
            cls.datasets.setdefault(self._store(body["dataset"]), []).extend(recs)
            return self._reply({"created": len(recs), "updated": 0,
                                "skipped": 0, "rejections": [],
                                "pages": [{"recordIds": [r["id"] for r in recs]}]})
        if self.path.endswith("/chat/messages"):
            cls.chat_posts.append(body)
            return self._reply({"recordIds": [f"m{len(cls.chat_posts)}"]})
        if self.path.endswith("/modify"):
            rec = body["records"][0]
            cls.datasets.setdefault(self._store(body["dataset"]), []).append(
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


def run_rt(base, spec, args, extra=(), cmd="run"):
    """`anyrt run <spec> --args …` (or `anyrt replay <run_id>` with
    cmd="replay": `spec` is then the run id) against the fake."""
    scratch = Path(tempfile.mkdtemp())
    (scratch / "config.json").write_text(json.dumps({
        "any.base_url": base,
        "bao.space": "s1",          # the memory home (ADR-017 §0), lifted to runtime
        "llm.tier.codegen": {"provider": "anthropic", "model": "t",
                             "base_url": f"{base}/llm",
                             "api_key_ref": "llm.key"},
        "llm.tier.classify": {"provider": "anthropic", "model": "t",
                              "base_url": f"{base}/llm",
                              "api_key_ref": "llm.key"},
    }))
    (scratch / "secrets.env").write_text("llm.key=sk-test\n")
    head = [spec, "--args", json.dumps(args)] if cmd == "run" else [spec]
    proc = subprocess.run(
        [rt_binary(), cmd, *head, *extra,
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
    # the trace landed in the space's local store and speaks only syscalls
    records = FakeBackends.local["trace_records"].values()
    assert any(r.get("kind") == "header" for r in records)
    effects = {r["effect"] for r in records if r.get("kind") == "effect"}
    assert all(e.split(".")[0] in
               ("http", "mailbox", "module", "kernel", "trace", "config", "runtime",
                "time", "random", "env", "uuid4", "sleep", "batch")
               for e in effects), effects


def test_error_status_on_guest_failure(backends):
    FakeBackends.reset([])  # llm immediately fails → toolcaller errors
    proc, out, _ = run_rt(backends, "toolcaller@v1",
                          {"space": "s1", "chatId": "c1", "userText": "hi"})
    assert proc.returncode == 1
    assert out["status"] == "error"


REPLIES_42 = [
    {"content": [{"type": "tool_use", "id": "t1", "name": "run_cell",
                  "input": {"code": "x = 40 + 2\nprint(x)\nx"}}],
     "stop_reason": "tool_use",
     "usage": {"input_tokens": 10, "output_tokens": 5}},
    {"content": [{"type": "text", "text": "It is 42."}],
     "stop_reason": "end_turn",
     "usage": {"input_tokens": 20, "output_tokens": 6}},
]


def _run_records(run_id):
    return [r for r in FakeBackends.local["trace_records"].values()
            if r.get("runId") == run_id]


def test_mock_and_replay_serve_recorded_effects(backends):
    """ADR-028 §4: `--mock <run>` serves every effect from the
    recording (the model is never called again), `replay <run>` walks
    it strictly, `--mock-except` keeps a glob live."""
    FakeBackends.reset(REPLIES_42)
    args = {"space": "s1", "chatId": "c1", "userText": "what is 40+2?"}
    proc, out, _ = run_rt(backends, "toolcaller@v1", args)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    live = out["traceRef"]
    live_calls = len(FakeBackends.llm_requests)
    assert live_calls == 2
    # the fake has no replies left: any live llm call would now fail
    FakeBackends.llm_replies = []

    proc, out, _ = run_rt(backends, "toolcaller@v1", args, extra=["--mock", live])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["value"]["replies"] == ["It is 42."]
    assert len(FakeBackends.llm_requests) == live_calls
    recs = _run_records(out["traceRef"])
    header = next(r for r in recs if r.get("kind") == "header")
    assert header["run"]["mock"] == {"from": live}
    assert header["run"]["args"] == args
    posts = [r for r in recs if r.get("kind") == "effect" and r["effect"] == "http.post"]
    assert posts
    for r in posts:
        assert r["meta"]["mocked"] is True
        assert r["meta"]["mock"]["from"] == live
        assert isinstance(r["meta"]["mock"]["seq"], int)
    # the trace's own reads are never served from the mock (§7)
    views = [r for r in recs if r.get("kind") == "effect" and r["effect"].startswith("trace.")]
    assert views and all(r["meta"]["mocked"] is False and "mock" not in r["meta"]
                         for r in views)

    proc, out, _ = run_rt(backends, live, None, cmd="replay")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["value"]["replies"] == ["It is 42."]
    assert len(FakeBackends.llm_requests) == live_calls
    header = next(r for r in _run_records(out["traceRef"]) if r.get("kind") == "header")
    assert header["run"]["replayOf"] == live
    assert header["run"]["program"] == "toolcaller@v1"

    # outside the mockable set the call is plainly live: the model IS
    # called, and nothing on those records says mock
    FakeBackends.llm_replies = list(REPLIES_42)
    proc, out, _ = run_rt(backends, "toolcaller@v1", args,
                          extra=["--mock", live, "--mock-except", "http.*"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(FakeBackends.llm_requests) == live_calls + 2
    recs = _run_records(out["traceRef"])
    assert next(r for r in recs if r.get("kind") == "header")["run"]["mock"] == {
        "from": live, "except": ["http.*"]}
    posts = [r for r in recs if r.get("kind") == "effect" and r["effect"] == "http.post"]
    assert posts and all(r["meta"]["mocked"] is False and "mock" not in r["meta"]
                         for r in posts)
    boots = [r for r in recs if r.get("kind") == "effect" and r["effect"] == "kernel.boot"]
    assert boots and boots[0]["meta"]["mocked"] is True


def test_mock_spec_errors_before_the_run(backends):
    FakeBackends.reset(REPLIES_42)
    args = {"space": "s1", "chatId": "c1", "userText": "hi"}
    proc, out, _ = run_rt(backends, "toolcaller@v1", args,
                          extra=["--mock", "run_0000000000000000"])
    assert proc.returncode != 0
    assert "--mock" in proc.stderr and "run_0000000000000000" in proc.stderr
    assert not FakeBackends.llm_requests
