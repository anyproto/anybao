# Part 1: The AI brain — a plugin in the connectors overlay: setup, people, promises, facts, cards and drafts, the reply hand-off, the caretaker, chats

Status: **Accepted** (2026-10-29; consolidated from Part 1–039, each Proposed 2026-10-25…28)
Date: 2026-10-29
Builds on: ADR-005 (the loop core and its backends), ADR-006 §3–§4 (`agent_config` rows, trigger records, `once`, the breaker), ADR-009 (overlays), ADR-010 (docstrings as the doc surface), ADR-016 (the email corpus), ADR-017 §0 (`bao/v1` stores), ADR-018 (chat triggers), ADR-019 (instants), ADR-021 (credentials), ADR-023 §1 (synced `agent_runs` summaries), ADR-027 (parts and the usecase catalog), ADR-029 (one type, collections), ADR-030 (the judge tier)
Design record: any-ui `docs/plans/2026-10-24-ai-brain-on-any-bao-design.md`; plans: any-ui `docs/plans/2026-10-25-ai-brain-*.md`; what waits: any-ui `docs/plans/2026-10-25-ai-brain-later.md`

## Parts

| Part | What | Programs |
| --- | --- | --- |
| 1 | The brain as a plugin: the switch, the setup run, jobs as records, the hub | `brainSetup@v1`, `brainKit@v1` |
| 2 | People, promises, tasks from synced mail | `brainPeople@v1`, `brainPromises@v1`, `brainTasks@v1`, `brainRecall@v1` |
| 3 | Facts about people, and the model consent | `brainFacts@v1` |
| 4 | The decision layer: signals, cards, reply drafts bound to their bytes | `brainHeartbeat@v1`, `brainDrafter@v1` |
| 5 | The reply hand-off: the user sends from Gmail, the heartbeat notices | (a heartbeat pass) |
| 6 | The caretaker: deterministic faults, record-only repairs by rule | `brainCaretaker@v1` |
| 7 | Tasks from conversations: promises and asks from the chats the user opted in | `brainChatScan@v1` |

Code carries pointers as `ADR-033 §<part>.<section>`. The hub has ONE
shape: every store, job and tier row below is installed by one setup run;
nothing here is installed anywhere yet, so there is no migration ladder.
The hub records `version: 1`; a later shape bumps it, and a reconcile run
that finds a lower number re-arms setup (§1.3).

## Part 1 — The AI brain as a plugin in the connectors overlay — setup as a program, jobs as records, no runtime change

### Context

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

### 1.1 The programs ship in the connectors overlay, as a plugin

The brain's programs live in `repos/_connectors/programs/` —
`brainSetup@v1`, `brainHeartbeat@v1`, `brainDrafter@v1`, `brainPeople@v1`,
`brainPromises@v1`, `brainFacts@v1`, `brainRecall@v1`, `brainTasks@v1`,
`brainCaretaker@v1`, `brainChatScan@v1`, the shared `brainKit@v1` — every
program in the folder form `programs/<name>@v1/program.py` with its
`manifest.json` — with the `brain-core`, `brain-voice-email` and
`brain-investor-record` skills in `repos/_connectors/skills/`. Every
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

### 1.2 The contract is two config rows and one trigger record

| Row / record | Writer | Value |
| --- | --- | --- |
| `brain.space` (agent_config) | `brainSetup.on()` / `off()` only, after the user's yes in chat | `{spaceId: <Bao's space> \| null, requestedAt, by: "chat"}` — absent = off (nothing starts the brain on its own), `null` = switched off |
| `brain.status` (agent_config) | the setup run | `{spaceId?, requestedAt, state: provisioning \| ready \| error \| off, at, error?: {type, message}, hub?: {objectId, datasets}, mailSync?, stopped?: [{id, name}]}` — `requestedAt` echoes the request it answers; `stopped` is a warning on a `ready` brain (§4) |
| the setup once (agent_triggers, `brain-setup` at first) | whoever writes `brain.space` | `{name: "brain setup", kind: "once", spec: {at: <now>}, program: "connectors:brainSetup@v1", args: {requestedAt}, enabled: true, lastRunAt: null}` |

Each request rewrites the setup once with `lastRunAt: null`, which
re-arms it. The null is load-bearing: a whole-value `$set` MERGES into
the stored record, so a plain rewrite keeps the fired run's `lastRunAt`,
and the runtime reads the once as consumed and never fires it again (seen
on a live run, 2026-10-25). The once is found by program and kind, not
by id, like the jobs (§4): the user can delete it from the Scheduled list
like any schedule, and a deleted id is burned, so the next request writes
a fresh `brain-setup-<epoch>` — found live 2026-10-28, when a deleted
once left `on()` and `off()` failing for good with "record is deleted;
the id cannot be reused". Nothing in the contract is ever deleted.

### 1.3 The setup run

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
   type `page`); the hidden, bodiless `brain_hub` type; its
   datasets `signals`, `config`, `incidents`, `observations` (here),
   `promises` (§2.1), `facts` (§3.2), `cards`, `drafts`, `approvals`,
   `standups` (§4.1), `sends` (§5.4) and `asks` (§7.4), each `shared` and
   with its full field set — declared through `any@v1`'s
   `_create_dataset`, the plumbing export `enrich@v1` uses, which also
   adds a field a stored definition lacks; the `brain/hub/v1` child; the
   exclusion list (`config/exclusions`, `{version: 1, personIds, emails,
   domains, identities?}`), the blocklist (§2.1) and the drafting knobs
   (§4.1), each seeded only when absent; and the tier rows
   `llm.tier.brain.extractor` (§3.3) and `llm.tier.brain.drafter` (§4.6),
   each copied from `llm.tier.codegen` only when absent — one tier row per
   brain job that calls a model, so Settings → Model repoints each on its
   own. The hub records `version: 1`; a reconcile run that finds a hub
   recorded below the current number re-arms setup, which installs what
   the shape adds.
   A `409 bundle.not_ready` is retried five times, two seconds apart.
5. Write the missing jobs, re-enable the ones the last off run disabled,
   adopt or write the mail sync, and — for a NEW request only — re-enable
   the jobs the runtime stopped after repeated failures (`lastStatus:
   auto_disabled`; the flip resets the breaker). Answer `ready` with the
   hub, listing any job still stopped under `stopped`. Any exception in 4–5 is answered as
   `error` and raised as `provision_failed`, so the run reads FAILED.

### 1.4 The brain's jobs are records

| Job (id when first written) | Spec | Program | Args |
| --- | --- | --- | --- |
| `brain-heartbeat` | `every_s: 600` | `connectors:brainHeartbeat@v1` (§4.4) | `{space, hub, datasets, tier: "brain.drafter"}` |
| `brain-reconcile` | `every_s: 3600` | `connectors:brainSetup@v1` | `{mode: "reconcile"}` |
| `brain-people` | `every_s: 900` | `connectors:brainPeople@v1` (§2.3) | `{space, hub, datasets}` |
| `brain-promises` | `every_s: 1800` | `connectors:brainPromises@v1` (§2.4) | `{space, hub, datasets}` |
| `brain-facts` | `every_s: 21600` | `connectors:brainFacts@v1` (§3.3) | `{space, hub, datasets, tier: "brain.extractor"}` |
| `brain-caretaker` | `every_s: 900` | `connectors:brainCaretaker@v1` (§6) | `{space, hub, datasets}` |
| `brain-chat-scan` | `every_s: 1800`, `enabled` follows `brain.chatScan.spaces` | `connectors:brainChatScan@v1` (§7.3) | `{space, hub, datasets}` |

The mail sync is adopted, not a brain job (§2.2).

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
  reads the jobs live, so the chat tool still sees it. The one other
  restart is the caretaker's (§6.6): `enabled: true` on a stopped
  brain job, at most once per incident, by rule; it never touches a
  paused one.
- Unowned when written, so the active device claims them; Pause and
  Repin in Scheduled work as for any job.

### 1.5 The heartbeat's probes

Every heartbeat run (§4.4) starts with the probes: it raises
`unconfigured` without its args; reads `config/exclusions` and, when it
is missing or not version 1, writes one `signals` row (`decision:
refused`) and raises `exclusions_unreadable`; writes one `heartbeat_gap`
signal when its last tick is more than 1 200 s old; records
`config/heartbeat.lastTickAt`.

### 1.6 The hub's stores are shared datasets

A read of a shared dataset returns `id: "<objectId>/<recordId>"`, and an
`id` filter compares that whole value; writes take the plain id (any
`docs/03-api.md` § Shared datasets). Every lookup of a hub record —
`config/exclusions`, `config/heartbeat` — filters on
`f"{hub}/{name}"`. A lookup by the plain id finds nothing, which would
re-seed the exclusion list over the user's on every setup run and make
the heartbeat refuse every tick.

### 1.C Consequences

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
- Calendar and meeting sync are not part of this ADR; the passes that
  read them (`calendar_events`, §4.4) are no-ops until a sync declares
  the store.

## Part 2 — The AI brain's people, promises and tasks — from synced mail; promises by a model, quoted and grounded

### Context

This part gives the brain what it knows. The mail half needs no
new connector: `gmailSync@v1` already syncs mail into Bao's space as
`email_messages` on one `mailbox` object per address. What is missing is
a person per correspondent, the user's promises, and a way to turn a
promise into a task — without a runtime change; people and tasks need
no model, promises are extracted by one and grounded in plain code.

### 2.1 The stores

The setup run (§1.3) installs or adopts the catalog
`tasks` app; creates two hidden collections with `create_collection`,
idempotent by xKey and leaving the catalog's own definitions untouched —
`brain_person` (`emails`, `aliases`, `lastContactAt`, `firstSeenAt`,
`mailCount`, `meetingCount`, `preferredName`, `addressForm`, `language`)
and `brain_task` (`sourceUri`, `personId` → `profile`, `origin`,
`suggestedDeadline`); declares the shared `promises` dataset on the hub;
seeds an empty `config/blocklist` (`{senders, domains, labels}`, each a
list; `labels` are Gmail label ids, matched against a synced message's
`labelIds`, and a message under one is read past by every brain reader;
entries are trimmed and lowercased; since setup seeds it, a missing
list, or one in another shape — a field that is not a list of text —
fails the run, like the exclusion list); and writes the `brain-people` (900 s)
and `brain-promises` (1 800 s) jobs (§1.4).

### 2.2 Mail sync is adopted, not duplicated

Setup adopts a `connectors:gmailSync@v1` cron whose `args.space` is
Bao's space, or writes one (`brain-mail-sync`, every 900 s). A cron into
another space is named in `brain.status.mailSync.other` and left alone;
the brain reads only Bao's copy. Mail sync is not a brain job: switching
the brain off leaves it running.

### 2.3 `brainPeople@v1`

Reads From / To / Cc — never a body — of mail that landed after its
cursor (`config/people.cursor.mail`, per mailbox the `_ver.id` of the last
record handled, read through `brainKit.mail_since`, §2.6), and keeps one catalog person
per address: a person whose `brain_person.emails` holds it, else one
whose `person.email` equals it (filed under `brain_person`), else a new
`profile` filed under `person` and `brain_person`. The mailbox's own
addresses, noise senders, blocklisted senders and domains, and excluded
people never become people. It preloads the first 1 000 persons; one
past them is found by any of their addresses (`brain_person.emails`,
then `person.email`) and merged into, never overwritten or minted
twice. It reads only mail dated within the last 30 days, with or
without a cursor (the backfill lands older mail after the cursor too),
so a mailbox is never walked from its first message: a real mailbox
holds years of mail, which at 200
messages a run would take days to walk before reaching this month. It
caps unknown addresses per run and advances the cursor only past
messages fully handled; one message with
more new addresses than a run may mint (300) is a mailing list, read past
without minting anyone on it — kept, it would hold the cursor there for
good (`massSkipped` counts them). It then back-fills
`personId` on promises whose recipient it now knows — found the same way
when they are past the preload.

### 2.4 `brainPromises@v1` — a model extracts, plain code grounds

Reads the user's sent mail (From = the mailbox's address) that landed
after its cursor (`config/promises.cursor`, per mailbox the `_ver.id` of
the last record handled, `brainKit.mail_since`) and dated within the
last 30 days, with or without a cursor, so a promise from months ago is
never listed as open and overdue — and asks a model for the
commitments in each message. It is the second brain job that reads
mail with a model, so the user's consent (`brain.model`, §3.1)
covers it: without it the job answers `{skipped: "consent"}` and reads
nothing. Per sent message, in this order:

1. **Kept out?** A message to an excluded or blocklisted recipient, or
   under a blocklisted label, never reaches the model.
2. **Already extracted?** A message that has a promise record (by
   `source`) is not asked again, so a re-run — the cursor reset — cannot
   mint a second, differently cut quote for it.
3. **The gate.** When a judge model is configured (`llm.judge_available()`,
   ADR-030), one `noul` question per message — does the writer commit
   to doing something themselves later? — and a message under 0.3 is
   not extracted. Without a judge every sent message is.
4. **Extraction.** One `llm@v1.chat` call on the `classify` tier (or the
   job's `tier` arg) over the cleaned body (quoted `>` lines dropped,
   2 400 chars), answered as a JSON array of `{quote, due}`; one retry
   on an unparsable reply, then the message counts as read with no
   promises.
5. **Grounding, in plain code.** A quote is kept only when, whitespace
   normalized, it is a verbatim substring of the cleaned body and 8–240
   characters long. A paraphrase is dropped, never repaired. Every drop
   is counted by reason in `config/promises.stats`, counts only.
6. **The record.** The verbatim `quote` (also `text`), the key-form
   `source` link, `personEmail` and `personId`, `madeAt`, and `dueAt`:
   the fixed rule table first (today, tomorrow, end of week, next week,
   weekday names; EN and DE), the model's `due` ("YYYY-MM-DD") only when
   the table finds nothing. The id is `sha256(messageId, quote)`, and
   the job writes only ids the store lacks: `status` is mutable, so
   re-sending a promise would reopen one the user or a completed task
   closed. Any per-record rejection fails the run with the cursor
   unmoved, but one: a deleted promise's id is burned, so re-sending it
   answers `upsert.record_deleted`, which the job takes as the user's
   answer rather than stop at that message for good.
7. **The cursor** passes only messages fully handled, in the order they
   landed (`_ver.id`, §2.6); it is written only when something was read. A provider failure
   — llm@v1's `LlmError` or an `EffectError` (a timeout, a refused
   connection, a missing secret), from the judge as from the call — stops
   the mailbox before its message; a run in which no model call
   succeeded fails (`provider_failed`), so a provider that is down trips
   the runtime's breaker and `brainSetup.status()` names the stopped
   job. An earlier "two failures in one run" rule never fired with one
   mailbox: the job passed, run after run, for reading nothing, and the
   brain stayed `ready` (found live 2026-10-28). At most 60 model calls
   per run; the rest waits for the next one, thirty minutes later.

Steps 3–5 are one public helper, `extract(text)` → `(items, calls)`: the
chat scan's own messages go through it too (§7.3), so one gate, one
prompt and one grounding rule serve both channels. A failure raises
`model_failed`, whose `calls` is what was spent; `extracted(space, hub,
dataset, sources)` is the public dedup by `source` the chat scan shares.

The regex sentence splitter and phrase table an earlier draft used are
gone: a fixed phrase list misses most commitments and cuts sentences
wrong, and the verbatim-quote rule gives the same precision with far
better recall. Cost, at thirty sent mails a day on Haiku-class
pricing, is cents a month; with a judge in front, less.

It also marks done an open promise whose catalog task the user
completed, finding the tasks by the `taskId`s the promises hold rather
than by the `brain_task` collection: a task the Promises view makes in
any-ui is a plain catalog task, not filed there.

### 2.5 `brainTasks@v1`

A chat tool: `suggest(limit=20)` lists open promises without a task (`None` = all), each with a
deadline from one rule table (its own `dueAt`, an ISO date, today,
tomorrow, this week, next week, a weekday, else two business days, never
earlier than today); `create(id, deadline=None)` makes a catalog `task`
in Bao's Tasks app (`planning: inbox`) filed under `brain_task` and sets
the promise's `taskId`; a second `create` is a no-op. Neither offers nor
makes a task of a promise to someone the exclusion list or the blocklist
keeps out (`create` raises `person_excluded`), nor of one whose message
now carries such a person — an exclusion added after the promise was
read; the message is read back by the promise's `source`. Both lists are
read fail-closed. The `brain-core`
skill tells the chat agent to offer, show the deadline, and create only
after a yes.

### 2.6 `brainKit@v1`

The jobs share one helper module for the server rules Part 1 found
live: checked writes (a refusal is HTTP 200 with `rejections`), hub
records read by `<objectId>/<recordId>`, and the exclusion list read
fail-closed. Three more rules live there once:

- **Mail is read in the order it landed, not by date**
  (`mail_since(space, mailbox, cursor, limit, floor_ms)` → rows and the
  new cursor). The cursor is the last record's `_ver.id` — set once at
  creation, lex-monotonic, exact under a string `$gt`. A cursor on the
  message's `internalDate` passes mail for good: the Gmail backfill
  writes newest first, in fuel-bound slices across its own ticks, so a
  job that cursors on the date sets its cursor to today on its first
  tick and never sees the older mail that lands after it (found in
  review, 2026-10-29). `floor_ms` (message date, inclusive) bounds every
  read, with or without a cursor: the backfill lands mail older than the
  floor after the cursor too, and a job that read it would walk a year
  of mail. Rows come in landing order; a pass that needs date order
  sorts them.
- **One notice** (`notice(space, text, attachments)`): a chat message
  under the literal agent name `bao` with `done: true`, which the chat
  watcher skips as its own, so it starts no chat turn and raises one
  notification (§4.8). The runtime exposes no configured agent name to
  guests. A failure is printed and counted, never raised.
- **One identity → person rule** (`IdentityResolver`, §7.5): a chat
  account identity is kept out when the exclusion list names it
  (`identities`) or when any person it may be is excluded. `exclusions()` resolves the persons excluded by id to the
addresses on their person objects (`brain_person.emails`,
`person.email`), so `kept_out` treats a message carrying one of them
like one carrying a listed address — found live: an id-only exclusion
had let a CC'd person's mail reach another person's facts, promises and
recall. Resolving at read time cannot help once the person object is
gone — the people job would mint a fresh person for the same address,
and their mail would reach the model — so `brainSetup.exclude(person=…)`
also copies the person's addresses into `emails` (and
`brainSetup.include(person=…)` takes both out again — the way back in
is a chat tool too, after the user asks; an id whose object is gone is
removed as is, and mail already kept out is not re-read: the list stamps
`includedAt: {personIds, emails, domains: {item: ms}}`, the moment each
item was let back in, `exclusions()` resolves it to `readFrom: {address
or domain: ms}`, and `included_after(addr, excl, at_ms)` keeps a message
from before that moment out for everyone in it — the facts job needs it,
since its cursor is per person, not per message; the promises cursor
passes kept-out mail on its own), and refuses an id that names no person
(an object filed under `person` or `brain_person`). Excluding an item
again drops its stamp.
It writes only the fields it adds to, and refuses a list that is not in
its version-1 shape rather than rewrite it into a readable one: a list
the jobs refuse to read must stay refused until it is fixed. The heartbeat uses it too, and the tools read the hub off
`brain.status` through its `ready_hub()`.

### 2.7 `brainRecall@v1`

A chat tool, deterministic. `person(id)` joins one catalog person's
`brain_person` columns, what the user promised them and the latest mail
with them across Bao's mailboxes (`participants` intersects their
addresses). `search(query)` runs one index search per scope (`basic`,
`email`, `promises`; one call with a shared limit lets mail crowd out
the rest), reads each hit's record back for its date, and reranks by
score × 0.5^(age / half-life): objects 180 days, mail 60, a promise not
at all. A scope the index refuses is reported as skipped. Both honour
the exclusion list and the blocklist: mail with an excluded or
blocklisted participant, a promise made to one, and an excluded person
never appear — an object hit that is a person kept out by address or
domain, and a promise whose message now carries one, included — and `person` on an excluded person raises. Calendar and
meetings join as scopes when their syncs land.

### 2.C Consequences

- Verified on a scratch `any` server with `anyrt serve`: an existing
  install upgraded itself through reconcile; seeded mail yielded one
  person per real correspondent (noise and the user skipped) and exactly
  the promises in sent mail, each linked to its person with the right
  deadline; a promise became a task linked to its person.
- Also verified there: `brainRecall` found the deck across objects,
  mail and promises, newest mail first, and its person view joined a
  person's mail and promises; a task the Promises view made in any-ui,
  once completed, closed its promise on the next `brainPromises` run.
- Calendar sync, meeting sync, and the people join on calendar
  attendees and transcript speakers wait for the live probes
  the plan names (Google Calendar's token path, Meet transcripts,
  Granola's wire shape).

## Part 3 — The AI brain's facts about people — from mail, quoted and grounded

### Context

This part gives the brain a model of the people the user works
with: where someone lives, how they want to be addressed, what they
said they care about. It is the first brain step that reads mail with
a model, so two things matter more than recall: every fact must be
traceable to the exact sentence it came from, and nothing may read
mail with a model until the user said so.

### 3.1 The model reads mail only after the user's consent

A config row `brain.model` (`{allowed: true|false, requestedAt, by:
"chat"}`) is the consent. Only `brainSetup.allow_model()` and
`deny_model()` write it, after the user's plain-words yes in chat to
this question: may the brain read your mail — and your own messages in
the chats you name — with a model, to keep facts about the people you
write with and the things you promised? (The `brain-core` skill carries
the wording; no client writes the row — owner decision, 2026-10-27, the
same as the brain's own switch, §1.2.) It is absent, so off, until
then, and separate from the switch. It covers every brain job that puts
the user's words in front of a model — the facts job here, the promises
job (§2.4), the reply drafter (§4.6) and the chat scan's own-message pass
(§7.3) — each of which returns `{skipped: "consent"}` without reading a
message or calling a model unless `allowed` is `true`; the people job,
the heartbeat, the caretaker and the asks pass need no model and run
without it. `brainSetup.status()`
reports it as `modelAllowed`.

### 3.2 The `facts` store

The setup run declares `facts` on the hub (shared; history kept, so a
supersession is auditable): `personId`, `kind`, `value`, `quote`,
`source`, `channel`, `observedAt`, `origin`, `confidence` and `model`
are written once; `status`, `supersedes`, `supersededBy`,
`moreSources`, `decidedAt` and `confirmedAt` change. A fact's id is
`sha256(personId | kind | normalized value)`, so re-extraction is
idempotent and a second source for the same value is appended to
`moreSources` (at most 5) rather than minting a duplicate. Kinds are a
closed table in the program; adding one is a program change.

### 3.3 `connectors:brainFacts@v1`, every six hours

1. **Consent, then exclusions.** No consent → skipped. The exclusion
   list is read fail-closed before any mail: missing or unreadable
   raises `exclusions_unreadable`, and so does a failed read of the
   persons it excludes by id.
2. **Work.** Persons with mail since the cursor, minus excluded ones,
   at most 40 per run, asked of the server in contact order (then id) a
   page at a time (an unordered capped read drops people the watermark
   then passes). The cut never falls inside a group that shares a contact
   time — everyone on one message does — since the watermark moves to the
   last one read and the next run starts after it; so a run can exceed 40
   by one such group. A (time, id) cursor would remove that excess and is
   left for later. A message with any excluded or blocklisted
   participant, or under a blocklisted label, is out for everyone in it; a person excluded by id is
   excluded by every address on their person object too. So is a message
   from before one of its participants was let back in (the exclusion
   list's `includedAt`, §2.6): the cursor here is per person, so
   lifting an exclusion would otherwise re-read the mail it kept out —
   found live 2026-10-28, when an included person's "I moved to Lisbon"
   from their excluded days became a fact.
3. **One model call per person** (`llm@v1.chat`, tier
   `brain.extractor`, no tools): the person's name, their current facts,
   and up to 8 000 characters of their newest mail, each message tagged
   `[M1]`, `[M2]`…. The reply is a JSON array of `{kind, value, quote,
   ref, confidence}`; one retry on an unparsable reply.
4. **Grounding, in plain code.** An item is kept only when its kind is
   in the table, its `ref` names a tagged message, its `quote` is a
   verbatim substring of that message after whitespace normalization
   (≤ 240 characters), its value is 1–200 characters, and its confidence
   (capped at 0.8) is at least 0.6. A model that paraphrases loses the
   fact: precision over recall. Every drop is counted by reason in
   `config/facts.stats`, which holds counts only, never names.
5. **Writes.** A single-valued kind (address form, language, preferred
   name, channel, location, role, company) supersedes a current
   inferred fact: the new record names `supersedes`, the old one turns
   `superseded` with `supersededBy`. A fact the user stated (or
   confirmed) is never superseded by inference; the conflict is a
   `fact_conflict` signal instead. `history.consent` lands `proposed`
   and needs the user's accept. Every fact records the model that
   proposed it.
6. **Cursor.** Per mailbox, advanced only after the person's writes
   landed; a provider error counts the person, holds the watermark and
   moves on; two in a row stop the run, and so does a run in which no
   call succeeded (`provider_failed`) — one person with new mail and a
   provider that is down would otherwise pass for done, run after run,
   with the watermark held for good and the brain `ready`.

`unpinned` is raised when no `llm.tier.brain.extractor` row exists. The
setup run seeds that row from `llm.tier.codegen` when absent, so on a
desktop whose Bao runs on the user's Codex or Claude Code login through
the shell's local AI proxy (the `anyai` backend), the extractor uses
that login with no API key.

### 3.4 The card is recall's person view

`brainRecall@v1.person(id)` (§2.7) is the card, one deterministic
tool and one shape: the person's columns plus `addressForm` and
`language` from their preference facts, their current facts grouped
(tastes, views, preferences, personal, work, history) each with its
verbatim quote and source, proposed facts apart, superseded ones gone,
what the user promised them with status, what they asked the user for
(`asked`, the `history.asked_for` facts), their latest mail, and
`markdown` — the card for chat, which quotes every fact and lists the
open promises and the latest threads. A separate brief tool would be a
second tool for the same question, so there is none. The view honours
the exclusion list.

For an investor, the deeper record is a skill rather than a tool:
`repos/_connectors/skills/brain-investor-record.md`. The agent runs it for
one person, on request (or after a yes to its one offer), and writes the
person's record page in the user's investor collection: a helicopter
callout, folded evidence, and every claim numbered against an exact quote
with its mail or transcript-line link. It uses this view as its map and
re-reads the sources it cites. It is a skill because it is judgment over
many sources and one page write, which the agent does with the tools it
already has, so it needs no new program. brain-core says when a person
counts as an investor.

### 3.C Consequences

- No runtime change: the extractor is a guest program on the existing
  `llm@v1`, and the model it uses is whatever the extractor tier names.
- Verified on a scratch `any` server with `anyrt serve`, the extractor
  tier pointing at a loopback stand-in for the desktop's local AI proxy
  that answers each request with one `codex exec` run.
- Facts from meeting transcripts, the per-meeting trigger, and the
  user's own facts wait for meeting sync and later phases.

## Part 4 — The AI brain's decision layer — signals, cards, reply drafts bound to their bytes

### Context

Phases 0–2 gave the brain what it knows: people, promises and facts from
the user's mail. This part makes it notice and ask. Every ten minutes a
deterministic loop looks at what arrived and what is due, writes down what
it saw — including what it chose to do nothing about, and why — and turns
what needs the user into cards. For mail that needs a reply it queues a
drafter that writes a draft and a pending approval bound to the draft's
exact bytes. Nothing sends: Part 5 hands an approved reply to
Gmail and the user presses send. No runtime change.

### 4.1 Four stores on the hub

The setup run (§1.3) declares four more shared datasets on the hub,
each `idRule: user`, `deleteBy: author`, with `creator`, `createdAt` and
`modifiedAt` stamps. ★ = required, ✎ = `mutableBy: author`; every other
field is written once.

| Dataset | Fields |
| --- | --- |
| `cards` (search `{title: title, text: [reason], scope: cards}`) | `kind`★, `title`★, `reason`★, `subjectKey`★, `sourceUri`, `signalId`, `personIds` (array), `briefPersonIds` (array), `status`✎, `snoozedUntil`✎ (datetime), `answer`✎, `draftId`✎, `approvalId`✎, `draftAttempts`✎ (number), `decidedAt`✎ (datetime) |
| `drafts` | `cardId`★, `sender`★ (mailbox object id), `senderAddress`★, `recipients`★ (array, lowercased), `format`★ (`text/plain`), `body`★, `subject`★, `inReplyTo` (message record id), `threadId`, `recipientProvenance`★ (`thread` \| `contact`), `model`★, `tier`★, `origin`★ (`drafter` \| `user`), `supersedes` (draft id) |
| `approvals` | `draftId`★, `cardId`★, `sender`★, `recipients`★, `format`★, `bodyHash`★ (sha256 of the body alone, hex), `reason`★, `status`✎, `decidedAt`✎, `consumedByRun`✎, `rejectedReason`✎, `editedTo`✎ |
| `standups` | `job`★, `tick`★ (run start, epoch seconds), `did`★ (array of short lines), `needs` (array), `counts`★ (object), `runId` |

Vocabularies. Card `kind`: `reply`, `nudge`, `overdue_promise`,
`meeting_soon`, `calendar_conflict`, `fact_conflict`, `info`, `question`,
`incident` (§6.8), `new_message` and `intro_step` (reserved),
`task_suggestion` (§7.7), `send_check` (§5.5). Card `status`:
`open`, `snoozed`, `answered`, `dismissed`, `sent` (§5.3). Approval
`status`: `pending`, `approved`, `rejected`, `void`, `consumed` (the user
sent it, Part 5), `held`. A draft is never edited: an edit is a new draft
(`origin: user`, `supersedes`) and a new approval, and the old approval
turns `void` with `editedTo` (any-ui writes it).

`config` gains two records: `drafting` (`{maxPerRun: 3, replyWindowDays:
5, meetingSoonHours: 24, quietSenders: [regex…]}`, seeded when absent and
the user's afterwards) and `heartbeat.cursor` (`{mail: {mailboxId: <_ver.id>},
stale: {mailboxId: ms}, factConflicts: tick}`, the heartbeat's own).

### 4.2 Signals: one per observation, with a decision and a reason

`signals` (Part 1) gains no fields. The heartbeat writes one row for
everything it saw, with `decision` ∈ `none | quiet | card | draft |
refused` and the required `reason`, and `cardId` when a card covers it.
Kinds: `new_mail`, `cc_thread`, `stale_thread`, `overdue_promise`,
`meeting_soon`, `calendar_conflict`, `card_woken`, `heartbeat_gap`,
`exclusions_unreadable`, `blocklist_unreadable` (and `fact_conflict`,
which the facts job writes). A signal's id is `<tick>-<kind>-<first 12 hex of
sha256(subject)>`, one per (tick, kind, subject); `subject` is a link —
the message (`any://o/<space>/<mailbox>/email_messages/<id>`), the card,
the promise or the event record. A cc-only thread is a `quiet` signal and
no card, which keeps Cards for what needs an answer.

Mail with an excluded or blocklisted participant, or under a blocklisted
label, gets one `refused` signal ("kept out of the brain") and nothing
else — decided from the headers, no thread read. The signal is also the
record that the message arrived while someone was kept out: the drafter
never reads a thread message that carries one (§6), so mail that arrived
while a person was excluded is never read after they are let back in.

### 4.3 Card identity

A card's id is `sha256(utf8(kind + "|" + identity))`, hex. `identity` is
the card's `subjectKey` — the thread id for a nudge, the promise id, the
event id, the sorted `"<idA>|<idB>"` pair, the fact-conflict signal id —
except for a `reply`, whose identity is `"<threadId>|<messageId>"` and
whose `subjectKey` is the thread id. A re-run writes no second card, and
a card that exists is never rewritten, since its `status` is the user's
answer. One reply card per message rather than per thread: once the user
has answered or dismissed a thread's card, the next message in it that
needs a reply gets a card of its own; while a reply card for the thread is
still open or snoozed, it covers the new message (`decision: card`, the
card's id). Of several unanswered messages in one thread in one run, the
newest gets the card and the others `decision: none`.

### 4.4 `connectors:brainHeartbeat@v1`

The `brain-heartbeat` record, every 600 s; args `{space, hub, datasets,
tier}`. It imports `any@v1` and `brainKit@v1` only — no model, no
connector, no web. No mail body reaches a model from here: the passes
below read headers, and the one pass that reads a body (sent detection,
§5.3) reads the user's own sent mail in plain code. Per run:

0. The probes (§1.5): the exclusion list and the blocklist read
   fail-closed (each a refused signal and `exclusions_unreadable` /
   `blocklist_unreadable`); a `heartbeat_gap` signal after more than
   1 200 s.
1. Snoozed cards whose `snoozedUntil ≤ now` turn `open` (`card_woken`).
2. **Mail.** Per mailbox, the messages that landed after its cursor
   (`brainKit.mail_since`, §2.6: `_ver.id`, so mail the backfill lands
   later is still seen) and whose `internalDate` is within
   `replyWindowDays` — a standing filter on every read, so mail older
   than the window is never carded whenever it lands — at most 200 a run,
   sorted by date for the decisions. The user's own mail is
   not an inbound observation. Decisions, in order: kept out → `refused`;
   a noise sender (Part 2's rule) or one matching `quietSenders` →
   `quiet` "automated sender"; mail Gmail files under Promotions, Social,
   Updates or Forums (or Spam, Trash) → `quiet` "a Gmail bulk tab", since
   a newsletter often comes from a plain address no sender rule catches;
   the user in To with a newer message from
   the user in the thread → `none` "already replied"; the user in To →
   a reply card (`reason` "addressed to you, no reply yet"), unless a
   closed reply card already covers the thread → `none` "a closed reply
   card covers this thread"; the user only in Cc → `cc_thread`, `quiet`;
   neither → `quiet` "not addressed to you". The rules are the pure
   `decide_mail(message, own, replied, quiet, kept_out)`. The reads
   behind them — the thread, the covering card, the card's existence, the
   sender's person — are one `$in` query each per mailbox, never one per
   message.
3. **Stale threads.** A message from the user that has just crossed the
   reply window (its own cursor on the message date, so each is judged
   once; a first run looks 14 days further back), to a known person,
   still the newest in its thread → a `nudge` card ("you wrote last, no
   answer for N days"), once per thread; a dismissed nudge stays
   dismissed. This pass judges by date, so a sent message the backfill
   lands below its cursor is not nudged — a nudge for a weeks-old thread
   is worth little.
4. **Overdue promises.** Open promises with `dueAt < now`, minus those
   kept out → an `overdue_promise` card ("due YYYY-MM-DD").
5. **Calendar.** One 7-day read of `calendar_events` on the hub
   (`start`, `end`, `title`, `attendeeIds`, `attendees`, `status`,
   `source`, per the design record §4.2) serves both passes; an event
   with any kept-out attendee is out of both. Events starting within
   `meetingSoonHours` with at least one known attendee → `meeting_soon`
   (`briefPersonIds`); overlapping non-cancelled events in the next 7
   days → one `calendar_conflict` per pair (at most 20 a run). No-ops
   while no calendar sync has declared the store.
6. **Fact conflicts.** `fact_conflict` signals since the cursor → a
   `fact_conflict` card naming the person; no second signal.
7. **Sent detection** (§5.3).
8. **Drafting** (§4.5).
9. **Writes, notice, standup, cursors.** Cards, then drafting, then the
   signals; one chat notice when cards were created (§4.8); one
   `standups` row when the run read or wrote anything; the cursors last,
   so a failed write leaves them where they were; `heartbeat.lastTickAt`.

Raises: `unconfigured`, `exclusions_unreadable`, `blocklist_unreadable`,
`cursor_corrupt` (`heartbeat.cursor` is `{mail: {mailboxId: <_ver.id>},
stale: {mailboxId: <internalDate ms>}, factConflicts: tick}`; anything
else, a numeric mail cursor included, is corrupt), and `write_rejected`
on any refused record.

### 4.5 Drafting: one-shots the heartbeat arms and counts

For each open reply card with no `draftId` and `draftAttempts < 3` —
the open reply cards from before the run, oldest first, then the cards
this run created — at most `maxPerRun` a run, the heartbeat writes a `once`
trigger `{kind: once, spec: {at: now}, program:
"connectors:brainDrafter@v1", enabled: true, args: {space, hub, datasets,
tier, cardId, signalId, messageId, mailboxId, threadId, recipients:
[<the source's From address>], provenance: "thread"}}`, unowned, on the
`bao/triggers/v1` anchor. Its id is `brain-draft-<messageId>`, then
`-a2`, `-a3`: the heartbeat derives the id from `draftAttempts` and
increments the counter AFTER the trigger write landed, because a one-shot
can die without writing anything (fuel, the wall, `unpinned`, a provider
failure) and the drafter never touches the counter. The rules that keep
it single and fresh:

- An id that exists is never written again — re-upserting a fired once
  re-arms it. Found by query first; one that exists while the counter
  says otherwise (the trigger landed, the counter did not) catches the
  counter up.
- While the last attempt's trigger has not fired (no `lastRunAt`), no
  next attempt is armed.
- Nothing is armed without the user's consent to reading mail with a
  model (`brain.model`, §3.1): the drafter would read nothing, and
  an attempt would be spent. The card stays, its signal says why
  (`decision: card`), and the standup asks for the consent; a later run
  arms it once given. A source message kept out by now is not armed
  either.
- A card past the budget is armed on a later run (`decision: card`,
  "draft budget"); a card with three attempts stays a card.
- A reply card whose source message is gone, or kept out by now, is
  closed (`status: dismissed`, `answer` says why) so it is not re-read
  every tick.
- The cards a run just wrote are armed from memory, not read back: a
  read after a write may not see it yet.

The split keeps the heartbeat's run short and a drafting failure on its
own one-shot, but it does not shield the ticker: due triggers run one
after another, so a slow draft delays the next tick. The bounds are
`max_tokens`, `llm@v1`'s stream timeouts and the budget of three a run.

### 4.6 `connectors:brainDrafter@v1`

One reply, no tools, no send capability; its only writes are one
`drafts` record, one `approvals` record, the card's `draftId` /
`approvalId` and one `standups` row. In order, nothing read before the
step that needs it:

1. **Consent** (`kit.model_allowed()`): without it, `{skipped: consent}`
   and nothing read. A switched-off brain (`brain.space` null) is
   `{skipped: off}`.
2. **The tier row** `llm.tier.<args.tier>` (`brain.drafter`): absent →
   `unpinned`, before any model call. Setup seeds it from
   `llm.tier.codegen` when absent (§1.3: one row per brain job that
   calls a model).
3. **The card**: gone, already drafted or closed → skipped.
4. **Provenance, in plain code, before the model.** The source message
   exists in its mailbox and thread; every recipient the heartbeat fixed
   is its From address or one of its `participants`; none is the
   mailbox's own address. Else `recipient_provenance`. The model never
   chooses a recipient. A mutation test removes this check and shows the
   outsider getting a draft.
5. **Exclusions**, fail closed: a source message or recipient kept out
   now → `{skipped: kept_out}`. The context drops every thread message
   kept out now and every one with a `refused` signal (§2).
6. **Context**: `brainRecall@v1.person(id)` — its `markdown`, `addressForm`
   and `language` (§3.4; a person kept out there skips the draft) —
   and the thread's last five messages up to the source, oldest first,
   each cleaned of quoted lines and cut at 1 500 chars. The voice is the
   `brain-voice-email` skill (§9).
7. **One `llm@v1.chat`** (`tools=[]`, `max_tokens` 1 200), one retry: two
   model calls at most, counted as calls. `LlmError` and `EffectError`
   (timeouts, connection failures, a missing secret) are both provider
   failures; a failure on every call raises `provider_failed`, so a
   failure never reads as a done run. Replies with no text raise
   `empty_draft`.
8. **Writes**, in order: the draft (`text/plain`, the body trimmed and
   ended with one newline, `subject` "Re: …", `model` from the tier row,
   `origin: drafter`); the approval for the hash (§7), `status: pending`,
   unless that id exists; the card's pointers; the standup (id
   `drafter:<tick>:<card id prefix>`).
9. A run that took more than 90 s raises `draft_timeout` after the writes
   — informational: the draft stands.

### 4.7 The approval hash

`sha256(utf8(sender + "\n" + recipients + "\n" + format + "\n" + cardId +
"\n" + body))`, hex, where `sender` is the mailbox object id,
`recipients` the addresses lowercased, sorted and joined by `,`, and
`cardId` the reply card the draft answers. Nothing else is normalised.
The approval's id is the hash, so a changed comma is a different
approval; the card id is in it because two cards can get byte-identical
drafts ("Thanks, that works.") and must not share one approval row
(found in review, 2026-10-29). Python (`brainDrafter@v1.hash_approval`)
and any-ui compute it alike; the pinned vector:

```
sender "mbx1", recipients ["B@x.io", "a@x.io"], format "text/plain", cardId "card1", body "Hi\n"
→ hashed text "mbx1\na@x.io,b@x.io\ntext/plain\ncard1\nHi\n"
→ 4c2c1965ae330a37c2f7489624499b742bd59222d06e86d9a16b4cb5054cf570
```

### 4.8 Standups, not chat messages; one notice

Any chat message whose `agent.name` is neither empty nor the serving
agent's name starts a chat turn, and every `done: true` agent message
raises a notification. So each brain job writes one `standups` row per
run that did something, and the Cards view shows them with repeats
collapsed. The chat gets one notice per heartbeat run that created
cards: `{text: "<n> new cards: <titles…>", agent: {name: "bao", done:
true}, attachments: {cards: {type: "brain_cards", link:
"any://o/<space>/<hub>/cards/<newest card id>"}}}` — the record form of
the link, through `brainKit.notice` (§2.6), under the literal name `bao`.
A failed notice is logged and counted, not raised: the cards are
written.

### 4.9 The voice is a skill

`repos/_connectors/skills/brain-voice-email.md` ships English and German
defaults (greeting and sign-off, du/Sie from `addressForm`, language,
length). The drafter reads it with `get_skill`, which reads Bao's space
first, so a skill of the same name there overrides it; a missing skill
falls back to a one-paragraph built-in voice. `brain-core` gains a
section: cards, drafts, approvals and standups are written by the
brain's programs and the user; the agent never approves, rejects,
consumes or sends, and quotes a card's `reason` when asked about it.

### 4.10 The setup run

The setup run (§1.3) declares the four stores, seeds `config/drafting`
when absent, seeds the `llm.tier.brain.drafter` row, and writes the
heartbeat job with `tier: brain.drafter` and the hub's datasets in its
args.

### 4.C Consequences

- No runtime change; the drafter is a guest program on `llm@v1`, and
  nothing in the brain can send mail.
- Not in this part: sending or any outbound channel (Part 5 hands the
  reply to Gmail), drafting new messages to people outside a thread (the
  `new_message` kind is reserved), the intro playbook, snooze rules
  learned from behaviour; the caretaker is Part 6.
- Not yet verified live: that the `bao`-named notice starts no chat turn
  (the watcher's self-skip), and the whole path on a real Gmail account —
  the owner's live test.
- `calendar_events` is read by the field names the design record gives;
  when calendar sync lands, its declaration is checked against them.

## Part 5 — The AI brain's reply hand-off — the user sends from Gmail, the heartbeat notices

### Context

After Part 4 an approved reply waits: nothing in the brain can send.
Giving Bao a send path that keeps "whatever reads untrusted mail cannot
also send" needs host code — a host-only send credential, a grant the
guest cannot forge, an `outbox.send` syscall — which the owner ruled out
for now (2026-10-25; Gmail stays read-only, 2026-10-28). So the first
version hands the approved text to the user, who sends it from Gmail, and
the brain notices the sent reply in the mail it already syncs.

### 5.1 The user sends; the brain cannot

Google access stays read-only (`gmail.readonly`), so the property holds
by construction: no brain program, and no chat turn, holds a credential
that sends mail. Gmail drafts are not used either: the scope that writes
them (`gmail.compose`) can also send them, which would put a send
capability behind the same guest-injectable token.

### 5.2 The hand-off is any-ui's

After Approve, the reply's card offers Open in Gmail (the thread, with
the approved text on the clipboard), Open as new message (Gmail's compose
window prefilled with recipients, `Re:` subject and, when it fits, the
text), Copy reply and Mark sent. The Gmail links are not a public
contract; any-ui probes them. Nothing here depends on
which one the user takes.

### 5.3 Sent detection, a heartbeat pass

A pass of the heartbeat (§4.4 step 7), before drafting. Of the oldest
200 `approved` approvals, the ones with no `send_check` card (§5.5) are
examined first and the ones already asked about after them, each group
oldest decision first, 50 a run in all, so approvals the user never sent
cannot starve a fresh one (found in review, 2026-10-29; past 200 of them
the ADR-level fix is to turn an asked-about approval `held`). For each
with no `sends` row:

1. Read its draft. A recipient kept out of the brain now → skip it: no
   mail is read for it and no check card is opened.
2. Look for the user's message after the approval: first in the draft's
   thread (`threadId`, `internalDate > approvedAt`, newest first), then —
   for a reply sent with Open as new message, which Gmail may file in a
   new thread — among messages to the draft's recipients
   (`participants ∋ recipient`, same time bound). `approvedAt` is the
   approval's `decidedAt` (any-ui stamps it on Approve), else its
   `modifiedAt`.
3. A message matches when it is from the mailbox's own address, carries
   no participant kept out, and its cleaned text contains the approved
   body's first 200 characters, both sides normalised the same way:
   quoted (`>`) lines dropped, markdown backslash escapes removed,
   whitespace collapsed, case folded. The escapes matter because synced
   mail is the HTML-to-markdown conversion of what Gmail sent. A reply
   rewritten in Gmail does not match; Mark sent and the check card cover
   it, and a looser match would mark the wrong message.
4. On a match: one `sends` row (`how: detected`), the approval
   `consumed`, the card `sent`, an open check card for the approval
   `answered`, and a `reply_sent` signal (`decision: none`). No chat
   message: success is silent.

A `sends` row that exists while its approval is still `approved` (Mark
sent half-written) is completed the same way, with no second row.

### 5.4 The `sends` store

Shared, `idRule: user`, `deleteBy: author`, every field written once,
`creator` and `createdAt` stamps: `approvalId`★, `cardId`★, `threadId`★,
`messageId` (the detected message; absent when marked by hand), `how`★
(`detected` | `marked`), `at`★ (datetime). The id is the approval id, so
a second detection — or a detection after Mark sent — writes nothing.
Mark sent is any-ui's: the `sends` row with `how: marked`, the approval
`consumed`, the card `sent`.

### 5.5 The seven-day check

An `approved` approval with no `sends` row seven days after `approvedAt`
opens one card `kind: send_check` ("Did you send this reply?"), once per
approval: id `sha256("send_check|" + approvalId)`, `subjectKey` and
`approvalId` the approval id, `draftId` the draft, `sourceUri` the
original card (`any://o/<space>/<hub>/cards/<card id>`), `personIds`
copied from it; a `send_check` signal (`decision: card`). Its answers are
any-ui's: Yes, it's sent (Mark sent) and Not yet (a snooze).

### 5.6 Vocabulary (adds to §4.1)

Card `kind` gains `send_check`; card `status` gains `sent`; signal kinds
gain `reply_sent` and `send_check`. Approval `consumed` now means "the
user sent it", detected or marked.

### 5.7 The setup run

The setup run (§1.3) declares `sends`; the heartbeat job is the one of
§4.4.

### 5.C Consequences

- No runtime change and no new consent; the brain still cannot send.
- The match rule misses a reply rewritten in Gmail, and one whose first
  200 characters Gmail or the markdown conversion change beyond the
  normalisation above (a link the user's mail client rewrote, a
  placeholder the user filled early in the text); Mark sent and the check
  card are the fallback.
- Bao sending by itself — the `google_send` host-only consent, the
  `approval_grant` attachment and `outbox.send` — stays on any-ui's
  `docs/plans/2026-10-25-ai-brain-later.md`.
- Not yet verified live: a reply sent from Gmail turning a card Sent
  within one mail sync and one tick — the owner's live test.

## Part 6 — The AI brain's caretaker — deterministic faults, record-only repairs by rule

### Context

The brain runs unattended: a heartbeat, people, promises, facts and a
mail sync, each an ordinary trigger record. When one of them breaks —
stopped by the breaker, pinned to a device that is gone, a model that
fails on every call — nobody notices until the user asks why the brain
knows nothing new. The caretaker is a brain job that watches the others,
from the same records the UI shows, and either repairs what it may or
tells the user. It calls no model: each fault class has one to three
sensible actions, and a rule picks one; a model turn constrained to the
same choices would add a tier row, a consent question and a breaker of
its own for no better choice (review decision, 2026-10-29). It never
touches anything a repair could make worse: secrets, approvals,
exclusions, deletions, other people's jobs.

### 6.1 The job

`connectors:brainCaretaker@v1` is a brain job like the others (§1.4):
`brain-caretaker`, `every_s: 900`, args `{space, hub, datasets}`,
written by the setup run and found by program. `unconfigured` when the
args lack the hub, `incidents`, `observations` or `signals`. It reads the
hub's live datasets from `brain.status.hub` when it names the same hub,
and uses a store only when that map names it: a job's args may predate a
store the hub declares now.

### 6.2 The snapshot

One pass reads, for the detectors: the `agent_triggers` records (the
runtime's rollup lives on them: `enabled`, `lastRunAt`, `lastStatus`,
`consecutiveFailures`, `owner`), among them the caretaker's own record
(its `spec.every_s` is the unit of its gap rule, §6.3); per brain job
its newest synced `agent_runs` summary (`bao/runs/v1`, filter
`triggerId`, sort `-startedAt`, limit 1 — the body any-ui's run list
sends); `brain.space` and `brain.status`; the exclusion record; the tier
rows the brain's jobs use; the newest `sync_state` object;
`googleAuth.status()`; the devices registry's active `bao` device; open
`reply` cards when the hub has `cards`.

`agent_runs` is read rather than `effects.runs`: the local mirror
covers this device only, so a device switch would read as every job
overdue. A field the run could not read is the literal string
`"unavailable"` wherever it is used — in the evidence — never omitted.
Three reads are required, and their failure raises `snapshot_failed`:
the trigger records (nothing can be graded without them), the non-closed
incidents (nothing can be reconciled) and the caretaker's own state,
`config/caretaker` (a run that could not read it would send a notice
twice).

### 6.3 Fault classes

Detection is plain code over the snapshot. A brain job is a `cron`
record whose `program` starts `connectors:brain`; the caretaker does not
grade itself as overdue (it is running).

| Class | Condition | Subject | Evidence |
| --- | --- | --- | --- |
| `job_overdue` | an enabled brain job whose last run — the newest of its record's `lastRunAt`, its newest `agent_runs` row and its caretaker reruns — is older than `2 × every_s + 120 s`, counted from no earlier than `uptimeSince` (below); a job that never ran counts from `brain.status.at` | trigger id | `lastRunAt`, `lastStatus`, `everyS`, `overdueS`, `owner`, `activeDevice` |
| `job_failing` | a brain job with `consecutiveFailures ≥ 2`, or `lastStatus: auto_disabled`; read from a job re-enabled and not run since (`enabled` with `auto_disabled`, §6.5) it opens no incident and only holds a pending repair (§6.7) | trigger id | `consecutiveFailures`, `lastStatus`, `lastErrorType` (newest run's `errorType`), `lastRunAt`, `enabled` |
| `mail_stale` | the mail sync trigger is enabled (a paused one is the user's choice, no fault) and `googleAuth.status().connected` is `false` (a dead credential makes the sync return an error dict without raising, so its run status is no signal), or the newest `sync_state`'s `modifiedAt` is older than 6 h counted from no earlier than `uptimeSince` (no `sync_state` yet: from `brain.status.at`) | mailbox id (`sync_state.mailbox_id`, else `mail`) | `syncStateModifiedAt`, `credentialConnected`, `cursor`, `skippedCount`, `mailTriggerId` (`brain.status.mailSync.triggerId`), `lastRunStatus` |
| `exclusions_unreadable` | `config/exclusions` missing or not in its version-1 shape (`brainKit.exclusions_problem`) | `config/exclusions` | `present`, `version`, `parseError` |
| `tier_unpinned` | a tier an enabled brain job calls — its `args.tier`, else the program's own (`classify` for promises, `brain.extractor` for facts), and the heartbeat's `brain.drafter` — has no row or no `model` | `llm.tier.<tier>` | `present`, `provider`, `model` |
| `setup_blocked` | `brain.space` asks for a state `brain.status` has not answered — waiting, setting up or switching off — for over an hour counted from no earlier than `uptimeSince`; or setup's once (found by program and kind, as setup finds it: a deleted `brain-setup` comes back under a fresh id) is gone | `brain-setup` | `requestState`, `requestedAt`, `answeredAt`, `ageS`, `setupTriggerPresent`, `setupOwner`, `setupLastRunAt`, `activeDevice` |
| `draft_backlog` | once the hub has `cards`: ≥ 3 open `reply` cards older than 1 h without `draftId`, or any with `draftAttempts ≥ 3` | `cards` | `count`, `oldestOpenAt`, `attemptsExhausted`, `drafterLastStatus` |
| `approval_stuck` | **inactive**: it needs the deferred send gate (any-ui's `2026-10-25-ai-brain-later.md`) | — | — |

- **Downtime is not lateness: `uptimeSince`.** The runtime re-arms every
  cron strictly forward at boot (`compute_next_due` = now + every_s), so
  after a restart every job's next run is boot + every_s, and a job's
  age counted from its pre-shutdown run reads overdue for jobs with a
  long cadence (found in review, 2026-10-29). The caretaker keeps
  `config/caretaker.uptimeSince`: set to the tick when the record has no
  `lastTickAt` or no `uptimeSince` yet, and whenever `tick − lastTickAt`
  exceeds its own record's `spec.every_s` (fallback 900) + 120 s — a gap
  in its own ticks means the device was down or asleep. Every age-based
  class counts from no earlier than it, so the first tick after a boot
  grades nothing by age.
- **A paused job is not a fault.** `enabled: false` without
  `lastStatus: auto_disabled` is the user's choice (§1.4); it is
  neither overdue nor failing, and a paused mail sync is not stale.
- A job whose model fails on every call raises `provider_failed` (§2.4
  step 7, §3.3 step 6, §4.6 step 7), so the runtime counts it and
  `job_failing` sees it; there is no separate "stalled" class.
- The design record's `bao.status` beat age is not a class: the guest
  cannot read the bus, and the caretaker runs inside the very serve
  whose beat it would measure. The UI's presence indicator owns it.

### 6.4 Incidents and observations

`incidents` and `observations` are the hub's stores (§1.3).

- An incident is opened for a `(faultClass, subject)` with no incident
  that is not `closed`: id `sha256(faultClass|subject|<openedAt epoch
  s>)[:32]`, `{faultClass, subject, openedAt, status: "open", summary}` —
  `summary` the class's one-line, code-written reading of the evidence
  (≤ 300 chars); it is the card's `reason` and the notice line.
- Every tick writes one observation per incident that is not `closed`
  at the start of the tick: id `<incidentId>:<tick>`, `{incidentId,
  tick, evidence}` — the class's evidence, or `{clean: true}` on a tick
  the fault was not seen, or the class's evidence plus `pending: true`
  while a repair waits for the job's own next run (§6.7). Written through `upsert_records`, so an
  observation is never updated. An `escalated` incident that is still
  dirty gets one an hour (`config/caretaker.observedAt`): it waits on
  the user, and a row every tick would grow the store by 96 a day for as
  long as the user leaves it.
- A clean tick closes an `open` or `escalated` incident (`status:
  closed`, `closedAt`); `verified` stays absent — it healed by itself,
  which the ladder did not verify. A `repairing` or `soaking` one
  follows the ladder (§6.7).

### 6.5 The action, by rule

An incident that has had no repair gets its action from one table, in
code, each tick: an open one is repaired or escalated; an escalated one
is repaired when the table now offers a repair (a job failing twice is
escalated at once; the runtime's stop at the third failure then gets its
`enable_job`). The model never chooses:

| Class | Action |
| --- | --- |
| `job_overdue` | `run_job` |
| `job_failing` | `enable_job` when the record is `auto_disabled` and disabled; else `escalate` |
| `mail_stale` | `run_job` when `brain.status.mailSync` names a sync trigger that exists and is enabled; else `escalate` |
| `tier_unpinned`, `exclusions_unreadable`, `setup_blocked`, `draft_backlog` | `escalate` (no allowed repair helps) |

A repair writes `action` and `actionAt` on the incident. `escalate` is
recorded as `action: "escalate"` and is the notice path (§6.8): a card
when the hub has `cards`, one notice line, `status: escalated`. An action
the executor refuses (§6.6) is a `signals` row `{tick, kind:
"repair_refused", subject: <incidentId>, decision: "refused", reason}`;
the incident escalates with the action it tried on the record, and is
not retried.

### 6.6 Repairs are record writes

| Action | What the code writes | Allowed for |
| --- | --- | --- |
| `run_job` | a `once` trigger `brain-rerun-<trigger id>-<tick>` (fresh, checked against the records it read) with the job's program and args, `at: now` | `job_overdue`; `mail_stale` when `brain.status.mailSync` names an enabled sync |
| `enable_job` | `$set enabled: true` on the job's record, nothing else | `job_failing` on a brain job the runtime stopped (`auto_disabled`) |
| `escalate` | a card (when the hub has `cards`) and one line in the next chat notice (§6.8); `status: escalated` | every class, when no repair applies |

- The target is the incident's own subject, resolved against the
  snapshot: `enable_job` refuses a record whose program does not start
  `connectors:brain` or that is not stopped; `run_job` refuses a record
  that is not an enabled cron of the brain's or the named sync. A
  refused execution is a `repair_refused` signal and an escalation.
- Every incident gets at most one repair: after it the incident is on
  the ladder, and a failed verification escalates it for good.
- **Advice only** — a notice line may name them, the code never does them:
  secrets, approvals, `config/exclusions` and the other user lists,
  deletions, `sends`, cursors, repinning a job, anything outside the
  brain's own jobs.
- **Re-enabling resets the breaker.** Read from the runtime, not yet
  probed live: a dataset `enabled` false→true flip on a record the
  registry holds resets its `consecutive_failures` and re-arms it
  (`triggers.rs` `reconcile_registry`) — in memory; the RECORD keeps
  `lastStatus: auto_disabled` and its count until the job's next own
  run, every_s later — so a `job_failing` read from an enabled record
  opens no incident (setup's on run restarts every stopped job at once,
  and each would read as failing for up to every_s); it only holds a
  pending repair (§6.7). A serve restarted between the stop and the flip
  rebuilds the entry from the record, so one more failure stops the job
  again at once; the caretaker then reads `job_failing` again and
  escalates rather than retry.
- Every `run_job` leaves a disabled `once` in `agent_triggers` (nothing
  in the contract is deleted); the brain's listings filter on `kind:
  cron`.

### 6.7 Ran is not fixed

A repair sets `status: repairing`, `actionAt`, `verifyAt = actionAt +
max(15 min, 2 × every_s + 120 s)` — the job's own cadence, since the
record only changes when the job runs again (found in review,
2026-10-29). At `verifyAt` the class's condition is read again for the
subject: clean → `soaking`, `verifyAt = now + max(60 min, 2 × every_s +
120 s)`; clean at the end of the soak → `closed`, `verified: true`,
`closedAt`. A dirty tick at the verify step or during the soak →
`verified: false`, `status: escalated` (§6.8), and no second repair.
After `enable_job`, while the record is enabled, still reads
`auto_disabled` and has not run since `actionAt`, the condition is
**pending**: neither clean nor dirty, the incident stays `repairing`,
and the observation says so. A `rerun` counts as a run of its job, so
`job_overdue` verifies after a nudge only while the job keeps running on
its own within the window.

### 6.8 Escalation, notices, reports

- **Escalation** is a card and a notice, once per incident. The card
  goes into `cards` only when the hub's datasets name it: id
  `sha256("incident|<incidentId>")` (the full hex digest, as the
  heartbeat names cards), `{kind: "incident", title: "<faultClass>:
  <subject>", reason: <summary>, subjectKey: <incidentId>, sourceUri:
  <the incident's record link>, status: "open"}`, written only when that
  id is absent so a dismissed card stays dismissed; its id lands in the
  incident's `cardId`. Without `cards`, the notice is all.
- **Notices** are batched: each escalation adds one line to
  `config/caretaker.pending`, and a run posts them in one
  `brainKit.notice` (§2.6) when `noticeCooldownMin` (default 60) has
  passed since `lastNoticeAt`; a line whose incident closed in the
  meantime is dropped. Success is silent.
- **The daily report**, on the first tick of a UTC day
  (`config/caretaker.lastReportDay`) on which an incident was opened or
  closed in the last 24 h: a `standups` row `{job: "caretaker", tick,
  did, needs, counts}` and an `info` card `{title: "Caretaker report
  <day>", subjectKey: "caretaker:<day>", reason}`, each when its store
  exists. An incident that has waited on the user for days is not
  reported again every day. The lines say "fixed (verified)" only for
  incidents the ladder closed, "tried, unverified" for those on it. A
  run that opened, repaired, verified, escalated or closed something
  writes a `standups` row; one that did nothing writes none.
- `config/caretaker` is the caretaker's own state: `{noticeCooldownMin,
  lastNoticeAt, pending, lastReportDay, observedAt, lastTickAt,
  uptimeSince}`.

### 6.9 What the caretaker cannot do

No runtime change: it cannot restart `serve` (there is no process to
restart inside one), read the event bus, repin a job to the active
device, or run anything but guest programs through records. The
election verdict it reads is the server's `active.bao`, evidence only.

### 6.C Consequences

- One definition of broken: the detectors read the records Scheduled,
  the Cards view and Settings → Model render. `job_failing` grades
  `consecutiveFailures ≥ 2` while the job still runs, so the caretaker
  escalates one cadence before the runtime stops the job, and repairs
  once it has.
- The caretaker calls no model, so it needs no tier row and no consent,
  and its own failures are the plain kind: `snapshot_failed` or
  `unconfigured` read FAILED, the breaker stops it after three, and
  `brain.status.stopped` names it (§1.4).
- `mail_stale` by age relies on `sync_state.modifiedAt` moving on every
  checkpoint; a mailbox with no Gmail history for six hours — every
  quiet night — may read stale if a same-value update does not bump it.
  If it does, `gmailSync` records a `lastSyncAt` and the detector reads
  that. To be probed live before the brain ships to users.
- Verified with the kernel harness and fakes; the live runs and the
  breaker probe (§6.6) are pending.

## Part 7 — The AI brain's tasks from conversations — promises and asks from the chats the user opted in

### Context

What the user owes does not only arrive by mail. They promise things in
chats ("I'll send you the deck tomorrow") and people ask them for things
there ("can you review this by Friday?"). This part reads the chats the user
opted in and turns both into records the task flow already knows. It
reads other people's words, so it reads only what the user named, only
messages addressed to the user, and never anyone the user keeps out of
the brain.

### 7.1 Opt-in per space, from chat

Two `agent_config` rows, written only by `brainSetup@v1`'s chat tools
after the user's yes in that conversation (no client writes them,
as with `brain.space`, §1.2):

| Row | Writer | Value |
| --- | --- | --- |
| `brain.chatScan.spaces` | `watch_chats(space_ids)`, `stop_watching_chats(space_ids)` | `{spaceIds: [...], requestedAt, by: "chat"}` — absent or empty = no chat is read |
| `brain.chatScan.pages` | `scan_pages(enabled)` | `{enabled: bool, requestedAt, by: "chat"}` — absent = off |

`watch_chats` takes space ids (or exact names) and refuses, writing
nothing: a brain without a hub (`not_set_up`), a space the account does
not have (`unknown_space`), Bao's own space (`bao_space`: its chat is the
conversation with Bao), and a space where the user can only read
(`read_only_space`: `ownRole` reader, guest or none — "none" also means
not loaded yet). One-to-one spaces are named like any other.

Each tool call re-arms `brain-setup` for the request it already answered
(§1.2), so it restarts nothing (§1.3 step 5). Every setup run makes the
`brain-chat-scan` job's `enabled` follow the row: on while the row names
a space, off while it names none. The row is the user's switch for this
one job, so a pause set in Scheduled holds only until the next setup run
(a watch / stop call, or a hub upgrade). The reconcile run only ever
disables it (a row emptied while a setup run was missed), never enables
it. The tools refuse (`not_set_up`) while the brain is not `ready` — off,
waiting or switching off — so a row cannot outlive an off request and be
read by the next `on()` without a fresh yes. Independently the job
reads the row at run time and returns `{skipped: "no chats"}` without a
single read when it names nothing: switching off holds even before the
setup run lands.

### 7.2 The read: `_ver.id`, not `createdAt` (the probe)

Each chat is read with

```
query(space, chatId, "chat_messages", filter={"_ver.id": {"$gt": cursor}},
      sort=["_ver.id"], limit=200)
```

and, on the first visit, the newest 200 (`sort=["-_ver.id"]`) of which
only the last 7 days (by `createdAt`) are read. Probed offline against
any `bb2e321` and the kernel harness, live verification pending:

- `createdAt` is filterable (an instant compared to `instant(…)`), but it
  is not indexed on `chat_messages` and it is stamped from the change
  clock at second resolution, so a `$gt` cursor drops the messages that
  share the cut's second when a page ends inside it.
- `_ver.id` is the record's creation version — the SDK's lex-monotonic DAG
  order, set once, never bumped by an edit — indexed (`idx_ver_id`,
  `internal/chat/handler.go`) and the cursor the server documents for chat
  paging (any `docs/08-clients.md` § 4). A string `$gt` range is exact.
- The real any@v1 passes that body to `POST …/query` unchanged:
  `chat_messages` is a canonical collection, and the instant guard does
  not touch `_ver.id` (`test_brain_chat_scan.py::test_probe_*`).
- The first visit's floor (7 days before it, by `createdAt`) is kept per
  chat in `config/chatScan.floor` (`{<chatId>: <epoch seconds>}`) and
  applied to every later read: a message created before it is passed,
  never read, however it syncs in.
- A message another member wrote offline can sync in below the cursor in
  either order; it is not read. Precision over recall: a missed ask costs
  nothing.

The record fields the scan reads: `text`, `creator`, `createdAt`,
`replyToMessageId`, `agent` (any agent's post, Bao's included — skipped),
`control` (a control record — skipped) and `mentions`, the identities the
server derives from the text's `any://u/<identity>` links (and the legacy
`any://m/<space>/<identity>`) plus the replied-to message's author. No
Python parser of the link grammar is needed; a record without `mentions`
falls back to a substring test on `any://u/<me>`.

### 7.3 `connectors:brainChatScan@v1`, every 30 minutes

A cron job (args `{space, hub, datasets, tier?}`), in this order:

1. The row (§1); empty → skipped. Then the exclusion list, fail closed
   (§2.6), and the account's identity (`me()`).
2. Per watched space: its `list_spaces` row must be active, not Bao's own
   space and not read-only; else it is skipped and counted. In a
   one-to-one space whose peer is kept out (§5) the chat is not queried.
3. Per message, ascending, after the cursor: control records and agent
   posts are passed over.
4. **Kept out, before the text is read.** A message whose sender, the
   author of the message it replies to, or any identity it mentions is
   kept out — the identity itself on the list's `identities`
   (`brainSetup.exclude(identity=…)`), or any person it may be (§7.5)
   excluded — is counted (`excludedSkipped`) and its text never looked at.
5. **The user's own messages → `promises`**, only with the model consent
   (`kit.model_allowed()`, §3.1, whose question names the watched chats);
   without it they are counted (`noConsent`) and passed. Extraction is `brainPromises@v1.extract(text)`
   — the mail path's own judge gate, classify-tier call, retry and
   verbatim grounding (§2.4) over the message as shown (mention
   links reduced to their label, 2 400 chars). At most 40 model calls per
   run, counted per call (a judge, a call and a retry are three; a failed
   extraction is charged for the calls it made, `model_failed.calls`); a
   chat whose next own message would pass the cap stops before it. A
   message that already has a promise (by `source`,
   `brainPromises.extracted(space, hub, dataset, sources)`) is not asked
   again. The
   record is the mail one with `channel: "chat"`, `mailboxId` = the chat
   object (the source's host object), `source` =
   `any://o/<space>/<chat>/chat_messages/<id>`, `madeAt` = `createdAt`,
   no `personEmail`, and `personId` = the peer's person in a one-to-one
   space when it resolves (§5).
6. **Other people's messages → `asks`**, deterministic, no model and no
   consent needed. A message is **addressed to the user** when the space
   is one-to-one, or `mentions` holds the user's identity (a mention or a
   reply to the user's message), or — the fallback — its text carries
   `any://u/<me>` or it replies to a message of the user's in the page
   read. Every other message is ignored. In an addressed message every
   sentence that carries a request phrase (`REQUEST_PHRASES`: EN `can
   you`, `could you`, `would you`, `please`, `let me know`, `send me`,
   `need you to`, `remind me`; DE `kannst du`, `könntest du`, `bitte`,
   `schick mir`, `brauche`, `sag mir`) becomes a `request` ask, and every
   other sentence ending in `?` a `question` ask.
7. Records are written only when the store lacks their id (`status` is
   mutable: re-sending would reopen what the user closed); a deleted id
   is the user's answer (`upsert.record_deleted`), any other refusal
   fails the run. The chat's cursor (`config/chatScan.cursor`,
   `{<chatId>: <_ver.id>}`, beside its floor in `config/chatScan.floor`)
   advances after its writes land, and only past messages fully handled: a provider failure leaves it before its
   message. After two failures the run calls no model again, and a run
   whose every model call failed raises `provider_failed` once its
   writes landed, so the runtime counts it (and stops the job after
   three, which `brain.status.stopped` reports). Counts only go to
   `config/chatScan.stats`.
8. **Pages, behind `brain.chatScan.pages`.** In each watched space,
   pages (`any.type: page`) modified since the space's page cursor (the
   last 24 hours on a first visit, 20 pages per run) are read block by
   block; each unchecked `check_list_item` becomes a `checklist` ask with
   the block link (`any://o/<space>/<page>/editor_blocks/<id>`) as
   `source`, the page as `chatId`, the page's last editor as sender and
   no suggested deadline. An open `checklist` ask whose block is
   `style.checked` when its page is read again turns `done` (`closedAt`;
   counted as `checked`), as step 9 does for a completed task. Off: no
   page is read.
9. Asks whose task the user completed turn `done` (the promise rule,
   §2.4, extended).
10. The daily suggestion card (§7).

Raises: `unconfigured`, `exclusions_unreadable`, `cursor_corrupt`,
`asks_rejected`, `promises_rejected`, `cards_rejected`, `provider_failed`.

### 7.4 The `asks` store

Declared shared on the hub like `promises` (`deleteBy: author`, search
`{text: [text, quote], scope: asks}`, stamps); ✎ = `mutableBy: author`:

| Field | Kind | Notes |
| --- | --- | --- |
| id | user | `sha256(messageId \| text)`, 32 hex |
| `senderIdentity` ★ | string | `any://u/<identity>` |
| `personId` ✎ | string | the sender's person when it resolves (§5); back-filled by `brainPeople@v1` |
| `spaceId` ★ | string | the chat's space |
| `chatId` ★ | string | the chat object (a page for `checklist`) |
| `kind` ★ | string | `question \| request \| checklist` (`mention` is reserved, not written) |
| `text` ★ | string | the sentence, whitespace normalized |
| `quote` ★ | string | the sentence verbatim, ≤ 240 chars |
| `source` ★ | string | the message's (or block's) record link |
| `askedAt` ★ | datetime | the message's `createdAt` |
| `suggestedDeadline` | datetime | `brainTasks.deadline_for("ask", text, askedAt, today=<the run's day>)`, never earlier than the day it is written (write-once) |
| `status` ✎ | string | `open \| done \| dismissed` |
| `taskId` ✎ | string | the catalog task made from it |
| `closedAt` ✎ | datetime | |

`brain_person` has `identity` (text, `any://u/<identity>`, one per
person).

### 7.5 Identities become persons only when unambiguous

`brainKit.IdentityResolver(space, exclusions, me)` (§2.6) is the one
resolver — `kept_out(identity)`: the identity itself in the list's
`identities`, else any candidate person excluded, never the user;
`person(identity)`: the one candidate, else None; the scan, `brainTasks`
and `brainPeople` all go through it, and `brainPeople` persists the
links. Its rule is `identity_candidates(persons, identity, name)`: the person whose
`brain_person.identity` names the identity; else every catalog person
(filed under `person` or `brain_person`) whose name or a
`brain_person.aliases` entry equals the identity's display name (case
folded) and who has no other identity. A **link** needs exactly one
candidate; an **exclusion** holds when the exclusion list names the
identity itself (`identities`, written by `brainSetup.exclude(identity=…)`
/ `include(identity=…)`) or when any candidate is excluded — a name two
persons share links nobody, but keeps out both persons' exclusions. A
name match is only as good as the name: a person excluded by id whose
chat name differs from their catalog name is not kept out of chats until
the user excludes the identity, which `brain-core` says to do; a display
name is the remote account's to choose, so a link by name is advice the
user can undo through `include(identity=…)` and a fresh `exclude`.
The name comes from the space's member list, else the identity
directory. The scan applies the rule read-only; `brainPeople@v1`
persists it: it writes `brain_person.identity` on the one candidate
(filing it under `brain_person` when needed, never overwriting one) for
the senders of unlinked asks and the peers of one-to-one chats holding
an unlinked chat promise, then back-fills `asks.personId` and those
promises' `personId`. A job written before the hub had `asks` finds the
store on `brain.status`. A bare name never mints a person.

### 7.6 Tasks from asks

`brainTasks@v1` (additive): `suggest(limit=20)` lists open promises
**and** open asks without a task (`limit=None` lists every one), each
with `origin` (`promise | ask | checklist`);
`create_from_ask(ask_id, deadline=None)` mirrors `create` (a catalog task
with `brain_task.origin` `ask` — `checklist` for a checklist item —, the
ask's `personId`, the quote and a link to the message as notes) and sets
the ask's `taskId`; `create_all()`
covers both. `deadline_for(origin, text, at)` is the one public rule
table: promises +2 business days by default, asks +3, checklist items
none; the sentence's own date, today, tomorrow, this / next week and
weekdays as for promises (§2.5). An ask from an excluded person, or from
an identity the user excluded, is never offered and refuses
`create_from_ask` (`person_excluded`).

### 7.7 One suggestion card a day, from the scan

When the hub names a `cards` store (the decision layer adds it), the
first scan run of a UTC day on which `brainTasks.suggest()` returns
anything writes one card, id `sha256("task_suggestion|tasks:<YYYY-MM-DD>")`
(64 hex), only when absent:

`{kind: "task_suggestion", title: "<n> promises and <m> asks without a
task" (counted over every open suggestion, `suggest(limit=None)`),
reason, subjectKey: "tasks:<YYYY-MM-DD>", status: "open", personIds,
signalId, items}`

`items` (array, write-once) is the suggestion list as `suggest()` returns
it — `[{id, origin, text, quote, personId?, source, suggestedDeadline?}]`,
plus `personEmail` on a promise and `kind` / `senderIdentity` on an ask —
the newest 20; `personIds` the items' persons. A `signals` row
`{kind: "task_suggestion", decision: "card", cardId}` records it, its id
the heartbeat's `<tick>-<kind>-<sha256(subject)[:12]>`. A deleted card's
id is the user's answer for the day. The setup run adds `items` to the
`cards` declaration whenever one is declared (`_with_card_items`): an
existing store gains the field, since `_create_dataset` adds a field the
draft has and the stored definition lacks.

### 7.8 What this part never does

No model reads anyone else's message; no task is made without a click or
a yes; no chat the user did not name is read, no space where the user
only reads; no message from an identity the user excluded, or from a
person linked to one they excluded, is read; nothing is posted into the
scanned chats.

### 7.C Consequences

- No runtime change. The setup run (§1.3) installs `asks`, the
  `identity` column and the `brain-chat-scan` job (off until a space is
  watched); `cards` carries `items` for the daily card.
- Deferred: a `standups` row per scan run (the decision layer's contract);
  late-synced offline messages below a cursor; identity links the user
  states ("this is Ada") rather than names.
- Live verification on a scratch `any` server with `anyrt serve` is
  pending.
