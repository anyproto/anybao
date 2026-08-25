"""ADR-019 §1/§8: the kernel's instant helpers — the only seam between
`now()` seconds and the server's `{"$date": …}` instants."""

import pytest
from kernelenv import load_kernel


def _app(offset_s=7200):
    def effect(name, payload):
        if name == "time.now":
            return {"epoch": 1787673600.0, "offset_s": offset_s, "tz": None}
        raise AssertionError(name)
    return load_kernel(effect=effect)


def test_ts_s_unwraps_both_wire_forms_and_passes_numbers():
    app = _app()
    assert app.ts_s({"$date": "2026-08-25T16:00:00.000Z"}) == 1787673600.0
    assert app.ts_s({"$date": "2026-08-25T18:00:00+02:00"}) == 1787673600.0
    assert app.ts_s({"$date": 1787673600000}) == 1787673600.0
    assert app.ts_s(1787673600) == 1787673600.0      # kind-pinned number
    assert app.ts_s(None) is None
    assert app.ts_s("2026-08-25") is None           # a string is not a stamp
    assert app.ts_s(True) is None
    assert app.ts_s({"$date": "garbage"}) is None


def test_instant_builds_the_millis_literal():
    app = _app()
    assert app.instant(1787673600.0) == {"$date": 1787673600000}
    assert app.instant(1787673600.0004) == {"$date": 1787673600000}
    assert app.instant("2026-08-25T16:00:00Z") == {"$date": 1787673600000}
    assert app.instant("2026-08-25") == {"$date": 1787616000000}  # midnight UTC
    lit = {"$date": "2026-08-25T16:00:00.000Z"}
    assert app.instant(lit) is lit                  # an instant passes through
    with pytest.raises(TypeError):
        app.instant(None)
    with pytest.raises(TypeError):
        app.instant(True)


def test_fmt_ts_renders_in_the_host_zone_with_explicit_offset():
    app = _app(offset_s=7200)
    assert app.fmt_ts({"$date": "2026-08-25T16:00:00.000Z"}) == \
        "Tue 2026-08-25 18:00 +02:00"
    assert app.fmt_ts(1787673600, "%Y-%m-%d", offset_s=-5 * 3600) == \
        "2026-08-25 -05:00"
    assert app.fmt_ts(None) == "undated"
    assert app.tz_offset() == 7200
    assert _app(offset_s=None).tz_offset() == 0     # pre-§8 recording


def test_helpers_are_program_globals():
    ns = _app()._fresh_ns()
    assert {"ts_s", "instant", "fmt_ts", "tz_offset", "now"} <= set(ns)
