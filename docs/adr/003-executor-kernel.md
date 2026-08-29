# ADR-003: Executor & kernel API

Status: **Accepted** (2026-07-07), amended 2026-08-14 (§4b: span name
defaults to the decorated def's `<module>.<function>`)
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
  `trace.effects_of(span=…)` for the digest. A failed cell ends its
  span with `ok: false` **and** `error: {type, message}` (the `@span`
  shape; the traceback stays digest text) — the record says why on
  its own, so a past-run reader (§4 `run=`) never has to mine the next
  llm request's tool_result for the exception.

### 4. Value store: `values` and `effects`

The kernel keeps every cell's printed values and last value, addressable
after the fact — the successor of v1's `_valueStore`/`logs.get` and
`_toolEffectsStore`/`toolEffects.get`, with the effects half now backed
by the trace instead of a parallel store:

```python
values.get(cell_id, i)        # i-th printed value of that cell
values.get(cell_id, "last")   # its last-expression value
values.list()                 # index: {cell_id: {n_prints, has_last, sizes}}
effects.of(cell_id)           # a scope's IMMEDIATE children (ADR-001 §4d):
effects.of(span="s3")         #   bare effects + child span rows, each row
                              #   carrying its own `span` id — recurse to
                              #   drill into a facade's inner effects
effects.get(seq)              # one full record (effect OR span) incl. output
effects.of(run=ref)           # a PAST run's root: llm.chat turn rows,
effects.of(run=ref, span=s)   #   the model cells run after each, top-
                              #   level effects; every view takes run=
effects.get(seq, run=ref)
effects.runs(program?, limit?)  # the run finder: [{id, program, status,
                                #   duration, turns, title, modifiedAt}]
effects.stats(run=ref)          # {run: {status, error, model, …},
                                #   turns: [{stop, in, out, cache*, cells,
                                #   effects, llmMs, costUsd}], total}
```

**Past runs (amendment 2026-08-28).** A chat reply's `traceRef`
(ADR-006 `agent_turns`) and a trigger's `lastRunRef` are handles the
guest can dereference: every trace view takes `run=<ref>` and then
reads that run from the trace store (ADR-001 §8) instead of the live
log. No scope with `run=` is the run's root — its parentless spans
(`llm.chat`, one row per model turn, interleaved with the `cell` spans
the loop runs after each turn; both are root-level siblings, ADR-005)
and top-level effects — so the same recursion the digest teaches (span
row → `effects.of(span=)`) walks a whole conversation: cell → tool
spans → syscalls, with each turn's reply on its `llm.chat` record.
Two finder/summary views complete it, both data twins of the CLI
(`trace ls`, `trace show --stats`): `effects.runs` and
`effects.stats`. With the trace store in `any` (ADR-023 §5, amendment
2026-08-29) `effects.runs` takes any-store `filter`/`sort` over the
per-run summaries and `effects.query(pipeline, coll=)` runs a
read-only aggregation over every record of every run — the cross-run
half (provenance, audit, failures) the per-run walk cannot answer. Access is the store's: any run in this bao's trace
store, any program — one store per anyrt instance, so there is no
cross-bao read to gate; the only check is that `run` is a run id.
Past-run records come back blob-resolved; they are plain data, and the
digest's own stub/`inferSchema` disclosure is how the model handles
their size — no separate rendering. The reads are recorded `trace.*`
effects of the current run (class read) like the live views, and the
digest excludes them from its side-effects line.

**Span drill-down (amendment 2026-07-18).** The clean collapsed facade
line is the default; the dig-in path is the same trace, on demand — the
agent's half of "clean tool errors, dig into the trace when something is
off" (ADR-003 §4b). `effects.of` takes either a `cell_id` (the cell's
top level) or `span=<id>` (one facade's immediate children); a span row
in either result carries the `span` id to recurse on. `effects.get`
resolves span records too (a `#seq` the digest cites for a facade is a
span-end record), so `effects.get(seq).span` → `effects.of(span=…)`
walks from a digest line down to the raw syscalls underneath.

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

**`kind` and clean method errors (amendment 2026-07-18).** The
decorator takes an optional `kind` — the guest-declared narrative
classification recorded on the span (ADR-001 §4d):

```python
@span("any.create_object", kind="mutator")
def create_object(self, space, body): ...   # http.post inside
```

The guest-side effect wrapper a tool author reaches for — bobrik's
`@effect` role, one clean `method(input) -> output` trace line with the
inner syscalls collapsed underneath — IS this decorator. The name stays
`span`, not `effect`: the guest global `effect` is the raw host channel
(`effect("http.post", …)`), and one name cannot be both the channel and
a decorator factory. `kind` mirrors the authored `### name(sig) [kind]`
doc; author keeps the two in sync, the same accepted
description-in-two-places drift as tool docs (schema vs code) — no
runtime path reads one from the other.

**Name defaults to the anchor (amendment 2026-08-14, with ADR-013).**
`span(name=None, kind=None)`: when `name` is omitted, the recorded
name is `<module>.<function>` read off the decorated def at decoration
time (the module object's `__name__`, which `use()` sets from the
spec). Survey at amendment time: ~90 span sites across both repos, and
every one but one restated exactly that pair as a string — a second
copy of a fact the code already carries, and it drifted the first time
an agent-authored tool landed (ADR-010's duplication argument, applied
to ourselves). An explicit `name` stays legal and is now *signal*: a
deliberate display override (`any.prune_ui_contexts` on the hidden
`_prune_ui_contexts`). The positional form cannot be dropped — frozen
published overlay versions call `span("name", kind=...)` forever
(ADR-009 freeze) — and the call form stays required (`@span(kind=...)`,
never bare `@span`): deploy's static scan and the ADR-013 write gate
key on the `@span(` line.

**Input is recorded by parameter name, and a leading `self` is
dropped.** The decorator reads `fn.__code__.co_varnames` (pure, no
`inspect`) so a method span records `{"space": …, "body": {…}}` — never
the bound instance, never an opaque positional `args` list. This
changes a span's canonical input form and therefore its `key`
(ADR-001 §3), so golden traces carrying spans regenerate with this
change (no-backcompat principle — the cleaner shape wins).

**Method-boundary errors are clean.** A raised exception ends the span
`ok: false` with `{type, message}` (no traceback) and re-raises; the
cell digest (ADR-005) surfaces that method-level line
(`any.create_object failed: AnyError not_found: …`) as the primary
error, the full inner effect trace reachable via `effects.of(cell)` /
`anyrt trace show` — legible at the boundary, internals on demand. This
is bobrik's contract (clean tool errors; dig into the trace when
something is off), now backed by the log instead of a parallel store.

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

  *Amended 2026-08-11 (gmail-sync sizing, ADR-012):* the per-run
  budget is **50B** (was 5B ≈ 2s of pure compute — starved legitimate
  data jobs; 50B ≈ tens of seconds, still a hard runaway ceiling).
  Exhaustion surfaces as a typed **`FuelExhausted`** error whose
  message says what works (split into smaller chunks) — the trap is
  deterministic, so a verbatim retry can never succeed, and serve
  forwards the typed text into the chat so the next turn's model acts
  on it. Long jobs avoid the cliff cooperatively: the
  **`fuel.state`** syscall returns `{remaining, budget}` (refreshed by
  the epoch callback, ≤ EPOCH_TICK_MS stale — a checkpoint signal,
  not an exact meter; recorded like any effect, so replay-stable) —
  batch loops check it and checkpoint + exit before exhaustion.
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
