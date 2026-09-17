# anyrt

The anybao runtime: a Rust crate that runs Python agent programs inside
a [wasmtime](https://wasmtime.dev) cage against a local
[`any`](https://github.com/anyproto/any) server. It ships as a library
(`anyrt`) with a thin CLI (`bin/anyrt`) on top. The crate carries no
product logic: the whole agent (the conversation loop, tools, cron
jobs, skills) is loaded from a space at run time.

- one guest kernel: CPython compiled to a wasm component, with a
  deny-by-default namespace, a curated import allowlist, no sockets, no
  filesystem, a virtual clock and a fuel budget per cell
- one way out: the `host-effect` import hands a named effect
  (`http.get`, `llm.chat`, `any.query`, ...) to the broker, which
  classifies it, checks its capability, consults replay or mock,
  executes it and records it
- one trace per run: an append-only effect log in execution order,
  stored in the `any` server's local store; strict replay and mock runs
  read it back
- programs, skills and config resolve from spaces (`[alias:]name@vN`),
  never from the filesystem of a running serve
- credentials are host-held and injected after the record is written;
  OAuth tokens never enter the guest
- `serve` is the agent: it watches a chat, runs conversations and
  cron/event triggers, and takes part in the multi-device election

## How a run works

1. `runner` instantiates the embedded kernel component in a fresh
   `Store` (one shared engine, one store per run) and calls `run-cell`
   with Python source.
2. The guest executes the cell in its persistent namespace. Anything
   that touches the world calls `host-effect(name, payload)`.
3. `broker` runs the effect through one pipeline: classify the route as
   read or mutate, derive the capability, check it against the run's
   grants, ask replay or mock for a canned reply, otherwise execute
   (`anyapi` for the server, `ureq` for HTTP, the LLM adapters, the
   blob store, the shell arm when compiled in), then append the record
   to the trace.
4. The reply crosses back as JSON. When the cell returns, the runner
   hands `{ok, prints, last, error}` to whoever asked: the CLI, the
   serve loop, or an embedder.

`anyrt replay run_<id>` runs the same program with every effect
answered from the recorded trace; the first divergence is the finding.
`anyrt run --mock` substitutes selected effects and keeps the rest live
(ADR-028).

## Layout

```
runtime/
  Cargo.toml         lib + bin, `shell` feature (off by default)
  wit/kernel.wit     the component world: host-effect in, run-cell / reset-ns out
  guest/app.py       the CPython kernel that becomes bin/kernel.wasm
  guest/VENDORED.md  pure-Python libraries bundled into the kernel
  src/lib.rs         public surface: Config, run_program, serve::start, replay
  src/main.rs        the clap CLI: run, replay, serve, deploy, trace, drift
  tests/             lib-mode smoke (embedder API) + every repo program validated
```

Modules in `src/`, grouped by role:

| role | modules |
|---|---|
| cage + effects | `runner` (engine, store, fuel, interrupt), `broker` (the effect pipeline), `caps` (grant policy), `routes` (read/mutate classification), `shell` (sh/fs syscalls, feature-gated) |
| trace | `trace` (format v2 records), `tracestore` (the one persistence seam), `replay`, `view` (`trace show`), `stats` (`trace stats`), `blob` (content-addressed bytes beside the trace) |
| server client | `anyapi` (typed blocking client, injectable transport), `drift` (OpenAPI pin vs coverage manifest) |
| agent host | `serve` (chat watcher, conversations, readiness), `triggers` (cron/event scheduler), `election` (active device), `oauth` (host-held tokens), `program_schema` (the `program` type) |
| assets | `config` (`anybao.toml`, builder, secrets), `resolver` (`name@vN` from spaces or a local dir), `deploy` (hash-gated publisher) |

Every module header names the ADR section it implements; the ADRs in
[`../docs/adr/`](../docs/adr/README.md) are the contract.

## Build

The kernel must exist before cargo runs: `runner.rs` embeds
`../bin/kernel.wasm` with `include_bytes!`, so one `anyrt` binary is a
single artifact.

```sh
make kernel                                   # componentize-py → bin/kernel.wasm
cargo build --release --manifest-path runtime/Cargo.toml     # or: make runtime
cargo build --release --features shell --manifest-path runtime/Cargo.toml   # or: make runtime-shell
```

The `shell` feature adds the `sh.*`/`fs.*` syscalls and the `bash`
tool (ADR-024). It is off by default so a path dependency from a
desktop app never carries shell code.

## Test and lint

```sh
cargo test --manifest-path runtime/Cargo.toml                    # default features
cargo test --features shell --manifest-path runtime/Cargo.toml   # the superset
make runtime-check                                               # clippy -D warnings (both feature sets) + fmt --check
```

`tests/repo_programs.rs` runs the deployer's validation over every
program under `../repos/`, so a program that would not deploy fails
CI. The end-to-end suite against a live server lives in the repo's
pytest tree.

## Embedding

An application runs the agent in-process by building a `Config` and
starting serve. Logging rides `tracing`: install a subscriber or get
silence.

```rust
let mut cfg = anyrt::Config::builder()
    .addr("http://127.0.0.1:7001")
    .agent_space("bao")
    .overlay_with_invite("agent", "<repo space id>", "<invite token>")
    .secret("llm.key.anthropic", key)
    .build();
anyrt::config::bootstrap(&mut cfg);
let agent = anyrt::serve::start(cfg)?;   // non-blocking; returns an AgentHandle
// ... agent.stop() on shutdown
```

`Config::from_toml` and `Config::load` read the same `anybao.toml` the
CLI uses. `anyrt::run_program` runs one program once, and
`anyrt::replay` replays a trace. The `anyapi::Transport` trait lets an
embedder route server calls through its own HTTP stack;
`tests/lib_api.rs` shows the surface without a server.

## CLI

| command | what it does |
|---|---|
| `anyrt serve` | run the agent: watch the chat, run conversations and triggers |
| `anyrt run <name@vN>` | run one program's `main(args)` and exit |
| `anyrt replay run_<id>` | strict replay of a recorded run |
| `anyrt deploy --source <dir> --target <space\|overlay>` | publish `programs/`, `skills/`, `README.md` to a space, hash-gated |
| `anyrt trace ls\|show\|follow\|diff\|stats\|blob` | read runs from the server's local store: list, render, tail, diff two runs, distributions, dump a blob |
| `anyrt drift` | vendored OpenAPI pin vs the coverage manifest |

Every command except `drift` takes `--config-file` and `--addr`; flags
override the file. Full usage: `anyrt <command> --help`.

## License

[MIT](../LICENSE)
