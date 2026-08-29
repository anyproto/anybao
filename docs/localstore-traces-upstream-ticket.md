# Upstream ticket draft — local store: what trace storage needs

Target: `~/any/any` (after PR #195 — local store — merges). Filed
from anybao ADR-023 (trace records in the local store). Everything
below is additive to `docs/26-local-store.md`; none of it blocks the
first anybao implementation, which works on #195 as shipped. Ordered
by how much each one buys.

## 1. Compression — a measured answer, then maybe a knob

Traces are the most repetitive data a bao writes: every conversation
turn re-sends the same system prompt and boot window (~80 KB, 37
messages) to the model, and every cron tick re-records the same
boot sequence. anybao will content-address blobs (dedupe across runs)
regardless, but the question that decides the blob threshold is
whether any-store compresses pages/values at all.

Ask: state it in `26-local-store.md` (compressed or not, at what
granularity). If it does not, a per-collection `compression: true`
on `PUT /v1/local/collections` — or a note that it is out of scope so
consumers dedupe/compact themselves.

## 2. Expiry — `deleteBefore`-style bulk delete that uses an index

Retention is the first thing a trace store needs and §Out of scope
lists TTL. A full TTL index is not required; what is: `POST
/v1/local/delete {coll, filter}` planning through a range index when
the filter is a range on an indexed field (`{"startedAt": {"$lt":
…}}`), and a response that says how many chunks ran so a housekeeper
can pace itself. Today the doc says matching ids are collected under
one read then removed 256/tx — is the collect step index-backed?

Optional: `expireAfter` on a collection (any-store side) later.

## 3. Cursor paging on `query` for whole-collection-slice reads

Loading one run = `query {filter: {runId}, sort: {seq: 1}}` with up to
a few thousand records; the cap is 1000 with offset paging. For an
immutable run offset is correct but O(n²) on the store side. Ask:
`after: <last id>` keyset paging on `query` (the sort is already on an
indexed field), or lift the cap when `filter` is an equality on a
unique-index prefix.

## 4. Local↔synced `$lookup` — priority vote

Already on the roadmap (gate: any-store `$lookup from` resolution).
The trace use is provenance in one pipeline — `trace_records` `$match
{name: "any.create_object", "output.objectId": X}` → `$lookup` the
object row — and audit joins to `<spaceId>_objects`. anybao ships a
two-step without it; this is a vote for the gate, not a blocker.

## 5. Integers come back as floats

A document inserted with `"fuel_used": 1986942113` reads back as
`"fuel_used": 1.986942113e+09` (small integers stay integers —
`"duration_ms": 6804`). Round-tripping a document through the local
store should not change its number encoding: consumers that `as_i64()`
their own fields silently miss them. anybao folds integral floats on
load; the store should not need it.

## 6. Collection stats

`GET /v1/local/collections` returns names; retention and monitoring
want `{docs, bytes}` per collection (any-store has the counts). One
field each on the listing row.

## Non-asks (checked, fine as is)

- `insert` ≤ 1000 docs / 256 per tx, 1 MiB bodies — a run flushes in
  ≤ 64-record batches; a spilled llm request is one ~80 KB doc.
- Space-scoped collections outliving their space — wanted: a wiped
  bao space keeps its traces until `drop`.
- No subscribe — `trace follow` polls `query`.
- Sink fence — anybao's `trace.query` refuses `$out/$merge` before
  the request leaves the guest anyway.
