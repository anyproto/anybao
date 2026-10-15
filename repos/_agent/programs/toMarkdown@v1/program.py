"""Convert HTML (and PDF) to markdown — pure compute, any program can call it.

`html(text)` → `{markdown}`: page scaffolding, hidden blocks and layout
tables dropped, deep nesting bounded, then markdown (links kept, images
dropped). Use it on fetched pages or HTML bodies before reading them or
writing them into an object.
"""

# ADR-031: one conversion program. html() is the generic half of
# gmailSync's clean_html (ADR-012 §4) — the email passes (quoted
# chains, tracker links, footers, signatures) stay in gmailSync, which
# runs them around this call.

import re

__any_tool__ = True  # agent-callable (ADR-010 §4)

_DROP_TAGS = ["script", "style", "head", "title", "meta", "link"]
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|max-height\s*:\s*0", re.I)
# depth bound (ADR-012 §4): markdownify recurses 2 Python frames per
# element level under the kernel's 1000-frame limit (RecursionError at
# ~493 levels), and bs4's html.parser has no implied end tags — Word/
# Outlook exports nest unclosed <p>/<div>/<font> hundreds deep in a few
# KB. Browsers cap the tree the same way (Gecko 200, Blink 512):
# elements deeper than this become siblings of the element at the cap.
_MAX_DEPTH = 200
# markdownify prefixes every line once per enclosing blockquote, so N
# nested quotes cost N²/2 "> " marks (403 KB of markdown for 400
# replies, measured). Deeper quotes flatten to this level.
_MAX_QUOTE_DEPTH = 5
_BLOCK_WRAPPERS = {"div", "p", "center", "section", "article"}
_INLINE_WRAPPERS = {"span", "font"}
_BLOCKS = _BLOCK_WRAPPERS | {"blockquote", "ul", "ol", "li", "pre", "hr",
                             "h1", "h2", "h3", "h4", "h5", "h6"}


def _sole_tag_child(tag, Tag, Comment):
    """The one element child of `tag` when every other child is
    whitespace or a comment; None otherwise."""
    sole = None
    for c in tag.contents:
        if isinstance(c, Tag):
            if sole is not None:
                return None
            sole = c
        elif not isinstance(c, Comment) and str(c).strip():
            return None
    return sole


def _collapse_wrapper_chains(soup, Tag, Comment):
    """Unwrap a wrapper whose only child is a like wrapper — block in
    block (div>div>p), inline in inline (font>font>span). Lossless for
    markdown (markdownify passes span/font through; a block child keeps
    the block break) and iterative: find_all snapshots, unwrap is O(k)."""
    for tag in soup.find_all(list(_BLOCK_WRAPPERS | _INLINE_WRAPPERS)):
        child = _sole_tag_child(tag, Tag, Comment)
        if child is None:
            continue
        like = _BLOCKS if tag.name in _BLOCK_WRAPPERS else _INLINE_WRAPPERS
        if child.name in like:
            tag.unwrap()


def _cap_quote_depth(soup, Tag, cap=_MAX_QUOTE_DEPTH):
    """A blockquote nested deeper than `cap` quote levels unwraps — its
    text stays, quoted at the cap. Collected on one explicit-stack walk,
    unwrapped after (unwrap is local; the marks don't interact)."""
    deep, stack = [], [(soup, 0)]
    while stack:
        node, q = stack.pop()
        for c in node.contents:
            if not isinstance(c, Tag):
                continue
            cq = q + 1 if c.name == "blockquote" else q
            if cq > cap and c.name == "blockquote":
                deep.append(c)
            stack.append((c, cq))
    for bq in deep:
        bq.unwrap()


def _cap_depth(soup, Tag, cap=_MAX_DEPTH):
    """Browser-style depth cap. An element `cap` levels down keeps its
    leading text; from its first element child on, its content is
    lifted out as following siblings (document order preserved), so the
    lifted elements sit at the cap and are bounded in turn. Explicit
    stack, every element visited once, O(n)."""
    stack = [(soup, 0)]
    while stack:
        node, d = stack.pop()
        if d < cap:
            stack.extend((c, d + 1) for c in node.contents if isinstance(c, Tag))
            continue
        first = next((i for i, c in enumerate(node.contents) if isinstance(c, Tag)), None)
        if first is None:
            continue
        tail = node.contents[first:]
        for c in reversed(tail):
            node.insert_after(c)
        stack.extend((c, d) for c in tail if isinstance(c, Tag))


@span("toMarkdown.html", kind="getter")  # noqa: F821 - guest global
def html(text):
    """HTML string → {markdown}; links kept, images and scaffolding dropped.

    Drops script/style/head, hidden blocks (display:none), 1×1 and
    alt-less images; layout tables become blocks; nesting deeper than
    200 levels (and quotes deeper than 5) is flattened before
    conversion. A document the converter still can't handle comes back
    as plain text rather than an error. Empty or None → "".
    """
    from bs4 import BeautifulSoup, Comment, Tag
    from markdownify import markdownify as _md

    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    soup = BeautifulSoup(text or "", "html.parser")

    for tag in soup(_DROP_TAGS):
        tag.decompose()
    for tag in soup.find_all(style=_HIDDEN_STYLE):
        tag.decompose()
    for img in soup.find_all("img"):
        w, h = img.get("width") or "", img.get("height") or ""
        if str(w) in ("0", "1") or str(h) in ("0", "1") or not img.get("alt"):
            img.decompose()
    # an anchor whose whole content was a (dropped) image renders as an
    # empty [](…) / [[ artifact — unwrap it before conversion
    for a in soup.find_all("a"):
        if not a.get_text(strip=True):
            a.unwrap()
    # tables are layout far more often than data — cells become blocks
    for cell in soup.find_all(["td", "th"]):
        cell.name = "div"
    for scaffold in soup.find_all(["table", "tbody", "thead", "tfoot", "tr"]):
        scaffold.unwrap()
    # depth bound (after the table pass, so its div chains collapse too)
    _collapse_wrapper_chains(soup, Tag, Comment)
    _cap_quote_depth(soup, Tag)
    _cap_depth(soup, Tag)

    try:
        md = _md(str(soup), heading_style="ATX", strip=["img"])
    except RecursionError:
        md = soup.get_text("\n")   # bs4 walks iteratively; never lose the text
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"^[ \t|]+$", "", md, flags=re.M)
    return {"markdown": md.strip()}
