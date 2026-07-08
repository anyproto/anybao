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
- [ ] Deploy tool (was sync.py) — GENERIC: deploy <src-dir> --space
      <target> (hash-gated, splits tool docs, sweeps orphans); reusable
      for any overlay. Agent-code deploy = one invocation to `agent:`.
      ONE splitter (Python). Skills written fresh incl. core skill.
- [~] History writer — build_turn + token-budgeted hierarchical
      boot-window renderer DONE (history.py, 6 pure tests); History
      wrapper (append/read via anyclient). REMAINING: hierarchical rollup
      trigger (turns→L1→L2 summarizer, runs as cron trigger via llm) —
      pairs with the trigger I/O layer.
- [~] Trigger subsystem v1 — scheduler core + TriggerRuntime + Watcher +
      TriggerStore (persistence, validated LIVE; agent_trigger type added
      to any server) DONE (20 offline + trigger integration test).
      REMAINING: SSE event feed (drop-snapshot subscription), program-
      execution adapter (→ executor/loop), HTTP control API.
- [ ] Minimal trace viewer (agent-side views done; human = enough to
      debug). Traces DEVICE-LOCAL (FileSidecarStore = production);
      traceRef = local run id; optional promote-to-synced escape hatch
      (AnyFileStore).
- [ ] Real transports + one-real-call llm/any fixture seeding.

Gate: side-by-side vs bobrik-watch on a test space + cutover checklist
(BOBRIK.md behavior walk) → retire bobrik-watch. Second metrics
checkpoint here (calibrate token counter + digest/ceiling defaults
from live traces).
