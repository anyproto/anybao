# ADR-030: Semantic refs — names for the ids a conversation touched

Status: **Proposed** (2026-09-25), experiment on `exp/semantic-ids`
Date: 2026-09-25
Builds on: ADR-005 §4/§5 (digest, boot window, message suffix),
ADR-006 §1 (agent_turns), ADR-010 §8 (bound cell globals),
ADR-017 §0 (the chat's log child)
Amends (if accepted): ADR-005 §5 (the message suffix gains a refs
line; the final reply gains chips for what the run changed), ADR-010
§8 (a `refs` global; object arguments accept a ref)
Tracks: Linear BOB-160, bullet 3 ("automap ids to semantic meanings");
evidence in `docs/bob-160-probe-results.md` (rounds 1 and 2)

## Context

The kernel lives for one message. Everything a run learned (the id
of the page it just created, the file it attached, the folder it
found) dies with it. The next message starts from the boot window,
which carries each past turn as its user text and its replies, and
nothing else (`history@v1 render_boot_window`). An id survives only
if the model happened to write it into a reply.

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

## Decision

The runtime keeps a small, named index of what the conversation
touched, and hands it back to every run by name. The model refers to
things by the names the user uses; ids stay under the hood.

### 1. A ref

```
{"name": "Dune", "kind": "object" | "file" | "space",
 "type": "book"?, "spaceId": "...", "objectId": "..."?,
 "fileId": "..."?, "uri": "any://o/<spaceId>/<objectId>"}
```

`name` is the object's name (a file's filename, a space's name). The
ref carries its space, so a ref is enough to address the thing.

### 2. Capture — from the run's own effect records

At the end of a run the loop reads its cells' effect records (it
already does for the digest, `trace.effects_of`) and derives refs:

- objects the run **wrote**: create_object, update_object, set_type,
  move_object, trash/restore, add/remove_from_collection,
  append/put/edit_markdown — name from the write input or the
  resolved row;
- files it **attached** (attach_file → the returned uri);
- objects and files its **final reply links** (the same links
  `_auto_attachments` already parses);
- spaces it **addressed by name** (the name → id resolution
  `_resolve_space` already does).

Plain reads (query results, search hits) do not become refs: a
query over 200 rows is not 200 things the user talked about. At most
20 refs per turn, newest first.

### 3. Persist — on the turn record

The refs ride the `agent_turns` record as a `refs` field (the dataset
is dynamic: no declaration change). The turn log already syncs across
devices and survives restarts; refs inherit that.

### 4. Rebind — a `refs` global, names in the message

At run start the loop collects refs from the raw turns in the boot
window (newest wins a name; the same name in two spaces is kept apart
as `Dune (Garden)` / `Dune (Books)`), and:

- binds `refs` in the cell prelude next to `baoSpaceConfig`:
  `refs["Dune"]` → the ref dict;
- adds one line to the user message suffix, names only:
  `[refs: Dune (book, Garden) · Books (folder, Garden) · demo.png
  (file on Dune)]`, capped at ~15 entries. No ids in the prompt.

### 5. Use — any@v1 takes a ref where it takes an object

Every any@v1 call that takes `(spaceConfig, object_id)` also accepts a
ref as the object argument, and a ref as spaceConfig resolves to its
space: `d = refs["Dune"]; c.update_object(d, d, {"book": {"rating":
9}})`. `llm.read(refs["demo.png"], …)` and `attach_file` take a file
ref. A ref whose object is gone fails like a wrong id does (404), and
teach-on-failure (ADR-005 §4) shows the method's doc.

### 6. Reply chips for what the run changed

The final reply gets attachment chips (the existing ≤32 attachments
map) for objects and files the run created or changed and the reply
did not link. The user sees what changed; the next turn's history
carries the links whether or not the model wrote them.

## Consequences

- Fewer lookup turns and no id copying on the common path ("mark it
  done", "what's in that image", "put Dune under Books").
- Weaker models gain the most: they read a name list and pass a
  name, instead of carrying 60-character strings across messages.
- Cost: the refs line is ~50–150 tokens per message; it rides the
  message suffix, outside the cached system blocks.
- Staleness: a renamed object keeps its old name until the next turn
  touches it; a deleted one 404s on use. Both are ordinary errors.
- Chips on every write may be noisy for bulk jobs (40 Sprouts): chips
  beyond a handful collapse into a count, or bulk writes do not chip
  (open question 3).

## Alternatives considered

- **Prompt rules only** — tried in round 2; they don't hold, least of
  all on weaker models.
- **Keep the kernel alive across messages** — variables would
  survive, but a long-lived kernel breaks replay (ADR-001: a run is
  self-contained) and grows without bound.
- **Print ids into replies automatically** — clutters what the user
  reads; chips (§6) carry the same link without the text.
- **Let the model write a scratch note of ids** — the model has to
  remember to do it, the same failure as the prompt rules.

## Open questions

1. Reads: should a single-object read the user asked about ("open
   Dune", get_markdown on one object) become a ref, or writes and
   links only?
2. The ref-as-spaceConfig form (`c.update_object(d, d, …)`) is
   explicit but repetitive. Alternative: object-taking calls accept a
   ref as their FIRST argument with the object argument omitted.
   That changes every signature's shape; decide before §5 lands.
3. Chips for bulk writes: collapse past N, or chip only objects the
   user named?
4. Scope: this chat's turns only (the boot window), or refs from
   other chats of the same bao?

## Verification

Before accepting: re-run S1–S6 and S10 of the probe suite (the series
where lookups and lost links showed) on sonnet-5 AND one weaker model
(GLM or Kimi via OpenRouter), measuring re-lookup cells, missing-link
turns and cost against round 2.
