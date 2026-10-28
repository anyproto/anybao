# ADR-039: The AI brain's tasks from conversations — promises and asks from the chats the user opted in

Status: **Proposed** (2026-10-28)
Date: 2026-10-28
Builds on: ADR-033 (the brain as a plugin, jobs as records), ADR-034
(people, promises, tasks), ADR-035 §1 (the model consent), ADR-019
(instants), ADR-027 §1 (the one general chat)
Plan: any-ui `docs/plans/2026-10-25-ai-brain-phase-6-tasks-from-conversations.md`,
as amended by `docs/plans/2026-10-28-ai-brain-phases-3-6-on-connectors.md`

## Context

What the user owes does not only arrive by mail. They promise things in
chats ("I'll send you the deck tomorrow") and people ask them for things
there ("can you review this by Friday?"). Phase 6 reads the chats the user
opted in and turns both into records the task flow already knows. It
reads other people's words, so it reads only what the user named, only
messages addressed to the user, and never anyone the user keeps out of
the brain.

## Decision

### 1. Opt-in per space, from chat

Two `agent_config` rows, written only by `brainSetup@v1`'s chat tools
after the user's yes in that conversation (no client writes them,
as with `brain.space`, ADR-033 §2):

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
(ADR-033 §2), so it restarts nothing (ADR-033 §3 step 5). The setup run
makes the `brain-chat-scan` job's `enabled` follow the row: on while the
row names a space, off while it names none. The row is the user's switch
for this one job, so a pause set in Scheduled holds only until the next
watch / stop call. The reconcile run only ever disables it (a row emptied
while a setup run was missed), never enables it. Independently the job
reads the row at run time and returns `{skipped: "no chats"}` without a
single read when it names nothing: switching off holds even before the
setup run lands.

### 2. The read: `_ver.id`, not `createdAt` (the probe)

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

### 3. `connectors:brainChatScan@v1`, every 30 minutes

A cron job (args `{space, hub, datasets, tier?}`), in this order:

1. The row (§1); empty → skipped. Then the exclusion list, fail closed
   (ADR-034 §6), and the account's identity (`me()`).
2. Per watched space: its `list_spaces` row must be active, not Bao's own
   space and not read-only; else it is skipped and counted. In a
   one-to-one space whose peer is kept out (§5) the chat is not queried.
3. Per message, ascending, after the cursor: control records and agent
   posts are passed over.
4. **Kept out, before the text is read.** A message whose sender, the
   author of the message it replies to, or any identity it mentions
   resolves to an excluded person is counted (`excludedSkipped`) and its
   text never looked at.
5. **The user's own messages → `promises`**, only with the model consent
   (`kit.model_allowed()`, ADR-035 §1); without it they are counted
   (`noConsent`) and passed. Extraction is `brainPromises@v1.extract(text)`
   — the mail path's own judge gate, classify-tier call, retry and
   verbatim grounding (ADR-034 §4) over the message as shown (mention
   links reduced to their label, 2 400 chars). At most 40 model calls per
   run, counted per call (a judge, a call and a retry are three); a chat
   whose next own message would pass the cap stops before it. A message
   that already has a promise (by `source`) is not asked again. The
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
   `{<chatId>: <_ver.id>}`) advances after its writes land, and only past
   messages fully handled: a provider failure leaves it before its
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
   no suggested deadline. Off: no page is read.
9. Asks whose task the user completed turn `done` (the promise rule,
   ADR-034 §4, extended).
10. The daily suggestion card (§7).

Raises: `unconfigured`, `exclusions_unreadable`, `cursor_corrupt`,
`asks_rejected`, `promises_rejected`, `provider_failed`.

### 4. The `asks` store (hub version 7)

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
| `suggestedDeadline` | datetime | `brainTasks.deadline_for("ask", text, askedAt)` |
| `status` ✎ | string | `open \| done \| dismissed` |
| `taskId` ✎ | string | the catalog task made from it |
| `closedAt` ✎ | datetime | |

`brain_person` gains `identity` (text, `any://u/<identity>`, one per
person), added to the existing collection by `create_collection`'s
missing-property path.

### 5. Identities become persons only when unambiguous

`brainKit.identity_candidates(persons, identity, name)` is the one rule:
the person whose `brain_person.identity` names the identity; else every
catalog person (filed under `person` or `brain_person`) whose name or a
`brain_person.aliases` entry equals the identity's display name (case
folded) and who has no other identity. A **link** needs exactly one
candidate; an **exclusion** holds when any candidate is excluded — a name
two persons share links nobody, but keeps out both persons' exclusions.
The name comes from the space's member list, else the identity
directory. The scan applies the rule read-only; `brainPeople@v1`
persists it: it writes `brain_person.identity` on the one candidate
(filing it under `brain_person` when needed, never overwriting one) for
the senders of unlinked asks and the peers of watched one-to-one spaces,
then back-fills `asks.personId` and, for a chat promise in a one-to-one
space, `promises.personId`. A bare name never mints a person.

### 6. Tasks from asks

`brainTasks@v1` (additive): `suggest()` lists open promises **and** open
asks without a task, each with `origin` (`promise | ask | checklist`);
`create_from_ask(ask_id, deadline=None)` mirrors `create` (a catalog task
with `brain_task.origin` `ask` — `checklist` for a checklist item —, the
ask's `personId`, the quote and a link to the message as notes) and sets
the ask's `taskId`; `create_all()`
covers both. `deadline_for(origin, text, at)` is the one public rule
table: promises +2 business days by default, asks +3, checklist items
none; the sentence's own date, today, tomorrow, this / next week and
weekdays as for promises (ADR-034 §5). An ask from an excluded person is
never offered and refuses `create_from_ask` (`person_excluded`).

### 7. One suggestion card a day, from the scan

When the hub names a `cards` store (the decision layer adds it), the
first scan run of a UTC day on which `brainTasks.suggest()` returns
anything writes one card, id `sha256("task_suggestion|tasks:<YYYY-MM-DD>")`
(64 hex), only when absent:

`{kind: "task_suggestion", title: "<n> promises and <m> asks without a
task", reason, subjectKey: "tasks:<YYYY-MM-DD>", status: "open", items}`

`items` (array, write-once) is the suggestion list as `suggest()` returns
it: `[{origin, id, text, quote, personId?, source, suggestedDeadline}]`,
at most 20. A `signals` row `{kind: "task_suggestion", decision: "card",
cardId}` records it. A deleted card's id is the user's answer for the day.

### 8. What Phase 6 never does

No model reads anyone else's message; no task is made without a click or
a yes; no chat the user did not name is read, no space where the user
only reads; nothing is posted into the scanned chats.

## Consequences

- No runtime change. The hub records `version: 7`; a reconcile run that
  finds an older hub re-arms setup (ADR-034 §1), which installs `asks`,
  the `identity` column and the `brain-chat-scan` job (off until a space
  is watched).
- The `cards` declaration (the decision layer's) must carry `items`
  (`array`) for §7's card; without the store no card is written.
- Deferred: a `standups` row per scan run (the decision layer's contract);
  late-synced offline messages below a cursor; identity links the user
  states ("this is Ada") rather than names.
- Live verification on a scratch `any` server with `anyrt serve` is
  pending.
