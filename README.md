# anybao

Agent harness + isolated runtime on top of [`any`](../any). The respawn of
the bobrik harness — design and rationale in [`docs/00-plan.md`](docs/00-plan.md).

## Layout

- `runtime/` — **anyrt**: the Rust runtime (binary `anyrt`) — effect
  boundary, trace/replay, cell executor, `any` HTTP client, deploy +
  space module loader, the agent loop and triggers. Plus the
  componentized CPython guest (`runtime/guest` → `bin/kernel.wasm`), the
  sandbox programs run in.
- `programs/`, `skills/` — space-resident units (1 file = 1 Any object).
  Guest sources the runtime deploys and `use()`s; the contract they're
  written against.
- `docs/adr/` — decision records. **No code lands ahead of its accepted ADR.**

## Dev environment

Nix flake is canonical (uv + pinned python + Rust toolchain inside):

```
nix develop          # or direnv allow
uv sync
make kernel                                   # bin/kernel.wasm (gitignored)
cargo test --manifest-path runtime/Cargo.toml
uv run pytest                                 # guest-module + wire tests
uv run ruff check .
```
