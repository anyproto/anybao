# ADR-036: The AI brain's decision layer — signals, cards, reply drafts bound to their bytes

Status: **Proposed** (2026-10-28)
Date: 2026-10-28
Builds on: ADR-033 (the brain in the connectors overlay), ADR-034 (people,
promises), ADR-035 (facts, the model consent), ADR-006 §4 (trigger records,
`once`), ADR-016 (the email corpus), ADR-019 (instants)
Plan: any-ui `docs/plans/2026-10-25-ai-brain-phase-3-decision-layer.md`,
with the delta `docs/plans/2026-10-28-ai-brain-phases-3-6-on-connectors.md`
(where they disagree, the delta wins)

## Context

Phases 0–2 gave the brain what it knows: people, promises and facts from
the user's mail. Phase 3 makes it notice and ask. Every ten minutes a
deterministic loop looks at what arrived and what is due, writes down what
it saw — including what it chose to do nothing about, and why — and turns
what needs the user into cards. For mail that needs a reply it queues a
drafter that writes a draft and a pending approval bound to the draft's
exact bytes. Nothing sends: Phase 4 (ADR-037) hands an approved reply to
Gmail and the user presses send. No runtime change.

## Decision

### 1. Four stores on the hub (hub version 4)

The setup run (ADR-033 §3) declares four more shared datasets on the hub,
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
`incident` (Phase 5), `new_message` and `intro_step` (reserved),
`task_suggestion` (Phase 6). Card `status`: `open`, `snoozed`, `answered`,
`dismissed`. Approval `status`: `pending`, `approved`, `rejected`, `void`,
`consumed`, `held`. A draft is never edited: an edit is a new draft
(`origin: user`, `supersedes`) and a new approval, and the old approval
turns `void` with `editedTo` (any-ui writes it).

`config` gains two records: `drafting` (`{maxPerRun: 3, replyWindowDays:
5, meetingSoonHours: 24, quietSenders: [regex…]}`, seeded when absent and
the user's afterwards) and `heartbeat.cursor` (`{mail: {mailboxId: ms},
stale: {mailboxId: ms}, factConflicts: tick}`, the heartbeat's own).

### 2. Signals: one per observation, with a decision and a reason

`signals` (ADR-033) gains no fields. The heartbeat writes one row for
everything it saw, with `decision` ∈ `none | quiet | card | draft |
refused` and the required `reason`, and `cardId` when a card covers it.
Kinds: `new_mail`, `cc_thread`, `stale_thread`, `overdue_promise`,
`meeting_soon`, `calendar_conflict`, `card_woken`, `heartbeat_gap`,
`exclusions_unreadable` (and `fact_conflict`, which the facts job
writes). A signal's id is `<tick>-<kind>-<first 12 hex of
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

### 3. Card identity

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

### 4. `connectors:brainHeartbeat@v2`

The same `brain-heartbeat` record, every 600 s; args `{space, hub,
datasets, tier}`. It imports `any@v1` and `brainKit@v1` only — no model,
no connector, no web — and reads headers, never a body. Per run:

0. The v1 probes (ADR-033 §5): the exclusion list and the blocklist read
   fail-closed (a refused signal and `exclusions_unreadable`); a
   `heartbeat_gap` signal after more than 1 200 s.
1. Snoozed cards whose `snoozedUntil ≤ now` turn `open` (`card_woken`).
2. **Mail.** Per mailbox, messages after its cursor, ascending, at most
   200; a mailbox with no cursor is read from `replyWindowDays` back, so
   the first run does not card a month of old mail. The user's own mail is
   not an inbound observation. Decisions, in order: kept out → `refused`;
   a noise sender (ADR-034's rule) or one matching `quietSenders` →
   `quiet` "automated sender"; the user in To with a newer message from
   the user in the thread → `none` "already replied"; the user in To →
   a reply card (`reason` "addressed to you, no reply yet"); the user only
   in Cc → `cc_thread`, `quiet`; neither → `quiet` "not addressed to you".
   The rules are the pure `decide_mail(message, own, replied, quiet,
   kept_out)`.
3. **Stale threads.** A message from the user that has just crossed the
   reply window (its own cursor, so each is judged once; a first run looks
   14 days further back), to a known person, still the newest in its
   thread → a `nudge` card ("you wrote last, no answer for N days"), once
   per thread; a dismissed nudge stays dismissed.
4. **Overdue promises.** Open promises with `dueAt < now`, minus those
   kept out → an `overdue_promise` card ("due YYYY-MM-DD").
5. **Calendar.** Events starting within `meetingSoonHours` with at least
   one known, not kept-out attendee → `meeting_soon` (`briefPersonIds`);
   overlapping non-cancelled events in the next 7 days → one
   `calendar_conflict` per pair (at most 20 a run). Both read
   `calendar_events` on the hub (`start`, `end`, `title`, `attendeeIds`,
   `attendees`, `status`, `source`, per the design record §4.2) and are
   no-ops while no calendar sync has declared it.
6. **Fact conflicts.** `fact_conflict` signals since the cursor → a
   `fact_conflict` card naming the person; no second signal.
7. **Drafting** (§5).
8. **Writes, notice, standup, cursors.** Cards, then drafting, then the
   signals; one chat notice when cards were created (§8); one `standups`
   row when the run read or wrote anything; the cursors last, so a failed
   write leaves them where they were. `heartbeat.lastTickAt` as in v1.

Raises: `unconfigured`, `exclusions_unreadable`, `blocklist_unreadable`,
`cursor_corrupt`, and `write_rejected` on any refused record.

### 5. Drafting: one-shots the heartbeat arms and counts

For each open reply card with no `draftId` and `draftAttempts < 3`,
oldest first, at most `maxPerRun` a run, the heartbeat writes a `once`
trigger `{kind: once, spec: {at: now}, program:
"connectors:brainDrafter@v1", enabled: true, args: {space, hub, datasets,
tier, cardId, signalId, messageId, mailboxId, threadId, recipients:
[<the source's From address>], provenance: "thread"}}`, unowned, on the
`bao/triggers/v1` anchor. Its id is `brain-draft-<messageId>`, then
`-a2`, `-a3`: the heartbeat increments `draftAttempts` and derives the id
from it, because a one-shot can die without writing anything (fuel, the
wall, `unpinned`, a provider failure) and the drafter never touches the
counter. The rules that keep it single and fresh:

- An id that exists is never written again — re-upserting a fired once
  re-arms it. Found by query first; one that exists while the counter
  says otherwise catches the counter up.
- While the last attempt's trigger has not fired (no `lastRunAt`), no
  next attempt is armed.
- Nothing is armed without the user's consent to reading mail with a
  model (`brain.model`, ADR-035 §1): the drafter would read nothing, and
  an attempt would be spent. The card stays, its signal says why
  (`decision: card`), and the standup asks for the consent; a later run
  arms it once given. A source message kept out by now is not armed
  either.
- A card past the budget is armed on a later run (`decision: card`,
  "draft budget"); a card with three attempts stays a card.

The split keeps the heartbeat's run short and a drafting failure on its
own one-shot, but it does not shield the ticker: due triggers run one
after another, so a slow draft delays the next tick. The bounds are
`max_tokens`, `llm@v1`'s stream timeouts and the budget of three a run.

### 6. `connectors:brainDrafter@v1`

One reply, no tools, no send capability; its only writes are one
`drafts` record, one `approvals` record, the card's `draftId` /
`approvalId` and one `standups` row. In order, nothing read before the
step that needs it:

1. **Consent** (`kit.model_allowed()`): without it, `{skipped: consent}`
   and nothing read. A switched-off brain (`brain.space` null) is
   `{skipped: off}`.
2. **The tier row** `llm.tier.<args.tier>` (`brain.drafter`): absent →
   `unpinned`, before any model call. Setup seeds it from
   `llm.tier.codegen` when absent (ADR-033 §3: one row per brain job that
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
   and `language` (ADR-035 §4; a person kept out there skips the draft) —
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

### 7. The approval hash

`sha256(utf8(sender + "\n" + recipients + "\n" + format + "\n" + body))`,
hex, where `sender` is the mailbox object id and `recipients` the
addresses lowercased, sorted and joined by `,`. Nothing else is
normalised. The approval's id is the hash, so a changed comma is a
different approval. Python (`brainDrafter@v1.hash_approval`) and any-ui
compute it alike; the pinned vector:

```
sender "mbx1", recipients ["B@x.io", "a@x.io"], format "text/plain", body "Hi\n"
→ hashed text "mbx1\na@x.io,b@x.io\ntext/plain\nHi\n"
→ fc45e7a45327b8d60fb22e052cbcc8c211d90c36750d610eb120c91035fc1bf3
```

### 8. Standups, not chat messages; one notice

Any chat message whose `agent.name` is neither empty nor the serving
agent's name starts a chat turn, and every `done: true` agent message
raises a notification. So each brain job writes one `standups` row per
run that did something, and the Cards view shows them with repeats
collapsed. The chat gets one notice per heartbeat run that created
cards: `{text: "<n> new cards: <titles…>", agent: {name: "bao", done:
true}, attachments: {cards: {type: "brain_cards", link:
"any://o/<space>/<hub>/cards/<newest card id>"}}}` — the record form of
the link, under the literal name `bao` (ADR-033: guests cannot read the
configured name). A failed notice is logged and counted, not raised: the
cards are written.

### 9. The voice is a skill

`repos/_connectors/skills/brain-voice-email.md` ships English and German
defaults (greeting and sign-off, du/Sie from `addressForm`, language,
length). The drafter reads it with `get_skill`, which reads Bao's space
first, so a skill of the same name there overrides it; a missing skill
falls back to a one-paragraph built-in voice. `brain-core` gains a
section: cards, drafts, approvals and standups are written by the
brain's programs and the user; the agent never approves, rejects,
consumes or sends, and quotes a card's `reason` when asked about it.

### 10. The setup run

Hub version 4: the four datasets, `config/drafting` when absent, the
`llm.tier.brain.drafter` row, and the heartbeat job pointed at
`connectors:brainHeartbeat@v2` with `tier: brain.drafter` and the hub's
datasets in its args. An older heartbeat record is rewritten in place —
`program` and `args` only, never `enabled` — so a heartbeat the user
paused stays paused, and no second heartbeat is written beside it. The
reconcile run that finds a version-3 hub re-arms setup (ADR-034 §1), and
refreshes the heartbeat's args whenever they differ from the hub's.

## Consequences

- No runtime change; the drafter is a guest program on `llm@v1`, and
  nothing in the brain can send mail.
- Not in this ADR: sending or any outbound channel (ADR-037 hands the
  reply to Gmail), drafting new messages to people outside a thread (the
  `new_message` kind is reserved), the intro playbook, snooze rules
  learned from behaviour, the caretaker.
- Not yet verified live: that the `bao`-named notice starts no chat turn
  (the watcher's self-skip), and the whole path on a real Gmail account —
  the owner's live test.
- `calendar_events` is read by the field names the design record gives;
  when calendar sync lands, its declaration is checked against them.
