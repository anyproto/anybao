The memory WRITE facade over a space's brain (`agent_memory_items`).
Bind it, then save through the dedup path:

```python
m = use("memory@v1").memory(c, space)   # c = any@v1 client
m.save_with_dedup({"category": "…", "context": "…"}, rec)  # rec = recall@v1
```

`save_with_dedup` is the default save: recall supplies lookalike items,
a fast-tier judge decides merge | supersede | create, and machine
re-sightings never blur user-stated text or lower confidence. Raw
`add` skips dedup — use it only when you know the fact is new. Reads
go through recall@v1 or the brain's dataset, not here.
