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

### 1a. Empirical basis (bao-space export, 2026-07-07)

The old space's 55 memory items, audited: **46 (84%) were
chat_chunks/episodes, ALL with accessCount 0** — never once recalled;
the only retrieved items were lessons and preferences (accessCount 4
each) — the small distilled-fact set (~9 items). Chunk quality ranged
to garbage (question-fragment contexts, tokenized-noise keywords, raw
transcript bodies); edges were `[]` on all 55; ~30 singleton invented
categories showed live vocabulary drift. Conclusions baked into this
ADR: (a) the useful "memory" experience came from HISTORY injection
(boot chunks), not the memory store — funneling chunks into memory was
the mistake, v2's storage split is correct and chunks NEVER become
memory items; (b) the memory store must stay small and high-signal —
distilled stable facts only; (c) low save frequency is healthy
(Claude's own memory behaves the same) when what's saved is distilled.

### 1b. Background extraction — conservative, measured

An async **trigger job over newly persisted turns** (batched, off the
hot path — NOT the old synchronous classifier), with a HIGH bar
informed by §1a: propose candidates only matching stable-fact shapes —
preference / decision / lesson / durable domain fact (the categories
that earned recalls) — **never episodes or session summaries** (that's
the history channel's job). Each candidate passes the §2 dedup judge;
survivors save with **provenance** (`fromSeq` pointer) and capped
confidence (≤6 — machine-derived never outranks user-stated). Fully
auditable via the trigger run log; re-derivable (turns are
append-only). **ROI is measured, not assumed**: accessCount bumps on
recall (§4), so extracted-but-never-recalled and
injected-but-never-referenced rates tune or kill the extractor.
Explicit `addMemory` (§1) remains the high-confidence path.

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
**Auto-recall injection — the recall-side structural mechanism, with
tool-result framing.** The harness runs `recall.search(user_message)`
at invocation start (index-backed, no LLM call) and injects top 3–5
hits, budget-capped — A-MEM §3.4's per-interaction retrieval, which v1
never wired. Two constraints from prior experience (user: injected
memory near the system prompt gets treated as ground truth) and §1a:
- **Framed as a tool result, not prompt truth**: rendered as a
  synthetic recall call + result (like any tool output), each item
  carrying provenance date + confidence — evidence the model weighs
  and can discount as stale, not doctrine it obeys.
- **Scope `agent` only** (distilled facts). History is NEVER injected
  as memories — it has its own channels (boot chunk window; `history`
  scope on explicit search).
The agent's explicit search remains for deliberate digging.
Category-name inventory stays (cheap, cache-stable); a ranked "top
memories" digest is deferred until scoring fields are consumed.

### 6. Bird's-eye maintenance = the composition

The accurate high-level picture is not one feature but the standing
composition: hierarchical chunks give lossless-by-pointer temporal
coverage; memory items give distilled facts; link generation keeps new
information attached to what it relates to; consolidation keeps
vocabulary and duplicates in check. All maintenance is trigger jobs
with run observability (ADR-006 §4) — when the bird's-eye view is
stale, a trigger's run log says why.

### 7. Search quality is a dependency risk (named, 2026-07-07)

Everything above leans on `any`'s `/search` (index candidates for
recall, dedup, auto-injection, extraction judging) — and the stack's
validation so far is SYNTHETIC, written after the implementation
(BEIR-grade evals behind the defaults, `any` docs/search/). That
catches regressions; it does not catch wrong assumptions. What it has
NOT had is real daily usage: short conversational queries, small
personal corpora, memory items, turns/chunks, "did it find the thing I
meant". Treat as a first-class risk:
1. **Workload-specific golden recall eval** — a small fixture set from
   real data (the bao export is seed material: query → expected item),
   run against fts/vector/hybrid modes; extends the existing
   docs/search eval harness. Built alongside the recall tool, red in
   CI when recall regresses.
2. **Live signals double as search QA** — the §1b/§5 ROI metrics
   (injected-but-never-referenced, extracted-but-never-recalled) and
   explicit-search outcomes flag quality drift in production.
3. **Fixes go upstream** (fix-upstream doctrine): RRF knobs, chunking,
   embedder choice, scope handling — improved in `any`'s indexer, not
   worked around in the harness.

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
