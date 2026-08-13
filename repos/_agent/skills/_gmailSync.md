# Skill: _gmailSync

Gmail → space sync (`connectors:gmailSync@v1`, ADR-012). Route by job
size — never drain a mailbox from chat, and never rebuild the corpus
through the raw `gmail@v1` connector:

- **Initial sync / big backlog →
  `start_backfill(space, agent_space, q=None)`.** The reliable path:
  arms a self-chaining once-trigger — every hop is its own run with a
  fresh fuel budget, a crashed hop resumes from the checkpoint, a
  circuit breaker stops the chain after 5 failed hops. `space` is
  where the mail lands; `agent_space` is YOUR serving space
  (`baoSpaceConfig` — the trigger lives on its anchor). Idempotent:
  re-arming resumes, never restarts. Tell the user it runs unattended
  and how to watch it.
- **Steady state → a cron trigger**: an `agent_triggers` record with
  kind `"cron"`, program `"connectors:gmailSync@v1"`, args
  `{"space", "q"?}` — each tick is one coalesced history increment.
- **A "sync now" nudge from chat →
  `sync_now(space, q?, max_messages≤50)`.** One bounded tick sharing
  YOUR run's 50B fuel (~0.3–0.6B per message) — never loop it over a
  backlog; hand off to `start_backfill` instead.

`q` is Gmail search syntax; default `newer_than:1y`, whole history is
opt-in, exclusions are negative terms (`-from:x -label:y`).

Watch progress with `status(space)` → `{cursor, pageToken,
syncedCount, emailCount}` (empty pageToken = backlog drained, now
ticking incrementally) or the `agent-progress` object (job
`"gmail-backfill"`) in the target space — the UI renders it live.

Reading the synced corpus (the `email` type, thread queries, label
filters) is the `_any` skill's job.
