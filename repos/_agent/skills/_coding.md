# Skill: _coding

Applies only where sh is bound in your cells. If sh is not a
global, this bao's runtime has NO shell access — never attempt shell
commands (no subprocess, no `effect("sh.run")`); say so instead.

Where it is bound, this bao runs on a real machine with a shell: the
bash tool and the sh / fs cell globals reach it (ADR-024). Where
you are is in Runtime context (shell: line) and on sh.cwd /
sh.home / sh.os. Nothing is confined — a command does what the
user can do; act like a careful colleague at their keyboard.

## Which tool

- **bash** — run one command and READ it: `git status`, `rg -n foo
  src`, `cargo test 2>&1 | tail -80`. Output comes back raw; the
  result is also sh.last (`.out`, `.err`, `.code`, `.ok`,
  .lines()) and `<as>` when you passed one.
- **run_cell** — PROCESS output or do several steps in one turn:
  `fails = [l for l in sh.last.lines() if "FAILED" in l]`, then act.
  Never paste command output back into a cell; it is already there.
  `sh("cmd")` inside a cell is the same call (`check=True` raises
  ShellError on non-zero); `sh.lines("cmd")` for a list.
- **fs.read(path, offset=, limit=)** to read a region, **fs.edit(path,
  old, new)** for an exact-match change (the trace records the diff —
  old must occur once, else widen it or `all=True`), **fs.write**
  for a whole new file. Searching, diffs, git stay in bash.
- **Bytes are a Blob.** `fs.read(path, encoding="blob")` is a binary
  file as a handle (no size cap) — pass it on to attach_file, an
  http `body=`, a File part; fs.write(path, blob_or_bytes) puts
  bytes on disk (an `http.get(url).blob` straight to a file). Never
  base64 a file through a cell.

## How to work

- **Absolute paths, always.** No working directory carries over
  between calls; pass `cwd=` or `cd /abs/path && …`.
- **Read before you edit.** fs.read the region (or `rg -n` for the
  spot) in this conversation before fs.edit; copy old exactly.
- **Bounded output.** `| head -100`, `| tail -80`, rg — never cat a
  big file or dump a whole tree; capture caps at 1 MiB per stream and
  the tool result shows head + tail past its budget.
- **Run the project's checks after a change** (its test runner,
  linter, build) and read the failure; a failing test is reported as
  failing, never hand-waved. Prefer the project's own tooling
  (make, `uv run`, cargo) over reimplementing it in a cell.
- **Exit codes and timeouts are data.** `[exit N]` / `[timed out]` in
  the result, `.code` / `.timed_out` on sh.last; default timeout is
  120 s, pass `timeout_s=` for a known-long build.
- **git discipline.** Stage by explicit path (`git add <files>`),
  never `git add -A`; commit only when asked. Never push, `--force`,
  `reset --hard`, `checkout -- .`, or delete files outside the project
  unless the user asked for exactly that in THIS conversation.
- **Long-running or interactive** (dev servers, REPLs, ssh, anything
  waiting on a prompt): nothing survives a bash call, so start it in
  tmux — `tmux new -d -s <name> '<cmd>'`, read with `tmux capture-pane
  -p -t <name>`, send keys with `tmux send-keys -t <name>` — and tell
  the user the session name; they can attach to it.
