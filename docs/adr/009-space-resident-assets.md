# ADR-009: Space-resident assets, host config, overlays, lib mode

Status: **Accepted** (2026-07-21), amended 2026-07-21 (§4 kernel
embedded, §8), 2026-07-23 (§4 compile cache)
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

1. **Fully space-resident agent** — assets load from spaces declared
   in config; `anyrt deploy` is the only publish step. A deployed
   space is a **repo** (package-overlay sense): a filesystem folder of
   programs, skills — more kinds later — published to a space, which
   other spaces then import from. The shipped agent is just one such
   repo; update it by bumping the overlay, the working space never
   churns.
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
std = "bafy..."            #   `agent` = shipped programs + skills.

[paths]
traces = "traces"

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
- Lib mode builds the same resolved `Config` via a builder — no
  file, no env reads.

### 2. Overlays are repos

An **overlay is a repo**: a filesystem folder holding programs and
skills (more asset kinds later; the kernel is NOT one — it is
embedded in the binary, §4), published to a space with

```
anyrt deploy --source <folder> --target <spaceId | overlayName>
```

(`--target` takes a raw space id or an overlay name from
`[overlays]`; hash-gated as today. The source folder's asset kinds
are its subfolders — `<src>/programs/`, `<src>/skills/`, future kinds
alongside — plus a `README.md` at the root, so one repo folder is
self-describing.) Any space deployed this way is a
repo other spaces import from — the package-overlay model. Each repo
carries a **README object** (deployed from the source root's
`README.md`); its content is the overlay's description. The agent is made aware of configured overlays as
name + description (from the README) — repo *contents* are not
injected into context; discovery is `list_programs`, which gains the
ability to list a given space's programs. Example of what this
enables later (not in scope): a `connectors` repo the agent knows
about by description and browses on demand.

The `agent` overlay is special **only** in that serve injects its
programs and skills into the initial context (§3); it is deployed the
regular way. The working space keeps chat, brain, memory,
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

### 4. Kernel embedded in the binary

(Amended 2026-07-21, superseding two earlier shapes — a dataset
record, then a files-API upload + markdown manifest.) The kernel is
the runtime's OWN guest half, versioned with the binary — shipping it
through a space added a publish step, a download/cache protocol, and
a sync dependency for zero gain. Instead:

- `bin/kernel.wasm` is **compiled into `anyrt`** via `include_bytes!`
  (`runner::EMBEDDED_KERNEL`, `Cage::embedded()`); binary + kernel
  are ONE artifact. `make kernel` precedes the cargo build (the
  Makefile runtime targets depend on it).
- `--kernel <path>` (run/serve) stays as the dev override for testing
  a rebuilt kernel without recompiling anyrt.
- The space carries programs and skills only; deploy does not touch
  the kernel. `kernel.boot` still records `kernel_sha256` (ADR-001) —
  provenance unchanged.
- No kernel cache — `[paths].cache` is gone from the config.
- **Compile cache (amended 2026-07-23)**: distinct from the (dead)
  artifact cache above, `Cage::new` enables wasmtime's on-disk
  *compile* cache (`Config::cache`, default wasmtime cache dir). It is
  keyed by engine config + wasm hash, so a rebuilt kernel invalidates
  itself; it affects compilation latency only, never execution
  semantics or the trace. First launch after a kernel change still
  compiles cold (in dev builds, slowly — an any-ui-side
  `[profile.dev.package]` opt-level override on the cranelift crates
  is the complementary fix); every subsequent launch, including
  release `serve` restarts, loads the cached artifact near-instantly.
  Best-effort: an unusable cache dir logs a warning and compiles cold
  rather than failing the boot.

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

### 8. Overlay membership: join-on-boot + reader approval (interim, amended 2026-07-21)

An overlay published by another account must be *joined* before its
space syncs to this device. Until guest-key spaces land, the interim
mechanics are:

- **Config**: an `[overlays]` entry is either the bare space id or an
  inline table with an invite token —
  `agent = { space = "bafy...", invite = "b58token..." }`. Values stay
  strictly ids; the invite is the any-server RequestToJoin token.
- **Join-and-proceed** (no polling): serve's overlay probe, on a space
  it cannot see, sends `POST /spaces/join {inviteToken}` when an
  invite is configured and then PROCEEDS (the cage boots eagerly —
  the kernel is embedded, §4; only program resolution waits).
  Readiness is re-checked when a chat message arrives: still pending
  → the host replies with the space's status (an operational bubble,
  same precedent as the failure bubble — not prompt wording); synced
  → the message is handled normally. Standing triggers skip ticks
  until ready. A missing invite is still a hard boot error.
- **Read-only doctrine**: joiners get **`reader`** permission — the
  server's vocabulary for view-only. The any server's invites carry NO
  permission; the grant is chosen at approval
  (`POST .../acl/accept {requestRecordId, permission: "reader"}`), so
  read-only is enforced by the approving publisher, not the token.
  This delivers 00-plan's trust posture: a readonly overlay ⇒ only the
  publisher writes ⇒ authenticity by CRDT ACL.
- **Guest keys (amended 2026-07-21, same day — upstream landed)**: the
  any server now mints a space's public read-only GUEST key
  (`POST /v1/spaces/{id}/guest-key`, owner-only, idempotent; CLI
  `any invite guest-key`) returned as an invite token for the SAME
  `POST /v1/spaces/join`, auto-detected — no join request, no
  approval, enforced read-only by construction. The config shape is
  unchanged: `invite` simply carries a guest token. serve tolerates
  the 409 already-tracked/deleted replies as pending-sync. The
  `approve_joins.py` interim daemon is DELETED — RequestToJoin +
  reader-approval remains valid for invite-only (non-public) repos,
  approved manually (`any acl accept`).

### 9. Non-goals

Overlay manifest/trust format beyond what ADR-008 already fixes
(publishing third-party overlays is future work); space→disk sync
(the filesystem stays authoring-only); async
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

1. ~~`agent_kernel` object + file-attach against the live server is
   unverified.~~ **Resolved 2026-07-21**: acceptable unverified; any
   kernel-load failure at boot is a hard error (§4), no fallback.
2. The files API marks each attach with a `durable` flag (server-side
   background processing — chunking/pinning the blob after the upload
   returns). Whether a `GET .../content` immediately after attach can
   race that processing on a local server is untested. Position:
   deploy does NOT poll `/files/{id}/status`; if an early boot download
   fails it is the §4 hard error and a retry (re-run serve) succeeds
   once processing settles.
3. ~~`stop()` latency: read timeout or accept heartbeat bound?~~
   **Resolved 2026-07-21 (lib-mode commit)**: no read timeout — the
   watcher observes the flag per SSE frame/heartbeat (the server
   heartbeats; a dead TCP peer ends the read via FIN), the reconnect
   sleep and ticker use sliced sleeps, the control API accepts with
   `recv_timeout(250ms)`. In-flight conversation threads are not
   joined by `stop()` — a running turn finishes on its own. Revisit
   with a stream read timeout only if heartbeat gaps bite in practice.
4. ~~Bootstrap of a fresh overlay space: who mints the space id?~~
   **Resolved 2026-07-21**: not anyrt's job — spaces are created
   externally (any CLI); `--target` stays strict, no helper.
