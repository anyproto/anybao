# Skill: gmailSync

Gmail in a space: reading synced mail (who wrote what, threads,
labels, counts) and syncing it with connectors:gmailSync@v1
(backfill, stop, cron, sync-now, stalls).

## Reading the synced mail

Synced mail is email_messages records on a mailbox object (ADR-016):

- A synced space holds **one mailbox object per gmail address**
  (find it: `query_objects(space, filter={"any.type": "mailbox"})`)
  carrying one email_messages DATASET RECORD per message — record
  id = the Gmail message id. Record fields are plain keys (never
  xKey-nested): threadId, from/to/cc/subject/date (plain
  strings), labelIds (Gmail's labels, e.g. TRASH, STARRED,
  CATEGORY_PROMOTIONS), internalDate (ms epoch — the sort key),
  snippet, body (cleaned markdown), signature, participants
  (normalized lowercase addresses from From/To/Cc — the person-join
  key), summary (generated digest) and notes (the user's OWN
  markdown notes — user-authored, edit only on request; both may be
  empty and both survive re-sync), plus derived
  creator/createdAt/modifiedAt (instants; a date-range filter
  on them is `{"createdAt": {"$gte": instant(t0)}}` — internalDate
  stays a plain ms number). **For "what
  did X and I email about", use the synced corpus first — never the
  live gmail connector** (that is the raw provider API: slower,
  quota-bound, needs OAuth, and blind to the cleaned corpus; reach
  for it only for something not yet synced).
- Query mail: `query(space, <mailboxId>, "email_messages",
  filter=..., sort=["-internalDate"], limit=...)` — NOT
  query_objects (records are not objects). A person is
  `{"participants": "ruud@ruuda.nl"}` (array contains); labels
  likewise `{"labelIds": "TRASH"}`. Bodies ride in the rows — no
  get_markdown step. Counts: `aggregate(space, [{"$count": "n"}],
  object_id=<mailboxId>, dataset="email_messages")`. Semantic search
  covers mail — subject + body + notes index under scope email
  (recall queries it by default; summary is NOT indexed); hits carry
  `dataset: "email_messages"` + recordId (the gmail id) — fetch the
  full record with `query(..., filter={"id": {"$in": [...]}})`.
- Threads are data, not structure: same threadId = one
  conversation, sort by internalDate.
- A sync_state object holds the sync cursor — bookkeeping, not
  content; leave it alone. Pre-2026-08 spaces may still hold legacy
  per-message email OBJECTS — a stale corpus; prefer the dataset
  and offer to delete the leftovers, never mix the two.

## Syncing

Route by job size (ADR-012) — never drain a mailbox from chat, and
never rebuild the corpus through the raw gmail@v1 connector:

**First, smoke-check the credential** — an unattended chain armed on
a dead grant just burns its 5 hops with nobody watching:
`use("connectors:gmail@v1").list_messages(max_results=1)`. `{ok:
True}` → proceed. A not-connected error → run
`use("connectors:googleAuth@v1").connect()` (opens Google consent in
the user's browser, blocks ≤120s, default scopes include
gmail.readonly; user-facing turns ONLY, never cron); on
consent_timeout tell the user to finish in the browser and poll
googleAuth.status() until connected. Re-probe, then arm.

**Scope is a conversation, not a default.** Before arming an initial
backfill, settle the flow with the user in ONE question: time window
(default newer_than:1y, whole history opt-in), exclusions (offer
the usual noise — newsletter/notification senders — as `-from:x`
negative terms; never `-category:` filters, categories overlap real
mail), and whether to register the steady-state cron once the backlog
drains. A wrong scope means re-fetching everything — cheap to ask
once, expensive to redo.

- **Initial sync / big backlog →
  start_backfill(space, agent_space, q=None).** The reliable path:
  arms a self-chaining once-trigger — every hop is its own run with a
  fresh fuel budget, a crashed hop resumes from the checkpoint, a
  circuit breaker stops the chain after 5 failed hops. space is
  where the mail lands; agent_space is YOUR serving space
  (baoSpaceConfig — the trigger lives on its anchor). Idempotent:
  re-arming resumes, never restarts. Tell the user it runs unattended
  and how to watch it. When the chain ends — drained or breaker — a
  system-nudge turn arrives in this chat: process it and REPLY with
  the one-message update it asks for (never chat_send it yourself —
  your reply is delivered automatically).
- **"Stop the sync" → stop_backfill(space, agent_space).** Disarms
  the pending hop and closes the bar; the checkpoint stays, so a later
  start_backfill with the same q resumes where it stopped (what was
  synced stays synced). `stopped: False` = nothing was armed.
- **Steady state → a cron trigger**: an agent_triggers record with
  kind `"cron"`, program `"connectors:gmailSync@v1"`, args
  `{"space", "q"?}` — each tick is one coalesced history increment.
- **A "sync now" nudge from chat →
  sync_now(space, q?, max_messages≤50).** One bounded tick sharing
  YOUR run's 50B fuel (~0.3–0.6B per message) — never loop it over a
  backlog; hand off to start_backfill instead.

q is Gmail search syntax; default newer_than:1y, whole history is
opt-in, exclusions are negative terms (`-from:x -label:y`). Widening
a drained sync's window = start_backfill with the new q — it
re-lists the new scope (synced mail skips); sync_now and the cron
NEVER re-list, their q only filters what a tick sees.

**Coverage = `status(space).q`, nothing else.** Never infer it from
the newer_than:1y default (the arm may have used any q) or from the
dates of synced messages (a 7-day corpus also "falls within" two
weeks — absence of older mail proves nothing). Asked for a window
wider than `status().q` ⇒ re-arm start_backfill with the wider q.

Watch progress with status(space) → `{q, cursor, pageToken,
syncedCount, skippedCount, skipped, mailboxId, emailCount}` (empty
pageToken = backlog drained, now ticking incrementally) or
`use("agent:progress@v1").jobs(space)` (job `"gmail-backfill"`) — the
UI renders the bar GLOBALLY (server process registry), whatever space
is open.
Diagnosing a stalled chain: a trigger record's `lastStatus: "ok"`
means the hop RAN, not that it synced — sync truth is status(space)
and the failed process row (`use("agent:progress@v1").jobs(space)`,
visible ~60s) — durable truth lives in status(space).
skippedCount = messages listed but NOT synced because the cleaner
could not process them (one weird email never stops a slice or trips
the breaker; skipped lists the newest with the reason). Tell the
user the count when it is non-zero; retry_skipped(space) re-runs
them through the current cleaner without re-listing the mailbox —
worth a try after a runtime update, otherwise they stay listed.

Re-arming start_backfill on a space synced before the
dataset move re-ingests everything into the dataset automatically
(one-shot state migration); the legacy per-message email objects
stay behind until the user asks to delete them.
