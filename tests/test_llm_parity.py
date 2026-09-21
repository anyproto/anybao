"""llm@v1 parity (ADR-005 §1.7): the reference conversation, per
profile/backend entry.

Two modes over ONE driver:

- **record** (`ANYBAO_LLM_PARITY=record`, live, key-gated): runs the
  conversation against the real backend and saves every request +
  response as the target's golden trace
  (`tests/fixtures/parity/<target>.json`). The key comes from
  `ANYBAO_SECRET_<REF>` (ref upper-cased, dots → underscores) and is
  injected as the header the credential names — it never lands in
  the fixture.
- **replay** (default, offline): serves the recorded responses back
  and asserts each outgoing request is byte-identical to the recorded
  one. A target with no golden trace SKIPS — it is not supported yet.

The conversation: a multi-turn `run_cell` loop with canned results,
an image part, a `length` stop, and — for `cache: "markers"` entries
— a cache read on the second call.
"""

import json
import os
import struct
import urllib.error
import urllib.request
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "repos" / "_agent" / "programs" / "llm@v1" / "program.py").read_text()
FIXTURES = ROOT / "tests" / "fixtures" / "parity"

# target → the tier config exactly as a `llm.tier.*` row would hold it
TARGETS = {
    "anthropic-claude": {
        "provider": "anthropic", "model": "claude-sonnet-5",
        "base_url": "https://api.anthropic.com", "api_key_ref": "llm.key.anthropic"},
    "anthropic-openai-compat": {  # Anthropic's OpenAI SDK compatibility endpoint
        "provider": "openai-compat", "model": "claude-sonnet-5",
        "base_url": "https://api.anthropic.com/v1", "api_key_ref": "llm.key.anthropic"},
    "gemini-openai-compat": {
        "provider": "openai-compat", "model": "gemini-3.7-flash",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key_ref": "google.key.gemini"},
    "openrouter-claude": {  # cache markers through OpenRouter
        "provider": "openai-compat", "model": "anthropic/claude-sonnet-5",
        "base_url": "https://openrouter.ai/api/v1", "api_key_ref": "llm.key.openrouter"},
    "openrouter-glm-5.3": {
        "provider": "openai-compat", "model": "z-ai/glm-5.3",
        "base_url": "https://openrouter.ai/api/v1", "api_key_ref": "llm.key.openrouter"},
    "openrouter-kimi-k3": {
        "provider": "openai-compat", "model": "moonshotai/kimi-k3",
        "base_url": "https://openrouter.ai/api/v1", "api_key_ref": "llm.key.openrouter"},
    "openrouter-deepseek-v4": {
        "provider": "openai-compat", "model": "deepseek/deepseek-v4-pro-0813",
        "base_url": "https://openrouter.ai/api/v1", "api_key_ref": "llm.key.openrouter"},
    "openrouter-deepseek-r1": {
        "provider": "openai-compat", "model": "deepseek/deepseek-r1-0528",
        "base_url": "https://openrouter.ai/api/v1", "api_key_ref": "llm.key.openrouter"},
    "openrouter-qwen3": {
        "provider": "openai-compat", "model": "qwen/qwen3-235b-a22b-2507",
        "base_url": "https://openrouter.ai/api/v1", "api_key_ref": "llm.key.openrouter"},
    "openai-terra": {  # gpt-5.6 chat/completions allows tools only with reasoning_effort none
        "provider": "openai-compat", "model": "gpt-5.6-terra",
        "base_url": "https://api.openai.com/v1", "api_key_ref": "llm.key.openai",
        "options": {"reasoning_effort": "none"}},
    "openrouter-gpt": {
        "provider": "openai-compat", "model": "openai/gpt-5-mini",
        "base_url": "https://openrouter.ai/api/v1", "api_key_ref": "llm.key.openrouter"},
    "openrouter-gemma-fenced": {
        "provider": "openai-compat", "model": "google/gemma-3-27b-it",
        "base_url": "https://openrouter.ai/api/v1", "api_key_ref": "llm.key.openrouter"},
    "ollama-qwen3": {  # keyless local server
        "provider": "openai-compat", "model": "qwen3:8b",
        "base_url": "http://127.0.0.1:11434/v1", "api_key_ref": None, "backend": "ollama"},
}

MODE = os.environ.get("ANYBAO_LLM_PARITY", "replay")

# --- the reference conversation --------------------------------------------

RUN_CELL = {
    "name": "run_cell",
    "description": "Execute a Python cell in a persistent kernel and return its output.",
    "input_schema": {"type": "object",
                     "properties": {"code": {"type": "string"}},
                     "required": ["code"]},
}

# a system block past every provider's minimum cacheable prefix (Anthropic:
# 1024 tokens on Sonnet) — deterministic filler, so the prefix is stable
_GUIDELINES = "\n".join(
    f"{i}. Guideline {i}: keep answers short, verify with a cell before "
    "stating a number, and never guess a value you could compute."
    for i in range(1, 121))
SYSTEM = ("You are a coding agent with exactly one tool, run_cell(code), which "
          "runs Python and returns the output. Use it for every computation. "
          "When you have the final answer, reply with plain text only — no "
          "tool call.\n\n" + _GUIDELINES)


def _png_2x2_red():
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 2 for _ in range(2))
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _pdf_one_word(word):
    """A minimal valid one-page PDF whose only content is `word` in
    Helvetica — uncompressed stream, real xref offsets."""
    content = f"BT /F1 36 Tf 72 400 Td ({word}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 420 595] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n"
            "%%EOF\n").encode()
    return bytes(out)


def _texts(reply):
    return " ".join(p["text"] for p in reply["parts"] if p["type"] == "text")


def drive(chat, traits, report):
    """The conversation; `report` collects the observations that are
    asserted after the run (kept out of the golden trace: usage varies)."""
    import base64
    msgs = [{"role": "user", "parts": [{"type": "text", "text":
             "Compute the sum of the first 20 prime numbers with run_cell, "
             "then tell me the number."}]}]
    calls = 0
    for _ in range(4):
        reply = chat(msgs, system=SYSTEM, tools=[RUN_CELL])
        msgs.append({"role": "assistant", "parts": reply["parts"]})
        if calls == 1 and traits["cache"] == "markers":
            report["cacheRead_second_call"] = reply["usage"]["cacheRead"]
        if reply["stop"] != "tool":
            break
        results = []
        for p in reply["parts"]:
            if p["type"] == "tool_call":
                calls += 1
                assert p["name"] == "run_cell", p
                assert "code" in p["args"] and not p.get("error"), p
                results.append({"type": "tool_result", "call_id": p["id"],
                                "content": "Last value: 639", "is_error": False})
        msgs.append({"role": "user", "parts": results})
    report["tool_calls"] = calls
    report["final_stop"] = reply["stop"]
    report["final_text"] = _texts(reply)

    # an image part on the same wire — a text-only profile must refuse it
    # BEFORE any call (ADR-020 §3), so the golden trace holds no 404
    png = base64.b64encode(_png_2x2_red()).decode()
    image_msg = [{"role": "user", "parts": [
        {"type": "file", "media_type": "image/png", "data": png},
        {"type": "text", "text": "What color is this image? Answer with one word."}]}]
    if traits["vision"]:
        reply = chat(image_msg, system="", tools=[])
        report["image_text"] = _texts(reply)
    else:
        try:
            chat(image_msg, system="", tools=[])
            report["image_text"] = "ACCEPTED (profile says text-only)"
        except Exception as e:  # UnsupportedMedia, raised in the guest
            report["image_text"] = f"refused: {type(e).__name__}"

    # a PDF part (ADR-020 §3): a backend whose wire carries documents
    # answers; any other refuses BEFORE any call — no request recorded
    pdf = base64.b64encode(_pdf_one_word("VIOLET")).decode()
    pdf_msg = [{"role": "user", "parts": [
        {"type": "file", "media_type": "application/pdf", "data": pdf, "name": "word.pdf"},
        {"type": "text", "text": "What single word is written in this PDF? "
                                 "Answer with that word only."}]}]
    if traits["pdf_input"] != "none":
        reply = chat(pdf_msg, system="", tools=[])
        report["pdf_text"] = _texts(reply)
    else:
        try:
            chat(pdf_msg, system="", tools=[])
            report["pdf_text"] = "ACCEPTED (backend does not carry documents)"
        except Exception as e:
            report["pdf_text"] = f"refused: {type(e).__name__}"

    # a truncated reply normalizes to `length`
    reply = chat([{"role": "user", "parts": [{"type": "text", "text":
                   "Write a 500-word essay about rivers."}]}],
                 system="", tools=[], max_tokens=24)
    report["length_stop"] = reply["stop"]


# --- hosts: live recorder / offline replayer ---------------------------------

def _secret_env(ref):
    return "ANYBAO_SECRET_" + ref.upper().replace(".", "_")


class Recorder:
    def __init__(self, prov):
        self.prov = prov
        self.steps = []

    def __call__(self, name, payload):
        if name == "config.get":
            return {"value": self.prov}
        assert name == "http.post"
        headers = {"content-type": "application/json", **payload["headers"]}
        cred = payload.get("credential")
        if cred:
            headers[cred["header"]] = cred.get("prefix", "") + os.environ[_secret_env(cred["ref"])]
        body = json.dumps(payload["json"]).encode()
        req = urllib.request.Request(payload["url"], data=body, headers=headers, method="POST")
        timeout = payload["timeout"]
        if isinstance(timeout, dict):
            # urlopen's timeout is per socket operation — the `idle`
            # semantics, not `total` (BOB-149; PR #58 review G6)
            timeout = timeout["idle"]
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = {"status": r.status, "headers": {}, "body": r.read().decode()}
        except urllib.error.HTTPError as e:
            resp = {"status": e.code, "headers": {}, "body": e.read().decode(errors="replace")}
        self.steps.append({"request": payload, "response": resp})
        return resp


class Replayer:
    def __init__(self, prov, steps):
        self.prov = prov
        self.steps = list(steps)
        self.n = 0

    def __call__(self, name, payload):
        if name == "config.get":
            return {"value": self.prov}
        assert name == "http.post"
        assert self.steps, f"call #{self.n}: more requests than the golden trace holds"
        step = self.steps.pop(0)
        self.n += 1
        want, got = step["request"], payload
        assert json.dumps(got, sort_keys=True) == json.dumps(want, sort_keys=True), (
            f"request #{self.n} drifted from the golden trace")
        return step["response"]


def _load(host):
    # the kernel's Blob/blob globals (ADR-026 §5): the adapters type-check
    # File parts against them; the parity conversation carries base64
    # parts only, so no blob.* effect is expected
    from kernelenv import load_kernel
    k = load_kernel(effect=lambda name, payload: pytest.fail(f"unexpected kernel effect {name!r}"))
    g = {"effect": host, "span": lambda name=None, kind=None: (lambda f: f),
         "use": lambda spec: pytest.fail(spec), "Blob": k.Blob, "blob": k.blob,
         "EffectError": type("EffectError", (Exception,), {})}   # the kernel's guest global
    exec(compile(SRC, "llm@v1.py", "exec"), g)
    return g


@pytest.mark.parametrize("target", sorted(TARGETS))
def test_parity(target):
    prov = TARGETS[target]
    path = FIXTURES / f"{target}.json"
    if MODE == "record":
        ref = prov.get("api_key_ref")
        if ref and not os.environ.get(_secret_env(ref)):
            pytest.skip(f"record mode: {_secret_env(ref)} not set")
        host = Recorder(prov)
    else:
        if not path.exists():
            pytest.skip(f"no golden trace for {target} — not a supported entry yet")
        host = Replayer(prov, json.loads(path.read_text())["steps"])

    g = _load(host)
    traits = g["profile"]("codegen")["traits"]
    report = {}
    try:
        drive(g["chat"], traits, report)
    finally:
        if MODE == "record" and host.steps:
            path.write_text(json.dumps({"target": target, "tier": prov, "steps": host.steps},
                                       indent=1, ensure_ascii=False) + "\n")
            print(f"\n[{target}] report: {json.dumps(report)}")

    assert report["tool_calls"] >= 1, "the model never called run_cell"
    assert report["final_stop"] == "done"
    assert "639" in report["final_text"], report["final_text"]
    if traits["vision"]:
        assert report["image_text"].strip(), "no answer for the image part"
    else:
        assert report["image_text"] == "refused: UnsupportedMedia", report["image_text"]
    if traits["pdf_input"] != "none":
        assert "violet" in report["pdf_text"].lower(), report["pdf_text"]
    else:
        assert report["pdf_text"] == "refused: UnsupportedMedia", report["pdf_text"]
    assert report["length_stop"] == "length"
    if traits["cache"] == "markers":
        assert report["cacheRead_second_call"] > 0, "no cache read on the second call"
    if MODE == "replay":
        assert not host.steps, "the golden trace holds more requests than the conversation made"
