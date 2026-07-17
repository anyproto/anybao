### recall(client, space, brain_object_id?, chat_object_id?) [setup]
Bind recall to one space over an any@v1 client. `brain_object_id` feeds the memory source of `by_period` (get it from `c.get_brain(space)`), `chat_object_id` the turns/chunks sources; `None` skips that source.

### search(query, scopes?, limit?) [getter]
Index search; returns the raw hits (`{scope, objectId, dataset, recordId, score, …}`) unwrapped from the envelope. Default scopes `("agent", "history", "basic")`, limit 10.

### hydrate(hits) [getter]
Hit pointers → `(hit, record)` pairs — one `$in` query per (object, dataset), hit order kept, missing records dropped.

### by_period(from_ts, to_ts) [getter]
Everything in `[from_ts, to_ts]` (unix seconds, inclusive): memory items by validFrom, turns by createdAt, chunks by period overlap. Merged and time-sorted, each record tagged `source` ∈ memory|turn|chunk.

### neighbors(object_id) [getter]
1-hop graph neighborhood: `{"forward": [{typeId, propId, propName, targetId}], "backlinks": [{sourceId, typeId, propId}]}` — forward from the object's links-format property values, backlinks from the server's reverse read.
