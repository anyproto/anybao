# Anybao

**Anybao is a malleable agent harness that can rewrite its own programs while it
runs.** Like Emacs, Bao lets you change how it works while you’re using it. With
Bao, you can describe the change you want and ask the agent to write it.

Ask Bao to collect your open GitHub issues every Monday. Later, tell it to group
them by project and flag the ones that need a reply. Bao can update the same
program, save it in your space, and use it on the next run.

That makes your context programmable. The program can fetch, filter, and group
your data before the model sees it. The same rules run each week, and only the
details that need the model’s attention enter its context. That makes recurring
work more predictable and can reduce token use.

Bao uses [`any`](https://github.com/anyproto/any) as its local-first database.
Your space is the workspace you share with Bao. Conversations, memory,
configuration, triggers, and the agent’s own code are objects in an end-to-end
encrypted space that syncs across your devices. The agent process holds no
durable state of its own. You can stop it on one device and pick up the same
conversation, code, and schedule on another.

## How it works

**Embeddable runtime.** [`anyrt`](./runtime) is a Rust library with a thin CLI.
The [`any-ui`](https://github.com/anyproto/any-ui) desktop app runs it
in-process. From the CLI, `anyrt serve` connects to the local `any` database,
watches conversations, and runs scheduled and event-driven jobs.

**Programs and skills live in your space.** Bao loads its defaults from a
read-only space. Your own space can override any program or skill by name, and
Bao can write those overrides itself. The running agent picks up deployed
changes without a restart.

**One tool for the model.** The model calls `run_cell` to write Python in a
persistent kernel. Variables, helpers, and imports carry across cells. Calls
such as `use("any@v1")` and `use("memory@v1")` load programs that give it
access to the database and memory. Large results come back as compact
references the model can explore. Docstrings are the documentation, so
`help(http.get)` describes the function the model actually calls.

**Sandboxed execution.** Agent code runs as CPython compiled to WebAssembly
inside [Wasmtime](https://wasmtime.dev/). It has no direct access to sockets
or the host filesystem, uses a virtual clock, and gets a fuel budget per cell.
Every external action passes through one host boundary that checks, executes,
and records it. The host injects API keys after writing the trace record and
retains OAuth tokens. Neither enters the model’s context.

**Traceable runs.** Bao records each run as an append-only log of effects in
`any`. Run summaries sync across devices. Detailed traces stay on the device
that ran the job. You can ask “Why did Bao do that?”, replay recorded execution
bit for bit, or test edited code against recorded or mock responses.
`anyrt trace show run_<id> --stats` shows token use and cost per turn.
[Guide to inspecting runs](docs/debugging.md)

**Built-in memory.** Background jobs extract and link facts, consolidate related
memories, and age out stale ones. Bao recalls relevant context automatically
for every conversation.

**Schedules and events.** Cron schedules and event triggers are objects in your
space. When several devices are online, they elect one to handle the job.

**Composable connectors.** Connectors are guest programs Bao can combine with
its own code. They cover GitHub, Linear, Gmail, Google Calendar, Drive, Sheets,
Granola, Attio, Figma, and Intercom.

**Model choice.** Bao supports Anthropic and OpenAI-compatible backends,
including OpenRouter, DeepSeek, Ollama, and vLLM. Each model has a profile,
and you switch models by changing a configuration row.

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
`direnv allow`). Without nix you need a recent stable Rust, Python 3.14,
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
To read, change and test those rows from outside the app (a coding
agent, a script): [`docs/skill-bao-config.md`](docs/skill-bao-config.md).
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

Anybao and anyrt are licensed under the [MIT License](LICENSE).
Third-party components retain their respective licenses; see
[Third-party notices](THIRD_PARTY_NOTICES.md).
