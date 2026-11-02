"""programs/config@v1 — the agent config store surface, run under the
REAL guest kernel (kernelenv) with `config.get`/`config.set` faked.
The MCP row guard (ADR-032 §2/§3): a misspelt key ref never reaches
the store."""

import pytest
from kernelenv import load_kernel

URL = "https://api.oreilly.com/api/search/v1/mcp"


def kernel(rows=None):
    rows = rows if rows is not None else {}

    def eff(name, payload):
        if name == "config.get":
            if payload["key"] not in rows:
                raise KeyError(f"no config row {payload['key']!r}")
            return {"value": rows[payload["key"]]}
        if name == "config.set":
            rows[payload["key"]] = payload["value"]
            return {}
        raise AssertionError(f"unexpected effect {name}")

    app = load_kernel(effect=eff)
    return app.use("config@v1"), rows


def test_set_stores_a_well_formed_mcp_row_with_or_without_a_key():
    cfg, rows = kernel()
    row = {"transport": "http", "url": URL, "api_key_ref": "mcp.key.oreilly"}
    assert cfg.set("mcp.server.oreilly", row) == {"key": "mcp.server.oreilly", "value": row}
    assert rows["mcp.server.oreilly"] == row
    cfg.set("mcp.server.pub", {"url": "https://mcp.example/mcp", "api_key_ref": None})
    assert rows["mcp.server.pub"]["api_key_ref"] is None
    cfg.set("mcp.server.pub", None)          # unset passes untouched
    assert rows["mcp.server.pub"] is None


@pytest.mark.parametrize("ref", ["oreilly.key.mcp", "oreilly.mcp.key", "connector.key.oreilly",
                                 "llm.key.oreilly", 7])
def test_set_refuses_a_key_ref_outside_the_mcp_key_namespace(ref):
    cfg, rows = kernel()
    with pytest.raises(ValueError) as e:
        cfg.set("mcp.server.oreilly", {"transport": "http", "url": URL, "api_key_ref": ref})
    msg = str(e.value)
    assert "`mcp.key.oreilly`" in msg and "always `mcp.key.<id>`" in msg
    assert "mcp.server.oreilly" not in rows


def test_set_refuses_a_rowless_shape_and_a_bad_id():
    cfg, rows = kernel()
    with pytest.raises(ValueError, match="an MCP server row is"):
        cfg.set("mcp.server.x", {"transport": "http"})
    with pytest.raises(ValueError, match="an MCP server row is"):
        cfg.set("mcp.server.x", "not-an-object")
    with pytest.raises(ValueError, match=r"an MCP server id is \[a-z\]"):
        cfg.set("mcp.server.OReilly", {"url": URL, "api_key_ref": None})
    assert rows == {}


def test_other_keys_pass_straight_through():
    cfg, rows = kernel()
    tier = {"provider": "anthropic", "model": "m", "base_url": "https://api.anthropic.com",
            "api_key_ref": "llm.key.anthropic"}
    cfg.set("llm.tier.codegen", tier)
    assert rows["llm.tier.codegen"] == tier
    assert cfg.get("llm.tier.codegen") == tier
    assert cfg.set_model("llm.tier.codegen", "m2")["value"]["model"] == "m2"
