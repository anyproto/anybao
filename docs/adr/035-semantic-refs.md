# ADR-035: Semantic refs — names for the ids a conversation touched

Status: **Proposed** (2026-09-25, revised 2026-10-16 and 2026-11-03),
experiment on `exp/semantic-ids`
Date: 2026-09-25
Builds on: ADR-001 §4 (span records: `input` on begin, `output` on
end), ADR-005 §1.4/§4/§5 (the boot-end cache breakpoint, digest, boot
window, message suffix), ADR-006 §2 (the anchored boot window),
ADR-010 §8 (bound cell globals), ADR-023 §3 (`effects.query` over the
trace records), ADR-023 §6 (retention)
Amends (if accepted): ADR-005 §5 (the message suffix gains a refs
line; the final reply gains chips for what the run changed), ADR-010
§8 (a `refs` global beside `baoSpaceConfig`)
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

What the model did is already on record. Every write a run made is a
span in its trace: the begin record carries the call's `input`
(parameter-keyed, ADR-010 §8), the end record its `output`
(ADR-001 §4), and `effects.query` (ADR-023 §3) reads those records
with the same pipeline language the model uses over datasets. The
window names every past run by its `traceRef`. The index this ADR
wants is a projection of records that exist; nothing has to be
stored to have it.

## Decision

At run start the loop derives a small, named index of what the
conversation touched — the ids from the trace records of the
window's runs, the names from one read of those ids — and binds it
in the kernel as a plain dict. The model refers to things by the
names the user uses; ids stay under the hood. Nothing is written for
this: a ref is a view over the trace, and expires with it. any@v1
does not change.

### 1. A ref

```
refs = {
  "Dune":     {"kind": "object", "type": "book", "spaceId": "...", "objectId": "...",
               "uri": "any://o/<spaceId>/<objectId>"},
  "demo.png": {"kind": "file", "spaceId": "...", "fileId": "...", "on": "<objectId>",
               "uri": "any://f/<spaceId>/<fileId>"},
  "Garden":   {"kind": "space", "spaceId": "..."},
}
```

The key is the object's current name (a file's filename, a space's
name). A ref carries its space, so `spaceId` + `objectId` go
wherever a call takes them. It holds no content — reading the body
is still `get_markdown(space, id)` — and no provenance: "how was
this made" is `effects.query` over the same records (ADR-023 §3).

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

Newest wins a name; the same name in two spaces is kept apart as
`Dune (Garden)` / `Dune (Books)`. The table is capped at ~15 entries
for the line (§3) and ~60 for the dict.

The query is an effect: it is in the trace, so a replay (ADR-001)
sees the same rows. A run whose records retention has dropped
(ADR-023 §6) contributes nothing; the turn text stays in the window
and the model is where it is today for that turn. The trace store is
local to the device, so a second device's bao sees refs only for the
turns it ran itself — the same degradation, never a wrong ref.

### 3. Bind — a plain `refs` dict, names in the message

The loop:

- binds `refs` in the `_ctx` prelude cell next to `baoSpaceConfig`,
  the dict of §1, nothing more — no class, no helper methods;
- adds one line to the user message suffix, names only:
  `[refs: Dune (book, Garden) · Books (folder, Garden) · demo.png
  (file on Dune)]`. No ids in the prompt.

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

### 4. Use — a dict, and one sentence in `_core`

The model uses `refs` as any dict, and calls stay exactly as today:

```python
d = refs["Dune"]
c.update_object(d["spaceId"], d["objectId"], {"book": {"rating": 9}})
llm.read(refs["demo.png"]["uri"], "describe the image")
```

`_core` (ADR-005 §5) gains one sentence: *the `[refs: …]` line on a
message names the things this conversation recently touched; they
are the keys of `refs`, and a ref's `spaceId` and `objectId` go
where a call takes them.* Nothing else is taught: no helper, no
query recipe, no resolution inside any@v1. A name not in the dict
is a plain KeyError; a ref whose object is gone was dropped by the
read (§2) before the run started. Anything beyond the dict — "which
books did we create this week", "how was Dune made" — is
`effects.query` over the same records (ADR-023 §3), already taught.

### 5. Reply chips for what the run changed

The final reply gets attachment chips (the existing ≤32 attachments
map) for objects and files the run created or changed and the reply
did not link — read from the run's own effect records, the same read
the digest makes. The user sees what changed; the next turn's
history carries the links whether or not the model wrote them.

### 6. Worked example

Exchange 1, "Add Dune to my books", view Garden. The message carries
the stamp only — there are no refs yet. The cell
`c.create_object(currentUserSpace, {"name": "Dune", "type": "book"})`
leaves a span whose begin record holds the name and space and whose
end record holds the id and link; the reply is `Added [Dune](…)`.

Exchange 2, the user sends demo.png, "this is the cover". At start
the query over run 1 yields two ids, the read names them `Dune`
(book) and `Garden`; `_ctx` binds `refs`, the message ends with the
stamp and `[refs: Dune (book, Garden) · Garden (space)]`. The cell
`d = refs["Dune"]; c.attach_file(d["spaceId"], d["objectId"],
"demo.png", data)` copies no id. The reply `Attached the cover to
Dune.` carries no link; §5 chips Dune anyway, because the run
changed it.

Exchange 3, "what's in that image?". The window shows the two past
turns exactly as today (stamp, `[ran: any.attach_file×1 · run_…]`,
replies); the current message ends with `[refs: demo.png (file on
Dune) · Dune (book, Garden) · Garden (space)]`. The cell
`llm.read(refs["demo.png"]["uri"], "describe the image")` is where
round 2 failed: the file link was in no reply, the model built a
wrong one.

## Consequences

- Fewer lookup turns and no id copying on the common path ("mark it
  done", "what's in that image", "put Dune under Books").
- Weaker models gain the most: they read a name list and pass a
  name, instead of carrying 60-character strings across messages.
- Nothing new is stored: no dataset field, no field-growth migration,
  no second copy of ids that can drift from the trace. Retention is
  the only housekeeping, and it is already there.
- Names are always current: the read, not the record, names a ref.
- "What exists" and "how it was made" are two projections of the
  same records, and a change to the minting rule reaches the past.
- Nothing new to learn: a dict, and the calls the model makes today.
- Cost: one local aggregate plus one batched read per run start
  (milliseconds), and a refs line of ~50–150 tokens per message
  that rides the message suffix, outside the cached prefix (system
  blocks and boot window), so it never costs a cache write. The
  saving is on the other side: replies and calls that name things
  instead of carrying 40-token ids, and lookups that do not happen —
  the 2026-10-16 replay put the window's stamps and footers at ~2k
  tokens with ids in them; names would roughly halve that.
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
- **A ref where any@v1 takes an object**, or a `Refs` class with
  `where()` filters — sugar over the dict; each adds a surface to
  learn (a resolution step in every object-taking method, or a
  reserved class) for a few tokens per call. Features do not fold
  into any@v1; the dict and `effects.query` cover both.
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
4. The view stamp names a space by id today. With refs in hand the
   stamp could name it (`space: Garden`) and drop the 1.6-chars/token
   ids from the window altogether — decide with §3, since the stamp
   is persisted as `context` and rendered on every past turn.

## Verification

Before accepting: re-run S1–S6 and S10 of the probe suite (the series
where lookups and lost links showed) on sonnet-5 AND one weaker model
(GLM or Kimi via OpenRouter), measuring re-lookup cells, missing-link
turns and cost against round 2. Add a replay check: a run with the
refs query and read replays strict (ADR-028) with the same dict.
