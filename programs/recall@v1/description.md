Recall over one space — one surface, three axes. Semantic: `search`
over the any index. Temporal: `by_period` merges memory items, turns,
and chunks in a time range. Graph: `neighbors` walks forward link
properties plus server backlinks. Read-only.

```python
r = use("recall@v1").recall(c, space,            # c = any@v1 client
                            brain_object_id=…,    # for memory items
                            chat_object_id=…)     # for turns/chunks
```

The temporal sources live on different objects, so pass the ids you
need; a `None` id just skips that source. `hydrate` turns search hits
into full records in one batched read.
