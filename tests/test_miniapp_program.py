"""programs/miniapp@v1 — the runtime-script guard (ADR-008 §6),
tested host-side by exec-ing the guest source with the `use`/`span`
globals stubbed. Only the pure normalization is covered here; the
object/dataset surface goes through the live server."""

from pathlib import Path

SRC = (Path(__file__).resolve().parents[1]
       / "programs" / "miniapp@v1" / "program.py").read_text()

CANONICAL = ('<script src="./react.js"></script>\n'
             '<script src="./react-dom.js"></script>\n'
             '<script src="./useAnytypeState.js"></script>\n')


def guard(html):
    g = {"use": lambda spec: None,
         "span": lambda *a, **k: (lambda fn: fn)}
    exec(compile(SRC, "miniapp@v1/program.py", "exec"), g)
    return g["_guard"](html)


def test_missing_tags_are_prepended_in_load_order():
    src = '<div id="app"></div><script>render();</script>'
    out, warnings = guard(src)
    assert out == CANONICAL + src
    assert warnings == ["auto-injected missing runtime script(s): "
                        "react.js, react-dom.js, useAnytypeState.js"]


def test_wrong_order_is_healed():
    # the standup-picker failure: react-dom loads before react
    src = ('<script src="./react-dom.js"></script>\n'
           '<div id="app"></div>\n'
           '<script src="./react.js"></script>\n'
           '<script src="./useAnytypeState.js"></script>\n'
           '<script>render();</script>')
    out, warnings = guard(src)
    assert out == CANONICAL + '<div id="app"></div>\n<script>render();</script>'
    assert warnings == ["moved runtime script tag(s) to canonical load "
                        "order: react.js, react-dom.js, useAnytypeState.js"]


def test_canonical_source_is_untouched():
    src = CANONICAL + '<div id="app"></div>'
    out, warnings = guard(src)
    assert out == src and warnings is None


def test_guard_is_idempotent():
    once, _ = guard('<script src="./react-dom.js"></script><div></div>')
    twice, warnings = guard(once)
    assert twice == once and warnings is None


def test_only_the_three_owned_tags_are_touched():
    src = ('<script src="https://cdn.example/lib.js"></script>\n'
           '<script src="./react.js"></script>\n'
           '<script>inline();</script>')
    out, warnings = guard(src)
    assert out == (CANONICAL
                   + '<script src="https://cdn.example/lib.js"></script>\n'
                   + '<script>inline();</script>')
    assert warnings == [
        "auto-injected missing runtime script(s): react-dom.js, "
        "useAnytypeState.js",
        "moved runtime script tag(s) to canonical load order: react.js"]


def test_attributed_tags_are_stripped_too():
    src = ('<script defer src="./react.js" crossorigin></script>\n'
           '<script src="./react-dom.js"></script>\n'
           '<script src="./useAnytypeState.js"></script>\n'
           '<div></div>')
    out, warnings = guard(src)
    assert out == CANONICAL + "<div></div>"
    assert warnings is not None
