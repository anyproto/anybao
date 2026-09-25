# Skill: _memory

Long-term memory policy. The store is small and high-signal:
distilled stable facts ONLY. The history channel (turns/chunks) already
keeps everything verbatim — episodes and session summaries NEVER become
memory items.

## Saving — budget ~1–2 per turn

Get the facades once: `c = use("agent:any@v1")`, `mem =
use("agent:memory@v1").memory(c)`, `r =
use("agent:recall@v1").recall(c, baoSpaceConfig)`. Memory has ONE
home — the bao space's brain; there is no per-space memory and the
facade takes no space. A fact about a user space goes in context
(and tags), never into that space: never ensure_bundle bao/v1
anywhere (refused). Every save goes through mem.save_with_dedup(candidate,
recall=r) with `candidate = {"category": ..., "context": ...}`.
category (lowercase slug) and context (one-line fact) are REQUIRED;
add body (detail), confidence (1–10, user-stated facts rank above
inferred ones), tags, edges. A `{"deduplicated": true,
"mergedInto": ...}` reply is a SUCCESS — the fact was already known and
got refreshed.

| ✓ Save | ✗ Skip |
|---|---|
| Stable preferences ("prefers tables as pages, not chat") | Greetings, meta-chatter, thanks |
| Decisions WITH the why ("picked sqlite: simpler ops") | Restatements of what was just said |
| Durable domain facts ("the staging server is at …") | Anything already in history/chunks (it's drillable) |
| Hard lessons ("bulk create without probe corrupted X") | Low-confidence speculation |
| Shipped outcomes ("migration to v2 completed 2026-07") | Session summaries, episode narration |

Builtin categories: preference, decision, lesson, fact,
insight (reflection-owned). The set is open — but check existing
categories before inventing one (vocabulary drift kills recall).

## Recall — one surface, three axes

- **Semantic**: `r.search(query, scopes=["agent", "history", "basic"],
  limit=...)` — memories, turns/chunks, and content in one call.
  Returns a bare LIST of hit pointers (unlike c.search, which wraps
  them in `{hits, mode, vectorStatus}`); hydrate via r.hydrate(hits).
- **Temporal**: r.by_period(from, to) — seconds or ISO strings;
  one instant range scan each over memory items (validFrom), turns
  (createdAt) and chunks (period overlap), merged by time. Per-day/
  week/month turn counts: history.activity(c, space, chat_id, unit).
- **Graph**: r.neighbors(object_id) — follow a hit's edges
  (`[{to, type}]`) and relation properties on objects; expansion
  AFTER retrieval.

"What do you remember?" is ENUMERATION, not search — `r.search("")`
is rejected (the index has no browse-all mode). List the brain
dataset instead: `c.query(baoSpaceConfig, c.get_brain()["objectId"],
"agent_memory_items", limit=...)` (sort/filter by category,
salience; time fields validFrom/createdAt/modifiedAt are
instants — filter with instant(seconds), render with fmt_ts).

Top hits for the user's message are auto-injected at turn start as an
already-run run_cell (the recall idiom; rec stays bound for reuse)
— dig explicitly when you need more than its digest shows. When a deliberate dig actually USES a memory item, bump it:
`mem.bump_access(item_id, current_count=<its accessCount>)` —
accessCount is the signal that keeps useful memories alive (auto-injected
items are bumped for you).

## Evolving

mem.evolve(item_id, ...) may change only: salience, accessCount,
confidence, importance, context, body, tags, edges. Everything else is
immutable after create.

## Graph write discipline

- **Canonicalize entities**: search before creating any object.
- A fact naming two entities ⇒ make sure both objects exist and link
  them via a relation property. Prose mentions feed the index;
  properties feed the graph.
- **Edge vocabulary** (curated — prefer these, create new types
  reluctantly): relates_to, caused_by, supersedes, decided_in,
  part_of, owned_by, discussed_in. (derived_from,
  contradicts are reflection-owned.)
- Reify as an object anything referenced twice or linkable (people,
  projects, decisions, meetings); keep purely descriptive scalars as
  properties.
