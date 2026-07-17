### memory(client, space, llm_chat?) [setup]
Bind the facade to one space's brain (server-resolved — no object id needed). `client` is an any@v1 client; `llm_chat` defaults to llm@v1 chat (injectable for tests).

### save_with_dedup(candidate, recall) [mutator]
The default save path. `candidate`: item fields (`category` + `context` required, plus `body`, `tags`, `edges`, `confidence`, `source`, …); `recall`: a recall@v1 object over the same space. Judge verdict merge/supersede/create is applied; merge is a SUCCESS: `{"deduplicated": True, "mergedInto", "action"}`, otherwise `{"itemId", "action"}`.

### add(category, context, **fields) [mutator]
Create an item unconditionally. `category` (lowercase slug) + `context` (one-liner) required. Returns `{"itemId"}`. Prefer `save_with_dedup`.

### evolve(item_id, **fields) [mutator]
Evolve mutable fields only — salience, accessCount, confidence, importance, context, body, tags, edges; anything else raises before sending. Author-only; server bumps modifiedAt.

### bump_access(item_id, current_count) [mutator]
accessCount = current + 1 on recall — the ROI signal separating earning-its-keep from extracted-but-never-recalled.

### delete(item_id) [mutator]
Delete a memory item (author-only).
