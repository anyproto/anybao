# ADR-001: Trace format v2

Status: **Proposed** (awaiting review)
Date: 2026-07-06

## Context

The trace is the load-bearing artifact of the whole design: it is the
replay oracle (deterministic evaluation), the mock source, the debug
record, the trigger run log, and the fixture format for golden tests.

The v1 format (`anytype-agent-runtime`) is
`map[effectName][serializedArgsJSON][]serializedOutputJSON` with three
parallel copies (lastTrace / traceDiff / callTrace). Known defects:

- **Order is lost.** A map can't say what happened in which sequence, so
  replay can't detect divergence, and retrospection can't reconstruct a
  run. The three parallel maps exist *because* the primary structure
  can't express views.
- **Keys are exact serialized strings.** Mock matching breaks on
  serialization drift (Go header ordering, GET's dropped `method` field
  — documented brittleness in the old integration tests).
- **Outputs are positional pops** per key — replay correctness depends
  on call order that the format itself doesn't record.
- **LLM calls are not traced**, so conversations can't be replayed.
- **No metadata**: no timing, no read/mutate classification (v1 guessed
  mutation by name prefix), no cell attribution, no schema version.
- **Secrets can leak into traces** (raw fetch headers).

## Decision

### 1. A trace is an append-only ordered log, not a map

One record per effect call, in execution order. All v1 map views
(by-effect, by-key, traceDiff, callTrace) become *derived* views over
the log. Serialization: **JSONL** — one record per line, append-friendly,
spill-friendly, diffable. Line 1 is a header record.

### 2. Record shape

```jsonc
// header (line 1)
{"kind": "header", "schema": 2, "run": {"id": "...", "program": "name@vN",
 "programHash": "...", "args": {...}, "instance": "...", "startedAt": ...}}

// effect record (one per call)
{"kind": "effect",
 "seq": 17,                     // monotonic per run
 "effect": "fetch",             // registered effect name
 "cell": "toolu_abc",           // attribution: which cell/turn issued it
 "input": {...},                // canonicalized structured input
 "key": "sha256:...",           // hash of canonical input (mock match key)
 "output": ...,                 // structured result (or absent if error)
 "error": {"type": "...", "message": "..."},   // effect failure is data
 "meta": {"t": 1234.5, "durMs": 88, "mocked": false,
          "class": "read"|"mutate", "usage": {...}}}   // usage: llm tokens etc.
```

### 3. Canonical inputs, per-effect normalizers, redaction

The mock-match `key` is a hash of the **canonical form** of the input:
JSON with sorted object keys, no insignificant whitespace, canonical
number forms. Each effect may register a **normalizer** that produces
the canonical input from raw args (e.g. fetch: uppercase method always
present, headers sorted + lowercased, default values made explicit).
This kills the v1 exact-string brittleness class.

Effects declare **sensitive fields** (e.g. `Authorization`); the
normalizer replaces them with a stable placeholder *before* the record
is written. Replay never needs the real credential (mocked calls don't
execute; live re-runs re-resolve credentials inside the effect boundary
per the config-effect design). Secrets structurally cannot enter traces.

### 4. Everything nondeterministic is a record

`llm.chat` (full request incl. messages, full response), `fetch`,
`time.now`, `random`, `env.get`, `sleep`, `chat.send`, `console.log`
(structured value — the model-facing output channel), and
`module.resolve` (name@version → spaceId + contentHash, so the loaded
code version is part of the recorded run). This is the isolation
principle made concrete: if it isn't in the trace, it didn't happen.

### 5. Two replay modes

- **`replay` (strict)** — the default for golden tests and deterministic
  evaluation. The next effect call must match the next unconsumed record
  (same effect + key, in sequence). Mismatch ⇒ **divergence error**
  carrying both expected and actual — nondeterminism and behavior drift
  become loud test failures, not silent wrong answers.
- **`mock` (loose)** — tracer-style debugging of *edited* code against
  old effects: match by (effect, key) anywhere in the log, FIFO per key
  (v1 semantics, kept for its proven use case). Unmatched calls execute
  live (or fail, per option) and are flagged `mocked: false` — the
  **traceDiff** view is exactly these records.

### 6. Classification is declared, not guessed

`class: read | mutate` comes from the `@effect` registration (ADR-002),
recorded per call. Replaces v1's name-prefix mutation heuristic.

### 7. Large outputs spill out-of-line

An output larger than a threshold (config, default ~64 KB) is replaced
by `{"__blob": "sha256:...", "bytes": N}` and the bytes stored next to
the trace (sidecar file / file attachment when the trace lives in a
space object). Replay resolves refs transparently. Keeps JSONL lines
bounded without truncating anything.

## Consequences

- Golden replay tests: record once, assert forever, no server / no keys.
- Divergence detection makes the determinism invariant *testable*.
- Debug records and trigger run logs are this same format — one
  inspect/replay toolchain everywhere.
- Retrospection = filtering a JSONL log (by cell, effect, class, time).
- Cost accounting falls out of `meta.usage` aggregation.
- The tracer-heritage mock debugging keeps working, minus the key
  brittleness.

## Open questions (reviewer input wanted)

1. **LLM input growth.** Recording the full `messages` array per call is
   O(turns²) bytes per conversation trace. Options: accept (spill rule
   bounds line size; traces are per-run), or prefix-compress (an
   `llm.chat` input may reference the previous call's messages by seq +
   delta). Lean: accept for v2.0, revisit with data.
2. **Spill threshold default** (64 KB?) and whether `console.log` values
   should spill at a lower threshold than effect outputs.
3. **Should `mock` mode fail or execute live on unmatched calls by
   default?** Old tracer executed live (exploration probes need it);
   golden tests want fail. Proposal: mode option, default = fail in
   tests, live in interactive debugging.
