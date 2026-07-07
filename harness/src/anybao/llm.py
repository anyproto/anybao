"""LLM effect + provider adapters — ADR-005 §1.

Neutral message model in the loop; adapters translate to/from each
provider inside the `llm.chat` effect. Translation is PURE and
offline-testable (recorded response -> neutral parts, and reverse);
the HTTP call is the effect boundary. One real call per provider seeds
a golden fixture; after that CI needs no key.

Neutral shapes (dicts, JSON-serializable for the trace):
  Part: {type: text|tool_call|tool_result|thinking, ...}
  Reply: {parts: [Part], stop: done|tool|length, usage: {in, out}}
"""

from __future__ import annotations

from typing import Any, Protocol


class Adapter(Protocol):
    def build_request(
        self, messages: list[dict], system: str, tools: list[dict], model: str
    ) -> dict: ...
    def parse_response(self, raw: dict) -> dict: ...


# --- Anthropic (native) -----------------------------------------------------

class AnthropicAdapter:
    """Native: thinking blocks round-trip via opaque provider_state;
    prefix_stable hint -> cache_control (wired M2 when config lands)."""

    def build_request(self, messages, system, tools, model):
        api_msgs = [self._to_anthropic_msg(m) for m in messages]
        req: dict[str, Any] = {"model": model, "messages": api_msgs, "max_tokens": 4096}
        if system:
            req["system"] = system
        if tools:
            req["tools"] = [
                {"name": t["name"], "description": t.get("description", ""),
                 "input_schema": t.get("input_schema", {"type": "object"})}
                for t in tools
            ]
        return req

    def _to_anthropic_msg(self, m: dict) -> dict:
        blocks = []
        for p in m["parts"]:
            t = p["type"]
            if t == "text":
                blocks.append({"type": "text", "text": p["text"]})
            elif t == "tool_call":
                blocks.append({"type": "tool_use", "id": p["id"],
                               "name": p["name"], "input": p["args"]})
            elif t == "tool_result":
                blocks.append({"type": "tool_result", "tool_use_id": p["call_id"],
                               "content": p["content"], "is_error": p.get("is_error", False)})
            elif t == "thinking":
                # round-trip the signed block verbatim (provider_state)
                blocks.append(
                    p.get("provider_state")
                    or {"type": "thinking", "thinking": p.get("text", "")}
                )
        return {"role": m["role"], "content": blocks}

    def parse_response(self, raw: dict) -> dict:
        parts = []
        for block in raw.get("content", []):
            bt = block["type"]
            if bt == "text":
                parts.append({"type": "text", "text": block["text"]})
            elif bt == "tool_use":
                parts.append({"type": "tool_call", "id": block["id"],
                              "name": block["name"], "args": block["input"]})
            elif bt in ("thinking", "redacted_thinking"):
                parts.append({"type": "thinking",
                              "text": block.get("thinking", ""),
                              "provider_state": block})
        stop = _NORM_STOP.get(raw.get("stop_reason", ""), "done")
        u = raw.get("usage", {})
        return {"parts": parts, "stop": stop,
                "usage": {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0)}}


_NORM_STOP = {"end_turn": "done", "stop_sequence": "done",
              "tool_use": "tool", "max_tokens": "length"}


# --- OpenAI-compatible (vLLM/llama.cpp/ollama/OpenRouter) --------------------

class OpenAICompatAdapter:
    def build_request(self, messages, system, tools, model):
        api_msgs = []
        if system:
            api_msgs.append({"role": "system", "content": system})
        for m in messages:
            api_msgs.extend(self._to_openai_msgs(m))
        req: dict[str, Any] = {"model": model, "messages": api_msgs}
        if tools:
            req["tools"] = [
                {"type": "function",
                 "function": {"name": t["name"], "description": t.get("description", ""),
                              "parameters": t.get("input_schema", {"type": "object"})}}
                for t in tools
            ]
        return req

    def _to_openai_msgs(self, m: dict) -> list[dict]:
        texts = [p["text"] for p in m["parts"] if p["type"] == "text"]
        calls = [p for p in m["parts"] if p["type"] == "tool_call"]
        results = [p for p in m["parts"] if p["type"] == "tool_result"]
        out = []
        if results:  # tool results are their own role in openai
            for r in results:
                out.append({"role": "tool", "tool_call_id": r["call_id"],
                            "content": r["content"]})
            return out
        msg: dict[str, Any] = {"role": m["role"], "content": " ".join(texts)}
        if calls:
            import json as _j
            msg["tool_calls"] = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": _j.dumps(c["args"])}}
                for c in calls
            ]
        return [msg]

    def parse_response(self, raw: dict) -> dict:
        import json as _j

        choice = raw["choices"][0]
        msg = choice["message"]
        parts: list[dict] = []
        if msg.get("content"):
            parts.append({"type": "text", "text": msg["content"]})
        for tc in msg.get("tool_calls", []) or []:
            parts.append({"type": "tool_call", "id": tc["id"],
                          "name": tc["function"]["name"],
                          "args": _j.loads(tc["function"]["arguments"])})
        fr = choice.get("finish_reason", "stop")
        stop = "tool" if fr == "tool_calls" else ("length" if fr == "length" else "done")
        u = raw.get("usage", {})
        return {"parts": parts, "stop": stop,
                "usage": {"in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0)}}


# --- Fenced fallback (tool-weak / bare completion models) -------------------

class FencedAdapter:
    """codeAct heritage: no tool API — the model replies with a ```cell
    block in plain text, parsed into a tool_call. Emulates the tool
    interface on any completion endpoint (ADR-005 §1)."""

    def __init__(self, inner: Adapter):
        self._inner = inner  # a text-only provider adapter builds the request

    def build_request(self, messages, system, tools, model):
        instr = (
            "\n\nTo run Python, reply with a fenced block:\n```cell\n<code>\n```\n"
            "Reply with plain text (no block) when done."
        )
        return self._inner.build_request(messages, (system or "") + instr, [], model)

    def parse_response(self, raw: dict) -> dict:
        reply = self._inner.parse_response(raw)
        text = " ".join(p["text"] for p in reply["parts"] if p["type"] == "text")
        code = _extract_fenced(text)
        if code is not None:
            return {"parts": [{"type": "tool_call", "id": "fenced_0",
                               "name": "run_cell", "args": {"code": code}}],
                    "stop": "tool", "usage": reply["usage"]}
        return reply


def _extract_fenced(text: str) -> str | None:
    marker = "```cell"
    i = text.find(marker)
    if i == -1:
        return None
    rest = text[i + len(marker):]
    end = rest.find("```")
    return rest[:end].strip("\n") if end != -1 else rest.strip("\n")


ADAPTERS = {
    "anthropic": AnthropicAdapter,
    "openai-compat": OpenAICompatAdapter,
}


# --- the llm.chat effect ----------------------------------------------------

def register_llm_effect(registry, *, transport, config):
    """Register `llm.chat` (ADR-005 §1). `transport(url, headers, body)
    -> dict` is the single HTTP call (its own effect in production so
    the raw exchange is recorded; injected here to keep provider glue
    testable). `config(key) -> value` resolves tier -> provider/model/
    endpoint/key (the config effect, M2)."""
    from anyrt.effects import effect

    @effect("llm.chat", kind="read", registry=registry)
    def llm_chat(ctx, messages, system="", tier="codegen", tools=None):
        prov = config(f"llm.tier.{tier}")           # {provider, model, base_url, api_key_ref}
        adapter = _build_adapter(prov["provider"], prov.get("fenced", False))
        req = adapter.build_request(messages, system, tools or [], prov["model"])
        raw = transport(prov, req)
        return adapter.parse_response(raw)

    return llm_chat


def _build_adapter(provider: str, fenced: bool):
    base = ADAPTERS[provider]()
    return FencedAdapter(base) if fenced else base


def http_transport(secret_key_lookup):
    """Production transport: one HTTP POST to the provider. `prov`
    carries endpoint/model; the api key is resolved HERE (inside the
    effect boundary) via secret_key_lookup(prov['api_key_ref']) — never
    passed through neutral messages or the trace."""
    import json as _j
    import urllib.request

    def transport(prov, req):
        headers = {"Content-Type": "application/json"}
        key = secret_key_lookup(prov["api_key_ref"])
        if prov["provider"] == "anthropic":
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
            path = "/v1/messages"
        else:
            headers["Authorization"] = f"Bearer {key}"
            path = "/chat/completions"
        r = urllib.request.Request(
            prov["base_url"].rstrip("/") + path,
            data=_j.dumps(req).encode(), method="POST", headers=headers,
        )
        with urllib.request.urlopen(r) as resp:
            return _j.loads(resp.read())

    return transport
