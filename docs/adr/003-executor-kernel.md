# ADR-003: Executor & kernel API

Status: **Proposed** (awaiting review)
Date: 2026-07-07
Builds on: ADR-001 (trace v2), ADR-002 (effect boundary) — both accepted

## Context

The executor runs LLM-written cells against a persistent kernel; the
kernel API is what cells see. ADR-002 fixed the boundary and namespace
contents; this ADR fixes the **engine decision** (deferred from ADR-002
review), the **executor interface**, **kernel semantics** (persistence,
value store, last-value), and **limits/interruption**. Out of scope:
module resolution grammar (ADR-004), digest rendering and loop policy
(ADR-005) — the executor returns raw material; the loop renders it.

## Decision

### 1. Engines: two, behind one interface; wasi is the v2.0 production engine

```
Executor (interface)
├── NativeEngine  — in-process exec() into a curated namespace.
│                   Dev/test/spike engine: fast, pdb-able, zero setup.
│                   Honest-code enforcement only (ADR-002 §5). NOT the
│                   production engine.
└── WasiEngine    — wasmtime-py host; guest = CPython compiled to
                    wasm32-wasi (official CPython target). The v2.0
                    PRODUCTION engine, required for GA / parity cutover.
                    Effects are host functions (the ADR-002 broker,
                    verbatim); fuel + epoch deadlines + memory caps are
                    engine-native.
```

Rationale for wasi-in-v2.0 (not deferred to a "security milestone"):
the isolation principle is core doctrine, not a hardening extra — and
the in-process watchdog is genuinely weak (CPython cannot interrupt a
cell stuck in a C-level loop or blocking call; async exceptions land
only between bytecodes). wasmtime's **fuel metering** (deterministic
instruction budget), **epoch interruption** (hard wall-clock deadline
that stops even C loops), and **per-store memory limits** solve the
three limits gaps properly and are all exposed through wasmtime-py.
Keeping NativeEngine permanently (not as a migration leftover) buys
fast unit tests of loop logic and painless debugging; golden replay
tests run on BOTH engines in CI, which continuously proves the
program-facing contract identical — the portability seam (ADR-002
rationale) stays honest by test, not by promise.

Costs accepted: vendoring a `python.wasm` + pure-Python stdlib bundle
(nix pins it like any other artifact); guest↔host marshaling is
JSON-serializable data only (already the trace constraint); guest
tracebacks arrive as strings; C extensions unavailable in cells (the
allowlist is pure-Python by design — ADR-002 §4).

### 2. Executor interface

```python
class Executor(Protocol):
    def run_cell(self, code: str, *, cell_id: str,
                 timeout_s: float | None = None) -> CellResult: ...
    def interrupt(self) -> None      # hard break (loop control)
    def reset(self) -> None          # drop the kernel namespace
    def close(self) -> None

@dataclass
class CellResult:
    cell_id: str
    ok: bool
    error: CellError | None          # {type, message, traceback_str}
    prints: list[ValueRef]           # print() records, in order
    last_value: ValueRef | None      # last-expression value (§4)
    duration_ms: int
    interrupted: bool                # True on break/timeout/fuel-exhausted
```

One executor instance per conversation. `run_cell` is synchronous from
the loop's view; `interrupt()` is callable from another thread (the
mailbox handler) and maps to epoch-bump (wasi) / best-effort async
exception (native). Effect records don't appear in CellResult — they
are already in the trace (ADR-001), attributed via `meta.cell =
cell_id`; the loop reads trace views to build the Side Effects digest.

### 3. Kernel semantics: the persistent namespace

- Top-level assignments, `def`/`class`, and imports **persist across
  cells within one conversation** (the v1 persistent-kernel behavior,
  IPython-like). Fresh namespace per conversation; `reset()` drops it
  mid-conversation (exposed to the agent as a tool, as today).
- **Nothing survives process restart.** State is re-derivable from the
  space + traces (stateless-agent doctrine). Wasm memory snapshots
  (resumable kernels) stay parked.
- **Last-expression value**: if the cell's final statement is an
  expression, its value is the cell's `last_value` (IPython convention;
  matches v1's "Last value" digest section).
- Cells are **single-threaded, synchronous** Python. No `async`/threads
  in guest code; concurrency is the host's job (`*_many`, ADR-002).

### 4. Value store: `values` and `effects`

The kernel keeps every cell's printed values and last value, addressable
after the fact — the successor of v1's `_valueStore`/`logs.get` and
`_toolEffectsStore`/`toolEffects.get`, with the effects half now backed
by the trace instead of a parallel store:

```python
values.get(cell_id, i)        # i-th printed value of that cell
values.get(cell_id, "last")   # its last-expression value
values.list()                 # index: {cell_id: {n_prints, has_last, sizes}}
effects.of(cell_id)           # that cell's effect records (trace view):
                              # [{seq, effect, class, key, meta, ...}]
effects.get(seq)              # one full record incl. output (blob-resolved)
```

`ValueRef` in CellResult carries `(cell_id, i, size, schema_digest)` —
the digest layer (ADR-005) decides inline-vs-stub per its budget; the
full value stays in the kernel. Values larger than the ADR-001 spill
threshold are held as blob refs; `values.get` re-hydrates transparently.
Store lives **kernel-side** (guest memory under wasi) with an LRU cap
(config; default generous) — evicted values fall back to their trace
blob if one was written, else raise a clear "evicted" error.

### 5. Limits & interruption (per cell)

| Limit           | NativeEngine              | WasiEngine                  |
|-----------------|---------------------------|-----------------------------|
| wall timeout    | watchdog + async-exc (soft)| epoch deadline (hard)      |
| instruction cap | —                         | fuel (deterministic)        |
| memory cap      | process-RSS watch (soft)  | store memory limit (hard)   |
| hard break      | best-effort               | epoch bump (always lands)   |

Defaults config-injected (ADR-002 policy rule); every limit hit is
recorded (a `cell.interrupted` marker in the trace via the cell's
terminal record) and surfaces as `CellResult.interrupted` + `error`.

## Consequences

- The isolation principle is physically enforced in production from
  v2.0 GA; "honest-code" mode exists only as a labeled dev engine.
- Two engines in CI = the identical-contract claim is continuously
  tested; drift between them is a red build, not a latent surprise.
- The spike can start on NativeEngine immediately (no wasm plumbing on
  the critical path) while WasiEngine lands before cutover.
- Value recovery idioms carry over from v1 with Pythonic names
  (`values.get`, `effects.of`), and the effects half stops being a
  second store — one source of truth (the trace).

## Open questions (reviewer input wanted)

1. **Is WasiEngine required for v2.0 GA?** Lean: yes (production =
   wasi; native = dev/test only). The alternative — ship GA on native,
   wasi later — restores the old "security milestone" staging.
2. **Spike on NativeEngine?** Lean: yes — spike validates loop + trace
   + replay ergonomics, not the cage; wasm plumbing would delay signal.
3. **`reset()` exposed to the agent** (as v1's `js.reset`) or
   loop-internal only? Lean: exposed — the agent knows when its
   namespace is poisoned.
4. **Value-store LRU default cap** — number? Lean: size-based (e.g.
   256 MB native / guest-memory-bounded under wasi), not count-based.
