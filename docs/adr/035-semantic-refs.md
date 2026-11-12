# ADR-035: Semantic refs — names for the ids a conversation touched

Status: **Accepted** (2026-11-12; proposed 2026-09-25, revised
2026-10-16, 2026-11-03 and 2026-11-04 after the community review,
2026-11-10 after the PR review, 2026-11-11 after the community chat
review and a replay on real windows, 2026-11-12 the collision rule
stated as a set lookup, with its measured cost, and the link kinds of
the server's registry)
Date: 2026-09-25
Builds on: ADR-001 §4 (span records: `input` on begin, `output` on
end), ADR-002 (the effect boundary: `http.post` is the one effect
under a model call), ADR-005 §1 (the neutral message model of
`llm@v1`), ADR-005 §1.4/§4/§5 (the boot-end cache breakpoint, digest,
boot window, message suffix), ADR-006 §2 (the anchored boot window),
ADR-010 §8 (bound cell globals), ADR-023 §3 (`effects.query` over
the trace records), ADR-023 §6 (retention), ADR-028 (a mock matches
an effect by name and input key)
Amends: ADR-005 §1 (`llm@v1 chat` gains the wall of §5:
every `any://` link in the text it sends is rendered as a handle, and
every handle in the reply is expanded back), ADR-005 §5 (the message
suffix gains a refs line; the final reply gains chips for what the
run changed), ADR-010 §8 (a `getRef` global beside `baoSpaceConfig`)
Tracks: Linear BOB-179 (from BOB-160 bullet 3, "automap ids to
semantic meanings"); evidence in `docs/bob-160-probe-results.md`
(rounds 1 and 2), the boot-window calibration of 2026-10-16 and the
window replay of 2026-11-11

## Context

The kernel lives for one message. Everything a run learned (the id
of the page it just created, the file it attached, the folder it
found) dies with it. The next message starts from the boot window,
which carries each past turn as its user text, a compact time + view
stamp, a `[ran: <effect>×<n> · run_<id>]` footer and its replies
(`history@v1 render_boot_window`, ADR-005 §5). The footer points at
the run's effects, which hold every id the turn touched, but reading
them back is a cell per lookup. An id survives in the prompt only if
the model happened to write it into a reply.

Ids are also the most expensive text in the window. The 2026-10-16
calibration (`count_tokens` over 186 real turns, claude-sonnet-5)
measured 2.5 chars/token for the window as a whole and 1.6 for the
`[now: … | user's view — space: <id>, object: <id>]` stamps: a
64-character id is ~40 tokens, and a typed `any://o/` link ~45. The
compact stamps (ADR-005 §5) now repeat a view only when it changes;
the ids themselves are still there, in every reply that carries a
link and in every call that passes one.

The probe sweeps measured what that costs:

- **Re-lookups.** In round 2, S1 opened with `list_spaces` just to
  find Garden's id in 6 of 7 runs; 3.3 re-searched Dune because 3.2's
  reply said only "Added."; 3.4, 8.2 and 10.3 re-queried ids already
  on screen. One to three turns each, every series.
- **Lost links.** 4.1 attached an image and replied with the page
  link only; 4.2 ("what's in that image?") had no file link, built a
  wrong one, then re-read the page body to find it. `_core` says
  "put the returned uri in your reply"; round 2 shows the model does
  not reliably follow it.
- **Copy cost and copy errors.** A typed link is ~130 characters, a
  bare object id 60: ~45 Claude tokens each time it is written, read
  back or pasted into a call. Weaker models mis-copy them; a single
  wrong character is a 404 or, worse, a write to another object.

Prompt rules were tried first (round 2: "reuse ids already in this
conversation", "put the uri in your reply", "a space name works as
is"). They helped the strong model somewhat and are the part of the
prompt weaker models drop first. The loop already turns links the
model *wrote* into chips (`_auto_attachments`); it has no memory of
what the model *did*.

The first draft of this ADR keyed the index by the object's name.
The 2026-11-03 review of that draft made two points that this
revision adopts: a name is not a stable key — it can change inside
one context window, and two objects can share one — and a short,
typed, link-shaped handle is what a model copies without error and
what a tokenizer encodes in a handful of tokens (`any://task/132` is
5 tokens for 14 characters, against ~40 for a bare object id). The
review proposed integers from 1; the Consequences explain why the
handle is a slice of the id instead. The 2026-11-04 PR review added
two points this revision adopts: a handle and a uri are two spellings
of one thing, so what keeps the short one out of the chat is a
conversion at the wall, not the scheme; and a key the model has seen
must never be reshaped under it.

The 2026-11-11 review and replay changed three things:

- **The short spelling belongs at the external boundary.** The loop,
  the kernel, the cells, the digests and the store should work with
  one id form; only the text on the wire to the provider needs the
  short one. The 11-10 revision put the wall in the loop, so the
  loop's own records held handles beside the uris of the effects
  below them.
- **Mint order decides cache stability, key length does not.** A
  replay of 354 real boot windows (543 turns of one chat, the real
  `history@v1` render) rewrote the ids under nine key rules. Every
  rule that gives keys from left to right, where the first id seen
  keeps its key, kept all 342 extending windows byte-stable — at one
  character, at six, and with a counter. The one rule that broke the
  cache (40 of 342 runs, 56% of the window lost each time) was
  "shortest prefix unique among all ids of the prompt", where a new
  id reshapes a key in an old turn. So a key does not have to be a
  pure function of the id; it has to depend on nothing after it.
- **The wire is already in the trace.** `llm.chat` is a span of the
  guest program `llm@v1`; under it the request goes out as one
  `http.post` effect whose record holds the provider body and the raw
  reply (ADR-002). A wall between the span boundary and that
  effect puts the long spelling in one record and the short one in
  the other, with nothing new stored.

The same replay measured the size of the prize on the largest window
(63 id occurrences, 31 objects; Qwen3 and `o200k` tokenizers, no
Claude count): 19,249 tokens with no wall, 15,201 with six-character
keys, 15,083 with three, 15,027 with keys from one character. The wall
removes about 21% of the window; the key length moves that by about
1%.

What the model did is already on record. Every write a run made is a
span in its trace: the begin record carries the call's `input`
(parameter-keyed, ADR-010 §8), the end record its `output`
(ADR-001 §4), and `effects.query` (ADR-023 §3) reads those records
with the same pipeline language the model uses over datasets. The
window names every past run by its `traceRef`. The index this ADR
wants is a projection of records that exist; nothing has to be
stored to have it.

## Decision

Two parts, one in the loop and one in `llm@v1`.

At run start the loop derives a small index of what the conversation
touched — the ids from the trace records of the window's runs, the
names from one read of those ids — binds it in the kernel behind one
resolver, and lists it on the current message as ordinary links
(§2–§4). The loop, the kernel and the store hold uris and nothing
else.

`llm@v1 chat` is the wall (§5). On the way to the provider it renders
every `any://` link as a **handle**, a short, typed, link-shaped
token; on the way back it expands every handle to its link. The table
between the two lives for that one call and is made from the call's
own text, left to right, so the same text always gives the same
handles and nothing is stored to mint them. The model lives in a
world of handles; everything else lives in a world of uris. any@v1
does not change.

### 1. A ref and its handle

```
ref:object/gyu#book  {"kind": "object", "type": "book", "name": "Dune", "spaceId": "...",
                      "objectId": "...", "uri": "any://o/<spaceId>/<objectId>"}
ref:file/3V2         {"kind": "file", "name": "demo.png", "spaceId": "...", "fileId": "...",
                      "on": "<objectId>", "uri": "any://f/<spaceId>/<fileId>"}
ref:space/a3n        {"kind": "space", "name": "Garden", "spaceId": "..."}
```

On the left is the handle the model reads and writes; on the right is
the ref, the dict `getRef` returns (§4). The dict holds no handle: a
handle exists only on the wire.

A handle is `ref:<kind>/<key>`, one shape for every kind — `object`,
`file`, `space`, and `user` and `prop` (below) — with an optional
`#<type>` fragment on an object.
The kind is a word, not a letter: `object`, `file` and `space` are
one token each, the same cost as `o`, `f`, `s`, and they carry the
meaning.

The kinds follow the link registry of the server (any
`docs/19-links.md`), one handle kind for each link kind that names a
thing with a long id:

| Link | Handle | The key names |
|---|---|---|
| `any://o/<space>/<object>` | `ref:object/<key>` | the object |
| `any://o/<space>/<object>/<dataset>/<recordId>` | `ref:object/<key>/<dataset>/<recordId>` | the object; the tail is not changed |
| `any://f/<space>/<file>` | `ref:file/<key>` | the file |
| `any://s/<space>` | `ref:space/<key>` | the space |
| `any://u/<identity>` | `ref:user/<key>` | the identity |
| `any://p/<space>/<object>/<propId>` | `ref:prop/<key>/<propId>` | the object, with the key it has as `ref:object`; the tail is not changed |

A record link (a chat message, an editor block) and a property link
are the handle of their object with the rest of the path after it.
The record id and the property id are short and stay as they are; the
table gains no entry for them. The registry is open, so the wall
holds this table and nothing more general: a kind that is not in it
is not rewritten (§5). `ref:object`, `ref:file` and `ref:space` are
the refs of §2 to §4; `ref:user` and `ref:prop` are spellings only —
the wall shortens and expands them, the index does not list them. The scheme is `ref:`, not `any://`, so that the wall (§5)
can tell the two apart mechanically: one regex finds every handle the
model wrote and one finds every uri it is about to read. A handle and
a uri are two spellings of one thing; the scheme is not what keeps
the short spelling out of the chat — the wall is.

The **key** is **three** characters sliced from the id. An id that
starts with `bafy` drops its first seven characters (multibase, codec
and hash header — constant for every id the server mints); any other
id is sliced from its start, except an identity, which drops its
first two characters (they take few values, so a slice from the start
would collide often). The slice runs over the letters and
digits of the id; the dot in a space id is skipped. So
`bafyreigyuur4hq…` keys as `gyu`, the file `3V2A2AJ6J8j` as `3V2`,
the space `bafyreia3nqgj…` as `a3n`.

Keys are given **in text order, and the first id seen keeps its
key**. The wall reads the text of one call from its start; each new
id takes its slice. If that slice is already the key of another id
of the same kind in this call, the new id **shifts** its slice one
character along the id (characters 2–4, then 3–5, …) until the slice
is free. The length never changes and a key already given never
changes. A collision is therefore not an error: it raises nothing.
The kind keeps the id spaces apart; a `ref:prop` handle has no key of
its own, it carries the key of its object.

"Free" is one question to one set: the keys already given in this
call, one key per id. The wall never compares an id with another id,
and never looks at the other slices of an id that has its key. Two
ids that start with the same characters therefore part after one
step: the first holds `gyu`, the second takes `yuu`, and `yuu` is
free because no id holds it as a key — that both ids contain it does
not matter. No reader matches a key against an id either: a handle
resolves by a lookup in the table of its call, in both directions.
An id of 59 characters has about 50 slices. If every one of them is
taken, the id takes its first slice with one more character (and one
more again, if needed), so the rule always ends. Only well-formed ids
get a key (§5), and for those the case is out of reach: with 120 keys
in the call the chance is about 10⁻²² for a file id (9 slices) and
about 10⁻¹²² for an object id (50 slices). A call with thousands of
ids needs a four-character key, not a better last resort.

The cost is linear in the number of ids in the call:

| | Measured (2026-11-12, one account, 50 days, 3,554 model calls of 517 conversation runs) |
|---|---|
| Lookups per id | 1.0002 (56,149 lookups for 56,135 keys given) |
| Calls with a shift | 14 of 3,554 (0.4%), all in one run |
| Ids that shift | 14 of 56,135 (0.02%) |
| Steps per shift | 1 in all 14 cases |
| Largest call | 55 lookups |
| Time per call | 0.02 ms for the keys; 1.5 ms to find the ids in the text (130,000 characters on average) |

With `n` ids of one kind in a call and 32,768 three-character keys,
the chance that a call has a collision is about `n(n−1)/65,536`: 0.4%
at the median run (17 ids), 2.4% at 40 ids, about 20% at the largest
run measured (120 ids). An id that collides takes `1/(1 − n/32,768)`
steps on average, 1.004 at 120 ids. The expected work is therefore
`n` lookups plus a fraction of one. The worst case, with ids built on
purpose, is one step per key already given, `n²/2` lookups in all:
7,200 at 120 ids, still less than the scan of the text.

A key is a function of the id and of the text before it, and of
nothing else — not of the process, the device, the run or the text
after it. Two consequences carry the design:

- the same text renders the same handles everywhere, so a prompt
  whose prefix is stable renders a stable prefix (the cache, §5), and
  a replay or a second device makes the same wire text (§5);
- in almost every call the key is the plain slice: at three
  characters 18 of 2,469 conversation runs had a collision among
  their links and stamp ids (2026-11-11, four accounts; 17 of the 18
  on one account, and one a pair of space ids with the same hash and
  different suffixes: the later one takes its key after one step,
  like any other pair). In
  two different chats an object may rarely get two different keys;
  nothing depends on them being equal, because no handle outlives
  its call (§5b).

The **type** rides as a `#` fragment, the way a fragment rides a
URL: a label, not part of the identity. The wall reads kind + key and
drops the fragment on the way out, so `ref:object/gyu#novel` and a
bare `ref:object/gyu` both expand to the same link after a
`set_type`. Files and spaces carry no fragment. Only the refs line
carries a fragment, from its read (§2, §3); everywhere else the wall
renders a bare handle.

A ref carries its space, so `spaceId` + `objectId` go wherever a
call takes them. It holds no content — reading the body is still
`get_markdown(space, id)` — and no provenance: "how was this made"
is `effects.query` over the same records (ADR-023 §3).

### 2. Derive — ids from one query, names from one read

At run start, before the first model call, the loop collects the
`traceRef` of every raw turn in the boot window — the turns from the
anchor on (ADR-006 §2), the same set the window renders — and runs
one `effects.query` over `records`:

```
[{"$match": {"runId": {"$in": <window runs>}, "kind": "span",
             "name": {"$in": <the minting set>}}},
 {"$project": {"runId": 1, "span": 1, "phase": 1, "name": 1, "ok": 1,
               "seq": 1, "input": 1, "output": 1}},
 {"$sort": {"seq": -1}}]
```

The loop folds begin and end records by `span` id and keeps the
spans that ended `ok`. The **minting set** is the rule, held in one
place in the loop:

- objects the run **wrote**: `any.create_object`, `update_object`,
  `set_type`, `move_object`, `trash_object`/`restore_object`,
  `add_to_collection`/`remove_from_collection`,
  `append_markdown`/`put_markdown`/`edit_markdown` — the space from
  the begin record's `input`, the id from `input.object_id` or the
  end record's `output.objectId`;
- files it **attached** (`any.attach_file` → the returned uri, `on`
  the target object, the filename from `input.name`);
- the spaces those calls addressed (their `space` argument; space
  name resolution is a private helper, not a span), and the space of
  the user's view on the current message and on the window's stamps.

One source is not a span and comes from the window the loop already
holds in memory: the links of each turn's **replies** (the same
links `_auto_attachments` parses). Plain reads (query results,
search hits) do not become refs: a query over 200 rows is not 200
things the user talked about. Widening the minting set is a
one-line change and applies to the past as well: the rule runs on
every start, over the records as they are.

The records name only what the run created or attached; an object
found by search and then updated leaves an id alone. So the names
come from the server, not the trace: **one read over the id set**
(a `query_objects` by ids per space, `list_spaces` for the spaces)
gives every ref its current `name` and `type`. A renamed object
shows its new name on the next message; a deleted one is missing
from the read and drops out, so a stale name never reaches the
line. The read is an effect, so a replay sees the same table.

The table holds ids, names and types. It holds **no handles**: the
loop never computes one (§5). Names are labels, never keys: two
entries may share one, and `getRef` by name then raises naming both
(§4). The table is capped at ~15 entries for the line (§3) and ~60
for the resolver.

The query is an effect: it is in the trace, so a replay (ADR-001)
sees the same rows. A run whose records retention has dropped
(ADR-023 §6) contributes nothing; the turn text stays in the window
and the model is where it is today for that turn. The trace store is
local to the device, so a second device's bao lists refs only for
the turns it ran itself — the same degradation, never a wrong ref.
The wall does not depend on the table: on every device it renders
every link the text carries (§5).

### 3. Bind — one global, a line of links in the message

The loop:

- binds `getRef` in the `_ctx` prelude cell next to `baoSpaceConfig`
  (§4) — one function over the table of §2, no class, no query sugar;
- adds one line to the user message suffix, each entry a markdown
  link whose text is name + where and whose target is the **uri**,
  with the type as a fragment on an object: `[refs: [Dune
  (Garden)](any://o/<s>/<o>#book) · … · [Garden](any://s/<spaceId>)]`.

The loop writes uris; the model never reads them. The wall renders
the line with the rest of the message (§5), and the model reads:
`[refs: [Dune (Garden)](ref:object/gyu#book) · [Books
(Garden)](ref:object/k7m#folder) · [demo.png (on
Dune)](ref:file/3V2) · [Garden](ref:space/a3n)]`. The link shape is
the one the model reads and writes everywhere else. No long ids reach
the model.

**Placement is load-bearing.** The line rides the current user
message, after the `[now: …]` stamp, and nowhere earlier: the system
tail and the boot window sit ahead of the boot-end cache breakpoint
(ADR-005 §1.4), and anything per-run placed there rewrites the
window's cache on every start — the lesson of the user's-space apps
line (ADR-005 §5, 2026-10-16). The place also keeps the keys of the
window still: the line comes after the window in text order, so it
can take a key but can never change one given earlier (§1). Like the
apps line, the refs line is llm-only: the persisted turn keeps the
raw `[now: …]` stamp as `context` and nothing else of this. A mid-run
inject with a view gets the same line, rebuilt.

### 4. Use — `getRef` and two sentences in `_core`

`getRef(x)` returns the dict of §1 for one entry. `x` is a uri
(`any://o/…`, `any://f/…`), a space id, or a name (`"Dune"`).

The model writes a handle — `getRef("ref:object/gyu")` — and the
kernel never sees it: the wall expands the cell source before the
loop has it (§5), so the call that runs is `getRef("any://o/…")`.
That is why `getRef` needs no key table, and why a handle is always
written whole, with its `ref:` prefix: the wall finds the full shape
and nothing shorter. There is no bare-key form.

A uri or a space id resolves without the table (a space link
`any://s/<spaceId>` resolves as its space id): kind, `spaceId` and
`objectId` are in it, and the table only adds `name` and `type` when
it has them. A name searches the table: more than one match raises
naming the candidates, a miss raises naming the nearest names. A
handle the wall could not expand arrives as `ref:unknown/<key>` (§5)
and raises as an unknown ref. No silent fallback anywhere: an
argument resolves to the right entry or raises. Calls stay exactly as
today:

```python
d = getRef("ref:object/gyu")
c.update_object(d["spaceId"], d["objectId"], {"book": {"rating": 9}})
llm.read(getRef("demo.png")["uri"], "describe the image")
```

There is no inverse. The model never holds a uri to turn into a
handle: every link it reads arrived as a handle through the wall
(§5), and a call's result is rendered the same way.

`_core` (ADR-005 §5) gains two sentences: *the `[refs: …]` line on a
message names the things this conversation recently touched;
`getRef(handle or name)` gives a ref's `spaceId`, `objectId` and
`uri`, which go where a call takes them.* And: *link a thing in a
reply as `[name](ref:…)`, the way the window shows it.* Nothing else
is taught: no query recipe, no resolution inside any@v1, no rule
about which spelling goes where — the wall owns that. A ref whose
object is gone was dropped by the read (§2) before the run started.
Anything beyond the resolver — "which books did we create this
week", "how was Dune made" — is `effects.query` over the same
records (ADR-023 §3), already taught.

### 5. The wall — handles on the way in, links on the way out

The wall is inside `llm@v1 chat`, between the `llm.chat` span
boundary and the `http.post` effect. `chat` takes neutral messages
and returns a neutral reply; the wall rewrites the first before the
adapter builds the provider request, and the second after the adapter
parses the provider reply. One **table** from key to id serves both
rewrites. It is made new in each call and dropped when the call
returns. Every caller of `chat` gets the wall — the loop, `read`, a
cron program; none of them knows it is there.

**Inward**, in text order over the system text and then the messages
— every text part, every `tool_call` input and every `tool_result`
content: each link of the table in §1 becomes its handle
(`any://o/<s>/<o>` becomes `ref:object/<key>`, `any://f/<s>/<f>`
becomes `ref:file/<key>`, `any://s/<s>` becomes `ref:space/<key>`,
`any://u/<identity>` becomes `ref:user/<key>`; a record or property
tail stays after the handle), and the stamp's `object: <id>` becomes
`object: ref:object/<key>`. A space id has a shape of its own (a
hash, a dot, a suffix), so a space id that stands outside a link
becomes `ref:space/<key>` too, wherever it is — in the stamp, in a
result. A bare object id that
this call's text has already shown as a link or in a stamp becomes
its handle too. A `#type` fragment after a link stays where it is,
after the handle. Each id takes its key by the rule of §1, and the
table records it. So an
object created this run is covered with no extra step: its uri is in
a `tool_result`, and the next call renders it as a handle.

**Only a well-formed link is rewritten.** The wall reads text, and
text holds link-shaped things that are not objects: a placeholder in
a skill (`any://o/<spaceId>/<id>`, `any://f/{sid}/{k}`), an id that a
tool result cut short, a link from a page or an email. The recorded
prompts hold all of these (2026-11-12). The wall takes a link only
when all of this holds:

- the kind is in the table of §1 (`o`, `f`, `s`, `u`, `p`);
- the space id is `bafy`, 55 base32 characters, a dot and a suffix of
  letters and digits;
- the object id is `bafy` and 55 base32 characters; the file id is
  letters and digits, eight or more (the server mints 9 to 11); the
  identity is letters and digits, forty or more;
- the path is one that the registry defines for the kind: nothing
  after the id, or `/<dataset>/<recordId>` on an object, or the
  `/<propId>` of a property link;
- the link ends there: what follows is the end of the text, a `#type`
  fragment, or a character that cannot be part of a link.

Everything else stays as it is written, and the model reads it as it
does today: a placeholder, a cut id, a link with a query or a longer
path (`…/<f>?variant=thumb`), the legacy member link
(`any://m/<space>/<identity>`: the server reads it and never writes
it, and a handle for it would come back in the `u` form, which is a
change of the text and not a shorter spelling of it), and a kind that
the table does not hold (the recorded prompts have `any://r/`).
A bare space id or object id is held to the same shape. A thing that
is not an object gets no handle, so the table holds only ids that the
server can resolve, and a text cannot fill the table with short fake
ids to push a real id off its slice (§1).

**Outward**, on the text parts and the `tool_call` inputs of the
reply: `ref:object/<key>`, `ref:file/<key>` and `ref:user/<key>`
become the uri from the table, fragment dropped; a tail after an
object handle stays after the uri, and `ref:prop/<key>/<propId>`
becomes the property link of that object. A space has two spellings,
and the place of the handle selects one: `ref:space/<key>` as the
target of a markdown link (`](ref:space/a3n)`) becomes
`any://s/<spaceId>`, a link that the chat can open; anywhere else it
becomes the space id, which is what every call takes for a space. The
loop receives real
links: the reply reaches the chat with them and the existing link
parser (`_auto_attachments`) chips them as today; the cell source
reaches the kernel with them, so a handle inside a string literal — a
page body for `put_markdown`, a memory, a program source — is a real
link by the time any@v1 sees it.

**A handle the table does not hold** — one the model invented, or one
it damaged — becomes `ref:unknown/<key>`, and the reply gains
`unresolvedRefs: ["ref:object/<key>", …]` with the handles as the
model wrote them. `unknown` is not a kind, so that text can never
expand later, in this call or in any other. The loop names the
unresolved handles in the digest of the cell that carried them, or in
the run's result. For the same reason the inward rewrite renames a
handle-shaped text that is already in its input — a user who typed
one, a page that quotes one — to `ref:unknown/…`: the only handles
the model reads are the ones this call made.

**Thinking parts are not touched** in either direction. A provider
signs them, and a rewritten block fails its signature on the next
call. A handle the model wrote inside its thinking goes back to it
unchanged.

**What the trace holds.** The `llm.chat` span records the messages
and the reply as the loop has them: uris only. The `http.post`
effect under it records the provider body and the raw reply: handles
only, exactly what the model read and wrote, a dropped character
included. A person who debugs the loop reads the first; a person who
debugs the model or `llm@v1` reads the second; `unresolvedRefs` on
the span points from one to the other. Nothing is added to either
record to make this true.

**Replay and mocks.** The handles are a function of the call's text,
so a strict replay builds the same provider body and its `http.post`
matches by input key (ADR-028). A mock run that serves some calls and
runs others live needs no table from the earlier run: a served
`http.post` returns the recorded raw reply, and the wall expands it
with the table it made from the same text. This is the reason the
table may not live longer than the call: a table that remembered
earlier calls or earlier runs would put the history of the process
into the provider body, and a replay in a new process would miss.

**The cache.** A prompt whose prefix is unchanged renders an
unchanged prefix, because each key depends only on the text before
it (§1). Across runs the boot window is that prefix, so the boot-end
breakpoint (ADR-005 §1.4) holds: shipping the wall costs one cache
miss per chat, then the window is stable — and about a fifth shorter.
Inside a run the message list only grows (the loop appends the reply
and the results; it never edits an earlier message), so each call
extends the one before it.

What the wall does not cover, and why that is fine: a bare
60-character id in a digest that no earlier text of the call showed
as a link stays long (it degrades to today, it is never wrong); a
handle assembled from fragments inside code escapes the rewrite and
misses at the first call, loudly; a key the model damaged into the
key of another object in the same call expands to that object, as a
mis-copied long id does today — three fixed characters make that a
one-in-30,000 accident per pair, and the raw reply in the
`http.post` record shows what was written. `llm@v1 judge` builds its
own request and has no wall.

### 5a. Reply chips for what the run changed

The final reply gets attachment chips (the existing ≤32 attachments
map) for objects and files the run created or changed and the reply
did not link — read from the run's own effect records, the same read
the digest makes. The user sees what changed; the next turn's
history carries the links whether or not the model wrote them.

### 5b. Lifetime — a handle lives for one call, a uri lives forever

A handle exists between the wall's two rewrites and nowhere else.
What the loop holds, what a cell runs and what anything persists is
always the uri: a program (ADR-013) the model saved with
`getRef("ref:object/gyu")` in it is stored as `getRef("any://o/…")`,
which resolves on any run without a table (§4). No write gate is
needed. Pages and memories are prose and arrive expanded the same
way.

The one handle-shaped text that can persist is `ref:unknown/<key>`
(§5). It is inert by construction: no table has an `unknown` kind,
so it expands to nothing on any later call, and `getRef` raises on
it. Because nothing live persists, keys are free to differ between
calls, chats and versions of the rule: changing the key length or the
collision rule later costs one cache miss per chat and nothing else.

### 6. Worked example

Exchange 1, "Add Dune to my books", view Garden. The message carries
the stamp only — there are no refs yet. The cell
`c.create_object(currentUserSpace, {"name": "Dune", "type": "book"})`
leaves a span whose begin record holds the name and space and whose
end record holds the id and link. The cell's result holds the uri;
on the next call the wall renders it as `ref:object/gyu`, and the
model replies `Added [Dune](ref:object/gyu)`, which the wall
expands: the loop and the chat see `[Dune](any://o/…)` and a chip.
The run's `llm.chat` records hold the uri; its `http.post` records
hold `ref:object/gyu`.

Exchange 2, the user sends demo.png, "this is the cover". At start
the query over run 1 yields two ids, the read names them `Dune`
(book) and `Garden`; `_ctx` binds the resolver, and the loop ends the
message with the stamp and a refs line of two links. The model reads
`[refs: [Dune (Garden)](ref:object/gyu#book) ·
[Garden](ref:space/a3n)]`, and the window shows exchange 1's reply
as `Added [Dune](ref:object/gyu)` — the same key, because the window
comes first in the text and Dune's id was first seen there. The model
writes `d = getRef("ref:object/gyu"); c.attach_file(d["spaceId"],
d["objectId"], "demo.png", data)`; the cell that runs has the uri in
place of the handle, and copies no id. The reply `Attached the cover
to Dune.` carries no link; §5a chips Dune anyway, because the run
changed it.

Exchange 3, "what's in that image?". The window shows the two past
turns exactly as today (stamp, `[ran: any.attach_file×1 · run_…]`,
replies) with handles for their links; the current message ends with
`[refs: [demo.png (on Dune)](ref:file/3V2) · [Dune
(Garden)](ref:object/gyu#book) · [Garden](ref:space/a3n)]`. The
cell `llm.read(getRef("ref:file/3V2")["uri"], "describe the image")`
is where round 2 failed: the file link was in no reply, the model
built a wrong one. Had the user renamed the book to "Dune: Part One"
between the exchanges, the line would carry the new name and the
same handle.

Had a second book's id sliced to `gyu` as well, the one later in the
text would read `ref:object/yuu` (its slice shifted by one) in that
call, and nothing else would change.

## Consequences

- Fewer lookup turns and no id copying on the common path ("mark it
  done", "what's in that image", "put Dune under Books").
- Weaker models gain the most: they read a short handle list and pass a
  handle, instead of carrying 60-character strings across messages.
- Nothing new is stored: no dataset field, no field-growth migration,
  no second copy of ids that can drift from the trace, no key table.
  Retention is the only housekeeping, and it is already there.
- Names are always current: the read, not the record, names a ref.
- "What exists" and "how it was made" are two projections of the
  same records, and a change to the minting rule reaches the past.
- Nothing new to learn: one resolver, and the calls the model makes
  today.
- One id form inside. The loop, the kernel, the cells, the digests,
  the `llm.chat` records and the store hold uris; the handle exists
  on the wire and in the `http.post` record that is the wire. The
  host does not change: no new effect, no state in the process, and
  nothing between the recorded request and the provider that the
  trace does not show.
- The table is **reproducible without a store**: it is a function of
  the call's text, so the same text gives the same handles on every
  run, every device and every replay. Three things follow from "the
  first id seen keeps its key":
  - the cache holds at any key length (0 breaks in 342 extending
    windows for every first-seen rule in the replay; 40 for the one
    rule that let a new id reshape an old key);
  - a collision is harmless — the later id shifts its slice — so the
    key can be short: three characters, not the six that the 11-10
    revision needed to make a raising collision rare;
  - an incrementing number by first appearance would be as stable as
    a slice (the 11-10 revision said a per-run number was
    cache-unstable; the replay shows that this holds only for a
    number given in table order, not in text order). The slice is
    kept for another reason, below.
- The key is a **fixed-length slice, not a counter and not a growing
  prefix**, because of what a damaged handle does. On the largest
  window (31 objects): with a key that starts at one character and
  grows on a collision, 23 of 31 keys become the key of another live
  object when one character is dropped; with a counter, 9 of 31, and
  a random number names a live object 31% of the time; with a fixed
  slice, 0 of 31, and a random three-character key hits a live
  object about once in 1,000. A fixed slice that is damaged misses
  the table, becomes `ref:unknown/…` and is named in `unresolvedRefs`
  (§5). The price is a token and a half per handle over a
  one-character key (5.5 against 4.0, Qwen3 and `o200k`) — less than
  1% of the window.
- The window is about a fifth shorter (15,083 tokens against 19,249
  on the largest replayed window), and stays byte-stable across runs.
- The two spellings never meet: the model reads and writes handles,
  everything else holds uris, and one place converts. No prompt rule
  about which goes where, and nothing for a weaker model to drop.
- Cost: one local aggregate plus one batched read per run start
  (milliseconds); two regex passes over the prompt per model call;
  and a refs line of ~100–200 tokens per message (about 13 per entry:
  a five-token handle, the name, the type and space, the link
  brackets) that rides the message suffix, outside the cached prefix
  (system blocks and boot window), so it never costs a cache write;
  plus one cache miss per chat, and one per cron program, when the
  wall ships. The saving is on the other side: cells that pass a
  five-token handle instead of a 40-token id, replies and a window
  that carry handles instead of 45-token links, and lookups that do
  not happen.
- A trace reader meets the short spelling in one place, the
  `http.post` record. `trace show` can print the uri beside a handle
  there by running the inward rule over the record's own span input;
  it needs no stored table.
- Device-local: the refs line for turns run on another device is
  absent, not wrong. The wall is not device-local.
- Chips on every write may be noisy for bulk jobs (40 Sprouts): chips
  beyond a handful collapse into a count, or bulk writes do not chip
  (open question 2).

## Alternatives considered

- **A `refs` field on the `agent_turns` record**, captured at run
  end (the 2026-10-16 revision of this ADR) — survives retention and
  syncs across devices, but duplicates ids the trace already holds,
  freezes the minting rule at write time, needs a declared field and
  the field-growth path, and gives the model a second read surface
  beside `effects.*`. The derived view keeps one surface and one
  truth; the two things it loses (long memory, other devices) were
  judged not worth a store.
- **Names as keys** (the first draft: `refs["Dune"]`) — a name
  changes inside one window and two objects share one; the key had
  to grow a `(Garden)` suffix to disambiguate. Rejected in the
  2026-11-03 review; names stay as labels and as a `getRef` shortcut.
- **Incrementing numbers as handles** (`any://task/132`, the review's
  proposal) — a counter that lasts across runs needs a store, and the
  trace cannot be one: it is device-local and retention drops its
  head.
- **A number by first appearance in the call** (the 11-12 chat
  proposal: the table per call of this ADR, with `ref:object/7` for
  the seventh object in the text) — no store, cache-stable, no
  collision rule, and the simplest to explain. Three things speak
  against it. It is dense: every number up to the count is live, so a
  mis-typed number, or a number cut short at the output limit (`1`
  from `12`), is another live object and nothing reports it
  (Consequences); a slice in the same state misses the table. Every
  key depends on the order of the text, where a slice depends on it
  for the ids that shift (0.02%): one object reads as `7` in one call
  and `9` in another, and a part that the wall cannot rewrite (a
  signed thinking part) holds a stale number as soon as the text
  before it changes. And it saves no tokens: with one table per call
  a handle costs 5.5 tokens on Qwen3 at 17 objects and 6.1 at 120,
  because each digit is a token, against 5.5 for a slice; on `o200k`
  a number is one token, 5.0 against 5.6. The dense key space is the
  reason that decides, and it weighs only as much as models damage
  handles — which the fluency tests measure, with the number as an
  arm (open question 5).
- **A key that grows from one character** (the 11-11 chat proposal) —
  the shortest handle (4.0 tokens) and cache-stable with first-seen
  order, but dense in the same way: `gy` exists only because `g` is
  taken, so a dropped character expands, silently, to another object.
- **A key that extends on a collision** (the later id keeps its
  first slice and takes a fourth character: `gyu`, then `gyuu`) —
  the same set lookup and the same cost as the shift, and every key
  stays the start of its id. It fails on three points. A dropped last
  character turns the longer key into the shorter one, so the model
  names the other object of the pair, silently — and the pair is the
  two objects it is most likely to confuse; a shifted key that loses
  a character has the wrong length and misses the table. The prefix
  does not identify the id: `gyuu` starts both ids of the pair, and
  for two space ids with one hash a prefix that tells them apart is
  the whole hash, about 53 characters. And the length is no longer
  fixed, which the rule for a damaged handle relies on (Consequences).
- **A pure six-character slice, a collision raises** (the 11-10
  revision) — a function of the id alone, which looked necessary for
  the cache and is not. It paid two tokens per handle over three
  characters to make the raise rare, and still left a run that could
  not name two of its objects.
- **A table-dependent key** — the 11-04 draft lengthened colliding
  keys (first both, then the newer only, the older keeping the short
  one by trace order). A rule that reshapes a key already shown
  breaks the cache (40 of 342 windows in the replay) and the 11-04
  older-wins rule resolved the newer entry's handle to the older
  object silently. First seen in text order with a shift has neither
  fault: no key ever changes, and no two ids share one.
- **The wall in the loop** (the 11-10 revision) — the loop rewrote
  the message list and the reply, with a run-local map. The loop's
  records then held handles while the effects below them held uris,
  `getRef` needed a key table in the kernel, and the handle forms
  multiplied (`object/gyuur4`, a bare key). At the `llm@v1` boundary
  the same two rewrites leave every layer above them unchanged.
- **The wall in the Rust host, with a table in process memory** (the
  11-11 chat proposal: a per-process map, or an LRU) — the right
  boundary, one layer too low. Below the `http.post` record nothing
  shows what the model read or wrote, so a wall error and a model
  error look the same; the host would parse every provider's body
  and stream; and keys would depend on what the process saw before,
  so a restart or an eviction breaks the cache and a replay cannot
  rebuild the text. A process table also buys nothing here: measured
  over 50 days, one table for all runs gives longer keys than a table
  per run (1.96 characters against 1.40 with growing keys), and
  nothing needs one object to keep one key across chats.
- **`any://` look-alike handles** — a short form under the real
  scheme (`any://book/7`) reads as a link the model already knows
  and gets passed where a real uri goes; any-ui would chip it as a
  dead link. `ref:` is a scheme of its own, with the link *shape*
  kept (`[name](ref:kind/key#type)`) so nothing new is learned.
- **A type segment in the path** (`ref:o/book/gyu`, the 11-04
  draft) — put the label where identity lives; one shape per kind.
  The `#` fragment says what the type is: a hint that does not change
  the identity.
- **No wall: a prompt rule keeps `ref:` out of the chat** (the 11-04
  draft: "write a real link into a reply, never a handle", with a
  programs@v1 write gate that refused a source naming `getRef`) —
  the rule is the kind weaker models drop first, the gate covered one
  of several leak paths, and both spellings had to live in the
  model's head at once. One conversion at the wall covers every path
  and nothing is taught.
- **A ref where any@v1 takes an object**, or a `Refs` class with
  `where()` filters — sugar; each adds a surface to learn (a
  resolution step in every object-taking method, or a reserved
  class) for a few tokens per call. Features do not fold into
  any@v1; `getRef` and `effects.query` cover both.
- **Names from the records** — only `create_object` and
  `attach_file` carry one; an updated object leaves an id alone, and
  a recorded name goes stale on rename. One read fixes both.
- **Prompt rules only** — tried in round 2; they don't hold, least of
  all on weaker models.
- **Keep the kernel alive across messages** — variables would
  survive, but a long-lived kernel breaks replay (ADR-001: a run is
  self-contained) and grows without bound.
- **Print ids into replies automatically** — clutters what the user
  reads; chips (§5a) carry the same link without the text.
- **Let the model write a scratch note of ids** — the model has to
  remember to do it, the same failure as the prompt rules.

## Open questions

1. Reads: should a single-object read the user asked about ("open
   Dune", get_markdown on one object) join the minting set, or
   writes and links only? A `$match` widening either way.
2. Chips for bulk writes: collapse past N, or chip only objects the
   user named?
3. Scope: this chat's runs only (the window's `traceRef`s), or every
   run of this bao within retention? A `runId` filter either way.
4. Bare ids that no earlier text of the call showed as a link: leave
   them long (today's behaviour), or render every id-shaped text?
   Over 50 days the model saw 1,006 distinct ids of which 373 were
   links or stamp ids; the rest were bare. A bare id gives no kind
   except by its shape, and its handle expands to a uri where the
   model may have wanted a bare id.
5. Key length and key shape: the draft starts at a three-character
   slice (2026-11-11). Four characters is the fallback (6.2 tokens
   against 5.5; a random key hits a live object about once in 30,000
   instead of once in 1,000; 2 runs with a collision in 2,469
   instead of 18). The model-fluency tests (Verification) decide
   whether three is enough for the weak models. The same tests
   measure a number by first appearance (Alternatives): if models
   copy a number with no more wrong-object errors than a slice, the
   number is the simpler key and this question opens again.

## Verification

On the staging rig, with the wall in place: re-run S1–S6 and S10 of the probe suite (the series
where lookups and lost links showed) on sonnet-5 AND one weaker model
(GLM or Kimi via OpenRouter), measuring re-lookup cells, missing-link
turns and cost against round 2, and counting `unresolvedRefs` and
wrong expansions per key shape (a slice of three and of four
characters, and a number by first appearance).

Unit-test the key rule: the `bafy` prefix drop, the three-character
slice, the dot of a space id skipped, first seen keeps its key, a
forced collision shifts the later id and raises nothing, a second
forced collision shifts again, two space ids with one hash and two
suffixes get two keys after one step, an id whose every slice is
taken extends its first slice by one character, the number of set
lookups for a window equals its ids plus its shift steps, kinds do
not collide with each other. Unit-test the link shape: a placeholder,
a cut id, a short id, a link with a query, a legacy `any://m/` link
and a link of an unknown kind all pass through unchanged and take no
key. Unit-test the kinds: a record link and a property link carry the
key of their object and keep their tail in both directions; an
identity keys after its first two characters; a space handle expands
to `any://s/…` as a markdown link target and to the bare id anywhere
else.

Test the wall in `llm@v1`: the inward rewrite of a window renders
byte-identical on two runs, and a window that extends another renders
a text that extends the other's (the 2026-11-11 replay as a test); an
outward round-trip on a reply text and on a `tool_call` input (a
handle in a string literal reaches `put_markdown` as a uri; a program
saved from a cell holds no handle; a space handle becomes a space
id); a fragment is kept inward and dropped outward; an unknown handle
becomes `ref:unknown/…` and is listed in `unresolvedRefs`; a
handle-shaped text in the input is renamed and never expands; a
thinking part is byte-identical in and out; a uri first seen in a
`tool_result` is a handle on the next call and expands from that
call's reply.

Test the records: outside thinking parts the `llm.chat` span holds no
handle of a real kind, and the `http.post` body under it holds no
`any://` link and no bare space id. Add two replay
checks: a run with the refs query, the read and the wall replays
strict (ADR-028) with the same `http.post` keys; and a mock run that
serves the first model call and runs the second live expands both
replies correctly.

Test `getRef`: a uri in and out of the table, a space id, a name
(unique and ambiguous), `ref:unknown/…` raises.
