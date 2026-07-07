"""WasiEngine tests — M0 exit criteria 1 and 3.

Needs bin/kernel.wasm (`make kernel`); skipped when absent.
"""

from pathlib import Path

import pytest
from anyrt import trace as tr
from anyrt.builtin_effects import register_builtin_effects
from anyrt.effects import Broker, Registry, effect

KERNEL = Path(__file__).resolve().parents[2] / "bin" / "kernel.wasm"

pytestmark = pytest.mark.skipif(
    not KERNEL.exists(), reason="bin/kernel.wasm missing — run `make kernel`"
)


def make_engine(**kw):
    from anyrt.wasi import WasiEngine

    reg = Registry()
    register_builtin_effects(reg, env={"MY_FLAG": "on"})

    @effect("http.get", kind="read", registry=reg)
    def http_get(ctx, url):
        return {"status": 200, "body": f"body of {url}"}

    w = tr.TraceWriter(run={"id": "wasi-test"})
    broker = Broker(reg, w)
    return WasiEngine(broker, kernel_wasm=KERNEL, **kw), w


def test_cell_executes_persists_and_effects_record():
    eng, w = make_engine()
    r1 = eng.run_cell("x = 40 + 2\nprint('computed', x)\nx", cell_id="c1")
    assert r1.ok and r1.last_value == "42" and r1.prints == ["computed 42"]

    r2 = eng.run_cell("x * 10", cell_id="c2")  # persistence
    assert r2.ok and r2.last_value == "420"

    r3 = eng.run_cell("resp = http.get('https://a')\nresp.text", cell_id="c3")
    assert r3.ok and r3.last_value == "'body of https://a'"

    kinds = [(r["kind"], r.get("effect") or r.get("cell")) for r in w.records[1:]]
    assert kinds == [
        ("effect", "kernel.boot"),
        ("cell", "c1"), ("cell", "c2"),
        ("effect", "http.get"), ("cell", "c3"),
    ]
    eff = next(r for r in w.records if r["kind"] == "effect" and r["effect"] == "http.get")
    assert eff["cell"] == "c3" and eff["output"]["status"] == 200
    cell3 = w.records[-1]
    assert cell3["metrics"]["fuel_used"] > 0


def test_reset_drops_namespace():
    eng, _ = make_engine()
    assert eng.run_cell("y = 1", cell_id="c1").ok
    eng.reset()
    r = eng.run_cell("y", cell_id="c2")
    assert not r.ok and r.error is not None and r.error.type == "NameError"


def test_guest_error_has_traceback_and_kernel_survives():
    eng, _ = make_engine()
    r = eng.run_cell("z = [1,2]\nz[10]", cell_id="c1")
    assert not r.ok and r.error is not None and r.error.type == "IndexError"
    assert "IndexError" in r.error.traceback_str
    assert eng.run_cell("z[1]", cell_id="c2").last_value == "2"  # namespace intact


def test_fuel_exhaustion_interrupts():
    eng, w = make_engine(fuel_per_cell=2_000_000)
    r = eng.run_cell("while True: pass", cell_id="c1")
    assert not r.ok and r.interrupted


def test_epoch_timeout_interrupts_busy_loop():
    eng, _ = make_engine()
    r = eng.run_cell("while True: pass", cell_id="c1", timeout_s=0.3)
    assert not r.ok and r.interrupted


def test_memory_cap_yields_memoryerror_kernel_survives():
    eng, _ = make_engine(memory_bytes=96 * 1024 * 1024)
    r = eng.run_cell("blob = 'x' * (200 * 1024 * 1024)", cell_id="c1")
    assert not r.ok and r.error is not None
    assert (r.error.type == "MemoryError") or r.interrupted
    # kernel survives a denied allocation (MemoryError path)
    if r.error.type == "MemoryError":
        assert eng.run_cell("1 + 1", cell_id="c2").last_value == "2"


def test_namespace_denies_ambient_authority():
    eng, _ = make_engine()
    r = eng.run_cell("open('/etc/passwd')", cell_id="c1")
    assert not r.ok and r.error is not None and r.error.type == "NameError"
    r = eng.run_cell("eval('1+1')", cell_id="c2")
    assert not r.ok and r.error is not None and r.error.type == "NameError"
    r = eng.run_cell("import socket", cell_id="c3")
    assert not r.ok and r.error is not None and r.error.type == "ImportError"
    assert "effect boundary" in r.error.message
    assert "math" in r.error.message  # error-as-teaching: lists what IS available


def test_allowlisted_and_proxied_imports():
    eng, w = make_engine()
    r = eng.run_cell("import math\nmath.floor(3.7)", cell_id="c1")
    assert r.ok and r.last_value == "3"
    r = eng.run_cell("import datetime\nd = datetime.datetime.now()\nd.year >= 2026", cell_id="c2")
    assert r.ok and r.last_value == "True"
    assert any(x["kind"] == "effect" and x["effect"] == "time.now" for x in w.records)
    r = eng.run_cell("import random\n0 <= random.random() < 1", cell_id="c3")
    assert r.ok and r.last_value == "True"


def test_shim_globals_and_env_allowlist():
    eng, w = make_engine()
    code = "e = env('MY_FLAG')\nmissing = env('NOPE', 'dflt')\n(e, missing)"
    r = eng.run_cell(code, cell_id="c1")
    assert r.ok and r.last_value == "('on', 'dflt')"
    r = eng.run_cell("u = uuid4()\nlen(u)", cell_id="c2")
    assert r.ok and r.last_value == "36"


def test_values_store_across_cells():
    eng, _ = make_engine()
    assert eng.run_cell("print({'big': 1})\n[1, 2, 3]", cell_id="c1").ok
    r = eng.run_cell("v = values.get('c1', 'last')\nsum(v)", cell_id="c2")
    assert r.ok and r.last_value == "6"
    r = eng.run_cell("values.get('c1', 0)['big']", cell_id="c3")
    assert r.ok and r.last_value == "1"
    eng.reset()
    r = eng.run_cell("values.get('c1', 'last')", cell_id="c4")
    assert not r.ok and r.error is not None and r.error.type == "KeyError"
