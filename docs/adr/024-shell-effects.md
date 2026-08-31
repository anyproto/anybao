# ADR-024: Shell effects — `sh.*` and `fs.*` on the serve's device

Status: **Accepted** (2026-08-31; user go-ahead — implementation follows the sketch below, one topic per commit)
Date: 2026-08-31
Builds on: ADR-002 (effect boundary: declaration, broker pipeline,
deny-by-default namespace), ADR-001 §7 (blob spill), ADR-003 §2
(cell cancellation: epoch deadline, fuel, hard break), ADR-010
(docstrings as the doc surface)
Amends when accepted: ADR-002 §3 (two new namespace globals), ADR-003
§2 (cancellation reaches child processes), ADR-005 §2 (a second tool,
`bash`, under the feature)
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
  the parsing, filtering, and shaping; the machine is reached
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
| `sh.run` | mutate | `sh.run` | `{cmd, cwd?, timeout_s?, stdin?, env?}` → `{exit, stdout, stderr, durationMs, truncated, timedOut}` |
| `sh.spawn` | mutate | `sh.spawn` | `{cmd, cwd?, env?}` → `{handle}` |
| `sh.poll` | mutate | `sh.poll` | `{handle, wait_s?}` → `{running, exit?, stdout, stderr, truncated}` (output since the previous poll) |
| `sh.kill` | mutate | `sh.kill` | `{handle}` → `{killed}` |

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
  ambient, ADR-002). `runtime.get("shell")` (§4) tells the model where
  it is.
- **Timeout is data, not an error.** `timeout_s` defaults to 120 and
  is clamped to the cell's remaining wall budget (ADR-003 §2). On
  expiry the runtime kills the process *group*, returns whatever was
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
| `fs.read` | read | `fs.read` | `{path, encoding?: "text"\|"base64", offset?, limit?}` → `{text\|data, size, lines?, truncated}` |
| `fs.list` | read | `fs.list` | `{path, glob?, depth?}` → `{entries: [{path, kind, size}]}` |
| `fs.write` | mutate | `fs.write` | `{path, content, encoding?, mkdirs?}` → `{bytes, created}` |
| `fs.edit` | mutate | `fs.edit` | `{path, old, new, all?}` → `{replacements}` |

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
— goes through `sh.run("uv run …")` on the machine. The wasm guest
never grows a package manager.

### 3. Caps: the default naming, nothing enforced

No custom cap names: each syscall's cap is its own name, the
reference-host default (`broker.rs::cap_of`), so a future grant
ledger keys on `sh.*` / `fs.*` wildcards (`GrantSet` prefix entries)
or on `fs.read` alone. With no grant policy wired, every cap is
permitted — exactly as for every other effect today. Nothing named
"host": in this repo *host* is the runtime side of the boundary
(ADR-002 §1), and these effects are guest-facing tools like `http`.

### 4. Guest surface, the `bash` tool, and the prompt

**Two tools, one effect (amends ADR-005 §2).** With the feature built
in, the toolcaller offers `run_cell(code)` and `bash(command,
as=None)`. The split is about generation, not architecture:

- The model is trained on a tool shaped `bash(command)` more heavily
  than on anything this repo could invent, and shell inside a Python
  string pays a second quoting layer (`"sed 's/\\t/ /'"`) whose
  every miss is a wasted turn. `bash` is the primary way to *run one
  command and read it*.
- `run_cell` stays the way to *process* output and to do several
  steps in one round-trip — the turn count is the dominant cost
  (every model turn re-reads the context), and `out = sh("rg -n TODO
  src").out; files = {…}` is one turn where a bash-only agent needs
  three. Python keeps the data role.

`bash` is **not a second executor**: the toolcaller runs it as a
subcell in the same kernel — `sh(command)` inside a `bash` span, so
the trace shows the command verbatim with its one `sh.run` effect —
and renders the result **raw**: stdout, then stderr, an `exit N` line
only when non-zero, head/tail truncation past the digest budget. No
JSON-escaped newlines (≈1.3× the tokens and harder to read).

**The output is already in the kernel.** Because `bash` runs in the
kernel namespace, its result is bound where the next `run_cell` can
reach it:

- `sh.last` — always rebound to the most recent `bash` result:
  `.out`, `.err`, `.code`, `.ok`, `.lines()`. Namespaced on the `sh`
  facade so it never collides with a name the model chose itself.
- `bash(command, as="tests")` — optional explicit binding, IPython's
  `x = !cmd`.
- The tool-result footer names the binding (`→ sh.last (also
  `tests`)`) so the model does not paste 200 lines of test output
  back into a cell. The bound object holds the **untruncated** output
  (up to §1's 1 MiB cap) even when the tool result showed head/tail —
  Python sees what the model did not.
- `values.get(<tool-use-id>)` keeps working as the durable route
  (ADR-005 §4); it is the fallback, not the pattern.

Typical flow: `bash("cargo test 2>&1 | tail -80")` → read → `run_cell`:
`fails = [l for l in sh.last.lines() if l.startswith("test ") and
"FAILED" in l]` — no re-run, no re-paste.

**In-cell surface, deliberately small.** Two namespace globals, `sh`
and `fs` (amends ADR-002 §3), added in `runtime/guest/app.py` beside
`http`; bound only when `runtime.get("shell")` resolves (§6). `sh` is
callable — `sh("cmd", cwd=None, timeout_s=None, stdin=None, env=None,
check=False)` returns the result object above (`check=True` raises on
non-zero exit); `sh.lines("cmd")` is the one-liner for
split-and-strip; `sh.run(...)` is the raw dict for programs that want
it; `sh.spawn/poll/kill` per §1. The result's `__repr__` prints stdout
raw with an exit line, so a cell ending in `sh("git status")` reads
like a terminal. No shell DSL (`sh.git("status")`-style argv builders
or a plumbum-like pipe algebra): models write orders of magnitude
less of it than raw bash, pipes get awkward, and every command
becomes a translation step. Docstrings are the doc (ADR-010):
`help(sh)` / `help(fs.edit)`.

- `runtime.get("shell")` (ADR-006 §3 surface) → `{cwd, home, shell,
  os}` — where bao is, so the first cell doesn't have to probe with
  `pwd`. Absent (KeyError) in a binary without the feature (§6).
- **`_coding.md`, a new space-resident skill**, deployed like any
  other (`anyrt deploy`), composed in when the binary has the
  feature (§6). It carries the workflow, not the API: `bash` to run
  and read one command, `run_cell` when you will process the output
  or need several steps, `sh.last` instead of re-pasting; absolute
  paths; read before you edit; `fs.edit` over rewriting a file; run
  the project's tests after a change and read the failure; bounded
  output (`| head`, `rg` before `cat`); stage by explicit path, never
  `git add -A`; never push, force, reset, or delete outside the
  project without the user asking in that conversation; report a
  failing test as failing. One nudge for later tooling: long-running
  or interactive work (dev servers, REPLs, ssh) belongs in a `tmux`
  session the user can attach to, driven through `bash` — tmux
  itself is not an effect (a connector over `~/code/tmux-http` is a
  possible follow-up, not this ADR). A bao that should not code
  drops the skill from its space — prompt-side, no runtime switch.
- The `_core` reply/cell discipline is unchanged: cell values land in
  the digest, large values stub to `values.get`, `print()` stays the
  model-facing channel.
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

### 6. Build variants: a cargo feature, one kernel

Shell effects are a **compile-time feature of the runtime crate**,
off by default:

- `runtime/Cargo.toml`: `[features] shell = []`. The `sh.*`/`fs.*`
  syscalls, process management (§1), and the `runtime.get("shell")`
  value are `#[cfg(feature = "shell")]`. Compiled out, the broker
  answers `unknown effect` — the code is not in the binary, so the
  raw `effect` global (ADR-002 §4 plumbing) cannot reach it either.
- `make runtime` builds without the feature (unchanged); `make
  runtime-shell` builds with it. `cargo test` and `runtime-check`
  run with `--features shell` so the syscalls are tested and linted.
- **any-ui is untouched.** `src-tauri` depends on `anyrt = { path =
  "../../anybao/runtime" }` with default features and builds the
  kernel with the plain componentize command; desktop builds never
  contain a shell unless that dependency opts in explicitly.
- **One kernel.** The guest (`app.py`) binds the `sh` and `fs`
  globals only when `runtime.get("shell")` resolves at boot; in a
  binary without the feature the names are absent from the
  namespace, `help()` does not list them, and the toolcaller leaves
  `_coding.md` out of `compose_system` (the skill is space-resident
  either way; composing it in without the tools would be pure prompt
  tax). Two kernel artifacts were considered and rejected: the
  cargo feature would still have to select the file, so it is the
  root switch regardless, and any-ui's CI would need a second build
  command for nothing.

Why the kernel is not the switch: the kernel holds two-line wrappers
around `_effect("sh.run", …)`; the capability is the syscall in the
binary. A kernel without the wrappers hides the tool from the prompt,
not from the machine.

### 7. Out of scope

UI (diff/terminal rendering — chat stays as is), PTY/interactive
commands, background services outliving a run, parallel subagents,
running on a device other than the serve's, restrictions (§5), file
upload to `any` (ADR-020 covers download only).

## Consequences

- bao can read, edit, and run code on the machine its serve runs on
  through recorded, replayable effects. A coding conversation
  replays exactly like any other run.
- **Every serve built with `--features shell` has shell**, with no
  runtime opt-in (the earlier draft's `[host]`-block gate was that
  opt-in and is dropped on purpose); the opt-in is the build (§6).
  Desktop builds via any-ui never have it. What
  stands between a prod bao and `rm -rf` is the model's judgement
  and the skill text, and that is the accepted v1 posture (§5).
  Deploy order matters accordingly: the skill lands in the space
  before the binary that makes the tools real.
- The thin-host line moves from "network only" to "network + the
  serve's own device". The restrictions ADR draws the real line.
- Trace volume grows: shell output is bulkier than JSON. The 1 MiB
  caps + blob spill + ADR-023 retention bound it; the `_coding`
  skill pushes the model toward bounded commands.
- The toolcaller has two tools for the first time; the skill text
  carries the when-which rule (§4). Prompt tax ≈ one tool schema.
- Two new failure classes for the digest to render well:
  `timedOut` runs with partial output, and non-zero exits — both
  data, both shown, neither an exception (§1).
- Broker gains process management (spawn/poll/kill, process groups,
  run-end reaping) — the first non-request/response syscall.
  Cancellation (ADR-003 §2) gets a second thing to clean up.

## Resolved questions (acceptance 2026-08-31)

1. **Login shell.** `$SHELL -lc <cmd>` (fallback `/bin/sh -c`). The
   profile cost per call is accepted for v1; `durationMs` is in every
   record, so the cost is measurable from traces and a PATH snapshot
   at serve start is the fallback if it shows.
2. **`fs.edit` is exact-match only.** Zero or several occurrences is
   a typed failure with no write; the model re-reads and retries.
3. **Per-stream cap starts at 1 MiB**, explicitly unmeasured; the
   constant is one line and `truncated` in the record tells us when
   it bites.
4. **`sh.spawn/poll/kill` are not v1.** `sh.run` with `timeout_s`
   ships first; long-running and interactive work goes through tmux
   driven from `bash` (§4 nudge). The three syscalls stay specified
   in §1 and land as a later commit if traces show `run` is not
   enough — nothing else in this ADR depends on them.

## Implementation sketch (after acceptance)

One topic per commit, on `feat/adr-024-shell-effects`:

1. `runtime/Cargo.toml` `shell` feature + Makefile `runtime-shell`;
   `broker.rs`: `sh.run` + `fs.read/list/write/edit` syscalls under
   the feature, output caps, process-group kill on timeout; unit tests against a
   temp dir; replay test (a `sh.run` record replays without
   executing).
2. `runner.rs`/`serve.rs`: child reaping on hard break, cell
   timeout, and run end (ADR-003 §2 amendment lands here).
3. `runtime/guest/app.py`: `sh`, `fs` globals with docstrings,
   bound only when `runtime.get("shell")` resolves; guest-module
   tests with the fake `effect`.
4. `toolcaller@v1`: the `bash` tool (subcell + raw rendering +
   `sh.last`/`as=` binding, ADR-005 §2 amendment), offered only when
   `runtime.get("shell")` resolves; `repos/_agent/skills/_coding.md`
   composed in under the same condition.
5. `sh.spawn/poll/kill` (if Q4 says yes).
6. Rig e2e on the prod-test serve: a conversation that clones or
   opens a repo, reads, edits, runs tests, and commits; trace review
   per `docs/debugging.md`.
