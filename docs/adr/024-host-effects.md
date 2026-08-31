# ADR-024: Host effects — `sh.*` and `fs.*` on the serve's device

Status: **Proposed**
Date: 2026-08-31
Builds on: ADR-002 (effect boundary: declaration, broker pipeline,
deny-by-default namespace), ADR-001 §7 (blob spill), ADR-003 §2
(cell cancellation: epoch deadline, fuel, hard break), ADR-010
(docstrings as the doc surface)
Amends when accepted: ADR-002 §3 (two new namespace globals), ADR-003
§2 (cancellation reaches child processes)
Deferred, not decided here: restrictions of any kind — capability
grants, consent, path confinement, sandboxing (see §5)

## Context

bao's loop is already the shape of a coding agent: `run_cell` in a
persistent kernel, digests with `values.get` stubs for large results,
ceilings, mailbox inject/break, a trace of every crossing,
space-resident skills, and `programs@v1` for code it writes itself.
What it cannot do is touch the machine it runs on. Every syscall in
the broker is HTTP, config, oauth, or trace (`broker.rs::execute`);
the guest is CPython-on-wasm with no filesystem and no process
(ADR-002 §3 — that is the isolation principle made physical). A
coding task — read a repo, edit files, run the tests, run git — has
no path to the world at all.

The user's constraints for the first cut:

- **Chat stays the same.** No UI work; the tooling is the whole
  change.
- **Python stays the data-manipulation language.** Cells keep doing
  the parsing, filtering, and shaping; the host machine is reached
  for files, processes, and the project's own toolchain.
- **Shell is non-negotiable.** A coding agent lives on `git`,
  `cargo`, `pytest`, `make`, `rg` — the project's tools, not a
  re-implementation of them.
- **Nothing to configure, nothing to restrict — yet.** bao simply
  gets another tool. It works on whatever device the serve runs on;
  if the code isn't there, it isn't there (or bao clones it). Config
  and memory stay in the `any` space as for everything else; no new
  device-local config. The grant/consent policy the plan designed
  (`caps.rs`: `GrantSet`, hash-keyed `GrantLedger`, `decide` with a
  chat consent prompt) is still dead code; wiring it, path
  confinement, sandboxing — all of it — waits until there is a
  working loop to restrict.

One prior position this ADR has to meet honestly: **the thin-host
rule** (00-plan §5, m-notes 2026-07-08: "never add host effects")
already bent for `oauth.*` (ADR-011) and `trace.*` (ADR-023) —
syscalls the guest structurally cannot implement. `sh` and `fs` are
the same class, with one difference worth stating: they are the first
effects that reach the *local machine* rather than the network. The
isolation principle still holds — every call is declared, classified,
recorded, and replayable from the trace — and it is the honest-code
stage of 00-plan §5 ("staging changes enforcement strength, never the
principle"): nothing ambient reaches the guest, so every command bao
runs is in the trace by construction. What the command then does on
the machine is not confined. That is the point of §5.

## Decision

### 1. `sh.*` — processes on the serve's device

Four syscalls, host-implemented in the broker, guest-exposed as one
global `sh` beside `http` (ADR-002 §3):

| effect | kind | cap | in → out |
|---|---|---|---|
| `sh.run` | mutate | `host.shell` | `{cmd, cwd?, timeout_s?, stdin?, env?}` → `{exit, stdout, stderr, durationMs, truncated, timedOut}` |
| `sh.spawn` | mutate | `host.shell` | `{cmd, cwd?, env?}` → `{handle}` |
| `sh.poll` | mutate | `host.shell` | `{handle, wait_s?}` → `{running, exit?, stdout, stderr, truncated}` (output since the previous poll) |
| `sh.kill` | mutate | `host.shell` | `{handle}` → `{killed}` |

- **Every `sh.*` call is `mutate`.** A shell is never safely
  re-executable — even `ls` observes state that the next command may
  change — so replay serves every result from the trace and never
  re-runs a command (ADR-001 §5). `poll` is `mutate` too: it consumes
  output, and consuming twice is not the same read.
- **`cmd` is one string, run by the user's shell**: `$SHELL -lc
  <cmd>` from the serve's environment, `/bin/sh -c` when `SHELL` is
  unset. Login shell because the serve may be launched by the
  desktop app with a bare PATH, and the project's toolchain (nix,
  uv, cargo) lives in the profile. No argv form: the model writes
  pipelines and quoting exactly as it would in a terminal, and the
  trace shows the literal line the user would have typed.
- **`cwd`** defaults to the serve process's working directory; the
  model passes `cwd` explicitly (the `_coding` skill says: absolute
  paths, always). There is no ambient cwd across calls — a cell that
  wants "the current directory" keeps its own variable (nothing
  ambient, ADR-002). `runtime.get().host` (§4) tells the model where
  it is.
- **Timeout is data, not an error.** `timeout_s` defaults to 120 and
  is clamped to the cell's remaining wall budget (ADR-003 §2). On
  expiry the host kills the process *group*, returns whatever was
  captured, and sets `timedOut: true`, `exit: null`. A non-zero exit
  is likewise data (`exit: 1`, stderr present) — the cell decides;
  `EffectError` is reserved for the boundary refusing the call
  (malformed payload, unknown handle, spawn failure).
- **Output capture is bounded**: 1 MiB per stream, head + tail with
  a `[… N bytes elided …]` marker and `truncated: true`. Anything
  over ADR-001's 64 KiB record threshold spills to the blob store as
  today; guest-side the digest stub + `values.get` walk apply
  unchanged (`_core.md`). The model is told to pipe through `head`/
  `tail`/`rg` first — bounded capture is the backstop, not the tool.
- **`spawn`/`poll`/`kill`** exist for builds and test suites longer
  than a cell: spawn in one cell, do other work, poll (with a
  bounded `wait_s`) in later cells. Handles are run-scoped: **every
  child still alive when the run ends is killed** — a run leaves no
  processes behind. A dev server that should outlive the
  conversation is a different feature (services), out of scope.
- **Cancellation reaches children (amends ADR-003 §2).** A hard
  break, a cell timeout, or fuel exhaustion kills every child of the
  cell's process group before the cell is reported interrupted. No
  orphaned `cargo build` after the user hits break.
- **Environment: the serve's, plus the call's `env` map.** The child
  inherits the serve process environment (that is what makes the
  login shell and the toolchain work) with the call's `env` merged
  on top. Secrets are store rows (ADR-021 §4), never env, so there
  is nothing of bao's to leak; whatever the user's own shell profile
  exports is the user's. Per-call `env` values are recorded in the
  trace like any payload; the standard `redact` paths apply
  (`env.*TOKEN*`, `env.*KEY*`, `env.*SECRET*`).

### 2. `fs.*` — files without a shell

Shell alone is not enough for editing: heredoc/`sed` edits are the
canonical failure mode of shell-only agents, and a `sh.run("sed …")`
record can never show *what changed*. Four syscalls, guest global
`fs`:

| effect | kind | cap | in → out |
|---|---|---|---|
| `fs.read` | read | `host.fs.read` | `{path, encoding?: "text"\|"base64", offset?, limit?}` → `{text\|data, size, lines?, truncated}` |
| `fs.list` | read | `host.fs.read` | `{path, glob?, depth?}` → `{entries: [{path, kind, size}]}` |
| `fs.write` | mutate | `host.fs.write` | `{path, content, encoding?, mkdirs?}` → `{bytes, created}` |
| `fs.edit` | mutate | `host.fs.write` | `{path, old, new, all?}` → `{replacements}` |

- **`fs.edit` is exact-replace**: `old` must occur exactly once
  (or `all: true`); zero or several occurrences is a typed failure
  (`fs.edit_ambiguous` / `fs.edit_not_found`) with no write. The
  record carries `old`/`new` verbatim — the trace *is* the diff.
- **`fs.read` is bounded** like `sh` output (1 MiB, `truncated`);
  `offset`/`limit` are line-based for text so the model reads a
  region, not a file. `encoding: "base64"` mirrors ADR-020 §1 for
  binary.
- **Paths** are taken as given: absolute, or relative to the serve's
  working directory. No resolution rules, no roots (§5).
- **Read effects are safe to re-execute in loose replay** (ADR-002
  §1 semantics) with the same caveat `http.get` already carries: the
  world may have moved. Strict replay serves them from the trace.
- Grep, find, diff, git stay in the shell — `rg`/`git diff` are
  better tools than any effect this ADR could design, and Python in
  the cell parses their output.

**Python keeps the data role.** `fs.read` returns text; the cell uses
`re`/`json`/`ast` (all tier-1 imports, ADR-002 §4, ADR-013) on it and
hands the result to `fs.write`/`fs.edit`. Anything needing the
project's real interpreter — C extensions, its venv, its test runner
— goes through `sh.run("uv run …")` on the host. The wasm guest
never grows a package manager.

### 3. Caps names fixed, nothing enforced

`cap_of` returns `host.shell` / `host.fs.read` / `host.fs.write` so
the names exist in every record from day one and a future grant
ledger has something to key on. With no grant policy wired, every
cap is permitted — exactly as for every other effect today. The
cap names are the only trace this ADR leaves for §5.

### 4. Guest surface and prompt

- Two namespace globals, `sh` and `fs` (amends ADR-002 §3), added in
  `runtime/guest/app.py` beside `http`. Docstrings are the doc
  (ADR-010): `help(sh)` / `help(fs.edit)` show signatures, return
  shapes, and the timeout/truncation contract. `sh.run` is the only
  thing most cells need; the docstring says so.
- `runtime.get()` (ADR-006 §3 surface) gains `host: {cwd, home,
  shell, os}` — where bao is, so the first cell doesn't have to
  probe with `pwd`.
- **`_coding.md`, a new space-resident skill**, deployed like any
  other (`anyrt deploy`), always composed in. It carries the
  workflow, not the API: absolute paths; read before you edit;
  `fs.edit` over rewriting a file; run the project's tests after a
  change and read the failure; bounded output (`| head`, `rg` before
  `cat`); stage by explicit path, never `git add -A`; never push,
  force, reset, or delete outside the project without the user
  asking in that conversation; report a failing test as failing.
  A bao that should not code drops the skill from its space —
  prompt-side, no runtime switch.
- The `_core` reply/cell discipline is unchanged: shell output lands
  in the digest like any value, large values stub to `values.get`,
  `print()` stays the model-facing channel.
- Model tier is config as always: `llm.tier.codegen` is an
  `agent_config` row (`claude-sonnet-5` from `config_defaults.json`);
  a coding bao flips the row in its space.

### 5. Restrictions: later, and stated as such

Nothing in this ADR confines what a command does. No workspace root,
no path rules, no allowlist, no consent, no sandbox. A serve that
runs this binary can run anything its user can. The user's call for
the first cut: get a loop that works, then decide what to restrict
from what it actually does. The follow-up ADR owns all of it — wiring
`caps.rs` (per-program grants, per-command approval via the ADR-021
request-in-chat pattern), path roots if they earn their keep, a
sandbox for the child (sandbox-exec / landlock) — informed by traces
of real coding sessions rather than guessed up front.

Until then the trace is the only control: every command, exit code,
and diff is recorded, `anyrt trace show` reads a coding session as it
reads a connector run, and the `_coding` skill states the etiquette
(§4) to a model that has so far followed etiquette well.

### 6. Out of scope

UI (diff/terminal rendering — chat stays as is), PTY/interactive
commands, background services outliving a run, parallel subagents,
running on a device other than the serve's, restrictions (§5), file
upload to `any` (ADR-020 covers download only).

## Consequences

- bao can read, edit, and run code on the machine its serve runs on
  through recorded, replayable effects. A coding conversation
  replays exactly like any other run.
- **Every serve gets shell the moment it runs this binary** — prod
  included. There is no opt-in; the earlier draft's `[host]`-block
  gate was exactly that opt-in and is dropped on purpose. What
  stands between a prod bao and `rm -rf` is the model's judgement
  and the skill text, and that is the accepted v1 posture (§5).
  Deploy order matters accordingly: the skill lands in the space
  before the binary that makes the tools real.
- The thin-host line moves from "network only" to "network + the
  serve's own device". The restrictions ADR draws the real line.
- Trace volume grows: shell output is bulkier than JSON. The 1 MiB
  caps + blob spill + ADR-023 retention bound it; the `_coding`
  skill pushes the model toward bounded commands.
- Two new failure classes for the digest to render well:
  `timedOut` runs with partial output, and non-zero exits — both
  data, both shown, neither an exception (§1).
- Broker gains process management (spawn/poll/kill, process groups,
  run-end reaping) — the first non-request/response syscall.
  Cancellation (ADR-003 §2) gets a second thing to clean up.

## Open questions (to settle before Accepted)

1. **Login-shell cost.** `$SHELL -lc` sources the profile on every
   call (tens of ms for zsh, more for a heavy fish config). Accept,
   or snapshot the login PATH once at serve start and run `$SHELL
   -c` with it? Leaning: `-lc`, measure, revisit if it shows in the
   digest timings.
2. **`fs.edit` whitespace tolerance.** Exact match only (simple,
   trace-honest) vs. a normalized fallback when the exact match
   fails. Leaning: exact only; the model re-reads and retries — the
   digest makes that cheap.
3. **Per-stream cap** — 1 MiB is a guess. Measure a `cargo test` and
   a `pytest -v` on this repo before the constant lands.
4. **Is `sh.spawn/poll/kill` v1?** `sh.run` with a long `timeout_s`
   may be enough to test with; spawn/poll can be the second commit.

## Implementation sketch (after acceptance)

One topic per commit, on `feat/adr-024-host-effects`:

1. `broker.rs`: `sh.run` + `fs.read/list/write/edit` syscalls,
   output caps, process-group kill on timeout; unit tests against a
   temp dir; replay test (a `sh.run` record replays without
   executing).
2. `runner.rs`/`serve.rs`: child reaping on hard break, cell
   timeout, and run end (ADR-003 §2 amendment lands here).
3. `runtime/guest/app.py`: `sh`, `fs` globals with docstrings;
   `runtime.get().host`; guest-module tests with the fake `effect`.
4. `repos/_agent/skills/_coding.md`.
5. `sh.spawn/poll/kill` (if Q4 says yes).
6. Rig e2e on the prod-test serve: a conversation that clones or
   opens a repo, reads, edits, runs tests, and commits; trace review
   per `docs/debugging.md`.
