# ADR-031: toMarkdown — one conversion program for HTML and PDF, pypdf in the kernel

Status: **Accepted** (2026-10-15)
Date: 2026-10-15
Builds on: ADR-002 §4 (the allowlist, tier 1b vendored packages, the
WASI floor), ADR-003 §2 (fuel, epoch deadlines), ADR-010 (docstrings
as the doc surface), ADR-012 §2/§4/§6 (the backlog governor,
`clean_html`, the vendored trio), ADR-020 (file input), ADR-026 §4
(the guest `Blob`)
Amends: ADR-002 §4 (tier 1b gains `pypdf`; the codec pre-imports),
ADR-003 §2 (fuel is a metric and an advisory budget, not a trap),
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
and found the cliff elsewhere: the 50 G per-run fuel trap, about 5 s
of compute, cut off a 117-page document. Wall-clock deadlines already
bound every run, so the trap guards nothing the deadline does not.

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
empty-anchor and alt-less image drop, the three depth bounds
(wrapper-chain collapse, quote-depth cap 5, browser-style depth cap
200), markdownify, and the plain-text fallback on `RecursionError`.

Stays in `gmailSync`: quoted-chain markers and cited blockquotes,
tracking pixels, tracker-link unwrap and tracking-param strip,
notification-footer trim, the signature split. `clean_html` runs
those on its own soup, serializes, and hands the string to `html()`;
its `{markdown, signature}` contract is unchanged. gmailSync is edited
in place, still `@v1`.

### 3. `pdf()` — pypdf extraction, our markdown passes

Text comes from vendored `pypdf` (§7) through its `visitor_text`
hook, which gives each fragment its text, font, font size and matrix.
Fragments group into lines (text, effective size, bold, y); the
passes, in order:

1. **Running headers/footers** — a line among a page's first or last
   two whose digit-normalized text recurs on at least half of the
   pages (≥ 3 pages) is dropped.
2. **Headings** — a short line (< 120 chars, no terminal period) set
   at ≥ 1.15× the body size (the size carrying the most characters),
   or in a bold face at body size, becomes a heading; levels rank the
   distinct heading sizes, capped at `####`. **Figure guard**: a run
   of consecutive heading candidates that are tiny fragments (axis
   labels, diagram text) stays body text.
3. **Paragraphs** — wrapped lines join; a vertical gap over 1.6× the
   line size breaks; a trailing hyphen joins only into a lowercase
   continuation; a page break ends a paragraph only after terminal
   punctuation.
4. **Lists** — bullet glyphs and `1.` / `a)` markers start `- ` items.
5. **Form fields** — AcroForm values (`get_fields`) with a value
   append as a `Fields` list; `fields` counts them.

Tables come through as their cell text in reading order; images are
ignored.

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

### 6. Fuel is a metric (amends ADR-003 §2)

The store runs with unlimited fuel: no `FuelExhausted` trap. Metering
stays on — `fuel_used` is recorded per run exactly as before, the
compute-cost metric ADR-003 §2 names. The runaway bound is the
wall-clock deadline every run already carries (serve 1200 s,
`anyrt run --timeout-s`), and the interrupt.

`fuel.state` keeps its shape. `budget` stays 50 G as an **advisory**
pacing figure and `remaining` is `budget − used`, floored at 0:
cooperative batch jobs (gmailSync's backlog governor, ADR-012 §2)
still size their slices by it, and nothing traps when it reaches 0.

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
- A run's compute is bounded by time alone. A text page costs ~0.4 G
  fuel (~40 ms), a figure-heavy one up to ~3.6 G; a 117-page document
  converts in about 5 s.
- A runaway cell burns until its deadline instead of stopping at 50 G:
  up to 20 minutes of one core under serve.

## Open questions

1. **One parse or two in gmailSync.** §2 re-parses the email-cleaned
   string; handing `html()` the soup avoids it, if values cross
   `use()` without a trace round-trip. Measured when §2 lands.
2. **Tables.** Out of v1; pypdf gives positions, so a column-gap
   table pass is possible later.
3. **AES.** Revisit with a host pure op for the AES primitive if
   encrypted PDFs turn out common in real use.
