# Android host for bao: overview for review

2026-11-09. This is a short entry point to [ADR-034](adr/034-android-agent-host.md), which is the full contract (about 1,600 lines). It states why the work exists, what was decided and why, and what changes in anybao. Status: Stage 0 is measured; Stage 1 is in progress on a throwaway account. Nothing is merged.

## Why

The goal is a bao that stays available when the user's laptop is closed. The target is a dedicated Android phone: always on a charger and on Wi-Fi, screen off, running the existing Anytype Android app (any-kotlin) with bao hosted inside it. Desktop and phone share one account. Only one device answers at a time, chosen by the existing ADR-015 device registry.

The phone already has what bao needs: login, synced spaces, chat and credentials come from the app's embedded Go `any`. What it lacks is `anyrt`. This project embeds `anyrt` (runtime and guest kernel) in the app and runs bao there.

## Stage 0 result (measured on a Nothing Phone (2a), K1–K7 GO, 2026-11-05)

- The anyrt kernel runs on ARM64 with no compiler on the device: a precompiled artifact, loaded by a deserialize-only Cage.
- 10 of 10 real model turns succeeded.
- An 8-hour, screen-off, charging soak completed 32 of 32 fixture turns within 120 s each, with no process replacement and no wake-lock lapse.
- Memory fit: combined PSS peaked at 678 MiB.

## Architecture in one picture

```text
Android app (same APK, opt-in "baoPoc" variant, hosting off by default)
├── main process: UI + Go any server + BaoHostService (foreground service)
│     Kotlin policy owner: device registry writes, user intent, release,
│     memory policy, worker supervision
│        │  Binder: Start / Activate / SetAdmission(Open|Close) / Release / Stop
│        │          callbacks: Accepted / Status / AdmissionApplied / Released / Drained
└── ":bao" worker process: anyrt (Rust, JNI) — no Go, no UI, no telemetry
      managed runtime: preparation → takeover → execution (lanes, dispatcher,
      chat watch, event sources, beats), every HTTP call through a gated transport
```

## Key decisions and why

| Decision | Why | Rejected |
|---|---|---|
| **Rust runs in a separate `:bao` process.** The main process owns the foreground service and Go. | Blocking `ureq`/DNS calls cannot be cancelled. Stop gets a 10 s grace, then the worker process is killed and death is confirmed. Rust crashes and OOMs cannot take down Go, the user's database or the UI. Each process has its own memory budget. | Rust inside the main process: a stuck call would mean killing the whole app, and a native abort kills Go. A separate APK: duplicate onboarding and identity. Termux. |
| **Kotlin main is the policy owner.** Rust only executes when granted. | The process that holds the device claim must outlive the worker, otherwise a dead worker leaves bao silent behind its claim (BOB-125). Main records an owed release before stopping the worker and retries it after a crash. | Running ADR-015's `election::boot` and its claim thread in Rust. |
| **Same election as desktop.** The registry is `/v1/devices` and the winner rule is server-side. | Nothing about who wins is reimplemented. Switching away from the phone from any device works as today. | — |
| **The phone claims only on explicit "Use this device" on the phone.** It never auto-claims; desktop does when it is the only bao. | A crash, update or restart must not silently take bao from the desktop. | Desktop's auto-claim and its "disabled-active" fallback. |
| **AppliedOpen comes only after a checked takeover.** The takeover writes the chat-watch record, the standing triggers with `nextDue` and the declared credential descriptors, and reads each one back before any dispatch. The grant expires on a calibrated boot clock. | It narrows the ≤10 s double-answer window (BOB-143), and it lets a stale or frozen worker stop by itself. | Desktop's optimistic flip, with unchecked takeover writes. |
| **Gated host transport:** an exact method/path/body allowlist per phase (Preparation vs Execution). Rust makes no registry or auth writes, and never writes `hosts` or `value` on secret rows. Guests keep the separate E egress allowlist. | The phone worker must not be able to provision, elect or mutate beyond what the phase needs. | Running `serve::start` behind a flag: it provisions spaces, stores and secrets and claims on boot. |
| **Frozen content.** The overlay programs and skills are pinned by fingerprint and verified at preparation. A drifted or unsynced pin means NeedsSetup. There is no on-device compiler and no cache. | The PoC must run exactly what was measured. | Live resolution as on desktop. |
| **Desktop behavior is the baseline** (user ruling, 2026-11-06). The phone adds hardening only where it is structurally needed: process split, bounded stop, owned threads, protocol semantics, the gate. Every stricter-than-desktop item was a decision for the user, and all nine took desktop parity: no replay marker, follow-ups as desktop, retention hourly, and so on. | The review loops kept adding phone-only machinery that desktop doesn't have. | A per-message replay ledger, a post-after-revocation fence, a capacity-fault close, and so on. |
| **Memory:** Kotlin enforces from Android's own signals (`lowMemory`, free RAM). Backstops are sized from device RAM: main 30 %, worker 40 %, combined 75 %. The worker only reports its RSS. | It suits a dedicated phone; fixed MiB limits from planning were never measured. | A worker-side self-gate. |
| **Traces** are device-local (ADR-023), with desktop's retention cadence. | Same store model as desktop. | — |

## Reuse vs desktop

The engine is shared unchanged: broker, runner/Cage, `triggers::{Scheduler, Trigger, Watcher, …}`, tracestore, the `any` client and stores.

The desktop helpers moved verbatim into shared modules. Desktop `serve` tests are unchanged (51/51) and the line-fidelity check is clean:
- `chatwatch.rs`, `presence.rs`, `stores.rs`;
- credential-request posting, chat follow-ups, the interrupted-turn record, the chat-watch release, the run summary, event-target selection.

The new code is the orchestration loops over those primitives: dispatcher, chat source, event sources, stand-down, beats. It is new because `serve::start` provisions and elects on boot, desktop standby still fires device-pinned triggers, and desktop leaves detached threads at stop. ADR-034 §5 says "today's `serve::start` must be split, not merely run behind a gate". The design's §2 gives a per-function reuse table: [s1-native-n2b-2-design.md](plans/android-host/s1-native-n2b-2-design.md).

## What changes in anybao

| Area | Change | Desktop effect |
|---|---|---|
| Build | `desktop` default feature keeps the compiler, cache, control API and loopback OAuth. A `--no-default-features` build targets Android: no cranelift, a deserialize-only Cage. Adds `build.rs`, `precompile.rs`, `engine_profile.rs`. | None by default |
| New modules | `managed/` holds the owner, session, worker protocol mirror, grant fence, admission/lanes, takeover, preparation, frozen pins, gated transport and execution. Also `guest_egress.rs` (E allowlist), `readiness.rs`, `stores.rs`, `chatwatch.rs`, `presence.rs`, and `stage0*` (the measured spike). | None |
| `serve.rs` | Helpers extracted to shared modules, bodies byte-identical. | Behavior-identical |
| `runner.rs` | Store permit/limiter hook, `run_program_with`, interrupt plumbing. | Same behavior |
| `broker.rs` | The managed broker factory (OAuth, consent and shell refused). Pinned-program refusals counted. The partial secrets-guard fix (Step A). | Step A changes desktop on purpose |
| `anyapi.rs`, `tracestore.rs`, `triggers.rs`, `resolver.rs`, `routes.rs` | Stream line cap; `publish_run_summary`; `RunResult::failed` and `event_targets`; frozen resolver / `NotPinned`; classifier visibility. | Same behavior |
| ADRs | ADR-034 is new. Reciprocal pointers in ADR-003/006/009/011/015/018/019/023/025. | Docs |
| Upstream `any` | Prerequisite before any real account: an unpredictable per-generation bearer token on **all** HTTP routes. Today any app on the phone can reach the loopback API. | — |

## State of Stage 1

| Part | State |
|---|---|
| Kotlin policy owner (L) and host skeleton (B0/B1, H1) | Implemented and reviewed in `any-kotlin` (isolated checkout) |
| Native preparation, protocol, session, takeover, lanes (N1–N2b-1, Step B) | Done. Each step was reviewed through four lenses; the last Step B correction round came back clean from four Codex lenses. |
| N2b-2 execution service, round A (shared extractions + core) | Done; four lenses clean |
| N2b-2 round B (chat source, stand-down, event sources, retention) | Written; checks and review pending |
| N2b-2 round C (production wiring, end-to-end tests) | Next. Production cannot reach Open until then. |
| Device qualification (80 h soak, takeover, Doze, reboot) | Not started for Stage 1 |

Known Kotlin prerequisites before this runs on a device:
- L must reset its quiescence evidence per activation.
- An expired Release must retire the worker without its deadline being extended.

The broker secrets-guard bypasses (A-1…A-4) are out of this track and get hardened before the public release.

## Questions for the anybao owner

1. Is the process model and policy split acceptable for anybao: a separate worker, Kotlin as policy owner, and Rust as a managed runtime with no election?
2. Two orchestration loops (desktop `serve`, phone `managed`) over a shared engine: OK for now? Long term we would rather move desktop onto the managed loop (stricter stop and reporting) than the reverse.
3. Is the `desktop` / no-default Cargo feature split the right seam?
4. Is the gated-transport allowlist the right long-term shape, given the planned API keys restricted to spaces?
5. The ADR number collides: 034 is also used by the client-bus draft (#112). Should we renumber this one (036 is free)?
6. How should it land? This branch carries only the Android work on top of `main`. It does not use the ChatGPT-OAuth code; it rejects ChatGPT-local grants as unsupported. Our proposal is to split it into one-topic commits, in this order: the build split, then the shared extractions, then the managed runtime.
