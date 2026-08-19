# ADR-006: Data contracts — turns/chunks v2, config object, triggers

Status: **Accepted** (2026-07-07)
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

### 0. Reuse the bao space; the derived general chat

anybao adopts the EXISTING bao space (review 2026-07-07) — this keeps
the per-space memory brain (shape unchanged), space identity, programs
history. Mixed-shape safety comes one level down: `agent_turns`/
`agent_chunks` live ON the chat object, so anybao gets clean v2
datasets from a fresh chat; the old chat and its v1 datasets stay
archived in place (no-backcompat: never read, never migrated).
Adoption rule mirrors v1 (`name == "bao" && status == active`).

**Amended 2026-07-16 — use the space's derived general chat, not a
self-created one.** The any server now derives exactly one "general"
chat per space (fixed seed `any/general-chat/v1`), materialized on
first sight and reported as `generalChatObjectId` on the single-space
GET (`GET /v1/spaces/{spaceId}` → `anyclient.get_space`; the *list*
route omits it). anybao resolves its chat from that field instead of
find-or-creating a `name=="general"` chat object of its own. This is
the space's one canonical chat, shared with every other client (the
desktop UI, etc.), so the agent and a human land in the same thread —
and the v2-datasets-on-a-fresh-object property still holds because
this space's derived chat is itself fresh. The `--chat-name` serve
flag is gone (no configured chat name to pick). Requires an any server
new enough to derive the field; anybao errors loudly if it's absent
rather than silently minting a private chat.

**Amended 2026-08-19 — resolve the bao space through the derived-space
registry.** The any server (SYN-164) compiles in a registry of
well-known per-account spaces; `bao` derives deterministically from
the account keys and the fixed seed `any/space/bao/v1`, so every
client and device converges on the same space id with no
check-then-create race. `ensure_space` resolution order:

1. `GET /v1/spaces/derived` — if the configured name is a registry
   entry with `created: true`, that spaceId IS the agent space
   (deterministic; kills the name-scan ambiguity once a derived and a
   legacy space share the name `bao`).
2. Registry entry, unmaterialized: `POST /v1/spaces/derived/bao`
   (lazy + idempotent; no `agent_space` flag needed — the config
   object derives on every single-space GET anyway). Registry names
   NEVER name-scan: there is no migration path — a legacy same-named
   space simply stops being the agent space (clean cut,
   no-backcompat; §0's 2026-07-07 adoption rule is superseded for
   registry names).
3. Pre-registry server (404) or non-registry name: the v1 rule —
   name scan, create on miss.

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
- **`traceRef` replaces `debugRef`**: the trace (ADR-001 format, one
  object per run, spilled blobs as file attachments) is the single
  SOURCE OF TRUTH; "the debug log" becomes two VIEWS over it — agent-
  facing (trace queries: `effects.of`, filters by cell/effect/class)
  and human-facing (a renderer/UI view; `#turn_N` anchors resolve to
  the Nth `llm.chat` record). Content-complete by loop purity (system
  prompt, responses, cell code, digests, usage all live inside
  `llm.chat` + `cell` records — ADR-001 §4b). What dies is the
  separately AUTHORED prose page (the dc* duplication); the
  navigability is owed as a viewer, not as a second data format.
  **The viewer owes debug-log parity (revised 2026-07-08)** — what the
  old dc* page carried per turn, `anyrt trace show` renders from the
  trace: run status + wall time + fuel (from the terminal cell record,
  errors in full), per-turn stop_reason / tokens / cacheRead, the cell
  CODE, and the tool_result digest the model saw (mined from the next
  provider request; error results never clipped). Clipped lines are
  locators, `#seq` is the key: `--seq N` dumps one record
  blob-resolved, `--full` lifts clips, `--system` prints the system
  prompt, and `--stats` renders the per-turn metrics table (stop /
  tokens / cacheRead+Write / cells / effects / llm ms, with totals and
  costUsd — priced offline from an embedded model-pricing table keyed
  by the model the trace recorded; amended 2026-07-17. The turn-record
  costUsd scalar stays unpopulated until pricing moves guest-side).
  Turn 1's boot channel is announced in a `boot:` header line (system
  prompt size, boot-window message count, tool names) with `--boot`
  dumping the window verbatim; content clips carry their hidden size
  (`… (+N chars — --full)`) and mined tool results name their source
  record (`result (mined from #seq)`) so `--seq` always reaches the
  full text (amended 2026-07-17). Blob-spilled records resolve through
  the sidecar before rendering. The body is a CHRONOLOGICAL walk — nothing in the trace
  is invisible: loose effects render in place (loop plumbing —
  `trace.*` reads, empty mailbox polls — filtered as noise), facade
  spans (`autorecall.plan`, `memory.save_with_dedup`, …) render as one
  header line plus their own effects and nested llm calls indented
  beneath, and `#turn_N` counts only PARENTLESS `llm.chat` spans — a
  child llm call (the dedup judge) belongs to its facade, not to the
  turn sequence.
  **The viewer also owes the run FINDER (added 2026-07-09)** — the old
  UI's debug-object list (newest first, titled by the chat message).
  `anyrt trace ls` renders it from the traces dir with NO format
  change: one row per run — file mtime as the clock (records are
  deliberately wall-clock-free; replay purity wins), run id, program,
  status/duration from the terminal cell record, parentless-`llm.chat`
  turn count, and turn 1's user text as the title, mined the same way
  `show` mines the first turn. `--program` filters (cron runs drown
  conversations ~25:1), and `trace show` accepts a bare run id
  resolved against `traces/` so `ls` output feeds `show` directly.
  **Traces are DEVICE-LOCAL by default (revised 2026-07-08).** The trace
  (JSONL + blobs) is huge and rarely read; syncing it into `any` would
  bloat the user's synced space for diagnostic data. So it lives on the
  device that ran it (FileSidecarStore is the production store, not just
  dev) and `traceRef` is a LOCAL run id. The lean layer stays synced:
  the turn record (`userText`/`replies`/`effects` one-liners/llm
  scalars), chat, chunks, memory — so "what happened" + bird's-eye view
  work cross-device; only DEEP replay/introspection of an old run is
  device-pinned (on another device `traceRef` dangles; the viewer shows
  "trace ran on another device"). Accepted tradeoff — cross-device deep
  replay is niche, and the synced summary covers the common case.
  Escape hatch: an on-demand **promote** action uploads one trace as an
  `any` object + file attachments (this is what `AnyFileStore`
  becomes — opt-in, not the default). Same progressive-disclosure
  philosophy: lean synced summary + heavy local detail.

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

**Implemented 2026-07-16 (MVP — device-local scope + secrets deferred).**
The config object now exists as a server built-in: the `agent_config`
type (`internal/agentconfig`, a `DefaultHandler` dataset) on an object
derived from seed `any/agent-config/v1`, materialized by the server and
reported as `agentConfigObjectId` on the single-space GET — the same
delivery path as `generalChatObjectId` (unconditional derive in
`spaceToAPI`; object-level `Derive` has no create-free id compute, so
gating on the `agent_space` create flag is deferred). anybao's
`create_space` sends `agent_space: true` to eagerly provision it.

Divergences from the design above, all deferred as follow-ups:
- **Cascade is `space-override ?? default`, not `localValue ?? value ??
  default`.** The harness holds the DEFAULT layer as data — `llm.tier.*`
  in `runtime/src/config_defaults.json` (embedded via `include_str!`,
  seeded by `bootstrap` in `main.rs`; `any.base_url` stays addr-derived)
  — and overlays space-scope override records (`{key, value}` on the
  config object, read at serve start) on top. Record-field *local* scope
  (slice 22) is now wired for secrets (see the 2026-07-17 amendment);
  *account* scope stays deferred until the SDK mirror lands.
- ~~**Secrets stay device-local via env/`secrets` map**, not on the config
  object.~~ **Resolved 2026-07-17 — secrets now persist device-locally on
  the config object** (see the amendment below). The env var seeds the
  store once; later starts read it back.
- The behavioral knobs the design listed as config keys
  (`loop.max_turns` etc.) are deliberately NOT config — they're hardcoded
  module constants + per-run args in `toolcaller@v1.py`.

**Amended 2026-07-17 — device-local secret persistence landed.** The
`localValue ?? value ?? default` cascade's device layer now exists for
secrets, closing the second divergence above. Why it was blocked and what
changed:

- **The blocker was server-side, not the SDK.** The SDK/HTTP `scope:
  "local"` write route is mature (chat's unread flags use it). But the
  `agent_config` dataset shipped schema-less (`DefaultHandler{}`, no
  declared fields) → a *Dynamic* keyspace where undeclared fields default
  to `ScopeSynced`, and the apply path rejects a *local* write to a synced
  field. A device-local field is writable only when the dataset schema
  **declares** it `ScopeLocal`. So the MVP could not persist a secret
  locally without an upstream schema change — cleanly deferred rather than
  worked around (isolation/upstream-fix doctrine).
- **Upstream (`~/any/any`, `internal/agentconfig`):** the dataset now
  declares a schema — `Dynamic: true` retained (undeclared dotted keys
  still work, synced) plus explicit fields: `value` synced (space
  override), `localValue` **`ScopeLocal`** (device-only, never synced),
  `secret` synced marker. `DataVersion` stays `"1"` — additive, no older
  writer rejected, config object not re-minted. A server HTTP test
  (`handlers_agentconfig_scope_test.go`) proves the local write is
  accepted, reads back, mints no DAG change, and is refused on the synced
  route.
- **Harness (`anyrt`):** a `set_local_field` client helper does the
  server-required two-step (a synced upsert materializes the record id
  carrying only `{key, secret}`, then a local `$set` writes the secret
  into `localValue` — local scope cannot create records). `serve`
  bootstraps once: a present device-local value is authoritative (env is a
  noop); an empty store with `ANTHROPIC_API_KEY` in env persists it now;
  both empty warns (serve still starts). The secret still lives only in
  the in-memory `secrets` map at runtime and is never read by cells —
  `localValue` is just its device-local at-rest home instead of the env.

**Amended 2026-07-22 — persistence covers all provider keys.** The
bootstrap above ran for the Anthropic key only; `GEMINI_API_KEY` /
`TOGETHER_API_KEY` were env-per-run, so an embedder launch without env
(e.g. a Dock-launched app) lost web search. `serve` now runs the same
load-authoritative / persist-from-env cycle over every provider ref
(`llm.key.anthropic`, `google.key.gemini`, `llm.key.together` — the
`PROVIDER_SECRET_REFS` table, mirroring `bootstrap_maps`'s env pairs).
Only the required Anthropic key warns when absent; the optional
providers stay silent until their effect needs them.

### 4. Triggers (`agent_trigger` type + `trigger_runs` dataset)

- Trigger object properties: `name`, `kind` (`cron | event | once`),
  `spec` (cron expression | `{dataset, objectId?, filter?}` |
  `{at: <epoch seconds>}`), `program`
  (ADR-004 spec string), `args`, `owner` (instance UUID), `enabled`,
  `logRuns`, and the observability rollup `lastRunAt / lastDurationMs /
  lastStatus / runCount / lastRunRef` (plan §4b semantics: single-owner,
  at-most-once, boot-disarmed, arm-after-sync).
- **`once` kind (amended 2026-08-02, E11)**: fires when `now >= at`
  provided it has never run (`lastRunAt` empty), then auto-disables
  (`enabled: false`) — the record stays as its own audit trail. A
  PAST `at` that never fired fires late on the next tick (a late
  reminder beats a lost one — deliberate inversion of cron's
  missed-occurrence-does-not-exist rule). A failed run consumes the
  shot (at-most-once bias): no retry, the error lives in the run
  record.
- **The dataset is the source of truth (amended 2026-08-02, E11)**:
  the owner reconciles its registry from `agent_triggers` records
  every tick — records it has never seen are parsed and, when
  `owner` is empty or its own, ADOPTED (owner stamped + persisted);
  foreign-owned records are left alone. `enabled` edits on adopted
  records are honored on the next tick. Malformed records are
  skipped loudly (log), never crash the ticker. This is what lets
  the agent CREATE triggers (reminders above all) by writing a
  record — previously the registry only ever held the standing
  built-ins.
- `trigger_runs` dataset ON the trigger object: `{ts, durationMs,
  status, error?, traceRef}` — the run's trace in ADR-001 format
  (inline small / file attachment large), keep-last-N retention.
- **Plain user-created type in v2.0** (harness-enforced invariants); a
  server handler (validation, immutable runs) is a later upstream item
  if the honor system proves insufficient.
- **API surface is a monitoring tool, not just CRUD** (review
  2026-07-07): `create / delete / patch / enable / disable / list /
  get / runs(triggerId)`. **`list` carries enough to monitor without
  opening traces**: definition + owner + enabled + the §4 rollup
  (lastRunAt/lastStatus/lastDurationMs/runCount/lastRunRef) **+
  aggregated resource stats** — `lastFuel`, `lastCostUsd`,
  `lastMemPages`, rolling `avgDurationMs`/`failureRate` (computed from
  the retained trigger_runs window). Run records therefore carry the
  metrics rollup (`fuel`, `costUsd`, `tokens`, `memPages`) alongside
  status/error/traceRef — extracted from the run's trace at write time
  so the list stays one query.
- **Per-trigger resource limits**: the definition gains
  `limits: {fuelPerRun?, timeoutS?, maxCostPerRun?}` — enforced by the
  executor mechanics (ADR-003 fuel/epoch) and the llm effect (cost).
  A background program structurally cannot run away.
- **Circuit breaker**: `maxConsecutiveFailures` (default 3) —
  exceeded ⇒ trigger auto-disables (`enabled: false`,
  `lastStatus: "auto_disabled"`, reason in the last run record);
  re-enable is manual (or an agent proposal). Failures are loud, cheap,
  and self-limiting — background programs stay in sane order by
  construction: budgeted per run (fuel/cost), observable per list
  query, and self-quarantining on repeated failure.

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

### 6. xKey normalization at the client boundary (added 2026-07-17)

**Context.** The `any` server stores and validates typed values by
content-id: a value lives at `record[typeId][propId]`, and
`create_object` / `objects/query` take those ids verbatim — the server
does **not** resolve xKeys (`handlers_objects.go` reads the `types` and
`initialProperties` keys as literal strings; the SDK rejects unknown
ids). So a thin pass-through guest client surfaces raw
`bafyrei…`-shaped type and property ids to the agent, both on read
(query records keyed by type id → prop id) and on write (the agent must
supply ids). The LLM cannot reason about `bafyreicxj2…` where it means
the type `task`; it printed raw ids and guessed at property handles.

**Decision.** The guest `any@v1` client (`programs/any@v1/program.py`)
normalizes to **xKeys** — the stable per-type/per-property slug — at the
client boundary, so the agent reads and writes types and properties by
xKey and never sees a raw content id. This is the bobrik-watch
`anyHelper.js` catalog+resolver ported to the guest client (the same
mechanism, in the same layer — a guest helper, not the host):

- **Per-space catalog**, memoized for the client's lifetime (one cell):
  the type list (`list_types`) + each type's property defs
  (`list_properties`), giving both directions of the xKey↔id map.
  Invalidated after `create_type` / `add_property`; resolvers refresh
  once on a miss so a freshly-created type/prop resolves.
- **Builtin vs user types**: builtins report `xKey == id` (`any`, `nav`,
  `program`, `chat`, …); only user types (CID id, slug xKey) are
  (reverse-)mapped. Reserved namespaces (`any`/`nav`/`program`/`_ver`)
  pass through with their literal keys on both read and write.
  **Amended 2026-08-04 (dev task A19)**: filter/sort/aggregate PATHS
  under `any`/`nav`/`program` no longer pass blind — the tail resolves
  against the builtin's own property catalog (the server exposes it:
  `/types/any/properties`), an unknown tail errors with the list, and
  `any`-group props with `scope: "derived"` (`id`, `author`,
  `createdAt`, …) rewrite to the bare top-level keys records actually
  carry them under (`any.id` → `id`). The server advertises `any.id`
  yet answers the group form with a silent `[]` — two independent runs
  induced that spelling (run_56fa3f8b78794f70, run_84465da8dd7e436a),
  so the mapping layer honors it. `_ver` stays literal.
- **Writes** (`create_object`, `update_object`): `types` entries and
  `initialProperties` / patch groups are named by xKey (ids still
  accepted) and resolved to the ids the server writes by. An unknown
  type or property key **raises** — never silently dropped (a misplaced
  key once lost a whole batch of writes upstream). `update_object`
  resolves every group before issuing any write, so a bad key cannot
  land a partial update.
- **Reads** (`query_objects`, `normalize=True` default): records come
  back xKey-nested — user-type groups keyed by type xKey, their props by
  prop xKey. `filter`/`sort` accept readable xKey paths
  (`task.status`, `-task.priority`) and an `any.types` xKey value, all
  resolved to the server's id paths. `normalize=False` returns the raw
  id-keyed shape for the few internal callers that need the ids
  themselves (graph edges in `recall.neighbors` are identified by
  type/prop id).

**Scope / non-goals.** The `any.types` VALUES inside a normalized
record stay raw type ids (matching bobrik-watch) — they are a builtin
namespace, read by id internally; only the group *keys* are slugged.
The **host** Rust client (`runtime/src/anyapi.rs`) stays a raw
pass-through: it is host-internal (deploy, boot, serve, resolver) and
only ever touches builtin types, so it needs no catalog. Normalization
issues extra `list_types` / `list_properties` http effects; under replay
these are recorded and deterministic (no compatibility concern — no
backcompat, traces regenerate).

## Consequences

- History scales: token-budgeted window over hierarchical chunks = all
  history at decreasing resolution, drill-down never dead-ends.
- One trace format everywhere: turn debugging, trigger runs, golden
  tests — `traceRef` unifies what were three formats (debug pages,
  turn records, tracer files).
- Config/trigger storage needs zero server work to start.
- The seq collision class, the dead-field debt (`think`, `cache*`),
  and the debug/turn duplication all close in one server pass.

## Resolved questions (review 2026-07-07)

1. **Reuse the bao space** — with a fresh chat object supplying clean
   v2 datasets (§0); memory brain and space identity continue.
2. **`think` stored full** — keep-all-raw applies to turns.
3. **L2+ rollups from child summaries only** — anchoring with sampled
   raw turns is a later tunable.
