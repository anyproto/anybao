"""Model calls in the neutral message shape — the loop's own surface.

Use for one-off structured judgments (classification, extraction,
scoring) inside a cell — a sub-call, not a way to talk to the user.
`chat(messages, system=…, tier="classify")`; Part is `{type:
text|tool_call|tool_result|thinking|file, …}`, the reply `{parts, stop:
done|tool|length, usage: {in, out}}`. `read(file, prompt)` puts one
`any` file (image / pdf / text — an `any://f/…` attachment) in front
of the model and returns its answer (ADR-020). `profile(tier)` is the
resolved model profile (backend + traits) the loop budgets from."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# ADR-005 §1: three pure tables — adapter (wire family), backend (where
# the model is served), profile (which model, as TRAITS) — and the
# single http.post syscall as the effect boundary. The api key never
# enters the guest — the request names a `credential` (config ref +
# header), the HOST injects the header.

import base64
import json
import re


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


# --- Traits (ADR-005 §1.3): the closed vocabulary ---------------------------
# A profile declares intent in these terms; the backend spells the wire
# form, the loop budgets from the loop-facing ones. Unknown keys or
# values are a ConfigError at resolve time — never decorative.

TRAITS = {
    "system_role": ("native", "first_user"),
    "tool_mode": ("native", "fenced", "xml"),
    "reasoning": ("none", "advisory", "roundtrip"),
    "thinking": ("default", "on", "off"),
    "cache": ("auto", "markers", "none"),
    "context_window": int,
    "max_output": int,
    "sampling": dict,
    "prompt_style": ("full", "compact"),
    "instructions_at": ("system", "last_user"),
    "malformed_retries": int,
}

GENERIC_TRAITS = {
    "system_role": "native", "tool_mode": "native", "reasoning": "advisory",
    "thinking": "default", "cache": "auto", "context_window": 128000,
    "max_output": 8192, "sampling": {}, "prompt_style": "full",
    "instructions_at": "system", "malformed_retries": 2,
}

# --- Profiles (ADR-005 §1.7): one entry per model family ---------------------
# `match` is tried against the tier's `model` (first hit, in order)
# when the tier names no `profile`; `traits` are deviations from
# GENERIC_TRAITS. Supported = a golden parity trace in the tree
# (docs/llm-models.md lists the verified entries).

PROFILES = {
    "claude": {"match": r"claude", "traits": {
        "reasoning": "roundtrip", "cache": "markers",
        "context_window": 200000, "max_output": 32768}},
    "gpt": {"match": r"^(gpt-|o[1-9]|chatgpt)", "traits": {
        "context_window": 128000, "max_output": 16384}},
    "gemini": {"match": r"gemini", "traits": {
        "context_window": 1000000, "max_output": 65536}},
    "deepseek-r1": {"match": r"deepseek-(r1|reasoner)", "traits": {
        "system_role": "first_user", "instructions_at": "last_user",
        "reasoning": "roundtrip", "sampling": {"temperature": 0.6},
        "context_window": 64000}},
    "deepseek": {"match": r"deepseek", "traits": {
        "sampling": {"temperature": 0.0}, "context_window": 128000}},
    "qwen3": {"match": r"qwen3", "traits": {
        "context_window": 32768}},
    "llama": {"match": r"llama", "traits": {
        "prompt_style": "compact", "context_window": 128000}},
    "gemma": {"match": r"gemma", "traits": {
        "system_role": "first_user", "tool_mode": "fenced",
        "prompt_style": "compact", "context_window": 128000}},
    "mistral": {"match": r"mistral|mixtral|devstral|magistral", "traits": {
        "context_window": 128000}},
    "glm": {"match": r"glm", "traits": {"context_window": 128000}},
    # explicit-only profiles (no match): the tool-carriage fallbacks
    "fenced": {"match": None, "traits": {"tool_mode": "fenced"}},
    "xml": {"match": None, "traits": {"tool_mode": "xml"}},
    "generic": {"match": None, "traits": {}},
}


class ConfigError(Exception):
    """A tier names an unknown profile/backend, or a profile declares a
    trait outside the closed vocabulary."""


def _check_traits(traits, where):
    for k, v in traits.items():
        allowed = TRAITS.get(k)
        if allowed is None:
            raise ConfigError(f"{where}: unknown trait {k!r}")
        if isinstance(allowed, tuple):
            if v not in allowed:
                raise ConfigError(f"{where}: {k}={v!r} not in {allowed}")
        elif not isinstance(v, allowed) or isinstance(v, bool):
            raise ConfigError(f"{where}: {k} must be {allowed.__name__}")


def _profile_name(prov):
    name = prov.get("profile")
    if name:
        if name not in PROFILES:
            raise ConfigError(f"unknown profile {name!r}")
        return name
    model = (prov.get("model") or "").lower()
    for name, entry in PROFILES.items():
        if entry["match"] and re.search(entry["match"], model):
            return name
    return "generic"


def _resolve_traits(prov):
    name = _profile_name(prov)
    traits = dict(GENERIC_TRAITS)
    traits.update(PROFILES[name]["traits"])
    _check_traits(traits, f"profile {name!r}")
    return name, traits


# --- Provider adapters (ADR-005 §1.4): neutral <-> wire family ---------------

class AnthropicAdapter:
    """Native: thinking blocks round-trip via opaque provider_state.
    Caching: breakpoints at end of system and end of the conversation
    — the loop's prefix is append-only, so each call writes the cache
    the next call reads."""

    def build_request(self, messages, system, tools, model, traits):
        """Neutral messages → the Anthropic wire request dict."""
        api_msgs = [self._to_anthropic_msg(m, traits) for m in messages]
        req = {"model": model, "messages": api_msgs,
               "max_tokens": traits["max_output"]}
        if traits["cache"] == "markers":
            self._mark_cache(api_msgs)
        if system:
            block = {"type": "text", "text": system}
            if traits["cache"] == "markers":
                block["cache_control"] = {"type": "ephemeral"}
            req["system"] = [block]
        if tools:
            req["tools"] = [
                {"name": t["name"], "description": t.get("description", ""),
                 "input_schema": t.get("input_schema", {"type": "object"})}
                for t in tools
            ]
        return req

    def _to_anthropic_msg(self, m, traits):
        blocks = []
        for p in m["parts"]:
            t = p["type"]
            if t == "text":
                blocks.append({"type": "text", "text": p["text"]})
            elif t == "tool_call":
                blocks.append({"type": "tool_use", "id": p["id"],
                               "name": p["name"], "input": p["args"] or {}})
            elif t == "tool_result":
                blocks.append({"type": "tool_result", "tool_use_id": p["call_id"],
                               "content": p["content"], "is_error": p.get("is_error", False)})
            elif t == "thinking":
                if traits["reasoning"] != "roundtrip":
                    continue
                # round-trip the signed block verbatim (provider_state)
                state = p.get("provider_state")
                signed = isinstance(state, dict) and state.get("type") in (
                    "thinking", "redacted_thinking")
                if signed:
                    blocks.append(state)
                elif p.get("text"):
                    blocks.append({"type": "thinking", "thinking": p["text"]})
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


class OpenAICompatAdapter:
    """One adapter for every `/chat/completions` server. Reasoning text
    (`reasoning_content` after the backend's normalize) is captured as
    a Thinking part; the `reasoning` trait says whether it is resent
    (`roundtrip`: DeepSeek thinking mode / OpenRouter
    `reasoning_details` need it for tool-call continuity) or kept for
    the trace only (`advisory`)."""

    def build_request(self, messages, system, tools, model, traits):
        """Neutral messages → the OpenAI-compatible wire request dict."""
        api_msgs = []
        if system:
            api_msgs.append({"role": "system", "content": system})
        for m in messages:
            api_msgs.extend(self._to_openai_msgs(m, traits))
        req = {"model": model, "messages": api_msgs,
               "max_tokens": traits["max_output"]}
        if tools:
            req["tools"] = [
                {"type": "function",
                 "function": {"name": t["name"], "description": t.get("description", ""),
                              "parameters": t.get("input_schema", {"type": "object"})}}
                for t in tools
            ]
        return req

    def _to_openai_msgs(self, m, traits):
        texts = [p["text"] for p in m["parts"] if p["type"] == "text"]
        calls = [p for p in m["parts"] if p["type"] == "tool_call"]
        results = [p for p in m["parts"] if p["type"] == "tool_result"]
        out = []
        files = [p for p in m["parts"] if p["type"] == "file"]
        if results:  # tool results are their own role in openai
            for r in results:
                out.append({"role": "tool", "tool_call_id": r["call_id"],
                            "content": r["content"]})
            # text riding the same neutral message (the wrap-up
            # instruction after synthetic results, ADR-005 §2) is a
            # user message of its own, after the results
            if not (texts or files or calls):
                return out
        if files:
            content = [{"type": "text", "text": " ".join(texts)}] if texts else []
            for f in files:
                if _media_class(f["media_type"]) != "image":
                    raise UnsupportedMedia(f["media_type"], "openai-compat")
                content.append({"type": "image_url", "image_url": {
                    "url": f"data:{f['media_type']};base64,{f['data']}"}})
            msg = {"role": m["role"], "content": content}
        else:
            # an assistant turn that only calls tools carries null, never ""
            msg = {"role": m["role"], "content": " ".join(texts) or None}
            if msg["content"] is None and not calls:
                msg["content"] = ""
        if calls:
            msg["tool_calls"] = []
            for c in calls:
                tc = {"id": c["id"], "type": "function",
                      "function": {"name": c["name"], "arguments": json.dumps(c["args"] or {})}}
                # opaque server state on the call (Gemini's thought
                # signature rides `extra_content`) — round-tripped verbatim
                tc.update(c.get("provider_state") or {})
                msg["tool_calls"].append(tc)
        if traits["reasoning"] == "roundtrip" and m["role"] == "assistant":
            for p in m["parts"]:
                if p["type"] != "thinking":
                    continue
                state = p.get("provider_state") or {}
                if state.get("reasoning_details"):
                    msg["reasoning_details"] = state["reasoning_details"]
                elif p.get("text"):
                    msg["reasoning_content"] = p["text"]
        out.append(msg)
        return out

    def parse_response(self, raw):
        """OpenAI-compatible response JSON → the neutral Reply."""
        choice = raw["choices"][0]
        msg = choice["message"]
        parts = []
        think = msg.get("reasoning_content")
        details = msg.get("reasoning_details")
        if think or details:
            part = {"type": "thinking", "text": think or ""}
            if details:
                part["provider_state"] = {"reasoning_details": details}
            parts.append(part)
        if msg.get("content"):
            parts.append({"type": "text", "text": msg["content"]})
        for tc in msg.get("tool_calls", []) or []:
            raw_args = tc["function"].get("arguments") or "{}"
            part = {"type": "tool_call", "id": tc["id"],
                    "name": tc["function"]["name"], "args": {}}
            if tc.get("extra_content"):
                part["provider_state"] = {"extra_content": tc["extra_content"]}
            try:
                args = json.loads(raw_args)
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
                part["args"] = args
            except ValueError as e:
                # the run never dies on a malformed call: the loop
                # answers the flagged call with an is_error result
                part["error"] = f"unparseable tool arguments ({e}): {raw_args[:200]}"
            parts.append(part)
        fr = choice.get("finish_reason", "stop")
        stop = "tool" if fr == "tool_calls" else ("length" if fr == "length" else "done")
        u = raw.get("usage", {})
        det = u.get("prompt_tokens_details") or {}
        cached = det.get("cached_tokens", 0)
        # `in` is the UNCACHED prompt on every wire (Anthropic's
        # input_tokens semantics; prompt_tokens here includes the cached
        # part) — the context in use is in + cacheRead + cacheWrite
        return {"parts": parts, "stop": stop,
                "usage": {"in": max(0, u.get("prompt_tokens", 0) - cached),
                          "out": u.get("completion_tokens", 0),
                          "cacheRead": cached,
                          "cacheWrite": 0}}


ADAPTERS = {
    "anthropic": AnthropicAdapter,
    "openai-compat": OpenAICompatAdapter,
}


# --- Profile hooks (ADR-005 §1.2): prepare / lift, driven by traits ---------

_FENCED_INSTR = (
    "\n\nTo run Python, reply with a fenced block:\n```cell\n<code>\n```\n"
    "Reply with plain text (no block) when done."
)


def _prepare(messages, system, tools, traits):
    """Prompt-level shaping before the wire: no-system-role models get
    the system text ahead of the first user turn; `fenced` carries the
    tool as a ```cell instruction with no tool API on the wire."""
    if traits["tool_mode"] == "fenced" and tools:
        system = (system or "") + _FENCED_INSTR
        tools = []
    if traits["system_role"] == "first_user" and system:
        messages = [dict(m) for m in messages]
        for m in messages:
            if m["role"] == "user":
                parts = list(m["parts"])
                i = next((i for i, p in enumerate(parts) if p["type"] == "text"), None)
                if i is None:
                    parts.insert(0, {"type": "text", "text": system})
                else:
                    parts[i] = {**parts[i], "text": system + "\n\n---\n\n" + parts[i]["text"]}
                m["parts"] = parts
                break
        system = ""
    return messages, system, tools


_THINK_RE = re.compile(r"<think>(.*?)</think>\s*", re.S)
_XML_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def _lift(reply, traits):
    """Lift what the server left in plain text: `<think>` blocks →
    Thinking parts (kept for the trace); ```cell (fenced) or
    `<tool_call>` JSON (xml) → ToolCall parts, stop → tool."""
    parts = []
    lifted = []
    for p in reply["parts"]:
        if p["type"] != "text":
            parts.append(p)
            continue
        text = p["text"]
        if traits["reasoning"] != "none":
            for m in _THINK_RE.finditer(text):
                if m.group(1).strip():
                    parts.append({"type": "thinking", "text": m.group(1).strip()})
            text = _THINK_RE.sub("", text)
        if traits["tool_mode"] == "fenced":
            code = _extract_fenced(text)
            if code is not None:
                lifted.append({"type": "tool_call", "id": f"fenced_{len(lifted)}",
                               "name": "run_cell", "args": {"code": code}})
                continue
        elif traits["tool_mode"] == "xml":
            calls = _XML_CALL_RE.findall(text)
            if calls:
                for j, body in enumerate(calls):
                    part = {"type": "tool_call", "id": f"xml_{len(lifted) + j}",
                            "name": "run_cell", "args": {}}
                    try:
                        call = json.loads(body)
                        part["name"] = call.get("name") or "run_cell"
                        part["args"] = call.get("arguments") or call.get("args") or {}
                    except ValueError as e:
                        part["error"] = f"unparseable <tool_call> ({e}): {body[:200]}"
                    lifted.append(part)
                text = _XML_CALL_RE.sub("", text).strip()
                if text:
                    parts.append({"type": "text", "text": text})
                continue
        if text.strip():
            parts.append({**p, "text": text})
    if lifted:
        return {"parts": parts + lifted, "stop": "tool", "usage": reply["usage"]}
    return {**reply, "parts": parts}


def _extract_fenced(text):
    marker = "```cell"
    i = text.find(marker)
    if i == -1:
        return None
    rest = text[i + len(marker):]
    end = rest.find("```")
    return rest[:end].strip("\n") if end != -1 else rest.strip("\n")


# --- Backends (ADR-005 §1.1): where the model is served ----------------------
# Each entry: `path` (appended to base_url), `credential` (header +
# prefix + the ADR-021 `about` label/help), `finish` (request dict →
# request dict: parameter spelling, extras, cache markers), `normalize`
# (raw response → raw response: field-name unification). Missing hooks
# are identity.

def _thinking_reasoning_effort(req, traits):
    if traits["thinking"] == "on":
        req["reasoning_effort"] = "high"
    elif traits["thinking"] == "off":
        req["reasoning_effort"] = "none"
    return req


def _finish_anthropic(req, traits):
    if traits["thinking"] == "on":
        req["thinking"] = {"type": "enabled",
                           "budget_tokens": max(1024, min(16000, req["max_tokens"] - 1))}
    return req


def _finish_openai(req, traits):
    req["max_completion_tokens"] = req.pop("max_tokens")
    return _thinking_reasoning_effort(req, traits)


def _mark_openrouter_cache(req):
    """Anthropic/Gemini through OpenRouter: explicit `cache_control` on
    the system text and on the last user/assistant text part — the
    same two breakpoints the native adapter sets."""
    msgs = req["messages"]
    for m in msgs:
        if m["role"] == "system" and isinstance(m.get("content"), str):
            m["content"] = [{"type": "text", "text": m["content"],
                             "cache_control": {"type": "ephemeral"}}]
            break
    for m in reversed(msgs):
        if m["role"] not in ("user", "assistant") or m.get("content") in (None, ""):
            continue
        if isinstance(m["content"], str):
            m["content"] = [{"type": "text", "text": m["content"],
                             "cache_control": {"type": "ephemeral"}}]
        else:
            last = m["content"][-1]
            m["content"][-1] = {**last, "cache_control": {"type": "ephemeral"}}
        break
    return req


def _finish_openrouter(req, traits):
    if traits["thinking"] == "on":
        req["reasoning"] = {"enabled": True}
    elif traits["thinking"] == "off":
        req["reasoning"] = {"enabled": False}
    if traits["cache"] == "markers":
        _mark_openrouter_cache(req)
    return req


def _finish_chat_template(req, traits):
    """vLLM / llama.cpp: thinking is a chat-template switch."""
    if traits["thinking"] != "default":
        req["chat_template_kwargs"] = {"enable_thinking": traits["thinking"] == "on"}
    return req


def _finish_llamacpp(req, traits):
    req["cache_prompt"] = True
    return _finish_chat_template(req, traits)


def _normalize_openai_compat(raw):
    """`reasoning` (OpenRouter/Together) → `reasoning_content`;
    DeepSeek's `prompt_cache_hit_tokens` → `cached_tokens`."""
    for ch in raw.get("choices", []) or []:
        msg = ch.get("message") or {}
        if msg.get("reasoning") and not msg.get("reasoning_content"):
            msg["reasoning_content"] = msg["reasoning"]
    u = raw.get("usage") or {}
    details = u.get("prompt_tokens_details") or {}
    if "prompt_cache_hit_tokens" in u and not details.get("cached_tokens"):
        u["prompt_tokens_details"] = {**details, "cached_tokens": u["prompt_cache_hit_tokens"]}
    return raw


_BEARER = {"header": "Authorization", "prefix": "Bearer "}

# backends that can write explicit cache breakpoints; elsewhere a
# profile's `cache: "markers"` resolves to "auto" (§1.5)
_MARKER_BACKENDS = ("anthropic", "openrouter")

BACKENDS = {
    "anthropic": {"path": "/v1/messages", "headers": {"anthropic-version": "2023-06-01"},
                  "credential": {"header": "x-api-key", "label": "Anthropic API key",
                                 "help": "https://console.anthropic.com/settings/keys"},
                  "finish": _finish_anthropic},
    "openai": {"path": "/chat/completions",
               "credential": {**_BEARER, "label": "OpenAI API key",
                              "help": "https://platform.openai.com/api-keys"},
               "finish": _finish_openai, "normalize": _normalize_openai_compat},
    "openrouter": {"path": "/chat/completions",
                   "credential": {**_BEARER, "label": "OpenRouter API key",
                                  "help": "https://openrouter.ai/settings/keys"},
                   "finish": _finish_openrouter, "normalize": _normalize_openai_compat},
    "gemini": {"path": "/chat/completions",
               "credential": {**_BEARER, "label": "Google AI Studio API key",
                              "help": "https://aistudio.google.com/apikey"},
               "finish": _thinking_reasoning_effort, "normalize": _normalize_openai_compat},
    "deepseek": {"path": "/chat/completions",
                 "credential": {**_BEARER, "label": "DeepSeek API key",
                                "help": "https://platform.deepseek.com/api_keys"},
                 "normalize": _normalize_openai_compat},
    "groq": {"path": "/chat/completions",
             "credential": {**_BEARER, "label": "Groq API key",
                            "help": "https://console.groq.com/keys"},
             "normalize": _normalize_openai_compat},
    "together": {"path": "/chat/completions",
                 "credential": {**_BEARER, "label": "Together API key",
                                "help": "https://api.together.ai/settings/api-keys"},
                 "normalize": _normalize_openai_compat},
    "vllm": {"path": "/chat/completions", "credential": {**_BEARER, "label": "vLLM API key"},
             "finish": _finish_chat_template, "normalize": _normalize_openai_compat},
    "llamacpp": {"path": "/chat/completions",
                 "credential": {**_BEARER, "label": "llama.cpp API key"},
                 "finish": _finish_llamacpp, "normalize": _normalize_openai_compat},
    "ollama": {"path": "/chat/completions", "credential": {**_BEARER, "label": "Ollama API key"},
               "normalize": _normalize_openai_compat},
    "generic": {"path": "/chat/completions", "credential": {**_BEARER, "label": "API key"},
                "normalize": _normalize_openai_compat},
}

# well-known hosts → backend, when the tier names none
_HOST_BACKENDS = (
    ("api.anthropic.com", "anthropic"), ("openrouter.ai", "openrouter"),
    ("api.openai.com", "openai"), ("generativelanguage.googleapis.com", "gemini"),
    ("api.deepseek.com", "deepseek"), ("api.groq.com", "groq"),
    ("api.together.xyz", "together"),
)


def _host(base_url):
    return base_url.split("//", 1)[-1].split("/", 1)[0]


def _backend_name(prov):
    name = prov.get("backend")
    if name:
        if name not in BACKENDS:
            raise ConfigError(f"unknown backend {name!r}")
        return name
    if prov["provider"] == "anthropic":
        return "anthropic"
    host = _host(prov.get("base_url", ""))
    for h, b in _HOST_BACKENDS:
        if host == h and b != "anthropic":
            return b
    return "generic"


def _credential(prov, backend):
    """The ADR-021 §1 credential: ref + header + `about` — or None when
    the tier names no key (`api_key_ref: null`, a local server)."""
    ref = prov.get("api_key_ref")
    if not ref:
        return None
    spec = BACKENDS[backend]["credential"]
    host = _host(prov["base_url"])
    label = spec["label"] if spec["label"] != "API key" else f"{host} API key"
    cred = {"ref": ref, "header": spec["header"],
            "about": {"label": label, "hosts": [host], "help": spec.get("help")}}
    if spec.get("prefix"):
        cred["prefix"] = spec["prefix"]
    return cred


def _effective(traits, backend):
    """Traits as this backend can honor them: `cache: "markers"` needs a
    backend that writes breakpoints, else it is the implicit prefix
    cache (`auto`)."""
    if traits["cache"] == "markers" and backend not in _MARKER_BACKENDS:
        traits = {**traits, "cache": "auto"}
    return traits


def _resolve(tier):
    prov = effect("config.get", {"key": f"llm.tier.{tier}"})["value"]  # noqa: F821 - guest global
    if prov.get("provider") not in ADAPTERS:
        raise ConfigError(f"llm.tier.{tier}: unknown provider {prov.get('provider')!r}")
    name, traits = _resolve_traits(prov)
    backend = _backend_name(prov)
    return prov, name, backend, _effective(traits, backend)


_TIMEOUT_S = 180  # a stalled provider connection must ERROR, never hang
                  # the conversation thread (live-caught with urlopen)

_EXCERPT = 400


@span(kind="getter")  # noqa: F821 - guest global
def profile(tier="codegen"):
    """The resolved profile of a tier: `{profile, backend, model,
    traits}` — traits are the loop's budget inputs (`context_window`,
    `max_output`, `prompt_style`, `instructions_at`, `tool_mode`,
    `malformed_retries`; ADR-005 §1.3)."""
    prov, name, backend, traits = _resolve(tier)
    return {"profile": name, "backend": backend, "model": prov["model"],
            "traits": traits}


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
    `max_tokens` overrides the profile's output cap (lower it for
    small classify-style calls). Returns `{parts, stop:
    "done"|"tool"|"length", usage: {in, out}}`; a ≥400 provider
    status raises `LlmError(status, body_excerpt)`."""
    prov, _name, backend, traits = _resolve(tier)
    if max_tokens:
        traits = {**traits, "max_output": max_tokens}
    adapter = ADAPTERS[prov["provider"]]()
    messages, system, tools = _prepare(messages, system, tools or [], traits)
    req = adapter.build_request(messages, system, tools, prov["model"], traits)
    req.update(traits["sampling"])
    be = BACKENDS[backend]
    req = be.get("finish", lambda r, _t: r)(req, traits)
    req.update(prov.get("options") or {})
    payload = {"url": prov["base_url"].rstrip("/") + be["path"],
               "headers": dict(be.get("headers", {})), "json": req,
               "timeout": _TIMEOUT_S}
    cred = _credential(prov, backend)
    if cred:
        payload["credential"] = cred
    resp = effect("http.post", payload)  # noqa: F821 - guest global
    if resp["status"] >= 400:
        raise LlmError(resp["status"], resp["body"][:_EXCERPT])
    raw = be.get("normalize", lambda r: r)(json.loads(resp["body"]))
    return _lift(adapter.parse_response(raw), traits)


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
