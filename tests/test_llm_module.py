"""programs/llm@v1 — model calls as a guest module, tested host-side by
exec-ing the guest source with fake `effect`/`span`/`use` globals (the
same seam the guest provides). Adapter translation asserted both ways;
the profile/backend tables (ADR-005 §1) asserted as pure transforms;
chat() asserted end-to-end: tier config resolution, provider URL, the
credential dict (ref + header ONLY — a key value never appears in the
syscall payload), the anthropic-version header, and the error path."""

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


def T(**over):
    """Generic traits with overrides — what a resolved profile yields."""
    return {**LLM["GENERIC_TRAITS"], **over}


CLAUDE = T(reasoning="roundtrip", cache="markers", max_output=32768)


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


def _openai_text(content, **msg):
    return {"choices": [{"message": {"content": content, **msg},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 9}}


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
        msgs, "SYS", [{"name": "run_cell"}], "claude-x", CLAUDE)
    assert req["system"] == [{"type": "text", "text": "SYS",
                              "cache_control": {"type": "ephemeral"}}]
    assert req["max_tokens"] == 32768
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
        [{"role": "assistant", "parts": [think]}], "", [], "m", CLAUDE)
    assert back["messages"][0]["content"][0] == signed
    # an advisory profile drops it from the wire
    back = LLM["AnthropicAdapter"]().build_request(
        [{"role": "assistant", "parts": [think, {"type": "text", "text": "ok"}]}],
        "", [], "m", T(reasoning="advisory"))
    assert [b["type"] for b in back["messages"][0]["content"]] == ["text"]


def test_openai_parse_and_build():
    r = LLM["OpenAICompatAdapter"]().parse_response(OPENAI_TOOL_RESP)
    assert r["stop"] == "tool"
    assert r["parts"][0]["args"] == {"code": "1+1"}
    assert r["usage"] == {"in": 90, "out": 20, "cacheRead": 0, "cacheWrite": 0}
    req = LLM["OpenAICompatAdapter"]().build_request(
        [{"role": "user", "parts": [{"type": "text", "text": "hi"}]}],
        "SYS", [{"name": "run_cell"}], "gpt-x", T())
    assert req["messages"][0] == {"role": "system", "content": "SYS"}
    assert req["tools"][0]["function"]["name"] == "run_cell"
    assert req["max_tokens"] == 8192


def test_openai_tool_only_assistant_turn_carries_null_content():
    msgs = [{"role": "assistant", "parts": [
        {"type": "tool_call", "id": "c1", "name": "run_cell", "args": {"code": "x"}}]}]
    req = LLM["OpenAICompatAdapter"]().build_request(msgs, "", [], "m", T())
    m = req["messages"][0]
    assert m["content"] is None and m["tool_calls"][0]["function"]["arguments"] == '{"code": "x"}'


def test_openai_malformed_tool_args_are_flagged_not_fatal():
    resp = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "run_cell", "arguments": '{"code": "print(1)'}}]},
        "finish_reason": "tool_calls"}], "usage": {}}
    r = LLM["OpenAICompatAdapter"]().parse_response(resp)
    call = r["parts"][0]
    assert r["stop"] == "tool" and call["args"] == {}
    assert "unparseable tool arguments" in call["error"]
    # round-tripping the flagged call still produces valid JSON on the wire
    back = LLM["OpenAICompatAdapter"]().build_request(
        [{"role": "assistant", "parts": [call]}], "", [], "m", T())
    assert back["messages"][0]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_anthropic_cache_breakpoint_on_conversation_end():
    msgs = [
        {"role": "user", "parts": [{"type": "text", "text": "hi"}]},
        {"role": "user", "parts": [
            {"type": "tool_result", "call_id": "t1", "content": "42",
             "is_error": False}]},
    ]
    req = LLM["AnthropicAdapter"]().build_request(msgs, "", [], "m", CLAUDE)
    assert "cache_control" not in req["messages"][0]["content"][-1]
    assert req["messages"][1]["content"][-1]["cache_control"] == \
        {"type": "ephemeral"}
    # cache: auto → no markers anywhere
    req = LLM["AnthropicAdapter"]().build_request(msgs, "S", [], "m", T())
    assert "cache_control" not in req["messages"][1]["content"][-1]
    assert req["system"] == [{"type": "text", "text": "S"}]


def test_anthropic_cache_breakpoint_skips_thinking_and_keeps_it_verbatim():
    signed = {"type": "thinking", "thinking": "hmm", "signature": "SIG"}
    msgs = [{"role": "assistant", "parts": [
        {"type": "text", "text": "ok"},
        {"type": "thinking", "text": "hmm", "provider_state": signed}]}]
    req = LLM["AnthropicAdapter"]().build_request(msgs, "", [], "m", CLAUDE)
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


def test_openai_reasoning_advisory_vs_roundtrip():
    resp = _openai_text("done", reasoning_content="let me think")
    r = LLM["OpenAICompatAdapter"]().parse_response(resp)
    assert r["parts"][0] == {"type": "thinking", "text": "let me think"}
    assert r["parts"][1] == {"type": "text", "text": "done"}
    hist = [{"role": "assistant", "parts": r["parts"]}]
    # advisory: the thinking part must NOT reach the wire
    back = LLM["OpenAICompatAdapter"]().build_request(hist, "", [], "m", T())
    assert back["messages"][0] == {"role": "assistant", "content": "done"}
    # roundtrip (DeepSeek thinking mode): resent as reasoning_content
    back = LLM["OpenAICompatAdapter"]().build_request(hist, "", [], "m", T(reasoning="roundtrip"))
    assert back["messages"][0]["reasoning_content"] == "let me think"


def test_openai_reasoning_details_roundtrip_opaque():
    details = [{"type": "reasoning.encrypted", "data": "XYZ"}]
    r = LLM["OpenAICompatAdapter"]().parse_response(
        _openai_text("ok", reasoning_details=details))
    assert r["parts"][0]["provider_state"] == {"reasoning_details": details}
    back = LLM["OpenAICompatAdapter"]().build_request(
        [{"role": "assistant", "parts": r["parts"]}], "", [], "m", T(reasoning="roundtrip"))
    assert back["messages"][0]["reasoning_details"] == details


# --- profile hooks: prepare / lift (ADR-005 §1.2) ---------------------------

def test_prepare_fenced_carries_the_tool_as_an_instruction():
    msgs, system, tools = LLM["_prepare"](
        [{"role": "user", "parts": [{"type": "text", "text": "hi"}]}],
        "SYS", [{"name": "run_cell"}], T(tool_mode="fenced"))
    assert tools == [] and system.startswith("SYS") and "```cell" in system


def test_prepare_first_user_folds_system_into_the_first_user_turn():
    src = [{"role": "user", "parts": [{"type": "file", "media_type": "image/png", "data": "x"},
                                      {"type": "text", "text": "hi"}]},
           {"role": "user", "parts": [{"type": "text", "text": "again"}]}]
    msgs, system, _ = LLM["_prepare"](src, "SYS", [], T(system_role="first_user"))
    assert system == ""
    assert msgs[0]["parts"][1]["text"] == "SYS\n\n---\n\nhi"
    assert msgs[1]["parts"][0]["text"] == "again"
    assert src[0]["parts"][1]["text"] == "hi"  # input untouched


def test_lift_fenced_extracts_code_and_emulates_tool():
    r = LLM["OpenAICompatAdapter"]().parse_response(_openai_text("Sure:\n```cell\nprint(1)\n```"))
    r = LLM["_lift"](r, T(tool_mode="fenced"))
    assert r["stop"] == "tool"
    assert r["parts"][-1]["args"] == {"code": "print(1)"}
    plain = LLM["_lift"](LLM["OpenAICompatAdapter"]().parse_response(_openai_text("All done.")),
                         T(tool_mode="fenced"))
    assert plain["stop"] == "done" and plain["parts"][0]["text"] == "All done."


def test_lift_xml_tool_calls_and_think_tags():
    text = ("<think>plan it</think>Running.\n<tool_call>\n"
            '{"name": "run_cell", "arguments": {"code": "2*2"}}\n</tool_call>')
    r = LLM["_lift"](LLM["OpenAICompatAdapter"]().parse_response(_openai_text(text)),
                     T(tool_mode="xml"))
    kinds = [p["type"] for p in r["parts"]]
    assert kinds == ["thinking", "text", "tool_call"] and r["stop"] == "tool"
    assert r["parts"][0]["text"] == "plan it"
    assert r["parts"][1]["text"] == "Running."
    assert r["parts"][2]["args"] == {"code": "2*2"}
    bad = LLM["_lift"](LLM["OpenAICompatAdapter"]().parse_response(
        _openai_text("<tool_call>{not json}</tool_call>")), T(tool_mode="xml"))
    assert bad["stop"] == "tool" and "unparseable" in bad["parts"][0]["error"]


def test_lift_native_leaves_text_alone():
    r = LLM["_lift"](LLM["OpenAICompatAdapter"]().parse_response(
        _openai_text("```cell\nx\n```")), T())
    assert r["stop"] == "done" and r["parts"][0]["type"] == "text"


# --- profiles + traits (ADR-005 §1.3 / §1.7) ---------------------------------

def test_every_profile_declares_only_known_traits():
    for name, entry in LLM["PROFILES"].items():
        LLM["_check_traits"]({**LLM["GENERIC_TRAITS"], **entry["traits"]}, name)
    assert set(LLM["GENERIC_TRAITS"]) == set(LLM["TRAITS"])


def test_unknown_trait_or_value_is_a_config_error():
    with pytest.raises(LLM["ConfigError"]):
        LLM["_check_traits"]({"sparkle": True}, "x")
    with pytest.raises(LLM["ConfigError"]):
        LLM["_check_traits"]({"tool_mode": "telepathy"}, "x")
    with pytest.raises(LLM["ConfigError"]):
        LLM["_check_traits"]({"context_window": "big"}, "x")


@pytest.mark.parametrize("model,profile", [
    ("claude-sonnet-5", "claude"), ("gpt-4.1-mini", "gpt"), ("o3", "gpt"),
    ("gemini-3.7-flash", "gemini"), ("deepseek-reasoner", "deepseek-r1"),
    ("deepseek-chat", "deepseek"), ("Qwen/Qwen3-32B", "qwen3"),
    ("meta-llama/llama-4-maverick", "llama"), ("gemma-3-27b-it", "gemma"),
    ("mistral-large", "mistral"), ("z-ai/glm-5.1", "glm"), ("mystery-7b", "generic"),
])
def test_profile_matches_on_model_name(model, profile):
    assert LLM["_profile_name"]({"model": model}) == profile


def test_explicit_profile_wins_and_unknown_fails():
    assert LLM["_profile_name"]({"model": "claude-x", "profile": "fenced"}) == "fenced"
    with pytest.raises(LLM["ConfigError"]):
        LLM["_profile_name"]({"model": "m", "profile": "nope"})


def test_backend_inferred_from_host_or_provider():
    bn = LLM["_backend_name"]
    assert bn({"provider": "anthropic", "base_url": "https://api.anthropic.com"}) == "anthropic"
    assert bn({"provider": "openai-compat",
               "base_url": "https://openrouter.ai/api/v1"}) == "openrouter"
    assert bn({"provider": "openai-compat", "base_url": "https://api.openai.com/v1"}) == "openai"
    assert bn({"provider": "openai-compat",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"}) == "gemini"
    assert bn({"provider": "openai-compat", "base_url": "http://localhost:11434/v1"}) == "generic"
    assert bn({"provider": "openai-compat", "base_url": "http://l:1", "backend": "vllm"}) == "vllm"
    with pytest.raises(LLM["ConfigError"]):
        bn({"provider": "openai-compat", "base_url": "http://l:1", "backend": "nope"})


# --- backends: finish / normalize (pure) -------------------------------------

def _req():
    return {"model": "m", "max_tokens": 100, "messages": [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "tool_call_id": "c", "content": "42"}]}


def test_backend_openai_spells_max_completion_tokens_and_effort():
    r = LLM["BACKENDS"]["openai"]["finish"](_req(), T(thinking="on"))
    assert "max_tokens" not in r and r["max_completion_tokens"] == 100
    assert r["reasoning_effort"] == "high"
    assert "reasoning_effort" not in LLM["BACKENDS"]["openai"]["finish"](_req(), T())


def test_backend_openrouter_cache_markers_and_thinking():
    r = LLM["BACKENDS"]["openrouter"]["finish"](_req(), T(cache="markers", thinking="off"))
    assert r["reasoning"] == {"enabled": False}
    assert r["messages"][0]["content"] == [{"type": "text", "text": "SYS",
                                            "cache_control": {"type": "ephemeral"}}]
    # the last user/assistant text carries the second breakpoint — the
    # tool message and the null-content assistant turn are skipped
    assert r["messages"][1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in json.dumps(r["messages"][2:])
    plain = LLM["BACKENDS"]["openrouter"]["finish"](_req(), T())
    assert plain["messages"][0]["content"] == "SYS" and "reasoning" not in plain


def test_backend_anthropic_thinking_budget():
    r = LLM["BACKENDS"]["anthropic"]["finish"]({"max_tokens": 4000}, T(thinking="on"))
    assert r["thinking"] == {"type": "enabled", "budget_tokens": 3999}
    assert "thinking" not in LLM["BACKENDS"]["anthropic"]["finish"]({"max_tokens": 4000}, T())


def test_backend_chat_template_thinking_switch():
    r = LLM["BACKENDS"]["vllm"]["finish"](_req(), T(thinking="off"))
    assert r["chat_template_kwargs"] == {"enable_thinking": False}
    r = LLM["BACKENDS"]["llamacpp"]["finish"](_req(), T())
    assert r["cache_prompt"] is True and "chat_template_kwargs" not in r


def test_normalize_unifies_reasoning_and_deepseek_cache_fields():
    raw = {"choices": [{"message": {"content": "x", "reasoning": "r"}}],
           "usage": {"prompt_tokens": 10, "prompt_cache_hit_tokens": 7}}
    out = LLM["_normalize_openai_compat"](raw)
    assert out["choices"][0]["message"]["reasoning_content"] == "r"
    assert out["usage"]["prompt_tokens_details"]["cached_tokens"] == 7
    r = LLM["OpenAICompatAdapter"]().parse_response({**out, "choices": [
        {**out["choices"][0], "finish_reason": "stop"}]})
    assert r["usage"]["cacheRead"] == 7


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
    cred = post["credential"]
    assert (cred["ref"], cred["header"]) == ("llm.keys.anthropic", "x-api-key")
    # ADR-021 §1: the descriptor the host shows when the key is missing
    assert cred["about"]["label"] == "Anthropic API key"
    assert cred["about"]["hosts"] == ["api.example"]
    assert post["json"]["model"] == "claude-x"
    # the claude profile matched: markers + its output cap
    assert post["json"]["system"][0] == {"type": "text", "text": "SYS",
                                         "cache_control": {"type": "ephemeral"}}
    assert post["json"]["max_tokens"] == 32768
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
    cred = post["credential"]
    assert (cred["ref"], cred["header"], cred["prefix"]) == (
        "llm.keys.local", "Authorization", "Bearer ")
    assert cred["about"]["hosts"] == ["localhost:8000"]
    assert cred["about"]["label"] == "localhost:8000 API key"
    assert reply["stop"] == "tool"
    assert reply["usage"] == {"in": 90, "out": 20, "cacheRead": 0,
                              "cacheWrite": 0}


def test_chat_null_key_ref_sends_no_credential():
    host = FakeHost({"provider": "openai-compat", "model": "qwen3-8b",
                     "base_url": "http://localhost:11434/v1", "api_key_ref": None},
                    body=_openai_text("ok"))
    load(host)["chat"](MSGS)
    assert "credential" not in host.posts[0]


def test_chat_backend_and_options_shape_the_request():
    host = FakeHost({"provider": "openai-compat", "model": "deepseek-reasoner",
                     "base_url": "https://api.deepseek.com", "api_key_ref": "k",
                     "options": {"top_p": 0.9, "max_tokens": 777}},
                    body=_openai_text("ok", reasoning_content="r"))
    load(host)["chat"](MSGS, system="SYS", tools=[{"name": "run_cell"}])
    req = host.posts[0]["json"]
    assert host.posts[0]["credential"]["about"]["label"] == "DeepSeek API key"
    # deepseek-r1 profile: no system role, the model card's temperature;
    # options merge last and win
    assert req["messages"][0]["role"] == "user"
    assert req["messages"][0]["content"].startswith("SYS\n\n---\n\nhi")
    assert req["temperature"] == 0.6 and req["top_p"] == 0.9 and req["max_tokens"] == 777


def test_chat_fenced_profile_emulates_tool_call():
    host = FakeHost({"provider": "openai-compat", "model": "small",
                     "base_url": "http://l:1", "api_key_ref": "k", "profile": "fenced"},
                    body=_openai_text("```cell\n1+1\n```"))
    reply = load(host)["chat"](MSGS, tools=[{"name": "run_cell"}])
    assert reply["stop"] == "tool" and reply["parts"][0]["name"] == "run_cell"
    # the fenced instruction rode the system prompt; no tools on the wire
    assert "```cell" in host.posts[0]["json"]["messages"][0]["content"]
    assert "tools" not in host.posts[0]["json"]


def test_chat_max_tokens_arg_overrides_profile_cap():
    host = FakeHost({"provider": "openai-compat", "model": "gpt-4.1",
                     "base_url": "https://api.openai.com/v1", "api_key_ref": "k"},
                    body=_openai_text("ok"))
    load(host)["chat"](MSGS, max_tokens=64)
    assert host.posts[0]["json"]["max_completion_tokens"] == 64


def test_profile_resolves_for_the_loop():
    host = FakeHost({"provider": "openai-compat", "model": "gemma-3-27b",
                     "base_url": "http://l:1", "api_key_ref": None})
    p = load(host)["profile"]("codegen")
    assert p["profile"] == "gemma" and p["backend"] == "generic"
    assert p["traits"]["tool_mode"] == "fenced" and p["traits"]["prompt_style"] == "compact"
    assert host.posts == []


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
    img = a.build_request(_file_msg("image/png"), "", [], "m", CLAUDE)["messages"][0]["content"]
    assert img[0]["type"] == "image"
    assert img[0]["source"]["data"] == PNG_B64
    pdf = a.build_request(_file_msg("application/pdf"), "", [], "m",
                          CLAUDE)["messages"][0]["content"]
    assert pdf[0]["type"] == "document" and pdf[0]["source"]["media_type"] == "application/pdf"
    txt = a.build_request(_file_msg("text/markdown; charset=utf-8", TXT_B64, "notes.md"),
                          "", [], "m", CLAUDE)["messages"][0]["content"]
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
                                  "wordprocessingml.document"), "", [], "m", T())
    assert "anthropic cannot read" in str(e.value)
    o = LLM["OpenAICompatAdapter"]()
    with pytest.raises(LLM["UnsupportedMedia"]):
        o.build_request(_file_msg("application/pdf"), "", [], "m", T())
    # images ride the openai wire as a data URI
    req = o.build_request(_file_msg("image/png"), "", [], "m", T())
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
