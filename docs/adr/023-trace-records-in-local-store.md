# ADR-023: Trace records in the `any` local store

Status: **Accepted** (2026-08-29; user go-ahead on the proposal — implementation follows the sketch below, one topic per commit)
Date: 2026-08-29
Builds on: ADR-001 §8 (`TraceStore` — storage is one trait, the file
layout is one impl), ADR-003 §4 (guest trace views, `run=`), ADR-017
(bundle-child stores for agent data), ADR-006 §4 (trigger records and
their audit fields)
Amends when accepted: ADR-001 §7 (blob sidecar → blob collection),
ADR-006 §4 (`lastRun*` become a view over the run summary),
ADR-003 §4 (`effects.query`)
Upstream: `~/any/any` PR #195 — local store (`docs/26-local-store.md`,
`/v1/local/*`); gated follow-ups in `docs/07-roadmap.md`; the asks this
ADR adds are drafted in [`../localstore-traces-upstream-ticket.md`](../localstore-traces-upstream-ticket.md)

## Context

A run's trace is the only record of what the agent did (ADR-001: if it
isn't in the trace, it didn't happen). Today it is one
`run_<id>.jsonl` per run plus a `.jsonl.blobs` sidecar in a
device-local directory, behind the `TraceStore` trait (ADR-001 §8),
and the guest reads any run of this bao through `effects.of/get/runs/
stats` (ADR-003 §4).

The staging e2e of those views (2026-08-28, findings in the
`tracepeek` space) showed where files stop:

- **The finder is the weak half.** Everything bao needed *inside* one
  run — outline, drill, one record — is a Python walk over that run's
  records and works. Everything *across* runs — "which run created
  object X", "what wrote to space S this week", "failed runs of
  program P", "cost per day" — is either impossible (open every file)
  or a client-side scan (`effects.runs` = program substring +
  Python filtering; a day summary took 16 `stats` + 32 outline reads).
- **Audit fields drift from the truth.** Trigger records carry
  `lastRunRef/lastRunAt/lastStatus/runCount` as a hand-rolled cache of
  the trace store; on staging they read null / `6` against 642 runs in
  the store. Bao reasoned from the cache.
- **Files grow unbounded** (4,229 runs on one dev box) with no
  retention, no compression, no query.

`any` PR #195 lands the missing primitive: **local store** — plain
any-store collections in the SDK's `sdk.db`, account- or space-scoped,
never synced, no DAG, no `_ver`, no subscribe, with the full any-store
surface: filters, sort, range indexes, modifiers, aggregation
pipelines (`$match/$group/$unwind/$project/$facet/$lookup/$out/$merge`
…), 256-doc write chunks, 1 MiB bodies, `query` limit cap 1000 with
offset paging, delete by filter. It speaks the same query language as
the synced data (`docs/09-query.md`, `docs/14-aggregation.md`) — the
language bao already uses through `any@v1.query/aggregate`.

Two things the local store is *not* settle the shape here: it is not
synced (each device sees only its own collections), and it is not
independently backed up (it lives in `sdk.db`). Both fit traces —
traces are per-device by nature, immutable once written, and
regenerable in the sense that matters (the next run makes a new one).

The `any` server is inseparable from a bao: every run already reads
and writes it through the effect boundary. Making the trace write
depend on it is not a new dependency.

## Decision

### 1. Two stores behind one seam

- **Bodies — local, per device.** Trace records and blobs go to local
  collections scoped to the **bao space** (`scope: "space"`, so
  `l_s_<baoSpace>_…`), through a second `TraceStore` impl,
  `AnyTraceStore`. The `TraceStore` contract (ADR-001 §8) does not
  change; `FileTraceStore` stays for `anyrt run` offline, tests, and
  replay fixtures. Serve defaults to `AnyTraceStore`;
  `[traces] backend = "any" | "file"` in the host config selects.
- **Summaries — synced, one record per run.** A small `agent_runs`
  record per run (`{runId, program, device, startedAt, endedAt,
  durationMs, status, errorType, turns, cells, effects, mutations,
  tokens{in,out,cacheRead,cacheWrite}, costUsd, model, title,
  triggerId?}`) lands in a bundle-child dataset of the bao space
  (ADR-017 shape) — the only trace data that replicates, so every
  device sees every device's runs, `agent_turns.traceRef` resolves
  everywhere, and the finder is cross-device. It is also **mirrored
  into a local collection** (`trace_runs`) so local↔local `$lookup`
  (available today) joins records to their run; local↔synced lookup
  is gated upstream and not required.
- CRDT/merge is deliberately not used for bodies: a run has exactly
  one writer and is append-only, so there is nothing to merge and
  the change tree would only cost. Summaries are distinct `runId`s
  from distinct devices — they merge without conflict for free.

### 2. Collections and documents

| collection (name; `l_s_<bao>_` prefix applied by the server) | document | indexes |
|---|---|---|
| `trace_records` | one trace record (ADR-001 §2 shape, unchanged) + `runId`; `id = "<runId>:<seq zero-padded 6>"` | `(runId, seq)` unique; `name`; `effect`; `error.type` sparse; `meta.class` |
| `trace_blobs` | `{id: <sha256 hash>, bytes, data}` (§7 spill) | primary key only |
| `trace_runs` | the `agent_runs` summary, local mirror, `id = runId`; `expired: true` once retention dropped the body | `program`; `startedAt`; `status`; `mutations` |

Records stay **time-free** (ADR-001 §2): wall-clock lives on the run
summary (`startedAt/endedAt`), and every time-scoped question is a
join or a two-step (`trace_runs` filter → `runId` list → records).
`device` is on the summary; bodies are per device by construction.

Space scope means the collections share the bao's lifetime; a wiped
bao space leaves them behind (local-store rule) and `drop` is the
cleanup. Collection names are not versioned — the record schema is
pinned by `header.schema` per run, as on disk.

### 3. Write path

- `open_sink(run)` inserts the header; every later record appends to
  an in-memory batch that flushes on `span.end`, `cell`, every 64
  records, and at run end (`/v1/local/insert`, ≤ 256/tx). Blobs go to
  `trace_blobs` at spill time via `upsert` (content-addressed:
  the 37-message boot window repeated on every turn of a
  conversation spills once).
- A flush failure is **surfaced, not swallowed**: the writer degrades
  to buffered like today, `dump()` retries the whole run once, and a
  run whose trace could not be landed ends `error` with
  `trace.unpersisted` — never a silent gap (the any server being down
  already fails the run's effects; this only makes the trace
  consistent with that).
- The live views (`effects.of/get` without `run=`) keep reading the
  in-memory log; nothing on the hot read path touches the server.
- At run end the summary is computed host-side (the `stats_data` +
  `ls_row` logic already in `view.rs`) and written twice: the synced
  `agent_runs` record and the local `trace_runs` mirror. The trigger
  runner (ADR-006 §4) stops stamping `lastRun*` itself — see §8.

### 4. Blobs

The §7 spill threshold stays (64 KB). What changes is where the bytes
live (a content-addressed collection instead of a per-run sidecar) and
that identical blobs dedupe across runs. Blob content is **not**
queryable by design — the request bodies the model saw are the least
query-worthy bytes and the largest; `effects.get` re-hydrates them as
today. Whether any-store compresses pages is an open upstream
question (ticket §2); if it does not, the dedupe alone removes most of
the repetition.

### 5. Reads and the query surface

- `effects.of/get/runs/stats` keep their shapes (ADR-003 §4).
  `load(run)` = `query {filter: {runId}, sort: {seq: 1}, limit: 1000}`
  with offset paging; `list()` = `trace_runs` sorted by `startedAt`.
- `effects.runs(filter=…, sort=…, limit=…)` gains the any-store
  filter form over the summary (`{"program": "extraction", "startedAt":
  {"$gte": …}, "mutations": {"$gt": 0}}`); the substring `program`
  argument stays as sugar.
- New: **`effects.query(pipeline, coll="records" | "runs" | "blobs")`**
  → `/v1/local/aggregate` on the named trace collection. The syscall
  (`trace.query`, class read) pins `coll` to the three trace
  collections; the guest never names a storage collection. Sinks
  (`$out/$merge`) are refused anywhere in the pipeline, `$facet`
  branches included — the guest reads traces, it does not write them.
  any-store names a `$group` row's key `id` (not `_id`); the docstring
  says so.
- Skill guidance (`_core.md`, past-runs paragraph) gains the three
  recipes the e2e asked for: provenance (`$match {name:
  "any.create_object", "output.objectId": X}`), audit (`$match
  {"meta.class": "mutate"}` → `$lookup trace_runs` → `$group` by
  program/day), failures (`$match {"error.type": {$exists: true}}` →
  `$group` by type). The rule from ADR-003 §4 stands: store-wide
  claims come from a query, never from a record field.

### 6. Retention

Host housekeeping in serve (a ticker, not a guest program — it writes
the trace store): `[traces] retain_conversations` (chat-loop runs,
**default 60d**) and `retain_jobs` (every other program, **default
30d**); `"never"` keeps forever. Two classes, not per program — the
summary carries `program`, so a per-program table is a later
refinement if a need appears; "keep failed jobs longer" is the more
likely first one.
Expiry deletes `trace_records` by `runId` filter (200 runs per
delete), then blobs no surviving record references (the live refs are
collected with one `$exists` query — local↔local `$lookup` was not
needed), and marks the `trace_runs` mirror row `expired`. **Summaries
are kept forever**, synced and mirrored — small, and what "did it run"
questions and `traceRef` links resolve against after the body is gone
(`effects.of/get/stats` on an expired run answer a typed error naming
retention and pointing at `effects.runs`). The file backend has no
retention (the jsonl dir is a dev/offline store).

### 7. CLI and tooling

`anyrt trace ls/show/follow/stats` take `--addr` (default from the
config file, like `serve`) and read through `AnyTraceStore`; a path or
`--traces-dir` argument selects `FileTraceStore` as today. `follow`
polls `query` (no subscribe on local collections). `anyrt trace import
<dir>` loads existing `.jsonl` files once (records + blobs + a
synthesized summary) so history survives the switch.

### 8. Trigger audit fields become a view (ADR-006 §4 amendment)

`lastStatus / lastRunAt / lastRunRef / consecutiveFailures` on a
trigger record are derived from `agent_runs` (`triggerId` = the
trigger id) at read time in `any@v1`'s trigger reads and in the UI;
the runner no longer stamps them and `runCount` is dropped. One source
of truth ends the drift class the e2e found (Issue 4 in `tracepeek`).
`consecutiveFailures` stays runner-owned only as scheduling state.

*Implementation phase (2026-08-29):* the host publishes every run's
summary to `agent_runs` **with `triggerId`** (the chat responder's id
on conversation runs, null on control/embedder runs), and the guest
reader has switched: the skill's "did it run?" is
`effects.runs(filter={"triggerId": …})` (this device) / the synced
`agent_runs` rows (other devices). The runner's `lastRun*` stamps
**stay** as a cache until any-ui's Scheduled view reads `agent_runs`
— removing them first would blank that view. That any-ui change, and
then the stamp removal + `runCount` drop here, is the remaining step
(a task in the dev space, not tracked here).

### 9. Isolation

The host writes the trace collections; the guest reads them through
`trace.*` syscalls only. Guest `http.*` calls to `/v1/local/*` are
classified by the existing route rules (`query`/`aggregate`/`get` →
read `data.read`; `insert/upsert/update/delete/collections` → mutate
`data.write`), and the broker's request guard (the `secrets_guard`
mechanism, ADR-021) additionally refuses guest requests naming a
`trace_*` collection of the bao space — trace integrity is a host
property, not a capability grant.

## Consequences

- One query language for data and traces; provenance/audit/failure
  questions become one call instead of a file sweep; day summaries
  need no per-run reads (Issues 5, 7 partly, and 4 in `tracepeek`
  close by construction).
- The finder is cross-device through the synced summary; deep reads
  stay per device (the body is only where it was written). A
  cross-device deep read is a future `TraceStore` concern, not this
  ADR's.
- Run correctness now includes "the trace landed" — a bao whose any
  server cannot take writes fails runs explicitly instead of leaving
  files behind. Accepted: the server is inseparable from the bao.
- `sdk.db` holds the traces; there is no backup story and a manual
  wipe of `sdk/` loses bodies (summaries survive). Same standing as
  the local store itself.
- Two backends to keep green: the replay fixtures and offline `run`
  pin `FileTraceStore`; serve pins `AnyTraceStore`; the trait's tests
  run against both.

## Open questions (to settle before Accepted)

1. **Compression** in any-store — measured, not assumed; drives
   whether the blob threshold moves.
2. **Local↔synced `$lookup`** (roadmap gate) — only needed to join a
   trace record straight to an object row; the two-step works.
3. **Per-device summary volume** — a bao with several devices writes
   several summaries per cron tick into one synced dataset; fine at
   today's cadence, check before prod cutover.
4. Whether `trace_records` should carry `program` denormalized (saves
   the join for the most common `$match`) — leaning yes, one string.

## Implementation sketch (after acceptance)

1. `anyapi.rs`: local-store client methods (`local_ensure`, `insert`,
   `upsert`, `query`, `aggregate`, `delete`, `collections`).
2. `tracestore.rs`: `AnyTraceStore` (sink batching, blob upsert,
   paging loads, summary write); trait tests parameterized over both
   impls; `[traces]` config.
3. `broker.rs`: `trace.query` syscall + collection pin; `run=`
   reads through the store (unchanged API).
4. Guest `app.py`: `effects.query`, `effects.runs(filter=…)`;
   `_core.md` recipes.
5. Serve: summary write at run end, retention ticker, trigger reads
   through `agent_runs`; `trace import`.
6. Staging e2e re-run of the `tracepeek` cases 2, 6, 8 + the new
   provenance/audit/failure recipes.
