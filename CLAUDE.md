# CLAUDE.md

anybao — the Rust runtime (`runtime/`, binary `anyrt`: effect boundary,
trace/replay, cell executor, `any` client, deploy, the agent loop +
triggers) plus the componentized CPython guest kernel it runs, on top of
the `any` server (`~/any/any`). The agent itself lives in
`repos/_agent/` (programs + skills — space-resident guest units;
serve is space-only — `anyrt deploy --source repos/_agent --target
<space|overlay>` publishes, ADR-009); the service connectors in
`repos/_connectors/` (see `repos/CLAUDE.md`). The respawn of the
bobrik harness.

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
- **Guest never imports the host**: repo `programs/` are import-free sources
  exec'd in the wasm kernel; they reach the runtime only through the
  effect boundary + `use()`.
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
cargo test --manifest-path runtime/Cargo.toml   # runtime unit + replay tests
make runtime           # runtime/target/release/anyrt (rt_e2e needs it, else skips)
uv run pytest          # guest-module + wire tests; rt_e2e skips w/o kernel+binary
uv run ruff check .
```

`make test` chains kernel + cargo test + pytest; `make lint` = ruff +
runtime-check (clippy -D warnings + fmt --check). CI runs these through
the flake.

Gotcha: `tests/fixtures/*.jsonl` are JSONL — one record per line is the
parse contract. View pretty with `jq . <file>`; never reformat the
buffer (a saved pretty-print breaks the parse).

## Local configs + data dirs (gitignored)

Host configs live in `configs/` — `anybao.toml` (prod),
`anybao.prod.test.toml`, `anybao.staging.toml`, plus the
`.connectors.env*` files anyrt reads *beside the config file*. Nothing
sits at the repo root any more, so `anyrt`'s default `./anybao.toml`
lookup no longer resolves: pass `--config-file configs/<name>.toml`
every time, prod included.

`any`-server data dirs live in `~/any/any/datadirs/` (also gitignored):
`repo-prod` (:7003 repo account), `prod-test-user` (:7005),
`staging-user` (:7134), `staging-repo` (:7021). The `any` configs are
in `~/any/any/configs/` — `any-config.yml` (prod network) and
`any-config-staging.yml` (staging); `staging.yml` stays at that repo's
root because its e2e tests look for it there.

## Deploying to the PROD repo spaces

The prod `_agentrepo` / `_connectorsrepo` spaces are owned by a
dedicated **repo account** whose server runs locally at
`http://127.0.0.1:7003` (data dir `~/any/any/datadirs/repo-prod`, prod network —
full bring-up runbook: `docs/prod-repo-account.md`). The
`configs/anybao.toml` account only *joins* them as guest — `anyrt
deploy --target agent` through it 403s (`space.read_only`). Deploy via
the repo server, addressing the space by raw id (the ids live in
`configs/anybao.toml [overlays]` and in the runbook):

```
anyrt deploy --addr http://127.0.0.1:7003 --source repos/_agent \
  --target <agent space id>          # same for repos/_connectors
```

## Prod-network TEST environment (`configs/anybao.prod.test.toml`)

For exercising unreleased runtime/repo changes from real prod-network
clients (desktop app, browser UI) WITHOUT touching the prod overlays:
a throwaway user account (`~/any/any/datadirs/prod-test-user`, server
`127.0.0.1:7005`, control `7014`) runs a test bao against TEST copies
of the repo spaces — `_agentrepo-test` / `_connectorsrepo-test`, owned
by the same prod repo account (:7003) and joined via guest keys. The
staging rig (`configs/anybao.staging.toml`, server on the staging
network via `--config configs/any-config-staging.yml`) is unreachable
from prod clients; this one is not. Ids, commands and the browser
recipe live in the toml's header
and in `docs/prod-repo-account.md` § Test spaces. Deploy to it
through :7003 by raw id, exactly like prod:

```
anyrt deploy --addr http://127.0.0.1:7003 --source repos/_agent \
  --target bafyreigm425yqiipjfch27bfbtjufwvpaykhm2hyp4hyorrw3jrfg3qyai.36hw3g1lpszh6
```

## Skill: analyze a toolcaller run

"Why did bao do that?" — the whole user-message→final-reply exchange
is ONE `toolcaller@v1` trace (cron jobs — extraction/rollup/linkgen —
outnumber conversations ~25:1 in `traces/`, so always filter):

```
anyrt trace ls --program toolcaller   # conversations, newest first,
                                      # titled by the user message
anyrt trace show run_<id>             # the whole story: turns, cells,
                                      # effects (* = mutate), results
anyrt trace show run_<id> --stats     # per-turn tokens/cache/cost table
anyrt trace show run_<id> --seq 42    # drill into one record, full,
                                      # blob-resolved
```

`anyrt` = `runtime/target/release/anyrt` (`make runtime`). A chat
reply's `agent_turns` record carries `traceRef` = the run id. Full
guide: [`docs/debugging.md`](docs/debugging.md).
