# ADR-027: any parts, modules and the usecase catalog — stores as parts, the chat from the catalog, bodies on `page`, descriptors in `xFormat`

Status: **Accepted** (2026-09-08)
Date: 2026-09-08
Builds on: ADR-006 §0/§6, ADR-010 §5/§8, ADR-011 §4, ADR-013 §1,
ADR-016 §1/§4, ADR-017 §0/§1/§4, ADR-018 §2, ADR-019 §4, ADR-022
Amends when accepted: ADR-006 §0 (chat), §6 (builtin groups); ADR-008
§6 and ADR-013 §1 (store declarations); ADR-010 §8 (`create_object`
body/parent, `collection`, apps); ADR-011 §4 (secrets guard); ADR-016
§1/§4 (`email_messages` collection); ADR-017 §0/§1/§3/§4 (parts,
collections, the log child's parent); ADR-019 §4 (time-key guard);
ADR-022 §1–§5 (superseded: descriptors)
Tracks: Linear BOB-93 (archived marker); `docs/any-parts-catalog-port-plan.md` (the port plan, Phase 0
evidence, companions with the full any-side and anybao-side
inventories)

## Context

any main (5d709c8, 2026-09-08; SDK v0.3.3) replaced the object model
under every client, 116 commits past anybao's pin a176029. The
contract now is: an object carries types; a type is properties plus
**parts**; each part owns datasets served by a **module** (`records`,
`editor`, `chat`); a dataset's records live in a **collection** the
server computes — `<typeId>_<key>`, or the module's canonical
collection when shared (`editor_blocks`, `chat_messages`). A
**bundle** is one root object registered under a permanent id; it can
be a type, an app (`miniapp` carrier) and an implementation of its
type at once. The server ships a **usecase catalog** of well-known
bundles (`system:` ids); the space's chat is one of them, on a derived
root, the `chat` module reserved to it. `nav` is gone; the tree is the
`wiki` usecase. Property descriptors moved from `format` to an opaque
`xFormat` bag and `meta` shrank to `index`. No write attaches a type.
An account opened by the new server carries a CRDT version mark no
older server will open. (Full inventory: the plan's companions.)

Against that server anybao main (43a659c) dies twice before the first
conversation — `anyrt deploy` on the deleted dataset POST, `anyrt
serve` on the general-chat ensure whose `rootTypes: ["chat"]` names a
type that no longer exists — and, once past those, every store read
would answer an empty list because the collection names changed. Every
property create fails on the `meta.pos` stamp alone. Phase 0 of the
plan confirmed each of these live.

Decision taken with the user (2026-09-08): **clean cut**. Nothing is
migrated — not the old chat roots, not the records under the old
collection names, not the pre-descriptor property definitions. The
code and the docs describe the new contract only; there are no
bridges, fallbacks or mentions of the former shapes. This is the
no-backcompat principle applied to our own past.

## Decision

### 1. The space's chat is the catalog's

`POST /v1/catalog/general-chat/setup {spaceId}` is the one way anybao
finds or installs a chat. The reply's `bundles[0]` carries the root:
`bundle.rootId` (== `typeId`, the root is its own type), `derived:
true`, `installed` (this call created it, or adopted). Both clients
call it — the host at serve boot for the bao space and the guest in
`general_chat(space)` / `create_space` — and both **assert `derived`**
on the reply: a non-derived root is a server we do not support, serve
stops naming it. Setup is idempotent and adopt-or-install on every
member and device, so it replaces the locked registry read
(`GET …/bundles` + ensure-on-definitive-miss): the server runs the
convergence wait itself, and `409 bundle.not_ready` keeps the boot's
brief retry. The chat root carries `miniapp` (`bundle:
"system:general-chat/v1"`), so it is a sidebar entry like every app.

Messages, the `agent`/`control` groups, read tracking, the
`chat_messages` event source (ADR-018 §2) are unchanged: the canonical
collection kept its name.

**The turn log stays a child of the chat's bundle.** `bundle_child`
on `system:general-chat/v1` with seed `bao/log/v1` is allowed for
clients (only *ensure* is reserved under `system:`; verified live) and
deterministic, so ADR-017 §0's rule holds with the new parent id.
`_chat_log` resolves the chat's registry row by root id as before.

Nothing in anybao names `general-chat/v1`. A space set up under that
recipe keeps its old root on disk; anybao never reads it, and a
sidebar that still shows it is the client's leftover to hide.

### 2. Stores are parts; the collection is read, never composed

Every harness store keeps its type and its bundle child (ADR-017 §0
unchanged: `bao/v1` root, `bao/config/v1`, `bao/secrets/v1`,
`bao/triggers/v1`, `bao/runs/v1`, `bao/brain/v1`, the chat's
`bao/log/v1`). What changes is the declaration and the address.

**Declaration.** One part per store, `POST …/types/:typeId/parts`
with the dataset inline: `{"key": "<store>", "datasets": [{"key":
"<store>", …draft}]}` — the part key and the dataset key are the
former dataset name (`agent_turns`, `agent_memory_items`,
`program_source`, `email_messages`, …), the draft body is the ADR-017
§1 / ADR-016 §1 shape with `name` → `key`. No `ui` (nothing renders a
harness store). Idempotence keys on `key` in `GET …/types/:typeId/datasets`
(`409 dataset.key_conflict` is the tell of a broken check); the
mutable `search.*` leaves still reconcile through `PATCH
…/types/:typeId/datasets/:defId` (ADR-016 §4's drift rule, unchanged);
field evolution keeps `POST/PATCH/DELETE …/datasets/:defId/fields…`.
The two harness-declared code types (`program`, ADR-013 §1;
`mini_app`, ADR-008 §6) follow the same rule, in `program_schema.rs`
and its guest twin `programs@v1`, and in `miniapp@v1`.

**Address.** The collection is the `collection` field the declaration
reply and the datasets listing carry. Nobody in anybao builds the
string `<typeId>_<key>`; the reads and writes that carry a `dataset`
value (`query`, `query/subscribe`, `modify`, `upsert`,
`delete-records`, `aggregate`, search hits) carry that field.

- **Host.** `AgentStores` (serve boot) resolves each store's
  collection once, next to the child object id, and every host
  consumer (config, secrets, triggers, runs, `program_source` /
  `program_manifest` in deploy and the resolver) reads it from there.
  The constants that named collections become the *keys* the resolver
  looks up.
- **Guest.** `any@v1`'s dataset argument accepts the **store key** and
  resolves it at the client boundary, the ADR-006 §6 way: for a
  per-object read or write it maps the key against the datasets of the
  types the host object carries (a per-space `type → datasets`
  catalog, memoized, refresh-once-on-miss); exactly one match is the
  collection; a canonical (`chat_messages`, `editor_blocks`) or an
  already-namespaced collection passes through; zero matches error
  naming the object's types and their keys; two matches (two carried
  types declaring one key) error listing both — never first-match.
  Records come back with the collection they live in; `search` hits
  gain `key` next to `dataset`, and the programs that classify hits
  (`recall`, `autorecall`) match on `key`. So `query(space, log,
  "agent_turns")` keeps its spelling in every program and skill, and
  the model never sees a type id in a collection name. `_ensure_store`
  returns the key → collection map for callers that want it.
- **Search scopes.** `list_search_scopes` reads discovery rows'
  `owners` (the field that replaced `typeId`) and each owner type's
  datasets for `search.scope`; the fixed set stays `basic`, `chat`,
  `props`.
- **The secrets guard** (ADR-011 §4, broker) refuses a guest body
  whose `dataset` is the secrets store's collection: the exact
  collection resolved at boot **or** any collection ending in
  `_agent_secrets` (the type id is per space; the suffix is the
  invariant). A guest that composes the name by hand gains nothing.
- **Time-key guard** (ADR-019 §4) keys its declared-datetime map by
  collection, filled from the same resolution.

**Harness types are hidden.** Every ensure PATCHes its type
`hidden: true` (`PATCH …/types/:typeId`) so a client's type picker never
offers `agent_config` or `agent_log`; the guest catalog lists types
with `includeHidden=true` and resolves them as before. The eleven
types: `program`, `agent_skill`, `agent_config`, `agent_secrets`,
`agent_trigger`, `agent_brain`, `agent_log`, `mini_app`,
`sync_state`, `enrichments`, `enrich_proposal`. None collides with the
reserved built-ins (`page`, `miniapp`, `bin`, `dataview`, `any`,
`type`, `spaceIndex`) or the catalog's handles. A store whose records
the client renders is NOT a harness type: `mailbox` (ADR-016 §1) is
listed, so it shows in Collections and its object opens as the inbox
layout — the client has no other entry point to mail (any-ui PR #853
retired the Mail mini-app and mailbox discovery). Hiding it takes the
corpus dark.

### 3. Bodies live on `page`; the tree is the wiki usecase

The editor routes carry the collection: `…/editor/editor_blocks/markdown`
(`GET`/`PUT`/`PATCH`), `…/editor/editor_blocks/markdown/append`,
`…/editor/editor_blocks/blocks…`. No write attaches a type, so:

- `create_object` with `markdown`/`body` puts the built-in `page` in
  `types` before the create; `update_object` writing a body onto an
  object that carries no editor-declaring type attaches `page` first
  (`POST …/properties/:objectId/attach/page`, idempotent). The guest
  catalog resolves `page` like the other hidden built-ins.
- Deploy's objects — `program` and `agent_skill` — carry their body
  through their **type**: both type ensures declare a shared editor
  part (`{"key": "body", "datasets": [{"module": "editor", "shared":
  true}]}`), so every existing and future program/skill object holds
  `editor_blocks` without a per-object attach.
- `deepResearch@v1` writes pages on the built-in `page`; it no longer
  mints a "Page" type (an xKey is required and `page` is reserved).

**Placement.** `create_object` gains `parent=`: an object id (or
`""` for the top level). When given, the guest runs
`POST /v1/catalog/wiki/setup` once per space per run (idempotent;
cached with the reply's `typeId` and `parentId`/`pos`/`folder` ids),
adds the wiki type to `types`, sets `parentId` and a `pos` it allocates
— a lexid after the last sibling's, read with one query on the parent
column sorted by pos (`_lexid_after`, already in the module). `folder`
is settable through the same property group. Without `parent` an
object is outside every tree, reachable by search and links; the
skills say so. `nav` disappears from the client: not a reserved group,
not a create-body key, not a builtin id in the docs. Editor **block**
records keep their own `nav.parentId` / `nav.pos` — that is the
module's per-document tree, and `enrich@v1`'s block read sorted by
`nav.pos` stands.

### 4. Descriptors: `xFormat`, the v1 vocabulary (supersedes ADR-022 §1–§4)

A property definition is `{name, xKey, kind, xFormat?, meta?}`. `kind`
is always sent and pinned; `meta` carries `index` only; everything
descriptive is `xFormat` (`docs/27-descriptors.md` in any is the
contract; this section fixes what `any@v1` does with it).

**Handle.** `xKey` is unique within a type on the server now
(`409 property.xkey_conflict`), so the handle is the xKey when present,
else the name (duplicate names suffixed with the id as before). The
marker-xKey set and `xKind` are deleted with the any-ui convention that
needed them. Resolution order id → handle → name → xKey and the
ambiguity error stand.

**Write encoding** (`_encode_value`, keyed on `xFormat.type` and
`kind`):

| `xFormat.type` | accepted input | wire value |
|---|---|---|
| `choice` | option key or name (exact, then casefold); a list; a scalar → one-element list; more than one only with `config.multiple` | **always an array** of option keys |
| `relation` | object id, `any://<id>`, typed `any://o/<sid>/<id>` (normalized down), an object **name** (exact `any.name`, then casefold-unique, within `relation.filter` when declared); a list; more than one only with `config.multiple` | `["any://<id>", …]` |
| `date` | `instant(…)`, ISO date/datetime, epoch s/ms | `{"$date": <midnight UTC>}` |
| `datetime` | same | `{"$date": …}` |
| `text` `longtext` `markdown` `url` `email` `phone` | a string | verbatim (the server validates `url`/`email` shape) |
| `number` `currency` `percent` `rating` `duration` | a number; `"42"` → 42 | number |
| `checkbox` | a bool | bool |
| `period` `money` `geo` | the composite object | verbatim, written whole |
| absent / unknown slug | the kind's JSON shape | verbatim after the kind check |
| any | `None` | `$unset` via `/modify`, as before |

Options are still created on demand: key `slugify(name)` uniquified,
a colour picked by key hash from the palette (an open string now, the
palette is ours), `pos` after the last, written as
`{"set": {"xFormat.options.<k>.name": …, ".color": …, ".pos": …}}` —
the leaf-only PATCH rule: a `set` never carries an object; containers
are unset-only.

**Reads** hydrate as ADR-022 §3 did: `choice` → names (dangling keys
raw), `relation` → `[{id, name, types}]` stubs with one batched `$in`
per page, dates stay instants; filters and sorts accept the display
forms. A property with no `xFormat` (a pre-descriptor definition, or a
plain one) reads and writes by `kind` alone — a plain array, a plain
string — with no special case for what it used to be.

**Definition surface.** `add_property` / `create_type` write `xFormat`
(`type`, `options`, `relation.targetTypes` as **xKeys**,
`relation.filter` as one JSON-text leaf, `config`) and `xFormat.pos`
for creation order; `patch_property` refuses the pinned paths (`kind`,
`scope`, `items`, `properties`) client-side and forwards the rest;
`set_option` / `remove_option` / `reorder_property` write under
`xFormat.options.*` / `xFormat.pos`. **The archived-property marker
is deleted**: `archive_property`, `_is_archived`, the hidden-row
filter in `list_properties` and the "archived in the UI" write warning
go, with nothing in their place — removing a property is
`delete_property`. Whether a soft-remove convention should exist at
all, and where any-ui's `anyUiArchived` came from, is under
investigation (Linear BOB-93, due 2026-09-09). `list_properties` rows are
`{handle, id, name, xKey, kind, scope, xFormat?, meta?, options?}`,
sorted by `xFormat.pos`. A relation property is what feeds the link
index; a links property created before the descriptor move indexes
nothing — it is left as it is (clean cut), the user re-creates it.

### 5. Links, discovery, and the apps a space has

- `backlinks(space, object)` returns the server's edges normalized:
  `{objectId, dataset?, recordId?, kind, typeId?/type, prop?}` from
  `{object, parts}`; new `links(space, object)` (forward edges) and
  `backlinks_everywhere(target_uri)` (`GET /v1/backlinks?target=`,
  global `any://o/<space>/<id>` form). `409 index.disabled` surfaces
  as an error naming the config key.
- **Apps are data.** `list_apps(space)` joins the sidebar query
  (`miniapp` carriers, minus `bin`, sorted by `miniapp.pos`), the
  bundles registry and the catalog into `[{name, bundleId, rootId,
  usecase?, description, hidden, pinned}]` — `description` is the
  root's `any.description` when set, else its usecase's catalog
  description; `installed: false` rows for catalog usecases the space
  lacks come from `list_available_apps(space)`. `setup_app(space,
  usecase)` runs `POST /v1/catalog/:usecase/setup` and returns the
  reply's bundles (`typeId`, the xKey → propId map). The toolcaller's
  runtime context carries one line per installed app — name, usecase
  id, description — for the agent space and for `currentUserSpace`
  when set, capped at fifteen; beyond that the model calls
  `list_apps`. `_space_context` gains the rule "apps are data: read
  `list_apps`, never assume a wiki or contacts exists". Descriptions
  on bundle roots are an upstream ask (the plan § 2.9): until the
  catalog stamps `any.description`, the join above is the source.

### 6. Fleet, pin, cutover

- Pin moves to any 5d709c8 (the docs commit on top of the v0.3.3
  bump); `api/openapi.vendored.json` re-vendored from the rig,
  `api/coverage.json` remapped (catalog ×3, parts ×5, editor
  `{collection}` ×4 + blocks, links ×2, field PATCH; the datasets POST
  and the old markdown routes gone); `make api-drift` joins CI.
- One-way door: the CRDT mark means every server of a fleet moves
  together and a data dir copy is the only rollback; no index wipe in
  this range. The cutover order is the plan's § 6 (repo server first,
  wait for sync, deploy both repos, prod serve, any-ui release).
- Test harnesses fake the new routes (`testutil.rs`, the `wire()` fake,
  `conftest.py` — which also loses its four long-dead `/agent/*`
  routes and their integration tests move onto the store children).
- The rig for all of it: `configs/anybao.staging-parts.toml` (`:7142`).

### 7. Skills and docs

`_any.md`: builtins are `any` and the hidden `page`/`miniapp`/`bin`;
chat found only through `general_chat`; bodies need `page` (handled by
`create_object`); `parent=` for the tree; the §4 write table; apps via
`list_apps`. `_core.md`, `_memory.md`, `_gmailSync.md`: dataset keys
unchanged in spelling (§2). `_space_context.md`: the apps rule.
`docs/debugging.md`, `docs/testing-agent-changes.md`,
the local `docs/environments.md`, `docs/api-parity.md` § C5,
`repos/CLAUDE.md`, `CLAUDE.md`: the catalog step, the rig, the new
routes. Every doc states the contract; none narrates the move.

## Consequences

- Two boots and a deploy that fail today succeed; every store,
  page, property and chat path is exercised by the plan's § 5 rig
  matrix, each scenario asserting a **non-empty** read where something
  was written — the new server answers an unknown collection with an
  empty list, not an error, so a missed re-key is otherwise silent.
- Existing spaces start over for bao: no chat history, no memory, no
  email corpus, no program source from before the cutover. The email
  corpus re-syncs; the cron jobs rebuild what they can; the rest is
  gone by decision.
- The model's spelling of stores does not change; the wire's does.
  One catalog read per space per run pays for the resolution (already
  paid for xKeys).
- Harness types stop appearing in type pickers.
- ADR-022's marker bridge and `xKind` go away with the convention
  they bridged.
- any-ui has to make the same move (its main still speaks the old
  chat recipe and `nav`); the cutover is a three-repo window.

## Open questions

1. Descriptions on bundle roots (`any.description` stamped from the
   catalog, `description` on the client ensure body) — the plan
   § 2.9 ticket; anybao's join stands until then.
2. The archived-property convention — BOB-93: origin, whether a
   soft-remove should exist, and where; nothing in anybao until then.
3. Whether `bao/v1` should carry `miniapp` (bao in the sidebar) — a
   later ADR once the UI stops hiding the home space.

## Amendments

Landed with the code that implements each section, one pointer line
per affected section:

| ADR | Change |
|-----|--------|
| 006 §0 | the chat is `catalog/general-chat/setup`'s root; the registry-read + ensure recipe and `general-chat/v1` are gone |
| 006 §6 | reserved groups are `any` and `_ver`; hidden built-ins `page`/`miniapp`/`bin`/`dataview` resolve by id; dataset keys resolve to collections at the boundary |
| 008 §6, 013 §1 | `mini_app` / `program` datasets declared as parts; collections resolved |
| 010 §8 | `create_object(markdown, parent)`, `list_apps` / `list_available_apps` / `setup_app`, `links`, `backlinks_everywhere`; the runtime-context apps lines |
| 011 §4 | secrets guard = resolved collection or `_agent_secrets` suffix |
| 016 §1, §4 | `email_messages` is a part's dataset key; the collection is read back |
| 017 §0, §1, §3, §4 | log child under `system:general-chat/v1`; parts declarations; `AgentStores` carries collections; hidden types |
| 019 §4 | declared-datetime map keyed by collection |
| 022 §1–§5 | superseded by §4 here |
