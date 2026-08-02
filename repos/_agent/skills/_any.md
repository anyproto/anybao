# Skill: _any

**You are an object-first agent.** Every task routes through the space
first. When the user says "create", "track", "save", "add", "organize"
— assume they mean an object (page, note, task, collection, custom
type) unless they explicitly say otherwise. Objects are the default
unit of work.

Core mechanics (get the client once: `c = use("agent:any@v1").client()`
— in cell code always qualify harness modules with the `agent:` overlay
alias; unqualified `use("any@v1")` resolves only in your working space
and fails in the standard overlay setup, ADR-004 §2):

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
  raw content ids.
- **Search before create**: `c.search(space, query, ...)` is cheap
  (one indexed call, zero tokens). Check for an existing object (and
  memory `preference` items about the workflow) before spawning a new
  one. Each hit is `{title, type, data, objectId, dataset, score}`:
  `data` is the matched text snippet, `title`/`type` the resolved
  object name + type (enriched client-side), `objectId` what you query
  for the full object. Hits are matched RECORDS, so several can share
  one `objectId` — dedup on it. (`search(..., enrich=False)` skips the
  title/type resolution when you only need ids.)

Spaces:

- You live in the user's space (chat, history, brain) with your code in
  the agent overlay, but you can reach **every space**: `space` is an
  explicit argument on every `any@v1` client call. Cross-space is normal.
- Your home space id is in the **Runtime context** section; the user's
  current view arrives on the newest user message
  (`[now: … | user's view — space: …, object: …]`) — "here" / "this
  page" / "this space" means the view context: pass its space id (and
  object id) to the calls that act on it, checking the line's age for
  staleness. `c.get_ui_context(space)` re-reads it live;
  `c.list_spaces()` enumerates everything else.
- Types and xKeys are **per-space**: resolve against the target space
  before typed writes there.

Chat:

- Every space derives exactly **one** general chat; its id is
  `c.general_chat(space)`. Post with `c.chat_send(space,
  c.general_chat(space), {"text": …})`. NEVER create a chat object or
  pick one from a query — name-matched "general" chats are peer-made
  impostors that split the conversation (the real derived chat carries
  no name/nav, so the obvious-looking pick is the wrong one).
- The local-scope filter trap (chat is the sharpest case): every
  property on the `chat` type (`unreadCount`, …) is `scope: "local"`,
  so `filter={"chat": {"$exists": true}}` asks "has THIS peer tracked
  unread state" — silently dropping chats this peer never opened. Type
  membership is always `filter={"any.types": "chat"}`; the rule
  generalizes to any type group whose properties are all local-scope.

Collections vs views:

- **Collections** are static folders — membership is manual.
- **Views/queries** are dynamic lenses over a type. The API cannot
  create or configure custom views today; every type gets a default
  view in the UI. If the user asks for "a filtered view of X", either
  propose a static collection or say the view must be configured in the
  UI — don't pretend the API covers it.
