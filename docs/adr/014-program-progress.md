# ADR-014: Program progress — `progress@v1` over agent-progress objects

Status: **Accepted** (2026-08-14), amended 2026-08-16 (§1: the module
is an agent tool — `__any_tool__` — so ad-hoc chat-driven batch jobs
discover it instead of hand-rolling the transport, which live testing
showed the agent otherwise does; plus a `jobs(space)` getter, the
read-side answer to "what's running?")
Date: 2026-08-14
Builds on: ADR-002 (all writes cross the effect boundary), ADR-006
(space objects as data contracts), ADR-009 §2 (cross-repo deps,
alias-qualified `use`), ADR-010 §4 (docstring convention), ADR-012
(gmailSync — the first publisher, previously via a private helper)

## Context

Long jobs (gmail backfill chains, future importers/indexers) need a
live progress surface in any-ui. The `agent_progress` protocol — one
object per job, property ticks riding the per-space objects firehose —
was designed and rig-verified 2026-08-12 and adopted by gmailSync
2026-08-13, but as a **private helper inside gmailSync**: every next
publisher would copy it, and every transport change would touch every
program.

Meanwhile the any server team has been asked to design a generic,
reusable progress facility that will live in `any`. Whatever we do now
is **temporary**, so the bar is: non-harmful (no upstream contract we
would later have to remove — the `status_task`/ui-commands prototype
explored in any-ui `feat/status-line` is parked for exactly this
reason: its server patch is additive going in but a removal going
out), non-ugly (no junk left in spaces), and swappable behind
interfaces on both ends.

The UI end already has its seam: any-ui's `task-status.ts` takes
idempotent full-state task writes from any source. The program end has
none — this ADR adds it.

## Decision

### 1. `progress@v1` — the program-facing interface

A repo program (`repos/_agent/programs/progress@v1.py`; an agent tool
since the 2026-08-16 amendment, so the toolcaller's inventory carries
it) is the ONLY way programs — and agent cells running ad-hoc batch
jobs — report progress. Connectors reach it alias-qualified
(`use("agent:progress@v1")`, ADR-009 §2). Surface:

- `start(space, job, label, total=0, current=0, detail="", program="")`
  → objectId. Publishes the 0% state **before work begins** (a bar
  that appears only at the first tick reads as a hang). Idempotent:
  an existing job converges (§3) and is rewritten — resume/retry
  reuse the same bar.
- `tick(space, job, current, total=None, detail=None, label=None)` —
  advance. Property writes only (a name rewrite doubles firehose
  frames). Self-heals a missing object. Writing `status: running`
  every tick is the reopen path after a transient `fail`.
- `done(space, job)` — finish + self-clean (§4).
- `fail(space, job, error, detail=None)` — `status: failed` + error;
  the object is KEPT as the visible record.
- `jobs(space)` — read-side: one row per live job, freshest wins (§3
  without pruning). Since `done` deletes, only running/failed rows
  exist — the answer to "what's running?".

Callers own the THROTTLE: tick per work chunk / percentage step /
~1 per second at most — every tick is a p2p-synced CRDT change.

### 2. Transport is an implementation detail

Today: one `agent-progress`-typed object per (space, job) — props
`{job, label, status running|done|failed, current, total (<=0 ⇒
indeterminate), detail, started_at, updated_at, error, program}`
(snake_case on purpose, amended 2026-08-16: the client snake-cases
names into xKeys and normalized reads key by xKey, so camelCase names
split the read shape from the write shape — and silently broke the
started_at carry in the §3 dance) —
found by `agent-progress.job`, updated by property writes; the UI
watches the objects firehose. When the any-native facility lands,
`progress@v1` is reimplemented against it and **no program changes**.
The type dict lives in `progress@v1`, nowhere else.

### 3. Duplicate convergence — the dance

Query-then-create races (concurrent hops, p2p partitions) mint
duplicate job objects; two bars for one job is the bug. Same dance as
gmailSync's `sync_state` (`_ensure_state` precedent): on every
resolve, query up to 10 rows by job key, sort by `modifiedAt`
descending, keep the freshest, best-effort delete the rest. One merge
nicety on top: the **earliest** `startedAt` across duplicates is
carried into the survivor's next write (the true start time survives
the race; counters do NOT merge — freshest wins, summing across racers
would double-count). Readers apply the same rule statelessly: group by
job, render the freshest, ignore the rest.

### 4. Completion self-cleans; failure is the record

`done` DELETES the job's object(s) — a finished bar leaves nothing in
the space tree (the non-ugly requirement). Disappearance is therefore
the success signal: since `fail` keeps its object, a watcher that sees
a running job's object vanish renders "succeeded" and lets it linger
out. A publisher wanting final counts visible in that linger ticks
them just before `done` (gmailSync's final hop does). `fail` keeps the
object until the user acts or the next `start`/`tick` of the same job
reopens it in place.

### 5. The UI half (any-ui, `feat/status-line`)

A ~40-line `ProgressSource` subscribes to the current space's objects,
filters to `agent-progress`, applies §3 freshest-wins per job, and
feeds the `task-status.ts` seam (`setTaskStatus`, keyed
`progress:<spaceId>:<job>`): running → ring (`current/total`,
indeterminate when `total <= 0`), failed → sticky danger item,
object removed while running → succeeded + linger (§4). The
`status_task` ui-commands dispatcher from the same branch stays
dormant — no `api.UICommand` extension upstream.

### 6. Notify-on-done (TENTATIVE interface — 2026-08-16, reworked same day)

`done(...)`/`fail(...)` accept `notify` — `baoSpaceConfig`, a
`{spaceId, chatId}` dict, or an agent-space config (chat defaults to
its general chat). When set, the terminal call posts a VISIBLE chat
message under the agent identity `trigger:<job>` ("[trigger: progress]
Job X … is DONE — 10/10 / FAILED: …"). The watcher's name-scoped
self-skip (ADR-009 §8 amendment: only the serving agent's own
`agent.name` never self-triggers) makes that message trigger the loop
like a user message — so the agent answers it in the chat with the
conversation's history in context, and the nudge itself is visible,
attributed, and not impersonating the user. (First cut armed an
invisible toolcaller once-trigger instead; replaced because the
visible message keeps chat history coherent and needs no trigger
plumbing. A future `source` field on chat messages can carry the
trigger identity for distinct UI rendering.) Best-effort: a
notification hiccup never fails the job. For detached trigger-driven
jobs only — inline work reports in its own turn. Provisional; revisit
alongside the any-native facility's terminal events.

## Consequences

- gmailSync drops `PROGRESS_TYPE` + `_progress()` and calls
  `agent:progress@v1`; behavior change: a drained chain now deletes
  its progress object (the completion nudge already reports counts
  through chat; `fail`/breaker objects remain findable).
- FTS/vector-indexing progress stays out of scope until the any-native
  facility exists (blocked on A15 regardless).
- The swap-out is two files: `progress@v1.py` internals and the UI
  source — no program, no server, nothing to remove upstream.
