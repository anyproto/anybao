# ADR-032: Engine backends: precompiled kernel, signed AOT on iOS, Pulley fallback

Status: **Proposed** (draft for review, 2026-10-16)
Date: 2026-10-16
Builds on: ADR-001 (`kernel.boot`), ADR-003 §2/§5 (fuel, epochs,
limits), ADR-004 §3/§4 (`module.resolve`, the resolve cache), ADR-005 §3
(soft and hard break), ADR-009 §4/§6 (embedded kernel, lib mode),
ADR-015 (election), ADR-025 (presence beats), ADR-026 (blobs), ADR-031 §6
(the 250 G budget)
Amends: ADR-003 §1 (code backends of the one wasi engine), §2 (the
kernel world gains a `compile-module` export) and §5 (the memory cap
is implemented), ADR-004 §3/§4 (a compiled-code cache beside
the source cache), ADR-009 §4 (precompiled kernel, `kernel.boot` names
the backend) and §6 (handle API, `stop_with_drain`, serve mode)
Inputs: the iOS feasibility review (local, not in the tree); the
Android branch `feat/android-anyrt` (local, not pushed; code ahead of
this ADR, merges after acceptance); the measurements in §10

## Context

anyrt now has to run on phones. Android works today on a local branch:
anyrt is cross-compiled with cargo-ndk, the kernel is precompiled to a
`.cwasm` at build time and embedded, and the app execs `libanyrt.so`
from a foreground service. Precompiling took kernel boot on a Pixel 9
Pro from 4.6 s (Cranelift on the phone) to 0.1 s.

iOS is harder in two ways. An app may not exec a second binary, so
anyrt links into the app process (ADR-009 §6 lib mode). And an app may
execute only machine code that its signature covers: memory written at
run time never becomes executable, so Cranelift cannot run on the
device and an ordinary `.cwasm` cannot be mapped executable either.
Wasmtime documents static linking of precompiled code as unsupported,
and lists `aarch64-apple-ios` as Tier 3.

Three ways remain to run the kernel on iOS: interpret it (Wasmtime's
Pulley, about 10× slower than Cranelift), translate it to code that is
signed into the app (wasm2c, or Wasmtime's own AOT output placed in the
signed binary), or borrow WebKit's JIT (unreliable in the background
and off under Lockdown Mode). Replacing CPython is out: the guest is
the whole agent, and ADR-002/003/004/005/013/031 fix Python.

The second number that matters is per-run work. Every run parses the
source of every program it `use()`s, because each run gets a fresh
Store (ADR-003 §3). A conversation loads `any@v1`, `llm@v1`,
`toolcaller@v1`, `history@v1` and `autorecall@v1`: 367 KB of source,
about 4 G of fuel at ~11 M/KB, before any model cell runs. On desktop
that is 0.4 s. On Pulley it would be most of a turn.

## Decision

### 1. One kernel, one wasi engine, three code backends (amends ADR-003 §1)

ADR-003 §1 stands: one engine, Wasmtime running the componentize-py
kernel through the ADR-002 broker. What varies by platform is only how
Wasmtime gets machine code for the kernel:

| backend | where the code comes from | platforms |
|---|---|---|
| `cranelift-jit` | Cranelift at boot, with the ADR-009 §4 compile cache | desktop, Linux, CI (default) |
| `cranelift-aot` | `anyrt kernel compile` at build time, embedded in the binary | Android; iOS (§3) |
| `pulley` | the same compile with target `pulley64`, interpreted | iOS fallback (§11) |

All three run the same kernel bytes, the same WIT world, the same WASI
floor and the same fuel and epoch instrumentation (Wasmtime emits fuel
and epoch checks before any backend runs), so fuel counts are identical
across backends. This is not a second engine in ADR-003's sense:
enforcement is still built once.

wasm2c is not adopted. It needs a second kernel build (a core module
instead of a component), an injected fuel pass with its own units and
a second embedding. It is recorded as the path if `cranelift-aot` fails
on iOS devices, and would need its own ADR (an ADR-003 amendment:
"kernel ABI, two implementations").

### 2. Precompiled kernel (amends ADR-009 §4)

- `anyrt kernel compile --target <triple> -o bin/kernel.cwasm` compiles
  the embedded or `--kernel` wasm for a target. A foreign target needs
  a host build with feature `cross-compile` (`wasmtime/all-arch`). It
  writes `kernel.cwasm.sha256`, the sha256 of the source wasm. The
  §4 platform knobs follow from the triple; `pulley64` runs anywhere,
  so it takes `--platform desktop|android|ios` (amended in
  implementation, 2026-10-16).
- Feature `precompiled-kernel` embeds `kernel.cwasm` and its sidecar in
  place of `kernel.wasm`. Default builds are unchanged. The `.cwasm` is
  built in the same job as the binary, every time, and never shipped
  separately: it loads only into an engine configured exactly like the
  one that compiled it.
- `kernel.boot` keeps `kernel_sha256` as the hash of the source wasm,
  so provenance does not depend on the backend. The record's `meta`
  gains `engine: {backend, target, wasmtime}` so a trace says what ran
  it. It goes in `meta`, not `input`: the replay key hashes the whole
  input (`trace.rs`), and a phone's trace must replay on a desktop.
  A replay restamps it with the engine that replayed. `target` is the
  build's triple (`pulley64` on the Pulley backend); `wasmtime` is the
  linked crate's version.
- The compile cache (`wasmtime::Cache::from_file`) is skipped in
  `precompiled-kernel` builds.
- `anyrt kernel check` loads the kernel and runs one built-in program
  with one effect, with no server, and prints `{loadMs, runMs,
  fuelUsed, precompiled}` (plus `status`, `engine`, `kernelSha256`). It
  is the on-device smoke test. It records into the in-memory
  `TraceWriter` the unit tests use and writes no trace anywhere, so it
  does not need the ADR-023 store. Its effect is `mailbox.drain`,
  whose reply is constant, so `fuelUsed` is equal on every backend and
  the check doubles as the §1 fuel-parity test.

### 3. Signed in-place loading on iOS

On iOS the `.cwasm` is placed in the binary's signed code segment and
used where it sits:

- The bytes go into a `__TEXT,__bao_kernel` section with `.incbin`,
  aligned to 16 KiB. The section is executable and read-only like all
  of `__TEXT`, and the app signature covers it.
- `Component::deserialize_raw` loads it without copying. The code has
  no relocations (Wasmtime's loader refuses any outside `.debug_*`), so
  nothing writes to the signed bytes.
- Because the memory is externally owned, Wasmtime cannot change its
  protection itself. The engine gets a `CustomCodeMemory` whose
  `publish_executable` checks that the range lies inside the embedded
  section and returns without changing anything. Any other range is an
  error.
- The component is loaded once per process and shared. Registering
  the same raw image twice trips an assert in Wasmtime's module
  registry (`module/registry.rs`, 49.0.3), although the `deserialize_raw`
  contract itself only requires that the memory outlives the component
  and is never modified. Every
  instantiate on iOS copies the 22.75 MB `.rodata.wasm` data image into
  linear memory, since Wasmtime's copy-on-write memory images need
  memfd, which only Linux has. Android is Linux (`memfd_create` from
  API 30) and keeps them (§4).

Android uses the same loader, with the bytes in a `.text.bao_kernel`
section of `libanyrt.so`. On the Pixel 9 Pro it passed 20+ `kernel
check` runs and removed the second resident copy that `deserialize`
makes (§10). The `.text` offset inside the `.cwasm` is 0x4000 on Darwin
and 0x10000 on Linux aarch64 (0x1000 on x86_64 Linux, the CI host), so
16 KiB alignment covers all three. The hook
is not part of Wasmtime's compatibility check, so one `.cwasm` loads
with either loader.

Implementation notes (amended 2026-10-16): the loader is
`runtime/src/kernel_image.rs`. The image's length comes from a
compile-time `include_bytes!(..).len()`, which also makes rustc rebuild
when the `.cwasm` changes (`.incbin` alone is invisible to it); the
bytes are emitted once. The hook's `required_alignment` is 4 KiB, the
smallest `.text` offset above, since it changes no protection. A
`precompiled-kernel` build that boots a `--kernel` wasm compiles it on
an engine without the hook (Android and desktop only; iOS cannot JIT).
The proof runs as `tools/hardened_kernel_check.sh` (CI job
`precompiled-aot-macos-hardened`): ad-hoc signed with the hardened
runtime and no entitlements, 40 `kernel check` runs, then `vmmap` on
the live process finds no writable+executable and no anonymous
executable region. Negative controls: the JIT build and the copying
`deserialize` loader, signed the same way, are SIGKILLed by the
kernel on their first unsigned code page.

### 4. Engine configuration: frozen and per-platform

One `engine_config(platform)` is shared by `kernel compile` and the
loader.

Frozen across every backend and platform, because changing them moves
deterministic traps or budgets:

- `consume_fuel(true)`, `epoch_interruption(true)`
- `max_wasm_stack` at Wasmtime's default (512 KiB). Python-level depth
  is bounded separately, by CPython's 1000-frame recursion limit
  (ADR-012 §4). This setting bounds C-level recursion in the kernel.
  `cranelift-jit` and `cranelift-aot` emit the same code, so such a
  trap lands at the same depth on both. Pulley frames differ in size,
  so on the fallback it may land elsewhere; accepted.
- `wasm_simd` at Wasmtime's default (on). The kernel uses no `v128`,
  but the setting changes validation.
- `native_unwind_info(false)`. Registering and deregistering unwind
  info per function costs 130 to 140 ms per Store teardown on the
  Pixel, and Wasmtime's docs recommend disabling it where that cost is
  high. Wasm backtraces come from frame pointers, and host panics are
  caught at the trampoline, so nothing unwinds through Wasm frames.
  What is lost is native profilers and debuggers walking through Wasm
  frames; the trace never carried native frames. It is part of the
  artifact, so it is frozen everywhere.
- the Wasm feature set, and NaN canonicalization off. NaN payloads
  differ between x86-64 and aarch64, and a guest can observe them
  through `struct.pack`. This stays a documented non-determinism:
  canonicalizing costs on every float op and would change desktop
  traces.

Per platform:

| knob | desktop | Android | iOS |
|---|---|---|---|
| `signals_based_traps` | default | default | `false` (also set in the precompile: it changes codegen) |
| `memory_guard_size` | default | default | 0 |
| `memory_reservation` | default | default | 0 |
| `memory_reservation_for_growth` | default | default | 128 MiB |
| `memory_init_cow` | default | default (memfd images; disable only if the device refuses `memfd_create`) | `false` (no memfd images off Linux; makes the copy explicit) |
| `EPOCH_TICK_MS` | 10 | 50 | 50 |
| compile cache | on | off | off |

Cross-compiled targets use Cranelift's default ISA flags (baseline
ARMv8.0). Wasm semantics do not depend on them, and every supported
device has them.

### 5. Memory cap (amends ADR-003 §5)

Each run's Store gets a resource limiter: a linear-memory limit of
1 GiB on desktop and 512 MiB on mobile, built with
`StoreLimitsBuilder::memory_size(..).trap_on_grow_failure(true)`.
Without `trap_on_grow_failure`, a denied growth only returns failure to
the guest. The limit applies per linear memory; the kernel has one, so
it is the run's cap. A thin limiter wrapping `StoreLimits` records the
peak size, which implements the `mem_pages` metric ADR-003 §5 names
(`StoreLimits` does not collect it).

A limit hit is reported the way fuel exhaustion is today: the run ends
with status `error` and a typed error, `MemoryLimit`, with its own
match arm beside `FuelExhausted` (`runner.rs`). Today a grow failure
would surface as the generic `Trap`. It is not an `interrupted` run,
which stays reserved for breaks. This amends ADR-003 §5's sentence
that every limit hit "surfaces as `CellResult.interrupted` + `error`":
fuel and memory limits surface as `error` with a typed reason, as the
code already does for fuel; breaks surface as `interrupted`. Today a
model cell can pull a 60 MB body into linear memory with no bound.

Implementation notes (2026-10-16): the limiter (`RunLimits`) types a
refused growth as `MemoryLimitHit`, which the runner downcasts from
the trap. `mem_pages` is the peak since instantiation, so it counts
the kernel's initial memory (364 pages, 22.75 MiB, for `kernel
check`); Wasm memory never shrinks, so the peak equals the size after
the cell, as ADR-003 §5 defines it. It rides the cell record's
`metrics`, which replay does not match.

### 6. Run threads

Wasm executes only on threads with a stack of at least 4 MiB. With
native code, Wasm frames use the host thread's stack, so the thread
must clearly exceed `max_wasm_stack` (512 KiB) plus the host frames
under an effect.

- Threads anyrt spawns for runs (chat conversations, the trigger
  ticker, the control API, the §7 compile job) are built with
  `thread::Builder::stack_size(4 MiB)`. Today they get Rust's 2 MiB
  default by accident. *Amended in implementation (2026-10-16):* the
  ADR-018 event-source threads fire event triggers inline, so they run
  Wasm too and get the same stack. All of them go through
  `runner::spawn_run_thread`, which names the thread (`anyrt-…`).
- `RunCtx::run` and the CLI run on the caller's thread today. They
  stay that way and document the requirement. `AgentHandle::run` (§8)
  spawns. An embedder that calls `RunCtx::run` directly must call it
  from a thread it created with that stack size. The iOS wrapper does.
- Loading the Cage may run on any thread: deserialization executes no
  Wasm.

### 7. Compiled-code cache (amends ADR-004 §3/§4)

Beside ADR-004 §4's source cache, the host keeps a cache of compiled
code objects. Only the host writes it, from a trusted compile. A run
only reads it.

- Key: `(kernel_sha256, spec, sourceHash)`. Marshal output is bound to
  the CPython version, which the kernel hash pins. It also embeds the
  code's filename, which `use()` sets to `<spec>` (`app.py`), so the
  same source under two specs gives different bytes. An alias spec
  (`agent:toMarkdown@v1` and `toMarkdown@v1`) is therefore two entries
  for one program, which is harmless.
- Location: `<config dir>/cache/code/<kernel_sha256>/<key hash>.marshal`.
  It lives outside the blob root, because ADR-023 §6 retention
  collects every blob no live record references. It is device-local,
  never synced, and safe to delete at any time.
- Filling it: the kernel gains one export, `compile-module(source,
  filename) -> list<u8>`, which compiles and marshals and runs nothing
  else. It bypasses the cell path entirely (no `_guard_kernel_names`,
  no printers). On a miss the run compiles from source as today and
  the host notes the key. The compile job runs right after the run, on
  the run's own thread, once the trace is finalized and the reply is
  posted. It is skipped when shutdown or a drain is set: batching on
  idle would never run on a phone that gets suspended. It instantiates
  a fresh Store with no program loaded, whose `Host` has a closed
  broker (every effect refused) and the WASI floor seeded with zeros,
  so the export cannot reach anything. It calls `compile-module` for
  each noted key and stores the bytes with their sha256. Budget: 20 G
  of fuel and a 10 s epoch deadline per key (compile costs about the
  parse, ~11 M/KB, so ~2.2 G for `any@v1`); a key that exceeds it is
  not cached. The instantiate (~0.1 s plus the data copy) is paid once
  per program version per device. The kernel's own code is trusted. Guest programs, including
  model cells, can call `effect()` with any name, so no effect can
  write the cache. That rules out a guest-supplied `module.compiled`,
  which would let one program poison another program's code for later
  runs. The compile job is cache maintenance, like the ADR-009 §4
  compile cache. It runs under its own fuel budget and emits no trace
  records, because no run observes it.
- Serving: on a hit, `module.resolve` output carries the marshal bytes
  base64-encoded, as a text spill (ADR-001) beside `source`, plus
  `codeHash`. Not a raw blob: raw blobs live in the directory beside
  the serve (ADR-026 §1), and a trace store read on another device
  cannot resolve raw refs (`tracestore.rs`), so a hit would replay only
  on the same device or through an export. Base64 costs 1.33× the bytes
  (~330 KB for `any@v1`), once per program version.
  Before serving, the host verifies the stored sha256; a mismatch is
  treated as a miss. The guest loads hits with `marshal.loads`.
- Recording: the resolve record keeps both spills. Spills are
  content-addressed (ADR-026), so a program's source and code are
  stored once however many runs load them. Replay returns the recorded
  output as today, so a replayed hit loads the same bytes and spends
  the same fuel as the recorded run, and ADR-004 §3's self-contained
  trace holds after the program is edited or deleted, or the cache is
  cleared.
- Meta gains `codeCache: hit|miss`. No new effect and no new mutate, so
  serve's retry rule for a missing program (no retry after a mutate)
  is unchanged.

Expected effect: from the second run of a program version, a
toolcaller run drops from ~4 G to ~0.5 G plus ~0.1 to 0.3 G per model
cell, on every platform.

Implementation notes (amended 2026-10-16):

- The export returns empty bytes for source that does not compile
  (never a valid marshal), so the WIT signature stays `list<u8>` and a
  trap is never the error channel. `use()` and the export share one
  `_compile_program(source, filename)`, so the cached object is the
  one a miss would have built.
- The compile job's instantiate spends from the first key's budget.
  A key that traps (budget, deadline) drops its Store; the next key
  instantiates a fresh one.
- Where it runs: after the chat reply is posted (conversation
  thread), after `finish_run` (trigger and event-source runs), before
  the control API's `POST /run` response (once per program version;
  never for an inline `source`, whose every edit would be a new key),
  and after `anyrt run` prints its result. Replays note no misses: the
  recorded resolve output is returned without running the hook.
- `Config.code_cache_dir`: `Config::load` sets
  `<config dir>/cache/code` (`/cache/` is gitignored at the repo
  root); `ConfigBuilder::code_cache_dir` for embedders; unset = no
  cache, no `code`, no `codeCache` meta.
- The `code` field is a text spill only as part of the whole resolve
  output: the trace spills a record's output as one unit (ADR-001
  §7), `source` included, so it is the output, not each field, that
  is content-addressed.
- Measured (2026-10-16, M-series Mac, `code_cache_tests`): loading the
  five conversation programs (`any@v1`, `llm@v1`, `toolcaller@v1`,
  `history@v1`, `autorecall@v1`) through a loader program costs
  3.09 G fuel cold and 0.16 G warm (19×); a strict replay of the warm
  run spends exactly its fuel with the cache gone; with no cache
  configured a run spends the same fuel as a cold run. A full
  toolcaller run (model cells included) is not measured yet.

Baking shipped programs into the kernel snapshot was considered and
rejected. Programs are space-resident and deployed after a release
without a restart (ADR-009). Baking them in couples two release cycles.

### 8. Embedding API (amends ADR-009 §6)

- `[agent] control = false` means no control listener. `control_port
  = 0` keeps its current meaning, an ephemeral port, because a shipped
  client writes it (any-ui's second instance, BOB-110). `AgentHandle`
  gains the methods that today exist only over HTTP: `status()`,
  `break_run(id)`, `run(program, args)` (spawns, §6), and trigger
  enable/disable. An app does not need a socket to talk to itself, and
  any-ui moves to the handle API on its own schedule.
- `AgentHandle::stop_with_drain(deadline)`, for an embedder to call
  before the `any` server it depends on goes away (suspend, sign-out).
  `stop()` keeps its current meaning. The contract, in order:
  1. Close admission. New chat inputs go to the backlog, for whichever
     device hosts next.
  2. Soft-break every live run: post a `break` item to its mailbox
     (ADR-005 §3). The run stops at its next turn boundary.
  3. At half the deadline, hard-break what is left (the interrupt flag;
     the epoch callback traps it inside Wasm).
  4. Host waits observe the run's interrupt: the broker's `sleep` waits
     on it instead of a plain timer, and SSE reads get a read timeout.
     A blocking HTTP call already on the wire cannot be cancelled with
     `ureq`.
  5. At the deadline, revoke every run still alive. The broker checks a
     per-run revoked flag before and after every effect: it issues no
     new request and drops late results. The thread is detached and
     finishes into a closed broker.
  6. Stop the service threads, each join bounded by what is left of the
     deadline.

  It returns `{drained, interrupted, revoked}`. Guarantee: after it
  returns, no run sends a new request to the `any` server or a
  provider. A request already on the wire may still complete
  server-side.
- Serve mode: `[agent] mode = "full" | "chat-only"`, default `full`.
  `chat-only` keeps the trigger ticker, because it also probes overlay
  readiness, drains conversations deferred during boot (ADR-009 §8)
  and reconciles trigger ownership. It stops firing cron and once
  triggers. The retention thread is skipped. Presence and election
  stay, so a phone still beats and can hand off its claim.

### 9. The phone as host (relates ADR-015, ADR-025)

- A phone runs `chat-only` serve. Triggers and cron need a process that
  outlives a turn.
- The phone never preempts. On foreground it runs the ADR-015
  reconcile unchanged, which claims only where ADR-015 already does: no
  qualifying winner exists (the phone is the only bao device, or the
  old winner's row is gone). Otherwise it stays standby, and the app
  shows which device answers with a one-tap switch ("Use this device",
  the existing activation). If the winner's ADR-025 beat is older than
  60 s on resume, the app shows the consent prompt at once. The phone
  does not take over from a live or merely silent winner by itself: it
  does not need to be active to chat, and every flip opens the ADR-015
  overlap window.
- The runtime cannot un-claim. When the phone stops hosting (after any
  continued background task for a running turn ends), it hands off with
  `activate {app: "bao", peerId: <the device with the freshest beat
  whose row carries apps.bao>}`. A device without `apps.bao` answers
  409 `device.app_not_installed`. If no such device is beating, the
  phone keeps the claim.
- How the app keeps a turn alive in the background and keeps the `any`
  server up meanwhile is the app's concern and is specified in the app,
  not here.

### 10. Measurements (point-in-time; move to notes after acceptance)

| measurement | value | source |
|---|---|---|
| desktop throughput | 10 to 11 G fuel/s | anyrt 0.7.0, this team's box |
| parse cost | ≈11 M fuel per KB of source (`use("any@v1")` 2.21 G vs `ast.parse` 2.23 G, an upper bound) | desktop |
| empty run (instantiate + boot) | 3.9 M fuel, 1 ms | pre-init works |
| toolcaller run, real traces (n=27) | p50 4.25 G, p95 5.28 G | `metrics.fuel_used` |
| cron runs (extraction, rollup, linkgen) | p50 ~2.32 G each | the `any@v1` floor |
| toMarkdown, 15-page PDF | 23.6 G, 2.3 s, 137 MB RSS | desktop |
| toMarkdown, 75-page PDF | 74.7 G, 7.3 s, 162 MB RSS | desktop |
| kernel boot, Pixel 9 Pro | 4.6 s Cranelift, 0.40 s warm cache, 0.10 s precompiled | Android branch |
| anyrt RSS, Pixel 9 Pro | 109 MB boot, 145 MB peak in a one-cell turn | Android branch |
| Pixel 9 Pro throughput | ~5.8 G fuel/s | Android branch, toolcaller traces |
| toolcaller run, Pixel 9 Pro | ~3.79 G fixed per run; turns and cells add under 1% | Android branch |
| `.cwasm` size, aarch64 | 48 MB: 17.4 MB `.text`, 22.75 MB `.rodata.wasm`, 3 MB address map, 1.5 MB `eh_frame`, 1.5 MB symtab | Android branch |
| `deserialize` vs in-place load, Pixel (median of 5) | load 94 vs 30 ms; RSS after load 107 vs 13.3 MB; peak 133 vs 68 MB; fuel identical (490.55 M) | Android branch, `kernel check` |
| first instantiate after in-place load | ~50 ms slower (first-touch page faults) | Android branch |
| Store teardown | 130 to 140 ms with either loader (likely one frame deregistration per function) | Android branch |
| `kernel check` fuel, three backends | 490 285 509 on `cranelift-jit`, `cranelift-aot` and `pulley` alike | M-series Mac, 2026-10-16 |
| `kernel check` run, M-series Mac | ~65 ms AOT, ~1 240 ms Pulley (~18×, above the ~10× assumed below) | same |
| in-place vs copying load, macOS hardened runtime | load 3–15 ms; peak RSS 66 vs 130 MB; copying and JIT builds SIGKILLed under hardened runtime | M-series Mac, 2026-10-16 |
| iOS knobs on desktop (bounds checks, no CoW) | 61 vs 56 ms per `kernel check` run, ~1.1× | same, knobs forced on a macOS build |
| iOS simulator, `aarch64-apple-ios-sim` | `kernel check` ok, same fuel; 0.8–1.1 s per run (simulator-spawned process, not a device number) | same |
| `.cwasm` size by target | 46 MB aarch64-apple-darwin / aarch64-linux-android, 61 MB aarch64-apple-ios (explicit bounds checks, no guard pages), 46 MB pulley64 | same |

Estimates for an iPhone 13 Pro Max, assumed between the desktop box
and the Pixel (which runs at about half the desktop's fuel rate). A p50 turn: ~0.4 to 1 s on `cranelift-aot`, ~4.5 s on `pulley`.
With the §7 cache: ~0.1 to 0.2 s on AOT, ~1 to 2 s on Pulley. A
15-page PDF: ~2.5 to 6 s on AOT, ~25 s on Pulley. The AOT range
assumes explicit bounds checks cost 1.1 to 1.3× over guard pages.

### 11. Fallback ladder

`cranelift-aot` (iOS) → `pulley` (`precompiled-kernel` plus feature
`pulley` = `wasmtime/pulley`, the engine targeting `pulley64`, with a
`kernel compile --target pulley64` artifact) → wasm2c (new ADR). The
backend is chosen at build time, and a build ships one. The Pulley
build stays compiled in CI (`make runtime-pulley`, the `precompiled`
CI job) so the fallback is a flag, not a project.

## Consequences

- One kernel, one trace format and one fuel scale on every platform.
  A trace recorded on a phone replays on a desktop.
- iOS runs the kernel at close to desktop speed with no JIT
  entitlement, if §3 holds on a signed device.
- We own patches for a Tier 3 target and an unsupported loading path.
  The pieces are public in 49.0.3 (`deserialize_raw`,
  `CustomCodeMemory`, `with_custom_code_memory`), but upstream neither
  supports nor tests running code in place from a binary's own
  section. Wasmtime is pinned. Every upgrade reruns `kernel check`
  through the in-place loader in a hardened-runtime macOS binary with
  no JIT entitlement. That binary needs its own macOS-targeted
  `.cwasm`, because loading checks the artifact's OS and architecture
  against the host. A signed iOS device run happens per release.
- Mobile binaries carry the 48 MB `.cwasm` instead of the ~29 MB wasm.
  With the in-place loader it is mapped from the binary and paged in on
  use, not copied: anyrt sat at 13 MB RSS after load on the Pixel.
- The compiled-code cache helps desktop too (~0.4 s per run).
- Companion PR, not part of this ADR: guest fuel governors read
  `remaining/budget` from `fuel.state` instead of absolute values
  (gmailSync's `_FUEL_FLOOR`), so budgets keep their meaning if units
  ever change.
- Out of scope: OAuth consent on mobile (an ADR-011 amendment; v1 enters
  credentials on desktop, and ADR-021 values are account-scoped),
  triggers on phones, automatic failover.

## Resolved in review (2026-10-16)

- `native_unwind_info`: `false`, frozen everywhere (§4).
- `EPOCH_TICK_MS` on mobile: 50 ms. Its consumers are the fuel gauge
  (at 5.8 G/s, 0.3 G of staleness against gmailSync's 4 G floor) and
  break and deadline latency. One ticker per Cage, so one per process.
- Compile job timing and budget: right after the run, 20 G and 10 s per
  key (§7).
- Simulator CI: an `aarch64-apple-ios-sim` job is a cheap PR gate for
  the build and the loader plumbing, next to the hardened-runtime macOS
  job. It does not prove §3: the simulator has no signed-code
  constraint. The macOS job is the proof, and a signed device run per
  release is the check.

## Open questions

1. Android `memory_init_cow`: confirm on the Pixel that `memfd_create`
   is allowed in the app's seccomp profile, and keep CoW images on if
   so.
