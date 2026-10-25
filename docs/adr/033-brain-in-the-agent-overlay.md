# ADR-033: The AI brain in the agent overlay — setup as a program, jobs as records, no runtime change

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
in Bao's own space. It must ship pre-installed for every user, be on by
default, and be switchable from any-ui's Settings → Brain and from chat.

A first plan asked for runtime work: built-in ("standing") triggers for
the brain's jobs, host-side provisioning, a ticker that watches a config
row, new model tiers in `config_defaults.json`, a third overlay. The owner
decided (2026-10-25) that the brain ships with **no change to the bao
runtime**. Everything it needs already exists for guest code:

- an unowned `agent_triggers` record runs on the election-active device,
  which stamps it (`triggers::reconcile_registry`, ADR-006 §4);
- a definition-core rewrite of a fired `once` re-arms it, and an
  `enabled` false→true flip resets the circuit breaker (ADR-006 §4,
  refresh);
- `any@v1` exposes `setup_app`, `ensure_bundle`, `create_type`,
  `create_dataset` (which passes its draft to the server unchanged) and
  `bundle_child`;
- `config.get` reads the live store row and `config.set` refuses only
  the secret namespaces.

## Decision

### 1. The programs ship in the agent overlay

`repos/_agent/programs/brainSetup@v1/program.py` and
`repos/_agent/programs/brainHeartbeat@v1.py`, plus the `brain-core`
skill in `repos/_agent/skills/`. They deploy with the agent overlay —
no new space, invite or `anybao.toml` entry, and no change to the
desktop shell, which writes the overlay list per release channel. A
separate `_brain` overlay stays a later option.

### 2. The contract is two config rows and one trigger record

| Row / record | Writer | Value |
| --- | --- | --- |
| `brain.space` (agent_config) | any-ui's switch, its first-open default, `brainSetup.on()/off()` from chat, or the setup run when it finds no row | `{spaceId: <Bao's space> \| null, requestedAt, by}` — absent = on (the default), `null` = off |
| `brain.status` (agent_config) | the setup run | `{spaceId?, requestedAt, state: provisioning \| ready \| error \| off, at, error?: {type, message}, hub?: {objectId, datasets}}` — `requestedAt` echoes the request it answers |
| `brain-setup` (agent_triggers) | whoever writes `brain.space` | `{name: "brain setup", kind: "once", spec: {at: <now>}, program: "agent:brainSetup@v1", args: {requestedAt}, enabled: true}`, no `owner` |

Each request rewrites `brain-setup` whole, which re-arms it. Nothing in
the contract is ever deleted: a deleted record id can never be written
again.

### 3. The setup run

`main(args)` without `args.mode`:

1. Read `brain.space`. Absent: write the default (`by: "default"`, Bao's
   space) and answer it. The status always echoes the row's current
   `requestedAt`, not the trigger's, so a second click wins.
2. `spaceId: null`: disable every brain job, remember which in the hub's
   `config/jobs.offDisabled`, answer `off`.
3. A space other than Bao's: answer `error` (`wrong_space`) and raise.
4. Otherwise answer `provisioning`, then install or adopt: the `people`
   and `meetings` catalog apps; the `brain/v1` bundle (a created root of
   type `page`); the hidden, bodiless `brain_hub` type; its four
   datasets `signals`, `config`, `incidents`, `observations`, each with
   its full field set; the `brain/hub/v1` child; the exclusion list
   (`config/exclusions`, `{version: 1, personIds, emails, domains}`) and
   the `llm.tier.brain.drafter` / `.extractor` / `.caretaker` rows,
   copied from `codegen` / `codegen` / `classify`, each only when absent.
   A `409 bundle.not_ready` is retried five times, two seconds apart.
5. Write the missing jobs, re-enable the ones the last off run disabled,
   answer `ready` with the hub. Any exception in 4–5 is answered as
   `error` and raised as `provision_failed`, so the run reads FAILED.

### 4. The brain's jobs are records

| Job (id when first written) | Spec | Program | Args |
| --- | --- | --- | --- |
| `brain-heartbeat` | `every_s: 600` | `agent:brainHeartbeat@v1` | `{space, hub, datasets}` |
| `brain-reconcile` | `every_s: 3600` | `agent:brainSetup@v1` | `{mode: "reconcile"}` |

- **Found by program and `args.mode`, not by id.** A job deleted by hand
  is rewritten under a fresh id (`<base>-<epoch>`), since its id is
  burned.
- **A pause holds.** The reconcile run only writes missing jobs; it
  never re-enables one. Only an on run re-enables, and only the jobs
  the off run before it disabled.
- Unowned when written, so the active device claims them; Pause and
  Repin in Scheduled work as for any job.

### 5. The heartbeat v0

Raises `unconfigured` without its args; reads `config/exclusions` and,
when it is missing or not version 1, writes one `signals` row
(`decision: refused`) and raises `exclusions_unreadable`; writes one
`heartbeat_gap` signal when its last tick is more than 1 200 s old;
records `config/heartbeat.lastTickAt`. It imports `any@v1` only.

## Consequences

- No file under `runtime/src/` changes. The cost, accepted:
  - the brain's jobs can be edited or deleted like any record — the
    hourly reconcile repairs deletions, an edit holds until fixed;
  - a request waits for the next ticker pass;
  - a runtime nobody opens the app against never starts the brain by
    itself (asking Bao in chat still works);
  - brain notices post under the literal agent name `bao`, as
    `remind@v1` does.
- What would need runtime code is kept in any-ui's
  `docs/plans/2026-10-25-ai-brain-later.md`: a send gate, built-in brain
  jobs, run ids stamped on writes, the configured agent name for guests,
  non-chat event triggers, enforced capabilities, a second Google
  account.
- Phase 0 ships no mail, calendar or meeting sync, no people, facts,
  cards or drafts — only the scaffold above.
