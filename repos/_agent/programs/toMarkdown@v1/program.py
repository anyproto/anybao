"""Convert HTML and PDF to markdown — pure compute, any program can call it.

`html(text)` → `{markdown}`: page scaffolding, hidden blocks and layout
tables dropped, deep nesting bounded, links kept, images dropped.
`pdf(data, pages=(1, 50))` → `{markdown, next, scanned, …}`: a text PDF
(bytes or a Blob) as headings, paragraphs and lists; `next` continues a
long one. Use them before reading a page or file, or writing it into an
object — a model that can't read PDFs can read this.
"""

# ADR-031: one conversion program. html() is the generic half of
# gmailSync's clean_html (ADR-012 §4) — the email passes (quoted
# chains, tracker links, footers, signatures) stay in gmailSync, which
# runs them around this call. pdf() is pypdf (vendored, ADR-031 §7)
# plus the passes of ADR-031 §3.

import io
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


# --- pdf (ADR-031 §3-§5) ----------------------------------------------------

_PDF_MAX_BYTES = 16 * 1024 * 1024   # §4: bigger → the model-native read
_PDF_PAGES = 50                     # §4: default pages per call
_SCANNED_CHARS = 20                 # §5: chars/page under which there's no text layer
_HEADING_RATIO = 1.15
_HEADING_MAX = 120
_FIGURE_RUN = 3                     # §3 figure guard: this many candidates in a row…
_FIGURE_MEDIAN = 12                 # …with a median length under this stay body text
_FIGURE_LONG_RUN = 5                # …and a run this long stays body text regardless
_BOLD = re.compile(r"bold|black|heavy|semibold|demi", re.I)
_BULLET = re.compile(r"^\s*([•◦▪‣·∙\-–*]|\(?\d{1,3}[.)]|\(?[a-z][.)])\s+")
_DIGITS = re.compile(r"\d+")
_SPACES = re.compile(r"\s+")


class TooLarge(ValueError):
    """The PDF is over the 16 MiB input cap."""


class PdfEncrypted(ValueError):
    """The PDF is encrypted — no handler is decrypted here."""


def _page_lines(page):
    """A page's text as lines {text, size, bold, rot, y}, in content order,
    from pypdf's text visitor (fragments on one baseline join; a line's
    size is the one most of its characters are set in)."""
    lines, cur = [], {"parts": [], "sizes": {}, "bold": True, "rot": False, "y": None}

    def flush():
        t = _SPACES.sub(" ", "".join(cur["parts"])).strip()
        if t:
            sizes = cur["sizes"]
            lines.append({"text": t, "size": max(sizes, key=sizes.get) if sizes else 0.0,
                          "bold": cur["bold"], "rot": cur["rot"], "y": cur["y"] or 0.0})
        cur.update(parts=[], sizes={}, bold=True, rot=False, y=None)

    def visit(text, cm, tm, font, size):
        if not text:
            return
        a, b, c, d, e, f = tm
        A, B, C, D, E, F = cm
        y = e * B + f * D + F
        scale = abs(d * D) or abs(a * A) or 1.0
        eff = (size or 0) * scale
        if cur["y"] is not None and abs(y - cur["y"]) > max(1.0, eff * 0.3):
            flush()
        name = str((font or {}).get("/BaseFont") or "")
        for i, piece in enumerate(text.split("\n")):
            if i:
                flush()
            if not piece:
                continue
            cur["parts"].append(piece)
            if piece.strip():
                sz = round(eff, 1)   # the line's size = the one carrying most chars
                cur["sizes"][sz] = cur["sizes"].get(sz, 0) + len(piece.strip())
                cur["bold"] = cur["bold"] and bool(_BOLD.search(name))
                cur["rot"] = cur["rot"] or abs(b) > 0.01 or abs(c) > 0.01
            if cur["y"] is None:
                cur["y"] = y

    page.extract_text(visitor_text=visit)
    flush()
    return lines


def _running_edges(per_page):
    """§3 pass 1: digit-normalized first/last-two lines recurring on at
    least half the pages (≥ 3 pages); one line per edge on a short page,
    whose edges would otherwise be its body."""
    n = len(per_page)
    if n < 3:
        return set()
    seen = {}
    for ls in per_page:
        k = 2 if len(ls) > 6 else 1
        for key in {_DIGITS.sub("#", ln["text"]) for ln in ls[:k] + ls[-k:]}:
            seen[key] = seen.get(key, 0) + 1
    return {k for k, v in seen.items() if v >= max(3, (n + 1) // 2)}


def _heading_levels(lines, body):
    """§3 pass 2: heading candidates by size or bold; figure runs demoted;
    levels ranked over the sizes that survive."""
    for ln in lines:
        t = ln["text"]
        ok = (len(t) < _HEADING_MAX and not t.endswith((".", ",", ";", ":"))
              and not ln["rot"] and sum(ch.isalpha() for ch in t) >= 3)
        big = ok and body and ln["size"] >= body * _HEADING_RATIO
        bold = (ok and ln["bold"] and abs(ln["size"] - body) <= 0.5
                and not _BULLET.match(t))
        ln["lvl"] = "size" if big else ("bold" if bold else None)
    i = 0
    while i < len(lines):
        if not lines[i]["lvl"]:
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j]["lvl"]:
            j += 1
        run = sorted(len(ln["text"]) for ln in lines[i:j])
        if j - i >= _FIGURE_LONG_RUN or (
                j - i >= _FIGURE_RUN and run[len(run) // 2] < _FIGURE_MEDIAN):
            for ln in lines[i:j]:
                ln["lvl"] = None
        i = j
    sizes = sorted({ln["size"] for ln in lines if ln["lvl"] == "size"}, reverse=True)
    level = {sz: min(k + 1, 4) for k, sz in enumerate(sizes)}
    for ln in lines:
        if ln["lvl"] == "size":
            ln["lvl"] = level[ln["size"]]
        elif ln["lvl"] == "bold":
            ln["lvl"] = min(len(sizes) + 1, 4)


def _blocks(per_page, drop):
    """§3 passes 3-4: headings, paragraphs (joined, dehyphenated), lists."""
    blocks, para = [], []

    def end_para():
        if para:
            blocks.append(" ".join(para))
            para.clear()

    for ls in per_page:
        last = None
        for ln in ls:
            t = ln["text"]
            if _DIGITS.sub("#", t) in drop:
                continue
            if ln["lvl"]:
                end_para()
                blocks.append("#" * ln["lvl"] + " " + t)
                last = None
                continue
            if _BULLET.match(t):
                end_para()
                para.append("- " + _BULLET.sub("", t, count=1))
                last = ln
                continue
            gap = (last["y"] - ln["y"]) if last else 0
            if last is not None and (gap > ln["size"] * 1.6 or gap < -ln["size"]):
                end_para()
            if para and para[-1].endswith("-") and t[:1].islower():
                para[-1] = para[-1][:-1] + t
            else:
                # body text opening with "#" would render as a heading
                para.append("\\" + t if t.startswith("#") and not para else t)
            last = ln
        if para and para[-1].endswith((".", ":", "?", "!")):
            end_para()   # a page break ends a paragraph only after a full stop
    end_para()
    out = []
    for b in blocks:   # consecutive items are one list, not loose paragraphs
        if out and b.startswith("- ") and out[-1].split("\n")[-1].startswith("- "):
            out[-1] += "\n" + b
        else:
            out.append(b)
    return out


def _fields(reader):
    """§3 pass 5: AcroForm fields that carry a value → [(name, value)]."""
    out = []
    for name, f in (reader.get_fields() or {}).items():
        v = f.get("/V") if hasattr(f, "get") else None
        v = str(v).strip().lstrip("/") if v is not None else ""
        if v and v not in ("Off",):
            out.append((name, v))
    return out


@span("toMarkdown.pdf", kind="getter")  # noqa: F821 - guest global
def pdf(data, pages=None):
    """PDF bytes or Blob → {markdown, first, last, total, next, scanned, fields}.

    Converts pages (first, last), 1-based, default (1, 50); `next` is
    the page to continue from (pages=(next, next + 49)), None when done.
    Headings by font size and bold, running headers/footers dropped,
    paragraphs rejoined, lists kept, filled form fields appended;
    tables come out as their text, images are skipped. scanned: True =
    no text layer (a scan) — hand the file to a model that reads PDFs
    instead. Raises PdfEncrypted for any encrypted file and TooLarge
    over 16 MiB; tell the user rather than retrying.
    """
    import pypdf

    size = len(data)
    if size > _PDF_MAX_BYTES:
        raise TooLarge(f"PDF is {size} bytes, over the {_PDF_MAX_BYTES}-byte cap — "
                       f"send the file to a model that reads PDFs natively instead")
    reader = pypdf.PdfReader(io.BytesIO(bytes(data)))
    if reader.is_encrypted:
        raise PdfEncrypted("PDF is encrypted (no decryption here) — send the file to a "
                           "model that reads PDFs natively, or ask for an unlocked copy")
    total = len(reader.pages)
    first, last = pages if pages else (1, _PDF_PAGES)
    first, last = max(1, int(first)), min(total, int(last))
    if first > total or last < first:
        return {"markdown": "", "first": first, "last": first - 1, "total": total,
                "next": None, "scanned": False, "fields": 0}

    per_page = [_page_lines(reader.pages[i]) for i in range(first - 1, last)]
    weight = {}
    for ls in per_page:
        for ln in ls:
            weight[ln["size"]] = weight.get(ln["size"], 0) + len(ln["text"])
    body = max(weight, key=weight.get) if weight else 0.0
    _heading_levels([ln for ls in per_page for ln in ls], body)
    blocks = _blocks(per_page, _running_edges(per_page))

    fields = _fields(reader) if first == 1 else []
    if fields:
        blocks.append("## Fields")
        blocks.append("\n".join(f"- {k}: {v}" for k, v in fields))
    chars = sum(len(ln["text"]) for ls in per_page for ln in ls)
    return {"markdown": "\n\n".join(blocks), "first": first, "last": last, "total": total,
            "next": last + 1 if last < total else None,
            "scanned": chars < _SCANNED_CHARS * len(per_page), "fields": len(fields)}
