# Port plan — any "one type per object, collections" break (BOB-146; any ffd2147 → b5be51c)

Status: 2026-09-17 — analysis only, user decisions folded in (§2,
§8; the inline replies of 2026-09-17). No branch, no code. Evidence is
the probe in §1 (a throwaway account on the new build, staging
network, `:7198`). Every live server today (`:7004`, `:7005`, `:7134`,
the mac) runs `ffd2147`, CRDT mark 1 — nothing is cut over.
Owner: anybao. Pin today: any `ffd2147` + SYN-252 routes (C7 in
`docs/api-parity.md`). Target: any main `b5be51c` (PR #247 merged as
`42536c2`, any-sync-sdk v0.4.0). Normative docs at that sha:
`docs/29-client-model.md`, `docs/03-api.md` § Types / § Collections /
§ Objects / § Properties / § Bundles / § Catalog,
`docs/28-well-known-bundles.md`, `docs/06-errors.md`,
`docs/09-query.md`, `docs/24-data-views.md`.

This document is the plan only. Contract changes land as ADR
amendments in the same commits as the code (ADR README working rule);
§7 lists which ADRs. The precedent is
`docs/any-parts-catalog-port-plan.md` — same shape, same clean-cut
rule (user, 2026-09-08): no bridges, no fallbacks, no mention of the
former shapes in code or docs.

## 0. What broke, in one screen

The model in three lines (any `29-client-model.md`): an object has
exactly one type (`any.type`, scalar) and any number of collections
(`any.collections`, array). A type carries layout, parts and
properties; a collection carries properties only. Every create names
its type; the server never stamps one.

| # | any change (verified on the probe unless noted) | anybao today | new server answers |
|---|---|---|---|
| 1 | Row: `any.types: [..]` → `any.type: "<id>"` + `any.collections: [..]`; values at `<ownerId>.<propId>` where the owner is the type OR a collection; definition rows carry `__type__` / `__collection__` in `any.type` and never match their own id | `_normalize_record`, `_types_of_object`, `_collection` (dataset resolution), link stubs `{id, name, types}`, `_enrich_hits` "primary type", `_dexify` on `any.types` | reads see `any.type` / `any.collections` keys the code never looks at: every object reports no types, every dataset resolution fails with "carries no type declaring …" |
| 2 | Filters: `{"any.types": X}` → `{"any.type": X}` (scalar, dense index) / `{"any.collections": X}` (membership, sparse); `$nin bin` and the sidebar move to `any.collections`; **no `__type__` exclusion needed** | `_resolve_filter` special-cases `any.types`; 7 guest filters, 2 runtime filters (`serve.rs:915`, `deploy.rs:710`), 3 programs, 1 connector | **silent**: `{"any.types": …}` returns `[]` (probe) — every "objects of type X" read is empty, `ensure_typed` mints a duplicate anchor on every call |
| 3 | `POST …/objects` takes `{type (required), collections?, initialProperties}`; `types` is `400 request.unknown_field`, no type is `400 request.missing_field` | every `create_object` (guest, runtime ×4, programs ×7) sends `types: [..]` | every create 400s |
| 4 | `…/attach/:t` and `…/detach/:t` are gone (404); `POST …/type/:typeId` SETS the one type (replace, no unset), `POST`/`DELETE …/collections/:collectionId` file/unfile; wrong slot = `400 type.not_a_type` / `collection.not_a_collection` | guest `attach_type` / `detach_type` / `_ensure_body` / `move_object`; runtime `Client::attach_type` | 404 |
| 5 | Bundles: `rootTypes` → `rootType` (required when nothing is declared) + `rootCollections`; children `{seed, type (required), collections?}` — `types` is 400, a child with no type is 400 | `ensure_bundle("bao/v1", rootTypes: ["page"])` at serve boot + guest `ensure_bundle(root_types=)`; 5 children (`bao/config`, `secrets`, `triggers`, `runs`, `log`) with `types: [tid]` | serve boot fails at the bundle ensure |
| 6 | `weight` removed everywhere (400 on type create/patch, absent from listings); no primary-type contest | guest `create_type` forwards `weight`; runtime `patch_type` doc | 400 whenever the model passes a weight; docstrings lie |
| 7 | Wiki is a COLLECTION: setup reply carries `collectionId` (`typeId: null`); placement values live under `<wikiCollectionId>`; a page is `type: page, collections: [wiki]` | `_wiki` requires `typeId` (raises `catalog.bad_reply`); `create_object(parent=)`, `move_object`, `list_children`, `detach_type(.., "wiki")` | every placement call raises; `list_children` raises |
| 8 | `GET …/types` lists `any`, `spaceIndex`, `type`, **`collection`**, user types; hidden built-in types are `page` + `dataview` only; `miniapp` / `bin` are collections under the new `GET …/collections[?includeHidden=true]` | `_BUILTIN_TYPE_IDS = {page, miniapp, bin, dataview}`, `_SYNTHETIC_TYPES = {any, spaceIndex, type}`, `list_apps` (`any.types: miniapp`), `list_programs` (`$nin bin`), `_is_user_type` | `collection` treated as a user type; the sidebar and program listings return `[]` |
| 9 | Catalog: contact / investor / customer / partner / vendor / cofounder / candidate are collections (a contact = `type: person, collections: [contact]`); setup rows carry `collectionId` for those; `selfTyped` gone | `setup_app` reads `typeId` only; `_catalog_type_xkeys` reads `type.xKey` only (a collection xKey is mintable by the model → `409` at the next setup) | role facets invisible to `setup_app`; reserved-handle guard has holes |
| 10 | One handle namespace: a type xKey and a collection xKey collide (`409 type.xkey_conflict` with `existingCollectionId`) | `create_type` checks type rows only | a user collection named like a type the model wants → 409 with a detail key the guest never reads |
| 11 | Bodies: ONE type per object, so a body needs the object's type to declare an editor part — `PUT markdown` on an object of a part-less user type is `400 dataset.not_declared` (probe); adding `{"key": "body", "datasets": [{"module": "editor", "shared": true}]}` to the type fixes it (probe) | `create_object(markdown=)` appends `page` to `types`; `_ensure_body` attaches `page` on update | every "typed page" (`{"types": ["book"], "markdown": …}`) fails; `program` / `agent_skill` are fine (they declare a body part) |
| 12 | Data views are objects (`type: dataview`, `dataview.host`), not a type attached to a host | bao writes no data views | only the built-in id sets (`dataview` stays a hidden TYPE) |
| 13 | Errors: `collection.not_found` (404), `collection.not_a_collection`, `collection.registered`, `type.not_a_type`, `membership.wrong_slot`, `membership.type_required` (400); `type.not_found` also on object create / children; `type.xkey_conflict` details gain `existingCollectionId` | `AnyError` is a passthrough; `_any.md` names the old codes | wrong hints to the model |
| 14 | CRDT mark 2, one-way; **no migration**: old rows keep `any.types` and have no `any.type`, so a new server lists ZERO objects of any type in an old space (issue text; the dense index is `any.type`) | every environment's account; the prod repo spaces hold `program` objects | a `serve` against an old `_agentrepo` sees "space has no programs" — the ADR-010 §5 silent-zero class, again; a fleet must move together AND start on fresh accounts |

Not affected: chat routes (the general chat root is found through
the catalog setup, messages via `…/chat/messages` and
`dataset: chat_messages` on that root — the probe reads it fine),
editor block/markdown routes, datasets/parts declaration routes,
search, links/backlinks (edge shape unchanged), bundles GET, events,
processes, files, secrets, triggers, the trace store.

## 1. Evidence

- any: PR #247 (`42536c2`, "cheggaaa/types-and-collections") on
  main `b5be51c`; 178 files, `internal/server/handlers_collections.go`
  new, `handlers_properties.go` (SetType / AttachCollection /
  DetachCollection), `internal/catalog/catalog.yml` (wiki + roles →
  `collection:`; app roots → `rootType: page`), `internal/index/`
  (`any.type` dense, `any.collections` sparse, `index.Members`).
- The probe (this session, `bin/any` built from `f4edd19`, which
  contains `42536c2`; `crdtVersion {supported: 2, stored: 2}`): script
  and log at the session scratchpad `probe.sh` / `probe.log`,
  `/v1/openapi.json` saved as `openapi-new.json`. Verified there:
  every row of §0 marked "probe"; the raw row shape
  `{"any": {"type", "collections", "name"}, "<typeId>": {…},
  "<collectionId>": {…}}`; retype keeps the old type's values as
  orphans; bin stamps `bin.movedAt`/`movedBy` on `POST
  …/collections/bin`; `$unset any.type` → `membership.type_required`;
  `{"any.collections": {"$nin": ["bin"]}}` matches rows with NO
  `collections` key (so the exclusion is safe on plain objects); the
  general-chat root reads `{"any": {"type": "__type__", "collections":
  ["miniapp"]}, "type": {"xkey": "general_chat", "hidden": true,
  "layout": {"type": "chat"}}}`; the wiki root
  `{"any": {"type": "__collection__", …}, "collection": {"xkey":
  "wiki"}}`; `system:collections/v1` root is `type: page,
  collections: [miniapp]`; `POST …/bundles {rootType: "page"}` and a
  child `{seed, type}` work, `rootTypes` / `types` / a typeless child
  are 400.
- OpenAPI: 146 paths (was 142). Added: `…/collections`,
  `…/collections/{collectionId}`, `…/collections/{collectionId}/properties`,
  `…/collections/{collectionId}/properties/{propId}`,
  `…/properties/{objectId}/collections/{collectionId}`,
  `…/properties/{objectId}/type/{typeId}`. Removed:
  `…/properties/{objectId}/attach/{typeId}`, `…/detach/{typeId}`.
  That is 12 new endpoint keys (GET/POST, GET/PATCH/DELETE, GET/POST,
  PATCH/DELETE, POST/DELETE, POST) and 2 removed in
  `api/coverage.json`; every route whose body lost `types` / `weight`
  / `rootTypes` re-fingerprints.
- any-ui (origin/main `018a0f76`, 2026-09-16): 0 hits for
  `any.collections`, 132 files with `any.types`, 33 with attach/detach,
  37 with `weight`; no branch or PR for the migration. Linear:
  DROID-182 "Type migration" (Android, In Progress) is the sibling;
  no WEB ticket found by title.
- Upstream nits seen on the probe (file, do not work around):
  `dataset.not_declared`'s message still says "attach a declaring
  type (POST …/attach/{typeId})" — a route that 404s;
  `POST …/set/<collectionId>` on an object NOT filed under that
  collection answers `400 dataset.validation` rather than a
  membership code; listings emit `"hidden": null` where the doc says
  the key is omitted (harmless: read absent-or-null as false).

## 2. Decisions the port needs (user calls)

### 2.1 Clean cut, again — recommended

Same call as ADR-027: no `any.types` alias, no `attach_type` /
`detach_type` shims, no dual-shape readers. Two deliberate ERRORS are
not bridges and are worth their lines, because the model's habits and
every existing trace say `any.types`:

- a guest filter / sort / aggregate path `any.types` raises
  `ValueError` naming `any.type` (one type, scalar) and
  `any.collections` (membership) — the server would otherwise answer
  `[]` silently (§0 row 2, the A18 pattern already used for
  `search(types=)`);
- `create_object` with a `types` key raises naming `type` +
  `collections` (the wire's own message is good, but it fires after
  the guest resolved handles).

`attach_type` / `detach_type` disappear; calling them is the ordinary
unknown-attribute error with the module inventory.

### 2.2 A default type — DECIDED (user, 2026-09-17): every new type has an editor body

Today a "typed page" is `types: [book, page]`. With one type per
object that object is a `book`, and its body needs the `book` type to
declare `{"key": "body", "datasets": [{"module": "editor", "shared":
true}]}` — exactly what the catalog does for `person`, `organization`,
`deal`, `journal`, `meeting`, and what deploy already does for
`program` and `agent_skill`.

The user's framing: **a type is a class** — its parts (the body, a
records dataset) are its methods, its properties its fields; **a
collection is a tag** (an empty one) or a **supertag** (one with
properties) — a means of categorisation and nothing else. A `page` is
the default class: no properties, one body. Hence the concept of a
**default type**: a user type minted by bao starts as "page plus
properties".

- `create_type` (guest) declares the shared body part on every type it
  mints and heals it onto an existing xKey on every ensure (a type
  minted by any-ui or by an older bao without the part gets it the
  first time bao touches it). `body=False` opts out; `_ensure_store`
  passes it for the store types (`agent_config`, `agent_memory`, …),
  which stay hidden and bodiless.
- `create_object(markdown=)` / `update_object(markdown=)` on an object
  whose type declares no editor part raises naming the type and the
  fix (re-run `create_type`, which heals it) — never a silent retype
  to `page`.
- A plain document stays `type: page` (§2.3).

**Harness types are listed, not hidden** (user, 2026-09-17, reversing
ADR-027 §2 for these two): `program` and `agent_skill` drop
`hidden: true` at mint and the ensure patches `hidden: false` onto an
existing one, so they show in a client's type picker and Collections
app like any user type (both already declare a body part). The store
types keep `hidden: true` — machinery, not classes a user picks.

### 2.3 Default type on create — recommended: `page` when the caller names none

The server refuses a typeless create and states the client's default
is `page`. The guest's `create_object(s, {"name": "Recipes"})` (no
`type`) — a plain note, a folder — sends `type: "page"`. A body,
`parent=`, `folder=` all work on it. Anything typed names `type`
explicitly. This keeps every "make me a note" flow one call.

### 2.4 Wiki placement

`parent=` files the object under the wiki COLLECTION (`collections:
[<wikiCollectionId>]`, placement values under that owner); the type is
whatever the caller said (default `page`). `move_object` files with
`POST …/collections/<wiki>`; "take it out of the Wiki" is
`remove_from_collection(space, obj, "wiki")`. A folder is `type: page` +
`folder: true` in the wiki group. `list_children` filters on
`<wikiCollectionId>.<parentId>`.

### 2.5 Guest surface names — DECIDED (user, 2026-09-17)

The flat `any@v1` surface gains the collection half, named for what
the model and a human say ("add it to the Reading list"):

| server | guest (`c.` and flat) | notes |
|---|---|---|
| `POST …/type/:t` | `set_type(space, object_id, type)` | replaces; no unset |
| `POST …/collections/:c` | `add_to_collection(space, object_id, collection)` | idempotent |
| `DELETE …/collections/:c` | `remove_from_collection(space, object_id, collection)` | |
| `GET …/collections?includeHidden=true` | `list_collections(space)` | rows `{id, xKey, name, hidden?, builtIn?}`; the meta row `collection` and the built-ins `miniapp` / `bin` are filtered out (§8 q3) |
| `POST …/collections` (+ properties) | `create_collection(space, {name, xKey?, hidden?, properties?})` | the `create_type` composite, no `layout`, no body part |
| `PATCH …/collections/:c` | `patch_collection(space, key, body)` (only if `patch_type` exists on the surface — check) | |
| `…/collections/:c/properties*` | the EXISTING `list_properties` / `add_property` / `patch_property` / `delete_property` / `set_option` / `reorder_property`, routing by the owner's kind | one property surface, the server's own rule |
| bin | `trash(space, object_id)` / `restore(space, object_id)` = add to / remove from the built-in `bin` | the user's pick over `bin`; the model asks for "delete" — the skill says trash first, `delete_object` is permanent |
| sidebar pin | not exposed (any-ui's) | |

`list_types` stays types-only (what the model picks from) and now
HIDES the four meta rows `any`, `spaceIndex`, `type`, `collection`
(§8 q3) — they were listed-and-guarded, now they are neither offered
nor nameable; `list_collections` is the multi-select half.
`create_object` body: `{type?, collections?, initialProperties, name,
description, markdown}`.

### 2.6 Fresh accounts everywhere — a fleet cutover with recreated spaces

The issue's own statement: existing accounts are not converted,
clients start new ones. For anybao that means, per environment: a new
`any` data dir (new account, new mnemonic in `~/.any-accounts/`), new
`_agentrepo` / `_connectorsrepo` spaces (the old ones list zero
programs on the new server — §0 row 14), new guest-key invites, new
ids in every `anybao*.toml`, a fresh bao space (memory, turns,
triggers, secrets start empty), and any-ui re-pointed
(`any-ui` bakes the prod overlay ids). Order in §6. The prod repo
account `A6BkPD…` (2026-09-09) is replaced for the second time in a
week — the user decides when; the mac serve and every prod client move
in the same window.

### 2.7 Coexistence with any-ui — anybao finishes on the rig, cutover waits

A `serve` on the new model against a server every other client still
reads on the old model is impossible: the server IS the model. So:
anybao ports and verifies on a fresh staging account (§5); no shared
server (`:7004`, `:7005`, `:7134`, mac) moves until any-ui has its
port on main (WEB ticket to be filed by the user — none exists) and
the prod any binaries are rebuilt together. `staging` (`:7134`)
becomes the first fleet to flip, on the user's go.

### 2.8 The word "collection" in skills and docs — and the mental model to teach

The refined logic (user, 2026-09-17): each object has one type; the
type defines the body and parts (its methods, in OOP terms) and its
properties; a collection has only properties, so an empty collection
is a tag and one with properties a supertag — categorisation, no
behaviour. This resolves the old open problems at once: which carried
type is "the main" one, what the default type is, why `page` has no
properties. `_any.md` teaches exactly that, in those words (class /
tag / supertag), and ADR-029 carries it as the context.

`_any.md` uses "collection" six times to mean a user TYPE ("Membership
in a collection/type is `attach_type`", "create … a collection"),
and any-ui calls its types feature "Collections"
(`system:collections/v1`). From this port on the word means the new
object kind only; the skill text says "type" where it means type and
teaches collections as facets ("a contact is a person filed under the
contact collection"). `docs/helper-style.md`, `docs/cutover-checklist.md`
and the ADRs get the same pass (§3.5).

## 3. Call-site inventory (what changes where)

Line numbers are main `d7d421b` (this checkout); ids per the grep of
2026-09-16.

### 3.1 Runtime (`runtime/src`)

| file:line | today | becomes |
|---|---|---|
| `anyapi.rs:490-510` `ensure_bundle(.., root_types, derived)` | body `rootTypes` | `root_type: &str, root_collections: &[&str]` → `rootType` / `rootCollections` |
| `anyapi.rs:525-545` `bundle_child(.., types)` | `types` optional | `type_id: &str` (required) + `collections: &[&str]` |
| `anyapi.rs:759-775` `list_types` doc | names miniapp/bin as hidden types | `page` / `dataview`; new `list_collections(space)` (`?includeHidden=true`) |
| `anyapi.rs:777` `patch_type` doc | `weight` | drop |
| `anyapi.rs:792-808` `attach_type` | `…/attach/:t` | delete; add `set_type(space, o, t)` (`POST …/type/:t`), `attach_collection` / `detach_collection` (`POST`/`DELETE …/collections/:c`) — the runtime needs `set_type` nowhere today (children carry their type from creation); add only what serve uses, the rest stays guest-only |
| `deploy.rs:616`, `:722`, `:916` (three `create_object` bodies) | `"types": [tid]` | `"type": tid` |
| `deploy.rs:710` `ensure_typed` filter | `any.types` | `any.type` |
| `resolver.rs:343`, `:508` (test seeds) | `types` | `type` |
| `serve.rs:290` bao/v1 ensure | `&["page"], false` | `root_type: "page", root_collections: &[]` |
| `serve.rs:253-275` `bundle_child_retry`, `:383-386` children, `:2623` log child | `types: &[tid]` | `type: tid` — every child already has exactly one type; nothing carries two |
| `serve.rs:915` credentials sweep | `any.types` | `any.type` |
| `program_schema.rs:104-112` | `create_type {.., "hidden": true}` | drop `hidden`; the ensure patches `hidden: false` on an existing type (§2.2); body part unchanged |
| `deploy.rs:827-830` `skill_schema` | `"hidden": true` | same as above |
| `testutil.rs` fake: `BUILTIN_TYPES` (196), `held_collections` (227), `create_object` (255), `query_objects` array match (293), `patch_type` keys (465), general-chat seed row (598), `ensure_bundle` (622), `bundle_child` (673), routes (705-786) | old model | `page`/`dataview` types + `miniapp`/`bin` collections; row `any.type` + `any.collections`; `held_collections` = the type's datasets + the row's own id when `any.type` is a marker; routes `…/type/:t`, `…/collections/:c` (POST/DELETE), `GET …/collections`, `GET/POST …/collections/:c/properties`; `attach` route removed; `rootType`/`rootCollections`; child `type` required |

### 3.2 Guest `repos/_agent/programs/any@v1/program.py`

| function (line) | change |
|---|---|
| constants 279-310 | `_RESERVED_GROUPS` unchanged; `_SYNTHETIC_TYPES` += `collection`; `_BUILTIN_TYPE_IDS` → `{page, dataview}`; new `_BUILTIN_COLLECTION_IDS = {miniapp, bin}`; `_MINIAPP` / `_BIN` are collections |
| `_catalog` (597) | one cache over BOTH listings: rows carry `kind: "type" \| "collection"`; `by_id`, `by_xkey` shared (one handle namespace); `_is_user_type` → `_is_user_def(row)`; `_resolve_type_seg` → `_resolve_owner_seg` returning `(id, kind)`; `_resolve_type_or_raise` keeps its name for the type slot and gains `_resolve_collection_or_raise`; error catalogs list both, labelled |
| `_type_props` / `_fetch_props` (614, 2054) | route by owner kind: `…/types/:id/properties` or `…/collections/:id/properties`; `_post_property`, `patch_property`, `delete_property`, `set_option`, `reorder_property`, `_options_of` writers (grep `/properties/` ×~8) likewise |
| `_types_of_object` (707) → `_owners_of_object` | returns `{type, collections}`; a definition row (`any.type` marker) owns itself — `members = [type] + collections + ([id] if marker)` |
| `_collection` (725) dataset resolution | resolve the key against the object's TYPE's datasets (and its own datasets when it is a definition); the "declared by N types" branch dies (one type) |
| `_resolve_type_value` / `_resolve_filter` (1153, 1170) | `any.type`: resolve a type xKey (scalar / `$in` list); `any.collections`: resolve a collection xKey (scalar / `$in` / `$nin` / `$all`); `__type__` / `__collection__` pass; `any.types` → `ValueError` (§2.1) |
| `_normalize_record` (1231) | `any.type` id → xKey, `any.collections` ids → xKeys; user groups keyed by owner xKey whether type or collection; `type` / `collection` meta groups pass through (synthetic) |
| `_hydrate_links` / `_load_stubs` (895-910, 1319-1331) | stubs `{id, name, type, collections}` |
| `_object_id_by_name` (980) | `targetTypes` → `{"any.type": {"$in": [...]}}` |
| `create_object` (1354) | keys `type`, `collections`; `type` defaults to `page` (§2.3); a `types` key raises (§2.1); synthetic guard on both slots; markdown → require the type to declare a body (§2.2), never append `page`; `parent=` → `collections += [wiki]` and placement under the wiki collection id; `_object_types` cache → owners cache |
| `_declares_body` / `_ensure_body` (1450-1465) | body check against the ONE type; `_ensure_body` becomes "raise with the fix" (§2.2) |
| `_wiki` (1467) | read `collectionId`; cache `{collectionId, parentId, pos, folder}`; `_next_tree_pos`, `move_object`, `list_children` use it; `move_object` files with `POST …/collections/<wiki>` |
| `update_object` (1528) | body write gate as above |
| `query_objects` doc (1585) + `_guard_filter` | doc the two keys |
| `list_programs` (1634) | `{"$and": [{"any.type": "program"}, {"any.collections": {"$nin": ["bin"]}}]}` — the `__type__` clause dies |
| `upsert_record` / `upsert_records` docs (1685-1713) | "the object's type declares the dataset" |
| `list_types` (2022) | filter out the meta rows `any` / `spaceIndex` / `type` / `collection` (§8 q3); doc; rows no longer carry miniapp/bin/weight |
| `list_properties` (2029) | accepts a collection key; doc says so |
| `create_type` (2078) | drop `weight`; refuse a handle a COLLECTION holds (read `existingCollectionId` too); declare + heal the body part unless `body=False` (§2.2); the catalog reservation reads `type.xKey` AND `collection.xKey` |
| new `create_collection`, `list_collections`, `set_type`, `add_to_collection`, `remove_from_collection`, `trash`, `restore` (2.5) | after `create_type` / `attach_type` |
| `attach_type` / `detach_type` (2562-2578) | delete |
| `ensure_bundle` (2596) | `root_type=`, `root_collections=` |
| `search` / `_enrich_hits` (2776-2870) | `type` = the one type's name; prop hits walk `[type] + collections` |
| `list_apps` (2971) | `{"any.collections": "miniapp"}` + `$nin bin`, sort `miniapp.pos` (miniapp is a collection group on the row — unchanged key) |
| `_catalog_type_xkeys` (2952) → `_catalog_handles` | union of `type.xKey` and `collection.xKey` |
| `setup_app` (3020) | echo `collectionId` beside `typeId` |
| `_ensure_store` (3041) | `create_type(.., body=False)` — hidden, bodiless |
| flat `search` A18 hint (3627) | `any.type` |
| `inferSchema` / any doc mentioning `any.types` (grep ×~12 docstrings) | reword |

### 3.3 Other programs and skills

| file:line | change |
|---|---|
| `enrich@v1/program.py:270, 275, 378, 494` | `any.type` filter; `type:` on three creates (the hub and minted objects get the body part through `create_type`, §2.2) |
| `miniapp@v1/program.py:76, 198, 328` | `any.type` ×2; `type: mini_app` |
| `programs@v1/program.py:386` | `type: "program"` |
| `deepResearch@v1/program.py:165` | `type: "page"` (`_page_type` already returns it) |
| `toolcaller@v1.py:638, 767` | `any.type` |
| `_connectors/programs/gmailSync@v1/program.py:403, 426, 452, 1126` | `any.type` ×2; `type:` ×2 |
| `_connectors/tests/test_gmailsync_program.py:116` | fake filter key |
| `skills/_any.md:16-30, 65-66, 92-96, 105-116, 120-125, 347, 357` | the model (class / tag / supertag, §2.8), `set_type` / `add_to_collection` / `remove_from_collection` / `trash` / `restore`, `create_object` shape, wiki as a collection, filters, no `__type__` clause, error codes, "collection" wording (§2.8), catalog role facets (`person` + `contact`) |
| `skills/_meta_skill.md:19` | `type: "agent_skill"` |
| `skills/_soul.md:37` | "weight of the ask" is English — leave |

### 3.4 Tests and fixtures

- `tests/test_any_module.py` (84 hits) — the `wire()` fake is
  reply-scripted; every path / body assertion on `attach`, `types`,
  `any.types`, `__type__`, `weight` is rewritten, plus new cases: the
  `any.types` error, `types` key error, default `page`, body-part
  guard, wiki as collection (`collectionId` in the setup reply),
  `list_collections`, `set_type` / `file` / `unfile`, collection
  property routing, `type.xkey_conflict` with `existingCollectionId`,
  stubs `{type, collections}`, `list_apps` sidebar filter.
- `tests/test_any_properties.py` (20), `test_enrich_*` (5),
  `test_recall_*` (5), `test_instants_integration.py` (3),
  `test_toolcaller.py` (2), `conftest.py` `AnyHttp.bundle_child` /
  `attach_type` (148-152), `test_kernel_introspect.py`,
  `test_rt_e2e.py`, `test_integration.py` — mechanical.
- `runtime/src/testutil.rs` per §3.1; `anyapi.rs` / `serve.rs` /
  `deploy.rs` / `resolver.rs` unit tests that assert the old bodies
  (`anyapi.rs:1570-1652`, `serve.rs:4094-4256`, `deploy.rs:996-1008`).
- `api/openapi.vendored.json` re-pinned from a scratch server on the
  new build (the C7 recipe: curl the live `/v1/openapi.json`, never
  hand-stamp), `api/coverage.json`: 12 keys added with helpers per
  §2.5, 2 removed, `anyrt drift --refresh` for the re-shaped bodies
  (`objects`, `types`, `bundles`, `children`, `catalog setup`,
  `properties/set`), then `anyrt drift` green; `docs/api-parity.md`
  § C8.

### 3.5 Docs

- ADR-029 (new, §7) + amendments to ADR-027 §3 / §5 / §6 (bodies on
  `page` → bodies on the type; the sidebar query; the pin), ADR-006
  §6 (reserved groups, xKey normalization now spans two surfaces),
  ADR-017 §0 (bundle root / children shape), ADR-010 §5 (harness
  types: hidden, bodiless — unchanged in substance, wording).
- `docs/helper-style.md:1`, `docs/cutover-checklist.md:1`,
  `docs/api-parity.md`, `docs/debugging.md` if it names `any.types`;
  `repos/CLAUDE.md`; the local `docs/environments.md` (new ids after
  §6).

## 4. Implementation phases (one topic = one commit; ADR in the same change)

### Phase 0 — reconnaissance on a rig (no code) — DONE 2026-09-16

§1. Left open for Phase 5: an OLD account opened by the new server
(does `serve` fail loudly or silently on a pre-#247 `_agentrepo`?)
— run once on a COPY of the staging data dir on a throwaway port
before the staging flip, purely to know what the failure looks like
for reporters (`docs/environments.md` § Gotchas).

### Phase 1 — ADR-029 "One type per object, collections" (docs only) — WRITTEN 2026-09-17, Proposed

`docs/adr/029-one-type-collections.md` on branch
`feat/bob-146-one-type-collections` (worktree
`~/any/anybao-wt-bob146`): the model in the user's words (§2.8), the
guest surface (§2.5), default `page` (§2.3), the default type (§2.2),
the wiki collection (§2.4), the two deliberate errors (§2.1), the
fleet rule (§2.6, §2.7), pin → any `b5be51c`. The inline amendment
lines in ADR-006 / 010 / 017 / 022 / 027 land with the code of Phases
2–3 (the ADR-027 convention; ADR-029's Amendments table lists them).
User review gate before Phase 2.

### Phase 2 — runtime — DONE 2026-09-17 (ADR-029 accepted 5e9b387; commits cca69e4 client + fake + call sites, 065448e listed `program` / `agent_skill`, the pin commit)

1. `anyapi.rs`: bundle shapes, `list_collections`, `set_type` +
   collection membership (only what serve needs), `attach_type`
   removed, docs. `testutil.rs` fake to the new model.
2. `deploy.rs` / `resolver.rs` / `serve.rs` call sites (§3.1); unit
   tests. `cargo test` green (default + `shell`).
3. `api/openapi.vendored.json` re-pin + `api/coverage.json` +
   `docs/api-parity.md` C8; `make api-drift` green.

### Phase 3 — guest any@v1, programs, skills — DONE 2026-09-17 (5e993e5 any@v1 + tests, 14fac11 programs + connector, 043a97e applet rename, the skills commit; `mini_app` → `applet` by user decision, the skill explains the two kinds of app)

1. `any@v1`: catalog over both surfaces, row normalization, filters,
   dataset resolution, stubs / enrich (one commit: "reads").
2. `any@v1`: `create_object` / `update_object` / body guard /
   `create_type` body part / wiki collection / `list_programs` /
   `list_apps` / `setup_app` (one commit: "writes + placement").
3. `any@v1`: `create_collection`, `list_collections`, `set_type`,
   `add_to_collection`, `remove_from_collection`, `trash`, `restore`,
   property routing by owner; `attach_type` / `detach_type` removed
   (one commit: "collections surface").
4. Programs + connector + skills (one commit). `_any.md` rewrite per
   §2.8 is part of it (skills deploy with programs).
5. `uv run pytest` green (`test_any_module`, properties, enrich,
   recall, toolcaller fakes).

### Phase 4 — tests and the rig

Integration suite (`ANYBAO_TEST_SERVER=…`) against the fresh staging
account (§5.1); `rt_e2e`; the §5.2 matrix by hand where no test
exists.

### Phase 4b — the 15-question questionnaire (fluency with the new model)

The usual flow (the soul-first / link-shape checks: a scripted
conversation against the rig, the traces read afterwards with
`anyrt trace show`): fifteen user messages that make bao SPEAK and
USE the new terminology, plus memory questions since the bao space is
recreated (§8 q4). Pass = every answer names the right concept and
the trace shows the right call; no `any.types`, no "attach", no
"collection" meaning type. Draft set (final wording in the run notes):

1. "What types does this space have?" — lists types, not the meta
   rows, not collections.
2. "What collections are there?" — collections, with the wiki as
   one and `miniapp` / `bin` absent.
3. "Make me a Book type with author and year" — `create_type`,
   body part on it.
4. "Add Dune, by Frank Herbert, 1965, with a short summary" —
   `type: book` + markdown, one call.
5. "Put Dune in a Reading list" — `create_collection` (empty = a
   tag) + `add_to_collection`.
6. "Add an Order column to the reading list and set Dune first" —
   a collection property (supertag) + `update_object` under the
   collection group.
7. "Which books are on the reading list?" — `any.collections`
   filter, not `any.type`.
8. "Which objects are books?" — `any.type` filter; the type
   definition row is not among them.
9. "Take Dune off the reading list" — `remove_from_collection`.
10. "Put the Dune page in the wiki under Fiction" — folder found or
    created, `parent=`; the wiki is a collection.
11. "Turn that note into a Book" — `set_type` (and the explanation
    that its old values stay).
12. "Make Ada a contact — she's a person we already have" —
    `setup_app("contact")` + `add_to_collection`, type stays person.
13. "Trash Dune" then "bring it back" — `trash` / `restore`, not
    `delete_object`.
14. "Remember that I prefer hardcovers" then, in a new chat, "what do
    I prefer?" — memory create + recall on the fresh bao space.
15. "Explain in two sentences how types and collections differ" —
    class / tag wording, no legacy terms.

### Phase 5 — cutover (§6), gated on any-ui and the user's go

## 5. Test plan

### 5.1 Fresh rig — must all pass

A NEW staging-network account on the new `bin/any`
(`nix develop -c ./bin/any init --config ./configs/any-config-staging.yml
--data-dir ~/any/any/datadirs/staging2`, mnemonic to
`~/.any-accounts/`), owning fresh `_agentrepo` / `_connectorsrepo`
spaces, `configs/anybao.staging2.toml` (control port 7020 — the memory
note: a control-port bind failure is silent), deploy both repos
through it by raw id, `serve`. The user starts the servers
(prefers owning live services); I hand over commands.

| # | scenario | expected |
|---|---|---|
| 1 | `serve` boot: bao/v1 root (`rootType: page`), 5 typed children, general-chat setup, stores declared | boots; `GET …/bundles` shows bao/v1; children rows `any.type` = their store type |
| 2 | `anyrt deploy` both repos into fresh spaces; `list_programs` from a run | every program listed; a second deploy is "unchanged" |
| 3 | chat: user message → reply; turns logged on the log child; `general_chat()` returns the derived root | trace shows `chat_messages` on the root |
| 4 | `create_object({"name": "Note", "markdown": "…"})` | `type: page`; body readable |
| 5 | `create_type("Book", properties…)` then `create_object({"type": "book", "markdown": …, "initialProperties": {"book": …}})` | type has a body part; object row `any.type` = book; body PUT ok; `query_objects(filter={"any.type": "book", "book.year": 1965})` finds it |
| 6 | `create_object(…, parent="")`, folder, `move_object`, `list_children`, `remove_from_collection(.., "wiki")` | rows carry `collections: [wiki]`, placement under the wiki collection; any-ui (when ported) shows the tree |
| 7 | `setup_app("contact")` then a person filed under contact: `create_object({"type": "person", "collections": ["contact"], "initialProperties": {"person": …, "contact": {"status": "Active"}}})` | reply carries `collectionId`; both groups on the row; `query_objects(filter={"any.collections": "contact"})` |
| 8 | `create_collection("Reading list")` + `add_to_collection` / `remove_from_collection` + a collection property write via `update_object` | `list_collections` shows it; values under the collection id; `type.xkey_conflict` when a type takes the same handle |
| 9 | `set_type(obj, "page")` then back | orphan values survive; `query_objects` by the new type |
| 10 | filters: `any.types` → guest error; `list_programs` / `list_apps` / `search` enrich `type` | as §2.1; `list_apps` lists the chat + wiki roots |
| 11 | enrich e2e (hub + proposal + apply) and a gmail sync into a mailbox (`_connectors`) | objects typed, `any.type` filters find them |
| 12 | `anyrt drift` and `make lint` | green |
| 13 | credentials sweep (`serve.rs:915`) after a program with `credentials` deploys | the sweep finds it (an `any.type` filter) |
| 14 | triggers: an anchor object via `ensure_typed`, a cron trigger firing once | no duplicate anchors across two serve restarts |

### 5.2 The old-account failure shape (know it, don't fix it)

Copy `datadirs/staging` to a throwaway dir, open it with the new
binary on a throwaway port (it stamps mark 2 on the COPY only), run
`serve` once: record the exact error / log line a reporter would see.
Then delete the copy. (A copy on the same network presents the old
device key — keep it up for seconds, not minutes.)

### 5.3 Coexistence with any-ui

Only after any-ui's port lands: the §5.1 space opened in the ported
any-ui shows the Book type with its body, the wiki tree with bao's
pages, the contact's two property groups.

## 6. Cutover order (prod)

Preconditions: any-ui main ported and released; every `any` binary in
the fleet rebuilt from the same sha (`nix develop -c go build -tags
'fts vector' -o bin/any ./cmd/any`); anybao main merged; mac anyrt
rebuilt.

1. Staging first (`:7134`): new account + spaces per §5.1 — it is the
   rehearsal; retire the 2026-09-09 staging account to
   `datadirs/ACCOUNTS-retired-*`.
2. Prod repo account: new data dir + account (`:7004`), `_agentrepo` /
   `_connectorsrepo` + their `-test` copies recreated, guest-key
   invites minted, deploy both repos by raw id, wait for sync-status
   before anything reads them.
3. `anybao.toml` (committed) + `configs/anybao.prod.test.toml` +
   `configs/anybao.prod.smoke.toml`: new ids and tokens; any-ui
   re-pointed (its baked overlay ids) and a nightly cut.
4. `:7005` prod-test: new account, smoke against the real overlays.
5. Mac: new server data dir + account, new bao space recreated empty
   (decided, §8 q4), `serve` restart on the rebuilt anyrt.
6. `docs/environments.md` + the memory notes rewritten with the new
   ids; old accounts' mnemonics archived, never deleted.

Rollback = the data-dir copies taken before step 1 on the old binary
(the mark is one-way).

## 7. ADRs touched

- **ADR-029 (new)** — One type per object, collections: the model
  (class / tag / supertag), the guest surface, default `page`, the
  default type (every new type has a body), the wiki collection, the
  deliberate errors, fresh-account fleet rule, pin `b5be51c`.
- ADR-027 §2 (harness types: `program` / `agent_skill` listed, stores
  hidden), §3 (bodies), §5 (sidebar query, `list_apps`), §6 (pin,
  cutover) — amended; §1 chat unchanged.
- ADR-006 §6 — xKey normalization over two surfaces; `any.type` /
  `any.collections` values speak xKeys; `collection` joins the
  synthetic rows; the BOB-68 row-root guard applies to collection
  handles too.
- ADR-017 §0 — bundle root `rootType`, children carry one type.
- ADR-010 §5 — wording only.

## 8. Decisions taken 2026-09-17 (were open questions)

1. Names: `add_to_collection` / `remove_from_collection`; bin is
   `trash` / `restore` (§2.5).
2. A default type: every new type has an editor body, healed on every
   ensure; `body=False` for stores (§2.2).
3. `list_types` hides the four meta rows `any`, `spaceIndex`, `type`,
   `collection` from the model entirely (§2.5).
4. The prod bao space is recreated empty; the questionnaire gains
   memory questions so recall on the fresh space is verified (Phase
   4b, q14).
5. The upstream nits from §1 go to Sergey Ch. as one SYN task to
   validate (filed 2026-09-17: SYN-260).

Still open: the any-ui WEB ticket for its half (the user files it);
`patch_collection` only if `patch_type` is already on the surface.
