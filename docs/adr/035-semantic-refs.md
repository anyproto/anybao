# ADR-035: Semantic refs — names for the ids a conversation touched

Status: **Proposed** (2026-09-25, revised 2026-10-16, 2026-11-03 and
2026-11-04 after the community review), experiment on `exp/semantic-ids`
Date: 2026-09-25
Builds on: ADR-001 §4 (span records: `input` on begin, `output` on
end), ADR-005 §1.4/§4/§5 (the boot-end cache breakpoint, digest, boot
window, message suffix), ADR-006 §2 (the anchored boot window),
ADR-010 §8 (bound cell globals), ADR-013 §3 (the programs@v1 write
gate), ADR-023 §3 (`effects.query` over the trace records), ADR-023 §6
(retention)
Amends (if accepted): ADR-005 §5 (the message suffix gains a refs
line; the final reply gains chips for what the run changed), ADR-010
§8 (`getRef` and `toRef` globals beside `baoSpaceConfig`), ADR-013 §3
(the write gate refuses a program that names them)
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
handle is a slice of the id instead.

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
run and every device and nothing is stored to mint it. The model
refers to things by handle or by name inside a cell; the long ids
stay under the hood. A ref is a view over the trace, lives for one
run, and expires with it. any@v1 does not change.

### 1. A ref and its handle

```
ref:o/book/gyuu   {"kind": "o", "type": "book", "name": "Dune", "spaceId": "...",
                   "objectId": "...", "uri": "any://o/<spaceId>/<objectId>"}
ref:f/3V2A        {"kind": "f", "name": "demo.png", "spaceId": "...", "fileId": "...",
                   "on": "<objectId>", "uri": "any://f/<spaceId>/<fileId>"}
ref:s/a3nq        {"kind": "s", "name": "Garden", "spaceId": "..."}
```

A handle is `ref:<kind>/<key>` for a file (`f`) or a space (`s`) and
`ref:o/<type>/<key>` for an object. The scheme is `ref:`, not
`any://`: the model already reads real `any://o/…` and `any://f/…`
links in the window and in `_core`, and a look-alike invites it to
pass the short form where a real uri is expected. A `ref:` handle
is never a link and never reaches the chat as one.

The **key** is a slice of the id itself. An id that starts with
`bafy` drops its first seven characters (multibase, codec and hash
header — constant for every id the server mints); any other id is
sliced from its start. The key is the next four characters, so
`bafyreigyuur4hq…` keys as `gyuu`, the file `3V2A2AJ6J8j` as `3V2A`,
the space `bafyreia3nqgj…` as `a3nq`. Four base32 characters give
one million values; two keys of the same kind that collide inside
one table are lengthened until distinct (§2). The kind keeps the
three id spaces apart.

The **type** segment is a label, not part of the identity: the
resolver reads kind + key and ignores it, so `ref:o/novel/gyuu`
still resolves after a `set_type`. Files and spaces carry no type
segment.

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
  name resolution is a private helper, not a span).

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

The handle is computed per entry from the id (§1). When two entries
of one kind share a key, both keys grow one character at a time
until they differ; the odds of that among 60 entries are about one
in five hundred. A lengthened key stays resolvable by its shorter
prefix as long as the prefix is unique (§4). Names are labels only:
the same name in two spaces reads `Dune (Garden)` / `Dune (Books)`
on the line, and the handles tell them apart. The table is capped at
~15 entries for the line (§3) and ~60 for the resolver.

The query is an effect: it is in the trace, so a replay (ADR-001)
sees the same rows. A run whose records retention has dropped
(ADR-023 §6) contributes nothing; the turn text stays in the window
and the model is where it is today for that turn. The trace store is
local to the device, so a second device's bao sees refs only for the
turns it ran itself — the same degradation, never a wrong ref.

### 3. Bind — two globals, handles and names in the message

The loop:

- binds `getRef` and `toRef` in the `_ctx` prelude cell next to
  `baoSpaceConfig` (§4) — two functions over the table of §2, no
  class, no query sugar;
- adds one line to the user message suffix, handle + name + where:
  `[refs: ref:o/book/gyuu Dune (Garden) · ref:o/folder/k7mp Books
  (Garden) · ref:f/3V2A demo.png (on Dune) · ref:s/a3nq Garden]`.
  No long ids in the prompt.

**Placement is load-bearing.** The line rides the current user
message, after the `[now: …]` stamp, and nowhere earlier: the system
tail and the boot window sit ahead of the boot-end cache breakpoint
(ADR-005 §1.4), and anything per-run placed there rewrites the
window's cache on every start — the lesson of the user's-space apps
line (ADR-005 §5, 2026-10-16). Like that line, the refs line is
llm-only: the persisted turn keeps the raw `[now: …]` stamp as
`context` and nothing else of this. Past turns render as they do
today; the line appears on the current message only. A mid-run
inject with a view gets the same line, rebuilt.

### 4. Use — `getRef`, `toRef`, and two sentences in `_core`

`getRef(x)` returns the dict of §1 for one entry. `x` is a handle
with or without the `ref:` prefix (`"o/book/gyuu"`, `"ref:f/3V2A"`),
a bare key or a unique prefix of one (`"gyuu"`), a full `any://`
uri, or a name (`"Dune"`). A bare key or a name searches every kind
and raises when more than one entry matches, naming the candidates;
a miss raises naming the nearest handles. No silent fallback.
Calls stay exactly as today:

```python
d = getRef("o/book/gyuu")
c.update_object(d["spaceId"], d["objectId"], {"book": {"rating": 9}})
llm.read(getRef("demo.png")["uri"], "describe the image")
```

`toRef(uri)` is the inverse for a link the model holds — one from a
past reply in the window, or one a call just returned: it slices the
id by the rule of §1 (lengthened only if the table says that key is
taken) and returns the handle. It is pure over the uri and needs no
table entry, so it also names things the table does not hold.

`_core` (ADR-005 §5) gains two sentences: *the `[refs: …]` line on a
message names the things this conversation recently touched;
`getRef(handle or name)` gives a ref's `spaceId`, `objectId` and
`uri`, which go where a call takes them.* And: *refs live inside
`run_cell` only — write a real `any://` link (the ref's `uri`) into a
reply, a page, a memory or a program, never a `ref:` handle.*
Nothing else is taught: no query recipe, no resolution inside
any@v1. A ref whose object is gone was dropped by the read (§2)
before the run started. Anything beyond the resolver — "which books
did we create this week", "how was Dune made" — is `effects.query`
over the same records (ADR-023 §3), already taught.

### 5. Reply chips for what the run changed

The final reply gets attachment chips (the existing ≤32 attachments
map) for objects and files the run created or changed and the reply
did not link — read from the run's own effect records, the same read
the digest makes. The user sees what changed; the next turn's
history carries the links whether or not the model wrote them.

Replies carry real links, as `_core` already asks. The loop does
**not** expand `ref:` handles in a reply: an expansion step would be
a second machinery, and it would teach the model that a handle is
something to write down. A handle that does leak into a reply is
inert text, and because it is a function of the id it is never
wrong — only unclickable.

### 5a. Lifetime — a ref lives in `run_cell` and nowhere else

A ref is a per-run view. The one place the model writes code that
outlives the run is a program (ADR-013), and a program that calls
`getRef` would resolve against whatever table the run that executes
it happens to hold — a miss at best. So the programs@v1 write gate
(`_validate_source`, ADR-013 §3) refuses a source whose AST names
`getRef` or `toRef` — an `ast.Name` walk beside the existing import
check, not a text match, so a comment or a string that mentions the
word passes and a call or an alias does not. The refusal names the
fix: resolve the ref in the cell and pass the ids, or the uri, into
the program. Pages and memories are prose and get guidance only
(§4): a leaked handle there is stable and harmless, and checking
every markdown body would cost more than it saves.

### 6. Worked example

Exchange 1, "Add Dune to my books", view Garden. The message carries
the stamp only — there are no refs yet. The cell
`c.create_object(currentUserSpace, {"name": "Dune", "type": "book"})`
leaves a span whose begin record holds the name and space and whose
end record holds the id and link; the reply is `Added [Dune](…)`.

Exchange 2, the user sends demo.png, "this is the cover". At start
the query over run 1 yields two ids, the read names them `Dune`
(book) and `Garden`; `_ctx` binds the resolver, the message ends
with the stamp and `[refs: ref:o/book/gyuu Dune (Garden) · ref:s/a3nq
Garden]`. The cell `d = getRef("o/book/gyuu"); c.attach_file(
d["spaceId"], d["objectId"], "demo.png", data)` copies no id. The
reply `Attached the cover to Dune.` carries no link; §5 chips Dune
anyway, because the run changed it.

Exchange 3, "what's in that image?". The window shows the two past
turns exactly as today (stamp, `[ran: any.attach_file×1 · run_…]`,
replies); the current message ends with `[refs: ref:f/3V2A demo.png
(on Dune) · ref:o/book/gyuu Dune (Garden) · ref:s/a3nq Garden]`. The
cell `llm.read(getRef("f/3V2A")["uri"], "describe the image")` is
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
  store**: the same object keys the same on every run and every
  device, a leaked handle can never point at another object, and a
  rename changes the label but not the key. This is why the handle is
  not an incrementing number, which the review proposed:
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
  - The price is two or three tokens per handle over an integer, and
    a rare lengthening on collision, which prefix resolution absorbs.
- The cache point cuts the other way too: because the handle needs no
  table, the window renderer may later replace the full links of
  past replies with handles and stay byte-stable across runs — the
  follow-up under open question 4.
- Cost: one local aggregate plus one batched read per run start
  (milliseconds), and a refs line of ~100–250 tokens per message
  (about 14 per entry: a 6-token handle, the name, the type and
  space) that rides the message suffix, outside the cached prefix
  (system blocks and boot window), so it never costs a cache write.
  The saving is on the other side: cells that pass a 6-token handle
  instead of a 40-token id, and lookups that do not happen — the
  2026-10-16 replay put the window's stamps and footers at ~2k
  tokens with ids in them; handles would cut that by more than half
  once open question 4 lands.
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
  dead link. `ref:` is a scheme of its own.
- **Expanding `ref:` in replies** — would let the model write the
  handle instead of the link; it is a second machinery and teaches
  the wrong lesson (§5). Replies carry real links.
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
  reads; chips (§5) carry the same link without the text.
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
4. Follow-up, not blocking: the view stamp and the links in past
   replies carry long ids at 1.6 chars/token. Because a handle needs
   no table, the renderer could rewrite them to handles
   (`space: ref:s/a3nq Garden`, `[Dune](ref:o/book/gyuu)`) and the
   window would stay byte-stable across runs. The stamp is persisted
   as `context`, so this is a render-time rewrite, and the rewrite
   must pick a fixed key length (no table-dependent lengthening) to
   keep that stability.

## Verification

Before accepting: re-run S1–S6 and S10 of the probe suite (the series
where lookups and lost links showed) on sonnet-5 AND one weaker model
(GLM or Kimi via OpenRouter), measuring re-lookup cells, missing-link
turns and cost against round 2. Add a replay check: a run with the
refs query and read replays strict (ADR-028) with the same table.
Unit-test the key rule: the `bafy` prefix drop, a forced collision
that lengthens both keys, `getRef` by handle, prefix, uri and name
(unique and ambiguous), `toRef` round-trip. Test the programs@v1
gate: a source that calls `getRef` or aliases `toRef` is refused with
the fix named; a source that only mentions the word in a docstring
is saved.
