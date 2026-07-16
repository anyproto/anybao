### create_object(space, body) [mutator]
Create a typed object. `body`: `{"types": [typeId, …], "initialProperties": {"any": {"name": …}, "<typeId>": {propId: value}}}`. Properties are nested type groups keyed by type; anywhere else errors. Returns `{objectId}`.

### query_objects(space, filter?, sort?, limit?, offset?) [getter]
Cross-object query over the space's object collection. `filter` on `any.*` fields and type-property paths (e.g. `{"any.types": typeId}`, `{"id": {"$in": [...]}}`). Returns object rows.

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

### get_ui_context(space) [getter]
The user's current view — the `ui_context` pointer any-ui maintains (`{spaceId, objectId, view, updatedAt}`). Resolves "here" / "this page" / "this object". `None` when unavailable.

### list_types(space) [getter]
All types in the space (`{id, name, xKey}`). Discover before creating — reuse an existing type instead of inventing a parallel one.

### list_properties(space, type_id) [getter]
One type's property catalog (`{id, name, xKey, kind}`) — the xKey↔propId map for reading/writing that type's values.

### create_type(space, body) [mutator]
Idempotent ensure-type. `body`: `{"name", "properties"?: [{"name", "kind"?}, …]}`. Reuses an existing type (by xKey or builtin id); xKeys auto-slug from names; result is immediately writable. Returns `{typeId, created, addedProps}`.

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
