# Vendored guest libraries (ADR-012 §6, ADR-002 §4)

Pure-Python packages bundled into the kernel wasm so guest programs can
parse HTML (`clean_html` in gmailSync@v1 and friends). Copied verbatim
from PyPI wheels; no local patches — re-vendor to upgrade.

| package | version | license | guest-importable |
|---|---|---|---|
| bs4 (beautifulsoup4) | 4.15.0 | MIT | yes |
| soupsieve | 2.9.2 | MIT | yes (bs4 dep) |
| markdownify | 1.2.3 | MIT | yes |
| six | 1.17.0 | MIT | no — markdownify internal |
| typing_extensions | 4.16.0 | PSF-2.0 | no — bs4 internal |

bs4 runs on the stdlib `html.parser` backend only (no lxml — C
extension). Guest-importable names are gated by `_ALLOWED` in app.py;
the internals resolve through the real import machinery once bundled.
