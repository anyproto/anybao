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
