"""llm@v1 — model calls as a guest module (ADR-005 §1).

Neutral message model in the loop; adapters translate to/from each
provider wire. Translation is PURE and offline-testable (recorded
response -> neutral parts, and reverse); the single `http.post` syscall
is the effect boundary. The api key never enters the guest: the request
names a `credential` (config ref + header) and the HOST injects the
secret header.

Neutral shapes (dicts, JSON-serializable for the trace):
  Part: {type: text|tool_call|tool_result|thinking, ...}
  Reply: {parts: [Part], stop: done|tool|length, usage: {in, out}}
"""

import json


class LlmError(Exception):
    """Provider answered with an error status; carries the status and a
    body excerpt (never the request — that may quote the conversation)."""

    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"llm provider returned {status}: {body}")


# --- Anthropic (native) -----------------------------------------------------

class AnthropicAdapter:
    """Native: thinking blocks round-trip via opaque provider_state.
    Caching (ADR-005 §1): breakpoints at end of system and end of the
    conversation — the loop's prefix is append-only, so each call
    writes the cache the next call reads."""

    def build_request(self, messages, system, tools, model):
        api_msgs = [self._to_anthropic_msg(m) for m in messages]
        self._mark_cache(api_msgs)
        req = {"model": model, "messages": api_msgs, "max_tokens": 4096}
        if system:
            req["system"] = [{"type": "text", "text": system,
                              "cache_control": {"type": "ephemeral"}}]
        if tools:
            req["tools"] = [
                {"name": t["name"], "description": t.get("description", ""),
                 "input_schema": t.get("input_schema", {"type": "object"})}
                for t in tools
            ]
        return req

    def _to_anthropic_msg(self, m):
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

    @staticmethod
    def _mark_cache(api_msgs):
        for msg in reversed(api_msgs):
            blocks = msg["content"]
            for i in range(len(blocks) - 1, -1, -1):
                if blocks[i].get("type") in ("thinking", "redacted_thinking"):
                    continue  # cache_control is invalid on thinking blocks
                # copy — a provider_state block is shared with the neutral
                # message and must round-trip byte-exact next call
                blocks[i] = {**blocks[i],
                             "cache_control": {"type": "ephemeral"}}
                return

    def parse_response(self, raw):
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
                "usage": {"in": u.get("input_tokens", 0),
                          "out": u.get("output_tokens", 0),
                          "cacheRead": u.get("cache_read_input_tokens", 0),
                          "cacheWrite": u.get("cache_creation_input_tokens", 0)}}


_NORM_STOP = {"end_turn": "done", "stop_sequence": "done",
              "tool_use": "tool", "max_tokens": "length"}


# --- OpenAI-compatible (vLLM/llama.cpp/ollama/OpenRouter) --------------------

class OpenAICompatAdapter:
    """Reasoning models on this wire (DeepSeek convention, Together/
    OpenRouter) return thinking as `reasoning_content`/`reasoning` on the
    message — captured as a thinking part so it lands in the trace, but
    advisory: the wire has no slot to resend it, the model re-reasons."""

    def build_request(self, messages, system, tools, model):
        api_msgs = []
        if system:
            api_msgs.append({"role": "system", "content": system})
        for m in messages:
            api_msgs.extend(self._to_openai_msgs(m))
        req = {"model": model, "messages": api_msgs}
        if tools:
            req["tools"] = [
                {"type": "function",
                 "function": {"name": t["name"], "description": t.get("description", ""),
                              "parameters": t.get("input_schema", {"type": "object"})}}
                for t in tools
            ]
        return req

    def _to_openai_msgs(self, m):
        texts = [p["text"] for p in m["parts"] if p["type"] == "text"]
        calls = [p for p in m["parts"] if p["type"] == "tool_call"]
        results = [p for p in m["parts"] if p["type"] == "tool_result"]
        out = []
        if results:  # tool results are their own role in openai
            for r in results:
                out.append({"role": "tool", "tool_call_id": r["call_id"],
                            "content": r["content"]})
            return out
        msg = {"role": m["role"], "content": " ".join(texts)}
        if calls:
            msg["tool_calls"] = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                for c in calls
            ]
        return [msg]

    def parse_response(self, raw):
        choice = raw["choices"][0]
        msg = choice["message"]
        parts = []
        think = msg.get("reasoning_content") or msg.get("reasoning")
        if think:
            parts.append({"type": "thinking", "text": think})
        if msg.get("content"):
            parts.append({"type": "text", "text": msg["content"]})
        for tc in msg.get("tool_calls", []) or []:
            parts.append({"type": "tool_call", "id": tc["id"],
                          "name": tc["function"]["name"],
                          "args": json.loads(tc["function"]["arguments"])})
        fr = choice.get("finish_reason", "stop")
        stop = "tool" if fr == "tool_calls" else ("length" if fr == "length" else "done")
        u = raw.get("usage", {})
        det = u.get("prompt_tokens_details") or {}
        return {"parts": parts, "stop": stop,
                "usage": {"in": u.get("prompt_tokens", 0),
                          "out": u.get("completion_tokens", 0),
                          "cacheRead": det.get("cached_tokens", 0),
                          "cacheWrite": 0}}


# --- Fenced fallback (tool-weak / bare completion models) -------------------

class FencedAdapter:
    """codeAct heritage: no tool API — the model replies with a ```cell
    block in plain text, parsed into a tool_call. Emulates the tool
    interface on any completion endpoint (ADR-005 §1)."""

    def __init__(self, inner):
        self._inner = inner  # a text-only provider adapter builds the request

    def build_request(self, messages, system, tools, model):
        instr = (
            "\n\nTo run Python, reply with a fenced block:\n```cell\n<code>\n```\n"
            "Reply with plain text (no block) when done."
        )
        return self._inner.build_request(messages, (system or "") + instr, [], model)

    def parse_response(self, raw):
        reply = self._inner.parse_response(raw)
        text = " ".join(p["text"] for p in reply["parts"] if p["type"] == "text")
        code = _extract_fenced(text)
        if code is not None:
            return {"parts": [{"type": "tool_call", "id": "fenced_0",
                               "name": "run_cell", "args": {"code": code}}],
                    "stop": "tool", "usage": reply["usage"]}
        return reply


def _extract_fenced(text):
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


def build_adapter(provider, fenced):
    """Adapter for a tier's provider; `fenced` wraps it in the ```cell
    emulation. Also exec'd host-side by the llm-seed CLI."""
    base = ADAPTERS[provider]()
    return FencedAdapter(base) if fenced else base


_TIMEOUT_S = 180  # a stalled provider connection must ERROR, never hang
                  # the conversation thread (live-caught with urlopen)

_EXCERPT = 400


@span("llm.chat")  # noqa: F821 - guest global
def chat(messages, system="", tier="codegen", tools=None):
    """One model call: resolve the tier's provider config, translate the
    neutral messages to the provider wire, POST through the http syscall
    (the host injects the api key from `credential.ref` — the key never
    enters the guest or the trace), parse back to a neutral Reply."""
    prov = effect("config.get", {"key": f"llm.tier.{tier}"})["value"]  # noqa: F821 - guest global
    adapter = build_adapter(prov["provider"], prov.get("fenced", False))
    req = adapter.build_request(messages, system, tools or [], prov["model"])
    if prov["provider"] == "anthropic":
        url = prov["base_url"].rstrip("/") + "/v1/messages"
        headers = {"anthropic-version": "2023-06-01"}
        credential = {"ref": prov["api_key_ref"], "header": "x-api-key"}
    else:
        url = prov["base_url"].rstrip("/") + "/chat/completions"
        headers = {}
        credential = {"ref": prov["api_key_ref"], "header": "Authorization",
                      "prefix": "Bearer "}
    resp = effect("http.post", {  # noqa: F821 - guest global
        "url": url, "headers": headers, "json": req,
        "timeout": _TIMEOUT_S, "credential": credential})
    if resp["status"] >= 400:
        raise LlmError(resp["status"], resp["body"][:_EXCERPT])
    return adapter.parse_response(json.loads(resp["body"]))
