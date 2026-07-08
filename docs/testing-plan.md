# anybao testing plan

Date: 2026-07-08 (M4/M5/M6 completion pass). The verification doctrine
follows the isolation principle: **everything nondeterministic is an
effect, so a recorded trace replays the whole system deterministically
— replay determinism is THE testable property**, and every layer below
exists to keep it cheap to assert.

## Layer model

| # | Layer | Runs | Gate |
|---|-------|------|------|
| L0 | Pure unit — policy/logic with fakes (no I/O, no engine) | every `uv run pytest` | red = broken logic |
| L1 | Golden replay — recorded traces re-driven strict/mock | every run | red = determinism broke |
| L2 | Guest-module policy — exec a program's source (`test_*_module.py`, `test_toolcaller.py`, sweeps) with a fake `effect`/`use`/`subcell` global | every run | red = module or loop policy broke |
| L3 | Wasi end-to-end — real `kernel.wasm`, scripted llm, fake anyclient (`test_runner_e2e.py`) | every run where `bin/kernel.wasm` exists (CI builds it); skip otherwise | red = boundary/engine broke |
| L4 | Wire integration — real `any` server (`-m integration`), incl. the `guest_use` live-shim (guest source over real http) | opt-in: `make test-integration` | red = wire contract broke |
| L5 | Live evals — golden recall eval vs a live index; ROI metrics review | opt-in + gate walks | red = retrieval quality regressed |
| L6 | Gate walk — docs/cutover-checklist.md, side-by-side vs bobrik-watch | manual, once per cutover | human sign-off |

Conventions: offline by default (`addopts -m 'not integration'`);
fixtures are JSONL one-record-per-line (never pretty-printed);
`UPDATE_GOLDEN=1` regenerates goldens (review the diff!); integration
tests poll-then-SKIP on async-indexer timing but assert firmly on
direct reads.

## Coverage matrix (subsystem × layer, after this pass)

| Subsystem | L0 | L1 | L2 | L3 | L4 |
|---|---|---|---|---|---|
| trace/replay/blobs (anyrt) | ✓ test_trace, test_blobstore | ✓ | — | ✓ test_wasi_engine | — |
| effect boundary + caps | ✓ test_effects, test_caps, test_effects_impl | ✓ (denial records) | — | ✓ | — |
| executor/kernel + subcell | ✓ | ✓ | — | ✓ fuel/epoch/mem | — |
| llm@v1 adapters + transport | — | ✓ | ✓ test_llm_module | — | seed via `anybao llm-seed` |
| toolcaller@v1 loop (ceilings/mailbox/digest) | — | ✓ | ✓ test_toolcaller | ✓ test_runner_e2e | — |
| any@v1 client + data calls | ✓ test_anyclient, test_anyclient_sse_memory | — | ✓ test_any_module | ✓ (runner e2e) | ✓ test_integration |
| history@v1/boot window/rollup | — | — | ✓ test_history_module, test_rollup_program | ✓ (runner e2e) | ✓ chunk-level (integration) |
| recall@v1 + eval | — | — | ✓ test_recall_module | — | ✓ test_recall_integration (guest_use), live eval |
| memory@v1/dedup | — | — | ✓ test_memory_module | — | ✓ test_memory_integration (guest_use) |
| autorecall@v1 injection + ROI | — | — | ✓ test_autorecall_module | ✓ (runner e2e) | — |
| extraction@v1/linkgen@v1 | — | — | ✓ test_cognition_programs | ✓ (program-in-guest e2e) | — |
| decay/reflection/evolution (gated OFF) | — | — | ✓ test_gated_mechanisms | — | — |
| triggers (sched/runtime/store/control/events) | ✓ test_triggers, test_trigger_runtime, test_trigger_control, test_trigger_events, test_trigger_shared_registry | — | — | — | ✓ test_trigger_events_integration |
| watcher | ✓ test_trigger_runtime | — | — | — | — |
| deploy/toolmd/modules/skills | ✓ test_deploy, test_toolmd, test_skills | — | — | ✓ deploy→use() live test | ✓ |
| overlays | ✓ test_overlays | — | — | — | — |
| runner (composition) | — | ✓ (trace dumped) | — | ✓ test_runner_e2e | ✓ test_integration runner tests |
| serve composition | ✓ test_serve | — | — | — | L6 gate |
| replay tooling / metrics / viewer | ✓ test_replay, test_metrics, test_viewer | ✓ | — | — | — |
| CLI | ✓ test_cli | — | — | — | — |

## Gaps this pass closes (the "write tests" list)

1. **Runner end-to-end, offline (L3)** — the composition was only
   integration-tested. `test_runner_e2e.py`: Runner with a scripted llm
   transport + REAL WasiEngine + fake anyclient → the toolcaller drives
   a model cell that reaches space data and memory through the guest
   modules; asserts: turn persisted with traceRef, trace file dumped +
   parseable, boot window + auto-recall injected into the first llm
   request, ROI log written, effects recorded.
2. **Guest program through the real kernel (L3)** — sweeps were only
   L2-tested. Same file: `run_program("rollup@v1")` with the real
   source via DictResolver → guest `use()` + `effect()` → L1 chunk
   created through the fake client; proves import-free guest-safe
   source.
3. **Replay determinism for the guest stack (L1)** — a conversation
   trace whose `http.*` data calls and nested `llm.chat` (the dedup
   judge) replay strict must pass; a mutated record must diverge.
4. **Serve loop resilience (L0)** — feed error → reconnect (already
   partially covered; extend if the loop changes).

## Standing rules

- Every new mechanism lands its tests in the same commit (repo rule).
- LLM discipline is asserted in code-level tests (allow-lists, caps,
  vocabularies) — never by trusting prompts.
- New effects: register in Runner, document in docs/effects/, and add
  a classification test (read/mutate + cap) — the catalog is data.
- Integration suite must be run against a live server before any
  release-ish moment: `ANYBAO_TEST_SERVER=... uv run pytest -m
  integration` (13.8s today).
- The golden recall eval is the retrieval-quality tripwire; ROI
  metrics (`roi.injection_stats` / `roi.extraction_stats`) are the
  standing production judges — review at every metrics checkpoint.
