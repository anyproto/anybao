# ADR-034: The AI brain's people, promises and tasks — from synced mail, deterministic

Status: **Proposed** (2026-10-25)
Date: 2026-10-25
Builds on: ADR-033 (the brain in the agent overlay), ADR-016 (the email
corpus on `email_messages`), ADR-019 (instants), ADR-027 (the usecase
catalog), ADR-029 (one type, collections)
Plan: any-ui `docs/plans/2026-10-25-ai-brain-phase-1-what-it-knows.md`
(this ADR covers its mail half; calendar and meeting sync wait for their
live probes)

## Context

Phase 1 of the AI brain gives it what it knows. The mail half needs no
new connector: `gmailSync@v1` already syncs mail into Bao's space as
`email_messages` on one `mailbox` object per address. What is missing is
a person per correspondent, the user's promises, and a way to turn a
promise into a task — all without a model and without a runtime change.

## Decision

### 1. Setup installs the stores (hub version 2)

The setup run (ADR-033 §3) additionally installs or adopts the catalog
`tasks` app; creates two hidden collections with `create_collection`,
idempotent by xKey and leaving the catalog's own definitions untouched —
`brain_person` (`emails`, `aliases`, `lastContactAt`, `firstSeenAt`,
`mailCount`, `meetingCount`, `preferredName`, `addressForm`, `language`)
and `brain_task` (`sourceUri`, `personId` → `profile`, `origin`,
`suggestedDeadline`); declares the shared `promises` dataset on the hub;
seeds an empty `config/blocklist` (`{senders, domains, labels}`, each a
list; `labels` are Gmail label ids, matched against a synced message's
`labelIds`, and a message under one is read past by every brain reader;
a list in another shape fails the run, like the exclusion list); and writes the `brain-people` (900 s)
and `brain-promises` (1 800 s) jobs. The hub records `version: 2`; a
reconcile run that finds an older hub re-arms setup, so an existing
install upgrades itself within the hour.

### 2. Mail sync is adopted, not duplicated

Setup adopts a `connectors:gmailSync@v1` cron whose `args.space` is
Bao's space, or writes one (`brain-mail-sync`, every 900 s). A cron into
another space is named in `brain.status.mailSync.other` and left alone;
the brain reads only Bao's copy. Mail sync is not a brain job: switching
the brain off leaves it running.

### 3. `brainPeople@v1`

Reads From / To / Cc — never a body — of mail newer than its cursor
(`config/people.cursor`, ms per mailbox), and keeps one catalog person
per address: a person whose `brain_person.emails` holds it, else one
whose `person.email` equals it (filed under `brain_person`), else a new
`profile` filed under `person` and `brain_person`. The mailbox's own
addresses, noise senders, blocklisted senders and domains, and excluded
people never become people. It preloads the first 1 000 persons; one
past them is found by any of their addresses (`brain_person.emails`,
then `person.email`) and merged into, never overwritten or minted
twice. It caps unknown addresses per run and
advances the cursor only past messages fully handled, then back-fills
`personId` on promises whose recipient it now knows.

### 4. `brainPromises@v1`

Reads the user's sent mail (From = the mailbox's address) newer than
its cursor, splits the cleaned body into sentences, and keeps those that
match `PHRASES` and none of `EXCLUSIONS` (a question, a condition, a
hypothetical, a negation anywhere in the sentence). Each is a `promises`
record: the verbatim `quote`, the key-form `source` link, `personEmail`
and `personId`, `madeAt`, and a `dueAt` from a fixed table (today,
tomorrow, end of week, next week, weekday names; EN and DE). The id is
`sha256(messageId, sentence)`, and the job writes only ids the store
lacks: `status` is mutable, so re-sending a promise that a re-read
message yields again would reopen one the user or a completed task
closed. A message to
an excluded or blocklisted recipient, or under a blocklisted label, is
skipped whole. Any per-record
rejection fails the run with the cursor unmoved. It also marks done an
open promise whose catalog task the user completed, finding the tasks by
the `taskId`s the promises hold rather than by the `brain_task`
collection: a task the Promises view makes in any-ui is a plain catalog
task, not filed there.

### 5. `brainTasks@v1`

A chat tool: `suggest()` lists open promises without a task, each with a
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

### 6. `brainKit@v1`

The jobs share one helper module for the server rules ADR-033 found
live: checked writes (a refusal is HTTP 200 with `rejections`), hub
records read by `<objectId>/<recordId>`, and the exclusion list read
fail-closed. `exclusions()` resolves the persons excluded by id to the
addresses on their person objects (`brain_person.emails`,
`person.email`), so `kept_out` treats a message carrying one of them
like one carrying a listed address — found live: an id-only exclusion
had let a CC'd person's mail reach another person's facts, promises and
recall. Resolving at read time cannot help once the person object is
gone — the people job would mint a fresh person for the same address,
and their mail would reach the model — so `brainSetup.exclude(person=…)`
also copies the person's addresses into `emails`, and refuses an id
that names no person (an object filed under `person` or `brain_person`).
It writes only the fields it adds to, and refuses a list that is not in
its version-1 shape rather than rewrite it into a readable one: a list
the jobs refuse to read must stay refused until it is fixed. The heartbeat uses it too, and the tools read the hub off
`brain.status` through its `ready_hub()`.

### 7. `brainRecall@v1`

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

## Consequences

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
