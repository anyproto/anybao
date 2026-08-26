"""Model calls in the neutral message shape — the loop's own surface.

Use for one-off structured judgments (classification, extraction,
scoring) inside a cell — a sub-call, not a way to talk to the user.
`chat(messages, system=…, tier="classify")`; Part is `{type:
text|tool_call|tool_result|thinking|file, …}`, the reply `{parts, stop:
done|tool|length, usage: {in, out}}`. `read(file, prompt)` puts one
`any` file (image / pdf / text — an `any://f/…` attachment) in front
of the model and returns its answer (ADR-020)."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# ADR-005 §1: adapters translate neutral <-> provider wire, PURE and
# offline-testable; the single http.post syscall is the effect
# boundary. The api key never enters the guest — the request names a
# `credential` (config ref + header), the HOST injects the header.

import base64
import json


class UnsupportedMedia(Exception):
    """A File part this provider's wire cannot carry (ADR-020 §3) —
    raised while building the request, before any http call."""

    def __init__(self, media_type, provider):
        self.media_type = media_type
        self.provider = provider
        super().__init__(
            f"{provider} cannot read {media_type!r} files; supported: "
            + ", ".join(_MEDIA[provider]))


# provider -> media classes carried natively (ADR-020 §3)
_MEDIA = {"anthropic": ("image/*", "application/pdf", "text/*"),
          "openai-compat": ("image/*",)}


def _media_class(media_type):
    mt = (media_type or "").split(";", 1)[0].strip().lower()
    if mt.startswith("image/"):
        return "image"
    if mt == "application/pdf":
        return "pdf"
    if mt.startswith("text/") or mt in ("application/json", "application/xml"):
        return "text"
    return None


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
        """Neutral messages → the Anthropic wire request dict."""
        api_msgs = [self._to_anthropic_msg(m) for m in messages]
        self._mark_cache(api_msgs)
        req = {"model": model, "messages": api_msgs, "max_tokens": 32768}
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
            elif t == "file":
                blocks.append(self._file_block(p))
        return {"role": m["role"], "content": blocks}

    @staticmethod
    def _file_block(p):
        mt = p["media_type"]
        cls = _media_class(mt)
        if cls == "image":
            return {"type": "image",
                    "source": {"type": "base64", "media_type": mt, "data": p["data"]}}
        if cls == "pdf":
            return {"type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf",
                               "data": p["data"]}}
        if cls == "text":
            text = base64.b64decode(p["data"]).decode("utf-8", "replace")
            block = {"type": "document",
                     "source": {"type": "text", "media_type": "text/plain", "data": text}}
            if p.get("name"):
                block["title"] = p["name"]
            return block
        raise UnsupportedMedia(mt, "anthropic")

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
        """Anthropic response JSON → the neutral Reply."""
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
        """Neutral messages → the OpenAI-compatible wire request dict."""
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
        files = [p for p in m["parts"] if p["type"] == "file"]
        if files:
            content = [{"type": "text", "text": " ".join(texts)}] if texts else []
            for f in files:
                if _media_class(f["media_type"]) != "image":
                    raise UnsupportedMedia(f["media_type"], "openai-compat")
                content.append({"type": "image_url", "image_url": {
                    "url": f"data:{f['media_type']};base64,{f['data']}"}})
            msg = {"role": m["role"], "content": content}
        else:
            msg = {"role": m["role"], "content": " ".join(texts)}
        if calls:
            msg["tool_calls"] = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                for c in calls
            ]
        return [msg]

    def parse_response(self, raw):
        """OpenAI-compatible response JSON → the neutral Reply."""
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
        """Delegate to the base adapter with the ```cell emulation applied."""
        instr = (
            "\n\nTo run Python, reply with a fenced block:\n```cell\n<code>\n```\n"
            "Reply with plain text (no block) when done."
        )
        return self._inner.build_request(messages, (system or "") + instr, [], model)

    def parse_response(self, raw):
        """Base parse, then lift fenced ```cell blocks to tool calls."""
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


def _build_adapter(provider, fenced):
    """Adapter for a tier's provider; `fenced` wraps it in the ```cell
    emulation."""
    base = ADAPTERS[provider]()
    return FencedAdapter(base) if fenced else base


_TIMEOUT_S = 180  # a stalled provider connection must ERROR, never hang
                  # the conversation thread (live-caught with urlopen)

_EXCERPT = 400


@span(kind="getter")  # noqa: F821 - guest global
def chat(messages, system="", tier="codegen", tools=None, max_tokens=None):
    """One model call; returns the neutral Reply.

    `messages`: `[{"role": "user"|"assistant", "parts": [Part]}]` —
    Part is `{"type": "text", "text"}` for plain turns; `{"type":
    "file", "media_type", "data": <base64>, "name"?}` puts a file in
    the turn (image / pdf / text natively — `any.file_content` returns
    that shape; `read()` is the one-call form);
    `tool_call`/`tool_result`/`thinking` parts round-trip loop
    traffic. `tier`: "codegen" (default, the strong model),
    "classify" (fast/cheap — one-off judgments) or "vision" (file
    reads). `tools`: `[{name,
    description, input_schema?}]`, empty for plain completions.
    `max_tokens` overrides the default 32768 output cap (lower it for
    small classify-style calls). Returns `{parts, stop:
    "done"|"tool"|"length", usage: {in, out}}`; a ≥400 provider
    status raises `LlmError(status, body_excerpt)`."""
    prov = effect("config.get", {"key": f"llm.tier.{tier}"})["value"]  # noqa: F821 - guest global
    adapter = _build_adapter(prov["provider"], prov.get("fenced", False))
    req = adapter.build_request(messages, system, tools or [], prov["model"])
    if max_tokens:
        req["max_tokens"] = max_tokens
    # `about` (ADR-021 §1): the descriptor the host shows the human
    # when the key is missing — the one credential the agent cannot
    # ask for itself.
    host = prov["base_url"].split("//", 1)[-1].split("/", 1)[0]
    if prov["provider"] == "anthropic":
        url = prov["base_url"].rstrip("/") + "/v1/messages"
        headers = {"anthropic-version": "2023-06-01"}
        credential = {"ref": prov["api_key_ref"], "header": "x-api-key",
                      "about": {"label": "Anthropic API key", "hosts": [host],
                                "help": "https://console.anthropic.com/settings/keys"}}
    else:
        url = prov["base_url"].rstrip("/") + "/chat/completions"
        headers = {}
        credential = {"ref": prov["api_key_ref"], "header": "Authorization",
                      "prefix": "Bearer ",
                      "about": {"label": prov["provider"] + " API key",
                                "hosts": [host]}}
    resp = effect("http.post", {  # noqa: F821 - guest global
        "url": url, "headers": headers, "json": req,
        "timeout": _TIMEOUT_S, "credential": credential})
    if resp["status"] >= 400:
        raise LlmError(resp["status"], resp["body"][:_EXCERPT])
    return adapter.parse_response(json.loads(resp["body"]))


@span(kind="getter")  # noqa: F821 - guest global
def read(file, prompt, tier="vision", system="", max_tokens=None, space=None):
    """Ask the model about ONE file and return its answer text.

    `file`: an `any://f/<spaceId>/<fileId>` URI (the `[attachment …]`
    line of a chat message — pass it verbatim), a bare fileId with
    `space=`, or a `{mime|media_type, data: <base64>, name?}` dict
    (e.g. `any.file_content(...)`'s result). Natively readable:
    images (png/jpeg/gif/webp), PDF, text; anything else raises
    `UnsupportedMedia` before any call — convert to text first.
    `prompt` is the question; `system` optional. Returns the reply's
    text (str). Bytes cross the wire once per read — re-read rather
    than keeping files in the conversation (ADR-020 §5)."""
    if isinstance(file, str):
        any_ = use("any@v1")  # noqa: F821 - guest global
        if not file.startswith("any://f/") and not space:
            raise TypeError("a bare fileId needs space=; or pass the any://f/ URI")
        file = any_.file_content(space or file.split("/")[3], file)
    part = {"type": "file",
            "media_type": file.get("media_type") or file.get("mime"),
            "data": file["data"]}
    if file.get("name"):
        part["name"] = file["name"]
    reply = chat([{"role": "user", "parts": [part, {"type": "text", "text": prompt}]}],
                 system=system, tier=tier, max_tokens=max_tokens)
    return "\n".join(p["text"] for p in reply["parts"] if p["type"] == "text")
