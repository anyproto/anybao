# ADR-007: Memory & graph write policy

Status: **Proposed** (awaiting review)
Date: 2026-07-07
Builds on: ADR-006 (data contracts); plan §4b/§4c; old `amemory@v2.md`
(the proven policy prose); A-MEM (paper) as the evolution reference

## Context

The old system's biggest memory failure was not machinery but the
missing POLICY layer: `addMemory` had zero callers in guidance, four
recall paths disagreed, scoring fields were write-only, edges were
never traversed. This ADR fixes when/what the harness memorizes and
links, how it avoids duplication and vocabulary drift, and the query
idioms the discipline is designed to feed — so recall stays high and
the bird's-eye view stays accurate as data grows. Policy *text* ships
as skills/tool docs (audited, evidence rule); this ADR fixes the
policy *content* and the mechanisms enforcing it.

## Decision

### 1. Memory write policy — agent-judged, budgeted, explicit

Port the proven `amemory@v2.md` policy (was live and working):
- **Budget ~1–2 saves per turn**, explicit `addMemory` tool calls — NO
  per-turn auto-extraction (the old classifier path was rightly
  discarded: latency + cost without quality).
- **Save**: stable preferences, decisions (with the why), domain facts,
  hard lessons, shipped outcomes. **Skip**: greetings, meta-chatter,
  restatements, things already in history/chunks, low-confidence
  speculation. (Full ✓/✗ table ported into the memory tool doc.)
- `category` + one-line `context` required at save time (agent-direct
  path); categories stay an open slug set with the documented builtins.

### 2. Dedup: search-before-save, judge-confirmed

Every save first runs recall over scope `agent` (index candidates),
then a cheap judge (`classify` tier): *same fact?* → **merge** (evolve
the existing item: context/tags/confidence, `modifiedAt` bumps) |
**supersede** (new item + `supersedes` edge) | **create**. Returned as
`{deduplicated: true, mergedInto}` — a success, not an error (v1
convention). The old 0.85-cosine lesson is inherited as a *retrieval*
rule, not a threshold rule: candidate retrieval is content-focused,
the judge decides — no magic similarity constant to drift.

### 3. Graph write discipline (the §4c doctrine, operationalized)

- **Entity canonicalization**: before creating any object, search for
  it (`search-before-create`, already doctrine for objects); the
  identities directory anchors people.
- **Typed-property preference**: a recorded fact naming two entities ⇒
  ensure both objects exist, link via an object-kind property. Prose
  mentions feed the index; properties feed the graph. Memory edges are
  properties (plan §4b) and may point at ANY object, not just
  memories.
- **Edge vocabulary**: ships as a curated starter set (relates_to,
  caused_by, supersedes, decided_in, part_of, owned_by, discussed_in —
  final list in the skill); the write path **suggests existing
  properties first** (catalog query) and creates new edge types
  reluctantly. A consolidation trigger flags synonym clusters for
  review — vocabulary drift is the known killer of traversal.
- **Object vs property-value**: reify as an object anything referenced
  twice or linkable (people, projects, decisions, meetings); keep as a
  scalar property what is purely descriptive of one object.

### 4. Link generation & evolution — trigger jobs, promoted behind evals

A-MEM's write-time steps run async (trigger on memory-item creation,
budgeted):
1. **Link generation**: seed-search neighbors of the new item (scopes
   agent+history+basic) → LLM proposes typed links → written as
   properties. Generalizes beyond memory: the same job can propose
   links for new docs/chunks.
2. **Evolution** (neighbor context/keyword refresh), **reflection**
   (synthesize unaccessed clusters into insights, detect
   contradictions → lower confidence + `contradicts` edge), and
   **salience decay** stay OFF until each passes an eval — they were
   never validated in production (amemory had them implemented but
   disabled). Mechanism ships; policy gates activation one job at a
   time.
3. **accessCount bump on recall** ships ON from day one (was live in
   v1); salience/confidence participate in recall rerank once decay/
   reflection give them meaning — until then they are recorded, not
   consumed (metrics-first, as with executor caps).

### 5. Recall idioms — one surface, three axes, then expand

The recall tool composes the axes the storage now supports (ADR-006):
- **Semantic**: `recall.search(q, scopes=["agent","history","basic"])`
  — memories, turns/chunks, and content in one call (the restored
  one-surface behavior).
- **Temporal**: `recall.by_period(from, to)` — fans across memory
  (validFrom), turns (createdAt), chunks (periodStart/End).
- **Graph**: `neighbors(id)` expansion on hits (forward props +
  backlinks, names resolved) — the third dimension applied AFTER
  retrieval, GraphRAG local-search style.
- **Drill-down**: chunk → children (recursive, ADR-006), run →
  trace (`effects.of` / viewer). Every summary keeps its pointers.
Boot injection stays lean: category names + counts only (v1 behavior);
a ranked "top memories" boot digest is deferred until scoring fields
are consumed (needs decay/reflection live to mean anything).

### 6. Bird's-eye maintenance = the composition

The accurate high-level picture is not one feature but the standing
composition: hierarchical chunks give lossless-by-pointer temporal
coverage; memory items give distilled facts; link generation keeps new
information attached to what it relates to; consolidation keeps
vocabulary and duplicates in check. All maintenance is trigger jobs
with run observability (ADR-006 §4) — when the bird's-eye view is
stale, a trigger's run log says why.

## Consequences

- The write side finally has policy with teeth: budget + table in the
  tool doc, dedup enforced in the save path itself.
- No magic thresholds: judges over candidates instead of cosine
  constants; jobs gated behind evals instead of speculatively on.
- Recall is one tool with three composable axes + drill-down — the
  four-disagreeing-paths era ends.
- Scoring fields stop being write-only the day their producers (decay/
  reflection) prove out — and not before.

## Open questions (reviewer input wanted)

1. **Save budget**: keep ~1–2/turn from v1, or scale with turn length?
   Lean: keep 1–2, revisit from metrics.
2. **Link-generation timing**: trigger per save (fresh but chatty) vs
   batched (e.g. hourly sweep over new items)? Lean: per save with a
   small debounce — freshness matters for the "attach new info"
   property.
3. **Dedup judge tier**: `classify` (consistent with orientation
   summaries)? Lean: yes.
