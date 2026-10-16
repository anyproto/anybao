# ADR-031: toMarkdown — one conversion program for HTML and PDF, pypdf in the kernel

Status: **Accepted** (2026-10-15)
Date: 2026-10-15
Builds on: ADR-002 §4 (the allowlist, tier 1b vendored packages, the
WASI floor), ADR-003 §2 (fuel, epoch deadlines), ADR-010 (docstrings
as the doc surface), ADR-012 §2/§4/§6 (the backlog governor,
`clean_html`, the vendored trio), ADR-020 (file input), ADR-026 §4
(the guest `Blob`)
Amends: ADR-002 §4 (tier 1b gains `pypdf`; the codec pre-imports),
ADR-003 §2 (the per-run fuel budget is 250 G),
ADR-012 §4 (the generic passes move here; gmailSync keeps the email
passes) and §6 (the vendored set), ADR-020 (the "guest's job"
conversion home), ADR-026 Consequences (`pdf.text` is a guest
program, not a host op)
Probe: [`docs/tomarkdown-probe-results.md`](../tomarkdown-probe-results.md)

## Context

HTML→markdown exists once, inside `gmailSync@v1` (`clean_html`, ADR-012
§4): vendored bs4 + markdownify wrapped in five email passes and a
depth bound that keeps markdownify's recursion under the kernel's frame
limit. Nothing else can call it, and half of it is not email-specific —
the depth bound protects any HTML a program fetches.

PDF has no converter at all. A model that reads PDFs natively gets the
file as a File part (ADR-020); a text-only tier on OpenRouter gets
the provider's parser; everything else raises `UnsupportedMedia`, and
the `_any` skill tells bao to make "at most one text-extraction
attempt" with nothing to attempt it with. ADR-020 already names the
gap ("files a provider cannot read natively are the guest's job:
convert to text"), and ADR-026 floated a host `pdf.text` op as a
separate feature.

The constraint is the kernel: guest code is CPython in wasm, so a
converter must be pure Python and deterministic. The probe ran pypdf
in the kernel at ~3× native, with output identical to host CPython —
and found the cliff elsewhere: the 50 G per-run fuel budget, about
4.5 s of compute, cut off a 117-page document.

## Decision

### 1. One program, `agent:toMarkdown@v1`

A tool program in `repos/_agent` (`__any_tool__ = True`, `@span` on
each method, ADR-010 docstrings), callable by bao in a cell and by any
program through `use("agent:toMarkdown@v1")`:

```
html(text) → {markdown}
pdf(data, pages=None) → {markdown, first, last, total, next, scanned, fields}
```

`data` is `bytes` or a `Blob` (ADR-026 §4) — an `any://f/` file read
through `any@v1` or an `http.get(...).blob` goes in as-is. Both
methods are pure compute.

### 2. `html()` — the generic half of `clean_html` (amends ADR-012 §4)

Moves here unchanged in behaviour: script/style/head/meta drop, hidden
elements (`display:none`, `max-height:0`), layout-table flattening,
1×1 tracking pixels, alt-less images and the empty anchors they leave,
the three depth bounds
(wrapper-chain collapse, quote-depth cap 5, browser-style depth cap
200), markdownify, and the plain-text fallback on `RecursionError`.

Stays in `gmailSync`: quoted-chain markers and cited blockquotes,
tracker-link unwrap and tracking-param strip,
notification-footer trim, the signature split. `clean_html` runs
those on its own soup, serializes, and hands the string to `html()`;
its `{markdown, signature}` contract is unchanged. gmailSync is edited
in place, still `@v1`.

### 3. `pdf()` — pypdf extraction, our markdown passes

Text comes from vendored `pypdf` (§7) through its `visitor_text`
hook, which gives each fragment its text, font, font size and matrix.
Fragments group into lines — text, size (the one most of the line's
characters are set in), bold (every fragment in a bold face), rotated,
y — and the passes run over the converted pages, in order:

1. **Running headers/footers** — a line among a page's first or last
   two (one, on a page of six lines or fewer) whose digit-normalized
   text recurs on at least half of the pages (≥ 3 pages) is dropped.
2. **Headings** — a candidate is a line under 120 chars, with at least
   3 letters, not rotated, not ending in `.`/`,`/`;`/`:`, and set at
   ≥ 1.15× the body size (the size carrying the most characters) or in
   a bold face at body size. **Figure guard**: a run of consecutive
   candidates stays body text when it is 5+ long, or 3+ long with a
   median under 12 chars (axis labels, diagram text). Levels rank the
   surviving size candidates' distinct sizes, largest `#`, capped at
   `####`; bold-at-body headings take the next level down.
3. **Paragraphs** — wrapped lines join; a vertical gap over 1.6× the
   line size breaks; a trailing hyphen joins only into a lowercase
   continuation; a page break ends a paragraph only after terminal
   punctuation; body text opening with `#` is escaped (`\#`).
4. **Lists** — bullet glyphs and `1.` / `a)` markers start `- ` items;
   consecutive items form one list.
5. **Form fields** — AcroForm values (`get_fields`) with a value
   append as a `Fields` list on a call starting at page 1; `fields`
   counts them.

Tables come through as their cell text in reading order; images are
ignored. Diagram text set at heading sizes can still surface as
headings — font size and position do not separate it from a title.

### 4. Page-bounded calls

`pages = (first, last)`, 1-based and inclusive; the default is
`(1, 50)`. A call converts that range and returns `next` — the first
page it did not convert, `None` when the document is done — so a
longer document continues with `pages=(next, next + 49)`, or asks for
more in one call. Opening a file costs compute by its size and page
count before any page converts, and each call pays it again. Input
over 16 MiB raises `TooLarge` before parsing, pointing at the
model-native read (ADR-020) instead.

### 5. What `pdf()` does not read

- **Scanned PDFs** (no text layer): `scanned: true` when the text
  averages under 20 characters a page; `markdown` holds whatever text
  there was. The caller routes the file to a model that reads PDFs
  natively or to an OCR tier (ADR-020); `pdf()` never OCRs.
- **Encrypted PDFs** — every handler, owner-password-only files
  included: `PdfEncrypted`, pointing at the model-native read. pypdf
  itself decrypts RC4 but needs a C crypto library for AES, and the
  pure-Python alternative spends ~45 G on AES-256 key derivation
  alone; one rule for all handlers keeps the refusal predictable.

### 6. The per-run fuel budget is 250 G (amends ADR-003 §2)

`FUEL_PER_CELL` is 250 G, about 20–25 s of pure compute: a ~100-page
text PDF converts inside one run with room left for the conversation
around it, and fuel stays the deterministic runaway ceiling.
`FuelExhausted`, `fuel.state` and gmailSync's backlog governor
(ADR-012 §2) are unchanged; the governor's slices simply grow.

### 7. Kernel amendment (ADR-002 §4, ADR-012 §6)

- Tier 1b gains **`pypdf`** (BSD-3), vendored verbatim under
  `runtime/guest/`, imported literally in `app.py`, guest-importable.
- **Codecs pre-imported** in `app.py` — the lazy-codec rule ADR-002 §4
  already states for `cp437`: `utf_16_be` is on nearly every PDF's
  string path, plus the UTF-16/32 family, `latin_1`, `cp125x`,
  `mac_roman`, `iso8859_15`, `charmap` and the CJK codecs pypdf names
  (`gbk`, `gb2312`, `cp932`, `cp950`, …).
- **The build precompiles bytecode** (`compileall` over
  `runtime/guest` before `componentize`). From a tree without
  `.pyc` files the pre-init snapshot keeps the compiler's heap: the
  kernel builds 25.1 MB clean vs 23.0 MB precompiled, and with pypdf
  50.7 MB vs 28.6 MB. CI always builds clean, and the kernel is
  embedded in `anyrt`.

### 8. Guidance riding along

- `_any.md`: the `UnsupportedMedia` line names `toMarkdown.pdf` as
  the one extraction attempt, and `scanned` / `PdfEncrypted` as the
  cue to stop and tell the user.
- ADR-020's "the guest's job" paragraph points here; ADR-026's
  `pdf.text` bullet records that the guest program took that role.

## Consequences

- Any program converts HTML with the depth bound included; a web
  fetch can hand the model markdown instead of raw HTML.
- Bao reads text PDFs on every tier, text-only models included,
  without a provider's parser or a paid OCR; native-PDF tiers keep
  the native read as the better path for layout-heavy files.
- The kernel grows 5.6 MB (23.0 → 28.6 MB), and so does every
  `anyrt` binary.
- A text page costs ~0.4 G fuel (~40 ms), a figure-heavy one up to
  ~3.6 G; a 117-page document (~50 G) converts in one run.
- A runaway cell stops after ~20–25 s of compute instead of ~4.5 s.

## Open questions

1. **One parse or two in gmailSync.** §2 re-parses the email-cleaned
   string; handing `html()` the soup avoids it, if values cross
   `use()` without a trace round-trip. Measured when §2 lands.
2. **Tables.** Out of v1; pypdf gives positions, so a column-gap
   table pass is possible later.
3. **AES.** Revisit with a host pure op for the AES primitive if
   encrypted PDFs turn out common in real use.
