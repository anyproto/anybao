# ADR-006: Data contracts — turns/chunks v2, config object, triggers

Status: **Proposed** (awaiting review)
Date: 2026-07-07
Builds on: ADR-001..005 (accepted); plan §4b (memory & history), §4
(config effect, triggers); no-backcompat principle

## Context

The shapes anybao writes into `any`. The no-backcompat principle frees
every shape, but `agent_turns`/`agent_chunks` are validated by a
server-side handler (`internal/agentlog`) — shape changes are `any`
work (upstream-fix doctrine, gladly). Legacy datasets are abandoned in
place.

## Decision

### 0. Fresh agent space

anybao boots its own agent space (fresh datasets, zero mixed-shape
risk); the old bao space stays as a readable archive. Name/adoption
rule mirrors v1 (`name == <agent-space-name> && status == active`).

### 1. Turns v2 (`agent_turns`, server changes in `internal/agentlog`)

- **Server-assigned `seq`**: append returns the allocated seq (single
  agent writer per chat in practice; the server allocates from its
  local max — the client probe/retry dance is deleted).
- **`think` gets wired** (was declared-but-never-written): model
  narration/thinking that did NOT go to chat (bubbles are chat
  messages already; `replies` = what the user saw). No more conflating
  narration into `replies`.
- **`stopReason` enum extended**: `done | wrapup | break_soft |
  break_hard | length | error` (ADR-005 outcomes — every invocation
  ends in a recorded, distinguishable way) + boolean `interrupted`.
- **`llm` scalars completed**: `cacheRead`/`cacheWrite` populated from
  usage (dead fields today); plus `costUsd` (computed from tier
  pricing), `fuelUsed`, `cells` (count) — the ADR-003 metrics rollup at
  turn level.
- **`traceRef` replaces `debugRef`**: the per-invocation debug record
  IS the trace (ADR-001 format, one object per run, spilled blobs as
  file attachments). The dc* collector's prose pages are superseded —
  one format for debugging, replay, and retrospection.

### 2. Chunks v2 — hierarchical (`agent_chunks`)

The single-level design's silent ~80-turn horizon is the known scaling
failure. v2 chunk record:

```jsonc
{"seq": 12, "level": 1,                  // 1 = over turns, 2+ = over chunks
 "fromSeq": 40, "toSeq": 49,             // level-1: turn seqs
                                          // level-2+: child CHUNK seqs
 "summary": "...", "periodStart": ..., "periodEnd": ...,
 "unitsCovered": 10}
```

- Same-level, contiguous, non-overlapping ranges; a level-N chunk's
  children are level-N-1 chunks (or turns at level 1). Drill-down is
  recursive: expand a chunk → its children (chunks or raw turns) —
  every summary keeps explicit pointers to what it covers (doctrine).
- **Boot window becomes token-budgeted composition** (assembled by the
  loop, ADR-005 §5): newest raw turns, then newest L1 chunks, then L2,
  ... until the budget fills — ALL history present at decreasing
  resolution; the fixed 8+8 count horizon is gone.
- Rollup (L1 from turns, L2 from L1s, ...) runs as **trigger jobs**
  (§4), off the completion hot path.
- Server validation: append-only stays; add `level` ≥ 1, range checks
  per level.
- **Growth** (reviewed): geometric series — N turns ⇒ ~N/10 L1 +
  ~N/100 L2 + … ≈ **11% record overhead**, depth grows
  logarithmically (at ~50 turns/day, a year ≈ 18k turns → ~1.8k L1 /
  ~180 L2 / ~18 L3 / ~2 L4). Boot window is constant-size by budget;
  summarizer work amortizes to ~11% extra, spread over background
  trigger jobs. The scaling watch-item is summary drift across levels
  (open Q3), not volume.
- **Turns + chunks enter the semantic index** (restores the original
  design — old amemory had `chat_chunk` as a memory category with
  vectors, period fields, and a recall boost; the current
  outside-the-index design silently dropped that). Mechanism: the
  dedicated gated agent-data chunker (docs/13 roadmap item) emits
  scope **`history`** — chunk entries = summary text + `level`/
  `periodStart`/`periodEnd` metadata; turn entries = userText+replies.
  `semsearch(scopes=["agent","history"])` = the old one-surface recall
  over memories AND history. Temporal range queries stay dataset
  queries (indexes exist) unified at the recall-tool level
  (`recall.by_period` fanning across memory + turns + chunks) —
  combining idioms are ADR-007's subject. Storage stays split on
  purpose: memory items evolve, chunks are immutable; chunk-as-memory-
  CATEGORY becomes chunk-as-recall-category.

### 3. Config object (`agent_config` dataset on a derived object)

- Per-space object derived from seed `any/agent-config/v1` (spaceIndex
  pattern). Records keyed by dotted config key
  (`llm.tier.codegen`, `overlays.std`, `loop.max_turns`).
- **Cascade via record-scoped fields** (slice 22): schema declares
  `value` (synced) and `localValue` (ScopeLocal). Resolution:
  `localValue ?? value ?? default`. Device-level overrides never sync;
  space-level values replicate.
- **Account tier deferred**: record-field account scope is
  declared-not-writable in the SDK today (plan §4 caveat) — the
  cascade ships device→space→default now; account slots in when the
  SDK mirror lands (upstream item).
- **Secrets are `localValue`-only, enforced** by the config helper
  (`secret: true` declarations refuse synced writes) and consumed only
  inside effect implementations (ADR-002 `ctx`).

### 4. Triggers (`agent_trigger` type + `trigger_runs` dataset)

- Trigger object properties: `name`, `kind` (`cron | event`), `spec`
  (cron expression | `{dataset, objectId?, filter?}`), `program`
  (ADR-004 spec string), `args`, `owner` (instance UUID), `enabled`,
  `logRuns`, and the observability rollup `lastRunAt / lastDurationMs /
  lastStatus / runCount / lastRunRef` (plan §4b semantics: single-owner,
  at-most-once, boot-disarmed, arm-after-sync).
- `trigger_runs` dataset ON the trigger object: `{ts, durationMs,
  status, error?, traceRef}` — the run's trace in ADR-001 format
  (inline small / file attachment large), keep-last-N retention.
- **Plain user-created type in v2.0** (harness-enforced invariants); a
  server handler (validation, immutable runs) is a later upstream item
  if the honor system proves insufficient.

### 5. `any`-side work list (upstream, sequenced before parity)

1. `internal/agentlog` v2: server-assigned seq, extended stopReason +
   `interrupted`, llm scalar additions, `traceRef`, chunk `level` +
   per-level range validation.
2. Config: none (slice 22 suffices) — account scope later.
3. Triggers: none in v2.0.
4. **Agent-data index chunker** (scope `history`): turns + chunks into
   the search index with level/period metadata — required for the
   unified recall surface (§2), not merely parallel work.
5. (Parallel, §4c): backlinks read surface.

## Consequences

- History scales: token-budgeted window over hierarchical chunks = all
  history at decreasing resolution, drill-down never dead-ends.
- One trace format everywhere: turn debugging, trigger runs, golden
  tests — `traceRef` unifies what were three formats (debug pages,
  turn records, tracer files).
- Config/trigger storage needs zero server work to start.
- The seq collision class, the dead-field debt (`think`, `cache*`),
  and the debug/turn duplication all close in one server pass.

## Open questions (reviewer input wanted)

1. **Fresh space naming**: new name (e.g. "bao2"/"anybao") or reuse
   "bao" with the old space renamed to an archive? Lean: agent keeps
   the name "bao" — the space is an implementation detail; rename old
   to "bao-archive".
2. **`think` content**: store full interim narration + thinking text,
   or a length-capped excerpt (thinking can be long)? Lean: full — it's
   one turn record per invocation, and the keep-all-raw doctrine
   applies to turns.
3. **L2+ rollup summarizer input**: summarize from child *summaries*
   only (cheap, lossy) or child summaries + sampled raw turns (dearer,
   anchored)? Lean: summaries-only for v2.0; anchoring is a tunable
   later.
