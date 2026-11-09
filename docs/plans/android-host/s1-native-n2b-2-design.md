# Stage 1 R/L-native — N2b-2 design: the managed execution service

> **Revision 3 (2026-11-06), scope-adjusted.** Revised after the round-2
> design review ([results](reviews/s1-native-n2b-2-design-r2-review-results.md),
> E1–E21), the user's rulings of 2026-11-06 (`user-follow-ups.md`: replay
> exclusion = option B, the per-chat high-water mark; memory = Kotlin main
> enforces, the worker reports; the separate `:bao` process stays) and the
> user's scope guidance of the same day: **the baseline is desktop
> behavior**; stricter-than-desktop behavior is built only where the phone
> structurally needs it (the `:bao` process and its bounded stop/kill, owned
> and joined threads, the Kotlin worker protocol's own semantics, the
> takeover-phase race, the gated transport); everything else stricter than
> desktop — including what an ADR-034 clause currently asks for when
> desktop works without it — is a **decision for the user** (§10) and stays
> out of the design body. r2 is frozen at
> `/private/tmp/anybao-s1-native-n2/n2b2-design-r2-review/frozen/design.md`
> (SHA-256 `5cd2e76a…c7b`). Every finding was re-verified on the live code;
> dispositions are in §12 (round 2) and §11 (round 1). Still phase 1: no
> `runtime/` edit, no ADR edit, no Cargo. Provenance: Claude Code subagent,
> Claude Fable.

What the scope guidance did to r2/r3 (details in §13):

- **Removed from the body** (now rows in §10, "pending user decision"):
  the post-time chat fence and "never a chat post after revocation" (R2);
  the capacity-fault session close with its fence invalidation and latch
  (R3); grant/gate/fault checks at injection beyond the watch-connection
  check (R4); the once shot persisted consumed before its run (R5); the
  60 s periodic skill re-verification (R6); retention confined to the Open
  phase (R7); the `unsupported_source` marking of foreign-space sources
  (R8); cancelling live runs on a revoking *Close* as distinct from Release/
  Stop (R9). The replay high-water mark (R1) is designed, self-contained
  and removable, and is a row too: the user chose it believing desktop had
  it; desktop does not.
- **Removed as machinery, by desktop parity**: the Writer thread and its
  write kinds (desktop does its bookkeeping inline on the run thread and the
  ticker, `serve.rs:2152-2270`, `:2596-2597`); the chat backlog/descriptor
  and the event deliveries park (desktop's chat watch spawns a run directly
  and events fire inline, sequentially, `:2342-2346`, `:2759-2768`); the
  uncertain set; the per-activation obligation records (one `owner_clear_due`
  flag suffices).
- **Kept as structural**: the gated transport and the session's sole
  authority over its phase (D1, E10), the takeover-phase ordering (E5),
  quiescence counted through the report's bookkeeping (D2, E6), interrupting
  live runs on Release/Stop (the protocol's bounded quiescence), owned
  threads for every desktop detached thread (soft-break deadline, event
  sources, the chat watch), permit-before-consume for lane dispatch (the
  lanes exist, N2a), the in-memory once/cron reservation while reconcile
  runs on another thread (D3/E9, the in-memory half), the RSS report
  (ruling), the turn-record and subscribe gate rules.

Inputs: ADR-034 (amended through 2026-11-06), ADR-005 §3, ADR-006 §4,
ADR-015 §3, ADR-017 §2, ADR-018 §2/§3, ADR-023 §6, ADR-025 §1/§2; the Step B
corrections round-4 gate; the N2b-1 and Step B reports; the Kotlin contract
(read only). Line numbers are against the tree at `bf6900d` plus the
uncommitted Android work.

## 0. The user's question, answered first

> Why do we need to reimplement a lot of runtime like scheduler, dispatch,
> chat services for Android? Why can't it just use existing Rust code for
> desktop?

It does use it. Everything that *computes* is shared: the broker, the runner
(`runner::run_program_with`, `runner.rs:571`), the cage, the trigger model
(`triggers::{Scheduler, Trigger, RunResult, reconcile_registry, health_pass,
desired_event_sources, EventSource, event_args, Watcher, LiveRun,
standing_triggers, owned_chat_watch}`), the trace store, the `any` client,
the stores, `chatwatch.rs` and `presence.rs` (moved verbatim out of
`serve.rs` in N2b-1). The r3 body transposes desktop's orchestration onto
owned threads and lanes; it adds behavior only where one of these forces it:

| Desktop fact (`serve.rs`) | Why the phone cannot run it as is | Clause |
|---|---|---|
| `start` provisions on boot (`:1095-1360`) | The worker may create device-local collections and nothing else before Open | ADR-034 §5 |
| Standby still fires device-pinned triggers (`trigger_ticker`, `:2507-2598`) | Nothing fires without main's grant (the protocol's AppliedOpen) | §5, §7 |
| Detached threads: one per conversation (`:2118`), per event source (`:2677`), per soft break (`:2018`); `stop` joins only the service threads (`:877-887`) | Every thread owned, cancellable, joined within the 10 s grace; exactly-once reports | §4 |
| The ticker consumes a once/cron before the run and runs it inline (`:2583-2597`) | The lanes (N2a, built) dispatch under a permit; consumption after the permit so a deferral mutates nothing | §2 |
| Every HTTP call is the plain client | The gated host transport | §5 |
| Stand-down lets in-flight runs finish (`:2822-2823`) | Release/Stop must reach quiescence inside main's deadline, else the worker is killed | §7 |
| Replay after a mid-turn death: the answered-cut (`snapshot_backlog`, `chatwatch.rs:65`) + the watcher's in-memory seen-set, **no durable started marker** — a desktop that dies mid-turn re-answers the message at its next boot | Not structural: the mark is a user decision (§10 R1) | §5 |

## 1. What N2b-2 builds on (the code as it is now)

Unchanged from r2/r3-draft (verified again for this revision): the owner
(`managed.rs`: `Task` ×8 `:266-278`, `spawn_owned` `:489-510`,
`stand_down`/`reopen` `:428-441`, `interrupt_all` `:443-463`, `health()`
`:712-775`, the finite-exit rule `:301-305`/`:560-565`); the lanes
(`lanes.rs`: `EpochDriver` `:33-63`, `Lanes::spawn`/`InboxGuard`/
`Completion`/`RunSlot` `:378-606`, `release()` before `report` `:595-597`,
the take→register gap `:564-575`, `offer` refusals `:442`, acceptance behind
a reporting worker `lanes/tests.rs:334-345`, `inject`/`soft_break_live`
`:184-203`, `hand_on` `:253`, `Backlog` `:209-238`); admission
(`admission.rs`: `LanePool`/`GateCause` `:38-234`, `consume_scheduled`
`:289-307`, `PendingQueue` with heartbeat priority `:411-457`); the service
(`service.rs`: `PreparedService` `:276-393`, `broker()` `:366-392`, `impl
LaneRunner` `:397-468`; the permit drops at `runner.rs:643` before the dump
at `service.rs:437`); the runner's final-outcome rule (`runner.rs:607-610`);
the transport allowlist (`transport.rs:1-38`, `:310-317`); the guest's reach
of every `/v1/local/*` op (`fixtures/android-guest-data-routes.txt:25-39`,
`guest_egress.rs:117-123`) and the broker's `trace_*` write guard
(`broker.rs:2357-2375`); `traces_dir` created at preparation
(`preparation.rs:503`); the session (`session.rs`: `Execution` `:146-162`,
`install_execution` `:382-387`, `ready_at` `:416-423`, `quiescent` `:427-429`,
`open` `:823-881`, `spawn_pending` `:889-908`, `finish_open` `:966-1004`,
`close_locally` `:1011-1026`, `release` `:1028-1061`, `check_release`/
`check_stop` `:1068-1167`, `check_grant` `:1171-1188`, `tick` `:1190-1232`,
`SharedOwner` `:1349-1459`); the fence (`grant.rs`: `activate` `:158-175`,
`begin_open` `:178-210`, `close`/`release` `:264-296`, `permits_dispatch`
`:303-324`); the takeover (`takeover.rs`: `run` `:117-138`,
`stamp_or_seed_chat_watch` `:259-315`, `TakeoverOutcome` `:107-114`); the
triggers (`triggers.rs`: `runnable`/`cron_due`/`once_due`/`record_run`
`:106-191`, `reconcile_registry` `:235-310`, `health_pass` `:521-562`,
`record_to_trigger` `:564-603`, `EventSource` `:450-488`); the desktop chat
follow-ups (`serve.rs:1870-2000`, `:2152-2270`); production today (no
`Execution` installed, `ready` false, no Open).

## 2. Reuse table

Legend: **(a)** as-is; **(b)** verbatim move (N1 method); **(c)** extracted
core shared by both hosts; **(d)** managed-only, with the structural reason.
Hunks: exact `serve.rs` lines; none overlaps an unrelated uncommitted hunk
(reuse lens, twice).

| Desktop function (`serve.rs`) | Decision | Shared / different | Hunk and proof |
|---|---|---|---|
| `trigger_ticker` (`:2464-2601`) | **(d)** loop; **(a)** every computation | Shared: `reconcile_registry`, `health_pass`, `standing_triggers`, `desired_event_sources`, `Scheduler::{cron_due, once_due, advance_cron}`, `StorePermit::consume_scheduled`. Managed-only (structural): the due scan runs `cron_due`/`once_due` on the real registry entries exactly as the ticker does (`:2583-2591`) but hands the due ones to the `PendingQueue` instead of running inline, and consumes under the permit (§3.5); reconcile reads happen on Housekeeping because the dispatcher must stay off HTTP while the consumer applies Close/Open (§3.2); only while AppliedOpen. | none |
| `run_trigger_program` (`:2605-2621`) | **(a)** runner; **(c)** error fold | `RunResult::failed(error)` in `triggers.rs` (the `refused_programs` line at `:2618` preserved). | `serve.rs:2610-2620` → one call; `triggers.rs` +12. |
| `finish_run` (`:2627-2641`) | **(a)** `Scheduler::record_run`; **(d)** 8-line wrapper | Called from the lane thread's report callback for scheduled **and event** runs, as desktop (`:2597`, `:2767`); persists `persisted_record` (ADR §5 `nextDue`). | none |
| `start_or_inject` (`:2093-2286`) | **(d)** structure; **(c)** post-run follow-ups | `chatwatch::follow_ups(rr, agent_name, trace_ref, posted, deferrals) -> Vec<FollowUp>` + `follow_up_body(..)`. The managed lane thread performs them **inline, in desktop order, after obtaining `posted`** from the moved `post_credential_requests` — desktop parity; no fence (§10 R2). Managed-only: no thread per conversation; the live-run registry is `Shared.live_runs` + `RunSlot`; injects through the bounded `lanes::inject`; no program-miss deferral (frozen host). | `serve.rs:2152-2270` → ~30 lines. Proof: exact-body tests; `serve` 51 unchanged. |
| `watch_chat` (`:2288-2415`) | **(d)** loop; **(a)** helpers | `snapshot_backlog`, `records_in`, `resolve_reply`, `Watcher::{on_message, input, is_self_message}`, `preview`, `BREAK_GRACE`, `escalate_soft_break`. Managed-only: gated subscribe; per-frame exit on `!open ∨ !owns_chat ∨ cancelled` (= desktop's per-frame `answers_chat`, `:2298`); a `Start` acquires the Chat permit and offers a lane job instead of spawning (§4.1); a soft break is a deadline on the dispatcher instead of a sleeper. | none |
| `resolve_sender` (`:2432-2462`) | **(d)** not used | members/identity routes are not host routes (ADR §5); one-member PoC account ⇒ no-op (BOB-192). | none |
| `on_break` (`:2007-2022`) | **(a)** `escalate_soft_break` | Deadline evaluated by the dispatcher (owned), not a detached sleeper (§4 of the ADR). | none |
| `append_interrupted_turn` (`:2032-2087`) | **(c)** record + seq; **(d)** discovery | `chatwatch::interrupted_turn_record(..)`, `chatwatch::next_turn_seq(rows)`; the managed target is the setup-recorded `turns` address (the gate cannot admit runtime discovery). Written on the lane thread for `rr.status == interrupted`, exactly desktop's rule (`:2218-2235`). | `serve.rs:2069-2074`, `:2080-2084`. |
| `post_credential_requests` (`:1870-2000`) | **(b)** verbatim with parameters instead of `ctx` | `chatwatch::post_credential_requests(client, space, chat, secrets_obj, secrets_dataset, config, agent_name, refs) -> usize`; `is_model_setup`/`tier_identity` move with it. Every route it uses is allowed in Execution. No `allow` parameter (§10 R2). | `serve.rs:1840-2000` → wrapper. Proof: N1 fidelity script; the `model_setup_marker…` test follows. |
| `reconcile_event_sources` (`:2651-2679`) | **(d)** | Four owned slots (the fixed `Task` set is structural; desktop spawns unbounded detached threads); working-space sources only (the gate); shares `desired_event_sources`. | none |
| `event_source_thread` (`:2686-2738`) | **(d)** loop; **(a)** `EventSource::{seed, fresh}`, `records_in`, `CHAT_MESSAGES` | Desktop parity: records are handled **inline and sequentially on the source thread** (`:2722-2727`); the only difference is that each fire acquires the Event-lane permit (cancel-aware wait) and offers a lane job instead of calling `run_trigger_program` on the source thread — so `fresh` is called after the permit is held, which is ADR §2's rule for free. | none |
| `fire_event` (`:2744-2769`) | **(c)** `triggers::event_targets`; **(a)** `event_args` | Target selection shared; each target fired in order with `finish_run` bookkeeping on its lane thread (desktop: `:2759-2768`). | `serve.rs:2749-2758` → one call. |
| `election_thread` / `takeover` / stand-down (`:2774-2927`) | **(d)**; the one write **(c)** | ADR §5 forbids election; the takeover is N2b-1's. The stand-down is desktop's: backlog cleared, standing built-ins locally unowned, **the one record write** `chatwatch::release_chat_watch(client, space, obj, dataset, &Trigger)` (`:2845-2854`), performed by the dispatcher thread (outside `state`) once per revoking close of an open activation. | `serve.rs:2845-2854` → one call. |
| `presence_thread` / `beat_role` (`:694-812`) | **(c)** `presence_decide`; **(d)** thread | `presence_pass` keeps its signature; the managed envelope adds `data.sessionId` + `data.activation` (ADR §7). | `presence.rs` (untracked). |
| `answers_chat` (`:2931-2935`) | **(a)** | `owned_chat_watch` = `Execution::owns_chat()`. | none |
| `note_chat_start` (`:2939-2954`) | **(d)** | Three assignments + a persist, on the chat-watch thread at offer (desktop: `:2343`, same thread). | none |
| Retention (`:1452-1475`, `:1630-1639`) | **(a)** `TraceStore::expire`; **(d)** thread | `Task::Retention`, desktop cadence (60 s after boot, then hourly); the phase question is §10 R7. | none |
| `RunCtx::publish_run` (`:1605-1624`) | **(c)** `tracestore::publish_run_summary` | | `serve.rs:1605-1624` → 3 lines. |
| `RunCtx::broker` (`:1641-1680`) | **(a)** twin | `PreparedService::broker`. | none |
| `control_api` (`:2958+`) | **(d)** N/A | `ControlApi::Disabled`. | none |
| `sliced_sleep` (`:1478-1485`) | **(a)** twin | `TaskContext::idle_until` / `LanePool::wait_until`. | none |

**Tracked-file hunks (10):** `serve.rs` ×7 (`run_trigger_program`,
`start_or_inject` follow-ups, `append_interrupted_turn` ×2,
`post_credential_requests`+helpers, `fire_event`, stand-down write,
`publish_run`); `triggers.rs` ×2 (`RunResult::failed`, `event_targets`);
`tracestore.rs` ×1. The `health_pass` parameter and `EventSource::seen` of
r2 are gone (R8; no park). Untracked: `chatwatch.rs`, `presence.rs`,
`managed/*`.

## 3. ExecutionService

### 3.1 Module, threads, state

`runtime/src/managed/execution.rs` (+ `execution/tests.rs`; `execution/mark.rs`
only if R1 is kept). `Task` grows from 8 to **17 slots**: + `Service` (the
dispatcher), `Presence`, `Housekeeping`, `Retention`, `ChatWatch`,
`EventSource0..3`. Idle wakeups ≈ 7/s.

```text
ExecutionService:
  shared, service (Arc<PreparedService>), config (space, chat, self_peer, agent_name, addresses)
  lanes: Lanes
  gen: AtomicU64                 // close generation (§3.2)
  activation_gen: AtomicU64      // bumped by every open(Some(outcome))
  open, owns_chat: AtomicBool
  inflight: AtomicUsize          // jobs from bind until their report callback returned (§3.6)
  queue_len: AtomicUsize; owner_clear_due: AtomicBool; owner_clear_in_flight: AtomicBool
  retention_running: AtomicBool
  rss_bytes, rss_sampled_at_ms: AtomicU64   // reported only (ruling)
  word: AtomicU64                // packed execution snapshot for health()
  state: Mutex<State>            // one lock, never across HTTP; lane calls under it: offer, queue ops
State:
  grant: Option<(OpenGrant, CalibratedBootClock)>; session_id; activation
  registry: BTreeMap<String, Trigger>; scheduler: Scheduler (self peer, armed)
  watcher: Watcher (self_name = agent_name; `live` stays EMPTY)
  queue: PendingQueue            // scheduled work only (heartbeat priority, N2a)
  jobs: BTreeMap<run_id, InFlight>; reserved: BTreeMap<trigger id, run_id>
  chat_live: Option<run_id>
  breaks: Vec<(Instant, run_id)>
  sources: BTreeMap<(space, object), SourceSlot{slot, stop, g, state: EventSource}>
```

### 3.2 Authority, generations, lock order (structural)

**Transport phase** — the session alone writes `PhaseGate`: `open()` sets
Execution before its takeover (`session.rs:863`); `close_locally` sets
`gate_return_pending` and flips nothing; `tick` flips Preparation when
`gate_return_pending ∧ pending.is_none() ∧ takeovers_in_flight == 0 ∧ stage
∉ {Opening, Open} ∧ execution.quiescent()` (D1). **Cleanup authorization**
(E10): when a revoking `close_locally`, `release` or `stop` makes the
owner-clear due while the gate is Preparation, the session sets
`gate(Execution)` again (routes only; admission stays closed) and
`gate_return_pending = true`.

**Takeover ordering** (E5): `spawn_pending` (`:889-908`) additionally waits
for `execution.standdown_idle()` (`owner_clear_due == false ∧
owner_clear_in_flight == false`) and is re-tried from `tick`; the session
calls `execution.release_owner_clear()` — which lets the dispatcher perform
the write — only when `takeovers_in_flight == 0` (immediately, else from
`takeover_done`). A superseded takeover's in-flight record write therefore
lands before the owner-clear, and no owner-clear crosses a new takeover's
verification. Bookkeeping writes of a finished run (`finish_run`, the run
summary) are not ordered against takeovers — desktop parity (`:2634`): the
registry is one map across activations (§3.9) and `record_run` applies to
whatever entry the id names, as on desktop.

**Close generation `gen`** — bumped by `Execution::close` under `state`
then `live_runs`; captured by streams at subscribe and by handlers before
blocking work; a mismatch ⇒ the handler drops its input (the chat still
holds it; the next snapshot re-derives it) and the stream reconnects with a
fresh snapshot (D20). This is desktop's per-frame `answers_chat` check made
explicit for a handler that was mid-frame.

**Lock order**: `state` → `Inbox.slot` / `pending_interrupts` / `live_runs`
→ `SharedMailbox` → `LanePool.usage`. Under `state`: `lanes.offer`, queue
ops; never `client.*`, never `inject`. `health()` reads atomics only.
`inject_if(shared, run_id, item, admit)` (new in `lanes.rs`) evaluates
`admit` under the live lock; the chat source passes `|| gen == g ∧ open`
(the watch-connection condition; more is §10 R4).

### 3.3 Installation and readiness

`Owner::build_execution`; the consumer installs on `Prepared`;
`ExecutionService::start` spawns Epoch, the lanes, Service, Presence,
Housekeeping, Retention, ChatWatch; sources on demand. **Until C4,
`SharedOwner::build_execution` returns `None`** and `ExecutionService::start`
is `cfg_attr(not(test), expect(dead_code, reason = "wired at N2b-2 C4"))`.

### 3.4 The Service loop (`Task::Service`)

`pool.wait_until(1 s, || cancelled ∨ woken)`. No HTTP except the one
stand-down write (outside `state`, after a revoking close — nothing else
needs the loop then). Each pass: (1) RSS sample every 5 s → `rss_bytes`,
`rss_sampled_at_ms` (reported in `health()`; nothing gated); (2) the due
scan every 5 s on the real registry entries — `scheduler.cron_due(entry) ∨
scheduler.once_due(entry)` (desktop's own calls and eligibility,
`triggers.rs:106-166`; `cron_due` arms a `None` deadline on the entry it is
given) — skipping reserved triggers, pushing `PendingWork{id, Scheduled,
heartbeat}`; (3) dispatch (§3.5); (4) soft-break deadlines (kept across a
non-revoking close; cleared by a hard interrupt or the run's end); (5)
event-source convergence (§4.2); (6) the owner-clear when released by the
session (§3.9); (7) publish `word`. On cancel: the owner-clear, if due and
unwritten, is marked unsettled (counted).

### 3.5 Dispatch and binding (scheduled work)

Predicate per admission: `open ∧ grant.fresh(clock.now_ms()) ∧
pool.admission_open()` (the grant is the protocol's; ADR §7 "checked at every
admission"). `queue.admit(&pool)` → `(work, permit)`:
`permit.consume_scheduled(&scheduler, entry)` on the real entry (a cron is
advanced; a once is disabled — desktop's `:2585`, `:2590`); for a once also
`entry.last_run_at = Some(bind_at)` so a reconcile that reads the still-
`enabled: true` row cannot make it due again (`once_due` needs
`last_run_at.is_none()`) — the in-memory half of D3/E9; `false` ⇒ drop the
permit (disabled/repinned meanwhile, E20). Reserve (`reserved[id] = run_id`
until the report callback returned). Build the job, `lanes.offer`; the only
reachable refusal (pool closed between `acquire` and `offer`) finalizes the
job `Cancelled` at once (D13). Heartbeat: `PendingWork.heartbeat` iff the
trigger's `program` names the pinned working-space `android-poc-heartbeat`
(Q8).

### 3.6 In-flight accounting and quiescence (protocol)

`InFlight{run_id, lane, kind, gen, epoch}` from bind (chat: from the
offer on the chat thread; event: from the offer on the source thread)
until **the report callback returned** — bookkeeping included, since it
runs inside the callback on the lane thread, as desktop runs it on the run
thread. `inflight` is the size of `jobs`, updated in the same critical
section. Quiescence, atomics only:

```text
inflight == 0 ∧ queue_len == 0 ∧ !owner_clear_due ∧ !owner_clear_in_flight ∧ !retention_running
```

plus, if R1 is kept, `handoffs == 0` (an injected handoff from the inject
decision through `inject_if`). `close(true)` sets `owner_clear_due` before
returning, so a `Session::release` on the next line sees "not quiescent"
(D2, E6). Stream threads blocked in a read are not counted (ADR §4's
documented fallback). The session adds `takeovers_in_flight == 0`.

### 3.7 Report bookkeeping (`on_report`, on the lane thread)

Desktop parity, inline, HTTP outside `state`:

| Outcome | Scheduled and Event | Chat |
|---|---|---|
| `Ok(rr)` | under `state`: `record_run(entry, &rr)`; a once stays `enabled: false`; release; then `upsert_record(persisted_record(entry))` (`finish_run`, `:2634`), `publish_run_summary` | under `state`: `chat_live = None` (desktop's "take the run down first", `:2142-2150`); then `publish_run_summary`; `posted = post_credential_requests(..)`; `follow_ups(rr, .., posted, 0)` performed in order — "Still waiting", `interrupted` ⇒ "Stopped." + `interrupted_turn_record` on the `turns` address, `error` ⇒ "Something broke" (`:2152-2270`); leftovers (`report.refused_input` ∪ the slot's hand-on) re-offered as a new chat job by this thread (desktop recursion, `:2271-2284`) after acquiring a fresh Chat permit; if the lane is deferred (gate held) they are dropped — the next snapshot re-derives them (desktop's loser clears its backlog, `:2824`) |
| `Err(TraceUnpersisted)` | `record_run(RunResult::failed)` + persist | `chat_live = None`; error follow-up; no synthetic turn |
| `Err(RunPath)` | `record_run(RunResult::failed)` + persist (the breaker, as desktop's `run_trigger_program` fold) | `chat_live = None`; error follow-up |
| `Err(Cancelled)` (never started) | nothing persisted; a once's in-memory consumption restored (`enabled = true`, `last_run_at = None`); release | `chat_live = None`; nothing posted; the message is re-derived by the next snapshot |

The job leaves `jobs` when the callback returns. The `turns` write and the
error/stop posts go through the Execution-phase gate, which the session
keeps until quiescence.

### 3.8 Memory (ruling) and capacity faults

The worker samples `VmRSS` every 5 s and reports value + boot-clock sample
time in `health()`; Kotlin main judges staleness and enforces the policy with
`Close(RESOURCE_PRESSURE)` (a `Policy` hold). `GateCause::Resource` stays a
public, documented-reserved variant (no Rust holder). Capacity faults
(mailbox, queue, leftovers, stream line) are counted on the pool as today
(N2a); `Execution::capacity_fault()` reports `queue.len() == MAX_PENDING ∨
refused_since_last_pass` and makes `ready` false in the Status as the
session already does (`session.rs:422`, `:462-465`); whether the session
*closes* on it is §10 R3.

### 3.9 Open, close, stand-down, Stop

`Execution::open(grant, session_id, takeover: Option<&TakeoverOutcome>,
clock)`: `Some(outcome)` on every Open that performed a takeover (initial
and every reopen after a close/expiry — both fences require activation
grade then, `grant.rs:195`, `WorkerProtocolFence.kt:44`): `activation_gen +=
1`; the registry's standing entries replaced by `outcome.standing` (armed
`next_due`) and the responder by `outcome.chat_watch` (new field), the
pinned user entries kept (desktop's one registry, `:2880-2896`);
`owner_clear_due = false`. `None` (a renewal within a fresh grant): grant
refreshed only. Then `reopen(Policy)`, `open = true`.

`Execution::close(revoking)` (consumer thread), under `state` then
`live_runs`: `shared.stand_down(Policy, revoking)`; `gen += 1`; `open =
false`; `queue.cancel()` (§7 "cancels queued admissions"); `watcher` reset
(the next snapshot re-derives under the reply rule); streams exit at their
next frame. **Non-revoking**: live runs finish and report as usual; `breaks`
kept. **Revoking** (Release, Stop, and — per the protocol — a revoking
Close, §10 R9): `interrupt_all` already ran; `breaks` cleared; the standing
entries' `owner` cleared locally (desktop `:2835-2840`); `owner_clear_due =
true` if this activation owns the responder (desktop's one write,
`:2845-2854`), performed by the dispatcher once the session releases it
(§3.2), outside `state`, once; a failure is logged and counted and does
not hold quiescence (the next winner's takeover re-stamps the record, ADR-018
§3 "last write wins"). One `shutdown` beat by the Presence thread on the
flip (Q5). Obligations are two flags per activation; a new `open(Some)`
resets them (E11).

**Stop** (`session.rs:1099`): stand-down (revoking) → wait for quiescence
to the deadline → cancel the owner → joins; blocked SSE reads and blocked
guest calls end in `DEADLINE_EXCEEDED` with the handles retained (ADR §4).

## 4. Sources, beats, housekeeping

### 4.1 Chat source (`Task::ChatWatch`) — desktop's `watch_chat` on an owned thread

`loop { wait_until(IDLE, || cancelled ∨ (open ∧ owns_chat)); if cancelled
{ return }; g = gen; subscribe (gated: `POST {w}/query/subscribe`
`{objectId: chat, dataset: chat_messages, sort: [-createdAt], limit: 64}`);
for frame { if cancelled ∨ gen != g ∨ !open ∨ !owns_chat { break };
handle } ; idle_until(now + 2 s) }`.

Per record (snapshot → `snapshot_backlog(&data, agent_name)` — the
answered-cut, unchanged; changes → `records_in`): `resolve_reply`; under
`state` revalidate `(gen == g ∧ open)` else drop; `action =
watcher.on_message(chat, &record)`:

- `Start` with `chat_live = Some(run)` ⇒ `inject_if(shared, run, item, ||
  gen == g ∧ open)` (desktop: `:2098-2105`); `NotLive` ⇒ fall through to a
  new run; `Full` ⇒ a counted capacity fault, the message dropped (the
  next snapshot re-derives it).
- `Start` with no live run ⇒ `pool.acquire(Chat)`: `Admitted(permit)` ⇒
  `note_chat_start` persisted (this thread, desktop `:2343`), `chat_live =
  Some(run_id)`, `InFlight`, `lanes.offer` of the toolcaller job (desktop
  args `:2130-2133`); `CapacityDeferred` ⇒ the gate was held between the
  frame check and the acquire: drop and log — the stream exits at its next
  frame and the reopen snapshot re-derives the message. [R1, if kept: the
  mark is written before the offer/inject; a failed write parks the
  message in the `Backlog` and the dispatcher retries after 5 s.]
- `BreakIdle` with a live run ⇒ hard: the interrupt flag; soft:
  `soft_break_live` + `breaks.push(now + BREAK_GRACE)`. Without a live run
  ⇒ nothing deferred to drop (no backlog).
- `Dup`/`Skip`: nothing.

The watch connects iff `open ∧ owns_chat` — ADR-018 §3's ownership rule,
as desktop's `answers_chat` (`:1426`).

### 4.2 Event sources (`Task::EventSource0..3`) — desktop's `event_source_thread` on owned slots

Convergence every 5 s under `state`: `desired_event_sources(&registry,
self_peer, space)` filtered to the working space (the gate admits no other;
a foreign-space source is logged once — marking it is §10 R8); stale
sources get `stop`; a stopped source exits `Ok(())` at its next frame
(`exit_is_failure` false for `EventSource0..3`, D17) and its slot is reused
after the owner joined it; the fifth desired source is logged and counted.

Thread body: as the chat source's shell; `snapshot` ⇒ `state.seed(&records)`;
`changes` ⇒ for each record of `records_in`, **sequentially** (desktop
`:2722-2727`): `targets = event_targets(..)`; for each target:
`pool.wait_until(cancel ∨ !open ∨ gen != g ∨ Admitted)` on `acquire(Event)`
— a cancel-aware wait for the Event lane, the phone's form of desktop's
inline sequential fire; if the wait ended for a reason other than a permit
⇒ drop the record (live-only, desktop's "not ready ⇒ dropped" `:2745-2748`);
with the permit: `state.fresh(vec![record], agent_name)` (seen-mark after
the permit, ADR §2, once per message — the first target marks, later
targets of the same record skip the mark), `InFlight`, offer the job
(`event_args`). The lane thread's `on_report` does `finish_run` (E14).
Gate widening: `POST {w}/query/subscribe` with `dataset == chat_messages`
and `objectId` any identifier in the working space (§8).

### 4.3 Stand-down writes and routes

| Write | Route | Body check | Phase |
|---|---|---|---|
| Chat-watch owner cleared (once per revoking close of an open activation) | `POST {w}/modify` on the triggers object | existing | Execution (cleanup authorization if needed) |
| Interrupted `agent_turns` record (`rr.status == interrupted`, desktop's rule) | `POST {w}/modify` on `turns.object_id`, `dataset == turns.collection` | **new**: one record, `id` = 8 ASCII digits, `upsert: true`, one `$set` path `""` whose value's keys are exactly `{seq, fromAgent, userText, replies, interrupted, traceRef, searchText, llm}` with `replies == []`, `interrupted == true`, `llm == {"stopReason": "break_hard"}` | Execution |
| `shutdown` beat; run summary; trigger bookkeeping / adoption / health marks; credential card + stamps; "Stopped." / error lines | existing routes | existing | Execution |

### 4.4 Housekeeping and Retention

Housekeeping every 5 s while open: the reconcile read → under `state`
`reconcile_registry` (reserved entries protected: a reserved once carries
`last_run_at`, a reserved cron its advanced `next_due`) → `health_pass`
(unparameterized, desktop's) → adoption stamps and marks persisted on this
thread (desktop's ticker does the same inline, `:2549-2574`). [R6, if kept:
the 60 s `reverify_skills`.] Retention: desktop cadence — 60 s after the
first open, then hourly; the phase question is R7; `retention_running`
set around `expire(.., &stop)`; `stop` is raised by a close (R7) or the
cancel.

### 4.5 Responder beats (`Task::Presence`)

As r2: `presence_decide` + `status_envelope` with `data.sessionId`,
`data.activation`, `role` from `owns_chat`, `winner` = self peer; 1 s poll,
10 s cadence + change; only while `open`; one `shutdown` beat on a revoking
close (Q5).

## 5. How production reaches Open, and why nothing else can

As r2: `attach → Start → Activate → Open → takeover → finish_open →
execution.open(.., Some(outcome), ..)`; `Shared::reopen(Policy)` has one
caller (`finish_open`); before C4 `build_execution` returns `None`. A reopen
after a policy Close or expiry is activation-grade with a takeover
(`grant.rs:195`, `:284`); `None` only within a fresh grant.

## 6. Open cross-repo rulings — compatibility

Unchanged (SB-P-1 fields on every beat; NeedsSetup final; SB-P-7 and
`PortFull` untouched; F2 untouched). For H/L-kotlin: main reads
`rss_bytes`/`rss_sampled_at_ms` from the health poll and judges staleness
itself (ruling); no Binder field changes.

## 7. Test plan

Deterministic, both legs, the r2 harness. Groups: (1) install gate; (2)
takeover across service passes incl. a held owner-clear vs a new
Activate+Open and a held superseded takeover write vs Release (E5); (3)
quiescence with a held report callback, the take→register gap held at a
`#[cfg(test)]` hook between `lanes.rs:564` and `:575` (E21), Release with
the dispatcher's owner-clear held; (4) once: in-memory consumed at bind, a
run held past two reconciles with `enabled: true` rows → no second
descriptor, dump held → nothing binds, fast run → persisted `enabled:
false` + the run's `lastRunAt`, `Cancelled` → restored; (5) arming and
eligibility (adopted heartbeat/user cron from rows, 65 disabled crons → no
pending work, disable between queue and bind); (6) dispatch predicate; (7)
offer refusal; (8) chat: a snapshot with A and B → A runs, B injected
(desktop) ; `NotLive` → new run; soft/hard breaks and deadlines across a
non-revoking close; leftovers re-offered by the lane thread; a
`CapacityDeferred` acquire drops and the reopen snapshot re-derives; (9)
handlers across a close (gen); (10) [R1] the mark's write-before-handoff,
monotonicity, fail-closed read, cut after the answered-cut, retry; (11)
follow-ups: existing card / new card / failed post, desktop order, exact
bodies; "Stopped." + turn record on `interrupted` after a user Stop and
after a Release-interrupt (desktop parity); (12) finalization table ×
kinds; event trigger failing three times → `auto_disabled` (E14); a run
that appended its own turn → no synthetic record (E15); two activations in
one session → two owner-clears, two shutdown beats (E11); (13) events:
two targets sequentially, `fresh` after the permit, reconnect while
waiting for a permit, stale source retires with a healthy owner (D11/D17);
(14) memory report in health; (15) [R3] capacity-fault close if kept, else
the Status shows `ready: false, INTERNAL` while dispatch continues; (16)
reopen semantics (D10); (17) Stop; (18) cleanup authorization (E10); (19)
skill drift at Open, [R6] periodic; retention timing [R7]; beats; health
word. Desktop leg: gated runner, the broker's trace reader over a seeded
run, `NotPinned` → `refused_programs` → health, the fidelity script.

## 8. ADR amendments (exact text; each lands with its checkpoint)

**§2, memory (C1; replaces "Every 5 s each process reads its own RSS …
Sampler CPU counts against T8.", user ruling 2026-11-06).** — "Memory
admission policy (amended 2026-11-06 by the user's ruling). The primary
trigger is the system: main reads `ActivityManager.getMemoryInfo()` every
5 s and closes new admission (`Close(RESOURCE_PRESSURE)`) when `lowMemory`
is true or `availMem` falls under `threshold`, reopening after 30 s of
samples clear of both. The per-process ceilings are a backstop, sized from
the device's RAM for a dedicated phone so that normal load never reaches
them: main ≤ max(1 GiB, 25 % of `totalMem`), worker ≤ max(1 GiB, 30 % of
`totalMem`), their sum ≤ 60 % of `totalMem`; a sample of either RSS over its
ceiling, or over the sum, closes admission the same way (proposed rule; to
be validated by T8, which keeps its current numbers until then). Kotlin
main enforces every rule; the Rust worker closes nothing on memory: it
samples its own `VmRSS` every 5 s on a thread that performs no HTTP and
reports the value and the sample's boot-clock time in `health()`, from
which main judges staleness (a worker sample older than 15 s is stale and
closes admission, as before). The guest Wasmtime ceilings are unchanged.
The earlier numbers — worker 896 MiB = 1.5 GiB − the first 640 MiB main
plan, and the 1.5 GiB sum, which conflicts with Stage 0's recorded
'combined 1 GiB' — are superseded by this rule; T8/K4 thresholds and the
frozen F2 evaluator follow in a separate change."

**§2, after "the 64 pending descriptors."** (C3) — "N2b-2 adds: at most
four event-source streams; a fifth desired source is logged and counted,
never silently dropped. Event records are handled sequentially on their
source thread, as on desktop, each fire waiting for the event lane."

**§2, after "service loops use interruptible waits."** (C1) — "The
execution service is six owned threads beside the lanes and the epoch
driver: a dispatcher, the responder-beat thread, housekeeping (registry
reads and stamps), retention, the chat watch and up to four event-source
streams; run bookkeeping and chat follow-ups run on the lane thread that
ran the job, as on desktop. A scheduled or event trigger is reserved from
its admission until its report's bookkeeping completed and cannot be
admitted again meanwhile; a cron is armed by the scheduler's own rule and
consumed only under a lane permit; a once shot is consumed in memory at
admission and persisted at its report, as on desktop. A stream thread exits
at its next frame on any close."

**§4, after "Do not claim a timeout retroactively shortened an
already-running request."** (C1) — "Bookkeeping a report performs after
the owner is cancelled is refused by the transport and logged, never
retried. Execution is quiescent only when every admitted job's report
callback has returned, the stand-down write of the current activation is
settled, no sweep runs and no takeover is in flight; the session alone
moves the host transport between its phases, and re-authorizes the
execution routes for a stand-down write that arises after a return to
Preparation, without opening admission."

**§5, Explicit setup row** (C2) — append: "…and the general chat's
`bao/log/v1` child with its `agent_turns` collection, recorded as the
`turns` fixture address."

**§5, Confirmed activation/Open row** (C2) — append: "Execution dispatch
and responder beats run only in this phase. Every completed takeover starts
a new activation of the execution service; its stand-down write is released
only when no takeover is in flight, and a takeover begins only when no
stand-down write is pending."

**§5, after "…and the host transport refuses every `hosts` write."**
(C2/C3) — "Stage-1 amendment (2026-11-06, N2b-2): the execution phase also
admits `POST /v1/spaces/{w}/query/subscribe` with `dataset: chat_messages` on
any identifier object of the working space — the event sources an enabled
own event trigger names (ADR-018 §2); a source in another space is not
watched — and one write on the setup-recorded log child: a single upserted
`agent_turns` record whose value is exactly the ADR-005 §3 interrupted turn
with an eight-digit id, written for a run the interrupt trapped, as on
desktop. Event runs keep ADR-018 §2's bookkeeping and breaker. The chat
watch does not fold sender names: the members and identity routes are not
host routes, and the PoC account has one member, for which the fold is a
no-op (BOB-192)."

**§5, "All dispatch requires…"** (C1) — append: "The worker's registry
reconciliation, due scan and event-source convergence run only while
AppliedOpen is in force; a once shot or cron occurrence is consumed, and an
event marked seen, only after the lane permit is held."

**§7, the responder-beat sentence** (C2) — "…published by the worker's own
beat thread only while Open: 10 s cadence plus change-triggered, one
`shutdown` beat per activation on a revoking close (Release, Stop,
ownership loss), none on a non-revoking close."

**§7, after "installing the execution service"** (C4) — "The execution
service is installed by the consumer when preparation first reports
Prepared; a refused spawn leaves the worker not ready and a failed spawn is
an owner failure (`ExecutionSpawnFailed`)."

**Clauses whose current ADR-034 text is stricter than this design and
await the user's decision (§10)** — on "keep": the text stands and the
row's design lands with its checkpoint; on "parity": the clause is amended
to desktop behavior in the same change. §2 "Skills … every 60 s while open"
(R6); §5 Preparation row "No … retention" (R7); §5/§7 "mark them uncertain
and require explicit retry", "possibly committed runs are never
automatically replayed" (R1, R2); §7 "a mismatched activation intent …
checked at every admission" as applied to injections (R4); §2 "a saturated
queue … fails the PoC" as a session close (R3); §7 "ownership loss, prune
and Stop cancel them" for a revoking Close (R9); ADR-018 §2's
`unsupported_source` for foreign spaces (R8).

## 9. Implementation checkpoints

Production readiness is unreachable until C4. Each checkpoint lands its
ADR text in the same change; every stop is code+contract coherent.

| # | Content | ADR text | Tracked hunks | Stop-green? |
|---|---|---|---|---|
| C0 | The (b)/(c) extractions + exact-body and sequencing tests + the fidelity script | none | the 10 of §2 | yes |
| C1 | `execution.rs` core: state, generations, dispatcher (RSS report, due scan, dispatch, reservations, deadlines), lane bookkeeping for Scheduled, Presence + beats, Housekeeping reconcile; session edits (gate authority + cleanup authorization, `build_execution`, `open` signature, `spawn_pending` stand-down rule, owner-clear release, `tick`); `TakeoverOutcome.chat_watch`; `CalibratedBootClock: Clone`; `Task` → 17; `inject_if`; the lanes test hook; `exit_is_failure`; tests 1–7, 12 (Scheduled), 14, 16, 17 | §2 memory, §2 threads, §4, §5 "All dispatch…" | none | yes |
| C2 | Chat source, chat bookkeeping/follow-ups, stand-down write + cleanup authorization, `PreseededFixture.turns`, turn-record gate rule, [R1 mark module if kept]; tests 8–12 (Chat), 18 | §5 (setup, Open rows; the turn/sender sentences), §7 beats, [R1/R2 text per decision] | none | yes |
| C3 | Event sources (slots, sequential fires, convergence), the subscribe widening, Retention; tests 13, 19 | §2 (sources), §5 (streams, event bookkeeping), [R7/R8 per decision] | none | yes |
| C4 | `SharedOwner::build_execution` wired, the positive install test, the desktop-leg end-to-end, `environments.md` `turns` row, report, evidence | §7 install | `docs/environments.md` | — |

## 10. Beyond-desktop hardening — decisions for the user

Each row is stricter than desktop today and **not** structurally required
by the phone; it is out of the design body until the user decides. "Cost"
counts what the row adds to the body above.

| # | Item | Desktop today | What the design would add; who asks for it | Cost | Desktop-parity alternative and its risk on the phone | Recommendation |
|---|---|---|---|---|---|---|
| **R1** | **Replay high-water mark (Q2 B)** | The answered-cut (`snapshot_backlog`, `chatwatch.rs:65-94`) + the watcher's in-memory seen-set (`triggers.rs:1115-1119`); no durable started marker: a desktop that dies mid-turn re-answers the message at its next boot | A per-chat file under `traces_dir` holding the newest handed message `(createdAt, id)`, written before every offer/inject, monotonic, never expiring, fail-closed read; the snapshot cut skips at/below it after the answered-cut; a failed write parks the message (`Backlog`) and retries after 5 s. Asked by ADR-034 §5 "Do not replay interrupted runs with possibly committed effects" and the user's ruling (made believing desktop had it) | `execution/mark.rs` (~150 lines), the `Backlog` + a retry timer, a `handoffs` quiescence term, 1 test group (10 cases); generated E1–E3, E6–E8 in r2 (all answered in the file design) | Parity: nothing durable; after a worker kill at the Stop deadline, a crash, or an OEM kill mid-turn, the next session re-answers the unanswered message — duplicate reply and possibly duplicate synthetic effects; T10 ("zero duplicate effects") and T7 ("zero duplicates") would see it after any mid-turn death during the campaign. Accepted loss with the mark: an input handed but never read before a death is skipped; the user resends | **Keep** (cheap, self-contained, no routes, no threads; it is what makes T7/T10's duplicate gates survivable after a kill) — but it is the user's call |
| R2 | Never a chat post after revocation; the post-time fence; suppress "Stopped." / error / credential card for a revoked run; the interrupted turn written silently | Stand-down lets in-flight runs finish and reply (`serve.rs:2822-2823`); an interrupted run posts "Stopped." and writes the turn (`:2218-2235`); credential cards post whenever a run ends on a miss (`:2161`); BOB-143 accepted the ≤ one-poll overlap of two devices answering | A revocation-epoch fence checked before every post (`post_credential_requests(.., allow)`, the Writer or an inline check), suppression counters, ADR §5 "stand-down … never a chat post" (the Q3 ruling of N2b-1) | The `allow` parameter hunk, ~40 lines of fence, 3 test groups; generated D5, E4, E13, E19 | Parity: the losing phone's interrupted run posts "Stopped." (and the turn record) into a chat the new device now answers — one stray bubble under the same `agent.name`, which the new device's backlog scan treats as the terminal reply (the same thing happens on desktop in the BOB-143 overlap); a credential card after revocation is a duplicate card at worst (dedup by `requestedIn`) | **Parity** (drop the fence; keep desktop's follow-ups verbatim) |
| R3 | Capacity-fault session close (+ fence `invalidate_grant`, the fault latch) | No bounds, no fault concept; the N2b-1 session reports a fault as `ready: false, INTERNAL` in Status and keeps running (`session.rs:462-465`, `:1217-1221`) | A local close on fault, reopening needs activation-grade evidence; ADR §2 "a saturated queue becomes a visible capacity fault and fails the PoC rather than silently dropping events" | session edits (~40 lines), `invalidate_grant`, 1 test group through the oracle; generated D9, E12, E17 | Parity: the fault is visible (Status, health counters) and main decides; dispatch continues; a saturated queue drops/defers visibly (counted). Risk: none beyond what main already sees | **Parity** |
| R4 | Grant freshness, gate causes and fault checked at every injection | An inject is a mailbox push while the run is live (`serve.rs:2098-2105`), gated only by the watch being connected | `inject_if` under the full chat predicate (grant on the calibrated clock, gate, fault); ADR §7 "checked at every admission" read to include injections | `grant_expires_ms` atomic + predicate; generated E18 | Parity: an inject into a run already admitted under a grant that expired a few seconds ago (the session closes on expiry within 1 s anyway); the run's wall budget bounds it; no new effect class is admitted — the run is already running | **Parity** (`inject_if` checks `gen ∧ open` only) |
| R5 | Once shot persisted consumed before its run (Q9) | Consumed in memory at due time, persisted at `finish_run` (`:2590`, `:2634`): a desktop killed mid-run re-fires the once on its next boot | A pre-run `OnceConsumed` write with the job waiting on it, `OnceRestore` on cancellation, `Writer::cancel`; ADR §2 "No retry of an already admitted, possibly mutating run" | the awaiting slot + 2 write kinds + ~60 lines; generated E9 (durable half) | Parity: a once trigger whose run died with the worker re-fires once on the next owner — exactly desktop; the in-memory reservation (kept in the body) still prevents the D3 double-bind | **Parity** |
| R6 | Periodic skill re-verification every 60 s while open → NeedsSetup close | Skills are read live from the space (ADR-009 §5); no pins, no drift check | Housekeeping's 60 s `reverify_skills` → `NeedsSetup(Invalid)` → the consumer's close; ADR §2 "every 60 s while open … drift gives a non-revoking close plus NeedsSetup" (logged default in `user-follow-ups.md`) | 3 reads/overlay/minute, ~30 lines, 1 test; the ADR residual text | Parity: pins verified at preparation and at every Open (N2b-1, kept); a skill edited during an open session reaches turns until the next Open instead of ≤ 60 s. Risk: a drifted skill body for the session's duration — the fixture pins are the PoC's own | **Parity** (verify at Open only; amend §2) |
| R7 | Retention only in the Open phase; a close ends a sweep at its next page | Retention runs hourly from boot+60 s regardless of state (`:1452-1475`) | `/v1/local/*` trace routes Execution-only; sweeps gated on `open`; the stop flag on every close; ADR §5 Preparation row "No … retention" | `retention_running` term + stop plumbing + 1 test | Parity: allow the device-local trace routes in every phase (they are host-only, device-local) and sweep on desktop's cadence; stop only on cancel. Risk: none identified (device-local store) | **Parity** (amend the §5 Preparation row) |
| R8 | Foreign-space event sources marked `unsupported_source` | Any readable space is watched (ADR-018 §2); `unsupported_source` marks only a non-`chat_messages` dataset (`triggers.rs:531-545`) | `health_pass(.., source_supported)` parameter (a tracked hunk in `triggers.rs` + `serve.rs:2564`) so the record says why nothing fires | 2 hunks, 1 test; generated D12 | Parity: the source is not watched (the gate denies the space) and a log line says so once; the record shows nothing. Risk: a user pinning a foreign-space trigger on the phone sees it silent — the PoC fixture has none | **Parity** |
| R9 | Cancel live runs on a revoking **Close** (ownership loss / prune / deauthorization), not only on Release/Stop | Stand-down never interrupts a turn (`:2822-2823`) | `interrupt_all` on every revoking close; ADR §7 "ownership loss, prune and Stop cancel them" | none beyond Release/Stop (one `revoking` flag the session already passes); it is what the drafted amendments assume | Parity: a revoking Close lets the run finish (≤ 1200 s) and post; the Release that main sends next must wait for quiescence or time out (DEADLINE_EXCEEDED → retirement/kill). Risk: every ownership loss with a live run ends in a kill instead of a clean Released | **Keep** (one rule for every revoking close; Release/Stop need it anyway) |

Notes, not rows: the lanes' host-buffer bounds, the pending queue with
heartbeat priority and the four event-source slots exist from N2a or are
the fixed `Task` set (structural); per-target event delivery is desktop's
(each target fires); the "exactly-one stand-down write" is desktop's; the
interrupted-turn record on `interrupted` is desktop's.

## 11. Review dispositions, round 1 (kept)

| ID | Disposition |
|---|---|
| D1 | Fixed (§3.2). |
| D2 | Fixed (§3.6: jobs counted until the report callback returned; `owner_clear_due` set before `close` returns). |
| D3 | Fixed in-memory (§3.5: `reserved` + `last_run_at` at bind); the durable half is R5. |
| D4 | Pending user decision (R1). |
| D5 | Pending user decision (R2). |
| D6 | Fixed (§3.4: `cron_due` on the real entry). |
| D7 | Fixed (§3.3, §9). |
| D8 | Superseded by the memory ruling (§3.8). |
| D9 | Pending user decision (R3). |
| D10 | Fixed (§3.9, §5). |
| D11 | Fixed by parity (§4.2: sequential fires, `fresh` after the permit). |
| D12 | Pending user decision (R8). |
| D13 | Fixed (§3.5). |
| D14 | Fixed (§7 desktop leg). |
| D15 | Fixed by parity (§4.1: a `Start` offers directly, injects while live; no backlog). |
| D16 | Fixed (§3.7 table; §3.9 flags). |
| D17 | Fixed (§4.2). |
| D18 | Fixed (§3.4). |
| D19 | Fixed (§3.2: the dispatcher does no HTTP but the one stand-down write outside `state`; bookkeeping on lane threads). |
| D20 | Fixed (§3.2, §4.1). |

## 12. Review dispositions, round 2

| ID | Raw | Disposition |
|---|---|---|
| E1 | O-3, I-2, R-1 | Pending user decision (R1); if kept, the mark never expires. |
| E2 | I-1 | Pending user decision (R1); if kept, a file under `traces_dir` outside every guest route (verified: `android-guest-data-routes.txt:25-39`, `broker.rs:2357-2375`, `preparation.rs:503`). |
| E3 | I-3 | Pending user decision (R1); if kept, fail-closed read. |
| E4 | O-2, I-4, R-2 | Pending user decision (R2); on parity the helper moves verbatim and posts as desktop. |
| E5 | O-1, I-5 | **Fixed** (§3.2: `spawn_pending` waits for `standdown_idle()`; the owner-clear is released only with no takeover in flight). |
| E6 | P-1, O-8 | Pending user decision (R1); if kept, `handoffs` counted in quiescence (§3.6). |
| E7 | O-7, I-6, P-3, R-4 | Pending user decision (R1); if kept, a failed mark write parks and retries, no session fault. |
| E8 | O-9, I-7, R-5 | Pending user decision (R1); if kept, no per-message entry exists (the mark is monotonic). |
| E9 | O-10, O-11, R-6 | **Fixed** in memory (§3.5/§3.7: `last_run_at` at bind, `enabled: false` persisted at the report, restored on `Cancelled`); the durable half is R5. |
| E10 | O-4, P-5 | **Fixed** (§3.2 cleanup authorization). |
| E11 | O-5, P-6 | **Fixed** (§3.9: two flags per activation, reset by `open(Some)`). |
| E12 | O-6, P-2 | Pending user decision (R3). |
| E13 | O-12, P-8 | Pending user decision (R2); on parity no suppression exists. |
| E14 | I-9, R-3 | **Fixed** by parity (§3.7: `finish_run` for event runs). |
| E15 | I-8 | **Fixed** by parity (§3.7: the synthetic turn only for `rr.status == interrupted`). |
| E16 | I-10 | **Fixed** (§8/§9: the §7 beat amendment with C2). |
| E17 | P-4 | Pending user decision (R3). |
| E18 | P-7 | Pending user decision (R4). |
| E19 | R-7 | **Fixed** by parity (§3.7: `posted` obtained first, inline on the lane thread, desktop order). |
| E20 | R-8 | **Fixed** by parity (§3.4: `cron_due`/`once_due` on the real entry; `consume_scheduled` revalidated at bind). |
| E21 | R-9 | **Fixed** (§7 test 3: the take→register hook). |

## 13. Net change against r2

- Threads/slots: 18 → **17** (the Writer is gone).
- Removed: the Writer and its write kinds/queue/`cancel`/`WritesDrained`;
  the per-message ledger; the chat backlog/descriptor machinery and
  `chat_retry_after` (kept only inside R1 if chosen); the event deliveries
  park, `EventSource::seen`, the 16-record bound; the uncertain set;
  per-activation obligation records (two flags instead); the worker-side RSS
  gate (ruling); the post-time fence and `allow` parameter (R2); the
  capacity-fault close, `invalidate_grant`, the fault latch (R3); the inject
  predicate beyond `gen ∧ open` (R4); `OnceConsumed`/`OnceRestore`/awaiting
  (R5); the 60 s skill check (R6); Open-only retention (R7); the
  `health_pass` parameter (R8).
- Added vs r2: nothing new; `inflight` now ends when the report callback
  returns (bookkeeping inline).
- Routes: the subscribe widening and the turn-record write remain; no
  local-collection route; [R7 parity would allow the trace routes in every
  phase].
- Tracked hunks: 12 → **10**.
