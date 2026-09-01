# ADR-022: Property formats — value resolution and hydration in `any@v1`

Status: **Accepted** (2026-08-27)
Date: 2026-08-26
Builds on: ADR-006 §6 (xKey normalization at the client boundary),
ADR-010 §8 (flat `any@v1` surface), ADR-019 (instants)
Amends when accepted: ADR-006 §6 (handle rule; no-silent-drop extends
to values; hydrated reads), ADR-007 §graph ("object-kind" → links
format), ADR-010 §8 (new definition tools)
Upstream: `any` main `675e4b2` (`website/03-database/{data-types,
property-lifecycle,types-and-properties}.md`, `internal/server/
propformat.go`); `any-ui` main `dc35cf68` (`packages/api-core/src/
resources/{types,property-values}.ts`, `src/components/properties/
{optionPalette,usePropertyEditor}.ts`)

## Context

The server's property model is small and settled. A property is a
structural `kind` (`string | number | boolean | null | array | object
| datetime`) plus an optional `format` that narrows the value
convention — five usable: `select`, `multiselect`, `links`, `date`,
`datetime` (`tags` is reserved). There is no text/number/checkbox/
url/email/file format; those are bare kinds. Select options are a map
**inside the definition**, `format.options.<key> = {name, color, pos,
meta?}`, and the option *key* is the stored value — renaming an option
is one definition write and zero object writes. Option CRUD is `PATCH
…/types/:t/properties/:p {set: {"format.options.k.name": …}, unset:
["format.options.k"]}`; setting a leaf on an unknown key creates the
option, a value write never does, and membership is not enforced
(dangling-tolerant by design). `links` values are the bare
`any://<objectId>` form only, with no existence or type check
(`format.filter` is advisory metadata for pickers). `date` values must
be a midnight-UTC instant. `null` is rejected by the kind check for
every kind — the only clear is a `$unset` through `/modify` on the
`objects` dataset. `kind`, `scope`, `format.type` and option keys are
pinned at first write; `name`, `xKey`, `xKind`, `meta.*`,
`format.{ui,filter,meta.*}` and option leaves are mutable. The server
resolves by content ids only; `xKey` is client metadata, not unique,
not derived for properties.

any-ui matches that contract and layers conventions on the open maps:
`meta.pos` (property order, lexid, byte compare), `meta.anyUiArchived
= "1"` (removal is an archive marker; `DELETE` is a sticky tombstone
the UI never issues), `format.meta.optionSort`. New options get a
slug key uniquified `_2`, `_3`…, a **random** color from a ten-name
palette (`grey yellow orange red pink purple blue ice teal green`)
and a lexid `pos` after the last. The UI resolves option keys and
object ids to display forms client-side from the definitions and the
space's objects window. Crucially, any-ui uses `xKey` as a **kind
marker** — `select`, `tags` (multiselect), `links`, `relation`
(legacy), `date`, `url`, `email`, `longtext` — and sends no `xKey` at
all for Text/Number/Check properties.

`any@v1` resolves *keys* both ways (ADR-006 §6) and passes every
*value* verbatim. Consequences, verified in code 2026-08-26:

1. **Handle collision.** `_resolve_prop_seg` matches id, xKey or
   name first-wins; `_normalize_record` labels values by `xKey or
   name or id`. A UI-made type with two Selects ("Status",
   "Priority") normalizes both under `"select"` — one value silently
   overwrites the other in every `query_objects` row, and a write to
   `{"select": …}` lands on whichever the server listed first. This
   is the ADR-006 §6 no-silent-drop failure, on the read side.
2. **No value resolution.** The agent must already know an option's
   key (nothing consumes `format.options`), must hand-build
   `["any://" + id]`, must produce a midnight instant for a `date`,
   and cannot clear a value. Failures surface as server 400s; the
   create-path `property.format_violation` omits the expected shape.
   `docs/00-plan.md` §"auto-hydration" already names opaque
   `bafyrei…` refs as why the agent ignores the graph.
3. **No definition surface beyond add.** No PATCH/DELETE mapping
   (`docs/api-parity.md` C, "demand-driven"), so a select the agent
   declares never gets options; no attach/detach; `create_type`
   drops `scope`/`description` on inline properties.
4. **Stale guidance.** `_any.md` shows property rows as `{id, name,
   xKey, kind}` with no `format`; `_memory.md` and ADR-007 say
   "object-kind property", a shape this server never had.

## Decision

### 1. The property handle

The handle the agent reads and writes a property by is:

- its `xKey`, when the xKey is present, unique within the type, and
  **not a marker** — the marker set is `select`, `tags`, `links`,
  `relation`, `date`, `url`, `email`, `longtext` (any-ui's
  `toAddPropertyParts`), plus any xKey equal to a `format.type`;
- otherwise its `name`; a true duplicate name is suffixed with the
  prop id (`Status~EwyHGrtTdxB`) so no two handles on a type collide.

`list_properties` rows carry `handle` explicitly next to `id`, `name`,
`xKey`. Resolution order for a written key is id → handle → name →
xKey; a key matching more than one property **errors listing the
candidates** (never first-match). `_normalize_record` labels by
handle. Builtin groups are untouched.

**`xKind` is the client-convention marker** (any-ui WEB-317: a slug
`xKey` per property, the kind marker in `xKind`, the server field that
exists for exactly this and never interprets it). The vocabulary is
any-ui's — `packages/api-core/src/resources/types.ts` `STRING_XKINDS`
(`longtext`, `date`, `url`, `email`, `select`) and `ARRAY_XKINDS`
(`tags`, `links`, `relation`) — until a shared property spec exists
(BOB-84); anybao mirrors it, it does not extend it. Two roles:

- **Beside a server format** the marker is redundant metadata the UI's
  picker/icons read: `any@v1` stamps `select` / `tags` / `links` /
  `date` next to `format.type` `select` / `multiselect` / `links` /
  `date`+`datetime`, so an agent-declared property looks like a
  UI-declared one.
- **Without a format** it is the whole convention: `url`, `email`,
  `longtext` are `kind: string` properties with no server validation
  (the server has no such formats; any-ui edits them as a text input
  with `inputMode` and renders an `href`). `any@v1` accepts them in
  the `format.type` spelling the model reaches for — `{"format":
  {"type": "url"}}` — and lowers that to `kind: string, xKind: url`
  with no `format` on the wire; a non-string `kind` alongside is an
  error, an explicit `xKind` passes through. Values are plain strings,
  written and read verbatim; the read ladder is any-ui's —
  `format.type` wins, then `xKind`, then the legacy marker xKey.
  `list_properties` rows carry `xKind`.

The marker-xKey rule above is the bridge for properties that predate
WEB-317 (the UI must read them forever; so must we).

### 2. Writes are definition-aware

One seam, `_encode_value(space, type_id, prop_def, value)`, is applied
in `_resolve_prop_groups` (so `create_object` and `update_object`
share it) and in `enrich@v1`'s apply path (replacing its
datetime-only special case). It mirrors any-ui's `encodePropertyWrite`
and extends it with name resolution. The result of a write reports
every resolution it made — `{"objectId", "resolved": {"<handle>":
<wire value>}, "createdOptions": [{"handle", "key", "name"}]}` — so the
trace shows what the agent's words became (ADR-002: if it isn't in
the trace, it didn't happen).

| format | accepted input | wire value |
|---|---|---|
| `select` | option key; option name (exact, then casefold); | the key |
| `multiselect` | list of the above; a scalar → one-element list; deduped, order kept | list of keys |
| `links` | object id; `any://<id>`; typed `any://o/<sid>/<id>` (normalized down); an object **name** — exact `any.name` match, then casefold-unique, within `format.filter` when declared; a list of any of these; a scalar → one-element list | `["any://<id>", …]` |
| `date` | `instant(…)`, ISO date/datetime string, epoch seconds or millis | `{"$date": <midnight UTC>}`; ISO `YYYY-MM-DD` when the prop is legacy `kind: string` |
| `datetime` | same inputs | `{"$date": …}`; RFC 3339 when legacy `kind: string` |
| bare kind | the kind's JSON shape; `"42"` → 42 for `number` | verbatim after a client-side kind check that names the expected shape |
| any | `None` | a `$unset` of `<typeId>.<propId>` via `/modify` (synced scope; account/local error naming the reason) |

**Options are created on demand.** An unknown select/multiselect
value creates the option the any-ui way — key `slugify(name)`
uniquified `_2`, `_3`… (no dots), a color from the ten-name palette
picked by key hash (deterministic — the guest has no randomness, and
replay must agree), `pos` appended after the last option's — with one PATCH per
type group before the value write, then the props cache is
invalidated. `create_options=False` on `create_object` /
`update_object` turns this into an error listing the existing
options. Casefold matching means "Done" and "done" are the same
option; a typo makes a new one, which the result and the trace show.

**Links never mint objects.** Zero or more than one name match errors
with the candidates (`{id, name, types}`) so the model picks an id.
`enrich@v1` keeps its own search-then-create for its targets.

**Mixed scopes.** `update_object` groups patches by declared scope and
issues one `set` per (type, scope); an unset on a non-synced property
errors up front instead of surfacing the SDK's rejection.

### 3. Reads are hydrated

`query_objects(normalize=True)` (the default) resolves display forms in
the normalized record:

- `select` → the option's `name`; `multiselect` → list of names. A
  dangling key passes through as the raw key. Names round-trip: §2
  accepts them on write.
- `links` → `[{"id", "name", "types"}]` stubs, resolved with **one**
  batched `{"id": {"$in": […]}}` query per result page (cap 200 ids;
  beyond it the tail stays raw `any://` strings), memoized per cell.
  A missing object is `{"id", "name": None}`.
- `date`/`datetime` stay instants (ADR-019).

`normalize=False` returns the raw wire shape, unchanged. `filter` /
`sort` accept the same display forms — `{"task.status": "In
progress"}` resolves to the key, `{"task.related": "Dune"}` to
`any://<id>` — through `_resolve_filter`, reusing §2's resolver; a
non-resolving name errors rather than matching nothing. Programs that
compare raw select values (`recall`, `enrich` diffing) read with
`normalize=False` or compare handles → keys explicitly.

### 4. Definition surface at parity with the UI

New `any@v1` tools, all `@span`'d, docstrings the docs (ADR-010),
calling the routes through the http effect directly — the host client
(`runtime/src/anyapi.rs`) gains passthroughs only when a host consumer
appears (unused Rust is a clippy failure):

- `patch_property(space, type_key, prop_key, set=None, unset=None)` —
  the wire PATCH with handles resolved; pinned paths error before the
  call.
- `set_option(space, type_key, prop_key, key_or_name, name=None,
  color=None, pos=None)` / `remove_option(...)` — option CRUD on top
  of `patch_property`; `set_option` on an unknown key mints it as §2
  does.
- `reorder_property(space, type_key, prop_key, after=None)` — writes
  `meta.pos`.
- `archive_property(space, type_key, prop_key)` — the UI's
  `meta.anyUiArchived = "1"` marker; `delete_property` is the real
  tombstone and says so in its docstring.
- `attach_type(space, object_id, type_key)` / `detach_type(...)` —
  the dedicated routes; never `$addToSet any.types`.
- `create_type` / `add_property` forward `scope`, `description`,
  `format.filter`, `format.ui` and seed `format.options` at create;
  new properties get `meta.pos` appended so the UI shows them in
  creation order. Dates the agent declares are format-bearing
  instants (server default), never any-ui's format-less string dates.
- `list_properties` returns rows sorted by `meta.pos` (byte compare,
  positioned first, then name), hides archived rows unless
  `include_archived=True`, and carries `handle` plus an ordered
  `options` list (`[{key, name, color}]` by `pos`).

Host test fake (`runtime/src/testutil.rs`) keeps `format` and `meta`
on `add_property` so host tests can observe them.

### 5. Guidance

`_any.md` gains a "Properties and formats" block: the row shape
`{handle, name, kind, format{type, options, filter}, scope,
meta.pos}`; the §2 write table in five lines; "check `format` before
writing someone else's type"; `any.tags` (free-form labels) vs a
select; attach/detach for membership; option CRUD; the existing
datetime-filter trap. `_space_context.md` lists formats and options
with a space's types so the vocabulary precedes the write.
`_memory.md` and ADR-007 say "links-format property".
`docs/helper-style.md` drops "uses `kind` (not `format`)";
`docs/debugging.md` shows how a resolution reads in a trace.

## Consequences

- The ADR-006 §6 no-silent-drop rule now covers values: a value that
  cannot be encoded for its definition errors client-side with the
  expected shape; nothing rides the wire to fail with an opaque 400.
- One extra definitions read per type per cell (already paid for key
  resolution), one PATCH per newly created option, one batched query
  per hydrated page. Replay-deterministic like every other effect.
- Select values the model sees change from keys to names. Writes
  accept both, so nothing that echoed a key back breaks; raw-value
  comparisons move to `normalize=False`.
- The marker-xKey rule is a bridge for any-ui's convention; when
  any-ui moves the marker to `xKind` it becomes dead weight and can
  go, dated in the code pointer.

## Work plan (one commit topic each, in order)

1. Handle rule (§1) + regression fixture with two `xKey: "select"`
   props and one xKey-less prop — a bug fix, may land as a hotfix
   ahead of acceptance.
2. `_encode_value` + option autocreate + links name resolution + unset
   (§2); enrich adopts it.
3. Hydrated reads + display-form filters (§3).
4. Definition tools + `api-parity.md` (§4).
5. Skills and docs (§5).
6. Tests: encoder matrix mirroring any-ui's `property-values.test.ts`,
   autocreate, ambiguity, hydration; rig e2e with a type created **in
   any-ui** (Select + Multiselect + Relation + Date) driven through
   `check-anybao-changes` in both directions.

## Upstream tickets (to file)

- `any-ui`: kind marker → `xKind`, slug `xKey` on every new property;
  `NumberCell` commits `null` to clear, which the SDK kind check
  rejects (`property-values.ts:58-67` vs `NumberCell.tsx:58`).
- `any`: stale comments — date defaults to `string`
  (`internal/api/types.go:29`, `handlers_types.go:112`; the SDK
  defaults to `datetime`), options cannot be seeded at create
  (`types.go:70`; they can); website docs list attach/detach as 501
  (live since `dc83801`); `property.format_violation` on the create
  path should name the expected shape; `docs/24-data-views.md` date
  grouping predates instants. Optional: an `xKey → propId` resolver
  or uniqueness hint on the property route.

## Resolved questions (2026-08-26)

- *Autocreate options by default?* Yes, echoed in the result and the
  trace; `create_options=False` to refuse.
- *Hydrate select values to names on read?* Yes; writes accept both,
  raw-value consumers use `normalize=False`.
- *Archived properties?* Hidden by default, still writable with a
  warning in the result.
