# ADR-002: Effect boundary & isolation

Status: **Accepted** (2026-07-07)
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
`http.get/head/post/put/delete(...)` with `params=/headers=/json=`
returning a `Response` (`.status`, `.json()`, `.text`, and `.url` — the
final post-redirect url, added by ADR-008 §2; `head` classifies as
`read` like `get`); **`print()` is the
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
security milestone (a static Rust host embedding wasmtime implements
the SAME syscall surface natively; nothing above the boundary changes).

## Design rationale: the thin host (syscall doctrine)

**Syscall analogy, taken literally.** The host is a kernel: it exposes
a SMALL, STABLE syscall set — `http.*` (the one outbound door),
`config.get`, `mailbox.drain`, time/random/uuid/sleep/env,
`module.resolve`, `batch`, `trace.*` views, span begin/end — plus the
broker machinery (trace, replay, capability check, redaction) and the
engine itself. Everything else — the any client, llm adapters, memory
policy, recall, history, the conversation loop itself — is guest-side
Python: modules in program space, deployed and versioned like any
program, hot-swappable without touching the host. The host surface
changes rarely and by necessity; the product changes weekly WITHOUT a
host release.

**What the host contributes is exactly what guest code must not hold:**

- **Classification truth.** read/mutate and the capability for an http
  call derive from (method, url) at the boundary — a route table
  (anybao.routes, any_base-scoped), data not code, sourced from the
  api-drift manifest when routes change. Spans let guest facades group
  their calls into legible trace lines, but spans are narrative; class
  and cap are boundary facts guest code cannot fake.
- **Secrets.** Named-credential injection: a payload names
  `{ref, header, prefix}`; the host resolves the config secret and sets
  the header AFTER the payload records. Key values never enter guest
  memory or the trace — so llm adapters can live guest-side while keys
  never do.
- **Recording and replay.** The broker records every crossing; loop
  control (mailbox.drain) is itself a recorded effect, so a replayed
  conversation replays its interruptions.

**The two wraps answer different questions.** The guest-side facade
(module functions, `http.get`, `print()`) provides *shape*: a natural
Python surface for LLM-written code, holding ZERO authority — bypassing
it gains nothing. The host-side broker provides *authority and truth*.
Between them sits a serialized crossing: a wasm host call today, a Rust
host function at the security milestone. Only the transport changes;
neither wrap's contents do — that is how "identical program-facing
contract at every stage" is achieved mechanically, not by promise.

One line: **the cage and the syscalls are infrastructure and never
change; everything the agent IS lives above the boundary as deployed
guest code — so the host can be wasmtime-py today and a static Rust
binary tomorrow, and the agent cannot tell.**

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

## Resolved questions (review 2026-07-07)

1. **Ergonomic shim names** — inject `now()/rand()/env()` globals in
   addition to proxied modules; both routes hit the same effects.
2. **`uuid`** — effect-backed `uuid4()` (recorded like `random`);
   deterministic uuid5 passes through the allowlist.
3. **Batch** — N individual records + host-side concurrency, exposed as
   **per-facade `*_many` methods** (no generic combinator in v2.0):
   `http.get_many(urls, ...)`, `llm.complete_many(prompts, tier=...)`.
   One guest→host crossing carries the list; the broker fans out to the
   same single-item effect on a pool (per-item cap checks/normalizers/
   redaction unchanged, no separate batch effect in the catalog).
   Records append in **input order** regardless of completion order
   (strict replay stays deterministic under concurrency), each
   individually mockable, `meta.batch: {id, i}` for provenance.
   Per-item failure is an `EffectError` value in that slot, never a
   whole-batch exception. Generic thunk-based `batch()` rejected:
   thunks are guest code and cells stay single-threaded; a
   descriptor-based combinator can be added later without touching the
   trace contract.
   *Teaching burden (accepted as low)*: not using `*_many` is
   correct-but-slow, never wrong — sequential calls yield the same
   records; real consumers are programs (classify maps, fan-outs), not
   ad-hoc cells. Guidance = tool docs + one skill line; plus a **digest
   hint** — when a cell loops the same effect many times sequentially,
   the result digest suggests `*_many` (teach at the moment it matters,
   zero standing prompt cost).
