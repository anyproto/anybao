# M4 — functional-parity port

Where anybao becomes a real agent and old bobrik-watch retires. Fresh
shapes, no bridges (no-backcompat). Checklist:

- [x] Helper style guide (docs/helper-style.md) — the porting rubric:
      naming, arg shapes, space handling, error normalization, catalog
      caching, nested property shape. Feeds both the port and the
      drift skill.
- [x] anyclient expansion — full any surface the helper needs (spaces,
      objects, chat, editor, query, agent turns/chunks, search, props).
- [x] Helper facades (Python) — objects/chat/editor/ui/schema/aggregate/search done (26 endpoints mapped); memory→M5, files/collab→M6, programs/skills→tool-docs pipeline over anyclient, THROUGH the workaround/
      skill audit (provenance tags, no test → no port).
- [x] Drift-flow bootstrap — vendor any swagger.json + coverage
      manifest + make api-drift (deferred from M3; first done here).
- [x] Deploy tool + splitter + resolver — toolmd.py (ONE splitter),
      deploy.py (Deployer: hash-gated program deploy to any space),
      modules.py (AnyModuleResolver: prod use() resolution). Full chain
      deploy→resolve→use()-in-guest validated LIVE.
- [x] Skills written fresh incl. core skill — skills/ (_core _soul _any
      _memory _space_context _meta_skill), skills.py (compose_system +
      SkillDeployer, hash-gated markdown objects). No legacy-JS residue
      (test-guarded).
- [x] History writer — build_turn + token-budgeted hierarchical
      boot-window renderer + History wrapper; boot window WIRED into
      Runner.run_conversation (raw_tail also feeds the ADR-007 §5
      deep-history guard). Hierarchical rollup = programs/rollup@v1
      (guest cron job; L2+ from child summaries only).
- [x] Trigger subsystem v1 — scheduler core + TriggerRuntime + Watcher +
      TriggerStore + SSE run feed (trigger_events) + program-execution
      adapter (runner) + HTTP control API (trigger_control). Validated
      LIVE (integration suite).
- [x] Primitive `any.*` data effects (programs/any@v1.py, 13 effects,
      docs/effects/syscalls.md) — the guest path to space data; what the
      background programs compose over.
- [x] Minimal trace viewer (agent-side views done; human = enough to
      debug). Traces DEVICE-LOCAL (FileSidecarStore = production);
      traceRef = local run id; optional promote-to-synced escape hatch
      (AnyFileStore).
- [x] Real transports + one-real-call llm/any fixture seeding —
      transports were real since M2/M3; `anybao llm-seed` CLI records
      the per-provider wire fixture (run once per provider with a key;
      consumer test skips until seeded).
- [x] `anybao serve` — the runnable composition (space/chat ensure,
      deploy, config bootstrap, Runner+Watcher+TriggerRuntime+control
      API, reconnecting drop-snapshot chat feed).
- [ ] **Gate (user-run)**: side-by-side vs bobrik-watch on a test
      space — docs/cutover-checklist.md is the walk. Needs an LLM key +
      a day of traffic; second metrics checkpoint rides it
      (metrics.scan over live traces → tune ceilings/budgets).
      Then old bobrik-watch retires.
