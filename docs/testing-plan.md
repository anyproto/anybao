# anybao testing plan

The verification doctrine follows the isolation principle: **everything
nondeterministic is an effect, so a recorded trace replays the whole
system deterministically — replay determinism is THE testable
property**, and every layer exists to keep it cheap to assert. The
runtime is Rust (`runtime/`, binary `anyrt`); the guest programs it runs
are Python exec'd in the wasm kernel. Tests split along that seam.

## Layer model

| # | Layer | What | Runs | Gate |
|---|-------|------|------|------|
| L0 | Cargo unit (runtime) | Rust policy/logic + replay determinism, with fakes — effect boundary, caps, broker, trace/replay, deploy, resolver, triggers, routes, toolmd | `cargo test --manifest-path runtime/Cargo.toml` | red = runtime logic or determinism broke |
| L2 | Guest-module policy (pytest) | exec a program's source (`tests/test_*_module.py`, `test_toolcaller.py`, the sweeps, `test_skills.py`) with a fake `effect`/`use`/`now` global | every `uv run pytest` | red = a guest module or its loop policy broke |
| L3 | Runtime binary end-to-end | `tests/test_rt_e2e.py`: `anyrt run` as a subprocess against a stdlib fake serving both backends (any server + anthropic); the CLI and the wire are the whole contract | every `uv run pytest` where `bin/kernel.wasm` + `runtime/target/*/anyrt` exist; skips otherwise | red = binary/boundary/engine broke |
| L4 | Wire integration | real `any` server (`-m integration`) — `test_integration.py`, `test_recall_integration.py`, `test_memory_integration.py`, incl. the `guest_use` live-shim (guest source over real http) | opt-in: `make test-integration` | red = wire contract broke |
| L5 | Live evals | golden recall eval vs a live index (`test_recall_eval.py`); ROI metrics review | opt-in + gate walks | red = retrieval quality regressed |
| L6 | Gate walk | docs/cutover-checklist.md, side-by-side vs bobrik-watch | manual, once per cutover | human sign-off |

Conventions: offline by default (`addopts -m 'not integration'`);
fixtures are JSONL one-record-per-line (never pretty-printed);
`UPDATE_GOLDEN=1` regenerates cargo golden fixtures (review the diff!);
integration tests poll-then-SKIP on async-indexer timing but assert
firmly on direct reads.

## Coverage matrix (subsystem × layer)

| Subsystem | L0 (cargo) | L2 (guest pytest) | L3 (rt_e2e) | L4 (integration) |
|---|---|---|---|---|
| trace / replay / determinism | ✓ trace, replay | — | ✓ (trace asserted) | — |
| effect boundary + caps | ✓ broker, caps | — | ✓ (effect-only trace) | — |
| deploy / resolver / space modules | ✓ deploy, resolver | — | ✓ (programs loaded) | — |
| routes / classifier | ✓ routes | — | — | — |
| triggers (sched/store/standing) | ✓ triggers | — | — | ✓ trigger datasets (test_integration) |
| toolmd | ✓ toolmd | — | — | — |
| any client + agentlog v2 | ✓ anyapi | ✓ test_any_module | ✓ (turn/chunk writes) | ✓ test_integration |
| llm@v1 adapters | — | ✓ test_llm_module | ✓ (anthropic wire) | — |
| toolcaller@v1 loop | — | ✓ test_toolcaller | ✓ full conversation | — |
| history@v1 / rollup | — | ✓ test_history_module, test_rollup_program | — | ✓ chunk-level (test_integration) |
| recall@v1 + eval | — | ✓ test_recall_module | — | ✓ test_recall_integration, test_recall_eval |
| memory@v1 / dedup | — | ✓ test_memory_module | — | ✓ test_memory_integration |
| autorecall@v1 injection + ROI | — | ✓ test_autorecall_module | — | — |
| extraction@v1 / linkgen@v1 | — | ✓ test_cognition_programs | — | — |
| decay/reflection/evolution (gated OFF) | — | ✓ test_gated_mechanisms | — | — |
| skills content | — | ✓ test_skills | — | — |
| runner / serve composition | ✓ (broker/runner wiring) | — | ✓ run + error paths | — |

## Standing rules

- Every new mechanism lands its tests in the same commit (repo rule).
- LLM discipline is asserted in code-level tests (allow-lists, caps,
  vocabularies) — never by trusting prompts.
- New effects: register in the runtime broker, document in
  docs/effects/, and add a classification test (read/mutate + cap) — the
  catalog is data.
- Run the integration suite against a live server before any release-ish
  moment: `ANYBAO_TEST_SERVER=... uv run pytest -m integration`.
- The golden recall eval is the retrieval-quality tripwire; ROI metrics
  (`roi.injection_stats` / `roi.extraction_stats`) are the standing
  production judges — review at every metrics checkpoint.
