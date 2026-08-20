# Plan: `agent_*` data moves to userspace datasets on derived objects

Point-in-time note, 2026-08-19 (estimate session against any PR #170 on
the :7129 rig). Feeds a future ADR — nothing here is implemented yet
except the derived bao space itself (ADR-006 §0 amendment, SYN-164).

## Context

- **Done**: the bao space is derived (any PR #170 / SYN-164 registry;
  anyrt resolves it via `GET/POST /v1/spaces/derived` — ADR-006 §0
  amended, no migration path, registry names never name-scan).
- **Upcoming upstream (SYN-163)**: per-space `Bundles` system dataset in
  the space index registering installed bundles; the bao bundle gets
  **one non-derived root ("tsar") object** whose setup objects are
  **derived children** (`DeriveObjectOpts.ParentId`, hashed into the
  child id, cascade-delete with the parent). Non-derived root =
  deletable, so concurrent multi-device installs resolve by CRDT winner
  in the Bundles record + GC of the loser subtree. Restore waits only
  for the space index, then re-derives children.
- **Goal**: move the compiled-in `agent_*` types out of the any server
  (`internal/agentlog`, `agentmem`, `agenttrigger`, `agentconfig`,
  `agentsecrets`) into anybao-owned runtime datasets, the way emails
  (ADR-016 / SYN-147) and enrichments (any PR #171) went. Precedent for
  the server side: PR #171 deleting the enrichment built-ins.

## What the runtime-dataset surface already covers (verified in code)

| need (today's custom handler) | runtime-dataset equivalent |
|---|---|
| device-local values (`agent_config.localValue`, `agent_secrets.value`) | field `scope: local` (`ParseScope`, SDK `internal/schema/dataset.go`); anyrt already writes agent_config through the generic `scope:"local"` modify route |
| server-stamped `creator`/`createdAt`/`modifiedAt` | field `stamp: creator \| createTime \| modifyTime` |
| turns/chunks immutable post-create; memory author-only mutation | `mutableBy: never \| author \| any` |
| search indexing | dataset `search: {title, text}` mapping → `SchemaChunker` (title-boost expressible: title = userText) |
| per-object datasets (turns/chunks per chat) | `object_id`/`dataset` params (ADR-016) |

Moves client-side with ADR-016 precedent (no upstream needed):
server-assigned monotonic `seq` on turns/chunks (single active writer,
ADR-015 election); agentmem validation/defaults/evolve semantics
(harness-enforced — already the stated model for triggers, which have
zero server validation); `SpaceInfo` id delivery (`agentConfigObjectId`
etc.) replaced by derive-or-query at serve boot. The effect-boundary
secrets block keys off dataset name and survives the move.

## Gap 1 — no HTTP surface for derived objects

`Objects().Derive` (seed + `ParentId` + types) is SDK/server-internal
only. **Resolution: ride the SYN-163 PR** — the bao bundle's tsar root
+ derived children is exactly the home-object mechanism:

- brain (memory items), config, secrets, trigger anchor → derived
  children of the tsar root, registered once in `Bundles`;
- per-chat turn/chunk logs → `ParentId`-derived children of each chat
  object (replaces today's multitype `agent_log` attach onto the chat;
  cascade-deletes with the chat). "Derived object on top of a
  non-derived object" in both cases.

Until that PR lands, the only userspace pattern is enrich's
`_ensure_store` (query-by-type, create on miss) — tolerated check-then-
create race, duplicate hubs never auto-deleted. Don't build on it for
agent data; wait for SYN-163.

## Gap 2 — runtime-dataset index scope is pinned to "basic"

Index scopes are an open slug set (`index.ValidScope`), and
**properties** can override scope via `meta["index"] = "<scope>"`
(`internal/index/prop.go`) — but that's the PropChunker path only.
**Runtime dataset records** all index under `ScopeBasic`
(`internal/index/schema.go:233`); the `search` mapping has no scope
field. Today turns/chunks index under scope `history` (TurnChunker) and
memory under `agent` — recall@v1/autorecall@v1 query exactly those
scopes, so moving without this loses recall scoping and pollutes basic
search with raw turns.

**Upstream ask**: `search.scope` on the dataset declaration (small —
open slugs already accepted by the search handler; mutable path like
`search.title`/`search.text`). Possibly also a client-materialized
`searchText` field on turn records (userText + replies concat — the
ADR-016 "computed fields move client-side" rule) since the mapping
takes single fields.

## Per-dataset difficulty

- **agent_config, agent_secrets — easy**: DefaultHandler already, no
  server logic; need only the home object (gap 1). anyrt read/write
  paths mostly generic already.
- **agent_triggers / agent_trigger_runs — easy**: DefaultHandler, zero
  validation, harness owns invariants; needs home object.
- **agent_turns / agent_chunks — medium**: client seq, mutableBy:never,
  per-chat ParentId child (gap 1), index scope (gap 2).
- **agent_memory_items — hardest**: brain home object, recall scope,
  evolve/decay/reflection/memory@v1/recall@v1 all switch from
  `/agent/memory` endpoints to generic dataset ops, validation moves to
  the harness.

## Sequencing (updated 2026-08-20, user-confirmed)

Status: SYN-163 bundles landed the mechanics (SDK #100 + any #172 —
registry, tsar root `bao/v1`, `bundles.Child`) but the root is
deliberately childless and there is NO HTTP surface for deriving
children yet. `search.scope` landed (#173/SDK #101) and bao adopted
it (email scope) — that gap is closed.

1. **WAIT**: the bundles HTTP surface is in progress upstream
   (sdk/any). When it lands, merge those PRs into the local-test
   branches (`local-test-all` in both repos, go.mod replace as now).
2. Then in one push:
   - anybao ADR in the ADR-016 mold (types, datasets, field decls,
     record shapes, cutover);
   - implement agent derived objects + custom agent datasets in bao —
     config + secrets + triggers first (pure storage), turns/chunks
     as per-chat derived children, memory last (widest program
     surface);
   - **a new any PR** removing the hardcoded agent datasets:
     `internal/agent*` + `/agent/*` endpoints + `SpaceInfo` id fields
     + agent chunkers (PR #171 precedent); any-ui trails as with
     enrichment. Clean cut, no data migration.
