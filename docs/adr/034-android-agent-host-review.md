# ADR-034 review record

Scope: [ADR-034](034-android-agent-host.md) (Proposed, 2026-11-02) and the
[Android PoC notes](../android-host-poc-notes.md). This is a review of the
documents only. Nothing was built or run on a device.

Method: three independent reviewers each read the ADR, the notes and the
code through one lens. They had the code, but not each other's conclusions.
The coordinator re-read the source behind every critical and high finding
(see the verification log at the end), merged the overlaps and re-rated
where needed. Paths are relative to the checkout parent: `anybao/`,
`any-kotlin/`, `any/`. A path without a prefix is under `anybao/`.

Everything down to the Round 2 heading is round 1, on the first draft.
[Round 2](#round-2-revised-adr-2026-11-02) reviews the first revision, and
[Round 3](#round-3-second-revision-2026-11-02) the second. Both are at the
end of this file.

| Lens | Critical | High | Medium | Low |
|---|---|---|---|---|
| A. Android platform and process lifecycle | 0 | 2 | 5 | 1 |
| B. Native runtime embedding and concurrency | 1 | 3 | 5 | 1 |
| C. Security, multi-device semantics and testability | 1 | 4 | 4 | 1 |
| Coordinator | 0 | 1 | 0 | 0 |

## Verdict

**Do not accept as written.** The direction is right: integrating into
any-kotlin, using `specialUse`, keeping it opt-in, declining Direct Boot,
and draining before the wipe. Most of the platform claims also checked out.

Four things stop the ADR from being accepted:

- It describes today's `stop()` wrongly (B1).
- It does not say who boots the Go server when no screen is open, and
  today's code deliberately forbids that (X1).
- The loopback scope-out does not hold once an agent runs unattended
  around the clock (C1).
- None of the acceptance criteria can fail (C9).

Amendments 1–8 below block acceptance. The rest can land as amendments
during the PoC.

## Blocking amendments

1. **Bounded stop-and-drain**, and a correct description of today's
   `stop()` (B1).
2. **Loopback**: either an auth token before a real account runs, or a
   throwaway account, stated in §1 (C1).
3. **One Go generation per bao instance.** `AnyRuntime` owns the drain
   hook, and the ADR defines the lock order (B3, A3, C6).
4. **A headless boot path.** The service must start the Go server without
   `MainActivity`, and the app's rule against that has to be amended (X1).
5. **Claims.** The off path releases the claim, and an election disabled
   by an error counts as a startup failure (C2, C3).
6. **A process-model decision.** Evaluate `android:process=":bao"`, and add
   a crash-loop breaker either way (B2).
7. **A kernel precompiled at build time.** This resolves the contradiction
   with the ADR's no-executable-in-writable-storage rule (B5).
8. **Acceptance criteria that can fail**: thresholds, collection methods,
   kill criteria, an unattended reboot, a soak that runs past 72 h, and
   the `benchmark` build (C9, A1, C8).

## Coordinator finding

**X1. High. §2, §3: the service would start the Go server in a process
the app deliberately keeps server-free.**

- `any-kotlin/app/src/main/java/com/anytypeio/anytype/app/MainActivity.kt:80-85`
  starts the runtime from the Activity, on purpose: "a process woken for a
  push or a FileProvider read never shows a screen and must not pay for,
  or keep running, a server."
- `AppCoordinator.initialize()` is the only code that checks auth and
  picks the boot path. It runs from `MainActivity` composition
  (`MainActivity.kt:225`).
- A boot start, a sticky restart and a package-replaced start have neither
  of these.
- §2 says "Startup verifies authorization against the server". It does
  not say who boots the Go server without a screen, or how a later
  Activity attaches to a server the service already started.

*Amendment:* define a headless boot path. The service calls
`AnyRuntime.ensureStarted()`, then runs the authorization checks from C6
without `AppCoordinator`. Amend the `MainActivity` rule and
`any-kotlin/docs/verticals/app-state-and-auth.md` so that an enabled host
is the one exception. An Activity that arrives later adopts the running
server and does not restart it.

## Lens A: Android platform and process lifecycle

**A1. Medium (first rated High, lowered after follow-up research on
2026-11-02). Context, §4, §6.3: after any reboot, bao stays down until
the PIN is typed. A 72 h inactivity reboot exists, but you have to turn
it on.**

- The ADR rightly declines Direct Boot. So after any reboot (an OS
  update, a crash, a power loss), bao stays down until someone unlocks
  the phone. The Context section names this boundary, but §6 never tests
  a reboot with nobody at the phone.
- The review first said a phone locked for 72 h always reboots. Stock
  Android does not do that by default:
  - Google reworded the Play services 25.14 changelog. It first read
    "your device automatically restarts if locked for 3 consecutive
    days". It now reads "Enables a future optional security feature"
    (https://9to5google.com/2025/04/16/android-auto-restart-security/).
  - Google's help page lists "Inactivity Reboot" as one of the
    protections of Advanced Protection. Advanced Protection is off until
    the user turns it on, and it requires a screen lock
    (https://support.google.com/android/answer/16339980?hl=en).
  - Samsung ships its own "Inactivity restart" toggle in One UI 8, and
    reports disagree on whether it is on by default
    (https://www.androidauthority.com/galaxy-s26-inactivity-restart-3647564/).
  - No source was found for a Nothing OS equivalent. The Phone (2a) runs
    Android 16 with Nothing OS 4.x, so Advanced Protection may be
    available on it.
- The always-on Android projects searched (Termux:Boot, OpenClaw on
  Termux, Home Assistant on a phone, kiosk apps) report no 72 h reboot
  problem. What they do report: processes killed in the background, and
  apps that do not start after a reboot until the first unlock
  (https://github.com/termux/termux-boot/issues/47,
  https://www.hexnode.com/forums/topic/android-kiosk-app-does-not-auto-launch-after-reboot-until-device-is-unlocked/).

*Amendment:*

- State that bao needs Advanced Protection off, and any OEM inactivity
  restart off, and check both during setup.
- Decide whether the phone may run without a secure lock screen. With
  none, credential-encrypted storage unlocks at boot, so `BOOT_COMPLETED`
  restarts the host with nobody present. The cost is that anyone holding
  the phone has the account.
- Add an unattended reboot to §6, and run the soak past 72 h so that any
  OEM timer shows up.

**A2. High. §4: `ApplicationExitInfo` cannot tell a user stop from a swipe
out of Recents.**

- `REASON_USER_REQUESTED` covers both a Settings force-stop and "removed
  the application away from Recents"
  (https://developer.android.com/reference/android/app/ApplicationExitInfo).
- A Task Manager stop records the same reason and sends no callback.
  `getSubReason` is hidden.
- Below API 34, an app update also reports `USER_REQUESTED`, and minSdk
  is 28.
- So two §4 rules contradict each other on any skin that kills the process
  on swipe: "require explicit re-enabling after a detected user stop" and
  "swiping out of Recents does not stop the service".
- The ADR runs the check only "on a later launch", so the next
  `BOOT_COMPLETED` or `MY_PACKAGE_REPLACED` undoes a Task Manager stop.

*Amendment:*

- List which exit reasons count as a user stop.
- Run the check in the receivers and on sticky restarts.
- Store the timestamp of the last exit record consumed.
- Treat `REASON_PACKAGE_UPDATED` as not a stop.
- Before committing to §6.4, measure what a swipe records on Nothing OS.

**A3. High. §4: the off path can be cancelled, and a retry can boot the
account being left.** (The lock-order side of this is B3.)

- Logout runs `stop()` then `wipeData()` in sequence, from
  `viewModelScope`
  (`any-kotlin/state/src/main/java/com/anytypeio/anytype/state/AppCoordinator.kt:437`, `513-516`).
- `ensureStarted()` registers a new start after `StartSuperseded`
  (`any-kotlin/app/.../runtime/AnyRuntimeImpl.kt:104-119`), and
  `wipeData()` requires a stopped runtime (`:313`).
- Two failures follow:
  - A supervisor retry or sticky restart that calls `ensureStarted`
    between the stop and the wipe boots the departing account again. The
    wipe check throws, logout ends in `Error(RestartApp)`, and the data is
    never wiped.
  - A drain that waits for an LLM turn in `viewModelScope` is cancelled
    when the user leaves the screen. If the service leaves the foreground
    before the drain ends and no UI is visible, the process becomes cached
    and may be frozen mid-drain (not verified).

*Amendment:*

- Add a runtime gate. The coordinator closes it before stop, and the
  supervisor's `ensureStarted` fails fast while it is closed.
- Run logout and the drain in an app-scoped `NonCancellable` job.
- The service stays in the foreground ("Stopping…") until the drain and
  the Go stop finish.

**A4. Medium. §3: the ADR understates what `startForeground` requires.**

- Every `startForegroundService` call must be answered with
  `startForeground` within a few seconds
  (https://developer.android.com/develop/background-work/services/fgs/troubleshooting).
  That includes a repeated Start, a boot start and a package-replaced
  start, and it holds even when the service then finds it is disabled or
  unauthorized and stops.
- The time limit also covers process start and `Application.onCreate`:
  - the Hilt graph and the Sentry init, then a synchronous theme read
    (`any-kotlin/app/.../AnytypeApplication.kt:63-86`);
  - generating the StrongBox key on first use
    (`any-kotlin/storage/storage-impl/.../KeystoreAead.kt:76`).
- A sticky restart arrives with a null intent, and no receiver exemption
  covers it. So `startForeground` can throw
  `ForegroundServiceStartNotAllowedException` there unless the battery
  exemption is granted (partly verified).

*Amendment:*

- Keep the service's injected graph lazy.
- Promote to the foreground first in every `onStartCommand`, then decide.
- Catch `ForegroundServiceStartNotAllowedException` on every path.
- Read the persisted choice in the receiver before starting the service.

**A5. Medium. §3: the ADR misses the Restricted battery mode, and the
claim that the charger is not required goes too far.**

- In Restricted mode an app "can't launch foreground services", and its
  existing foreground services are removed from the foreground. For apps
  targeting 33+, `BOOT_COMPLETED` is not delivered
  (https://developer.android.com/topic/performance/background-optimization).
- One documented trigger for the system's "restrict this app" prompt is
  "1 partial wake lock held for an hour when screen is off". That is this
  design.
- Doze "ignores wake locks", except for exempted apps
  (https://developer.android.com/training/monitoring-device-state/doze-standby).

*Amendment:* show `isBackgroundRestricted()` as a blocking setup state.
Make "does not require the charger" depend on the exemption, and test it
unplugged with `dumpsys deviceidle force-idle`.

**A6. Medium. §3: renewing the wake lock only "while healthy" can stall
recovery.**

If renewal depends on health, the lock lapses during backoff or a slow
check. Once the CPU suspends, coroutine `delay` and Handler timers do not
fire, so the capped exponential backoff can stall for hours.

*Amendment:*

- Hold the lock through Starting, backoff and Running.
- Renew it from an independent timer at most every timeout/3.
- Release it only in terminal states, and log every lapse.

**A7. Medium. §2, §6: Android 17 per-app memory limits (only if the phone
runs Android 17).**

- According to the reviewer's source
  (https://developer.android.com/about/versions/17/behavior-changes-all),
  Android 17 limits each app's memory based on device RAM, with separate
  limits for visible and non-visible processes.
- One screen-off process holds Go, Wasmtime, CPython and the UI.
- The coordinator did not verify this source. The phone's OS version is
  still unknown (notes, "User constraints").

*Amendment:* record the OS version first. If it is 17, log
`am memory-limiter status` during the soak, and treat memory-limiter kills
as restartable, not as user stops.

**A8. Low. §3: the notification is not a reliable Stop control.**

With notifications denied, the foreground service shows only in Task
Manager
(https://developer.android.com/develop/ui/views/notifications/notification-permission).
Recent releases also let users dismiss foreground-service notifications.

*Amendment:* make the in-app switch the canonical Stop, and drop the word
"persistent". Show active vs standby on the notification (see C10).

## Lens B: Native runtime embedding and concurrency

**B1. Critical. §4: today's `stop()` is described wrongly, and the drain
has no upper bound.**

- **Wrong claim.** §4 says `stop()` "joins service loops but leaves
  in-flight run threads detached". That holds for chat and event-source
  runs, but not for triggers:
  - Cron and once triggers run inline on the ticker thread
    (`runtime/src/serve.rs:3405-3407`), and `stop()` joins that thread
    (`serve.rs:2094-2099`, `1589-1594`).
  - So `stop()` already blocks for a whole trigger run, up to the 1200 s
    wall cap (`serve.rs:2405`), and nothing interrupts it.
- **Never joined:**
  - chat runs (`serve.rs:2832`);
  - event-source threads and the runs they fire (`3486`, `3575`);
  - soft-break timers (`2721`);
  - OAuth receivers (`runtime/src/oauth.rs:668`);
  - the 10 ms epoch ticker (`runtime/src/runner.rs:223`), which lives
    until the last `Arc<Cage>` drops.
- **`start()` cannot be cancelled.** No shutdown flag exists before
  `serve.rs:1892`. Boot makes about 15 serial server calls, each with a
  timeout of up to 300 s (`anyapi.rs:161`), and then compiles the kernel.
- **The break only reaches some work.** The "existing break semantics" are
  real: a mailbox break plus a 20 s grace (`serve.rs:2695-2735`), an epoch
  trap (`runner.rs:311-322`) and a stream-chunk check (`broker.rs:1731`).
  But a plain HTTP call that is blocked never sees them. Its default
  timeout is 180 s, and a guest can set up to 86 400 s
  (`broker.rs:2425-2467`).
- **Failure.** The logout spinner runs for minutes. Threads still calling
  the Go server across `Mobile.stop` reproduce `account_in_use`. That gets
  one retry, then `Error(RestartApp)`
  (`any-kotlin/docs/verticals/app-state-and-auth.md:15-18`).

*Amendment:*

- Correct the description of today's `stop()`.
- Specify a bounded drain:
  - an admission gate in `RunCtx`;
  - an immediate hard break of every live run, triggers included;
  - host calls clamped to the stop deadline;
  - a join of every thread.
- Say what happens past the deadline. Either kill the bao process (B2), or
  persist a "pending wipe" marker, kill the app, and wipe at the next cold
  start before anything boots.
- Give `start()` a cancel token.

**B2. High. §1: a separate process inside the same APK was never
evaluated, and one crash takes everything down.**

The §1 table rejects only a standalone APK. A same-APK
`android:process=":bao"` keeps one database, one UID, one storage
directory and one credential store. And anyrt reaches Go over loopback
HTTP even in-process (`serve.rs:1789`), so sharing the process gains
nothing on the data path.

A separate process buys:

- isolation of a Rust abort, a Wasmtime SIGSEGV or an OOM from the Go
  server and the UI (the JNI boundary cannot convert an abort into an
  error);
- no Wasmtime signal handlers next to Go's (B6);
- separate low-memory-killer accounting (B7, A7);
- `Process.killProcess` as a real bounded stop, which closes B1.

It costs:

- a second ART instance (size not measured);
- Binder for lifecycle and status;
- a per-process guard in Hilt's `Application.onCreate`;
- re-sending `baseUrl` after every Go start.

In-process, a deterministic native crash at startup turns sticky restart
into a crash loop. AOSP then marks the process bad and blocks background
restarts until an Activity launches (from memory; not verified on
Android 16).

*Amendment:* add the `:bao` row to the table, and either adopt it or
justify in-process against these benefits. Either way, add a crash-loop
breaker:

- Write a "native start in progress" marker (or use
  `setProcessStateSummary`) before the JNI start.
- After N `REASON_CRASH_NATIVE` exits with the marker set, stop retrying
  and show "needs attention".

**B3. High. §2, §4: no lock order, and a bao instance is not tied to one
Go start generation.** (Merges A3's race and C6's missing callers.)

- `AnyRuntimeImpl` serializes start, stop and wipe under its own `Mutex`
  (`AnyRuntimeImpl.kt:70`, `288-306`), and nothing that depends on it can
  register a hook.
- Every Go start binds a new ephemeral port
  (`LISTEN_ADDR = "127.0.0.1:0"`, `:380`). anyrt's client keeps a fixed
  address (`serve.rs:1789`).
- §4 lists the callers that must drain first. It misses three:
  - `rebuildIndex` (`AppCoordinator.kt:585-587`);
  - the re-`initialize` on `RestartApp`;
  - the stop of an untracked server (`AnyRuntimeImpl.kt:225-227`).
- The code already warns that streams can reconnect "against the next
  account's runtime" (`AppCoordinator.kt:441-444`).
- The failures:
  - Draining inside `AnyRuntime.lock` while bao's start waits on
    `ensureStarted()` under the bao lock is a deadlock.
  - Draining outside it lets bao boot on a dying `baseUrl`, or on a reused
    port that now belongs to the next account's server.
  - `/v1/devices/me`, `/activate` and `/v1/events` are account-scoped, so
    an orphaned bao writes the next account's device and claim rows.

*Amendment:*

- `AnyRuntime` owns a registered pre-stop drain participant, called from
  `stop`, `wipeData` and `rebuildIndex`.
- The lock order is `AnyRuntime.lock` before the bao lock. Bao never calls
  into `AnyRuntime` while holding its own lock.
- Each bao instance carries the Go start generation and is torn down on
  any `baseUrl` change.

**B4. High. §2: catching panics at the JNI boundary works, but most panics
happen elsewhere.**

- The release profile sets no `panic` key, so panics unwind and
  `catch_unwind` at the JNI boundary works (`runtime/Cargo.toml:53-54`).
- No `catch_unwind` or panic hook exists anywhere in `src`. Service
  threads panic outside any JNI frame.
- `serve.rs` has 71 `lock().unwrap()` calls, so one panic poisons a mutex
  and the failure spreads.
- A panic inside `ctx.run` skips `live_runs.remove` (`serve.rs:2397-2407`)
  and `conversation_done` (`2861-2864`). That chat's mailbox is then dead,
  and new messages vanish into it (`2812-2818`). The drain's "await all
  admitted runs" never finishes.
- `join_all` returns at the first panicked thread and does not join the
  rest (`serve.rs:1589-1594`).
- The release build strips symbols (`strip = true`).

*Amendment:*

- Add a panic hook that logs and moves the supervisor to Failed.
- Use RAII guards for run registration.
- Build a `health()` call on `JoinHandle::is_finished`.
- Join every thread.
- Ship unstripped symbols for the PoC.

**B5. Medium-high. §2: the compiled kernel would be executable code in
writable storage.**

§2 says both "no executable downloaded or extracted into writable app
storage" and "compiled-kernel cache files are in app-private cache
storage". But a Wasmtime compile cache is Cranelift machine code.

- Today, `wasmtime::Cache::from_file(None)` (`runner.rs:210`) finds its
  directory through the user's home directory, which is unset on Android.
  (The reviewer read the `dirs-sys-next` Android fallback; the
  coordinator did not re-check it.) So boot warns and compiles cold on
  every start and every restart. The ADR states neither the cost in time
  nor the transient RSS.
- If a cache directory is added, wasmtime-cache copies the artifact into
  anonymous executable memory. That avoids Android 10's denial of
  executing app-storage files. But it breaks the ADR's own rule, and
  Android's guidance to load only code embedded in the APK
  (https://developer.android.com/about/versions/10/behavior-changes-10).
- A `.cwasm` mapped from app storage would be denied.

| Option | Cost |
|---|---|
| Cold compile on every boot | Tens of seconds of CPU and a large transient RSS on the phone's cores (not measured) |
| Cache on the device | Fast after the first boot, but breaks the §2 rule |
| Precompile at build time, embed in the `.so`, `Component::deserialize` from memory | No Cranelift at runtime. RSS about the size of the code, as anonymous memory |

*Amendment:* precompile at build time. Make the engine `Config` and the
wasmtime 49.0.x version a build invariant, and embed the kernel sha256
separately.

**B6. Medium. §2: the ADR leaves out ART's libsigchain, a third signal
handler.**

- Go (a gomobile c-shared library) installs synchronous-signal handlers
  when `libgojni` loads.
- Wasmtime installs SIGSEGV, SIGBUS, SIGILL and SIGFPE handlers at
  `Engine::new` (`runner.rs:216`). Signal-based traps are on.
- libsigchain intercepts `sigaction` and runs ART's handler first
  (inferred from AOSP, not verified).
- **Failure:** a Go nil-deref inside a `net/http` handler is normally
  recovered as a panic. To recover, the signal must pass through ART, then
  Wasmtime, then Go, with the ucontext intact and `SA_ONSTACK` set. If the
  chain breaks, a recoverable panic becomes a process crash.
- The fallback, `signals_based_traps(false)`, is "up to 2x slower" and
  needs Spectre mitigations off
  (https://docs.wasmtime.dev/api/wasmtime/struct.Config.html).

*Amendment:*

- Fix the load order.
- After `Engine::new`, test:
  - wasm out-of-bounds and divide-by-zero traps;
  - a Go nil-deref that recovers;
  - a Java NPE and a `StackOverflowError`;
  - that tombstones are written.
- Keep the non-signal mode, measured, as the fallback.

**B7. Medium. §2, §6: idle wakeups, and no memory ceiling.**

- The epoch ticker fires every 10 ms for the Cage's whole life, whether or
  not a run is live (`runner.rs:109`, `223-227`).
- Service loops poll in 100 ms slices (`serve.rs:2172-2179`), and the
  control accept loop every 250 ms (`3834`).
- Together that is more than 150 wakeups per second while idle, around the
  clock, under a wake lock.
- Stores have no `ResourceLimiter` or `StoreLimits` (`runner.rs:292-300`),
  so a guest can grow to 4 GiB. Concurrent runs have no cap either.
- In the shared process, a low-memory kill also takes Go and the UI down.

*Amendment:*

- Tick only while a store is live, and use condvar waits instead of
  polling.
- Add `StoreLimits` and a cap on concurrent runs.
- State an RSS budget in §6, and refuse new runs on `onTrimMemory`.

**B8. Medium. §2: desktop defaults and sockets that misbehave on Android.**

| Item | Evidence | Needed |
|---|---|---|
| `traces_dir` defaults to `"traces"`, relative to the cwd. The Android cwd is `/`, so boot fails at `serve.rs:1996` | `config.rs:215`, `blob.rs:94` | Use the existing option (`config.rs:305`) |
| OAuth without a `consent_hook` runs `xdg-open` and logs the URL | `oauth.rs:684`, `1037-1049` | Set the existing hook (`config.rs:266-274`); see C5 |
| The OAuth callback binds `127.0.0.1:0`, on a loopback every app shares. PKCE and state stop code theft, but any app can tear the flow down | `oauth.rs:615` | Name it as an accepted risk, or route consent elsewhere |
| `time.now` reads `TZ`, which is unset on Android | `broker.rs:1142` | Pass the zone from Kotlin |
| The control API includes an unauthenticated `POST /run` that runs any program text | ADR-009 §6 | Make it unreachable on Android at compile time, not behind a flag |

**B9. Medium. §2: the build, packaging and version pinning are not
specified.**

- No Android pipeline exists (notes:27).
- `kernel.wasm`'s provenance is not checked (notes:26). It is embedded
  with `include_bytes!` (`runner.rs:181`), so the `.so` is at least
  24 MB, on top of the 49 MB `libgojni.so`.
- No pin ties anyrt to the any AAR. any-kotlin pins `any v0.3.5`
  (`any-kotlin/gradle/libs.versions.toml:2`).
- "The selected build channel's published overlays" has no counterpart in
  the config, where overlays are bare space ids (`config.rs:276-299`).
- **Failure:** an overlay deployed from the desktop needs a newer anyrt or
  kernel than the phone has.

*Amendment:*

- anybao CI publishes a versioned AAR (rustc 1.99.0, NDK 28.1, sha256,
  symbols), the way the any AAR is published
  (`any-kotlin/.github/workflows/sync-any-aar.yml`).
- Boot checks a manifest of {anyrt version, kernel sha, minimum any}.
- Define "build channel".

**B10. Low. §2: the TLS policy is unstated.**

- anyrt uses ureq 2.12 with rustls 0.23, ring and compiled-in
  webpki-roots (`Cargo.lock`). That needs no JNI verifier init, which is
  good.
- But it ignores Android's CA store, its network-security-config and its
  proxy/PAC settings, and the set of trusted roots is frozen per build.
- The untracked `runtime/src/broker/chatgpt.rs` brings in reqwest and
  tokio. reqwest's default native-tls would pull OpenSSL into the Android
  build.

*Amendment:* state the TLS policy, and require rustls for any new HTTP
stack. Test behind a proxy and on an IPv6-only network.

## Lens C: Security, multi-device semantics and testability

**C1. Critical (blocks a real account). §2: loopback cannot stay out of
scope.**

- **The server has no client auth.**
  - The any server gates `/v1` only on "engine ready"
    (`any/internal/server/routes.go:79-102`). Its docs say "loopback is
    still the trust boundary" (`any/docs/02-server.md:70-71`).
  - The `X-Any-Control-Token` guards only `/v1/auth`, `/v1/shutdown` and
    `/v1/local-discovery` (`02-server.md:60-62`).
  - any-kotlin sends no auth header
    (`any-kotlin/any-client/any-client-impl/.../AnyClientModule.kt:52-58`)
    and binds `127.0.0.1:0` (`AnyRuntimeImpl.kt:380`).
- **Any app can reach it.** On Android, every app that has `INTERNET` can
  reach a loopback port, whatever its UID. Such an app can:
  - find the port;
  - post into bao's chat as the owner and read the reply. Bao then acts
    with the account's mail and LLM credentials.
  - query the `agent_secrets` values, which the broker itself reads over
    the same loopback (ADR-021:197-200).
- **The ADR raises the risk.** The exposure already exists while the app
  runs. The ADR makes it around the clock, screen off, with an agent that
  acts on what it reads. "Does not expand its access policy" is true of
  the policy, not of the risk.

*Amendment:* before the PoC runs on a real account, require a per-process
bearer token on every `/v1` route. Hand it in-process to OkHttp and, over
JNI, to anyrt, and never log or persist it. The managed control token is
the natural base. Otherwise, limit the PoC to a throwaway account with
scoped test keys, and say so in §1.

**C2. High. §4, §5: if the phone is active and is stopped, uninstalled or
killed by the OEM, nothing answers.**

- The registry carries no liveness (`any/docs/23-devices.md:51-52`). A
  claim is lost only when the winner's row loses `apps.bao`, or the row
  is pruned (`23-devices.md:72-78`).
- A desktop that sees a winner row carrying `apps.bao` stands by
  (`runtime/src/election.rs:139-147`).
- `AgentHandle::stop` releases nothing (`serve.rs:1571-1581`), and the
  restart is "best effort, without a promised deadline".
- **Failure:** the user taps Stop, or Nothing OS kills the app overnight.
  The phone stays the winner and the desktop stays on standby, so nothing
  answers until someone claims on the desktop.

*Amendment:*

- The off path (Stop, switch off, logout) ends, after the drain, with
  `PUT /v1/devices/me {"apps":{"bao":null}}`. That is the documented way
  to give up a claim.
- State that a crash or an Android uninstall leaves a winner that only a
  manual claim elsewhere recovers. Test both cases.

**C3. High. §5: "does not silently seize" is not guaranteed, and turning
the election off on an error is dangerous on an always-on host.**

- **A fresh phone can win.** Rule 2 claims whenever the local replica
  shows no other bao row (`election.rs:132-137`). A claim's seq is the
  highest visible + 1 (`23-devices.md:39-40`), and claims from a stale
  replica are documented (`:99`). So a freshly logged-in phone whose
  registry has not synced can claim at seq 1, and beat the desktop's
  seq-1 claim on `at`.
- **An error makes the phone active.** A failed device registration turns
  the election off, and the device stays active "this run"
  (`election.rs:258-259`; `Verdict::disabled()` is `active: true` at
  `:41-46`; ADR-015:156-159). On an always-on host, "this run" lasts days
  of split-brain.

*Amendment:*

- Start anyrt only after the server reports that the tech-space bootstrap
  is complete (`ServerBootstrapMonitor`).
- Treat an election turned off by an error as a startup failure with
  backoff, not as "active".
- Add to §6: enable bao on a freshly logged-in phone while the desktop is
  active.

**C4. High. §5, §6.4: split-brain and duplicate side effects on reconnect.**

- The registry read is local, and an error keeps the last verdict
  (ADR-015:139-143). So a phone that is active and offline stays active.
- Runs in flight finish after stand-down (ADR-015:133-135).
- ADR-015's own motivating incident was a device that woke, replayed its
  backlog and answered twice (ADR-015:31-34).
- **Scenario:**
  1. The phone loses Wi-Fi mid-turn.
  2. The user claims on the desktop, and the desktop answers.
  3. The phone reconnects, finishes its turn, and answers the synced
     backlog for up to 10 s.
  4. The result is duplicate replies and duplicate external effects, such
     as sent mail.
- §5 disclaims exactly-once only for scheduled jobs.

*Amendment:* state that effects triggered by chat are at-least-once. On
reconnect, reconcile the election before chat is admitted again. Add the
scenario to §6 with "zero duplicate replies" as the pass bar, or record it
as an accepted risk.

**C5. High. §2, §5: "Reuse account-stored credentials" does not hold for
every kind of credential.**

- **ChatGPT grants.**
  - They are device-local and never synced (ADR-030:106-107).
  - They need native platform custody before a platform is enabled
    (030:136-145).
  - Setup readiness is the responder's ADR-030 §8 beat (030:693-698). But
    any-kotlin's gate checks only that a beat is fresh
    (`any-kotlin/feature/feature-agent-devices/.../BaoDevices.kt:122-134`).
  - **Failure:** on an account that uses the ChatGPT preset, the phone
    claims and every turn fails.
- **OAuth consent.**
  - Transport A runs the browser and the loopback receiver on the device
    that serves (ADR-011:276-283).
  - `system_browser` has no Android branch and falls through to `xdg-open`
    (`oauth.rs:1037-1049`). Transport B is "not built" (ADR-011:293).
  - **Failure:** a consent request on the unattended phone has no way to
    complete.

*Amendment:*

- List the credential kinds the PoC supports.
- Declare ChatGPT-local grants and `oauth.connect` unsupported on Android.
  Return a typed failure, or set a `consent_hook` that posts the URL to
  chat.
- Gate "Use this device" on ADR-030 §8 readiness when that preset is
  configured.
- Add ADR-011, ADR-021 and ADR-030 to "Builds on".

**C6. Medium-high. §2: "verifies authorization" is not defined.**
(The generation tie is in B3.)

*Amendment:* define it. All four must hold:

- `GET /v1/auth` reports authorized.
- Its accountId equals the account the opt-in was enabled for.
- The device's own peer id is unchanged.
- The device is not pruned.

Check again on every sticky or boot restart, and on a `Deauthorized`
close.

**C7. Medium. The device threat model and the logs are not addressed.**

- **Device transfer.** `wallet.key` is on disk
  (`any/docs/02-server.md:48`) under `filesDir`
  (`AnyRuntimeImpl.kt:268`). `allowBackup=false` does not block Android
  12+ device-to-device transfer, and the transfer rules exclude only
  sharedpref (`any-kotlin/app/src/main/res/xml/data_extraction_rules.xml:4,7`).
  A phone migration could copy the key the peer id derives from, leaving
  two phones with one peer id (not verified on a device).
- **Debuggable build.** A debuggable PoC build lets `run-as` read the
  wallet and the decrypted store.
- **Prune.** A prune is noticed only at boot or at a claim
  (ADR-015:150-155), so a pruned phone on standby keeps firing pinned
  jobs.
- **Grants.** A local delete does not revoke grants at the provider
  (ADR-011:453).
- **Logs.** Serve logs previews of user messages (`serve.rs:3054`, `3097`,
  `3322`). In PROD, Timber feeds Sentry breadcrumbs
  (`any-kotlin/.../CrashReportingModule.kt:27`).

*Amendment:*

- Add a lost-phone runbook: prune the device, rotate keys, revoke grants.
- Exclude the data dir from device transfer.
- Name anyrt's log sink: an app-private rotating file, never Timber or
  Sentry.
- State whether running a debuggable build on the real account is
  accepted.

**C8. Medium. §1, §6.6: no mechanism keeps the PoC out of release.**

- The app has no product flavors. Its build types are:
  - `debug`: `.dev`, debuggable;
  - `benchmark`: inherits from debug, keeps `.dev`, not debuggable;
  - `release`.

  (`any-kotlin/app/build.gradle.kts:138-183`.)
- A library module's manifest (the service, the receivers,
  `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`) and its jniLibs merge into every
  variant that depends on the module. That includes a transitive
  dependency through `agent-devices-impl`.
- The "development PoC build" is defined nowhere.

*Amendment:*

- Name the variant, for example a `baoPoc` build type with
  `baoPocImplementation(project(":agent-host"))`.
- The UI depends only on an api interface, with a no-op binding in the
  other variants.
- §6.6 becomes a CI check on the release APK: `apkanalyzer` finds none of
  the PoC permissions or services, and the APK holds no anyrt `.so`.

**C9. Medium. §6: none of the acceptance criteria can fail.**

- **No thresholds** for latency, missed or duplicate replies, restarts,
  RSS, storage, temperature, or battery health at a constant 100% charge.
- **No named jobs.** "Scheduled-job traces" names no jobs, and the
  built-in jobs run only on the active device (ADR-015:105-112).
- **No collection method.** Trace bodies stay on the phone
  (ADR-023:66, 306-307), so collecting them needs `adb forward` plus
  `anyrt trace` or `/v1/local/export`. logcat will not hold 24 h.
- **No unplugged test.** §3 claims the service runs unplugged, but §6
  tests only on charge.
- **Wrong build.** A debuggable build skews RSS, CPU and thermal numbers;
  use `benchmark` (`app/build.gradle.kts:169-182`).
- **No kill criterion.** The notes say only "record the actual failure"
  (notes:69-70).
- **No scope sentence** limiting the conclusions to this phone and this OS
  version.
- **Too short.** A 24 h soak cannot show that no inactivity reboot is
  on, and §6 has no unattended reboot (A1).

*Amendment:*

- Number the criteria, each with a threshold and a collection method.
- Add kill criteria, for example: a Go/Wasmtime signal conflict that
  cannot be fixed, more than N low-memory kills in 24 h, or any duplicate
  reply.
- Soak for at least 80 h on `benchmark`.
- Add the scenarios from C1–C4, A2 and B1, and the scope sentence.

**C10. Low. Cross-repo ownership, the contract, and the product.**

- **Ownership.** The ADR dictates any-kotlin code but names no any-kotlin
  owner or DROID issue. These any-kotlin docs and comments become false:
  - `docs/verticals/bao-status.md:9`;
  - `BaoDevices.kt:117-120`;
  - `AgentDevicesViewModel.kt:47-51`;
  - `docs/verticals/app-state-and-auth.md:41`.
- **The contract is left to code**, against the README rule
  (`docs/adr/README.md:6-9`):
  - the name of the control-listener option;
  - the cache-path option ("if necessary");
  - what stop-and-drain returns and does at its deadline, including with
    OAuth's 5-minute receiver.
- **Product.** Nothing requires the notification to show active vs
  standby. The desktop shows the phone as "localhost" or a model code
  until the devices screen writes a name (`bao-status.md:9`).

*Amendment:* add a §7 "Embedder contract" with signatures and semantics.
List the any-kotlin doc changes, and name who accepts them on that side.

## Checked and found correct

- `specialUse` is not on Android 15's list of types barred from
  `BOOT_COMPLETED`: dataSync, camera, mediaPlayback, phoneCall,
  mediaProjection and microphone. The `dataSync` claims (6 h per 24 h, no
  start from `BOOT_COMPLETED` for targets 35+) are accurate.
- `BOOT_COMPLETED`, `MY_PACKAGE_REPLACED` and the battery exemption are all
  documented exemptions from the rule against starting a foreground
  service from the background. "Receivers only request the start" is
  sound.
- A denied notification permission does not stop a foreground service.
- The control listener is unconditional, port 0 binds an ephemeral port,
  and a bind failure is not fatal (`serve.rs:2093-2098`, `3824-3829`).
- No embedder cache-path option exists. A `setenv(HOME)` redirect would
  race Go's and ART's threads, so the ADR is right to forbid it.
- `Home` can be reached after a failed auth check
  (`AppCoordinator.kt:80`; `app-state-and-auth.md:20-21`), so auth must be
  checked on its own.
- The wallet is in credential-encrypted storage, and the Keystore key does
  not require an unlocked device (`KeystoreAead.kt:91-101`). So a start
  after first unlock works while the phone stays locked, and declining
  Direct Boot is consistent.
- Shell effects are off by default (`runtime/Cargo.toml`, `default = []`).
  The release profile unwinds, so `catch_unwind` at the JNI boundary is
  possible.
- TLS is rustls with webpki-roots, so no JNI verifier init is needed.

## Coordinator verification log

**Re-read in the source:**

- **B1:** `serve.rs:3405-3407` (triggers run inline), `2094-2099` (the
  ticker is among the joined threads), `2405` (1200 s cap), `1571-1594`
  (`stop` and `join_all`).
- **B4, B5, B7, B8:**
  - `runtime/Cargo.toml:53-54`: no `panic` key; `strip = true`.
  - A grep finds no `catch_unwind`, panic hook or `ResourceLimiter` in
    `src`, and 71 `lock().unwrap()` in `serve.rs`.
  - `runner.rs:109` (10 ms tick), `210` (`Cache::from_file(None)`),
    `223-227` (the ticker loop), and `config.rs:215` (relative
    `traces_dir`).
- **C1:** `routes.go:79-102`, `02-server.md:60-71`,
  `AnyClientModule.kt:52-58`, `AnyRuntimeImpl.kt:380`.
- **C2, C3:**
  - `serve.rs:1571-1581`;
  - `election.rs:41-46`, `132-147`, `258-259`;
  - `23-devices.md:49-53`, `72-78`;
  - ADR-015:31-34, 139-143, 156-159;
  - `oauth.rs:1037-1049`.
- **C5, C7:** ADR-030:106-107, and `data_extraction_rules.xml:4,7`.
- **A3, A4, X1, C8:**
  - `AnyRuntimeImpl.kt:104-119`, `268`, `313`;
  - `AppCoordinator.kt:80`, `437`, `513-516`;
  - `MainActivity.kt:80-85`, `225`;
  - `AnytypeApplication.kt:58-86`;
  - `KeystoreAead.kt:70-101`;
  - `app/build.gradle.kts:160-183` (no flavors).

**Taken from the reviewers, not re-verified (flagged in each finding):**

- whether Nothing OS 4.x ships its own inactivity restart (no source
  found; A1);
- `ApplicationExitInfo` and Task Manager behaviour on Nothing OS;
- the AOSP bad-process rule on Android 16;
- the Android 17 memory limiter;
- the libsigchain ordering;
- the `dirs-sys-next` Android fallback;
- the remaining line references into ADR-011, ADR-021, ADR-023 and
  ADR-030 and `any/docs/23-devices.md`;
- the broker and oauth line references other than `oauth.rs:1037-1049`.

**Side note, outside this review:** any-kotlin's `CLAUDE.md` says the
`benchmark` build uses a `.dev2` applicationId. `app/build.gradle.kts:166-169`
says it keeps `.dev`.

## Round 2 (revised ADR, 2026-11-02)

Scope: the revised ADR-034 (513 lines), plus the author's disposition
table and source corrections in the PoC notes
(`docs/android-host-poc-notes.md:81-174`).

Method: the same three lenses, with fresh reviewers. Each one read the
revised ADR, the notes and the round-1 record above. Each judged its own
lens's round-1 dispositions against the revised text, not against the
notes' summary of it, and then looked for new defects. Two reviewers
found the two worst problems independently (R2-1 and R2-2). The
coordinator re-read the source behind every high and medium-high finding
(log at the end) and merged the overlaps.

| Lens | Round-1 resolved | Round-1 partly resolved | New: high | New: medium-high | New: medium | New: low |
|---|---|---|---|---|---|---|
| A. Android platform and process lifecycle | 6 of 9 | 3 | 2 | 1 | 4 | 3 |
| B. Native runtime embedding and concurrency | 7 of 10 | 3 | 3 | 2 | 3 | 0 |
| C. Security, multi-device semantics and testability | 3 of 10 | 7 | 2 | 2 | 4 | 0 |

New findings that two lenses raised are counted under both, so the row
counts add up to more than the 21 findings below.

### Round 2 verdict

**Much closer, but not yet acceptable.**

The revision resolves 16 of the 29 round-1 findings in specification,
partly resolves the other 13, and ignores none. The new design is the
right shape:

- a Rust worker in `:bao`, with Go kept in main;
- a trusted kernel artifact built at build time;
- a throwaway account;
- strict, explicit election;
- eleven pass/fail tests.

Five new high findings block acceptance:

- Three are text fixes inside this ADR: R2-2, R2-3 and R2-4.
- Two need a decision between upstream `any` work and an explicit
  fallback:
  - R2-1: a Go stop that reports when it has drained, or a pending wipe
    plus killing main.
  - R2-5: an upstream way to clear the device's own claim, or a beat mode
    that runs without registering. Otherwise, accept that a phone that
    claimed once cannot return to standby.

The four medium-high findings (R2-6 to R2-9) are contract text and should
land in the same revision. The rest can be amended during implementation.

### Round-1 dispositions, judged against the revised text

| ID | Verdict | What remains |
|---|---|---|
| X1 | Partly | Admission check 3 relies on a health signal that does not mean "bootstrap complete" (R2-6). |
| A1 | Resolved | — |
| A2 | Partly | The exit-reason table misreads `REASON_USER_STOPPED` and misses several reasons (R2-10). |
| A3 | Resolved | The new stop rule creates R2-1. |
| A4 | Resolved | T2's "from process start" means nothing when the process is already running (an Activity attach). Measure those starts from `onStartCommand`. |
| A5 | Resolved | — |
| A6 | Resolved | A residual remains: R2-21. |
| A7 | Partly | Memory-limiter kills are not classified as resource deaths (R2-10). |
| A8 | Resolved | — |
| B1 | Partly | The Go-side check can never fire (R2-1). The 30 s cap and the cancellation mechanism are wrong or unspecified (R2-3). |
| B2 | Resolved | The kill semantics are a new problem: R2-2. |
| B3 | Partly | Start atomicity and the Binder calls cannot be implemented as worded (R2-8). |
| B4 | Resolved | Implementing `health()` is a new problem: R2-12. |
| B5 | Resolved | The specification needs more detail: R2-13. |
| B6 | Resolved | Sentry's NDK handlers must also stay out of `:bao` (R2-17). |
| B7 | Partly | The capacity semantics and the fit with T8 (R2-7). |
| B8 | Resolved | — |
| B9 | Resolved | — |
| B10 | Resolved | The reqwest-based `runtime/src/broker/chatgpt.rs` is untracked and not in the module tree. |
| C1 | Partly | The agent itself is a loopback client (R2-4). |
| C2 | Partly | Takeover after an uninstall is untested: T5 checks restarts, not a desktop takeover. The 5 s "propagation/read check" names no signal, though the tech space's sync status exists. |
| C3 | Partly | The bootstrap signal (R2-6). An explicit claim made from a stale replica can still lose silently. |
| C4 | Partly | "Network loss" is undefined (R2-9). Right after a reconnect, the local registry still names the phone as the winner. |
| C5 | Resolved | — |
| C6 | Resolved | Nit: a GET cannot tell a pruned device from one not yet registered. Only a write to the device's own row returns `device.pruned` (`any/docs/23-devices.md:124-132`). |
| C7 | Partly | The non-debuggable build turns PROD Sentry on (R2-17). The device-transfer gap in the shipped app is still untracked. |
| C8 | Resolved | Nit: `baoPoc` inherits `matchingFallbacks += debug` (`any-kotlin/app/build.gradle.kts:182`). |
| C9 | Partly | T7 has no attribution rule (R2-15), there is no seam for injecting faults (R2-18), and the power state is not recorded (R2-20). |
| C10 | Partly | The `any` repo is a prerequisite per §1, but it is missing from the cross-repo list and the acceptance request. The list of amended ADRs leaves out ADR-025 (gate semantics) and ADR-011's consent hook. |

### Corrections to round 1

The author made these corrections, and the round-2 reviewers confirmed
them.

- **A4.** Round 1 said a sticky restart may throw
  `ForegroundServiceStartNotAllowedException` without the battery
  exemption. The `Service#START_STICKY` reference says the background-start
  restriction "doesn't impact restarts of a sticky foreground service"
  (https://developer.android.com/reference/android/app/Service#START_STICKY).
  That claim is withdrawn, and §4 now matches the documentation.
- **C3.** Round 1's amendment to wait for `ServerBootstrapMonitor` would
  fail open. The monitor starts at `false` and drops its flag after errors
  or a timeout
  (`any-kotlin/state/.../ServerBootstrapMonitor.kt:28-31`, `42`, `57-62`,
  `80-88`). That amendment is withdrawn; R2-6 gives the replacement.
- **A1** was corrected before the revision: the 72 h inactivity reboot is
  opt-in.
- **Confirmed edge case.** The author added one: restoring `apps.bao`
  revives a retired claim (`any/docs/23-devices.md:61-64`, `74-76`). It is
  confirmed, and it is the root of R2-5.

### New findings

**R2-1. High. §4: "`Mobile.stop` must complete successfully before
deletion" can never fail, so it does not protect the wipe.** (Lenses A
and B, independently.)

- `Mobile.Stop()` always returns `nil`
  (`any/mobile/android/mobile.go:75-78`).
- Go's engine teardown gives up on in-flight handlers at its graceful
  deadline, and releases their resources later in a goroutine
  (`any/internal/server/engine.go:552-575`). A handler started by the
  killed worker can still be writing to the account directory when the
  wipe runs.
- The graceful embedded stop waits on `<-h.done` with no deadline
  (`any/internal/embedded/embedded.go:371-376`). A hang leaves the service
  in Stopping with no end.
- `AnyRuntimeImpl.stop()` logs stop errors and carries on
  (`any-kotlin/app/.../runtime/AnyRuntimeImpl.kt:300-305`).
- There is no way out of a failed logout. It ends in
  `Error(RestartApp)`, whose Restart re-runs `initialize()` in the same
  process (`AppCoordinator.kt:571-576`). With the transition gate closed,
  that cannot make progress, and the foreground service keeps the process
  alive.
- As written, the rule changes logout for every variant, not only
  `baoPoc`.

*Amendment:*

- Add an upstream, bounded stop that reports whether Go drained or left
  stragglers. Block wipe, rebuild and account change on it.
- If it reports stragglers or times out, persist a pending wipe, leave the
  foreground and kill main. The next cold start finishes the wipe before
  Go boots.
- Say whether the rule applies only to `baoPoc`.

**R2-2. High. §1, §4: a killed worker comes back, because the binding
uses `BIND_AUTO_CREATE`.** (Lenses A and B, independently.)

- With `BIND_AUTO_CREATE`, a binding survives the death of the service's
  process. The system recreates the service and calls
  `onServiceConnected` again
  (https://developer.android.com/reference/android/content/ServiceConnection).
- So "bound-only, with no independent sticky/boot restart" is not true. A
  `killProcess` while main is still bound respawns `:bao` with a new pid,
  during the 5 s death check or during the wipe.
- A worker that crashes on load is restarted at the system's own pace,
  and the breaker never sees it.
- After main dies, an orphaned `:bao` loses its client's importance and
  can be cached and frozen. A new main's bind can then land in that
  process, with the old native state still in it.

*Amendment:*

- Unbind before every kill, and on `onServiceDisconnected` and
  `onBindingDied`.
- Confirm the old process's death with a `DeathRecipient`.
- Make each worker process single-session: a second Start in the same pid
  is refused, and main kills that process.
- Before binding, a new main kills any `:bao` that survived (found with
  `getRunningAppProcesses()`).

**R2-3. High. §4: the 30 s cap on "Android host operations" would cut
real model calls short, and the cooperative drain has no cancellation
mechanism.** (Lens B.)

- **LLM calls.** If the cap covers guest HTTP, it breaks them:
  - LLM calls use `{"idle": 60, "total": 900}`, and one backend uses
    590 s (`repos/_agent/programs/llm@v1/program.py:1238`, `1120`).
  - The broker defaults are 180 s plain and 900 s streamed
    (`runtime/src/broker.rs:2425-2467`).
  - T7's p95 ≤120 s needs turns made of several calls, any of which can
    run past 30 s.
- **Server waits.** It would also cut them short:
  - bundle creates, which the server holds for up to 2 min
    (`any/internal/server/handlers_bundles.go:44`);
  - the wait for registry convergence, up to 30 s
    (`runtime/src/anyapi.rs:153-161`);
  - SSE streams, which are unbounded by design (`anyapi.rs:158`).
- **Cooperative drain.** The 10 s budget cannot be met as built:
  - idle streams see the stop flag only at the 25 s keepalive
    (`any/internal/server/handlers_subscribe.go:16`,
    `runtime/src/serve.rs:1568-1570`);
  - ureq 2 has no API to cancel a request in flight;
  - `getaddrinfo` cannot be interrupted.

*Amendment:*

- Set budgets per class of operation: idle or progress timeouts, total
  caps sized to the server's own waits, and SSE exempt. Clamp all of them
  to the stop deadline.
- Name the cancellation mechanism, for example a registry of sockets that
  can be shut down.
- Otherwise, state plainly that killing the worker is the normal stop path
  and the 10 s is a grace period.

**R2-4. High (if a real-account Any app shares the phone). §1, §5: the
agent itself is an unfenced loopback client.** (Lens C.)

- Guest code reaches `any` over loopback HTTP by design. A guest
  `http.get` to `127.0.0.1` runs live (`runtime/src/broker.rs:2626-2633`).
- The only fences are the secrets-store guard (`broker.rs:1805-1860`) and
  the host checks on requests that carry credentials (`broker.rs:1436`,
  `1532`, `2402`).
- So `run_cell` code, or a prompt injection that steers it, can:
  - call this phone's own `/v1/devices/activate`, or revive a claim with
    `PUT /v1/devices/me`, which bypasses `ExplicitClaimStrict`;
  - call `DELETE /v1/devices/:peer`, which permanently prunes another
    device;
  - scan for any other Any app's loopback port on the phone
    (`127.0.0.1:0`, `AnyRuntimeImpl.kt:380`).
- The user's normal dev app, on a real account, may be on the same phone.
  If so, the throwaway-account framing hides that the PoC adds an
  LLM-steerable client next to that real-account server. Whether it is
  installed was not checked.

*Amendment:*

- Either record in the device inventory that no Any app on a real account
  runs on the phone during the PoC, or add a broker egress rule. The rule
  denies guests every loopback and private destination except the lease's
  `baseUrl`, and denies them writes to the device registry.
- T11 adds an agent-originated loopback probe and a forged `activate`
  probe.
- The setup disclosure names both risks.

**R2-5. High. §5: strict election and the presence gate deadlock after an
explicit off.** (Lens C.)

- After an off, the device's own row no longer carries `apps.bao`. The
  switch gate then returns `NOT_INSTALLED` before it looks at heartbeats
  (`any-kotlin/feature/feature-agent-devices/.../BaoDevices.kt:127`).
- A heartbeat needs a serve, and serve's boot registers `apps.bao` first
  (`runtime/src/election.rs:219-225`).
- So "Enable and use this device" has to register before presence can be
  confirmed, and registering revives the stored claim
  (`any/docs/23-devices.md:74-76`).
- That contradicts §5's "activation reuses the existing claim plus
  presence confirmation". It is also the hazard the gate exists to
  prevent: a claim that wins for a device that may not answer
  (`BaoDevices.kt:117-120`).
- No write clears a claim, so a phone that has claimed once can never come
  back as standby, unless another device claims later. §7's
  `ElectionPolicy` has no observe-only mode.

*Amendment:* choose one of these, and add a T4 arm that turns the phone
off and re-enables it as standby:

- an upstream way for a device to clear its own claim, so a release
  clears the claim and the app together and re-registering is plain
  standby;
- a beat mode that runs without registering, with the gate extended to
  accept it.

**R2-6. Medium-high. §3 item 3, §5 reconnect: the health bootstrap
signal exists, but it does not mean what the ADR needs.** (Lenses A and
C.)

- `/v1/health` has a `bootstrapping` field, but `false` means either
  "unauthorized" or "boot pass finished"
  (`any/internal/server/handlers_meta.go:172-189`).
- `account` and `bootstrapping` are read under separate gate entries
  (`:197-206`). One response can therefore pair an account with the
  `false` of a teardown in progress.
- The flag is one-shot: it never re-arms on reconnect. It covers the
  local boot and catch-up pass, not registry sync.
- The ADR rightly says no flag proves convergence, but §5's reconnect
  check is left with no signal to use.

*Amendment:*

- At boot, accept `account == recorded && !bootstrapping` only when it
  falls between two `/v1/auth` reads of the same generation.
- On reconnect and before activation, require the tech space's sync
  status to read synced with at least one network peer
  (`GET …/sync-status/objects/:indexObjectId`,
  `any/internal/server/techspace.go:108-110`). Then check
  `active.bao == self`.
- Or list a three-state bootstrap signal as upstream work in §7.

**R2-7. Medium-high. §2: the capacity cap does not match how serve runs
work.** (Lens B.)

- **How serve runs work today:**
  - one live run per chat; a second message is injected into it
    (`runtime/src/serve.rs:2807-2830`);
  - one trigger at a time, inline on the ticker (`:3405-3407`);
  - one thread per event source (`:3486`, `:3575`);
  - `use()` runs inside the same store.
- **So a cap of two is mis-sized.** A "third concurrent chat" cannot
  happen. But chat plus one cron fills the cap, and an event that fires
  then is dropped.
- **A rejection recorded as a failure has side effects:**
  - three cron failures trip `auto_disabled`
    (`runtime/src/triggers.rs:170-181`);
  - a once trigger's shot is consumed before the run (`serve.rs:3400`),
    so the shot is lost.
- **T7 lateness.** The serial ticker also carries the 15-minute and
  hourly standing jobs (`triggers.rs:623-650`). T7's "≤60 s late" bound
  for the heartbeat fails whenever one of them runs long.
- **T8 budget.** Two stores at 512 MiB each, plus Go, two ART instances
  and the deserialized artifact, can exceed T8's 1.5 GiB p95 on their
  own.

*Amendment:*

- Add a `capacity` status that does not count as a failure or toward the
  breaker.
- Reserve a slot before a once trigger's shot is consumed.
- Use per-class slots instead of one global reject.
- Derive an aggregate cap from T8.
- Measure the peaks of the ADR-031 PDF conversion and of mail sync.
- Give the heartbeat a lane the standing jobs cannot delay, or loosen
  T7's lateness bound.

**R2-8. Medium-high. §4, §7: the start atomicity and the Binder calls
cannot be implemented as worded.** (Lens B.)

- `AnyRuntimeImpl`'s `lock` is a private, non-reentrant `Mutex`
  (`AnyRuntimeImpl.kt:70`), and the start job holds it across
  `Mobile.start` (`:150-153`).
- `ensureStarted` returns no generation; only `_baseUrl` is published.
- So registering the lease "under that order" means exposing the mutex
  and holding it across a two-way `Worker.start`.
- Binder transactions have no timeout. A wedged worker blocks a two-way
  `Worker.stop` or `Worker.status` call indefinitely, while main holds the
  transition mutex.

*Amendment:*

- Make every `Worker.*` call and callback `oneway`, with main enforcing
  its own timers.
- `AnyRuntime` publishes an atomic `(generation, baseUrl, gateOpen)`
  snapshot.
- The start path validates the snapshot, registers the session and sends
  Start, all under the bao lock only.
- The drain participant runs while stop holds the mutex. It closes the
  gate, then invalidates the lease under the bao lock.

This keeps the mutex private and leaves no gap between the check and the
start.

**R2-9. Medium-high. §5: "network loss" is undefined, and cancelling on
it conflicts with T7.** (Lens C.)

- §5 does not say whether loss means Android connectivity, sync-node
  reachability or LLM reachability. Android connectivity misses captive
  portals and sync-node outages.
- §2 says "do not automatically retry possibly committed work". Together
  with that rule, cancelling live turns on every network blip drops probe
  replies, so T7 fails by the ADR's own rules.

*Amendment:*

- Define loss as sync-node unreachability sustained for N seconds.
- Close new admission, but let in-flight turns finish unless the election
  verdict changes after resync.
- Define how unanswered messages are picked up after a reconnect.

**R2-10. Medium. §4: the exit-reason table is wrong in one place and
incomplete.** (Lens A.)

- `REASON_USER_STOPPED` means a multi-user profile was stopped, not that
  the user stopped the app
  (https://developer.android.com/reference/android/app/ApplicationExitInfo).
- These reasons are not classified:
  - `REASON_PACKAGE_STATE_CHANGE`: toggling the boot receiver without
    `DONT_KILL_APP` kills main.
  - `REASON_MEMORY_LIMITER`, and `REASON_OTHER` with
    "MemoryLimiter:AnonSwap". Round-1 A7 is still open on this: they are
    not said to count as resource deaths.
  - `REASON_FREEZER`.
  - plain SIGKILL on devices where low-memory kill reporting is not
    supported.

*Amendment:* write an explicit table from exit reason to action. Stamp
each process with `setProcessStateSummary(session, generation)`, so its
exit record identifies the session without relying on PIDs.

**R2-11. Medium. §2: the memory-pressure gate has no signal on API 34+.**
(Lens A.)

From API 34, apps no longer receive `TRIM_MEMORY_RUNNING_*`, `MODERATE`
or `COMPLETE`, and `onLowMemory` is never called
(https://developer.android.com/reference/android/content/ComponentCallbacks2).

*Amendment:* define the signal: each process's own RSS against a budget,
plus `ActivityManager.getMemoryInfo()` polled. Test it under induced
memory pressure.

**R2-12. Medium. §7: `health()` cannot be non-blocking with today's
locking.** (Lens B.)

- Locks are held across HTTP calls:
  - `waiting` (`serve.rs:2271-2285`);
  - `roster`, across `members()` (`:3225-3236`);
  - the trigger and scheduler locks, across `upsert_record`
    (`:3436-3450`).
- After a panic, an `unwrap()` on the poisoned lock panics again.

*Amendment:* publish a lock-free snapshot plus per-thread progress
counters, so that `health()` never takes a service mutex.

**R2-13. Medium. §2: removing Cranelift is feasible, but the ADR leaves
out how.** (Lens B.)

- The kernel is the only compile site (`runner.rs:217`, reached through
  `serve.rs:1770-1776` and `main.rs:408`). Agent programs are Python.
- What the build needs:
  - `default-features = false` with an explicit feature set;
  - a crate feature that gates `Cage::new`, `Cache` and
    `EMBEDDED_KERNEL` (`runner.rs:181`, `210`);
  - a precompiler built with Cranelift;
  - one explicit engine `Config` (wasm features, tunables, memory
    reservation and guard size, trap mode). The defaults differ between
    the two builds, and a mismatch would surface as
    `IncompatibleArtifact`.
- `Component::deserialize` copies the artifact into anonymous memory, so
  the whole artifact counts toward PSS.

*Amendment:*

- Specify the feature set and the shared `Config`.
- T1 asserts that
  `cargo tree --target aarch64-linux-android -i cranelift-codegen` is
  empty.
- T8 budgets the artifact copy.
- Change "where supported" to "must".

**R2-14. Medium. T8: PSS cannot be sampled every 30 s as written.**
(Lens A.)

Since Android 10, `getProcessMemoryInfo` is rate-limited, and calling it
faster returns the previous data.

*Amendment:* each process samples itself (`/proc/self/smaps_rollup`), and
`:bao` reports its numbers over Binder. The sampler's own CPU counts
against the idle budget.

**R2-15. Medium. T7: no attribution rule, and "final reply" is
undefined.** (Lens C.)

- An outage of the provider, the budget-capped key, the sync node, the
  ISP or the sender voids an 80 h run, and nothing diagnoses which.
- "Final reply" is not separated from status and failure bubbles, or from
  mailbox injection (ADR-009:261-262, 316-318, 360-363).
- Duplicates can arrive after 180 s.

*Amendment:*

- Define a reply as: bao is the author, it carries the nonce, and it has
  a completion marker.
- Measure latency on the sender's clock.
- Watch the chat until the soak ends plus 1 h.
- Classify each failure with independent evidence. A pass means zero
  failures attributable to the host.
- Size the key budget at no less than twice the projected spend.

**R2-16. Medium. T4, T7, T10: the desktop side is not documented.**
(Lens C.)

These tests need a desktop bao on the throwaway account, next to the
user's real desktop. That requires:

- an `any` binary at the same tag as the phone's AAR. An older server
  silently lists zero objects (`docs/environments.md:15-19`).
- a second `any` and `anyrt`, with a control port other than 7010
  (`runtime/src/config.rs:213`) and its own `traces_dir` (`:215`). A bind
  failure on that port is not fatal, so a clash would go unnoticed.
- repo-account slots for the test overlays (`docs/environments.md:35-37`).

*Amendment:* add an `android-poc` row to `docs/environments.md`, with the
binaries, ports, data dirs, repo account and config, and the host that
sends the T7 probes.

**R2-17. Medium. §1, §5: Sentry in the PoC variant.** (Lenses B and C.)

- **PROD reporting.** `SENTRY_ENABLED` is true for every build type
  (`any-kotlin/app/build.gradle.kts:104`). A non-debuggable build reports
  as PROD (`any-kotlin/app/.../di/AppModule.kt:33`), with Timber INFO and
  above sent as breadcrumbs (`CrashReportingModule.kt:27`). T3/T5's
  deliberate crashes would land in PROD Sentry.
- **Sentry in `:bao`.** `Application.onCreate` also runs in `:bao`,
  including `crashReporter.init()` (`AnytypeApplication.kt:63-65`). Sentry
  Android's NDK signal handlers would then sit beside Wasmtime's
  (unverified).
- **The gating is small.** Return early when the process name ends in
  `:bao`, and turn the five injected fields into `dagger.Lazy`: about
  20–40 lines. Content providers without `android:process` are created
  only in the default process, so the ADR's concern about providers is
  mostly moot.

*Amendment:*

- Set `SENTRY_ENABLED = false` for `baoPoc`, or use a dedicated DSN with
  no Timber.
- `:bao` initializes nothing.
- A test checks that `/proc/self/maps` in `:bao` contains neither
  `libgojni` nor `libsentry`.
- T11 asserts there is no telemetry traffic.

**R2-18. Medium. §6: the faults T3–T5 inject have no seam on a
non-debuggable phone.** (Lenses A and C.)

- The shell cannot signal the app's processes.
- `force-stop` and Task Manager are user stops, which by design disable
  restart.
- T3 and T4 need injected panics, broken Binders, election errors and a
  missing bootstrap.
- T11's prune permanently retires that device's peer, and no test order
  is set.

*Amendment:*

- Add a `baoPoc`-only fault injector, for example a receiver guarded by
  `android.permission.DUMP`, which the shell holds. T1 proves it is absent
  from release.
- Or cover those arms with integration tests against a fake server.
- Run T11's prune last, or reinstall after it.

**R2-19. Low–medium. §4, T5: the crash breaker against the system's
restart backoff.** (Lens A.)

- The system backs off sticky restarts after repeated crashes, roughly
  1 s and then ×4 per quick crash (unverified).
- A death in `Application.onCreate` or in Hilt never reaches the
  admission code, so the breaker never trips.
- "With working OS restart delivery" means T5 can never fail on a missing
  restart.

*Amendment:*

- Read the breaker marker before Hilt starts.
- When it has tripped: promote, `stopSelf`, return `START_NOT_STICKY`.
- T5 fails when `dumpsys activity services` shows no scheduled restart.

**R2-20. Low. T7, T10: the soak does not record whether the phone was
really on charge.** (Lens A.)

- A Mac's USB port may stop supplying power while the Mac sleeps
  (unverified), which would turn T7 into a battery run.
- Forced Doze over a USB adb cable still charges the phone.
- Wireless adb conflicts with the Wi-Fi-loss test.

*Amendment:*

- Use a wall charger for T7.
- Log the plug state, `isDeviceIdleMode` and power-save into the T9
  evidence. Any unplug invalidates T7.
- For T10, run `force-idle`, then pull the cable.

**R2-21. Low. §3: a lapsed wake lock cannot recover.** (Lens A, the
residual of A6.)

If the lock lapses and the CPU suspends, nothing wakes the supervisor,
because §4 bans alarms.

*Amendment:* allow one inexact `setAndAllowWhileIdle` backstop (Doze
permits about one every 9 minutes per app). State that it serves
recovery, not defeating a user stop.

### Checked and found correct in the revision

- The two-process importance model holds. A process hosting a service is
  never less important than its most important client, so with a plain
  `BIND_AUTO_CREATE` binding from main's foreground service, `:bao` is
  neither cached nor frozen.
  - "Without waiving importance" correctly means no `WAIVE_PRIORITY`,
    `ALLOW_OOM_MANAGEMENT` or `NOT_FOREGROUND`.
  - `BIND_ABOVE_CLIENT` would be wrong: it would get main and Go killed
    first.
  - Consider `BIND_NOT_PERCEPTIBLE`, so the low-memory killer takes the
    worker before the process that holds the database.
- `REASON_PACKAGE_UPDATED` exists only from API 34, which justifies the
  API 34 floor. The Android 17 memory limiter is documented
  (https://developer.android.com/about/versions/17/behavior-changes-all#app-memory-limits).
- **Epochs.** Ticking only while stores are live fits the epoch-deadline
  use in `runner.rs:307-322`.
- **Memory limits.** "Aggregate across its memories" correctly calls for
  a custom `ResourceLimiter`, because `StoreLimits` limits each memory
  separately.
- **Device name.** The server seeds the name "localhost" from the
  hostname (`any/internal/server/engine.go:356-365`), so §5's
  blank-or-`localhost` rule is right.
- **Backup exclusion.** A per-variant device-transfer exclusion is
  feasible: `app/src/baoPoc/res/xml/data_extraction_rules.xml` overrides
  main's file. It must exclude `root`, `file`, `database`, `sharedpref`
  and `external`, plus the `device_*` domains.
- **House rule.** ADR-034 is the only Proposed ADR, so the README's
  one-in-review rule holds.

### Round 2 verification log

**Re-read in the source:**

- **R2-1:**
  - `any/mobile/android/mobile.go:73-78`;
  - `any/internal/server/engine.go:545-578`;
  - `any/internal/embedded/embedded.go:366-378`;
  - `AppCoordinator.kt:571-576`;
  - `AnyRuntimeImpl.kt:296-306`.
- **R2-3:** `repos/_agent/programs/llm@v1/program.py:1120`, `1238`.
- **R2-4:** `runtime/src/broker.rs:2620-2636` (a guest `http.get` to
  `127.0.0.1` runs live) and `1800-1812`.
- **R2-5:** `BaoDevices.kt:110-133`.
- **R2-6:** `any/internal/server/handlers_meta.go:168-208`.
- **R2-7:** `runtime/src/serve.rs:2807-2832` and
  `runtime/src/triggers.rs:167-180`.
- **R2-17:** `app/build.gradle.kts:104` and `AppModule.kt:28-36`.

**Taken from the reviewers, not re-verified:**

- the `ServiceConnection` respawn wording (two reviewers cited it
  independently);
- the API 34 `ComponentCallbacks2` change;
- the `getProcessMemoryInfo` rate limit;
- the meanings of the `ApplicationExitInfo` reasons;
- the sticky-restart backoff numbers and the freezer delay;
- that ureq 2 cannot cancel a request in flight;
- the Wasmtime feature names;
- Sentry's NDK handlers;
- the 25 s keepalive;
- the 2 min hold on bundle creates;
- the `techspace.go` sync-status route;
- the `environments.md` rows;
- the `ServerBootstrapMonitor.kt` lines;
- the `serve.rs` lock sites in R2-12;
- `triggers.rs:623-650`.

**Side note.** any-kotlin's `devpass` skill names the phone "A063", while
the ADR names a Phone (2a). Resolve which device it is in the device
inventory, since every T-result is device-specific.

## Round 3 (second revision, 2026-11-02)

Scope: the ADR as revised after round 2 (780 lines), and the author's
round-2 response in the notes (`docs/android-host-poc-notes.md:170-265`).

Method: a coordinator pass, not three new lens reviewers. The coordinator
checked every round-2 disposition against the revised ADR text. It then
verified, in the source, the claims the new mechanisms depend on (log at
the end). A full three-lens round can still run before acceptance; the
verdict explains why that is not the best next step.

### Round 3 verdict

**Acceptable in shape. Accept it in two stages.**

- All 21 round-2 findings are resolved in the specification. The small
  residuals are listed below, and none of them blocks acceptance.
- One new medium design gap, R3-2: what a phone does after it loses the
  claim.
- The main risk is no longer a design defect. It is the plan (R3-1).

### Round-2 dispositions, judged against the revised text

| ID | Verdict | Residual |
|---|---|---|
| R2-1 | Resolved | What the user sees when main is killed during a logout is unspecified (R3-4). |
| R2-2 | Resolved | — |
| R2-3 | Resolved | — |
| R2-4 | Resolved | The egress rule names the wrong source for its route table (R3-6), and it also removes LAN capabilities (R3-5). |
| R2-5 | Resolved | The state after the phone loses the claim to another device is undefined (R3-2). |
| R2-6 | Resolved | "Fresh" sync evidence is undefined (R3-3). |
| R2-7 | Resolved | — |
| R2-8 | Resolved | — |
| R2-9 | Resolved | — |
| R2-10 | Resolved | — |
| R2-11 | Resolved | — |
| R2-12 | Resolved | — |
| R2-13 | Resolved | The feature allowlist is valid for v49.0.3 (see "Checked"). Whether the profile loads this kernel is still T1's question, as the ADR says. |
| R2-14 | Resolved | — |
| R2-15 | Resolved | Every external outage costs a full new 80 h run (see R3-1). |
| R2-16 | Resolved | The row exists at `docs/environments.md:31`, but nothing is provisioned yet. |
| R2-17 | Resolved | — |
| R2-18 | Resolved | — |
| R2-19 | Resolved | — |
| R2-20 | Resolved | — |
| R2-21 | Resolved | — |

### New findings

**R3-1. High (the plan, not the design). §6, §7: the PoC is
all-or-nothing, and its cheapest feasibility questions are not answered
first.**

- The ADR grew from 235 to 780 lines over two review rounds. Much of that
  growth answered this review's findings, so the review shares
  responsibility for it.
- To pass, the PoC now needs:
  - a two-process host with a Binder protocol;
  - journaled account transitions across a fresh process;
  - a broker egress firewall;
  - a new election policy and a new beat type;
  - scheduler lanes with deferred admission, and a memory admission
    controller;
  - a fault-injection receiver;
  - an engine profile and an AAR pipeline;
  - a desktop fixture;
  - an 80 h soak that restarts in full after any external outage.
- "T1–T11 incomplete means PoC incomplete" makes acceptance
  all-or-nothing. There is no go/no-go point before that whole
  investment.
- The questions most likely to end the project are cheap, and come first
  in dependency order:
  1. Does a Wasmtime 49 build with this feature allowlist and
     `EngineProfileV1` load the precompiled CPython artifact in an Android
     app process and run it? This is part of T1.
  2. Does the worker fit its memory budget during a real turn? This is
     part of T8.
  3. Does one real bao turn complete on the phone with the test keys?

  None of them needs the host service, election, the journal or the egress
  firewall.

*Amendment:* split acceptance into stages, each with its own go/no-go.

- **Stage 0, a native spike.**
  - Scope: §2's artifact, engine-profile and feature decisions, and a
    minimal `:bao` worker started from a debug screen on the throwaway
    account.
  - Gate: T1's artifact and dependency checks, plus a T8 memory sample
    during one turn.
  - Leave out: the foreground service, election, the journal and egress.
    Run Stage 0 only when the device inventory shows no Any app on a real
    account.
  - Kill criteria: the artifact does not load, an unexplained signal
    fault, or a memory budget that does not fit.
- **Stage 1, the always-on host:** everything else, gated on Stage 0.

Accepting Stage 0 now satisfies the README rule (no code ahead of an
accepted ADR) without committing to the rest.

**R3-2. Medium. §5: a phone that lost the claim can become the winner
again without any new claim.**

- The server computes the winner as the highest-`seq` claim among rows
  that carry `apps.bao` (`any/docs/23-devices.md:62`). Removing a row, or
  its app, forfeits that row's claim (`:74-76`).
- Scenario:
  1. The phone is activated.
  2. Later, the user claims on the desktop. The phone sees another
     winner and stops dispatching, but it keeps `apps.bao` and its older
     claim.
  3. The desktop's row is then pruned, or loses `apps.bao`. The phone's
     older claim wins again.
  4. §5 lets an activated phone dispatch whenever `active.bao` is self,
     so the phone starts answering.
- The ADR defines PreparedUnregistered and Active, but not this third
  state: registered, but not the winner. The result is a silent failover,
  which §5 says does not exist ("no liveness lease/failover").

*Amendment:* when the phone observes another winner, drain and release
`apps.bao`, returning to PreparedUnregistered, so a later fallback cannot
revive the phone. Or state the fallback as intended behaviour and test it
in T4.

**R3-3. Low. §5: "fresh" sync evidence is undefined.**

Both sync-status responses carry a last-sync timestamp
(`any/internal/api/sync_status.go:26`, `:35`). An idle tech space can read
`synced` with an old timestamp. If "fresh" means a recent `lastSyncedAt`,
an idle phone never reopens dispatch.

*Amendment:* define fresh as "fetched within this sample window, under an
unchanged lease". Do not key on `lastSyncedAt` or `lastSyncAt`.

**R3-4. Low. §4: what the user sees at the fresh-process boundary.**

Logout kills main while the user is looking at the app. Android may
relaunch the top Activity in a new process right away, which would then
run the journal (not verified on this phone). Or the app may simply
disappear.

*Amendment:* say what the user sees. T3 checks the flow end to end: no
crash dialog, a recovery or progress screen, and the operation completing
in the fresh process.

**R3-5. Low. §2, §5: the egress rule also removes LAN capabilities.**

The rule denies private and link-local destinations to every guest
effect, MCP included. That makes LAN MCP servers (ADR-032) and model
endpoints hosted on the LAN unusable. This is a reasonable PoC limit, but
§2 does not list it with the unsupported capabilities.

*Amendment:* list it in §2. Make readiness reject a configured provider or
MCP server whose endpoint the egress rule would deny, so it fails at
activation and not in the middle of a turn.

**R3-6. Low–medium. §5: the "vendored route classifier" is not a security
boundary.**

- What exists today is `runtime/src/routes.rs`, which classifies effects
  as read or mutate for mocks and replay. It works by:
  - suffix matching over a naive `split_url` (`routes.rs:17-24`);
  - an "is this Any" test that falls back to a `/v1/spaces` prefix
    heuristic when no base URL is set (`:33-39`).
- It does no percent-decoding, userinfo handling or path normalization.
- If the egress allowlist reuses this classifier for the lease origin, an
  encoded or oddly shaped path could reach a denied device route.
- §5 already requires canonicalization. Only the named source is wrong.

*Amendment:* build the egress route table separately, with a real URL
parser and an explicit allowlist. Do not reuse `split_url` or the
heuristic fallback. Add encoded-path variants of the denied routes to T11.

### Checked and found correct in this revision

- **Device registry semantics.** `PUT /v1/devices/me` writes only the
  fields present, and `apps` merges per slug (`any/docs/23-devices.md:115`).
  So §3's name-only upsert cannot drop `apps.bao`. `activate` restores
  `apps.<app>` itself (`:116`), as §5 says.
- **API fields and routes.**
  - `/v1/account` returns `techSpaceId` (`any/internal/api/account.go:13`).
  - A space carries `spaceIndexObjectId` (`spaces.go:87`).
  - A space's sync status carries `networkPeers` (`sync_status.go:22`).
  - The sync-status routes exist on the tech space
    (`any/internal/server/techspace.go:108-110`).
  - A space can read `synced` with zero network peers
    (`any/docs/03-api.md:4063`), so requiring `networkPeers > 0` is right.
- **Wasmtime features.** The allowlist is valid for v49.0.3:
  - Android: `anyhow`, `runtime`, `std`, `component-model`, `async`,
    `backtrace`. Precompiler: `cranelift`, `all-arch`,
    `parallel-compilation`.
  - `wasmtime-wasi`'s `p2` needs `wasmtime/component-model` and
    `wasmtime/async`, both on the list. No WASI feature pulls in
    `cranelift` or `wasmtime/default`.
  - Version 49 has no `signals-based-traps` crate feature; signal traps
    are engine configuration, so nothing is missing from the list.
- **`AMPLITUDE_ENABLED`** already exists as a build-config field
  (`any-kotlin/app/build.gradle.kts:110`).
- **Desktop fixture.** The proposed `android-poc` row is in
  `docs/environments.md:31`.
- **Upstream-fix rule.** §7's dated exception refers to a real rule:
  `CLAUDE.md:38` ("`any`-server quirks are fixed upstream").

### Round 3 verification log

**Re-read in the source:**

- `any/docs/23-devices.md:25-131`;
- `any/internal/api/sync_status.go:1-40`, `account.go:13`, `spaces.go:87`;
- `any/docs/03-api.md:332-338`, `4044-4063`;
- `any/internal/server/techspace.go:100-115`;
- `runtime/src/broker.rs:1085-1100` and `runtime/src/routes.rs:1-45`;
- `docs/adr/025-bao-status-pubsub.md` (one publisher, offline after 30 s);
- `docs/environments.md:31`;
- `CLAUDE.md:38`;
- `any-kotlin/app/build.gradle.kts:110`.

The `[features]` sections of Wasmtime's and wasmtime-wasi's v49.0.3
`Cargo.toml` were read on GitHub.

**Not checked:**

- ADR-025's rules for a second beat type published by an unregistered
  device;
- what Android does after `killProcess` on a foreground app (R3-4);
- whether reference types work without the `gc` crate feature (T1).
