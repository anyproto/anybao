"""anybao CLI — llm-seed (docs/llm-fixtures.md one-real-call step),
tested with a monkeypatched transport (no key, no network)."""

import json
from pathlib import Path

import pytest
from anybao import cli

ANTHROPIC_RAW = {
    "content": [{"type": "text", "text": "OK"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 12, "output_tokens": 1},
}

FIXTURE = Path(__file__).parent / "fixtures" / "llm_anthropic.json"


def test_llm_seed_writes_raw_response(tmp_path, monkeypatch):
    seen = {}

    import json as _j

    from anybao import effects_impl

    def fake_request(method, url, *, params=None, headers=None,
                     json_body=None, body=None, timeout=None):
        seen["url"], seen["req"], seen["headers"] = url, json_body, headers
        return {"status": 200, "headers": {}, "body": _j.dumps(ANTHROPIC_RAW)}

    monkeypatch.setattr(effects_impl, "_http_request", fake_request)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    out = tmp_path / "llm_anthropic.json"
    rc = cli.main(["llm-seed", "--provider", "anthropic", "--out", str(out)])
    assert rc == 0
    assert json.loads(out.read_text()) == ANTHROPIC_RAW      # raw response only
    assert "sk-test" not in out.read_text()                   # never the key
    assert seen["req"]["messages"][0]["role"] == "user"       # real adapter build


def test_llm_seed_without_key_is_a_clean_error(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cli.main(["llm-seed", "--provider", "anthropic"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


@pytest.mark.parametrize("provider", ["anthropic", "openai-compat"])
def test_seeded_fixture_translates_when_present(provider):
    """The fixture-consumer side: once llm-seed has run for real, the
    adapter must translate the recorded wire shape."""
    fixture = FIXTURE.parent / f"llm_{provider}.json"
    if not fixture.exists():
        pytest.skip(f"no seeded {provider} fixture yet — run `anybao llm-seed`")
    adapter = cli.llm_module()["build_adapter"](provider, False)
    reply = adapter.parse_response(json.loads(fixture.read_text()))
    assert reply["parts"] and reply["stop"] in ("done", "tool", "length")
    assert reply["usage"]["in"] > 0
