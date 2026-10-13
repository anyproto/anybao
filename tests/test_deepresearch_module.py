"""programs/deepResearch@v1 under the real guest kernel — the provider
row guard (ADR-008 §4): Gemini's wire only, another provider a config
failure returned before any call."""

import pytest
from kernelenv import load_kernel


def _load(prov, post=None):
    """deepResearch@v1 with a fake host; `post` answers the first
    grounded call (its payload is logged), anything later fails."""
    calls, posts = [], []

    def effect(name, payload):
        calls.append(name)
        if name == "config.get":
            return {"value": prov}
        if name == "time.now":
            return {"epoch": 0.0}
        if name == "http.post" and post is not None:
            posts.append(payload)
            return post
        pytest.fail(f"unexpected effect {name!r}")

    dr = load_kernel(effect=effect).use("deepResearch@v1")
    dr.posts = posts
    return dr, calls


GEMINI = {"provider": "gemini", "model": "gemini-3.7-pro",
          "base_url": "https://generativelanguage.googleapis.com",
          "api_key_ref": "google.key.gemini"}
FAILED = {"status": 500, "headers": {}, "body": '{"error": {"message": "boom"}}'}


def test_the_grounded_call_follows_the_search_wire_rules():
    # ADR-008 §4 = §3's gemini wire: the row's timeout, the credential `about`
    dr, _ = _load({**GEMINI, "timeout": 300}, FAILED)
    assert dr.research("space1", "rust 2027?") == {
        "ok": False, "error": "Initial search failed: boom"}
    req, = dr.posts
    assert req["timeout"] == 300
    assert req["credential"] == {
        "ref": "google.key.gemini", "header": "x-goog-api-key",
        "about": {"label": "Gemini API key", "hosts": ["generativelanguage.googleapis.com"],
                  "help": "https://aistudio.google.com/apikey"}}
    # a keyless row (a local gateway) sends no credential; no timeout = 150 s
    dr, _ = _load({**GEMINI, "api_key_ref": None}, FAILED)
    dr.research("space1", "rust 2027?")
    assert "credential" not in dr.posts[0] and dr.posts[0]["timeout"] == 150


@pytest.mark.parametrize("provider", ["openai-compat", "bing"])
def test_a_non_gemini_provider_is_refused_before_any_call(provider):
    dr, calls = _load({"provider": provider, "model": "codex/gpt-5.5",
                       "base_url": "http://127.0.0.1:20123/v1",
                       "api_key_ref": "llm.key.anyai"})
    out = dr.research("space1", "what changed in rust 2027?")
    assert out == {"ok": False, "error": f"search.provider.deepresearch: unknown provider "
                                         f"{provider!r}; supported: gemini"}
    assert "http.post" not in calls


def test_a_grounded_call_keeps_at_most_ten_sources():
    import json
    dr, _ = _load(GEMINI)
    chunks = [{"web": {"uri": f"https://s{i}.example/", "domain": f"s{i}.example"}}
              for i in range(40)]
    body = {"candidates": [{"content": {"parts": [{"text": "A."}]},
                            "groundingMetadata": {"groundingChunks": chunks}}]}
    out = dr._parse({"status": 200, "headers": {}, "body": json.dumps(body)})
    assert out["ok"] and len(out["sources"]) == 10
