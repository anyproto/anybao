# Skill: _memory

Long-term memory policy (ADR-007). The store is small and high-signal:
distilled stable facts ONLY. The history channel (turns/chunks) already
keeps everything verbatim — episodes and session summaries NEVER become
memory items.

## Saving — budget ~1–2 per turn

Every save goes through `effect("memory.save_with_dedup", {"candidate":
{"category": ..., "context": ...}})`. `category` (lowercase slug) and
`context` (one-line fact) are REQUIRED; add `body` (detail),
`confidence` (1–10, user-stated facts rank above inferred ones),
`tags`, `edges`. A `{"deduplicated": true, "mergedInto": ...}` reply is
a SUCCESS — the fact was already known and got refreshed.

| ✓ Save | ✗ Skip |
|---|---|
| Stable preferences ("prefers tables as pages, not chat") | Greetings, meta-chatter, thanks |
| Decisions WITH the why ("picked sqlite: simpler ops") | Restatements of what was just said |
| Durable domain facts ("the staging server is at …") | Anything already in history/chunks (it's drillable) |
| Hard lessons ("bulk create without probe corrupted X") | Low-confidence speculation |
| Shipped outcomes ("migration to v2 completed 2026-07") | Session summaries, episode narration |

Builtin categories: `preference`, `decision`, `lesson`, `fact`,
`insight` (reflection-owned). The set is open — but check existing
categories before inventing one (vocabulary drift kills recall).

## Recall — one surface, three axes

- **Semantic**: `effect("any.search", {space, query, scopes: ["agent",
  "history", "basic"], limit})` — memories, turns/chunks, and content
  in one call. Hits are pointers; hydrate via `any.query` on
  `(objectId, dataset)` filtered by `recordId`.
- **Temporal**: query `agent_memory_items` by `validFrom`,
  `agent_turns` by `createdAt`, `agent_chunks` by period overlap.
- **Graph**: follow `edges` on a hit (`[{to, type}]`), and links-format
  properties on objects — expansion AFTER retrieval.

Top hits for the user's message are auto-injected at turn start as a
`recall` tool result — dig explicitly when you need more than they
show. When a deliberate dig actually USES a memory item, bump it:
`effect("memory.bump_access", {"item_id": ..., "current_count":
<its accessCount>})` — accessCount is the signal that keeps useful
memories alive (auto-injected items are bumped for you).

## Evolving

`effect("memory.evolve", {"item_id": ..., ...})` may change only:
salience, accessCount, confidence, importance, context, body, tags,
edges. Everything else is immutable after create.

## Graph write discipline

- **Canonicalize entities**: search before creating any object.
- A fact naming two entities ⇒ make sure both objects exist and link
  them via an object-kind property. Prose mentions feed the index;
  properties feed the graph.
- **Edge vocabulary** (curated — prefer these, create new types
  reluctantly): `relates_to`, `caused_by`, `supersedes`, `decided_in`,
  `part_of`, `owned_by`, `discussed_in`. (`derived_from`,
  `contradicts` are reflection-owned.)
- Reify as an object anything referenced twice or linkable (people,
  projects, decisions, meetings); keep purely descriptive scalars as
  properties.
