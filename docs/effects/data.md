# `any.*` — primitive data-access effects

Doc-per-effect (ADR-002 §3-4). Source: `harness/src/anybao/data_effects.py`.

Low-level primitives — each mirrors ONE anyclient call 1:1. No catalog
resolution, no default space: the ergonomic layer (anyhelper / recall /
memory tools) is space-resident programs composed over these
(ADR-004 §1). `space` is always an explicit argument — the agent has
arbitrary-spaceId scope, cross-space calls are normal.

Reads carry cap `data.read` and are re-executable in mock replay;
writes carry `data.write` and are not.

## Reads (`kind: read`, cap `data.read`)

| Effect | anyclient call | Returns |
|---|---|---|
| `any.query(space, object_id, dataset, filter?, sort?, limit?, offset?)` | `query` | record list (one object's dataset: `chat_messages`, `agent_turns`, `editor_blocks`, …) |
| `any.query_objects(space, filter?, sort?, limit?, offset?)` | `query_objects` | record list (per-space objects collection) |
| `any.search(space, query, scopes?, limit?, mode?)` | `search` | `{hits, mode, vectorStatus}` |
| `any.aggregate(space, pipeline)` | `aggregate_objects` | aggregation result |
| `any.get_markdown(space, object_id)` | `get_markdown` | markdown string |
| `any.list_properties(space, type_id)` | `list_properties` | `[{id, name, xKey, kind}]` |

Absent optional args are dropped before the wire (no `null` bodies).

## Writes (`kind: mutate`, cap `data.write`)

| Effect | anyclient call | Returns |
|---|---|---|
| `any.modify(space, body)` | `modify` | `{versionId, changeId, recordIds}` |
| `any.create_object(space, body)` | `create_object` | `{objectId}` |
| `any.create_type(space, body)` | `create_type` | `{typeId}` |
| `any.add_property(space, type_id, body)` | `add_property` | `{propId}` |
| `any.upsert_record(space, object_id, dataset, record_id, value)` | `upsert_record` | upsert result |
