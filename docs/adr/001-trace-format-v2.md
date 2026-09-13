 # ADR-001: Trace format v2

Status: **Accepted** (2026-07-07)
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

**Written at commit time, not at run end (revised 2026-07-08).** The
writer streams: the header lands when the run starts, every record
appends (with flush) as it is committed, and blob spills append to the
sidecar as they happen (spill order, not sorted — consumers key by
hash). So an in-flight run is tail-able and a crashed run leaves a
partial trace — exactly the run you most want to read. Records stay
buffered in memory too; if a stream write ever fails the writer
degrades to buffered mode and the end-of-run `dump()` (a no-op on a
healthy stream) rewrites the whole file. Bytes are identical either
way.

### 2. Record shape

```jsonc
// header (line 1)
{"kind": "header", "schema": 2, "run": {"id": "...", "program": "name@vN",
 "programHash": "...", "args": {...}, "instance": "...", "startedAt": ...,
 "seed": "<64 hex>"}}   // startedAt + seed = the WASI floor (ADR-002 §4):
                        // filled at run start, replayed from the header

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

**`seq` is the record's stable ADDRESS, not a replay mechanism**
(amendment 2026-07-07): replay consumes the log as a sequence
(cursor = list position; mock index = key map) and never reads `seq`.
Its consumers are everything that handles records *individually* or
*extracted from the intact log*: `EffectError.seq` (an exception points
at its record), kernel lookups (`effects.get(seq)`, ADR-003), filtered
views (`effects.of(cell)` — file order is gone, seq keeps subsets
self-describing and gap-checkable), cross-artifact refs (turn
`traceRef` + seq anchors, viewer "#turn_N" — and the guest's own
`run=` reads, ADR-003 §4: a `traceRef` is dereferenceable from inside
a later run), and quotable divergence reports ("expected record #23").

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
`time.now`, `env.get`, `sleep`, `chat.send`, `console.log`
(structured value — the model-facing output channel), and
`module.resolve` (name@version → spaceId + contentHash, so the loaded
code version is part of the recorded run). This is the isolation
principle made concrete: if it isn't in the trace, it didn't happen.
Randomness is one record, not one per draw (amendment 2026-09-04,
ADR-002 §4): the header's `seed` derives every random byte the guest
sees — `rand()`, the stdlib `random`, `os.urandom` behind `uuid`
and `secrets` — through the WASI floor; `uuid4()` the global stays
its own recorded effect (an identity worth a visible line).

### 4b. Cell records (amendment 2026-07-07)

A third record kind marks each cell's lifecycle end — cells are not
effects, and the per-cell facts (ADR-003 metrics) need a home:

```jsonc
{"kind": "cell", "seq": 23, "cell": "toolu_abc",
 "ok": true, "error": null, "interrupted": false,
 "metrics": {"fuel_used": 184223, "mem_pages": 512, "duration_ms": 240,
             "value_store": {"entries": 7, "bytes": 91234}}}
```

Written by the executor at cell end — one per program run, since the
host runs a single cell (the program `main`, ADR-003 §2). The model's
per-turn cells run as nested `subcell`s inside it and get their trace
granularity from `cell`-named spans (§4c), not a cell record each.
Replay treats the cell record as a checkpoint (sequence-checked in
strict mode like any record). Turn boundaries need no marker — they ARE
the `llm.chat` records.

### 4c. Span records (amendment 2026-07-08)

Composite guest facades — space-resident tool programs like a
`linear.createTask` composed over `any.modify` + `http.*` — read as
primitive noise at effect granularity. A **span** groups the effect
records of one facade call under a single input/output pair, so views
can render the call like a host effect: a collapsed one-liner by
default, the inner records on demand.

```jsonc
{"kind": "span", "seq": 24, "phase": "begin", "span": "s1", "parent": null,
 "name": "linear.createTask", "cell": "toolu_abc",
 "input": {...}, "key": "sha256:..."}            // canonical-key rule of §3

{"kind": "span", "seq": 31, "phase": "end", "span": "s1",
 "name": "linear.createTask", "cell": "toolu_abc",
 "ok": true, "output": {...}, "error": null,
 "meta": {"durMs": 88, "effects": 3, "mutations": 1}}
```

Effect records between the pair carry `"span": "<id>"` (the innermost
open span). The field is **absent** outside spans, so span-free traces
stay byte-identical to pre-amendment schema 2. Span ids are
broker-assigned per run (`s1`, `s2`, …) — execution order makes them
deterministic; `parent` chains nested spans.

**A span is a view-level collapse, never a recording-level one.** The
inner effect records stay canonical: they are what replay consumes,
what capability checks anchored to, where redaction applied. A span is
guest-*declared* narrative — `kind: "span"` stays distinct from
`kind: "effect"` precisely so a tool program cannot fake boundary
truth; views may render the two alike, the log never confuses them.

- **Strict replay**: span records are checkpoints like cell records
  (§4b) — begin matched on (name, key), end on (name, ok); output and
  meta are not matched (the output is the guest's deterministic
  recomputation, meta legitimately varies). Loose mock mode ignores
  them (the mock index holds effect records only); reruns write their
  own.
- **No normalizer, no redaction**: span input/output come from guest
  code, and guest code never holds secrets (§3 — credentials resolve
  inside the boundary), so there is nothing to redact. Non-JSON guest
  values degrade to `repr()` at the facade. The spill rule (§7)
  applies.
- **Dangling spans**: a trapped cell (fuel/epoch/memory) can skip the
  guest-side `finally`, so the broker force-closes open spans at cell
  end (`ok: false`, `error.type: "unclosed_span"`) before the cell
  record — the log stays well-nested by construction. Synthesized ends
  replay-match like any record; a rerun that traps differently
  diverges loudly, same doctrine as cell `ok` matching.
- **Views**: the human viewer renders spans collapsed by default —
  `#24 linear.createTask [span, 3 effects] -> {...}`, mutation-marked
  when the span contains a mutate — with inner records behind an
  expand flag. The `span` stamp passes through `effects.of` so agent-
  facing views can group the same way.

Out of scope, deliberately: span-level mocking (serving a whole
composite from its recorded output without executing the guest code) —
that changes execution semantics and gets its own decision if ever
wanted. The guest-side `span` facade shape is ADR-003 §4b.

### 4d. Span method-kind + the immediate-children view (amendment 2026-07-18)

Composite **tool-method** spans (the `any@v1` client, the ADR-008
tools) are what the agent actually calls all turn long; two additions
let them read as first-class operations — at bobrik's `[kind]`-tagged
granularity — without weakening the narrative/boundary split.

**`meta.kind` on the span-end record.** A facade may declare its
method's kind — `getter | mutator | setup | program`, the authored
`### name(sig) [kind]` vocabulary (parsed by toolmd, stored in
`program_methods` since folder-tool authoring; this is its first
runtime consumer). It rides the end record's `meta`:

```jsonc
{"kind": "span", "seq": 31, "phase": "end", "span": "s1",
 "name": "any.create_object", "cell": "toolu_abc",
 "ok": true, "output": {"objectId": "..."},
 "meta": {"durMs": 88, "effects": 3, "mutations": 1, "kind": "mutator"}}
```

`meta.kind` is **narrative** — the author's declared intent, like
`name`, and like the whole span it carries zero authority. It is NOT
the mutation oracle: `meta.mutations` (the count of inner
`class:"mutate"` effect records) stays the boundary-backed truth (§6,
ADR-002 — class derives from (method, url) at the boundary, guest code
cannot fake it). A declared `getter` whose span contains a mutate, or a
`mutator` with `mutations:0`, is a legible inconsistency a view or lint
may flag; the log never lets the label override the boundary fact.
`meta.kind` is absent when undeclared, so span-free and kind-free
traces stay byte-identical to the pre-amendment schema.

**The per-cell effects view surfaces immediate children, not just bare
effects.** `trace.effects_of` (ADR-003 §4) returned only `kind:"effect"`
records under the *exact* queried span — so an effect nested in a child
span vanished from the view and the span itself never appeared.
Consequence: wrapping a facade in a span *removed* it from the digest
instead of collapsing it to a line. The view now returns a cell's (or
span's) **immediate children**: bare effect records whose `span` is the
queried scope, **plus** child span-end records whose `parent` is that
scope, in `seq` order. A composite call renders as one line
(`any.create_object {…} -> {…}`, kind- and mutation-marked from
`meta`), its inner effects reachable by drilling into the child span.
This is a derived view only (§1) — strict/loose replay still consume
the intact log unchanged.

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

**How matching works over a log.** The log is the source of truth;
matching structures are derived from it at replay setup. Loose mode
folds the log in one O(n) pass into exactly the v1 shape —
`effect → key → FIFO output queue`, queue order = log order — and pops
per call, byte-for-byte v1 pop semantics (v1's ordering was an implicit
map-insertion artifact; here it's a provable property of the log).
Strict mode uses no index at all: a **cursor** — the next call must
match `log[i]`, then advance. A map can answer "seen this input?" but
cannot detect reordered, extra, or missing calls; the cursor detects
all three, which is what makes determinism testable.

### 6. Classification is declared, not guessed

`class: read | mutate` comes from the `@effect` registration (ADR-002),
recorded per call. Replaces v1's name-prefix mutation heuristic.

### 7. Large outputs spill out-of-line

An output larger than a threshold (config, default ~64 KB) is replaced
by `{"__blob": "sha256:...", "bytes": N}` and the text stored next to
the trace (the store's text blobs, ADR-023 §4). Replay resolves refs
transparently. Keeps records bounded without truncating anything.

**Two ref shapes (amendment 2026-09-04, ADR-026 §2).** A ref carrying
`mime` — `{"__blob", "bytes", "mime"}` — is raw bytes at
`<traces_dir>/blobs/<hex>` (hash over the bytes); a ref without `mime` is the canonical JSON text of a
spilled value, in the store's text-blob place (ADR-023 §4). A text
spill too large for one store request is written as a raw blob
(`mime: application/json`). A failed blob write records the ref and
warns; it never fails the run.

### 8. Storage is one trait, one store (amendment 2026-08-28, revised 2026-09-13)

Where a run persists is a **storage** decision, not a format one.
`anyrt::tracestore::TraceStore` is the single seam: `open_sink(run)`
(the streaming append of §1), `write_run` (the buffered fallback),
`list` (newest first, with the store's modified time — the run's only
wall-clock), `load` (the intact log), `blobs` (§7's spilled values),
plus derived defaults `load_resolved` / `header` / `load_in_flight`.
Every writer (`TraceWriter`) and every reader — `trace ls/show/
follow/stats`, serve, the guest's `trace.*` syscalls — takes a
`&dyn TraceStore`; nothing else opens a run. The one implementation is
`AnyTraceStore` — the any server's local store (ADR-023); raw blobs
sit in a directory beside it (`paths.traces`, ADR-026 §1). There is
no file layout: a trace is a set of documents, and the record contract
(§1–§7) is what those documents carry.

Run ids are the store's keys and the guest's handles (`traceRef`,
`lastRunRef`): `run_<[A-Za-z0-9_-]+>`, validated at the syscall
boundary so a `run=` argument can name nothing but a run.

## Consequences

- Golden replay tests: record once, assert forever, no server / no keys.
- Divergence detection makes the determinism invariant *testable*.
- Debug records and trigger run logs are this same format — one
  inspect/replay toolchain everywhere.
- Retrospection = filtering a JSONL log (by cell, effect, class, time).
- Cost accounting falls out of `meta.usage` aggregation.
- The tracer-heritage mock debugging keeps working, minus the key
  brittleness.

## Resolved questions (review 2026-07-07)

1. **LLM input growth** — record the full `messages` array per call;
   accept O(turns²) for v2.0 (the spill rule bounds line size; traces
   are per-run). Revisit with real data; prefix-compression (reference
   prior call's messages by seq + delta) is the known escape hatch.
2. **Spill threshold** — default 64 KB, one config knob, same threshold
   for effect outputs and `console.log` values. Revisit only if digest
   noise shows console values need a lower bar.
3. **`mock` mode on unmatched calls** — a mode option: **fail** is the
   default under tests, **execute live** is the default in interactive
   debugging (exploration probes need live execution; golden tests need
   the failure).
