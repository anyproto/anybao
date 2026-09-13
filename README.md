# anybao

A space-resident AI agent and its runtime, **anyrt**. The agent — its
programs, skills, memory, chat — lives in [`any`](../any) spaces; the
runtime is a Rust host that cages a componentized CPython guest in wasm
and lets nothing escape except through a recorded effect boundary.
Every run writes a trace; a trace replays bit-exact. The respawn of the
bobrik harness — design and rationale in
[`docs/00-plan.md`](docs/00-plan.md).

## Two modes

**Embedded (Rust library)** — apps run the agent in-process. The
any-ui desktop (Tauri) app bundles anyrt exactly this way: a Cargo
path dep on `../anybao/runtime`, the wasm kernel compiled into the
crate — no agent binary, no asset tree to ship. Build order matters:
`make kernel` before any cargo build of a consumer:

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

[overlays]                  # program repos (values are space IDs);
                            # invite ⇒ join on boot. This is the
                            # staging-env agent repo space:
agent = { space = "bafyreibu6a7ewtk7t6lsnbmazazgcsfqx5efpmyycaycovbpzshvq2zrvy.1mmhvs7exubo9", invite = "2gChBtaWg5EX1PgV7SgXdJDtgszDPrSKcVmvvgUc1o8d16SRHrxMBkGyAQa2ZhUXua8r5gNF7bToC63m9yEGNkMfcXZCvimFtCgTqvAyZARnCWjEXvcYWQJ71ZibD6ZPZkNUghif5DHWJtLYawY2izi37Ng2MsGAwAzFcgASgzEbWW3uXMKemFcK5mRmZF7F6uh" }

[paths]
traces = "traces"           # raw-blob directory (ADR-026), relative to
                            # this file's dir (this is the default);
                            # traces themselves live in the any local store
```

## Operations

| command | what it does |
|---|---|
| `anyrt serve` | run the agent: watch the chat, run conversations + cron triggers |
| `anyrt deploy --source repos/_agent --target <space\|overlay>` | publish a repo folder (`programs/`, `skills/`, `README.md`) to a space, hash-gated |
| `anyrt run <name@vN>` | run one guest program from the local dir (dev; the trace lands in the space's local store on `--addr`) |
| `anyrt trace ls --program toolcaller` | list runs in a serve's local store, newest first (`--addr`, default `http://127.0.0.1:7001`) |
| `anyrt trace show <run_id>` | render one run: turns, cells, effects (`--stats`, `--seq N`) |
| `anyrt trace follow` | live-render the newest run as records land |

Programs and skills load from **spaces, not the filesystem** — `deploy`
is the only publish step; a running serve picks changes up on its next
conversation. The agent's code comes from the `agent` overlay (a repo
space joined read-only); your own space can shadow it by name
(ADR-004/ADR-009).

## Standalone stack: any + anyrt + browser UI

The dedicated (non-Tauri) way to run the whole thing — one any server,
one agent process, the UI in a browser:

```fish
# 1. the any server (needs a real nodeconf for sharing/guest joins —
#    without one it boots the sanitized embedded fallback and joins NO
#    network):
cd ~/any/any && ./bin/any run --config ./configs/any-config.yml   # 127.0.0.1:7001

# 2. the agent (this repo; anybao.toml carries space + agent overlay):
make runtime && ./runtime/target/release/anyrt serve
#   first run on a fresh space: ANTHROPIC_API_KEY=... anyrt serve
#   (key persists device-locally after — docs/config-secrets.md)

# 3. the UI, in a browser (any-ui repo): vite proxies /v1 to the server
cd ~/any/any-ui && pnpm dev            # VITE_API_TARGET overrides the
# open http://localhost:5173           # default http://127.0.0.1:7001
```

Browser mode has NO embedded agent — `anyrt serve` IS the agent. Don't
also run the desktop app against the same working space: two agents on
one chat means doubled replies.

**Prod-network test environment**: `configs/anybao.prod.test.toml` (untracked,
header documents everything) runs a test bao on a throwaway prod-network
account (`:7005`, control `:7014`) against `_agentrepo-test` /
`_connectorsrepo-test` — test copies of the repo spaces owned by the
prod repo account. Real prod clients can join it; deploy to it through
`:7003` by raw id. Browser: `VITE_API_TARGET=http://127.0.0.1:7005
VITE_ANYRT_TARGET=http://127.0.0.1:7014 pnpm dev`. See
the local `docs/environments.md`.

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
