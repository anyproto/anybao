# ADR-019: Instants — native dates in agent data and queries

Status: **Accepted** (2026-08-25)
Date: 2026-08-25
Builds on: ADR-006 §6 (client-boundary normalization), ADR-007
(memory temporal axis), ADR-017 (agent datasets)
Amends when accepted: ADR-006 (new §7 instants), ADR-007 §temporal
(validFrom is an instant), ADR-017 §1 (time fields of the agent
datasets are `datetime`), ADR-016 §stamps wording
Upstream: `any` #179 (SYN-136), `any-sync-sdk` #107 / v0.2.5

## Context

`any` stores time as any-store's native `TypeDateTime`: every
server-derived stamp (`createdAt` / `modifiedAt` on objects, chat
messages and runtime-dataset records) and every property or dataset
field of kind `datetime` reads and writes as `{"$date": "<RFC 3339>"}`
(writes and filter literals also take `{"$date": <unix millis>}`).
Instants are memcmp-orderable, index-keyable, and the input of the
`/aggregate` date operators (`$dateTrunc`, `$year`, `$dateDiff`).

anybao predates this. Its guest code treats every stamp as unix
seconds from `now()`: `min`/`max`/`sorted` over stamps (`rollup`,
`linkgen`, `evolution`, `toolcaller` tool order, `gmailSync` row
picks, `any@v1` pointer ranks), bare-second filter literals
(`recall.by_period`, `reflection`, the `linkgen`/`evolution` cursors)
and epoch formatting (`autorecall`). The agent's own time fields —
`agent_memory_items.validFrom`, `agent_chunks.periodStart`/`periodEnd`
— are declared `number`, so one range query over "everything that
week" spans two literal shapes and merges client-side.

Two hazards make this more than a type update:

1. **A bare literal against an instant does not error.** any-store
   compares by type tag first (`query/filter.go:326`,
   `bytes.Compare` on marshaled values; `TypeNumber`=2 sorts below
   `TypeDateTime`=12): `$gte <number>` matches every row, `$lt` none,
   `$eq`/`$in` (byte-equality) never match. Verified live on `:7005`.
   The wrong answer is also slow — `Comp.IndexBounds` emits the
   number-tagged bound, so the "range scan" walks the whole datetime
   index and the CBO misprices the plan.
2. **Equal stamps hide the crash class.** Python's tuple ordering only
   compares two dicts when they differ, so fresh rigs — every shipped
   tool stamped by one deploy batch, single-row state datasets, empty
   brains — pass every turn and every cron. The failures need a used
   space.

## Decision

### 1. Instants are the only time type crossing the boundary

Every time value a guest reads from or writes to the server is an
instant `{"$date": …}`. Guests never do arithmetic on the raw value
and never put a bare number or string where the server expects an
instant. Two helpers, kernel globals next to `now()` (`runtime/guest/
app.py` — every program, connectors included, has them without a
`use()`):

- `ts_s(v) -> float | None` — the unix seconds of an instant
  (`{"$date": "<RFC 3339>"}` or `{"$date": <millis>}`); a bare number
  passes through unchanged (kind-pinned numeric fields, and chat rows
  materialized by an older peer — the server documents both as
  tolerated shapes); anything else → `None`.
- `instant(seconds: float) -> {"$date": <int millis>}` — the literal
  for writes and filters. Millis, not RFC 3339: no formatting in the
  guest, and `now()` stays what it is (`time.now` keeps returning
  seconds; no host effect is added — ADR-002 isolation unchanged).

Sorting a set of records by a stamp uses `ts_s`; comparing two stamps
uses `ts_s`; rendering a stamp for the model uses `ts_s` then
`datetime`. The host clock and the server clock meet only through
these two functions.

### 2. Agent datasets: time fields are `datetime`

ADR-017 §1 amended: `agent_memory_items.validFrom`,
`agent_chunks.periodStart` and `periodEnd` are declared
`{"kind": "datetime"}`. Stamps (`createdAt` / `modifiedAt`,
`stamp: createTime|modifyTime`) already are — the SDK forces the kind
on stamped fields. `seq`, `level`, `fromSeq`/`toSeq`, `unitsCovered`,
scores and counts stay `number`.

Writers put `instant(...)` in those fields. `memory.remember` defaults
`validFrom` to `instant(now())`; `rollup._emit` writes the chunk period
as instants taken straight from the turns' `createdAt` (no unwrap,
no re-wrap).

Cursors in `agent_job_state` (`linkgen.lastCreatedAt`,
`evolution.lastModifiedAt`) store the newest record's instant
**verbatim** and pass it back as the `$gt` literal. The cursor never
becomes a number.

Field kinds pin at first declaration (`create_dataset` reuses an
existing def by name; the SDK pins kinds per definition), so a space
whose agent stores predate this ADR keeps `number` there. The cut is
clean, per the no-back-compat rule: **such a bao space is wiped** and
re-created — no legacy check in code. Running the new bao against an
old store fails loudly on its first write (`400 dataset.validation:
kind mismatch: got datetime, declared number`, verified on `:7135`),
which is the whole detection. Prod carries no agent data yet
(bring-up pending); the rigs are scratch.

### 3. Temporal recall is one native range query per source

`recall.by_period(from_ts, to_ts)` keeps its seconds signature (the
model's clock is seconds) and turns both bounds into instants once:

```
memory: {"validFrom":   {"$gte": lo, "$lte": hi}}, sort ["validFrom"]
turns:  {"createdAt":   {"$gte": lo, "$lte": hi}}, sort ["createdAt"]
chunks: {"periodStart": {"$lte": hi}, "periodEnd": {"$gte": lo}}
```

with `lo = instant(from_ts)`, `hi = instant(to_ts)`. One literal
shape, index range scans on every source, merge by `ts_s`.
`reflection`'s age gate is `{"createdAt": {"$lt": instant(now() -
minAgeDays*DAY_S)}}`; `linkgen`/`evolution` filter `{"<stamp>": {"$gt":
<cursor instant>}}`.

Because stamps are now real dates server-side, `history@v1` gains
`activity(chat, unit)` — turns per calendar `unit` (`day`|`week`|
`month`) via `/aggregate` `$dateTrunc` over `agent_turns.createdAt` —
the first consumer of date arithmetic in the store and the shape the
boot window's "earlier context" line can be built from later. Nothing
else in ADR-017 §2–§3 changes; `seq` remains the ordering key for
chunk drill-down.

### 4. The client boundary refuses bare time literals

**Amended 2026-09-08 (ADR-027 §2):** the declared-datetime map is keyed by the store KEY (a collection maps back to its key), and the guard runs before the key resolves to a collection, so nothing reaches the wire.

`any@v1` extends its ADR-006 §6 resolution step with a kind check.
While rewriting a filter it knows, per key, whether the target is an
instant:

- objects (`query_objects`): the catalog row's `kind == "datetime"`,
  or the derived stamps `createdAt` / `modifiedAt` (`any.createdAt` →
  `createdAt`);
- datasets (`query`): the declarations `any@v1` owns (§2) plus any
  draft passed through `create_dataset` in this run, plus the
  universal stamp keys `createdAt` / `modifiedAt` / `createTime` /
  `modifyTime` on every dataset (built-ins included: `chat_messages`,
  `email_messages`). Unknown keys pass through — a dynamic dataset's
  free keyspace is the writer's contract.

For such a key, an operand of `$eq`/`$in`/`$gt`/`$gte`/`$lt`/`$lte`
(or a bare value) that is not an instant **raises** `ValueError`
naming the key, its kind and the fix (`instant(seconds)`). Not an
auto-wrap: a bare `1756058400` is ambiguous between seconds and
millis, and rewriting it silently trades a loud wrong answer for a
quiet one. The same check guards `upsert_record` / `upsert_records`
values for declared `datetime` fields. `sort` needs no check —
instants order natively.

The server is the second layer: `checkFilter`
(`internal/server/handlers_query.go`) already parses every filter at
the request boundary and the property / dataset kinds are in memory
there, so a typed `filter.invalid` 400 for a mismatched literal costs
one map lookup per filter key per request. That is upstream work
(ticket below), not a bridge — the client check stays because it
speaks xKeys and fires before the round trip.

### 5. Property formats

`create_type` / `add_property` declare `date` / `datetime` formats
without a `kind` — the server derives `datetime`. `_post_property`
defaults `kind: string` only when no format is present. Values for
such properties are written as instants (`date` = midnight UTC).
`enrich` proposal items stay `kind: string`; apply parses the
reviewed value and writes `instant(...)` when the target property's
kind is `datetime`, the string otherwise (a legacy `kind: string` date
property keeps its ISO convention for life).

### 6. Runtime

`deploy.rs ensure_typed` (the oldest-anchor tiebreak that fixed the
2026-08-12 duplicate-anchor incident) parses `createdAt` as an instant
(RFC 3339 string or millis) with a numeric fallback. Nothing else in
the runtime reads a server stamp; `agent_triggers.spec.at` /
`lastRunAt` / `nextDue` and `agent_trigger_runs.ts` are host-authored
seconds in `dynamic: true` datasets and stay numbers (ADR-006 §4 —
`{"at": now() + delay_s}` remains the reminder recipe).
`FakeSpace` stamps rows and records as instants so the unit suite
runs on the real shape; one legacy-seconds chat message stays in the
backlog test to pin tolerance.

### 7. Prompt surface

`describe()` docstrings (`any@v1` `list_properties` / `create_type` /
`add_property` / `query*`), `_any.md` and `_memory.md` state the
contract in one paragraph each: stamps and date fields are instants;
`ts_s` to read, `instant` to write or filter; a bare number in a date
filter raises. The eval `q25_create_type_kinds` expects a date format
without `kind`.

### 8. Local time for the model

Bao and the client may sit in different zones; that problem is not
solved here. What is fixed: the model never sees UTC where the user
sees local. `time.now` — the one recorded clock — also returns the
host's UTC offset (`offset_s`, plus `tz` when the `TZ` env names it);
no second nondeterministic source. The kernel exposes `tz_offset()`
and `fmt_ts(v, fmt=..., offset_s=None)` — an instant (or seconds)
rendered in that zone with an explicit `+HH:MM` suffix, so the model
knows which zone it is reading. The prompt's `[now: …]` line,
autorecall's "saved <date>" pointers and every date the agent renders
go through `fmt_ts`. Storage stays UTC instants; only rendering is
local. Filters built from a user's "yesterday" use `instant()` on
seconds the model computed from the local `[now: …]` line — the
offset is in the line, so the arithmetic is the model's, not a
hidden conversion.

## Consequences

- One shape for every time on the wire; range queries hit indexes;
  `by_period` is three index scans and a merge instead of a mixed-
  literal query the server could not answer correctly.
- The crash class of §Context 2 is gone by construction: no
  `sorted`/`min`/`max` ever sees a dict.
- A mismatched literal fails fast in the guest with an xKey-level
  message — before the server's own 400 exists.
- Cost: agent stores on pre-ADR spaces are re-declared (rig data
  copied by a scratch program, prod has none). `time.now` is
  unchanged; the seconds↔instant seam is two functions.
- Tests move to the real shape: every fake that stamps `createdAt: 1`
  stamps an instant, the recall filter assertion pins the wrapped
  literal, `deploy.rs` gains the tiebreak test it never had.

## Upstream tickets (to file)

- `any-sync-sdk`: `/upsert` (batch ingest) rejected every `$date`
  form on a declared `datetime` field — `goToAnyenc` converted the
  `encoding/json` map without the extjson wrapper rule `/modify`'s
  fastjson path applies (`kind mismatch: got object, declared
  datetime`, found by `tests/test_instants_integration.py`). Fixed
  locally on `fix/upsert-extjson-date` (single-key `$`-map → the
  fastjson decoder); needs an SDK tag + `any` bump before chunks
  (rollup) write on a released server.

- `any`: schema-aware filter literal validation in `checkFilter` —
  reject `$eq/$in/$gt/$gte/$lt/$lte` operands that are not instants on
  `datetime`-kind properties / dataset fields (`filter.invalid`, path +
  kind + expected shape). ~300 LOC; the write-side twin is
  `propformat.validateFormatSemantics`.
- `any-store`: (a) `Comp.IndexBounds` with a number-tagged bound on a
  datetime-keyed index degrades to a full-range scan and misleads
  `interpolateRangeSel` — a perf bug under the correctness one;
  (b) optional: a per-query cross-type-compare counter on the
  `DocBuffer`, surfaced via `CollectionStats` (~80 LOC, no hot-path
  cost) for consumers without a schema layer.

## Resolved questions

- *Auto-wrap or raise?* Raise (§4) — seconds/millis ambiguity.
- *Millis or RFC 3339 for literals?* Millis — no guest formatting,
  server accepts both, reads come back RFC 3339 either way.
- *Rename the re-kinded fields?* No — clean cut, same keys; the space
  re-declares.
