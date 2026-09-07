# Skill: _any

**Object-first.** Every task routes through the space first. When the
user says "create", "track", "save", "add", "organize" — assume they
mean an object (page, note, task, collection, custom type) unless they
explicitly say otherwise. Objects are the default unit of work.

Core mechanics (import once: `c = use("agent:any@v1")` — a flat module,
every space-scoped call takes a spaceConfig first: a space NAME
("dev"), `currentUserSpace`, `baoSpaceConfig`, a space id, or a
`list_spaces()` row. In cell code
always qualify harness modules with the `agent:` overlay alias;
unqualified `use("any@v1")` resolves only in your working space and
fails in the standard overlay setup, ADR-004 §2):

- Everything in a space is a **typed object**. Types define which
  properties objects can have.
- Discover existing types before creating new ones —
  `c.query_objects(space, ...)` over the catalog or
  `c.list_properties(space, type_xkey)` for one type's property map
  (`[{handle, name, kind, scope, format?, options?}]`, in display
  order). Types are ALWAYS named by xKey (an unknown xKey errors with
  the catalog); never pass or repeat raw type content-ids. Find an
  existing fit first; avoid inventing parallel types.
- Types are referenced by **xKey** (the stable slug, e.g. `"pages"`),
  properties by their **`handle`** (the xKey when it is a real slug,
  else the display name — UI-made properties often carry no xKey or a
  marker one like `select`), NEVER the raw content id — the client
  resolves handles to ids under the hood; an ambiguous key errors
  listing the candidates.
  Builtins use their id (`chat`, `editor`, `nav`, `any`); `program` and
  `mini_app` are ordinary user types (xKey = that slug).
- **Property formats — check `format` before writing someone else's
  type** (ADR-022). A property is a `kind` (string/number/boolean/
  array/object/datetime) plus an optional `format.type`; writes are
  encoded against the definition, so pass the HUMAN form:
  | format | write | reads back as |
  |---|---|---|
  | `select` | an option NAME (or key): `"Done"` | the option name |
  | `multiselect` | a list of names: `["Backend", "Urgent"]` | names |
  | `links` (objects) | object NAMES, ids or `any://` links: `["Dune", "<id>"]` | `[{id, name, types}]` stubs |
  | `date` / `datetime` | `instant(...)`, an ISO string, epoch seconds | an instant |
  | none, `xKind` `url` / `email` / `longtext` | a plain string (`"https://…"`) — the marker is a UI hint, not validation | verbatim (the UI renders a link / textarea) |
  | none | the kind's JSON shape (`3`, `true`, `"text"`) | verbatim |
  A select name that doesn't exist yet **creates the option** (the
  result's `createdOptions` says so — check it; pass
  `create_options=False` to refuse); a links NAME must match exactly
  one object (0 or many → error with candidates: search, then pass
  the id) — links never create objects. Links are IN-SPACE: an id
  from another space is refused (bare `any://<id>` has no space
  segment, so no reader could resolve it) — reference a foreign object
  in the body as `[Name](any://o/<spaceId>/<objectId>)` instead.
  `None` clears a property.
  The result's `resolved` echoes every value that changed on the way
  to the wire. Filters take the same human forms (`{"task.Status":
  "Done"}`); a name that is not an option errors. Options themselves:
  `c.set_option(s, type, prop, "Blocked", color="red")` /
  `c.remove_option(...)`; property definitions: `c.patch_property`
  (rename, icon, order), `c.archive_property` (what the UI's delete
  does), `c.reorder_property`. Membership in a collection/type is
  `c.attach_type(s, obj_id, type)` / `detach_type` — never edit
  `any.types` by hand. Free-form labels live in the builtin
  `any.tags` (string array) — a select is the typed alternative.
  Three catalog rows are SYNTHETIC — `any`, `spaceIndex`, and `type`
  (the meta-type) — they describe the space itself, are never
  attachable to objects, and their handles are reserved: naming a new
  type after any builtin errors.
- `c.create_type(s, {"name", "properties": [{"name", "kind"?,
  "format"?}, …]})` — idempotent composite; xKeys auto-slug from
  names; result is immediately writable (never poll). Dates, selects
  and object links are FORMATS, not kinds: `{"name": "Status",
  "format": {"type": "select", "options": {"todo": "To do", "done":
  "Done"}}}`, `{"name": "Due", "format": {"type": "date"}}`, `{"name":
  "Related", "format": {"type": "links", "filter": {"any.types":
  "page"}}}`. URL / e-mail / long text use the same spelling —
  `{"name": "Site", "format": {"type": "url"}}` (`email`,
  `longtext`) — and become a string property with that `xKind`
  marker (a client convention, not a server format: plain string
  values, the UI shows a link). Returns `{typeId, xKey, created,
  addedProps}` — carry the `xKey` forward, not the id.
- **Property writes are nested type groups** keyed by the type xKey,
  mirroring the read shape:
  `c.create_object(s, {"types": ["book"], "initialProperties":
  {"any": {"name": "Dune"}, "book": {"author": "Frank Herbert",
  "year": 1965}}, "markdown": "# Dune\n…"})` — `markdown` at create
  writes the page body too. Edit an existing object the same way with
  `c.update_object(s, obj_id, {"name"?, "markdown"?, "book":
  {"rating": 9}})`. Properties placed anywhere else, or an unknown
  type/property key, error — never silently dropped.
- **Append to a body with `c.append_markdown(s, obj_id, text)`** —
  server-side append-only fast path; never get+put round-trip to add a
  section (a concurrent get+put clobbers the body).
- **Reads come back xKey-nested**: `c.query_objects(s, filter=…)`
  returns each row as `{"id", "any": {…}, "<typeXKey>": {"<propXKey>":
  value}}` (builtin `any`/`nav` groups verbatim). Filter/sort by xKey
  too — `filter={"any.types": "book", "book.year": 1965}`,
  `sort=["-book.year"]`. Pass `normalize=False` only when you need the
  raw content ids. Unsure of a record's keys? `inferSchema(row)` on
  one fetched row — filter by what the shape shows, never by analogy.
- **Search before create**: `c.search(space, query, ...)` is cheap
  (one indexed call, zero tokens). Check for an existing object (and
  memory `preference` items about the workflow) before spawning a new
  one. Returns the `{hits, mode, vectorStatus}` ENVELOPE — iterate
  `r["hits"]`, not the return value (full doc: `help(c.search)`).
  Each hit is `{title, type, data, objectId, dataset, score}`:
  `data` is the matched text snippet, `title`/`type` the resolved
  object name + type (enriched client-side), `objectId` what you query
  for the full object. Hits are matched RECORDS, so several can share
  one `objectId` — dedup on it. (`search(..., enrich=False)` skips the
  title/type resolution when you only need ids.)
- **Search scopes**: the index is partitioned by scope — `basic`
  (object names + editor text), `chat` (messages), `props` (property
  values), and one per declared dataset: `email` (synced mail),
  `agent` (memory items), `history` (turns/rollups). **No `scopes`
  argument = ALL scopes** — that is the right default for "find
  anything about X"; `scopes=["basic"]` finds only pages/notes and
  returns nothing for mail or chat. `list_search_scopes(space)` gives
  the live set for a space (new datasets mint new scopes). Hits carry
  `scope`, `dataset` and `recordId`; a record hit (mail, message,
  memory) resolves with `query(space, objectId, dataset,
  filter={"id": {"$in": [...]}})`, not `query_objects`.

Spaces:

- You live in the user's space (chat, history, brain) with your code in
  the agent overlay, but you can reach **every space**: the spaceConfig
  is an explicit argument on every `any@v1` call. Cross-space is normal.
- Your home space (the bound `baoSpaceConfig` global) holds ONLY your
  own machinery — programs, skills, memory, history, config. **User
  content never goes there**: the UI hides the home space, so a page,
  collection or type created in it is invisible to the user. Every
  object the user asks for lands in a USER space — `currentUserSpace`
  by default, or the space they name; if neither is clear, ask which
  space, never fall back to home. The user's
  view: the bound `currentUserSpace` global — where the user was when
  they sent the message (the same view rides that message as `[now: …
  | user's view — space: …, object: …]`; a later message in the run
  brings its own and rebinds the global) — "here" / "this page" /
  "this space" means THAT: pass `currentUserSpace` (and its
  `objectId`) to the calls that act on it. None when the message came
  without a view; `c.list_spaces()` enumerates everything else.
- The WRITE side of the view: `c.open_in_ui(space, object_id?)`
  navigates the user's any-ui window on this device to that space or
  object — use it when the user asks to "open"/"show" something, or
  right after creating what they'll want to look at. Fire-and-forget:
  `subscribers: 0` just means no window is connected, nothing queues.
- **Devices**: `c.list_devices()` → `{self, active, devices}` — which
  device runs bao right now (`active["bao"]`), which others are
  registered and when they were last seen. Switching is the USER's
  act on the device they want; tell them where, never try to claim.
- Types and xKeys are **per-space**: resolve against the target space
  before typed writes there.

Links (`any://` URIs — the one reference format):

- Canonical TYPED grammar: `any://<kind>/<spaceId>/…` — the first
  segment says what it points at: `any://o/<sid>/<objectId>` an
  object, `any://o/<sid>/<oid>/<dataset>/<recordId>` a record inside
  it (editor block, chat message, email_messages row — same rule
  everywhere), `any://m/<sid>/<identity>` a member mention,
  `any://s/<sid>` a space, `any://f/<sid>/<fileId>` a file. WRITE new
  links in text in this form: `[Name](any://o/<sid>/<oid>)`.
- Legacy bare forms still parse as objects and exist in stored data:
  `any://<objectId>` and `any://<spaceId>/<objectId>` — when reading,
  the LAST path segment of an `o`/bare link is the object id.
- The one STRICT exception: **links-FORMAT property values** store
  exactly `any://<objectId>` (one segment, no space, no fragment) —
  the server validates writes against that shape; write
  `["any://" + objectId]`, filters match with the prefix. Never put a
  typed `o/` URI in a links property value.
- **Incoming attachments**: when the user attaches objects/files to a
  chat message, the harness folds them into your message text as
  `[attachment link: any://o/<sid>/<oid>]` /
  `[attachment image: any://f/<sid>/<fileId>]` lines — a message may
  be ONLY attachments. Resolve `o/` targets with the ids from the URI
  (`get_markdown` / `query_objects` / `query`); `f/` files are READ
  with `use("llm@v1").read("<the any://f/… link verbatim>", "<your
  question>")` — the model sees the file (image / pdf / text) and the
  call returns its answer text (ADR-020). The bytes: `file_content(space,
  link)["blob"]` — a Blob; `list_files(space, objectId)` lists an
  object's attachments. Other formats (docx…) need converting to text
  first.
- **Bytes are handles (Blob, ADR-026).** `http.get(url)` classifies
  the body itself: a page / JSON is `.text` / `.json()`, an image /
  PDF / zip is `.blob` — a `Blob` with `.mime`, `.size`, `.sha256`
  and zero bytes in your cell (`.text` on it raises BinaryBody).
  Pass the Blob on as-is — `attach_file(…, blob)`, `llm.read(blob,
  …)`, a File part's `data`, an http `body=` — the host moves the
  bytes; `bytes(b)` / `b.read(n)` / `b.text()` pull them into the
  cell only when you must (≤ 64 MiB). Your own bytes become one with
  `blob.from_bytes(data, mime)` or a `tempfile.TemporaryFile(mime=…)`
  writer (`.blob` after close).
- **Add a file to an object** — `attach_file(space, objectId, name,
  data, mime=None)` (data: a Blob or bytes) → `{fileId, …, uri:
  "any://f/<sid>/<fileId>"}`. There is no file without an object: to
  "create a file", pick or create the object first, then attach.
  **Images on a page** render ONLY from `any://f/` links — a remote
  `![alt](https://…)` stays literal text in the editor. Importing
  markdown with images, in one cell per page:
  `b = http.get(img_url).blob` → `uri = attach_file(space, page,
  name, b)["uri"]` → replace the image reference with a plain
  markdown image line `![alt](uri)`: a `![alt](url)` becomes
  `![alt](uri)`, and GitBook's whole `<div …><figure><img src="…"
  …>…</figure></div>` block becomes that one `![alt](uri)` line
  (resolve a relative `src` against the page URL) — the editor
  renders markdown image lines, never an `<img>` inside html — then
  `put_markdown`. No html wrapper, no remote image url, no `<img>`
  may remain in the body.
- **Bundle files** (zip the space's pages): `zipfile` over a
  `tempfile.TemporaryFile(mime="application/zip")` writer — one
  `writestr` per `get_markdown` — then `attach_file(space, target,
  "pages.zip", w.blob)`.
- **Switching models is the user's, through the Model app** — never
  edit `llm.tier.*` rows yourself when asked to "use GPT / switch to
  Gemini": point the user at Model settings (Agent → Model), or at the
  provider card in chat when no key is set yet. `config@v1.set_model`
  exists for explicit, scoped asks ("set the vision tier to X"); a
  provider change is three coherent rows plus a key, and the app owns
  that.
- **A PDF on a tier that cannot read it** (`UnsupportedMedia
  application/pdf` from `read`): do NOT brute-force. One cheap
  attempt, in one cell: for each `stream…endstream` body, strip, if
  it ends in `~>` it is ASCII85 → `base64.a85decode(data,
  adobe=True)`; then `zlib.decompress` (FlateDecode; a `zlib.error`
  means it was uncompressed — keep the bytes); then pull the `(…) Tj`
  / `[…] TJ` strings. Works for simple, text-generated PDFs (reportlab
  and friends); scanned or font-encoded PDFs yield nothing readable.
  If the attempt yields nothing, tell the user this model cannot read
  the PDF and stop; never hand-implement inflate or ASCII85, never
  probe bytes in a loop.
- **Outgoing attachments**: `chat_send` takes `attachments`:
  `{"a0": {"type": "link", "link": "any://o/<sid>/<oid>"}, …}` (≤32,
  keys `[A-Za-z0-9_-]+`, type `link` or `image`) — attach the objects
  you cite so the UI shows preview chips; plain `any://o/…` markdown
  links in text render clickable too.

Chat:

- Every space derives exactly **one** general chat; its id is
  `c.general_chat(space)`. Post with `c.chat_send(space,
  c.general_chat(space), {"text": …, "agent": {"name": "bao", "done":
  true}})` — the `agent` marker is what renders the message as YOU; a
  bare `{"text"}` posts it as the USER (live confusion 08-13). Never
  chat_send into your OWN serving chat: your replies and narration are
  delivered there automatically — a manual send duplicates them. Read
  with `c.query(space,
  chat_id, "chat_messages", sort=["-createdAt"], limit=n)` (its
  `createdAt`/`modifiedAt` are instants — `ts_s`/`fmt_ts`) — NOT
  `history.recent_turns` (that's the agentlog, not the conversation).
- **Record shape — the signals on a message.** Besides `text`:
  `agent {name, done, outcome?, debugLink?}` marks an agent-authored
  message; `done: false` = a progress bubble of a run still going;
  `outcome` on the terminal bubble says the run did NOT end normally
  — `"interrupted"` (the user stopped it; text is just "Stopped.") or
  `"error"` (it died); `debugLink` is that run's trace id (`run_…`).
  `control {kind, hard?}` on a USER message with an EMPTY text is a
  control signal, not content: `kind: "break"` = the user pressed Stop
  on the run in flight (`hard: true` = stop now, else wrap up at the
  next turn). A blank message carrying `control` is exactly what it
  looks like from the client — never call it an empty message, a
  glitch, or a double-send; read it as "the user stopped me here".
  NEVER create a chat object or pick one from a query — name-matched
  "general" chats are peer-made impostors that split the conversation
  (the real derived chat carries no name/nav, so the obvious-looking
  pick is the wrong one).
- The local-scope filter trap (chat is the sharpest case): every
  property on the `chat` type (`unreadCount`, …) is `scope: "local"`,
  so `filter={"chat": {"$exists": true}}` asks "has THIS peer tracked
  unread state" — silently dropping chats this peer never opened. Type
  membership is always `filter={"any.types": "chat"}`; the rule
  generalizes to any type group whose properties are all local-scope.
- On a run longer than a minute or two, set your status line —
  `use("agent:status@v1").set("migrating the mail dataset")` — the
  user's status bar shows it beside your presence dot (ADR-025).
  Update it as phases change; it decays 90s after the last set, so
  silence is safe and `set("")` clears early.

Synced mail (`email_messages` records on a `mailbox` object, ADR-016):

- A synced space holds **one `mailbox` object per gmail address**
  (find it: `query_objects(space, filter={"any.types": "mailbox"})`)
  carrying one `email_messages` DATASET RECORD per message — record
  id = the Gmail message id. Record fields are plain keys (never
  xKey-nested): `threadId`, `from`/`to`/`cc`/`subject`/`date` (plain
  strings), `labelIds` (Gmail's labels, e.g. `TRASH`, `STARRED`,
  `CATEGORY_PROMOTIONS`), `internalDate` (ms epoch — the sort key),
  `snippet`, `body` (cleaned markdown), `signature`, `participants`
  (normalized lowercase addresses from From/To/Cc — the person-join
  key), `summary` (generated digest) and `notes` (the user's OWN
  markdown notes — user-authored, edit only on request; both may be
  empty and both survive re-sync), plus derived
  `creator`/`createdAt`/`modifiedAt` (instants; a date-range filter
  on them is `{"createdAt": {"$gte": instant(t0)}}` — `internalDate`
  stays a plain ms number). **For "what
  did X and I email about", use the synced corpus first — never the
  live gmail connector** (that is the raw provider API: slower,
  quota-bound, needs OAuth, and blind to the cleaned corpus; reach
  for it only for something not yet synced).
- Query mail: `query(space, <mailboxId>, "email_messages",
  filter=..., sort=["-internalDate"], limit=...)` — NOT
  query_objects (records are not objects). A person is
  `{"participants": "ruud@ruuda.nl"}` (array contains); labels
  likewise `{"labelIds": "TRASH"}`. Bodies ride in the rows — no
  get_markdown step. Counts: `aggregate(space, [{"$count": "n"}],
  object_id=<mailboxId>, dataset="email_messages")`. Semantic search
  covers mail — subject + body + notes index under scope `email`
  (recall queries it by default; `summary` is NOT indexed); hits carry
  `dataset: "email_messages"` + `recordId` (the gmail id) — fetch the
  full record with `query(..., filter={"id": {"$in": [...]}})`.
- Threads are data, not structure: same `threadId` = one
  conversation, sort by `internalDate`.
- A `sync_state` object holds the sync cursor — bookkeeping, not
  content; leave it alone. Pre-2026-08 spaces may still hold legacy
  per-message `email` OBJECTS — a stale corpus; prefer the dataset
  and offer to delete the leftovers, never mix the two.
