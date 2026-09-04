# ADR-026: Blobs — bytes as handles, a host-owned content-addressed store beside the trace

Status: **Accepted** (2026-09-04)
Date: 2026-09-04
Builds on: ADR-001 §7 (spill), ADR-002 §4 (allowlist, proxies), ADR-003
§4 (trace views), ADR-020 (file input), ADR-023 (trace store), ADR-024
§1 (`fs.*`)
Amends when accepted: ADR-001 §7 (two ref shapes), ADR-002 §4 (tiers,
WASI determinism floor), ADR-020 §1/§2/§3/§6 (`response: "base64"`
retired; `file_content` returns a Blob; `File.data` may be a Blob;
trace view), ADR-023 §4/§6/§7 (raw blobs in the directory; retention
sweep; tooling)
Tracks: Linear BOB-86 (design), WEB-400 (symptom: markdown image import)

## Context

bao can read `any` files (ADR-020: `list_files`, `file_content`,
`llm.read`) and cannot write one. There is no attach on `any@v1`, and
the guest could not build one itself: the boundary is
`host-effect(name, payload: string) -> string`, `http.post` sends
`json`/`body` as text, and ADR-020 §1 put base64 on the *response*
side only. A JPEG fetched with `response: "base64"` is bytes the guest
can POST nowhere. The server side has had the route all along —
`POST /v1/spaces/:s/objects/:o/files?name=…`, raw body, exempt from
the body cap — and the host client wraps it for deploy
(`AnyApi::attach_file`, ADR-009 §4).

The read half is not sound either. A spilled trace value is one
document `{id, bytes, data}` in the `trace_blobs` local collection
(ADR-023 §4), written over `/v1/local/upsert`, whose body cap is 1 MiB.
A `file_content` of a 1 MB image is a 1.4 MB base64 body: the upsert
fails, the sink is dropped, the run buffers in memory, and the dump
fails on the same document — the run's records never land. Behind
that route the store is any-store v2: a B-tree pager with 4 KiB pages,
overflow chains for large values, S2 on values over 256 bytes, in the
same file as every synced record of the account. It is a document
store; a 5 MB value is ~1300 chained overflow pages cloned whole on
every balance. The trace store on a staging rig holds 104 blobs, 8.3 MB
in total, the largest 144 KB — it has never seen a payload the size of
an image, and it should not.

The requests behind this: "add images to the flower pages", "import
the Anytype docs" (WEB-400: pages landed, every image stayed a literal
`![alt](url)` or a GitBook `<figure><img>` wrapper — the any-ui editor
renders images only from `any://f/<space>/<file>` links, by design),
"zip the markdown in this space".

## Decision

### 1. A Blob is a reference; the bytes live in a directory bao owns

```
{"__blob": "sha256:<hex>", "bytes": <n>, "mime": "<media type>"}
```

The bytes sit at `<traces_dir>/blobs/<hex>` — one file per content
hash, written once (temp + rename), deduped by name, owned by the
serve process, per device like the rest of the trace body (ADR-023
§1). The hash is over the raw bytes. The reference is the only thing
that ever appears in a trace record, a cell value, or an effect
payload; the guest never sees a path.

The `any` server holds nothing bao-specific for this: the local
collections keep what they keep today, the directory is a sibling of
the jsonl dir the file backend already uses, and a full wipe of a
rig's traces is "drop the three trace collections, remove the
directory".

### 2. Which spills go where (amends ADR-001 §7, ADR-023 §4)

Two reference shapes, told apart by `mime`:

| shape | content | store |
|---|---|---|
| `{__blob, bytes}` | canonical JSON text of a value over the spill threshold (64 KB) | `trace_blobs` collection (any backend) / `.jsonl.blobs` sidecar (file backend) — as today |
| `{__blob, bytes, mime}` | raw bytes | `<traces_dir>/blobs/<hex>`, both backends |

A text spill that would not fit one local-store request (> 700 KB
canonical) is written as a raw blob with `mime: "application/json"`
instead of failing. Text spills stay queryable through
`effects.query(coll="blobs")` (ADR-023 §5); raw blobs are not
queryable by design — they are the bytes the model fetched or built,
the least query-worthy content there is.

A blob write that fails never fails the run: the ref is recorded, the
failure is a warning naming the run and hash, and `trace show` renders
the ref unresolved. The sink is not dropped; a refused text spill gets
one more write at run end.

A record whose `input`/`output` carries raw refs anywhere — an http
body, a File part's `data`, a request payload — lists their hashes in
a top-level `blobs` array. That field is the index every raw-blob
reader uses: the retention sweep's live set (§6) and the CLI's
resolution (§7) never walk record bodies for refs.

### 3. `http.*`: the host classifies the body; blobs forward through payloads (amends ADR-002 §1, ADR-020 §1)

**Response.** The host decides text versus bytes from the actual
response, never the caller: `text/*`, `application/json`, `+json`,
`+xml`, `application/xml`, `application/x-www-form-urlencoded`,
`application/javascript` bodies that decode as UTF-8 come back as
`body` text exactly as today; every other media type, and any body
that fails UTF-8, comes back as a Blob reference in `body`. The
output shape is unchanged (`{status, headers, body, url}`); the trace
record carries the ref. `response: "text"` forces text (UTF-8 with
replacement) for a misdeclared body — the one direction the host can
be wrong in a way the guest can fix. `response: "base64"` is retired:
nothing needs base64 in the guest once bytes are a handle.

In the guest, `Response.text` / `.json()` raise a typed error naming
`.blob` when the body is a Blob; `Response.blob` is `None` when the
body is text. A wrong assumption fails on the next line, loudly, not
in the model's rendering of the page.

**Request.** A Blob reference anywhere in an `http.*` payload is
expanded by the host at send time: inside `json`, the ref is replaced
by the base64 string of the bytes (the provider wire for images and
PDFs, ADR-020 §3); as the whole `body`, the raw bytes are sent with
`Content-Type` from `mime` unless a header says otherwise (the `any`
attach route, any raw upload). The **recorded input keeps the ref** —
a request carrying a 5 MB image is a trace record of a few hundred
bytes, not a spilled 6.7 MB body. This is the only forwarding
mechanism; there is no host-side `attach_file` effect, because the
`any` client is guest Python (ADR-002 thin-host doctrine) and
`http.post(url, body=blob)` is the whole of an upload.

Classification and capability do not change: `http.post` is `mutate`,
the route table decides the cap, the redaction paths apply — the
blob ref is just a value in the payload.

### 4. The guest surface: `Blob`, readers, writers

```python
r = http.get(url)                 # r.blob when the body was bytes
b = r.blob                        # Blob: .sha256 .size .mime — 0 bytes in the guest
b.size, b.mime                    # metadata, before any read
head = b.read(1024); b.seek(0)    # file-like; the host serves the range
data = bytes(b)                   # the whole payload (ceiling: 64 MiB, refused with a hint)
text = b.text()                   # bytes(b) decoded as UTF-8, for text/* payloads

b2 = blob.from_bytes(data, mime="application/zip")   # guest bytes → Blob
with tempfile.TemporaryFile() as w:                  # writer: chunks in …
    w.write(chunk)
    b3 = w.blob                                       # … Blob out (finalised on close)

c.attach_file(space, object_id, "rose.jpg", b)       # any@v1, §5
```

- `Blob` serialises as its reference: it can sit in an effect
  payload, a cell's return value (`values` shows the ref), a `File`
  part. `print(b)` renders `<Blob image/jpeg 563619 bytes sha256:…>`.
- Two syscalls, both class `read` (no world effect; deterministic on
  content): `blob.read {hash, offset, length} → {data}` and
  `blob.put {data, mime} → ref`. `blob.put`'s normaliser (ADR-001 §3)
  materialises: it writes the file and records the **ref** as the
  input — the bytes are in the directory, not in the record. Replay
  serves both from the directory, which is part of the replay corpus
  the way the sidecar is.
- Bytes cross the boundary only when the guest asks (`read`,
  `bytes()`, `from_bytes`, the writer), as base64 inside the existing
  JSON envelope — the escape hatch, not the main path. Fetch → attach
  moves zero payload bytes through the guest. A raw-bytes WIT import
  is a later optimisation of that hatch, taken only if traces show a
  guest pulling large payloads for a real reason.
- `bytes(b)` refuses above 64 MiB with a hint to `read(n)`; the
  number moves on data (ADR-003 §5 rule).
- `tempfile.TemporaryFile` / `NamedTemporaryFile` /
  `SpooledTemporaryFile` are the writer, through the proxy tier
  (ADR-002 §4): the file object the guest writes becomes a Blob on
  close. Temporary *directories* are out of scope here — they arrive
  with the ADR-024 fs surface as a scoped folder under the same
  directory, and `glob` with them.

### 5. `any@v1`: `attach_file`, `file_content` on the handle (amends ADR-020 §2)

- `attach_file(spaceConfig, object_id, name, data, mime=None)` — `data`
  is a Blob or `bytes` (wrapped with `from_bytes`); `mime` defaults to
  the Blob's. One `http.post` with `body=blob` to the attach route;
  returns the server's `FileInfo` plus `uri: any://f/<sid>/<fileId>`.
  There is no file without an object in `any` (files bind to objects,
  any docs/17): "create a file" is always "attach to this object".
- `file_content(spaceConfig, file)` returns the Blob the content GET
  produced (`{fileId, mime, size, blob}`); `list_files` is unchanged.
- `llm.read` accepts a Blob; a `File` part's `data` is base64 **or a
  Blob ref** (ADR-020 §3) — the adapters place whatever they were
  given into the provider JSON and the host expands it (§3). The
  anthropic text-document path, which needs the decoded text in the
  guest, calls `b.text()`.

### 6. Retention and cleanup (amends ADR-023 §6)

The retention pass (serve's hourly ticker) gains one step after record
expiry: collect the hashes every surviving record lists in `blobs`
(one `$exists` query, the shape that already protects `trace_blobs`),
list the directory, unlink every file whose hash is not referenced. Raw blobs
follow their runs' retention class; nothing is kept past the last
record that names it. The file backend, which has no retention today
(ADR-023 §6), gains none — its directory is swept by hand with its
jsonl dir.

### 7. Tooling (amends ADR-020 §6, ADR-023 §7)

- `anyrt trace show` renders a raw ref as one line, `<blob image/jpeg
  563619 bytes sha256:…>`, in http lines, inside llm requests, in cell
  values — never inline base64. `--seq N` prints the record with the
  ref; `anyrt trace blob <hash> [-o file]` writes the bytes.
- The CLI resolves raw blobs from `traces_dir` of the config file it
  reads `--addr` from — same machine as the serve. Against a serve on
  another machine the ref shows unresolved; the local store is
  per-device already, this changes nothing about scope.
- `anyrt trace import <dir>` copies a jsonl dir's `blobs/` along.

## Consequences

- WEB-400's recipe becomes three lines the `_any` skill carries:
  `http.get(img_url).blob` → `attach_file(space, page, name, blob)` →
  rewrite `![alt](<uri>)` / GitBook `<figure><img>` wrappers before
  `put_markdown`.
- A file read no longer costs the file twice in the trace (ADR-020
  Consequences): the GET output and the llm request both carry the
  same ref; the bytes exist once, in the directory.
- The read-side failure (a >700 KB `file_content` losing the run) is
  closed by §2.
- The guest allowlist grows the batteries this needs (`zipfile`,
  `tarfile`, `gzip`, `csv`, …) under ADR-002 §4 as amended alongside;
  the determinism those modules need (timestamps in archives, seeds)
  comes from the WASI floor, not from proxies.
- Host pure ops over blobs (`image.resize`, `pdf.text`, hash → hash)
  are the natural next step but are **separate features**, each
  admitted on the ADR-002 §4 test; none is part of this ADR.

## Open questions

None blocking. Settled in review 2026-09-04: bao owns the directory
(no server-side blob route); text spills stay in the collection;
unresolved refs on remote `trace show` are acceptable; no staged
"step 1/2/3" — one design, committed one topic at a time.
