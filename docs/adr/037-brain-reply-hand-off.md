# ADR-037: The AI brain's reply hand-off — the user sends from Gmail, the heartbeat notices

Status: **Proposed** (2026-10-28)
Date: 2026-10-28
Builds on: ADR-036 (the decision layer: drafts, approvals bound to their
bytes, heartbeat v2), ADR-034 (exclusions and the blocklist), ADR-033
(no runtime change)
Plan: any-ui `docs/plans/2026-10-25-ai-brain-phase-4-one-exit.md` (the
first, hand-off half; the runtime send gate at its end stays deferred),
with the delta `docs/plans/2026-10-28-ai-brain-phases-3-6-on-connectors.md`

## Context

After ADR-036 an approved reply waits: nothing in the brain can send.
Giving Bao a send path that keeps "whatever reads untrusted mail cannot
also send" needs host code — a host-only send credential, a grant the
guest cannot forge, an `outbox.send` syscall — which the owner ruled out
for now (2026-10-25; Gmail stays read-only, 2026-10-28). So the first
version hands the approved text to the user, who sends it from Gmail, and
the brain notices the sent reply in the mail it already syncs.

## Decision

### 1. The user sends; the brain cannot

Google access stays read-only (`gmail.readonly`), so the property holds
by construction: no brain program, and no chat turn, holds a credential
that sends mail. Gmail drafts are not used either: the scope that writes
them (`gmail.compose`) can also send them, which would put a send
capability behind the same guest-injectable token.

### 2. The hand-off is any-ui's

After Approve, the reply's card offers Open in Gmail (the thread, with
the approved text on the clipboard), Open as new message (Gmail's compose
window prefilled with recipients, `Re:` subject and, when it fits, the
text), Copy reply and Mark sent. The Gmail links are not a public
contract; any-ui probes them (its Phase 4 B2). Nothing here depends on
which one the user takes.

### 3. Sent detection (`connectors:brainHeartbeat@v3`)

The heartbeat's v2 passes (ADR-036 §4) plus one, before drafting. For
each `approved` approval (at most 50 a run, oldest decision first) with
no `sends` row:

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

### 4. The `sends` store (hub version 5)

Shared, `idRule: user`, `deleteBy: author`, every field written once,
`creator` and `createdAt` stamps: `approvalId`★, `cardId`★, `threadId`★,
`messageId` (the detected message; absent when marked by hand), `how`★
(`detected` | `marked`), `at`★ (datetime). The id is the approval id, so
a second detection — or a detection after Mark sent — writes nothing.
Mark sent is any-ui's: the `sends` row with `how: marked`, the approval
`consumed`, the card `sent`.

### 5. The seven-day check

An `approved` approval with no `sends` row seven days after `approvedAt`
opens one card `kind: send_check` ("Did you send this reply?"), once per
approval: id `sha256("send_check|" + approvalId)`, `subjectKey` and
`approvalId` the approval id, `draftId` the draft, `sourceUri` the
original card (`any://o/<space>/<hub>/cards/<card id>`), `personIds`
copied from it; a `send_check` signal (`decision: card`). Its answers are
any-ui's: Yes, it's sent (Mark sent) and Not yet (a snooze).

### 6. Vocabulary (amends ADR-036 §1)

Card `kind` gains `send_check`; card `status` gains `sent`; signal kinds
gain `reply_sent` and `send_check`. Approval `consumed` now means "the
user sent it", detected or marked.

### 7. The setup run

Hub version 5 declares `sends` and points the heartbeat record at
`connectors:brainHeartbeat@v3`, in place and keeping its `enabled`
(ADR-036 §10). v3 is v2's run with the sent-detection pass added; v2
stays deployed, since v3 runs on it.

## Consequences

- No runtime change and no new consent; the brain still cannot send.
- The match rule misses a reply rewritten in Gmail, and one whose first
  200 characters Gmail or the markdown conversion change beyond the
  normalisation above (a link the user's mail client rewrote, a
  placeholder the user filled early in the text); Mark sent and the check
  card are the fallback.
- Bao sending by itself — the `google_send` host-only consent, the
  `approval_grant` attachment and `outbox.send` — stays on any-ui's
  `docs/plans/2026-10-25-ai-brain-later.md`, as the deferred half of the
  Phase 4 plan describes it.
- Not yet verified live: a reply sent from Gmail turning a card Sent
  within one mail sync and one tick — the owner's live test.
