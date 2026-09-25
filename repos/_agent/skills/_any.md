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
  the property drafts). Re-tag with add_to_collection /
  remove_from_collection; change what an object is with c.set_type.
  Never edit any.type or any.collections by hand.
- Queries: `c.query_objects(s, filter={"any.type": "book",
  "book.year": 1965})`, membership `{"any.collections":
  "reading_list"}`. "Objects of a type" is always any.type: an
  `{"<type>": {"$exists": true}}` filter silently drops objects whose
  fields are all per-peer.
- **Trash, don't delete.** "Delete X" = c.trash(s, obj_id) right
  away; say restore undoes it. delete_object is permanent: only when
  the user asks for exactly that.
  Listings the user sees exclude the bin: add `{"any.collections":
  {"$nin": ["bin"]}}`.
- **The Wiki** is the objects filed under the wiki collection. Place
  things with `parent=` on create_object (`""` = top level, or a
  folder's id; `folder=True` makes a folder) or c.move_object; find
  folders with c.list_children(s, ""). Placing an object installs the
  wiki when the space has none: never ask first. Say where you put it.
- Add to a body with c.append_markdown, never a get+put round-trip.
- **Search before create.** c.search(space, query) with no scopes
  searches everything: pages, chat, properties, memory, synced mail
  (reading mail: `c.get_skill("gmailSync")`). Hits are records:
  iterate `r["hits"]` and dedup on objectId.

**Apps**: the server's apps are a space's catalog installs (wiki,
chat, contacts, CRM…): c.list_apps(space) before assuming one exists.
The user asking for one ("add contacts") is the yes: c.setup_app;
when it is your idea, offer first. An **applet** is a small HTML app you
write (`use("agent:applet@v1")`); "make me an app" with no catalog
match means an applet.

**Spaces.** Every space is reachable; cross-space is normal, and
types are per-space. Your home space holds only your machinery and is
hidden in the UI: user content never goes there. It lands in
currentUserSpace or the space the user names; if neither is clear,
ask. c.open_in_ui(space, object_id) shows something to the user.
"Where are you running?": c.list_devices() names the active device.
Switching is the user's: Settings ▸ Agent ▸ Devices ▸ "Use this
device", clicked on the device they want.

**Links.** Write `[Name](any://o/<spaceId>/<objectId>)`; files are
`any://f/<spaceId>/<fileId>`. The space segment is always the ID,
never a name. Paste attach_file's `uri` as returned.

**Files.** An attachment arrives as an `[attachment image:
any://f/…]` line: read it with `use("llm@v1").read(link, question)`.
Binary bodies are Blob handles: pass them on as-is. Page images
render only from any://f/ links: attach_file the image, then write
`![alt](uri)`, never an html `<img>`. From the web:
`c.attach_file(space, obj_id, name, http.get(url).blob)`. Put the
returned uri in your reply so later turns can find it. A file always
belongs to an object. A PDF this model can't read (UnsupportedMedia): at most one
text-extraction attempt, then tell the user.

**Models** are switched by the user in Agent ▸ Model; point there,
never edit llm.tier rows for "use GPT".

**Chat.** A space's one chat is c.general_chat(space) (an id
string). Read it with `c.query(space, chat_id, "chat_messages",
sort=["-createdAt"], limit=n)`. Posting as
yourself needs the agent marker: `{"text": …, "agent": {"name":
"bao", "done": true}}`; without it the message shows as the user's.
Never chat_send into the chat you are answering in: your reply goes
there already. A user message with empty text and `control.kind ==
"break"` means the user pressed Stop.
