# M5 — memory & background cognition (ADR-007)

All mechanisms landed 2026-07-08; the LLM-quality halves of the evals
await live traffic (the ROI metrics are the standing judge).

- [x] Recall primitives — one surface, three axes (programs/recall@v1.py:
      search / by_period / neighbors incl. backlinks with pre-route fallback) +
      `hydrate` (pointer→record, shared with the judge and injection).
- [x] Memory write facade + dedup judge (programs/memory@v1.py) — add/evolve/
      delete/bump_access/save_with_dedup; the classify-tier judge runs
      inside the module (its llm call is a credentialed http effect,
      so the judge's exchange records in the same trace).
- [x] Auto-recall injection (programs/autorecall@v1.py, ADR-007 §5) — synthetic
      recall tool call + result at invocation start; both scopes
      rendered distinctly; relevance threshold, deep-history guard
      (boot-window raw tail), token budget; accessCount bump ON.
- [x] Background extraction (programs/extraction@v1) — batched sweep,
      stable-fact shape allow-list + confidence ≤ 6 + provenance
      fromSeq ENFORCED IN CODE; every candidate through the dedup
      judge; cursor in agent_job_state.
- [x] Hourly link-gen sweep (programs/linkgen@v1) — seed-search
      neighbors, curated edge vocabulary enforced in code, edges via
      memory.evolve.
- [x] Evolution / reflection / decay (programs/{evolution,reflection,
      decay}@v1) — mechanism present, triggers ship enabled=False;
      test_gated_mechanisms.py is the offline eval gate each must hold;
      live-quality evals (real LLM) are the activation bar.
- [x] Golden recall eval (recalleval.py + recall-eval.jsonl) — the
      workload-shaped fixture (12 items / 8 cases), red in CI below
      recall@5 = 1.0 when an index is live (`-m integration`); re-seed
      from a fresh bao export by regenerating item lines.
- [x] Live ROI metrics (programs/autorecall@v1.py — log_roi) — extracted-but-unrecalled (accessCount
      stuck at 0) + injected-but-unreferenced (significant-word
      heuristic), logged per injection by the toolcaller. These tune or
      kill the extractor/injector — review at the M4 gate's metrics
      checkpoint and monthly after.

Gaps closed on review (recon cross-check 2026-07-08): explicit-recall
accessCount bump = `memory.bump_access` effect (auto-recall bumps
host-side); batched judge/summary calls need no `complete_many` — the
generic `batch` effect (`effect("batch", {"name": "llm.chat",
"payloads": [...]})`) already fans out with input-order records
(ADR-002 resolved Q3); the stable block's tool-docs + memory-category
sections assemble in `anybao serve` (ADR-005 §5).

Live-verified 2026-07-08 (gate walk 1): auto-recall injected on a real
conversation (accessCount bumps, ROI log `referenced: true`), the
extraction trigger fired through the control API and swept real turns
through the dedup judge in the guest, and the golden recall eval hit
recall@5 = 1.0 on the live hybrid index. Two policy calibrations came
out of it: the RRF score scale (threshold 0.35 → 0.015) and the humble
merge (ADR-007 §2 amendment — machine re-sightings were blurring
user-stated facts).

Learnings:

- Guest programs are the natural home for every sweep: `effect()` +
  `use()` make them offline-testable by exec-ing the source with a fake
  `effect` global — no engine needed for policy tests.
- LLM discipline lives in code, not prompts: shape allow-lists,
  confidence caps, edge vocabularies, and neighbor-id validation all
  reject bad model output instead of trusting it.
- The dev server's indexer doesn't populate in the test window — the
  golden eval skips there (documented convention); run it against a
  server with the embedder enabled for the real signal.
