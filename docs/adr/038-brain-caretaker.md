# ADR-038: The AI brain's caretaker — deterministic faults, one checked model turn, record-only repairs

Status: **Proposed** (2026-10-28)
Date: 2026-10-28
Builds on: ADR-033 (the brain as a plugin, its jobs as records), ADR-034
(people, promises), ADR-035 (facts, `unpinned`), ADR-006 §4 (trigger
records, the breaker), ADR-023 §1 (the synced `agent_runs` summaries),
ADR-030 (the judge tier)
Plan: any-ui `docs/plans/2026-10-25-ai-brain-phase-5-caretaker.md`, as
amended by `docs/plans/2026-10-28-ai-brain-phases-3-6-on-connectors.md`

## Context

The brain runs unattended: a heartbeat, people, promises, facts and a
mail sync, each an ordinary trigger record. When one of them breaks —
stopped by the breaker, pinned to a device that is gone, a model that
fails on every call while the run still reads `ok` — nobody notices
until the user asks why the brain knows nothing new. The caretaker is a
brain job that watches the others, from the same records the UI shows,
and either repairs what it may or tells the user. It must never pick
its own model, never decide by model whether something is broken, and
never touch anything a repair could make worse: secrets, approvals,
exclusions, deletions, other people's jobs.

## Decision

### 1. The job, and the pin first

`connectors:brainCaretaker@v1` is a brain job like the others (ADR-033
§4): `brain-caretaker`, `every_s: 900`, args `{space, hub, datasets,
tier: "brain.caretaker"}`, written by the setup run and found by
program. The setup run seeds `llm.tier.brain.caretaker` from
`llm.tier.codegen` when absent, with the job (ADR-033 §3), and the hub
records version 6 so an installed brain upgrades through reconcile.

The run's first statement reads `llm.tier.<args.tier>`. A missing row
— the broker says `ConfigError: no config value for "<key>"` — or one
that names no `model` raises `unpinned` before any read or write: an
unattended job that may flip a trigger must never run on a model the
user did not pin. Any other `ConfigError` (the store unreachable) is
`snapshot_failed`. Then `unconfigured` when the args lack the hub,
`incidents`, `observations` or `signals`.

The datasets the args carry are the ones the hub had when the job was
written; a later hub version adds stores (Phase 3's `cards`, `standups`,
`drafts`). The run therefore reads the hub's live datasets from
`brain.status.hub` when it names the same hub, and uses a store only
when that map names it.

### 2. The snapshot

One pass reads, for the detectors: the `agent_triggers` records (the
runtime's rollup lives on them: `enabled`, `lastRunAt`, `lastStatus`,
`consecutiveFailures`, `owner`); per brain job its newest synced
`agent_runs` summary (`bao/runs/v1`, filter `triggerId`, sort
`-startedAt`, limit 1 — the body any-ui's run list sends); `brain.space`
and `brain.status`; the exclusion record; the tier rows the brain's jobs
use; the jobs' own stats and cursors in the hub's `config`
(`promises.stats` / `.cursor`, `facts.stats` / `.cursor`); the newest
`sync_state` object; `googleAuth.status()`; the devices registry's
active `bao` device; open `reply` cards when the hub has `cards`.

`agent_runs` is read rather than `effects.runs`: the local mirror
covers this device only, so a device switch would read as every job
overdue. A field the run could not read is the literal string
`"unavailable"` wherever it is used — in the evidence and in the model's
input — never omitted. Three reads are required, and their failure
raises `snapshot_failed`: the trigger records (nothing can be graded
without them), the non-closed incidents (nothing can be reconciled) and
the caretaker's own state, `config/caretaker` (a run that could not read
it would send a notice twice).

### 3. Fault classes

Detection is plain code over the snapshot; the model never decides
whether something is broken. A brain job is a `cron` record whose
`program` starts `connectors:brain`; the caretaker does not grade
itself as overdue (it is running).

| Class | Condition | Subject | Evidence |
| --- | --- | --- | --- |
| `job_overdue` | an enabled brain job whose last run — the newest of its record's `lastRunAt`, its newest `agent_runs` row and its caretaker reruns — is older than `2 × every_s + 120 s`; a job that never ran counts from `brain.status.at` | trigger id | `lastRunAt`, `lastStatus`, `everyS`, `overdueS`, `owner`, `activeDevice` |
| `job_failing` | a brain job with `consecutiveFailures ≥ 2`, or `lastStatus: auto_disabled` | trigger id | `consecutiveFailures`, `lastStatus`, `lastErrorType` (newest run's `errorType`), `lastRunAt`, `enabled` |
| `mail_stale` | `googleAuth.status().connected` is `false` (a dead credential makes the sync return an error dict without raising, so its run status is no signal), or the newest `sync_state`'s `modifiedAt` is older than 6 h (no `sync_state` yet: counted from `brain.status.at`) | mailbox id (`sync_state.mailbox_id`, else `mail`) | `syncStateModifiedAt`, `credentialConnected`, `cursor`, `skippedCount`, `mailTriggerId` (`brain.status.mailSync.triggerId`), `lastRunStatus` |
| `exclusions_unreadable` | `config/exclusions` missing or not in its version-1 shape (`brainKit.exclusions_problem`) | `config/exclusions` | `present`, `version`, `parseError` |
| `tier_unpinned` | a tier an enabled brain job calls — its `args.tier`, else the program's own (`classify` for promises, `brain.extractor` for facts), and `brain.drafter` once the hub has `drafts` — has no row or no `model` | `llm.tier.<tier>` | `present`, `provider`, `model` |
| `model_stalled` | a job that reads mail with a model (promises, facts) whose last two runs each added to its `providerFailed` count while its cursor did not move | trigger id | `streak`, `providerFailedDelta`, `providerFailedTotal`, `cursorMoved`, `lastRunAt`, `lastRunStatus` |
| `setup_blocked` | `brain.space` asks for a state `brain.status` has not answered — waiting, setting up or switching off — for over an hour; or the `brain-setup` record is gone | `brain-setup` | `requestState`, `requestedAt`, `answeredAt`, `ageS`, `setupTriggerPresent`, `setupOwner`, `setupLastRunAt`, `activeDevice` |
| `draft_backlog` | once the hub has `cards`: ≥ 3 open `reply` cards older than 1 h without `draftId`, or any with `draftAttempts ≥ 3` | `cards` | `count`, `oldestOpenAt`, `attemptsExhausted`, `drafterLastStatus` |
| `approval_stuck` | **inactive**: it needs the deferred send gate (any-ui's `2026-10-25-ai-brain-later.md`) | — | — |

- **`model_stalled` is the silent failure.** With one mailbox and a
  failing provider, `brainPromises` stops that mailbox before the
  failing message and reports success with `providerFailed: 1`, every
  run, its cursor never moving; the runtime sees only `ok` runs. The
  stats are cumulative, so the caretaker keeps, per job, the last run it
  looked at, the count and a hash of the cursor in `config/caretaker`
  (`watch`), and counts the runs since that failed without progress.
  A run with no failure, or one whose cursor moved, resets the streak.
- **`setup_blocked` escalates only.** The usual cause is a `brain-setup`
  record pinned to a device that is gone (a whole-value `$set` merges,
  so a re-arm keeps the `owner`), or one deleted by hand, whose id is
  burned for good. No allowed repair helps, so there is no model turn.
- **Away is not overdue.** When the caretaker's own previous run is
  older than its own overdue limit the device was asleep or off, and
  every age reads stale for the same reason: that tick grades no age
  (`job_overdue`, `mail_stale` by age, `setup_blocked` by age). A
  disconnected credential and a gone record still count.
- **A paused job is not a fault.** `enabled: false` without
  `lastStatus: auto_disabled` is the user's choice (ADR-033 §4); it is
  neither overdue nor failing.
- The design record's `bao.status` beat age is not a class: the guest
  cannot read the bus, and the caretaker runs inside the very serve
  whose beat it would measure. The UI's presence indicator owns it.

### 4. Incidents and observations

`incidents` and `observations` are the hub's stores (ADR-033 §3, fields
declared in hub version 1).

- An incident is opened for a `(faultClass, subject)` with no incident
  that is not `closed`: id `sha256(faultClass|subject|<openedAt epoch
  s>)[:32]`, `{faultClass, subject, openedAt, status: "open"}`.
- Every tick writes one observation per incident that is not `closed`
  at the start of the tick: id `<incidentId>:<tick>`, `{incidentId,
  tick, evidence}` — the class's evidence, or `{clean: true}` on a tick
  the fault was not seen. Written through `upsert_records`, so an
  observation is never updated.
- A clean tick closes an `open` or `escalated` incident (`status:
  closed`, `closedAt`); `verified` stays absent — it healed by itself,
  which the ladder did not verify. A `repairing` or `soaking` one
  follows the ladder (§7).

### 5. One model turn

An `open` incident whose `modelTurnAt` is absent or older than an hour
gets one `llm@v1.chat` call on the caretaker's tier, no tools, at most
600 output tokens, at most five calls per run (the rest wait for the
next tick). The input is built by code: `{faultClass, subject, summary,
evidence, history: the last 5 observations' evidence, allowed}`, where
`allowed` is the class's repairs that apply to this subject (§6) plus
`escalate`. The reply must be one JSON object `{hypothesis, confidence,
evidence, action, target}`, validated by plain code:

- `confidence` is a JSON number in `[0, 1]`; anything else — a word, a
  string number, a boolean, out of range — is 0. Below 0.6 the action
  becomes `escalate`: an unsure turn never repairs.
- every `evidence` entry is a key of the input's `evidence` (or
  `history`); a repair cites at least one.
- `action` ∈ `allowed`.
- `target` equals the incident's `subject`. The model confirms the
  target, never supplies it: the code reads it from the incident and
  the snapshot, and the reply's string is discarded.
- an unparsable reply, or one that is not an object, escalates.

A reply that breaks a rule is refused: one `signals` row `{tick, kind:
"repair_refused", subject: <incidentId>, decision: "refused", reason}`
and the incident escalates. An accepted reply writes `hypothesis` (≤ 500
chars), `confidence`, `evidence`, `action`, `modelTurnAt` and `model`
(`llm.profile(tier).model`) on the incident.

**A model failure must not look like success.** The call catches
`EffectError` (a timeout, a connection error, a missing secret) as well
as `LlmError` and any other error the call raises; the incident
escalates with the error's type, so the user still hears of the fault,
and when every call of the run failed the run raises `provider_failed`
after its writes, so the runtime counts it and the breaker stops a
caretaker whose model is gone.

`llm@v1.judge` needs a TypeSafe wire the account may not have (ADR-030);
when `judge_available()` reads true a second validator over typed
`choice` / `score` questions can replace the JSON one. The allowlist and
the target rule do not change with it.

### 6. Repairs are record writes

| Action | What the code writes | Allowed for |
| --- | --- | --- |
| `run_job` | a `once` trigger `brain-rerun-<trigger id>-<tick>` (fresh, checked against the records it read) with the job's program and args, `at: now` | `job_overdue`, `job_failing`; `mail_stale` when `brain.status.mailSync` names the sync |
| `enable_job` | `$set enabled: true` on the job's record, nothing else | `job_failing` on a brain job the runtime stopped (`auto_disabled`) |
| `rewind_cursor` | the job's cursor 24 h back: `people.cursor.mail.<mailbox>` and `promises.cursor.<mailbox>` in ms, `facts.cursor.watermark` in s | `job_failing` on people, promises, facts |
| `notice` | one line in the next chat notice (§8) | every class but `setup_blocked` |

- The target is the incident's own subject, resolved against the
  snapshot: `enable_job` refuses a record whose program does not start
  `connectors:brain` or that is not stopped; `rewind_cursor` refuses a
  job without a rewindable cursor (the mail sync's Gmail `historyId`, a
  calendar sync token — the job resyncs on a 410 by itself) and an
  unreadable one. A refused execution is a `repair_refused` signal and an
  escalation.
- Every incident gets at most one repair: after it the incident is on
  the ladder, and a failed verification escalates it for good.
- **Advice only** — a hypothesis may name them, the code never does
  them: secrets, approvals, `config/exclusions` and the other user
  lists, deletions, `sends`, repinning a job, anything outside the
  brain's own jobs and cursors.
- **Re-enabling resets the breaker.** Read from the runtime, not yet
  probed live: a dataset `enabled` false→true flip on a record the
  registry holds resets its `consecutive_failures` and re-arms it
  (`triggers.rs` `reconcile_registry`). A serve restarted between the
  stop and the flip rebuilds the entry from the record, whose
  `consecutiveFailures` still reads the old count, so one more failure
  stops the job again at once; the caretaker then reads `job_failing`
  again and escalates rather than retry.

### 7. Ran is not fixed

A repair sets `status: repairing`, `actionAt`, `verifyAt = now + 15
min`. At `verifyAt` the class's condition is read again for the subject:
clean → `soaking`, `verifyAt = now + 60 min`; clean at the end of the
soak → `closed`, `verified: true`, `closedAt`. A dirty tick at the verify
step or during the soak → `verified: false`, `status: escalated` (§8),
and no second repair. A `rerun` counts as a run of its job, so
`job_overdue` verifies after a nudge only while the job keeps running
on its own within the window.

### 8. Escalation, notices, reports

- **Escalation** is a card and a notice, once per incident. The card
  goes into `cards` only when the hub's datasets name it (Phase 3's
  contract): id `sha256("incident|<incidentId>")[:32]`, `{kind:
  "incident", title: "<faultClass>: <subject>", reason: <hypothesis, or
  "caretaker could not decide — <summary>">, subjectKey: <incidentId>,
  sourceUri: <the incident's record link>, status: "open"}`, written
  only when that id is absent so a dismissed card stays dismissed; its id
  lands in the incident's `cardId`. Without `cards`, the notice is all.
- **Notices** are batched: each escalation or `notice` repair adds one
  line to `config/caretaker.pending`, and a run posts them in one
  `chat_send` to Bao's general chat when `noticeCooldownMin` (default
  60) has passed since `lastNoticeAt`. A notice is `agent: {name:
  <runtime.get("agent.name"), else "bao">, done: true}`: the watcher
  skips the serving agent's own messages, so a notice starts no turn,
  and raises one notification. Success is silent.
- **The daily report**, on the first tick of a UTC day
  (`config/caretaker.lastReportDay`) that had incidents in the last 24 h:
  a `standups` row `{job: "caretaker", tick, did, needs, counts}` and an
  `info` card `{title: "Caretaker report <day>", subjectKey:
  "caretaker:<day>", reason}`, each when its store exists. The lines say
  "fixed (verified)" only for incidents the ladder closed, "tried,
  unverified" for those on it. A run that opened, repaired, verified,
  escalated or closed something writes a `standups` row; one that did
  nothing writes none.
- `config/caretaker` is the caretaker's own state: `{noticeCooldownMin,
  lastNoticeAt, pending, lastReportDay, watch, lastTickAt}`.

### 9. What the caretaker cannot do

No runtime change: it cannot restart `serve` (there is no process to
restart inside one), read the event bus, repin a job to the active
device, or run anything but guest programs through records. The
election verdict it reads is the server's `active.bao`, evidence only.

## Consequences

- One definition of broken: the detectors read the records Scheduled,
  the Cards view and Settings → Model render.
- The caretaker's own failures surface where every job's do: an
  `unpinned` or `provider_failed` run reads FAILED, the breaker stops it
  after three, and `brain.status.stopped` names it (ADR-033 §4).
- `mail_stale` by age relies on `sync_state.modifiedAt` moving on every
  checkpoint; a mailbox with no Gmail history for six hours may read
  stale. If it does, `gmailSync` records a `lastSyncAt` and the detector
  reads that.
- Verified with the kernel harness and fakes; the live runs the plan
  lists (A3) and the breaker probe (§6) are pending.
