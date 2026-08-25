# Datetime migration plan — instants replace epoch numbers

Upstream: `any` PR #179 (`d6f02a0`, 2026-08-22, SYN-136) + `any-sync-sdk`
PR #107 (`91dabd0`, SDK v0.2.5). anybao has **zero** `$date` references
today; every timestamp site still assumes a bare unix-seconds number.

## 1. The new contract (what the server now does)

- **Every server-stamped time is an instant**: `{"$date": "<RFC 3339>"}`
  on read. Writes and filter literals accept `{"$date": "<RFC 3339>"}` or
  `{"$date": <unix MILLIS>}` (verified live on `:7005`, both forms filter
  identically).
  - object `createdAt` / `modifiedAt` (row-root stamps)
  - chat `createdAt` / `modifiedAt`, `reactions.<emoji>.<account>` leaves
  - runtime-dataset `createTime` / `modifyTime` stamps — the SDK now
    **forces kind `datetime`** on any field with those stamps, whatever
    the dataset declares
  - tech-space space-list row `createdAt`; but `SpaceInfo.createdAt` on
    `GET /v1/spaces` **stays a plain RFC 3339 string**
- **Filter literals must be wrapped.** A bare number/string does not
  error: instants rank above numbers, so `$gte <number>` matches every
  row, `$lt` matches none, `$eq` never. Verified: `{"modifiedAt":{"$gte":
  1756058400}}` returned all 13 rows on `:7005`.
- **Property formats**: `date`/`datetime` format now implies kind
  `datetime` (`date` = instant at midnight UTC). Passing `kind: "string"`
  keeps the old ISO-string convention. **Kind is pinned at first write**,
  so existing properties keep behaving; only newly created props change.
- **Sorting** on instants is chronological; `$year` / `$dateTrunc` /
  `$dateDiff` now work in `/aggregate` on stamps.
- **Re-index**: SDK handler `LocalVersion` bumped 1→2; existing spaces get
  their derived stamps rewritten by the per-space sweep. While in flight
  a collection can hold both shapes, sorting as two type-grouped blocks.
  Chat stamps from older peers may still arrive as unix seconds —
  clients must tolerate both (`DataVersion` deliberately not gated).
- **any-store v2.0.0 upgrade is one-way**: once a new server opens a data
  dir, an older build refuses it (`btree: database is corrupt`). Back up
  data dirs before pointing a new binary at them.

Guest-side clock is unchanged: `now()` (`time.now` effect,
`runtime/src/broker.rs:531`, `runtime/guest/app.py:62`) returns unix
seconds float. Every break below is "seconds from `now()` meets an
instant from the server".

## 2. Blast radius in anybao

Legend: 💥 = TypeError / crash · 🔇 = silent wrong answer · 📝 = docs/prompt.

**Why nothing visibly breaks on a fresh rig today** (checked on `:7135`,
fresh account @0a5670d, and `:7005`): every 💥 site is a `sorted`/`min`/
`max` over stamps, and Python only raises when it has to compare two
*unequal* dicts. With one row (gmail state/mailbox, single ui_context) or
rows sharing one deploy-batch stamp (all shipped tools) the comparison
short-circuits on `==`. Crons on an empty brain query nothing (`0 turns,
0 cells`). The 🔇 sites need data too: a bare-number `$gt` cursor over an
empty dataset returns `[]` either way. So "ok" traces on a fresh rig are
not evidence — the failures need ≥2 differing stamps or a populated
dataset, which is exactly what a used space has.

### Rust runtime (`runtime/src`)

The runtime never converts server bodies (`sys_http` passes the body
string through, `broker.rs:840`), so only one real site:

| site | class | what happens |
|---|---|---|
| `deploy.rs:574` `ensure_typed` oldest-anchor tiebreak: `r["createdAt"].as_f64()` | 🔇 | every row → `i64::MIN`, tiebreak degrades to smallest id — silently un-fixes the 2026-08-12 duplicate-`agent-triggers`-anchor incident |
| `serve.rs:1192,1562` `sort:["-createdAt"]` on `chat_messages` | ok | sort-only; `snapshot_backlog` walks the window by author, no arithmetic. Depends on the server keeping a total order during the mixed-shape re-index window |
| `triggers.rs` `spec.at`, `lastRunAt`, `nextDue`; `agent_trigger_runs.ts` | ok (latent) | runtime-authored f64 in `dynamic: true` datasets — must **stay numbers**; a writer switching to `$date` makes `once` triggers unarmable (`triggers.rs:340`) |
| `serve.rs:2304` test helper `"createdAt": 1`; `testutil.rs` FakeSpace stamps nothing | test gap | unit suite cannot see the change |
| `drift.rs` / `api/openapi.vendored.json` | blind | object/record/chat stamps are not in the OpenAPI schema; `make api-drift` will not flag this. Re-vendor anyway |

### Guest programs (`repos/_agent`, `repos/_connectors`)

| site | class | note |
|---|---|---|
| `toolcaller@v1.py:334,338` tool ordering `(p["createdAt"] or 0, name)` | 💥 (latent) | standing-prompt build, every turn — but tuple ordering only reaches `dict < dict` when two stamps **differ**; every shipped tool shares one deploy-batch stamp, so fresh rigs (`:7135`, `:7005`, verified 08-25) sail through. The first working-space tool (ADR-013, a different `createdAt`) or a redeploy that re-stamps one program makes the sort raise |
| `rollup@v1.py:67-68` `min/max(t["createdAt"])` | 💥 | enabled L1 cron; crashes after the LLM call, no chunk emitted → re-bills the same batch every run |
| `evolution@v1.py:69` filter `{"modifiedAt":{"$gt": last}}`; `:104` `max(...)` | 🔇 then 💥 | |
| `linkgen@v1.py:84` filter `{"createdAt":{"$gt": last}}`; `:110` `max(...)` | 🔇 then 💥 | hourly cron |
| `reflection@v1.py:55,61` `{"createdAt":{"$lt": cutoff}}` | 🔇 | age gate is the whole selection |
| `decay@v1.py:41` `ts - modifiedAt` | 💥 | trigger ships disabled |
| `recall@v1/program.py:28-30` `_ts()`; `:121-122` `by_period` filter | 🔇 | turns collapse to ts 0; filter matches all/none |
| `autorecall@v1.py:58-62,66,78` `_date()` | 🔇 | every injected history pointer becomes "undated" |
| `any@v1/program.py:200` `_ui_context_rank`; `:972` type-dedup rank | 💥 | only on ties/duplicates |
| `gmailSync@v1/program.py:299,341,858` row sorts on `modifiedAt`/`createdAt` | 💥 | `:299` runs every tick → **whole sync stops** |
| `enrich@v1/program.py:494` writes LLM string into target prop | 🔇 | wrong shape if target is a date-format prop (items are `kind: string`) |
| `any@v1/program.py:1148,1065,1223,1233` docstrings "dates ISO strings", "date⇒string" | 📝 | rendered into the standing prompt — teaches the wrong write shape |
| `skills/_any.md:129,150,156`, `skills/_memory.md:39,48` | 📝 | model does arithmetic / bare filters on stamps |
| `tools/eval/questions/anyv1.jsonl:25` q25 "kind omitted or string" | 📝 | eval encodes old convention |

Must **stay numbers** (kind-pinned, agent-authored): `validFrom`
(`any@v1:103,1536`), `periodStart`/`periodEnd` (`any@v1:157-158`),
`gmailSync.internalDate`, `agent_triggers.spec.at`/`lastRunAt`,
`ui_context.updated_at` (client-written — but `toolcaller@v1.py:120`
should guard in case any-ui re-mints it as datetime).

Unaffected: `extraction`/`history` (seq cursors), all HTTP-wrapper
connectors (attio, granola, intercom, figma, linear, github, gcal,
gsheets, gdrive — provider fields only), `election.rs`, `resolver.rs`.
Nobody reads `reactions` yet.

### Docs

ADR-017 §stamps (`:112-113,141-145`), ADR-016 `:50`, ADR-006 `:292,444`,
ADR-007 `:77,136`, ADR-014 `:88`, ADR-009 `:265`,
`docs/agent-userspace-datasets-plan.md:31` — state stamps without the
shape or as numbers. Per the doc rule: state the current contract, no
"was/now".

## 3. Migration steps (one topic = one commit)

Contract: [ADR-019](adr/019-instants.md) (proposed). Summary of what it fixes:

- Server stamps and date-format props are `{"$date": …}`; guests
  **never do arithmetic on them raw**.
- Two helpers in the any@v1 program (exported, import-free), used by
  every consumer:
  - `ts_s(v) -> float|None` — unwrap `{"$date": str|int}` → unix seconds;
    passthrough for a number (legacy peers / kind-pinned number props);
    `None` otherwise.
  - `instant(seconds: float) -> {"$date": int(seconds*1000)}` — the
    write/filter literal. Millis form avoids RFC 3339 formatting in the
    guest and is verified accepted by the server for filters and writes.
- Agent-authored numeric fields (`validFrom`, `periodStart/End`,
  trigger `at`) stay numbers — pinned, documented, not migrated.
- New `date`/`datetime`-format properties are declared **without**
  `kind` (⇒ datetime) and written as instants; `_post_property` stops
  defaulting `kind: "string"` when a date format is present.

### 1. Guest core: any@v1

- Add `ts_s` / `instant` (+ expose through the flat surface so skills
  can call them from cells).
- Fix `_ui_context_rank` (:200) and type-dedup rank (:972) via `ts_s`.
- `_post_property` (:1233): kind default only when no format.
- Docstrings :1065/:1148/:1223 — write shape `{"$date": …}`; `describe()`
  output is model-facing, so this is also a prompt fix.
- Dataset declarations (:109-110,146,164): leave `stamp:` fields as is
  (SDK forces kind); add a comment that they read back as instants.

### 2. Guest consumers

- `toolcaller@v1.py:334` sort key via `ts_s`; guard `:120` `updatedAt`.
- `rollup@v1.py:67-68` — `ts_s` on turns; keep `periodStart/End`
  numbers (kind-pinned).
- `recall@v1` — `_ts()` unwraps; `by_period` filter on turns uses
  `instant(from_ts)`/`instant(to_ts)` while memory (`validFrom`) and
  chunks (`periodStart/End`) keep numeric literals — three datasets,
  two literal shapes, document it in the docstring.
- `autorecall@v1.py` `_date()` unwraps.
- `evolution`, `linkgen`: filter literal via `instant(last)`, cursor via
  `ts_s(max(...))`, state keeps storing seconds.
- `reflection`: `{"createdAt": {"$lt": instant(cutoff)}}`.
- `decay`: `ts - ts_s(...)`.
- `enrich@v1:494`: when the target property's format is `date`/
  `datetime` and the prop's kind is `datetime`, parse the ISO string and
  write `instant(...)`; otherwise write the string (kind-pinned legacy).
- `gmailSync@v1:299,341,858`: sorts via `ts_s`.

### 3. Runtime

- `deploy.rs:574`: parse `createdAt` as instant (`$date` string → chrono
  RFC 3339, or `$date` millis) with numeric fallback; add a unit test
  with two anchors, mixed shapes, asserting oldest wins.
- `testutil.rs` FakeSpace: stamp `createdAt`/`modifiedAt` as `$date` on
  rows and records so the suite exercises the real shape;
  `serve.rs:2304` `msg()` helper likewise (plus one legacy-int message in
  the backlog test to pin tolerance).
- Re-vendor `api/openapi.vendored.json` from the new server, `make
  api-drift`, refresh `api/coverage.json` fingerprints.

### 4. Skills / prompt / eval

- `_any.md`, `_memory.md`: one paragraph — stamps are `{"$date":…}`,
  use `ts_s`/`instant`, never a bare number in a filter. Sort idioms
  unchanged.
- `tools/eval/questions/anyv1.jsonl:25` q25 → "kind omitted (⇒
  datetime); `kind: string` only for the legacy ISO convention".

### 5. Docs

ADR-016/017 stamp lines, ADR-006 :444, ADR-007, ADR-009, ADR-014,
`agent-userspace-datasets-plan.md` — say "instant" where they name a
stamp. Update `docs/debugging.md` if it shows a stamp.

## 4. Automated testing

### Offline (CI, `uv run pytest` + `cargo test`)

All existing fakes feed ints and will keep passing while prod breaks —
they must be migrated **to the new shape, with one legacy-int case per
consumer** to pin tolerance:

- `tests/test_gated_mechanisms.py` (decay/reflection/evolution): fake
  stamps → `{"$date": ms}`; the fake's `$lt`/`$gt` must implement
  type-rank semantics (instant vs bare number ⇒ match-all / match-none)
  so a regression to a bare literal **fails** the test, not passes it.
- `tests/test_recall_module.py:188` — assert the emitted turn filter is
  `{"createdAt": {"$gte": {"$date": …}, "$lte": {"$date": …}}}` and the
  memory/chunk filters remain numeric.
- `tests/test_rollup_program.py:74`, `test_cognition_programs.py:100,158`,
  `test_toolcaller.py:404`, `test_kernel_introspect.py:130`,
  `test_any_module.py:242,859,1105-1168`, `test_autorecall_module.py:146`
  (`_date({"$date": "2025-07-01T00:00:00Z"}) == "2025-07-01"` and the
  int form).
- `tests/test_any_module.py:565-572` — property with `format date` and no
  kind must **not** get `kind: string`; with explicit `kind: string` it
  must pass through.
- `repos/_connectors/tests/test_gmailsync_program.py:172-510` — FakeAny
  stamps as instants.
- New: `tests/test_instants.py` for `ts_s`/`instant` (str/ms/int/None/
  legacy number, midnight-UTC `date`).
- Rust: `deploy.rs` tiebreak test; FakeSpace stamps; backlog test with
  mixed shapes.
- `tests/test_rt_e2e.py` FakeBackends: stamp `/upsert` echoes with
  `{"$date": …}` so anyrt replay runs see the real shape.

### Integration (`make test-integration`, real server on `:7009`)

Build `~/any/any` at ≥ `d6f02a0` (`bin/any` from 2026-08-24 20:09 already
is); run on a **fresh** data dir (one-way upgrade) and
`ANYBAO_TEST_SERVER=http://127.0.0.1:7009 uv run pytest -m integration`.
Fix the tests that compare stamps directly:

- `tests/test_memory_integration.py:32-39` — `modifiedAt >= before` via
  `ts_s`.
- `tests/test_recall_integration.py:67` — sort key via `ts_s`.
- `tests/test_enrich_integration.py:137` — assert shape
  (`"$date" in f["createdAt"]`), not truthiness.
- Add: a query with a wrapped `$gte` returns a strict subset; a bare
  number returns everything (documents the trap so a server change that
  starts rejecting bare literals is noticed).
- Add: `create_type` with a `date`-format prop, write `instant(...)`,
  read back `$date` at midnight UTC; `$dateTrunc` month aggregate over
  `agent_turns.createdAt` returns real buckets.

### Rig-level (existing rigs, `anyrt serve`)

- Fresh rigs hide the crash class (see §2 note) — seed first: author one
  working-space tool via `programs` (differing `createdAt` ⇒ tool sort),
  hold ≥ one rollup batch of turns, add ≥2 memory items, then run the
  crons.
- `:7005` / `anybao.prod.test.toml` — already on the new server. Run the
  crons once (`anyrt trigger run rollup`, `linkgen`, `evolution`,
  `reflection`) and read the traces: no `TypeError`, cursors advance,
  filters return a subset.
- `:7129/:7019` test rig — **back up the data dirs**, then restart on the
  new binary; watch the SDK re-index sweep finish (mixed-shape window),
  then repeat the cron runs. This is the only place the
  "existing space, old rows re-stamped" path gets exercised before prod.

## 5. Manual testing (with bao, via `check-anybao-changes`)

Deploy to the `_agentrepo-test` / `_connectorsrepo-test` spaces
(through `:7003` by raw id) and talk to the test bao on `:7005`:

1. **Any turn** — the standing prompt builds (tool ordering no longer
   crashes). Confirm in the toolcaller trace that `_tool_docs` ran.
2. "What did we talk about last week?" — `recall.by_period` returns
   turns **and** memory **and** chunks; trace shows the wrapped literal on
   the turns query only.
3. Ask something with related history — autorecall pointers carry a
   real date, not "undated".
4. "Create a type Book with a release date property, add Dune released
   1965-08-01" — property declared without kind; value written as
   `$date` at midnight UTC; read-back shows `{"$date": "1965-08-01T00:00:
   00.000Z"}`; then "which books were released before 1970?" — filter
   literal wrapped, correct answer.
5. Set a `once` reminder ("remind me in 2 minutes") — `spec.at` stays a
   bare number, it fires.
6. `gmail sync_status` / one sync tick — mailbox pick and state row sort
   work.
7. Run the rollup cron twice — second run summarizes nothing new
   (`_covered()` advanced, no re-billing).
8. Chat backlog: stop serve, send 2 messages from the desktop app, start
   serve — both answered, oldest first (sort order across the re-index
   window).
9. Desktop app on the prod network against `:7005`: open an object,
   check created/modified render; edit; modified updates.

## 6. Rollout order

1. ADR-006 amendment (§3.0) → any@v1 helpers (§3.1) → consumers (§3.2)
   → runtime (§3.3) → skills/eval (§3.4) → docs (§3.5), each its own
   commit, tests migrated alongside the consumer they pin.
2. `make test` green offline; integration green on fresh `:7009`.
3. Rig `:7005` manual pass (§5), then `:7129` upgrade-in-place pass.
4. `make runtime` + `make kernel`; restart serve (binary change).
5. Prod: back up `~/.any-repo-prod` and the user data dir; upgrade the
   `:7003` repo server (Aug 21 binary, pre-datetime) **after** deploying
   the fixed `_agentrepo`/`_connectorsrepo`, since the fixed guests
   tolerate both shapes but the old guests crash on the new one.

## 7. Open questions for upstream

- Ordering guarantee across the mixed-shape re-index window for
  `chat_messages -createdAt` (anybao's backlog cut depends on it).
- Should `time.now` grow a millis/instant variant, or is a guest helper
  enough? (Plan assumes helper; no host effect added.)
- `HistoryChange.timestamp` in the OpenAPI spec is still "Unix seconds"
  — confirm intended.
