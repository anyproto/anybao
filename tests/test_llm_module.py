"""programs/llm@v1 — model calls as a guest module, tested host-side by
exec-ing the guest source with fake `effect`/`span`/`use` globals (the
same seam the guest provides). Adapter translation asserted both ways
(ported from test_llm_adapters); chat() asserted end-to-end: tier
config resolution, provider URL, the credential dict (ref + header
ONLY — a key value never appears in the syscall payload), the
anthropic-version header, and the error path."""

import base64
import json
from pathlib import Path

import pytest

PROGRAMS_DIR = Path(__file__).resolve().parents[1] / "repos" / "_agent" / "programs"
SRC = (PROGRAMS_DIR / "llm@v1" / "program.py").read_text()


def load(effect=None):
    g = {
        "effect": effect or (lambda name, payload: pytest.fail(f"unexpected effect {name!r}")),
        "span": lambda name=None, kind=None: (lambda f: f),
        "use": lambda spec: pytest.fail(f"unexpected use({spec!r})"),
    }
    exec(compile(SRC, "llm@v1.py", "exec"), g)
    return g


LLM = load()  # adapters are pure — one exec serves all adapter tests


# --- fixtures: minimal recorded response shapes (seed from a real call) -----

ANTHROPIC_TOOL_RESP = {
    "content": [
        {"type": "text", "text": "Let me compute."},
        {"type": "tool_use", "id": "toolu_1", "name": "run_cell",
         "input": {"code": "1+1"}},
    ],
    "stop_reason": "tool_use",
    "usage": {"input_tokens": 100, "output_tokens": 30},
}

ANTHROPIC_DONE_RESP = {
    "content": [{"type": "text", "text": "The answer is 2."}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 120, "output_tokens": 8},
}

OPENAI_TOOL_RESP = {
    "choices": [{
        "message": {"content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "run_cell", "arguments": '{"code": "1+1"}'}}]},
        "finish_reason": "tool_calls",
    }],
    "usage": {"prompt_tokens": 90, "completion_tokens": 20},
}


# --- adapter translation (pure, offline) -------------------------------------

def test_anthropic_parse_tool():
    r = LLM["AnthropicAdapter"]().parse_response(ANTHROPIC_TOOL_RESP)
    assert r["stop"] == "tool"
    assert r["parts"][0] == {"type": "text", "text": "Let me compute."}
    assert r["parts"][1] == {"type": "tool_call", "id": "toolu_1",
                             "name": "run_cell", "args": {"code": "1+1"}}
    assert r["usage"] == {"in": 100, "out": 30, "cacheRead": 0, "cacheWrite": 0}


def test_anthropic_parse_done_and_length():
    assert LLM["AnthropicAdapter"]().parse_response(ANTHROPIC_DONE_RESP)["stop"] == "done"
    maxed = {**ANTHROPIC_DONE_RESP, "stop_reason": "max_tokens"}
    assert LLM["AnthropicAdapter"]().parse_response(maxed)["stop"] == "length"


def test_anthropic_build_request_roundtrips_blocks():
    msgs = [
        {"role": "user", "parts": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "parts": [
            {"type": "tool_call", "id": "t1", "name": "run_cell", "args": {"code": "x"}}]},
        {"role": "user", "parts": [
            {"type": "tool_result", "call_id": "t1", "content": "42", "is_error": False}]},
    ]
    req = LLM["AnthropicAdapter"]().build_request(
        msgs, "SYS", [{"name": "run_cell"}], "claude-x")
    assert req["system"] == [{"type": "text", "text": "SYS",
                              "cache_control": {"type": "ephemeral"}}]
    assert req["tools"][0]["name"] == "run_cell"
    assert req["messages"][1]["content"][0]["type"] == "tool_use"
    assert req["messages"][2]["content"][0]["type"] == "tool_result"


def test_anthropic_thinking_roundtrip_verbatim():
    signed = {"type": "thinking", "thinking": "hmm", "signature": "SIG"}
    resp = {"content": [signed, {"type": "text", "text": "ok"}],
            "stop_reason": "end_turn", "usage": {}}
    parsed = LLM["AnthropicAdapter"]().parse_response(resp)
    think = parsed["parts"][0]
    assert think["type"] == "thinking" and think["provider_state"] == signed
    # feeding it back reproduces the exact signed block
    back = LLM["AnthropicAdapter"]().build_request(
        [{"role": "assistant", "parts": [think]}], "", [], "m")
    assert back["messages"][0]["content"][0] == signed


def test_openai_parse_and_build():
    r = LLM["OpenAICompatAdapter"]().parse_response(OPENAI_TOOL_RESP)
    assert r["stop"] == "tool"
    assert r["parts"][0]["args"] == {"code": "1+1"}
    assert r["usage"] == {"in": 90, "out": 20, "cacheRead": 0, "cacheWrite": 0}
    req = LLM["OpenAICompatAdapter"]().build_request(
        [{"role": "user", "parts": [{"type": "text", "text": "hi"}]}],
        "SYS", [{"name": "run_cell"}], "gpt-x")
    assert req["messages"][0] == {"role": "system", "content": "SYS"}
    assert req["tools"][0]["function"]["name"] == "run_cell"


def test_anthropic_cache_breakpoint_on_conversation_end():
    msgs = [
        {"role": "user", "parts": [{"type": "text", "text": "hi"}]},
        {"role": "user", "parts": [
            {"type": "tool_result", "call_id": "t1", "content": "42",
             "is_error": False}]},
    ]
    req = LLM["AnthropicAdapter"]().build_request(msgs, "", [], "m")
    assert "cache_control" not in req["messages"][0]["content"][-1]
    assert req["messages"][1]["content"][-1]["cache_control"] == \
        {"type": "ephemeral"}


def test_anthropic_cache_breakpoint_skips_thinking_and_keeps_it_verbatim():
    signed = {"type": "thinking", "thinking": "hmm", "signature": "SIG"}
    msgs = [{"role": "assistant", "parts": [
        {"type": "text", "text": "ok"},
        {"type": "thinking", "text": "hmm", "provider_state": signed}]}]
    req = LLM["AnthropicAdapter"]().build_request(msgs, "", [], "m")
    blocks = req["messages"][0]["content"]
    assert blocks[1] == signed and "cache_control" not in blocks[1]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert signed == {"type": "thinking", "thinking": "hmm",
                      "signature": "SIG"}  # shared dict never mutated


def test_openai_cached_tokens_surface_as_cache_read():
    resp = {"choices": [{"message": {"content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 5,
                      "prompt_tokens_details": {"cached_tokens": 80}}}
    r = LLM["OpenAICompatAdapter"]().parse_response(resp)
    assert r["usage"] == {"in": 100, "out": 5, "cacheRead": 80,
                         "cacheWrite": 0}


def test_openai_reasoning_captured_but_not_resent():
    resp = {"choices": [{"message": {"content": "done",
                                     "reasoning_content": "let me think"},
                         "finish_reason": "stop"}], "usage": {}}
    r = LLM["OpenAICompatAdapter"]().parse_response(resp)
    assert r["parts"][0] == {"type": "thinking", "text": "let me think"}
    assert r["parts"][1] == {"type": "text", "text": "done"}
    # feeding the reply back: the thinking part must NOT reach the wire
    back = LLM["OpenAICompatAdapter"]().build_request(
        [{"role": "assistant", "parts": r["parts"]}], "", [], "m")
    assert back["messages"][0] == {"role": "assistant", "content": "done"}


def test_fenced_extracts_code_and_emulates_tool():
    fa = LLM["FencedAdapter"](LLM["OpenAICompatAdapter"]())
    resp = {"choices": [{"message": {"content": "Sure:\n```cell\nprint(1)\n```"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 9}}
    r = fa.parse_response(resp)
    assert r["stop"] == "tool"
    assert r["parts"][0]["args"] == {"code": "print(1)"}


def test_fenced_plain_text_is_done():
    resp = {"choices": [{"message": {"content": "All done."},
                         "finish_reason": "stop"}], "usage": {}}
    r = LLM["FencedAdapter"](LLM["OpenAICompatAdapter"]()).parse_response(resp)
    assert r["stop"] == "done" and r["parts"][0]["text"] == "All done."


def test_build_adapter_selects_and_fences():
    assert isinstance(LLM["_build_adapter"]("anthropic", False), LLM["AnthropicAdapter"])
    fenced = LLM["_build_adapter"]("openai-compat", True)
    assert isinstance(fenced, LLM["FencedAdapter"])


# --- chat(): tier config -> syscall -> parsed reply --------------------------

class FakeHost:
    """Answers config.get / http.post like the thin host."""

    def __init__(self, prov, status=200, body=None):
        self.prov = prov
        self.status = status
        self.body = body if isinstance(body, str) else json.dumps(body or {})
        self.config_keys = []
        self.posts = []

    def __call__(self, name, payload):
        if name == "config.get":
            self.config_keys.append(payload["key"])
            return {"value": self.prov}
        assert name == "http.post", name
        self.posts.append(payload)
        return {"status": self.status, "headers": {}, "body": self.body}


MSGS = [{"role": "user", "parts": [{"type": "text", "text": "hi"}]}]


def test_chat_anthropic_url_credential_and_parse():
    host = FakeHost({"provider": "anthropic", "model": "claude-x",
                     "base_url": "https://api.example/",
                     "api_key_ref": "llm.keys.anthropic"},
                    body=ANTHROPIC_TOOL_RESP)
    reply = load(host)["chat"](MSGS, system="SYS", tier="codegen",
                               tools=[{"name": "run_cell"}])
    assert host.config_keys == ["llm.tier.codegen"]
    post = host.posts[0]
    assert post["url"] == "https://api.example/v1/messages"
    assert post["headers"]["anthropic-version"] == "2023-06-01"
    # the credential names the ref + header — never a key value
    assert post["credential"] == {"ref": "llm.keys.anthropic", "header": "x-api-key"}
    assert post["json"]["model"] == "claude-x"
    assert post["json"]["system"][0]["text"] == "SYS"
    assert reply["stop"] == "tool" and reply["parts"][1]["args"] == {"code": "1+1"}


def test_chat_openai_compat_url_and_bearer_credential():
    host = FakeHost({"provider": "openai-compat", "model": "gpt-x",
                     "base_url": "http://localhost:8000/v1",
                     "api_key_ref": "llm.keys.local"},
                    body=OPENAI_TOOL_RESP)
    reply = load(host)["chat"](MSGS, tier="classify")
    assert host.config_keys == ["llm.tier.classify"]
    post = host.posts[0]
    assert post["url"] == "http://localhost:8000/v1/chat/completions"
    assert post["credential"] == {"ref": "llm.keys.local",
                                  "header": "Authorization", "prefix": "Bearer "}
    assert reply["stop"] == "tool"
    assert reply["usage"] == {"in": 90, "out": 20, "cacheRead": 0,
                              "cacheWrite": 0}


def test_chat_fenced_tier_emulates_tool_call():
    host = FakeHost({"provider": "openai-compat", "model": "small",
                     "base_url": "http://l:1", "api_key_ref": "k", "fenced": True},
                    body={"choices": [{"message": {"content": "```cell\n1+1\n```"},
                                       "finish_reason": "stop"}], "usage": {}})
    reply = load(host)["chat"](MSGS)
    assert reply["stop"] == "tool" and reply["parts"][0]["name"] == "run_cell"
    # the fenced instruction rode the system prompt; no tools on the wire
    assert "```cell" in host.posts[0]["json"]["messages"][0]["content"]
    assert "tools" not in host.posts[0]["json"]


def test_chat_error_status_raises_llm_error_with_excerpt():
    host = FakeHost({"provider": "anthropic", "model": "m",
                     "base_url": "https://x", "api_key_ref": "k"},
                    status=500, body="upstream exploded" + "x" * 1000)
    g = load(host)
    with pytest.raises(g["LlmError"]) as e:
        g["chat"](MSGS)
    assert e.value.status == 500
    assert "upstream exploded" in str(e.value)
    assert len(e.value.body) <= 400  # excerpt, not the whole body


# --- File parts (ADR-020 §3/§4) ----------------------------------------------

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
TXT_B64 = base64.b64encode(b"hello, file").decode()


def _file_msg(media_type, data=PNG_B64, name=None):
    part = {"type": "file", "media_type": media_type, "data": data}
    if name:
        part["name"] = name
    return [{"role": "user", "parts": [part, {"type": "text", "text": "what is it?"}]}]


def test_anthropic_routes_file_parts_by_media_type():
    a = LLM["AnthropicAdapter"]()
    img = a.build_request(_file_msg("image/png"), "", [], "m")["messages"][0]["content"]
    assert img[0] == {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                  "data": PNG_B64},
                      "cache_control": {"type": "ephemeral"}} or img[0]["type"] == "image"
    assert img[0]["source"]["data"] == PNG_B64
    pdf = a.build_request(_file_msg("application/pdf"), "", [], "m")["messages"][0]["content"]
    assert pdf[0]["type"] == "document" and pdf[0]["source"]["media_type"] == "application/pdf"
    txt = a.build_request(_file_msg("text/markdown; charset=utf-8", TXT_B64, "notes.md"),
                          "", [], "m")["messages"][0]["content"]
    # text source is DECODED text, not base64; the name becomes the title
    assert txt[0]["source"] == {"type": "text", "media_type": "text/plain",
                                "data": "hello, file"}
    assert txt[0]["title"] == "notes.md"
    # the prompt follows the file; cache breakpoint lands on the last block
    assert txt[1]["type"] == "text" and "cache_control" in txt[1]


def test_unsupported_media_raises_before_any_call():
    a = LLM["AnthropicAdapter"]()
    with pytest.raises(LLM["UnsupportedMedia"]) as e:
        a.build_request(_file_msg("application/vnd.openxmlformats-officedocument."
                                  "wordprocessingml.document"), "", [], "m")
    assert "anthropic cannot read" in str(e.value)
    o = LLM["OpenAICompatAdapter"]()
    with pytest.raises(LLM["UnsupportedMedia"]):
        o.build_request(_file_msg("application/pdf"), "", [], "m")
    # images ride the openai wire as a data URI
    req = o.build_request(_file_msg("image/png"), "", [], "m")
    content = req["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "what is it?"}
    assert content[1]["image_url"]["url"] == f"data:image/png;base64,{PNG_B64}"


def test_read_resolves_ref_through_any_and_uses_vision_tier():
    host = FakeHost({"provider": "anthropic", "model": "eyes",
                     "base_url": "https://api.example", "api_key_ref": "k"},
                    body={"content": [{"type": "text", "text": "a cat"}],
                          "stop_reason": "end_turn", "usage": {}})
    fetched = []

    class FakeAny:
        def file_content(self, space, file):
            fetched.append((space, file))
            return {"fileId": "f1", "mime": "image/png", "size": 8, "data": PNG_B64}

    g = load(host)
    g["use"] = lambda spec: FakeAny() if spec == "any@v1" else pytest.fail(spec)
    out = g["read"]("any://f/sp1/f1", "what is it?")
    assert out == "a cat"
    assert fetched == [("sp1", "any://f/sp1/f1")]
    assert host.config_keys == ["llm.tier.vision"]
    content = host.posts[0]["json"]["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[1]["text"] == "what is it?"
    # an already-fetched dict skips any@v1
    assert g["read"]({"mime": "image/png", "data": PNG_B64}, "again?") == "a cat"
    assert len(fetched) == 1
    with pytest.raises(TypeError):
        g["read"]("bare-file-id", "?")
