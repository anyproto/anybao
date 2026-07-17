### create_object(space, body) [mutator]
Create a typed object. `body`: `{"types": ["<typeXKey>", …], "initialProperties": {"any": {"name": …}, "<typeXKey>": {"<propXKey>": value}}}`. Types and property groups are named by **xKey** (the client resolves them to ids); reserved builtin groups (`any`, `nav`) use their literal keys. Properties are nested type groups keyed by type; anywhere else, or an unknown type/property key, **errors** (never silently dropped). Returns `{objectId}`.

### update_object(space, object_id, body) [mutator]
Update an existing object by xKey. `body`: `{"name"?, "markdown"?, "<typeXKey>": {"<propXKey>": value}, …}` — same nested type-group shape as create_object. `name` sets the display name, `markdown` replaces the editor body, each type group patches that type's properties. Groups resolve BEFORE any write, so a bad key can't leave a partial update. Returns `{objectId}`.

### query_objects(space, filter?, sort?, limit?, offset?, normalize?) [getter]
Cross-object query over the space's object collection. `filter` / `sort` take readable **xKey** paths — an `any.types` xKey value (`{"any.types": "task"}`), dotted type-property paths (`{"task.status": "open"}`, `sort: ["-task.priority"]`), plus builtin/`id` keys (`{"id": {"$in": [...]}}`) — all resolved to server ids. Records come back **xKey-normalized**: user-type groups keyed by type xKey, props by prop xKey (`{"id": …, "any": {…}, "task": {"status": "open"}}`); builtin namespaces (`any`, `nav`) pass through verbatim. Pass `normalize=False` for the raw id-keyed shape (when you need the content ids themselves).

### query(space, object_id, dataset, filter?, sort?, limit?) [getter]
Per-object dataset query — `chat_messages`, `agent_turns`, `agent_memory_items`, `program_methods`, etc. Returns the dataset's records.

### upsert_record(space, object_id, dataset, record_id, value) [mutator]
Write one record to a plain dataset (whole-value `$set`, upsert by `record_id`). The generic path for unregistered datasets like `agent_triggers`.

### modify(space, body) [mutator]
Low-level dataset write: `{objectId, dataset, records: [{id, upsert?, ops: [{type, path, value}]}]}`. Prefer `upsert_record` / `create_object` unless you need partial ops.

### aggregate(space, pipeline) [getter]
Run an aggregation pipeline over the space's objects. For counts / grouping when a plain query won't do.

### get_markdown(space, object_id) [getter]
The object's editor body as markdown text.

### put_markdown(space, object_id, content) [mutator]
Replace the object's editor body with `content` (markdown). Whole-body write.

### list_spaces() [getter]
Every space on the account as raw rows (`{id, name, status, …}`). Operate on `status == "active"` unless told otherwise.

### create_space(name, description?) [mutator]
Create a new top-level space. Returns the single-space row: `id` (the new space id) plus `generalChatObjectId` (its derived general chat — write chat there, never create chat objects). The space starts empty — resolve/create types in it before typed writes (types and xKeys are per-space). Check `list_spaces()` first to avoid minting a duplicate.

### get_ui_context(space) [getter]
The user's current view — the `ui_context` pointer any-ui maintains (`{spaceId, objectId, view, updatedAt}`). Resolves "here" / "this page" / "this object". `None` when unavailable.

### list_types(space) [getter]
All types in the space (`{id, name, xKey}`). Discover before creating — reuse an existing type instead of inventing a parallel one. Reference a type by its **xKey** everywhere else (create/update/query resolve it); you never need the raw `id`.

### list_properties(space, type_id) [getter]
One type's property catalog (`{id, name, xKey, kind}`) — the xKey↔propId map. Read/write that type's values by **xKey** (create/update/query resolve them); the `id` is informational.

### create_type(space, body) [mutator]
Idempotent ensure-type. `body`: `{"name", "properties"?: [{"name", "kind"?}, …]}`. Reuses an existing type (by xKey or builtin id); xKeys auto-slug from names; result is immediately writable. Returns `{typeId, xKey, created, addedProps}` — use `xKey` (and the `addedProps` xKeys) for subsequent create/update/query calls.

### add_property(space, type_id, body) [mutator]
Add one property to a type. `body`: `{"name", "kind"? (default "string"), "meta"?}`. Returns `{propId}`.

### chat_send(space, chat_id, body) [mutator]
Post a message to a chat object. `body`: `{"text": …}`. Use the space's general chat id.

### append_turn(space, chat_id, body) [mutator]
Append an `agent_turns` record (server-assigned seq). Harness-level; conversations write these for you.

### create_chunk(space, chat_id, body) [mutator]
Append a compressed history chunk record. Harness-level (rollup).

### search(space, query, scopes?, limit?, mode?, enrich?) [getter]
Full-text / hybrid index search. Each hit is a matched RECORD: `{title, type, data, objectId, dataset, score}` — `data` is the matched snippet, `title`/`type` the resolved object (enriched), `objectId` what you query for the full object. Hits can share an `objectId` — dedup on it. `enrich=False` skips the title/type resolution.

### backlinks(space, object_id) [getter]
Objects that reference `object_id` through a links-format property. Returns `[{objectId, typeId, propId}]` (never null).

### get_brain(space) [getter]
The per-space brain object id (`{objectId}`) hosting `agent_memory_items`. Deterministic — no create race.

### create_memory(space, fields) [mutator]
Create a memory item; `fields` needs `category` + `context`. Server resolves the brain. Returns ModifyResult (`recordIds[0]` = item id). Prefer the `memory@v1` dedup path over raw creates.

### evolve_memory(space, item_id, fields) [mutator]
Evolve a memory item's mutable fields (salience, accessCount, confidence, importance, context, body, tags, edges). Author-only; `modifiedAt` bumped server-side.

### delete_memory(space, item_id) [mutator]
Delete a memory item by id.
