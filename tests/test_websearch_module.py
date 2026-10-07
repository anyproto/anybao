"""programs/webSearch@v1 — the two search wires (ADR-008 §3), tested
host-side by exec-ing the guest source with a fake `effect` global:
the provider row picks the wire, each wire's request shape and parse,
per-query failures in place, and redirect resolution only where the
sources are redirects (Gemini grounding)."""

import json
from pathlib import Path

import pytest

SRC = (Path(__file__).resolve().parents[1] / "repos" / "_agent" / "programs"
       / "webSearch@v1" / "program.py").read_text()

GEMINI = {"provider": "gemini", "model": "gemini-3.7-flash",
          "base_url": "https://generativelanguage.googleapis.com",
          "api_key_ref": "google.key.gemini"}
PROXY = {"provider": "openai-compat", "model": "codex/gpt-5.5",
         "base_url": "http://127.0.0.1:8765/v1", "api_key_ref": "llm.key.anyai"}


def load(prov, posts=(), gets=None):
    """The guest with a fake host: `config.get` returns `prov`, the
    http.post batch answers `posts` in order, the http.get batch
    answers from `gets` (url -> location). Every effect is logged."""
    calls = []

    def effect(name, payload):
        calls.append((name, payload))
        if name == "config.get":
            return {"value": prov}
        if name == "batch" and payload["name"] == "http.post":
            return {"results": list(posts)}
        if name == "batch" and payload["name"] == "http.get":
            if gets is None:
                pytest.fail("unexpected redirect resolve")
            return {"results": [{"status": 302, "headers": {"location": gets[p["url"]]}}
                                for p in payload["payloads"]]}
        pytest.fail(f"unexpected effect {name!r}")

    g = {"effect": effect, "span": lambda name=None, kind=None: (lambda f: f)}
    exec(compile(SRC, "webSearch@v1.py", "exec"), g)
    return g, calls


def ok(body):
    return {"status": 200, "body": json.dumps(body)}


def openai_reply(content, annotations=()):
    return ok({"choices": [{"message": {"role": "assistant", "content": content,
                                        "annotations": list(annotations)}}]})


def cite(url, title=""):
    return {"type": "url_citation",
            "url_citation": {"url": url, "title": title, "start_index": 0, "end_index": 1}}


def test_openai_wire_request_shape():
    g, calls = load(PROXY, [openai_reply("answer")])
    g["search"]("rust 2027 edition")
    (_, batch), = [c for c in calls if c[0] == "batch"]
    req, = batch["payloads"]
    assert req["url"] == "http://127.0.0.1:8765/v1/chat/completions"
    assert req["json"]["model"] == "codex/gpt-5.5"
    assert req["json"]["web_search_options"] == {}
    assert req["json"]["messages"][1] == {"role": "user", "content": "rust 2027 edition"}
    assert req["json"]["messages"][0]["role"] == "system"
    # a bearer ref bound to the base_url host — never a key value
    assert req["credential"] == {
        "ref": "llm.key.anyai", "header": "Authorization", "prefix": "Bearer ",
        "about": {"label": "127.0.0.1:8765 API key", "hosts": ["127.0.0.1:8765"]}}


def test_openai_wire_without_a_key_sends_no_credential():
    g, calls = load({**PROXY, "api_key_ref": None}, [openai_reply("answer")])
    g["search"]("q")
    (_, batch), = [c for c in calls if c[0] == "batch"]
    assert "credential" not in batch["payloads"][0]


def test_openai_wire_sources_come_from_url_citations_and_never_resolve():
    reply = openai_reply("Rust 2027 ships in May.", [
        cite("https://blog.rust-lang.org/2027", "Rust Blog"),
        {"type": "file_citation", "file_citation": {"file_id": "f1"}},  # not a web source
        cite("https://blog.rust-lang.org/2027", "dup"),
        cite("https://lwn.net/rust", "LWN"),
    ])
    g, calls = load(PROXY, [reply])  # gets=None: any http.get fails the test
    out, = g["search"]("rust 2027")
    assert out == ("[1] rust 2027\nhttps://blog.rust-lang.org/2027\n\n"
                   "Rust 2027 ships in May.\n\nSources:\n- LWN — https://lwn.net/rust")
    assert [c[1]["name"] for c in calls if c[0] == "batch"] == ["http.post"]


def test_openai_wire_answer_without_sources_is_still_an_answer():
    g, _ = load(PROXY, [openai_reply("No results, but: 42.")])
    assert g["search"]("q") == ["[1] q\n\n\nNo results, but: 42."]


def test_openai_wire_failures_stay_in_their_slot():
    posts = [
        {"status": 401, "body": json.dumps({"error": {"message": "bad token"}})},
        openai_reply(""),
        {"error": {"type": "http.timeout", "message": "120s"}},
        {"status": 200, "body": "<html>"},
        openai_reply("fine", [cite("https://ok.example")]),
    ]
    g, _ = load(PROXY, posts)
    out = g["search"]("a", "b", "c", "d", "e")
    assert out[0] == '[ERROR] query 1 ("a") failed: bad token'
    assert out[1] == '[ERROR] query 2 ("b") failed: empty response from the search model'
    assert out[2] == '[ERROR] query 3 ("c") failed: http.timeout: 120s'
    assert out[3] == '[ERROR] query 4 ("d") failed: unparseable response (status 200)'
    assert out[4].startswith("[5] e\nhttps://ok.example\n\nfine")


def test_gemini_stays_the_default_wire_and_resolves_redirects():
    body = {"candidates": [{
        "content": {"parts": [{"text": "Answer."}]},
        "groundingMetadata": {"groundingChunks": [
            {"web": {"uri": "https://vertexaisearch.cloud.google.com/r/1", "title": "a.com"}},
            {"web": {"uri": "https://vertexaisearch.cloud.google.com/r/2", "title": "b.com"}}]},
    }]}
    gets = {"https://vertexaisearch.cloud.google.com/r/1": "https://a.com/x",
            "https://vertexaisearch.cloud.google.com/r/2": "https://b.com/y"}
    for prov in (GEMINI, {k: v for k, v in GEMINI.items() if k != "provider"}):
        g, calls = load(prov, [ok(body)], gets)
        out, = g["search"]("q")
        assert out == "[1] q\nhttps://a.com/x\n\nAnswer.\n\nSources:\n- b.com — https://b.com/y"
        req = [c for c in calls if c[0] == "batch"][0][1]["payloads"][0]
        assert req["url"].endswith("/v1beta/models/gemini-3.7-flash:generateContent")
        assert req["credential"]["header"] == "x-goog-api-key"


def test_unknown_provider_is_a_config_error_before_any_call():
    g, calls = load({**PROXY, "provider": "bing"})
    with pytest.raises(ValueError, match="unknown provider 'bing'"):
        g["search"]("q")
    assert [c[0] for c in calls] == ["config.get"]
