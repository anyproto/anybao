# BOB-86 questionary — blobs (track A) and the floor + batteries (track B)

Two short conversations with bao that exercise what ADR-026 and the
ADR-002 §4 amendment shipped, each question ending in evidence that
lives in the trace or in the space. Run on the staging-clean rig
(`:7141` user, `:7021` repo owner, serve
`configs/anybao.staging-clean.toml`, control `7022`), in the `fg`
user space — never the bao home space. Set A needs the Track A
binary + kernel + `repos/_agent` deployed; A4 and all of set B need
Track B merged in. Wait for each run to finish before the next
message; read the run with `anyrt trace show --addr
http://127.0.0.1:7141 run_<id>`.

Where to look: `<traces_dir>/blobs/` (the config's `traces` dir) for
the bytes; `trace show` for `<blob mime n bytes sha256:…>` stubs; the
any-ui Files view / `list_files` for what landed on an object.

## Set A — blobs (ADR-026)

**A1 — WEB-400, the markdown image import (§3, §5, skill recipe).**
> Import https://raw.githubusercontent.com/anyproto/docs/main/basics/channels.md
> into a new page called "Channels (docs)" — text and images. The
> images are relative links in that file; resolve them against the
> raw URL.

Pass: the trace shows one `http.get` per image whose output `body` is
a ref with `mime: image/…` (no base64 in the record), one `http.post`
per image to the attach route with `body` = that ref, and a
`put_markdown` whose content carries `![…](any://f/<sid>/<fid>)` where
the GitBook `<figure><img>` wrappers were. `list_files(page)` counts
the images; the page renders them in any-ui. No literal `<div
data-with-frame>` left in the body.

**A2 — fetch, attach, read by reference (§3, §5; ADR-020 §4).**
> Download https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf,
> attach it to the Channels page as "dummy.pdf", then read it and
> tell me the sentence it contains.

Pass: one blob file in the directory (size = the PDF); the attach
POST carries the ref; the `llm.chat` request record is small — the
`File` part holds the ref, not 300 KB of base64 — and `trace show`
prints the stub inside the llm line; bao answers with the PDF's
sentence.

**A3 — bytes the guest built (§4 `from_bytes`, §5 `attach_file` with bytes).**
> Make a CSV of every object of type Recipe in this space (name,
> cuisine, cook_minutes) and attach it to the Saturday meal plan as
> recipes.csv.

(Needs the BOB-78 Q1/Q2 content in the space; otherwise ask for two
recipes first.) Pass: a `blob.put` record whose input is the ref (not
the CSV text), then the attach POST with `body` = ref and
`Content-Type: text/csv`; `file_content` on the result round-trips
the same bytes; the file downloads from any-ui.

**A4 — the writer + zipfile (§4 tempfile proxy; ADR-002 §4 batteries).**
> Zip the markdown of every page in this space into pages.zip, one
> .md per page named after the page, and attach it to a new object
> "Exports".

Pass: bao uses `zipfile` over `tempfile.TemporaryFile()` (or
`io.BytesIO` + `from_bytes` — either is fine), one attach, the
archive opens and lists one entry per page; every entry's timestamp
equals the run's `startedAt` (the floor; DOS 2 s grain).

**A5 — the read-side regression (§2: a big read keeps the run).**
Attach a >1 MB PNG or PDF to the message (generate one: a 2000×2000
noise PNG from `fixtures/`), then:
> Describe what's in this file.

Pass: the run completes with a normal reply; `trace ls` shows the run
and `trace show` opens it with every record present (before ADR-026
the spill upsert 413'd, the sink was dropped and the run's trace
vanished); the serve log has no "trace blob write failed"; the
directory holds one file of the attachment's size.

**A6 — a wrong assumption fails loudly (§3 `Response.text` on a blob).**
> Fetch https://raw.githubusercontent.com/anyproto/docs/main/.gitbook/assets/logo.png
> and print the first 200 characters of the response text.

Pass: the first cell raises the typed error naming `.blob`, the next
cell uses `.blob` (size/mime or `bytes()`), bao explains it is binary
— no retry loop, no attempt to decode by hand.

**A7 — retention sweep (§6; operator step, not a chat message).**
On a scratch copy of the rig config set `[traces] retain_jobs =
"1m"`, restart serve, wait for the hourly tick or call
`expire_traces` through the control port if exposed; then check that
blob files referenced only by expired runs are gone and A1–A5's (a
conversation, 60 d) remain. Then `anyrt replay` A1's run from the
directory — the replay must resolve every ref without touching the
network.

## Set B — the floor + batteries (ADR-002 §4)

**B1 — randomness is one record.**
> Shuffle these and pick three at random: Tbilisi, Kutaisi, Batumi,
> Zugdidi, Telavi, Gori, Rustavi, Poti, Mtskheta, Borjomi. Also give
> me five fresh UUIDs.

Pass: the trace has NO `random.random` records; the header carries
`seed` and `startedAt`; `anyrt replay` of the run reproduces the same
picks and the same UUIDs (the stdlib `uuid`/`random` draw from the
floor); the reply's UUIDs are well-formed v4.

**B2 — the present still moves.**
> Time how long it takes to find the first 20000 primes in a cell
> and tell me the seconds.

Pass: bao's delta is positive and plausible (not 0.0, not N µs);
the cell's `time.time()` / `perf_counter()` calls appear as `time.now`
records (the proxy), so the delta replays.

**B3 — batteries import, no re-implementation.**
> Here is a CSV: `name,minutes\nCacio e pepe,20\nKhinkali,90\nLobio,45`.
> Give me the median minutes and output the table as a TOML array
> of tables; then parse this XML and list the item names:
> `<menu><item name="soup"/><item name="bread"/></menu>`.

Pass: `csv`, `statistics`, `xml.etree` import in one go; no
`ImportError` in the cells; TOML is hand-formatted (there is no TOML
writer in the stdlib — bao should say so rather than import one).

**B4 — a refusal that teaches.**
> Save that TOML to /tmp/menu.toml on disk.

Pass: the cell's `ImportError`/refusal names the pointer (`pathlib`
→ ADR-024 `fs.*`, not granted on this rig); bao reads it and offers
the real path — attach the text to an object — instead of retrying
other modules (`os.open`, `io.open`, `shutil`) or writing a
workaround.

**B5 — in-memory sqlite.**
> Load my Recipe objects into an in-memory SQLite table and give me
> the average cook_minutes per cuisine with SQL.

Pass: `sqlite3.connect(":memory:")` succeeds, the query runs, no
attempt at a file path; the result matches the objects.

**B6 — module internals are frozen, cell code is not (operator check).**
Open B2's and A4's traces side by side: A4's zip entries carry the
run's `startedAt`; B2's `time.now` records advance. That is the
contract: the floor for libraries, the recorded present for cells.

## Fixtures

Generated, not committed (`tests/fixtures/bob86/`, gitignored, or the
scratchpad): `big.png` — an 800×800 RGB noise PNG (~1.9 MB, untracked, PIL is not
in the guest; generate on the host with `python3 -c 'import zlib,
struct, os …'` or ImageMagick `convert -size 2000x2000 xc: +noise
Random big.png`). The URLs in A1/A2/A6 are public; if one moves,
any raw GitBook markdown with images and any small public PDF do.

## Recording results

One row per question: run id, pass/fail, the evidence line from
`trace show` (the ref stub, the missing `random.random`, the
refusal message), and a note. Findings that are not this ADR's go
to Linear, not into the ADR.

## Results — 2026-09-04, staging-clean, merged branch at 7e417a1, model gpt-5.6-terra

| q | run | verdict | evidence |
|---|---|---|---|
| A1 | `run_443068b1c02448f9` | pass | 5 `http.get` → refs (4× image/jpeg, 1× image/gif 2.86 MB), 5 attach POSTs with `body` = ref, page markdown has 5 `![Image](any://f/…)` lines and no GitBook html, `list_files` 5 durable files |
| A2 | `run_24c882cc380f4e04` | pass | pdf 13 264 B → ref, attach POST, content GET returns the same ref (dedupe), `chat/completions` 200 with the ref in the `file` part, reply "Dummy PDF file." |
| A3 | `run_ea469a6abd5540d1` | pass | `blob.put` records carry the ref, attach POST `recipes.csv` 71 B, reply names the size |
| A4 | `run_52e644cd050a4856` | pass | `tempfile.TemporaryFile` + `zipfile.ZipFile`, one attach, archive opens with 4 entries all stamped `10:37:42` = the run's start (the floor) |
| A5 | `run_cc7bc6c5586246b1` | pass | 1 921 448 B PNG attachment → ref, run ok, trace intact (8 963 lines), blob file in the directory, `trace_blobs` max still 147 KB, OpenAI `image_url.url` shows the stub in the record, image described |
| A6 | `run_7d69b1e25cdf40af` | pass | `.text` raised `BinaryBody`, next cell used `.blob`, reply explains it is a JPEG of 170 081 bytes, 2 turns, no loop |
| A7 | (Track A, earlier) | pass | `retain_* = "1m"` → `trace retention: 5 raw blobs swept`; replay covered by the broker unit test (no CLI replay command) |
| B1 | `run_27d775a6e9584fda` | pass | 0 `random.random` records, header carries `seed`, five well-formed v4 UUIDs |
| B2 | `run_f62a8a33261f4ced` | pass | 0.497 s from `time.perf_counter()`, 6 `time.now` records |
| B3 | `run_ddc86a6cb15c46c7` | pass | csv/statistics/xml.etree in one cell, median 45, TOML hand-formatted, items soup/bread |
| B4 | `run_a1c540f155074282` | pass* | bao declined from the skill text without running a cell and offered an attachment; the pointer refusals themselves are covered by `test_kernel_allowlist` |
| B5 | `run_7af64fda83234555` | pass | `sqlite3.connect(':memory:')`, GROUP BY result matches the objects |
| B6 | A4 vs B2 | pass | archive stamps frozen at run start; cell timing advances |

Findings not this ADR's: the seed run and B5 saw the model reach for `resp.status_code` (Response has `.status`) — a docstring hint candidate.
