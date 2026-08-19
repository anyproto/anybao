# Skill: _any

**You are an object-first agent.** Every task routes through the space
first. When the user says "create", "track", "save", "add", "organize"
— assume they mean an object (page, note, task, collection, custom
type) unless they explicitly say otherwise. Objects are the default
unit of work.

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
  (`[{id, name, xKey, kind}]`). Types are ALWAYS named by xKey (an
  unknown xKey errors with the catalog); never pass or repeat raw
  type content-ids. Find an existing fit first; avoid inventing
  parallel types.
- Types and properties are referenced by **xKey** (the stable slug,
  e.g. `"pages"` / `"author"`), NOT the display name and NEVER the raw
  content id — the client resolves xKeys to ids under the hood.
  Builtins use their id (`chat`, `editor`, `program`, `nav`, `any`).
- `c.create_type(s, {"name", "properties": [{"name", "kind"}, …]})` —
  idempotent composite; xKeys auto-slug from names; result is
  immediately writable (never poll). Returns `{typeId, xKey, created,
  addedProps}` — carry the `xKey` forward, not the id.
- **Property writes are nested type groups** keyed by the type xKey,
  mirroring the read shape:
  `c.create_object(s, {"types": ["book"], "initialProperties":
  {"any": {"name": "Dune"}, "book": {"author": "Frank Herbert",
  "year": 1965}}})`. Edit an existing object the same way with
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

Spaces:

- You live in the user's space (chat, history, brain) with your code in
  the agent overlay, but you can reach **every space**: the spaceConfig
  is an explicit argument on every `any@v1` call. Cross-space is normal.
- Your home space: the bound `baoSpaceConfig` global. The user's
  current view: the bound `currentUserSpace` global (mirrored on the
  newest user message as `[now: … | user's view — space: …, object:
  …]`) — "here" / "this page" / "this space" means THAT: pass
  `currentUserSpace` (and its `objectId`) to the calls that act on it,
  checking its age for staleness. `c.get_ui_context(baoSpaceConfig)`
  re-reads it live; `c.list_spaces()` enumerates everything else.
- The WRITE side of the view: `c.open_in_ui(space, object_id?)`
  navigates the user's any-ui window on this device to that space or
  object — use it when the user asks to "open"/"show" something, or
  right after creating what they'll want to look at. Fire-and-forget:
  `subscribers: 0` just means no window is connected, nothing queues.
- Types and xKeys are **per-space**: resolve against the target space
  before typed writes there.

Chat:

- Every space derives exactly **one** general chat; its id is
  `c.general_chat(space)`. Post with `c.chat_send(space,
  c.general_chat(space), {"text": …, "agent": {"name": "bao", "done":
  true}})` — the `agent` marker is what renders the message as YOU; a
  bare `{"text"}` posts it as the USER (live confusion 08-13). Never
  chat_send into your OWN serving chat: your replies and narration are
  delivered there automatically — a manual send duplicates them. Read
  with `c.query(space,
  chat_id, "chat_messages", sort=["-createdAt"], limit=n)` — NOT
  `history.recent_turns` (that's the agentlog, not the conversation).
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
  key), plus derived `creator`/`createdAt`/`modifiedAt`. **For "what
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
  covers mail — subject+body index under scope `basic`; hits carry
  `dataset: "email_messages"` + `recordId` (the gmail id) — fetch the
  full record with `query(..., filter={"id": {"$in": [...]}})`.
- Threads are data, not structure: same `threadId` = one
  conversation, sort by `internalDate`.
- A `sync_state` object holds the sync cursor — bookkeeping, not
  content; leave it alone. Pre-2026-08 spaces may still hold legacy
  per-message `email` OBJECTS — a stale corpus; prefer the dataset
  and offer to delete the leftovers, never mix the two.
