# Skill: _any

**Object-first.** Every task routes through the space first. When the
user says "create", "track", "save", "add", "organize" — assume they
mean an object (page, note, task, a tag, a custom type) unless they
explicitly say otherwise. Objects are the default unit of work.

**The model, in one breath.** Every object IS exactly one **type** —
its class: the type gives it a body and parts (its methods) and its
property fields. page is the default class: a plain document, no
fields, one body. An object is FILED UNDER any number of
**collections** — a collection is a **tag** (an empty one) or a
**supertag** (one with properties: its members carry those columns).
Filing adds columns, never behaviour; unfiling hides them and keeps
the values. A type or a collection is itself an object (a
definition) and is never a member of itself. "Collection" means THIS
and nothing else — never a type; the client's types feature (the
Collections app) is "the types feature" when you speak of it.

Core mechanics (`c = use("agent:any@v1")`; every space-scoped call
takes a spaceConfig first, as the module surface above says):

- Everything in a space is an object of ONE type; types define the
  body and the property fields their objects have; collections add
  columns to whatever is filed under them.
- Discover existing types before creating new ones —
  c.list_types(space) (hidden ones included — the built-in page,
  my own machinery types, the apps' types) and
  c.list_collections(space) (the wiki, an app's role facets such as
  contact, the user's own tags), or c.list_properties(space,
  xkey) for one type's OR collection's property map (`[{handle,
  name, kind, scope, xFormat?, options?}]`, in display order). Types
  and collections are ALWAYS named by xKey (an unknown xKey errors
  with the catalog); never pass or repeat raw content-ids. Find an
  existing fit first; avoid inventing parallel types — and prefer an
  app's definition (list_apps, below) over a private one for
  people, contacts, deals.
- Types and collections are referenced by **xKey** (the stable slug,
  e.g. `"book"`, `"reading_list"`), properties by their **handle**
  (the xKey, else the display name), NEVER the raw content id — the
  client resolves handles to ids under the hood; an ambiguous key
  errors listing the candidates. The built-ins use their id: the
  types page / dataview, the collections miniapp (the sidebar)
  / bin (the trash); program, agent_skill and applet are
  ordinary user types of mine (xKey = that slug). Types and
  collections share ONE handle namespace — a name held on one side
  errors on the other.
- **Property descriptors — check xFormat before writing someone
  else's type.** A property is a kind (string/number/boolean/array/
  object/datetime) plus an optional descriptor xFormat.type (the
  slug); writes are encoded against the definition, so pass the
  HUMAN form:
  | xFormat.type | write | reads back as |
  |---|---|---|
  | choice | an option NAME (or key): `"Done"`; a list when config.multiple | the option names (a list) |
  | relation (objects) | object NAMES, ids or any:// links: `["Dune", "<id>"]`; one unless config.multiple | `[{id, name, type, collections}]` stubs |
  | date / datetime | instant(...), an ISO string, epoch seconds | an instant |
  | text longtext markdown url email phone | a plain string (`"https://…"`) | verbatim |
  | number currency percent rating duration | a number | verbatim |
  | checkbox | true / false | verbatim |
  | none | the kind's JSON shape (`3`, true, `"text"`) | verbatim |
  A choice name that doesn't exist yet **creates the option** (the
  result's createdOptions says so — check it; pass
  `create_options=False` to refuse); a relation NAME must match
  exactly one object among the relation's targetTypes (0 or many →
  error with candidates: search, then pass the id) — relations never
  create objects. Relations are IN-SPACE: an id from another space is
  refused (bare `any://<id>` has no space segment, so no reader could
  resolve it) — reference a foreign object in the body as
  `[Name](any://o/<spaceId>/<objectId>)` instead. None clears a
  property.
  The result's resolved echoes every value that changed on the way
  to the wire. Filters take the same human forms (`{"task.status":
  "Done"}`); a name that is not an option errors. Options themselves:
  `c.set_option(s, type, prop, "Blocked", color="red")` /
  c.remove_option(...); property definitions: c.patch_property
  (rename, icon, the slug within its kind, `xFormat.config.*`),
  c.reorder_property; c.delete_property is permanent — confirm
  first — all of them on a type or a collection alike. Free-form
  labels live in the builtin any.tags (string array) — a collection
  is the shareable tag, a choice the typed alternative. The catalog's
  meta rows (any, spaceIndex, type, collection) describe the
  space itself: no object has one as its type, none lists them, and
  their handles are reserved — naming a new definition after any
  builtin errors.
  So are the record-root keys every object carries bare (id,
  author, createdAt, modifiedAt, modifiedBy, spaceId): a
  type named "Author" keeps its name and takes an explicit xKey
  (author_type) — the xKey is a handle the user never sees.
- `c.create_type(s, {"name", "hidden"?, "properties": [{"name",
  "kind"?, "xFormat"?}, …]})` — idempotent composite; xKeys auto-slug
  from names; result is immediately writable (never poll). Dates,
  choices and object relations are descriptor SLUGS, not kinds (the
  kind follows from the slug): `{"name": "Status", "xFormat":
  {"type": "choice", "options": {"todo": "To do", "done": "Done"}}}`,
  `{"name": "Tags", "xFormat": {"type": "choice", "config":
  {"multiple": true}}}`, `{"name": "Due", "xFormat": {"type":
  "date"}}`, `{"name": "Author", "xFormat": {"type": "relation",
  "relation": {"targetTypes": ["person"]}}}`, `{"name": "Site",
  "xFormat": {"type": "url"}}`. Returns `{typeId, xKey, created,
  addedProps}` — carry the xKey forward, not the id. **Every type
  you mint has a body**: a user type is "page plus fields", so its
  objects hold markdown like a page; an existing type without a body
  gains one the first time create_type runs on it. Minting a type
  also installs the space's Collections app when missing — that is
  what makes types and their objects visible in the client.
- **A tag is `c.create_collection(s, {"name": "Reading list",
  "properties"?: [...]})`** — same drafts as create_type, no body, no
  layout; with properties it is a supertag and its members carry
  those columns. Returns `{collectionId, xKey, created, addedProps}`.
  "Put Dune in the reading list" = `c.add_to_collection(s, obj_id,
  "reading_list")` (idempotent; the type is untouched), "take it off"
  = c.remove_from_collection(...) (the values stay stored). "Turn
  that note into a Book" = `c.set_type(s, obj_id, "book")` — the one
  type is replaced, the old type's values stay as orphans, and there
  is no unset (a plain document is `set_type(..., "page")`). Never
  edit any.type / any.collections by hand.
- **Trash before delete.** "Delete this" is c.trash(s, obj_id) —
  the object goes to the bin (out of every listing, reversible with
  c.restore(s, obj_id)); c.delete_object is permanent and needs
  an explicit ask. Ordinary listings exclude the bin:
  `{"any.collections": {"$nin": ["bin"]}}`.
- **Property writes are nested groups keyed by the OWNER's xKey** —
  the object's type or one of its collections — mirroring the read
  shape: `c.create_object(s, {"type": "book", "collections":
  ["reading_list"], "initialProperties": {"any": {"name": "Dune"},
  "book": {"author": "Frank Herbert", "year": 1965}, "reading_list":
  {"order": 1}}, "markdown": "# Dune\n…"})`. No type = a page
  (a note, a folder, a plain document); markdown at create writes
  the body too — the object's type must have one (every type you
  mint does; a type without one errors naming the fix, nothing is
  retyped). Edit an existing object the same way with
  `c.update_object(s, obj_id, {"name"?, "markdown"?, "book":
  {"rating": 9}, "reading_list": {"order": 2}})`. Properties placed
  anywhere else, or an unknown type/collection/property key, error —
  never silently dropped. There is no types key: an object IS one
  type and is FILED UNDER collections.
- **The Wiki is a collection**: it shows exactly the objects filed
  under the space's wiki collection (its parentId / pos /
  folder columns are the placement); whatever an object IS, unfiled
  it is not in the Wiki. Never file under wiki by hand — the
  placement calls do: `parent=` on create_object (`""` = top level,
  or a folder's object id) puts a new object in the tree; `c.move_
  object(s, obj_id, parent, folder=None)` files or moves an existing
  one. `folder=True` makes a folder — a create with a name, no body:
  `c.create_object(s, {"name": "Recipes"}, parent="", folder=True)`.
  To file a page under a folder, find the folder first with
  `c.list_children(s, "")` (match by name; list_children(s,
  folder_id) walks deeper), create it once if missing, then pass its
  objectId as parent. `c.remove_from_collection(s, obj_id,
  "wiki")` takes an object out of the Wiki without deleting it.
  Without parent an object is outside every tree and still
  reachable by search, links and queries — say where you put it.
- **Append to a body with c.append_markdown(s, obj_id, text)** —
  server-side append-only fast path; never get+put round-trip to add a
  section (a concurrent get+put clobbers the body).
- **Reads come back xKey-nested**: c.query_objects(s, filter=…)
  returns each row as `{"id", "any": {"name", "type": "book",
  "collections": ["reading_list"], …}, "<ownerXKey>": {"<propXKey>":
  value}}` — one group per owner, the type and each collection.
  "Which objects are books" = `filter={"any.type": "book"}` (several:
  `{"$in": [...]}`); "what is on the reading list" = `filter=
  {"any.collections": "reading_list"}` (membership; `$in` / `$nin` /
  `$all`). A definition's own row never matches its members — no
  marker clause. Add `{"any.collections": {"$nin": ["bin"]}}` to keep
  trashed objects out. Columns filter and sort by owner.prop —
  `filter={"any.type": "book", "book.year": 1965}`,
  `sort=["reading_list.order"]`. There is no any.types (it errors —
  the server would answer it with a silent empty list). Pass
  `normalize=False` only when you need the raw content ids. Unsure of
  a record's keys? inferSchema(row) on one fetched row — filter by
  what the shape shows, never by analogy.
- **Search before create**: c.search(space, query, ...) is cheap
  (one indexed call, zero tokens). Check for an existing object (and
  memory preference items about the workflow) before spawning a new
  one. Returns the `{hits, mode, vectorStatus}` ENVELOPE — iterate
  `r["hits"]`, not the return value (full doc: help(c.search)).
  Each hit is `{title, type, data, objectId, dataset, key, score}`:
  data is the matched text snippet, title/type the resolved
  object name + type (enriched client-side), objectId what you query
  for the full object, key the store the record lives in
  (agent_memory_items, email_messages, chat_messages, …). Hits
  are matched RECORDS, so several can share one objectId — dedup on
  it. (search(..., enrich=False) skips the title/type resolution
  when you only need ids.)
- **Search scopes**: the index is partitioned by scope — basic
  (object names + editor text), chat (messages), props (property
  values), and one per declared dataset: email (synced mail),
  agent (memory items), history (turns/rollups). **No scopes
  argument = ALL scopes** — that is the right default for "find
  anything about X"; `scopes=["basic"]` finds only pages/notes and
  returns nothing for mail or chat. list_search_scopes(space) gives
  the live set for a space (new datasets mint new scopes). Hits carry
  scope, key and recordId; a record hit (mail, message,
  memory) resolves with `query(space, objectId, key,
  filter={"id": {"$in": [...]}})`, not query_objects. A dataset is
  always named by its store KEY (`query(space, obj, "email_messages")`)
  — the key resolves against the object's TYPE to the server's
  storage collection; an object whose type declares no such key
  errors with the list. (A "storage collection" is where a dataset's
  records live — the server's address, unrelated to the collections
  objects are filed under.)

Apps — two different things share the word, keep them apart:

- **The server's apps** are the catalog installs a space has — each
  space installs its own set, and the user calls them by name: the
  wiki, collections, the chat, journal, meetings, contacts, a CRM.
  c.list_apps(space) → `[{name, bundleId?, rootId, usecase?,
  description, hidden, pinned}]` — the space's sidebar (objects filed
  under the built-in miniapp collection), the user's own pinned
  objects included. Read it before assuming a space HAS a wiki or
  contacts; the same list rides the runtime context of every
  conversation. c.list_available_apps(space) → the catalog with
  installed per usecase (wiki, collections, people, contact,
  contacts, crm, investor, customer, …). Offer, and on the user's yes
  c.setup_app(space, usecase) installs it (dependencies too,
  idempotent) and returns each definition's ids + xKey→propId map. An
  app brings TYPES (person, organization, deal, journal,
  meeting — what its objects are) and COLLECTIONS (wiki, and the
  role facets contact, investor, customer, partner, vendor,
  cofounder, candidate — what objects are filed under): a contact
  is a person filed under contact — `create_object(s, {"type":
  "person", "collections": ["contact"], ...})`, or `add_to_collection
  (s, person_id, "contact")` for someone you already have; never a
  type named contact.
- **Applets** are the small HTML apps I write for the user
  (`use("agent:applet@v1")` — a page-sized program with its own
  state), nothing to do with the sidebar. "Make me an app" with no
  catalog match means an applet.

Spaces:

- You can reach **every space**: the spaceConfig is an explicit
  argument on every any@v1 call. Cross-space is normal.
- Types and xKeys are **per-space**: resolve against the target space
  before typed writes there.
- Your home space (the bound baoSpaceConfig global) holds ONLY your
  own machinery — programs, skills, memory, history, config. **User
  content never goes there**: the UI hides the home space, so a page,
  collection or type created in it is invisible to the user. Every
  object the user asks for lands in a USER space — currentUserSpace
  by default, or the space they name; if neither is clear, ask which
  space, never fall back to home.
- The WRITE side of the view: c.open_in_ui(space, object_id?)
  navigates the user's any-ui window on this device to that space or
  object — use it when the user asks to "open"/"show" something, or
  right after creating what they'll want to look at. Fire-and-forget:
  `subscribers: 0` just means no window is connected, nothing queues.
- **Devices**: c.list_devices() → `{self, active, devices}`; each
  row carries name, os, self (the device THIS run executes on),
  active (holds the bao claim — the device that answers chat) and
  bao (has run bao). "Where are you running?", "make this device main", "why did I get two replies?": name the active
  device, list the others that run bao, and point to the switch —
  Settings ▸ Agent ▸ Devices ▸ "Use this device", clicked ON the
  device they want (the dot on bao's face says where chat is
  answered: this device / another / nowhere). A device can only
  claim for itself, so never try to claim from here and never
  promise a switch you have not seen in active. What to expect,
  said plainly: the new device takes over within ~10 s; switching
  back and forth within seconds can get one message answered twice
  (each device notices on its own clock) — that is the hand-off
  window, not a fault in the message.

Links (any:// URIs — the one reference format):

- Canonical TYPED grammar: `any://<kind>/<spaceId>/…` — the first
  segment says what it points at: `any://o/<sid>/<objectId>` an
  object, `any://o/<sid>/<oid>/<dataset>/<recordId>` a record inside
  it (editor block, chat message, email_messages row — same rule
  everywhere), `any://m/<sid>/<identity>` a member mention,
  `any://s/<sid>` a space, `any://f/<sid>/<fileId>` a file. WRITE new
  links in text in this form: `[Name](any://o/<sid>/<oid>)`.
- **The space segment of a link is the space ID, never a name.**
  Names work in every space ARGUMENT (`put_markdown("ta", …)`) and
  nowhere inside a URI: `any://f/ta/<fileId>` is a dead link — the
  server answers 404 and the UI shows a broken chip. Take ids from
  `currentUserSpace["spaceId"]`, `get_space("ta")["id"]` or the rows
  you queried, and paste `attach_file(...)["uri"]` verbatim — a
  hand-built file link is always wrong. put/append/edit_markdown
  and chat_send report a mis-shaped link (name or missing space
  segment) under warnings and as a warning: line; they never
  rewrite it — fix the text and write again.
- Legacy bare forms still parse as objects and exist in stored data:
  `any://<objectId>` and `any://<spaceId>/<objectId>` — when reading,
  the LAST path segment of an o/bare link is the object id.
- The one STRICT exception: **relation property values** store
  exactly `any://<objectId>` (one segment, no space, no fragment) —
  the server validates writes against that shape; write
  `["any://" + objectId]`, filters match with the prefix. Never put a
  typed o/ URI in a relation value. What links to an object:
  c.backlinks(space, obj_id) → `{object: [edge], parts: [edge]}`
  (edges from blocks, messages and relation values — kind, the
  source objectId, prop "type.prop" or key/recordId);
  c.links(space, obj_id) the other direction.
- **Incoming attachments**: when the user attaches objects/files to a
  chat message, the harness folds them into your message text as
  `[attachment link: any://o/<sid>/<oid>]` /
  `[attachment image: any://f/<sid>/<fileId>]` lines — a message may
  be ONLY attachments. Resolve o/ targets with the ids from the URI
  (get_markdown / query_objects / query); f/ files are READ
  with `use("llm@v1").read("<the any://f/… link verbatim>", "<your
  question>")` — the model sees the file (image / pdf / text) and the
  call returns its answer text (ADR-020). The bytes: `file_content(space,
  link)["blob"]` — a Blob; list_files(space, objectId) lists an
  object's attachments. Other formats (docx…) need converting to text
  first.
- **Bytes are handles (Blob, ADR-026).** http.get(url) classifies
  the body itself: a page / JSON is `.text` / .json(), an image /
  PDF / zip is `.blob` — a Blob with `.mime`, `.size`, `.sha256`
  and zero bytes in your cell (`.text` on it raises BinaryBody).
  Pass the Blob on as-is — attach_file(…, blob), llm.read(blob,
  …), a File part's data, an http `body=` — the host moves the
  bytes; bytes(b) / b.read(n) / b.text() pull them into the
  cell only when you must (≤ 64 MiB). Your own bytes become one with
  blob.from_bytes(data, mime) or a tempfile.TemporaryFile(mime=…)
  writer (`.blob` after close).
- **Add a file to an object** — attach_file(space, objectId, name,
  data, mime=None) (data: a Blob or bytes) → `{fileId, …, uri:
  "any://f/<sid>/<fileId>"}` — uri IS the file link: `[name](uri)`
  in markdown or chat text is the download, `![alt](uri)` an image.
  There is no file without an object: to "create a file", pick or
  create the object first, then attach.
  **Images on a page** render ONLY from any://f/ links — a remote
  `![alt](https://…)` stays literal text in the editor. Importing
  markdown with images, in one cell per page:
  `b = http.get(img_url).blob` → `uri = attach_file(space, page,
  name, b)["uri"]` → replace the image reference with a plain
  markdown image line `![alt](uri)`: a `![alt](url)` becomes
  `![alt](uri)`, and GitBook's whole `<div …><figure><img src="…"
  …>…</figure></div>` block becomes that one `![alt](uri)` line
  (resolve a relative src against the page URL) — the editor
  renders markdown image lines, never an `<img>` inside html — then
  put_markdown. No html wrapper, no remote image url, no `<img>`
  may remain in the body.
- **Bundle files** (zip the space's pages): zipfile over a
  `tempfile.TemporaryFile(mime="application/zip")` writer — one
  writestr per get_markdown — then `attach_file(space, target,
  "pages.zip", w.blob)`.
- **Switching models is the user's, through the Model app** — never
  edit `llm.tier.*` rows yourself when asked to "use GPT / switch to
  Gemini": point the user at Model settings (Agent → Model), or at the
  provider card in chat when no key is set yet. config@v1.set_model
  exists for explicit, scoped asks ("set the vision tier to X"); a
  provider change is three coherent rows plus a key, and the app owns
  that.
- **A PDF on a tier that cannot read it** (`UnsupportedMedia
  application/pdf` from read): do NOT brute-force. One cheap
  attempt, in one cell: for each `stream…endstream` body, strip, if
  it ends in `~>` it is ASCII85 → base64.a85decode(data,
  adobe=True); then zlib.decompress (FlateDecode; a zlib.error
  means it was uncompressed — keep the bytes); then pull the `(…) Tj`
  / `[…] TJ` strings. Works for simple, text-generated PDFs (reportlab
  and friends); scanned or font-encoded PDFs yield nothing readable.
  If the attempt yields nothing, tell the user this model cannot read
  the PDF and stop; never hand-implement inflate or ASCII85, never
  probe bytes in a loop.
- **Outgoing attachments**: chat_send takes attachments:
  `{"a0": {"type": "link", "link": "any://o/<sid>/<oid>"}, …}` (≤32,
  keys `[A-Za-z0-9_-]+`, type link or image) — attach the objects
  you cite so the UI shows preview chips; plain `any://o/…` markdown
  links in text render clickable too. Every `[Name](any://…)` link in
  your final REPLY becomes a chip automatically — a link with a space
  name in it becomes a broken chip nobody can open.

Chat:

- Every space has exactly **one** general chat — the server's, in the
  sidebar like every app; its id is c.general_chat(space). Post
  with `c.chat_send(space,
  c.general_chat(space), {"text": …, "agent": {"name": "bao", "done":
  true}})` — the agent marker is what renders the message as YOU; a
  bare `{"text"}` posts it as the USER (live confusion 08-13). Never
  chat_send into your OWN serving chat: your replies and narration are
  delivered there automatically — a manual send duplicates them. Read
  with `c.query(space,
  chat_id, "chat_messages", sort=["-createdAt"], limit=n)` (its
  createdAt/modifiedAt are instants — ts_s/fmt_ts) — NOT
  history.recent_turns (that's the agentlog, not the conversation).
- **Record shape — the signals on a message.** Besides text:
  `agent {name, done, outcome?, debugLink?}` marks an agent-authored
  message; `done: false` = a progress bubble of a run still going;
  outcome on the terminal bubble says the run did NOT end normally
  — `"interrupted"` (the user stopped it; text is just "Stopped.") or
  `"error"` (it died); debugLink is that run's trace id (`run_…`).
  `control {kind, hard?}` on a USER message with an EMPTY text is a
  control signal, not content: `kind: "break"` = the user pressed Stop
  on the run in flight (`hard: true` = stop now, else wrap up at the
  next turn). A blank message carrying control is exactly what it
  looks like from the client — never call it an empty message, a
  glitch, or a double-send; read it as "the user stopped me here".
  NEVER create a chat object or pick one from a query — the chat
  module is reserved to the server's one chat, and a name-matched
  "General" object is something else.
- The local-scope filter trap: a type group whose properties are all
  `scope: "local"` (per-peer state such as read tracking) makes
  `filter={"<type>": {"$exists": true}}` ask "has THIS peer tracked
  state" — silently dropping objects this peer never opened. "Objects
  of a type" is always `filter={"any.type": "<xKey>"}`.
