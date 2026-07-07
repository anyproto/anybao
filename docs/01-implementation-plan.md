# anybao — high-level implementation plan

Date: 2026-07-07. Inputs: docs/00-plan.md (analysis) + ADR-001..007
(all accepted). Working rules stand: one topic = one commit; no code
ahead of its accepted ADR; each milestone has explicit exit criteria
reviewed before the next starts.

## Milestone map

```
M0 spike ──► M1 runtime core ──► M2 harness core ──► M4 parity port ──► cutover
                       │                                   ▲
                       └────► M3 any-side upstream ────────┘
                                                    M5 memory & cognition (needs M3+M4)
                                                    M6 hardening & extras (post-cutover)
```

## M0 — Spike (de-risk the hard integration first)

Scope: `python.wasm` vendored via nix; wasmtime-py embedding;
`host.effect` bridge; persistent guest namespace across cells; minimal
broker (record-only) + minimal trace (header/effect/cell records);
loop skeleton driven through FakeExecutor for unit tests and
WasiEngine for the real path; **one golden replay test** seeded from a
converted real-conversation trace.

Exit criteria (all offline, CI green, no server, no API key):
1. A cell executes in the guest; its effects cross the boundary and
   record in trace v2 shape.
2. Strict replay of the golden trace passes; a mutated trace produces
   a divergence error (the detector demonstrably works).
3. Fuel exhaustion, epoch timeout, and memory-cap `MemoryError` each
   demonstrated by a test.
4. Numbers in hand: guest boot ms, per-cell overhead, per-crossing
   marshaling cost — the facts the executor defaults get tuned from.

## M1 — Runtime core (`anyrt` complete; ADR-001..004)

- Trace v2 full: normalizers + redaction, blob spill, strict/mock
  replay modes, `traceDiff`/`callTrace` views.
- Effect boundary full: `@effect` registry, broker pipeline
  (cap-check bookkeeping ON with permissive profile, denials recorded),
  `EffectError`, `*_many` fan-out (input-order records).
- Executor full: kernel semantics (persist/reset/last-value), `values`
  / `effects` kernel API, `interrupt()`, per-cell metrics records.
- Namespace: curated builtins, proxied datetime/random/time/env,
  shim globals (`now()/rand()/env()`, effect-backed `uuid4`), `print`.
- Module loading: `use()`, resolution order, probe-validated cache,
  frame-chain attenuation bookkeeping, self-contained resolve records.
- Test story: pure unit tests + golden fixtures; no network anywhere.

## M2 — Harness core (`anybao` app layer; ADR-005 + config)

- `anyclient` — typed `any` HTTP client (boring, complete enough for
  the loop; grows with M4).
- `llm` effect: anthropic + openai-compat + fenced adapters; tier
  resolution via the config effect; `complete_many`.
- Config effect (ADR-006 §3): derived object, `localValue ?? value ??
  default`, secret enforcement; CLI/env bootstrap for first keys.
- `loop.py` + `digest.py`: turn cycle, ceilings→wrap-up, mailbox
  (inject/break), token-budgeted digests, orientation summaries
  (classify tier), teaching hints, progress bubbles.
- Verification: golden conversation replays in CI (whole-loop
  determinism); manual end-to-end against a dev `any` server.

## M3 — `any`-side upstream pass (parallel with M2 once M1 fixes contracts)

- `internal/agentlog` v2: server-assigned seq, extended stopReason +
  `interrupted`, llm scalars (cache/cost/fuel/cells), `traceRef`,
  chunk `level` + per-level range validation.
- Agent-data index chunker, scope `history` (turns + chunks with
  level/period metadata).
- Drift-flow bootstrap: vendor swagger.json pin + coverage manifest
  skeleton + `make api-drift` detector (the mini-skill comes with M4's
  helper work).
- Deferred upstream items tracked, not built: backlinks surface (§4c),
  account-scope record fields (config), trigger server handler.

## M4 — Functional parity port (fresh shapes, no bridges)

- Helper surface: anyHelper → Python facades, THROUGH the
  workaround/skill audit (provenance tags, no test → no port); write
  the **helper style guide** first — it's the porting rubric.
- Tool-docs pipeline: ONE splitter (Python), `sync.py` (hash-gated,
  fresh chat object per ADR-006 §0), skills written fresh.
- History: turns/chunks v2 writer, hierarchical rollup trigger,
  token-budgeted boot-window renderer.
- Trigger subsystem v1: owner/arming semantics, run records + metrics
  rollups, limits + circuit breaker, monitoring list API; **watcher =
  trigger #1** (chat event → conversation runner, cursor + dedup,
  mid-run messages → mailbox).
- Minimal trace viewer (agent-side views complete; human-side = enough
  rendering to debug — full UI later).
- Gate: side-by-side vs bobrik-watch on a test space; cutover
  checklist (feature walk of BOBRIK.md behaviors); then old
  bobrik-watch retires.

## M5 — Memory & background cognition (ADR-007; needs M3 chunker + M4 triggers)

- Recall tool: `search` (agent+history+basic) / `by_period` /
  `neighbors` (forward props now; backlinks when upstream lands) /
  recursive drill-down.
- Auto-recall injection (tool-result framing, both scopes, deep-history
  guard, thresholds).
- Write path: `addMemory` + dedup judge (classify); background
  extraction trigger (stable-fact shapes, provenance, capped
  confidence); hourly link-gen sweep; accessCount bump.
- **Golden recall eval** seeded from the bao export — in CI, red on
  regression; live ROI metrics wired (injected-but-unreferenced,
  extracted-but-unrecalled).
- Evolution/reflection/decay: mechanism present, OFF, each behind an
  eval.

## M6 — Hardening & extras (post-cutover, order by appetite)

Capability teeth (grant prompts via chat/UI-command, trust tiers,
attestation); overlays v1 (aliases + manifest + frozen versions);
orientation-summary and ceiling tuning from metrics; retrospective
replay tooling ("re-run turn 3 of yesterday with mocks"); trace viewer
proper; wasm-snapshot resumable kernels (parked).

## Cross-cutting

- **Verification**: every milestone lands golden/replay tests in the
  same commit stream as code (verify skill on nontrivial slices).
- **Metrics checkpoints**: after M2 and M4, review the recorded
  numbers (boot cost, fuel/mem distributions, digest sizes) and tune
  defaults — decisions from data, per ADR-003/005.
- **Docs discipline**: implementation divergence from an ADR = amend
  the ADR in the same change (the 00-plan.md rule, inherited).
