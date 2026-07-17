The client for the `any` server — everything in a space is a typed object,
and this is how you read and write it. Get the client once, then `space` is
an explicit first argument on every call (cross-space is normal):

```python
c = use("any@v1").client()
```

Surface: typed objects (`create_object` / `update_object` /
`query_objects`), per-object datasets (`query` / `upsert_record` /
`modify`), editor bodies (`get_markdown` / `put_markdown`), the type
catalog (`list_types` / `list_properties` / `create_type` /
`add_property`), full-text search (`search`), the memory brain
(`get_brain` / `create_memory` / `evolve_memory`), chat (`chat_send`),
and the user's live view (`get_ui_context`). Types and properties are
named by **xKey** — the client resolves them to the server's content
ids, and query rows come back xKey-nested (never raw ids). Errors raise
`AnyError` (`{code, message}` from the wire).

Method signatures and per-method notes are in the schema — call the method
you need; the names below are the whole surface.
