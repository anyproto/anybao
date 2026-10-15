"""Tiny PDF writer for tests: pages of positioned text lines in the
standard-14 Helvetica faces (WinAnsi, no embedding), plus optional
AcroForm text fields. Enough structure for pypdf's text visitor — font size, face, and
baseline — so the toMarkdown passes can be pinned on exact inputs."""


def _esc(s):
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _text_string(s):
    """A PDF text string: literal when it is ASCII, else UTF-16BE hex with
    the byte-order mark — the encoding pypdf decodes with utf_16_be."""
    if s.isascii():
        return f"({_esc(s)})"
    return "<FEFF" + s.encode("utf-16-be").hex().upper() + ">"


def pdf(pages, fields=None):
    """pages: [[(text, size, bold, y), ...], ...] — one list per page,
    x fixed at 72, y in points from the bottom of a 612×792 page.
    fields: {name: value} → AcroForm text fields on page 1 (non-ASCII
    values as UTF-16BE)."""
    objs = []   # 1-based object bodies

    def add(body):
        objs.append(body)
        return len(objs)

    cat = add(None)
    pages_id = add(None)
    f_reg = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                b"/Encoding /WinAnsiEncoding >>")
    f_bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                 b"/Encoding /WinAnsiEncoding >>")
    kids, widgets = [], []
    for n, lines in enumerate(pages):
        ops = []
        for text, size, bold, y in lines:
            face = "F2" if bold else "F1"
            ops.append(f"BT /{face} {size} Tf 72 {y} Td ({_esc(text)}) Tj ET")
        stream = "\n".join(ops).encode("cp1252")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        annots = ""
        if n == 0 and fields:
            for i, (name, value) in enumerate(fields.items()):
                w = add(f"<< /Type /Annot /Subtype /Widget /FT /Tx /T ({_esc(name)}) "
                        f"/V {_text_string(value)} /Rect [300 {700 - 30 * i} 500 {720 - 30 * i}] >>"
                        .encode("latin-1"))
                widgets.append(w)
            annots = " /Annots [" + " ".join(f"{w} 0 R" for w in widgets) + "]"
        page = add(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
                   f"/Resources << /Font << /F1 {f_reg} 0 R /F2 {f_bold} 0 R >> >> "
                   f"/Contents {content} 0 R{annots} >>".encode("latin-1"))
        kids.append(page)
    acro = ""
    if widgets:
        acro = " /AcroForm << /Fields [" + " ".join(f"{w} 0 R" for w in widgets) + "] >>"
    objs[cat - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R{acro} >>".encode("latin-1")
    objs[pages_id - 1] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] "
                          f"/Count {len(kids)} >>").encode("latin-1")

    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, cat, xref)
    return bytes(out)


def body_lines(texts, size=11, top=650, leading=14):
    """Consecutive body lines down the page."""
    return [(t, size, False, top - i * leading) for i, t in enumerate(texts)]
