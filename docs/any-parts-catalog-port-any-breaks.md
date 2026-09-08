# Companion to any-parts-catalog-port-plan.md — the any-side breaking-change inventory a176029 → 5d709c8 (generated 2026-09-08 from the any repo at HEAD; §16 error diff, §17 route diff, §19 checklist).

# any HTTP API + data model: breaking-change inventory

**Range:** `a176029` (anybao's pin) → `5d709c8` (HEAD), 116 commits.
**Repo:** `/home/zarkone/any/any`
**Purpose:** port anybao (`/home/zarkone/any/anybao`) onto the new contract.

Dependency bumps (`go.mod`):

```
github.com/anyproto/any-store      v0.4.7  → v1.0.1
github.com/anyproto/any-sync       v0.13.1 → v0.13.2
github.com/anyproto/any-sync-sdk   v0.2.8  → v0.3.3
+ github.com/coder/websocket v1.8.14, github.com/tmc/go-iroh v0.1.0 (indirect)
```

Each section gives: (a) the old contract at `a176029`, (b) the new contract at
HEAD, (c) exact routes / body fields / error codes, (d) which anybao call sites
fail and how.

---

## Table of contents

1. [`nav` is deleted — the tree is the `wiki` usecase](#1-nav-is-deleted--the-tree-is-the-wiki-usecase)
2. [Types have parts; `editor` and `chat` became modules](#2-types-have-parts-editor-and-chat-became-modules)
3. [Editor routes gained a `:collection` segment](#3-editor-routes-gained-a-collection-segment)
4. [Runtime dataset collections renamed: `name` → `key`, `<typeId>_<key>`](#4-runtime-dataset-collections-renamed-name--key-typeid_key)
5. [Property `format` → `xFormat`; `xKind` / `meta.pos` / `meta.icon` deleted](#5-property-format--xformat-xkind--metapos--metaicon-deleted)
6. [Chat: reserved module, general chat is a catalog install](#6-chat-reserved-module-general-chat-is-a-catalog-install)
7. [Bundles: `datasets` → `parts`, plus `properties` / `xKey` / `layout` / `weight` / `hidden`](#7-bundles-datasets--parts-plus-properties--xkey--layout--weight--hidden)
8. [New: the usecase catalog (`/v1/catalog`)](#8-new-the-usecase-catalog-v1catalog)
9. [Built-in types: `page` rebuilt, `miniapp` / `bin` / `dataview` added, all hidden](#9-built-in-types-page-rebuilt-miniapp--bin--dataview-added-all-hidden)
10. [Types: `weight`, `layout`, `hidden`, `meta`, `PATCH …/types/:typeId`](#10-types-weight-layout-hidden-meta-patch-typestypeid)
11. [Query and subscribe](#11-query-and-subscribe)
12. [Backlinks replaced by a real link index](#12-backlinks-replaced-by-a-real-link-index)
13. [Dataset discovery reply changed](#13-dataset-discovery-reply-changed)
14. [Account CRDT version — the data-dir one-way door](#14-account-crdt-version--the-data-dir-one-way-door)
15. [Smaller contract changes](#15-smaller-contract-changes)
16. [Full error-code diff](#16-full-error-code-diff)
17. [Full route diff](#17-full-route-diff)
18. [New endpoints anybao may want](#18-new-endpoints-anybao-may-want)
19. [Port checklist](#19-port-checklist)

---

## 1. `nav` is deleted — the tree is the `wiki` usecase

Commit `cd8d1ae`. Docs: `docs/03-api.md:1683-1752` (§ The wiki tree),
`docs/28-well-known-bundles.md:373-383` (§ What clients delete),
`docs/09-query.md` § Paths, CLAUDE.md items 3 and 51.

### Old (`git show a176029:docs/03-api.md`, lines 1443-1512)

Every `POST …/objects` was auto-stamped server-side (`internal/nav`,
`injectNavDefaults`):

- `nav` appended to `any.types`
- `nav.type` — 1 = item, 2 = folder
- `nav.parentId` — string id of the parent folder, `""` = root
- `nav.pos` — lexid, **server-allocated** (next lexid after the current max in
  the target folder, `Middle()` when empty)

The create body accepted a third top-level key:

```json
{ "types": ["..."],
  "initialProperties": { "...": { "...": "..." } },
  "nav": { "type": 2, "parentId": "obj_parent_id", "pos": "PPQY" } }
```

`nav` was a **virtual built-in type**: surfaced through `GET …/types`
(`builtIn: true`) and `GET …/types/nav/properties`, with literal string property
paths (`nav.parentId`), not content-addressed propIds. Moves were
`POST …/properties/:oid/set/nav {"patch": {"parentId": …, "pos": …}}`.

### New

`internal/nav` is deleted. Nothing is appended to `types` server-side. Nothing
is stamped on create.

| call | new result |
|---|---|
| `POST …/objects` with a `"nav"` key | `400 request.unknown_field` naming the accepted set |
| `GET …/types` | `nav` no longer listed |
| `GET …/types/nav/properties` | `404 type.not_found` |
| `POST …/properties/:oid/set/nav` | no such type to resolve |
| filter/sort on `nav.parentId` / `nav.pos` in `objects/query` | no error; matches nothing (values on old rows are inert) |

The create vocabulary is now exactly two keys, enforced by
`checkUnknownFields` against `api.ObjectCreateRequest`
(`internal/api/doc_models.go:18-29`, `internal/server/handlers_objects.go:23,64`):

```go
type ObjectCreateRequest struct {
    Types             []string                  `json:"types,omitempty"`
    InitialProperties map[string]map[string]any `json:"initialProperties,omitempty"`
}
```

Shape is checked per field too (`types` must be an array, `initialProperties` an
object, each group an object of `{propertyId: value}`) → `400 request.schema`
(`handlers_objects.go:74-105`).

### Replacement: the wiki usecase

```
POST /v1/catalog/wiki/setup   {"spaceId": "<spaceId>"}
→ the bundles[] entry with id "system:wiki/v1":
    typeId                # <wikiTypeId>
    properties.parentId   # string; "" = top level
    properties.pos        # lexid string; orders siblings
    properties.folder     # boolean
```

An object is in the tree only when it carries the wiki type. Placement is three
ordinary property values at `<wikiTypeId>.<propId>`:

```json
POST /v1/spaces/:spaceId/objects
{ "types": ["page", "<wikiTypeId>"],
  "initialProperties": {
    "<wikiTypeId>": { "<parentIdPropId>": "", "<posPropId>": "a0", "<folderPropId>": false } } }
```

Children of a node (`""` lists the top level):

```json
POST /v1/spaces/:spaceId/objects/query
{ "filter": { "<wikiTypeId>.<parentIdPropId>": "<parentObjectId>" },
  "sort":   [ "<wikiTypeId>.<posPropId>" ] }
```

A move is one property write, no dedicated route:

```json
POST /v1/spaces/:spaceId/properties/:oid/set/<wikiTypeId>
{ "patch": { "<parentIdPropId>": "<newParent>", "<posPropId>": "<p>" } }
```

**`pos` is the client's.** The server allocates nothing. Compute it with the
lexid allocator the editor blocks use: alphabet `CharsAllNoEscape`, block size 4,
step 100 — match the Go side byte-for-byte.

`parentId` and `pos` carry `meta.index: none` (kept out of search) and are plain
strings, not relations, so `/backlinks` never reports a parent link. `folder`
being a boolean is never indexed. All three are unindexed on the `objects`
collection, so a children query is a scan (`docs/09-query.md` § Indexes).

### Anybao impact

**Editor block records keep their own `nav.parentId` / `nav.pos`** — that is the
block module's per-document tree, unrelated to the deleted object namespace. So
anybao's one *live* `nav` use is safe:

- `repos/_agent/programs/enrich@v1/program.py:175` —
  `c.query(space, transcript_id, "editor_blocks", sort=["nav.pos"], limit=BLOCK_LIMIT)`
  keeps working (see §4 for the collection-name caveat).

Dead code and one real break:

| file:line | what | action |
|---|---|---|
| `repos/_agent/programs/any@v1/program.py:1163` | `"nav"` in the accepted object-create key set; passed through verbatim | **BREAK** — remove; server now 400s |
| `repos/_agent/programs/any@v1/program.py:272` | `_RESERVED_GROUPS = {"any", "nav", "_ver"}` | drop `nav` |
| `repos/_agent/programs/any@v1/program.py:1235` | `_split_by_scope` passes reserved groups through | follows from above |
| `repos/_agent/programs/any@v1/program.py:2382` | `primary = next(t for t in types if t not in ("nav", "editor"))` | rewrite (see §9) |
| `repos/_agent/programs/recall@v1/program.py:22` | `RESERVED_GROUPS = {"any", "nav"}` | drop `nav` |
| `repos/_agent/skills/_any.md:31,95,264` | docs claiming `nav` is a builtin | rewrite |
| `tests/test_recall_module.py:32`, `tests/test_any_module.py:276-294,630-634,726-727,748,1008`, `tests/test_any_properties.py:37` | fixtures asserting the `nav` type / passthrough | rewrite |

---

## 2. Types have parts; `editor` and `chat` became modules

Commit `a7190ff` ("Types declare parts; editor and chat become modules").
Docs: `docs/03-api.md:2456-2568` (§ Parts and modules),
`docs/29-client-model.md:9-28`, CLAUDE.md item 45.

### The model

```
object ──carries──▶ type ──has──▶ part ──owns──▶ dataset (module)
  │                  │
  └─ property values └─ property definitions
```

A **type** is properties plus **parts**. A part is a display unit a client
renders; each part owns datasets served by a **module**:

- `records` — the generic schema-enforced module (the default when a dataset
  names none)
- `editor` — block bodies (compiled in, `internal/editor`)
- `chat` — messages (compiled in, `internal/chat`; **reserved**, see §6)

### Registered types: old vs new

`git show a176029:internal/server/sdk.go:109-116`:

```go
func serverTypes() []handler.Type {
    return []handler.Type{
        editor.NewType(),
        chat.NewType(),
        dataview.NewType(), // data_views
        nav.NewType(),
        page.NewType(),     // marker-only
    }
}
```

`internal/server/sdk.go:124-145` at HEAD:

```go
func serverTypes() []handler.Type {
    out := []handler.Type{
        dataview.NewType(), // hidden: dataviews + views
        page.NewType(),     // hidden: one part sharing editor_blocks
        miniapp.NewType(),  // hidden, property-only
        bin.NewType(),      // hidden, property-only
    }
    return append(out, extraCatalog.types...)
}

func serverModules() []handler.Module {
    out := []handler.Module{ editor.NewModule(), chat.NewModule() }
    return append(out, extraCatalog.modules...)
}
```

So: **`editor` and `chat` are no longer types.** `GET …/types/editor` and
`GET …/types/chat` are `404 type.not_found`. `types: ["chat"]` on object create
names a type the space does not have.

### Collections

Where a dataset's records live is the **collection** — the `dataset` value on
every read and write:

- **namespaced** (the default): **`<typeId>_<key>`**. Owned by this type alone;
  two types each declaring a `notes` editor part have two bodies. `records`
  datasets are always namespaced.
- **shared** (`"shared": true`): the module's canonical collection —
  `editor_blocks`, `chat_messages`. Every type declaring a shared editor part
  contributes to the same body. The key is the canonical name (omit it or spell
  it exactly; anything else is `400 dataset.shared_conflict`); one shared dataset
  per module per type (a second is `409 dataset.key_conflict`); only modules with
  a canonical collection share (`records` never does). `chat` is shared-only in v1.

### New routes

`internal/server/handlers_spaces.go:145-149`:

```
GET    /v1/spaces/:spaceId/types/:typeId/parts
POST   /v1/spaces/:spaceId/types/:typeId/parts                  → 201 {partId}
PATCH  /v1/spaces/:spaceId/types/:typeId/parts/:partId          → 204
DELETE /v1/spaces/:spaceId/types/:typeId/parts/:partId          → 204
POST   /v1/spaces/:spaceId/types/:typeId/parts/:partId/datasets → 201 {datasetDefId, collection}
```

**`POST /v1/spaces/:spaceId/types/:typeId/datasets` is REMOVED.** The path
`/spaces/:spaceId/types/:typeId/datasets` is registered for `GET` only
(`handlers_spaces.go:150`), so a POST is method-not-allowed, not 404.

Retained dataset routes (unchanged paths):

```
GET    /v1/spaces/:spaceId/types/:typeId/datasets
PATCH  /v1/spaces/:spaceId/types/:typeId/datasets/:defId
DELETE /v1/spaces/:spaceId/types/:typeId/datasets/:defId
POST   /v1/spaces/:spaceId/types/:typeId/datasets/:defId/fields
PATCH  /v1/spaces/:spaceId/types/:typeId/datasets/:defId/fields/:fieldId   ← NEW
DELETE /v1/spaces/:spaceId/types/:typeId/datasets/:defId/fields/:fieldId
```

### Part draft shape

```json
POST …/types/:typeId/parts
{ "key": "body", "name": "Description", "pos": "a0",
  "ui": { "type": "document" },
  "datasets": [ { "module": "editor", "shared": true } ] }
```

```json
{ "key": "transcript", "name": "Transcript", "ui": { "type": "table" },
  "uses": ["speakers"],
  "datasets": [ { "key": "segments", "idRule": "user",
      "fields": [ { "key": "time", "kind": "number" },
                  { "key": "speaker", "kind": "string" },
                  { "key": "text", "kind": "string", "mutableBy": "any" } ] } ] }
```

- `key` — slug `[a-z][a-z0-9_]*`, ≤64, unique among the type's parts, pinned
  (`409 dataset.key_conflict`; `400 dataset.decl_invalid` for a non-slug)
- `name` / `icon` / `pos` / `hidden` — display slice; clients sort parts by `pos`
- `ui` — widget descriptor `{type, config}`, client vocabulary, replaced whole
- `uses` — keys of other datasets of this type the part renders without owning
- `datasets` — initial declarations; `module` defaults to `records`; an unknown
  module is `400 dataset.module_unknown`; a module-served dataset (`editor`,
  `chat`) carries **no** `fields` → `409 dataset.module_owned`

`GET …/types/:typeId/parts` returns
`{parts: [{id, key, name?, icon?, pos?, hidden?, ui?, uses?, datasets: [DatasetDef…]}]}`,
each dataset carrying `collection`, `module`, `shared`, `partId`.

`PATCH …/parts/:partId` takes `{set, unset}` over the mutable leaves `name`,
`icon`, `pos` (strings), `hidden` (boolean), `ui` (object, replaced whole),
`uses` (array). `key` is pinned → `400 dataset.immutable`.

Every write on a registered built-in type is `400 type.registered`.

### The write gate: no write attaches a type

`docs/03-api.md:2510-2518`. CLAUDE.md item 45: "`editor.EnsureType` /
`chat.ensureType` are gone."

An object holds a collection **only while it carries a type whose part declares
it**. A write into a collection none of the object's types declare is
`400 dataset.not_declared` (new code) — the SDK's local write-time check, on
every module's write path. Attach the type first via object create `types` or
`POST …/properties/:objectId/attach/:typeId`. **No write attaches one.**

Inbound changes stay read-tolerant, so a peer that removed a part still applies
data from before the removal. Removing a part withdraws the declaration:
the collection stops accepting writes on objects carrying no other declaring
type; existing records are not cleaned up.

### Anybao impact

**Breaks:**

| file:line | call | failure |
|---|---|---|
| `runtime/src/anyapi.rs:893` | `POST …/types/{type_id}/datasets` | 405 — route gone |
| `repos/_agent/programs/any@v1/program.py:1851` | same | 405 |
| `runtime/src/anyapi.rs:705` (`put_markdown`), used by `runtime/src/deploy.rs:620-624` (README), `:780-795` (skills) | markdown PUT on a fresh object | `400 dataset.not_declared` — the object carries no editor-declaring type |
| `repos/_agent/programs/any@v1/program.py:1213-1214` | `create_object` with `markdown`/`body` = create then PUT | same |
| `repos/_agent/programs/deepResearch@v1/program.py:162` | `c.put_markdown(space, oid, markdown)` | same |
| `repos/_agent/programs/enrich@v1/program.py:241` | `if t.get("builtIn") and t.get("xKey") != "editor"` | `editor` is not a type at all |
| `runtime/src/testutil.rs:351,355` | stub route matcher for `types/:tid/datasets` GET/POST | update |

Fix for the markdown writes: include `page` in the object's `types` at create,
or declare your own document type with a shared editor part and register it as
a bundle.

---

## 3. Editor routes gained a `:collection` segment

`internal/server/handlers_spaces.go:86-92`.

| old (a176029) | new (HEAD) |
|---|---|
| `GET    …/objects/:o/editor/markdown` | `GET    …/objects/:o/editor/:collection/markdown` |
| `PUT    …/objects/:o/editor/markdown` | `PUT    …/objects/:o/editor/:collection/markdown` |
| `PATCH  …/objects/:o/editor/markdown` | `PATCH  …/objects/:o/editor/:collection/markdown` |
| `POST   …/objects/:o/editor/markdown/append` | `POST   …/objects/:o/editor/:collection/markdown/append` |
| `POST   …/objects/:o/editor/blocks` | `POST   …/objects/:o/editor/:collection/blocks` |
| `PATCH  …/objects/:o/editor/blocks/:blockId` | `PATCH  …/objects/:o/editor/:collection/blocks/:blockId` |
| `DELETE …/objects/:o/editor/blocks/:blockId` | `DELETE …/objects/:o/editor/:collection/blocks/:blockId` |

The old paths have one fewer segment, so they match **no route**.

`:collection` is the collection a type's part declared with
`{"module": "editor"}`:

- the canonical **`editor_blocks`** for a shared part — the body every document
  type shares, so an object carrying two such types has one body
- a namespaced **`<typeId>_<key>`** instance for a part that wants its own editor

A `:collection` no editor part in the space declares is
**`404 dataset.not_found`** (new code, `details.collection`). A write into a
collection the object's types do not declare is `400 dataset.not_declared`.

Reads still go through `POST …/query` with `dataset` set to the same
`:collection`, sorted on `nav.pos`.

`markdown.no_match`'s message now cites `.../editor/:collection/markdown`.

### Anybao impact

| file:line | literal |
|---|---|
| `runtime/src/anyapi.rs:691` | `GET /v1/spaces/{space_id}/objects/{object_id}/editor/markdown` |
| `runtime/src/anyapi.rs:705` | `PUT …/editor/markdown` body `{"content": …}` |
| `runtime/src/testutil.rs:374,378` | stub route matchers |
| `repos/_agent/programs/any@v1/program.py:1522` | `GET …/editor/markdown` |
| `repos/_agent/programs/any@v1/program.py:1529,1546` | `PUT …/editor/markdown` |
| `repos/_agent/programs/any@v1/program.py:1556` | `POST …/editor/markdown/append` |
| `runtime/src/broker.rs:2024` | secrets-guard test hitting `…/editor/markdown` |

anybao never calls `…/editor/blocks` (those exist only in
`api/openapi.vendored.json`), so only the markdown routes need the segment.

The body-field landmine noted at `runtime/src/anyapi.rs:688-690` (the PUT field
is `content`, not `markdown`) is unchanged.

---

## 4. Runtime dataset collections renamed: `name` → `key`, `<typeId>_<key>`

**The single largest data-model break for anybao.** Docs:
`docs/03-api.md:2569-2725` (§ Runtime dataset schemas),
`docs/25-favorites.md:13-18`.

### Old (`a176029:docs/03-api.md:2153-2290`)

```json
POST …/types/:typeId/datasets   → 201 {datasetDefId}
{ "name": "articles", "displayName": "Articles",
  "idRule": "user", "deleteBy": "author",
  "search": { "title": "title", "text": "body" },
  "fields": [ … ] }
```

> `name` — the dataset's collection name; pinned, **space-unique**
> (`409 dataset.name_conflict` against built-ins, handler datasets and other
> runtime definitions; `prop` / `schema` are reserved by the search indexer).

The `name` **was** the collection: the `dataset` value on `/query`, `/modify`,
`/upsert`, `/delete-records`.

`GET …/types/:typeId/datasets` returned
`{datasets: [{id, name, displayName?, description?, dynamic?, idRule, idPattern?, idMaxLen?, deleteBy, skipHistory?, search?, fields: […], invalid?, invalidReason?}]}`.

### New

```json
POST …/types/:typeId/parts/:partId/datasets   → 201 {datasetDefId, collection}
{ "key": "articles", "displayName": "Articles",
  "idRule": "user", "deleteBy": "author",
  "search": { "title": "title", "text": "body" },
  "fields": [ … ] }
```

`api.DatasetDraftRequest` (`internal/api/typedatasets.go:113-152`):

```go
Key         string `json:"key,omitempty"`      // slug inside the TYPE, pinned
Module      string `json:"module,omitempty"`   // "records" (default) | "editor"; "chat" reserved
Shared      bool   `json:"shared,omitempty"`
DisplayName string `json:"displayName,omitempty"`
Description string `json:"description,omitempty"`
Dynamic     bool   `json:"dynamic,omitempty"`
IdRule      string `json:"idRule,omitempty"`
IdPattern   string `json:"idPattern,omitempty"`
IdMaxLen    int    `json:"idMaxLen,omitempty"`
DeleteBy    string `json:"deleteBy,omitempty"`
SkipHistory bool   `json:"skipHistory,omitempty"`
Search      *DatasetSearchFields `json:"search,omitempty"`
Fields      []DatasetFieldDraft  `json:"fields,omitempty"`
```

There is **no `name` field**. The draft is bound with `bindBodyStrict`
(`internal/server/handlers_typedatasets.go:88`), so sending `name` is
**`400 request.unknown_field`**.

- `key` is a slug inside the type, unique among the type's parts and datasets
  → `409 dataset.key_conflict` (replaces `dataset.name_conflict`).
- Keys are **never space-unique**: two types can each declare `entries`.
- The collection is **server-computed** as `<typeId>_<key>` (or the module
  canonical when shared) and returned as `collection`.
- Definitions racing in from other members fold by key SDK-side (smallest
  definition id wins; a pinned-leaf disagreement marks the fold `invalid`).

`GET …/types/:typeId/datasets` read-back (`internal/api/typedatasets.go:251-280`):

```go
type DatasetDefResponse struct {
    Id          string `json:"id"`
    Key         string `json:"key"`          // was: Name
    Collection  string `json:"collection"`   // NEW — the read/write address
    Module      string `json:"module"`       // NEW
    Shared      bool   `json:"shared,omitempty"`   // NEW
    PartId      string `json:"partId"`       // NEW
    DisplayName string `json:"displayName,omitempty"`
    Description string `json:"description,omitempty"`
    Dynamic     bool   `json:"dynamic,omitempty"`
    IdRule      string `json:"idRule"`
    IdPattern   string `json:"idPattern,omitempty"`
    IdMaxLen    int    `json:"idMaxLen,omitempty"`
    DeleteBy    string `json:"deleteBy"`
    SkipHistory bool   `json:"skipHistory,omitempty"`
    Search      *DatasetSearchFields `json:"search,omitempty"`
    Fields      []DatasetFieldDef    `json:"fields"`
    Invalid       bool   `json:"invalid,omitempty"`
    InvalidReason string `json:"invalidReason,omitempty"`
}
```

Field read-back gained `description`, `shape` and `xFormat`
(`DatasetFieldDef`, `internal/api/typedatasets.go:282-300`).

New: **`PATCH …/types/:typeId/datasets/:defId/fields/:fieldId`**
(`TypesAPI.PatchDatasetField`) — edits `name`, `description` and every path
under `xFormat` under the property-PATCH rules. `key`, `kind`, `shape`,
`scope`, `required`, `mutableBy`, `stamp` are pinned → `400 dataset.immutable`.

Registered built-in types (`dataview`, `page`, `miniapp`, `bin`) refuse
`400 type.registered` — their datasets are statically declared.

**No back-compat and no migration path in the server.** There is no legacy
handling in `handlers_typedatasets.go` / `handlers_typeparts.go` /
`internal/bundles`.

### Every anybao collection name changes

These are the `dataset` values on `/query`, `/query/subscribe`, `/modify`,
`/upsert`, `/delete-records`:

| old collection | new collection | declared at |
|---|---|---|
| `agent_config` | `<typeId>_agent_config` | `runtime/src/serve.rs:252-262` |
| `agent_secrets` | `<typeId>_agent_secrets` | `runtime/src/serve.rs:264-296` |
| `agent_triggers` | `<typeId>_agent_triggers` | `runtime/src/serve.rs:297-305` |
| `agent_runs` | `<typeId>_agent_runs` | `runtime/src/serve.rs:306-314` |
| `program_source` | `<typeId>_program_source` | `runtime/src/program_schema.rs:39-43` |
| `program_manifest` | `<typeId>_program_manifest` | `runtime/src/program_schema.rs:44-47` |
| `agent_memory_items` | `<typeId>_agent_memory_items` | `any@v1/program.py:91` |
| `agent_job_state` | `<typeId>_agent_job_state` | `any@v1/program.py:120` |
| `agent_roi_injections` | `<typeId>_agent_roi_injections` | `any@v1/program.py:127` |
| `agent_turns` | `<typeId>_agent_turns` | `any@v1/program.py:132` |
| `agent_chunks` | `<typeId>_agent_chunks` | `any@v1/program.py:154` |
| `mini_app` | `<typeId>_mini_app` | `miniapp@v1/program.py:30-35` |
| `enriched_data` | `<typeId>_enriched_data` | `enrich@v1/program.py:53-68` |
| `enrich_proposal_items` | `<typeId>_enrich_proposal_items` | `enrich@v1/program.py:74-86` |
| `email_messages` | `<typeId>_email_messages` | `gmailSync@v1/program.py:47-80` |

Unchanged (module canonicals): `chat_messages`, `editor_blocks`.

### Read/write sites to re-key

Reads and writes by literal collection name, from the anybao scan:

- `chat_messages` — `runtime/src/triggers.rs:311,380`; `runtime/src/serve.rs:2317,2725`;
  `any@v1/program.py:1590,2301`; `history@v1/program.py:144`;
  `tools/eval/anyeval.py:45`; `skills/_any.md:247` — **no change needed**
- `editor_blocks` — `enrich@v1/program.py:174`; `runtime/src/anyapi.rs:648`;
  `tests/test_enrich_*` — **no change needed** (shared canonical)
- `agent_turns` — `runtime/src/serve.rs:2130`; `any@v1/program.py:2274`;
  `history@v1/program.py:148,171`; `extraction@v1.py:94`; `rollup@v1.py:61`;
  `recall@v1/program.py:124`; `autorecall@v1.py:24,79`; `tools/eval/anyeval.py:45`
- `agent_chunks` — `any@v1/program.py:2282`; `history@v1/program.py:155`;
  `rollup@v1.py:36,78`; `recall@v1/program.py:129`; `autorecall@v1.py:24,65,81`
- `agent_memory_items` — `decay@v1.py:33`; `reflection@v1.py:60`;
  `evolution@v1.py:69,83`; `linkgen@v1.py:85`; `recall@v1/program.py:63,114`;
  `autorecall@v1.py:141,151`; `toolcaller@v1.py:611`
- `agent_job_state` — `extraction@v1.py:24,52`; `evolution@v1.py:19,47`;
  `linkgen@v1.py:21,48`
- `agent_config` — `runtime/src/serve.rs:384,425,459,1357,4021`;
  `config@v1/program.py:26,36`
- `agent_secrets` — `runtime/src/serve.rs:767,806,821,840,1107,1275,1345,1948,2036`;
  **and the broker deny-list at `runtime/src/broker.rs:1364`**
- `agent_triggers` — `runtime/src/serve.rs:2663`; `runtime/src/anyapi.rs:669,1272`;
  `gmailSync@v1/program.py:667-668`
- `agent_runs` — `skills/_core.md:91,196`
- `program_source` — `runtime/src/deploy.rs:449,521,1120,1139`;
  `runtime/src/resolver.rs:354,532`; `programs@v1/program.py:299,425`
- `program_manifest` — `runtime/src/deploy.rs:458,529,534,1190,1203`
- `email_messages` — `gmailSync@v1/program.py:46,410,542,587,866`;
  `ui@v1/program.py:16,68`
- `mini_app` — `miniapp@v1/program.py:26` and its read/write paths
- `enriched_data` — `enrich@v1/program.py:510`
- `enrich_proposal_items` — `enrich@v1/program.py:409,454`

Device-local `/v1/local/*` collections (`trace_records`, `trace_blobs`,
`trace_runs`, `runtime/src/tracestore.rs:423-425`) are **unaffected** — a
different surface.

### Two different failure modes

- A **read** of a collection the space does not serve answers
  `200 {"records": []}` — **silent**, not an error. Expect empty agent memory,
  empty history, empty program source, rather than a crash.
- A **write** answers `400 dataset.unknown` (`details.dataset` — the space
  serves no such records collection; a module collection such as
  `chat_messages` is never upsertable) or `400 dataset.not_declared`
  (`details.dataset`, `details.objectId` — served, but this object carries no
  declaring type).

### Two silent hazards

1. **Idempotence checks stop matching.** `runtime/src/serve.rs:173-185`
   (`ensure_dataset`, name-match, never reconciles),
   `runtime/src/program_schema.rs:108-119`, and
   `any@v1/program.py:1780-1863` all key on `d.get("name")`, which no longer
   exists in the reply. They will re-declare on every run.
2. **The secrets guard stops firing.** `runtime/src/broker.rs:1364` refuses a
   write when `body_dataset == Some("agent_secrets")`. That literal never
   matches `<typeId>_agent_secrets`. **Fix this early** — it is a security
   control, not a convenience.

### Existing data

Records already written sit physically under the old collection names. Nothing
in the server migrates them. Treat pre-upgrade agent data (memory, turns,
chunks, config, secrets, email corpus, program source) as unreachable unless
you migrate it yourself.

### Favourites, as the worked example

`docs/25-favorites.md:60-78` — the canonical ensure body moved from

```json
{ "id": "favorites/v1", "name": "Favorites",
  "datasets": [{ "name": "entries", "idRule": "user", "dynamic": true, … }] }
```

to

```json
{ "id": "favorites/v1", "name": "Favorites", "hidden": true,
  "parts": [{ "key": "entries", "datasets": [{ "key": "entries", "idRule": "user", "dynamic": true, … }] }] }
```

and every read/write moved from `"dataset": "entries"` to
`"dataset": "<rootId>_entries"` — "read it off the parts list rather than
composing it."

---

## 5. Property `format` → `xFormat`; `xKind` / `meta.pos` / `meta.icon` deleted

Commits `306e311` ("Property & field descriptors: one opaque xFormat bag"),
`5030024` ("Harden the descriptor surface after review"),
`524e407` (built-in fields carry descriptions and descriptors).
Docs: **`docs/27-descriptors.md`** (new, 590 lines), `docs/03-api.md:2302-2455`,
`docs/29-client-model.md:130-198`, CLAUDE.md item 44.

`docs/27-descriptors.md:13-18` states the migration policy outright:

> **Migration: none. Definitions written before this are not converted.**
> A property created under the former `format` object reads by `kind` alone — a
> date renders as a bare instant, a URL as text, a select as an array of opaque
> keys, because its labels and colours sit under a key nothing looks at any
> more. Spaces predating this are recreated, not upgraded.

### Wire shape

`api.AddPropertyRequest` (`internal/api/types.go:60-90`):

```go
Name        string          `json:"name,omitempty"`
Description string          `json:"description,omitempty"`
XKey        string          `json:"xKey,omitempty"`
Kind        string          `json:"kind"`              // REQUIRED, pinned
Meta        map[string]string `json:"meta,omitempty"`  // only meta.index
XFormat     json.RawMessage `json:"xFormat,omitempty"`
Scope       string          `json:"scope,omitempty"`
```

No `Format`. No `XKind`. Bound with `bindBodyStrict`
(`internal/server/handlers_types.go:121`), so **`format` and `xKind` are
`400 request.unknown_field`**.

`kind` is required and pinned — **nothing is defaulted from the descriptor**.
Under the old contract `kind` could be omitted when a `format` was set.

Spelling: the HTTP wire and PATCH paths use `xKey` / `xFormat`; the stored
record fields are `x-key` / `x-format`.

### The guarantee boundary

> **`kind` is the guarantee. `xFormat` is a hint.**

| | enforced on every peer at apply |
|---|---|
| **type property values** | the top-level `kind`, and nothing else |
| **dataset record fields** | `kind` recursively through `items` / `properties`, plus `required`, `mutableBy`, `stamp`, `idRule`, `deleteBy` |

`any` (not the SDK) is the semantics boundary: it validates **every property
write** against the current slug's shape and rejects what does not fit.

### The `xFormat` bag

```json
"xFormat": {
  "type":     "choice",
  "icon":     "tag",
  "pos":      "a6",
  "options":  { "<key>": { "name": …, "color": …, "pos": …, "meta": {} } },
  "relation": { "targetTypes": ["…"], "filter": "<json text>" },
  "config":   { "<per-format>": … },
  "links":    "link | links | markdown | none"
}
```

Seven interpreted keys. Any other top-level key is a **vendor namespace**
(`acme`), stored verbatim at any depth, never validated. Every key (vendor
subtrees included) must be non-empty, contain no `.` and not start with `$`.
`"xFormat": null` on create reads as absent.

Reserved and unwritten: `validate`, `compute` → `400 property.format_invalid`
on a `set`; an `unset` stays allowed as the repair path.

### Old → new vocabulary

| old `format.type` (a176029:docs/03-api.md:2038-2076) | new `xFormat.type` + `kind` |
|---|---|
| `links` (array of `any://<objectId>`) | `relation`, `kind: array`, `xFormat.relation.targetTypes` names types by **xKey** |
| `select` (a single option key, `kind: string`) | `choice`, `kind: array` — value is **always an array**, `["qualified"]` |
| `multiselect` | `choice` + `config.multiple: true`, `kind: array` |
| `date` (instant at midnight UTC, or ISO string on `kind: string`) | `date`, `kind: datetime` — a date slug on a string kind is **rejected at create** |
| `datetime` | `datetime`, `kind: datetime` |
| `format.ui` (`select`/`multiselect`/`link`/`links`) | **deleted** |
| `format.filter` (a mongo condition object) | `xFormat.relation.filter` — a **single JSON-text leaf** |
| `format.options.<k>.{name,color,pos,meta}` | `xFormat.options.<k>.{name,color,pos,meta.<k>}` (same shape, new prefix) |
| `format.meta` (string→string bag) | gone — use `xFormat.config` or a vendor key |
| `xKind` (free-form marker; anybao used `url`/`email`/`longtext`) | **deleted** — real slugs `url` / `email` / `longtext` on `kind: string` |
| `meta.pos` (lexid display order) | **deleted** — `xFormat.pos` |
| `meta.icon` (icon name) | **deleted** — `xFormat.icon` |
| `meta.index` | **kept** — the only `meta` key the server accepts |
| `tags` | reserved for a future space-level shared tag table → `400 property.format_invalid` |

### Full v1 slug vocabulary (`docs/27-descriptors.md` § v1 vocabulary)

| `type` | `kind` | `options` | `relation` | `config` | value check |
|---|---|---|---|---|---|
| `text` | string | | | | string |
| `longtext` | string | | | | string |
| `markdown` | string | | | | string; the link index scans it for `any://` refs |
| `url` | string | | | | absolute URL with a scheme |
| `email` | string | | | | one `local@domain` |
| `phone` | string | | | | string |
| `choice` | array | ✓ | | `multiple` `optionSort` | non-empty option keys; one unless `multiple` |
| `relation` | array | | ✓ | `multiple` | bare `any://<objectId>` URIs; one unless `multiple` |
| `number` | number | | | `decimals` `separators` `showAs` `outOf` `color` | number |
| `currency` | number | | | + `currency` | number |
| `percent` | number | | | `decimals` `showAs` | number |
| `rating` | number | | | `max` `glyph` | number, `0 ≤ v ≤ max` when `max` set |
| `checkbox` | boolean | | | | boolean |
| `date` | datetime | | | `datePattern` | instant at **midnight UTC** |
| `datetime` | datetime | | | `datePattern` `timePattern` `zone` | instant |
| `duration` | number | | | `unit` | number |
| `period` | object | | | | `{from?, to?}` instants, at least one, `to ≥ from` |
| `money` | object | | | | `{amount: number, currency: string}` exactly |
| `geo` | object | | | | `{lat, lng}` in range exactly |

The slug set is **open** — an unknown value is never an error; it renders
structurally from `kind` and gets no value checks.

### `meta` narrowed to `index`

`docs/03-api.md:2340-2348`:

> **`meta`** — the consumer flags the server interprets: only **`meta.index`**
> … Any other key → `400 request.invalid_field`; descriptive metadata (display
> order, icon, …) lives under `xFormat`.

`meta.index` semantics are unchanged (absent ⇒ scope `props`; `"<scope>"` ⇒ that
scope; `"none"` ⇒ excluded from the **text** index — but a property carrying a
link marker still reports its edges, `docs/13-index.md` § Links).

### PATCH rules changed

`docs/03-api.md:2380-2420`, `docs/27-descriptors.md` § Editing: leaves only.

**Mutable:** `name`, `description`, `xKey`, `meta.index`, and every path under
`xFormat` — **including `xFormat.type`**, which was pinned as `format.type`
before. A slug now moves freely *within* its pinned `kind`:

| `kind` | slug moves between |
|---|---|
| `string` | text · longtext · url · email · phone |
| `number` | number · currency · percent · rating · duration |
| `datetime` | date · datetime |
| `object` | period · money · geo |
| `array` | choice ↔ relation (legal, lossy-looking; clients should not offer it) |

**Pinned** → `400 property.immutable`: `kind`, `scope`, `items`, `properties`.
Note `format` / `format.type` are no longer *pinned* paths — they are *unknown*
paths → `400 request.invalid_field` (as is `xKind`).

**Leaf-only rule (structural).** A `set` **never carries an object**. These can
only be **unset**, never set: `xFormat`, `xFormat.options`,
`xFormat.options.<key>`, `xFormat.options.<key>.meta`, `xFormat.relation`,
`xFormat.config`, `meta`. Violation → `400 request.invalid_field`.

Interpreted leaves are typed: `type` / `icon` / `pos` / option
`name` / `color` / `pos` / `meta.<k>` are strings; `relation.targetTypes` an
array of strings; `relation.filter` a string that parses as a query condition;
`config.<k>` a scalar. Outside `xFormat` a set value is a JSON string; `null` is
refused, a clear is an unset.

Example:

```json
{ "set":   { "xFormat.options.high.name": "High",
             "xFormat.options.high.color": "red",
             "xFormat.options.high.pos": "a0",
             "xFormat.config.multiple": true },
  "unset": [ "xFormat.options.low" ] }
```

### New code: `409 property.xkey_conflict`

`xKey` is unique **within one type**, checked on add and on rename
(`details: {xKey, existingPropId}`). It is a read-then-create preflight, not a
guarantee: two devices working apart can both land the same handle, and then
**both columns persist** with different ids. That is the defined behaviour;
resolving it is the client's job.

### Merge model to know

`options`, `options.<key>`, `relation` and `config` are **nested** — two authors
adding two options both land; an option entry is **not atomic**, so two authors
adding the same key produce one entry with a name from each.
`relation.filter` is a **single JSON-text leaf** (a field-merged query condition
is not a valid query) and may outlive the `targetTypes` it was written for.

`relation.filter` paths are `<typeId>.<propId>` — resolve xKeys before writing
one. **A filter does not survive a bundle**: property ids are content-addressed
per space, so a filter shipped in a bundle is inert on arrival. Only
`targetTypes` travels.

### Composites

`period`, `money`, `geo` are `kind: object`, written **whole** (a true replace —
no sub-path unset). Object-kind values are **not search-indexed**. Nested
descriptors are pinned wholesale.

### Value shapes that bite (`docs/29-client-model.md:162-169`)

| descriptor `type` | kind | value |
|---|---|---|
| `text` / `url` / `email` / `phone` | `string` | a string |
| `date` / `datetime` | `datetime` | `{"$date": "2026-08-05T17:00:00.000Z"}` — `date` must land on midnight UTC |
| `relation` | `array` | `["any://<objectId>"]` — **a bare object id is rejected** |
| `choice` | `array` | **always an array** of option **keys**, not names |
| `number` / `currency` / `percent` | `number` | a number |
| `checkbox` | `boolean` | a bool |

`xFormat.relation.targetTypes` holds **xKeys**, not type ids.

### Where values are validated

`400 property.format_violation` (`details: {propId, format, reason}`) on:
`POST …/properties/:objectId/set/:typeId`, object-create `initialProperties`,
and bundle `rootProperties`. Option membership is **not** enforced
(dangling-tolerant), nor is object existence or type. Known gap: raw
`POST /v1/spaces/:spaceId/modify` against the `properties` dataset bypasses
value validation.

The write side is **strict** — an unchanged re-save of a value stranded by a
slug flip is also rejected. The client rule is *never silently rewrite*:
surface the error, do not normalise the value.

### Anybao impact

`xFormat` appears **nowhere** in anybao. The wire spelling everywhere is
`format`, plus `xKind`.

Encoder to rewrite — `repos/_agent/programs/any@v1/program.py:1897-1965`:

```python
fmt = body.get("format")                                  # :1920
if fmt["type"] in _XKIND_MARKERS:  body.pop("format")     # :1922-1928
                                   body.setdefault("xKind", fmt["type"])   # :1930
elif fmt["type"] not in _FORMATS:  raise ValueError(...)  # :1937
else: body.setdefault("xKind", _XKIND_OF_FORMAT[fmt["type"]])  # :1940
      opts = fmt.get("options"); fmt["options"] = {}      # :1941-1951
      body["format"] = fmt                                # :1952
meta = dict(body.get("meta") or {})                       # :1957
meta["pos"] = _lexid_after(last)                          # :1959  ← now a 400
body["meta"] = {k: str(v) for k, v in meta.items()}       # :1962
POST f"/v1/spaces/{space}/types/{type_id}/properties"     # :1964
```

That `meta["pos"]` write alone fails **every** property create with
`400 request.invalid_field` (meta is narrowed to `index`).

Constants to rewrite:

| file:line | constant | note |
|---|---|---|
| `any@v1/program.py:288` | `_FORMATS = ("select","multiselect","links","date","datetime")` | remap to slugs |
| `any@v1/program.py:294` | `_XKIND_MARKERS = ("url","email","longtext")` | become real slugs |
| `any@v1/program.py:298-299` | `_XKIND_OF_FORMAT` | delete |
| `any@v1/program.py:281-287` | `_MARKER_XKEYS` | delete (CLAUDE.md item 44: "the legacy marker-xKey set and any denylist built from it") |
| `any@v1/program.py:307-309` | `_PINNED_PATHS` includes `"format"`, `"format.type"` | replace: pinned is `kind`, `scope`, `items`, `properties` |
| `any@v1/program.py:300-301` | `_KINDS` | unchanged |
| `any@v1/program.py:304-305` | `_OPTION_COLORS` | `color` is an open string now |
| `any@v1/program.py:310` | `_ARCHIVED_META = "anyUiArchived"`, read at `:351` | a `meta` key other than `index` → `400`; move under a vendor `xFormat` key |

Readers to rewrite: `:355` (`_options_of`), `:640-684` (format dispatch),
`:759` (`format.filter` for links candidates), `:976-982`, `:1066-1074`,
`:1092`, `:1684`, `:2010-2022` (`upsert_option`), and
`recall@v1/program.py:192`.

PATCH sites: `:855`, `:2004`, `:2043`, `:2059`, `:2088`, `:2102`, and the
per-type re-pos at `:2067`, `:2086-2089` (`{"set": {"meta.pos": pos}}` → must
become `xFormat.pos`).

Unaffected property creates (no format, `meta.index` only):
`runtime/src/deploy.rs:734`; `runtime/src/program_schema.rs:100-102`;
`programs@v1/program.py:210-218`; `gmailSync@v1/program.py:44,84-94`.

Test fixtures encoding the old shape: `tests/test_any_properties.py:15,20,23,26,28,217,222,388-400`.

Docs to rewrite: `repos/_agent/skills/_any.md:74-75`
(`{"format": {"type": "links", "filter": {"any.types": "page"}}}`).

Note also that a `relation` property is what feeds the link index — a property
created without the `relation` slug indexes nothing until patched with
`{"set": {"xFormat.type": "relation"}}` (`docs/29-client-model.md:150-155`).

---

## 6. Chat: reserved module, general chat is a catalog install

Commits `0894704` ("General chat under the reserved chat module"),
`e3b666a`, `6f0a5d5` (miniapp sidebar / general chat as a mini app),
`8a63272`. Docs: `docs/03-api.md:2968-3040` (§ Chat),
`docs/16-chat.md:1-55`, `docs/28-well-known-bundles.md:386-396`,
`docs/29-client-model.md:306-315`, CLAUDE.md items 24 and 52.

### Old (`a176029:docs/03-api.md:2433-2470`)

`chat` was a registered built-in type. The client convention:

```
POST /v1/spaces/:spaceId/bundles
{ "id": "general-chat/v1", "name": "General", "rootTypes": ["chat"], "derived": true }
→ 200 { "bundle": { "rootId": "<chat object>", "derived": true, ... }, "installed": true|false }
```

> `id` is yours to choose; `general-chat/v1` is the convention for "the chat of
> this space", and a space can carry as many purpose-specific chat bundles as
> you want.

### New

There is **no `chat` type**. `chat` is a `handler.Module` with
`Reserved: true`. A client part, part dataset or bundle body naming it is
**`400 dataset.module_reserved`**, decided from the compiled-in catalog before
any wait. Only the server's own catalog install (through
`space.SystemInstall()`) or a registered type's static part may declare it.

The one declaration is the catalog's `general-chat` usecase:

```
POST /v1/catalog/general-chat/setup
{ "spaceId": "<spaceId>" }
→ 200 { "usecase": "general-chat", "bundles": [ { "id": "system:general-chat/v1",
        "bundle": { "rootId": "<chat object>", "derived": true, ... },
        "installed": true|false, "typeId": "<chat object>" } ] }
```

The root `system:general-chat/v1` is:

- **derived** — its id is a function of (space, bundle id), computed offline, so
  two sides of a 1-1 meet on the one object and it can never fork
- **hidden**
- **self-typed** — handle `general_chat`, `layout {"type": "chat"}`
- one shared `chat` part, so it holds `chat_messages` from the first write
- a **`miniapp` carrier** (`bundle = system:general-chat/v1`), so the chat is a
  sidebar entry like every other app

**Sole-carrier rule.** The root is its type's **only** carrier. Creating an
object with that type, `POST …/properties/:objectId/attach/<general_chat>`, or
an `any.types` op through `…/modify` is **`400 type.reserved_carrier`** (new
code; `details {typeId, spaceId}`). Implementation:
`internal/server/reserved_carrier.go:25-50`, message:

> this type declares a module reserved to the server and is carried only by its
> own root — the space's catalog install, not another object
> (POST /v1/catalog/{usecaseId}/setup returns it)

The chat write routes are unchanged in shape; a write on any other object is
`400 dataset.not_declared`:

```
POST   /v1/spaces/:spaceId/objects/:objectId/chat/messages
PATCH  /v1/spaces/:spaceId/objects/:objectId/chat/messages/:msgId
DELETE /v1/spaces/:spaceId/objects/:objectId/chat/messages/:msgId
POST   /v1/spaces/:spaceId/objects/:objectId/chat/messages/:msgId/reactions/:emoji
POST   /v1/spaces/:spaceId/objects/:objectId/chat/read-all
POST   /v1/spaces/:spaceId/objects/:objectId/chat/messages/:msgId/read
POST   /v1/spaces/:spaceId/objects/:objectId/chat/messages/:msgId/reactions-read
```

Read tracking, unread flags, the message wire shape and the descending
`-_ver.id` subscribe recipe are unchanged.

### No back-compat, and the split-brain risk

`docs/28-well-known-bundles.md:392-396`:

> No back-compat: a chat registered under the former client recipe
> (`general-chat/v1`) is a different root the server neither detects nor adopts
> — the usecase installs the chat anew, and the old root is left behind.

`docs/16-chat.md`: the old root "still carries its declaring type and still
takes writes (the rule refuses additions only) — so `chat_messages` `owners`
can list two ids there."

So on an upgraded anybao space you get **two chat roots**: the old
`general-chat/v1` created-or-derived root that still accepts writes, and the new
`system:general-chat/v1` catalog root that every new client resolves. Plan the
cutover explicitly.

### Anybao impact

Both ensure sites send the identical old body:

- `runtime/src/serve.rs:105` (`const ID = "general-chat/v1"`) + `:129`
  `c.ensure_bundle(space, ID, "General", &["chat"], true)`
- `repos/_agent/programs/any@v1/program.py:1621-1622`
  `root_types=["chat"]`

Failure chain:

1. `rootTypes: ["chat"]` → **`400 type.not_found`** — "rootTypes names a type
   this space does not have" (`internal/server/handlers_bundles.go:403-406`).
   The check runs **before** the root is created, so nothing is orphaned.
2. Dropping `rootTypes` does not help: the resulting root has no chat part, so
   `POST …/chat/messages` on it is `400 dataset.not_declared`.
3. `get_bundle(space, "general-chat/v1")["rootId"]`
   (`any@v1/program.py:1597`) keeps returning the **old** root on an upgraded
   space.
4. `runtime/src/serve.rs:111` asserts `bundle["derived"] == true` and bails
   otherwise — keep an equivalent assertion against the catalog reply's
   `bundle.derived`.
5. Objects created with `{"types": ["chat"]}` —
   `tests/test_integration.py:24,34,44,54`,
   `tests/test_recall_integration.py:52` — now carry an unknown type that the
   SDK drops, and every chat write on them is `400 dataset.not_declared`.
6. `tests/test_instants_integration.py:56-57`,
   `tests/test_any_module.py:854-855`, `runtime/src/anyapi.rs:1463-1481`,
   `runtime/src/serve.rs:3537` all assert the old body.
7. `tools/eval/anyeval.py:43` reads `generalChatObjectId` off
   `GET /v1/spaces/{space}` — that field was already removed before the pin and
   stays gone.

Also changed in `docs/16-chat.md`:

- Chat `createdAt` / `modifiedAt` come from the change envelope's clock, which
  is **second-resolution**, so they are display-only. Detect an edit with
  `_ver.text != _ver.id`, not by comparing stamps.
- Listing a space's chats is now
  `{"any.types": {"$in": [<owners of chat_messages>]}}`, resolved from
  `GET /v1/spaces/:spaceId/datasets`, not `{"any.types": "chat"}`.
- Every `any://` reference in a message (text links, mentions, each
  attachment's `link`, the agent group's `debugLink`) now lands in the link
  index (§12).

`runtime/src/triggers.rs:311,380` and `runtime/src/serve.rs:2317,2725` watch
`chat_messages` — the collection name is unchanged, but the `objectId` must
become the catalog root.

---

## 7. Bundles: `datasets` → `parts`, plus `properties` / `xKey` / `layout` / `weight` / `hidden`

Commits `43ad390` ("Bundles declare a full type; reserved modules and ids"),
`df51fa3`, `dfdd059`, `175c768`, `5a5ab94`.
Docs: `docs/03-api.md:977-1300`, CLAUDE.md item 45(c).

### Ensure body

**Old:** `{id, name?, rootTypes?, rootProperties?, derived?, datasets?}`

**New:** `{id, name?, rootTypes?, rootProperties?, derived?, parts?, properties?, xKey?, layout?, weight?, hidden?}`
(`internal/api/bundle.go:45-70`)

The body is checked against the closed set derived from `BundleEnsureRequest`
(`internal/server/handlers_bundles.go:47-50,105`), so the old **`datasets` key
is `400 request.unknown_field`**.

### Bundle-declared types

`parts`, `properties` or an `xKey` make the root implement itself as a type:
`any.types = ["__type__", "<rootId>"]`, `typeId = rootId`, readable through
`GET …/types/:rootId` and its `parts` / `properties` / `datasets` routes.
`layout`, `weight` and `hidden` describe that type and ride along — **alone**
they are `400 request.invalid_field`.

- **`parts: [...]`** — the `POST …/types/:typeId/parts` draft shape, ≤32
  entries / 64 KiB. A records dataset the bundle declares is namespaced to the
  root: collection **`<rootId>_<key>`** (read it off `collection` in the parts
  list). A part naming a module (`{"module": "editor", "shared": true}`) makes
  the root hold that module's canonical collection. A part naming a reserved
  module (`chat`) is `400 dataset.module_reserved`.
- **`properties: [...]`** — the `POST …/types/:typeId/properties` draft shape,
  ≤64 entries / 64 KiB. **Every draft must carry an `xKey`**, unique in the body
  (`400 request.missing_field` / `400 request.invalid_field`), because the
  **property id is derived from (rootId, xKey)** — two devices installing while
  apart mint ONE column per handle. Resolve `xKey → propId` through
  `GET …/types/:rootId/properties`. Each draft passes the property gate.
- **`xKey`** — the type's handle. An xKey **alone** declares a marker type — no
  columns, no parts, a flag objects carry. Unique among the space's types: an
  install whose xKey a type already holds (as its xKey or its id, hidden or not)
  is `409 type.xkey_conflict` (`details: {xKey, existingTypeId, bundleId}`),
  **install path only** — an adopted root never conflicts with itself.
- **`layout` / `weight` / `hidden`** — written with the root's name on install.
  **`hidden` is explicit** now; the old "hidden by construction" rule is gone.
  A root that only hosts its bundle's records should ask for it; a root that is
  a type objects carry stays listed.

### Install shape

An install writes the root as **root + up to 3 changes**:

1. one `objects` change carrying the types (`__type__`, the root's own id,
   `rootTypes`), `any.name`, the type metadata (`type.xkey` / `layout` /
   `weight` / `hidden`) and the seeded `rootProperties` values;
2. after the registry row, one `datasets` change when the bundle declares parts;
3. one `properties` change when it declares properties.

A peer may briefly see the parts before the property definitions. A bundle with
no declaration (a bare miniapp) mints its root through the ordinary object
create plus the name stamp.

**Adopt heals what is absent and never patches** — parts only on a root carrying
no part declaration at all; properties per handle (a definition the root lacks
is written; one it carries under any id, or removed via
`DELETE …/types/:rootId/properties/:propId` — the tombstone keeps the id — is
left alone). Ensure never touches the root's name, layout, weight or hidden flag
once stamped.

### Reserved `system:` namespace

**ids under `system:` are the server's**, installed only through the embedded
catalog. A client ensure with such an id is **`409 bundle.reserved`**, before
any wait. Reads, resolve and children on a `system:` id work like on any other.

### Bounds

`id` ≤256 B, `xKey` ≤256 B, `name` ≤1024 B, `rootTypes` ≤32 entries,
`rootProperties` ≤64 KiB, `parts` ≤32 entries / 64 KiB, `properties` ≤64
entries / 64 KiB.

Pre-flighted before the root is created: type ids must exist in the space
(`400 type.not_found`, `internal/server/handlers_bundles.go:403-417`) and
property values must fit their descriptor slug
(`400 property.format_violation`).

### Locked reads and `synced`

`GET …/bundles` and `GET …/bundles/:bundleId` answer only **after** the space's
registry convergence wait (local fast path when already synced; fast expiry with
no reachable peer). The reply carries **`synced`**: true means an absent bundle
is definitively not installed; false (cold offline device) means absence is
provisional. `GET …/bundles/:bundleId` returns `{bundle, synced}`.

The raw `bundles` dataset via `POST …/query[/subscribe]` on
`spaceIndexObjectId` stays the live local view; raw rows carry the stored
`rootId` register and **no `derived` field** — the derived verdict is applied by
`GET …/bundles[/:bundleId]`.

### `bundle.not_ready` is now 409, not 404

Commit `dfdd059`: a tree not held yet is `409 bundle.not_ready`, not `404`.
This covers a tree the SDK does not hold at all right after a join. Retry.

### Tech-space bundles

`docs/03-api.md:1189-1250`. A **type declaration is now required** (`parts`,
`properties` or an `xKey`); `rootTypes` / `rootProperties` / `children` are
refused (`400`, "a tech bundle root is its own type",
`internal/server/handlers_bundles.go:129-131`). The normal shape is a CREATED
root; `derived: true` is the exception. `types/:rootId/parts…` and the property
mutators are admitted on bundle roots.

### Children, conflicts, resolve — unchanged

`POST …/bundles/:bundleId/children {seed, types?}` → `{objectId}`, deterministic
per (space, root, seed). Under a derived root the child binds by seed. On a
member whose copy of the winner has not landed, `409 bundle.not_ready`.

`POST …/bundles/:bundleId/resolve {loserRootId}` → 204;
`409 bundle.loser_not_ready` until the SDK reports the root fully synced and it
has been a visible loser for 5 minutes; `409 bundle.not_loser` for the winner or
an unclaimed root; idempotent.

### Anybao impact

| site | body | verdict |
|---|---|---|
| `runtime/src/serve.rs:129` | `general-chat/v1`, `rootTypes: ["chat"]`, derived | **BREAK** — see §6 |
| `any@v1/program.py:1621-1622` | same | **BREAK** — see §6 |
| `runtime/src/serve.rs:225` | `bao/v1`, `"bao"`, `rootTypes: ["page"]`, created | **OK** — `page` is a registered hidden type and passes the preflight |
| `runtime/src/anyapi.rs:446-462` | body builder (`id`, `name`, `rootTypes`, `derived`) | OK; no `datasets` sent anywhere |
| `any@v1/program.py:2141-2168` | body builder (+ `rootProperties`) | OK |
| `runtime/src/serve.rs:314-318` | children `bao/config/v1`, `bao/secrets/v1`, `bao/triggers/v1`, `bao/runs/v1` | OK |
| `any@v1/program.py:2238,2448`; `config@v1/program.py:35`; `gmailSync@v1/program.py:655`; `skills/_core.md:92,166` | children | OK |
| `any@v1/program.py:2210` | resolve `{loserRootId}` | OK |
| `any@v1/program.py:2139` | path builder percent-encoding the slash | OK |

Nothing in anybao sends the retired `datasets` field, so that removal costs
nothing — but the *types* those bundles' children carry declare datasets, which
is where §4 lands.

---

## 8. New: the usecase catalog (`/v1/catalog`)

Commits `26dbaca` ("Usecase catalog: well-known bundles the server sets up on
request"), `9df9ca7`, `5cf4bb8`.
Docs: **`docs/28-well-known-bundles.md`** (new, 410 lines),
`docs/03-api.md:1300-1377`, `docs/29-client-model.md:66-89`, CLAUDE.md item 50.

Account-scoped, behind the auth guard, **outside the space group**
(`internal/server/handlers_catalog.go:29-31`):

```
GET  /v1/catalog                    → 200 {usecases: [CatalogUsecase]}
GET  /v1/catalog/:usecaseId         → 200 CatalogUsecase        404 catalog.not_found
POST /v1/catalog/:usecaseId/setup   → 200 CatalogSetupResponse
     {spaceId}
```

### Model

```
usecase  = { id, name, description?, requires: [usecase id], bundles: [bundle] }
bundle   = { id: "system:<name>/v<n>", name, description?, derived?, hidden?, type?, miniapp?, parts? }
type     = { xKey, weight?, layout?, properties: [property draft with xKey] }
miniapp  = { bundle?, <any other property of the built-in miniapp type> }
parts    = [ part draft ]                     # the POST …/types/:typeId/parts shape
```

| declares | the root is | objects relate to it by |
|---|---|---|
| `type` | a type object, `typeId = rootId`; `xKey` its handle | carrying it in `any.types`; values at `<typeId>.<propId>` |
| `miniapp` | the object a client opens; carries the built-in `miniapp` with `bundle` = the bundle id | opening it |
| `parts` | a records host — the app's own state lives in records on the root | nothing; asks for `hidden` |
| `type` + `miniapp` | one object that is both (the wiki) | both |

Usecase ids are slugs (`[a-z][a-z0-9-]*`), need no path encoding. Bundle ids are
`system:<name>/v<n>`. Type xKeys are `[a-z][a-z0-9_]*`, unique across the
catalog and disjoint from registered type ids.

### Setup semantics

One call does, in order:

1. **Dependency closure** — transitive `requires`, dependencies first, each
   once, the requested usecase last; `requires` walked in declaration order, so
   deterministic. Within a usecase, bundles in declaration order.
2. **One registry-convergence wait** for the whole list (30s with a peer, 3s
   with none; instant when already synced). The first bundle that needs the
   verdict pays it, the rest reuse it.
3. **Adopt-or-install per bundle** — a live winner is adopted (a pure read, so a
   reader member gets the ids too; a writer's adopt also heals a property the
   root lacks by handle, a `miniapp` value it lacks, and a `choice` option key
   the catalog gained). Otherwise the handle check runs and the root is minted.

Idempotent: a second setup adopts everything (`installed: false`, same ids). A
failure mid-walk leaves the dependencies it installed, names the failing step,
and the next call resumes.

Who may do what: when the wait cannot complete, the space's **owner** installs
anyway; any other member is `409 bundle.not_ready`. A member without write
permission adopts but cannot install (`403` on the install path).

### Reply

```jsonc
{ "usecase": "contact",
  "bundles": [
    { "usecase": "people", "id": "system:person/v1",
      "bundle": { "id": "system:person/v1", "name": "Person", "rootId": "<rootId>", "roots": ["<rootId>"] },
      "installed": true, "typeId": "<rootId>",
      "properties": { "email": "<propId>", "phone": "<propId>", "…": "…" } },
    { "usecase": "people", "id": "system:organization/v1", "…": "…" },
    { "usecase": "contact", "id": "system:contact/v1", "…": "…" } ] }
```

`typeId` (the root id) and `properties` (**the xKey → propId map — this is what
you cache**) are present when the bundle declares a type (`type`, or `parts`).
A `miniapp` bundle echoes `miniapp` with `bundle` filled in. `installed` reports
whether THIS call registered the root.

### Handle conflicts

The catalog's type xKeys are checked against the space **on the install path
only**: before a root is minted, no type in the space — hidden or not, user or
registered — may hold the bundle's `xKey` (as its `xKey` or as its id). A hit is
`409 type.xkey_conflict` with `details.xKey` and `details.existingTypeId`,
raised before anything is created.

The case that bites: a user type minted before the catalog knew the handle. It
blocks that usecase in that space until the type is gone, **and there is no
rename or delete of a type over HTTP in v1**.

### Errors

`404 catalog.not_found` (`details.usecaseId`); `400 request.missing_field`
without `spaceId`; `405 space.unsupported` on the tech space; the space errors;
and per bundle the § Bundles set — `409 bundle.not_ready`, `403`,
`409 type.xkey_conflict`. Every error of the walk carries `details.usecaseId`,
`details.usecase`, `details.bundleId`, `details.spaceId`.

### Full catalog content — `internal/catalog/catalog.yml`

13 usecases, 15 bundles, 12 types.

| usecase | requires | bundle id | type xKey (weight, layout) | property xKeys (slug) |
|---|---|---|---|---|
| `wiki` | — | `system:wiki/v1` | `wiki` — hidden, + miniapp | `parentId` (string, `meta.index: none`), `pos` (string, `meta.index: none`), `folder` (boolean, `checkbox`) |
| `collections` | — | `system:collections/v1` | — bare miniapp | — |
| `general-chat` | — | `system:general-chat/v1` | `general_chat` — derived, hidden, `layout {type: chat}`, + miniapp; part `chat` = `{module: chat, shared: true}` | — |
| `people` | — | `system:person/v1` | `person` (10, `profile`) | `email` (email), `phone` (phone), `organization` (relation→organization), `job_title` (text), `location` (text), `linkedin` (url), `birthday` (date), `tags` (choice, multiple); part `body` = shared editor, `ui {type: document}` |
| | | `system:organization/v1` | `organization` (10, `profile`) | `kind` (choice: company/school/university/nonprofit/government/community), `domain` (url), `categories` (choice, multiple), `location` (text), `size` (choice: xs 1–10 / s 11–50 / m 51–200 / l 201–1000 / xl 1000+), `linkedin` (url), `main_contact` (relation→person); part `body` = shared editor |
| `contact` | people | `system:contact/v1` | `contact` (5) | `owner` (relation→person), `status` (choice: new/active/dormant/archived), `source` (choice: referral/intro/event/inbound/outbound/network), `referred_by` (relation→person), `last_contact` (date), `next_follow_up` (date) |
| `investor` | people | `system:investor/v1` | `investor` (5) | `investor_type` (choice: angel/vc/family_office/corporate/accelerator/syndicate), `investor_status` (choice: target/contacted/in_talks/passed/committed/invested), `focus` (choice, multiple), `stages` (choice, multiple: pre_seed/seed/a/b_plus/growth), `check_size` (currency), `portfolio` (relation→organization, multiple) |
| `customer` | people | `system:customer/v1` | `customer` (5) | `account_status` (choice: prospect/trial/active/at_risk/former), `plan` (choice, empty options), `annual_value` (currency), `customer_since` (date), `renewal_date` (date) |
| `partner` | people | `system:partner/v1` | `partner` (5) | `partnership_type` (choice: integration/reseller/referral/co_marketing/technology/community), `partner_status` (choice: exploring/active/paused/ended), `since` (date), `review_date` (date) |
| `vendor` | people | `system:vendor/v1` | `vendor` (5) | `services` (choice, multiple: legal/accounting/design/engineering/cloud/marketing), `vendor_status` (choice: evaluating/active/paused/ended), `contract_value` (currency), `renewal_date` (date) |
| `cofounder` | people | `system:cofounder/v1` | `cofounder` (5) | `founded` (relation→organization), `since` (date), `responsibilities` (choice, multiple: product/engineering/sales/ops/finance), `equity` (percent) |
| `candidate` | people | `system:candidate/v1` | `candidate` (5) | `role` (text), `candidate_stage` (choice: new/screening/interview/offer/hired/closed), `next_interview` (date), `resume` (url) |
| `contacts` | people, contact | `system:contacts/v1` | miniapp, hidden | part `layouts`, dataset `layouts` (`idRule: user`, id = an identity type's xKey), field `blocks` (array, `mutableBy: any`) |
| `crm` | contacts | `system:deal/v1` | `deal` (10, `profile`) | `stage` (choice: lead/qualified/proposal/negotiation/won/lost), `owner` (relation→person), `organization` (relation→organization), `amount` (currency), `close_date` (date); part `body` = shared editor |
| | | `system:crm/v1` | miniapp only | — |

Notes from the yaml header comments:

- `choice` and `relation` properties are `kind: array` even when single-valued.
- An amount is `currency` on `kind: number`.
- The two identities are one usecase because `person.organization` and
  `organization.main_contact` reference each other and the `requires` graph must
  stay acyclic.
- The roles are one usecase each so a role lands only when picked; `crm` does
  not require them.
- `GET /v1/catalog` returns the yaml with one normalization: a `miniapp` map
  always carries `bundle` = the bundle id.

### Evolution

Adopt heals what a root lacks and never patches, so a later release may, without
a new bundle id: add a property to a type; add a `miniapp` value; add an option
key to a `choice` property; add a bundle to a usecase or a usecase to
`requires`. A heal that fails is not an error — the setup still answers 200.

Needs a **new bundle id** (`/v2`): changing or removing a part or dataset;
changing a property's `kind` or `scope`; renaming a type or property xKey;
changing `derived`.

Uninstall is `DELETE …/objects/<rootId>` (except the derived chat). No
usecase-level uninstall, no reference counting.

### Forks

Every catalog root except the chat is created, so two devices installing while
apart each mint a root. The registry names one winner and lists the other in
`losers`. The client re-homes objects **column by column through the xKey map**,
then `POST …/bundles/system%3A<name>%2Fv1/resolve {loserRootId}`. **The server
never merges.**

Client rule (`docs/28-well-known-bundles.md:216-220`): **resolve catalog types
through the bundle registry** (bundle id → `rootId`), never by scanning the type
list for the xKey — after a fork two roots carry the same xKey.

### Validation

`make catalog-validate [FILES="candidate.yml …"]`, boot refusal, CI on every PR
and before every release build. Problem codes: `catalog.bad_yaml`,
`catalog.unknown_field`, `catalog.bad_id`, `catalog.duplicate`,
`catalog.missing`, `catalog.bad_field`, `catalog.unknown_usecase`,
`catalog.cycle`, `catalog.broken_link`, `catalog.bad_miniapp`.

### Anybao impact

anybao calls **no** `/v1/catalog` route today. It must adopt at least
`general-chat` (§6) and, if it wants tree placement, `wiki` (§1).

---

## 9. Built-in types: `page` rebuilt, `miniapp` / `bin` / `dataview` added, all hidden

Commits `3a6108b` ("Built-in hidden types: page, miniapp, bin"), `6f0a5d5`,
`062db50` + `954eb95` (dataview), `b40c06c` (bin namespace), `f8ad05c`.
Docs: `docs/03-api.md:2794-2930`, `docs/29-client-model.md:93-113`,
CLAUDE.md items 46 and 47.

All four are **`hidden`**:

- absent from `GET …/types` unless the request carries **`?includeHidden=true`**
- `GET …/types/:typeId` resolves them always
- `builtIn: true` with `xKey` equal to the id, which **reserves `page`,
  `miniapp`, `bin` and `dataview` against user types** → `409 type.xkey_conflict`
- static — every write is `400 type.registered`, now including
  `PATCH …/types/:typeId` itself
- present in every space by construction; nothing installs them and nothing
  stamps them onto an object

### `page`

**Old:** a pure marker — no dataset, no properties. The body lived on the
separate `editor` type's `editor_blocks` dataset, "attached on first block
write". Listed in `GET …/types` (not hidden).

**New:** no properties; **one part `body`** (`ui {"type": "document"}`) whose
dataset is the editor module's shared collection. So an object carrying `page`
holds `editor_blocks` and every `…/editor/editor_blocks/**` route works on it,
and `page` is always among the `owners` of `editor_blocks`. Hidden. Being
registered, it carries **no `weight` and no `layout`** — an object carrying only
`page` has no primary type.

This is the type to add wherever anybao writes markdown to a fresh object (§2).

### `miniapp` (new)

The marker of a sidebar entry: an object that runs an installed bundle, or an
ordinary object the user pinned. Three properties, no parts:

- **`bundle`** (string) — the id of the installed bundle. Absent on a pinned
  object. The bundle id is the app's whole identity; a client never guesses from
  the root's name.
- **`pos`** (string) — sidebar position, a client-allocated lexid.
- **`hidden`** (boolean, `checkbox`) — takes the entry out of the sidebar
  without uninstalling. There is no uninstall of a catalog install yet; hiding is
  the supported "remove".

Pin = `POST …/properties/:objectId/attach/miniapp`, unpin = `…/detach/miniapp`;
values through `POST …/properties/:objectId/set/miniapp`.

Sidebar query (`docs/29-client-model.md:52-60`):

```json
{"filter": {"$and": [{"any.types": "miniapp"},
                     {"any.types": {"$nin": ["bin"]}},
                     {"miniapp.hidden": {"$ne": true}}]},
 "sort": ["miniapp.pos"]}
```

This one query does **not** take the `__type__` exclusion — a miniapp row is a
bundle root, and the roots that also declare a type are exactly the ones the
exclusion would drop.

A client never detaches `miniapp` from a catalog root: the install would stay
and become unreachable.

### `bin` (new)

The marker of an object moved to the bin. Two server-stamped properties:
`movedAt` (datetime, `{"$date": …}`, the server clock) and `movedBy` (string,
the account identity).

- Move: `POST …/properties/:objectId/attach/bin`
- Restore: `POST …/properties/:objectId/detach/bin`

The membership op and the stamps ride **one** synced change, so the returned
`changeId` names the move, a bin carrier never lacks its stamps and a restored
object never keeps stale ones. A second move re-stamps.

Commit `b40c06c`: `detach/bin` now issues **one `$unset` on the whole `bin`
namespace** instead of two leaf unsets — the CRDT deletes a leaf but never
prunes the emptied parent, so a restored row used to keep `bin: {}` and read as
a carrier to any client testing the key or filtering on `$exists`. A restore on
an object that was never binned stays a no-op 200.

Clients filter carriers out of ordinary lists with
`{"any.types": {"$nin": ["bin"]}}` and list the bin with
`{"any.types": "bin"}` sorted `-bin.movedAt`.

**`/search` does not know about the bin** (`docs/08-clients.md` § 3): a binned
object's text still surfaces as a hit, and a hit carries no types, so drop
binned hits by reading the hit's object row before rendering.

### `dataview` (replaces `data_view`)

`docs/24-data-views.md`, `docs/03-api.md:2794-2858`.

**No back-compat**, stated at `docs/24-data-views.md:30-40`:

> It replaces the earlier `data_view` type and its `data_views` dataset with no
> back-compat: on an upgraded space the old collection and its rows stay on disk
> unreachable (an unregistered dataset), `data_view` lingers in `any.types`
> while `GET …/types/data_view` answers 404, and an old change that arrives late
> parks for good. Existing installs are abandoned in place; a client detaches
> `data_view` from its hosts and ensures the new defaults.

Two levels now, under one part `views` (`ui {"type": "table"}`):

- **`dataviews`** — one record per named table on the host.
  `{id, name, icon, pos, creator, createdAt, modifiedAt}`; `name` + `pos`
  required.
- **`views`** — one record per view.
  `{id, dataview, name, icon, pos, layout, query, layoutSettings, localSettings, creator, createdAt, modifiedAt}`;
  `dataview` + `name` + `pos` + `layout` required.

A view's `dataview` is required but **not validated**, and deleting a dataview
does **not** cascade — orphan views stay readable and writable.

`query`, `layoutSettings` and `localSettings` are opaque (checked only for being
objects). `localSettings` is `scope: local`. Record ids are client-supplied
(`idRule: user`) in both datasets; a deleted id is **burned permanently** — an
ensure must inspect `rejections` and fall through a deterministic id sequence
(`default`, `default-2`, …). View ids are one namespace per host; a second
dataview's views take `<dataviewId>.<key>` ids.

No bespoke endpoints and no `dataview` module: write through `…/modify` with
`dataset: "dataviews"` / `"views"`, read through `…/query[/subscribe]`.

### `any`, `spaceIndex`, `type`

Still returned first by `GET …/types`. Not attachable. The meta-type `type` grew
from one `xkey` property to **`xkey`, `weight`, `layout`, `hidden`, `meta`**.
Their properties now come back with the same `description` / `xFormat` slice a
user definition carries (`any.name` is `{"type": "text"}`, `any.createdAt`
`{"type": "datetime"}`, `type.hidden` `{"type": "checkbox"}`).

### Anybao impact

| file:line | what | action |
|---|---|---|
| `any@v1/program.py:2382` | `primary = next(t for t in types if t not in ("nav","editor"))` | must skip `page`, `miniapp`, `bin`, `dataview`, `__type__` |
| `enrich@v1/program.py:241` | `if t.get("builtIn") and t.get("xKey") != "editor"` | `editor` is not a type |
| `runtime/src/anyapi.rs:713`, `any@v1/program.py:1651` | `GET …/types` | add `?includeHidden=true` where the hidden built-ins matter |
| `deepResearch@v1/program.py:153` | resolves `("pages", "page")` before creating | creating a user type with xKey `page` is now `409 type.xkey_conflict` |
| `miniapp@v1/program.py:25-28` | user type `mini_app`, dataset `mini_app` | **safe** — `mini_app` ≠ the reserved `miniapp`; but see §4 for the collection rename |
| `any@v1/program.py:276` | `_SYNTHETIC_TYPES = {"any","spaceIndex","type"}` | still correct |
| — | `data_view` / `dataview` / `bin` | zero occurrences in anybao; nothing to port |

Consider adopting `miniapp` so anybao's mini apps appear in the client sidebar,
and `bin` instead of hard deletes.

---

## 10. Types: `weight`, `layout`, `hidden`, `meta`, `PATCH …/types/:typeId`

Commits `a7190ff`, `f8ad05c` ("Types: hidden flag and per-key meta bag over
HTTP"). `docs/03-api.md:2262-2300`.

### Create body

**Old:** strictly `{name?, description?, iconCid?, xKey}`
**New:** `{name?, description?, iconCid?, xKey, weight?, layout?, hidden?, meta?}`

Inline property definitions are still not accepted; a `properties` key is
`400 request.unknown_field`.

### New route: `PATCH /v1/spaces/:spaceId/types/:typeId`

Body `{name?, description?, iconCid?, weight?, layout?, hidden?, meta?}` → 204.
Absent keeps, an empty string clears a text field, `"layout": null` clears the
layout. `400 type.registered` on a built-in, `404 type.not_found`.

### The three new fields

- **`weight`** (`type.weight`) — an object carries several types; the one with
  the highest weight is its **primary** type, whose `layout` a client renders.
  `any` and the built-ins carry no weight and never win; ties break on type id.
- **`layout`** (`type.layout`) — an opaque descriptor in the x-format shape,
  `{"type": "<slug>", "config": {…}}`, the client's vocabulary, checked only for
  being an object.
- **`hidden`** (`type.hidden`) — keeps the type out of `GET …/types` unless
  `?includeHidden=true`. `GET …/types/:typeId` resolves a hidden type always.
- **`meta`** (`type.meta`) — an open bag of consumer flags: one string, bool or
  number per single-level key (no `.`, no `$`, ≤64 bytes; otherwise
  `400 request.invalid_field`), opaque to the server. Create takes it whole;
  PATCH patches **per key** — a scalar sets, `null` unsets, unnamed keys are
  untouched.

### The rendering rule clients must adopt

`docs/29-client-model.md:120-128`:

> Render the primary type's `layout` **with the parts of every carried type**,
> ordered by `pos` — a shared collection appears once however many types share
> it. Rendering only the primary type's parts is the common mistake.

### The `__type__` query trap (new, load-bearing)

`docs/29-client-model.md:200-247`. A bundle root that declares a type **carries
that type** (role 3: an implementation of itself), so
`{"any.types": "<personTypeId>"}` returns the Person *definition* next to the
actual people. Exclude the marker:

```json
{"$and": [{"any.types": "<typeId>"},
          {"any.types": {"$ne": "__type__"}},
          {"any.types": {"$nin": ["bin"]}}]}
```

Write the filter this way regardless — a type created through `POST …/types` is
a definition only and does not self-match today, but it will if it later ships
as a bundle.

anybao filters on `any.types` at: `any@v1/program.py:959,1320`;
`toolcaller@v1.py:442,587`; `miniapp@v1/program.py:70,322`;
`enrich@v1/program.py:268`; `gmailSync@v1/program.py:299,860`;
`runtime/src/deploy.rs:580`; `skills/_any.md:62,74,96,268,270,281`. Each should
gain the `__type__` and `bin` exclusions.

---

## 11. Query and subscribe

Commit `3e7f2f8` ("A subscribe window without a sort is a 400, not a 500").
`docs/09-query.md:42-46`.

**The one hard break:** on `/subscribe`, **`limit` requires `sort`**:

> a live window has to be ordered for the engine to know which records fall
> inside it, so a limit with no sort is `400 request.invalid_field`. Snapshots
> take an unordered limit: there it is just an arbitrary page.

Previously this was a 500.

Everything else in the grammar is unchanged. The doc's `nav.*` examples moved to
`<typeId>.<propId>`. Path literals are now `any.types`, `any.name`, `_ver.id`,
plus the row-root derived stamps `author`, `createdAt`, `modifiedAt`,
`modifiedBy`, `spaceId` (objects collection only). `modifiedBy` is unindexed, so
a filter on it scans.

Also unchanged but worth restating for the port:

- Timestamp filter literals must be `{"$date": …}` — a bare number or ISO string
  does not error, it silently matches nothing, because comparisons are bracketed
  by type.
- `includeTotal`, `includeDeleted`, `projection` semantics are unchanged.
- The body vocabulary is closed; an unknown key is `400 request.unknown_field`.

### Anybao impact

Every anybao subscribe carries a sort (`runtime/src/serve.rs:2317-2318`,
`:2725`; `runtime/src/anyapi.rs:1000`), so nothing breaks. Keep it that way.

Guards already in place that stay correct:
`any@v1/program.py:1287-1290` (options limited to `{filter, sort, limit, offset}`),
`:1299` and `:1340` (ADR-019 instant guard on datetime filter keys).

---

## 12. Backlinks replaced by a real link index

Commits `9a56a5f` ("Link index: backlinks as a second sink on the search
indexer"), `b416f76`, `333f1f9`.
Docs: `docs/03-api.md:1770-1836` (§ Links and backlinks),
`docs/13-index.md` § Links, `docs/19-links.md`, `docs/21-events.md:194-215`,
CLAUDE.md item 54.

### Old (`a176029:docs/03-api.md:1522-1546`)

```
GET /v1/spaces/:spaceId/objects/:objectId/backlinks
→ {"backlinks": [{"objectId": "…", "typeId": "…", "propId": "…"}]}
```

A scan over the objects collection for `format.type: "links"` property values
containing `"any://<X>"`. One entry per (referencing object, property) pair.
No existence check; an unknown id returned `{"backlinks": []}`.

### New

Three reads over a real reverse index, all answering `409 index.disabled` when
`index.enabled` is off, and reflecting a write after the indexer's debounce (a
few hundred ms), never inside the write.

```
GET /v1/spaces/:spaceId/objects/:objectId/backlinks   → {object: [Link…], parts: [Link…], truncated?}
GET /v1/spaces/:spaceId/objects/:objectId/links       → {links: [Link…], truncated?}
GET /v1/backlinks?target=<uri>                        → {spaces: [{spaceId, object, parts, truncated?}]}
```

An **edge** (`internal/api/backlinks.go:11-45`):

```json
{ "source": {"spaceId": "…", "objectId": "P", "dataset": "editor_blocks",
             "recordId": "blk_a", "typeId": "…", "field": "…"},
  "kind":   "link",
  "target": {"uri": "any://o/<sp>/X/editor_blocks/blk_z", "kind": "o",
             "spaceId": "<sp>", "objectId": "X", "dataset": "editor_blocks",
             "recordId": "blk_z", "propId": "…", "identity": "…", "fileId": "…"} }
```

- `source.dataset` is the collection the reference was found in — a module or
  runtime collection, or the virtual **`prop`** for a property value, where
  `recordId` is the property id and `source.typeId` the declaring type.
- `kind` ∈ `mention` (identity in text), `link` (object/record/value/file
  reference in text or a chat attachment), `card` (an editor paragraph that is
  exactly one whole-line link), `embed` (synced-block reference), `relation`
  (a value of a link-bearing property or field). **The set is open.**

`…/backlinks` splits: `object` holds edges pointing at the object itself,
`parts` those pointing at one of its records or property values.
`?record=<id>&dataset=<collection>` or `?prop=<propId>` narrows to one part
(then `object` holds that part's edges and `parts` is empty). `?kind=`
(repeatable) filters. `?limit=` caps the reply (default and max 500; a larger
value clamps) — applied to the read **before** the split; `"truncated": true`
says the cap was hit; there is no continuation. No existence check.

`…/links` is the forward direction; `?dataset=` alone selects one collection.

`/v1/backlinks?target=` requires a **global** URI form (`any://o/<sp>/…`,
`any://m/…`, `any://f/…`, `any://p/…`); the bare in-space form is
`400 request.invalid_field`. It reads every space this device indexes, so it is
access-filtered by construction.

### What produces edges

Editor blocks, chat messages, and every property or field whose descriptor
carries a link marker — `xFormat.links` ∈ `{link, links, markdown, none}`,
**implied by the `relation` slug (`links`) and the `markdown` slug
(`markdown`)**, settable explicitly on any other shape.

Two consequences for the port:

- A property created without the `relation` slug (any pre-descriptor definition)
  **indexes nothing** until it is patched with
  `{"set": {"xFormat.type": "relation"}}` (`docs/29-client-model.md:150-155`).
- A `relation` slug or `links` marker nested in a **composite** is legal but
  **invisible** to the index — it inspects only top-level definitions.

Only live references count: a deleted record, a cleared value, a detached type's
values and a deleted object all drop their edges. The wiki tree's `parentId` is
a plain string, not a link.

`docs/16-chat.md`: every `any://` reference in a message — text links and
mentions, each attachment's `link`, the agent group's `debugLink` — lands in the
index, so "which messages mention this member" is a backlinks read.

### Liveness

New device-scope event `links.updated` (`docs/21-events.md:194-215`):

```jsonc
{ "spaceId": "<source space — where the writing records live>",
  "targets": ["any://o/<sp>/<obj>", "any://m/<sp>/<identity>", …],
  "truncated": false }
```

`spaceId` is the **source** space; a target's own space is inside its URI, so
match on `targets`, never on `spaceId`. Capped at 200 entries
(`api.MaxEventLinksTargets`). At-most-once like every bus event.

New process kind: **`index.links_backfill.<spaceId>`**
(`docs/22-processes.md:204-213`) — a space whose edges predate the link sink's
layout is re-extracted once by its worker before it advances. Announced past
`AnnounceAfter`; `done` counts objects, `total` unknown; terminal `done` /
`failed`, retried on the next start.

### Storage

`<spaceId>_links` in `index.db`, written in the same page transaction behind the
same cursor as the text docs (`internal/indexer/links_store.go`). Doc id
`objectId:dataset:recordId:<hash>`, so the text-doc prefix evictions apply
verbatim. The search **index schema version is unchanged at 7**
(`internal/indexer/store.go:45`), so this does **not** force an index wipe.

### Anybao impact

| file:line | call | break |
|---|---|---|
| `runtime/src/anyapi.rs:966` | `GET …/objects/{object_id}/backlinks` | reply shape changed |
| `repos/_agent/programs/any@v1/program.py:2409` | same | reply shape changed |
| `tests/conftest.py:125` | same | fixture |

All three parse `{"backlinks": [{objectId, typeId, propId}]}`. Rewrite against
`{object, parts}` and the `Link` edge shape. Consider adopting `…/links` and
`/v1/backlinks` — anybao has neither today.

`docs/19-links.md` also renames the implementation homes: relation property-value
validation moved from `internal/server/propformat.go` (deleted, 440 lines) to
`internal/server/descriptor.go`; `anyuri/links.go` now holds `ExtractLinks`,
`Canonical`, `ObjectKey`, `IsPart`, with `ExtractMentions` a filter over it.

---

## 13. Dataset discovery reply changed

`internal/api/datasets.go:31-53`, `docs/03-api.md:723-788`.

### Old

```
GET /v1/spaces/:spaceId/datasets   → { datasets: [ { name, schema, typeId? } ] }
GET /v1/datasets                   → { datasets: [ { name, schema } ] }
```

> `typeId` names the owning type for ext-type and runtime-defined datasets
> (absent for space-level built-ins).

### New

```
GET /v1/spaces/:spaceId/datasets   → { datasets: [ { name, schema, owners?, module, shared? } ] }
GET /v1/datasets                   → { datasets: [ { name, schema, module } ] }
```

```go
type DatasetSchema struct {
    Name   string          `json:"name"`    // the COLLECTION
    Schema json.RawMessage `json:"schema"`
    Owners []string        `json:"owners,omitempty"`  // replaces typeId
    Module string          `json:"module,omitempty"`  // records | editor | chat
    Shared bool            `json:"shared,omitempty"`
}
```

- **`typeId` is gone.** `owners` lists the types whose parts declare the
  collection: one for a namespaced `<typeId>_<key>` instance, **every** type
  sharing a module's canonical collection (`editor_blocks`, `chat_messages`),
  none for space-level built-ins. A canonical collection nothing declares yet is
  listed **without** `owners`, and no object can hold it.
- A collection lives on an object only while it carries one of its owners, so
  consumers gate indexing and eviction on that set.
- Every field node now carries **`description`**, and **`x-format`** where the
  descriptor vocabulary names its value (commit `524e407`).

`x-scope` and the behavioral `x-*` keywords are unchanged.

### Anybao impact

`repos/_agent/programs/any@v1/program.py:2346-2351` (`list_search_scopes`) reads
`row["typeId"]` off each discovery row and walks
`GET …/types/{typeId}/datasets` per row to collect `search.scope`, unioned with
`_FIXED_SCOPES = ("basic", "chat", "props")` (`:2333`). Rewrite against `owners`
(and note the walked reply's field renames from §4).

The pinned assertion at `tests/test_any_module.py:1336-1352` — which also
asserts that rows with `typeId: "chat"` are *not* walked — needs a rewrite.

---

## 14. Account CRDT version — the data-dir one-way door

Commit `513263a` ("Surface the account CRDT version: refuse a newer account,
report it on health"). Docs: `docs/02-server.md:109-116, 287-311`,
`docs/03-api.md:163-176`, `docs/06-errors.md`, CLAUDE.md item 45.

The SDK stamps the account's tech space with the CRDT data-model version it
writes (`space.CRDTVersion` on the tech-space index object, `crdtVersion` system
dataset, **monotonic by handler rule** — the mark only ever rises).

`GET /v1/health` gained (`internal/api/meta.go`):

```json
{ "version": "any v0.1.0 (sdk v0.0.0)",
  "startedAt": "2026-04-23T18:12:00Z",
  "account": "A3...",
  "bootstrapping": false,
  "crdtVersion": { "supported": 1, "stored": 1, "newer": false } }
```

Absent when unauthorized. `supported` = what this server's SDK writes;
`stored` = what the account's tech space records; `newer` = stored is ahead.

Two refusal modes:

1. **At boot** — an account a newer release wrote refuses to open.
   `POST /v1/auth` → **`409 sdk.crdt_version_newer`** (`details.stored` >
   `details.supported`); `any run` exits with the same reason.
2. **At runtime** — when the raise arrives through sync while the server is
   running (a second device upgraded first), the account turns **read-only**:
   reads keep serving, and **every synced write answers
   `409 sdk.crdt_version_newer`** until the server is upgraded.

A lower or absent mark is raised to this release's version on open. The mark
exists from this release on, so only releases carrying it refuse each other; an
older release without the check runs unguarded — which is exactly why a mixed
fleet is dangerous.

### Operational consequence

**Once any server on the new build opens a data dir, older `any` binaries can
never open that account again.** Upgrade the whole fleet together:

- `~/any/any/datadirs/repo-prod` (`:7003`, prod repo owner)
- `~/any/any/datadirs/prod-test-user` (`:7005`)
- `~/any/any/datadirs/staging-repo` (`:7021`), `staging-user` (`:7134`)
- the mac prod server
- and rebuild anybao's `anyrt` alongside

### Index version: no forced wipe

`internal/indexer/store.go:45` — `indexSchemaVersion = 7`, unchanged across this
range (it was already 7 at `a176029`). A mismatch would be a boot error advising
removal of the index dir with no migration, but this range does not trigger one.

---

## 15. Smaller contract changes

### Derived objects refuse deletion

Commit `c0e28a9`. `docs/03-api.md:1753-1769`. `DELETE …/objects/:objectId` on
any bundle root installed with `derived: true` — the general chat above all — is
**`409 object.derived_undeletable`**; the row stays readable. Unknown or
already-deleted id → `404 sdk.not_found`; reader or guest → `403
space.read_only`; a non-root id on the tech space → `405 space.unsupported`.

anybao deletes objects at `any@v1/program.py:1265`.

### A deleted space still reads 200

`docs/03-api.md:446-452`:

> `GET /v1/spaces/:spaceId` on a tombstone returns the row with
> `status:"deleted"`; only an id the account never knew is `404
> space.not_found`. Every write and every space-scoped surface on a tombstone is
> `409 space.deleted`. **Branch on `status`, not on the status code** — a client
> keyed on `200` treats a deleted space as live.

anybao reads spaces at `runtime/src/anyapi.rs:388`, `any@v1/program.py:1579`,
`tools/eval/anyeval.py:43`.

### `record.deleted` (410) — new

A write addressed a tombstoned record (a block, a message, a runtime record):
the id is burned for good and never reused.

### `cancel-join` is account-level

Commits `b8d1b91`, `231b652`, `d7d3e06`, plus the synced join lifecycle
(`9380cd9`, `05da4c9`, `8fa9904`). `POST …/acl/cancel-join` no longer resolves
the space; it calls the SDK's `Service.CancelJoin`. New code
**`409 space.join_not_pending`** — the row is not joining, or the owner accepted
first. A declined or withdrawn join is re-requestable via
`POST /v1/spaces/join` (`space.deleted` now says so). Works from any device of
the account.

### Space list / join status

The pending join now lives in the tech-space row's **synced** `remoteStatus`
(`joining` / `joinEnded`) instead of device-local `localStatus`, so every device
of the joiner's account lists the space as `joining` and none materializes it.
`SpaceInfo.status` keeps its vocabulary — no wire change for anybao.

### Push

Commit `5669a86` — one-to-one chat pushes are classified with heart's enums.
`docs/20-push.md` touched; no client-visible break.

### SSE robustness

`d7ac5bf` fences the keepalive goroutine off a returned handler; `73540d0`
makes query-subscribe streams always write `closed` on an engine teardown.
Both are fixes, no wire change.

### Upsert

`POST /v1/spaces/:spaceId/upsert` is unchanged in shape. `dataset` is now the
**collection** (§4). Whole-call errors restated:
`400 upsert.requires_user_ids`, `400 dataset.unknown` (no such records
collection — a module collection such as `chat_messages` is never upsertable),
`400 dataset.not_declared` (the object carries no type declaring it).
Per-record rejection codes unchanged: `upsert.immutable_field`,
`upsert.not_author`, `upsert.record_deleted`, `upsert.rejected`.

### Search

`POST /v1/spaces/:spaceId/search` is unchanged. Scopes are still an open slug
set. Note `/search` does not filter binned objects (§9).

### Index chunkers

`docs/13-index.md` — the editor and chat chunkers became `index.ModuleChunker`s:
one registered chunker per module, resolved per space from `Space.Datasets`, so
they index the module's canonical collection **plus every namespaced instance**.
Entries carry the real collection as their `Dataset`. The gate is collection
ownership (the discovery document's `owners`). The editor chunker is now an
`index.MultiReconciler` (`ReconcileAll` per collection), so an edit in a part's
own editor never touches the shared body's docs.

`meta.index: "none"` now excludes from the **text** index only — a property
carrying a link marker still reports its edges.

### Aggregation

`docs/14-aggregation.md` — only an example changed (`$group` on `nav.type` →
`author`). No contract change.

---

## 16. Full error-code diff

From `git diff a176029..HEAD -- docs/06-errors.md`.

### Added

```
space.join_not_pending           # 409 — POST …/acl/cancel-join with nothing to withdraw
bundle.not_found                 # 404 — no live install for the bundle id in this space
bundle.not_ready                 # 409 — the winner's tree (or the registry) has not reached this device yet; retry
bundle.not_loser                 # 409 — resolve target is the current winner or was never claimed
bundle.loser_not_ready           # 409 — the losing root is still syncing / inside the grace window
bundle.reserved                  # 409 — a client ensure with an id under the server's `system:` prefix
catalog.not_found                # 404 — GET /v1/catalog/:usecaseId or POST …/setup naming no usecase (details.usecaseId)
record.deleted                   # 410 — a write addressed a tombstoned record; the id is burned for good
object.derived_undeletable       # 409 — DELETE on a derived object (e.g. the general chat)
dataset.not_declared             # 400 — a write into a collection none of the object's types declare (details.dataset, details.objectId)
dataset.not_found                # 404 — the :collection of an editor route is not an editor dataset in this space (details.collection)
dataset.key_conflict             # 409 — a part or dataset with this key already exists on the type (details.key)
dataset.shared_conflict          # 400 — shared on a module with no canonical collection, a shared key that is not the canonical name, or a namespaced dataset on a shared-only module (chat)
dataset.module_unknown           # 400 — the dataset names a module this server does not compile in
dataset.module_owned             # 409 — a field declaration on a module-served dataset (editor, chat)
dataset.module_reserved          # 400 — a part or dataset draft names a module reserved to the server (`chat`)
type.reserved_carrier            # 400 — object create `types`, attach, or an any.types op names a type whose part declares a reserved module (details.typeId)
property.xkey_conflict           # 409 — add/rename a property to an xKey another property of the type holds (details.xKey, details.existingPropId)
sdk.crdt_version_newer           # 409 — the account's data was written by a newer release (details.stored > details.supported)
```

### Removed

```
dataset.name_conflict            # 409 — AddDataset name already in use in the space   → replaced by dataset.key_conflict
```

### Changed meaning

| code | old | new |
|---|---|---|
| `space.deleted` | 409, 1-1s re-creatable via one-to-one start | + a declined or withdrawn join is re-requestable via `POST /v1/spaces/join` |
| `space.unsupported` | 405 on the tech space (list of surfaces) | + `catalog setup` |
| `dataset.unknown` | a record write names a dataset the object does not carry | a record write names a **collection the space does not serve as a records dataset**; the "declared but not on this object's type" half split off into `dataset.not_declared` |
| `dataset.decl_invalid` | malformed dataset declaration | + malformed **part** declaration, non-slug key |
| `dataset.immutable` | PATCH a pinned dataset-def path (mutable: description, displayName, search.title, search.text) | + parts (`name`, `icon`, `pos`, `hidden`, `ui`, `uses` mutable) and fields (`name`, `description`, `xFormat.*` mutable); + `search.scope` |
| `type.xkey_conflict` | 409 on `POST …/types` only | + raised by `POST …/bundles` and `POST /v1/catalog/:usecaseId/setup` on the **install path only**, never on adopt (`details.bundleId`; a setup adds `details.usecase`, `details.usecaseId`) |
| `type.registered` | add/patch/remove a property or dataset on a registered built-in | + **parts**, + `PATCH` the type itself |
| `property.immutable` | pinned paths incl. the whole `format` and `format.type` | pinned paths are now only `kind` / `scope` / `items` / `properties` — `format.*` became an *unknown* path (`request.invalid_field`) |
| `property.format_invalid` | bad format leaf (unknown ui, unparseable filter, `format.*` on a format-less property) | descriptor vocabulary problem on property **or dataset field**: slug does not fit the pinned kind, reserved slug/key (`tags`, `validate`, `compute`), unparseable `relation.filter`, empty slug |
| `property.format_violation` | a value write violated its declared format | a value write does not fit its descriptor's **current slug** (`details.format` is the slug) |
| `markdown.no_match` | "GET .../editor/markdown and quote exactly" | "GET .../editor/:collection/markdown" |

---

## 17. Full route diff

From `git diff a176029..HEAD -- internal/server/docs/swagger.yaml | grep -E '^[+-]  /'`, plus route registrations.

### Added

```
GET  /v1/catalog
GET  /v1/catalog/{usecaseId}
POST /v1/catalog/{usecaseId}/setup
GET  /v1/backlinks
GET  /v1/spaces/{spaceId}/objects/{objectId}/links
GET  /v1/spaces/{spaceId}/types/{typeId}/parts
POST /v1/spaces/{spaceId}/types/{typeId}/parts
PATCH  /v1/spaces/{spaceId}/types/{typeId}/parts/{partId}
DELETE /v1/spaces/{spaceId}/types/{typeId}/parts/{partId}
POST /v1/spaces/{spaceId}/types/{typeId}/parts/{partId}/datasets
PATCH  /v1/spaces/{spaceId}/types/{typeId}/datasets/{defId}/fields/{fieldId}
PATCH  /v1/spaces/{spaceId}/types/{typeId}                          (TypesAPI.Patch)
```

### Renamed (old form no longer matches any route)

```
/v1/spaces/{spaceId}/objects/{objectId}/editor/blocks
  → /v1/spaces/{spaceId}/objects/{objectId}/editor/{collection}/blocks
/v1/spaces/{spaceId}/objects/{objectId}/editor/blocks/{blockId}
  → /v1/spaces/{spaceId}/objects/{objectId}/editor/{collection}/blocks/{blockId}
/v1/spaces/{spaceId}/objects/{objectId}/editor/markdown
  → /v1/spaces/{spaceId}/objects/{objectId}/editor/{collection}/markdown
/v1/spaces/{spaceId}/objects/{objectId}/editor/markdown/append
  → /v1/spaces/{spaceId}/objects/{objectId}/editor/{collection}/markdown/append
```

### Removed

```
POST /v1/spaces/{spaceId}/types/{typeId}/datasets     (moved to …/parts/{partId}/datasets)
```

The `/datasets`, `/debug/p2p`, `/devices*` entries that appear on both sides of
the grep are yaml reordering, not changes.

### Kept but with a changed reply body

```
GET /v1/spaces/{spaceId}/objects/{objectId}/backlinks   {backlinks:[…]} → {object:[…], parts:[…], truncated?}
GET /v1/spaces/{spaceId}/datasets                       {name, schema, typeId?} → {name, schema, owners?, module, shared?}
GET /v1/datasets                                        {name, schema} → {name, schema, module}
GET /v1/spaces/{spaceId}/types/{typeId}/datasets        name → key, + collection/module/shared/partId; fields + description/shape/xFormat
GET /v1/spaces/{spaceId}/bundles/{bundleId}             Bundle → {bundle, synced}
GET /v1/health                                          + crdtVersion {supported, stored, newer}
```

### Kept but with a changed request body

```
POST  /v1/spaces/{spaceId}/objects                      "nav" key removed
POST  /v1/spaces/{spaceId}/bundles                      "datasets" removed; + parts/properties/xKey/layout/weight/hidden
POST  /v1/spaces/{spaceId}/types                        + weight/layout/hidden/meta
POST  /v1/spaces/{spaceId}/types/{typeId}/properties    "format"/"xKind" removed; + xFormat; kind now required; meta narrowed to index
PATCH /v1/spaces/{spaceId}/types/{typeId}/properties/{propId}   format.* paths → xFormat.*; leaf-only set rule
POST  /v1/spaces/{spaceId}/query/subscribe              limit now requires sort
POST  /v1/spaces/{spaceId}/objects/query/subscribe      limit now requires sort
```

---

## 18. New endpoints anybao may want

| endpoint | why |
|---|---|
| `GET /v1/catalog`, `GET /v1/catalog/:usecaseId` | discover usecases before offering them |
| `POST /v1/catalog/:usecaseId/setup` | **required** for the general chat; optional for `wiki` tree placement, `collections`, `people`/`contacts`/`crm` |
| `GET …/types/:typeId/parts` | the compiled view of a type's parts with each dataset's `collection` — **read the collection, never compose it** |
| `POST …/types/:typeId/parts` | declare parts + datasets in one change |
| `POST …/types/:typeId/parts/:partId/datasets` | add a dataset to an existing part |
| `PATCH …/types/:typeId/datasets/:defId/fields/:fieldId` | edit a field's `name` / `description` / `xFormat` without re-registering |
| `PATCH …/types/:typeId` | set `weight` / `layout` / `hidden` / `meta` on anybao's user types so clients render them |
| `GET …/objects/:o/links` | forward edges — what a note or transcript links to |
| `GET /v1/backlinks?target=` | account-wide backlinks, e.g. everything mentioning one person across spaces |
| `POST …/bundles/:bundleId/children` | unchanged, already used |
| built-in `miniapp` | put anybao mini apps in the client sidebar (`bundle`, `pos`, `hidden`) |
| built-in `bin` | soft delete with `movedAt` / `movedBy` instead of `DELETE …/objects` |
| built-in `dataview` | saved views over agent datasets (`dataviews` + `views`) |
| `links.updated` device event | refresh a links panel without polling |
| process kind `index.links_backfill.<spaceId>` | surface backfill progress |

---

## 19. Port checklist

Rough dependency order.

1. **Upgrade the whole fleet at once** — §14 is a one-way door on every data dir.
   Rebuild `bin/any` with the search tags (`go build -tags 'fts vector'`) on
   `:7003`, `:7005`, `:7021`, `:7134` and the mac prod server, then rebuild
   `anyrt`. A mixed fleet silently resolves old type shapes and lists zero
   objects.
2. **Chat** — replace the `general-chat/v1` bundle ensure with
   `POST /v1/catalog/general-chat/setup` at `runtime/src/serve.rs:129` and
   `any@v1/program.py:1621`. Decide the migration for the old root that still
   exists and still accepts writes in each live space. Keep the derived
   assertion, reading it off the catalog reply.
3. **Datasets** — move every declaration to `POST …/types/:typeId/parts`
   (`name` → `key`), read the returned `collection` back, and re-key every
   `dataset:` value on `/query`, `/query/subscribe`, `/modify`, `/upsert`,
   `/delete-records`. **Fix `runtime/src/broker.rs:1364`** — the
   `agent_secrets` deny-list literal no longer matches. Fix the
   idempotence checks that key on `d.get("name")`.
4. **Properties** — rewrite the encoder and every reader onto `xFormat`; drop
   `xKind`, `meta.pos`, `meta.icon`, `format.ui`; make `kind` explicit; adopt
   the leaf-only PATCH rule; move `anyUiArchived` out of `meta`.
5. **Editor** — add the `:collection` segment to the six markdown routes, and
   add `page` to `types` wherever anybao writes a body to a fresh object
   (`deploy.rs:620-624`, `:780-795`, `any@v1:1213-1214`,
   `deepResearch@v1:162`).
6. **Object create** — drop the `"nav"` key (`any@v1:1163`) and the `nav`
   reserved group.
7. **Discovery and backlinks** — rewrite `list_search_scopes` onto `owners`
   (`any@v1:2346-2351`), and the backlinks readers onto `{object, parts}`
   (`anyapi.rs:966`, `any@v1:2409`, `conftest.py:125`).
8. **Type listing** — add `?includeHidden=true` where the hidden built-ins
   matter; teach primary-type selection about `page` / `miniapp` / `bin` /
   `dataview` / `__type__` (`any@v1:2382`, `enrich@v1:241`).
9. **Filters** — add the `__type__` and `bin` exclusions to every
   `any.types` filter.
10. **Contract** — re-pin `api/openapi.vendored.json` and re-run the drift check
    (`runtime/src/drift.rs:256`).
11. **Tests** — the anybao fixtures listed throughout encode the old shapes:
    `tests/test_any_module.py`, `tests/test_any_properties.py`,
    `tests/test_integration.py`, `tests/test_recall_*`,
    `tests/test_instants_integration.py`, `tests/test_enrich_*`,
    `runtime/src/testutil.rs`, `runtime/src/anyapi.rs` (in-file tests),
    `runtime/src/serve.rs:3537,4021`.
