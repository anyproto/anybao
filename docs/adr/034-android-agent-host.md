# ADR-034: Android host for an always-on bao

Status: **Stage 0 accepted on 2026-11-03; K1–K7 measured GO on 2026-11-05; Stage 1 accepted for implementation on 2026-11-05**
Date: 2026-11-03
Builds on: ADR-009 §6 (embedding), ADR-015 (election), ADR-025 (presence),
ADR-023 (traces), ADR-011 (OAuth), ADR-021 (credentials). Platform-local
grants (the ChatGPT-local OAuth proposal) are unsupported on the phone (§2).
Review: [review record](034-android-agent-host-review.md).

## Context

Run bao on the user's Nothing Phone (2a), normally connected to power,
with the screen off and the UI closed. The installed Android/Nothing OS
version must be measured, not inferred from “latest.” `any-kotlin` embeds
Go `any` and provides login, synced spaces, chat, credentials and device
selection, but does not run Rust `anyrt`.

There is no permission that guarantees uninterrupted execution. Charging,
a foreground service and a wake lock improve availability. Force-stop,
process death, network partitions and reboot before first unlock remain
boundaries. This PoC proves availability on one measured device/OS; it
cannot promise an unattended recovery from every reboot.

The full host uses separate Go/main and Rust worker processes, explicit
headless boot and an 80-hour acceptance plan. A separately accepted native
spike first tests artifact/real-turn fit, then an eight-hour supervised
screen-off probe tests early foreground-service viability.
Findings, source verification and review dispositions live in the
[PoC notes](../android-host-poc-notes.md).
Work order, dependencies and milestone gates live in the
[implementation plan](../android-host-implementation-plan.md).

## Decision

### 0. Staged acceptance and feasibility gate

Inventory and source inspection may precede acceptance. **Stage 0** and
**Stage 1** require separate explicit acceptance recorded here and in the
ADR index. The user accepted Stage 0 on 2026-11-03 by directing implementation
to begin after the final review. This authorizes the S−1a preparation and
S0 work. K1–K7 subsequently passed the measured review linked below.
On 2026-11-05 the user explicitly accepted Stage 1: “lets go for stage 1”.
This authorizes the full opt-in PoC implementation and its planned checks;
it does not establish any unmeasured qualification result. Pinned-source
verification remains required for each affected integration. The user's
no-further-reboot instruction remains binding: record T6 as unavailable
until separately authorized, and resolve inactivity-reboot settings before
the long locked qualification campaign without changing security settings
implicitly. Stage-0 acceptance covers
this section, its referenced native build constraints and the explicitly
limited S0b foreground/notification/battery/wake-lock behavior. Add
only its relevant reciprocal ADR pointers then; amendments for the full
host follow Stage-1 acceptance. A stage result does not accept the next.

Stage 0 uses an isolated checkout and a non-debuggable `baoPoc` variant
with its own `org.any.anykotlin.baoPoc` ID, initialized from `benchmark`.
It never installs over, wipes, signs out or changes the network of the
shared `.dev`/devpass installation. Reserve an idle devpass window; if
its real-data server cannot be kept stopped, use another device. Apply
§1's Firebase exclusions/no-op bindings immediately, with
`SENTRY_ENABLED=false` and Amplitude disabled in **both** processes.
Go uses the pinned AAR and a fresh throwaway account. The normal app and
all host controls remain off by default.

**S0a, visible native spike:** a temporary test screen binds a plain
`:bao` worker, with no main Hilt graph/Go/telemetry in that worker. Use a
thin JNI adapter over `run_program` and a deserialize-based Cage with its
epoch ticker. Neither `serve::start`, `run_config_store` nor
`AnyTraceStore::new` is allowed: these provision or mutate stores. Pin
in-memory config (`llm.tier.*` and model profile), and enter the phone's
test key on a masked local screen, send it once to the fixed worker and
seed `llm.key.<provider>` in memory. Never log or sync it; discard it on
retirement. On 2026-11-03 the user explicitly chose the same local test key
for the phone and desktop canary. Record equal key labels and shared quota;
the probe is a separate request path, not an independent credential or quota.
Provider attribution still requires the actual failed request/transport evidence
and same-minute canary, as specified below.

Use the actual `toolcaller@v1`, normal prompt/skills/history/autorecall
assembly and `llm@v1`/broker against preseeded synthetic chats. There is
currently no tools-off argument: Stage 0 includes a narrowly scoped
`allowTools=false` fixture option, retaining the ordinary default, and a
broker guard rejecting model-driven tool execution. Do not use `quiet`
mode to skip history. Bundle frozen Python sources as APK assets, including
the fixture entry, modified toolcaller, llm, history, autorecall, any and all
their dependencies. Verify/extract them into app-private fixture storage;
use `LocalDirResolver` behind a fixed manifest spec/alias map, including
`agent:` imports and one explicitly bound per-turn fixture-space alias.
Include shipped tool-doc modules as well as the execution dependencies;
normal prompt assembly must not silently omit their docs. Reject unlisted specs; no `AnyModuleResolver` or network
source fallback. Desktop calibration uses the identical bundle bytes and
spec map. Skills/tool docs remain seeded fixture data from that revision.
Record bundle/per-module hashes and every resolved source hash per turn.

Freeze five small and five representative context templates (the latter
have 20 prior turns and 10 synthetic memory items, with normal skills/docs).
Preseed a fresh isolated working-space/chat copy for **each** measured turn,
including every K6 batch, all 32 K7 turns and any allowed retry. Bind that
turn's bao.space/codeSpace to its copy; history, recall and mutable memory/
ROI records must not see earlier turns' output. Preseed all copies before
the timed batch/probe; do not clone/reset inside its latency interval.
This also isolates autorecall's access-counter writes. Record the template,
actual input-context hash, bytes/history count and ID mapping per turn;
normalize only declared fixture IDs for template comparison and retain the
raw reads. Drift makes the affected measurement inconclusive. Permit the
normal synthetic reply/history/recall-bookkeeping writes only inside that
turn's copy, and retain them as output evidence, never as a later input.
The explicit method/path/object allowlist covers these fixture data routes
and the chosen public HTTPS provider, with redirects disabled. No arbitrary
programs, registry/auth/debug writes or real connectors.

Reuse the existing `MemTraceStore` behind a spike-only feature (it is
currently `cfg(test)`), with bounded drain/export; never Go local collections
during S0. Export synthetic traces and samples to `getExternalFilesDir`; prove
`adb pull` access on this exact non-debuggable build before collecting a
batch. If inaccessible, explicitly select a local SAF export directory
and prove a round trip first. Display the captured Go port/session on the
test screen; do not depend on benchmark Timber logs or `run-as`. Trace
`meta.mocked=false`, provider response/request IDs where supplied and usage
on the phone's key must reconcile. These are real feasibility turns, not
proof of chat-watcher delivery.

A recording-only `ResourceLimiter` reports successful guest-memory growth
and table high-water marks for each store and their sum; failed growth is
not counted as allocation. It does not measure native/host memory. Keep
two initialized idle CPython stores alive alongside each chat turn. Report
the chat's peak against 256 MiB and the aggregate against 768 MiB; Stage 1
still needs enforcement and full concurrent workloads. Measure provider
wait as the union of traced request/retry intervals; report remaining
elapsed time as on-device/Go overhead, not CPU time.

Pin `max_wasm_stack=512 KiB`, `async_stack_size=2 MiB` (calls remain
synchronous) and an 8 MiB Rust thread stack in EngineProfileV1. Run wasm on
that explicit Rust thread, joined from a background JNI caller; never on
the UI/Java default stack. Rust never calls the JVM. These are proposed
starting budgets, not measurements of Android's default thread stack.

For K6, measure the phone caller interval from immediately before the JNI
request through its return, including native preparation and trace export.
Keep owner dispatch-to-verified-admission time separately. The owner has a
130 s watchdog for the whole exchange, allowing 10 s for IPC and subsequent
durable evidence handling; this does not relax the JNI caller limit of 120 s.
Check both elapsed limits at admission even if a timeout callback was delayed.
The K2 owner watchdog remains 5 s. Missing, invalid or overdue evidence never
supports a pass. After an abrupt Wasmtime trap, fuel counters may be stale
snapshots; retain them with an explicit precision label, not as exact usage.

A run fixes worker session, account and baseUrl; disable account/server
replacement during it. In S0a, leaving the Activity cancels/retires the
experiment. Unbind before self/same-UID `Process.killProcess`, confirm
death via Binder plus process identity, then rebind; recreated workers are
inert. “Fresh” means this verified process replacement, **not** cold disk
cache. Collect ten fresh loads, of which the first after a recorded reboot
is labelled cold; never claim caches were dropped. Time load from immediately
before `System.loadLibrary` through engine creation and component
deserialization, excluding later store instantiation (report that separately).
Before loading the library, sample the empty-worker control, then sample
the no-live-store loaded baseline. Record RssAnon/RssFile/RssShmem from
`/proc/self/status` and PSS from `smaps_rollup`; gate K3 on incremental PSS
relative to that control, retaining total RSS and PSS. Sample both processes
each second during turns and record boundary `dumpsys meminfo`.

K2 has five fixed 40-case groups, with input/artifact hashes and expected
classification: successful Python cells; guard-page out-of-bounds accesses
in a second trusted precompiled test component (`MemoryOutOfBounds`);
that component's infinite loop with 1,000,000 fuel (`OutOfFuel`); its loop
with ample fuel and an epoch interrupt requested at 100 ms (`Interrupted`,
not `OutOfFuel`); and non-tail recursive wasm calls (`StackOverflow`).
Use the 10 ms epoch ticker and a separate 5 s test watchdog. Record the
underlying Wasmtime trap/callback cause before wrapper error translation.
Verify the OOB artifact's generated instruction/fault mapping exercises
the signal-handler path; an explicit bounds-check trap alone does not pass
this arm. Test CPython recursion separately as a diagnostic; its Python
exception is not proof of native-stack containment. No deliberate native
crash is counted among the 200 successful trap-containment cases.

| Gate | GO condition | PIVOT / STOP review |
|---|---|---|
| K1 | Trusted artifact loads with recorded profile/ISA/source hashes | STOP after the initial profile plus at most two explicit corrections still cannot load; never use device compilation. |
| K2 | All 200 classified cases above pass; zero unexplained native signals | An unexplained native signal stops immediately. Missing signal-path evidence is INCONCLUSIVE. |
| K3 | Loaded, no-store worker incremental PSS ≤128 MiB above its empty-worker control | >128 MiB requires a budget PIVOT; >256 MiB stops for viability review. Record total resident memory and instantiated-store overhead too. |
| K4 | Each turn with two idle stores: main RSS ≤1 GiB, worker RSS ≤896 MiB, combined PSS ≤1.0 GiB; chat guest peak ≤256 MiB and aggregate ≤768 MiB | Excess is PIVOT; combined PSS ≥2 GiB or a §6 thermal limit stops immediately. This does not replace T8. |
| K5 | Maximum load time across ten fresh processes ≤10 s | >10 s requires startup-budget PIVOT; >30 s stops for viability review. |
| K6 | 10/10 real turns succeed, each caller latency ≤120 s | At n=10 nearest-rank p95 is max; no redundant 180 s rule. Host failure is PIVOT. Proven external failure invalidates the batch. |
| K7 | S0b below: 8 h screen-off on charge, 32/32 fixture turns ≤120 s, no process replacement, wake-lock lapse, evidence gap or K4/thermal violation | Host failure is PIVOT; unexplained native signal is STOP. A proven external/fixture failure is INCONCLUSIVE, never background GO. |

The initial main-process RSS budget was 640 MiB, a conservative planning
choice rather than an Android restriction. On 2026-11-05 the user explicitly
accepted approximately 1 GiB after the key-free preparation diagnostic measured
959.85–970.45 MiB. The revised main limit applies prospectively to a reviewed
candidate; preserve the original measurements and source limits. This does not
raise the combined PSS, worker, guest or thermal limits, or establish K4.

K6 external attribution requires the actual request/retry/status or
transport-error trace plus a desktop provider canary in the same minute;
a nearby canary miss alone is insufficient. Allow at most two batches;
two externally invalid batches yield INCONCLUSIVE and a user decision,
not a third automatic batch. Missing measurements are also INCONCLUSIVE.
PIVOT requires an ADR amendment/explicit decision; STOP ends the experiment.

On 2026-11-05, the [measured four-lens review](../plans/android-host/reviews/round-8-results.md)
closed K1–K6 and cleared minimal S0b implementation. Retain the qualified native
bytes for this foreground-service experiment; the known metadata-summary
limitation requires raw trace reconstruction for every turn before acceptance.
An automatically completed probe therefore remains **review pending**, not GO.
Its owner must retire the worker, drain accepted evidence writes and stream a
fresh per-session export with a verified hash before releasing its wake lock
and foreground ownership. Export failure preserves private evidence and an
incomplete result. This keeps completion independent of UI/ADB availability.

On 2026-11-05, the [final measured review](../plans/android-host/reviews/round-13-results.md)
closed K7 after the full Mac-USB probe and reviewed additive evidence-order
proof. The [measurement report](../plans/android-host/reports/s0b-phone-8h.md)
preserves the original auditor failure, correction and explicit disposition.
This completes the accepted Stage-0 measurement; the user subsequently
accepted Stage 1 on the same date as recorded above.

**S0b, early background probe:** the user chose the 8-hour probe on
2026-11-03; the later implementation instruction accepted Stage 0,
including this probe.
After K1–K6 pass, add only a minimal main-process `specialUse` FGS owning
the spike worker, persistent notification/in-app Stop, verified battery
exemption and timed partial wake lock as in §3 (renew every 60 s, 180 s
timeout). Only those FGS/notification/battery/wake permissions apply; no
boot/backstop alarm is added in S0b. Start manually from the
visible screen, return non-sticky, and stop on owner/worker death or any
gate failure; no boot receiver, automatic retry, election, production host
modules/protocol or account-transition journal. The main owner runs 32
fixed toolcaller turns at t=0,15,…,465 min, each using its fresh context copy
and the same two idle stores, then watches through hour 8 on continuous external
power, screen locked. A wall charger is the default. For the user's 2026-11-05
overnight request, Stage 0 also permits USB power from this mains-powered Mac
with its lid open and bounded sleep inhibition covering setup and the full
probe. Record this as a Mac-USB run, including Mac power/lease evidence and the
phone's actual plugged-source/status samples; it is not wall-charger evidence.
The phone owns scheduling and durable evidence independently of ADB and the
Mac. Lost external power invalidates the charged interval; missing power
evidence is inconclusive. Stage 1's wall-charger requirement is unchanged.
Add a spike-local main-process `POST /v1/events` call now,
verified against the pinned server; the Stage-1 client binding is not a
dependency. Publish only synthetic progress/result IDs every 10 s for the
desktop observer. This at-most-once bus is advisory: a 60 s remote progress
gap alerts the operator, but neither stops the phone nor proves failure.
Judge K7 from the phone's durable local progress timestamps, wake-lock and
process-identity records plus turn metrics. A turn exceeding 120 s, a local
progress gap of 60 s or a demonstrated lifecycle/resource violation fails
the applicable gate; missing local evidence is INCONCLUSIVE. No wireless-ADB
dependency. Keep the operator available to respond to alerts.
Allow at most one externally invalid retry, after fixture repair; a host
failure needs review before any repeat. Retire on Stop and discard keys.

Only a complete K1–K7 report can recommend Stage-1 acceptance. It proves
limited screen-off viability, not boot recovery, election, chat-watcher
delivery, long-term retention or 24/7 availability. General egress, full
IPC/lifecycle modules, AAR publication and the 16 KB emulator gate remain
Stage 1. No shared-process/JIT/Termux fallback is implicit.

Use the [S0 report template](../plans/android-host/s0-report-template.md)
with a shared run header and adjacent raw-value JSON for K1–K7; no Stage-1
evaluator is required. The pre-acceptance freeze of §§1–7 ended with the
user's Stage-1 implementation instruction on 2026-11-05. Complete S−1b's
pinned-source checks before the affected integration, and review the
implementation across native, lifecycle and evidence concerns, preferring
simplification where measurements permit it. Preserve the frozen Stage-0
measurement artifacts and audit history.

### 1. Scope, process model and build variant

Integrate in the existing any-kotlin source tree, with a dedicated
`baoPoc` build type initialized from `benchmark`: non-debuggable,
release-like shrinking, debug-signed for local installation, separate
applicationId `org.any.anykotlin.baoPoc`. Use an opt-in “Run bao on this
phone” control on Agent / Devices, off by default. This reuses the app's
onboarding and UI without opening an existing installation's data.
Set dependency `matchingFallbacks` explicitly to `benchmark`, `release`,
then `debug`; do not accidentally inherit benchmark's debug-only fallback.
Inspect the resolved modules and merged manifest as well as app flags.
With the switch off, ordinary app use starts neither BaoHostService nor
the Rust worker; the UI's Go runtime remains available. Enabling the host
starts preparation/standby and its FGS; activation is a separate action.
Turning it off stops both host components without requiring logout.

`baoPoc` is **FCM-free**: exclude its Google Services resource generation,
Firebase initialization providers, FCM service/auto-init and registration
calls, including logout's Android token deletion; bind a no-op token
store/push session in disjoint variant sources, moving the real binding out
of `src/main`. Use `tools:node="remove"` in the variant manifest for the
providers/service. Merely disabling the Gradle task is insufficient. Do not register a Firebase
client or use the production client ID for the new applicationId. Normal
variants retain their push behavior. Notification permission and the
host's local foreground notification remain independent of FCM. This does
not disable Go's internal push-node pairing/chat notification traffic;
inventory it against the pinned Any source/config and include those
non-analytics destinations in T11. No upstream push-off flag is assumed.

**The first PoC uses a fresh throwaway Any account, synthetic data and
budget-limited test API keys only.** All devices in its test fleet use
that account. Do not restore a personal account or attach mail/calendar
or other real-data connectors. Android apps with `INTERNET` can reach the
current unauthenticated loopback API and read secrets or submit agent
instructions. A separate process does not fix that. This limitation is
shown during setup; the PoC is not approved for real-account operation.

Before real-account use, `any` must enforce an unpredictable,
per-server-generation bearer token on **all** HTTP routes and upgrades
(including meta, files, SSE and mutations), handed only to trusted clients
in process / Binder, never logged or persisted. The existing control
token protects selected management routes only. This is an upstream
prerequisite, not a loopback proxy workaround or an implemented feature.

| Model | Decision |
|---|---|
| Separate APK with its own database | Duplicates onboarding/device identity; unnecessary for this experiment. |
| Separate APK relying on any-kotlin's server | Does not own the database process lifetime. |
| Same APK, Rust beside Go and UI | Avoids Binder but native aborts/OOM affect all components; no independent forced-stop boundary. Rejected. |
| Same APK, Go/main foreground owner + Rust `:bao` worker | Selected. Reuses one database and account, isolates Rust crashes and permits bounded worker termination. |
| Termux / PRoot | Adds a Linux userspace and a different operational stack. |

`BaoHostService` runs in the **main process**, owns the notification,
wake lock, policy and supervisor, and retains the singleton `AnyRuntime`.
It binds `BaoWorkerService` in `android:process=":bao"` using
`BIND_AUTO_CREATE`, without waiving importance. Both services are
non-exported; Binder entry points validate caller UID and session identity.
The worker is bound-only. An active binding can still recreate it after
a crash; §4 explicitly retires bindings before termination/retry. Its
process hosts ART and Rust/Wasmtime, **never Go**. This is crash isolation,
not a security sandbox: both processes share the application UID.

Only main owns persistent policy/state. Worker config and status travel
via Binder; no multiprocess SharedPreferences. Set `SENTRY_ENABLED=false`
and `AMPLITUDE_ENABLED=false` for `baoPoc`,
including main. Gate Application/Hilt initialization by process before
resolving lazy dependencies: the worker initializes no main app graph,
telemetry, theme observers or Go runtime. Default-process content
providers are not assumed to run in `:bao`; inspect merged provider
process declarations. T1 checks worker maps for absence of `libgojni`
and `libsentry`, and T11 checks zero telemetry traffic. Main's
headless foreground path must also avoid blocking keystore/theme work
before notification promotion. A live main FGS protects the database
process; a lone FGS in `:bao` would not establish that protection.
Use process selection before `super.onCreate`, Hilt `@CustomInject` with
main-only explicit injection, and a plain worker service without
`@AndroidEntryPoint`. Custom injection does not prevent other entry points
from constructing the graph: inspect generated code and providers. Keep
unavailable/real host bindings in disjoint variant source sets.

Main passes a session Binder to the worker. On main Binder death the
worker closes admission and requests its own termination. This callback
may be delayed by freezing; it is not proof the orphan stopped. Before
loading Go, a new main enumerates its own UID/processes and retires any
`:bao` survivor under §4. A stale worker may never reuse a later server port.

Put the implementation in `:agent-host-impl`, the UI contract in
`:agent-host-api`. Only `baoPocImplementation` depends on the impl/AAR;
other variants bind an unavailable implementation. No transitive runtime
dependency through feature modules. CI inspects the release APK/manifest
for absence of host components, newly added PoC-only permissions and
anyrt native code; existing app permissions are not removed.

### 2. Native build, kernel and capabilities

Build the Rust JNI adapter for `aarch64-linux-android`, API floor 28,
with the pinned Rust toolchain and NDK 28.1.13356709. The PoC host feature
itself supports API 34+; older devices keep the unavailable UI binding.
Package native code through normal APK native libraries, checking 4 KB
and 16 KB page-size loading. Keep shell effects off.

Precompile the **CPython component** at build time for the ARM64 Android
engine target. Embed the trusted serialized artifact in the native
library; load it using `Component::deserialize` from those bytes only.
Never deserialize an artifact from a space, download, user input or
writable cache. The Android runtime build **must** omit the raw kernel,
runtime Cranelift and compile cache.
There is no cold-compilation fallback on the phone. Failure to load is
`IncompatibleArtifact`, not a reason to compile or download native code.

Pin the exact Wasmtime version (currently Cargo.lock: **49.0.3**), Cargo
features, engine configuration, ARM64 CPU baseline, source-kernel SHA256
and artifact SHA256 together. Build-time and runtime engine configuration
come from the same definition, including fuel, epoch interruption and
trap mode. Use separate Cargo invocations for the precompiler and Android
library to avoid feature unification. Wasmtime and wasmtime-wasi both use
`default-features=false`. The Android Wasmtime feature allowlist is
`anyhow,runtime,std,component-model,async,backtrace`; WASI uses `p2` only.
The host precompiler adds `cranelift,all-arch,parallel-compilation`. Gate
`Cage::new`, `Cache` and `EMBEDDED_KERNEL` behind a desktop/compiler
feature unavailable to the Android library. Agent Python source never
needs Cranelift. T1 rejects any target dependency on `cranelift-codegen`,
even if introduced transitively by WASI or another workspace member.
The runtime crate's default `desktop` feature must explicitly retain
compiler/cache/control/loopback-OAuth dependencies for desktop consumers.
Android depends with `default-features=false`; make `tiny_http` optional
and the CLI bin require `desktop`. Keep a shared locked dependency graph,
and test the any-ui path consumer as well as default/shell builds.

A shared, versioned `EngineProfileV1` builder explicitly sets every wasm
proposal switch and tunable used by the component, rather than inheriting
feature-dependent defaults. Baseline: component model, bulk memory,
multi-value, multi-memory, reference types and SIMD on; threads, GC,
relaxed SIMD, memory64, stack switching and component async proposals off;
synchronous component calls, fuel/epochs and signal traps on; on-demand
allocation, 512 MiB memory reservation, 64 KiB guard, zero reserved growth;
`max_wasm_stack=512 KiB`, `async_stack_size=2 MiB`, and an 8 MiB native
Rust run-thread stack (§0). Do not execute wasm on an implicit Java stack.
Record the complete resolved profile/CPU flags as a build artifact and
compare its hash at load. Test this baseline against the source kernel;
any required proposal/tunable change is an explicit manifest/profile
revision, applied to both builds. The deserialized artifact copy counts
toward worker resident memory, not just APK size.

Stage-0 profile correction 1 (2026-11-03): enable **extended const** in
both builders and record `profile_revision=2`. The initial baseline
rejected the existing kernel (SHA256
`699bb3255d7b74b6a980bb7b7fb90f634ce0802f6f8faed117de42857161aed1`)
at offset `0x116e05b`: `constant expression required: non-constant
operator: i32.add`. Its original build provenance is unknown; repeat
the compile with the locked guest builder before freezing the artifact.
This consumes one of K1's two allowed profile corrections; a host-side
compile is not a phone load pass.

Explicitly record target triple/ISA, backtrace detail parsing, native debug
info, memory-init COW, reservation/guard and signal-trap choices. Use the
same resolved profile on each side; clear `WASMTIME_*` only in the separate
precompiler environment, never mutate ART's process environment. Source
kernel identity comes from the manifest, distinct from the artifact hash.
Check two clean kernel builds; if they differ, fix provenance or freeze
the exact source-kernel bytes rather than claiming reproducibility.

The generated manifest contains these plus anyrt revision,
JNI protocol version, Rust/NDK versions and the tested minimum/compatible
`any` version. Do not label the current `any v0.3.5` pin compatible until
it passes the integration fixture. Refuse incompatible pairs before runs.
The earlier server-source review used local v0.2.3-nightly.20260925.1,
not this AAR source. Recheck §§3/5 routes and registry semantics at the
pinned tag before treating them as available; incompatibility blocks setup.

Add Android AAR/symbol packaging to the repository's workflow (currently
desktop tarballs only). Qualify locally built, hash-recorded bytes; CI
publication may proceed separately and must upload the exact qualified
bytes. Pin version/checksum with Gradle dependency verification, including
mavenLocal resolution. Android packaging checks must prove the source
kernel is not redundantly embedded.

Before the first Stage-1 APK, define an optimized Android Cargo profile
with `panic="unwind"`, `strip=false`, `debug="line-tables-only"` and
build IDs; archive matching symbols and R8 mappings. Preserve JNI symbols
with keep rules. Kotlin-owned threads call JNI; Rust threads never call
the JVM. Wrap JNI entry points in catch_unwind and report a terminal panic
flag; this does not catch aborts or signals. Kotlin polls native health
and emits Binder callbacks. Its worker scope also updates native time
context from Android at startup, on a timezone change, at the zone's next
offset transition and on a wall-clock change (`ACTION_TIME_CHANGED`),
without a Rust-to-JVM callback from time.now; the effect still records the
supplied context, and a stale sample refuses the whole effect (below). Add a redacted logcat tracing subscriber and
bounded file log tree (Sentry-off release currently has no Timber tree).
`baoPoc` is shell-profileable, still non-debuggable. Verify tombstone
collection and symbolization on that build before qualification; an API
31+ native trace stream is protobuf, so decode/convert it before using
ndk-stack/LLVM symbolization. No assumed readable crash file is evidence.

For the PoC, “channel” means a dedicated test overlay pair deployed to
throwaway repo spaces. Pin each deployed content hash and its source
revision separately from the runtime/AAR revision, and validate their
compatibility. Record space IDs and guest invites in the fixture manifest.
Do not deploy to stable/nightly spaces or follow their moving contents.
Changing these test overlays invalidates the soak and requires revalidation.

Stage-1 amendment (2026-11-05, N1 review): the pins cover **everything the
guest can load** — every program (deploy fingerprint plus a separate digest
of its raw `credentials` declaration) and every skill (`agent_skill` body
hash) of every configured overlay; an overlay without pins must hold neither,
and a pin naming an unconfigured overlay is a setup error. The pins also name
the kernel source hash and engine profile hash beside the runtime revision and
kernel protocol. Preparation verifies the programs once and captures the
content: the managed module resolver serves programs **from that verified
snapshot only**, never a live read, so `use()` performs no HTTP and a redeploy
after readiness cannot run; the declared credential table is built from the
same verified **overlay** rows and never refreshed — a working-space program
declares nothing, whatever its `credentials` property says (ADR-021 §8.1:
declarations are manifests deploy wrote into spaces the guest cannot write).
The working space's own programs present at explicit setup (including §6's
`android-poc-heartbeat@v1`) are pinned like an overlay's under the reserved
`working` alias, so the resolver serves them, by alias, unqualified from a
cell, or by the space's raw id as the toolcaller spells its tools; a program
authored by the agent during a run (ADR-013) is refused with the typed
`NotPinned` miss on the managed path — the guest sees the ordinary `KeyError`
from `use()`, the run result lists the refusal in `refused_programs` (never
in ADR-009 §8's `missing_programs`, which would re-run the message), and
health counts it (summed over the run results the lanes report) — a default
recorded for a user ruling; so is the
consequence that a program or skill authored into the working space after
setup makes the next preparation `NeedsSetup` until it is re-pinned. Skills
are read live by the guest over the data routes: their pins are verified at
preparation and at every Open before dispatch, and not again while open —
desktop verifies none (amended 2026-11-06 by the user's ruling, N2b-2 design
§10 R6: desktop parity). A skill edited while open reaches turns until the
next Open, whose re-verification then finds the drift: no Open completes and
the worker reports `NeedsSetup`; a live run reads skills on demand for the
rest of its budget. Drift is `NeedsSetup`
(reason `Invalid`); a pinned object not yet on the device — a program whose
manifest record or a skill whose body has not arrived included — an unsynced
working bundle, a failed read or a failed device-local collection create is
`NeedsSetup` (reason `NotSynced`) — reported once, with no native retry or
sync poller (§5): main's monitor re-requests preparation. Absent and
not-yet-synced are told apart only at first preparation: at a re-verification
the pins were verified present and non-empty on this device, so a pinned
skill emptied, or its skill type gone, since then is drift. A device copy
still at the *previous* version of a pinned object (setup pinned from the
desktop's read-back before the phone synced the redeploy) fails its
fingerprint or digest and is `Invalid`, not `NotSynced`: setup pins only
versions the phone's own server already reads back (Stage-1 amendment
2026-11-06, Step B corrections; setup trap 6 in
[environments.md](../environments.md#android-poc--explicit-setup-manifest-fields-stage-1-adr-034-25)). A key with no bound hosts
anywhere is not usable; the managed broker takes a credential's destination
from the declared table or its stored row only, never from the guest request,
and never binds a row from first use; the host transport refuses every
`hosts` write.

All account-scoped file paths are absolute under app-private account
storage. Disable Android's anyrt control listener at compile time and set
`ControlApi::Disabled` (§7); port 0 is not disabling. No `POST /run`
listener may be constructed on Android. JNI/Binder supplies status and
lifecycle; the existing Any API remains the data transport.

Supported credentials: test provider API keys in the existing synced
secret store, with all configured model tiers verified usable before
activation. **ChatGPT-local grants and initiating connector OAuth are
unsupported in this PoC.** A typed `UnsupportedCapability` is returned
before opening a listener/browser or reading a local grant. Set an
explicit API-key-only policy: managed connector OAuth credential injection
and refresh also return `UnsupportedCapability` before token/grant reads,
and readiness rejects those configured credentials before activation. Set an
explicit rejecting consent hook; never fall through to `xdg-open` or
post a consent URL that the unattended phone cannot complete. A fresh
presence beat alone is not sufficient readiness for “Use this device.”
LAN/private/link-local model endpoints and MCP servers are also unsupported
by the PoC egress policy. Readiness rejects such configured endpoints
before activation, rather than discovering the denial during a turn.

Use rustls and pinned public roots for outbound TLS. This does not inherit
Android user CAs, network-security-config or proxy/PAC settings. No
certificate-validation bypass. Any added HTTP stack must use rustls;
proxy/custom-CA environments are unsupported until separately validated.
Test direct HTTPS and IPv6-only access. Supply zone ID and offset from
Android at the recorded `time.now` effect, handling timezone changes;
never change global `TZ`/`HOME` in ART. Each sample carries the window in
which its offset is valid: from the previous transition (or the sampling
instant) to the zone's next transition, DST included; a sample whose end has
already passed is refused. Android refreshes it at startup, on timezone change,
at the next transition and on a wall-clock change (`ACTION_TIME_CHANGED`).
Outside that window — including a wall clock set
back before it — or after a failed update, the whole `time.now` effect is a
typed refusal (`TimeContextStale`, fail closed: no epoch is served either)
until a fresh sample arrives; native health reports the staleness computed
from the sample itself, never from a latch — the window is held as one word
in whole seconds (start rounded up, end rounded down), and health and
`time.now` apply the same rule to it; a bound the word cannot hold (past
2106) makes the sample always stale, never a widened window. Scheduling still
follows the existing scheduler contract, not an implicit new local-time policy.

Use RAII for run registration and mailbox completion, and report thread
panics/unexpected essential-thread exit through `health()`. A JNI
`catch_unwind` catches only unwinding panics on that call stack; it does
not contain aborts, signals or other threads. After a panic, retire the
worker instead of continuing with poisoned state. ART/Wasmtime trap
handling still needs testing in `:bao`; Go's signal handlers are no longer
in that process. Test wasm traps, Java exceptions and tombstone/symbol
capture; test Go recovered panics independently in main. A non-signal
Wasmtime fallback requires a measured engine/artifact revision, not a
silent runtime toggle.

Admission uses three class lanes: one chat store (256 MiB guest budget),
one scheduled-job store (384 MiB), and one event-source store (128 MiB).
A shared `ResourceLimiter` caps all live guest memories at 768 MiB and
caps each lane across all its memories; tables and host buffers also
have explicit bounds. These are ceilings, not guaranteed reservations.
The explicit bounds (2026-11-05, N2a): per store 65,536 table elements
(the Stage-0 phone peak was 11,520), 16 instances, 4 memories, 8 tables;
host buffers: a run mailbox of at most 256 items and 1 MiB, a deferred
chat backlog of 64 inputs (on the managed path, the inputs that wait for a
chat run not live yet, N2b-2), an 8 MiB event-stream line, and the 64
pending descriptors. N2b-2 adds: at most four event-source streams; a fifth
desired source is logged and counted, never silently dropped. Event records
are handled sequentially on their source thread, as on desktop, each fire
waiting for the event lane. Lanes, aggregate and descriptors are unchanged. Each bound is
enforced, and its saturation counted as a capacity fault, where it is
refused (mailbox, backlog, pending queue, leftover sink, stream line); the
stream line is bounded at the read itself: the managed transport reads at
most 8 MiB + 1 bytes of a line, so an over-long line is never buffered
whole (2026-11-06, Step B corrections).
The 1.5 GiB whole-app PSS target leaves about 768 MiB for Go, both ART
instances, native heaps and the artifact copy; T8 must measure that fit.

A new `CapacityDeferred` admission result is **not a run failure**: it
changes neither failure counters nor `lastRunAt` and cannot auto-disable
a trigger. Acquire the lane/memory permit before consuming a once shot,
advancing an admitted cron occurrence, or marking event input seen.
The lanes' reopenable admission gate starts closed (nothing before the
first AppliedOpen) and is open only while no stand-down cause holds —
main's admission policy, or the reserved worker-local cause that no worker
code holds since memory is main's (below) — so a renewal never reopens a
gate another cause closed. Every permit records the revocation epoch of its
acquire: an interrupting stand-down (Release, Stop) also cancels a job whose
permit was taken before it but which had not started, which is reported
`Cancelled` with its input intact and never run (2026-11-06, Step B
corrections). A revoking Close (ownership loss, prune, deauthorization, user
Stop) by itself interrupts nothing: as desktop's stand-down, it lets live
runs finish, and the Release main sends with it interrupts them (amended
2026-11-06 by the user's ruling, N2b-2 design §10 R9). Every way a job
leaves a lane without running — interrupted before start, or cancelled in
its inbox, also when its worker never started — hands its mailbox to the
leftover sink and carries what the sink refuses in its report; a job that
unwinds before its report is handed over (a panicking run or leftover sink)
is counted orphaned once, and never both reported and orphaned. Accepted
residual: a report callback that panics before it delivers leaves its job
uncounted — the callback is the service's own, and its panic fails the owner.
Busy lanes retain bounded pending work (maximum 64 descriptors across
all lanes); a saturated queue becomes a visible capacity fault — counted in
health, and the worker's Status not `ready` while it holds — rather than
silently dropping events. The worker closes nothing on it and keeps
dispatching what it holds, as desktop has no such fault; main decides
(amended 2026-11-06 by the user's ruling, N2b-2 design §10 R3). An admitted
run is never retried within a worker. As on desktop, a once shot is
consumed in memory under its lane permit and persisted consumed at its
report, so a once whose run a worker death cut off fires again on the next
owner; a cron occurrence does not, since the next owner arms strictly
forward (§6 counts such duplicates; 2026-11-06 user ruling, R5). A new message for an existing chat goes
to that run's mailbox, without acquiring another store; `use()` likewise
remains inside its caller's store. Scheduler dispatch leaves the ticker
free to maintain health and pending work. The heartbeat gets priority
among due scheduled work, but does not interrupt the current job; T7
allows the existing 1200 s run wall cap plus dispatch margin.

Memory admission policy (amended 2026-11-06 by the user's rulings). The
primary trigger is the system: main reads `ActivityManager.getMemoryInfo()`
every 5 s and closes new admission (`Close(RESOURCE_PRESSURE)`) when
`lowMemory` is true or `availMem` falls under `threshold`, reopening after
30 s of samples clear of both. The per-process ceilings are a backstop,
sized from the device's RAM for a dedicated phone so that normal load never
reaches them: main ≤ max(1 GiB, 30 % of `totalMem`), worker ≤ max(1 GiB,
40 % of `totalMem`), their sum ≤ 75 % of `totalMem`; a sample of either RSS
over its ceiling, or of the sum over its own, closes admission the same way
(to be validated by T8, which keeps its current numbers until then). Kotlin
main enforces every rule. The Rust worker closes nothing on memory: its
execution dispatcher, which performs no HTTP, samples its own `VmRSS` every
5 s and reports the value and the sample's CLOCK_BOOTTIME time in
`health()`, from which main judges staleness (a worker sample older than
15 s is stale and closes admission, as before). The guest Wasmtime ceilings
are unchanged. The earlier numbers — main 1 GiB (the user's 2026-11-05
allowance, §0; measured Stage-0 main RSS peak 658.762 MiB), worker 896 MiB
= 1.5 GiB − the first 640 MiB main plan, and the 1.5 GiB sum, which
conflicts with Stage 0's recorded "combined 1 GiB" — are superseded by this
rule; the T8/K4 thresholds and the frozen F2 evaluator follow in a separate
change. Other Stage-1 resource limits remain as specified here and must be
measured under the full host workload. Do not depend on deprecated
running-memory trim callbacks on API 34+. Before T7, measure synthetic ADR-031 PDF conversion
and a mocked mail-sync payload; external mail connectors remain disabled.
An incompatible memory budget needs an explicit revision, not silent
limit removal. Idle epochs stop when no store is live; the epoch driver
ticks through cancellation until the last attached store drops, also when
its thread first runs after the cancel, and the supervisor likewise runs
its stop propagation (pool closed, live runs interrupted) when an external
token cancel landed before its thread first ran; service loops use
interruptible waits. The execution service (N2b-2) runs on owned threads
beside the lanes and the epoch driver: a dispatcher (the memory sample, the
due scan, dispatch and soft-break deadlines; it performs no HTTP, so no
escalation waits on a write), the stand-down writer (the one owner-clear
write), the responder-beat thread, housekeeping (the registry reconcile
and its stamps), retention, the chat watch and up to four event-source
streams; run bookkeeping — a chat run's
follow-ups and interrupted turn included — runs on the lane thread that ran
the job, as desktop runs it on the run thread. The chat watch connects only
while Open and owning the responder, and a stream thread exits at its next
frame on any close; a message for a chat run that is offered but not live
yet waits, in order, in the deferred chat backlog until the run is live or
its report re-dispatches it, as desktop's watcher holds a run's mailbox
before it starts (N2b-2). A scheduled or event trigger is reserved from
its admission until its report's last write landed: it cannot be admitted
again meanwhile, and its registry entry sits every reconcile out — also a
reconcile whose read began before the reservation ended (a scheduling
revision taken before the read, bumped when a report ends a reservation;
N2b-2 round A-r2 review B1) — so a stale
row cannot undo its breaker, schedule or consumed shot. An entry housekeeping
adopts or marks is not dispatchable until that stamp landed, as desktop
writes its stamps before its scan. A cron is armed by the scheduler's own
rule on the registry entry and consumed only under a lane permit; a once shot
is consumed in memory at admission (`enabled: false`) and persisted at its
report, as on desktop (N2b-2 round A review A1/A3/A7). Sampler CPU counts
against T8.

### 3. Foreground execution and headless boot

Main's service uses `specialUse` with the manifest subtype describing
user-enabled continuous chat and scheduled agent work. Declare
`FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_SPECIAL_USE`, `WAKE_LOCK`,
`RECEIVE_BOOT_COMPLETED`, `POST_NOTIFICATIONS`, and
`REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` in the PoC variant; retain existing
`INTERNET`. This is not a Google Play approval claim.

Every `onStartCommand` first performs the minimal notification promotion,
including repeated, boot, update and null-intent starts, then evaluates
policy. Disabled or unauthorized starts promptly stop again. Keep injected
graphs lazy; instrument process/Application/service startup so slow work
before the service does not exhaust the platform's foreground deadline.
Catch foreground-start restrictions/security failures on every entry path
and record a recoverable state. Receivers check persisted opt-in and stop
history before requesting the service, and never boot native code inline.

Notification status distinguishes Starting, Active, Standby, Waiting,
Stopping and Needs attention. Its Stop action is a convenience: denial
or dismissal can remove it. The in-app switch is the canonical Stop.
Notification denial is shown, not misreported as an FGS prohibition.

Setup requires `isBackgroundRestricted() == false` and a verified
battery-optimization exemption for the unattended claim. Show the system
prompt/settings fallback and re-read the state; opening settings grants
nothing. If restricted, enter Needs attention; do not claim readiness.
Verify actual Nothing OS settings. Hold a timed partial wake lock during
Starting, Running/Standby, retry backoff and Stopping; renew every 60 s
with a 180 s timeout from an independent supervisor timer. Record lapses,
release in terminal states. A single inexact `setAndAllowWhileIdle`
backstop, scheduled for four minutes after the latest renewal, may request
recovery if renewal stalls; delivery may be substantially later in Doze.
It checks enabled intent, exit history, transition/breaker state and
account identity before any start. Cancel it on explicit off, user stop
or terminal failure. It cannot override a user stop or guarantee uptime;
a wake-lock lapse fails the uninterrupted soak. The screen may sleep.
Unplugged availability is conditional on these settings and the
forced-Doze test, not on the wake lock alone. Add no Wi-Fi lock without
device evidence.

**Headless boot is an explicit exception to the current Activity-only
rule.** Enabled `BaoHostService` calls `AnyRuntime.ensureStarted()` without
`AppCoordinator`, then uses a generation-bound admission check:

1. Two successful authorized `/v1/auth` reads naming the enabled account
   bracket health with `account == recorded` and `bootstrapping == false`.
   Keep runtime generation and supervisor auth epoch unchanged throughout;
   missing fields/errors wait. Do not use the fail-open UI bootstrap monitor.
2. Read `/v1/devices`, bind the self peer and evaluate any saved claim
   before modifying it. Ordinary non-Active startup removes `apps.bao` with
   `PUT /v1/devices/me {"apps":{"bao":null}}` as the idempotent 409 prune
   probe, under the ReleaseOwed rule in §5. GET absence is not proof of
   prune. A bounded validated-resume check is the exception: preserve its
   claim read-only until §5 resolves it. Do not clear an Active claim as
   a reconnect probe. Every real registry write still treats 409 as terminal.
3. After bootstrap and fresh registry sync, seed the self name **once per
   account/peer**, only if blank/localhost. Persist completion; reconnect
   does not repeat a name upsert or overwrite the user's synced name.
4. Check artifact/server, overlay and supported-provider readiness. For
   activation/resume/reconnect, obtain the additional registry sync evidence
   in §5. No local readiness check alone permits dispatch.

A later Activity joins the same tracked Go generation. Push/FileProvider
activation alone still does not start Go. Recheck auth/health, identities,
compatibility and sync on each boot/sticky/native restart and reconnect;
the once-only name seed and non-Active prune probe are not repeated blindly.
Deauthorization, identity change or runtime transition closes main's gate
immediately and initiates worker closure under §7's bounded delivery rules.

### 4. Generation ownership, shutdown and recovery

`AnyRuntime` owns transition serialization and a pre-stop participant.
Its mutex stays private. It publishes an immutable atomic snapshot:
`(mainNonce, generation, baseUrl, gateOpen)`. A generation changes whenever
Go is replaced, even if its port is reused. Core AnyRuntime does not own
HTTP login/account identity. After §3 validation, the supervisor adds
`authEpoch, accountId, selfPeerId` to the Lease. Auth changes can happen
inside one Go generation: increment authEpoch and revoke leases on logout,
identity change or uncertain authorization; revalidate before a new lease.

The bao supervisor serializes lease registration/retirement under its own
short state lock. Start reads/validates the snapshot and records a session
and send-intent under this lock. It never calls `ensureStarted` or Binder
while holding the lock. Runtime stop holds its private transition mutex,
publishes `gateOpen=false`, then takes the bao lock to invalidate every
lease/send-intent, then releases that lock before waiting for retirement.
This gives two cases: a Start registered before closure belongs to the
participant's retirement set; a Start seeing closure is rejected. A
queued/outgoing message is never proof of admission. A native start racing
closure is retired before any Go teardown. The snapshot check alone is
not a cross-lock atomicity claim.

Resolve Go before lease registration. All worker calls and callbacks are
AIDL **oneway**, dispatched outside either lock by a bounded per-session
sender. They carry the lease/session and have async acknowledgment/status
callbacks. Main owns deadlines; never wait for a two-way Binder response.
Even an oneway send can stall in driver pressure, so no Binder transact
runs on main, the policy actor, the transition executor or a deadline
thread. A stuck sender causes worker retirement; do not accumulate
replacement sender threads in the same main process. Callbacks only post
state events and never acquire the runtime mutex. §7 defines the protocol.

Before every intentional kill, mark the session terminal and invalidate
all queued Start/retry messages, **unbind**, retain the old Binder's
DeathRecipient, then terminate that process. `onServiceDisconnected`,
`onBindingDied` and null-binding callbacks also unbind, record failure and
return scheduling to the supervisor. No callback automatically sends Start.
A late `onServiceConnected` for a retired session is unbound immediately.
A process recreated by Android stays inert until an authorized Start.

Each worker process accepts exactly one native session in its lifetime;
a duplicate identical request may only acknowledge that session, never
start again. A new session or second start after stop is refused and the
process retired. A new main inventories its own UID for the exact `:bao`
process name before starting Go, terminates surviving workers and confirms
their exit. Use a live process identity/handle (UID, process name, start
identity and Binder death where connected), never a saved PID alone. If
identity or death cannot be established, stay in recovery with Go closed.
Process enumeration by itself is not death confirmation. Worker-side
owner-death cleanup is a secondary safeguard, not the only orphan check.

Today's `AgentHandle::stop()` joins the ticker (including an inline long
trigger) but leaves other work detached. `Mobile.stop()` always returns
nil; Go may release straggler resources later and its graceful wait is
unbounded. Neither return value certifies safe account deletion.

For Rust, managed startup has a cancellation token before boot and a
run-admission gate. Stop closes admission, interrupts every live store,
wakes managed sleeps/queues and requests stream/helper shutdown. Join all
owned threads, collecting failures rather than returning on the first
panic. With current ureq 2/blocking resolver calls, cancellation cannot
reliably interrupt a socket read or DNS call. Therefore **10 s is a grace
period, not a guaranteed cooperative drain**. After it, unbind and kill
`:bao`; allow 5 s to confirm death. Forced worker termination is an
expected PoC stop outcome for blocked I/O, with unfinished effects/traces
marked indeterminate. Do not claim a timeout retroactively shortened an
already-running request. Bookkeeping a report performs after the owner is
cancelled is refused by the transport and logged, never retried (N2b-2).
Execution is quiescent only when every admitted job's report callback has
returned, nothing is queued, the stand-down write, every activation's
`shutdown` beat and housekeeping's stamp writes are settled, and no responder
beat is being published; the session adds that no takeover is in flight. An
activation owes its one `shutdown` beat from its Open to its revoking close
— a non-revoking close keeps it owed — and only that beat's own publish
settles it; a publish the host transport itself refuses is no attempt and
leaves it owed. The beat thread takes it only after the session released
it, which the session does once its close restored cleanup authorization
(N2b-2 round A-r2 review B2). The service's stand-down and beat threads settle what is still
due when the owner is cancelled, also when they first start after the
cancel (N2b-2 round A review A5/A6). The session alone moves the host
transport between its phases: back to Preparation only at that quiescence,
and to Execution again — for the stand-down write and the owed `shutdown`
beat alone, admission staying closed — when a revoking close makes them due
after the return. A
future cancellable transport is optional optimization, not a hidden
prerequisite for the 15 s worker boundary.

Keep existing operation-class budgets while running: local one-shot Any
calls connect ≤10 s and total ≤300 s (including bundle/convergence waits);
subscription SSE has no total cap and is supervised for progress; guest
HTTP retains its validated plain/stream idle and total policy (including
LLM 590/900 s totals), bounded by its run wall deadline. No global 30 s
cap. Cancellation prevents new calls and caps newly started cleanup to
its remaining budget; worker death is the fallback for existing calls.

**Destructive transitions use a fresh-process boundary in `baoPoc`.**
This selects a fallback instead of making unimplemented upstream Go drain
reporting a prerequisite. It applies to logout/wipe, account/network
replacement, index rebuild, and any recovery needing Go replacement;
ordinary release/debug/benchmark behavior remains unchanged.

1. Persist disabled host intent and a durable pending operation outside
   the directory it may delete. Record the old account, operation kind,
   whitelisted relative target, generation/nonce and phase; never keys.
   The transition gate closes across all `ensureStarted`, `prewarm`,
   retries, receivers and UI callers. Commit failure stops the transition.
2. Drain/terminate and confirm death of every old worker. Attempt orderly
   claim release (§5). Main's Go stays available during this phase.
3. Request Go shutdown with at most 5 s grace on a noncritical executor;
   ignore neither errors nor a hang, but also do not interpret nil as
   drained. Persist `AwaitingColdStart`, cancel alarms/retries, mark main's
   exit expected, drop foreground/stop the service with non-sticky policy,
   and terminate main **regardless** of the Go result. This independent
   deadline cannot wait on the Go call. Do not wipe in the old process.
4. The next user launch explains/resumes recovery. Only a fresh main
   process, after retiring any orphan worker and before loading/starting
   Go, executes the journaled wipe/rebuild/change idempotently. A plain
   Activity reinitialization in the old process is not recovery. Clear the
   journal/open the gate only after success; on failure show recovery,
   keep Go/bao closed, and allow an explicit retry. The account operation
   is not reported complete until this cold-start phase completes.

The app-owned transition outlives a cancelled UI waiter. Its short durable
state updates are non-cancellable; worker/Go waits retain their deadlines.
A process dying at any phase leaves a recoverable journal, not permission
to boot. The same check gates all headless paths, before Hilt/native work;
a background launch leaves a pending destructive operation for explicit
foreground recovery. Stop bao alone does not need main recycling: retire
Rust, release capability, leave Go for the UI, and stop the host service.
An upstream bounded `stopAndReportDrain` could later remove the cold-start
requirement, but needs a separate tested contract in `any`.
Before deliberate main recycling, show “Finishing account change; the app
will close. Reopen it to finish.” If Android recreates a visible Activity,
show recovery progress there without requiring a second tap; otherwise
the next user launch shows it. Do not report logout/rebuild success until
cleanup finishes. T3 checks both paths, no crash dialog, and a retry screen
on recovery failure; actual Nothing relaunch behavior must be recorded.

Only main uses `START_STICKY`. Before resolving the Hilt graph, run a tiny
journal/exit/breaker preflight; do not clear markers just because a new
Application exists. A tripped breaker uses only a minimal service graph
that promotes, stops itself and returns `START_NOT_STICKY`. Stamp each
process with `setProcessStateSummary` containing compact session/generation
identity. Persist a native-start marker before startup and correlate exits
by summary + timestamp + process, not PID alone. Three unexpected crashes,
ANRs, early-start deaths or failed bind/health attempts within 10 minutes
trip a persisted Needs attention breaker. A bind that never connects must
still expire and count. Manual re-enable resets it. Native-independent
health supervision and Binder death detect a failed worker; intentional
kills are identified by prior expected-exit markers.

| Exit evidence | Action before any automatic start |
|---|---|
| Persisted off, transition pending, or breaker tripped | Wins over every exit reason; no auto-start. |
| `REASON_USER_REQUESTED` | Disable auto-start, including ambiguous Recents kills; require explicit re-enable. |
| `REASON_USER_STOPPED` | Android user/profile stopped. Preserve intent but wait for that profile to run/unlock and revalidate; not an app Stop. |
| `REASON_PACKAGE_UPDATED`, expected component change | Revalidate artifacts/session, resume only if enabled; do not count as a crash. Use `DONT_KILL_APP` for our own component toggles. |
| Unexpected `REASON_PACKAGE_STATE_CHANGE` or permission change | Recheck component/permission state; Needs attention if disabled/restricted. No inference of user app Stop. |
| `REASON_LOW_MEMORY`, supported `REASON_MEMORY_LIMITER`, or `REASON_OTHER` with `MemoryLimiter:AnonSwap` | Resource death; release admission, back off and count toward T8's resource limit. |
| Unexplained SIGKILL | Unknown/resource-suspected death, not a proven user stop or proven low-memory event; preserve evidence and count toward the unexpected-death breaker. |
| `REASON_FREEZER`, crash/native crash, ANR, initialization failure, unexpected dependency death | Recoverable failure with backoff/breaker accounting; investigate any freezer death of a bound foreground worker. |
| Expected self/signal exit for the recorded session | Follow its Stop/recycle/fault-test intent; no fresh crash count. |
| Other/unknown reason | Needs attention after one controlled retry; do not silently relabel it. |

Consume records on every receiver/sticky/foreground entry and keep a
watermark. Guard constants introduced after the installed API/extension
level; the memory-limiter description fallback remains valid. Retry normal
transient failures with jittered 1/2/4/8/16/30/60 s backoff. OS sticky-restart
timing is outside our scheduler: T5 still fails if the OS does not deliver
recovery in budget or schedules no restart. Never explain a failed test
away as “working OS delivery” being absent.

Notification Stop and the in-app switch persist off before any teardown.
A swipe that leaves the service alive is supported; if Nothing kills it
with `REASON_USER_REQUESTED`, manual re-enable is required. Force-stop
may suppress boot delivery. The optional inexact wake-lock backstop in
§3 rechecks all stop gates and does not defeat those controls.

Boot/update resume only after credential-encrypted storage is available.
Keep the secure lock screen: unattended reboot means no bao until first
unlock, then restart without opening the app. Record Advanced Protection
and OEM inactivity-reboot settings; a reboot during the soak invalidates
continuous availability. Do not silently disable security protections or
remove the PIN. Direct Boot and a lock-free appliance remain separate
user decisions.

### 5. Preparation, election and account safety

Use Android `ElectionPolicy::HostControlled`, overriding ADR-015's
claiming path only for this embedder. Main alone writes `/v1/devices`;
Rust makes **no** registry mutations, even through a management helper.
It does not start `election::boot` or the claiming `election_thread`.
A read-only reconcile result cannot create intent or activate dispatch.
DesktopDefault retains its behavior.

Each new enable reaches **PreparedUnregistered**. Distinguish three write
sets; today's `serve::start` must be split, not merely run behind a gate:

| Phase | Permitted writes |
|---|---|
| Preparation | Main: post-bootstrap name seed, non-Active prune/release probe, owed release, joins of declared frozen overlays, host-status event. Worker: create device-local trace collections, and sweep trace retention on desktop's cadence — the device-local trace routes are allowed in every phase (amended 2026-11-06 by the user's ruling, N2b-2 design §10 R7). No synced config/secret descriptor bootstrap, chat/space/store provisioning, trigger install or ordinary responder beat. |
| Explicit setup | Separately invoked fixture/setup flow creates working spaces, chats, stores/config/credentials and any missing initial trigger records. Desktop can provision synced data but cannot create the phone's local trace collections. Missing synced prerequisites in preparation mean Needs setup. Setup also records the general chat's `bao/log/v1` child with its `agent_turns` collection as the `turns` fixture address (N2b-2). |
| Confirmed activation/Open | Worker performs checked takeover: seed/stamp the chat-watch record and standing triggers, re-arm their cron schedule strictly forward, and reconcile declared secret descriptors/missing rows (never key values). Await/verify these writes before opening guest dispatch. Main still owns all registry writes. Execution dispatch and responder beats run only in this phase. Every completed takeover starts a new activation of the execution service; its stand-down write (the responder's owner cleared on a revoking close) is released only when no takeover is in flight, and a takeover begins only when no stand-down write is pending (N2b-2). |

All dispatch requires the main grant AND local resource/health gates AND
`verdict.active`; chat additionally requires `owns_chat_watch`. Drive that
verdict from host admission. Android's takeover/stand-down hooks must be
called without the desktop election thread; do not retain its ignored-write
errors. Ownership loss closes admission on **all** lanes, including pinned
jobs, and the Release main sends with it drains them: the revoking Close
itself lets live runs finish, as desktop's stand-down does, and the Release
interrupts what is still live (amended 2026-11-06 by the user's ruling,
N2b-2 design §10 R9). A takeover write that fails cannot be treated as
AppliedOpen. The worker's registry reconciliation, due scan and event-source
convergence run only while AppliedOpen is in force, and a once shot or cron
occurrence is consumed, and an event marked seen, only after its lane permit
is held (N2b-2).

Stage-1 amendment (2026-11-05, N2b-1): **verify = read-back equal.** Each
takeover write is one record, read back by id (triggers) or key (secrets)
after the write; it is verified only when every field the write carried
reads back equal (a null written field may read back absent; integral
floats compare as integers; server-added fields are not compared). Any
transport error, per-record rejection, tombstone, read failure or mismatch
fails the takeover: no AppliedOpen, and the fence has consumed the policy
and evidence sequences, so main needs fresh evidence to try again. The
standing built-ins are written with this peer as owner, the record's own
`enabled`/rollup state kept, and for crons an explicit `nextDue` strictly
after the write (never a lazy arm), persisted on the record on the managed
path so a reader of the store can prove the advance; desktop readers ignore
the field. A persisted `nextDue` is kept only while it is in the future and
no further out than the occurrence armed from now; anything else (past, or
far-future from a stepped-back clock or an edited record) is armed again
from now. The chat-responder record is re-stamped when present and seeded
otherwise, then verified. Declared credential descriptors are reconciled as
`label`/`help`/`note`, plus `status: missing` for a row that does not exist
yet; unlike the desktop boot stamp (`stamp_declared`, which also writes the
declared `hosts`), the managed host never writes `hosts` — set or unset, on
any row — and never a `value`: declared refs take their destination from
the frozen declared table at injection (§2), and the host transport refuses
every `hosts` write. What the managed host does write on a secrets row,
through the same gate, is the descriptor minus `hosts` and the status stamps:
the broker's on a miss, a provider rejection and a use (`status`,
`requestedBy`, `rejectedAt`, `rejectedWith`, `lastUsedAt`), the chat-card
request's (`requestedIn`, `requestedAt`) and the set-status stamp
(`updatedAt`), so a revoked key reads `rejected` on the device
and the next preparation refuses the tier. The pinned skills are re-verified before the first
write of every Open (not periodically while open: §2, R6); drift is
`NeedsSetup` (`Invalid`) and no Open completes. A tombstoned standing
built-in (deleted from the dataset, e.g. from any-ui) is setup-level the same
way: the takeover fails typed, the worker reports `NeedsSetup` (`Invalid`)
and is no longer ready, so main stops reopening it until explicit setup
restores the record (2026-11-06, Step B corrections; logged as a default).
Supersession is checked before every takeover write, the chat-responder
seed's generation writes included. A takeover accepted while a superseded
one is still in flight starts only when that one has reported: its single
in-flight whole-record write could otherwise land after the new takeover's
read-back; a task that never reports leaves that Open pending until main's
30 s claim deadline closes it. A missing or unsynced
prerequisite at preparation is `NeedsSetup` with its typed reason, final
for the worker session (§2): the worker runs no retry loop and no second
sync poller; main re-prepares by stopping that worker and starting a fresh
worker/session.

Stage-1 amendment (2026-11-06, N2b-2): the execution phase also admits
`POST /v1/spaces/{w}/query/subscribe` with `dataset: chat_messages` on any
identifier object of the working space — the event sources an enabled own
event trigger names (ADR-018 §2); a source in another space is not watched,
which the worker logs once, and its record carries no marker (user ruling
2026-11-06, N2b-2 design §10 R8). Event runs keep ADR-018 §2's bookkeeping
and breaker. The execution phase also admits one
write on the setup-recorded log child: a single upserted `agent_turns`
record whose value is exactly the ADR-005 §3 interrupted turn
(`seq`, `fromAgent`, `userText`, `replies: []`, `interrupted: true`,
`traceRef`, `searchText`, `llm: {stopReason: break_hard}`) under an
eight-digit id, written for a run the interrupt trapped, as on desktop.
A chat run's follow-ups are desktop's, in desktop's order and with no
post-time fence (user ruling 2026-11-06, N2b-2 design §10 R2): the
credential cards, then "Still waiting", "Stopped." with the interrupted
turn, or the typed error; a run that produced no result posts nothing, and
a run that a revoking Close let finish posts its follow-ups as on desktop.
The chat watch does not fold sender names: the members and identity routes
are not host routes, and the PoC account has one member, for which the fold
is a no-op (BOB-192).

Main publishes `bao.host.status.v1` every 10 s with peer/session/phase,
ready, registered, supportedCapabilities, failureCode and diagnostics
sequence/freshness. Its 30 s UI freshness is not an active responder beat.
Use the matching local worker Ready snapshot plus this beat to offer
activation for a prepared self with no `apps.bao`. Main owns subscriptions
and the 5 s policy monitor even with no open screen. Only Open execution
publishes ordinary `bao.status`.

All baoPoc self-activation, including Claim again, goes through main's
host API. Remote display and other variants retain existing behavior.
One fresh explicit user intent has these durable/in-memory substates:

| State | Transition/deadline |
|---|---|
| IntentReady | Ready, fresh auth/sync, matching lease and worker ActivationReady. Persist intent before one activation POST; record ClaimAttempted before sending, since a lost response may still commit. No blind POST retry. |
| ClaimWritten | Read back matching self claim `(seq, at)` and winner. Complete checked worker takeover and AppliedOpen within 30 s of POST dispatch. If definitely never opened by the deadline, persist ReleaseOwed and close/drain/release. A failed/ambiguous POST also owes release. |
| Open | Worker atomically publishes AppliedOpen after takeover and before dispatch, with policy/state sequence. Callbacks have a 5 s acknowledgment budget. An unknown/lost acknowledgment may mean effects already ran: query status under the same lease, close on uncertainty and classify as indeterminate; never replay. |
| BeatSeen | Fresh matching active beat within 30 s of AppliedOpen confirms the UI operation. A missing beat **after Open** is a health fault handled by normal close/recovery, not activation rollback or a repeat claim. UI says not confirmed/Needs attention until evidence resolves it. |

A pre-Open release must reconcile before another explicit activation.
After Open, health recovery revalidates the saved claim; it never sends
another claim without new user intent. A user Stop or observed new winner
always revokes the intent and releases, regardless of the activation phase.

Persist `ReleaseOwed{accountId, peerId, intentId, reason, claim?}` **before**
Off, relinquishing, rollback or a destructive transition begins draining.
Revoke resume intent in the same durable update. Close/drain Rust (§4),
then main clears `apps.bao`; an interrupted drain leaves the record for
recovery. While Go runs for that account, main retries independently of
host enablement at 1/2/4/8/16/30/60 s, capped at 60 s between attempts.
This retry starts no FGS/worker/Go by itself when the ordinary app is off.
After process death it resumes on the next authorized Go start. There is
no automatic recovery promise for a force-stopped/uninstalled app.

Exit ReleaseOwed on successful PUT plus verified local absence (retain a
separate propagation-pending receipt until fresh sync), or on confirmed
409 device.pruned. Try up to 5 s for propagation evidence, then show pending;
healthy two-device convergence still must meet T4's 30 s budget. On account
replacement, retain an abandoned old-account receipt, show Needs attention/
manual takeover and never write that release into the replacement account.
Permanent errors stay visible and closed. Re-enable remains unregistered
standby; no repeated registration while release is unresolved.

Invariant: a non-Active own row carrying `apps.bao` owes release, except a
bounded ClaimWritten/Opening or validated-resume transition. On restart,
a previously enabled Active intent may enter ResumeCheck for at most 120 s
without dispatch or writes. Persisted `(seq, at)` must still match self,
`active.bao` must be self, and **no visible row, including rows whose bao
app was removed, may carry a higher bao claim**. Require the two fresh
activation-grade samples below. Otherwise revoke/ReleaseOwed. If the pinned
API hides pruned claim history or history completeness is unknown, automatic
resume after downtime cannot prove this condition: require explicit
activation instead and report that recovery limitation in T5/T6. Never
present local election as global fencing.

On observing a foreign winner, atomically persist revocation/ReleaseOwed,
close, stand down/drain, and release. Successful release returns Prepared;
failed release remains pending with no dispatch even if self later becomes
the fallback winner. T4 covers observed takeover → desktop app removal,
and takeover during phone downtime → removal before phone returns, plus
pruned/incomplete-history cases. All require zero silent revival/claim.

Main owns readiness and the 5 s sync/registry monitor using Kotlin
AnyClient; Rust consumes its sequenced admission decisions. Do not create
a second Rust sync poller. Registry readiness has two levels. Local
bootstrap is §3's auth/health/auth
check within one runtime generation. Before activation and after a sync
outage, additionally obtain `techSpaceId` from `/v1/account` and
`spaceIndexObjectId` from that space's GET. Require a fresh object
`/sync-status/objects/:indexId` result of `synced` **and** a fresh space
`/sync-status` with `networkPeers > 0`, bracketed by unchanged lease/auth.
“Fresh” means both responses were fetched and completed within the current
5 s sample window under the unchanged lease, not that lastSyncAt or
lastSyncedAt recently changed. An idle space's old sync timestamp is valid.
The second sample must be at most 5 s old when used for activation/resume.
Require two successful samples 5 s apart before reopening dispatch, then
re-read the registry; an activated phone may dispatch only if `active.bao`
is self. Missing fields, unknown/syncing/error, or unverified identity
keep it waiting. No fixed sleeps or zero-peer synced state substitutes.
This deliberately requires a sync-node path for the PoC, even when LAN
peers could converge. It is evidence of current local sync, not a proof
that all devices agree; activation still needs presence confirmation.

Poll sync/registry evidence every 5 s. Define **sync-path loss** as zero
network peers, offline/error state, or failed sync-status reads sustained
for 30 s. Android connectivity hints trigger an immediate probe; loss of
LLM reachability alone is a provider failure, not an ownership change.
Close new chat/job admission on sync-path loss and show Waiting for sync.
Let already admitted turns finish within their existing budgets; cancel
them on the Release or Stop main sends for an explicit stop,
prune/deauthorization or an observed changed winner — the revoking Close
itself lets them finish, as desktop's stand-down does (2026-11-06, R9).
The prior blanket “cancel on every network loss” rule is withdrawn.

After two renewed sync samples and election reconciliation, reconnect the
chat watcher and use its snapshot/backlog rules. Re-read replies/turn
completion before admission so prompts answered by another device are
excluded. Within one worker an interrupted run is never replayed: it is
finalized as interrupted, as on desktop, and the host does not run it
again (§2: an admitted run is never retried within a worker). Nothing
durable marks a run started, so after
a worker death a message may be answered again — and a once shot fire
again — by the next owner, as on desktop (amended 2026-11-06 by the
user's ruling, N2b-2 design §10 R1; §6 T7/T10 count such duplicates).
The earlier “mark them uncertain and require explicit retry” rule is
withdrawn. Never insert unadmitted
messages into a completed/seen set. All registration/reconcile failures,
including disabled-active verdicts with `enabled=true`, block dispatch.
Detect self prune during operation and stop **all** work, including pinned
jobs. Queued work remains subject to capacity and existing missed-job rules.

Guest code cannot control this policy. Add `AndroidPocEgress` at the broker
boundary for **all** guest network effects: deny loopback, private,
link-local, unspecified and other non-public destinations, except the
exact lease's literal loopback origin. For that origin use an explicit
allowlist of required Any data routes built separately from the pinned
OpenAPI and tested server routes; deny device/claim mutations, auth/shutdown, local import/export, debug and
host-control routes, irrespective of credentials. Queries of devices may
remain read-only. Canonicalize methods/paths and reject ambiguous encoded
paths; validate every redirect and every resolved address and pin the
validated address for the connection, including IPv4-mapped IPv6. Cover
all broker HTTP/provider/MCP paths; a new transport cannot bypass the
policy. Trusted main management calls use a separate host client,
not a guest-supplied capability. `routes.rs` classifies effects for replay;
neither its naive split_url nor its heuristic fallback is an access-control
boundary. Use a real URL parser and explicit method/path patterns.
T11 probes direct/redirect/DNS aliases, forged activation/prune and a
second test listener. This is required even
for the throwaway account, since the agent itself is a local HTTP client.

Inventory other Any installations/processes during device setup. Do not
run a personal-account Any server alongside the PoC until the egress
checks pass, and never probe real data as a test. Setup discloses both
risks: another app can access the PoC's unauthenticated server, and an
unfenced guest could attack another local server. Broker egress protects
against the latter; all-route upstream auth is still required for real
account use and protection against unrelated apps.

Crash/OEM kill/uninstall/force-stop can leave the phone's claim visible.
There is no liveness lease/failover: manually claim on another device.
Release intent is tied to the old account/peer and must never write into
its replacement. Prune or release does not cancel already-issued external
effects. Partitions can still produce duplicate replies/effects; this PoC
permits synthetic effects only and promises neither exactly-once nor
guaranteed at-least-once delivery. T10's zero observed duplicates is a
regression gate, not a fencing proof; a duplicate attributable to a worker
death mid-turn is counted and reported instead (§6, 2026-11-06).

Exclude account/device keys, databases, blobs, opt-in/journal state and
logs from cloud backup and device transfer for `baoPoc` (root/file/database/
sharedpref/external and applicable device-protected domains). Recover a
new installation through login with fresh device identity. Private logs
rotate at 20 MiB and omit credentials, message bodies and consent URLs.
Sentry/Amplitude are disabled by variant; worker loads neither their
native libraries nor Go. Test exports require explicit setup. Lost-phone
procedure: prune remotely, take over elsewhere, rotate test keys/revoke
grants and clear the installation when possible; prune is not erasure or
provider revocation. Existing production backup/telemetry policy is
separate follow-up work, not silently changed by this PoC.

### 6. Acceptance and failure criteria

These are Stage-1 proposed engineering budgets, not measured results;
Stage 0 uses §0. Run on the non-debuggable `baoPoc` variant inherited from `benchmark`. Record phone
RAM, OS/build/security patch, page size, power/thermal settings, APK/AAR
hashes and all versions from §2. Findings apply only to that combination.
Keep security settings unchanged and report them. Build-only results do
not satisfy device gates; changing budgets requires an ADR amendment.

| ID | Test and collection | Pass / failure condition |
|---|---|---|
| T1 | Inspect APK/manifest/dependency trees, ELF alignment and worker maps; execute precompiled kernel on phone and 16 KB ARM64 emulator | No target `cranelift-codegen`, raw kernel, compile cache or control listener. No `libgojni`/`libsentry` in worker maps. Release lacks PoC code, permissions and fault receiver. Incompatible manifests fail before runs. HTTPS/IPv6 fixture succeeds. The 16 KB emulator reports 16384-byte pages with compat mode off; ELF and APK alignment checks pass. Absence checks have positive controls. |
| T2 | Cold/warm headless start, sticky restart and Activity attach; monotonic startup events, services and process identities | FGS promotion ≤2 s from process start for a cold process, or from `onStartCommand` in an existing process. On synced fixture, Prepared ≤60 s; one Go generation and worker session, zero ANRs. No native work before promotion. With host off, normal app use starts no bao service/worker. |
| T3 | Stop during boot, blocked HTTP/DNS, idle SSE, sleep, chat/cron/event; panic/Binder fault; logout/rebuild and cancelled UI waiter | Worker drained or confirmed dead ≤15 s; no binding resurrection or stale-generation request. Ten cycles pass. Destructive transitions journal/recycle main; wiping happens only after verified cold start, never from `Mobile.stop` success. Invalid journal/death evidence blocks boot/deletion. Visible recovery finishes in the fresh process without a crash dialog or false success. |
| T4 | Fresh phone with desktop active; activation; off → Prepared standby → activation; failed release and stale explicit claim | Zero implicit registration/claim in Prepared state; no runs on identity/prune/readiness failure. Healthy activation and release converge ≤30 s; otherwise visibly pending/failed. Observe desktop and phone agreement, not just local 204. Missing bootstrap/sync fields remain waiting. After desktop takeover/removal, including during phone downtime, no silent claim revival. Unknown/pruned history requires explicit activation. Rust makes zero registry writes. |
| T5 | Controlled worker/main death, early-init crashes, three failed binds/startups, update, Recents, Task Manager Stop and force-stop | Recoverable death returns Prepared/Active as appropriate ≤120 s. When main requires OS relaunch, no scheduled restart in `dumpsys activity services` fails availability; every recovery path must meet the deadline. Breaker records 3 faults/10 min; recovery preflight remains reachable before Hilt. Explicit user Stop never auto-resurrects during the 30 min observation and subsequent boot/update opportunity; only explicit re-enable ends the arm. Record any Recents limitation. |
| T6 | Unattended reboot, then one unlock without opening app | No agent before unlock; after unlock Prepared/Active ≤120 s on synced fixture. Record outage; this is secure lifecycle behavior, not unattended reboot availability. |
| T7 | 80 h on a wall charger, locked/no UI; 320 remote probes every 15 min and 160 scheduled heartbeats every 30 min | Each probe has one defined final reply: sender-clock p95 ≤120 s, max ≤180 s, zero duplicates through +1 h observation — except a duplicate reply or effect attributable to a worker death mid-turn, which is counted and reported, not a host failure (2026-11-06 user ruling: nothing durable marks a run started, desktop parity; the frozen F2 evaluator still scores it strictly, a logged follow-up). Each scheduled occurrence runs once and starts ≤21 min late (one existing 1200 s job plus margin). Zero host-attributable misses, crashes, restart, wake-lock lapses or inactivity reboots. Attribution/invalid-run rules below apply; no failures silently excluded. |
| T8 | Both processes self-sample memory, progress/CPU and thermal state; synthetic PDF/mail payload peaks and saturation test | Combined PSS p95 ≤1.5 GiB, peak <2 GiB; idle CPU p95 <5% of one core; no idle epoch tick, supervisor wakeups ≤10/s excluding measured sampling work. No silent event drop/once consumption/auto-disable under CapacityDeferred. Stop at SEVERE thermal state, battery ≥45°C, or 2 resource deaths/24 h. Budgets are experiment limits, not hardware safety guarantees. |
| T9 | Export metrics/trace evidence hourly, log plug/idle/power-save state and local bytes every 5 min | Fresh memory evidence, logs ≤20 MiB, traces ≤256 MiB plus one sweep, total test data <1 GiB. Complete per-probe correlation and account/generation/session history. Any unexplained evidence gap fails; unplugging invalidates the charged-soak claim. |
| T10 | Separate tests: 10 min sync-path loss mid-turn, desktop takeover, reconnect; provider-only outage; 30 min unplugged forced Doze | Distinguish sync loss from provider failure. After renewed sync, ready/ownership settles ≤120 s, no stale new admissions and zero duplicate effects/replies observed, a duplicate attributable to a worker death mid-turn counted and reported instead (as T7). In each of three 30 min Doze arms, all 3 probes complete within 180 s with exemption (9 probes total); restore by minute 32. Without required settings no availability claim. |
| T11 | Guest-origin/second-UID loopback probes, forged registry writes, telemetry/backup checks, crash/uninstall manual desktop takeover, then prune | Guest cannot reach second local listener or mutate registry via any effect/redirect/address alias. Document independent-app exposure to the test server only. Zero observed app-UID telemetry under the audited capture/call paths; no automatic private export/backup. Expected Go push-node traffic is listed separately. Manual desktop takeover after abrupt loss works ≤30 s with sync; observed prune stops every job. Prune is last. Real-account auth remains a separate unmet prerequisite. |

The reproducible desktop/overlay/sender fixture is the proposed
`android-poc` environment in [environments.md](../environments.md#android-poc-proposed).
All accounts and ports are separate from real-account desktop servers.
The desktop Any binary matches the phone AAR's exact source tag/CRDT
version; the desktop anyrt matches the phone runtime revision. Overlay content
hashes are pinned independently and their compatibility is checked.

Deploy `android-poc-heartbeat@v1` only in the throwaway working space.
It records scheduled occurrence, actual start and run ID with no external
mutation. Run every 30 min with priority among due scheduled jobs; keep
normal extraction/rollup/linkgen enabled. Define 160 occurrence IDs before
T7. The 21 min lateness ceiling is deliberate: a heartbeat may wait for
one 1200 s scheduled run, but must precede other queued normal jobs. This
test proves scheduling continuity, not sub-minute scheduling accuracy.

For T7, define 320 unique nonce messages before starting. A qualifying
reply has bao author identity, the exact nonce and fixture marker
`ANDROID_POC_DONE:<nonce>`, and belongs to a completed successful
`agent_turns` record. Typing/status/error bubbles and mailbox injections
are not final replies. Send non-overlapping probes; record all replies,
including late ones, using the sender's monotonic clock for elapsed time.
Watch until hour 81 to catch delayed duplicates. The marker is a test
instruction in synthetic prompts, not a change to production chat format.

Keep independent sender connectivity, sync-node status and provider HTTP
outcome/latency evidence; run a small provider canary from the sender.
Before T7 estimate tokens from a calibration batch, provision at least
2× projected probe/standing-job spend, and record remaining quota without
logging keys. Classify every miss as host, provider/quota, sync network,
sender, or unresolved. External failures do not count as host bugs, but
invalidate a clean end-to-end pass: report all windows and rerun a full
80 h after correction. Unknown attribution never becomes a pass. Do not
invalidate or discard evidence in order to hide host failures.

Memory sampler: every 30 s each process reads its own
`/proc/self/smaps_rollup` for RSS/PSS and reports sequence/time with the
worker's oneway status; use `/proc/self/status` RSS at the 5 s admission
cadence. Do not sample `getProcessMemoryInfo` as if it were unthrottled.
Probe access on the actual non-debuggable build; absent/frozen/stale data
fails T8 rather than repeating an old value. Sampler/export CPU and memory
are included in the app budgets. Capture `getMemoryInfo` and exit evidence
alongside; on relevant Android 17 releases capture memory-limiter status.
PSS samples are nominally 30 s apart; a gap over 60 s is incomplete
evidence. RSS admission freshness remains the stricter 15 s rule in §2.

Use a wall charger for smoke/T7, not a sleeping laptop USB port. Log
plugged source, temperature, idle/power-save and settings. Use continuous
self samplers and the desktop status/export monitor; host profiling and
wired ADB are confined to pre-soak windows. Wireless ADB is not collection
or recovery infrastructure. No unplugged interval belongs to a charged run.

T10 has three sync-loss arms, three provider-loss arms and **three separate
30 min forced-Doze arms**, each with probes at +5/+15/+25 min (9 total).
For each Doze arm, enter forced idle over ADB, unplug, and collect locally.
The present operator must replug at minute 30 and finish `deviceidle unforce`
and `dumpsys battery reset` by minute 32, with a receipt and state readback.
Late/missing restoration invalidates the arm. Use a host timer/alarm;
no detached on-phone shell timer is presumed to survive suspend. Router
fault rules have their own expiry independently of this manual restoration.

A sync partition must deny nodeconf peers, iroh and pkarr relays, direct
UDP/TCP IPv4/IPv6 (including link-local) and mDNS/local discovery. Prefer
phone egress default-deny with only verified provider/DNS/control essentials
allowed, because enumerating relay IPs cannot cover hole-punched peers.
Keep desktop sync/provider reachable. Prove phone networkPeers/localPeers/
globalPeers are zero, no phone global peer is listed on desktop, and a
synthetic canary object cannot converge until restoration while a provider
canary succeeds. Recheck pinned v0.3.5 behavior; missing peer fields require
an explicit measurement revision, not treating them as zero. Node-only loss
with P2P alive is a different optional arm, not the partition acceptance arm.

T11 uses a separate telemetry window: a consented test-APK VpnService with
baoPoc-only app routing, IPv4/IPv6 forwarding and per-flow
`getConnectionOwnerUid` attribution. Test TCP/UDP, DNS and labelled control
traffic; correlate the app UID with the installed APK. Go and Rust share
that UID, so do not claim per-process attribution from it. Keep router
captures as corroboration, and inspect per-UID netstats deltas for gross
coverage gaps; aggregate buckets or host-off/on comparison alone cannot
attribute a destination. Unresolved app flows remain INCOMPLETE, not OS
traffic by assumption. Inventory all expected Any/provider/Go-push flows
and forbidden telemetry paths. Disable/remove VPN routing and verify
normal networking before T10/smoke/soak; do not test outages through an
unqualified VPN fixture. Capture cannot inspect encrypted payload contents;
combine it with component/call-path absence tests, no production DSN.

Fault seams: a `baoPoc`-only explicit receiver protected by
`android.permission.DUMP` accepts a fixed action enum and test-run nonce
while a locally enabled, expiring test session exists. It permits no
arbitrary code, path, URL or PID. Actions cover self worker/main process
death, injected native panic/early-init failure, blocked startup/I/O,
Binder non-response and memory pressure. Use labelled expected-test-exit markers with an explicit count-as-fault
bit. T5 exercises the **production breaker implementation and durable
counters**, including the three faults/10 min boundary, never a parallel
test-only algorithm; test deaths are not user-stop records. Missing/failed bootstrap, identity and election replies also
have deterministic fake-server integration tests. Main-process death is
initiated inside the app, not by an unauthorized shell signal. T1 verifies
the receiver/actions are absent from release; T11 verifies another app
without DUMP cannot use it. Disable the session outside fault tests.

The receiver is exported only
in baoPoc and protected by DUMP, lives in main, and forwards worker actions
over the existing session. A local test screen issues a random nonce with
boot count, elapsedRealtime expiry (at most 30 minutes), permitted actions
and per-action use counts. Validate and consume a count durably before
injection; three early-init failures can be pre-armed and survive process
restart, never reboot. Boot-count mismatch expires the session. Include a
non-mutating status action, but query it only while main is already alive:
a broadcast can itself launch main and would invalidate a recovery test.

Transfer the armed nonce from the local screen to the operator's host
runner (display/QR); keep it in a mode-0600 temporary control file, outside
logs/evidence/git, and erase at expiry. Run batches shorter than 25 min;
operator re-arms from the screen between batches. Dry-run duration determines
and records the operator reservation; no speculative hour estimate is a
promise. Inventory every package holding DUMP before accepting the fixture.
The unprivileged probe APK must not hold it.

For T2 only, a fixed `START_HOST_TEST` action can launch a cold main through
the protected receiver, with locally pre-armed nonce/use count, valid host
opt-in/account and real OS background-start eligibility. It cannot enable
a disabled host or bypass FGS restrictions; rejected starts fail the arm.
The service stays non-exported. Establish main absence before delivery and
measure from Process.getStartRequestedElapsedRealtime, not receiver entry.
Keep these harness-triggered starts distinct from T5 OS recovery/T6 boot:
neither of those may use this action or another wakeup broadcast.

After injecting death, the harness observes dumpsys/logs from outside the
app and sends no wakeup/status broadcast. Never use the seam to awaken a
force-stopped package or send include-stopped-packages; only a recorded
manual launch/re-enable may end the user-stop arm. T6 starts with the seam
disarmed. Restore battery simulation, forced idle, network rules and fixture-seeded
breaker/session state before soak and export the restoration receipt.
Never erase unexplained production faults from the evidence.
The compiled seam stays in the qualified/handoff baoPoc bytes, disabled
by default; removal requires requalification. Its enabled state is in every
hourly diagnostic export and live host diagnostics; enabled means T7 is
invalid. Ordinary release contains no seam. No arbitrary code/URL/PID is
accepted by any action.

Before the long soak, run fault/recovery, all T10 arms and T11 abrupt-loss/
uninstall/takeover on expendable identities. Freeze/reinstall the candidate,
then pass a **12 h smoke + 1 h duplicate-observation** window (48 probes,
24 scheduled occurrences). Smoke validates the evidence pipeline and early
regressions, not inactivity reboot, long retention or quota exhaustion;
T8 exercises the retention sweep explicitly. Only then run 80 h + 1 h;
prune last, never test again with permanently pruned keys.

Campaign bounds have two ledgers. H admits at most **two host candidates**:
a host-attributable or unresolved failure retires its candidate, consumes
one slot and requires a reviewed fix before the second; rebuilding never
resets H. X allows at most **three proven external/fixture invalidations**
across smoke/long runs; two consecutive X events pause for fixture repair
and user review before retry. A provider transport error plus independently
consistent incident/quota evidence can establish X even without an HTTP
response; a canary alone cannot. An event containing a host failure is H,
never hidden as X. Exhausting either budget requires a new user decision.
One ≤2 h pipeline rehearsal before qualification is uncounted, retained,
and cannot serve as smoke or conceal known defects. Success still requires
one complete smoke and long run with identical validated bytes/config.

Abort on the first invalidating event; preserve partial evidence. During
smoke/soak, live host/diagnostic beats warn at 30 s stale and invalidate at 60 s (UI readiness
already expires at 30 s). A required hourly export missing at 65 min
invalidates; expired memory samples use §2/T8's separate budgets. A probe
past 180 s or a scheduled occurrence past 21 min late invalidates without
waiting for the next export. Continue collecting late replies through the
observation window when possible; aborted data never contributes to a pass.

Fingerprint/security/APEX or app/runtime changes, reboot, changed fixture
settings or unknown version drift invalidate the affected run. A GMS/Play
app update without reboot is recorded, not an automatic failure solely
because its version changed; certify unaffected call paths with user review.
Use the plan's change-impact matrix, with the user approving every claim
that a gate is unaffected; uncertainty means affected. Never change security
settings silently. Resolve inactivity reboot before the Stage-1 long locked
qualification campaign; implementation acceptance does not waive this gate.

The proposed [evidence schema](../plans/android-host/evidence.schema.json)
and [predicate catalog](../plans/android-host/evidence-predicates.md) are
reviewable contract artifacts, not an implemented evaluator. Mechanical
predicates consume typed observations; UI/exposure/attribution decisions
consume signed reviewer rulings linked to evidence hashes. Missing inputs,
unknown predicate IDs and unexecuted arms cannot pass. Require golden PASS,
negative and boundary bundles, then validate a real dry-run bundle before
freeze. Commit sanitized per-gate verdict/arm manifest/rulings/restore
receipts, SHA256 bundle manifest and exact evaluator invocation. Keep two
raw copies outside git until handoff; no keys or real databases in reports.

On the non-debuggable build `run-as` is not a collector. Before T7 opt in
to diagnostic export to a local Documents tree selected via SAF. Main
writes bounded metrics and hourly synthetic-trace bundles there for later
`adb pull`, without unlock/UI interaction. Exclude keys, wallet/database
files and auth headers. Validate locked export and storage capacity first;
failed/gapped exports fail T9. Never enable anyrt's control listener for
measurement. Correlate monotonic clocks and boot count to UTC. Publish
only diagnostic
freshness/sequence, seam state and byte counts alongside host status, never
private export contents/paths. The desktop continuously collects these
beats and scheduled-heartbeat records, applies the explicit freshness
thresholds above and marks invalidity without waiting for the SAF pull.

Immediate stop/review: account/generation crossover, guest egress escape,
a duplicate synthetic effect not attributable to a worker death mid-turn
(those are counted, T7), unexplained native signal fault, breaker or
resource/thermal limit, or unexpected reboot. No unreviewed fallback to
shared process, on-device compilation or Termux. Preserve failure evidence;
restart a full soak after correction. T1–T11 incomplete means PoC
incomplete, and no battery-longevity claim follows from an 80 h test.

### 7. Embedder contract and cross-repository changes

These are proposed APIs, not symbols already available in the tree:

```text
ControlApi = Disabled | Loopback(port)  // Loopback unavailable on Android
ElectionPolicy = DesktopDefault | HostControlled
start_managed(Config, ManagedOptions, CancelToken) -> ManagedAgent
ManagedAgent.health() -> HealthSnapshot
ManagedAgent.cancel()                  // valid during preparation/startup
ManagedAgent.drain_until(Instant) -> Drained | DeadlineExceeded | Failed
```

`start_managed` returns a controllable preparation task before HTTP/kernel
initialization. ManagedOptions fixes the trusted artifact/profile, class
capacity policy, guest egress, unsupported-consent hook, time provider and
explicit election. Preparation starts native supervision and reports
readiness to main for its host-ready beats; it does not start registering
election boot or guest dispatch. Only Kotlin main may issue the explicit
registry claim in §5; Rust has no management claim path.

`health()` reads an immutable atomically published snapshot plus atomic
per-task progress counters; it takes none of the service `waiting`,
roster, scheduler or run mutexes. Essential loops publish phase/progress
and wait-class/deadline before blocking. Lack of progress while a known
bounded HTTP call waits is not automatically a failed thread. The
independent supervisor heartbeat, thread-exit flag or expired wait budget
provides the liveness verdict. Panic reporting uses independent atomic
failure state and RAII cleanup, not a poisoned service lock. A stale
supervisor snapshot for 30 s is a failure. `drain_until` retains ownership
on timeout; only Drained means all work joined. Process-death confirmation
is a separate outcome and carries indeterminate work information.

```text
RuntimeSnapshot = {mainNonce, generation, baseUrl, gateOpen}
Lease = {mainNonce, generation, baseUrl, authEpoch, accountId, selfPeerId}
ActivationIntent = {activationId, intentRevision}
oneway Worker.start(requestId, sessionId, Lease, options, ownerBinder)
oneway Worker.requestStatus(requestId, sessionId, Lease)
oneway Worker.activate(requestId, sessionId, Lease, activationIntent)
oneway Worker.release(requestId, sessionId, Lease, reason,
                      deadlineElapsedRealtime, revokeThroughIntentRevision)
oneway Worker.setAdmission(requestId, sessionId, Lease, policySeq, decision)
oneway Worker.stop(requestId, sessionId, Lease, deadlineElapsedRealtime)
oneway Host.onAccepted/onStatus/onActivationReady/onReleased/onAdmissionApplied/
            onDrained/onFailed(requestId, sessionId, Lease,
                              stateSeq, appliedPolicySeq, result)
```

Main owns the only 5 s auth/sync/registry monitor and durable intents.
Worker.activate validates readiness and returns ActivationReady, without
registry writes or dispatch. Activate, ActivationReady, Open and status
carry the same ActivationIntent pair. Main rechecks readiness, issues one
explicit claim, confirms the winner and sends Open with that intent, evidenceSeq,
observedAt and expiresAt in Android elapsedRealtime. Rust uses native CLOCK_BOOTTIME, calibrated against Android
elapsedRealtime at session start and checked at every admission; a
suspend-pausing clock is not valid for grant expiry. Initial Open requires activation-grade evidence and
checked takeover in §5. AppliedOpen, not merely onAccepted, changes admission.

Stage-1 amendment (2026-11-05, N2b-1): the calibration bracket (native
CLOCK_BOOTTIME before and after one Android `elapsedRealtime` read) is
sampled in the `:bao` process around the Start's arrival and is a worker-
local Start input, never a Binder field; an unordered or future bracket, a
lease whose origin, account or peer differs from explicit setup's record, or
`elapsedRealtimeAtStart` after the worker's own Android sample refuses the
Start, and a refused first Start is terminal for the process — stricter than
Kotlin's reference `WorkerProtocolFence`, which leaves a refused Start
retryable; the difference is recorded for the H/L-kotlin phase. Status phases
are PREPARING while preparation runs, PREPARED when the service is retained
and nothing is open, OPENING during takeover, OPEN, RELEASING until
execution is quiescent, QUIESCENT after Release, STOPPING/DRAINED after
Stop (STOPPING also when the owner was cancelled outside the session's
Stop), and FAILED when preparation is NeedsSetup/unsupported or the owner
failed; NeedsSetup is final for the session (`Status{FAILED, NOT_READY}`),
there is no re-prepare command, and main re-prepares by stopping the worker
and starting a fresh worker/session. `ready` is not the PREPARED phase: it
is preparation `Prepared` with an installed execution service, no owner
failure or owner cancel and no capacity fault, in any non-terminal phase
(renewals need it while OPEN). Fence refusals
map to INVALID_GRANT (intent/evidence), INVALID_SEQUENCE (policy sequence,
terminal), TAKEOVER_FAILED, NOT_READY and INTERNAL (clock). AppliedOpen
also requires `ready` when the takeover completes. A spontaneous
Status follows every applied Close, grant expiry, preparation settling,
installing the execution service, an owner failure and a capacity-fault
flip; a NeedsSetup flip or an owner failure while open closes locally
first, so a FAILED Status never stands beside an open gate (one reading of
the owner — preparedness and failure — both builds a Status and decides
that close, on every Status path). Activate and Open are refused NOT_READY
while a Release waits. A Release reports Released only when execution is
quiescent and no takeover task is in flight; each Release is answered once —
Released, or Failed(DEADLINE_EXCEEDED) at its own deadline, after which
main has removed it (a command ends at its first non-Accepted callback), so
no late Released follows; the Start-correlated Status reports QUIESCENT
when it is reached, and every Release still waiting then gets Released, in
arrival order. The one exception: a Stop supersedes the Releases still
waiting and they are not answered. That is safe for main because main sends
Stop only to retire the worker: retirement suppresses every further send, a
retirement request is idempotent, and confirmed death removes the session —
so a Release receipt that expires while retiring (its Accepted delayed or
lost) issues no new worker command and does not restart the retirement
(sb-fix reviews SBC-P-4, SBC2-P-3). Stop cancels the owner only after the stand-down writes and
every takeover task in flight, and
reports Drained(DRAINED) when every owned execution thread is joined, else
DEADLINE_EXCEEDED at its deadline with the handles retained, and the worker
still answers RequestStatus afterwards. Commands are consumed in arrival
order, and a queued command is taken before a takeover outcome posted after
it — the command queue is polled again once an outcome is in hand — so a
queued Close/Release/Stop preempts a completing Open; an ordinary
command reserves one of 64 queue slots before it is queued, 8 stay reserved
for Close/Release/Stop, and a refused command is dropped by the adapter,
counted in native health, and left to main's 5 s receipt budget
(2026-11-06, Step B corrections). `Accepted` for a Close carries the
previous applied policy sequence; the applied sequence advances only on
AppliedOpen and applied Close. The responder beat (N2b-2) is the desktop
`bao.status` envelope (`scope: account`, `target: <self peer>`) plus
`data.sessionId` and `data.activation = {activationId, intentRevision}`;
`role` is derived from the stamped chat-responder ownership
(`Execution::owns_chat()`: `active` only while this peer owns the enabled
chat-watch record, `standby` otherwise — a paused or repinned responder
never beats "answering", ADR-025 §1/BOB-111), `winner: <self peer>`,
published by the worker's own beat thread only while Open: 10 s cadence plus
change-triggered, one `shutdown` beat per activation on a revoking close
(a revoking Close — ownership loss, prune, deauthorization, user Stop — or
Release or Stop), none on a non-revoking close, after which the activation
still owes it to the revoking close or Release that follows (N2b-2).

Within one lease/session, main assigns each fresh explicit intent a strictly
increasing positive `intentRevision`; an old intent never receives a new
revision. Request IDs remain unordered correlation IDs. Release and each
revoking Close (ownership loss, deauthorization, prune or user Stop) carry
`revokeThroughIntentRevision`: the latest allocated intent revision they
invalidate, including queued/unseen Activate messages. Non-revoking Close
carries no barrier. The worker retains a scalar revocation high-water mark,
never an unbounded set of IDs, and rejects Activate/Open at or below it.
A stale barrier below the current intent or high-water mark is rejected;
it cannot close a newer intent. While an intent is unrevoked, only its
identical Activate may be acknowledged; a different fresh intent follows
revocation and must be above the retained barrier. An equal revision with
a different ID is not a new intent. Stop remains terminal for the session.

Renew every 5 s from **loss-grade** evidence: successful authorized reads
for the same account/peer and a reachable registry sync path with peers.
A healthy `syncing` index alone is not loss. No peers, unknown/error state
or failed reads do not renew; expiresAt is at most observedAt + 30 s, not
send time + 30 s. Never extend from stale data or reuse evidence across
leases. Once closed by expiry/sustained loss, reopening needs two fresh
activation-grade samples and winner/history checks. Revoked intent never
reopens. Reject expired/future/inconsistent timestamps, old policySeq or
a mismatched activation intent (ID or revision); persist no open grant across
process death.

Callbacks carry monotonically increasing worker stateSeq and the highest
applied policySeq; duplicate/stale acknowledgments cannot undo newer state.
A requested Status correlates to its RequestStatus request. A spontaneous
expiry/Close, resource or health Status correlates to the session's accepted
Start request, with the same lease/session and fresh stateSeq; no invented
status request is needed. The same state/applied-policy high-water checks
apply to both. A Status carrying an open grant, like AdmissionApplied, must
match main's latest sent policySeq before it may represent an open gate;
an older worker snapshot cannot undo a newer pending Close.
The bounded sender has a single latest-wins admission slot. Close/Stop
preempts queued Open/renew/status traffic, invalidates older sends and uses
a reserved control slot; an in-flight Binder transact cannot be recalled.
Main closes its local gate immediately; remote closure occurs on application
or lease expiry (≤30 s after last good evidence), not instantaneously by
assertion. Ownership loss (through the Release main sends with its revoking
Close) and Stop additionally cancel and use §4's ≤15 s retirement/death
deadline. A callback for any accepted command is due within
5 s; silence, including activate, is a health failure. Terminal prepare/
release/drain deadlines remain their own budgets, not this receipt budget.
A stuck sender cannot create unbounded replacement threads.

Effective dispatch is the unexpired main grant AND worker resource/health
gates AND the active verdict/ownership checks in §5. Main renewals cannot
override memory pressure; resource recovery still obeys §2 hysteresis.
Expire/Close emits a new sequenced status and cancels queued admissions.
Already admitted runs keep their existing budget on every Close — sync loss,
and, as on desktop's stand-down, a revoking one (ownership loss, prune,
deauthorization) — and Release and Stop interrupt them (amended 2026-11-06
by the user's ruling, R9). Within a worker, possibly committed runs are
never automatically replayed; across a worker death nothing durable marks a
run started, so as on desktop a once shot whose run was cut off fires again
on the next owner (§2).

Worker.release runs stand-down, quiesces all execution and reports Released;
main then clears the registry using the already durable ReleaseOwed record.
A Release that outlives its deadline is answered Failed(DEADLINE_EXCEEDED)
and never by a late Released; quiescence reached afterwards shows only in
the Start-correlated QUIESCENT Status. Kotlin prerequisites before production
wiring (N2b-2 round A review A2/A8, logged in `user-follow-ups.md`): a worker
may host several activations, so L resets its quiescence evidence at every
new activation and drain and never carries one activation's Released over to
the next; and an expired Release deadline triggers retirement, which a later
revoke must not postpone by extending that deadline.
Released is not Drained: preparation/supervision may remain alive.
Worker.stop drains the whole session. On timeout main retires the process
under §4 before attempting its release write. Status distinguishes accepted,
quiescent, registry locally released and release propagated. Main emits host
beats; the worker emits responder beats only while Open/active. Missing
post-Open beats use health recovery, never pre-Open rollback.

Messages have fixed bounded payloads, no arbitrary program text/paths or
large trace bodies. A worker accepts one native session until process exit,
even after Drained. Main caches callbacks for UI; no blocking remote UI read.
RuntimeSnapshot must exist before freezing this protocol. Stage 0's fixed
bridge is not this API. AnyRuntime keeps its mutex private and invokes
retirement under transition ownership; enqueue/ack is not completion proof.

This ADR proposes Android-specific amendments to ADR-009 embedding and
kernel loading, ADR-015 election, ADR-025 preparation/presence gating,
ADR-011 unsupported consent, ADR-019 time, ADR-003 resources, ADR-006
capacity/scheduling and ADR-018 event admission. Desktop defaults remain
intact. Add reciprocal pointers after acceptance, not silent changes to
accepted contracts.

Cross-repository implementation in anybao and any-kotlin must update:
`MainActivity`/`AnytypeApplication` headless/lazy initialization,
`AnyRuntime` snapshots and PoC-only cold transition callers,
`docs/verticals/app-state-and-auth.md`, `docs/verticals/bao-status.md`,
`BaoDevices`/`AgentDevicesViewModel` preparation readiness, variant/backup/
telemetry rules and test injection/export. `any` is a **future real-account
prerequisite** for all-route token authentication; a bounded Go drain API
is optional follow-up, not assumed by this PoC. Existing production backup
exclusions and PROD crash-reporting exposure remain documented follow-ups
in the notes. This revision chooses preparation beats over an upstream
claim-clear API and cold-process cleanup over an upstream drain API.
The cold-process fallback is a proposed, dated (2026-11-02) PoC exception
to `CLAUDE.md`'s upstream-fix rule. It is not a production workaround;
before extending it beyond this experiment, assign an upstream drain
contract issue and resolve the production approach. No issue exists yet.

Stage 0 is accepted for §0 as recorded above; Stage 1 was explicitly accepted
on 2026-11-05 after its feasibility evidence and covers the full anybao/any-kotlin host and the
declared upstream boundaries. No real-account enablement
or upstream production rollout is authorized by this document. No separate
Android maintainer or DROID issue was supplied; record one if assigned,
never invent it. Qualification results remain separate from implementation acceptance.

## References

- Android: [service types](https://developer.android.com/develop/background-work/services/fgs/service-types),
  [timeouts](https://developer.android.com/develop/background-work/services/fgs/timeout),
  [Service / sticky restarts and process importance](https://developer.android.com/reference/android/app/Service),
  [exit reasons](https://developer.android.com/reference/android/app/ApplicationExitInfo),
  [battery restrictions](https://developer.android.com/topic/performance/background-optimization),
  [Doze](https://developer.android.com/training/monitoring-device-state/doze-standby),
  [Advanced Protection / inactivity reboot](https://support.google.com/android/answer/16339980?hl=en),
  [Android 17 behavior](https://developer.android.com/about/versions/17/behavior-changes-all).
- Wasmtime: [platform support](https://docs.wasmtime.dev/stability-platform-support.html),
  [trusted precompiled artifacts](https://docs.wasmtime.dev/examples-pre-compiling-wasm.html),
  [49.0.3 stack configuration](https://github.com/bytecodealliance/wasmtime/blob/v49.0.3/crates/wasmtime/src/config.rs),
  [49.0.3 ResourceLimiter scope](https://github.com/bytecodealliance/wasmtime/blob/v49.0.3/crates/wasmtime/src/runtime/limits.rs).
- Telemetry fixture: [connection-owner UID](https://developer.android.com/reference/android/net/ConnectivityManager#getConnectionOwnerUid(int,%20java.net.InetSocketAddress,%20java.net.InetSocketAddress)),
  [per-app VPN routing](https://developer.android.com/reference/android/net/VpnService.Builder#addAllowedApplication(java.lang.String)).
- Reference projects: [ZeroAI](https://github.com/Natfii/ZeroAI),
  [andClaw](https://github.com/coderredlab/andClaw),
  [FlutterClaw](https://github.com/flutterclaw/flutterclaw),
  [PocketClaw](https://github.com/pocketclaw/pocketclaw).
  Pinned observations are in the companion notes.
