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


def lastrepr(r):
    assert r.last_value is not None
    return r.last_value.repr


def make_engine(programs=None, **kw):
    from anyrt.builtin_effects import DictResolver
    from anyrt.wasi import WasiEngine

    reg = Registry()
    register_builtin_effects(
        reg, env={"MY_FLAG": "on"}, resolver=DictResolver(programs or {})
    )

    @effect("http.get", kind="read", registry=reg)
    def http_get(ctx, url):
        return {"status": 200, "body": f"body of {url}"}

    w = tr.TraceWriter(run={"id": "wasi-test"})
    broker = Broker(reg, w)
    return WasiEngine(broker, kernel_wasm=KERNEL, **kw), w


def test_cell_executes_persists_and_effects_record():
    eng, w = make_engine()
    r1 = eng.run_cell("x = 40 + 2\nprint('computed', x)\nx", cell_id="c1")
    assert r1.ok and lastrepr(r1) == "42"
    assert [p.repr for p in r1.prints] == ["computed 42"]

    r2 = eng.run_cell("x * 10", cell_id="c2")  # persistence
    assert r2.ok and lastrepr(r2) == "420"

    r3 = eng.run_cell("resp = http.get('https://a')\nresp.text", cell_id="c3")
    assert r3.ok and lastrepr(r3) == "'body of https://a'"

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
    assert lastrepr(eng.run_cell("z[1]", cell_id="c2")) == "2"  # namespace intact


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
        assert lastrepr(eng.run_cell("1 + 1", cell_id="c2")) == "2"


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
    assert r.ok and lastrepr(r) == "3"
    r = eng.run_cell("import datetime\nd = datetime.datetime.now()\nd.year >= 2026", cell_id="c2")
    assert r.ok and lastrepr(r) == "True"
    assert any(x["kind"] == "effect" and x["effect"] == "time.now" for x in w.records)
    r = eng.run_cell("import random\n0 <= random.random() < 1", cell_id="c3")
    assert r.ok and lastrepr(r) == "True"


def test_shim_globals_and_env_allowlist():
    eng, w = make_engine()
    code = "e = env('MY_FLAG')\nmissing = env('NOPE', 'dflt')\n(e, missing)"
    r = eng.run_cell(code, cell_id="c1")
    assert r.ok and lastrepr(r) == "('on', 'dflt')"
    r = eng.run_cell("u = uuid4()\nlen(u)", cell_id="c2")
    assert r.ok and lastrepr(r) == "36"


def test_values_store_across_cells():
    eng, _ = make_engine()
    assert eng.run_cell("print({'big': 1})\n[1, 2, 3]", cell_id="c1").ok
    r = eng.run_cell("v = values.get('c1', 'last')\nsum(v)", cell_id="c2")
    assert r.ok and lastrepr(r) == "6"
    r = eng.run_cell("values.get('c1', 0)['big']", cell_id="c3")
    assert r.ok and lastrepr(r) == "1"
    eng.reset()
    r = eng.run_cell("values.get('c1', 'last')", cell_id="c4")
    assert not r.ok and r.error is not None and r.error.type == "KeyError"


def test_effects_views_from_guest():
    eng, w = make_engine()
    r = eng.run_cell("resp = http.get('https://a')\nresp.status", cell_id="c1")
    assert r.ok
    r = eng.run_cell(
        "recs = effects.of('c1')\nprint(recs)\n[e['effect'] for e in recs]",
        cell_id="c2",
    )
    assert r.ok and lastrepr(r) == "['http.get']"
    r = eng.run_cell(
        "seq = effects.of('c1')[0]['seq']\nfull = effects.get(seq)\nfull['output']['status']",
        cell_id="c3",
    )
    assert r.ok and lastrepr(r) == "200"


def test_http_get_many_input_order_and_records():
    eng, w = make_engine()
    code = (
        "urls = ['https://a', 'https://bb', 'https://ccc']\n"
        "rs = http.get_many(urls)\n"
        "[len(r.text) for r in rs]"
    )
    r = eng.run_cell(code, cell_id="c1")
    # stub returns "body of <url>" — lengths differ by url length
    assert r.ok
    assert lastrepr(r) == str([len(f"body of {u}") for u in ["https://a", "https://bb", "https://ccc"]])
    # three http.get records, input order, batch-tagged
    gets = [x for x in w.records if x["kind"] == "effect" and x["effect"] == "http.get"]
    assert len(gets) == 3
    assert [g["input"]["url"] for g in gets] == ["https://a", "https://bb", "https://ccc"]
    assert all("batch" in g["meta"] for g in gets)



def test_use_loads_program_and_caches():
    prog = "GREETING = 'hi'\n\ndef greet(name):\n    return GREETING + ' ' + name\n"
    eng, w = make_engine(programs={"greeter@v1": prog})
    r = eng.run_cell("g = use('greeter@v1')\ng.greet('bao')", cell_id="c1")
    assert r.ok and lastrepr(r) == "'hi bao'"
    # second use -> cache hit (one module.resolve total is fine; marker match)
    r = eng.run_cell("use('greeter@v1').GREETING", cell_id="c2")
    assert r.ok and lastrepr(r) == "'hi'"
    resolves = [x for x in w.records if x["kind"] == "effect" and x["effect"] == "module.resolve"]
    assert len(resolves) == 2  # probes every call
    assert resolves[0]["output"]["cache"] == "miss"
    assert resolves[1]["output"]["cache"] == "hit"  # marker unchanged
    assert resolves[0]["output"]["sourceHash"].startswith("sha256:")


def test_use_transitive_defining_space_first():
    helper = "def double(x):\n    return x * 2\n"
    main = "h = use('helper@v1')\n\ndef run(n):\n    return h.double(n) + 1\n"
    eng, _ = make_engine(programs={"helper@v1": helper, "main@v1": main})
    r = eng.run_cell("use('main@v1').run(10)", cell_id="c1")
    assert r.ok and lastrepr(r) == "21"


def test_use_unknown_program_errors():
    eng, _ = make_engine(programs={})
    r = eng.run_cell("use('nope@v1')", cell_id="c1")
    assert not r.ok and r.error is not None
    assert r.error.type == "EffectError" and "program not found" in r.error.message


def test_span_decorator_lifts_facade_to_one_pair():
    eng, w = make_engine()
    code = (
        "@span('helper.fetch_both')\n"
        "def fetch_both(a, b):\n"
        "    return http.get(a).status + http.get(b).status\n"
        "fetch_both('https://a', 'https://b')"
    )
    r = eng.run_cell(code, cell_id="c1")
    assert r.ok and lastrepr(r) == "400"
    spans = [x for x in w.records if x["kind"] == "span"]
    assert [s["phase"] for s in spans] == ["begin", "end"]
    begin, end = spans
    assert begin["name"] == "helper.fetch_both" and begin["cell"] == "c1"
    assert begin["input"] == {"args": ["https://a", "https://b"]}
    assert end["ok"] is True and end["output"] == 400
    assert end["meta"]["effects"] == 2 and end["meta"]["mutations"] == 0
    gets = [x for x in w.records if x["kind"] == "effect" and x["effect"] == "http.get"]
    assert [g["span"] for g in gets] == [begin["span"], begin["span"]]


def test_span_decorator_error_reraises_after_end_record():
    eng, w = make_engine()
    code = (
        "@span('helper.boom')\n"
        "def boom():\n"
        "    raise ValueError('nope')\n"
        "boom()"
    )
    r = eng.run_cell(code, cell_id="c1")
    assert not r.ok and r.error is not None and r.error.type == "ValueError"
    end = next(x for x in w.records if x["kind"] == "span" and x["phase"] == "end")
    assert end["ok"] is False and end["error"] == {"type": "ValueError", "message": "nope"}
    # no dangling span: the cell record follows a well-nested log
    assert w.records[-1]["kind"] == "cell" and w.records[-1]["cell"] == "c1"


def test_span_in_use_program_facade():
    prog = (
        "@span('greeter.greet')\n"
        "def greet(name):\n"
        "    return 'hi ' + name\n"
    )
    eng, w = make_engine(programs={"greeter@v1": prog})
    r = eng.run_cell("use('greeter@v1').greet('bao')", cell_id="c1")
    assert r.ok and lastrepr(r) == "'hi bao'"
    spans = [x for x in w.records if x["kind"] == "span"]
    assert [s["phase"] for s in spans] == ["begin", "end"]
    assert spans[0]["name"] == "greeter.greet"
    assert spans[0]["input"] == {"args": ["bao"]}
    assert spans[1]["output"] == "hi bao" and spans[1]["meta"]["effects"] == 0
