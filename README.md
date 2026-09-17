# anybao

A personal AI agent that lives inside your  [`any`](https://github.com/anyproto/any) spaces,
and [`anyrt`](./runtime), the runtime that runs it.

- local-first: chat, memory, config, triggers and the agent's own code
  are objects in an end-to-end encrypted space that syncs between your
  devices; the agent process holds no state of its own
- sandboxed: agent code runs as CPython compiled to wasm inside
  [wasmtime](https://wasmtime.dev), with no sockets, no filesystem, a
  virtual clock and a fuel budget per cell; every side effect goes out
  through one host boundary that checks, executes and records it
- traceable: every run is an append-only effect log that replays
  bit-exact, drives mock runs, and answers "why did bao do that?"
- one tool: the model writes Python cells in a persistent kernel against
  the database, memory and connectors; docstrings are the documentation
- space-resident: programs and skills are deployed to a space and
  resolved live, so editing the agent is a deploy, never a restart; the
  agent can write programs for itself
- memory that does not depend on model initiative: background jobs
  extract, roll up, decay and link facts; recall is injected into every
  conversation
- runs on its own schedule: cron and event triggers are objects in the
  space; with several devices online an election picks the one that
  answers
- model-agnostic: Anthropic and any OpenAI-compatible backend
  (OpenRouter, DeepSeek, Ollama, vLLM, ...) through per-model profiles;
  switching is a config row
- credentials never reach the model: the host injects keys after the
  trace record is written; OAuth tokens are host-held
- connectors for GitHub, Linear, Gmail, Google Calendar/Drive/Sheets,
  Granola, Attio, Figma and Intercom, written as guest programs
- embeddable: `anyrt` is a Rust library with a thin CLI; the
  [any-ui](https://github.com/anyproto/any-ui) desktop app runs it
  in-process

`any` runs a local database server on each of your devices. `anyrt
serve` connects to it as a client, watches the chat in your `bao` space,
answers, and runs scheduled and event-driven jobs. Everything the agent
knows and everything it is made of lives in the database next to your
data, so stopping the process on one device and starting it on another
picks up the same conversation, memory and schedule.

The agent's code is not trusted with the machine. It runs inside a wasm
cage whose only exit is one host function that hands a named effect
(`http.get`, `llm.chat`, `any.query`, ...) to a broker. The broker
checks the effect, executes it, and appends it to the run's trace with
its inputs, outputs, timing and read/mutate class. A trace replays a
run bit-exact, feeds a mock run ("same effects, edited code", or "mock
the third-party API, keep everything else live"), and carries the
per-turn token and cost stats:

```sh
anyrt trace ls --program toolcaller      # conversations, newest first
anyrt trace show run_<id>                # turns, cells, effects, results
anyrt trace show run_<id> --stats        # tokens, cache hits, cost per turn
anyrt replay run_<id>                    # strict replay; a divergence is the finding
```

The model gets a single tool, `run_cell`, and writes Python against
facades it pulls in with `use("any@v1")`, `use("memory@v1")` or a
connector, in a kernel that keeps variables, helpers and imports alive
across cells. Large results come back as short stubs the model can
drill into, so context stays small. `help(http.get)` prints the real
docstring: the code is the documentation.

The shipped agent is one repo of programs and skills, published to a
space your account joins read-only. Your own space can shadow any unit
by name, and bao can author programs into it. Every contract behind
this is an accepted ADR in [`docs/adr/`](docs/adr/README.md); no code
lands ahead of its ADR.

> [!WARNING]
> **Alpha software.** Data shapes change without migration, and the
> local `any` server trusts every process on your machine. Use a
> dedicated account for experiments.

## Getting started

You need two processes: an `any` server (your account and spaces) and
`anyrt serve` (the agent). Both stay on `127.0.0.1`.

### 1. Run `any` and sign in

Build the `any` binary from the
[`any` repository](https://github.com/anyproto/any) (its README has
the build steps). Then create an account and start the server:

```sh
any init      # creates ~/.any and a fresh account; prints the mnemonic ONCE
any run       # foreground server on http://127.0.0.1:7001
```

Save the mnemonic somewhere safe: it is the account. A fresh data
directory has no account, so `any run` without a prior `any init` starts
unauthorized and every data route answers `401 auth.required` until you
sign in from another shell:

```sh
any auth login                      # generate a new account in place
any auth login --mnemonic-stdin     # or restore an existing one (paste the phrase)
any auth status
```

To add a second device later, restore from the mnemonic on that device.
Never copy `wallet.key` between machines.

### 2. Build anyrt

The canonical environment is the nix dev shell (`nix develop`, or
`direnv allow`). Without nix you need a recent stable Rust, Python 3.13,
and [`uv`](https://docs.astral.sh/uv/).

```sh
uv sync
make kernel     # componentized CPython guest -> bin/kernel.wasm
make runtime    # runtime/target/release/anyrt (embeds the kernel)
```

### 3. Give bao a model key

Secrets never go in the config file. Put a dotenv-style
`.connectors.env` next to `anybao.toml` (it is gitignored); serve seeds
it into the account-scoped secrets store on every boot:

```
llm.key.anthropic=sk-ant-...
```

Use an Anthropic key scoped to a single workspace (Console > Settings >
API keys > Create key > choose a workspace). Other providers and how to
pick a model per tier: [`docs/llm-models.md`](docs/llm-models.md).
Connector keys (`connector.key.github=...`, `connector.key.linear=...`)
go in the same file. Details: [`docs/config-secrets.md`](docs/config-secrets.md).

### 4. Start the agent

```sh
./runtime/target/release/anyrt serve
```

The committed [`anybao.toml`](anybao.toml) points at the local server
and at the published bao repos (the agent and the connectors). On first
boot serve creates your `bao` working space, joins both repo spaces
read-only through their public guest keys, and waits for them to sync.
Watch the log for:

```
anyrt serving space=<bao space id> chat=<chat id> control=127.0.0.1:7010
overlays synced — agent ready
```

A message sent before the sync lands gets a "Not ready yet" status
reply instead of an answer.

### 5. Talk to bao

The `bao` space has one chat. Its ids are in the `anyrt serving` line
above:

```sh
any chat send <bao space id> <chat id> --text "hi, what can you do?"
any chat list <bao space id> <chat id> --limit 5
```

Then read the run it produced:

```sh
./runtime/target/release/anyrt trace ls --program toolcaller
./runtime/target/release/anyrt trace show run_<id>
```

## Running your own agent code

The agent is just a repo folder: [`repos/_agent`](repos/_agent)
(the conversation loop, tool and cron programs, skills) and
[`repos/_connectors`](repos/_connectors) (GitHub, Linear, Gmail, Google
Calendar/Drive/Sheets, Granola, Attio, Figma, Intercom). To run a
modified copy, publish it to a space you own and point the overlay at
it:

```sh
REPO=$(curl -s -X POST http://127.0.0.1:7001/v1/spaces -d '{"name":"_agentrepo"}' | jq -r .id)
./runtime/target/release/anyrt deploy --source repos/_agent --target $REPO
```

```toml
# anybao.toml
[overlays]
agent = "<that space id>"        # your own space: no invite needed
```

Deploys are hash-gated and picked up by a running serve on its next
conversation. Restart only for anyrt binary or kernel changes.
Authoring rules for programs and skills: [`repos/CLAUDE.md`](repos/CLAUDE.md).

## Embedding the runtime

`anyrt` is a library with a thin CLI on top. A Rust app can run the
agent in-process, with the kernel compiled into the crate (build
`make kernel` before the consumer's cargo build):

```rust
let mut cfg = anyrt::Config::builder()
    .addr("http://127.0.0.1:7001")
    .agent_space("bao")
    .overlay_with_invite("agent", "<repo space id>", "<invite token>")
    .build();
anyrt::config::bootstrap(&mut cfg);
let agent = anyrt::serve::start(cfg)?;   // non-blocking
// ... agent.stop() on shutdown
```

## Commands

| command | what it does |
|---|---|
| `anyrt serve` | run the agent: watch the chat, run conversations and triggers |
| `anyrt deploy --source <repo> --target <space\|overlay>` | publish `programs/`, `skills/`, `README.md` to a space, hash-gated |
| `anyrt run <name@vN>` | run one guest program once (dev loop; `--from-space` runs the deployed form) |
| `anyrt trace ls\|show\|follow` | list, render, or tail runs from the server's local store |
| `anyrt replay run_<id>` | strict replay of a recorded run |
| `anyrt drift` | check the vendored `any` OpenAPI pin against the coverage manifest |

`make runtime-shell` builds a variant with shell and filesystem effects
and a `bash` tool for coding tasks. It is off by default.

## Develop

```sh
make test       # kernel + cargo tests (shell feature) + pytest
make lint       # clippy -D warnings, fmt --check, ruff
```

Read [`docs/adr/README.md`](docs/adr/README.md) first: it is the index
of every contract and the working rules. Then the ADR for whatever you
are touching, and [`docs/debugging.md`](docs/debugging.md) for reading
a trace. Point-in-time notes and runbooks live under `docs/`.

## License

[MIT](LICENSE)
