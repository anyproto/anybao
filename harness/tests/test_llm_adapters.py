"""Adapter translation — pure, offline. Recorded response shapes ->
neutral parts, and neutral messages -> provider request. One real call
per provider seeds these fixtures (harness/tests/fixtures/llm_*.json);
here we assert the mapping both ways.
"""

from anybao.llm import AnthropicAdapter, FencedAdapter, OpenAICompatAdapter

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


def test_anthropic_parse_tool():
    r = AnthropicAdapter().parse_response(ANTHROPIC_TOOL_RESP)
    assert r["stop"] == "tool"
    assert r["parts"][0] == {"type": "text", "text": "Let me compute."}
    assert r["parts"][1] == {"type": "tool_call", "id": "toolu_1",
                             "name": "run_cell", "args": {"code": "1+1"}}
    assert r["usage"] == {"in": 100, "out": 30}


def test_anthropic_parse_done_and_length():
    assert AnthropicAdapter().parse_response(ANTHROPIC_DONE_RESP)["stop"] == "done"
    maxed = {**ANTHROPIC_DONE_RESP, "stop_reason": "max_tokens"}
    assert AnthropicAdapter().parse_response(maxed)["stop"] == "length"


def test_anthropic_build_request_roundtrips_blocks():
    msgs = [
        {"role": "user", "parts": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "parts": [
            {"type": "tool_call", "id": "t1", "name": "run_cell", "args": {"code": "x"}}]},
        {"role": "user", "parts": [
            {"type": "tool_result", "call_id": "t1", "content": "42", "is_error": False}]},
    ]
    req = AnthropicAdapter().build_request(msgs, "SYS", [{"name": "run_cell"}], "claude-x")
    assert req["system"] == "SYS" and req["tools"][0]["name"] == "run_cell"
    assert req["messages"][1]["content"][0]["type"] == "tool_use"
    assert req["messages"][2]["content"][0]["type"] == "tool_result"


def test_anthropic_thinking_roundtrip_verbatim():
    signed = {"type": "thinking", "thinking": "hmm", "signature": "SIG"}
    resp = {"content": [signed, {"type": "text", "text": "ok"}],
            "stop_reason": "end_turn", "usage": {}}
    parsed = AnthropicAdapter().parse_response(resp)
    think = parsed["parts"][0]
    assert think["type"] == "thinking" and think["provider_state"] == signed
    # feeding it back reproduces the exact signed block
    back = AnthropicAdapter().build_request(
        [{"role": "assistant", "parts": [think]}], "", [], "m")
    assert back["messages"][0]["content"][0] == signed


def test_openai_parse_and_build():
    r = OpenAICompatAdapter().parse_response(OPENAI_TOOL_RESP)
    assert r["stop"] == "tool"
    assert r["parts"][0]["args"] == {"code": "1+1"}
    assert r["usage"] == {"in": 90, "out": 20}
    req = OpenAICompatAdapter().build_request(
        [{"role": "user", "parts": [{"type": "text", "text": "hi"}]}],
        "SYS", [{"name": "run_cell"}], "gpt-x")
    assert req["messages"][0] == {"role": "system", "content": "SYS"}
    assert req["tools"][0]["function"]["name"] == "run_cell"


def test_fenced_extracts_code_and_emulates_tool():
    inner = OpenAICompatAdapter()
    fa = FencedAdapter(inner)
    resp = {"choices": [{"message": {"content": "Sure:\n```cell\nprint(1)\n```"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 9}}
    r = fa.parse_response(resp)
    assert r["stop"] == "tool"
    assert r["parts"][0]["args"] == {"code": "print(1)"}


def test_fenced_plain_text_is_done():
    inner = OpenAICompatAdapter()
    resp = {"choices": [{"message": {"content": "All done."},
                         "finish_reason": "stop"}], "usage": {}}
    r = FencedAdapter(inner).parse_response(resp)
    assert r["stop"] == "done" and r["parts"][0]["text"] == "All done."


def test_llm_effect_wires_adapter_and_transport():
    """The effect end-to-end with a fake transport (no network) — proves
    config->adapter->transport->parse. Real seeding replaces the fake
    transport with one live call; see docs/llm-fixtures.md."""
    from anybao.llm import register_llm_effect
    from anyrt import trace as tr
    from anyrt.effects import Broker, Registry

    def config(key):
        assert key == "llm.tier.codegen"
        return {"provider": "anthropic", "model": "claude-x", "base_url": "x", "api_key_ref": "k"}

    seen = {}

    def transport(prov, req):
        seen["req"] = req
        return ANTHROPIC_TOOL_RESP

    reg = Registry()
    register_llm_effect(reg, transport=transport, config=config)
    broker = Broker(reg, tr.TraceWriter(run={"id": "l"}))
    reply = broker.call("llm.chat", {
        "messages": [{"role": "user", "parts": [{"type": "text", "text": "hi"}]}],
        "tier": "codegen", "tools": [{"name": "run_cell"}]})
    assert reply["stop"] == "tool" and reply["parts"][1]["args"] == {"code": "1+1"}
    assert seen["req"]["model"] == "claude-x"  # adapter built the request
