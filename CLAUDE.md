# CLAUDE.md

anybao — agent harness (`harness/` pkg `anybao`) + isolated runtime
(`runtime/` pkg `anyrt`) on top of the `any` server (`~/any/any`).
The respawn of the bobrik harness.

## Read first, in this order

1. [`docs/adr/README.md`](docs/adr/README.md) — ADR index, working
   rules, **documentation tier rule**. Status of everything lives here.
2. The ADR for whatever you're touching (`docs/adr/00N-*.md`) — ADRs
   are the canonical why/contract; code carries `ADR-00N §M` pointers.
3. [`docs/01-implementation-plan.md`](docs/01-implementation-plan.md) —
   milestones + exit criteria; [`docs/m0-notes.md`](docs/m0-notes.md)
   etc. for point-in-time learnings.
4. [`docs/00-plan.md`](docs/00-plan.md) — the full analysis behind it
   all (also published in the foo space).

## Hard rules

- **No code ahead of its accepted ADR.** One topic = one commit.
  Milestones end with a user review gate — don't start the next one
  unprompted.
- **Implementation divergence from an ADR = amend the ADR in the same
  change.**
- **Isolation principle**: nothing executes side effects except through
  the effect boundary (broker). Everything nondeterministic is an
  effect. If it isn't in the trace, it didn't happen.
- **Import direction**: `anybao` → `anyrt`, never back.
- **`any`-server quirks are fixed upstream** (in `~/any/any` / SDK),
  never worked around here; a workaround is a dated bridge with an
  upstream ticket.
- **No backward compatibility** with bobrik-watch — fresh shapes,
  clean cut, no legacy-JS execution.

## Build / test

```
nix develop            # canonical env (or: direnv allow)
uv sync
make kernel            # componentized CPython guest -> bin/kernel.wasm (gitignored)
uv run pytest          # wasi tests skip if kernel.wasm missing
uv run ruff check .
UPDATE_GOLDEN=1 uv run pytest -k up_to_date   # regenerate golden fixture (review the diff!)
```

CI runs exactly these through the flake.

Gotcha: `harness/tests/fixtures/*.jsonl` are JSONL — one record per
line is the parse contract. View pretty with `jq . <file>`; never
reformat the buffer (a saved pretty-print breaks the golden tests).
