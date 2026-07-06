# ADR-002: Effect boundary & isolation

Status: **Proposed** (awaiting review)
Date: 2026-07-07
Builds on: ADR-001 (trace format v2 — accepted)

## Context

The isolation principle (00-plan.md §5): nothing executes side effects
except through the effect boundary — one invariant, two faces (bit-exact
replay + confinement). ADR-001 fixed what a recorded effect *looks
like*; this ADR fixes how effects are **declared, called, checked, and
how the cell namespace is built** so that no other path to the world
exists. Out of scope here: executor mechanics (ADR-003), module
resolution grammar (ADR-004), the concrete effect catalog (fetch/llm/
chat/… specified as built, each a small doc-per-effect).

## Decision

### 1. Declaration: the `@effect` decorator

Effects are host-side functions (anyrt/anybao code — never guest code),
registered declaratively:

```python
@effect("http.get", kind="read",            # JSON trace field: "class"
        normalize=normalize_http,           # raw args -> canonical input (ADR-001 §3)
        redact=["headers.authorization"],   # sensitive paths, masked pre-record
        cap="net.http")                     # required capability (default: effect name)
def http_get(ctx, url, *, params=None, headers=None, timeout=None): ...
```

**Pythonic surface (not JS land).** The old harness mimicked JS
(`fetch`, `console.log`); anybao's effect catalog mimics Python idioms —
the model has seen far more requests-style Python than JS-in-Python:
`http.get/post/put/delete(...)` with `params=/headers=/json=` returning
a `Response` (`.status`, `.json()`, `.text`); **`print()` is the
model-facing output channel** (the console.log role — traced as a
structured-value record, primary input to the digest); snake_case
everywhere. Granular verbs also fix classification: `http.get` is
`read`, `http.post` is `mutate` — one JS-style `fetch` couldn't declare
either honestly.

- `kind` is declared, never guessed (kills the v1 name-prefix
  heuristic). `kind="read"` effects are safe to re-execute; `mutate`
  are not — replay and future tooling rely on this.
- `normalize` produces the canonical input; identity+canonical-JSON if
  omitted. `redact` masks before the record is written — secrets
  structurally cannot enter traces (ADR-001 §3).
- Inputs and outputs MUST be JSON-serializable (trace constraint); the
  decorator enforces this at call time, loudly.
- `ctx` is the broker handle, visible only to effect implementations:
  credential/config resolution happens here, inside the boundary —
  guest code passes tier names/handles, never key bytes.

### 2. The broker: one pipeline for every call

Every effect call, no exceptions, flows through:

```
normalize → key → capability check → replay/mock consult → execute → record → return
```

- **Capability check** precedes execution. The active grant set is the
  Macaroons-style **intersection over the import chain** (a program can
  never lend more authority than it holds; frame tracking is supplied
  by the executor/loader, the *rule* lives here). Denials are recorded
  as error records (`error.type = "capability_denied"`) — audit for
  free.
- **Replay/mock consult** per ADR-001 §5 (strict cursor / loose FIFO).
  Mocked calls skip execute but still record (`mocked: true`).
- **Record always** — success, error, denial, mock. If it isn't in the
  trace, it didn't happen; corollary: if it happened, it's in the trace.
- Effect failures return as **data** in the record AND raise a typed
  `EffectError(seq, type, message)` into the guest — the cell may catch
  it; the digest shows it; nothing is silently swallowed.

**The enforcement mechanism ships enabled from day one.** v2.0 runs a
permissive default grant profile (self-authored context), but checks
execute, denials are possible, and every decision is recorded — policy
tightens later without touching mechanism.

### 3. The cell namespace: deny-by-default

Cells execute in a constructed namespace containing **only**:

- **kernel facades** (tools; ADR-003 owns their shape),
- **effect shims** (§4) and **`print()`** — the traced structured-value
  output channel (console.log's role in v1),
- **curated builtins**: the pure computation subset (`len`, `range`,
  `enumerate`, `zip`, `sorted`, `min/max/sum`, `dict/list/set/tuple`,
  `str/int/float/bool`, `isinstance`, `repr`, comprehension machinery,
  exceptions). Excluded: `open`, `input`, `eval`, `exec`, `compile`,
  `breakpoint`, `globals`, `vars`, raw `__import__` (replaced by our
  hook), `memoryview`/`bytearray` stay (pure), `object.__subclasses__`
  escape hatches are *not* chased in-process — see §5.
- **the import hook** (§4) as the only `__import__`.

### 4. Shims and imports: ambient authority replaced, not blocked

- **Module allowlist with three tiers**, resolved by our
  MetaPathFinder (the only importer):
  1. *Pure stdlib* — passes through: `math`, `json`, `re`, `itertools`,
     `functools`, `collections`, `textwrap`, `heapq`, `bisect`,
     `statistics`, `dataclasses`, `enum`, `typing`, `decimal`,
     `fractions`, `base64`, `hashlib`, `uuid`(v5 only — v1/v4 are
     nondeterministic; see open Q2), `unicodedata`.
  2. *Proxied stdlib* — modules whose API is wanted but which carry
     ambient authority return a **proxy module**: `datetime`
     (construction/arithmetic pass through; `datetime.now()`,
     `date.today()` are effect-backed → `time.now` records), `random`
     (all functions effect-backed → `random` records), `time`
     (`time()`/`sleep()` effect-backed), `os` reduced to
     `os.environ`-as-`env.get` effect.
  3. *Space programs* — `name@version` per the resolution rules
     (ADR-004); each load emits a `module.resolve` record.
  Everything else: `ImportError` with a message naming the boundary.
- Direct shims also injected as globals for ergonomics (`now()`,
  `rand()`, `env(...)`) — same effects underneath.
- **Import is an effect**: allowlist decisions and resolved versions
  are part of the recorded run.

### 5. Enforcement strength is staged; the API is not

In-process v2.0 delivers this contract for honest code: nothing ambient
is *reachable*, so accidental effects and accidental nondeterminism are
structurally impossible. Deliberate escape (ctypes-style) is not
defensible in-process and we do not pretend otherwise — that is the
security milestone (wasmtime + CPython-on-WASI guest; engine timing and
host shape are ADR-003's decision — deferred by review 2026-07-07).
**Effect registration stays in Python in every engine variant**: with
wasmtime-py the Python harness IS the host and `@effect` functions are
the host imports; with a Rust engine binary the engine relays host
calls to the same Python implementations over IPC. The engine choice
picks the cage, never where effects are written. Programs and effects
see an identical API at every stage.

## Consequences

- One choke point: tracing, mocking, capability checks, redaction, and
  limits all hook the same pipeline — each future feature is a broker
  middleware, not a new mechanism.
- The effect catalog becomes declarative data (name/kind/cap/normalizer)
  — auditable, diffable, and the natural source for the capability
  manifest in the program format.
- Guest code is portable across enforcement stages by construction.
- Cost accepted: proxy modules are fiddly to build correctly (datetime
  especially); the tier-1 allowlist will need occasional additions.

## Open questions (reviewer input wanted)

1. **Ergonomic shim names**: inject `now()/rand()/env()` globals in
   addition to proxied modules, or force module imports only? Lean:
   inject — cells are written by an LLM; shorter is fewer tokens and
   fewer mistakes, and both routes hit the same effects.
2. **`uuid`/id generation**: allow only deterministic uuid5, or provide
   an effect-backed `uuid4()` (recorded like `random`)? Lean:
   effect-backed uuid4 — id generation is too common to ban.
3. **Batch effects**: keep `fetchBatch`-style batching as first-class
   effects (one record carrying N sub-inputs), or N records + broker
   parallelism? Lean: N individual records (uniform trace, replay
   stays simple); the *facade* may still offer a batch helper that fans
   out.
