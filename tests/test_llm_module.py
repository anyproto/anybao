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


BLOBS = {}   # the fake blob directory behind the kernel's blob.* effects


def _kernel_effect(name, payload):
    import base64
    import hashlib
    if name == "blob.put":
        raw = base64.b64decode(payload["data"])
        h = "sha256:" + hashlib.sha256(raw).hexdigest()
        BLOBS[h] = raw
        return {"__blob": h, "bytes": len(raw), "mime": payload["mime"]}
    if name == "blob.read":
        raw = BLOBS[payload["hash"]][payload["offset"]:payload["offset"] + payload["length"]]
        return {"data": base64.b64encode(raw).decode(), "bytes": len(raw)}
    pytest.fail(f"unexpected kernel effect {name!r}")


class EffectError(Exception):
    """What the kernel raises for a failed effect: `<type>: <message>`
    (runtime/guest/app.py) — bound as the guest global of that name."""


def load(effect=None):
    from kernelenv import load_kernel
    k = load_kernel(effect=_kernel_effect)
    g = {
        "effect": effect or (lambda name, payload: pytest.fail(f"unexpected effect {name!r}")),
        "span": lambda name=None, kind=None: (lambda f: f),
        "use": lambda spec: pytest.fail(f"unexpected use({spec!r})"),
        "Blob": k.Blob, "blob": k.blob, "EffectError": EffectError,
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
    # an UNSIGNED thinking part (another provider's, or lifted from
    # <think>) never reaches the wire — Anthropic requires the signature
    back = LLM["AnthropicAdapter"]().build_request(
        [{"role": "assistant", "parts": [{"type": "thinking", "text": "kimi's"},
                                          {"type": "text", "text": "ok"}]}],
        "", [], "m", CLAUDE)
    assert [b["type"] for b in back["messages"][0]["content"]] == ["text"]
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


def test_openai_text_after_tool_results_survives_as_a_user_message():
    # the wrap-up shape (ADR-005 §2): synthetic error results for the
    # dangling calls, then the summarize instruction — one neutral message
    msgs = [{"role": "user", "parts": [
        {"type": "tool_result", "call_id": "c1", "content": "not executed: length",
         "is_error": True},
        {"type": "text", "text": "[length] No more cells. Summarize."}]}]
    wire = LLM["OpenAICompatAdapter"]().build_request(msgs, "", [], "m", T())["messages"]
    assert [m["role"] for m in wire] == ["tool", "user"]
    assert wire[0]["tool_call_id"] == "c1"
    assert wire[1]["content"] == "[length] No more cells. Summarize."
    # results alone stay results alone
    only = [{"role": "user", "parts": [msgs[0]["parts"][0]]}]
    wire = LLM["OpenAICompatAdapter"]().build_request(only, "", [], "m", T())["messages"]
    assert [m["role"] for m in wire] == ["tool"]


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


def test_openai_tool_call_provider_state_roundtrips_verbatim():
    extra = {"google": {"thought_signature": "SIG"}}
    resp = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "c1", "type": "function", "extra_content": extra,
         "function": {"name": "run_cell", "arguments": "{}"}}]},
        "finish_reason": "tool_calls"}], "usage": {}}
    r = LLM["OpenAICompatAdapter"]().parse_response(resp)
    assert r["parts"][0]["provider_state"] == {"extra_content": extra}
    back = LLM["OpenAICompatAdapter"]().build_request(
        [{"role": "assistant", "parts": r["parts"]}], "", [], "m", T())
    assert back["messages"][0]["tool_calls"][0]["extra_content"] == extra
    # a call the server sent without state carries none back
    plain = LLM["OpenAICompatAdapter"]().parse_response(OPENAI_TOOL_RESP)
    back = LLM["OpenAICompatAdapter"]().build_request(
        [{"role": "assistant", "parts": plain["parts"]}], "", [], "m", T())
    assert "extra_content" not in back["messages"][0]["tool_calls"][0]


def test_markers_degrade_to_auto_off_marker_backends():
    eff = LLM["_effective"]
    assert eff(T(cache="markers"), "anthropic")["cache"] == "markers"
    assert eff(T(cache="markers"), "openrouter")["cache"] == "markers"
    assert eff(T(cache="markers"), "generic")["cache"] == "auto"
    assert eff(T(cache="auto"), "anthropic")["cache"] == "auto"
    host = FakeHost({"provider": "openai-compat", "model": "claude-sonnet-5",
                     "base_url": "https://api.anthropic.com/v1", "api_key_ref": "k"})
    assert load(host)["profile"]()["traits"]["cache"] == "auto"


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
    # `in` is the uncached prompt: prompt_tokens minus the cached part
    assert r["usage"] == {"in": 20, "out": 5, "cacheRead": 80,
                         "cacheWrite": 0}
    # OpenRouter's explicit-marker write shows up as cacheWrite
    resp["usage"]["prompt_tokens_details"] = {"cached_tokens": 0, "cache_write_tokens": 95}
    r = LLM["OpenAICompatAdapter"]().parse_response(resp)
    assert r["usage"] == {"in": 5, "out": 5, "cacheRead": 0, "cacheWrite": 95}


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

def test_prepare_text_only_model_refuses_files_before_any_call():
    msgs = [{"role": "user", "parts": [{"type": "file", "media_type": "image/png", "data": "x"},
                                       {"type": "text", "text": "?"}]}]
    with pytest.raises(LLM["UnsupportedMedia"]) as e:
        LLM["_prepare"](msgs, "", [], T(vision=False))
    assert "text-only" in str(e.value)
    LLM["_prepare"](msgs, "", [], T())  # a vision model passes them through
    with pytest.raises(LLM["ConfigError"]):
        LLM["_check_traits"]({"vision": "yes"}, "x")


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
    two = LLM["_lift"](LLM["OpenAICompatAdapter"]().parse_response(_openai_text(
        '<tool_call>{"name": "run_cell", "arguments": {"code": "a"}}</tool_call>\n'
        '<tool_call>{"name": "run_cell", "arguments": {"code": "b"}}</tool_call>')),
        T(tool_mode="xml"))
    assert [p["id"] for p in two["parts"]] == ["xml_0", "xml_1"]  # sequential, unique
    bad = LLM["_lift"](LLM["OpenAICompatAdapter"]().parse_response(
        _openai_text("<tool_call>{not json}</tool_call>")), T(tool_mode="xml"))
    assert bad["stop"] == "tool" and "unparseable" in bad["parts"][0]["error"]


def test_lift_reasoning_only_done_reply_becomes_the_answer():
    resp = _openai_text(None, reasoning_content=(
        "The sum is **639**<|close|>response<|sep|><|close|>message<|sep|>"))
    r = LLM["_lift"](LLM["OpenAICompatAdapter"]().parse_response(resp), T())
    assert r["stop"] == "done"
    assert [p["type"] for p in r["parts"]] == ["thinking", "text"]
    assert r["parts"][1]["text"] == "The sum is **639**"
    # a reasoning-only LENGTH stop is a truncation, not an answer
    cut = {**resp, "choices": [{**resp["choices"][0], "finish_reason": "length"}]}
    r = LLM["_lift"](LLM["OpenAICompatAdapter"]().parse_response(cut), T())
    assert [p["type"] for p in r["parts"]] == ["thinking"] and r["stop"] == "length"


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
    ("mistral-large", "mistral"), ("z-ai/glm-4.7", "glm"), ("z-ai/glm-5.3", "glm-5"),
    ("moonshotai/kimi-k3", "kimi-k3"), ("moonshotai/kimi-k2.6", "kimi"),
    ("deepseek/deepseek-v4-pro-0813", "deepseek-v4"), ("mystery-7b", "generic"),
    ("z-ai/glm-5v-turbo", "glm"), ("deepseek/deepseek-v4-flash-vision-exp", "deepseek"),
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
    r = LLM["BACKENDS"]["anthropic"]["finish"]({"max_tokens": 40000}, T(thinking="on"))
    assert r["thinking"]["budget_tokens"] == 16000
    # a cap too small for the 1024 floor: no thinking block rather than a 400
    for cap in (512, 1024, 1025):
        r = LLM["BACKENDS"]["anthropic"]["finish"]({"max_tokens": cap}, T(thinking="on"))
        assert ("thinking" in r) == (cap - 1 >= 1024), cap
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
        self.sleeps = []    # retry backoff (BOB-148): recorded, never slept

    def __call__(self, name, payload):
        if name == "config.get":
            self.config_keys.append(payload["key"])
            return {"value": self.prov}
        if name == "sleep":
            self.sleeps.append(payload["seconds"])
            return {"slept": payload["seconds"]}
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
    # the card tells people which key type to create before they hit
    # the identity-linked 400
    assert "single workspace" in cred["about"]["note"]
    assert cred["about"]["help"].startswith("https://platform.claude.com/")
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
    assert e.value.hint is None  # a transient failure carries no fix


def test_chat_identity_linked_key_400_names_the_fix_not_billing():
    body = json.dumps({"type": "error", "error": {
        "type": "invalid_request_error",
        "message": "anthropic-workspace-id is required when authenticating with "
                   "an identity-linked API key; send the id of the workspace "
                   "this request acts in."}})
    host = FakeHost({"provider": "anthropic", "model": "m",
                     "base_url": "https://api.anthropic.com", "api_key_ref": "k"},
                    status=400, body=body)
    g = load(host)
    with pytest.raises(g["LlmError"]) as e:
        g["chat"](MSGS)
    assert e.value.status == 400
    assert "single workspace" in e.value.hint and "not a credit" in e.value.hint
    assert "identity-linked" in str(e.value) and e.value.hint in str(e.value)
    # bao never sends the header the API asks for — the fix is the key type
    assert "anthropic-workspace-id" not in host.posts[0]["headers"]


def test_chat_low_credit_400_names_billing_not_the_key():
    body = json.dumps({"type": "error", "error": {
        "type": "invalid_request_error",
        "message": "Your credit balance is too low to access the Anthropic API. "
                   "Please go to Plans & Billing to upgrade or purchase credits."}})
    host = FakeHost({"provider": "anthropic", "model": "m",
                     "base_url": "https://api.anthropic.com", "api_key_ref": "k"},
                    status=400, body=body)
    g = load(host)
    with pytest.raises(g["LlmError"]) as e:
        g["chat"](MSGS)
    assert "settings/billing" in e.value.hint and "key itself is fine" in e.value.hint
    assert "subscription" in e.value.hint


# --- chat(): transport + provider retry (BOB-148) ----------------------------
#
# The three transport failures a reporter's export carried (BOB-147,
# anybao-export-20260917-091830.zip, 20 records — every one a single
# `http.post` to api.anthropic.com raising URLError, every one the end
# of its run). Messages verbatim from the trace records.

URL = "https://api.anthropic.com/v1/messages"
DNS_BLIP = (f"{URL}: Dns Failed: resolve dns name 'api.anthropic.com:443': "
            "failed to lookup address information: nodename nor servname "
            "provided, or not known")                       # run_255f0111… durMs 71
CONN_RESET = (f"{URL}: Network Error: Network Error: Error encountered in "
              "the status line: Connection reset by peer (os error 54)")  # run_f61a9d42… 54435
READ_STALL = (f"{URL}: Network Error: Network Error: Error encountered in "
              "the status line: timed out reading response")  # run_e65db6ee… 180003


class ScriptedHost(FakeHost):
    """A FakeHost whose http.post answers follow a script: an Exception
    is raised, a `(status, body)` / `(status, body, headers)` tuple is
    returned. `sleep` effects are recorded, never slept."""

    def __init__(self, outcomes, prov=None):
        super().__init__(prov or {"provider": "anthropic", "model": "m",
                                  "base_url": "https://api.anthropic.com",
                                  "api_key_ref": "llm.key.anthropic"})
        self.outcomes = list(outcomes)

    def __call__(self, name, payload):
        if name != "http.post":
            return super().__call__(name, payload)
        self.posts.append(payload)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        status, body, *rest = out
        return {"status": status, "headers": rest[0] if rest else {},
                "body": body if isinstance(body, str) else json.dumps(body)}


def transport(msg):
    return EffectError(f"URLError: {msg}")


OK = (200, ANTHROPIC_DONE_RESP)


@pytest.mark.parametrize("msg", [DNS_BLIP, CONN_RESET, READ_STALL],
                         ids=["dns", "reset", "stall"])
def test_chat_retries_one_transport_failure(msg):
    host = ScriptedHost([transport(msg), OK])
    reply = load(host)["chat"](MSGS)
    assert reply["stop"] == "done"
    # the same request went out twice — each attempt is its own effect
    assert len(host.posts) == 2 and host.posts[0] == host.posts[1]
    assert host.sleeps == [1]


def test_chat_gives_up_after_three_transport_attempts():
    host = ScriptedHost([transport(DNS_BLIP)] * 3 + [OK])
    with pytest.raises(EffectError) as e:
        load(host)["chat"](MSGS)
    assert "nodename nor servname" in str(e.value)   # the last error, unchanged
    assert len(host.posts) == 3
    assert host.sleeps == [1, 4]
    assert host.outcomes == [OK]      # never reached


def test_chat_retries_a_stalled_read_like_any_transport_failure():
    # streamed (BOB-149): a stall costs the idle timeout, not 180 s, so
    # it gets the same three attempts as a DNS blip
    host = ScriptedHost([transport(READ_STALL)] * 3 + [OK])
    with pytest.raises(EffectError) as e:
        load(host)["chat"](MSGS)
    assert "timed out reading response" in str(e.value)
    assert len(host.posts) == 3 and host.sleeps == [1, 4]


@pytest.mark.parametrize("status,body", [
    (529, {"type": "error", "error": {"type": "overloaded_error",
                                      "message": "Overloaded"}}),
    (503, "upstream unavailable"),
    (429, {"type": "error", "error": {"type": "rate_limit_error",
                                      "message": "slow down"}}),
], ids=["529-overloaded", "503", "429"])
def test_chat_retries_transient_provider_status(status, body):
    host = ScriptedHost([(status, body), OK])
    reply = load(host)["chat"](MSGS)
    assert reply["stop"] == "done"
    assert len(host.posts) == 2 and host.sleeps == [1]


def test_chat_honours_retry_after():
    host = ScriptedHost([(429, "slow down", {"retry-after": "7"}), OK])
    load(host)["chat"](MSGS)
    assert host.sleeps == [7]


def test_chat_caps_retry_after():
    host = ScriptedHost([(429, "slow down", {"retry-after": "3600"}), OK])
    load(host)["chat"](MSGS)
    assert host.sleeps == [60]


def test_chat_raises_llm_error_when_the_provider_stays_down():
    host = ScriptedHost([(529, "Overloaded")] * 3 + [OK])
    g = load(host)
    with pytest.raises(g["LlmError"]) as e:
        g["chat"](MSGS)
    assert e.value.status == 529
    assert len(host.posts) == 3 and host.sleeps == [1, 4]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
def test_chat_never_retries_a_client_error(status):
    host = ScriptedHost([(status, "no"), OK])
    g = load(host)
    with pytest.raises(g["LlmError"]) as e:
        g["chat"](MSGS)
    assert e.value.status == status
    assert len(host.posts) == 1 and host.sleeps == []


def test_chat_does_not_retry_other_effect_failures():
    # only the transport is retried — a capability denial, a missing
    # secret or a mock miss are not going to change on a second try
    host = ScriptedHost([EffectError("SecretMissing: llm.key.anthropic"), OK])
    with pytest.raises(EffectError) as e:
        load(host)["chat"](MSGS)
    assert "SecretMissing" in str(e.value)
    assert len(host.posts) == 1 and host.sleeps == []


# --- streaming (BOB-149, ADR-005 §1.2) ---------------------------------------
#
# The provider call streams: the request asks for SSE, the host
# records the SSE text as ONE http.post body, and the adapter folds
# the events into the same provider JSON the non-streaming wire
# returns, so parse_response is the single reader of both.

def _sse(*events):
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)


ANTHROPIC_FINAL = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "m",
    "content": [
        {"type": "thinking", "thinking": "Let me think.", "signature": "sig_abc"},
        {"type": "text", "text": "Let me compute."},
        {"type": "tool_use", "id": "toolu_1", "name": "run_cell", "input": {"code": "1+1"}},
    ],
    "stop_reason": "tool_use", "stop_sequence": None,
    "usage": {"input_tokens": 100, "output_tokens": 30,
              "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3},
}

ANTHROPIC_SSE = _sse(
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "m",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 1,
                  "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "thinking", "thinking": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "thinking_delta", "thinking": "Let me "}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "thinking_delta", "thinking": "think."}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "signature_delta", "signature": "sig_abc"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("content_block_start", {"type": "content_block_start", "index": 1,
                             "content_block": {"type": "text", "text": ""}}),
    ("ping", {"type": "ping"}),
    ("content_block_delta", {"type": "content_block_delta", "index": 1,
                             "delta": {"type": "text_delta", "text": "Let me "}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 1,
                             "delta": {"type": "text_delta", "text": "compute."}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 1}),
    ("content_block_start", {"type": "content_block_start", "index": 2,
                             "content_block": {"type": "tool_use", "id": "toolu_1",
                                               "name": "run_cell", "input": {}}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 2,
                             "delta": {"type": "input_json_delta", "partial_json": "{\"code\": "}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 2,
                             "delta": {"type": "input_json_delta", "partial_json": "\"1+1\"}"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 2}),
    ("message_delta", {"type": "message_delta",
                       "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                       "usage": {"output_tokens": 30}}),
    ("message_stop", {"type": "message_stop"}),
)


def test_anthropic_stream_folds_to_the_final_message():
    a = LLM["AnthropicAdapter"]()
    assert a.parse_stream(ANTHROPIC_SSE) == ANTHROPIC_FINAL
    # the one reader: the folded message parses exactly like the wire's
    assert a.parse_response(a.parse_stream(ANTHROPIC_SSE)) == a.parse_response(ANTHROPIC_FINAL)
    # thinking round-trips whole (signature included, ADR-005 §1.4)
    think = a.parse_response(a.parse_stream(ANTHROPIC_SSE))["parts"][0]
    assert think["provider_state"] == ANTHROPIC_FINAL["content"][0]


def _chunk(delta=None, finish=None, usage=None, choices=True):
    c = {"id": "c1", "object": "chat.completion.chunk", "model": "m",
         "choices": ([{"index": 0, "delta": delta or {}, "finish_reason": finish}]
                     if choices else [])}
    if usage is not None:
        c["usage"] = usage
    return c


OPENAI_USAGE = {"prompt_tokens": 100, "completion_tokens": 30,
                "prompt_tokens_details": {"cached_tokens": 7}}
OPENAI_FINAL = {
    "id": "c1", "object": "chat.completion.chunk", "model": "m",
    "choices": [{"index": 0, "message": {
        "role": "assistant", "content": "Let me compute.", "reasoning_content": "hmm",
        "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                        "function": {"name": "run_cell", "arguments": "{\"code\": \"1+1\"}"}}]},
        "finish_reason": "tool_calls"}],
    "usage": OPENAI_USAGE,
}
OPENAI_SSE = "".join(f"data: {json.dumps(c)}\n\n" for c in [
    _chunk({"role": "assistant", "content": ""}),
    _chunk({"reasoning_content": "hmm"}),
    _chunk({"content": "Let me "}),
    _chunk({"content": "compute."}),
    _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                            "function": {"name": "run_cell", "arguments": ""}}]}),
    _chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{\"code\": "}}]}),
    _chunk({"tool_calls": [{"index": 0, "function": {"arguments": "\"1+1\"}"}}]}),
    _chunk({}, finish="tool_calls"),
    _chunk(usage=OPENAI_USAGE, choices=False),
]) + "data: [DONE]\n\n"


def test_openai_stream_folds_to_the_final_completion():
    a = LLM["OpenAICompatAdapter"]()
    assert a.parse_stream(OPENAI_SSE) == OPENAI_FINAL
    norm = LLM["_normalize_openai_compat"]
    folded, wire = norm(a.parse_stream(OPENAI_SSE)), norm(OPENAI_FINAL)
    assert a.parse_response(folded) == a.parse_response(wire)


def test_openai_stream_keeps_openrouter_reasoning_fields():
    # OpenRouter streams `reasoning` text + `reasoning_details` items —
    # both must survive the fold so normalize/parse see them as on the
    # non-streaming wire (`roundtrip` continuity, ADR-005 §1.4)
    a = LLM["OpenAICompatAdapter"]()
    sse = "".join(f"data: {json.dumps(c)}\n\n" for c in [
        _chunk({"reasoning": "why ",
                "reasoning_details": [{"type": "reasoning.text", "text": "why "}]}),
        _chunk({"reasoning": "not",
                "reasoning_details": [{"type": "reasoning.text", "text": "not"}]}),
        _chunk({"content": "ok"}, finish="stop"),
    ]) + "data: [DONE]\n\n"
    msg = a.parse_stream(sse)["choices"][0]["message"]
    assert msg["reasoning"] == "why not"
    assert msg["reasoning_details"] == [{"type": "reasoning.text", "text": "why "},
                                        {"type": "reasoning.text", "text": "not"}]
    assert msg["content"] == "ok"


def test_chat_streams_with_idle_and_total_timeouts():
    host = FakeHost({"provider": "anthropic", "model": "m",
                     "base_url": "https://api.anthropic.com", "api_key_ref": "llm.key.anthropic"},
                    body=ANTHROPIC_SSE)
    reply = load(host)["chat"](MSGS, tools=[{"name": "run_cell"}])
    post = host.posts[0]
    assert post["stream"] is True and post["json"]["stream"] is True
    assert post["timeout"] == {"idle": 60, "total": 900}
    assert reply["stop"] == "tool" and reply["usage"]["out"] == 30


def test_chat_openai_stream_asks_for_usage():
    host = FakeHost({"provider": "openai-compat", "model": "m", "base_url": "https://api.openai.com/v1",
                     "api_key_ref": "llm.key.openai"}, body=OPENAI_SSE)
    load(host)["chat"](MSGS)
    req = host.posts[0]["json"]
    assert req["stream"] is True and req["stream_options"] == {"include_usage": True}


def test_chat_tier_row_overrides_the_timeouts():
    host = FakeHost({"provider": "anthropic", "model": "m",
                     "base_url": "https://api.anthropic.com", "api_key_ref": "llm.key.anthropic",
                     "timeout": {"idle": 1, "total": 5}}, body=ANTHROPIC_SSE)
    load(host)["chat"](MSGS)
    assert host.posts[0]["timeout"] == {"idle": 1, "total": 5}


def test_chat_reads_a_json_answer_to_a_stream_request():
    # a proxy that ignores `stream` answers JSON — still one reader
    host = FakeHost({"provider": "anthropic", "model": "m",
                     "base_url": "https://api.anthropic.com", "api_key_ref": "llm.key.anthropic"},
                    body=ANTHROPIC_DONE_RESP)
    assert load(host)["chat"](MSGS)["stop"] == "done"


OVERLOADED_SSE = _sse(
    ("message_start", {"type": "message_start", "message": {
        "id": "m", "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}}),
    ("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}))


def test_chat_stream_error_event_is_retried_like_the_status():
    # Anthropic signals overload under an open stream as an `error`
    # event on a 200 — the same three attempts a 529 status gets
    host = ScriptedHost([(200, OVERLOADED_SSE), (200, OVERLOADED_SSE), (200, ANTHROPIC_SSE)])
    reply = load(host)["chat"](MSGS, tools=[{"name": "run_cell"}])
    assert reply["stop"] == "tool"
    assert len(host.posts) == 3 and host.sleeps == [1, 4]


def test_chat_stream_error_event_is_a_typed_provider_error_after_the_retries():
    host = ScriptedHost([(200, OVERLOADED_SSE)] * 3 + [OK])
    g = load(host)
    with pytest.raises(g["LlmError"]) as e:
        g["chat"](MSGS)
    assert e.value.status == 529 and "Overloaded" in str(e.value)
    assert len(host.posts) == 3 and host.outcomes == [OK]


@pytest.mark.parametrize("body", [
    ANTHROPIC_SSE.split("event: message_stop")[0],   # cut before the terminator
    ANTHROPIC_SSE.split("event: content_block_stop")[0],   # cut mid-text
    "",                                               # a 200 with no bytes
    "<html>gateway error</html>",                     # a proxy's error page
], ids=["no-message-stop", "mid-text", "empty", "html"])
def test_chat_cut_stream_is_incomplete_and_retried_never_done(body):
    # a dropped connection or a zero-byte 200 used to fold to a
    # half-sentence with stop=done — posted as the final answer
    host = ScriptedHost([(200, body), (200, ANTHROPIC_SSE)])
    reply = load(host)["chat"](MSGS, tools=[{"name": "run_cell"}])
    assert reply["stop"] == "tool" and len(host.posts) == 2 and host.sleeps == [1]
    host = ScriptedHost([(200, body)] * 3 + [OK])
    g = load(host)
    with pytest.raises(g["IncompleteReply"]) as e:
        g["chat"](MSGS)
    assert e.value.status == 502 and "incomplete" in str(e.value)


def test_openai_cut_stream_is_incomplete():
    a = LLM["OpenAICompatAdapter"]()
    cut = OPENAI_SSE.split("data: [DONE]")[0]          # [DONE] lost, finish_reason kept
    assert a.parse_stream(cut)["choices"][0]["finish_reason"] == "tool_calls"
    cut = "".join(f"data: {json.dumps(c)}\n\n" for c in [_chunk({"content": "Half an ans"})])
    with pytest.raises(LLM["IncompleteReply"]):
        a.parse_stream(cut)


def test_chat_total_timeout_failure_is_not_retried():
    # the provider generated for the whole `total` — three of those
    # outrun every run deadline (PR #58 review G4)
    total = transport(f"{URL}: stream exceeded the total timeout of 900s")
    host = ScriptedHost([total, OK])
    with pytest.raises(EffectError) as e:
        load(host)["chat"](MSGS)
    assert "total timeout" in str(e.value)
    assert len(host.posts) == 1 and host.sleeps == []


def test_chat_tier_row_turns_streaming_off():
    host = FakeHost({"provider": "openai-compat", "model": "m", "base_url": "https://api.openai.com/v1",
                     "api_key_ref": "llm.key.openai", "stream": False}, body=OPENAI_FINAL)
    reply = load(host)["chat"](MSGS)
    post = host.posts[0]
    assert post["stream"] is False and post["timeout"] == 900
    assert post["json"]["stream"] is False and "stream_options" not in post["json"]
    assert reply["stop"] == "tool"


def test_chat_options_stream_false_is_followed_by_the_host_flag():
    host = FakeHost({"provider": "anthropic", "model": "m",
                     "base_url": "https://api.anthropic.com", "api_key_ref": "llm.key.anthropic",
                     "options": {"stream": False}}, body=ANTHROPIC_DONE_RESP)
    load(host)["chat"](MSGS)
    assert host.posts[0]["stream"] is False and host.posts[0]["json"]["stream"] is False


def test_chat_local_backends_default_to_the_plain_wire():
    host = FakeHost({"provider": "openai-compat", "model": "llama3", "backend": "ollama",
                     "base_url": "http://127.0.0.1:11434/v1", "api_key_ref": None},
                    body=OPENAI_FINAL)
    load(host)["chat"](MSGS)
    assert host.posts[0]["stream"] is False and host.posts[0]["json"]["stream"] is False
    host = FakeHost({"provider": "openai-compat", "model": "llama3", "backend": "ollama",
                     "base_url": "http://127.0.0.1:11434/v1", "api_key_ref": None,
                     "stream": True}, body=OPENAI_SSE)
    load(host)["chat"](MSGS)
    assert host.posts[0]["stream"] is True


def test_openai_stream_without_usage_is_marked_missing():
    # a server that ignores stream_options: zeros are no count
    a = LLM["OpenAICompatAdapter"]()
    sse = "".join(f"data: {json.dumps(c)}\n\n" for c in [
        _chunk({"content": "ok"}, finish="stop")]) + "data: [DONE]\n\n"
    usage = a.parse_response(LLM["_normalize_openai_compat"](a.parse_stream(sse)))["usage"]
    assert usage == {"in": 0, "out": 0, "cacheRead": 0, "cacheWrite": 0, "missing": True}
    assert "missing" not in a.parse_response(OPENAI_FINAL)["usage"]


# --- the fold under a lossy or odd stream (PR #58 review) --------------------

def _anthropic_sse(*blocks_events, stop="end_turn"):
    start = ("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "m",
        "content": [], "usage": {"input_tokens": 10, "output_tokens": 1}}})
    end = (("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop},
                              "usage": {"output_tokens": 5}}),
           ("message_stop", {"type": "message_stop"}))
    return _sse(start, *blocks_events, *end)


def test_anthropic_stream_partial_tool_input_is_an_error_part():
    # max_tokens mid-tool-call: the partial JSON must not raise out of
    # chat() — it folds like the OpenAI wire's malformed-arguments case
    sse = _anthropic_sse(
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "tool_use", "id": "toolu_1",
                                                   "name": "run_cell", "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "input_json_delta",
                                           "partial_json": "{\"code\": \"print(4"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        stop="max_tokens")
    a = LLM["AnthropicAdapter"]()
    reply = a.parse_response(a.parse_stream(sse))
    part = reply["parts"][0]
    assert part["type"] == "tool_call" and part["args"] == {}
    assert part["error"].startswith("unparseable tool input") and "print(4" in part["error"]
    assert reply["stop"] == "length"


def test_anthropic_stream_tolerates_a_lossy_relay():
    # a delta for a block that never started opens it; a hole in the
    # indices is dropped; nothing is None when parse_response reads it
    sse = _anthropic_sse(
        ("content_block_delta", {"type": "content_block_delta", "index": 2,
                                 "delta": {"type": "text_delta", "text": "late "}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 2,
                                 "delta": {"type": "text_delta", "text": "start"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 2}))
    a = LLM["AnthropicAdapter"]()
    folded = a.parse_stream(sse)
    assert folded["content"] == [{"type": "text", "text": "late start"}]
    assert a.parse_response(folded)["parts"] == [{"type": "text", "text": "late start"}]


def test_sse_events_split_on_newline_only():
    # U+2028 / U+2029 / U+0085 are legal raw inside a JSON string and
    # `splitlines()` would cut the data line there; `\r\n` endings too
    text = "event: x\r\ndata: " + json.dumps({"t": "a b c\u0085d"}) + "\r\n\r\n"
    events = LLM["_sse_events"](text)
    assert events == [("x", json.dumps({"t": "a b c\u0085d"}))]
    assert json.loads(events[0][1])["t"] == "a b c\u0085d"


def _openai_sse(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def test_openai_stream_tool_call_without_index_continues_the_last_call():
    # vLLM / llama.cpp style: no `index` on any tool_call chunk
    a = LLM["OpenAICompatAdapter"]()
    sse = _openai_sse(
        _chunk({"tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "run_cell", "arguments": ""}}]}),
        _chunk({"tool_calls": [{"function": {"arguments": "{\"code\": "}}]}),
        _chunk({"tool_calls": [{"function": {"arguments": "\"1+1\"}"}}]}),
        _chunk({}, finish="tool_calls"))
    calls = a.parse_stream(sse)["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 1 and calls[0]["id"] == "call_1"
    assert calls[0]["function"]["arguments"] == "{\"code\": \"1+1\"}"
    norm = LLM["_normalize_openai_compat"]
    assert a.parse_response(norm(a.parse_stream(sse)))["parts"][0]["args"] == {"code": "1+1"}


def test_openai_stream_tool_call_index_is_coerced_and_null_id_never_pins():
    a = LLM["OpenAICompatAdapter"]()
    sse = _openai_sse(
        _chunk({"tool_calls": [{"index": "0", "id": None, "type": "function",
                                "function": {"name": "run_cell", "arguments": ""}}]}),
        _chunk({"tool_calls": [{"index": 0, "id": "call_1",
                                "function": {"arguments": "{}"}}]}),
        _chunk({"tool_calls": [{"index": "1", "id": "call_2", "type": "function",
                                "function": {"name": "other", "arguments": "{}"}}]}),
        _chunk({}, finish="tool_calls"))
    calls = a.parse_stream(sse)["choices"][0]["message"]["tool_calls"]
    assert [c["id"] for c in calls] == ["call_1", "call_2"]
    assert [c["index"] for c in calls] == [0, 1]


def test_openai_stream_only_text_fields_concatenate():
    a = LLM["OpenAICompatAdapter"]()
    sse = _openai_sse(_chunk({"role": "assistant", "content": "a"}),
                      _chunk({"role": "assistant", "content": "b"}, finish="stop"))
    msg = a.parse_stream(sse)["choices"][0]["message"]
    assert msg["role"] == "assistant" and msg["content"] == "ab"


def test_chat_numeric_tier_timeout_is_the_total():
    # the ADR-002 whole-request spelling on a tier row still works
    host = FakeHost({"provider": "anthropic", "model": "m",
                     "base_url": "https://api.anthropic.com", "api_key_ref": "llm.key.anthropic",
                     "timeout": 300}, body=ANTHROPIC_SSE)
    load(host)["chat"](MSGS)
    assert host.posts[0]["timeout"] == {"idle": 60, "total": 300}


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


def test_blob_file_parts_ride_as_refs_the_host_expands():
    # ADR-026 §5: a Blob's ref is what the adapters place on the wire —
    # bare inside anthropic source blocks, data-URI form on the OpenAI
    # image_url / file_data fields; a text document is read through
    # the host
    b = LLM["blob"].from_bytes(b"hello, file", "text/plain")
    png = LLM["Blob"]("sha256:" + "ab" * 32, 8, "image/png")
    a = LLM["AnthropicAdapter"]()
    img = a.build_request(_file_msg("image/png", png), "", [], "m", CLAUDE)
    assert img["messages"][0]["content"][0]["source"]["data"] == png.ref()
    txt = a.build_request(_file_msg("text/plain", b), "", [], "m", CLAUDE)
    assert txt["messages"][0]["content"][0]["source"]["data"] == "hello, file"
    o = LLM["OpenAICompatAdapter"]()
    req = o.build_request(_file_msg("image/png", png), "", [], "m", T())
    assert req["messages"][0]["content"][1]["image_url"]["url"] == png.ref("data-uri")
    pdf = LLM["Blob"]("sha256:" + "cd" * 32, 8, "application/pdf")
    req = o.build_request(_file_msg("application/pdf", pdf, "menu.pdf"), "", [], "m",
                          T(pdf_input="file"))
    assert req["messages"][0]["content"][1]["file"]["file_data"] == pdf.ref("data-uri")


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


def test_pdf_rides_the_openai_wire_as_a_file_part_when_the_backend_carries_it():
    # ADR-020 §3: `pdf_input` is a backend grant, not a profile trait
    eff = LLM["_effective"]
    assert eff(T(), "openrouter")["pdf_input"] == "file"
    assert eff(T(), "openai")["pdf_input"] == "file"
    assert eff(T(), "anthropic")["pdf_input"] == "file"
    assert eff(T(), "gemini")["pdf_input"] == "image_url"   # probed 2026-09-01
    assert eff(T(), "generic")["pdf_input"] == "none"
    assert eff(T(), "deepseek")["pdf_input"] == "none"
    o = LLM["OpenAICompatAdapter"]()
    req = o.build_request(_file_msg("application/pdf", "UERG", "menu.pdf"), "", [], "m",
                          T(pdf_input="file"))
    content = req["messages"][0]["content"]
    assert content[1] == {"type": "file", "file": {
        "filename": "menu.pdf", "file_data": "data:application/pdf;base64,UERG"}}
    # no name → a stable default filename
    req = o.build_request(_file_msg("application/pdf", "UERG"), "", [], "m", T(pdf_input="file"))
    assert req["messages"][0]["content"][1]["file"]["filename"] == "document.pdf"
    # Gemini's carriage: the PDF data URI rides image_url
    gem = o.build_request(_file_msg("application/pdf", "UERG"), "", [], "m",
                          T(pdf_input="image_url"))
    assert gem["messages"][0]["content"][1] == {
        "type": "image_url", "image_url": {"url": "data:application/pdf;base64,UERG"}}
    # OpenRouter: a request with a file part pins the FREE parser (the
    # host default is paid OCR); no file part → no plugin; an explicit
    # `plugins` (a tier's options) is left alone
    fin = LLM["_finish_openrouter"]
    assert fin(dict(req, messages=req["messages"]), T())["plugins"] == [
        {"id": "file-parser", "pdf": {"engine": "cloudflare-ai"}}]
    plain = o.build_request([{"role": "user", "parts": [{"type": "text", "text": "hi"}]}],
                            "", [], "m", T())
    assert "plugins" not in fin(plain, T())
    pinned = dict(req, plugins=[{"id": "file-parser", "pdf": {"engine": "native"}}])
    assert fin(pinned, T())["plugins"][0]["pdf"]["engine"] == "native"


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
    # a Blob (http.get(url).blob) / a file_content result carrying one
    png = g["Blob"]("sha256:" + "ab" * 32, 8, "image/png")
    assert g["read"](png, "blob?") == "a cat"
    assert host.posts[-1]["json"]["messages"][0]["content"][0]["source"]["data"] == png.ref()
    assert g["read"]({"mime": "image/png", "blob": png}, "dict?") == "a cat"
    with pytest.raises(TypeError):
        g["read"]("bare-file-id", "?")
