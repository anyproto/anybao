# ADR-017: Agent data on userspace datasets, homed on bundle children

Status: ACCEPTED 2026-08-20 (review call: proceed; non-general-chat
logs deferred — options in §0a). Companion `any` PR:
`zarkone/agent-builtins-userspace` (deletes the server's agent data
layer; PR #171 precedent). Upstream contracts: bundles registry +
children (`any` #172/`395740d`, SDK #100), runtime dataset schemas
(SYN-147, ADR-016), `search.scope` (#173/SDK #101). Analysis:
[`docs/agent-userspace-datasets-plan.md`](../agent-userspace-datasets-plan.md).

## Context

The agent's operational data — turns, chunks, memory items, triggers,
config, secrets — lives in five compiled-in server types with custom
handlers, dedicated `/agent/*` endpoints, id delivery on `SpaceInfo`,
and hardcoded index chunkers. Emails (ADR-016) and enrichments proved
the replacement: harness-declared runtime datasets. The bundles
registry supplies what those two still lacked — deterministic,
conflict-resolvable home objects. No backward compatibility: the
companion PR deletes the built-ins; fresh datasets, old data unread.

## Decision

### 0. Home objects — the `bao/v1` bundle and its children

**Amended 2026-09-08 (ADR-027 §1/§2):** the chat's bundle is `system:general-chat/v1` (the catalog's); every store below is declared as a part of its type and addressed by the collection the declaration reports (`AgentStores::*_ds`), never by the bare name.

**Amended 2026-09-17 (ADR-029 §7):** the `bao/v1` root is ensured with `rootType: page` (one type per object; the built-in gives it its body) and filed under no collection; every child names its ONE store type at derivation (`bundle_child(…, type_id)`); the host client has no `attach_type`, `list_collections` reads the collection surface, and the credentials sweep and `ensure_typed` filter on `any.type`.

**Amended 2026-09-08 (memory home):** `bao/v1` exists in the bao space and nowhere else — memory has ONE home. The runtime publishes the bao space id as `runtime.get("bao.space")` (serve; `run --from-space`, or a `bao.space` config key); the guest memory verbs take no space (`get_brain()`, `create_memory(fields)`, `evolve_memory(id, fields)`, `delete_memory(id)`, `memory(c)`), recall's memory source reads that brain whatever space recall is bound to, and the guest `ensure_bundle` refuses the `bao/v1` id. A fact about a user space is a memory item with that space in `context`/`tags`, not a brain in that space (observed 2026-09-08: the model installed `bao/v1` into a user space to repair a `bundle.not_found`).

anyrt registers the **`bao/v1` bundle** at serve boot (the server
keeps no catalog) and derives one child per store:

| child seed | hosts dataset(s) | replaces |
|---|---|---|
| `bao/config/v1` | `agent_config` | config object + `SpaceInfo.agentConfigObjectId` |
| `bao/secrets/v1` | `agent_secrets` | secrets object + `SpaceInfo.agentSecretsObjectId` |
| `bao/brain/v1` | `agent_memory_items` | brain object + `GET /agent/brain` |
| `bao/triggers/v1` | `agent_triggers`, `agent_trigger_runs` | the `ensure_typed` name-queried anchor |

Turn logs stay per-chat: a chat is a bundle root (`general-chat/v1`,
…), and its log is the child **seed `bao/log/v1` of that chat's
bundle**, hosting `agent_turns` + `agent_chunks`. This replaces the
multitype attach onto the chat object; the log cascade-deletes with
its chat. v1 wires the general chat's log; additional chats derive
theirs from their own bundle when the harness grows multi-chat serve.

Children carry their owning type at derivation (`types` on the child
call). Ensure + derive run in one boot pass; `bundle.not_ready`
retries briefly (same policy as the chat ensure).

#### 0a. Deferred: logs for chats that are not bundle roots

v1 wires only the general chat's log. Options for future multi-chat
serve, to be decided when it lands:

- **Every served chat is a bundle root.** Chats register through the
  bundles API anyway (the upstream direction), so each brings its own
  `bao/log/v1` child. Caveat: bundle registry records are permanent
  (record deletes refused), so ephemeral/throwaway chats would leave
  registry rows forever — fine for long-lived named chats, wrong for
  disposable ones.
- **Upstream ask: children of arbitrary objects.** The SDK's
  `DeriveObjectOpts.ParentId` is generic; the HTTP children endpoint
  exposes only bundle winners as parents. A
  `POST …/objects/:id/children {seed, types}` would let any chat
  object parent a log child — per-chat scoping and cascade-delete
  without a registry row.
- **One per-space log home** (`bao/logs/v1` child) with `chatId` on
  every record — needs nothing upstream, but gives up structural
  per-chat scoping and cascade-delete-with-chat; a chat wipe becomes
  a filtered delete-records pass.

### 1. Types and datasets (userspace, ensured by their writers)

**Amended 2026-09-08 (ADR-027 §2):** the names below are dataset KEYS. Each store is one part (`POST …/types/:typeId/parts`, `{key, datasets: [draft]}`, `name` → `key`), idempotent by key; records live in the `collection` the datasets listing reports (`<typeId>_<key>`). Every harness type is `hidden`. The pre-metatype xKey re-claim bridge is gone.

**Amended 2026-09-17 (ADR-029 §7):** `program` and `agent_skill` are LISTED (minted without `hidden`; `hidden: false` healed onto an existing hidden row at ensure) — classes with a body a user may open. The store types stay hidden and bodiless.

Five user types, same type/dataset names as before (the built-ins are
deleted; no coexistence). Declarations use the runtime-dataset field
surface: `stamp` for server-stamped identity/time, `mutableBy` for
write rules, `scope: local` for device-local values, `dynamic: true`
for free-form keyspaces (undeclared fields: any-typed, freely mutable
— declared fields require a kind), `search.scope` for recall scoping.

**Each store is ensured by its writer.** anyrt (host) ensures what it
writes before any guest code can run: `agent_config` + `agent_secrets`
(the boot bootstrap writes synced key records + device-local values
pre-overlay-sync; local-scope writes cannot create records) and
`agent_trigger` (serve arms standing triggers). any@v1 (guest) ensures
what only guest code writes, lazily on first use: `agent_brain` inside
`get_brain`, `agent_log` inside the chat-log resolver — idempotent
`create_type`/`create_dataset`, the enrich/gmailSync pattern.

The same rule covers the two former server builtins that carried code
(amended 2026-08-26): `program` — ensured by `anyrt deploy` in the
target space and by `programs@v1` in the working space (ADR-010 §5,
ADR-013 §1) — and `mini_app`, ensured by `miniapp@v1` (ADR-008 §6).
Both declare their datasets without a `search` mapping: code is never
indexed.

**`agent_config`** — dataset `agent_config`, `idRule: user` (record id
= the dotted config key), `deleteBy: anyone`, **`dynamic: true`**.
Declared field: `key` (string, `mutableBy: any`); `value` is
UNDECLARED — the HTTP declaration path requires a kind per declared
field, and dynamic-keyspace fields are any-typed and freely mutable
(the DefaultHandler semantics). Synced only, no device-local tier
(ADR-006 §3); anyrt reads/writes this store through the generic
query/modify surface.

**`agent_secrets`** — dataset `agent_secrets`, `idRule: user` (record
id = the secret ref), `deleteBy: anyone`. Fields: `key` (string),
`secret` (boolean), `value` (string, `scope: local`). The
effect-boundary guest-read block keys on the dataset name and is
unchanged (ADR-011 §4 stays: values raw, no host checks).

**`agent_brain`** — three datasets. `agent_memory_items`: `idRule: auto`,
`deleteBy: author` (author-only delete, as before). Write-once fields:
`category` (string, required), `context` (string, required),
`validFrom` (number), `chatId` (string), `fromAgent` (string).
Mutable (`mutableBy: author` — the evolve allowlist): `body`, `tags`
(array), `entities` (array), `keywords` (array), `confidence`
(number), `importance` (number), `salience` (number), `accessCount`
(number), `edges` (array). Stamps: `creator`, `createdAt`,
`modifiedAt`. `validFrom` and the stamps are `datetime` instants
(ADR-019 §2). Search: `{title: context, text: body, scope: "agent"}`.
Validation (required fields, 0–10 ranges, defaults, `embeddingRef`
rejection, evolve allowlist beyond what `mutableBy` enforces) moves
into the any@v1 wrappers — the triggers precedent: harness-enforced.
The brain also hosts `agent_job_state` (cron cursors, one record per
job id) and `agent_roi_injections` (autorecall's injection log,
best-effort writes — a stale index hit pointing at a retired store is
skipped, never a failed conversation) — both `idRule: user`,
`deleteBy: anyone`, `dynamic: true`, zero declared fields.

**`agent_trigger`** — datasets `agent_triggers` and
`agent_trigger_runs`, both `idRule: user`, `deleteBy: anyone`,
**`dynamic: true` with zero declared fields** — the whole record shape
(kind/spec/program/args/owner/enabled/limits + the rollup fields)
rides the free keyspace, raw storage exactly as the DefaultHandler
gave; invariants stay harness-enforced (ADR-006 §4 unchanged).

**`agent_log`** — datasets `agent_turns` and `agent_chunks`, both
`idRule: user` (record id = zero-padded seq — lexical order stays
insertion order), `deleteBy: author`, all fields write-once (the
default). Turns: `seq` (number), `fromAgent`, `userName`, `userText`,
`think`, `replies` (array), `effects` (array), `messageIds` (array),
`traceRef`, `interrupted` (boolean), `llm` (object), and
**`searchText`** (string) — client-materialized userText + replies
concatenation (the ADR-016 computed-fields rule; the mapping takes
single fields). Chunks: `seq` (number), `level` (number), `fromAgent`,
`summary`, `periodStart`/`periodEnd` (datetime — the turns' own
`createdAt` instants, ADR-019 §2), `fromSeq`/`toSeq` (number),
`unitsCovered` (number). Stamps on both: `creator`, `createdAt`. Search: turns `{title: userText, text: searchText,
scope: "history"}`; chunks `{title: summary, text: summary, scope:
"history"}`. Recall scopes (`agent`, `history`) are therefore
unchanged for every consumer.

### 2. Client-side seq

`seq` allocation moves to the writer: read the highest record id
ever written — `POST …/query {objectId, dataset, "includeDeleted":
true, "sort": ["-id"], "limit": 1}` (any `docs/09-query.md` §
Tombstones) — assign `int(id) + 1`, write with the zero-padded id.
Tombstones count: a deleted id is burned for good
(`upsert.record_deleted`), and the *live* maximum falls below the
burned ids as soon as any row was deleted, which turned a wiped log
into a permanently failing append (every later turn re-hit the same
tombstone and never advanced). Tombstoned rows come back content-wiped
(`{id, _deletedAt, _ver}`, no `seq`), which is why the probe sorts on
the id — the id IS the seq. Servers without `includeDeleted` are
unsupported (400 `request.unknown_field`; no-backcompat). Safe under
the single-active-writer contract (ADR-015 election); a duplicate id
upsert is the collision signal (rejected on the write-once fields),
same role the built-in's "append_only" rejection played. The host's
`append_interrupted_turn` (ADR-005 §3) runs the same probe.

### 3. any@v1 surface

New flat methods (ADR-010 §8): `ensure_bundle(space, id, name=None,
root_types=None, root_properties=None)`, `list_bundles(space)`,
`get_bundle(space, id)`, `bundle_child(space, bundle_id, seed,
types=None)`, `resolve_loser(space, bundle_id, loser_root_id)` — 1:1
over the server endpoints, ids verbatim in bodies, percent-encoded in
paths by the client.

Declaration tier, `_`-private on the flat surface (ADR-010 §1: program
plumbing, never in the chat agent's inventory): `_list_datasets`,
`_create_dataset`, `_remove_dataset`, and the additive-evolution pair
`_add_dataset_field(space, type_key, dataset_def_id, field)` →
`{fieldDefId}` / `_remove_dataset_field(space, type_key,
dataset_def_id, field_def_id)` — one field in or out of an existing
definition (§1), keeping the declaration's pinned behaviour where
remove + re-declare would drop it. Records stay public (`query`,
`upsert_records`, `delete_records`).

Memory verbs, over the generic surface: `bao_space()` (the runtime-wired
home), `get_brain()` (resolves the bao space's `bao/brain/v1` child,
cached per run), `create_memory(fields)` / `evolve_memory(id, fields)`
/ `delete_memory(id)` (upsert/delete records on the brain child + the
§1 validation) — no space parameter, memory has one home (§0).
`append_turn` / `create_chunk` (upsert on the chat's `bao/log/v1`
child with client seq) keep their space. Dedicated-endpoint
paths inside these methods are deleted, not conditionally kept.

### 4. anyrt (host)

**Amended 2026-09-08 (ADR-027 §2):** `provision_agent_stores` returns the four collections next to the child ids; every host consumer reads them from `AgentStores` / `RunCtx` (`triggers_ds`, `runs_ds`, the config and secret stores' `dataset`); the guest-declared `agent_log` collection is resolved through `store_collection` when the host writes an interrupted turn.

- Boot: register `bao/v1`, derive the config/secrets/triggers
  children, ensure the three host-written types + datasets
  (idempotent; the dataset ensure reconciles mutable `search.*`
  leaves per ADR-016), and stamp display names on the three children
  (`agent-config` / `agent-secrets` / `agent-triggers`; read-first so
  a no-op boot appends no change). The names are UI legibility only —
  every consumer resolves the anchors by bundle seed, never by name
  (a derived child materializes nameless, and name-discovery invites
  duplicates the runtime never reads). Brain and log are guest-owned
  (§1) — the host never touches them.
- Config/secrets resolution: the bundle children replace the
  `SpaceInfo` id fields everywhere (config.rs bootstrap, serve boot).
- Triggers: the anchor is the `bao/triggers/v1` child; `ensure_typed`
  for the anchor is deleted. Trigger args drop `brainId` — cron
  programs resolve the brain themselves via `get_brain`
  (deterministic ids are not passed around).
- The anyapi.rs `/agent/*` client methods are deleted with their
  routes; turns/chunks are guest-written only (toolcaller, rollup).

### 5. What is deleted where

`any` (companion PR): `internal/agentlog|agentmem|agenttrigger|
agentconfig|agentsecrets`, `internal/api/agent.go`, the `/agent/*`
routes + handlers, `SpaceInfo.agentConfigObjectId`/
`agentSecretsObjectId`, `SpaceCreateRequest.agent_space`, the three
agent chunkers, their tests and docs. anybao: every consumer of the
above (host and guest). any-ui trails (docs already point at this
move — `any-ui/docs/derived-bao-space.md`).

### 6. Cutover

No data migration (no-backcompat): new stores start empty; existing
brains/logs on old rigs are unread. Rigs re-arm triggers and re-seed
config/secrets from `.connectors.env` bootstrap as on any fresh
space. Deploy order on a rig: merged `any` (built-ins deleted) →
rebuilt anyrt → repo deploys → serve boot provisions everything.
