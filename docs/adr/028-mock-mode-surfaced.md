# ADR-028: Mock mode surfaced — the mock spec on `anyrt run` and `run_cell`, strict replay as a verb, traceDiff views

Status: **Accepted** (2026-09-14)
Date: 2026-09-14
Builds on: ADR-001 §5 (two replay modes), ADR-002 §2 (the broker
pipeline), ADR-003 §4 (guest trace views), ADR-005 §2 (the `run_cell`
tool), ADR-023 (trace records in the `any` local store)
Amends when accepted: ADR-001 §5 (the spec shape, `meta.mock`
provenance, wildcard keys, the traceDiff predicate), ADR-002 §2
(span-scoped index, the mockable set, non-mockable syscalls), ADR-005
§2 (`mock` on `run_cell`, the digest header), ADR-023 §4 (a run's
records as a mock source)
Tracks: Linear BOB-121

## Context

The broker implements both replay modes ADR-001 §5 promised —
`Mode::Replay` (strict cursor, divergence error) and `Mode::Mock`
(`MockIndex`: loose FIFO by `(effect, key)`, a hit skips execute and
records `mocked: true`, `MockUnmatched::{Fail, Live}` on a miss). Both
are reachable only from Rust unit tests. `MockUnmatched::Live` still
carries an `allow(dead_code)` naming "the mock wiring in main.rs (next
round)"; that round never landed. `anyrt run` has no `--mock`, there is
no `replay` verb, and nothing renders the traceDiff view ADR-001 §5
defines.

The cost is paid on every reproduction of an effect-shaped bug: BOB-87
(2026-09-13) was reproduced live against a hand-written stdlib HTTP
server on a spare port pretending to be a token-gated API. Real LLM
turns, real side effects, one throwaway fixture per case, while the
machinery to feed a run from recorded effects sits unreachable.

A run id alone is not the feature. "Same effects, edited code" is one
use; "mock the third-party API, keep `any.*` live", "rehearse a
mutating flow against last run's records so nothing unmatched fires",
and "a scripted 401 nobody has recorded yet" are the others, and they
need a filter and inline records, not just a source.

## Decision

### 1. One mock spec, shared by the CLI and `run_cell`

```
mock = {
  from:      "run_<id>" | ["run_a", "run_b"],   # recorded effects, log order
  only:      ["http.*"],                         # globs: the mockable set
  except:    ["any.*", "llm.*"],
  records:   [                                   # inline, consulted BEFORE `from`
    {effect: "http.get", input: {url: "…"}, output: {…}},          # exact key
    {effect: "http.get", output: {…}, repeat: true},                # wildcard key, peek
    {effect: "http.get", error: {type: "http", message: "401 …"}},  # scripted failure
  ],
  unmatched: "fail" | "live"                     # default "fail"
}
```

Sources and filter are separate concerns:

- **`only` / `except`** decide the *mockable set* by glob (`only`
  narrows, `except` subtracts; both absent = everything mockable but
  §7). A glob matches the **effect name or any enclosing facade (span)
  name**: the any server's traffic is `http.*` effects under `any.*`
  spans, connector calls sit under `github.*` and the like, so
  `except: ["any.*"]` keeps every any call live while a bare
  `http.get` replays, and `only: ["any.create_object"]` rehearses one
  write. An effect outside the set is never consulted and always
  executes live, whatever `unmatched` says. This is how "part of a
  run" is expressed — never by record sequence numbers. A URL pattern
  for `http.*` (two bare hosts in one cell, no facade between them) is
  deferred until it shows up in practice.
- **`from`** folds the referenced runs' effect records into the index in
  log order (ADR-001 §5 v1 pop semantics), read from the trace store on
  the run's server (ADR-023 §4; the guest reads its own store, the CLI
  reads `--addr`). Spilled outputs are blob-resolved at index build
  (ADR-001 §7, ADR-026 §2); a missing blob is a spec error before
  anything runs.
- **`records`** are inline. An entry with `input` is keyed exactly as a
  recorded call would be (`input_key` of the normalized input). An
  entry without `input` takes the **wildcard key** `(effect, "*")`,
  consulted after the exact key misses. `repeat: true` peeks instead
  of popping. `error` instead of `output` scripts a failure: the call
  raises the typed `EffectError` and records an error record, exactly
  as a live failure would (ADR-002 §2).
- Inline records sit at the **front** of their key's queue, so they
  override a `from` record for the same call.
- **`unmatched`** is the only knob shared by both surfaces. `fail`
  raises `EffectError(type: "mock_unmatched")` into the guest and
  records it; `live` executes and records `mocked: false` with
  `meta.mock.unmatched: true`.

### 2. Matching modes are not a switch

Strict replay (cursor over the whole log; reorder, extra or missing
call = divergence) is the runtime's determinism check. It gets its own
verb (§4) and never appears in the spec. Loose mock is the spec above,
on both surfaces. `run_cell` has exactly one matching mode.

### 3. Scope: run-level on the CLI, span-scoped on `run_cell`

The broker's index is per run today. Cells execute inside the guest
kernel (`subcell` is the kernel's own `_run_cell`, ADR-003 §2), so the
host has no per-cell entry point except the `span.begin(name: "cell")`
the toolcaller already emits around each cell (ADR-005 §2). The spec
rides that span: `span.begin` with `input.mock` **installs** an index
for the span's lifetime; the matching `span.end` **drops** it. The
spec is thereby recorded on the span record with no extra machinery
and a mocked cell cannot leak into the next. A run-level index (CLI)
and a span-scoped one may coexist; the span-scoped one is consulted
first. Nested cell spans each carry their own; a nested span without
`input.mock` inherits the enclosing one.

### 4. CLI

- `anyrt run --mock <run_id | spec.json>` — a bare run id is sugar for
  `{"from": "<run_id>"}`. Common-case flags layer on top:
  `--mock-only <glob>…`, `--mock-except <glob>…`,
  `--mock-unmatched fail|live`. The spec is recorded on the run header.
- `anyrt replay <run_id>` — strict mode over the referenced run
  (`Mode::Replay` + `ReplayCursor`), same run command shape otherwise.
- `MockUnmatched::Live` loses its `allow(dead_code)`.
- Mocked runs land in the trace store as ordinary runs (ADR-023).

### 5. `run_cell(code, mock=…)` and what it returns

The tool gains an optional `mock` argument holding the §1 spec;
`mockref: "run_<id>"` is sugar for `mock: {from: "run_<id>"}`. The
toolcaller passes it through as `input.mock` on the cell span (§3).
The digest (ADR-005 §2, `render_digest`) keeps its shape — prints,
last value, side effects from `trace.effects_of`, error, hints — and
gains, only when a spec was present:

```
[MOCK] 3 of 4 effects served from run_7c1e… (http.get ×3); 1 live: any.query #88

Output: …
Last value: …

Side effects: any.query ×1, http.get ×3 (mocked)
  would mutate any.modify #91 (mocked: NOT executed)

Values above came from recorded effects, not live data. Nothing marked
mocked was executed or written. Re-run without `mock` to do it for real.
```

- The header is **always first** when a spec is present, including
  `0 of N effects mocked`: a wrong glob or stale keys must be visible,
  never inferred. A **second line warns** when an `only` / `except`
  glob matched no call in the cell (`WARNING: only: ["any.*"] matched
  no call in this cell — every effect ran live`): the span-scoped index
  counts the calls `only` admitted and `except` removed, the cell
  span's end record carries them as `meta.mockFilter`, and the
  `span.end` effect returns that meta to the loop. A narrowing glob
  that matches nothing would otherwise silently widen to "everything
  live" — on a rehearsal, that is the write executing.
- Prints and last value are unchanged. That is the feature.
- Side-effect rows carry `(mocked)`, `(live)`, or, for a span facade
  whose inner effects split, `(mixed: 2 mocked, 1 live)`. A mocked
  mutation renders `would mutate … (mocked: NOT executed)`, never
  `mutate`.
- A miss under `unmatched: fail` is a typed error in the normal
  `Error:` slot and the tool result is `is_error: true`, like any cell
  failure. Under `live` the miss shows `(live)`.
- A bad spec (unknown run, unparseable glob, inline record with
  neither `output` nor `error`, unresolvable blob) is an `is_error`
  tool result **before** any cell span opens.
- The trailing sentence is fixed text on every mocked result. It is
  the guard against a stale-mock pass being reported as verification.

Where the model learns about it: the `run_cell` tool schema plus one
paragraph in `_core` (the always-composed skill). Not `_coding`: that
skill rides only with the shell feature (ADR-024 §4).

### 6. traceDiff views

ADR-001 §5 defines traceDiff as the records executed live inside a
mocked run. With a mockable set (§1) "live" alone is too wide: an
effect outside the set is live by design. The predicate is
`meta.mock.unmatched == true`.

- `anyrt trace show <run> --unmocked` — the traceDiff view.
- `anyrt trace diff <run_a> <run_b>` — effect-level set difference by
  `(effect, key)` plus changed outputs on shared keys (output hash).
  "Adjust code, compare" needs both views.
- `anyrt trace show` marks mocked effect rows `≈`, beside the existing
  `*` for mutate (`~` stays the facade-span marker); a facade line
  counts its served inner effects (`…, 2 mocked`).

### 7. Never mockable

`span.*` and `trace.*` are the trace's own machinery; a mocked trace
view would lie about the run it is in. The kernel's plumbing —
`module.resolve`, `kernel.boot`, `runtime.get`, `mailbox.drain`,
`fuel.state` — is nothing anyone rehearses, and under `unmatched:
fail` a mockable `module.resolve` kills the cell on its first `use()`
before the rehearsed call is reached. All of these execute live
regardless of the spec and carry no `meta.mock`. Everything else,
including `time.now`, `config.get` and `llm.*`, is mockable:
nondeterminism is exactly what a mock replaces.

### 8. Provenance

Every consulted effect record carries:

- served: `meta.mocked: true`, `meta.mock: {from: run_id, seq}` or
  `meta.mock: {inline: i}`;
- unmatched-live: `meta.mocked: false`, `meta.mock: {unmatched: true}`;
- unmatched-fail: an error record, `error.type: "mock_unmatched"`,
  `meta.mock: {unmatched: true}`;
- outside the mockable set: no `meta.mock` key at all.

Capability check still precedes the consult (ADR-002 §2 order
unchanged): a denied effect is denied whether or not a mock exists.

## Consequences

- One live e2e trace becomes the fixture for the next run of the same
  case; BOB-87-style reproductions stop needing a hand-written server.
- The agent can iterate on an agent-authored program (ADR-013) against
  real recorded data with no side effects, rehearse mutating flows
  with `unmatched: fail`, and re-run a past cell to answer "why did
  that return X".
- Keys are input-shaped: an edit that keeps the same effect inputs
  still hits the mock. That is the feature; `_core` says so.
- Runtime change (broker, CLI): `anyrt` rebuild + serve restart.
  Toolcaller + `_core` change: deploy. No kernel change.
- `from` may point at another program's run; records are effect-level.

## Implementation notes

- `runtime/src/replay.rs`: `MockSpec::decide` (effect + facade globs), `Unmatched`, `MockIndex::build`
  (inline first, wildcard key, `repeat`), `glob_match`,
  `never_mockable`. `runtime/src/broker.rs`: `span_mocks` (the
  span-scoped stack), `build_mock_index`, the consult in `call`,
  span-end `meta.mocked`; `trace.effects_of` rows lift `unmatched`
  (effect) and the served count `mocked` (facade span) for the digest.
- `runtime/src/main.rs`: `run_cmd(spec, args, RunOpts, RunHow)` behind
  `run --mock*` and `replay`; the run header carries `args` (+ `mock`,
  or `replayOf`). `runtime/src/view.rs`: `≈` / `[unmocked]`,
  `unmocked`, `diff`.
- `repos/_agent/programs/toolcaller@v1.py`: `mock` / `mockref` on
  `run_cell`, `_mock_header`, mock-aware `_side_effects`, `MOCK_GUARD`;
  `repos/_agent/skills/_core.md` paragraph.
- `mock` ships on the tool schema (the §5 question resolved at
  acceptance): the runtime-side guards (header always first, `would
  mutate`, the fixed closing sentence) are what make that safe, not
  the prompt.
