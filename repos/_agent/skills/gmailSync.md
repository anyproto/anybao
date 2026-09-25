# Skill: gmailSync

Gmail in a space: reading synced mail, and syncing it with
connectors:gmailSync@v1 (help() it for backfill, stop, cron, status).

## Reading the synced mail

Answer "what did X and I email about" from the synced corpus, never
the live gmail connector (slower, quota-bound, blind to the cleaned
corpus); go live only for mail not yet synced.

- Each gmail address has one mailbox object in the space:
  `query_objects(space, filter={"any.type": "mailbox"})`. Its mail is
  email_messages records, one per message, id = the Gmail message id.
- Read with `query(space, mailbox_id, "email_messages", filter=…,
  sort=["-internalDate"], limit=…)`, not query_objects. Fields are
  plain keys: threadId, from, to, cc, subject, date, labelIds,
  internalDate (ms), snippet, body (markdown), participants
  (lowercase addresses), summary, notes (the user's own; edit only on
  request). A person: `{"participants": "ruud@ruuda.nl"}`; a label:
  `{"labelIds": "STARRED"}`. Same threadId = one conversation.
- Counts: `aggregate(space, [{"$count": "n"}], object_id=mailbox_id,
  dataset="email_messages")`. c.search covers mail under scope email.

## Syncing

1. Check the credential first:
   `use("connectors:gmail@v1").list_messages(max_results=1)`. Not
   connected → `use("connectors:googleAuth@v1").connect()` (opens
   Google consent in the browser; user-facing turns only), then
   re-check.
2. Settle scope in ONE question before an initial backfill: time
   window (default newer_than:1y), senders to exclude (as `-from:x`,
   never `-category:`), and whether to keep it synced with a cron.
3. Pick the call: a first sync or big backlog = start_backfill (runs
   unattended; its end arrives as a message in this chat, answer it
   with a reply); "stop" = stop_backfill; ongoing = a cron trigger
   running connectors:gmailSync@v1; "sync now" = sync_now with
   max_messages ≤ 50, never looped over a backlog.

Coverage is `status(space).q`, never guessed from dates. A non-zero
skippedCount: tell the user and offer retry_skipped(space).
