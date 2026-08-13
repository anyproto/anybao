# Skill: _gmailSync

Gmail → space sync (`connectors:gmailSync@v1`, ADR-012). Route by job
size — never drain a mailbox from chat, and never rebuild the corpus
through the raw `gmail@v1` connector:

**First, smoke-check the credential** — an unattended chain armed on
a dead grant just burns its 5 hops with nobody watching:
`use("connectors:gmail@v1").list_messages(max_results=1)`. `{ok:
True}` → proceed. A not-connected error → run
`use("connectors:googleAuth@v1").connect()` (opens Google consent in
the user's browser, blocks ≤120s, default scopes include
gmail.readonly; user-facing turns ONLY, never cron); on
`consent_timeout` tell the user to finish in the browser and poll
`googleAuth.status()` until connected. Re-probe, then arm.

**Scope is a conversation, not a default.** Before arming an initial
backfill, settle the flow with the user in ONE question: time window
(default `newer_than:1y`, whole history opt-in), exclusions (offer
the usual noise — newsletter/notification senders — as `-from:x`
negative terms; never `-category:` filters, categories overlap real
mail), and whether to register the steady-state cron once the backlog
drains. A wrong scope means re-fetching everything — cheap to ask
once, expensive to redo.

- **Initial sync / big backlog →
  `start_backfill(space, agent_space, q=None)`.** The reliable path:
  arms a self-chaining once-trigger — every hop is its own run with a
  fresh fuel budget, a crashed hop resumes from the checkpoint, a
  circuit breaker stops the chain after 5 failed hops. `space` is
  where the mail lands; `agent_space` is YOUR serving space
  (`baoSpaceConfig` — the trigger lives on its anchor). Idempotent:
  re-arming resumes, never restarts. Tell the user it runs unattended
  and how to watch it. When the chain ends — drained or breaker — a
  system-nudge turn arrives in this chat: process it and REPLY with
  the one-message update it asks for (never chat_send it yourself —
  your reply is delivered automatically).
- **Steady state → a cron trigger**: an `agent_triggers` record with
  kind `"cron"`, program `"connectors:gmailSync@v1"`, args
  `{"space", "q"?}` — each tick is one coalesced history increment.
- **A "sync now" nudge from chat →
  `sync_now(space, q?, max_messages≤50)`.** One bounded tick sharing
  YOUR run's 50B fuel (~0.3–0.6B per message) — never loop it over a
  backlog; hand off to `start_backfill` instead.

`q` is Gmail search syntax; default `newer_than:1y`, whole history is
opt-in, exclusions are negative terms (`-from:x -label:y`). Widening
a drained sync's window = `start_backfill` with the new q — it
re-lists the new scope (synced mail skips); `sync_now` and the cron
NEVER re-list, their q only filters what a tick sees.

Watch progress with `status(space)` → `{cursor, pageToken,
syncedCount, emailCount}` (empty pageToken = backlog drained, now
ticking incrementally) or the `agent-progress` object (job
`"gmail-backfill"`) in the target space — the UI renders it live.
Diagnosing a stalled chain: a trigger record's `lastStatus: "ok"`
means the hop RAN, not that it synced — sync truth is `status(space)`
and the progress object's `error` field.

Reading the synced corpus (the `email` type, thread queries, label
filters) is the `_any` skill's job.
