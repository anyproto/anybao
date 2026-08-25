# ADR-020: File input — `any` files as model input, by reference

Status: **Accepted** (2026-08-25)
Date: 2026-08-25
Builds on: ADR-002 §1 (http effects), ADR-005 §1 (neutral message
model), ADR-008 §2 (http result shape), ADR-018 (chat attachments
reach the user turn)
Amends when accepted: ADR-002 §1 (http `response` param), ADR-005 §1
(`File` part; `vision` tier), ADR-008 §2 (http result `encoding`)
Upstream: `any` files v2 (`docs/17-files.md`, `docs/19-links.md`)

## Context

`any` stores files as space data (files v2): a file attaches to an
object, is addressed `any://f/<spaceId>/<fileId>`, and its verified
plaintext is one HTTP resource — `GET
/v1/spaces/:s/files/:fileId/content`, `Content-Type` = the stored mime.
Chat messages carry `attachments: {id: {type, link}}`, and the chat
watcher already folds them into the user turn as `[attachment <type>:
any://f/…]` lines (`triggers.rs::attributed_text`). So bao *sees* that
a file was sent — and can do nothing with it:

1. `http.get` reads the response with `into_string()`; a PNG or PDF
   through it is a UTF-8 error. The guest cannot read file bytes.
2. The neutral message model (ADR-005 §1) has no part for non-text
   input; the adapters translate `text|tool_call|tool_result|thinking`
   only.

The providers accept files as content blocks keyed by media type. On
Anthropic exactly three input shapes exist: `image` (png/jpeg/gif/
webp, ≤5 MB, ≤8000 px), `document` with a base64 PDF source (≤32 MB
request, ≤600 pages; 100 on 200K-context models), and `document` with
a plain-text source. Nothing else (docx, xlsx, audio, archives) is
accepted natively — the API 400s. The OpenAI-compatible wire takes
images as data-URI `image_url` blocks and has no document block.

## Decision

### 1. Binary http responses: `response: "base64"` (amends ADR-002 §1, ADR-008 §2)

`http.*` accepts `response: "base64"`. The output keeps its shape
(`{status, headers, body, url}`) with `body` = the base64 of the raw
response bytes and one added field `encoding: "base64"`. Without the
flag the body is text, as today. No new effect: the thin-host rule
holds (nothing file-shaped enters the host; a file read is a recorded
GET like any other), the trace stays the record (a multi-MB body
spills to the blob sidecar over ADR-001's 64 KiB threshold, replay
resolves it), and a binary *request* body — upload — is out of scope
(`json`/`body` stay text).

### 2. `any@v1.file_content(spaceConfig, file)` and `list_files`

`file_content(spaceConfig, file)` — `file` is an `any://f/<sid>/<fid>`
URI or a bare fileId — issues the content GET with `response:
"base64"` and returns `{fileId, mime, size, data}` (`mime` from
`Content-Type`, `size` = decoded byte length, `data` base64). A
`?variant=<tag>` on the URI passes through. `list_files(spaceConfig,
object_id=None)` wraps `GET /files[?objectId=]` (name/mime/size per
file) so bao can see what is attached to an object before reading it.
Both are getters on the flat any surface (ADR-010 §8): they wrap
server endpoints, not a feature.

### 3. `File` part; adapters route by media type (amends ADR-005 §1)

```python
Part += File{media_type, data: <base64>, name?}
```

One neutral part for every file, whatever it came from `any` as —
`file_content`'s result wraps into it directly. `data` is base64
always, text included: byte-exact, no encoding guesses in the guest.
The adapter owns the media-type routing, because that is where the
providers differ:

| media type | anthropic | openai-compat |
|---|---|---|
| `image/*` | `image` / base64 source | `image_url` data URI |
| `application/pdf` | `document` / base64 source | unsupported |
| `text/*` | `document` / text source (decoded UTF-8; `name` → `title`) | unsupported |
| other | unsupported | unsupported |

Unsupported raises `UnsupportedMedia(media_type, provider)` in
`build_request` — *before* any http call, so the trace names the
reason instead of a provider 400. The fenced adapter takes no files.
Files a provider cannot read natively (docx, …) are the guest's job:
convert to text and send a `text/plain` File part — the neutral shape
needs no change for that (an ADR-013 agent-authored program is the
natural home).

### 4. `llm.read(file, prompt, tier="vision")` and the `vision` tier

`read(file, prompt, *, tier="vision", system="", max_tokens=None)` is
the one-call form: `file` is an `any://f/…` URI, a bare fileId with a
`space=` kwarg, or an already-fetched `{mime, data, name?}` dict;
returns the reply text. Internally: `file_content` (when given a
ref) + one `chat` with `[File, Text(prompt)]` parts. `chat` itself
accepts `File` parts on any tier — a text-only tier fails as its
provider fails, honestly.

`llm.tier.vision` joins the config defaults (ADR-006 §3) — the same
model as `codegen` today, so no behavior change — so that pointing
`codegen` at a text-only local model never silently breaks file
reads; the routing decision lives in config, not at call sites.

### 5. Files travel by reference, never in the conversation prefix

The user turn carries `[attachment image: any://f/…]` (ADR-018); bao
reads it with `llm.read` when the task needs it. Bytes cross the wire
once per read, not once per turn: the loop's append-only prefix
(ADR-005 §1 caching) stays text, the persisted turn log stays small,
and a re-read is an explicit, traced choice. Putting a `File` part
into the toolcaller conversation itself is deliberately NOT done —
if multi-turn file chat becomes the use case, the path is a provider
file handle (`provider_ref` on the part, the `provider_state`
pattern), not inline bytes.

### 6. Trace view elides binary

`anyrt trace show` renders a base64 body / `data` field as
`<base64 <n> bytes <mime>>` — in http lines and inside the llm
request. The bytes stay in the trace (blob sidecar); `--seq N` still
dumps them.

## Consequences

- Each read costs the file twice in the trace (GET output + llm
  request input), both as blobs — the price of "if it isn't in the
  trace, it didn't happen". Acceptable at document scale.
- Size/page limits are the provider's; the guest surfaces the 400 as
  `LlmError`. Downscaling images (`?variant=thumb`) is available at
  the ref level when needed.
- A `File` part in a tool result is legal by the shape (a cell may
  return one) — unused for now; the trace view rule covers it.
