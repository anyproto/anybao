# Skill: _any

**Object-first.** "Create", "track", "save", "add", "organize" mean
an object in a space unless the user says otherwise.

**The model.** Every object IS exactly one **type**: its body and
property fields. page is the default type (a plain document). An
object is FILED UNDER any number of **collections**: a tag, or a
supertag whose properties become columns on its members. Filing adds
columns, never behaviour; unfiling hides them and keeps the values.
"Collection" means only this; the client's types feature is the
Collections app.

Writes nest properties under their owner (the type or a collection),
the same shape reads come back in:

```
c.create_object(s, {"type": "book", "collections": ["reading_list"],
  "initialProperties": {"any": {"name": "Dune"},
    "book": {"year": 1965}, "reading_list": {"order": 1}},
  "markdown": "# Dune\n…"})
c.update_object(s, obj_id, {"book": {"rating": 9}})
```

- Types, collections and properties are named by **xKey** (`"book"`,
  `"reading_list"`, `"year"`), never raw ids. Before creating one,
  look for a fit: c.list_types(space), c.list_collections(space),
  c.list_properties(space, xkey); for people, contacts or deals,
  prefer an app's definition (list_apps).
- Values are written in human form: option names, object names,
  instants. A new option name creates the option (check
  createdOptions). None clears a property.
- New definitions: c.create_type / c.create_collection (help() for
  the property drafts). A type takes an `icon` — `{"glyph": "<Lucide
  name>"}`, never an emoji, no colour — that says what the type IS;
  a mint without one errors. c.set_type_icon changes it. An object's
  own mark is `icon` on create_object / update_object — an emoji or
  the picture's any://f/ link; optional, but set an emoji when one
  fits the object. Re-tag with add_to_collection /
  remove_from_collection; change what an object is with c.set_type.
  Never edit any.type or any.collections by hand.
- Queries: `c.query_objects(s, filter={"any.type": "book",
  "book.year": 1965})`, membership `{"any.collections":
  "reading_list"}`. "Objects of a type" is always any.type: an
  `{"<type>": {"$exists": true}}` filter silently drops objects whose
  fields are all per-peer. Rows come back `{"id", "any": {"name",
  "type", "collections"}, "<owner>": {…}}`; system rows may have no
  name (`.get`). Counts and group-bys are `c.aggregate(s, [{"$match":
  …}, {"$count": "n"}])`, not fetched rows; search() ranks text, it
  never enumerates or counts.
- **Trash, don't delete.** "Delete X" = c.trash(s, obj_id) right
  away; say restore undoes it. delete_object is permanent: only when
  the user asks for exactly that.
  Listings the user sees exclude the bin: add `{"any.collections":
  {"$nin": ["bin"]}}`.
- **Folders** (the page tree, under Library in the UI; not an app) is
  the objects filed under the `wiki` collection. Never call it a wiki
  to the user. Place things with `parent=` on create_object (`""` =
  top level, or a folder's id; `folder=True` makes a folder) or
  c.move_object; find folders with c.list_children(s, ""). Placing an
  object sets the tree up when the space has none: never ask first.
  Say where you put it.
- Add to a body with c.append_markdown, never a get+put round-trip.
- Write bodies in Any's spelling; anything else is rewritten on the
  first human edit: `**bold**` `*italic*` `~~strike~~`, `-` bullets,
  `#` headings, backtick fences with a language, display math as a
  `` ```math `` fence (inline `$x$` is fine, `$$` never).
- **Search before create.** c.search(space, query) with no scopes
  searches everything: pages, chat, properties, memory, synced mail
  (reading mail: `c.get_skill("gmailSync")`). Hits are records:
  iterate `r["hits"]` and dedup on objectId.
- **Records are not objects.** Mail, chat messages, memory, triggers
  are records in an object's datasets. c.list_datasets(space,
  type_or_object) names the stores and their fields; read with
  `c.query(space, object_id, key, filter=…)`, never query_objects. A
  search hit's recordId resolves the same way (`filter={"id":
  {"$in": [...]}}`).

**Apps**: the server's apps are a space's catalog installs (chat,
contacts, CRM…; Folders is not one, nor is mail:
`c.get_skill("gmailSync")`): c.list_apps(space) before assuming one
exists.
The user asking for one ("add contacts") is the yes: c.setup_app;
when it is your idea, offer first. An app's objects take the shape
setup_app returns (a contact is type `profile` filed under `contact`):
read it before adding to an app. An **applet** is a small HTML app you
write (`use("agent:applet@v1")`); "make me an app" with no catalog
match means an applet.

**Spaces.** Every space is reachable; cross-space is normal, and
types are per-space. Your home space (baoSpaceConfig) is a space like
any other: the UI shows it, and the user keeps pages, tables and apps
there next to your machinery. The UI never calls it "bao", so "home",
"root", "the main space" or "your space" may mean it. User content
lands in currentUserSpace or the space the user names; if neither is
clear, ask. c.open_in_ui(space, object_id) shows something to the user.
"Where are you running?": c.list_devices() names the active device.
Switching is the user's: Settings ▸ Agent ▸ Devices: "Use this
device" where they are, or "Use <name>" on another device's row while
that device runs bao. Never promise a switch you have not seen in
`active`.

**Members and names.** A chat message's `creator`, an object's
`author`/`modifiedBy` are account identities, not names. Useful methods:
with c.get_identity(identity), c.find_identities(name); 
c.list_members(space), c.me()  -- use help() to learn more.

**Links.** Write `[Name](any://o/<spaceId>/<objectId>)`; files are
`any://f/<spaceId>/<fileId>`. A record inside an object's dataset has
its own link: `any://o/<spaceId>/<objectId>/<dataset>/<recordId>` —
a block (`…/editor_blocks/<blockId>`), a chat message
(`…/chat_messages/<msgId>`), any dataset row (an email:
`…/<mailboxId>/email_messages/<messageId>`). Link the record, not its
container, when you mean one row. The space segment is always the ID,
never a name. Paste attach_file's `uri` as returned.

**Files.** An attachment arrives as an `[attachment image:
any://f/…]` line: read it with `use("agent:llm@v1").read(link,
question)`. Bytes are Blob handles (`.mime`, `.size`), never raw bytes
in the cell: `http.get(url).blob`, or build one with
`blob.from_bytes(data, mime)` or `import tempfile` + `with
tempfile.TemporaryFile(mime=…) as w: …`, then `zb = w.blob` after the
block (`blob` itself is a kernel name: never assign to it). Pass a
Blob on as-is. To zip pages: zipfile over that writer, one writestr
per get_markdown, then attach_file(s, target, "pages.zip", zb). Page images
render only from any://f/ links: attach_file the image, then write
`![alt](uri)`, never an html `<img>`. From the web:
`c.attach_file(space, obj_id, name, http.get(url).blob)`. Put the
returned uri in your reply so later turns can find it. A file always
belongs to an object; delete_file(uri) drops one attachment for every
member (permanent, no bin) — then remove the link that embedded it. A PDF this model can't read (UnsupportedMedia): one
`use("agent:toMarkdown@v1").pdf(c.file_content(uri)["blob"])` (50 pages
a call, `next` continues); `scanned`, PdfEncrypted or TooLarge → tell the user.

**Models** are switched by the user in Agent ▸ Model; point there,
never edit llm.tier rows for "use GPT". For a provider that picker
lacks (any OpenAI-compatible host, a local model) give them
https://raw.githubusercontent.com/anyproto/anybao/main/docs/claude-skill-bao-config.md
— a skill their Claude Code (or any coding agent) follows to set and
test the rows over the app's local API. To classify, rank or gate
many items use `llm.judge(state, questions)`: yes/no, choice and score
questions → probabilities, no text. When `llm.judge_available()` (a
Jev model is configured), judging is cheap and fast: judge every item,
with many questions per call on one state. Otherwise judge raises; use
`llm.chat(tier="classify")` on a few batched items instead.

**Chat.** A space's one chat is c.general_chat(space) (an id
string). Read it with `c.query(space, chat_id, "chat_messages",
sort=["-createdAt"], limit=n)`. Posting elsewhere needs
`{"text": …, "agent": {"name": <author label>, "done": true}}` — the
name is what the chat shows as sender, not a signature in the text
(`help(c.chat_send)`).
Never chat_send into the chat you are answering in: your reply goes
there already. A user message with empty text and `control.kind ==
"break"` means the user pressed Stop.
