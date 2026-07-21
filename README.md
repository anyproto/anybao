# anybao

A space-resident AI agent and its runtime, **anyrt**. The agent — its
programs, skills, memory, chat — lives in [`any`](../any) spaces; the
runtime is a Rust host that cages a componentized CPython guest in wasm
and lets nothing escape except through a recorded effect boundary.
Every run writes a trace; a trace replays bit-exact. The respawn of the
bobrik harness — design and rationale in
[`docs/00-plan.md`](docs/00-plan.md).

## Two modes

**Embedded (Rust library)** — apps (e.g. the any-ui desktop app) run
the agent in-process. The wasm kernel is compiled into the crate — no
assets to ship:

```rust
let mut cfg = anyrt::Config::builder()
    .addr("http://127.0.0.1:7001")
    .agent_space("bao")
    .overlay_with_invite("agent", "<repoSpaceId>", "<inviteToken>")
    .build();
anyrt::config::bootstrap(&mut cfg);       // llm-tier defaults + env keys
let agent = anyrt::serve::start(cfg)?;    // non-blocking; threads inside
// … agent.stop() on shutdown
```

Logs ride `tracing` — install a subscriber or get silence.

**Standalone (`anyrt serve`)** — the same agent as a process next to an
any server, configured by `anybao.toml`:

```toml
addr = "http://127.0.0.1:7001"

[agent]
space = "bao"               # working space: chat, memory, your edits

[overlays]                  # program repos (values are space IDS);
agent = { space = "bafy…", invite = "…" }   # invite ⇒ join on boot

[paths]
traces = "traces"
```

## Operations

| command | what it does |
|---|---|
| `anyrt serve` | run the agent: watch the chat, run conversations + cron triggers |
| `anyrt deploy --source . --target <space\|overlay>` | publish a repo folder (`programs/`, `skills/`, `README.md`) to a space, hash-gated |
| `anyrt run <name@vN>` | run one guest program from the local dir (offline dev) |
| `anyrt trace ls --program toolcaller` | list runs, newest first |
| `anyrt trace show <run_id>` | render one run: turns, cells, effects (`--stats`, `--seq N`) |
| `anyrt trace follow` | live-render the newest run as records land |

Programs and skills load from **spaces, not the filesystem** — `deploy`
is the only publish step; a running serve picks changes up on its next
conversation. The agent's code comes from the `agent` overlay (a repo
space joined read-only); your own space can shadow it by name
(ADR-004/ADR-009).

## Build

```
nix develop           # canonical env (or: direnv allow)
uv sync
make kernel           # componentized CPython → bin/kernel.wasm —
                      #   REQUIRED before any cargo build: the kernel
                      #   embeds into the binary/lib (ADR-009 §4)
make runtime          # runtime/target/release/anyrt
make test             # kernel + cargo tests + pytest
make lint             # clippy -D warnings + fmt + ruff
```

## Docs

- [`docs/adr/README.md`](docs/adr/README.md) — the ADR index: every
  contract + the working rules (**no code lands ahead of its accepted
  ADR**). Start with
  [ADR-009](docs/adr/009-space-resident-assets.md) for config,
  overlays, kernel embedding, lib mode.
- [`docs/debugging.md`](docs/debugging.md) — "why did bao do that?":
  trace analysis.
- [`docs/repo-overlay-e2e.md`](docs/repo-overlay-e2e.md) — two-account
  setup: a repo account publishes code, a user account joins read-only.
- [`docs/config-secrets.md`](docs/config-secrets.md) — API keys: seeded
  once (env), persisted device-local in the space, never synced.
