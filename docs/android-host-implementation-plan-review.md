# Android host implementation plan — review record

Scope: the [implementation plan](android-host-implementation-plan.md)
(Draft, 2026-11-02, milestones P0–P7). It implements
[ADR-034](adr/034-android-agent-host.md), whose own reviews are in the
[ADR review record](adr/034-android-agent-host-review.md). This is a review
of documents and source only. Nothing was built or run on a device.

Method: three independent reviewers each read the plan, the ADR, the PoC
notes, the ADR review and the code through one lens. They had the code,
but not each other's conclusions. The coordinator merged the overlaps,
re-rated where needed and re-read the source behind every high finding
(see the verification log at the end). Paths are relative to the checkout
parent: `anybao/`, `any-kotlin/`, `any/`. A path without a prefix is under
`anybao/`. `plan:NN` and `ADR:NN` are line numbers in the plan and ADR as
of 2026-11-03.

Everything down to the Round 2 heading is round 1, on the first draft
(P0–P7). Its line numbers refer to that draft.
[Round 2](#round-2-revised-plan-2026-11-03) reviews the first revision
(S−1, S0, then the Stage-1 tracks), and
[Round 3](#round-3-second-revision-2026-11-03) the second. Both are at the
end of this file.

| Lens | High | Medium-high | Medium | Low |
|---|---|---|---|---|
| A. Sequencing, risk retirement and scope (as reported) | 4 | 2 | 5 | 1 |
| B. Source fidelity and engineering correctness (as reported) | 3 | 0 | 7 | 2 |
| C. Verification, evidence and operability (as reported) | 5 | 3 | 3 | 1 |
| **Merged, coordinator-rated** | **4** | **5** | **10** | **6** |

Many findings came from more than one lens. The merged list below gives
each one once and names its sources.

## Verdict

**Do not start implementation from this plan as written.** The content is
mostly right: the code claims match the source, the work items cover the
ADR, and the rules ("no fallback to a shared process", "do not rebuild
qualified bytes", "no emulator credit for device gates") are good. The
problem is the order of work and what the plan leaves out.

- **It does not adopt R3-1.** P1 is described as "intentionally narrow",
  but it needs about 19 build items before the first native byte runs. It
  never runs a real turn: the first live chat is at P5 exit, after the
  election, egress, lifecycle and journal work. No gate has a numeric kill
  threshold or a "stop the project" outcome (IP-1).
- **The existing election loop would re-claim on its own.** The plan
  fences `election::boot`, but the 10 s `reconcile` loop also claims. This
  is the code path behind R3-2, which the plan doesn't schedule (IP-2).
- **The test campaign has no tools.** No milestone builds the harness, the
  network fixture T10 needs, the second-UID probe app T11 needs, or an
  evaluator that turns evidence into pass/fail (IP-3).
- **The expensive tests come in the wrong order.** T10 and the destructive
  T11 arms run after the 80 h soak, and any fix they force invalidates the
  soak. Reruns have no budget or cap (IP-4).

Blocking amendments before implementation starts:

1. Replace P1 with a Stage-0 spike that runs one real turn, give it
   numeric GO / PIVOT / STOP criteria, and accept Stage 0 alone (IP-1,
   IP-5).
2. Fence the periodic `reconcile` under `PrepareThenExplicitClaim`, and
   define the registered-but-not-winner state in ADR §5 (IP-2).
3. Add the harness, network fixture, probe app and evaluator as named
   deliverables (IP-3).
4. Run T10, the destructive T11 arms and a short smoke soak before the
   freeze. Add a change-impact matrix and an attempt cap (IP-4).
5. Before the first `baoPoc` build, decide the variant's Firebase/FCM
   setup and diagnostics (IP-7, IP-8).
6. In P0, re-verify the `any` side at the pinned `v0.3.5` (IP-14).

## Round-3 findings: how the plan handles them

| R3 finding | Plan | ADR text | Status |
|---|---|---|---|
| R3-1 staged acceptance, Stage-0 spike with kill criteria | P1 is "intentionally narrow" (plan:44) but is not a spike: no real turn, no thresholds; P0 still requires full acceptance (plan:27) | Unchanged | **Open** → IP-1, IP-5 |
| R3-2 lost claim revives | Only "without implicit claim revival" for off → standby (plan:215) | ADR:511 still lets an activated phone dispatch whenever `active.bao` is self | **Open**, and worse than R3-2 said → IP-2 |
| R3-3 "fresh" sync evidence undefined | Not mentioned | ADR:507-508 unchanged | **Open** |
| R3-4 what the user sees at the fresh-process boundary | P5 "explain … pending destructive cleanup" (plan:283-285); no T3 check | Unchanged | **Partly** |
| R3-5 egress removes LAN MCP/model endpoints | Not mentioned | §2 unsupported list unchanged (ADR:173-180) | **Open** |
| R3-6 `routes.rs` is not a security boundary | Adopted: "not itself an access-control allowlist" (plan:183-185) | ADR:540-541 still says "from the vendored route classifier" | **Plan only**; the ADR contradicts it |

The plan's coverage table lists Round-2 findings only (plan:372-379). The
PoC notes have no Round-3 response. The plan says contract changes need an
ADR amendment (plan:16-18), and anybao `CLAUDE.md` asks for the ADR to be
amended in the same change, so R3-6 being fixed in the plan alone breaks
its own rule.

## High

**IP-1. High. P1 is not the R3-1 spike: it is large, runs no real turn,
and has no kill criteria.** (A1, A2, C5, B10)

- Plan text: "The first milestone that executes native code is
  intentionally narrow … It does not run a live agent" (plan:44-47); "do
  not enable … model-driven guest requests before P3 passes" (plan:128-129).
- Before the first native byte runs on the phone, P1 requires (plan:86-134):
  - the feature split, `EngineProfileV1`, a host precompiler and an
    artifact manifest;
  - a JNI library, plus local and CI AAR packaging;
  - two new Gradle modules, an unavailable binding and the `baoPoc`
    variant;
  - lazy Application/Hilt init, the `:bao` worker and a foreground owner;
  - the oneway protocol envelope and session identity;
  - a DUMP-protected, expiring test seam;
  - self-sampling on the non-debuggable build;
  - 16 KB-emulator, IPv6 and release-APK checks.
- R3-1 named three questions most likely to end the project (review:1558-1567):
  1. Does the artifact load?
  2. Does the memory fit during a real turn?
  3. Does a real turn complete?

  P1 answers only the first. The first live chat is P5's exit evidence
  (plan:310-311).
- A real turn needs none of P2–P4. The lib API already exports `start`
  and `run_program` (`runtime/src/lib.rs:74-75`, `serve.rs:1781`).
- "Record kernel load time, artifact size and resident memory"
  (plan:125-127) records numbers but decides nothing. On failure the plan
  only lists what not to do (plan:133-134).
- One arithmetic check the plan never states (computed from ADR numbers,
  not measured):
  - The worker ceiling is 896 MiB (ADR:226) and the guest aggregate
    768 MiB (ADR:204-205).
  - That leaves 128 MiB for the worker's ART, the Rust heap, the `.so`
    and the deserialized artifact.
  - The source kernel alone is 25 MB (`bin/kernel.wasm`).
- Failure: memory or turn behaviour fails after P2–P4, which is most of
  the Rust and Kotlin work.
- Fix: replace P1 with a Stage-0 spike, and move P1's packaging, variant
  and protocol work to Stage 1.
  - Scope:
    - the artifact, profile and feature split;
    - a JNI shim over `serve::start`;
    - a plain `:bao` service started from a debug screen, with no Sentry
      in that process and `baseUrl` taken from the running Go;
    - one Python cell plus trap, fuel and epoch runs;
    - ten real turns using a fixed program with tools off, the broker
      restricted to the provider host and the lease's loopback origin,
      and desktop anyrt stopped;
    - `dumpsys meminfo` alongside the self-samples.
  - Kill and pivot criteria (proposed; the user sets the final numbers):

    | ID | Criterion | Outcome |
    |---|---|---|
    | K1 | Artifact fails to load after ≤2 explicit profile revisions | STOP |
    | K2 | Any unexplained signal fault in ~200 cell/trap runs | STOP |
    | K3 | Worker baseline RSS above 128 MiB | PIVOT (budget amendment); far above it: STOP review |
    | K4 | During a turn, main RSS above 640 MiB, or combined PSS above 1.0 GiB | PIVOT |
    | K5 | Kernel load above 10 s | Review; above 30 s: PIVOT. Go already takes 10–18 s (notes:40) against Prepared ≤60 s |
    | K6 | 10 real turns not within p95 ≤120 s and max ≤180 s | PIVOT |
  - Each gate ends in GO, PIVOT (ADR amendment) or STOP (withdraw ADR-034
    with the evidence). The user decides.

**IP-2. High. The periodic election loop auto-claims; the plan fences only
the boot path.** (B1; coordinator R3-2)

- Plan text: "Implement preparation independently of today's registering
  election boot" (plan:189-190). The ADR: "it does not call today's
  `election::boot` implicitly" (ADR:469).
- How the code behaves today:
  - `election_thread` calls `reconcile` every 10 s
    (`runtime/src/serve.rs:3583-3600`).
  - `decide` returns `Claim` when no other row carries the app, and also
    when other rows exist but none is a live winner
    (`runtime/src/election.rs:136-149`).
  - `reconcile` then calls `activate_device` (`election.rs:176`).
  - A flip from standby to active runs `takeover` (`serve.rs:3619-3627`).
  - ADR-015's documented rule is "row pruned / app uninstalled → any
    survivor → claim" (`any/docs/23-devices.md:96`).
  - Neither the ADR nor the plan mentions `reconcile`.
- Failure: the phone is activated; later the desktop claims; then the
  desktop's row is pruned or loses `apps.bao`. The phone's loop claims
  again without anyone asking. That breaks ADR §5's "no automatic repeated
  claims" and "no liveness lease/failover", and it can produce duplicate
  replies across the handover.
- Fix:
  - Under `PrepareThenExplicitClaim`, `reconcile` may observe but never
    calls `activate` without a current, lease-bound activation intent.
  - When the phone sees another winner: drain, write `apps.bao=null`, and
    return to PreparedUnregistered (R3-2's amendment).
  - Amend ADR §5.
  - Add a T4 arm: activate phone, claim on desktop, remove the desktop's
    `apps.bao`, and expect zero phone claims.
  - Keep `DesktopDefault` unchanged.

**IP-3. High. No milestone builds the test tools.** (C2, C4, C10)

- Plan text: "keep process/death assertions in device tests" (plan:266);
  "Save command recipes" (P6); "pre-armed bounded restoration" (P7.3);
  "telemetry absence … demonstrated" (P5 exit).
- Missing pieces:
  - **Harness.** any-kotlin has one androidTest in total
    (`storage/storage-impl/src/androidTest/.../KeystoreAeadTest.kt`).
    Nothing says whether each T arm is a host-side adb script, an
    instrumentation test or a manual step.
    - In-process instrumentation is the wrong tool for exit-reason tests:
      `am instrument` force-stops the target. That claim is from memory;
      verify it on the device.
  - **Network fixture.** T10 needs sync loss with the provider still
    reachable, and a provider-only outage with sync still up. An unrooted
    phone can't block per destination.
    - `any` turns LAN discovery on by default
      (`any/internal/config/local_discovery.go:13-18`). A Mac on the same
      Wi-Fi can sync directly as a `LocalPeer`
      (`any/internal/api/sync_status.go:12-24`).
    - "Zero telemetry" needs DNS/SNI capture. Per-UID netstats can't
      separate the two processes, because they share one UID.
  - **Probe app.** T11's second-UID probe app and second test listener
    appear in neither the deliverables nor the source map.
  - **Existing doubles not named.** The plan doesn't name the existing
    test doubles it should reuse:
    - anybao `runtime/src/testutil.rs` (`StubTransport` at :17,
      `FakeSpace` at :126);
    - the replay machinery;
    - any-kotlin `FakeAnyClient`, `FakeMobileBridge`, and the
      real-binary integration tests (`ANY_INTEGRATION_BIN`).
  - **Evaluator.** Nothing turns an evidence bundle into pass/fail.
- Fix:
  - Make a checked-in host-side harness a Stage-1 deliverable (e.g.
    `tools/android-poc/`). Give every T arm an executor (script, manual or
    hybrid) and an operator-presence window.
  - Write evidence keyed by run id.
  - Use a separate test APK for probes, never in-process instrumentation
    of the app under test.
  - Add a network fixture to `environments.md`: a dedicated router or
    SSID with scripted drop rules for sync nodes and for the provider,
    LAN discovery recorded on or off, Private DNS off for the telemetry
    window. Dry-run it in P5.
  - Add a checked-in evaluator that computes each arm's verdict.

**IP-4. High. T10 and the destructive T11 arms run after the soak, and
reruns have no budget.** (A3, A10, C3)

- Plan text: P7.3's T10 arms "are separate from the charged soak"
  (plan:352-354); P6 "Repeat earlier tests when the final artifact
  differs" (plan:319-320); P7.2 "rerun the full soak" (plan:350). The
  ADR orders it the same way: "Run fault/recovery tests, then the soak and
  reconnect/Doze tests" (ADR:668-669).
- Failure: the unplugged forced-Doze arm (3 probes ≤180 s) or the
  takeover arm fails after a clean soak. The fix changes the artifact, so
  P6 requalification and a new 81 h soak follow.
- Run invalidators, counted by Lens A:
  1. external outage (plan:349-350);
  2. unplugging or an evidence gap (ADR:601);
  3. a host failure (plan:336-338);
  4. a changed artifact (plan:319);
  5. an overlay change (ADR:165);
  6. any reboot (ADR:457-458);
  7. an immediate-stop event (ADR:680-684);
  8. unknown attribution (ADR:635-636);
  9. overlays pinned to the anybao revision (`docs/environments.md:72-74`),
     so even a Rust-only fix redeploys them.
  - OS, Play services and Mainline updates are outside change control
    altogether.
- Rough odds (inferred, not measured):
  - With a per-probe external miss rate p, a run is clean with probability
    (1−p)^320. That gives 73 % at p = 0.1 %, 38 % at 0.3 % and 20 % at
    0.5 %.
  - Lens C's composite estimate for a clean first attempt is 5–35 %.
  - One late host fix costs about 7–13 days.
- Fix:
  - Run T10 and the abrupt-loss/uninstall T11 arms in P6, before the
    freeze, and amend ADR:668-669. Keep only prune last.
  - Run a 12–24 h smoke soak on the frozen candidate first, and abort a
    soak at its first invalidating event.
  - Add a change-impact matrix: which change forces which reruns.
  - Pin overlays by content hash, not by anybao revision.
  - Cap soak attempts, with a re-plan rule.
  - Freeze OS and Play auto-updates for the campaign. Record
    `ro.build.fingerprint`, the GMS version and
    `pm list packages --apex-only --show-versioncode` at each run's start
    and end.

## Medium-high

**IP-5. Medium-high. The P0 gate is inverted.** (A6)

- Plan text: P0 depends on "ADR acceptance before implementation"
  (plan:27). P0.1 says "Record acceptance … Add the reciprocal pointers"
  (plan:51-53).
- Full acceptance and pointers into eight accepted ADRs (ADR:737-742) come
  before any feasibility evidence. The ADR is still "revised after round
  2" (ADR:3), and R3-2..R3-6 are open.
- The inventory P0.4 produces is an input to the acceptance decision, but
  it arrives after it: model, RAM, OS, page size, the inactivity-reboot
  setting, and other Any apps on the phone.
- The anybao rule is that no code lands ahead of an accepted ADR
  (`docs/adr/README.md`). R3-1's staged acceptance was written to satisfy
  it.
- Fix:
  - P0 becomes: inventory, device reservation, toolchain, and acceptance
    of Stage 0 with K1–K6.
  - Full acceptance, the R3 amendments and the reciprocal pointers come
    after Stage 0.

**IP-6. Medium-high. The Binder protocol has no messages for the intents
P3 depends on, and P1 freezes it.** (B3, A8)

- Plan text: "Fix the bounded oneway protocol envelope and session
  identity now" (plan:115); "Rust consumes session-bound intents and
  reports outcomes" (P3.2).
- ADR §7 defines only `Worker.start`, `requestStatus` and `stop`, plus
  four callbacks (ADR:719-726).
- §5 needs messages it doesn't have:
  - an explicit activation intent;
  - release with pending-release state;
  - admission closure on sync loss.
- The readiness reads exist only in Kotlin (`AnyClient.kt`, `Models.kt`);
  `anyapi.rs` has only the device calls. The plan doesn't say which
  process runs the 5 s sync monitor.
- Session identity needs the `Lease` (`mainNonce`, `generation`, …), but
  `AnyRuntime` exposes only `baseUrl` today
  (`any-kotlin/core/.../runtime/AnyRuntime.kt:16`). The snapshot is a P4.1
  deliverable.
- Failure: P3 adds Binder methods no review has seen, or duplicates
  readiness polling in Rust.
- Fix:
  - Amend §7 with `activate`, `release` and `setAdmission(requestId,
    sessionId, Lease, intent|reason)` and their callbacks.
  - State that main runs the readiness and sync monitor.
  - Move the `AnyRuntime` snapshot to the start of Stage 1.
  - Freeze the message enum with the envelope. With IP-1's spike, that
    freeze moves to Stage 1.

**IP-7. Medium-high. `baoPoc` has no Firebase client, and the push
decision touches T11.** (B2)

- Plan text: "dedicated `baoPoc` build type/applicationId … disable its
  telemetry/backup" (plan:103-105).
- Evidence:
  - The google-services plugin is applied (`any-kotlin/app/build.gradle.kts:7`).
  - `app/google-services.json` has clients for `org.any.anykotlin` and
    `org.any.anykotlin.dev` (lines 88, 107), not for
    `org.any.anykotlin.baoPoc`.
  - `benchmark` builds today only because `initWith(debug)` keeps `.dev`
    (`build.gradle.kts:140`, `:171`).
  - The app uses FCM (`DeviceTokenStoringServiceImpl.kt`, manifest
    messaging service).
- Failure: `processBaoPocGoogleServices` fails with "No matching client
  found" (this is how the plugin behaves; no build was run). Disabling
  the task makes `FirebaseMessaging.getInstance()` throw at runtime.
  Registering a client adds FCM and Firebase Installations traffic, which
  T11's zero-telemetry check would have to account for.
- Fix: P1 decides push for the variant. Either:
  - remove the plugin's effect, the FCM service, auto-init and the token
    store from `baoPoc`; or
  - register a client and list its traffic in T11 as expected and
    non-telemetry.

**IP-8. Medium-high. Diagnostics that change the APK bytes are decided too
late.** (C7, B5)

- Plan text: P1 "Capture each process's own memory/maps … local crash
  evidence … Confirm those data are accessible"; P1 exit "symbolized
  native failure".
- Evidence:
  - A non-debuggable build with Sentry off has no Timber output at all
    (`any-kotlin/app/.../AnytypeApplication.kt:66-73`, which says so in a
    comment). `baoPoc` turns telemetry off, so it has no logs unless the
    PoC plants a file tree.
  - `[profile.release] strip = true` (`runtime/Cargo.toml`), and there is
    no `panic` key.
  - The manifest has no `<profileable android:shell="true">`, so
    simpleperf and Perfetto profilers work only on debuggable apps
    (developer docs).
  - The runtime installs no tracing subscriber for embedders
    (`runtime/src/lib.rs:9-10`).
  - R8 runs on release-like variants and can strip classes referenced
    only from JNI.
- Failure: a native crash in P6 can't be symbolized, there are no logs to
  attribute a soak miss, and any of these fixes changes the bytes after
  the freeze.
- Fix: settle all of this in the spike, or at latest the first Stage-1
  build.
  - Make `baoPoc` profileable.
  - Add an Android Cargo profile with `strip = false` and
    `debug = "line-tables-only"`, plus a build-id keyed symbol archive.
  - Put `catch_unwind` at every JNI entry, with a panic flag in `health()`.
  - Keep JNI pull-only: Kotlin threads call in, and no Rust thread calls
    the JVM.
  - Add R8 keep rules for the JNI surface.
  - Add a logcat tracing subscriber and a bounded file log tree.
  - Prove native-crash capture through
    `ApplicationExitInfo.getTraceInputStream` with `ndk-stack` output.

**IP-9. Medium-high. The phone and the Mac are shared and unreserved, and
the soak has no live view.** (C6, A6)

- Plan text: P5 "remote nonce sender/collector"; P7 "no UI kept open".
- Evidence:
  - The devpass skill uses the same Nothing phone with the dev app on the
    "Stage Kostya" staging account
    (`any-kotlin/.claude/skills/devpass/checklist-rules.md:6`). P6 and P7
    need it exclusively for weeks.
  - R3-1 asked that Stage 0 run only when no Any app on a real account is
    present; decide whether that staging account counts.
  - The Mac is sender, desktop peer and evidence store for 81 h
    (`docs/environments.md`).
  - SAF exports are read after the run, so an export gap at hour 5
    surfaces at hour 81.
- Fix:
  - The user reserves the phone and the Mac in P0.
  - Stop or uninstall the devpass app for the campaign.
  - On the Mac, use `caffeinate -s`, turn off auto-updates, log
    `pmset -g log`, and treat any Mac sleep as invalidating. Use a sender
    clock that counts sleep.
  - Collect `bao.host.status.v1` and heartbeat records live through the
    desktop peer, and alert on the first miss.
  - Give the canary its own provider key, so it doesn't share the phone's
    rate limit.

## Medium

**IP-10. Medium. Preparation's server writes are undefined.** (B11)

- Plan text: "PreparedUnregistered must start no guest jobs" (P3.2).
- Before any election, `serve::start` already does the following:
  - provisions the bao space, the general chat and the agent stores
    (`serve.rs:1790-1798`);
  - bootstraps secrets and config overrides (`:1820-1834`);
  - sends overlay join requests;
  - starts the presence thread and the control API unconditionally
    (`:2092-2097`).
- Failure: a standby phone rewrites the desktop's synced config or
  secrets, or publishes normal `bao.status` beats that ADR §5 reserves
  for responders.
- Fix:
  - List which writes Prepared may make. Seeding the device name belongs
    here too (ADR:500-501, and see IP-11).
  - In Prepared, publish only `bao.host.status.v1`.
  - Gate the control API off on Android. The plan already says so for P1.

**IP-11. Medium. The existing Agent Devices claim flow bypasses the
host.** (B9)

- Plan text: "Preserve the existing remote-device flow" (plan:281).
- Today the screen calls `activate` directly. It re-reads the registry
  every 3 s, offers "Claim again" at 20 s, and reports "the runtime did
  not take over" at 60 s. Only the screen seeds the device name
  (`any-kotlin/docs/verticals/bao-status.md:9`).
- Failure: in `baoPoc` the self switch registers without lease or sync
  checks (fails T4), "Claim again" conflicts with ADR §5's 30 s rule and
  "no automatic repeated claims", and a headless phone never seeds its
  name.
- Fix: in `baoPoc`, self-activation goes only through the host.
  - Adopt the 30 s rule on that path.
  - Move name seeding into preparation.
  - Keep the remote flow unchanged.

**IP-12. Medium. The Cargo restructure is under-specified.** (B4)

- Plan text: "Build host tools and Android library in separate Cargo
  invocations/target directories to prevent feature unification"
  (plan:88-90).
- Evidence:
  - The package enables Wasmtime's default features itself:
    `wasmtime = { version = "49", features = ["component-model"] }` and
    `wasmtime-wasi = "49"`, without `default-features = false`
    (`runtime/Cargo.toml:21-22`).
  - `tiny_http` is unconditional (`:35`). It serves the control API and
    the OAuth consent listener.
  - The `[[bin]]` has no `required-features`.
  - any-ui depends on the crate by path.
- Failure: separate invocations don't help. Any Android lib that depends
  on this package gets Cranelift and the cache, because the package
  turns them on.
- Fix:
  - Set `default-features = false` on both Wasmtime crates, and add a
    `desktop` default feature carrying Cranelift, the cache, the control
    API and loopback OAuth.
  - Make `tiny_http` optional, and give the bin `required-features`.
  - Keep one `Cargo.lock` with `--locked`; a Wasmtime patch mismatch
    fails deserialization.
  - Check T1 with `cargo tree -e normal --target aarch64-linux-android -i
    cranelift-codegen`.
  - Add the any-ui build to the regression list.

**IP-13. Medium. `EngineProfileV1` may miss inputs that deserialization
checks.** (B6; partly unverified)

- Today the engine is `Config::new()` with only fuel and epoch
  interruption set (`runtime/src/runner.rs:203-205`). `kernel_sha256` is
  hashed from the raw bytes at load (`:233`).
- Unverified against the 49.0.3 source, from Wasmtime knowledge:
  - `Config::new()` reads `WASMTIME_BACKTRACE_DETAILS`, which changes a
    checked tunable.
  - Cross-compiling needs an explicit `target(...)` and pinned ISA flags.
  - Deserialization also compares guard and reservation sizes,
    `memory_init_cow`, signals-based traps and debuginfo settings.
- Failure: the precompiler machine's environment silently produces an
  artifact the phone rejects. Copy-on-write memory init behaves
  differently from bytes, which changes per-store RSS.
- Fix:
  - Set every tunable explicitly, and clear `WASMTIME_*` in the
    precompiler.
  - Take `kernel_sha256` from the manifest.
  - Measure instantiation RSS in the spike.

**IP-14. Medium. The `any`-side evidence predates the pinned AAR.** (B8)

- Plan text: "any: No required PoC code change" (plan:388); "allowlist
  against the pinned Any API" (P3.1).
- The pin is `anyRuntimeVersion = "v0.3.5"`
  (`any-kotlin/gradle/libs.versions.toml:2`). The local `any` checkout is
  `v0.2.3-nightly.20260925.1` (`34efbf0`, 2026-09-24) and has no v0.3.x
  tags.
- **This also applies to the ADR review.** Every `any/` citation in rounds
  1–3 was checked against that older checkout, including the registry
  rules behind R3-2 and the sync-status fields behind R3-3.
- Fix:
  - In P0, check out `v0.3.5`.
  - Re-verify `/v1/account` `techSpaceId`, the space GET
    `spaceIndexObjectId`, both sync-status routes, `PUT /devices/me`
    merge, 409 `device.pruned` and health `bootstrapping`.
  - Re-vendor the OpenAPI the allowlist is built from.
  - Build the desktop fixture from that tag.

**IP-15. Medium (conditional). P0 inventories the inactivity-reboot
setting but says nothing about what to do if it's on.** (A4, C1;
coordinator re-rated)

- Plan text: P0.4 "Inventory … secure-lock and inactivity-reboot settings"
  (plan:66-68).
- T7 requires 80 h locked with zero inactivity reboots (ADR:599), and the
  ADR forbids changing security settings (ADR:457-459, 588).
- Both lenses rated this High. The coordinator rates it Medium:
  - The round-1 research found the 72 h reboot is opt-in, under Advanced
    Protection (ADR review A1, rewritten after that research).
  - Google reworded the Play services note to "optional"
    ([9to5Google](https://9to5google.com/2025/04/16/android-auto-restart-security/);
    [Google help](https://support.google.com/android/answer/16339980?hl=en)).
  - Lens C cites reports that disagree about the default
    ([gsmarena](https://m.gsmarena.com/your_android_phone_now_autorestarts_if_its_been_locked_for_three_days-news-67404.php)).
  - It is probably off on this phone, but nobody has checked.
- Fix:
  - Make it a P0 exit item: read the setting and the GMS version.
  - If the setting is on, the user decides before Stage 1, with an ADR
    amendment. The options are: turn it off for the window; allow a
    scheduled lock-screen unlock (no app UI) every <72 h, recorded as a
    fixture action; or run shorter windows.
  - Record `Settings.Global.BOOT_COUNT` at the start and end of each run.

**IP-16. Medium. The fault seam is named, not specified.** (C8)

- Plan text: "variant-only, expiring DUMP-protected test seam" (plan:117);
  "disable the injection session before soak" (P6).
- Unspecified:
  - The receiver must be exported and DUMP-protected. Shell holds DUMP,
    so adb can drive it.
  - Which process hosts it: a broadcast to a dead main cold-starts it and
    perturbs death tests.
  - How the nonce is issued and which clock expires it.
    `elapsedRealtime` resets on reboot, which T6 needs.
  - How early-init faults persist across the three failed starts in T5.
  - Delivery to a force-stopped package.
  - Whether the handoff APK contains the seam.
- Fix:
  - Write a one-page seam spec with the first lifecycle work.
  - Arm faults with a use count, and auto-disarm.
  - Add a status read-back action.
  - Add a pre-soak restore script (`dumpsys battery reset`,
    `deviceidle unforce`, seam off, test breaker reset) whose output goes
    into the run manifest.
  - Put seam state in the hourly T9 export. An enabled seam invalidates
    T7.

**IP-17. Medium. The linear chain creates false dependencies and hides a
few true ones.** (A7, A8)

- Plan text: "Cross-milestone work waits for the preceding review gate"
  (plan:36-37).
- False dependencies:
  - P4 is already specified against "a fake worker/client" (plan:259).
  - Fixture provisioning and provider calibration (plan:296-303) need no
    P1–P4 code.
  - The `bootstrapping = false` decode fix is pure Kotlin
    (`any-client-api/.../Models.kt:31`).
  - The Devices UI can be built against fake status.
  - CI packaging.
- Used before it's built:
  - P1's session identity needs P4.1's snapshot (IP-6).
  - P3.5's key verification needs the keyed throwaway fixture, which is
    a P5 item.
- Fix: draw a DAG. After Stage-1 acceptance, run P2, P3-egress,
  P3-election, P4 (snapshot first), UI, Kotlin decode, fixture and CI as
  parallel tracks with per-track gates. Provision the fixture first.

**IP-18. Medium. ADR requirements with no plan step.** (B10, C11, A9)

These are separate from the Round-3 table above.

- Runtime refusal of an incompatible `any` server version (ADR §2,
  ADR:150-153).
- The notification states and their Stop action (ADR:254-257).
- "CI inspects" the release APK (ADR:102). The plan only inspects
  manually in P1.
- The ADR says the Android artifact goes through "the existing
  repository's artifact workflow" (ADR:155-156). Release CI builds
  desktop tarballs only (`.github/workflows/release.yml:50-52`). The plan
  is right that Android packaging is new; the ADR text is wrong.
- Doc updates are deferred to P7.5. They belong with the code: P4 for the
  runtime and auth vertical, P5 for the bao-status vertical.
- Fix: add these rows to the coverage table, together with R3-2..R3-6,
  and amend the ADR wording in the same change.

**IP-19. Medium. Timing gates have no sample size, clock or owner.** (C10)

- Plan text: P4 "Demonstrate the ADR's promotion and worker-retirement
  deadlines"; P6 "repeated enable/disable".
- Fix:
  - Set N per arm: for example ≥10 cold and ≥10 warm starts, and ≥5 per
    recovery path. A threshold applies to the maximum unless the ADR
    says p95.
  - Name the clocks: `Process.getStartRequestedElapsedRealtime` to the
    return of `startForeground`.
  - Count wakeups from per-thread `voluntary_ctxt_switches`.
  - Commit the attribution rules before T7 (for example, a provider-canary
    failure within ±5 min means provider). The user reviews each
    classification.
  - Keep per-gate reports in their own files linked from the PoC notes,
    so the notes stay readable.

## Low

**IP-20. Low. CI publication sits on the critical path, which is stricter
than the ADR.** (A11)

- The ADR publishes "after local validation" (ADR:155-157), and the plan
  already allows hash-recorded local AARs (plan:39-42).
- Fix: qualify the locally built, hash-recorded bytes. Upload those same
  bytes after P7. Build CI as a parallel track, not a P5 gate.

**IP-21. Low. The source-kernel hash may not be reproducible.** (B7)

- `Makefile:8-11` runs `compileall` in its default timestamp mode before
  `componentize-py`. Whether the bundled `.pyc` files carry source mtimes
  is unverified.
- Fix: in P0, build twice from clean checkouts and compare. Use
  `--invalidation-mode unchecked-hash` or `SOURCE_DATE_EPOCH`, or treat
  the kernel bytes as a recorded input.

**IP-22. Low. The absence checks can pass vacuously.** (C9)

- A wrong path or grep pattern passes silently. R8 renaming would break
  class-name greps once it is on.
- Fix: give each check a positive control:
  - main's maps show `libgojni`;
  - the precompiler build shows `cranelift-codegen`;
  - the `baoPoc` APK shows the receiver;
  - a build with a DSN produces traffic that the capture detects.

**IP-23. Low. Name the Application and variant mechanics.** (B12)

- Isolation is feasible:
  - Code already runs before `super.onCreate()`
    (`AnytypeApplication.kt:52-55`).
  - Sentry's provider auto-init is off in the manifest.
  - Go is reached only through `RealMobileBridge`.
- Name the mechanism:
  - check `getProcessName()` before `super`;
  - inject only in main (Hilt `@CustomInject`);
  - skip the theme and icon work in the worker;
  - make the worker a plain service, not `@AndroidEntryPoint`.
- Put the "unavailable binding" outside `src/main`, or `baoPoc` gets a
  duplicate Hilt binding.
- `mavenLocal()` resolves first (`settings.gradle.kts:4,14`) and there is
  no `verification-metadata.xml`. A "pinned checksum" therefore needs
  Gradle dependency verification.

**IP-24. Low. The 16 KB emulator run can pass in compat mode.** (C12)

- Fix:
  - Assert `getconf PAGE_SIZE` = 16384, with compat mode off.
  - Add static `zipalign -c -P 16` and `llvm-objdump -p` (LOAD align
    2**14) checks.
  - Keep the emulator off the critical path after the spike: it proves
    loading, not FGS or Doze behaviour.

**IP-25. Low. Some P1 work is planned to be redone.** (A12)

- P1's smoke owner, its unbind handling and the first seam actions are
  rebuilt in P4 and P5. The plan says "Finish the fault tooling begun in
  P1/P4" (plan:294).
- The 16 KB, IPv6 and release-APK checks belong in the final T1 run, not
  in the first native milestone.
- With IP-1's spike, these move out of the first milestone.

## Proposed re-sequencing

| Step | Content | Decision |
|---|---|---|
| S−1, no code | Connect the phone; inventory (`getprop`, RAM, OS, page size, GMS, inactivity reboot, Any apps); reserve phone and Mac; toolchain; check out `any` v0.3.5; measure provider latency and spend on desktop with the throwaway account | User accepts Stage 0 with K1–K6 |
| S0 spike | IP-1's scope: artifact/profile/feature split, JNI shim, plain `:bao` service, cell/trap/fuel/epoch, 10 real turns with a fixed program, memory samples | GO / PIVOT / STOP |
| S0b, background, optional | The spike agent plus a trivial foreground service and wake lock, locked on the charger for ≥72 h, answering probes from the Mac; one forced-Doze arm | Early verdict on OEM kills, reboots, thermals, restart timing |
| Stage-1 acceptance | ADR with the R3 amendments, IP-2, IP-6, IP-18 wording; reciprocal pointers | User |
| Parallel tracks | P2 ∥ P3-egress ∥ P3-election ∥ P4 (snapshot first, fake worker) ∥ UI (fake status) ∥ Kotlin decode ∥ fixture + harness + network fixture ∥ CI | Per-track gates |
| P6′ | All pre-soak arms including T10 and abrupt-loss T11; 12–24 h smoke soak; freeze | User |
| P7′ | T7 with capped attempts; prune last; publish the same bytes | Report or stop |

These are rough calendar estimates (inferred). After the freeze, with zero
failures, P6′ plus the soak plus T11 takes about 7–9 days and 3 user gates.
The plan has no engineering estimate for the build work: `serve.rs` and
`broker.rs` are about 5.1k lines each.

## Checked and correct

- The plan does not presume acceptance (plan:19-21). It bans
  shared-process and on-device-compilation fallbacks (plan:133-134,
  matching ADR:682-683).
- `routes.rs` is correctly called a classifier, not an allowlist
  (`runtime/src/routes.rs:17-24`, `:33-39`).
- MCP is a guest program using broker HTTP (`repos/_agent/programs/mcp@v1/program.py`).
  The runtime holds only its key prefix.
- `EMBEDDED_KERNEL` (`runner.rs:181-182`), `Cage::new` with
  `Cache::from_file` (`:201-215`) and the 10 ms ticker (`:109`) exist
  where the plan implies. The control listener is unconditional
  (`serve.rs:2093`, `:3818-3863`).
- Cron and once runs execute inline on the ticker (`serve.rs:3406`), so
  P2.4's move is needed. The guest `sleep` is uninterruptible
  (`broker.rs:1152-1158`), so P2.2's interruptible waits are needed.
- Kotlin:
  - `AnyHealth.bootstrapping = false` (`Models.kt:31`).
  - `BaoStatusSource` uses `WhileSubscribed`, and the bus has no replay
    (`BaoStatusSource.kt:51`, `:118-124`).
  - `FakeMobileBridge` is in `AnyRuntimeImplTest.kt:61`.
  - `stopUntrackedServer` is at `AnyRuntimeImpl.kt:222`, and the
    `StartSuperseded` retry at `:104-151`.
  - `recoverFromError` is at `AppCoordinator.kt:571`.
- `benchmark` is `initWith(debug)`, keeps `.dev`, and has only the
  `debug` fallback (`app/build.gradle.kts:140`, `:171-182`). The explicit
  fallbacks in P1 are warranted.
- The Hilt-order caveat is right: injection happens inside
  `super.onCreate()` (`AnytypeApplication.kt:56`).
- Android AAR packaging is correctly called new work. Release CI builds
  desktop targets only (`release.yml:50-52`).
- Evidence and mnemonics stay outside the repos (`environments.md`).
  Trace credential redaction is structural (`broker.rs:3139-3142`); still
  scan exported evidence for the key value.
- Sender-clock latency needs no cross-device clock sync.
- "Preserve these bytes; publication must not rebuild them" and the
  refusal of unexplained local binaries are the right freeze rules.

## Withdrawn or corrected during this review

- **Phone model.** The coordinator briefly concluded the phone was a
  Nothing Phone (1), because the devpass skill labels it "A063" and public
  listings tie A063 to the Phone (1). The user confirmed the phone is a
  **Phone (2a)**. That claim is withdrawn and appears nowhere above. P0.4
  already reads the model from device properties.
- **Recovery alarm.** Lens A said the plan makes mandatory a backstop
  alarm that the ADR leaves optional. ADR:266-268 makes the single inexact
  backstop part of the design; "may" there describes what it does. Dropped.
- **Inactivity reboot.** Re-rated from High to Medium (IP-15), following
  the round-1 research.

## Verification log

The coordinator re-read these sources:

- `runtime/src/election.rs:130-180` (`decide` returns `Claim`;
  `reconcile` calls `activate_device`) and `runtime/src/serve.rs:3583-3630`
  (10 s loop, `takeover` on flip). This confirms IP-2. `grep reconcile` in
  ADR-034 finds no fence.
- `any-kotlin/app/google-services.json` (six clients, no `.baoPoc`),
  `app/build.gradle.kts:7, 140, 171-182`. This confirms IP-7.
- ADR:712-730 (§7 messages). This confirms IP-6.
- `git -C any describe --tags` gives `v0.2.3-nightly.20260925.1`;
  `libs.versions.toml:2` gives `v0.3.5`. This confirms IP-14.
- `any-kotlin/app/.../AnytypeApplication.kt:50-80` (no Timber tree
  without DEBUG; Sentry gated). There is no `profileable` in
  `app/src/`. This confirms IP-8.
- `runtime/Cargo.toml` (Wasmtime defaults on, `tiny_http`
  unconditional, `strip = true`, `default = []`) and `Makefile:6-12`.
  This confirms IP-12 and IP-21.
- `runtime/src/serve.rs:1786-1836`, `:2092-2099`. This confirms IP-10.
- `any-kotlin/docs/verticals/bao-status.md:9` (3 s / 20 s / 60 s, "Claim
  again", name seeding). This confirms IP-11.
- `any/internal/config/local_discovery.go:13-18` and
  `any/internal/api/sync_status.go:12-26`. This confirms IP-3's LAN-peer
  point. The checkout is the older one; see IP-14.
- There is one androidTest in any-kotlin. `runtime/src/testutil.rs:17,
  126`.
- `runtime/src/lib.rs:74-75`, `serve.rs:1781` (lib `start`). ADR:200-230
  (lanes, 896 MiB worker ceiling). `bin/kernel.wasm` is 25,002,714 bytes.
  This confirms IP-1.
- ADR:660-686 (test order, immediate-stop list), `environments.md:68-76`,
  `.github/workflows/release.yml:44-56`, `settings.gradle.kts:4,14`.
- `any-kotlin/.claude/skills/devpass/SKILL.md:3`, `checklist-rules.md:6`,
  `devpass.sh:37`. These confirm the shared phone for IP-9. The model
  label is not evidence of the model; see above.
- ADR:466-520, 534-550 and 170-186 for the Round-3 table.

Not verified, and labelled where used:

- the `am instrument` force-stop behaviour;
- the google-services failure message (the plugin was not run);
- the Wasmtime 49.0.3 deserialization checks in IP-13;
- `.pyc` mtimes in the kernel;
- the run-success odds and the calendar estimates.

---

## Round 2 (revised plan, 2026-11-03)

Scope: the revised [plan](android-host-implementation-plan.md) (463
lines: S−1, S0, Stage-1 tracks B0/B1/R/E/L/H/U/F/C, I/Q, V), ADR-034 as
revised (1007 lines, with the new §0 and its K1–K6), the updated
`environments.md`, and the author's
[response](plans/android-host/2026-11-03-review-response.md).
`plan:NN` and `ADR:NN` below refer to these revised files.

Method: three fresh reviewers, each limited to new content:

- A covered Stage 0 (S−1, S0 and ADR §0).
- B covered the Stage-1 contract against the code (ADR §§1–5 and §7;
  tracks B, R, E, L, H, U).
- C covered the qualification campaign (track F, the executor table, I/Q,
  V, the rerun rules and ADR §6).

They used the round-1 findings only to avoid reporting them again. The
coordinator checked the round-1 dispositions and re-read the source behind
every high and medium-high finding. Server-side citations come from the
local `any` checkout, `v0.2.3-nightly.20260925.1`, which is older than the
`v0.3.5` pin (IP-14), so they are provisional.

| Lens | High | Medium-high | Medium | Low |
|---|---|---|---|---|
| A. Stage 0 (as reported) | 2 | 3 | 4 | 1 |
| B. Stage-1 contract (as reported) | 0 | 2 | 5 | 3 |
| C. Qualification campaign (as reported) | 2 | 2 | 4 | 2 |
| **Merged, coordinator-rated** | **3** | **8** | **13** | **6** |

### Verdict

**The structure is now right, and every round-1 finding has a specified
remedy.** Stage 0 comes first and has numeric K gates. Stage 1 is a
separate decision, built as dependency tracks with a harness, an
evaluator, pre-soak T10 and T11 runs and capped attempts.

The remaining problems sit in two places with different deadlines:

- **Before Stage-0 acceptance** (IP2-1 to IP2-10):
  - The spike can't start as written. Its installation slot is the devpass
    install (IP2-1).
  - The way the worker gets its key, its config and its evidence on and off
    a non-debuggable app is unspecified (IP2-2).
  - Several K gates can fail because of how memory is counted, or can
    pass without testing what Stage 1 needs: one `llm.chat` call with no
    limiter rather than a chat turn, and trap arms with no defined inputs
    (IP2-3 to IP2-7).

  These are edits to ADR §0 and the S0 text, not redesign.
- **Before Stage-1 acceptance** (IP2-11 to IP2-30). None of these block
  Stage 0:
  - The network fixture misses Any's global P2P layer, so cutting the sync
    nodes doesn't partition the phone (IP2-11).
  - The new election states have crash and timeout edges (IP2-12, IP2-13,
    IP2-17, IP2-19).
  - The attempt cap draws host failures and external noise from one budget
    (IP2-14).
  - The evaluator has no schema (IP2-15).
  - The telemetry capture can't attribute traffic to the app (IP2-16).

One decision belongs to the user: whether a short supervised screen-off
probe should be an input to Stage-1 acceptance (IP2-9). The coordinator
recommends it.

### Round-1 dispositions

The author's response maps every finding. The coordinator checked each one
against the revised text.

| Finding | Status | Where |
|---|---|---|
| IP-1 Stage-0 spike, K criteria | Resolved in spec; executability gaps → IP2-1..IP2-7 | ADR:34-90, plan:71-120 |
| IP-2 periodic reconcile fence | Resolved in spec; new edges → IP2-12, IP2-13, IP2-17 | ADR:624-639, plan:239-246 |
| IP-3 harness, network fixture, probe APK, evaluator | Resolved in spec; the fixture misses global P2P → IP2-11; evaluator → IP2-15 | plan:297-347, `environments.md` |
| IP-4 T10/T11 before soak, smoke, caps, change matrix | Resolved in spec; cap rule → IP2-14; matrix gaps → IP2-23 | plan:383-439 |
| IP-5 P0 gate inverted | Resolved: inventory precedes Stage-0 acceptance | plan:25-69, ADR:36-41 |
| IP-6 §7 messages, snapshot first | Resolved in spec; expiry and renewal gaps → IP2-21 | ADR:922-952, plan:160-168 |
| IP-7 push-free baoPoc | Resolved for Stage 1 (ADR:108-114, plan:172-175); Stage 0 sidesteps it with `.dev` → IP2-1; Go-side push → IP2-28 | |
| IP-8 diagnostics before freeze | Resolved in spec | ADR:252-261, plan:178-182 |
| IP-9 phone/Mac reservation, live view | Resolved in spec; the `.dev` slot conflict remains → IP2-1 | plan:34-39, 326-330 |
| IP-10 preparation writes | Resolved in spec; does not match `serve::start` → IP2-18 | ADR §5, plan:233-237 |
| IP-11 Devices claim flow | Resolved | ADR:607-611, plan:286-290 |
| IP-12 Cargo restructure | Resolved | plan:83-89 |
| IP-13 engine profile inputs | Resolved; stack size still missing → IP2-7 | plan:97-101 |
| IP-14 `any` source vs pin | Specified, **not executed**: the sibling checkout is still `v0.2.3-nightly`. `v0.3.5` is a public tag (Lens A, `git ls-remote`). | plan:49-57 |
| IP-15 inactivity reboot | Resolved as a Stage-1 prerequisite | plan:40-44, ADR:863 |
| IP-16 fault seam | Resolved in spec; operations gaps → IP2-26 | ADR §6 (~:825-840) |
| IP-17 false dependencies | Resolved; new graph errors → IP2-20 | plan:131-156 |
| IP-18 ADR requirements without a plan step | Resolved | plan:385-386, ADR:239, 246 |
| IP-19 sample sizes, clocks, owner | Resolved | plan:349-381 |
| IP-20 CI not on the critical path | Resolved | plan:184, 345-347 |
| IP-21 kernel reproducibility | Resolved | plan:99-101 |
| IP-22 positive controls | Resolved | plan:376-381 |
| IP-23 variant mechanics | Resolved | plan:170-176, 185-186 |
| IP-24 16 KB checks | Resolved | plan:188-191 |
| IP-25 throwaway P1 work | Resolved | plan:76-82 |
| R3-1 staged acceptance | Resolved | ADR §0 |
| R3-2 lost-claim revival | Resolved for an observed takeover; a takeover during phone downtime → IP2-17 | ADR:631-639 |
| R3-3 "fresh" defined | Resolved | ADR:661-663 |
| R3-4 fresh-process UX | Resolved | ADR:522-527 |
| R3-5 LAN endpoints unsupported | Resolved | ADR:287, plan:228 |
| R3-6 route table separate from `routes.rs` | Resolved, in both the ADR and the plan | ADR:696-706, plan:219-220 |

The author's corrections to suggested methods are accepted:

- Context switches are not one-to-one wakeups.
- A nearby canary failure alone doesn't attribute a probe miss.
- DNS/SNI capture has blind spots.
- A separate key may still share a quota.
- The review's odds and calendar estimates are not facts.

### Before Stage-0 acceptance

**IP2-1. High. The spike's `.dev` installation slot is the devpass
install.** (A1)

- Plan text: "use the existing non-debuggable benchmark variant and its
  `.dev` ID on the reserved disposable installation" (plan:76-77). ADR:
  "Go uses a fresh throwaway account … no other Any server with real or
  shared staging data may run on the phone" (ADR:47-49). S−1: "Never
  overwrite/uninstall its `.dev` installation without the owner's agreed
  preservation/replacement procedure" (plan:37-38).
- Evidence:
  - `benchmark` is `initWith(debug)`, so it keeps `.dev` and installs over
    the dev app with its data (`any-kotlin/app/build.gradle.kts:140`,
    `:170-182`).
  - The phone has one `org.any.anykotlin.dev`. Devpass runs it on the
    shared staging account, and its rules say "Never sign the phone in or
    out. Never change its network" (`.claude/skills/devpass/SKILL.md:27`).
  - Account keys are Keystore-wrapped (`KeystoreAead.kt`), so copying
    files can't preserve the install. "Preservation" really means wipe,
    then re-login later.
- Failure: S0 waits on the owner, or the spike's first launch opens shared
  staging data, which ADR:47-49 forbids.
- Why `.dev` was probably chosen: `google-services.json` has no client for
  a new ID (IP-7). ADR:48-49 already turns push off for the spike, and that
  is most of the push-free work.
- Fix: in the spike checkout, give the spike its own ID suffix (ideally
  §1's `baoPoc`). Disable `process<Variant>GoogleServices`, remove the
  Firebase providers and the FCM service in the variant manifest, and
  no-op the token store (`DeviceTokenStoringServiceImpl.kt:42,61`). The
  alternatives are a second phone, or a wipe and re-login scheduled with
  the owner. Record the choice in S−1.

**IP2-2. High. The spike's inputs and evidence channels are unspecified.**
(A2)

- Plan text: "The fixture supplies program/config inputs; returned text and
  traces prove actual model calls" (ADR:56-58); "separate phone/canary
  keys" (plan:60-61).
- Evidence:
  - `llm@v1` needs `llm.tier.*` config and the credential ref
    `llm.key.<provider>` (`repos/_agent/programs/llm@v1/program.py:1139-1152`).
    Without a secret store the broker falls back to in-memory seeds
    (`runtime/src/broker.rs:1386-1401`).
  - Nothing says how the key reaches the worker. The synced secret store
    is shared with the desktop, so the phone can't have its own key that
    way.
  - Reusing the `anyrt run` composition writes to Go:
    - `AnyTraceStore::new` → `local_ensure` creates local collections
      (`tracestore.rs:440-456`);
    - `run_config_store` → `provision_agent_stores` provisions stores
      (`serve.rs:585-607`).
  - Evidence off the phone: benchmark plants no Timber tree
    (`AnytypeApplication.kt:71-73`). So the port log line the
    `any-server-probe` skill depends on never appears. The app isn't
    profileable, and with no `run-as` its private files can't be read.
- Failure: the spike is built, then stalls on key delivery or on getting
  traces and samples out. Or it quietly provisions stores in Go, which
  contradicts §0's no-provisioning scope.
- Fix: S0 names each channel.
  - **Key:** typed on the test screen, or pushed to app-external storage
    and deleted after it is read.
  - **Config:** pinned in-memory seeds, no config store.
  - **Traces:** in-memory or a file sink, with no Go local collections.
  - **Samples and traces out:** written to `getExternalFilesDir` for
    `adb pull`.
  - **Go port:** shown on the test screen.
  - **Real calls:** prove them with `meta.mocked=false` in the trace,
    reconciled against usage on the phone's key at the provider.

**IP2-3. Medium-high. K4 and K6 test one `llm.chat` call with no limiter,
not a chat turn.** (A3)

- Plan text: "does a real turn fit; does it finish promptly?" (plan:73);
  K4 "worker RSS ≤896 MiB" (ADR:81).
- Evidence:
  - A real reply runs `toolcaller@v1`, which builds the system prompt from
    skills, tool docs, memory, history and autorecall
    (`toolcaller@v1.py:1180-1232`, per Lens A).
  - `run_program` builds one `Store` with no `ResourceLimiter`
    (`runtime/src/runner.rs:292-300`; no limiter anywhere in `runner.rs`).
    So the 256/768 MiB lane caps never apply, and one small call can't
    approach 896 MiB.
- Failure: a GO, then T7 or T8 fails after the Stage-1 spend.
- Fix:
  - Run `toolcaller@v1` with tools off over a seeded synthetic chat.
  - Install a recording-only limiter, and report the guest-memory
    high-water mark against the 256 MiB chat lane.
  - Keep two idle stores instantiated during the turn.
  - Split each turn's latency into on-device and provider time, using
    trace durations.

**IP2-4. Medium-high. K3 may fail on how RSS is counted, and "fresh" and
"cold" are undefined.** (A4)

- Plan text: K3 "Worker baseline RSS ≤128 MiB after artifact load"; K1/K5
  "10 fresh worker processes" (ADR:72-80).
- Inferred:
  - An empty ART process's RSS already includes zygote-shared pages.
  - Deserializing an artifact embedded in the `.so` adds anonymous copies,
    and copy-on-write images built from bytes add more.
  - The shell can't kill an app pid or drop caches.
- Fix:
  - Measure a control: the `:bao` process before the native library
    loads. Gate on the delta.
  - Report RssAnon, RssFile, RssShmem and PSS from in-process
    `smaps_rollup`.
  - Define "fresh" as unbind → `Process.killProcess` → rebind, and
    "cold" as the first load after a reboot.
  - Define the start and end of "load".

**IP2-5. Medium-high. K2's trap and fuel arms have no defined inputs.**
(A5)

- Plan text: K2 "50 expected wasm traps, 50 fuel limits, 50 epoch
  interruptions" (ADR:79).
- Evidence:
  - Fuel is a constant 250B per run, about 20–25 s of desktop compute
    (`runner.rs:110-113`), set on every store (`:300`). Fifty fuel arms
    is roughly an hour of phone CPU.
  - A shorter `timeout_s` reports `timeout`, not fuel exhaustion.
  - The guest can't import `sys` or `ctypes` (Lens A), so no ready Python
    input causes a wasm trap.
  - Fuel and epoch traps likely surface through libcalls rather than
    signals (inferred). The ART/Wasmtime signal-handler interplay, which
    is K2's real risk, may then go untested while K2 passes.
- Fix:
  - Give each arm a defined input and an expected classification.
  - Add a per-run fuel parameter for the fuel arm.
  - Add a signal-based trap source, such as a second trusted precompiled
    trap component, and assert its trap code.

**IP2-6. Medium. K6's rules don't close.** (A7)

- With 10 samples, nearest-rank p95 is the maximum (ADR:84-86). The binding
  limit is therefore 120 s, and "max ≤180 s" never applies.
- `llm@v1` retries 429/5xx with waits of up to 60 s (per Lens A), which
  turns provider trouble into a latency failure.
- "Independently evidenced external failure" relies on canary tooling that
  arrives in Stage 1's track F.
- Nothing is defined for the case where both batches are invalidated.
- Fix:
  - State the effective rule: max ≤120 s.
  - Define S0 attribution as the trace's retry and status record, plus a
    desktop canary call in the same minute.
  - Two invalid batches give INCONCLUSIVE, and the user decides.

**IP2-7. Medium. The native stack size is not pinned.** (A6)

- Wasm runs on the calling thread's stack. Nothing in `runtime/src` sets
  `max_wasm_stack` or a thread stack size.
- Android Java threads get about 1 MiB (inferred), against 2–8 MiB on the
  desktop.
- Failure: a deep recursion overflows the native stack. That is an
  "unexplained signal fault", so K2 stops the experiment.
- Fix:
  - Put `max_wasm_stack` in `EngineProfileV1`.
  - Run each run on a Rust thread with an explicit stack size, joined
    from the JNI caller.
  - Add a deep-recursion case to K2.

**IP2-8. Medium. S−1 front-loads Stage-1 checks and misses Stage-0
inputs.** (A8)

- S−1 verifies registry, sync-status and prune semantics (plan:53-56), and
  prepares the overlay fixture. S0 uses neither.
- "Refresh the vendored API input" changes a tracked file and needs a
  build, so it is not read-only.
- S−1 is missing the inputs S0 needs: the slot decision (IP2-1), the key
  and evidence channels (IP2-2), and how to reach Go on the phone.
- Fix: split S−1.
  - S−1a holds the Stage-0 inputs and gates Stage-0 acceptance.
  - S−1b holds the Stage-1 inputs and runs in parallel with S0.

**IP2-9. Medium. A GO leaves the most likely project-ender untested.**
(A9; a user decision)

- Plan text: "The first spike omits the optional 72-hour background
  experiment" (plan:118-120).
- Every expensive Stage-1 track and the soak assume that a foreground
  service and its worker survive screen-off on Nothing OS. S0 never leaves
  the foreground.
- Fix: make a short supervised screen-off probe an input to Stage-1
  acceptance: a minimal foreground service plus the spike worker, one
  fixture turn every 15 min, 8–12 h, phone on the charger. Or state
  explicitly that Stage 1 is accepted without it. The coordinator
  recommends the probe. It is about a day of work and answers the question
  the soak answers last.

**IP2-10. Low. Telemetry in main and source pinning during the spike.**
(A10)

- The plan says to skip Sentry "in the worker" (plan:79). The ADR turns it
  off for the whole spike (ADR:48-49).
- Benchmark reports to `SentryEnvironment.PROD` when a DSN is present. The
  DSN depends on the build machine's `apikeys.properties`.
- Fix:
  - Set `SENTRY_ENABLED=false` for the spike build.
  - Make the plan match the ADR.
  - Assert the resolved source hashes of the fixture program and `llm@v1`
    on every run.

### Before Stage-1 acceptance

**IP2-11. High. The network fixture misses Any's global P2P layer, so a
sync-node outage isn't a partition.** (C1)

- Plan text: "separately controllable phone/desktop sync and provider
  paths, … LAN discovery state" (plan:316-319); `environments.md` asks
  only to check that no LocalPeer path bypasses the outage.
- Evidence (provisional, local `any` checkout):
  - `internal/config/config.go:132-137`: "spaces shared with a discovered
    peer sync directly, including while sync nodes are unreachable".
  - On the production network, iroh relays (`relay-{fr,de}-1.anytype.io`)
    and pkarr account discovery default on
    (`internal/config/p2p_global_prod.go:24-36`).
  - The embedded server defaults global P2P on when no nodeconf is given
    (`internal/embedded/embedded.go:149-159`).
  - The Android entry point passes no override (`mobile/android/mobile.go:59-70`).
  - `docs/30-global-p2p.md:134-136`: "A device with no node stream asks
    its connected global peers for pushes and becomes their sole path."
  - The fixture is on prod (`environments.md`).
- Failure: the router drops the sync nodes. The phone and the Mac share an
  account and keep converging through a relay or a hole-punched path. The
  T10 takeover is seen live, and reconnect is never exercised.
- Fix: define the sync-path deny set. It covers:
  - the nodeconf peers;
  - the iroh relays and the pkarr relays;
  - direct phone↔Mac UDP over v4 and v6, including link-local;
  - mDNS.

  In the dry run, assert that `networkPeers`, `localPeers` and
  `globalPeers` are all 0 on the phone, and that the Mac lists no phone
  global peer. Alternatively, name "node loss with P2P up" as its own arm.
  Re-check the default on `v0.3.5` in S−1.

**IP2-12. Medium-high. A release that is owed is not durable, and nothing
retries it once the host is off.** (B1)

- Plan and ADR text:
  - "Off drains/terminates Rust, then main writes `PUT …{"bao":null}`"
    (ADR:641-643).
  - "failed release stays ReleasePending" (ADR:635).
  - Only an activation failure gets "persistent pending-release state"
    (ADR:617-619).
- Failure:
  - The user taps Off, then swipes the app away during the drain (up to
    15 s). Main dies before the PUT. The host stays off and starts
    nothing, and the registry winner is still the inert phone. The
    desktop stands by forever, since a DesktopDefault device defers to a
    live foreign winner (`runtime/src/election.rs:139-149`).
  - A main death during Relinquishing is similar.
  - ReleasePending has no retry cadence, no exit, and no rule for a
    replaced account.
- Fix:
  - Persist `ReleaseOwed{account, peer}` before any drain: Off,
    Relinquishing, activation rollback and destructive transitions.
  - Main retries the release whenever Go runs for that account, with the
    host enabled or not.
  - State the invariant: any non-Active state whose own row carries
    `apps.bao` is ReleaseOwed.
  - Define the exits: success, 409, or account change (abandon, and show
    Needs attention).

**IP2-13. Medium-high. Activation is undefined between the claim write and
the confirming beat, and that beat can only follow Open.** (B2)

- ADR text:
  - "Only confirmed local winner plus a fresh active responder beat
    completes the operation … after 30 s show not confirmed"
    (ADR:613-617).
  - Main "confirms the winner, then sends setAdmission(Open…)"
    (ADR:935-936).
  - The worker "emits normal responder beats only while active"
    (ADR:947-948).
- Failure:
  - (a) The claim returns 204, then sync drops. The UI says "not
    confirmed". After reconnect the desktop sees the phone's higher seq
    and stands down, while the phone never opens, so nobody answers. If
    the phone rolls back instead, the offered retry is blocked by "No
    re-registration before that state is reconciled".
  - (b) Open is sent and chat is answered, but a beat publish fails. Today
    that is only logged (`serve.rs:1375-1381`, per Lens B). At 30 s main
    treats it as failure and rolls back after replies have gone out.
- Fix:
  - Use explicit sub-states ClaimWritten → Open → BeatSeen, each with
    its own deadline.
  - ClaimWritten past its deadline → ReleaseOwed (IP2-12).
  - After Open, a missing beat is a health fault, not a rollback.

**IP2-14. Medium-high. The attempt cap draws host failures and external
noise from one budget.** (C2)

- Plan text: "two smoke attempts and two long-soak attempts in total,
  including external invalidations. Rebuilds do not reset the counter"
  (plan:417-419).
- Failure: long run 1 is invalidated by a provider incident. Long run 2
  finds a host bug. Its fix needs a new smoke and a new long run, so the
  cap ends the campaign without a T7 verdict.
- Provider timeouts with no response can't meet "actual request outcome
  plus independent canary" (plan:338-340), so they stay unresolved. In
  effect they count against the host.
- "Alerts on the first miss" (plan:327-328) has no staleness threshold
  for beats that travel over sync.
- Fix:
  - Keep two ledgers. Host-attributable or unresolved failures get two
    candidates. Proven external or fixture invalidations get their own
    count or calendar budget, with "two in a row → repair the fixture".
  - Allow one uncounted pipeline rehearsal.
  - Set numeric beat-staleness limits.
  - Record GMS/Play app updates that cause no reboot instead of
    invalidating on them (`baoPoc` is push-free).

**IP2-15. Medium-high. The "deterministic evaluator" has no schema, and
several rows need human judgement.** (C3)

- No evidence schema or arm manifest exists in the ADR or the plan.
- These clauses can't be computed mechanically:
  - T3 "no crash dialog or false success";
  - T4 "visibly pending/failed";
  - T5 "Record any Recents limitation", and "never auto-resurrects" with
    no observation window;
  - T9 "unexplained evidence gap";
  - T10 "Distinguish sync loss…";
  - T11 "Document … exposure".
- The golden set is negative only (plan:333-335), so an evaluator that
  never passes anything passes its own tests.
- Fix:
  - Check in a JSON schema and a predicate table that maps each ADR clause
    to its evidence field and computation, tagged MECH or HUMAN.
  - Human rulings become signed inputs to the evaluator.
  - Add golden PASS and threshold-edge bundles: p95 exactly 120 s at
    n=320, and a heartbeat exactly 21 min late.
  - Validate against a real dry-run bundle before the freeze.

**IP2-16. Medium-high. A router capture can't attribute flows to the
app's UID, so T11 will likely stay INCOMPLETE.** (C4)

- `environments.md`: "Unknown destinations or missing coverage make T11
  incomplete."
- GMS and Nothing OS traffic from other UIDs always appears.
- Failure: T11 never passes, or a person allowlists OS traffic with
  nothing to attribute it.
- Fix: attribute per UID during the telemetry window. Options:
  - a VpnService capture using `getConnectionOwnerUid`;
  - per-UID `dumpsys netstats` buckets;
  - comparing captures with the host off and on.

  Keep this separate from the T10 router rules.

**IP2-17. Medium. Resume revives a claim superseded while the phone was
down.** (B3)

- ADR text: "A current enabled session may resume its previously confirmed
  claim after a validated restart" (ADR:620-621). Resume intent is revoked
  only "on observing another winner" (ADR:631).
- Failure:
  1. The phone is active at seq 3.
  2. It reboots and waits for unlock.
  3. The user claims on the desktop (seq 4), then removes bao there.
  4. The phone unlocks, sees `active.bao` = self, and resumes silently.

  This is R3-2 again, without a partition. T4 tests only an observed
  takeover.
- Fix:
  - Persist the confirmed `(seq, at)` with the resume intent.
  - Resume only if the phone's own claim still matches it and no visible
    row has a higher bao seq. Rows keep their claims after the app is
    removed (`any/docs/23-devices.md`). Otherwise revoke.
  - Add a T4 arm with the takeover during phone downtime.

**IP2-18. Medium. The preparation and activation write sets don't match
what `serve::start` does.** (B4)

- ADR text: preparation's "only server writes are a name-only self upsert,
  joining … overlays …, pending … release, and the host-status event".
- What `start` also does before the election:
  - writes declared secret descriptors into synced rows, including new
    "missing" rows;
  - creates the trace local collections (`tracestore.rs:440-456`). These
    are device-local, so the desktop fixture can't provision them for the
    phone;
  - seeds the chat-watch record if it is missing.
- Takeover's stamping (`serve.rs:3683`) is called only from
  `election_thread` (`:3621`), which Android doesn't run.
- Dispatch is keyed on owning the chat-watch record (`answers_chat`,
  `serve.rs:3740-3744`).
- Failure: the phone is permanently "Needs setup" (no track owns
  phone-side setup), or it breaks the whitelist test. Or, after Open,
  the desktop still owns the chat-watch record and the phone stays silent
  while admitted.
- Fix: define three write sets in ADR §5:
  - preparation, which also creates the local collections;
  - Open, which runs takeover and stamps descriptors;
  - explicit setup.

  Relinquishing runs the stand-down, and admission drives
  `verdict.active`.

**IP2-19. Medium. The prune probe upserts the name before bootstrap.** (B5)

- ADR text: §3 step 2's "harmless self-name upsert" (ADR:390-394) comes
  before step 3's `bootstrapping == false` check (ADR:395-401). The steps
  are rechecked "on every … reconnect" (ADR:409-410).
- Evidence: the server waits for bootstrap before its own upsert, because
  "seeding the hostname then would clobber a user-set name account-wide
  once the synced row merges" (`any/internal/server/engine.go:359-371`).
- Failure: before bootstrap, the row reads blank. The phone writes a name
  and overwrites the user's synced name. Every reconnect also adds a
  registry change.
- Fix:
  - Run step 3 before step 2.
  - In non-Active states, use `{"apps":{"bao":null}}` as the 409 probe. It
    is idempotent and already whitelisted.
  - Seed the name once, after bootstrap, and only when it is blank or
    `localhost`.

**IP2-20. Medium. The dependency graph and ownership have errors.** (B6)

- R ("own every … presence/election loop", plan:195-197) and L ("Separate
  preparation from all existing serve::start effects", plan:233) rewrite
  the same function (`serve.rs:1781-2140`). L needs R's managed-start
  handle first.
- H's service lives in the host module, behind the variant and the process
  gate that B1 creates. H and B1 both edit the Application's code that
  runs before `super.onCreate`.
- U calls the host API (B1) and depends on H's service-owned subscription.
- L's state machine and its fake-server tests are Kotlin (ADR:652, :932),
  but the plan assigns "election" to anybao (plan:449).
- Fix:
  - Add R0 (the managed-start skeleton) → L-rust.
  - Add a B1 skeleton (modules, variant, process gate) → H and U.
  - Split L into L-kotlin and L-rust.

**IP2-21. Medium. The §7 protocol is incomplete for its own expiry rule.**
(B7)

- ADR text: an open grant "expires within 30 s without renewal … closing
  admission is immediate" (ADR:936-939).
- Gaps:
  - **Renewal evidence is undefined.** If it is activation-grade (`synced`
    plus peers), a `syncing` index object closes admission. The phone's
    own reconnect write causes exactly that. This contradicts §5, where
    only sustained 30 s loss closes admission.
  - **Callbacks can't be ordered.** They carry no worker sequence or
    applied policy sequence, so unsolicited status can't be ordered
    against acknowledgements.
  - **A Close isn't immediate.** It waits in the FIFO behind status and
    renewals.
  - **An `activate` that is never acknowledged isn't counted.**
  - **No combination rule.** Nothing says how main's grant combines with
    the worker's own RSS closure.
- Fix:
  - Renew on loss-grade evidence from each sample.
  - Add `stateSeq` and `appliedPolicySeq` to the callbacks.
  - Keep a single latest-wins admission slot.
  - Count an unacknowledged command as a health failure.
  - Make effective admission main's grant AND the worker's resource gate.

**IP2-22. Medium. Forced-Doze restoration can't be "pre-armed" once ADB is
unplugged.** (C5)

- `environments.md`: "Pre-arm bounded restoration before unplugging or
  disconnecting ADB".
- `deviceidle unforce` and `battery reset` need the shell UID; the app
  lacks DEVICE_POWER. A detached shell timer may be killed or may stop
  in suspend. These are from memory and unverified.
- "3 each … Doze arm" (plan:371) is ambiguous between three arms and the
  ADR's three probes.
- Fix:
  - Script the unplug and replug with a per-port power-switching USB hub
    (uhubctl-class), so the restore runs on schedule over ADB.
  - Or make the operator's replug deadline explicit, and invalidate the
    arm if it slips.
  - Define the counts.

**IP2-23. Medium. The change-impact table has missing and undecidable
rows.** (C6)

- Missing:
  - an evaluator bug found after a run (re-score the preserved bundles or
    rerun?);
  - a desktop Any or desktop anyrt binary change;
  - the fixture side of an AAR bump;
  - drift in the sync nodes' IPs or nodeconf, which leaves router rules
    stale;
  - a key rotation on the same tier;
  - phone setting drift (battery exemption, Private DNS);
  - a macOS update or restart.
- Undecidable: "if timing, collection or the verdict can change"
  (plan:437) names no decider.
- Fix: add the rows. The user approves every "unaffected" claim in the
  gate report; unsure counts as affected.

**IP2-24. Medium. T8's host profiler conflicts with the wall-charger
rule.** (C7)

- T8 needs "self samplers + host profiler: continuous during smoke/soak"
  (plan:369). T7 requires a wall charger, and wireless ADB is distrusted.
- Fix: use a charger-backed powered hub that keeps wired ADB, and log the
  power source. Or limit host profiling to pre-soak windows.

**IP2-25. Medium. A reviewer can't re-derive a verdict from what goes into
git.** (C8)

- Plan text: "Keep raw bundles/keys outside git" (plan:447). Reports carry
  only evidence paths on one Mac.
- Fix: commit the following per gate:
  - the evaluator verdict JSON;
  - the arm manifest;
  - the attribution rulings;
  - the restore receipts;
  - a sha256 manifest of the bundle;
  - the exact evaluator command.

  Keep a second copy of the raw bundles until handoff.

### Low

**IP2-26. Low–medium. The harness and seam operations are unspecified.**
(C9)

- Seam sessions are armed from a test screen and last at most 30 min.
  T3 and T5 need 100+ cycles, and no channel gets the nonce to the
  harness.
- T2's "≥10 cold" headless starts have no shell trigger for a
  non-exported service of a non-debuggable app.
- T5 doesn't say whether it tests the production breaker or the test
  breaker.
- DUMP may be grantable to other apps through `pm grant` (from memory),
  so inventory which apps hold it.
- Lens C estimates 11–15 operator-present hours per qualification pass,
  over 15+ sessions, repeated after every native change. Treat that as a
  budgeting input, not a fact.

**IP2-27. Low–medium. Some wording still implies Rust has a claim path.**
(B8)

- The policy is named `PrepareThenExplicitClaim` (ADR:579), and the text
  mentions a "management claim path" (ADR:904). Both conflict with "Main
  alone performs activation writes" (ADR:627).
- Fix: rename the policy (for example `ObserveOnly`). State that Rust makes
  no `/v1/devices` writes, and assert that in T4.

**IP2-28. Low–medium. "Push-free" covers only Firebase.** (B9)

- On prod with an empty nodeconf, the embedded Go server pairs with the
  production push node and notifies on chat sends (`any/docs/20-push.md`,
  provisional).
- Logout still calls `DeletePushToken`.
- Fix: accept and list the Go-side push traffic for T11, or request an
  upstream push-off option. Name the Android mechanism in B1:
  `tools:node="remove"` in `src/baoPoc`, and the real push binding moved
  out of `src/main`.

**IP2-29. Low. The snapshot puts `accountId` and `selfPeerId` in core
`AnyRuntime`.** (B10)

- `AnyRuntime` knows only `baseUrl` (`core/.../AnyRuntime.kt:15-16`). Login
  happens over HTTP within a generation, outside its mutex.
- Fix: `AnyRuntime` publishes `(nonce, generation, baseUrl, gateOpen)`. The
  supervisor binds the account and peer into the Lease after the §3
  checks.

**IP2-30. Low. The smoke soak's scope is narrow.** (C10)

- It has no +1 h observation for delayed duplicates.
- It can't catch the inactivity reboot, the trace sweep or quota
  exhaustion.
- Fix: add the observation hour. State that the smoke covers the pipeline
  and early regressions only.

### Checked and correct (round 2)

- **Choice of seam.** `run_program` is the right Stage-0 seam.
  - `anyrt run` already runs a provider-backed program without serve
    (`runtime/src/main.rs:299-556`).
  - `serve::start` writes before it returns.
  - The spike needs a deserialize-based Cage that starts the epoch ticker,
    or the deadline never fires (`runner.rs:221-228`, `:307`).
- **The fixture broker guard is small.**
  - It has one choke point, `sys_http` (`broker.rs:1464`), and one ureq
    builder.
  - It must force zero redirects, because unauthenticated requests follow
    redirects by default (`broker.rs:1575-1578`).
  - It isn't the Stage-1 egress layer.
- **Skipping the main graph in the worker** is about 10 lines before
  `super.onCreate()`. Library ContentProviders run only in main. The only
  `@AndroidEntryPoint` classes are MainActivity, MediaActivity and the FCM
  service. Nothing uses WorkManager or androidx.startup.
- **Ownership is consistent across ADR §5, §7, B0 and L.**
  - Main runs the monitor, makes the claim and release writes, and decides
    sync-loss closure.
  - It also publishes `bao.host.status.v1`.
  - The worker publishes `bao.status` only while active.
  - Not starting `election_thread` on Android is correct: both `boot` and
    the periodic `reconcile` claim (`election.rs:139-180`,
    `serve.rs:3583-3627`).
- **The executor table is consistent.** The 12 h smoke gives 48 probes and
  24 heartbeats, and 80 h gives 320 and 160. The enabled standing jobs all
  fire within 12 h.
- **Named tests exist.** Every test the plan names exists. `FakeMobileBridge`
  is a private class inside `AnyRuntimeImplTest`, not a shared fixture.
- **Network addressing.** The sync nodes and relays are distinct OVH IPv4
  hosts with no CDN in front (Lens C, `dig`, 2026-11-03), so IP rules can
  separate them from providers.
- **Fault seam.** A DUMP-protected explicit receiver can be driven by
  `adb shell am broadcast` on a non-debuggable build.
- **Stale doc.** `any-kotlin/CLAUDE.md` and the frame-profiling skill say
  benchmark uses `.dev2`. The build file says `.dev`, so those docs are
  stale.

### Verification log (round 2)

The coordinator re-read these sources:

- `any-kotlin/app/build.gradle.kts:170-182` (benchmark `initWith(debug)`)
  and `.claude/skills/devpass/SKILL.md:20-30` (never sign the phone in or
  out). This confirms IP2-1.
- `runtime/src/broker.rs:1386-1402` (secret-store or in-memory fallback),
  `serve.rs:585-608` (`run_config_store` provisions),
  `tracestore.rs:440-456` (`local_ensure`) and the `llm@v1` key refs at
  `program.py:1139-1152`. This confirms IP2-2.
- `runtime/src/runner.rs:106-114` (fuel constant, 10 ms epoch) and
  `:288-302` (`Store::new`, no limiter; `grep limiter|max_wasm_stack` in
  `runner.rs` finds nothing). This confirms IP2-3, IP2-5 and IP2-7.
- `any/internal/config/config.go:128-175`, `p2p_global_prod.go:20-40`,
  `internal/embedded/embedded.go:145-162`,
  `mobile/android/mobile.go:50-75`, `docs/30-global-p2p.md:130-138`, and
  the prod network in `environments.md`. This confirms IP2-11; it is
  provisional pending `v0.3.5`.
- ADR:600-666 (§5 activation, fence, Relinquishing, Off), ADR:922-952
  (§7) and ADR:384-411 (§3 steps). The only durable pending release is on
  activation failure. This confirms IP2-12, IP2-13, IP2-17, IP2-19 and
  IP2-21.
- `any/internal/server/engine.go:355-372` (`registerDevice` waits for
  BootstrapDone). This confirms IP2-19.
- `runtime/src/serve.rs:3621` (only `takeover` call), `:3683` and
  `:3740-3744` (`answers_chat`). This confirms IP2-18.
- `environments.md`: android-poc on prod, the campaign reservation and the
  selective network fixture sections.

Not verified, and labelled where used:

- Android Java thread stack size;
- how Wasmtime 49 surfaces fuel and epoch traps;
- whether `@CustomInject` exists in Hilt 2.59.2;
- shell-only `deviceidle`/`battery` and detached-timer behaviour;
- DUMP grantability through `pm grant`;
- `toolcaller@v1` prompt assembly, beyond Lens A's citation;
- the operator-hour estimate.

---

## Round 3 (second revision, 2026-11-03)

Scope: the plan as revised again (531 lines: S−1a, S−1b, S0 with S0a/S0b,
then Stage 1), ADR-034 (1232 lines; §0 is now 139 lines and adds K7), the
[round-2 response](plans/android-host/2026-11-03-round-2-response.md), and
three new artifacts:

- [evidence.schema.json](plans/android-host/evidence.schema.json);
- [arm-manifest.json](plans/android-host/arm-manifest.json) (64 arms);
- [evidence-predicates.md](plans/android-host/evidence-predicates.md)
  (46 predicates).

Method: a coordinator pass with no new lens agents, as in the ADR's round
3. The next decision is Stage-0 acceptance, so this pass goes deepest on
ADR §0, S−1a and S0, with source checks. It spot-checks the Stage-1
dispositions, and it cross-checks the artifacts mechanically. `plan:NN`
and `ADR:NN` refer to the revised files. Server citations are still from
the older local `any` checkout.

### Verdict

**Accept Stage 0 (K1–K7) once the three small amendments below are
folded in. Stop reviewing Stage 1 on paper until the K1–K7 report
exists.**

- **Stage 0 is now executable as written, apart from three gaps.**
  - It has its own `baoPoc` ID, so it no longer collides with the devpass
    install.
  - Its key, config, trace, export and port channels are named.
  - The K gates measure a real `toolcaller@v1` turn, with a recording
    limiter and two idle stores.
  - The K2 arms have defined inputs, the stacks are pinned, and K6's
    effective rule is stated.
  - The 8-hour screen-off probe (S0b/K7) is bounded to a manual
    foreground service, a wake lock and Stop.

  The three gaps are small:
  - the guest-module source on the phone is unnamed (IP3-2);
  - the frozen contexts are rewritten by the turns themselves (IP3-3);
  - S0b's progress path needs a Kotlin publish binding that Stage 1 was
    going to add (IP3-4).
- **Every one of the 30 round-2 findings has a specified remedy**,
  including the ten that gate Stage 1. The coordinator spot-checked them:
  - the activation substates (IntentReady → ClaimWritten → Open →
    BeatSeen, ADR:710-716);
  - the durable `ReleaseOwed` with retry while Go runs and the host is
    off (ADR:724-737);
  - the resume check against higher claims, including on removed-app
    rows (ADR:741-752);
  - `HostControlled` (ADR:676);
  - the iroh/pkarr/direct sync partition (ADR:932);
  - per-app VPN attribution for T11 (ADR:943);
  - the latest-wins admission slot (ADR:1147);
  - R0, the B1 skeleton, and the L-kotlin/L-rust split in the plan;
  - two ledgers plus a rehearsal (plan:466-472).
- **Why stop the paper review of Stage 1 (IP3-1).**
  - The ADR has grown 235 → 513 → 780 → 1007 → 1232 lines over five
    rounds, much of it in answer to this review.
  - Further Stage-1 rounds will keep finding edges, because a state
    machine this size always has more. But the S0 results (memory
    headroom, load time, trap containment, screen-off survival) could
    change the Stage-1 design: lane sizes, whether a worker process is
    needed at all, the restart policy.
  - The review time is better spent after S0. At that point, do one
    consolidated Stage-1 review against measured numbers, with the
    pinned `v0.3.5` source in hand (S−1b).

### Round-2 dispositions

| Findings | Status |
|---|---|
| IP2-1 `.dev` slot | Resolved: own `org.any.anykotlin.baoPoc` ID from S0 on, Firebase exclusions and no-op bindings, no wipe/re-login workaround (ADR §0 :11-19, plan:31-35) |
| IP2-2 channels | Resolved: masked key entry → in-memory seed, in-memory config, non-Go trace sink, external-files export with a proven SAF fallback, on-screen port. A channel round trip comes before measured runs (ADR §0 :24-53, plan:47-56). The in-memory sink already exists (`runtime/src/tracestore.rs:1240`, `MemTraceStore`). |
| IP2-3 turn realism | Resolved: actual `toolcaller@v1` over frozen contexts, recording-only limiter, two idle stores (ADR §0 :31-62). New issue → IP3-3. |
| IP2-4 K3 accounting | Resolved: empty-worker control, incremental PSS, defined "fresh"/"cold" and load clock (ADR §0 :70-83) |
| IP2-5 K2 arms | Resolved: five 40-case groups, a trusted test component, signal-path proof required (ADR §0 :85-97) |
| IP2-6 K6 rules | Resolved: max ≤120 s binding, attribution evidence, INCONCLUSIVE after two invalid batches (ADR §0 :106-114) |
| IP2-7 stacks | Resolved: 512 KiB wasm, 2 MiB async, 8 MiB Rust thread (ADR §0 :64-68) |
| IP2-8 S−1 split | Resolved: S−1a/S−1b (plan:26-76) |
| IP2-9 screen-off probe | Resolved: the user chose the 8 h probe; S0b/K7 (ADR §0 :107, :116-133) |
| IP2-10 telemetry, source hashes | Resolved (ADR §0 :16-17, :42-43) |
| IP2-11..IP2-30 (Stage 1) | Resolved in spec; spot-checked as listed in the verdict. Not re-reviewed in depth, by design (IP3-1). |

The artifacts are consistent. The schema declares Draft 2020-12. All 64
arms reference 46 predicate IDs, and every one is defined in the catalog,
with no extras. The catalog tags 41 predicates MECH and 8 HUMAN. The
manifest covers T1–T11 only, which is fine for now (IP3-5).

### Findings

**IP3-1. Medium (process). Stop iterating on the Stage-1 contract until
the S0 report exists.**

- ADR-034 is now 1232 lines. Of these, §6 is 229, §5 is 174, §4 is 168
  and §2 is 166.
- Each review round has added about 225 lines, and this review accounts
  for much of that.
- None of it can be checked against a measurement yet. K3–K7 could change
  the Stage-1 design: lane sizes, admission thresholds, the worker split
  itself, restart policy. Text written now may be rewritten after the
  spike.
- Fix:
  - Freeze Stage-1 text at this revision.
  - Accept Stage 0.
  - After the K1–K7 report and S−1b's `v0.3.5` checks, run one
    consolidated Stage-1 review, three lenses, against the measured
    numbers, and accept or amend Stage 1 then.
  - Prefer cutting mechanism over adding it in that round.

**IP3-2. Low–medium. S0 doesn't say where the worker gets its guest
modules.**

- ADR §0 requires "the actual `toolcaller@v1` … and `llm@v1`/broker", and
  hashing "every resolved guest module" (ADR §0 :31-43). S−1a says "frozen
  guest sources" (plan:41-43). Neither says how the phone's worker
  resolves them.
- How it works today:
  - `use()` resolves through the broker's resolver (`runtime/src/broker.rs:2007-2041`).
  - `anyrt run --from-space` uses `AnyModuleResolver` over Go spaces and
    overlays (`runtime/src/main.rs:356-382`). On the phone that needs the
    overlay spaces synced into the throwaway account, plus the resolver's
    read routes in the S0 allowlist.
  - The alternatives already exist: `LocalDirResolver` and
    `InlineResolver` (`runtime/src/resolver.rs:81`, `:307`).
- The `allowTools=false` option is a change to `toolcaller@v1`
  (ADR §0 :33-35). Wherever it lives, the phone runs a modified
  toolcaller, and desktop calibration must run the same bytes.
- Fix: bundle the frozen sources as hashed assets, served by
  `LocalDirResolver` or `InlineResolver` in the worker. That covers the
  fixture program, `toolcaller@v1` with the fixture option, `llm@v1`,
  `history@v1`, `autorecall@v1`, `any@v1` and their dependencies.
  - The S0 allowlist then needs only the fixture's data reads and the
    reply writes.
  - Run desktop calibration from the same bundle.
  - Or name `AnyModuleResolver` and list its routes. That makes S0 depend
    on overlay sync.

**IP3-3. Low–medium. The turns write their replies into the contexts they
read, so the "frozen" contexts drift.**

- ADR §0 freezes "five small and five representative contexts (20 prior
  turns …)" and permits "turn-history writes" (ADR §0 :36-41). S0b runs
  "32 fixed toolcaller turns … same … inputs" (ADR §0 :125-127).
- Evidence:
  - `toolcaller@v1` reads history through `history@v1`
    (`repos/_agent/programs/toolcaller@v1.py:1231`).
  - Unless `quiet` is set, which §0 rightly forbids, it posts each reply
    into the same chat (`:1349-1355`, `c._post_reply`).
- Failure:
  - In S0b, 32 turns over 10 contexts grow each chat by about 3 exchanges
    and change autorecall's inputs. Memory and latency trend with history
    length, so a K7 drift in either can't be told apart from a host leak
    or slowdown.
  - A second K6 batch also runs on contexts mutated by the first.
- Fix: give every turn a fresh copy. Either seed a new chat from the
  frozen snapshot per turn, or post into a scratch chat while history
  reads the frozen one. Record the hash of the context each turn actually
  read. If drift is acceptable, state that and report history length per
  turn.

**IP3-4. Low. S0b's live progress needs a publish binding the plan
schedules for Stage 1, and its bus is at-most-once.**

- ADR §0 says "publish only synthetic progress/result IDs over the fixture
  event bus … every 10 s; … missing live progress for 60 s
  stops/invalidates the probe" (ADR §0 :127-131).
- Evidence:
  - any-kotlin's `AnyClient` has `subscribeEvents` (`AnyClient.kt:186`)
    but no publish call.
  - The plan adds "any missing Kotlin publish-event client binding" only
    in Stage 1's B0 (plan:184).
  - The server's bus is "an in-memory, at-most-once broadcast"
    (`any/internal/server/routes.go:186-191`, `POST /v1/events`).
- Failure: S0b has no progress channel. Or a delivery gap on the bus
  invalidates an otherwise healthy probe. The phone can't know the desktop
  missed beats, so "stops" can't happen on the phone side anyway.
- Fix:
  - Add the `POST /v1/events` binding, or a raw call in the spike screen,
    to S0 step 6.
  - Treat a remote progress gap as an operator alert.
  - Decide invalidity from the phone's own local progress export: its
    timestamps, its wake-lock record, and the process identity.

**IP3-5. Low. K1–K7 have no evidence manifest.**

- `arm-manifest.json` and the schema cover T1–T11 only.
- The K report will be read by a person, which is fine for a spike.
- Fix: write the S0 report with the same run-manifest header as the
  schema (source, artifact, profile and module hashes, device fingerprint,
  BOOT_COUNT). Keep raw K values in a small JSON next to it, so the
  Stage-1 review can re-derive every K verdict.

### Checked and correct (round 3)

- **Devpass can stay intact.** A stopped devpass install is enough for S0:
  its Go server starts only with its Activity, and push alone doesn't
  start Go (ADR §3). Force-stopping it leaves its account and data alone.
- **The trace sink exists.** S0's "bounded in-memory/file sink" needs no
  new store: `MemTraceStore` implements `TraceStore`
  (`runtime/src/tracestore.rs:1240`, `:1303`).
- **The broker supports in-memory key seeding.** It resolves a credential
  from its in-memory `secrets` map when there is no secret store
  (`broker.rs:1386-1401`).
- **The spike needs a deserialize-based Cage.** `run_program` takes a
  `Cage`, a `Broker` and a spec (`runner.rs:262-270`). The only Cage
  constructors today compile and cache: `Cage::new` and
  `Cage::embedded`.
- **The new Stage-1 text holds together where spot-checked.** It closes
  each round-2 scenario:
  - main dies mid-Off (`ReleaseOwed` before the drain);
  - beat lost after Open (a health fault, not a rollback);
  - takeover during downtime (ResumeCheck against higher claims, or
    explicit activation when history is incomplete);
  - name upsert before bootstrap (auth, health and bootstrap first).

### Verification log (round 3)

- ADR §0 (ADR:35-173), plan S−1a/S−1b/S0 (plan:26-125) and the round-2
  response, read in full.
- `runtime/src/runner.rs:262-300` (the `run_program` signature).
- `runtime/src/broker.rs:2007-2041` (resolver dispatch).
- `runtime/src/resolver.rs` (Inline/Any/LocalDir resolvers).
- `runtime/src/main.rs:340-410` (`anyrt run` composition).
- `runtime/src/tracestore.rs:44-53`, `:1240`, `:1303`.
- `repos/_agent/programs/toolcaller@v1.py:12-14`, `:1225-1241`,
  `:1349-1355`.
- `any-kotlin/.../AnyClient.kt:186`;
  `any/internal/server/routes.go:186-197`.
- ADR:706-760 (activation substates, ReleaseOwed, ResumeCheck), :676,
  :932, :943, :1085, :1123, :1147. Plan greps for R0, the B1 skeleton,
  L-kotlin/L-rust, the ledgers, the rehearsal, VpnService, re-score and
  the evaluator command.
- Artifact cross-check (Python, read-only): 64 arms; 46 used predicate IDs
  equal 46 defined; 41 MECH and 8 HUMAN tags; no K gates in the manifest
  or schema. The ADR section line counts were measured with `awk`.

Not verified: everything that needs the device or the pinned `v0.3.5`
source, as the author's response also states.
