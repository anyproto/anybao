# Android host PoC — investigation notes

Updated: 2026-11-03. Design: [ADR-034](adr/034-android-agent-host.md),
Stage 0 accepted for implementation; Stage 1 remains proposed/frozen.
Current response: [Final-review dispositions](plans/android-host/2026-11-03-round-3-response.md).
Round-2 response: [dispositions](plans/android-host/2026-11-03-round-2-response.md).
Earlier response: [first plan review](plans/android-host/2026-11-03-review-response.md).
The visible S0a candidate is built and ready for device testing:
[candidate and local checks](plans/android-host/reports/s0a-ready.md).
All four post-fix reviews are clean. Current evidence and agent rounds are
tracked in [the implementation log](plans/android-host/implementation-log.md).
The [round-2 fixture report](plans/android-host/reports/s0-fixture-round-2.md)
records pinned local route evidence and explicitly unmeasured provider/device work.
The initial preflight below is historical, not current device/build validation.

## User constraints

- Nothing Phone (2a), updated Android/Nothing OS; exact versions unknown.
- Normally always connected to a charger.
- Either a separate app or integration was acceptable for the PoC.
- Recommendation after source inspection: opt-in integration in any-kotlin,
  off by default; ordinary app use starts neither host FGS nor Rust worker.
- On 2026-11-03 the user selected an 8-hour supervised screen-off probe on
  charge as part of Stage 0, then authorized implementation after final review.
  Stage 0 is accepted; Stage 1 is not.
- For the later end-to-end check, run Any on the Mac in a separate data
  directory using the Android throwaway account. Android is the only Bao host
  for that account; send from the Mac and verify that its reply syncs back.
- OpenAI API is selected. The user supplied a local key and explicitly chose
  to share it between phone and desktop health probe; record the shared quota.

## Local preflight — historical, before implementation

| Check | Observed result |
|---|---|
| anybao checkout | `bf6900d`; existing unrelated untracked work left intact |
| any-kotlin checkout | `cd4f4f39`; no edits made |
| Android CLI | Installed; documentation search/fetch succeeded |
| Device connection | `adb devices -l` returned an empty device list |
| Android SDK | Platforms 35 and 36 installed |
| Android app build | Requires compile SDK 37, targets 36, minimum 28; ARM64 only |
| NDK | `28.1.13356709` installed |
| Rust environment | No cargo/rustc/rustup/nix found in PATH or checked standard installation locations |
| Runtime requirements | `rust-toolchain.toml` pins 1.99.0; Wasmtime/wasmtime-wasi 49 |
| Kernel | Existing `bin/kernel.wasm`, approximately 24 MB; build provenance not revalidated |
| Native bridge | No Android/JNI binding or Android build pipeline found in anybao |
| Cross-build/device tests | Not run; toolchain setup and device connection still required |

The Android CLI's own local-cache writes and public source downloads were
run with sandbox escalation and succeeded. No approval-review rejection
occurred. The available toolchain has not established Wasmtime 49 or the
current kernel's Android compatibility.

## Relevant local integration points

- `any-kotlin/app/.../runtime/AnyRuntimeImpl.kt`: singleton Go runtime,
  actual `baseUrl`, serialized start/stop/wipe; start may take 10–18 seconds.
- `any-kotlin/docs/verticals/app-state-and-auth.md`: boot, logout and
  network lifecycle. Authentication must be checked independently of UI
  `Home`, which is also reachable after some failed probes.
- `any-kotlin/feature/feature-agent-devices/agent-devices-impl/`: existing
  runtime-presence-gated device control. Its current documentation
  explicitly says Android has no local bao runtime.
- `any-kotlin/state/.../agent/BaoStatusSource.kt`: shared presence stream.
- `runtime/src/serve.rs`: `start`, `AgentHandle::stop`, inline scheduled
  runs on the joined ticker, detached chat/event work, and the currently
  unconditional loopback control listener.
- `runtime/src/config.rs`: `ConfigBuilder`, overlays, paths and control port.
- `runtime/src/runner.rs`: embedded kernel, Wasmtime engine and desktop
  default cache discovery.

## Reference implementations inspected

These are observations at pinned commits, not assurances of reliability
on the user's phone. The source was read, not executed or copied.

| Project / revision | Relevant implementation | Implication |
|---|---|---|
| [ZeroAI `bbf422a`](https://github.com/Natfii/ZeroAI/tree/bbf422a7940bc6c5608685098b6ae28f48deadaa) | `ZeroAIDaemonService.kt`, `BootReceiver.kt`, manifest; Kotlin + Rust/UniFFI, sticky service, timed wake lock, opt-in boot; daemon manifest includes `specialUse` plus an optional media-projection use | Closest architecture reference. Anybao only needs the continuous agent host, not screen capture. |
| [andClaw `92f659d`](https://github.com/coderredlab/andClaw/tree/92f659d4538b6358f654f471df7e7d9cb74596e3) | `GatewayService.kt`, manifest; `specialUse`, wake locks, boot/update/watchdog receivers, PRoot gateway | Useful supervision model; its Linux userspace is unnecessary for an existing Rust library. |
| [FlutterClaw `57fd65b`](https://github.com/flutterclaw/flutterclaw/tree/57fd65b50a9310c009c567850761d8cd82b9645e) | `lib/services/background_service.dart`, Android manifest; boot-enabled foreground task, wake lock, `dataSync\|remoteMessaging` | Do not reproduce the data-sync type for an Android 15+ continuous host. |
| [PocketClaw `fa8afab`](https://github.com/pocketclaw/pocketclaw/tree/fa8afab21ac8756ac7463269a6e1eace21f80530) | `scripts/boot-openclaw.sh`; Termux boot, PID checks, watchdog/cron and device-level modifications | A separate appliance experiment, not the smallest path into any-kotlin. |

## Next implementation checkpoint

The [implementation plan](android-host-implementation-plan.md) now starts
with S−1a spike inputs and S−1b full-host source/fixture verification, then
separately accepted Stage 0: K1–K6 native/toolcaller measurements followed
by K7, the user-selected 8-hour supervised screen-off probe on charge.
Full Stage-1 acceptance follows evidence, not precedes it. Its dependency
tracks and qualification campaign replace the historical P0–P7 sequence.
Stage 0 is accepted and underway; Stage 1 is unaccepted. The host remains
opt-in/off by default.
Stage 1 is frozen until K1–K7 measurements and S−1b source checks exist;
use the [S0 report template](plans/android-host/s0-report-template.md) for that gate.

The sibling any source currently describes v0.2.3-nightly.20260925.1,
whereas any-kotlin pins AAR v0.3.5. **Earlier server-source checks below are
historical, not verification of the pinned AAR.** S−1b must recheck that
tag and establish compatibility; tracked API-input refresh/build occurs
after Stage-1 acceptance. No upstream source checkout or pinned-server build was
performed while addressing this document review.

Local build/test reports now live in docs/plans/android-host/reports and are
linked from the current candidate report above. Measured phone gate reports
remain pending.

## Round 1 response — historical

The original review file is unchanged. The table maps every finding to
the revised proposal; “addressed” here means specified in that proposal,
not built, tested, accepted by the reviewer, or accepted by the user.
This records the first revision. Round 2 below supersedes its Go-drain,
election, capacity and network-loss dispositions; ADR-034 is the current
contract. The earlier table is retained so the review history is legible.

| Finding | Disposition in revised ADR-034 |
|---|---|
| X1 | §3 names main's headless service, independent fail-closed probes, and later Activity attachment; §7 lists the comments/docs to amend. |
| A1 | §4 retains the secure lock and measures settings; T6 explicitly expects no service before first unlock. T7 lasts 80 h. Disabling Advanced Protection/PIN is not presumed authorized. Full unattended reboot availability remains outside this proposal. |
| A2 | §4 consumes exit records on every entry, defines stop/update handling and narrows host support to API 34+. Recents removal can be indistinguishable from user stop; no promise to bypass that. |
| A3 | §4 persistent transition gate spans stop/wipe/reinitialize; app-owned cleanup cannot be abandoned by a cancelled ViewModel waiter. |
| A4 | §3 promotes first on each start and defers graphs/IO; T2 measures the whole process deadline. Official Service docs correct the review's tentative sticky-restart restriction claim (see below). |
| A5 | §3 checks Restricted mode and actual exemption; T10 tests forced Doze unplugged. |
| A6 | §3 wake lock includes startup/backoff/drain; renews independently every 60 s with 180 s timeout. |
| A7 | T8/T9 measure both processes; Android 17 memory-limiter evidence is conditional on the actual OS. No OS version is assumed. |
| A8 | §3 in-app Stop is canonical; notification may be denied/dismissed and shows active/standby. |
| B1 | §4 corrects inline-trigger blocking, makes boot cancellable, bounds worker drain at 10+5 s, and blocks wipe until worker death/drain and successful Go stop. |
| B2 | §1 chooses a same-APK `:bao` Rust worker; main retains FGS/Go ownership. §4 adds persisted crash markers and a 3-in-10-minute breaker. |
| B3 | §4 defines generation leases, lock order, no callback inversion and all runtime teardown entry points. |
| B4 | §2 requires RAII, essential-thread health, worker retirement on panic and retained symbols; §4 joins all helpers. |
| B5 | §2 uses a build-time precompiled trusted component, pins engine/artifact invariants, and disallows on-device compile/cache fallback. |
| B6 | §1 removes Go from the Wasmtime process; §2 still tests ART/Wasmtime traps and independent Go recovery. No unverified signal-order or slowdown assertion adopted. |
| B7 | §2 caps active stores and aggregate per-store memory, removes idle epoch ticks and bounds admission. T8 measures whole-app memory/CPU. |
| B8 | §2 fixes absolute paths, rejects OAuth initiation, supplies Android time at the effect boundary, and compile-excludes the control listener. |
| B9 | §2 specifies versioned AAR/symbols, exact Wasmtime 49.0.3 and tested server pairing; dedicated frozen test overlays define the PoC channel. |
| B10 | §2 defines rustls/public roots, no automatic Android CA/proxy inheritance, direct HTTPS/IPv6 tests and unsupported custom-CA/proxy environments. |
| C1 | §1 selects the review's throwaway-account option. All-route authentication is an explicit upstream prerequisite for real-account use, not claimed implemented. |
| C2 | §5 releases `apps.bao` after drain, persists failed release intent and documents stale claims after crashes/uninstall; T4/T5 test handoff/failure. |
| C3 | §5 uses explicit claims and fail-closed election, including error-degraded verdicts with `enabled=true`; §3 requires a real successful bootstrap response. |
| C4 | §5 cancels/gates on network loss and reconciles before resuming. T10 requires zero observed duplicates. The risk remains accepted only for synthetic effects; no exactly-once or guaranteed at-least-once claim. |
| C5 | §2 supports test API keys only; ChatGPT-local grants and OAuth initiation fail before dispatch. Activation checks supported-provider readiness as well as presence. |
| C6 | §3 binds authorization, account, peer/prune and bootstrap checks to §4 generation ownership. |
| C7 | §5 excludes data from backup/transfer, defines private redacted logs and lost-phone steps. Non-debuggable dedicated appId and test account replace real-account debug use. |
| C8 | §1 names `baoPoc`, scoped implementation dependency and no-op alternatives; T1 checks release artifact contents. |
| C9 | §6 defines T1–T11, thresholds, kill criteria, named probes, locked-screen export and 80 h non-debuggable measurement. Budgets are proposed, not measured. |
| C10 | §7 names APIs and cross-repo changes; §3/§5 cover notification/device name. The user is the requested acceptance authority; no named DROID issue/maintainer is invented. |

### Independent source checks and corrections

- Confirmed `serve.rs` runs cron/once inline on the joined ticker and
  detaches other work; `join_all` currently returns early on panic.
  `anyapi.rs` allows 300 s local calls; `broker.rs` permits much longer
  guest HTTP timeouts. Deadline-aware cancellation needs implementation;
  a Rust timeout cannot terminate a blocked thread by itself.
- Confirmed `AnyRuntimeImpl`'s ephemeral port, independent stop/wipe
  calls, and retry after `StartSuperseded`. A drain participant alone
  cannot close the stop-to-wipe race without the transition gate.
- Confirmed `MainActivity`/`AnytypeApplication` deliberately avoid
  server boot for ordinary headless activation.
- **Additional finding:** `ServerBootstrapMonitor` drops its flag after
  repeated failures/timeout. The review's recommendation to rely on it
  would still fail open. Use a fresh successful health response instead;
  even that is not proof of global registry convergence.
- Confirmed election can degrade to active even with `enabled=true`
  when initial reconcile fails. Testing `enabled` alone is insufficient.
- Confirmed `any/docs/23-devices.md` supports `apps.bao = null` and
  retains claims. **Additional edge case:** restoring the app flag can
  revive a retired claim; §5 therefore requires explicit activation
  consent before such re-registration after off.
- Confirmed current management-token scope excludes ordinary data
  routes, and any-kotlin's client has no all-route bearer token.
- Confirmed `benchmark` is non-debuggable but retains `.dev` in current
  Gradle code. The new `baoPoc` appId is separate. Existing CLAUDE.md's
  `.dev2` statement is stale and should be corrected in that checkout
  during integration; it is not used as evidence here.
- [Android's Service documentation](https://developer.android.com/reference/android/app/Service#START_STICKY)
  explicitly exempts sticky FGS **restarts** from the general background
  start restriction. The review's tentative claim that a null-intent
  restart requires a battery exemption is not adopted.
- [ApplicationExitInfo](https://developer.android.com/reference/android/app/ApplicationExitInfo#REASON_USER_REQUESTED)
  confirms force-stop/Recents ambiguity and pre-34 update ambiguity.
  Actual Nothing behavior remains untested.
- [Google's Advanced Protection help](https://support.google.com/android/answer/16339980?hl=en)
  documents a 72-hour locked-device reboot protection. Nothing OS
  settings and their enabled state remain unknown. Retain security
  settings and expose the availability tradeoff rather than silently
  changing them.
- [Android 17's official behavior documentation](https://developer.android.com/about/versions/17/behavior-changes-all#app-memory-limits)
  confirms the memory limiter. `REASON_OTHER` with `MemoryLimiter:AnonSwap`
  is resource pressure, not a user stop; collect it only on relevant OSes.
- [Wasmtime's precompilation guidance](https://docs.wasmtime.dev/examples-pre-compiling-wasm.html)
  requires trusted serialized artifacts. AOT is selected to remove cold
  compile/cache uncertainty; this response does not assert that Android
  categorically forbids JIT anonymous executable memory.

## Round 2 response — historical design revision

This records the second revision; the linked 2026-11-03 response and
current ADR supersede its stage, ownership and test-order descriptions.
All 21 findings had proposed dispositions below. This does not declare
them accepted by the reviewer or demonstrated on Android. No runtime,
any-kotlin or upstream any implementation changed in this response. The
review file remains unchanged.

| Finding | Disposition in revised ADR-034 |
|---|---|
| R2-1 | §4 chooses PoC-only pending-operation journaling and main-process recycling for destructive transitions/Go replacement. `Mobile.stop()` success never permits deletion. Next explicit launch completes cleanup before Go; ordinary variants keep their behavior. The dated exception to the upstream-fix rule is explicit in §7. |
| R2-2 | §4 unbinds before every kill and on failed/disconnected bindings, rejects late callbacks, confirms old worker death, makes workers single-session and retires orphans before new Go startup. T3/T5 cover resurrection/early-init failures. |
| R2-3 | §4 withdraws the global 30 s cap. Running calls retain class budgets; 10 s is a cooperative grace period, followed by forced worker termination and 5 s death confirmation when blocking I/O cannot be interrupted. |
| R2-4 | §5 requires broker egress enforcement across all guest transports, validated/pinned addresses and redirects, an exact lease-origin data-route allowlist and denial of registry mutations. T11 includes forged claims/prune and a second synthetic listener. Inventory other Any installations; do not test against real data. |
| R2-5 | §5 chooses PreparedUnregistered mode plus fresh `bao.host.status.v1` beats and matching local Binder readiness. Re-enable as standby does not register or revive a claim. Explicit activation registers; failure rolls back. T4 covers off → standby → activation. |
| R2-6 | §3 brackets health with authorized same-account/generation auth reads. §5 requires two fresh tech-index synced/space-peer samples before activation/reconnect, followed by registry reconciliation. No health bit is treated as global convergence. |
| R2-7 | §2 adds class lanes with 768 MiB aggregate guest memory, CapacityDeferred without trigger failure/consumption, admission before once/cron/event state changes, and bounded pending work. T8 measures PDF/mail peaks. T7 retains standing jobs and permits heartbeat lateness up to one 1200 s run plus margin. |
| R2-8 | §4/§7 keep AnyRuntime's mutex private, publish atomic snapshots, register send-intent under the bao lock and retire races before Go changes. All Binder methods/callbacks are oneway; bounded senders run outside locks, policy and deadline threads. Status is cached from callbacks. |
| R2-9 | §5 defines 30 s sustained sync-path loss separately from provider failure, closes new admission and permits in-flight completion within existing budgets. Renewed sync/election precedes backlog reconciliation; uncertain interrupted effects require explicit retry. |
| R2-10 | §4's explicit exit table corrects profile-stop semantics, handles package/permission/freezer/resource/unknown deaths, and correlates summaries plus timestamps. Memory-limiter constants are guarded for the installed API/extension. |
| R2-11 | §2 uses each process's own RSS plus system getMemoryInfo, with thresholds, stale-data rejection and recovery hysteresis. No dependency on running-memory trim callbacks. |
| R2-12 | §7 health reads atomically published snapshots/counters, never HTTP-held service locks. Wait classes/deadlines distinguish legitimate long requests from stalled supervision; panic state is independent. |
| R2-13 | §2 names exact Cargo feature sets, separate precompiler/Android invocations and a shared explicit EngineProfileV1. Android excludes compiler/cache/raw kernel; T1 checks transitive dependencies, T8 includes artifact-copy memory. Baseline compatibility remains untested. |
| R2-14 | §6 self-samples smaps_rollup every 30 s and status RSS every 5 s, with worker sequence/time reporting and stale-data failure. No assumption that getProcessMemoryInfo produces fresh samples at that rate. |
| R2-15 | §6 defines final reply identity/nonce/marker/successful turn, sender-clock latency and observation through hour 81. Independent canaries attribute misses; budget is at least 2× projected spend. External outages invalidate a clean run rather than silently disappearing from results. |
| R2-16 | Added a proposed android-poc row and fixture in environments.md: pinned binaries, separate user/repo accounts, ports 7140/7141/7142, config/blob/data paths and Mac probe sender. Nothing is provisioned. Desktop bao is stopped during the phone soak. |
| R2-17 | §1/§5 explicitly disable Sentry and Amplitude in baoPoc/main, avoid the main graph in :bao, and require worker maps without Go/Sentry plus zero telemetry traffic. Production follow-ups are recorded below. |
| R2-18 | §6 specifies a variant-only DUMP-protected, locally enabled/expiring fault receiver with fixed actions/nonce, plus fake-server cases. Fault exits differ from user Stop. T1 checks release absence; prune runs last. |
| R2-19 | §4 checks the persisted breaker before Hilt, counts failed/never-connected binds and early deaths, and keeps the tripped path minimal/non-sticky. T5's 120 s bound fails when OS restart delivery is absent or late. No unverified OS backoff multiplier adopted. |
| R2-20 | §6 specifies a wall charger and plug/idle/power-save evidence. Unplugging invalidates the charged soak. Forced-Doze/network tests collect locally with a pre-armed bounded restore path. |
| R2-21 | §3 permits one inexact wake-lock recovery alarm, gated by enabled/account/exit/transition/breaker state and cancelled on off/failure. It makes no delivery bound and cannot turn a wake-lock lapse into a passing soak. |

Also incorporated the review's residual details: warm promotion timing
starts at onStartCommand; a harmless self-row write distinguishes prune
from unregistered state; explicit dependency fallbacks replace the inherited
debug-only fallback; T11 tests manual takeover after crash/uninstall;
§7 includes ADR-025/ADR-011 and the upstream any boundary. Device model,
RAM and exact OS remain to be read from the connected test phone, not
inferred from an `A063` label in another skill.

### Round 2 source checks

- Local source confirms Go stop's unconditional nil, delayed straggler
  cleanup and unbounded graceful wait; election boot's registration,
  the app-presence gate, health's separate authorization reads and the
  tech-space sync endpoints; existing LLM/server timeout classes and
  once/cron/event admission effects. The proposal changes these host
  contracts; none is claimed already implemented.
- [ServiceConnection](https://developer.android.com/reference/android/content/ServiceConnection)
  confirms a binding remains active across service-process death and
  creation may fail before a connection callback. This makes unbinding
  and an independent failed-bind deadline necessary.
- [ApplicationExitInfo](https://developer.android.com/reference/android/app/ApplicationExitInfo)
  distinguishes profile stop from user-requested app termination and
  documents memory-limiter exit reporting. Guard newer constants rather
  than assuming the user's OS version.
- [ComponentCallbacks2](https://developer.android.com/reference/android/content/ComponentCallbacks2)
  documents the API 34 running-memory notification changes;
  [ActivityManager](https://developer.android.com/reference/android/app/ActivityManager#getProcessMemoryInfo(int[]))
  documents memory-info sampling limits. Self-sampling still needs
  validation on the actual non-debuggable APK.
- Exact [Wasmtime 49.0.3 features](https://github.com/bytecodealliance/wasmtime/blob/v49.0.3/crates/wasmtime/Cargo.toml)
  and [WASI features](https://github.com/bytecodealliance/wasmtime/blob/v49.0.3/crates/wasi/Cargo.toml)
  support separate compiler/runtime feature selections. Inspection of
  the manifests is not evidence that the proposed engine profile loads
  this CPython artifact; T1 must establish that.
- The repo's config schema confirms `[agent].control_port` and
  `[paths].traces` for the desktop fixture. Existing per-rig TOMLs named
  in environments.md are not present in this checkout, so the new
  fixture is specified from the schema, not copied from a live config.

### Follow-ups outside this PoC

These are recorded work items, with no assigned maintainer or external
issue yet. Do not interpret this list as a production change or rollout.

- **any all-route authentication:** required before any real-account
  Android host use; protect data/meta/stream/mutation routes and upgrades,
  not only management endpoints. An app-local egress rule does not
  protect the server from another installed app.
- **any bounded drain contract:** upstream reporting of drain/stragglers
  may replace the dated cold-process PoC exception. File/assign the
  upstream issue before promoting that fallback to production.
- **any-kotlin production backup/device transfer:** review the shipped
  identity/secret exclusion policy and fresh-device recovery separately;
  the variant-specific exclusion does not repair production manifests.
- **any-kotlin production telemetry:** review PROD Sentry/Timber content
  and multiprocess initialization separately. PoC telemetry is disabled;
  no dedicated DSN or production permission change has been created.

### Still open

Stage-0 device/fixture readiness and K1–K7 results; completion of pinned-source
verification; subsequent Stage-1 acceptance and all T1–T11 results; and upstream
all-route authentication before any real account. The secure-lock
pre-unlock outage and partition/indeterminate-effect limits are declared
scope boundaries, not defects claimed fixed by documentation.
