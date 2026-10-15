"""programs/toMarkdown@v1 — the shared converter (ADR-031) under the real
guest kernel. Pinned for html(): page scaffolding / hidden blocks /
pixels drop, links survive, layout tables flatten, the depth bound keeps
markdownify under the kernel's 1000-frame limit, and a converter failure
degrades to plain text — never an error. Pure compute: no effects."""

import pytest
from kernelenv import load_kernel


def _load():
    def effect(name, payload):
        raise AssertionError(f"toMarkdown ran effect {name}")

    return load_kernel(effect=effect).use("toMarkdown@v1")


@pytest.fixture
def kernel_recursion_limit():
    # the wasm kernel runs CPython's default limit of 1000; pin the host to
    # it so a raised host limit can't hide a depth regression
    import sys
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(1000)
    yield
    sys.setrecursionlimit(old)


def test_html_keeps_text_links_and_headings():
    md = _load().html('<h2>Title</h2><p>See <a href="https://ex.com/a">the doc</a>.</p>')
    assert md == {"markdown": "## Title\n\nSee [the doc](https://ex.com/a)."}


def test_html_drops_scaffolding_hidden_blocks_and_pixels():
    md = _load().html(
        "<html><head><title>T</title><style>p{}</style></head><body>"
        "<script>alert(1)</script><div style='display:none'>preheader</div>"
        "<img src='p.gif' width='1' height='1' alt='px'><img src='a.png'>"
        "<p>body</p></body></html>")["markdown"]
    assert md == "body"


def test_html_unwraps_image_only_anchors():
    md = _load().html('<a href="https://x.com/p"><img src="a.png"></a><p>after</p>')["markdown"]
    assert "[" not in md and md == "after"


def test_html_flattens_layout_tables():
    md = _load().html("<table><tr><td>left</td><td>right</td></tr></table>")["markdown"]
    assert md == "left\n\nright"


def test_html_survives_unclosed_tag_chains(kernel_recursion_limit):
    md = _load().html("<div>" * 600 + "deep text")["markdown"]
    assert md == "deep text"


def test_html_caps_quote_depth():
    md = _load().html("<blockquote>" * 12 + "q" + "</blockquote>" * 12)["markdown"]
    assert md.count(">") == 5


def test_html_falls_back_to_text_on_recursion_error(monkeypatch):
    mod = _load()
    import markdownify

    def boom(*a, **k):
        raise RecursionError("maximum recursion depth exceeded")
    monkeypatch.setattr(markdownify, "markdownify", boom)
    md = mod.html("<p>still <b>here</b></p>")["markdown"]
    assert "still" in md and "here" in md and "**" not in md


def test_html_empty_none_and_bytes():
    mod = _load()
    assert mod.html("") == {"markdown": ""}
    assert mod.html(None) == {"markdown": ""}
    assert mod.html("<p>héllo</p>".encode()) == {"markdown": "héllo"}
