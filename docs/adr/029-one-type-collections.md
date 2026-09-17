# ADR-029: One type per object, collections — classes and tags, the default type, membership verbs, the fleet cut

Status: **Accepted** (2026-09-17)
Date: 2026-09-17
Builds on: ADR-006 §6 (xKey normalization), ADR-010 §5/§8 (harness
types, the flat surface), ADR-017 §0/§1 (bundle-child stores),
ADR-022 §3 (hydration), ADR-027 §1–§7 (parts, catalog, bodies, pin)
Amends: ADR-006 §6 (two definition surfaces; `any.type`
/ `any.collections` values speak xKeys; `collection` joins the
synthetic rows), ADR-010 §5 (`program` listed, not hidden) and §8
(the membership verbs, `create_object` body), ADR-017 §0/§1 (bundle
root `rootType`, children carry one type; `program`/`agent_skill` are
listed), ADR-022 §3 (link stubs `{id, name, type, collections}`),
ADR-027 §2 (which harness types are hidden), §3 (bodies live on the
object's type, not on an attached `page`), §5 (the sidebar query),
§6 (pin, cutover)
Tracks: Linear BOB-146; SYN-260 (three wire nits found by the probe);
`docs/bob-146-one-type-collections-plan.md` (inventory, probe
evidence, phases, the rig matrix, the questionnaire)

## Context

any main `b5be51c` (PR #247, any-sync-sdk v0.4.0) replaced the
object model again. An object has **exactly one type** (`any.type`, a
scalar) and **any number of collections** (`any.collections`, an
array). A type carries the layout, the parts and property
definitions; a collection carries property definitions and nothing
else. Property values live at `<ownerId>.<propId>` where the owner is
the type or one of the collections. Definition rows carry a marker
(`__type__` / `__collection__`) in `any.type`, never their own id, so
a member query needs no marker exclusion. `weight` and the
primary-type contest are gone; `attach` / `detach` are gone;
`POST …/type/:typeId` sets the one type and `POST`/`DELETE
…/collections/:collectionId` file and unfile. Every create names its
type — the server never stamps one. The wiki and the CRM role facets
(contact, investor, customer, …) are collections; app roots that
declare nothing are `type: page`. The account CRDT mark is 2, one-way,
and **nothing is migrated**: an old row has no `any.type`, so a new
server lists zero objects of any type in an old space.

The user's framing (2026-09-17), which is also why the server moved:
**a type is a class** — its parts (the body, a records dataset) are
its methods, its properties its fields; **a collection is a tag** when
empty and a **supertag** when it has properties — a means of
categorisation with no behaviour. `page` is the default class: no
fields, one body. This settles the questions the old model could not:
which carried type is "the main" one, what the default type is, why
`page` has no properties.

Against that server anybao main (`a3ed5f7`) fails at every layer:
serve boot on `rootTypes`, every create on `types`, every placement
on the wiki reply's missing `typeId`, and — worst — every
`{"any.types": …}` read answers an empty list silently. The full
inventory, and the probe that verified each row on a throwaway
account, is the plan's §0–§1.

Decisions taken with the user (2026-09-17): the clean cut again (no
bridges, no dual-shape readers, no mention of the former shapes);
a **default type** — every type bao mints has an editor body; the
membership verbs are `set_type`, `add_to_collection`,
`remove_from_collection`, `trash`, `restore`; `program` and
`agent_skill` are listed types; `list_types` hides the meta rows; the
prod bao space is recreated empty; the acceptance check is a
fifteen-question conversation on the rig.

## Decision

### 1. The model, in the words the agent is taught

The skill says it as the user does. A **type** is what an object IS —
its class. It gives the object its body and parts (the methods) and
its property fields. Every object has exactly one, named at create.
`page` is the default class: a plain document, no fields, one body. A
**collection** is what an object is FILED UNDER — a tag; with
properties, a supertag. An object is in any number of them. Filing
adds columns, never behaviour; unfiling removes the columns from view
and keeps the values as orphans. A **definition** (a type or a
collection) is itself an object, hosts its own values and datasets,
and is never a member of itself.

The word "collection" means this and nothing else from now on —
never a user type, never the any-ui feature. The Collections app
(`system:collections/v1`) is "the types feature" in prose.

### 2. The row, the filters, the reads

- Wire row: `{"any": {"type": "<id>", "collections": ["<id>", …],
  "name", …}, "<ownerId>": {"<propId>": …}}`. A normalized read
  (ADR-006 §6) maps `any.type` to the type's xKey, each
  `any.collections` entry to its xKey, and every owner group — type or
  collection — to `out[<ownerXKey>][<propXKey>]`. The meta groups
  `type` and `collection` (a definition's own metadata) pass through
  like `any`.
- The guest catalog is ONE map over both surfaces: `GET
  …/types?includeHidden=true` and `GET …/collections?includeHidden=true`,
  rows tagged by kind, `by_id` / `by_xkey` shared because the server
  keeps one handle namespace (`409 type.xkey_conflict` names
  `existingTypeId` or `existingCollectionId`).
- Filters, sorts and aggregate paths: `any.type` takes a type xKey
  (scalar, or `$in` of several); `any.collections` takes a collection
  xKey (scalar membership, `$in`, `$nin`, `$all`). Dotted paths
  resolve under either owner. The markers `__type__` /
  `__collection__` pass literally. **No member query carries a marker
  exclusion**; an ordinary listing excludes the bin with
  `{"any.collections": {"$nin": ["bin"]}}` (matches rows with no
  collections at all — verified).
- **`any.types` is a deliberate error.** A filter, sort or aggregate
  path `any.types` raises naming `any.type` and `any.collections` —
  the server answers that spelling with a silent `[]`, and every
  existing trace and habit says it. This is the A18 pattern
  (`search(types=)`), not a bridge: nothing is rewritten.
- Link stubs (ADR-022 §3) are `{id, name, type, collections}` (xKeys).
  Search-hit enrichment's `type` is the one type's display name; a
  property hit is named under whichever owner declares it.
- Dataset resolution (ADR-027 §2): a store key resolves against the
  datasets of the object's ONE type — plus the object's own datasets
  when it is a definition (the general-chat root hosts
  `chat_messages` while `any.type` is `__type__`). The "declared by
  two carried types" branch no longer exists.

### 3. Creating objects; bodies; the default type

- `create_object(space, {type?, collections?, initialProperties?,
  name?, description?, markdown?}, parent=, folder=)`. `type` and
  each `collections` entry are xKeys (ids accepted), resolved to the
  slot the server validates (`type.not_a_type` /
  `collection.not_a_collection` never reach the model: the guest
  checks the kind first and errors with the right verb). A `types`
  key raises naming `type` + `collections`. The synthetic rows
  (`any`, `spaceIndex`, `type`, `collection`) are refused in both
  slots.
- **Default `page`.** A create that names no `type` sends
  `type: "page"` — a note, a folder, a plain document. Anything typed
  says so. The server's own statement ("page for a plain document")
  is the guest's default; nothing else is defaulted.
- **A body needs the type to declare it.** `markdown` on create or
  update is written only when the object's type declares an editor
  part (`page` does; catalog types with a body do; §3 below makes
  bao's own types do). Otherwise the call raises, naming the type and
  the fix — never a silent retype to `page`, never an attached second
  type.
- **The default type.** `create_type` declares the shared body part
  `{"key": "body", "datasets": [{"module": "editor", "shared": true}]}`
  on every type it mints and heals it onto an existing xKey on every
  ensure, so a type minted by any-ui or by an older bao gains a body
  the first time bao touches it. A user type is "page plus fields" by
  construction. `body=False` opts out; `_ensure_store` passes it —
  the store types stay bodiless. `weight` is not accepted anywhere.
- `parent=` files the object under the wiki collection (§5); `folder`
  is a wiki column. The object's type is whatever the caller said.

### 4. The membership surface

On `any@v1` (flat and `c.`), named for what a human says:

| verb | wire | rule |
|---|---|---|
| `set_type(space, object_id, type)` | `POST …/properties/:o/type/:t` | replaces the one type; old values stay as orphans; no unset exists |
| `add_to_collection(space, object_id, collection)` | `POST …/properties/:o/collections/:c` | idempotent |
| `remove_from_collection(space, object_id, collection)` | `DELETE …` | idempotent; values stay |
| `trash(space, object_id)` / `restore(space, object_id)` | add to / remove from the built-in `bin` | the server stamps `bin.movedAt` / `movedBy`; the skill offers trash before `delete_object` |
| `list_collections(space)` | `GET …/collections?includeHidden=true` | user collections and the catalog's (wiki, contact, …); the meta row `collection` and the built-ins `miniapp` / `bin` are filtered out |
| `create_collection(space, {name, xKey?, hidden?, properties?})` | `POST …/collections` + `…/properties` | the `create_type` composite without layout or body; the same handle guards (builtins, catalog handles on BOTH surfaces, record-root keys) |
| `list_properties` / `add_property` / `patch_property` / `delete_property` / `set_option` / `reorder_property` | `…/types/:t/properties*` or `…/collections/:c/properties*` | one property surface, routed by the owner's kind |

`list_types` returns the types a user picks from: hidden ones
included (bao's machinery, the catalog's hidden types), the four meta
rows `any`, `spaceIndex`, `type`, `collection` **omitted** — they
were listed-and-guarded, now they are neither offered nor nameable.
`attach_type` and `detach_type` do not exist; `patch_collection`
exists only if `patch_type` is on the surface at implementation time.

### 5. The wiki is a collection

`POST /v1/catalog/wiki/setup` answers `collectionId` and the three
property ids; the guest caches `{collectionId, parentId, pos,
folder}`. A tree object is `collections: [<wiki>]` with placement
values under `<wikiCollectionId>`; `move_object` files with
`add_to_collection` and writes the placement; `list_children` filters
on `<wikiCollectionId>.<parentId>` sorted on `pos`; "take it out of
the Wiki" is `remove_from_collection(space, obj, "wiki")`. A folder is
`type: page` with `folder: true`.

### 6. The catalog

`setup_app` echoes `collectionId` beside `typeId`; the reserved-handle
guard reads `type.xKey` AND `collection.xKey` from the catalog. A
contact is `type: person, collections: [contact]` and the skill says
so; the role facets are filed, never typed. `list_apps` is
`{"any.collections": "miniapp"}` minus `bin` sorted by `miniapp.pos`;
`list_programs` is `{"any.type": "program"}` minus `bin`. The general
chat is unchanged (ADR-027 §1): the derived root, found through setup.

### 7. Harness types and the bao bundle

- `program` and `agent_skill` are **listed** types (they carry a body
  and are classes a user may open): their ensures stop sending
  `hidden: true` and patch `hidden: false` onto an existing row. The
  store types (`agent_config`, `agent_secrets`, `agent_trigger`,
  `agent_brain`, `agent_log`, `mini_app`, `sync_state`, `enrichments`,
  `enrich_proposal`) stay hidden and bodiless. `mailbox` stays listed
  (ADR-027 §2).
- `bao/v1` is ensured with `rootType: "page"`; every child
  (`bao/config/v1`, `secrets`, `triggers`, `runs`, the chat's
  `bao/log/v1`) carries its store type as `type` — one each, as today.
  `ensure_typed` anchors and the credentials sweep filter on
  `any.type`. Every runtime and program create sends `type`.
- The host client gains only what serve and deploy use: bundle shapes,
  `list_collections`; `attach_type` is removed. Membership verbs are
  guest-only until a host consumer appears.

### 8. Fleet, pin, cutover

- Pin moves to any `b5be51c`; `api/openapi.vendored.json` re-vendored
  from a scratch server on that build (146 paths), `api/coverage.json`
  gains the twelve collection / membership endpoints mapped to §4 and
  loses the two attach / detach ones; `make api-drift` green.
- **Fresh accounts everywhere.** The mark is one-way and old rows are
  invisible, so every environment starts on a new account: new data
  dir, new `_agentrepo` / `_connectorsrepo` spaces (an old repo space
  lists zero programs — the ADR-010 §5 silent-zero class), new
  invites, new ids in every `anybao*.toml`, a fresh bao space. The
  prod bao space is recreated empty by decision. Order: the plan §6.
- **any-ui gates the cutover** of every shared server: the server is
  the model, so no `:7004` / `:7005` / `:7134` / mac server moves
  before any-ui's port is on main and every `any` binary in the fleet
  is rebuilt from one sha. anybao ports and verifies on a fresh
  staging account first.

### 9. Skills, docs, the acceptance check

`_any.md` is rewritten in §1's words: class / tag / supertag, one
type, the verbs of §4, filters of §2, the wiki as a collection, a
contact as a filed person, trash before delete, the new error codes.
`_meta_skill.md`, `_core.md` where they name a create shape. Docs
state the contract (helper-style, cutover-checklist, api-parity C8,
the ADR amendments of the table below); none narrates the move.

Acceptance is the plan's Phase 4b: fifteen user messages on the rig
that make bao speak and use the model — types vs collections, a
supertag with a column, filing and unfiling, set_type, the wiki, a
contact, trash and restore, memory on the recreated bao space —
every answer in the right words, every trace showing the right call,
no `any.types`, no "attach", no "collection" meaning type.

## Consequences

- The model's vocabulary changes once and matches the product's: one
  type, tags with columns. The old "which type is primary" ambiguity
  and the `page`-as-second-type trick disappear with it.
- Every existing space is left behind for bao: memory, turns,
  triggers, secrets, program sources start over on the new accounts.
  The email corpus re-syncs; the cron jobs rebuild what they can.
- Types minted by other clients without a body gain one when bao
  first ensures them — an additive part, visible in any-ui as a body
  tab.
- `program` and `agent_skill` appear in type pickers; a user can
  create a Program object by hand. Deploy's hash gate and the
  resolver read only well-formed rows, so a hand-made one is inert.
- Two deliberate guest errors (`any.types`, `types`) are the only
  text that mentions the old spelling, and only to name the new one.
- Three-repo window again: any (done), anybao (this), any-ui (no port
  yet, 132 files on `any.types`).

## Open questions

1. `patch_collection` — only if `patch_type` is already on the flat
   surface when §4 lands.
2. The any-ui WEB ticket for its half — the user files it.
3. SYN-260 — the three wire nits (stale route in
   `dataset.not_declared`'s message, `dataset.validation` on a set
   under an unfiled collection, `hidden: null` in listings) are
   Sergey's to validate; anybao reads absent-or-null as false and
   works around nothing.

## Amendments

Landed with the code that implements each section (the ADR-027
convention), one pointer line per affected section:

| ADR | Change |
|-----|--------|
| 006 §6 | the catalog spans types AND collections (one handle namespace); `any.type` / `any.collections` values speak xKeys; `collection` is the fourth synthetic row; the BOB-68 row-root guard covers collection handles; `any.types` is an error |
| 010 §5 | `program` is listed, not hidden |
| 010 §8 | `create_object` takes `type` / `collections` (default `page`); `set_type`, `add_to_collection`, `remove_from_collection`, `trash`, `restore`, `list_collections`, `create_collection`; `list_types` hides the meta rows |
| 017 §0, §1 | `bao/v1` root is `rootType: page`; children carry one `type`; `agent_skill` / `program` listed, the stores hidden |
| 022 §3 | link stubs `{id, name, type, collections}` |
| 027 §2 | the hidden set loses `program` and `agent_skill` |
| 027 §3 | bodies live on the object's type (the default type, §3 here); `create_object` never adds `page` to a typed object; `_ensure_body` is a guard, not an attach |
| 027 §5 | the sidebar and programs queries on `any.collections` / `any.type`; `list_apps` unchanged in shape |
| 027 §6 | pin `b5be51c`; the fleet rule adds fresh accounts and the any-ui gate |
