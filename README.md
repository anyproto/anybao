# anybao

Agent harness + isolated runtime on top of [`any`](../any). The respawn of
the bobrik harness — design and rationale in [`docs/00-plan.md`](docs/00-plan.md).

## Layout

- `runtime/` — **anyrt**: effect boundary, trace/replay, cell executor,
  space module loader. The contract programs are written against.
- `harness/` — **anybao**: toolcaller loop, triggers, history/memory,
  `any` HTTP client. Imports anyrt, never the other way.
- `programs/`, `skills/` — space-resident units (1 file = 1 Any object).
- `docs/adr/` — decision records. **No code lands ahead of its accepted ADR.**

## Dev environment

Nix flake is canonical (uv + pinned python inside):

```
nix develop          # or direnv allow
uv sync
uv run pytest
uv run ruff check .
```
