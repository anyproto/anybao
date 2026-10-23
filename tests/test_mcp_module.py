"""programs/mcp@v1 — the generic MCP client (ADR-032), run under the
REAL guest kernel (kernelenv): `http.post` and `config.get` are the
only fakes. A scripted MCP server answers in JSON or SSE, issues a
session, forgets it on demand, pages its catalogue and reports
tool-level and JSON-RPC errors — the wire shapes bao will meet behind
the browser relay and any other streamable-HTTP server."""

import json

import pytest
from kernelenv import load_kernel

URL = "http://127.0.0.1:27311/mcp"
ROW = {"transport": "http", "url": URL, "api_key_ref": "mcp.key.t", "timeout": 7}
TOOLS = [{"name": "read_page", "description": "Markdown of a granted tab.",
          "inputSchema": {"type": "object", "properties": {"tabId": {"type": "integer"}},
                          "required": ["tabId"]}},
         {"name": "list_pages", "description": "Pages inside the grant.",
          "inputSchema": {"type": "object", "properties": {}}}]


class ScriptedMcp:
    """An MCP server behind the http.post effect: records every request
    (payload as the guest sent it) and answers by method."""

    def __init__(self, sse=False, session="sess-1", page=False, instructions=None):
        self.sse = sse
        self.instructions = instructions
        self.session = session
        self.page = page
        self.requests = []
        self.forget_after = None      # answer 404 to the Nth session request
        self.sessions_issued = 0
        self.call_results = {}        # tool name → tools/call result
        self.rpc_errors = {}          # method → JSON-RPC error object

    def __call__(self, payload):
        self.requests.append(payload)
        msg = payload["json"]
        headers = payload.get("headers") or {}
        method = msg.get("method")
        if method == "notifications/initialized":
            return self._http(202, "", "text/plain")
        if method == "initialize":
            self.sessions_issued += 1
            sid = f"{self.session}-{self.sessions_issued}" if self.session else None
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "scripted", "version": "0"}}
            if self.instructions is not None:
                result["instructions"] = self.instructions
            return self._rpc(msg["id"], result, session=sid)
        if self.session and headers.get("Mcp-Session-Id") is None:
            return self._http(400, json.dumps({"error": "missing session"}))
        if self.forget_after is not None:
            n = len([r for r in self.requests if r["json"].get("id") is not None])
            if n == self.forget_after:
                return self._http(404, "")
        if method in self.rpc_errors:
            return self._rpc(msg["id"], error=self.rpc_errors[method])
        if method == "tools/list":
            cursor = (msg.get("params") or {}).get("cursor")
            if self.page and cursor is None:
                return self._rpc(msg["id"], {"tools": TOOLS[:1], "nextCursor": "c2"})
            return self._rpc(msg["id"], {"tools": TOOLS[1:] if cursor else TOOLS})
        if method == "tools/call":
            name = msg["params"]["name"]
            if name in self.call_results:
                return self._rpc(msg["id"], self.call_results[name])
            text = f"{name}({json.dumps(msg['params'].get('arguments'))})"
            return self._rpc(msg["id"], {"content": [{"type": "text", "text": text}]})
        if method == "ping":
            return self._rpc(msg["id"], {})
        return self._rpc(msg["id"], error={"code": -32601, "message": f"unknown {method}"})

    def _rpc(self, rid, result=None, error=None, session=None):
        reply = {"jsonrpc": "2.0", "id": rid}
        reply["error" if error is not None else "result"] = error if error is not None else result
        headers = {"Mcp-Session-Id": session} if session else {}
        if self.sse:
            note = {"jsonrpc": "2.0", "method": "notifications/message",
                    "params": {"level": "info", "data": "hi"}}
            body = (f"event: message\ndata: {json.dumps(note)}\n\n"
                    f"event: message\ndata: {json.dumps(reply)}\n\n")
            return self._http(200, body, "text/event-stream", headers)
        return self._http(200, json.dumps(reply), "application/json", headers)

    @staticmethod
    def _http(status, body, media="application/json", headers=None):
        h = {"content-type": media}
        h.update(headers or {})
        return {"status": status, "headers": h, "body": body, "url": URL}


class FakeAny:
    """The any@v1 surface config@v1.list touches, plus bao_space()."""

    def __init__(self, rows):
        self.rows = rows

    def bao_space(self):
        return "bao-space"

    def bundle_child(self, space, bundle, seed):
        return {"objectId": "cfg-child"}

    def query(self, space, obj, dataset, **kw):
        return [{"key": k, "value": v} for k, v in self.rows.items()]


def kernel(server, rows=None, raise_http=None):
    rows = rows if rows is not None else {"mcp.server.t": ROW}

    def eff(name, payload):
        if name == "config.get":
            if payload["key"] not in rows:
                raise KeyError(f"no config row {payload['key']!r}")
            return {"value": rows[payload["key"]]}
        if name == "config.set":
            rows[payload["key"]] = payload["value"]
            return {}
        if name == "http.post":
            if raise_http is not None:
                raise raise_http
            return server(payload)
        raise AssertionError(f"unexpected effect {name}")

    app = load_kernel(effect=eff, any_client=FakeAny(rows))
    return app.use("mcp@v1")


def test_tools_initializes_first_then_lists_with_the_session_header():
    srv = ScriptedMcp(page=True)
    m = kernel(srv)
    out = m.tools("t")
    assert out == {"ok": True, "tools": TOOLS}
    methods = [r["json"].get("method") for r in srv.requests]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/list"]
    init = srv.requests[0]
    assert init["url"] == URL
    assert init["stream"] is True
    assert init["timeout"] == {"idle": 7, "total": 105}
    assert init["headers"]["Accept"] == "application/json, text/event-stream"
    assert init["headers"]["MCP-Protocol-Version"] == "2025-06-18"
    assert "Mcp-Session-Id" not in init["headers"]
    assert init["json"]["params"] == {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "bao", "version": "anyrt"}}
    # the key never enters the guest: a ref + the binding the card shows
    assert init["credential"] == {
        "ref": "mcp.key.t", "header": "Authorization", "prefix": "Bearer ",
        "about": {"label": "t MCP server key", "hosts": ["127.0.0.1:27311"], "help": URL}}
    # the session the server issued rides every later message
    for r in srv.requests[1:]:
        assert r["headers"]["Mcp-Session-Id"] == "sess-1-1"
    # paging: the second list call carried the cursor
    assert srv.requests[3]["json"]["params"] == {"cursor": "c2"}
    # ids are distinct and the notification has none
    ids = [r["json"].get("id") for r in srv.requests]
    assert ids[1] is None and len({i for i in ids if i is not None}) == 3


def test_call_keeps_mcp_names_and_joins_text():
    srv = ScriptedMcp()
    srv.call_results["read_page"] = {
        "content": [{"type": "text", "text": "# Title"}, {"type": "image", "data": "AA==",
                                                           "mimeType": "image/png"},
                    {"type": "text", "text": "body"}],
        "structuredContent": {"wordCount": 2}}
    m = kernel(srv)
    out = m.call("t", "read_page", {"tabId": 3})
    assert out["ok"] is True
    assert out["text"] == "# Title\n\nbody"
    assert out["content"][1] == {"type": "image", "data": "AA==", "mimeType": "image/png"}
    assert out["structuredContent"] == {"wordCount": 2}
    assert out["isError"] is False
    assert "error" not in out
    assert srv.requests[-1]["json"]["params"] == {"name": "read_page", "arguments": {"tabId": 3}}
    # a second call in the same run does not initialize again
    m.call("t", "list_pages")
    assert [r["json"].get("method") for r in srv.requests].count("initialize") == 1
    assert srv.requests[-1]["json"]["params"] == {"name": "list_pages", "arguments": {}}


def test_sse_answers_are_parsed_and_notifications_dropped():
    srv = ScriptedMcp(sse=True)
    m = kernel(srv)
    out = m.call("t", "list_pages")
    assert out["ok"] is True
    assert out["text"] == "list_pages({})"


def test_tool_level_error_is_ok_false_with_the_whole_result():
    srv = ScriptedMcp()
    srv.call_results["read_page"] = {
        "isError": True, "content": [{"type": "text", "text": "tab 9 is not granted"}],
        "structuredContent": {"reason": "scope"}}
    m = kernel(srv)
    out = m.call("t", "read_page", {"tabId": 9})
    assert out["ok"] is False
    assert out["error"] == "tab 9 is not granted"
    assert out["isError"] is True
    assert out["structuredContent"] == {"reason": "scope"}
    assert out["content"][0]["text"] == "tab 9 is not granted"


def test_jsonrpc_error_surfaces_code_and_message_intact():
    srv = ScriptedMcp()
    srv.rpc_errors["tools/call"] = {
        "code": -32001,
        "message": "No browser extension is connected. "
                   "Install Any Web Clipper and pair it in Settings.",
        "data": {"hint": "settings"}}
    m = kernel(srv)
    out = m.call("t", "read_page", {"tabId": 1})
    assert out == {"ok": False, "code": -32001, "data": {"hint": "settings"},
                   "error": "No browser extension is connected. Install Any Web Clipper "
                            "and pair it in Settings."}


def test_a_404_on_the_session_reinitializes_once_and_retries():
    srv = ScriptedMcp()
    srv.forget_after = 2            # the 2nd id-bearing request (the first tools/list)
    m = kernel(srv)
    out = m.tools("t")
    assert out["ok"] is True and out["tools"] == TOOLS
    methods = [r["json"].get("method") for r in srv.requests]
    assert methods == ["initialize", "notifications/initialized", "tools/list",
                       "initialize", "notifications/initialized", "tools/list"]
    assert srv.requests[-1]["headers"]["Mcp-Session-Id"] == "sess-1-2"


def test_sessionless_server_sends_no_session_header():
    srv = ScriptedMcp(session=None)
    m = kernel(srv)
    assert m.raw("t", "ping") == {"ok": True, "result": {}}
    assert all("Mcp-Session-Id" not in r["headers"] for r in srv.requests)


def test_missing_key_tells_the_user_where_to_enter_it():
    m = kernel(None, raise_http=RuntimeError("no secret for credential ref \"mcp.key.t\""))
    out = m.tools("t")
    assert out["ok"] is False
    assert "needs its key (mcp.key.t)" in out["error"]
    assert "credential prompt" in out["error"]
    assert "code" not in out


def test_transport_failure_names_the_server_and_url():
    m = kernel(None, raise_http=ConnectionError("connection refused"))
    out = m.call("t", "read_page", {"tabId": 1})
    assert out["ok"] is False
    assert out["error"].startswith("MCP server 't' unreachable at " + URL)
    assert "connection refused" in out["error"]


def test_http_error_status_is_reported():
    class Dead(ScriptedMcp):
        def __call__(self, payload):
            return self._http(503, "gateway down", "text/plain")
    m = kernel(Dead())
    out = m.tools("t")
    assert out == {"ok": False, "error": "MCP server 't' answered HTTP 503: gateway down"}


def test_unknown_server_and_bad_rows_fail_before_any_request():
    srv = ScriptedMcp()
    m = kernel(srv, rows={"mcp.server.t": ROW,
                          "mcp.server.pipe": {"transport": "stdio", "url": "x"},
                          "mcp.server.bare": "not-an-object"})
    out = m.tools("nope")
    assert out["ok"] is False and "no MCP server 'nope'" in out["error"]
    assert "mcp.server.nope" in out["error"]
    out = m.tools("pipe")
    assert out["ok"] is False and "transport 'stdio' is not supported" in out["error"]
    out = m.tools("bare")
    assert out["ok"] is False and "must be an object with a `url`" in out["error"]
    assert srv.requests == []


def test_keyless_row_sends_no_credential():
    srv = ScriptedMcp()
    m = kernel(srv, rows={"mcp.server.t": {"url": URL, "api_key_ref": None}})
    assert m.raw("t", "ping")["ok"] is True
    assert all("credential" not in r for r in srv.requests)
    # default timeout when the row names none
    assert srv.requests[0]["timeout"] == {"idle": 60, "total": 900}


def test_servers_lists_rows_without_key_values():
    m = kernel(ScriptedMcp(), rows={
        "mcp.server.t": ROW,
        "mcp.server.pub": {"url": "https://mcp.example/mcp", "api_key_ref": None},
        "llm.tier.codegen": {"provider": "anthropic"},
        "mcp.key.t": "never-listed-here-anyway"})
    assert m.servers() == {"servers": [
        {"id": "pub", "url": "https://mcp.example/mcp", "transport": "http", "api_key_ref": None,
         "description": None},
        {"id": "t", "url": URL, "transport": "http", "api_key_ref": "mcp.key.t",
         "description": None}]}


def test_initialize_instructions_fill_an_empty_description_once():
    """ADR-032 §2: the first sentence of `instructions`, clipped to 160,
    lands in the row's `description` when the row has none."""
    rows = {"mcp.server.t": dict(ROW)}
    srv = ScriptedMcp(instructions="Echoes what you send, and tells the time.\n\n"
                                   "Use echo for text. Use now for the clock.")
    m = kernel(srv, rows)
    m.tools("t")
    assert rows["mcp.server.t"]["description"] == "Echoes what you send, and tells the time."
    assert rows["mcp.server.t"]["url"] == ROW["url"]          # the rest of the row kept
    assert m.servers()["servers"][0]["description"] == "Echoes what you send, and tells the time."
    inits = [r for r in srv.requests if r["json"].get("method") == "initialize"]
    assert len(inits) == 1                                     # the fill is not a re-init
    rows2 = {"mcp.server.t": dict(ROW)}
    kernel(ScriptedMcp(instructions=" ".join(["w" * 45] * 6)), rows2).tools("t")
    d = rows2["mcp.server.t"]["description"]
    assert len(d) <= 160 and d.endswith("…")


def test_a_written_description_wins_and_no_instructions_write_nothing():
    rows = {"mcp.server.t": dict(ROW, description="Mine.")}
    kernel(ScriptedMcp(instructions="Theirs."), rows).tools("t")
    assert rows["mcp.server.t"]["description"] == "Mine."
    rows = {"mcp.server.t": dict(ROW)}
    kernel(ScriptedMcp(), rows).tools("t")
    assert "description" not in rows["mcp.server.t"]


def test_raw_returns_the_result_verbatim_and_errors_with_code():
    srv = ScriptedMcp()
    m = kernel(srv)
    assert m.raw("t", "ping") == {"ok": True, "result": {}}
    out = m.raw("t", "resources/list", {"cursor": None})
    assert out["ok"] is False and out["code"] == -32601
    assert srv.requests[-1]["json"]["params"] == {"cursor": None}


def test_main_dispatches_for_anyrt_run():
    srv = ScriptedMcp()
    m = kernel(srv)
    assert m.main({"server": "t"})["tools"] == TOOLS
    assert m.main({"server": "t", "tool": "list_pages"})["text"] == "list_pages({})"
    assert m.main({"server": "t", "method": "ping"}) == {"ok": True, "result": {}}


@pytest.mark.parametrize("name", ["servers", "tools", "call", "raw"])
def test_public_methods_are_spans(name):
    """Deploy's tool rule (ADR-010 §4): every public method is a span."""
    src = (__import__("pathlib").Path(__file__).resolve().parents[1]
           / "repos" / "_agent" / "programs" / "mcp@v1" / "program.py").read_text()
    assert f'@span("mcp.{name}"' in src
