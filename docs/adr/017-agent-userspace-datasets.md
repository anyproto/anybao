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

**`agent_config`** — dataset `agent_config`, `idRule: user` (record id
= the dotted config key), `deleteBy: anyone`, **`dynamic: true`**.
Declared fields: `key` (string, `mutableBy: any`), `secret` (boolean,
`mutableBy: any`), `localValue` (string, `scope: local`, `mutableBy:
any`); `value` is UNDECLARED — the HTTP declaration path requires a
kind per declared field, and dynamic-keyspace fields are any-typed and
freely mutable (the DefaultHandler semantics). Device-local values are
strings in practice; a non-string local write rejects loudly. The
ADR-006 §3 cascade (localValue ?? value ?? default) is unchanged;
anyrt already reads/writes this store through the generic query/modify
surface, including the `scope: "local"` route.

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
`modifiedAt`. Search: `{title: context, text: body, scope: "agent"}`.
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
`summary`, `periodStart`/`periodEnd` (number), `fromSeq`/`toSeq`
(number), `unitsCovered` (number). Stamps on both: `creator`,
`createdAt`. Search: turns `{title: userText, text: searchText,
scope: "history"}`; chunks `{title: summary, text: summary, scope:
"history"}`. Recall scopes (`agent`, `history`) are therefore
unchanged for every consumer.

### 2. Client-side seq

`seq` allocation moves to the writer: read the dataset's max seq
(one sorted query, cached per serve session), assign max+1, write
with the zero-padded id. Safe under the single-active-writer contract
(ADR-015 election); a duplicate id upsert is the collision signal
(rejected on the write-once fields), same role the built-in's
"append_only" rejection played.

### 3. any@v1 surface

New flat methods (ADR-010 §8): `ensure_bundle(space, id, name=None,
root_types=None, root_properties=None)`, `list_bundles(space)`,
`get_bundle(space, id)`, `bundle_child(space, bundle_id, seed,
types=None)`, `resolve_loser(space, bundle_id, loser_root_id)` — 1:1
over the server endpoints, ids verbatim in bodies, percent-encoded in
paths by the client.

Kept signatures, reimplemented over the generic surface (programs and
skills keep working unchanged): `get_brain` (resolves the
`bao/brain/v1` child, cached per run), `create_memory` /
`evolve_memory` / `delete_memory` (upsert/delete records on the brain
child + the §1 validation), `append_turn` / `create_chunk` (upsert on
the chat's `bao/log/v1` child with client seq). Dedicated-endpoint
paths inside these methods are deleted, not conditionally kept.

### 4. anyrt (host)

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
