# ADR-024: Host effects — `sh.*` and `fs.*` on the serve's device

Status: **Proposed**
Date: 2026-08-31
Builds on: ADR-002 (effect boundary: declaration, broker pipeline,
deny-by-default namespace), ADR-001 §7 (blob spill), ADR-003 §2
(cell cancellation: epoch deadline, fuel, hard break), ADR-009
(host config), ADR-010 (docstrings as the doc surface), ADR-015
(active-instance election)
Amends when accepted: ADR-002 §3 (two new namespace globals), ADR-003
§2 (cancellation reaches child processes), ADR-009 (a `[host]` config
block)
Deferred, not decided here: capability grants + consent for host
effects (`caps.rs` wiring — see §6)

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
- **Something to test first.** The grant/consent policy the plan
  designed (`caps.rs`: `GrantSet`, hash-keyed `GrantLedger`,
  `decide` with a chat consent prompt) is still dead code; wiring it
  is real design work and is explicitly *not* in this ADR. This ADR
  ships the mechanism with one blunt guardrail so the loop can be
  exercised end to end.

Two prior positions this ADR has to meet honestly:

- **The thin-host rule** (00-plan §5, m-notes 2026-07-08: "never add
  host effects") already bent for `oauth.*` (ADR-011) and `trace.*`
  (ADR-023) — syscalls the guest structurally cannot implement. `sh`
  and `fs` are the same class, with one difference worth stating:
  they are the first effects that reach the *local machine* rather
  than the network. The isolation principle still holds — every call
  is declared, classified, recorded, and replayable from the trace —
  but *confinement of the child process* is policy, and v1's policy
  is thin (§3).
- **Staging changes enforcement strength, never the principle**
  (00-plan §5). v1 here is the "honest code" stage: the API refuses
  what it can see (paths outside the workspace); it does not sandbox
  what a shell command does once it runs. The adversarial stage
  (sandbox-exec / landlock, per-command consent) is the caps
  follow-up.

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
- **`cmd` is one string, run by the configured shell** (§4:
  `[host] shell`, default `/bin/sh -c`). No argv form: the model
  writes pipelines and quoting exactly as it would in a terminal,
  and the trace shows the literal line the user would have typed.
- **`cwd`** is resolved per §3; default = the workspace root. A cell
  that wants "the current directory" keeps its own `cwd` variable —
  there is no ambient cwd across calls (nothing ambient, ADR-002).
- **Timeout is data, not an error.** `timeout_s` defaults to 120 and
  is clamped to the cell's remaining wall budget (ADR-003 §2). On
  expiry the host kills the process *group*, returns whatever was
  captured, and sets `timedOut: true`, `exit: null`. A non-zero exit
  is likewise data (`exit: 1`, stderr present) — the cell decides;
  `EffectError` is reserved for the boundary refusing the call
  (§3 denials, unavailable host §5, malformed payload).
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
- **Environment is an allowlist, never inheritance.** The child gets
  `PATH HOME USER SHELL LANG LC_* TZ TMPDIR` from the serve process,
  plus `[host] env_passthrough` names, plus the call's `env` map —
  and nothing else. The serve's own environment (however the desktop
  app launched it) never reaches a child by default; secrets are
  store rows (ADR-021 §4), never env, and `env.get` today serves an
  empty map — this keeps that true one level down. Per-call `env`
  values are recorded in the trace like any payload; the standard
  `redact` paths apply (`env.*TOKEN*`, `env.*KEY*`, `env.*SECRET*`).

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

### 3. Confinement v1: the workspace root

The single guardrail this ADR ships:

- **Every path and every `cwd` resolves under the workspace root**
  (`[host] workspace`, §4). Relative paths resolve against it;
  absolute paths are canonicalized (symlinks followed) and must stay
  inside it; anything else is a boundary refusal recorded as an
  error record `host.outside_workspace` — the same shape as a
  capability denial (ADR-002 §2), so the audit trail is the trace.
  `[host] roots = [...]` may list additional allowed roots (a
  sibling repo, a scratch dir).
- **This confines the API, not the process.** A command can `cd /`
  or `rm -rf ~` once it runs; v1 does not pretend otherwise. It is
  the honest-code stage: accidental escapes are impossible, hostile
  ones are not prevented. The `_coding` skill (§5) states the
  workspace rule to the model; the trace records every command.
- **No grants, no consent** in v1. `cap_of` returns `host.shell` /
  `host.fs.*` so the caps names are fixed now and the ledger has
  something to key on later; with no grant policy wired, every cap
  is permitted, as it is for every other effect today. Wiring
  `caps.rs` (per-program grants, per-command approval via the
  ADR-021 request-in-chat pattern, a sandbox for the child) is the
  next ADR, after this mechanism has been exercised.

### 4. Host config: the `[host]` block (amends ADR-009)

Host effects are **device-local**, so their config lives in the
device's toml, not in space config (`agent_config` rows sync across
devices; a workspace path does not):

```toml
[host]
workspace = "/Users/me/src/project"   # required to enable sh.*/fs.*
roots = ["/Users/me/src/other"]       # optional extra allowed roots
shell = "/bin/sh"                     # optional; invoked as `<shell> -c <cmd>`
env_passthrough = ["CARGO_HOME"]      # optional extra env names
```

- **No `[host]` block = host effects off.** The broker refuses
  `sh.*`/`fs.*` with a typed failure `host.unavailable` naming the
  device. Existing rigs and prod are unaffected until someone adds
  the block.
- `runtime.get` (ADR-006 §3 surface) gains `host: {workspace, roots,
  enabled}` so the guest can tell — and the toolcaller can compose
  the prompt accordingly (§5) — without a failed probe call.
- A `[host]` block on a serve that is election-standby (ADR-015) is
  fine: standby runs nothing anyway.

### 5. Guest surface and prompt

- Two namespace globals, `sh` and `fs` (amends ADR-002 §3), added in
  `runtime/guest/app.py` beside `http`. Docstrings are the doc
  (ADR-010): `help(sh)` / `help(fs.edit)` show signatures, return
  shapes, the workspace rule, and the timeout/truncation contract.
  `sh.run` is the only thing most cells need; the docstring says so.
- **`_coding.md`, a new space-resident skill**, included in
  `compose_system` only when `runtime.get().host.enabled` — a bao
  without a workspace never pays prompt tax for tools it lacks. It
  carries the workflow, not the API: read before you edit; `fs.edit`
  over rewriting a file; run the project's tests after a change and
  read the failure; bounded output (`| head`, `rg` before `cat`);
  stage by explicit path, never `git add -A`; never push, force,
  reset, or delete outside the workspace without the user asking
  in that conversation; report a failing test as failing.
- The `_core` reply/cell discipline is unchanged: shell output lands
  in the digest like any value, large values stub to `values.get`,
  `print()` stays the model-facing channel.
- Model tier is config, not this ADR: `llm.tier.codegen` defaults to
  `claude-sonnet-5` (`config_defaults.json`); a coding bao flips the
  row.

### 6. Device binding: fail loudly, route later

The workspace exists on one device. Under ADR-015 the *active*
instance may be another device (the laptop that also runs bao). v1:
only the device with the repo carries a `[host]` block; a coding
request that lands on a device without one fails with
`host.unavailable` and bao says which device it is on. Routing a
conversation to the device that owns the workspace — device-pinning
a chat the way ADR-006 §4 pins triggers — is deferred with the caps
work; the failure is loud, recorded, and cheap to hit.

### 7. Out of scope

UI (diff/terminal rendering — chat stays as is), PTY/interactive
commands, background services outliving a run, parallel subagents,
remote execution (a workspace on a device other than the serve's),
grants/consent/sandboxing (§3, next ADR), and file upload to `any`
(ADR-020 covers download only).

## Consequences

- bao can read, edit, and run a project on the machine its serve
  runs on — the desktop app's mac — through recorded, replayable
  effects. A coding conversation replays exactly like any other run:
  every command, exit code, and diff is in the trace; `anyrt trace
  show` reads a coding session as it reads a connector run.
- The thin-host line moves from "network only" to "network + the
  serve's own device inside a declared root". That is a policy line
  the caps ADR has to draw properly; this ADR's guardrail is a
  deliberate placeholder and says so.
- Trace volume grows: shell output is bulkier than JSON. The
  1 MiB caps + blob spill + ADR-023 retention bound it; the
  `_coding` skill pushes the model toward bounded commands.
- Two new failure classes for the digest to render well:
  `timedOut` runs with partial output, and non-zero exits — both
  data, both shown, neither an exception (§1).
- Broker gains process management (spawn/poll/kill, process groups,
  run-end reaping) — the first non-request/response syscall.
  Cancellation (ADR-003 §2) gets a second thing to clean up.

## Open questions (to settle before Accepted)

1. **Shell invocation.** `/bin/sh -c` is portable but the user's
   PATH (nix, uv, cargo) lives in a login shell. Options: `[host]
   shell = "/bin/zsh"` invoked `-lc` (profile cost per call, tens of
   ms); or resolve PATH once at serve start from a login shell and
   pass it through. Leaning: `-lc` with the configured shell, `PATH`
   snapshot as the fallback when `shell` is unset.
2. **`fs.edit` whitespace tolerance.** Exact match only (simple,
   trace-honest) vs. a normalized fallback when the exact match
   fails. Leaning: exact only; the model re-reads and retries — the
   digest makes that cheap.
3. **Per-stream cap** — 1 MiB is a guess; ADR-003's numbers were
   measured before being fixed. Measure a `cargo test` and a
   `pytest -v` on this repo before the constant lands.
4. **Does `sh.poll` need a `wait_s` at all**, or is `sh.run` with
   a long `timeout_s` plus the cell's own budget enough for v1?
   Spawn/poll can be the second commit if `run` proves sufficient
   to test with.

## Implementation sketch (after acceptance)

One topic per commit, on `feat/adr-024-host-effects`:

1. `broker.rs`: `sh.run` + `fs.read/list/write/edit` syscalls,
   `[host]` config + workspace resolution + `host.unavailable` /
   `host.outside_workspace` refusals, env allowlist, output caps,
   process-group kill on timeout; unit tests against a temp
   workspace; replay test (a `sh.run` record replays without
   executing).
2. `runner.rs`/`serve.rs`: child reaping on hard break, cell
   timeout, and run end (ADR-003 §2 amendment lands here).
3. `runtime/guest/app.py`: `sh`, `fs` globals with docstrings;
   `runtime.get().host`; guest-module tests with the fake `effect`.
4. `repos/_agent/skills/_coding.md` + toolcaller gating on
   `host.enabled`.
5. `sh.spawn/poll/kill` (if Q4 says yes).
6. Rig e2e: a `[host]` block on the prod-test toml pointed at a
   scratch clone; a conversation that reads, edits, runs tests, and
   commits; trace review per `docs/debugging.md`.
