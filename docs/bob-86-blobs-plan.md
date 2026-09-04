# BOB-86 — blobs: implementation order

Contract: [ADR-026](adr/026-blobs.md) + the amendments it lists
(ADR-001 §7, ADR-002 §4, ADR-020, ADR-023). Nothing below lands before
ADR-026 is accepted. One topic = one commit; each commit names its
section. Symptom ticket: WEB-400 (markdown image import).

## Order

Two independent tracks. A is the blob work; B is the determinism
floor + batteries, which A's `zipfile`/`tempfile` recipes depend on
but whose first commits do not.

### Track A — blobs (ADR-026)

1. **Blob directory + ref kind + spill rule** (host only; §1, §2).
   `runtime/src/trace.rs`: `{__blob, bytes, mime}` writer to
   `<traces_dir>/blobs/<hex>` (temp + rename, idempotent); the text
   spill over 700 KB canonical goes raw; a failed write warns and
   keeps the sink. `tracestore.rs`: `blob(run, hash)` resolves
   collection then directory in both backends. `replay.rs`:
   `resolve_blobs` leaves raw refs as refs. Unit tests: spill routing,
   idempotent write, sink survives a failed write.
   *Verify:* a `file_content` of a >1 MB file no longer loses the run
   (rig; today it does).
2. **`trace show` / `trace blob` / retention sweep** (§6, §7).
   `view.rs` renders `<blob mime n bytes sha256:…>`; `anyrt trace
   blob <hash> [-o]`; `serve.rs::expire_traces` unlinks unreferenced
   files after record expiry; `trace import` copies `blobs/`.
   Retention unit test with a referenced and an orphan file.
3. **http: implicit classification + payload forwarding** (host; §3).
   `broker.rs`: media-type table → text or raw ref in `body`;
   `response: "text"` override; `response: "base64"` and `encoding`
   removed; ref expansion inside `json` (base64 string) and as `body`
   (raw, Content-Type from mime); recorded input keeps the refs.
   Golden: a PNG GET yields a ref; a POST with a ref in `json` sends
   base64 and records the ref; a POST with `body=ref` sends raw bytes.
4. **Guest `Blob` + syscalls** (§4). `runtime/guest/app.py`: `Blob`
   (`sha256/size/mime`, `read/seek/tell`, `bytes()` with the 64 MiB
   refusal, `text()`, repr, JSON as ref), `Response.blob` +
   the typed error on `.text/.json()`, `blob.from_bytes`, the writer;
   `broker.rs`: `blob.read`, `blob.put` (normaliser writes the file,
   records the ref). Kernel rebuild. Guest tests through the kernel
   (`tests/`): round trip, ceiling, replay of a run with a `blob.put`.
5. **`any@v1.attach_file` + `file_content` on the handle; `llm@v1`
   File-part refs** (§5). `attach_file(space, object_id, name, data,
   mime=None)` → FileInfo + `uri`; `file_content` → `{fileId, mime,
   size, blob}`; `llm.read` takes a Blob; adapters pass `data` through
   whether str or ref (anthropic text-document path uses `b.text()`);
   `test_any_module`, `test_llm_module`; parity goldens re-checked
   (the wire is unchanged — the fixture inputs change shape).
6. **Skill recipes** (`_any.md`): import a page with images (fetch →
   attach → rewrite `![alt](any://f/…)` and GitBook `<figure><img>`
   before `put_markdown`); "create a file" = attach to an object;
   zip the space's markdown (`zipfile` + `tempfile` → `attach_file`).
   Files-related lines in ADR-020's skill text updated to the handle.
7. **Migrate `fs.read_bytes` / `fs.write_bytes`** (ADR-024 §1) onto
   the handle — `fs.read(path, encoding="blob")` → ref, `fs.write`
   with a Blob body. Small, last, optional for the first release.

### Track B — determinism floor + batteries (ADR-002 §4)

8. **WASI floor.** `runner.rs`: `wall_clock` = cell start (frozen),
   `monotonic_clock` = counter, `secure_random`/`insecure_random` from
   a per-run seed; the seed in the run header record (ADR-001 §4);
   replay reuses it. Delete `_random_proxy`; `rand()` over stdlib
   `random`. Test: two runs from one seed produce one sequence;
   `uuid.uuid4()` in a replayed cell matches the recording.
9. **Allowlist table.** `app.py`: dotted-name matching, the tier-1
   list, the refused-with-pointer map and messages; proxies for `io`
   (minus `open`), `tempfile` (→ blob writer, so this lands after A4),
   `sqlite3` (`:memory:`), `mimetypes` (built-in table); `os` gains
   `fspath`/`PathLike`. The audit test: import every admitted name,
   a zip write with a deterministic timestamp, an in-memory sqlite
   query, every refused name's message. Kernel rebuild.

### Release

10. `make kernel && make runtime`; deploy `repos/_agent` to the
    staging repo space; rig e2e on staging-clean (`:7141`, serve
    `configs/anybao.staging-clean.toml`):
    - WEB-400 scenario: "import <GitBook page url> into a page" —
      trace shows `http.get` → ref, `attach_file` POST with `body=ref`,
      `put_markdown` with `any://f/` links; the page renders images in
      any-ui.
    - zip scenario: "zip every markdown page in this space onto
      object X" — one `attach_file`, archive opens.
    - replay of both runs from the directory; `trace show` stubs;
      `expire_traces` with retention set to minutes sweeps the files.
    - the read-side regression: `llm.read` on a 3 MB PDF keeps the run.
11. Prod: any-server fleet needs nothing; `anyrt` rebuild on the mac
    (runtime + kernel), `repos/_agent` deploy via `:7003`.
12. Linear: rewrite BOB-86's body to the ADR (drop the step 1/2/3
    framing), link WEB-400 as its symptom, comment on WEB-400 with the
    root cause (bao had no upload path; the `/files/<id>` paths were
    GitBook's, not fabricated).

## Not in this work

Host pure ops over blobs (`image.*`, `pdf.text`), a raw-bytes WIT
import, temporary directories / `glob`, a server-side blob route — each
is its own ADR item if traces show the need.
