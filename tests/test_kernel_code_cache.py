"""ADR-032 §7: the compile-module export and use() on a code-cache hit."""

import base64
import marshal

from kernelenv import load_kernel

SRC = "VALUE = 41\n\n\ndef main(args):\n    return VALUE + args\n"


def _host(name, payload):
    if name == "runtime.get":
        return {"value": None}
    return {}


def test_compile_module_marshals_the_code_use_would_build():
    app = load_kernel(effect=_host)
    raw = app.WitWorld().compile_module(SRC, "<calc@v1>")
    code = marshal.loads(raw)
    assert code.co_filename == "<calc@v1>"
    assert marshal.dumps(app._compile_program(SRC, "<calc@v1>")) == raw


def test_compile_module_returns_empty_for_bad_source():
    app = load_kernel(effect=_host)
    assert app.WitWorld().compile_module("def broken(:\n", "<bad@v1>") == b""


def test_use_loads_a_hit_from_code_not_source():
    app = load_kernel(effect=_host)
    code = app.WitWorld().compile_module(SRC, "<calc@v1>")
    hit = {"source": "raise RuntimeError('source must not run on a hit')\n",
           "code": base64.b64encode(code).decode()}
    app = load_kernel(effect=_host,
                      module_source=lambda spec: hit if spec == "calc@v1" else None)
    res = app._run_cell("r = use('calc@v1').main(1)\n", "c1")
    assert res["ok"], res
    assert app._ns["r"] == 42


def test_use_compiles_source_on_a_miss():
    app = load_kernel(effect=_host,
                      module_source=lambda spec: {"source": SRC} if spec == "calc@v1" else None)
    res = app._run_cell("r = use('calc@v1').main(1)\n", "c1")
    assert res["ok"], res
    assert app._ns["r"] == 42
