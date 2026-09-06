"""Kernel names are reserved (runtime/guest/app.py, ADR-003 §3): a cell
that would rebind or delete a runtime-bound global at module scope is
refused at parse time with `ReservedNameError`, before anything runs;
own-scope bindings (parameters, locals, comprehension targets) pass.
Runs the REAL kernel via kernelenv."""

import pytest
from kernelenv import load_kernel


def _err(app, code, cid="c1"):
    out = app._run_cell(code, cid)
    assert not out["ok"], code
    return out["error"]


@pytest.mark.parametrize("code, how", [
    ('effects = use("agent:programs@v1")', "assign to"),
    ("del effects", "delete"),
    ("effects += 1", "assign to"),
    ("for values in [1]: pass", "assign to"),
    ("(use := 1)", "assign to"),
    ("a, *span = [1, 2]", "assign to"),
    ("with open('x') as http: pass", "assign to"),
    ("import json as blob", "import as"),
    ("from json import loads as now", "import as"),
    ("def effects(): pass", "define"),
    ("class values: pass", "define"),
    ("try:\n    pass\nexcept Exception as print:\n    pass", "bind (except … as)"),
    ("def f():\n    global effects\n    effects = 1\nf()", "declare global"),
    ("match 1:\n    case help:\n        pass", "capture (match)"),
    ("sh = 1", "assign to"),     # reserved even without the feature
    ("del __builtins__", "delete"),
])
def test_module_scope_rebind_or_del_of_a_kernel_name_is_refused(code, how):
    app = load_kernel()
    e = _err(app, code)
    assert e["type"] == "ReservedNameError"
    assert f"cannot {how} `" in e["message"] and "kernel name" in e["message"]
    assert e["message"].startswith("line ")
    assert e["traceback"] == ""   # the message is the whole story
    # nothing ran: the namespace still has the real binding
    assert app._run_cell("type(effects).__name__", "c2")["last"]["repr"] == "'_Effects'"


def test_incident_shape_fails_the_cell_and_effects_survives():
    app = load_kernel()
    e = _err(app, 'effects = use("agent:programs@v1")  # placeholder')
    assert e["type"] == "ReservedNameError"
    assert "line 1: cannot assign to `effects`" in e["message"]
    out = app._run_cell("effects", "c2")
    assert out["ok"] and "._Effects object" in out["last"]["repr"]


def test_own_scope_bindings_pass():
    app = load_kernel()
    out = app._run_cell(
        "def f(values, use=2):\n"
        "    effects = values + use\n"
        "    return effects\n"
        "g = lambda span: span\n"
        "class K:\n"
        "    http = 1\n"
        "xs = [blob for blob in range(3)]\n"
        "print(f(1), g(2), K.http, xs, type(effects).__name__)", "c1")
    assert out["ok"], out["error"]
    assert out["prints"][0]["repr"] == "3 2 1 [0, 1, 2] _Effects"


def test_ordinary_names_and_the_toolcaller_ctx_cell_pass():
    app = load_kernel()
    out = app._run_cell(
        "currentUserSpace = {'spaceId': 's'}\n"
        "baoSpaceConfig = {'spaceId': 's', 'chatId': 'c'}\n"
        "c = 1\nrec = 2\nresult = c + rec\nresult", "_ctx")
    assert out["ok"], out["error"]
    assert out["last"]["repr"] == "3"


def test_kernel_names_are_derived_from_the_namespace():
    app = load_kernel()
    ns = set(app._fresh_ns())
    assert ns <= app._KERNEL_NAMES
    assert {"print", "help", "sh", "fs", "ShellError"} <= app._KERNEL_NAMES
    assert "c" not in app._KERNEL_NAMES and "rec" not in app._KERNEL_NAMES
