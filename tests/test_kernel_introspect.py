"""Native introspection (runtime/guest/app.py, ADR-010 §2): `inspect`
is tier-1 importable, describe() renders docstring + typed signatures
with span kind tags, help() prints through the cell's traced print.
Runs the REAL kernel via kernelenv — the only fake is the effect
boundary."""

import inspect as host_inspect
import types

from kernelenv import load_kernel

SRC = '''\
"""Fake connector — one-liner summary.

Body line of the module docstring."""


def _hidden(x):
    """Never listed."""
    return x


@span("fake.list_things", kind="getter")
def list_things(owner: str, state: str | None = None) -> dict:
    """List things, newest first.

    Returns {ok, items}."""
    return {"ok": True, "items": [owner, state]}


@span("fake.delete_thing", kind="mutator")
def delete_thing(owner, number):
    """Delete one thing."""
    return {"ok": True}


def plain(a, b=2):
    return a + b


def main(args):
    return {}
'''


def guest_module(app, src=SRC, name="fake"):
    """A module exec'd exactly the way use() does it (ADR-004), minus
    the resolve effect."""
    mod = types.ModuleType(name)
    mod.__dict__.update(app._fresh_ns())
    exec(compile(src, f"<{name}@v1>", "exec"), mod.__dict__)
    return mod


def test_inspect_is_importable_and_sees_signatures():
    app = load_kernel()
    out = app._run_cell(
        "import inspect\n"
        "def f(a: int, b=1) -> int:\n"
        "    return a + b\n"
        "print(str(inspect.signature(f)))\n",
        "c1",
    )
    assert out["ok"], out
    assert out["prints"][0]["repr"] == "(a: int, b=1) -> int"


def test_describe_module_lists_public_methods():
    app = load_kernel()
    mod = guest_module(app)
    out = app.describe(mod)
    # module docstring leads, in full
    assert out.startswith("Fake connector — one-liner summary.")
    assert "Body line of the module docstring." in out
    # typed signature + kind tag + first doc line only
    assert ("list_things(owner: str, state: str | None = None) -> dict "
            "[getter] — List things, newest first.") in out
    assert "delete_thing(owner, number) [mutator] — Delete one thing." in out
    # undecorated public function: signature, no tag
    assert "plain(a, b=2)" in out
    # hidden: underscore names, main, and the kernel-injected globals
    assert "_hidden" not in out
    assert "\n  main(" not in out
    assert "Returns {ok, items}" not in out  # method bodies stay out
    for injected in ("use(", "subcell(", "uuid4("):
        assert injected not in out


def test_describe_function_gives_full_docstring():
    app = load_kernel()
    mod = guest_module(app)
    out = app.describe(mod.list_things)
    assert out.splitlines()[0] == (
        "list_things(owner: str, state: str | None = None) -> dict [getter]")
    assert "Returns {ok, items}." in out


def test_span_stamps_kind_and_preserves_signature():
    app = load_kernel()
    mod = guest_module(app)
    assert mod.list_things.__span_kind__ == "getter"
    assert mod.delete_thing.__span_kind__ == "mutator"
    sig = str(host_inspect.signature(mod.list_things))
    assert sig == "(owner: str, state: str | None = None) -> dict"


def test_help_prints_through_the_cell_printer():
    app = load_kernel()
    out = app._run_cell(
        "def f(a: int) -> int:\n"
        '    """Add one."""\n'
        "    return a + 1\n"
        "help(f)\n",
        "c1",
    )
    assert out["ok"], out
    assert out["prints"][0]["repr"] == "f(a: int) -> int\nAdd one."


def test_tool_docs_composes_from_real_programs():
    """The ## Tools block (toolcaller `_tool_docs`, ADR-010 §3) built
    from REAL program sources through the real kernel: docstring +
    signature lines, kind tags from @span, binder pattern intact."""
    app = load_kernel(effect=lambda n, p: {})
    tc = app.use("toolcaller@v1")

    class C:
        def list_types(self, space):
            return [{"id": "progT", "xKey": "program"}]

        def query_objects(self, space, filter=None, **kw):
            return [
                {"id": "p1", "createdAt": 1, "program":
                    {"name": "webSearch", "version": "v1", "any_tool": True}},
                {"id": "p2", "createdAt": 2, "program":
                    {"name": "memory", "version": "v1", "any_tool": True}},
            ]

    docs = tc._tool_docs(C(), "space")
    assert docs.startswith("## Tools")
    assert "### webSearch" in docs
    assert "Grounded web search" in docs                       # module docstring
    assert ("search(*queries) [getter] — Run one or more web searches; "
            "one formatted string per query.") in docs
    assert "### memory" in docs
    assert "memory(client, llm_chat=None) [setup]" in docs
    # handle methods stay behind help(m) — no method line for them
    assert "  save_with_dedup(" not in docs
    assert "_provider" not in docs         # underscore names hidden
    assert "\n  main(" not in docs         # entry point excluded


def test_import_error_teaches_inspect_and_help():
    app = load_kernel()
    out = app._run_cell(
        "try:\n"
        "    import socket\n"
        "except ImportError as e:\n"
        "    print(str(e))\n",
        "c1",
    )
    assert out["ok"], out
    msg = out["prints"][0]["repr"]
    assert "inspect" in msg
    assert "help()" in msg


def test_inferschema_is_a_cell_global():
    # the data-shape counterpart of describe() (ADR-010 §2): filters
    # ground in an observed shape, not a guessed one (dev task A19)
    app = load_kernel()
    out = app._run_cell(
        'row = {"id": "o1", "any": {"name": "Ship", "types": ["task"]},'
        ' "task": {"status": "open"}}\n'
        "print(inferSchema(row))\n",
        "c1",
    )
    assert out["ok"], out
    shape = out["prints"][0]["repr"]
    assert shape.startswith("{id:str")          # id is top-level in the shape
    assert "any:{name:str" in shape
    assert "task:{status:str}" in shape


def test_span_name_derives_from_module_and_def():
    """ADR-003 §4b (2026-08-14): span(name=None) records the anchor
    `<module>.<def>` read off the decorated function; an explicit name
    stays a deliberate display override; no module context (cell code)
    falls back to the bare def name."""
    app = load_kernel()
    recorded = []
    app._effect = lambda name, payload=None: recorded.append((name, payload)) or {}

    mod = types.ModuleType("mytool")
    mod.span = app.span
    exec(compile(
        '@span(kind="getter")\n'
        "def go(x):\n"
        "    return x\n"
        "\n"
        "\n"
        '@span("mytool.public", kind="mutator")\n'
        "def _hidden(x):\n"
        "    return x\n",
        "<m>", "exec"), mod.__dict__)

    mod.go(1)
    assert recorded[0] == ("span.begin", {"name": "mytool.go",
                                          "input": {"x": 1}, "kind": "getter"})
    assert recorded[1][0] == "span.end"
    assert mod.go.__span_kind__ == "getter"

    recorded.clear()
    mod._hidden(2)
    assert recorded[0][1]["name"] == "mytool.public"   # override wins

    recorded.clear()
    g = dict(app._fresh_ns())          # cell-shaped namespace: no __name__
    exec(compile('@span(kind="getter")\ndef solo():\n    return 1\n',
                 "<c>", "exec"), g)
    g["solo"]()
    assert recorded[0][1]["name"] == "solo"


def test_module_print_reaches_the_calling_cells_digest():
    """ADR-003 §3 (2026-09-09): a program module's print() is the
    active cell's printer; outside a cell it is a no-op."""
    from kernelenv import load_kernel
    src = 'def hi():\n    print("from module")\n    return 1\n' \
          'print("at import")\n'
    k = load_kernel(module_source=lambda spec: src if spec == "p@v1" else None)
    r = k._run_cell('m = use("p@v1")\nm.hi()', "c1")
    assert r["ok"], r["error"]
    assert [p["repr"] for p in r["prints"]] == ["at import", "from module"]
    r = k._run_cell("m.hi()", "c2")
    assert [p["repr"] for p in r["prints"]] == ["from module"]
    k._ns["print"]   # restored after the cell: a module print now goes nowhere
    k._ns["m"].hi()


def test_help_on_http_verbs_and_env_teaches_credential_injection():
    """ADR-021 §8.7: `help(http.get)` names `credential=` and the
    `local.key.*` namespace, `help(env)` says it is never a secret —
    the anyscribe run (BOB-87) showed bao reaching for `env()` because
    `help(http.get)` printed only the signature."""
    app = load_kernel()
    for verb in ("get", "post", "put", "patch", "delete", "head"):
        out = app.describe(getattr(app.http, verb))
        assert out.splitlines()[0].startswith(f"{verb}(url, **kw)"), out
        assert "credential" in out and "local.key." in out, (verb, out)
        assert "about" in out and "hosts" in out, (verb, out)
    env_doc = app.describe(app.env)
    assert "secrets never enter guest code" in env_doc, env_doc
    assert "credential=" in env_doc, env_doc
