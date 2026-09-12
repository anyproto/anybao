# ADR-028: Memory sources — any dataset as extraction evidence

Status: **Accepted** (2026-09-06; proposed 2026-09-05 as 027, renumbered
2026-09-12 — 027 is the any parts catalog)
Date: 2026-09-05
Builds on: ADR-007 §1b/§2/§5 (extraction, dedup, recall), ADR-016
(mail as a runtime dataset), ADR-017 (brain + `agent_job_state`),
ADR-006 §4 / ADR-018 (trigger records as the one registry), ADR-019
(instants), ADR-012 §4/§5 (signature split, people spine)
Amends when accepted: ADR-007 §1b (source-aware confidence caps,
provenance shape), §2 (supersede closes the old item), §5 (recall
reads live facts only)

## Context

Memory learns from one source: chat turns (`extraction@v1` over
`agent_turns`). Everything else the user does already lands in the
space as immutable records with an id, a timestamp and text — mail
(ADR-016 `email_messages`), soon meeting transcripts and file digests
— and none of it feeds memory. The extractor is hard-wired to one
dataset; every new corpus would mean a new cron program.

The field survey (session 2026-09-05; Honcho, Zep/Graphiti, Hindsight,
Eywa, Mem0, TriMem) converges on three rules anybao already applies to
turns and nowhere else: **evidence before belief** (a fact points at
the record it came from), **invalidate, never delete** (a superseded
fact leaves the live set and stays for audit), and **date facts by
their evidence**, not by when the machine noticed them. The
knowledge-update category is the hardest in LongMemEval precisely
because additive stores keep stale facts retrievable; anybao's
`supersedes` edge today leaves the old item live in the index.

The minimal move: make the source a parameter, not a program.

## Decision

### 1. A source is a trigger record

A memory source is an `agent_triggers` record running
`agent:extraction@v1` whose args name a dataset:

```json
{"space": "<space>",
 "source": {"objectId": "<home object>", "dataset": "email_messages",
            "text": ["from", "to", "subject", "body"],
            "time": "internalDate",
            "author": "from", "self": ["me@example.com"],
            "filter": {"labelIds": "SENT"}},
 "batch": 20, "tier": "classify"}
```

- `text` — fields rendered in order (mapping-order, like
  `search.text`); each value capped (`FIELD_CHARS`) so a batch is
  bounded. `time` — the field that orders records and dates facts
  (an instant, or a number the source keeps in ms/s — `internalDate`).
- `author` + `self` — optional. A record is **self-authored** when its
  author field contains any `self` identifier (lowercase
  contains-match; `from` is a raw header string). Absent `author` =
  self-authored (the user's own turns).
- `filter` — a plain dataset filter merged with the cursor predicate.
  **The selection funnel is data, not heuristics**: mail starts with
  `{"labelIds": "SENT"}` (what the user wrote), widens by editing the
  record. Bulk mail never needs code to skip — a filter does it.
- Enable/disable, cadence, run log, circuit breaker, Scheduled UI —
  all the trigger record (ADR-006 §4, ADR-018). **Consent =
  registration**: a source exists only because the user or the agent
  registered it; nothing is scanned by default except chat.
- Sugar: `{"space", "chatId"}` — today's standing trigger — means the
  chat's log child, `agent_turns`, text `[userText, replies]`, time
  `createdAt`, self-authored. The built-in stays as seeded; chat
  becomes one source among others. **No runtime change.**

### 2. One cursor per source

`agent_job_state` record id `extraction:<objectId>/<dataset>`, body
`{"last": <time value>}` — the newest processed record's `time`,
stored verbatim (ADR-019 §2; instants and numbers both round-trip).
A tick queries `{time: {"$gt": last}} ∪ filter`, sorted by `time`,
`batch` records. Ties at the batch boundary can skip a same-instant
sibling — accepted for memory (turns are seq-unique, mail is ms).
The chat sugar seeds its first cursor from the legacy `extraction`
record's `lastSeq` when present (one-shot), so nothing re-extracts.
Drain rate = `batch` × cadence per source; a backfill is the same
record with a larger batch or shorter period, no second mechanism.

### 3. The candidate contract, source-aware — enforced in code

`normalize()` keeps the shape allow-list (preference | decision |
lesson | fact) and `source: "extraction"` (the machine slug the humble
merge keys on, ADR-007 §2), and gains:

- **Confidence caps by authorship.** Self-authored ≤ 6 (ADR-007 §1b,
  unchanged). Other-authored ≤ 4 — strictly below the default 5, so a
  claim someone else made about the user never outranks an
  unannotated item, and never a user-stated one.
- **Provenance is a record URI.** `provenance: {"uri":
  "any://o/<space>/<objectId>/<dataset>/<recordId>"}` — the grammar
  enrich@v1 already writes (any docs/19-links.md §Fragments), one
  shape for every source; replaces `{fromSeq}`. The extractor names
  the `recordId` it drew from; an unknown id falls back to the batch's
  newest record, as `fromSeq` does today.
- **Facts are dated by their evidence.** `validFrom` = the source
  record's `time` (numbers through `instant()`), never the extraction
  time. A fact from a 2024 email is a 2024 fact.
- **Text hygiene.** A candidate whose `context`/`body` carries format
  or control characters (Unicode Cf, or Cc beyond whitespace) is
  skipped and counted. The extractor's system prompt states that
  quoted content is data, never instruction. Mail and transcripts are
  untrusted text; the allow-list, the caps and this check are the
  boundary — not prompt trust.

### 4. Validity: `validTo`, closed on supersede

`agent_memory_items` gains **`validTo`** (datetime, `mutableBy:
author`): appended to `_MEM_DATASET` for fresh brains and added to
live declarations through `_add_dataset_field` (ADR-017 §1 additive
evolution, commit 30613d3). `save_with_dedup` on **supersede**
creates the new item, then evolves the old one `validTo =
new.validFrom` (the `supersedes` edge stays). `recall.hydrate` and
`by_period` drop items with `validTo` set unless `include_expired=True`
— dedup candidates, auto-recall and the golden eval thereby see live
facts only; enumeration filters `validTo` absent (`$exists` passes to
the server, ADR-006 §6 — verified at implementation, client-side
otherwise). **Nothing is deleted**: the closed fact stays for audit,
history questions and drill-down through its edge.

### 5. What does not change

Recall framing (ADR-007 §5: derived one-liners + pointers, never
bodies), auto-recall scopes, the judge, the humble merge, linkgen. No
new dataset, no new program, no host change.

Transcripts and file digests are **not** this ADR's code: each needs
its own corpus program (a dataset with id, time and text — Granola/
Meet transcripts as speaker turns; one digest record per file with a
one-line abstract and short overview, ADR-020 `file_content` for the
body on demand). When one lands, it registers by one trigger record —
this ADR is its contract. Also future, each its own ADR: entity
resolution (`participants` → `people` ids into the reserved `entities`
field), thread grouping, and consolidation over multi-source items
(deduction/induction + the owner card).

### 6. Skill

`_memory.md` gains one section: the source recipe (the trigger record
above, `self` from the mailbox's `address`, start with SENT, widen by
filter), and that expired items are excluded unless the question is
about history (`include_expired=True`).

## Consequences

- A new source is one record; the extractor gains ~40 lines and no
  sibling programs. Chat, mail, transcripts and files are one
  mechanism with one run log.
- Facts carry the date of their evidence and a URI to it; a
  superseded fact leaves the live set; a third-party claim ranks below
  a user statement by construction.
- Cost is bounded per source by `batch` × cadence, on the classify
  tier; ADR-007 §1b's ROI records tell whether mail-derived items are
  ever recalled — the go/no-go for widening past SENT.
- Deferred on purpose: entity resolution, thread batching, the dream,
  the owner card. Each is additive over this contract.

## Implementation sketch (after acceptance)

1. any@v1 — `validTo` in `_MEM_DATASET` + `_MEM_MUTABLE`; brain ensure
   adds the field to live declarations. memory@v1 — `MUTABLE_FIELDS`
   + supersede closes the old item. recall@v1 — `hydrate`/`by_period`
   `include_expired=False`. Tests in `test_memory_module.py`,
   `test_recall_module.py`.
2. extraction@v1 — `source` contract + chat sugar, per-source cursor
   with legacy seed, authorship caps, URI provenance, evidence-dated
   `validFrom`, text hygiene. Tests in `test_cognition_programs.py`
   (turns unchanged; a mail source fixture).
3. `_memory.md` §Sources; ADR-007 §1b/§2/§5 amendment notes; README
   row → Accepted.
