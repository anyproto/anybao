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

**Amended 2026-09-08 (ADR-027 §1): the general chat is the catalog's** — `POST /v1/catalog/general-chat/setup` returns the derived root (`system:general-chat/v1`); no registry read, no client ensure, no `general-chat/v1`. The turn log is the `bao/log/v1` child of that bundle.

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
3. Non-registry name: the v1 rule — name scan, create on miss. The
   registry route itself is required (no-backcompat: a server
   without it is unsupported and errors loudly).

**Amended 2026-08-20 (second) — the general chat is the
`general-chat/v1` bundle.** The server keeps no catalog and installs
nothing on its own (SYN-163): a space's one chat is the bundle's root,
and every client lands on it through the bundles registry. anybao
resolves it at serve boot. The bundles route is required
(no-backcompat).

**Amended 2026-08-21 — the chat root is DERIVED (any #177,
SYN-172).** The install is `POST /v1/spaces/:s/bundles {id:
"general-chat/v1", name: "General", rootTypes: ["chat"], "derived":
true}`: the root id is computed from the bundle id, so every device
and member — both sides of a 1-1 included — lands on the same chat
offline and the install can never fork (chat content cannot be merged
across objects, so a fork must be impossible rather than resolvable).
The trade is permanence: a derived root is undeletable. Servers
without any #177 are unsupported.

**Amended 2026-08-25 — read first, ensure on a definitive miss.**
Serve boot reads the registry (`GET /v1/spaces/:s/bundles`, a locked
read: `synced: true` + no row is a definitive miss; `synced: false`
makes absence provisional — re-read, never install, because a derived
ensure on a device that has not yet seen an existing install demotes
that install to a loser irreversibly). A `general-chat/v1` row bound
to a non-derived root is not a general chat under this contract:
serve stops with `derived general chat not found in space <id>:
general-chat/v1 is bound to non-derived chat object <object>` — no
created-root fallback, no migration. Recovery: delete that chat
object (a deleted winning root reads as uninstalled, so the next boot
installs the derived root; the derived bao space itself is
undeletable) or use a fresh account. Same shape as any-ui
(`docs/general-chat-bundle.md`).

**Space create is a this-side-installs case.** Whoever creates a space
installs its general chat in the same breath: any@v1 `create_space`
follows `POST /v1/spaces` with the derived ensure above and returns
the row plus `generalChatId` (any-ui's space create does the same).
A derived install on a fresh owned space needs no locked read — the
id is computed, nothing can be demoted. No chat id rides a space row
(`generalChatObjectId` is not on the wire); `general_chat(space)`
reads the registry.

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
- **`llm` prompt provenance (ADR-005 §5, 2026-09-07)**:
  `promptFingerprint` (16 hex — sha256 of the system block as sent) and
  `soulFingerprint` (16 hex — sha256 of the identity body; absent when
  the run composed no identity). Nested keys of the declared `object`
  field — no dataset field is added (ADR-017 §1).
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
  `anyrt trace ls` renders it from the serve's local store (ADR-023)
  with NO format change: one row per run — the summary's end time as
  the clock, its start while in flight (records are deliberately
  wall-clock-free; replay purity wins), run id, program,
  status/duration from the terminal cell record, parentless-`llm.chat`
  turn count, and turn 1's user text as the title, mined the same way
  `show` mines the first turn. `--program` filters (cron runs drown
  conversations ~25:1), and `trace show` takes the bare run id so
  `ls` output feeds `show` directly.
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

The agent's non-secret configuration — the LLM tiers and the search
providers — lives in the bao space, not in the binary. Secrets live
beside it in `agent_secrets` (ADR-021 §4); the two stores share one
scope and one bootstrap shape.

**What is config, what is not.** Agent config is what programs read
through `config.get`: `llm.tier.*` (ADR-006 §0 tiers), `search.provider.
websearch`, `search.provider.deepresearch`. Runtime wiring is a
different namespace with a different effect: `runtime.get` serves
`any.base_url` (from `addr`) and `overlays.aliases` (from the toml
`[overlays]` table) — per device, from the runtime config, never the
space. The behavioral knobs (`loop.max_turns` etc.) are module
constants in `toolcaller@v1.py`, not config.

**Store.** `agent_config` on the host-written config child of the
bao/v1 bundle (ADR-017 §0): one record per dotted key, `{key, value}`;
`key` declared, `value` on the dynamic keyspace (any-typed), `dynamic:
true`, `DataVersion "1"`. `value` is a synced field in the owner-only
bao space — any-sync encrypts every change with the space read key, so
it is end-to-end encrypted and reaches every device of the account,
exactly like `agent_secrets.value`. There is no device-local tier: a
per-device difference is runtime wiring by definition, and it lives in
the toml. Existing spaces that still carry the retired `localValue` /
`secret` declarations keep them inert — `ensure_dataset` is
create-if-absent and nothing migrates.

**Seeding (serve start)** — the seed passes of `bootstrap_secrets`,
over `agent_config`; nothing is loaded into the host:

1. **Hard seeds** = the config file's `[config]` table: write-through —
   a stored value that differs is overwritten. This is the per-rig
   lever ("this environment runs model X"), the config analogue of
   `.connectors.env`.
2. **Soft seeds** = `runtime/src/config_defaults.json` (embedded via
   `include_str!`): persisted only for keys with no row. They are the
   fresh-space defaults, nothing more — changing the json changes what
   a NEW space gets; an existing space keeps its rows.

**Reads are read-through.** `config.get` queries the row on every
call; the host holds no copy, so a row written by a cell, the UI, or
another device is live for the next cell. A store hiccup fails that
`config.get` loudly (`ConfigError`), per key — never a stale value.
`anyrt run` (offline, no space) answers from the soft seeds (+ its
`--config` file) instead of a store.

**Effects.**
- `config.get {key}` → `{value}` — the row; refuses the secret
  namespaces by name (`llm.key.*`, `connector.key.*`, oauth refs).
- `config.set {key, value}` — mutate, traced (ADR-002): upserts the
  row. Refuses the secret namespaces with `ConfigError`.
- `runtime.get {key}` → `{value}` — runtime wiring (`any.base_url`,
  `overlays.aliases`, `shell` — an object with the shell feature, `null`
  without, ADR-024 §4); `KeyError` for an unknown key.
- `config@v1` (`list/get/set/set_model`) is the agent's tool: `get`,
  `set`, `set_model` over the effects, `list` a plain dataset query
  through `any@v1` (the `bao/config/v1` child of the `bao/v1` bundle).
  A config mini-app is the UI's equivalent.

Server side, `agentconfig` stays a dynamic dataset with a declared
`key` — no upstream change.

### 4. Triggers (`agent_trigger` type + `trigger_runs` dataset)

- Trigger object properties: `name`, `kind` (`cron | event | once`),
  `spec` (cron expression | `{dataset, objectId, spaceId?, filter?}` — event
  delivery per ADR-018 §2 | `{at: <epoch seconds>}`), `program`
  (ADR-004 spec string), `args`, `owner` (the DEVICE PIN — see below),
  `enabled`, `logRuns`, and the observability rollup `lastRunAt /
  lastDurationMs / lastStatus / runCount / lastRunRef` (plan §4b
  semantics: single-owner, at-most-once, boot-disarmed,
  arm-after-sync).
- **`owner` is a device pin (amended 2026-08-24)**: the peer id of the
  device (ADR-015 / SYN-165 devices registry) that runs this trigger —
  stable across restarts, unlike the pre-amendment `anyrt-<pid>`
  stamps, which any reader now treats as UNOWNED (a pid never survives
  a restart; the live incident: every serve restart permanently
  orphaned all adopted triggers). A pinned trigger fires on its device
  whenever that device's serve is up — election-independent (a standby
  bao still fires its pins; only a PRUNED device fires nothing). An
  offline pinned device simply doesn't fire — intended: pins are the
  substrate for remote agent/VM runners. Repin = write `owner` on the
  record (UI or agent); the old device evicts within a tick, the new
  one adopts on its next tick. An empty `owner` is UNASSIGNED — a
  transient state the election-active device claims and stamps on its
  next tick (every trigger is pinned once claimed; moving the active
  bao never moves user triggers — ADR-018 §1). Clearing `owner`
  reassigns the trigger to whoever is active now. When no peer id exists (devices API
  unavailable), the runtime stamps the legacy `anyrt-<pid>` form,
  which stays adoptable across restarts by construction.
- **`once` kind (amended 2026-08-02, E11)**: fires when `now >= at`
  provided it has never run (`lastRunAt` empty), then auto-disables
  (`enabled: false`) — the record stays as its own audit trail. A
  PAST `at` that never fired fires late on the next tick (a late
  reminder beats a lost one — deliberate inversion of cron's
  missed-occurrence-does-not-exist rule). A failed run consumes the
  shot (at-most-once bias): no retry, the error lives in the run
  record.
- **The dataset is the source of truth (amended 2026-08-02 E11;
  reconcile semantics 2026-08-24)**: every tick, each running device
  CONVERGES its in-memory registry on the `agent_triggers` records
  (`triggers::reconcile_registry`):
  - **adopt** — a record pinned to this device, or (election-active
    only) an unowned record, which gets this device's peer id stamped
    + persisted;
  - **refresh** — a definition-core edit (`kind / spec / program /
    args / name / limits / maxConsecutiveFailures`) rebuilds the
    registry entry from the record: crons re-arm strictly forward, and
    a `once` takes the record's `lastRunAt` as its consumed state — so
    rewriting the definition (which drops the rollup) re-arms the
    shot. `enabled` edits alone are honored in place, no re-arm — except a
    false→true flip, which resets the circuit breaker and re-arms
    forward (manual re-enable, same semantics as the control plane's
    `enable`);
  - **evict** — a record repinned to another device, or deleted from
    the dataset, leaves the registry within a tick (delete and repin
    actually work; deleted record ids stay tombstoned server-side, so
    recreating a trigger means a new id).
  Foreign-pinned records are otherwise left alone; malformed records
  are skipped loudly (log) and keep any live entry, never crash the
  ticker. The standing built-ins are code-owned: their record ids are
  ignored by the reconcile and their entries never evicted — they
  follow the ELECTION (ADR-015), not a pin. This is what lets the
  agent CREATE triggers (reminders above all) by writing a record —
  previously the registry only ever held the standing built-ins.
- `trigger_runs` dataset ON the trigger object: `{ts, durationMs,
  status, error?, traceRef}` — the run's trace in ADR-001 format
  (inline small / file attachment large), keep-last-N retention.
- **Plain user-created type in v2.0** (harness-enforced invariants); a
  server handler (validation, immutable runs) is a later upstream item
  if the honor system proves insufficient.
- **API surface is a monitoring tool, not just CRUD** (review
  2026-07-07): `create / delete / patch / enable / disable / list /
  get / runs(triggerId)`. The mutating control-plane routes write
  THROUGH to the dataset record (2026-08-24) — the dataset is the
  source of truth, so a registry-only edit would be reverted by the
  next reconcile tick. **`list` carries enough to monitor without
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
- **Inert definitions are marked, not silent (2026-08-24)**: an
  enabled trigger that can never fire gets a `lastStatus` marker
  stamped onto its record by a per-tick health pass — `invalid_spec`
  (a cron whose spec yields no next occurrence, a `once` without a
  numeric `at`, an event without a `(dataset, objectId)` source, or a
  spec key its kind does not read — cron `cron`/`every_s`, once `at`,
  event `dataset`/`objectId`/`spaceId`/`filter`; cron fields are UTC,
  so an ignored `tz` would fire at the wrong hour unnoticed) or
  `unsupported_source` (an event source this runtime does not deliver
  — ADR-018 §2, v1 delivers `chat_messages` only; the earlier
  `unsupported_kind` marker is retired and cleared on sight).
  Markers self-clear once the definition is fixed and are overwritten
  by the first real run status. Only the owning device judges its own
  enabled entries. The ticker's other silent paths log on TRANSITIONS
  (never per-tick): overlays-not-ready pause/resume, and reconcile
  query failure/recovery — "why isn't it firing" must be answerable
  from the record or the log (the BOB-39 lesson: bao could not
  diagnose a stalled scheduler from inside).
- **Circuit breaker**: `maxConsecutiveFailures` (default 3) —
  exceeded ⇒ trigger auto-disables (`enabled: false`,
  `lastStatus: "auto_disabled"`, reason in the last run record);
  re-enable is manual (or an agent proposal). Failures are loud, cheap,
  and self-limiting — background programs stay in sane order by
  construction: budgeted per run (fuel/cost), observable per list
  query, and self-quarantining on repeated failure.
  *(ADR-023 §8, 2026-08-29: of these only `lastRunAt` and
  `lastStatus` remain on the record — scheduler state and the runner's
  verdict; run history is the `agent_runs` summary per run.)*

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

**Amended 2026-09-17 (ADR-029 §2):** the catalog spans BOTH definition surfaces — types and collections share one handle namespace (`GET …/types` + `GET …/collections`, `includeHidden=true`); the membership slots `any.type` (scalar) and `any.collections` (array) speak xKeys both ways; `collection` is the fourth synthetic row; the BOB-68 row-root guard covers collection handles; a filter / sort / aggregate path `any.types` is a deliberate error naming the two slots (the server answers it with a silent `[]`).

**Amended 2026-09-08 (ADR-027 §2/§3):** the reserved groups are `any` and `_ver`; `nav` is gone; the hidden built-in types (`page`, `miniapp`, `bin`, `dataview`) resolve by their literal id; a dataset argument is a store KEY resolved against the host object's types to the server's collection (a canonical or already-resolved collection passes through; zero or several matches error).

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
  `chat`, `editor`, …); only user types (CID id, slug xKey) are
  (reverse-)mapped. Reserved namespaces (`any`/`nav`/`_ver`) pass
  through with their literal keys on both read and write. `program`
  and `mini_app` are harness-declared USER types (ADR-010 §5, ADR-008
  §6; amended 2026-08-26 — the server builtins are deleted): their
  groups and paths resolve by xKey like any other user type.
  **Amended 2026-08-04 (dev task A19)**: filter/sort/aggregate PATHS
  under `any`/`nav` no longer pass blind — the tail resolves
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

**Amended 2026-08-21 (any PR #176 — meta-type xkey).** The server
stores a type's handle in the meta-type namespace (`type.xkey`); the
catalog carries a third synthetic row `type` (id == xKey == `type`)
next to `any`/`spaceIndex`, and every builtin handle is reserved
against user types (`409 type.xkey_conflict`). Client contract:

- The synthetic rows (`any`, `spaceIndex`, `type`) describe the space
  and are **not attachable** — `create_object` rejects them in
  `types` with an actionable error.
- `create_type` **errors on any builtin-handle collision** (name slug
  or explicit xKey) instead of silently reusing the builtin row — a
  reuse used to hand back the builtin's id and then 400 on the
  property adds.
- A type created before the move lists with **no xKey** (the old
  `any.xkey` row is not read back; property xKeys are unaffected).
  Every ensure path — guest `create_type`, host `ensure_type`
  (serve) and `skill_schema` (deploy) — re-claims the handle **in
  place**: on an xKey miss, an xKey-less row matching by name/slug
  gets one `type.xkey` property write
  (`POST …/properties/{typeId}/set/type`) and is reused, never
  shadowed by a duplicate type.
- **Amended 2026-09-12 (BOB-68).** The derived `any` props are
  record-ROOT keys (`id`, `author`, `createdAt`, `modifiedAt`,
  `modifiedBy`, `spaceId`), and a normalized read places every
  user-type group at that same level under its xKey — so a type whose
  xKey equals one of them shadows the record's own field, silently,
  on read only. `create_type` refuses to MINT a type under such an
  xKey (name slug or explicit; the static set plus whatever the
  space's `any` catalog reports as scope `derived`) with an error that
  keeps the display name and asks for an explicit xKey
  (`author_type`); an existing type is reused under whatever handle it
  has — the guard is on the create path only, so ensuring a type never
  reads the catalog and never locks a pre-rule type out of reshaping.
  The server does not collide (it keys groups by CID) and stays
  unchanged.

**Amended 2026-08-27 (ADR-022).** The property handle is the xKey
only when it is unique on its type and not any-ui's kind marker
(`select`/`tags`/`links`/…), else the name; ambiguity errors. Values
are encoded against the definition on write (option names → keys,
object names → `any://` refs, dates → instants, `None` → `$unset`)
and hydrated on read (option names, `{id, name, types}` link stubs) —
the no-silent-drop rule now covers values. Contract: ADR-022.

**Scope / non-goals.** The `any.types` VALUES inside a normalized
record stay raw type ids (matching bobrik-watch) — they are a builtin
namespace, read by id internally; only the group *keys* are slugged.
The **host** Rust client (`runtime/src/anyapi.rs`) stays a raw
pass-through: it is host-internal (deploy, boot, serve, resolver) and
carries no general catalog — the few user types the host writes or
reads (`agent_skill`, the ADR-017 stores, `program`) each resolve their
own `{typeId, propIds}` by xKey at the call site (`skill_schema`,
`serve::ensure_type`, `program_schema::ProgramSchema`). Normalization
issues extra `list_types` / `list_properties` http effects; under replay
these are recorded and deterministic (no compatibility concern — no
backcompat, traces regenerate).

### 7. Instants (added 2026-08-25, ADR-019)

Every server time — the derived `createdAt`/`modifiedAt` stamps, chat
stamps, runtime-dataset `createTime`/`modifyTime`, and every property
or field of kind `datetime` — crosses the boundary as an instant
`{"$date": …}`. Guests read it through `ts_s`, write and filter it
through `instant`; the `any@v1` client refuses a bare literal against
such a key. The trigger spec's `{at: <epoch seconds>}` (§4) and the
other host-authored numbers in `dynamic` datasets are not instants
and stay numbers. Contract and rationale: ADR-019.

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
