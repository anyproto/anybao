# ADR-033: The AI brain as a plugin in the connectors overlay — setup as a program, jobs as records, no runtime change

Status: **Proposed** (2026-10-25)
Date: 2026-10-25
Builds on: ADR-006 §3 (`agent_config` rows, `config.get` / `config.set`),
ADR-006 §4 (trigger records: unowned records, `once`, refresh), ADR-009
(overlays), ADR-010 (docstrings as the doc surface), ADR-017 §0 (`bao/v1`
stores), ADR-027 (parts and the usecase catalog)
Design record: any-ui `docs/plans/2026-10-24-ai-brain-on-any-bao-design.md`;
plan: any-ui `docs/plans/2026-10-25-ai-brain-phase-0-packaging-scaffold.md`

## Context

The AI brain is a set of jobs and stores that keep what Bao learns from
the user's mail, calendar and meetings — people, promises, facts, cards —
in Bao's own space. It ships pre-installed for every user and stays off
until the user says yes to it in chat: the agent offers it at the
moments the `brain-core` skill names (Google just connected, a question
about people, promises or mail), and switches it on or off through its
own tools. No client writes the switch (owner decision, 2026-10-27; an
earlier draft had any-ui switch it on at first open and from Settings →
Brain, which armed a setup program a release could lack, PRO-1935).

A first plan asked for runtime work: built-in ("standing") triggers for
the brain's jobs, host-side provisioning, a ticker that watches a config
row, new model tiers in `config_defaults.json`, a third overlay. The owner
decided (2026-10-25) that the brain ships with **no change to the bao
runtime**. Everything it needs already exists for guest code:

- an unowned `agent_triggers` record runs on the election-active device,
  which stamps it (`triggers::reconcile_registry`, ADR-006 §4);
- a `once` whose record carries no `lastRunAt` fires on the next tick,
  and an `enabled` false→true flip resets the circuit breaker (ADR-006
  §4, refresh);
- `any@v1` exposes `setup_app`, `ensure_bundle`, `create_type`,
  `create_dataset` (which passes its draft to the server unchanged) and
  `bundle_child`;
- `config.get` reads the live store row and `config.set` refuses only
  the secret namespaces.

## Decision

### 1. The programs ship in the connectors overlay, as a plugin

The brain's programs live in `repos/_connectors/programs/` —
`brainSetup@v1`, `brainHeartbeat@v1` and the later phases' `brain*@v1`
— with the `brain-core` skill in `repos/_connectors/skills/`. Every
one declares `__any_category__ = "plugin"` (ADR-010 §4): the brain is
a plugin of the connectors repo, grouped apart from the service
connectors by that stamp. They deploy with the connectors overlay — no
new space, invite or `anybao.toml` entry, and no change to the desktop
shell, which writes the overlay list per release channel.

What follows from the repo:

- Imports are alias-qualified across repos (ADR-009): the brain calls
  `use("agent:any@v1")` and `use("agent:llm@v1")`; `brainKit@v1` is
  imported unqualified inside its own repo.
- The programs are `connectors:brain…@v1` — in job records, in the
  prefix the setup run matches brain jobs by, and in any client that
  lists them.
- Tools in the connectors overlay are not in the toolcaller's `## Tools`
  block, which lists the agent overlay and the working space (ADR-005
  §5). The agent reaches the brain through the `## Repos` inventory and
  the `brain-core` skill's index line (`## Skills`, loaded on demand).
  That costs no standing prompt per turn; the skill's one line carries
  the words that bring it in (brain, people, promises, mail, facts).
- `get_skill` and the skills index read the connectors overlay too, so
  the skill ships beside its programs.

### 2. The contract is two config rows and one trigger record

| Row / record | Writer | Value |
| --- | --- | --- |
| `brain.space` (agent_config) | `brainSetup.on()` / `off()` only, after the user's yes in chat | `{spaceId: <Bao's space> \| null, requestedAt, by: "chat"}` — absent = off (nothing starts the brain on its own), `null` = switched off |
| `brain.status` (agent_config) | the setup run | `{spaceId?, requestedAt, state: provisioning \| ready \| error \| off, at, error?: {type, message}, hub?: {objectId, datasets}, mailSync?, stopped?: [{id, name}]}` — `requestedAt` echoes the request it answers; `stopped` is a warning on a `ready` brain (§4) |
| `brain-setup` (agent_triggers) | whoever writes `brain.space` | `{name: "brain setup", kind: "once", spec: {at: <now>}, program: "connectors:brainSetup@v1", args: {requestedAt}, enabled: true, lastRunAt: null}` |

Each request rewrites `brain-setup` with `lastRunAt: null`, which re-arms
it. The null is load-bearing: a whole-value `$set` MERGES into the stored
record, so a plain rewrite keeps the fired run's `lastRunAt`, and the
runtime reads the once as consumed and never fires it again (seen on a
live run, 2026-10-25). Nothing in the contract is ever deleted: a deleted
record id can never be written again.

### 3. The setup run

`main(args)` without `args.mode`:

1. Read `brain.space`. Absent: a stale arm — set nothing up, write
   nothing, answer `{state: off, reason: "no request"}`. The status
   always echoes the row's current `requestedAt`, not the trigger's, so
   a second request wins.
   Before any of this, `on()` itself refuses (`google_not_connected`)
   while `googleAuth.status()` reports no connected account: the brain
   reads the user's Gmail, so the agent offers `googleAuth.connect()`
   first. A brain tool called while there is no hub raises `not_set_up`
   whose message IS the offer (what the brain reads, what it keeps, and
   that `on()` follows a yes) — the lazy bootstrap path, taken by the
   skill.
2. `spaceId: null`: disable every brain job, remember which in the
   agent_config row `brain.jobs` (`{offDisabled}`), answer `off`. Not a
   hub record: an off run after a failed setup has no hub in its status
   to write to, and the next on run would re-enable nothing. The list is
   written BEFORE the jobs are disabled, so a run cut short leaves none
   unrecorded; a failed off run is answered `error` and raised. An on run
   clears only the ids it could see — one whose record has not synced to
   this device yet stays remembered — and a `brain.jobs` in another shape
   raises `jobs_unreadable` rather than re-enable nothing silently.
3. A space other than Bao's: answer `error` (`wrong_space`) and raise.
4. Otherwise answer `provisioning`, then install or adopt: the `people`
   and `meetings` catalog apps; the `brain/v1` bundle (a created root of
   type `page`); the hidden, bodiless `brain_hub` type; its four
   datasets `signals`, `config`, `incidents`, `observations`, each
   `shared` and with its full field set — declared through `any@v1`'s
   `_create_dataset`, the plumbing export `enrich@v1` uses; the `brain/hub/v1` child; the exclusion list
   (`config/exclusions`, `{version: 1, personIds, emails, domains}`) and
   the `llm.tier.brain.extractor` row, copied from `llm.tier.codegen`
   only when absent (ADR-035 §3) — one tier row per brain job that
   calls a model, written with that job and never ahead of it, so
   Settings → Model repoints each on its own.
   A `409 bundle.not_ready` is retried five times, two seconds apart.
5. Write the missing jobs, re-enable the ones the last off run disabled,
   adopt or write the mail sync, and — for a NEW request only — re-enable
   the jobs the runtime stopped after repeated failures (`lastStatus:
   auto_disabled`; the flip resets the breaker). Answer `ready` with the
   hub, listing any job still stopped under `stopped`. Any exception in 4–5 is answered as
   `error` and raised as `provision_failed`, so the run reads FAILED.

### 4. The brain's jobs are records

| Job (id when first written) | Spec | Program | Args |
| --- | --- | --- | --- |
| `brain-heartbeat` | `every_s: 600` | `connectors:brainHeartbeat@v3` (v1 before hub version 4, v2 before 5; the setup run rewrites the record in place, ADR-036 §10, ADR-037 §7) | `{space, hub, datasets, tier}` |
| `brain-reconcile` | `every_s: 3600` | `connectors:brainSetup@v1` | `{mode: "reconcile"}` |

- **Found by program and `args.mode`, not by id.** A job deleted by hand
  is rewritten under a fresh id (`<base>-<epoch>`), since its id is
  burned. The server reports a burned id the way it reports every
  per-record refusal: HTTP 200, the id even listed in `recordIds`, the
  reason only in `rejections` — so every brain write checks
  `rejections` and raises on one (`write_rejected`).
- **A pause holds.** The reconcile run only writes missing jobs; it
  never re-enables one. Only an on run re-enables: the jobs the off run
  before it disabled, and those the runtime stopped.
- **A stopped job is not a pause, and not an outage.** When the runtime
  has stopped a brain job or the mail sync the brain set up (`lastStatus:
  auto_disabled`), the reconcile run keeps the answer — state `ready`,
  its `at`, hub and mail sync — and lists the job under `stopped`
  (`[{id, name}]`); it clears the list once the job is enabled again.
  `status()` names it as a warning, and every brain tool and `exclude`
  keep working, since the rest of the brain still does. The user's "try
  again" in chat is `on()` again — a new request, which restarts them
  (§3 step 5); a setup run re-armed for the request it already answered
  — a hub upgrade — restarts nothing. Two limits: the
  runtime keeps `lastStatus` until a job's next run, so a stopped job the
  user resumed and paused again before it ran is still read as stopped;
  and a stopped `brain-reconcile` cannot report itself — `status()`
  reads the jobs live, so the chat tool still sees it.
- Unowned when written, so the active device claims them; Pause and
  Repin in Scheduled work as for any job.

### 5. The heartbeat v0

Raises `unconfigured` without its args; reads `config/exclusions` and,
when it is missing or not version 1, writes one `signals` row
(`decision: refused`) and raises `exclusions_unreadable`; writes one
`heartbeat_gap` signal when its last tick is more than 1 200 s old;
records `config/heartbeat.lastTickAt`. It imports `any@v1` only.

### 6. The hub's stores are shared datasets

A read of a shared dataset returns `id: "<objectId>/<recordId>"`, and an
`id` filter compares that whole value; writes take the plain id (any
`docs/03-api.md` § Shared datasets). Every lookup of a hub record —
`config/exclusions`, `config/heartbeat` — filters on
`f"{hub}/{name}"`. A lookup by the plain id finds nothing, which would
re-seed the exclusion list over the user's on every setup run and make
the heartbeat refuse every tick.

## Consequences

- No file under `runtime/src/` changes. The cost, accepted:
  - the brain's jobs can be edited or deleted like any record — the
    hourly reconcile repairs deletions, an edit holds until fixed;
  - a request waits for the next ticker pass;
  - nothing starts the brain but a yes in chat: a user who never talks
    to Bao about mail, people or promises never gets it, by design;
  - brain notices post under the literal agent name `bao`, as
    `remind@v1` does.
- What would need runtime code is kept in any-ui's
  `docs/plans/2026-10-25-ai-brain-later.md`: a send gate, built-in brain
  jobs, run ids stamped on writes, the configured agent name for guests,
  non-chat event triggers, enforced capabilities, a second Google
  account.
- Phase 0 ships no mail, calendar or meeting sync, no people, facts,
  cards or drafts — only the scaffold above.
