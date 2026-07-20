# ADR-009: Space-resident assets, host config, overlays, lib mode

Status: **Proposed**
Date: 2026-07-21
Builds on: ADR-002 (isolation), ADR-004 (module loading — amends §6,
delivers §7's deferred overlay config), ADR-006 §3 (secrets), ADR-008
(capabilities); plan §4 Overlays + the `agent:` overlay split (00-plan,
decided 2026-07-08)

## Context

The runtime locates all three asset kinds through cwd-relative clap
defaults: `programs/` and `skills/` (auto-deployed to the space on
every `serve` start), `bin/kernel.wasm` (read off disk at boot). Host
configuration is scattered across flags; there is no config file, and
the crate is bin-only. Two goals force the change:

1. **Fully space-resident agent** — code loads from spaces declared in
   config; `anyrt deploy` is the only publish step. This implements
   the already-decided `agent:` overlay split: agent CODE (shipped
   programs/skills/kernel, churns every deploy) separated from user
   DATA (chat, memory, user-authored skills/programs — permanent).
   Update the agent by bumping the overlay; user data never churns.
2. **Lib mode** — other Rust apps embed the runtime (run the agent,
   run one program, replay traces) with config built programmatically.

The ground is prepared: serve's per-run module resolution is already
space-only (ADR-004 §6); the resolver already implements strict
`alias:name@vN` semantics (ADR-004 §2); the any server has a files API
(raw upload attached to an object, ranged download) that carries the
~20 MB kernel; the crate has no statics, no async runtime, env reads
confined to CLI bootstrap.

## Decision

### 1. Host config: `anybao.toml`

One TOML file for everything host-side; CLI flags override file
values; secrets NEVER appear in it (ADR-006 §3 / ADR-008 §1 unchanged:
env vars, `--secrets`, device-local `localValue`).

```toml
# anybao.toml
addr = "http://127.0.0.1:7001"   # any server

[agent]
space = "bao"              # working space: chat, brain, memory,
name = "bao"               #   agent_config, user-authored skills/programs
control_port = 7010

[overlays]                 # named module sources (the alias namespace);
agent = "bafy..."          #   values are STRICTLY space ids, never names.
std = "bafy..."            #   `agent` = shipped programs, skills, kernel.

[paths]
traces = "traces"
cache = "~/.cache/anybao"  # default: $XDG_CACHE_HOME/anybao

[config]                   # guest-visible cascade layer: FLAT quoted
"llm.tier.chat" = { provider = "anthropic", model = "..." }  # dotted keys
```

- Host precedence: built-in defaults < `anybao.toml` < CLI flag.
- Guest cascade (lowest→highest): `config_defaults.json` < `[config]`
  table < `--config` JSON < space `agent_config` overrides — the
  existing cascade gains one layer, nothing else moves.
- Discovery: `--config-file <path>`; default `./anybao.toml` when
  present, else pure defaults. Unknown keys are a parse error (typos
  fail loudly; single-repo tool, no forward-compat lenience).
- Lib mode builds the same resolved `Config` via a builder — no file,
  no env reads (cache dir et al. passed explicitly).

### 2. The `agent` overlay — code/data split

The code space is **just an overlay named `agent`** — no dedicated
config concept. Its space holds shipped programs, skills, and the
kernel; `anyrt deploy` writes it (programs + skills + kernel,
hash-gated as today). The working space keeps chat, brain, memory,
`agent_config`, and everything the user or the running agent authors.

Resolution (ADR-004 §2 intact, now wired):

- The resolver's current space **stays the working space**. Unqualified
  `name@vN` → working space → private fallback — never overlays. A
  user copy in their space deliberately shadows shipped code
  (Nix-style), because the harness reaches shipped code only through
  the explicit alias.
- Harness specs are explicit: serve spawns `agent:toolcaller@v1`;
  standing triggers run `agent:rollup@v1` etc. Transitive imports
  inside overlay programs resolve defining-space-first (ADR-004 §2.4),
  so agent code is self-contained.
- Every `[overlays]` entry feeds the resolver alias map verbatim;
  `use("std:tool@v2")` works the day the entry exists.
- Degenerate default: no `agent` entry ⇒ the alias binds the working
  space id — a config-less setup behaves exactly like today's
  single-space shape (deploy targets the working space too).
- serve start validates each overlay id with a strict `get_space`
  (never creates); a missing `agent` space fails with "run anyrt
  deploy first".

This amends **ADR-004 §6**: serve (and `run --from-space`) gain
aliases — the "no aliases" gap closes. `anyrt run`'s local-dir mode
stays as the offline dev/test path (the two worlds still never mix
within a run).

### 3. Skills: two tiers, guest-merged

Shipped skills deploy to the `agent` overlay; user skills live in the
working space. `toolcaller@v1` composes its system prompt from BOTH —
`agent_skill` objects of the overlay and of the working space, merged
by name, **working space wins** (same shadowing doctrine as programs).
The host passes `codeSpace = overlays["agent"]` in spawn args; it
still injects no prompt wording (ADR-005 unchanged). This is the one
guest-side change in this ADR.

### 4. Kernel in the space + content-hash cache

Data contract, in the `agent` overlay space:

- One object, `any.name = "anyrt-kernel"`, type `agent_kernel` (same
  ensure-typed pattern as the trigger anchor). Object name is a fixed
  convention — no config knob.
- Dataset `agent_kernel`, record `"main"`:
  `{sha256, size, fileId, name, uploadedAt}`.
- Bytes attached via the files API (`POST
  /spaces/{id}/objects/{oid}/files?name=kernel.wasm`, raw
  octet-stream). Deploy is hash-gated on the record's `sha256`.

Boot protocol: read the record → cache hit on
`<cache>/kernel/<sha256>.wasm` uses local bytes; miss downloads
(`GET .../files/{fileId}/content`), verifies the digest (mismatch =
hard error naming both hashes), writes tmp + rename. No eviction —
one ~20 MB file per kernel version; the user prunes the cache dir.
`--kernel <path>`, when explicitly passed, is a dev override that
bypasses the space. `kernel.boot` still records `kernel_sha256`
(ADR-001) — provenance is unchanged, only the byte source moves.

### 5. serve is space-only (breaking)

serve drops `--programs`/`--skills` and its startup auto-deploy.
`anyrt deploy` is the only publish path. Workflow change: run
`anyrt deploy` before the first `serve` against a fresh space.

### 6. Lib mode: lib+bin split

The crate becomes `[lib]` + `[[bin]]`; `main.rs` is a thin CLI (clap,
env-var bootstrap, `process::exit`, log subscriber — all bin-only).
Public surface: `Config`/`ConfigBuilder`; `serve::{start, AgentHandle,
RunCtx}`; `runner::{Cage, RunOutcome, run_program}`; `replay`;
`deploy`; `anyapi::Client` (+ `Transport` for injection).

- `start(cfg) -> AgentHandle` does everything serve does up to the
  watch loop, then spawns it; `stop()` flips a shutdown flag checked
  by the watch/reconnect loops, the trigger ticker, and the control
  API (switched to a `recv_timeout` accept loop), then joins. CLI
  `serve` = `start(cfg)?.join()`.
- `Cage`'s epoch-ticker thread gains a stop flag joined on `Drop`
  (embedders create/drop cages; the detached thread must not leak).
- Logging routes through `tracing` (`info!`/`warn!`/`error!`); CLI
  *output* (run-result JSON, deploy summaries, trace renders) stays
  stdout. Embedders get silence by default or full capture via their
  own subscriber; the bin installs a fmt subscriber.

### 7. Capability forward-compat (note, not scope)

`module.resolve` already records `{spaceId, sourceHash}` per load;
grants bind to content hashes (ADR-008). Gating overlay imports later
= the broker intersecting grants keyed by (overlay space, contentHash)
over the frame chain (ADR-004 §5) — overlay maps are host config, so
enforcement needs no resolver change. Explicitly out of scope here.

### 8. Non-goals

Overlay manifest/trust format beyond what ADR-008 already fixes
(publishing third-party overlays is future work); space→disk sync
(the filesystem stays authoring-only); kernel cache eviction; async
lib API (the crate stays sync/thread-based).

## Consequences

- The agent is fully space-resident: a binary + `anybao.toml` + a
  deployed overlay is a complete install; no repo checkout at runtime.
- Agent updates = bump the overlay; user data untouched. One overlay
  can back many working spaces.
- Breaking: serve no longer self-deploys; fresh setups need one
  `anyrt deploy`.
- Embedders get the same runtime as the CLI with config as data and
  clean thread lifecycle; replay/trace tooling comes along free.
- rt_e2e and the pytest suite stay on `run`'s local-dir mode —
  unchanged.

## Open questions (for review)

1. `agent_kernel` object + file-attach against the live server is
   unverified (the `agent_trigger` ensure-typed precedent suggests it
   works); fallback is a plain untyped anchor object.
2. File durability tier right after a 20 MB attach on a local server —
   deploy should not need to wait on `durable`; confirm.
3. `stop()` latency is bounded by the next SSE frame/heartbeat
   (blocking reads); a read timeout on the stream is the likely fix —
   decide during the lib-mode commit.
