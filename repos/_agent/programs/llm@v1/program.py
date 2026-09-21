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
        supported = ", ".join(_MEDIA.get(provider, ())) or "none"
        super().__init__(f"{provider} cannot read {media_type!r} files; supported: {supported}")


# provider -> media classes carried natively (ADR-020 §3)
_MEDIA = {"anthropic": ("image/*", "application/pdf", "text/*"),
          "openai-compat": ("image/*",)}


def _wire_data(data, mime, data_uri=False):
    """A File part's `data` on the wire (ADR-026 §3/§5): a base64 str
    goes as-is; a Blob / ref goes as the ref the host expands — bare
    base64 inside JSON, or the `data:<mime>;base64,…` form for the
    OpenAI `image_url` / `file_data` fields."""
    if isinstance(data, Blob):  # noqa: F821 - guest global
        data = data.ref()
    if isinstance(data, dict) and "__blob" in data:
        return {**data, "encoding": "data-uri"} if data_uri else data
    return f"data:{mime};base64,{data}" if data_uri else data


def _text_of(data):
    """A text File part's payload as str: base64 decoded, or a Blob /
    ref read through the host."""
    if isinstance(data, Blob) or (isinstance(data, dict) and "__blob" in data):  # noqa: F821
        return blob.of(data).text()  # noqa: F821 - guest global
    return base64.b64decode(data).decode("utf-8", "replace")


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
    body excerpt (never the request — that may quote the conversation).
    `hint` names the fix when the body is a known configuration
    mistake rather than a transient failure (ADR-005 §1.6)."""

    def __init__(self, status, body):
        self.status = status
        self.body = body
        self.hint = _error_hint(status, body)
        msg = f"llm provider returned {status}: {body}"
        if self.hint:
            msg += f" — {self.hint}"
        super().__init__(msg)


class IncompleteReply(LlmError):
    """A 200 whose body is not a whole answer: a stream that ended
    without its terminator (`message_stop` / `[DONE]` or a
    `finish_reason`), an empty body, a non-JSON answer to a plain
    request. Carries 502 so the caller's transient-status path retries
    it (ADR-005 §1.6) — the old wire raised on `json.loads` of a
    truncated body; a stream cut mid-sentence must never be "done"."""

    def __init__(self, detail):
        super().__init__(502, detail)
        self.args = (f"llm reply incomplete: {detail}",)


_ANTHROPIC_KEY_NOTE = (
    "Create the key scoped to a single workspace: Console → Settings → API keys "
    "→ Create key → choose a workspace. A key linked to your account with no "
    "workspace is rejected — the API then wants a workspace id on every request, "
    "which bao does not send.")


def _error_hint(status, body):
    """A known provider error → what to change. Anthropic's 400
    `anthropic-workspace-id is required …` is a key-type problem, not
    a billing one: a personal / service-account key created without a
    single workspace needs a workspace id per request; bao asks for a
    workspace-scoped key instead (the host also raises the credential
    card for it, ADR-021 §2)."""
    if status == 400 and "anthropic-workspace-id" in body:
        return ("this is not a credit/billing error — the Anthropic key is not "
                "scoped to a workspace. " + _ANTHROPIC_KEY_NOTE +
                " Then enter the new key in the credential card (or Help → Import "
                "connector keys). Details: " + _ANTHROPIC_KEY_HELP)
    # Anthropic bills API use from prepaid credits, apart from any
    # Claude subscription; an empty balance is a 400 too. The key is
    # fine — no credential card, just the fix.
    if status == 400 and "credit balance is too low" in body:
        return ("the Anthropic account behind this key has no API credits — the key "
                "itself is fine. Add credits at Console → Plans & Billing "
                "(https://console.anthropic.com/settings/billing); a Claude "
                "subscription (Pro/Max) does not cover API use")
    return None


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
    "vision": bool,
    "signed_tool_calls": bool,
    "pdf_input": ("none", "file", "image_url"),
}

GENERIC_TRAITS = {
    "system_role": "native", "tool_mode": "native", "reasoning": "advisory",
    "thinking": "default", "cache": "auto", "context_window": 128000,
    "max_output": 8192, "sampling": {}, "prompt_style": "full",
    "instructions_at": "system", "malformed_retries": 2, "vision": True,
    "signed_tool_calls": False, "pdf_input": "none",
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
        "context_window": 1000000, "max_output": 65536,
        "signed_tool_calls": True}},
    # order matters: a family's newest generation lists before the family
    "deepseek-v4": {"match": r"deepseek-v4(?!-flash-vision)", "traits": {
        "reasoning": "roundtrip", "context_window": 1048576, "max_output": 65536,
        "vision": False}},
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
    "kimi-k3": {"match": r"kimi-k3", "traits": {
        "reasoning": "roundtrip", "context_window": 1048576, "max_output": 65536}},
    "kimi": {"match": r"kimi|moonshot", "traits": {
        "reasoning": "roundtrip", "context_window": 262144, "max_output": 32768}},
    "glm-5": {"match": r"glm-5(?!v)", "traits": {  # glm-5v-* are the vision line
        "reasoning": "roundtrip", "context_window": 1310720, "max_output": 65536,
        "vision": False}},
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
        elif allowed is bool:
            if not isinstance(v, bool):
                raise ConfigError(f"{where}: {k} must be bool")
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

def _sse_events(text):
    """SSE text → `[(event, data)]`: `data:` lines of one event joined
    with newlines, a blank line ends the event; `id:`/`retry:` and `:`
    comments are dropped. The host records a streamed response as this
    text in one `http.post` body (ADR-002 §1, BOB-149). Lines end at
    `\\n` (a trailing `\\r` dropped) — never `splitlines()`, which also
    breaks on U+2028/U+2029/U+0085, legal raw inside a JSON string."""
    events, name, data = [], None, []
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if line == "":
            if data:
                events.append((name, "\n".join(data)))
            name, data = None, []
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
    if data:
        events.append((name, "\n".join(data)))
    return events


# an Anthropic `error` event mid-stream, as the status the non-streaming
# wire would have answered with (so the caller's error handling is one)
# the `chat.completions` delta fields that arrive as text pieces and
# concatenate in the fold; every other scalar delta is first-wins
_STREAM_TEXT_FIELDS = ("content", "reasoning_content", "reasoning", "refusal")
_STREAM_ERROR_STATUS = {"overloaded_error": 529, "rate_limit_error": 429,
                        "api_error": 500, "authentication_error": 401,
                        "permission_error": 403, "invalid_request_error": 400,
                        "not_found_error": 404, "request_too_large": 413}


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
                # round-trip the SIGNED block verbatim (provider_state);
                # an unsigned thinking part (another provider's, or one
                # lifted from <think> tags) has no valid wire form here —
                # Anthropic requires the signature — so it stays out
                state = p.get("provider_state")
                signed = isinstance(state, dict) and state.get("type") in (
                    "thinking", "redacted_thinking")
                if signed:
                    blocks.append(state)
            elif t == "file":
                blocks.append(self._file_block(p))
        return {"role": m["role"], "content": blocks}

    @staticmethod
    def _file_block(p):
        mt = p["media_type"]
        cls = _media_class(mt)
        if cls == "image":
            return {"type": "image",
                    "source": {"type": "base64", "media_type": mt,
                               "data": _wire_data(p["data"], mt)}}
        if cls == "pdf":
            return {"type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf",
                               "data": _wire_data(p["data"], mt)}}
        if cls == "text":
            text = _text_of(p["data"])
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
                part = {"type": "tool_call", "id": block["id"],
                        "name": block["name"], "args": block["input"]}
                if block.get("input_error"):
                    # a streamed call cut mid-JSON (BOB-149): the run
                    # never dies on a malformed call — the loop answers
                    # the flagged call with an is_error result
                    part["error"] = block["input_error"]
                parts.append(part)
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

    def parse_stream(self, text):
        """Anthropic SSE text → the final message JSON `parse_response`
        reads: blocks assembled from `content_block_start` + deltas
        (text, `input_json_delta` tool input, thinking + signature),
        `stop_reason` and output usage from `message_delta`, input
        usage from `message_start`. An `error` event raises `LlmError`
        with the status the plain wire would have sent. Tolerant of a
        lossy relay: a delta for a block that never started opens it
        (typed by the delta), a hole in the indices is dropped, a tool
        input cut mid-JSON (`max_tokens`) folds to `{}` with an
        `input_error` the neutral Reply carries as the part's `error`.
        No `message_stop` = the stream was cut: `IncompleteReply`."""
        msg, blocks, partial, done = {}, [], {}, False

        def block(i):
            while len(blocks) <= i:
                blocks.append({})
            return blocks[i]

        for name, data in _sse_events(text):
            ev = json.loads(data)
            t = ev.get("type", name)
            if t == "message_start":
                msg = dict(ev["message"])
                blocks = list(msg.get("content") or [])
                msg["content"] = blocks
            elif t == "content_block_start":
                i = ev["index"]
                block(i).update(ev["content_block"])
                partial[i] = ""
            elif t == "content_block_delta":
                i, d = ev["index"], ev["delta"]
                b, dt = block(i), d.get("type")
                if dt == "text_delta":
                    b.setdefault("type", "text")
                    b["text"] = b.get("text", "") + d["text"]
                elif dt == "input_json_delta":
                    partial[i] = partial.get(i, "") + d["partial_json"]
                elif dt == "thinking_delta":
                    b.setdefault("type", "thinking")
                    b["thinking"] = b.get("thinking", "") + d["thinking"]
                elif dt == "signature_delta":
                    b["signature"] = b.get("signature", "") + d["signature"]
            elif t == "content_block_stop":
                i = ev["index"]
                b, raw = block(i), partial.get(i, "")
                if b.get("type") == "tool_use" and raw.strip():
                    try:
                        b["input"] = json.loads(raw)
                        if not isinstance(b["input"], dict):
                            raise ValueError("input must be a JSON object")
                    except ValueError as e:
                        b["input"] = {}
                        b["input_error"] = f"unparseable tool input ({e}): {raw[:200]}"
            elif t == "message_delta":
                msg.update(ev.get("delta") or {})
                usage = dict(msg.get("usage") or {})
                usage.update(ev.get("usage") or {})
                msg["usage"] = usage
            elif t == "error":
                err = ev.get("error") or {}
                raise LlmError(_STREAM_ERROR_STATUS.get(err.get("type"), 500),
                               json.dumps(ev)[:_EXCERPT])
            elif t == "message_stop":
                done = True
            # ping carries nothing
        if not done:
            raise IncompleteReply(
                f"stream ended without message_stop after {len(text)} chars")
        blocks[:] = [b for b in blocks if b.get("type")]
        return msg


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
                cls = _media_class(f["media_type"])
                if cls == "image":
                    content.append({"type": "image_url", "image_url": {
                        "url": _wire_data(f["data"], f["media_type"], data_uri=True)}})
                elif cls == "pdf" and traits.get("pdf_input") == "file":
                    # ADR-020 §3: the wire's document part — the backend
                    # (OpenAI natively, OpenRouter via its file-parser)
                    # reads it; the model never sees bytes
                    content.append({"type": "file", "file": {
                        "filename": f.get("name") or "document.pdf",
                        "file_data": _wire_data(f["data"], "application/pdf", data_uri=True)}})
                elif cls == "pdf" and traits.get("pdf_input") == "image_url":
                    # Gemini's OpenAI layer: no `file` part (400), but a
                    # PDF data URI under image_url is read as a document
                    content.append({"type": "image_url", "image_url": {
                        "url": _wire_data(f["data"], "application/pdf", data_uri=True)}})
                else:
                    raise UnsupportedMedia(f["media_type"], "openai-compat")
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
                if traits.get("signed_tool_calls"):
                    # Gemini 3+ rejects unsigned functionCall parts (400).
                    # Calls we construct ourselves (autorecall injection,
                    # lifted fenced/xml calls) carry no signature — stamp
                    # the documented skip sentinel; real signatures from
                    # provider_state above are left untouched.
                    google = tc.setdefault("extra_content", {}).setdefault("google", {})
                    google.setdefault("thought_signature",
                                      "skip_thought_signature_validator")
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
        u = raw.get("usage")
        if not u:
            # a server that ignores `stream_options.include_usage` (an
            # old build, a proxy) — the loop must know the count is no
            # count: `missing` says so; `stream: false` on the tier row
            # restores exact usage from the JSON body
            return {"parts": parts, "stop": stop,
                    "usage": {"in": 0, "out": 0, "cacheRead": 0, "cacheWrite": 0,
                              "missing": True}}
        det = u.get("prompt_tokens_details") or {}
        cached = det.get("cached_tokens", 0)
        written = det.get("cache_write_tokens", 0)  # OpenRouter, explicit markers
        # `in` is the UNCACHED prompt on every wire (Anthropic's
        # input_tokens semantics; prompt_tokens here includes the cached
        # and written parts) — the context in use is in + cacheRead + cacheWrite
        return {"parts": parts, "stop": stop,
                "usage": {"in": max(0, u.get("prompt_tokens", 0) - cached - written),
                          "out": u.get("completion_tokens", 0),
                          "cacheRead": cached,
                          "cacheWrite": written}}

    def parse_stream(self, text):
        """`chat.completions` chunk SSE → one completion JSON in the
        non-streaming shape: string deltas concatenated per field
        (`content`, `reasoning_content`, OpenRouter's `reasoning`),
        list deltas appended (`reasoning_details`), tool calls merged
        by `index` with their `arguments` concatenated, `finish_reason`
        from the chunk that carries it, `usage` from the last chunk
        that carries it, `[DONE]` ends. An `error` chunk raises.
        Only the text fields concatenate; every other scalar is
        first-wins (a repeated `role` is not `assistantassistant`).
        A tool-call `index` is coerced to int; a call without one
        opens a new call when it names the function, else continues
        the last; a null `id` never pins. Neither `[DONE]` nor a
        `finish_reason` = the stream was cut: `IncompleteReply`."""
        final, msg, calls, finish, done = {}, {}, {}, None, False

        def call_at(tc):
            fn = tc.get("function") or {}
            try:
                idx = int(tc["index"])
            except (KeyError, TypeError, ValueError):
                opener = tc.get("id") or fn.get("name")
                idx = len(calls) if opener or not calls else max(calls)
            cur = calls.setdefault(idx, {"index": idx,
                                         "function": {"name": "", "arguments": ""}})
            for kk, vv in tc.items():
                if kk == "function":
                    for fk, fv in fn.items():
                        if fk == "arguments":
                            cur["function"]["arguments"] += fv or ""
                        elif fv:
                            cur["function"][fk] = fv
                elif kk != "index" and vv and kk not in cur:
                    cur[kk] = vv

        for _name, data in _sse_events(text):
            if data.strip() == "[DONE]":
                done = True
                break
            ch = json.loads(data)
            if ch.get("error"):
                code = ch["error"].get("code") if isinstance(ch["error"], dict) else None
                raise LlmError(code if isinstance(code, int) and code >= 400 else 500,
                               json.dumps(ch)[:_EXCERPT])
            if not final:
                final = {k: v for k, v in ch.items() if k not in ("choices", "usage")}
            if ch.get("usage"):
                final["usage"] = ch["usage"]
            for choice in ch.get("choices") or []:
                if choice.get("index", 0) != 0:
                    continue
                for k, v in (choice.get("delta") or {}).items():
                    if k == "tool_calls":
                        for tc in v or []:
                            call_at(tc)
                    elif isinstance(v, str) and k in _STREAM_TEXT_FIELDS:
                        msg[k] = msg.get(k, "") + v
                    elif isinstance(v, list):
                        msg[k] = (msg.get(k) or []) + v
                    elif v is not None and k not in msg:
                        msg[k] = v
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        if not done and not finish:
            raise IncompleteReply(
                f"stream ended without [DONE] or a finish_reason after {len(text)} chars")
        if calls:
            msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        final["choices"] = [{"index": 0, "message": msg, "finish_reason": finish}]
        return final


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
    tool as a ```cell instruction with no tool API on the wire; a
    text-only model refuses IMAGE parts before any call (ADR-020 §3) —
    a PDF is the backend's to carry (`pdf_input`: OpenRouter's parser
    hands a text-only model the text), so the adapter decides that."""
    if not traits["vision"]:
        for m in messages:
            for p in m["parts"]:
                if p["type"] == "file" and _media_class(p["media_type"]) == "image":
                    raise UnsupportedMedia(p["media_type"], "this model (text-only)")
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
# a leaked chat-template trailer: a run of <|token|> markers (with the
# template's own words between them) at the very end of the text
_TEMPLATE_TOKEN_RE = re.compile(r"(?:\s*<\|[^|<>]*\|>\w*)+\s*$")
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
                for body in calls:
                    part = {"type": "tool_call", "id": f"xml_{len(lifted)}",
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
    if reply["stop"] == "done" and not any(p["type"] in ("text", "tool_call") for p in parts):
        # a finished reply that carries only reasoning IS the answer —
        # the server's template failed to split it (Kimi K3 on OpenRouter
        # returns the final text under `reasoning` with leaked
        # `<|close|>…` markers); silence would be worse than the text
        think = " ".join(p["text"] for p in parts if p["type"] == "thinking" and p.get("text"))
        think = _TEMPLATE_TOKEN_RE.sub("", think).strip()
        if think:
            parts = parts + [{"type": "text", "text": think}]
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
    # budget_tokens must be ≥ 1024 and < max_tokens: a small output cap
    # (a classify-style call) leaves no room — send no thinking block
    budget = min(16000, req["max_tokens"] - 1)
    if traits["thinking"] == "on" and budget >= 1024:
        req["thinking"] = {"type": "enabled", "budget_tokens": budget}
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


def _has_file_part(req):
    return any(isinstance(m.get("content"), list)
               and any(p.get("type") == "file" for p in m["content"])
               for m in req["messages"])


# OpenRouter parses PDFs for every model through its file-parser plugin
# (ADR-020 §3). Its default engine is the PAID OCR (mistral-ocr, $2 per
# 1k pages), so a request carrying a file part pins the free text
# extractor unless the tier's `options` already choose (`plugins` in
# options wins — options merge after this hook).
_OPENROUTER_PDF_PLUGIN = {"id": "file-parser", "pdf": {"engine": "cloudflare-ai"}}


def _finish_openrouter(req, traits):
    if traits["thinking"] == "on":
        req["reasoning"] = {"enabled": True}
    elif traits["thinking"] == "off":
        req["reasoning"] = {"enabled": False}
    if traits["cache"] == "markers":
        _mark_openrouter_cache(req)
    if _has_file_part(req) and "plugins" not in req:
        req["plugins"] = [dict(_OPENROUTER_PDF_PLUGIN, pdf=dict(_OPENROUTER_PDF_PLUGIN["pdf"]))]
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
# How each backend's wire carries a PDF (ADR-020 §3) — a host fact, not
# a model-family one: OpenAI reads the `file` part natively, OpenRouter
# parses it for any model, Anthropic has `document` (the native
# adapter ignores the carriage), Gemini's OpenAI layer rejects `file`
# but reads a PDF data URI under `image_url` (probed 2026-09-01).
# Absent = the wire has no known carriage: refuse before any call.
_PDF_CARRIAGE = {"anthropic": "file", "openai": "file", "openrouter": "file",
                 "gemini": "image_url"}

_ANTHROPIC_KEY_HELP = ("https://platform.claude.com/docs/en/manage-claude/"
                       "authentication#select-a-workspace")

BACKENDS = {
    "anthropic": {"path": "/v1/messages", "headers": {"anthropic-version": "2023-06-01"},
                  "credential": {"header": "x-api-key", "label": "Anthropic API key",
                                 "help": _ANTHROPIC_KEY_HELP, "note": _ANTHROPIC_KEY_NOTE},
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
    # local servers: no middlebox to keep alive, and usage-on-stream is
    # not a given on every build — the plain JSON wire by default
    # (`stream: true` on the tier row opts in; ADR-005 §1.2)
    "vllm": {"path": "/chat/completions", "credential": {**_BEARER, "label": "vLLM API key"},
             "finish": _finish_chat_template, "normalize": _normalize_openai_compat,
             "stream": False},
    "llamacpp": {"path": "/chat/completions",
                 "credential": {**_BEARER, "label": "llama.cpp API key"},
                 "finish": _finish_llamacpp, "normalize": _normalize_openai_compat,
                 "stream": False},
    "ollama": {"path": "/chat/completions", "credential": {**_BEARER, "label": "Ollama API key"},
               "normalize": _normalize_openai_compat, "stream": False},
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


# ADR-021 §8.1: the SaaS providers' keys, declared with the one API host
# each is ever sent to — a `base_url` edit cannot redirect them. A
# self-hosted backend (vllm, llama.cpp, ollama, generic) has no fixed
# host: its `llm.key.<name>` resolves like an open ref, bound to the
# tier's base_url host when its row is created.
__any_credentials__ = [
    {"ref": "llm.key.anthropic",
     "about": {"label": "Anthropic API key", "hosts": ["api.anthropic.com"],
               "help": "https://platform.claude.com/docs/en/manage-claude/authentication#select-a-workspace",  # noqa: E501
               "note": "Create the key scoped to a single workspace: Console → Settings → API keys → Create key → choose a workspace. A key linked to your account with no workspace is rejected — the API then wants a workspace id on every request, which bao does not send."}},  # noqa: E501
    {"ref": "llm.key.openai",
     "about": {"label": "OpenAI API key", "hosts": ["api.openai.com"],
               "help": "https://platform.openai.com/api-keys"}},
    {"ref": "llm.key.openrouter",
     "about": {"label": "OpenRouter API key", "hosts": ["openrouter.ai"],
               "help": "https://openrouter.ai/settings/keys"}},
    {"ref": "llm.key.gemini",
     "about": {"label": "Google AI Studio API key", "hosts": ["generativelanguage.googleapis.com"],
               "help": "https://aistudio.google.com/apikey"}},
    {"ref": "llm.key.deepseek",
     "about": {"label": "DeepSeek API key", "hosts": ["api.deepseek.com"],
               "help": "https://platform.deepseek.com/api_keys"}},
    {"ref": "llm.key.groq",
     "about": {"label": "Groq API key", "hosts": ["api.groq.com"],
               "help": "https://console.groq.com/keys"}},
    {"ref": "llm.key.together",
     "about": {"label": "Together API key", "hosts": ["api.together.xyz"],
               "help": "https://api.together.ai/settings/api-keys"}}
]


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
    if spec.get("note"):  # shown on the credential card at entry (ADR-021 §2)
        cred["about"]["note"] = spec["note"]
    if spec.get("prefix"):
        cred["prefix"] = spec["prefix"]
    return cred


def _effective(traits, backend):
    """Traits as this backend can honor them: `cache: "markers"` needs a
    backend that writes breakpoints, else it is the implicit prefix
    cache (`auto`); `pdf_input` (the PDF carriage) is granted by the
    backend, whatever the model family says."""
    if traits["cache"] == "markers" and backend not in _MARKER_BACKENDS:
        traits = {**traits, "cache": "auto"}
    carriage = _PDF_CARRIAGE.get(backend)
    if carriage and traits["pdf_input"] == "none":
        traits = {**traits, "pdf_input": carriage}
    return traits


def _resolve(tier):
    prov = effect("config.get", {"key": f"llm.tier.{tier}"})["value"]  # noqa: F821 - guest global
    if prov.get("provider") not in ADAPTERS:
        raise ConfigError(f"llm.tier.{tier}: unknown provider {prov.get('provider')!r}")
    name, traits = _resolve_traits(prov)
    backend = _backend_name(prov)
    return prov, name, backend, _effective(traits, backend)


# the streamed call's `{idle, total}` timeouts (ADR-005 §1.2, BOB-149)
_TIMEOUT = {"idle": 60, "total": 900}
# BOB-148: one transient failure must not end a run. Waits between
# attempts (so len+1 attempts); a `retry-after` header wins, capped.
_RETRY_DELAYS_S = (1, 4)
_RETRY_AFTER_CAP_S = 60

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
    "file", "media_type", "data": <Blob | base64>, "name"?}` puts a
    file in the turn (image / pdf / text natively — a Blob from
    `any.file_content(...)["blob"]` / `http.get(...).blob` rides as its
    ref, the host puts the bytes on the wire, ADR-026 §5; `read()` is
    the one-call form);
    `tool_call`/`tool_result`/`thinking` parts round-trip loop
    traffic. `tier`: "codegen" (default, the strong model),
    "classify" (fast/cheap — one-off judgments) or "vision" (file
    reads). `tools`: `[{name,
    description, input_schema?}]`, empty for plain completions.
    `max_tokens` overrides the profile's output cap (lower it for
    small classify-style calls). Returns `{parts, stop:
    "done"|"tool"|"length", usage: {in, out, cacheRead, cacheWrite}}`
    — `usage.missing: true` when the provider streamed no usage (the
    zeros are no count; `stream: false` on the tier row restores
    exact usage); a ≥400 provider status raises `LlmError(status,
    body_excerpt)`, a stream cut before its end `IncompleteReply`
    after the retries."""
    prov, _name, backend, traits = _resolve(tier)
    if max_tokens:
        traits = {**traits, "max_output": max_tokens}
    adapter = ADAPTERS[prov["provider"]]()
    messages, system, tools = _prepare(messages, system, tools or [], traits)
    req = adapter.build_request(messages, system, tools, prov["model"], traits)
    req.update(traits["sampling"])
    be = BACKENDS[backend]
    req = be.get("finish", lambda r, _t: r)(req, traits)
    # the tier row's `stream` wins over the backend's default; an
    # `options.stream` flips the body field and the host follows it
    req["stream"] = bool(prov.get("stream", be.get("stream", True)))
    if req["stream"] and be["path"] == "/chat/completions":
        req.setdefault("stream_options", {"include_usage": True})
    req.update(prov.get("options") or {})
    stream = bool(req.get("stream"))
    if not stream:
        req.pop("stream_options", None)
    timeout = _timeout_of(prov)
    payload = {"url": prov["base_url"].rstrip("/") + be["path"],
               "headers": dict(be.get("headers", {})), "json": req,
               "stream": stream, "timeout": timeout if stream else timeout["total"]}
    cred = _credential(prov, backend)
    if cred:
        payload["credential"] = cred
    raw = _post(payload, lambda body: _fold(adapter, body, stream))
    raw = be.get("normalize", lambda r: r)(raw)
    return _lift(adapter.parse_response(raw), traits)


def _fold(adapter, body, stream):
    """A 200 body → the provider's JSON: a JSON body as-is (a server
    that ignores `stream` answers JSON — one reader either way), SSE
    text through the adapter's fold; anything else is incomplete."""
    if body.lstrip().startswith("{"):
        try:
            return json.loads(body)
        except ValueError as e:
            raise IncompleteReply(f"unparseable JSON body ({e}) after {len(body)} chars") from None
    if stream and body.strip():
        return adapter.parse_stream(body)
    raise IncompleteReply("empty body" if not body.strip()
                          else f"non-JSON body: {body[:_EXCERPT]!r}")


def _timeout_of(prov):
    """A tier row's `timeout` as the `{idle, total}` the host takes:
    a plain number (the ADR-002 whole-request spelling) is the total,
    an object overrides the defaults key by key, absent = defaults."""
    t = prov.get("timeout")
    if isinstance(t, (int, float)) and not isinstance(t, bool):
        return {**_TIMEOUT, "total": t}
    return {**_TIMEOUT, **(t or {})}


def _retry_after(resp):
    """`retry-after` in whole seconds (the host lowercases header
    names), capped; None when absent or not a number (an HTTP date)."""
    try:
        return min(int((resp.get("headers") or {}).get("retry-after")), _RETRY_AFTER_CAP_S)
    except (TypeError, ValueError):
        return None


# the host's total-deadline failure (broker `http.post`, ADR-002 §1):
# the provider generated for the whole `total` — a retry would cost
# the same again (3 × 900 s is past every run deadline), so it is the
# one URLError that is not retried
_TOTAL_TIMEOUT_MARK = "exceeded the total timeout"


def _transient(status):
    return status == 429 or status >= 500


def _post(payload, fold):
    """The provider call with a bounded retry (ADR-005 §1.6, BOB-148),
    returning the FOLDED provider JSON — the fold runs inside the loop
    so what it learns can be retried.

    Retried: a transport failure (`URLError` — DNS, connection reset,
    a stalled read; NOT the total-deadline failure), a transient
    provider status (429, 5xx incl. 529 overloaded), and the same
    statuses arriving as a mid-stream `error` event under a 200, and an
    incomplete body (a stream cut before its terminator, an empty
    200). Not retried: any other 4xx, and any other effect failure (a
    denied capability, a missing secret, a mock miss). Each attempt is
    its own `http.post` record and the wait crosses the boundary as a
    `sleep` effect, so the trace shows every attempt and replay never
    waits. The last failure raises unchanged."""
    attempt = 0
    while True:
        try:
            resp = effect("http.post", payload)  # noqa: F821 - guest global
        except EffectError as e:  # noqa: F821 - guest global
            retry = str(e).startswith("URLError") and _TOTAL_TIMEOUT_MARK not in str(e)
            if not retry or attempt >= len(_RETRY_DELAYS_S):
                raise
            wait = _RETRY_DELAYS_S[attempt]
        else:
            status = resp["status"]
            if status >= 400:
                if not _transient(status) or attempt >= len(_RETRY_DELAYS_S):
                    raise LlmError(status, resp["body"][:_EXCERPT])
                wait = _retry_after(resp) or _RETRY_DELAYS_S[attempt]
            else:
                try:
                    return fold(resp["body"])
                except LlmError as e:
                    if not _transient(e.status) or attempt >= len(_RETRY_DELAYS_S):
                        raise
                    wait = _RETRY_DELAYS_S[attempt]
        effect("sleep", {"seconds": wait})  # noqa: F821 - guest global
        attempt += 1


@span(kind="getter")  # noqa: F821 - guest global
def read(file, prompt, tier="vision", system="", max_tokens=None, space=None):
    """Ask the model about ONE file and return its answer text.

    `file`: an `any://f/<spaceId>/<fileId>` URI (the `[attachment …]`
    line of a chat message — pass it verbatim), a bare fileId with
    `space=`, a Blob (`http.get(url).blob`), or a `{mime|media_type,
    blob | data: <base64>, name?}` dict (`any.file_content(...)`'s
    result). Natively readable:
    images (png/jpeg/gif/webp), text, and PDF on a backend whose wire
    carries documents (Anthropic, OpenAI, Gemini, any model via
    OpenRouter — the `pdf_input` trait); anything else raises `UnsupportedMedia`
    before any call — convert to text first, or route the read to a
    tier whose backend can.
    `prompt` is the question; `system` optional. Returns the reply's
    text (str). Bytes cross the wire once per read — re-read rather
    than keeping files in the conversation (ADR-020 §5)."""
    if isinstance(file, str):
        any_ = use("any@v1")  # noqa: F821 - guest global
        if not file.startswith("any://f/") and not space:
            raise TypeError("a bare fileId needs space=; or pass the any://f/ URI")
        file = any_.file_content(space or file.split("/")[3], file)
    if isinstance(file, Blob):  # noqa: F821 - guest global
        file = {"mime": file.mime, "blob": file}
    part = {"type": "file",
            "media_type": file.get("media_type") or file.get("mime"),
            "data": file["blob"] if file.get("blob") is not None else file["data"]}
    if file.get("name"):
        part["name"] = file["name"]
    reply = chat([{"role": "user", "parts": [part, {"type": "text", "text": prompt}]}],
                 system=system, tier=tier, max_tokens=max_tokens)
    return "\n".join(p["text"] for p in reply["parts"] if p["type"] == "text")
