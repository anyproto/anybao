"""Vendored html stack in the guest kernel (ADR-012 §6, ADR-002 §4/1b):
bs4 + markdownify importable and working under the REAL kernel import
gate; internals (six, typing_extensions) stay outside the allowlist."""

import pytest

from kernelenv import load_kernel

HTML = (
    '<div><h1>Invoice</h1><table><tr><td>Total</td><td>&euro;42</td></tr>'
    "</table><p>Thanks &amp; <b>regards</b></p></div>"
)


def guest_exec(src):
    """Exec under the kernel's curated builtins — the same namespace a
    cell gets, so the import gate applies exactly as in wasm."""
    app = load_kernel(lambda name, payload: {})
    ns = {"__builtins__": app._SAFE_BUILTINS}
    exec(src, ns)
    return ns


def test_bs4_parses_under_guest_import_gate():
    ns = guest_exec(
        "import bs4\n"
        "from bs4 import BeautifulSoup\n"
        f"text = BeautifulSoup({HTML!r}, 'html.parser').get_text(' ', strip=True)\n"
    )
    assert "Invoice" in ns["text"] and "€42" in ns["text"]


def test_markdownify_converts_under_guest_import_gate():
    ns = guest_exec(
        "import markdownify\n"
        f"md = markdownify.markdownify({HTML!r})\n"
    )
    assert "Invoice" in ns["md"] and "**regards**" in ns["md"]


def test_tier1_html_and_email_importable():
    ns = guest_exec(
        "import html\n"
        "import email.utils\n"
        "unescaped = html.unescape('&amp;&euro;')\n"
        "addr = email.utils.parseaddr('Ruud <ruud@ruuda.nl>')\n"
    )
    assert ns["unescaped"] == "&€"
    assert ns["addr"] == ("Ruud", "ruud@ruuda.nl")


def test_vendored_internals_stay_unimportable():
    for mod in ("six", "typing_extensions"):
        with pytest.raises(ImportError, match="effect boundary"):
            guest_exec(f"import {mod}\n")
