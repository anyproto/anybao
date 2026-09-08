# Port plan — any "parts / modules / bundles / catalog" break (any a176029 → 5d709c8)

Status: 2026-09-08 — ADR-027 accepted; Phases 2 (runtime), 3 (guest,
programs, skills) and 4 (tests) DONE; the §5.1 matrix rows 1–4, 6, 7,
11, 15 are pinned by `tests/test_integration.py` + the live
conversation (10/10 integration tests green on :7142); rows 5, 8–10,
12, 13 and §5.3 (any-ui coexistence) remain for the cutover rehearsal;
branch
`feat/any-parts-catalog-port`. DECIDED (user, 2026-09-08): **clean
cut, no migration of anything** — old chat roots, old collections and
old property definitions are left behind unread; the code and docs
carry no bridges, no fallbacks and no mention of the former shapes.
§2.1 and §2.3 are settled by that call; the export in §2.3 is the
user's to run or skip.
Owner: anybao. Pin today: any `a176029` (`api/openapi.vendored.json`,
`docs/api-parity.md` § C4). any main is 116 commits ahead; the
website tutorial (`any/website/03-tutorial/*`, commit 5d709c8) and
`any/docs/29-client-model.md` describe the target model.

This document is the plan only. The contract changes it proposes land
as ADR amendments in the same commits as the code (ADR README working
rule); §7 lists which ADRs.

## 0. What broke, in one screen

any replaced its object model underneath every client. Five contract
breaks hit anybao directly; two more are operational.

| # | any change | anybao today | new server answers |
|---|---|---|---|
| 1 | `chat` is no longer a type; it is a **reserved module**. The space's chat is the catalog usecase `general-chat` → bundle `system:general-chat/v1` (derived, hidden, self-typed `general_chat`, `miniapp` carrier) | `ensure_bundle("general-chat/v1", rootTypes=["chat"], derived)` at serve boot and in `create_space`; `general_chat()` = `get_bundle("general-chat/v1")` | `400 type.not_found` on the ensure (no `chat` type). On an EXISTING space the old `general-chat/v1` root stays, still takes writes, but any-ui will move to `system:general-chat/v1` → **two chats, bao listens on the wrong one** |
| 2 | Datasets are declared under **parts**; `POST …/types/:id/datasets` is gone (`POST …/types/:id/parts` with inline `datasets`, or `…/parts/:partId/datasets`); the draft key is `key` not `name`; records live in **`<typeId>_<key>`**, never in a space-unique name | every store (`agent_turns`, `agent_memory_items`, `agent_config`, `agent_secrets`, `agent_triggers`, `program_source`, `mini_app`, `email_messages`, …) is declared with `name` via the old route and read/written by that literal collection name | `404` on the declare route; every `query`/`modify`/`upsert` naming `agent_turns` → `400 dataset.unknown`. Pre-parts definitions in existing spaces: compile status unknown (§2.3 test) |
| 3 | Editor routes carry the collection: `…/editor/:collection/markdown[/append]`, `…/editor/:collection/blocks`; **no write attaches a type** — an object must carry `page` (built-in hidden) or a type with an editor part, else `400 dataset.not_declared` | `…/editor/markdown` (runtime + guest); `create_object(markdown=…)` creates a bare object then PUTs markdown, relying on the server's `editor.EnsureType` auto-attach | `404` on the route; after fixing the path, `400 dataset.not_declared` on every page bao writes |
| 4 | `nav` built-in removed: no `nav` in the create body (`400 request.unknown_field`), no `nav.*` on rows, no auto-stamping. Tree = catalog usecase `wiki` (`system:wiki/v1` type with `parentId`/`pos`/`folder`), client-allocated `pos` | `create_object` accepts a `nav` block; skills teach `nav` as a builtin group; `_RESERVED_GROUPS` includes `nav`; object placement in the user's tree is implicit | folders/tree placement silently absent; any `nav` in a body 400s |
| 5 | Property descriptors: `format` → **`xFormat`** bag, new slug vocabulary (`choice` with `options` map + `config.multiple`, `relation` with `relation.targetTypes`, `date`/`datetime`, `markdown`, `checkbox`, `currency`, …); `xKind` deleted; `kind` required (never defaulted from the descriptor); **`meta` narrowed to `index`** (`meta.pos`/`meta.icon`/any other key → `400 request.invalid_field`; display order and icon live at `xFormat.pos`/`xFormat.icon`); PATCH is leaf-only, `xFormat.type` mutable within the kind; new `409 property.xkey_conflict`; **no migration of old `format` definitions** (they read by `kind` alone) | ADR-022 encode/decode keyed on `pdef["format"]["type"]` ∈ `select`/`multiselect`/`links`/`date`/…; `add_property` writes `format` + `xKind` and stamps `meta.pos` on EVERY create; `archive_property` writes `meta.anyUiArchived` | `add_property` fails on every call (`meta.pos` alone is a 400); new definitions carry `xFormat` so hydration sees no format → option names, links, dates degrade to raw kinds; a links property created before the change indexes no backlinks until patched to `xFormat.type: relation` |
| 6 | Subscribe with `limit` requires `sort` (`400`); `any.types` filters must exclude `__type__` rows (a bundle root matches its own type) and `bin` carriers | runtime chat/trigger subscriptions already sort; guest `subscribe` callers unknown (inventory §3) | 400 on any unsorted window; type-filtered lists gain the type-definition row |
| 7 | Account CRDT version mark (`sdk.crdt_version_newer`, monotonic, stamped on open), any-sync-sdk v0.2.8 → v0.3.3, any-store v0.4.7 → v1.0.1; index schema stays 7 (no forced index wipe in this range); built-in hidden `page`/`miniapp`/`bin`/`dataview` reserve those xKeys | fleet on mixed builds | once the new server opens a data dir, no older binary opens that account again; a fleet must move together (ADR-010 §5 lesson) |
| 8 | Reply shapes: `GET …/datasets` rows carry `owners`/`module`/`shared` instead of `typeId`; backlinks answer `{object, parts}` of edge objects (plus `…/links`, `GET /v1/backlinks?target=`, `409 index.disabled`); a deleted space reads `200` with `status: "deleted"`; `410 record.deleted` on a write to a tombstone; the old `POST …/types/:id/datasets` is method-not-allowed and a draft with `name` is `400 request.unknown_field` | `list_search_scopes` walks `typeId`; two backlinks readers parse `{backlinks: [...]}`; `get_space` does not branch on `status` | scopes list loses every runtime dataset; backlinks empty; a deleted space looks live |

Also new, worth adopting while porting: `GET /v1/catalog`,
`POST /v1/catalog/:usecase/setup`, bundles declaring a full type
(`parts`/`properties`/`xKey`/`layout`/`weight`/`hidden`), `GET
…/bundles` `synced` flag (already used), backlinks reply `{object,
parts}` + `GET …/objects/:o/links` + account-wide `GET /v1/backlinks`,
`GET …/types?includeHidden=true`, `409 object.derived_undeletable`,
`410 record.deleted`.

## 1. Evidence

- any commits (all 2026-09-04…09-08): a7190ff (types declare parts;
  editor and chat become modules), 3a6108b (built-in hidden types
  page/miniapp/bin), 43ad390 + df51fa3 (bundles declare a full type,
  reserved modules and ids), 306e311 + 5030024 (xFormat descriptors),
  062db50 (dataview), 513263a (CRDT version), 26dbaca + 9df9ca7 +
  5cf4bb8 (usecase catalog), cd8d1ae (nav removed), 0894704 + e3b666a
  (general chat under the reserved chat module), 6f0a5d5 (miniapp
  sidebar), 3e7f2f8 (subscribe limit requires sort), 9a56a5f + b416f76
  + 333f1f9 (link index), c0e28a9 (derived objects refuse delete),
  dfdd059 (bundle.not_ready).
- any docs at HEAD: `docs/03-api.md` § Parts and modules, § Runtime
  dataset schemas, § Built-in hidden types, § Bundles, § Catalog;
  `docs/28-well-known-bundles.md` (esp. § What clients delete — states
  the no-backcompat for `nav` and for client-registered chats);
  `docs/29-client-model.md`; `docs/27-descriptors.md`; `docs/16-chat.md`
  § Finding the chat object; `docs/06-errors.md` diff (new codes:
  `dataset.not_declared`, `dataset.module_reserved`,
  `type.reserved_carrier`, `bundle.reserved`, `catalog.not_found`,
  `object.derived_undeletable`, `record.deleted`,
  `sdk.crdt_version_newer`; gone: `dataset.name_conflict`).
- any routes diff (swagger): editor routes gained `{collection}`;
  `POST /types/{typeId}/datasets` replaced by `POST
  /types/{typeId}/parts` and `POST /types/{typeId}/parts/{partId}/datasets`;
  `/v1/catalog*` added; GET/PATCH/DELETE `…/types/{typeId}/datasets/{defId}`
  and the field routes survive.
- SDK v0.3.3 `docs/17-user-datasets.md`, `docs/bundles.md`,
  `docs/02-tech-space.md` § CRDT version mark (`CRDTVersion = 1`,
  stamped on `Open`; older SDKs have no mark at all).
- Companion surveys (same directory): `any-parts-catalog-port-any-breaks.md`
  (the any-side inventory: 19 sections, full error-code diff §16, full
  route diff §17, port checklist §19) and
  `any-parts-catalog-port-inventory.md` (every anybao call site by
  `path:line`).
- any-ui main (c0351cc6) still speaks `general-chat/v1` and `nav.*`
  (28 files): the UI has NOT ported yet. Both clients must land on
  `system:general-chat/v1`; since the root is derived from the bundle
  id there is nothing to coordinate on the id, only on timing for
  existing spaces (§2.2).

## 2. Decisions the port needs (user calls)

### 2.1 Chat root in existing spaces — clean cut

Proposal: bao resolves the chat exclusively through
`POST /v1/catalog/general-chat/setup` and never reads
`general-chat/v1` again. The old root and its `bao/log/v1` child
(turn history) stay on disk, unread — the ADR-006 §0 rule that already
applies to the v1 chat. Consequence: conversation history in every
existing space starts empty for recall (`history` scope) until the
turns are re-homed. Alternative (more work, keeps history): a one-shot
`anyrt migrate-chat-log` that copies `agent_turns`/`agent_chunks`
from the old chat's log child to the new chat's log child through
upsert. Recommend clean cut for rigs and the prod-test env; decide for
prod after §2.3's answer, because the turn records may be unreadable
anyway.

### 2.2 Cutover timing with any-ui

Until any-ui ports, a user on the new any server would see the old
chat in the UI and bao would answer in the new one. Sequence: server
build → anybao port verified on a rig → any-ui port → prod cutover of
all three in one window (`docs/prod-repo-account.md` flow). The plan
below treats any-ui as a gate, not as anybao work.

### 2.3 Existing agent stores — migrate or recreate

Records live under space-unique names (`agent_turns`) declared by
pre-parts definitions. The new SDK compiles datasets from parts; a
definition record with `name` and no part is either folded into an
implicit part (then its collection is still `agent_turns`, readable)
or marked invalid (then unreadable). This is answered by Phase 0 on a
COPY of a real data dir, not by reading code. Outcomes:

- readable: keep reading old collections until a cutover, then
  re-upsert into `<typeId>_<key>` (memory items, turns, chunks,
  config, triggers, email corpus) with a one-shot migration command;
- unreadable: no migration path exists; accept the loss on the bao
  home space (machinery-only per the UI) and on the user's space the
  brain/history/email corpus are re-derived by the cron jobs over
  time. The email corpus is re-syncable (ADR-012). Memory items are
  the real loss; a pre-cutover JSON export via the OLD server (`anyrt`
  at the pin + `query` by old names) is the cheap insurance and should
  run regardless.

### 2.4 Keep the store model (types + bundle children), not one root

The tutorial's shape is "one bundle root that is type, instance and
app". anybao's ADR-017 shape is `bao/v1` root + one derived child per
store, each child carrying its own user type. Keep it: the children
already give deterministic ids and per-store types, and only the
dataset DECLARATION moves (from `POST …/types/:id/datasets` to one
part per type). Re-modelling as a self-typed bundle is a later ADR if
we want bao in the sidebar as a miniapp.

### 2.5 The bao space in the sidebar — not now

The sidebar is the list of `miniapp` carriers. Bao's `bao/v1` root
could carry `miniapp` (`bundle: "bao/v1"`) so any-ui lists it; today
the UI hides the home space entirely. Defer; note as a follow-up.

### 2.6 `mini_app` vs built-in `miniapp`

anybao's `mini_app` user type (miniapp@v1, ADR-008 §6) does not
collide with the built-in `miniapp` (different xKey), but the built-in
now owns "what shows in the sidebar". Keep `mini_app` as the code
store; decide with any-ui whether an agent-built app is ALSO a
`miniapp` carrier (`bundle` = a `bao:` id) — out of scope here.

### 2.7 Page placement — adopt the wiki usecase

Objects bao creates in the user's space carried a `nav` row for free;
now they are outside every tree unless they carry `system:wiki/v1`
with `parentId`/`pos`. Proposal: `create_object` gains an optional
`parent` argument; when given, bao runs `catalog/wiki/setup` once per
space (idempotent, cached), attaches the wiki type and sets
`parentId` + a client-allocated `pos` (lexid, last child + 1; the
allocator moves into the guest). Without `parent`, no tree placement —
the object is reachable by search and links, which is what the UI's
"library" shows. Skills say so explicitly.

### 2.8 Property descriptor rework — full move to `xFormat`

ADR-022's encode/decode keys on the old `format` slugs. Move to the
`27-descriptors.md` vocabulary wholesale: `choice` (options map keyed
by stored value; `config.multiple`), `relation` (`any://<objectId>`
values, `relation.targetTypes` by xKey), `date` (midnight UTC) /
`datetime`, `text`/`url`/`email`/`phone`/`markdown`, `number`/
`currency`/`percent`, `checkbox`. Old-format definitions in existing
spaces read by kind alone (server rule) — bao must tolerate a
property with no `xFormat` (already does: it falls back to kind).
`add_property`/`patch_property` write `xFormat`; option create =
`xFormat.options.<key>.{name,color,pos}`.

### 2.8a Two silent failure modes to design the tests around

A READ of a collection the space does not serve answers `200
{"records": []}` — not an error. After the port, a missed re-key shows
up as empty memory, empty history, empty program source, and the agent
keeps talking. Every rig scenario in §5 therefore asserts a non-empty
read where data was written, never just "no error". Writes fail loud
(`400 dataset.unknown` / `dataset.not_declared`). The second one:
`ensure_dataset` (host), `ProgramSchema::ensure` and `create_dataset`
(guest) key their idempotence on `d["name"]`, which the reply no longer
carries — they would re-declare every boot and hit `409
dataset.key_conflict`; they key on `key` after the port.

### 2.8b Harness types become `hidden`

`PATCH …/types/:typeId {hidden: true}` exists now. Bao's twelve
harness types (`agent_config`, `agent_log`, `program`, …) should be
hidden so a user's type picker in any-ui never offers them; the guest
`_catalog` reads with `includeHidden=true` and resolves them as today.
One line per ensure.

### 2.9 App discoverability — bao must know what a space has installed

The point of bundles/miniapps for bao: "the wiki", "my contacts",
"the CRM" become things in the data structure, not conventions. What
the structure carries today (any HEAD):

| source | what it gives | description? |
|---|---|---|
| `GET /v1/catalog` (account-scoped) | every usecase: `id`, `name`, `description`, `requires`, `bundles[{id, name, description?, type{xKey, properties…}, miniapp, parts}]` | usecase `description` yes (wiki, collections, general-chat, people, contact, contacts, crm; the roles have none); bundle `description` is an optional yaml field, empty everywhere today (`internal/catalog/catalog.yml`) |
| `GET …/spaces/:s/bundles` | the installed registry: `id`, `name`, `rootId`, `roots`, `losers`, `derived`, `synced` | no |
| objects query `{"any.types": "miniapp"}` sorted `miniapp.pos` | the sidebar: one row per app root with `any.name`, `any.description`, `miniapp.bundle`, `miniapp.hidden`; also user-pinned objects (no `bundle`) | `any.description` exists on every object but the catalog install writes only `any.name` (`internal/server/catalog.go` stamps name + miniapp values; the client ensure body has no `description` field — `internal/api/bundle.go`) |
| `GET …/types?includeHidden=true` | the types the usecases brought (`wiki`, `person`, …) with `xKey`, `weight`, `layout`, `hidden` | type `description` on built-ins only |

So the answer to "do they have descriptions" is: the CATALOG does, per
usecase; the INSTALLED objects do not. Two-step plan:

1. **Now, no upstream change.** Add `list_apps(space)` to any@v1: the
   sidebar query joined with the bundles registry and the catalog —
   `[{name, bundleId, rootId, usecase, description, hidden, typeId?,
   pinned?}]`, where `description` is the catalog usecase's (bundle id
   → usecase by walking `GET /v1/catalog` once per run, cached) and
   falls back to `any.description` on the root (a user-authored app
   can set it). Inject a one-line-per-app "Installed apps" list into
   the runtime context of every conversation — after `chat object:`
   in `toolcaller@v1` `runtime_ctx` — for the agent space AND for
   `currentUserSpace` when set (that is where "the wiki" lives). Keep
   it inventory-cheap: name, usecase id, one-line description, ≤ ~15
   apps; the model calls `list_apps` for the rest. `_space_context`
   stays a rules document and gets one rule: "apps are data — read
   `list_apps`, never assume a wiki/contacts exists".
2. **Upstream (file a SYN ticket).** (a) stamp the catalog bundle's
   `description` (and, when the bundle has none, its usecase's) onto
   the root's `any.description` at install and heal it on adopt, so
   the sidebar row is self-describing for every client; (b) fill the
   `description` field for every bundle in `catalog.yml`; (c) accept a
   `description` on `POST …/bundles` and stamp it the same way, so
   agent-authored apps (and `bao/v1` itself) describe themselves;
   (d) echo `description` in `GET …/bundles` rows. Until (a) lands, step
   1's join is the fallback and stays as the offline path.

#### Descriptions — the short plan

Vocabulary first, because the three words get mixed up:

| | lives | id | what it is |
|---|---|---|---|
| **usecase** | in the server's embedded catalog only, never in a space | slug (`wiki`, `crm`) | the unit a client asks for: a set of bundles set up together plus `requires` |
| **bundle** | in the space — one row of the `bundles` registry, one root object | permanent, `system:wiki/v1` (catalog) or `bao/v1` (client) | one install: a type, a miniapp, a records host, or any mix; converges across devices |
| **miniapp** | on an object — the built-in hidden type a root carries | — | the sidebar marker; a bundle root that is an app carries it (`bundle` = the bundle id), a pinned object carries it without one |

So a description belongs on the **bundle root** (`any.description`, the
universal group every object has), sourced from the catalog where the
bundle is the catalog's, and from the installer where it is a client's.

1. **any, catalog content** (`internal/catalog/catalog.yml`): a
   `description` on every bundle (today none has one) and on the role
   usecases (investor, customer, partner, vendor, cofounder, candidate
   have none). One-line, user-facing: "A tree of pages with folders",
   "The space's chat", "People and organisations you keep in touch with".
2. **any, install path** (`internal/server/catalog.go`,
   `internal/bundles/bundles.go`): stamp `any.description` on the root at
   install — the bundle's description, falling back to its usecase's —
   next to the `any.name` stamp; heal it on adopt when the root has none
   (never overwrite: a user-edited description wins, the Evolution rule).
3. **any, client bundles** (`internal/api/bundle.go`,
   `handlers_bundles.go`): `description` (≤1024 B, like `name`) on
   `POST …/bundles`, stamped the same way; echoed on `GET …/bundles`
   rows and `GET …/bundles/:id` so a registry read is self-describing
   without an objects query. One SYN ticket for 1–3.
4. **anybao**: `ensure_bundle(..., description=)` in both clients;
   `bao/v1` registers with "bao's agent stores (config, secrets,
   triggers, brain, run log)"; `programs@v1` / `miniapp@v1` pass the
   app's summary when they register an agent-built app as a bundle.
   `list_apps` reads `any.description` off the miniapp rows and falls
   back to the catalog join until 2 ships; the runtime-context line
   uses whichever is present.
5. **any-ui**: show `any.description` on the sidebar entry (tooltip /
   app header) and let the user edit it like any object description —
   no new UI concept, the field already exists.

Also worth surfacing to the model: the usecases NOT installed
(catalog minus registry) so bao can offer "set up contacts?" and run
`catalog/<usecase>/setup` on a user's yes — a `list_available_apps`
twin, or a `installed: bool` column on the same list.

## 3. Call-site inventory (what changes where)

The runtime and the guest each own a copy of the any client; both
change. Line numbers are at anybao main 43a659c.

### 3.1 Runtime (`runtime/src`)

- `anyapi.rs`: `ensure_bundle` (431–463: drop the `rootTypes: ["chat"]`
  shape; add `parts`/`properties`/`xKey`/`hidden` passthrough),
  `get_markdown`/`put_markdown` (688–710: `/editor/editor_blocks/markdown`),
  `create_dataset` (881–896: → `add_part` = `POST …/parts` with inline
  datasets; keep `list_datasets` GET, it survives and now carries
  `collection`), new `catalog_setup(usecase, space)`, new
  `add_part`, new `attach_type`; tests 1441–1525 (bundle body, chat
  ensure, subscribe fixtures).
- `serve.rs`: `general_chat` (91–135: replace the registry-scan +
  `ensure_bundle` with `POST /v1/catalog/general-chat/setup`; keep the
  `409 bundle.not_ready` retry; keep the derived assertion on the
  reply); `ensure_dataset` (178–190: match on `key`, declare through a
  part; returns the `collection`); `provision_agent_stores` (210+: the
  `bao/v1` ensure with `rootTypes: ["page"]` still valid — `page` is a
  built-in; every store's collection name becomes `<typeId>_<key>`
  and must be RETURNED to the callers instead of the literal
  constants); `watch_chat` (2313) and the trigger feed (2721) already
  sort — keep; every `query`/`modify` naming `CONFIG_DATASET`,
  `agent_secrets`, `agent_triggers`, `agent_trigger_runs`, `bao/runs/v1`.
- `program_schema.rs` (39–47, 108–120): `name` → `key`, declare via a
  part; `SOURCE_DATASET`/`MANIFEST_DATASET` become resolved
  collections (`<typeId>_program_source`).
- `triggers.rs` (`CHAT_MESSAGES` stays: it is the shared canonical
  collection; the `agent_triggers` records are read by collection —
  resolve through the schema).
- `broker.rs` (1342–1370): the `agent_secrets` guest-read block keys on
  the exact dataset name; make it suffix-aware (`_agent_secrets`) or
  resolve the collection once at boot and compare ids. ADR-011 §4
  wording changes accordingly.
- `deploy.rs`: uses `ProgramSchema::ensure` — follows. It also
  CREATES the `program` and `agent_skill` objects (488, 589, 788) with
  `types: [<tid>]` only and then `put_markdown`s their body (626, 784,
  796) — under the no-auto-attach rule every one of those writes is
  `400 dataset.not_declared`. Fix: declare a shared editor part
  (`{"key": "body", "datasets": [{"module": "editor", "shared": true}]}`)
  on the `program` and `agent_skill` types in their ensure (one
  `POST …/parts`, idempotent by key), so the body follows the type and
  existing objects keep working without a per-object attach; the
  `SKILL_TYPE` ensure at 679–741 and `program_schema.rs` gain the part.
- `resolver.rs` (185) keys its probe cache on `_addSeq`, a record field
  that is not in the OpenAPI pin; re-check it survives on the new SDK
  during Phase 0 (it is SDK-internal and should).
- `drift.rs` + `api/openapi.vendored.json` + `api/coverage.json`:
  re-pin (`anyrt drift --refresh`, recipe in `docs/api-parity.md`
  § C4); map the new routes (catalog ×3, parts ×4, editor `{collection}`
  ×4, links/backlinks ×3), drop the gone ones.

### 3.2 Guest `repos/_agent/programs/any@v1/program.py`

- chat: `general_chat` (1582–1596), `create_space` (1598–1623), doc
  strings at 254, 2222, 2299; `_chat_log` (2198+: the log child is now
  derived under `system:general-chat/v1` — verify `…/children` is
  allowed on a `system:` bundle: only ENSURE is reserved (`bundle.reserved`
  is raised in `handlers_bundles.go:114` on the ensure path); if
  children are refused, fall back to a `bao/v1` child keyed by chat id
  — ADR-017 §0a option 3).
- datasets: `list_datasets`/`create_dataset`/`remove_dataset`/
  `add_dataset_field` (1776–1900): declare through `POST …/parts`
  (one part per store, `key` = the old name, `ui` omitted), reconcile
  `search.*` through the surviving `PATCH …/datasets/:defId`; return
  and CACHE `collection`; `_ensure_store` (2425) returns the
  `{key → collection}` map; every literal dataset name in `query`,
  `modify`, `upsert_records`, `delete_records`, `get_brain`,
  `create_memory`, `_TURNS_DATASET`/`_CHUNKS_DATASET`/`_MEM_DATASET`/
  `_JOB_STATE_DATASET`/`_ROI_DATASET` (`name` → `key`), `_dataset_time_keys`
  (ADR-019 §4 guard keyed by name → by collection), `list_search_scopes`
  (2333–2360: walks `GET …/types/:id/datasets`, still valid).
- editor: `get_markdown`/`put_markdown`/`edit_markdown`/`append_markdown`
  (1522–1560): `/editor/editor_blocks/…`; `create_object` (1138–1240):
  drop `nav`, add `page` to `types` when `markdown`/`body` is given
  (and in `update_object` when a body is first written), add `parent`
  (§2.7).
- catalog/types: `_catalog` (488: builtins no longer include
  `chat`/`editor`/`nav`; `page`/`miniapp`/`bin`/`dataview` are hidden —
  request `?includeHidden=true` so `page` resolves), `_is_user_type`
  (527), `_RESERVED_GROUPS` (272: drop `nav`), primary-type pick (2382:
  `("nav","editor")` → hidden built-ins), `_SYNTHETIC_TYPES` ok.
- descriptors (ADR-022): every `pdef.get("format")` (309, 355, 640,
  759, 976, 1066, 1092, 2020), `_post_property` (1917–1968: `format` →
  `xFormat`, drop the `xKind` lowering and `_XKIND_OF_FORMAT`, drop
  `meta.pos` — a 400 on every create today — in favour of
  `xFormat.pos`, `meta` reduced to `index`), the constants 279–311
  (`_MARKER_XKEYS` deleted, `_FORMATS` → slugs, `_PINNED_PATHS` =
  `kind`/`scope`/`items`/`properties`, `_ARCHIVED_META` moves to a
  vendor key such as `xFormat.bao.archived`), `set_option`/`remove_option`/
  `patch_property`/`reorder_property`/`archive_property` (2000–2105:
  leaf-only sets, `xFormat.options.<k>.*`, `xFormat.pos`), `_options_of`
  (355), links candidates from `format.filter` (759 → `relation.filter`,
  a JSON-text leaf), `recall@v1/program.py:192`. Hydration: `choice`
  values are always arrays of keys; `relation` values bare `any://<id>`;
  `date` midnight UTC. Also patch bao-created relation properties in
  existing spaces to `xFormat.type: relation` or they feed no backlinks.
- discovery + backlinks: `list_search_scopes` (2333–2360) reads
  `owners` instead of `typeId` and the walked reply's `key`/`collection`;
  `backlinks` (2403–2420) parses `{object, parts}` of edges
  (`source{spaceId, objectId, dataset, recordId, typeId, field}`, `kind`,
  `target{uri, …}`) — add `links(space, object)` and the account-wide
  `GET /v1/backlinks?target=` as new helpers; tolerate `409 index.disabled`.
- spaces: `get_space` / `list_spaces` branch on `status` (`deleted`
  reads 200); `create_space` unchanged except the chat step.
- subscribe: guard `limit` without `sort` client-side with a clear
  error (the wire 400 names it too).

### 3.3 Other programs and skills

- `config@v1` (bundle child `bao/config/v1` — unchanged; dataset name
  `agent_config` → resolved collection).
- `programs@v1`, `miniapp@v1` (dataset decls 222–226, 31–34: `name` →
  `key`; collection resolution).
- `recall@v1`, `autorecall@v1` (`h["dataset"] == "agent_memory_items"`,
  `_HISTORY_DATASETS`): search hits report the COLLECTION; match on
  suffix or on the resolved map.
- `history@v1`, `rollup@v1`, `reflection@v1`, `evolution@v1`,
  `decay@v1`, `memory@v1`, `extraction@v1`, `linkgen@v1`, `enrich@v1`
  (block query `sort=["nav.pos"]` on `editor_blocks` is CORRECT and
  stays — block records keep their own `nav.*`), `ui@v1`
  (`email_messages`), `progress@v1` (`general_chat`).
- `deepResearch@v1` (`_page_type`, 150–156): creates a type named
  "Page" with NO xKey — `POST …/types` now requires one, and `page` is
  reserved by the built-in (`409 type.xkey_conflict`). Replace with the
  built-in `page` (resolve with `includeHidden=true`).
- `enrich@v1` (254–264: `enrichments`, `enrich_proposal` types;
  53–80: `enriched_data`, `enrich_proposal_items` datasets) — parts
  declaration + collection names; its `editor_blocks` read sorted by
  `nav.pos` (174) stays as is.
- `_connectors` gmail sync (ADR-016 `email_messages` on a `mailbox`
  type — declaration + collection; also the `sync_state` type/dataset
  at `gmailSync@v1/program.py:83–96`, and `general_chat` at 686).
- Full type list anybao declares (all must survive the reserved
  handles `page`/`miniapp`/`bin`/`dataview`/`any`/`type`/`spaceIndex`
  and the catalog handles `wiki`, `person`, `contact`, … — none collide
  today): `program`, `agent_skill`, `agent_config`, `agent_secrets`,
  `agent_trigger`, `agent_brain`, `agent_log`, `mini_app`, `mailbox`,
  `sync_state`, `enrichments`, `enrich_proposal`.
- skills: `_any.md` (lines 25–35 builtins list, 86–95 `nav` groups,
  237–270 chat rules incl. `filter={"any.types": "chat"}` → the chat is
  found ONLY through `general_chat()`, 278 email), `_space_context.md`
  (63), `_core.md` (63, 228: `query(space, chat_id, "agent_turns")`),
  `_memory.md` (49: `"agent_memory_items"`), `_gmailSync.md` (69:
  `email_messages` on `mailbox`). Skills should stop naming raw
  collections: teach the `collection()` helper (or have `query` accept
  the store KEY and resolve it) so the prompt stays stable across
  spaces — the collection name now embeds a per-space type id.
- `repos/_connectors` gmailSync program: `_create_dataset` draft for
  `email_messages` on the `mailbox` type (its test harness
  `tests/test_gmailsync_program.py:105–164` pins the old draft and the
  literal dataset name).

### 3.4 Tests and fixtures

- `tests/test_any_module.py` (catalog fixture with `nav`, 264–300,
  399, 630, 726–748, 1008), `tests/test_any_properties.py` (37),
  `tests/test_recall_module.py` (27–32, 266), `tests/test_miniapp_program.py`,
  `tests/test_programs_module.py`, `tests/test_history_module.py`,
  `tests/test_memory_module.py`, `tests/test_enrich_*`, `tests/test_rt_e2e.py`
  (fake any server: routes + reply shapes), `runtime/src/*` unit tests
  with wire fixtures, `tests/fixtures/*.jsonl` recorded replies.
- `api/openapi.vendored.json` re-pin makes `anyrt drift` red until the
  manifest is remapped — that is the checklist. `make api-drift` is
  not in CI today (only the fingerprint self-consistency unit test
  runs); add it to `.github/workflows/ci.yml` in this port.
- Already dead, found while inventorying: `tests/conftest.py:115–125`
  calls four `/agent/*` routes removed at the C3 pin (the `-m
  integration` tests `test_integration`, `test_instants_integration`,
  `test_memory_integration`, `test_recall_eval` 404 against any current
  server; they skip without a live server, which hid it), and
  `runtime/src/testutil.rs:466` fakes `GET /spaces/_/agent/brain`.
  Both get rewritten onto the bundle-child stores in Phase 4.
- `runtime/src/routes.rs:8` classifies a guest POST as a read by path
  suffix (`/query`, `/objects/query`, `/search`, `/aggregate`); the new
  routes are POST `catalog/:id/setup` (a write — correct by default)
  and GETs, so no misclassification, but re-check the list when the
  pin moves.

### 3.5 Docs

ADR amendments (§7), `docs/api-parity.md` § C5, `docs/debugging.md`
(chat lookup), `docs/testing-agent-changes.md` + `docs/prod-repo-account.md`
(catalog setup step, index v7 rm), `docs/cutover-checklist.md`
(superseded parts), `repos/CLAUDE.md`, root `CLAUDE.md` (skill
"analyze a toolcaller run" unchanged).

## 4. Implementation phases (one topic = one commit; ADR in the same change)

### Phase 0 — reconnaissance on a rig (no code)

1. Build any main with search tags into a NEW worktree
   (`~/any/any-wt-main` — never touch `any-wt-a176029`, the pin the
   running rigs use): `nix develop -c go build -tags 'fts vector' -o
   bin/any ./cmd/any`; `make catalog-validate`.
2. Fresh staging rig on unused ports (say user `:7142`, repo `:7022`,
   control `7017`): `any init` twice, `ACCOUNT.txt` saved, config
   `configs/anybao.staging-parts.toml` (+ `.connectors.env.off` beside
   it). Run CURRENT anybao against it and record the failure list —
   this is the acceptance baseline (expected: serve dies at the
   general-chat ensure).
3. Migration probes on a COPY of `~/any/any/datadirs/staging-user`
   (cp -a, then `rm index/`): open with the new server; `GET
   …/bundles` (old `general-chat/v1` row present? `synced`?); `GET
   …/types/<agent_log>/datasets` (old flat definitions: listed with a
   `collection`? `invalid`?); `POST …/query {dataset: "agent_turns"}`
   (records readable?); an object row with `nav.*` values; a property
   with old `format`; `GET /v1/health` `crdtVersion`. Then try the
   OLD binary on the same copy to confirm the one-way door. Write the
   answers into §2.3 and decide.
4. Same probes for any-ui's expectations are any-ui's job; only
   confirm the catalog chat id equality by running the UI's
   `catalog/general-chat/setup` equivalent (curl) on the rig and
   comparing to bao's.

#### Phase 0 results (2026-09-08, rig `configs/anybao.staging-parts.toml`)

Rig: any main 5d709c8 built as `~/any/any/bin/any-main` (fts+vector,
`make catalog-validate` ok), fresh staging-network account
`AAr7nWV1…` (data `~/any/any/datadirs/staging-parts`, mnemonic in
`ACCOUNT.txt`), server `:7142`, control `7017`, one account owning
`_agentrepo` / `_connectorsrepo` / `bao` / scratch `phase0`. Start:

```fish
cd ~/any/any && nix develop -c ./bin/any-main run --config configs/any-config-staging.yml --data-dir ~/any/any/datadirs/staging-parts --addr 127.0.0.1:7142
```

Baseline with anybao main 43a659c (the acceptance bar):

- `anyrt deploy` → `404 request.not_found` on `POST …/types/<program>/datasets` (the `program` type itself was created).
- `anyrt serve` → `general-chat bundle ensure: 400 type.not_found: rootTypes names a type this space does not have`; the derived `bao` space was created first.

Contract probes, every answer confirmed live:

| probe | result |
|---|---|
| `catalog/general-chat/setup` ×2 | `installed: true` then `false`, same `rootId`, `derived: true`, `typeId == rootId`, reply carries `miniapp: {bundle}`; `GET …/bundles` lists it with `synced: true` |
| chat root row | `any.types = ["__type__", <rootId>, "miniapp"]`, `any.name = "General"`, **no `any.description`**, `miniapp.bundle` set |
| old recipe `rootTypes: ["chat"]` | `400 type.not_found` |
| client bundle with a `chat` part | `400 dataset.module_reserved` |
| client ensure of a `system:` id | `409 bundle.reserved` |
| `…/bundles/system%3Ageneral-chat%2Fv1/children {seed: "bao/log/v1"}` | **allowed**, deterministic (same `objectId` twice) — the turn log stays a child of the catalog chat |
| chat send + read on the root | works, `chat_messages` unchanged |
| old `POST …/types/:id/datasets` | `404` (not 405) |
| `POST …/parts` with an inline records dataset | `201 {partId}`; re-declaring the same key → `409 dataset.key_conflict` |
| `GET …/types/:id/datasets` row | `{id, key, collection: "<typeId>_agent_turns", module: "records", partId}`, **`name` absent** |
| upsert by `collection` | created; by the old name → `400 dataset.unknown`; read by the old name → `200 {records: []}` (silent) |
| record read-back | `creator` + `createdAt` stamps, **`_addSeq` present** (resolver probe key survives) |
| `GET …/datasets` discovery | `editor_blocks` owners `["page"]`, `chat_messages` owners `[<chat root>]`, the namespaced one owners `[<typeId>]`; no `typeId` |
| `PATCH …/types/:id {hidden: true}` | 204; the type leaves `GET …/types` and returns with `?includeHidden=true` (`hidden: true`, `builtIn` absent); built-ins `dataview`/`page`/`miniapp`/`bin` listed hidden + builtIn |
| old `PUT …/editor/markdown` | `404`; new route on an object without `page` → `400 dataset.not_declared`; after `attach/page` PUT/append/GET round-trip; `types: ["page"]` at create works |
| `nav` in a create body | `400 request.unknown_field` |
| property with `format` + `meta.pos` | `400 request.unknown_field`; `meta.pos` alone → `400 request.invalid_field`; `xFormat` `choice`/`relation`/`date` create fine and read back as `xFormat`, `meta`/`format` null |
| subscribe `limit` without `sort` | `400 request.invalid_field` (`details.field: "limit"`) |
| `catalog/wiki/setup` | `typeId` + `properties {parentId, pos, folder}`; an object created with `types: ["page", <wiki>]` and `parentId: ""`, `pos: "a0"` lists under the parent filter sorted by pos |
| sidebar query | two rows: General (`system:general-chat/v1`) and Wiki (`system:wiki/v1`), both `__type__` + `miniapp`, `description` null |

Not run (clean cut decided): the copied-data-dir probes and the
old-binary one-way-door check.

### Phase 1 — ADR-027 "any parts, modules and the usecase catalog" (docs only)

Accept the contract: chat via catalog (amends ADR-006 §0), stores as
parts with `<typeId>_<key>` collections and the resolved-collection
rule (amends ADR-017 §0/§1, ADR-016 §1, ADR-013 §1, ADR-008 §6),
`page` attach on body writes + wiki placement (amends ADR-010 §8,
ADR-006 §6), `xFormat` descriptors (supersedes ADR-022 §1–§4 slug
tables), `agent_secrets` guard by collection (ADR-011 §4), the §2
decisions with their answers from Phase 0. One commit.

### Phase 2 — runtime

1. `anyapi.rs`: routes (editor collection, parts, catalog, attach),
   bundle body passthrough; unit tests on the recorded wire shapes.
2. `serve.rs` general chat through the catalog; `provision_agent_stores`
   through parts, returning resolved collections; `AgentStores` gains
   the collection map; consumers (`config`, `secrets`, `triggers`,
   `runs`) read it. `program_schema.rs` same. `broker.rs` secrets guard.
3. Drift re-pin: `anyrt drift --refresh`, manifest remap, `anyrt drift`
   clean; `docs/api-parity.md` § C5.

### Phase 3 — guest any@v1 and programs

1. Chat: `general_chat`/`create_space`/`_chat_log` via catalog setup.
2. Stores: parts declaration + collection map; every consumer program
   reads names through `any@v1` (add `collection(space, xkey, key)`
   to the flat surface for programs that query by name today).
3. Editor: collection routes; `page` attach in `create_object`/
   `update_object`; `nav` removed; `parent` + wiki setup + `pos`
   allocator.
4. Descriptors: `xFormat` encode/decode + option lifecycle (ADR-022
   rewrite).
5. Catalog: hidden types, builtin list, primary-type pick, backlinks
   shape, subscribe guard, `list_search_scopes`.
6. Programs: miniapp@v1, programs@v1, recall/autorecall suffix match,
   ui@v1, gmail connector.
7. Skills: `_any.md`, `_space_context.md`, `_gmailSync.md`.

### Phase 4 — tests

Unit fixtures (§3.4), `test_rt_e2e.py` fake-server routes, parity
fixtures untouched (LLM-side). `make test` + `make lint` green;
`anyrt drift` clean.

### Phase 5 — rig verification (§5 matrix) and prod cutover (§6)

## 5. Test plan

Everything runs through `check-anybao-changes` style loops (deploy →
message bao → read the `toolcaller@v1` trace) on the Phase-0 rig;
traces are the evidence. Two rigs: **fresh** (new accounts, new
spaces) and **upgrade** (the data-dir copies from Phase 0.3).

### 5.1 Fresh rig — must all pass

1. Serve boot: `catalog/general-chat/setup` on the bao space → the
   reply's `rootId` == `GET …/bundles/system%3Ageneral-chat%2Fv1`.rootId;
   `derived: true`; the chat root carries `miniapp` and the
   `general_chat` type; `GET …/types?includeHidden=true` lists it.
2. Stores: `bao/v1` ensured (created root, `page`); children
   `bao/config/v1`, `bao/secrets/v1`, `bao/triggers/v1`, `bao/runs/v1`
   derived with their types; `GET …/types/<t>/datasets` lists each
   store with `collection == <typeId>_<key>`; a second serve boot is a
   pure adopt (`installed: false`, same ids, nothing re-declared).
3. Conversation: a user message in any-ui's chat (or `any chat send`
   on the SAME root) → trigger fires → reply lands on that root with
   `agent.done: true`; the trace's `agent_turns` record is on the
   `bao/log/v1` child of the chat's bundle.
4. Memory: explicit save → `agent_memory_items` record under the
   resolved collection; recall scope `agent` finds it; autorecall
   injects it on the next turn (the search hit's `dataset` is the
   namespaced collection and the program still classifies it).
5. History: rollup cron produces `agent_chunks`; `history@v1`
   `$dateTrunc` aggregation over `createdAt` works on the new
   collection.
6. Pages: "write a page about X" → object carries `page` + body via
   `/editor/editor_blocks/markdown`; `edit_markdown` PATCH; `append`;
   `get_markdown` round-trips. With `parent`: wiki setup ran once, the
   object carries `system:wiki/v1` with `parentId`/`pos`, and the UI
   shows it under the parent.
7. Properties: create a type with `choice`, `relation`, `date`,
   `url` properties → write with option NAMES, object names, ISO dates
   → read back hydrated (names, `any://` links, instants); an unknown
   option is created under `xFormat.options.<key>`; filters by handle
   resolve to `<typeId>.<propId>`; the type-definition row is excluded
   from `list_objects` by type.
8. Programs/mini apps: `programs@v1` deploy of an agent-authored
   program → `program_source` records under the resolved collection;
   `miniapp@v1` build → `mini_app` records; the resolver loads both.
9. Gmail: connector sync into `email_messages` (resolved collection),
   `list_search_scopes` lists the `email` scope, search finds a mail.
10. Triggers: cron + `chat_messages` event trigger survive a serve
    restart (BOB-39 checks) on the new chat root.
11. Errors: `create_object(nav=…)` refused client-side; subscribe
    with `limit` and no `sort` refused client-side; an `ensure_bundle`
    with a `system:` id → `409 bundle.reserved` surfaced verbatim;
    a bundle body with a `chat` part → `400 dataset.module_reserved`.
12. Two-device convergence: a second server on the same account
    (staging-repo account pattern) boots serve against the same
    space → adopts every bundle and child, same collections;
    `losers` stays empty (children and the chat are derived; `bao/v1`
    is created — the owner escape applies, verify no fork with both
    online).
13. Overlay: `anyrt deploy` to the repo space through the owning
    server; the guest account joins read-only and serve resolves
    programs — unchanged path, but the repo server is on the new
    build too (mixed fleets list ZERO objects silently, ADR-010 §5).
14. `anyrt drift` clean; `make test`; `uv run ruff check .`;
    `cargo clippy -D warnings`.
15. App discovery (§2.9): set up `wiki` + `contacts` in the user's
    space via curl; the next conversation's system prompt (trace,
    `--seq` of the first llm call) carries the "Installed apps" lines
    with the catalog descriptions; "what apps do I have here?" is
    answered from `list_apps` without a guess; "do I have a CRM?" →
    bao reports it is not installed and offers the setup; on "yes",
    `catalog/crm/setup` runs and the reply lists the new bundles.

### 5.2 Upgrade rig — the data-dir copies

1. Boot serve on the copied user account: `catalog/general-chat/setup`
   installs the NEW chat next to the old `general-chat/v1` root;
   serve picks the new one; the old one is never written again.
2. Depending on §2.3: old stores readable → run the migration command
   and verify counts (memory items, turns, chunks, config keys,
   triggers) match old-server counts; unreadable → serve re-provisions
   fresh stores without error and the boot log names what it left
   behind.
3. The pre-cutover export (§2.3) restores into the new collections.
4. Old-format properties on existing user types still read (by kind)
   and bao's hydration degrades cleanly (no exception on a missing
   `xFormat`).
5. Objects with inert `nav.*` values still list and open.

### 5.3 Coexistence with any-ui

Run any-ui from its port branch against the fresh rig
(`VITE_API_TARGET`/`VITE_ANYRT_TARGET`): the sidebar shows the chat
miniapp; a message typed in the UI reaches bao and the reply renders
in the same thread; a page bao creates with `parent` shows in the
wiki tree; a page without one shows in the library.

### 5.4 Regression evidence kept

Trace ids of every scenario in a `docs/rig-parts-port-YYYY-MM-DD.md`
(the voice-A/B report style), plus the Phase-0 baseline failure list
so the fix is reviewable against it.

## 6. Cutover order (prod)

Preconditions: any-ui port merged; anybao PR merged; §2.3 decided.

1. Export memory items / turns / config from prod via the OLD stack
   (insurance, §2.3).
2. Copy every data dir first (the only rollback), then build any main
   on every server host (mac prod server, this box's `:7003` repo
   server, `:7005` prod-test); no index wipe is forced by this range
   (schema stays 7 — the boot log says so if that changes); start the
   repo server first, wait for sync (`wait-for-sync-before-stopping-owner`
   rule).
3. `anyrt` rebuild on the mac (runtime change) and here.
4. Deploy `repos/_agent` + `repos/_connectors` through `:7003` to the
   prod repo spaces by raw id; poll `/sync-status` before touching
   anything else.
5. Start the prod serve: watch the boot log for `catalog/general-chat`
   adopt/install and the stores' collections; send one message from
   the desktop app; confirm the reply in the same thread.
6. Run the migration command (if §2.3 says readable) and verify
   counts; otherwise confirm the export restores.
7. Release any-ui nightly; confirm the desktop client lands on the
   same chat root (`system:general-chat/v1`).

No rollback exists once the new server has opened an account (CRDT
mark, parts-declared datasets): the data-dir copies from step 2 are
the rollback.

## 7. ADRs touched

| ADR | section | change |
|---|---|---|
| new ADR-027 | all | the port contract; decisions §2 with Phase-0 answers |
| 006 | §0 chat, §6 xKey boundary | chat via catalog; builtin groups list |
| 017 | §0 home objects, §1 stores, §3 surface, §4 anyrt | parts declaration, resolved collections, log child parent |
| 016 | §1 storage, §4 surface | `email_messages` → `<mailboxTypeId>_email_messages` |
| 013 / 008 §6 | program + mini_app stores | same |
| 022 | §1–§4 | `xFormat` vocabulary |
| 011 | §4 | secrets guard by collection |
| 010 | §8 flat surface | `create_object` page/parent, `collection()` helper, hidden types |
| 019 | §4 | time-key guard keyed by collection |

## 8. Open questions

1. RESOLVED on the rig: `…/bundles/system%3A…/children` is allowed for
   clients and deterministic — the turn log stays a `bao/log/v1` child
   of the catalog chat's bundle.
2. Moot (clean cut): whether the SDK folds a pre-parts definition.
3. `bao/v1` root carries `page` — keep, or declare bao's own bundle
   type with `xKey: bao` so the root self-types (tutorial shape)?
   Only matters if bao becomes a `miniapp` (§2.5).
4. Does any-ui need bao's pages to carry `page` explicitly to render a
   body? (The UI's "library" and the `page` built-in — confirm with
   the any-ui port.)
5. Which any commit to pin: 5d709c8 (docs only on top of 4c6b427) —
   pin the merge commit that carries the SDK v0.3.3 bump.
