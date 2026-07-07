# M3 — any-side upstream pass — ACCEPTED 2026-07-08

Branch `feat/agentlog-v2` in ~/any/any. Implements the ADR-006 data
contracts + the history index chunker server-side. NOT pushed/PR'd.

## Done

**agentlog v2** (6 commits) — internal/agentlog + api/agent.go +
handlers_agentlog.go + regen swagger:
- debugRef → traceRef (bump agent_turns-v2).
- stopReason CLOSED enum {done|wrapup|break_soft|break_hard|length|
  error}; interrupted bool; llm scalars costUsd/fuelUsed/cells.
- Server-assigned seq: omit `seq` → server allocates max+1 (-seq
  limit-1 query) with Upsert-collision CAS + bounded retry; client seq
  still controllable. Deletes the client probe/retry dance.
- Chunk hierarchical `level` ≥1 (compound index [level,seq], default 1,
  bump agent_chunks-v2).
- Turns stay written-once/immutable — append-only untouched (v2 fields
  are set at creation).

**History index chunker** (1 commit) — internal/agentlog/chunker.go +
index scope + registry:
- ScopeHistory = "history"; TurnChunker + ChunkChunker on agent_log
  type (gated like chat). Turns index userText+replies (Title on
  userText); chunks index summary. Wired into NewIndexRegistry.
- Makes deep history semantically reachable — search over
  [agent, history, basic]. docs/13 updated.

All agentlog + index + indexer + full server suite green.

## Deferred to M4 (sequencing call)

**Drift-flow bootstrap** (swagger pin + coverage manifest + make
api-drift): deferred to M4. Rationale — the coverage manifest maps
any endpoints → anybao helper methods, but the helper is written in M4.
There is nothing to drift-check until the helper exists, and the plan
already pairs the drift mini-skill with M4's helper work. Doing the
bootstrap now would produce an empty manifest; it's naturally the first
step of the M4 helper port instead.

## Also tracked-not-built (deferred upstream, per plan)
backlinks read surface (§4c), account-scope record fields (config),
trigger server handler.
