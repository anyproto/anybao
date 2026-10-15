"""programs/toMarkdown@v1 — the shared converter (ADR-031) under the real
guest kernel. Pinned for html(): page scaffolding / hidden blocks /
pixels drop, links survive, layout tables flatten, the depth bound keeps
markdownify under the kernel's 1000-frame limit, and a converter failure
degrades to plain text — never an error. For pdf(), on generated PDFs
(tests/pdfgen.py): the §3 passes, the page bound + `next`, scanned /
encrypted / oversize refusals, Blob input. Pure compute: no effects
beyond blob.read."""

import pytest
from kernelenv import load_kernel
from pdfgen import body_lines, pdf


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


# --- pdf() --------------------------------------------------------------------

def _pdf_load(blobs=None):
    """The module plus its kernel; `blobs` {sha: bytes} serves blob.read."""
    import base64

    def effect(name, payload):
        if name == "blob.read" and blobs is not None:
            data = blobs[payload["hash"]]
            off, n = payload.get("offset", 0), payload.get("length", len(data))
            chunk = data[off:off + n]
            return {"data": base64.b64encode(chunk).decode(), "bytes": len(chunk)}
        raise AssertionError(f"toMarkdown ran effect {name}")

    app = load_kernel(effect=effect)
    return app, app.use("toMarkdown@v1")


def test_pdf_headings_paragraphs_dehyphenation_and_lists():
    doc = pdf([
        [("Quarterly Report", 20, False, 720), ("Introduction", 11, True, 690)]
        + body_lines(["The quick brown fox jumps over the lazy", "dog and keeps run-",
                      "ning far away."], top=670)
        + [("• first item", 11, False, 610), ("2. second item", 11, False, 596)],
    ])
    out = _pdf_load()[1].pdf(doc)
    assert out == {
        "markdown": "# Quarterly Report\n\n## Introduction\n\n"
                    "The quick brown fox jumps over the lazy dog and keeps running far away."
                    "\n\n- first item\n- second item",
        "first": 1, "last": 1, "total": 1, "next": None, "scanned": False, "fields": 0}


def test_pdf_drops_running_headers_and_footers():
    pages = [[("ACME confidential", 9, False, 770)]
             + body_lines([f"Body text on page {i} goes here."])
             + [(f"Page {i}", 9, False, 30)] for i in range(1, 4)]
    md = _pdf_load()[1].pdf(pdf(pages))["markdown"]
    assert md == ("Body text on page 1 goes here.\n\nBody text on page 2 goes here."
                  "\n\nBody text on page 3 goes here.")


def test_pdf_paragraph_continues_across_a_page_break_without_a_full_stop():
    md = _pdf_load()[1].pdf(pdf([body_lines(["A sentence that runs on"]),
                                 body_lines(["into the next page."])]))["markdown"]
    assert md == "A sentence that runs on into the next page."


def test_pdf_pages_bound_and_next():
    mod = _pdf_load()[1]
    doc = pdf([body_lines([f"Page {i} has its own text."]) for i in range(1, 6)])
    out = mod.pdf(doc, pages=(2, 3))
    assert (out["first"], out["last"], out["total"], out["next"]) == (2, 3, 5, 4)
    assert out["markdown"] == "Page 2 has its own text.\n\nPage 3 has its own text."
    assert mod.pdf(doc, pages=(4, 99))["next"] is None
    assert mod.pdf(doc)["last"] == 5
    past = mod.pdf(doc, pages=(9, 12))
    assert past["markdown"] == "" and past["next"] is None


def test_pdf_default_bound_is_fifty_pages():
    out = _pdf_load()[1].pdf(pdf([body_lines([f"p{i} text"]) for i in range(60)]))
    assert (out["last"], out["next"]) == (50, 51)


def test_pdf_without_a_text_layer_is_flagged_scanned():
    out = _pdf_load()[1].pdf(pdf([[], []]))
    assert out["scanned"] is True and out["markdown"] == ""


def test_pdf_figure_labels_stay_body_text():
    md = _pdf_load()[1].pdf(pdf([
        body_lines(["Some body text long enough to set the body size of the page."])
        + [("Enc", 16, False, 500), ("Dec", 16, False, 480), ("Tok", 16, False, 460)]
        + body_lines(["More body text continues after the figure labels."], top=400)]))
    assert "#" not in md["markdown"]


def test_pdf_body_text_opening_with_a_hash_is_escaped():
    md = _pdf_load()[1].pdf(pdf([body_lines(["#L #H #A are the table columns here"])]))
    assert md["markdown"] == "\\#L #H #A are the table columns here"


def test_pdf_appends_filled_form_fields():
    out = _pdf_load()[1].pdf(pdf([body_lines(["A form with a couple of fields on it."])],
                                 fields={"name": "Ann Lee", "empty": ""}))
    assert out["markdown"].endswith("## Fields\n\n- name: Ann Lee")
    assert out["fields"] == 1


def test_pdf_refuses_encrypted_files():
    import io

    import pypdf  # the vendored copy — kernelenv puts runtime/guest on the path
    w = pypdf.PdfWriter(clone_from=pypdf.PdfReader(io.BytesIO(pdf([body_lines(["secret"])]))))
    w.encrypt(user_password="", owner_password="owner", algorithm="RC4-128")
    buf = io.BytesIO()
    w.write(buf)
    mod = _pdf_load()[1]
    with pytest.raises(mod.PdfEncrypted, match="encrypted"):
        mod.pdf(buf.getvalue())


def test_pdf_refuses_over_sixteen_mib_before_parsing():
    mod = _pdf_load()[1]
    with pytest.raises(mod.TooLarge, match="cap"):
        mod.pdf(b"x" * (16 * 1024 * 1024 + 1))


def test_pdf_reads_a_blob():
    doc = pdf([body_lines(["Read through the blob handle."])])
    app, mod = _pdf_load(blobs={"sha256:abc": doc})
    blob = app.Blob("sha256:abc", len(doc), "application/pdf")
    assert mod.pdf(blob)["markdown"] == "Read through the blob handle."
