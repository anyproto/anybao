# ADR-003: Executor & kernel API

Status: **Accepted** (2026-07-07)
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

### 1. One engine: wasi. The interface stays; a test double, not a second engine

```
Executor (protocol)
├── WasiEngine    — THE engine, spike onward. wasmtime-py host; guest =
│                   CPython compiled to wasm32-wasi (official CPython
│                   target). Effects are host functions (the ADR-002
│                   broker, verbatim); fuel + epoch deadlines + memory
│                   caps are engine-native.
└── FakeExecutor  — a TEST DOUBLE (canned CellResults behind the same
                    protocol) for loop-logic unit tests. Not an engine:
                    it executes nothing.
```

A NativeEngine (in-process exec) was proposed and **rejected in review
(2026-07-07)** — "faster first result" is not a real benefit, and the
two-engine design had hidden costs the wasi-only design deletes:

- **Enforcement gets built once.** The in-process engine needed the
  entire deny-by-default namespace rebuilt by hand; under wasi the
  cage is the substrate. **Correction (review 2026-07-07 — trace
  semantics):** WASI syscalls are NOT the effect layer. The trace line
  stays at semantic effects (ADR-001/002); the guest reaches the world
  through ONE custom host function (`host.effect(name, payload)` → the
  broker). No wasi-http, no sockets, no fs imports are linked into the
  guest — network syscalls are structurally impossible, so syscall
  noise cannot enter traces. WASI proper is reduced to interpreter
  plumbing made **deterministic by construction, untraced**: a virtual
  clock (boot-recorded epoch, deterministic advance) and a fixed
  boot-recorded seed — CPython internals call the clock constantly
  (imports/gc), and those reads are both indistinguishable from user
  calls at the syscall layer and worthless in a trace; a hash-pinned
  read-only stdlib bundle covers module loading. One `kernel.boot`
  header record captures epoch/seed/bundle-hash. **User-visible
  time/random stay ADR-002 tier-2 proxies** installed by the kernel
  boot prelude — `datetime.now()`/`random()` in cell code route
  through the effect host function: REAL wall-clock, one semantic,
  agent-legible record. Cell code must never silently receive the
  virtual clock's fake time.
- **Fast loop tests never needed an engine** — the FakeExecutor double
  covers them.
- **pdb-on-cells contradicts doctrine** — traces are the debugging
  story for guest code; effects (host Python in every design) stay
  pdb-able regardless.
- **Dual-engine "contract proving" was circular** — one engine, no
  drift to prove. The protocol remains the seam for the double and any
  future engine (e.g. a Rust host).

Bonus: the spike now de-risks the riskiest integration (python.wasm +
wasmtime-py + host functions) first instead of last.

Costs accepted: vendoring a `python.wasm` + pure-Python stdlib bundle
(nix pins it like any other artifact); guest↔host marshaling is
JSON-serializable data only (already the trace constraint); guest
tracebacks arrive as strings; C extensions unavailable in cells (the
allowlist is pure-Python by design — ADR-002 §4); a heavier spike,
explicitly accepted.

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

One executor instance per conversation. The host runs ONE `run_cell` —
the program's `main` (a conversation is `toolcaller@v1`); the model's
per-turn cells run inside it through the guest-global `subcell` (§3).
`run_cell` is synchronous from the host's view; `interrupt()` is
callable from another thread (the hard-break watchdog in the Runner)
and maps to epoch-bump — it lands in the guest loop or a wedged subcell
alike. Effect records don't appear in CellResult — they are already in
the trace (ADR-001), attributed via `meta.cell = cell_id`; the guest
loop reads trace views (`trace.effects_of`) to build the Side Effects
digest.

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
- **`subcell(code, cell_id)` — nested cells inside a program.** A
  program that drives model turns (the `toolcaller`) runs each model
  cell through the guest-global `subcell`: same persistent namespace,
  same print/last-value capture, returning the cell's result to the
  driver (a JSON-friendly `{ok, prints, last, error}`, not the host
  CellResult dataclass). Nested execution **restores the caller's
  printer** on return, so a child cell's `print()` output never leaks
  into the driver's own. Per-model-cell trace granularity comes from
  wrapping each `subcell` in a `cell`-named span (§4b): the span groups
  that cell's effects, and the driver reads them back with
  `trace.effects_of(span=…)` for the digest.

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

`values.get`/`effects.of` are guest globals: the toolcaller's in-guest
digest (ADR-005) reads them together with `trace.effects_of` to render
each model cell's result. `ValueRef` in CellResult carries
`(cell_id, i, size, schema_digest)` — the digest decides inline-vs-stub
per its budget; the full value stays in the kernel. Values larger than the ADR-001 spill
threshold are held as blob refs; `values.get` re-hydrates transparently.
Store lives **kernel-side** (guest memory under wasi), **uncapped for
now** (review 2026-07-07): no LRU/eviction until real numbers justify
one — instead, per-cell and per-conversation store metrics are always
recorded (§5) so the decision is made from data. Guest memory itself is
the natural backstop (a runaway store hits the memory limit as a clean
`MemoryError`).

### 4b. `span` — composite facades lift to one record pair (amendment 2026-07-08)

Tool programs compose primitive effects (ADR-002 §1's granular verbs),
so their runs read as primitive noise in every view. The kernel
namespace carries a `span(name)` decorator that wraps a facade
function in an ADR-001 §4c span record pair:

```python
@span("linear.createTask")
def create_task(title, assignee=None): ...   # any.modify + http.* inside
```

Callers and the trace viewer see `linear.createTask {input} ->
{output}` like a host effect; the inner effect records stay recorded
underneath (visible on expand). The decorator is guest-side *shape*
with zero authority (ADR-002's syscall analogy: it is libc, not the
kernel): it emits `span.begin`/`span.end` over the one host channel —
**reserved names** the engine dispatches to broker span methods, never
registry effects, so they cannot be capability-granted and produce
`kind: "span"` records only. Exceptions re-raise after an `ok: false`
end record; non-JSON inputs/outputs degrade to `repr()`.

### 5. Limits, interruption & metrics (per cell)

| Limit           | Mechanism (wasi)                       |
|-----------------|----------------------------------------|
| wall timeout    | epoch deadline (hard, lands in loops)  |
| instruction cap | fuel (deterministic, replayable)       |
| memory cap      | store memory limit (hard, MemoryError) |
| hard break      | epoch bump (always lands)              |

Defaults config-injected (ADR-002 policy rule); every limit hit is
recorded (a `cell.interrupted` marker in the trace via the cell's
terminal record) and surfaces as `CellResult.interrupted` + `error`.

**Metrics are first-class (review 2026-07-07)** — sobek could measure
none of this; wasmtime measures all of it for free, so every cell's
terminal trace record (and thus the debug log) carries:
`fuel_used`, `mem_pages` (linear-memory size after the cell; growth
events from the ResourceLimiter callback give the peak), `duration_ms`,
`value_store` {entries, bytes}, and per-effect timing already in each
record's `meta.durMs`. Capacity decisions (store caps, fuel defaults,
timeout defaults) get made later FROM these numbers, not guessed.

## Consequences

- The isolation principle is physically enforced from the first spike
  cell onward — no honest-code interim in production, no second
  enforcement implementation to build and maintain.
- One engine; loop logic tested via the FakeExecutor double; the
  Executor protocol remains the seam for future engines.
- Runtime behavior is measurable for the first time (fuel/memory/
  timing per cell in the debug log) — capacity tuning becomes
  data-driven.
- Value recovery idioms carry over from v1 with Pythonic names
  (`values.get`, `effects.of`), and the effects half stops being a
  second store — one source of truth (the trace).

## Appendix: how the wasi limits actually work

Root fact: **every guest instruction is wasm compiled by wasmtime** —
CPython's interpreter loop included. No native guest code exists, so
all of it can be instrumented and bounded (the "can't interrupt a C
loop" problem disappears: there is no C on the guest side).

- **Fuel** — Cranelift inserts a per-basic-block counter decrement at
  compile time; `store.set_fuel(n)` sets the budget; zero ⇒ trap at
  exactly that instruction. Counts instructions, not time ⇒
  **deterministic and replayable cutoffs** (same cell + inputs = same
  fuel), and leftover fuel is a free compute-cost metric for `meta`.
  ~few % overhead.
- **Epoch interruption** — one global engine counter; compiled code
  does a load+compare against the store's deadline at function entries
  and **loop back-edges** (nearly free). A ticker thread bumps the
  epoch; deadline passed ⇒ trap at the next back-edge — lands inside
  any guest loop, guaranteed. `interrupt()` = set deadline to now +
  bump. Wall-clock-driven ⇒ non-deterministic — the tool for
  *cancellation*; fuel is the tool for *budgets*.
- **Memory** — the guest lives in one bounds-checked linear memory;
  growth (`memory.grow`, i.e. Python's allocator asking for pages) is
  routed through a host `ResourceLimiter` callback. Deny ⇒ guest
  malloc fails ⇒ ordinary `MemoryError` inside the cell — **cell
  fails, kernel namespace survives** (vs an OS kill destroying the
  conversation state). Dropping the store frees everything.

Nuance: epoch checks run only in guest code — while inside a host
function (an effect), interruption is cooperative. Host functions are
our own effects, which honor cancellation (HTTP timeouts etc.):
wasmtime hard-bounds everything the agent's code does; the effect
boundary bounds everything we do on its behalf. No un-cancellable path
remains — the property the sobek runtime lacks end-to-end.

## Resolved questions (review 2026-07-07)

1+2. **wasi-only, spike included.** NativeEngine rejected — "faster
   first result" is not a benefit; wasi-only builds enforcement once at
   the syscall layer and de-risks the hard integration first (§1).
3. **`reset()` exposed to the agent** (as v1's `js.reset`) — the agent
   knows when its namespace is poisoned.
4. **No value-store cap for now.** Measure first: fuel/memory/timing/
   store-size metrics in every cell's terminal record and the debug log
   (§5) — things sobek could never measure. Capacity decisions come
   from those numbers later; guest memory limit is the backstop.
