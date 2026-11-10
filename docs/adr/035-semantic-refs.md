# ADR-035: Semantic refs — names for the ids a conversation touched

Status: **Proposed** (2026-09-25, revised 2026-10-16, 2026-11-03 and
2026-11-04 after the community review, 2026-11-10 after the PR
review), experiment on `exp/semantic-ids`
Date: 2026-09-25
Builds on: ADR-001 §4 (span records: `input` on begin, `output` on
end), ADR-005 §1.4/§4/§5 (the boot-end cache breakpoint, digest, boot
window, message suffix), ADR-006 §2 (the anchored boot window),
ADR-010 §8 (bound cell globals), ADR-023 §3 (`effects.query` over
the trace records), ADR-023 §6 (retention)
Amends (if accepted): ADR-005 §5 (the message suffix gains a refs
line; every `any://` link the model reads is rendered as a handle and
every handle it writes is expanded back — the wall of §5; the final
reply gains chips for what the run changed), ADR-006 §2 (the boot
window renders handles, not ids), ADR-010 §8 (a `getRef` global
beside `baoSpaceConfig`)
Tracks: Linear BOB-179 (from BOB-160 bullet 3, "automap ids to
semantic meanings"); evidence in `docs/bob-160-probe-results.md`
(rounds 1 and 2) and the boot-window calibration of 2026-10-16

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
must never be reshaped under it, which rules out any table-dependent
lengthening.

What the model did is already on record. Every write a run made is a
span in its trace: the begin record carries the call's `input`
(parameter-keyed, ADR-010 §8), the end record its `output`
(ADR-001 §4), and `effects.query` (ADR-023 §3) reads those records
with the same pipeline language the model uses over datasets. The
window names every past run by its `traceRef`. The index this ADR
wants is a projection of records that exist; nothing has to be
stored to have it.

## Decision

At run start the loop derives a small index of what the conversation
touched — the ids from the trace records of the window's runs, the
names from one read of those ids — and binds it in the kernel behind
one resolver. Each entry has a **handle**: a short, typed, link-shaped
token that is a pure function of the id, so it is the same on every
run, every device and every message, and nothing is stored to mint
it. The model lives in a world of handles: every `any://` link it
reads is rendered as a handle at the wall, and every handle it writes
is expanded back to the link at the same wall (§5). Inside a cell it
refers to things by handle or by name; the long ids stay under the
hood. A ref is a view over the trace, lives for one run, and expires
with it. any@v1 does not change.

### 1. A ref and its handle

```
ref:object/gyuur4#book  {"kind": "object", "type": "book", "name": "Dune", "spaceId": "...",
                         "objectId": "...", "uri": "any://o/<spaceId>/<objectId>"}
ref:file/3V2A2A         {"kind": "file", "name": "demo.png", "spaceId": "...", "fileId": "...",
                         "on": "<objectId>", "uri": "any://f/<spaceId>/<fileId>"}
ref:space/a3nqgj        {"kind": "space", "name": "Garden", "spaceId": "..."}
```

A handle is `ref:<kind>/<key>`, one shape for every kind — `object`,
`file`, `space` — with an optional `#<type>` fragment on an object.
The kind is a word, not a letter: `object`, `file` and `space` are
one token each, the same cost as `o`, `f`, `s`, and they carry the
meaning. The scheme is `ref:`, not `any://`, so that the wall (§5)
can tell the two apart mechanically: one regex finds every handle the
model wrote and one finds every uri it is about to read. A handle and
a uri are two spellings of one thing; the scheme is not what keeps
the short spelling out of the chat — the wall is.

The **key** is a slice of the id itself. An id that starts with
`bafy` drops its first seven characters (multibase, codec and hash
header — constant for every id the server mints); any other id is
sliced from its start. The key is the next **six** characters, so
`bafyreigyuur4hq…` keys as `gyuur4`, the file `3V2A2AJ6J8j` as
`3V2A2A`, the space `bafyreia3nqgj…` as `a3nqgj`. Six base32
characters give a billion values: two ids of one kind collide with
odds near one in a billion per pair, so a table of sixty entries sees
one about once in a million chats, and one that happens raises on
resolution (§4). There is **no lengthening** and no table in the
rule: the same id keys the same six characters in every message,
every run and every device, which is what lets the window render
handles and stay cacheable (§5). The kind keeps the three id spaces
apart.

The **type** rides as a `#` fragment, the way a fragment rides a
URL: a label, not part of the identity. The resolver reads kind +
key and ignores the fragment, so `ref:object/gyuur4#novel` and a
bare `ref:object/gyuur4` both resolve after a `set_type`. Files and
spaces carry no fragment. The wall renders a bare handle (it knows no
type); the refs line adds the fragment from its read (§2).

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
  the user's view on the current message and on the window's stamps,
  so the handle the stamp shows (§5) always resolves.

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

The handle is computed per entry from the id (§1) and nothing else.
Two entries of one kind that share six characters are both kept, and
`getRef` on that key raises naming both (§4): loud, not wrong. Names
are labels, never keys: two entries may share one, and `getRef` by
name then raises the same way. The table is capped at ~15 entries
for the line (§3) and ~60 for the resolver.

The query is an effect: it is in the trace, so a replay (ADR-001)
sees the same rows. A run whose records retention has dropped
(ADR-023 §6) contributes nothing; the turn text stays in the window
and the model is where it is today for that turn. The trace store is
local to the device, so a second device's bao sees refs only for the
turns it ran itself — the same degradation, never a wrong ref.

### 3. Bind — one global, handles and names in the message

The loop:

- binds `getRef` in the `_ctx` prelude cell next to `baoSpaceConfig`
  (§4) — one function over the table of §2, no class, no query sugar;
- adds one line to the user message suffix, each entry a markdown
  link whose text is name + where and whose target is the handle:
  `[refs: [Dune (Garden)](ref:object/gyuur4#book) · [Books
  (Garden)](ref:object/k7mpx2#folder) · [demo.png (on
  Dune)](ref:file/3V2A2A) · [Garden](ref:space/a3nqgj)]`. The link
  shape is the one the model reads and writes everywhere else; only
  the scheme differs. No long ids in the prompt.

**Placement is load-bearing.** The line rides the current user
message, after the `[now: …]` stamp, and nowhere earlier: the system
tail and the boot window sit ahead of the boot-end cache breakpoint
(ADR-005 §1.4), and anything per-run placed there rewrites the
window's cache on every start — the lesson of the user's-space apps
line (ADR-005 §5, 2026-10-16). Like that line, the refs line is
llm-only: the persisted turn keeps the raw `[now: …]` stamp as
`context` and nothing else of this. Past turns render as today, with
their links as handles (§5); the line appears on the current message
only. A mid-run inject with a view gets the same line, rebuilt.

### 4. Use — `getRef` and two sentences in `_core`

`getRef(x)` returns the dict of §1 for one entry. `x` is a handle
with or without the `ref:` prefix (`"object/gyuur4#book"`,
`"ref:file/3V2A2A"`), a bare key (`"gyuur4"`), a full `any://` uri,
or a name (`"Dune"`). A handle resolves by exact match on kind + key;
a bare key or a name searches every kind. More than one match — a
six-character collision, or two entries with one name — raises
naming the candidates; a miss raises naming the nearest handles. No
silent fallback: a key resolves to the right entry or not at all,
never to another. A uri resolves without the table: kind, `spaceId`
and `objectId` are in the uri, and the table only adds `name` and
`type` when it has them. Calls stay exactly as today:

```python
d = getRef("object/gyuur4")
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

The model call is one place in the loop: one spot builds the message
list, one spot reads the reply and the cell source back. The wall is
two text rewrites at that spot and one run-local **map** from handle
to uri, seeded from the table of §2.

**Inward**, on every text the model reads — the boot window, the
user message and its stamp, a mid-run inject, every cell digest:
`any://o/<s>/<o>` becomes `ref:object/<key>`, `any://f/<s>/<f>`
becomes `ref:file/<key>`, the stamp's `space: <id>` becomes `space:
ref:space/<key>`, and a bare id the map already knows becomes its
handle. Each uri rewritten is added to the map. The rewrite is a
pure function of the text (fixed key, no table in the rule, §1), so
the window renders byte-identical across runs and the boot-end
breakpoint (ADR-005 §1.4) holds: shipping the rewrite costs one
cache miss per chat, then the window is stable — and shorter, since
every 45-token link in a past reply is now a 9-token handle. An
object created this run is covered the same way: its uri lands in a
digest, the wall renders it as a handle and the map learns it.

**Outward**, on every text the model writes — the reply and the cell
source: `ref:<kind>/<key>` becomes the uri from the map, fragment
dropped. The reply reaches the chat with real links; the existing
link parser (`_auto_attachments`) chips them as today. The cell
source is rewritten before the kernel executes it, so a handle
inside a string literal — a page body for `put_markdown`, a memory,
a program source — is a real link by the time any@v1 sees it, and
`getRef("ref:…")` keeps working because `getRef` takes a uri. A
handle the map does not know (one the model built by hand, or for a
thing no text named) stays as inert text, and the digest of that
cell, or the run's result, names it as unresolved.

What the wall does not cover, and why that is fine: a bare
60-character id in a digest that the map has never seen stays long
(it degrades to today, it is never wrong); a handle assembled from
fragments inside code escapes the source rewrite and misses at the
first call, loudly. Neither earns a guard.

### 5a. Reply chips for what the run changed

The final reply gets attachment chips (the existing ≤32 attachments
map) for objects and files the run created or changed and the reply
did not link — read from the run's own effect records, the same read
the digest makes. The user sees what changed; the next turn's
history carries the links whether or not the model wrote them.

### 5b. Lifetime — a ref lives for one run, a uri lives forever

A ref is a per-run view; what persists is always the uri. Because
the cell source crosses the wall expanded, nothing the model saves
from a cell holds a handle: a program (ADR-013) saved with
`getRef("ref:object/gyuur4")` in it is stored as `getRef("any://o/…")`,
which resolves on any run without a table (§4). No write gate is
needed. Pages and memories are prose and arrive expanded the same
way. The one path that can persist a handle is the inert-text case
of §5, which is stable and harmless: a function of the id, never
another object.

### 6. Worked example

Exchange 1, "Add Dune to my books", view Garden. The message carries
the stamp only — there are no refs yet. The cell
`c.create_object(currentUserSpace, {"name": "Dune", "type": "book"})`
leaves a span whose begin record holds the name and space and whose
end record holds the id and link. The digest shows the new object as
`ref:object/gyuur4` — the wall rendered it and the map learned it —
and the model replies `Added [Dune](ref:object/gyuur4)`, which the
wall expands: the chat sees `[Dune](any://o/…)` and a chip.

Exchange 2, the user sends demo.png, "this is the cover". At start
the query over run 1 yields two ids, the read names them `Dune`
(book) and `Garden`; `_ctx` binds the resolver, the message ends
with the stamp and `[refs: [Dune (Garden)](ref:object/gyuur4#book) ·
[Garden](ref:space/a3nqgj)]`; the window shows exchange 1's reply as
`Added [Dune](ref:object/gyuur4)`. The cell `d = getRef("object/gyuur4");
c.attach_file(
d["spaceId"], d["objectId"], "demo.png", data)` copies no id. The
reply `Attached the cover to Dune.` carries no link; §5a chips Dune
anyway, because the run changed it.

Exchange 3, "what's in that image?". The window shows the two past
turns exactly as today (stamp, `[ran: any.attach_file×1 · run_…]`,
replies); the current message ends with `[refs: [demo.png (on
Dune)](ref:file/3V2A2A) · [Dune (Garden)](ref:object/gyuur4#book) ·
[Garden](ref:space/a3nqgj)]`. The cell
`llm.read(getRef("file/3V2A2A")["uri"], "describe the image")` is
where round 2 failed: the file link was in no reply, the model built
a wrong one. Had the user renamed the book to "Dune: Part One"
between the exchanges, the line would carry the new name and the
same handle.

## Consequences

- Fewer lookup turns and no id copying on the common path ("mark it
  done", "what's in that image", "put Dune under Books").
- Weaker models gain the most: they read a short handle list and pass a
  handle, instead of carrying 60-character strings across messages.
- Nothing new is stored: no dataset field, no field-growth migration,
  no second copy of ids that can drift from the trace. Retention is
  the only housekeeping, and it is already there.
- Names are always current: the read, not the record, names a ref.
- "What exists" and "how it was made" are two projections of the
  same records, and a change to the minting rule reaches the past.
- Nothing new to learn: one resolver, and the calls the model makes
  today.
- The handle is a slice of the id, so it is **stable without a
  store**: the same object keys the same on every run, every device
  and every message, a leaked handle can never point at another
  object, and a rename changes the label but not the key. This is why
  the handle is not an incrementing number, which the review
  proposed:
  - an incrementing counter needs a store, and the trace cannot be
    one — it is device-local and retention drops its head, so a
    second device or a swept store mints the same number again for
    another object;
  - a per-run number (1..N over the table, dead at reply time) keeps
    no store but is **cache-unstable**: the boot window sits before
    the boot-end breakpoint (ADR-005 §1.4) and must render the same
    text on every run, so a number can never appear in a past turn,
    and the 45-token links there can never be compressed;
  - a per-run number is also **leak-unstable**: every path from model
    text to persistence — a program, a page, a memory, loose prose —
    would need expansion or a check, and a number that slips through
    resolves silently to the wrong thing next run. A sliced key
    needs none of that: it fails loudly or resolves right.
  - The price is three or four tokens per handle over an integer, and
    one more than a four-character key would cost; the six characters
    buy a rule with no table and no lengthening.
- The cache point cuts the other way too: because the handle needs no
  table, the window renders handles in place of every link and stamp
  id and stays byte-stable across runs (§5). The 2026-10-16 replay
  put the window's stamps and footers at ~2k tokens with ids in them;
  handles cut that by more than half, and every linked reply shrinks
  with them.
- The two spellings never meet: the model reads and writes handles,
  the chat and the store hold uris, and one place converts. No prompt
  rule about which goes where, and nothing for a weaker model to
  drop.
- Cost: one local aggregate plus one batched read per run start
  (milliseconds), and a refs line of ~100–250 tokens per message
  (about 17 per entry: a 9-token handle, the name, the type and
  space, the link brackets) that rides the message suffix, outside
  the cached prefix (system blocks and boot window), so it never
  costs a cache write; plus one cache miss per chat when the window
  rewrite ships. The saving is on the other side: cells that pass a
  9-token handle instead of a 40-token id, replies and a window that
  carry handles instead of 45-token links, and lookups that do not
  happen.
- Device-local: refs for turns run on another device are absent, not
  wrong. The window text for those turns is unchanged from today.
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
  proposal) — shortest in tokens, but stable only with a store;
  per-run numbers are cache-unstable and leak-unstable. The
  Consequences list the three reasons.
- **`any://` look-alike handles** — a short form under the real
  scheme (`any://book/7`) reads as a link the model already knows
  and gets passed where a real uri goes; any-ui would chip it as a
  dead link. `ref:` is a scheme of its own, with the link *shape*
  kept (`[name](ref:kind/key#type)`) so nothing new is learned.
- **A type segment in the path** (`ref:o/book/gyuu`, the 11-04
  draft) — put the label where identity lives; one shape per kind.
  The `#` fragment says what the type is: a hint that does not change
  the identity.
- **A table-dependent key** — the 11-04 draft lengthened colliding
  keys (first both, then the newer only, the older keeping the short
  one by trace order). Either way the key depended on the table, so
  the window could not render handles and stay cacheable, and the
  older-wins rule resolved the newer entry's handle to the older
  object silently when both showed the same short key. A fixed
  six-character key has no table in it; a collision raises.
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
4. Bare ids in digests that the map has never seen: leave them long
   (today's behaviour), or classify them with a read so the wall can
   render them too?

## Verification

Before accepting: re-run S1–S6 and S10 of the probe suite (the series
where lookups and lost links showed) on sonnet-5 AND one weaker model
(GLM or Kimi via OpenRouter), measuring re-lookup cells, missing-link
turns and cost against round 2. Add a replay check: a run with the
refs query and read replays strict (ADR-028) with the same table.
Unit-test the key rule: the `bafy` prefix drop, the six-character
slice, a forced collision that raises naming both, `getRef` by
handle, bare key, uri (in and out of the table) and name (unique and
ambiguous). Test the wall: the inward rewrite of a window renders
byte-identical on two runs; an outward round-trip on a reply and on
a cell source (a handle in a string literal reaches `put_markdown`
as a uri; a program saved from a cell holds no handle); an unknown
handle stays inert and is named; the map learns a uri from a digest
and expands it in the same run's reply.
