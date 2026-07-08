"""anybao CLI — llm-seed (docs/llm-fixtures.md one-real-call step),
tested with a monkeypatched transport (no key, no network)."""

import json
from pathlib import Path

import pytest
from anybao import cli
from anybao.llm import AnthropicAdapter

ANTHROPIC_RAW = {
    "content": [{"type": "text", "text": "OK"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 12, "output_tokens": 1},
}

FIXTURE = Path(__file__).parent / "fixtures" / "llm_anthropic.json"


def test_llm_seed_writes_raw_response(tmp_path, monkeypatch):
    seen = {}

    def fake_transport(secret_lookup):
        def transport(prov, req):
            seen["prov"], seen["req"] = prov, req
            return ANTHROPIC_RAW
        return transport

    monkeypatch.setattr(cli, "http_transport", fake_transport)
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


def test_seeded_fixture_translates_when_present():
    """The fixture-consumer side: once llm-seed has run for real, the
    adapter must translate the recorded wire shape."""
    if not FIXTURE.exists():
        pytest.skip("no seeded fixture yet — run `anybao llm-seed`")
    reply = AnthropicAdapter().parse_response(json.loads(FIXTURE.read_text()))
    assert reply["parts"] and reply["stop"] in ("done", "tool", "length")
    assert reply["usage"]["in"] > 0
