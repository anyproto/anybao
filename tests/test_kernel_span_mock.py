"""The kernel's `span` decorator under a served facade (ADR-003 §4b,
ADR-028 §3a): when `span.begin` answers with `mock`, the body does NOT
run — the output is returned (or the error raised) and `span.end`
carries `mocked: True`. Real app.py, host-side, `_effect` patched."""

import pytest
from kernelenv import load_kernel


def _app_with(begin_replies):
    app = load_kernel(effect=lambda n, p: {})
    log = []

    def fake_effect(name, payload=None):
        log.append((name, payload))
        if name == "span.begin":
            return begin_replies.pop(0)
        return None

    app._effect = fake_effect
    return app, log


def test_served_output_skips_the_body():
    app, log = _app_with([{"span": "s1", "mock": {"ok": True, "output": {"objectId": "o"}}},
                          {"span": "s2"}])
    ran = []

    @app.span("any.create_object", kind="mutator")
    def create_object(space, body):
        ran.append(body)
        return {"objectId": "live"}

    assert create_object("sp", {"name": "x"}) == {"objectId": "o"}
    assert ran == []                       # the body never ran
    ends = [p for n, p in log if n == "span.end"]
    assert ends == [{"ok": True, "output": {"objectId": "o"}, "error": None, "mocked": True}]
    # no mock in the reply: the body runs as always
    assert create_object("sp", {"name": "y"}) == {"objectId": "live"}
    assert ran == [{"name": "y"}]
    assert [p for n, p in log if n == "span.end"][-1] == {"ok": True,
                                                          "output": {"objectId": "live"}}


def test_served_error_raises_from_the_call():
    app, log = _app_with([{"span": "s1", "mock": {"ok": False, "error": {
        "type": "RehearsalBlock", "message": "refused"}}}])

    @app.span("any.modify", kind="mutator")
    def modify(space, body):
        raise AssertionError("body must not run")

    with pytest.raises(app.EffectError, match="RehearsalBlock: refused"):
        modify("sp", {})
    end = [p for n, p in log if n == "span.end"][0]
    assert end["ok"] is False and end["mocked"] is True
    assert end["error"] == {"type": "RehearsalBlock", "message": "refused"}
