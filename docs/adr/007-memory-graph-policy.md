# ADR-007: Memory & graph write policy

Status: **Proposed** (awaiting review)
Date: 2026-07-07
Builds on: ADR-006 (data contracts); plan §4b/§4c; old `amemory@v2.md`
(the proven policy prose); A-MEM (paper) as the evolution reference

## Context

Correction of record (2026-07-07 review): the OLD generation DID have
full write guidance — `amemory@v2.md` (a tool doc, injected via the
tools section) carried the ✓/✗ table, confidence/importance rubrics,
and dedup instructions; toolcall_core pinned amemory second and
rendered a live category inventory at boot. The current bobrik-watch
deferred the wiring (hence "zero callers" there). **The decisive
empirical finding (user, from live testing): even WITH that guidance,
the agent saved/searched only rarely — essentially only when memory
was the conversation topic.** Prompt guidance alone does not produce
memory behavior; meta-tasks lose to the task at hand. This ADR
therefore pairs the ported policy prose with STRUCTURAL mechanisms
that do not depend on model initiative (§1b auto-extraction, §5
auto-recall injection) — plus dedup/vocabulary discipline and the
query idioms, so recall stays high and the bird's-eye view stays
accurate as data grows.

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

### 1b. Background extraction — the save-side structural mechanism

Because explicit saves are empirically rare, the volume comes from an
async **trigger job over newly persisted turns** (batched, off the hot
path — NOT the old synchronous per-save classifier): propose 0–2
candidate memories per turn (cheap tier), run each through the §2
dedup judge, save survivors with **provenance** (`fromSeq` pointer to
the source turn) and capped confidence (≤6 — machine-derived facts
never outrank user-stated ones). Fully auditable: the trigger's run
log shows what was derived from where; wrong derivations are
deletable and re-derivable (turns are append-only). Explicit
`addMemory` (§1) remains the high-confidence in-the-moment path.

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
**Auto-recall injection — the recall-side structural mechanism.** The
harness itself runs `recall.search(user_message)` at invocation start
(index-backed, no LLM call) and injects top hits as a budget-capped
"relevant memories" section in the boot context — A-MEM §3.4's
per-interaction retrieval, which v1 never wired (it injected only
category names). The agent's explicit search remains for deliberate
digging; the common case stops depending on model initiative.
Category-name inventory stays too (cheap, cache-stable); a ranked
"top memories" digest is deferred until scoring fields are consumed
(needs decay/reflection live to mean anything).

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
