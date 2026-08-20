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

## Bundles are for EVERY userspace feature, not just agent_*

(2026-08-20, user-confirmed direction.) Each feature contract gets its
own bundle per space, owned by the program that owns the contract:

| bundle | space | root's derived children |
|---|---|---|
| `bao/v1` | bao | config, secrets, brain, trigger anchor |
| `enrich/v1` | each enriched space | the enrichments hub |
| `email/v1` | Emails space | `sync_state` + one mailbox child per address (seed = address) |

This retires the two tolerated races shipped today: enrich's
`_ensure_store` duplicate hubs ("never auto-deleted") and gmailSync's
oldest-wins mailboxes + the `_ensure_state` "ui-context dance until
derived" comment. Winner via the registry CRDT, loser roots GC-able
(non-derived), restore = re-derive children from the winning rootId,
feature teardown = one cascade delete.

The in-progress upstream bundles update is confirmed (user,
2026-08-20) to land a **generic derived-objects API** — bundle
registration and child derivation for arbitrary bundle ids, replacing
today's compiled-in-only installs (`bao/v1`, `general-chat/v1` are
just rows, not special cases). So userspace registration is the
design, not an ask; the details to watch when it lands: id
namespacing between server- and userspace-owned bundles, and the open
question that type/dataset DEFINITIONS stay ensure-by-name outside
bundles (bundles cover object identity, not type identity) — confirm
the server's type ensure is idempotent under concurrent creates.
Registry read already exists (fenced `bundles` dataset query).

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

## Sequencing (updated 2026-08-20 evening — UNBLOCKED)

The generic bundles HTTP surface LANDED (any `395740d` on #172 + SDK
convergence hardening on #100, merged into both `local-test-all`
branches and live-verified on :7131):

```
POST /v1/spaces/:s/bundles                     adopt-or-install {id, name?, rootTypes?, rootProperties?}
GET  /v1/spaces/:s/bundles[/:id]               list / read (id percent-encoded in paths, verbatim in bodies)
POST /v1/spaces/:s/bundles/:id/resolve         cascade-delete a settled loser {loserRootId}
POST /v1/spaces/:s/bundles/:id/children        derive setup child {seed, types?} — deterministic per (space, root, seed)
```

The server keeps no catalog: every install is client-registered.
anybao ensures `general-chat/v1` at serve boot and watches its
winning `rootId` (150e14e). Retryable states: 409 `bundle.not_ready`
(winner's tree not local), 409 `bundle.loser_not_ready` (resolve
before the loser settled). Verified on :7131: userspace registration
(`probe/v1`, installed:true), idempotent child derivation, chat
adoption (same root, history intact). `search.scope` is adopted
(email scope). **Nothing blocks the migration.**

Next session, in one push:
1. anybao ADR in the ADR-016 mold (types, datasets, field decls,
   record shapes, cutover) — includes the any@v1 guest wrappers
   (`ensure_bundle` / `bundle_child` / `resolve_loser`).
2. Implement agent derived objects + custom agent datasets in bao —
   config + secrets + triggers first (pure storage, children of
   `bao/v1`), turns/chunks as per-chat derived children, memory last
   (widest program surface). enrich/v1 + email/v1 bundles ride the
   same wrappers (§ Bundles above).
3. **A new any PR** removing the hardcoded agent datasets:
   `internal/agent*` + `/agent/*` endpoints + `SpaceInfo` id fields
   + agent chunkers (PR #171 precedent); any-ui trails as with
   enrichment. Clean cut, no data migration.

Still to check during (1): id-namespacing convention between server-
and userspace-owned bundles (server owns none now — likely moot) and
type-ensure idempotency under concurrent creates.
