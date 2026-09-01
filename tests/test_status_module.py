"""programs/status@v1 — the status-line tool (ADR-025 §3) under the
real guest kernel. Pinned: the line crosses the boundary as ONE
`bao.status` effect (the guest never publishes bus events), None/""
clear, and a serve-less runtime's typed `not_configured` failure
propagates as EffectError — never a silent no-op."""

import pytest
from kernelenv import load_kernel


def _load(calls=None, fail=None):
    def effect(name, payload):
        assert name == "bao.status", f"unexpected effect {name}"
        if fail is not None:
            raise fail
        calls.append(payload)
        line = payload["line"].strip()
        return {"ok": True, "line": line, "at": 100.0}

    app = load_kernel(effect=effect)
    return app.use("status@v1")


def test_set_crosses_as_one_bao_status_effect():
    calls = []
    status = _load(calls)
    out = status.set("  reindexing the email corpus  ")
    assert calls == [{"line": "  reindexing the email corpus  "}]
    assert out["line"] == "reindexing the email corpus"


def test_none_and_empty_clear():
    calls = []
    status = _load(calls)
    status.set(None)
    status.set("")
    assert [c["line"] for c in calls] == ["", ""]


def test_not_configured_propagates():
    # anyrt run (no presence wiring): the broker fails typed — the
    # tool surfaces it, never a silent no-op (ADR-025 §3)
    not_configured = type("not_configured", (Exception,), {})
    status = _load(fail=not_configured("serve only"))
    with pytest.raises(Exception, match="not_configured"):
        status.set("x")
