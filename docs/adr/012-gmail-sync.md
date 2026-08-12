# ADR-012: Gmail mailbox sync — space objects, email cleanup, contact graph

Status: **Proposed**
Date: 2026-08-11
Builds on: ADR-002 (effect boundary; §4 tier-1 allowlist — amended
here), ADR-003 (kernel — vendored guest modules), ADR-008 §2 (http
surface), ADR-009 (connectors overlay), ADR-010 (docstrings as docs;
§8 flat any@v1), ADR-011 (managed Google credential)

## Context

Target: a persona-first knowledge base built from a working inbox —
every email a space object, every correspondent a contact object the
emails link to (the "VC/CEO brain"; see Creandum's "A VC's AI Brain"
write-up for the shape this imitates: person-centric indexing, a named
html→text filter between ingestion and storage, raw kept upstream).
Gmail is the first source; gmail@v1 stays a thin read-only API wrapper
and gains no sync logic.

A server-side **email dataset** (`~/any/any` branch
`zarkone/email-dataset`, `docs/21-email.md`: per-address mailbox
object, `email_messages` records keyed by provider id, idempotent
batch ingest, a dedicated `email` search scope) was built and
rig-validated 2026-08-11 as an alternative storage surface, and
**rejected 2026-08-12** (user decision): mail stays the userspace
`email` type this ADR describes — ordinary objects on stock server
main. The dataset branch remains unmerged; nothing here depends on
it.

Everything below is probe-verified against a real 41.8k-message
mailbox (2026-08-10/11, scratch programs + traces referenced inline;
sources preserved in gitignored `scratch/gmail-sync-probes/`). The
load-bearing numbers:

- Batched `messages.get` (multipart `POST /batch/gmail/v1`) is 7×
  faster than sequential through the broker, but >~25 concurrent
  parts trips Gmail's per-user concurrency limit — 100-part batches
  returned 22 inner 429s, 25-part batches zero (run_5051ddd6237c4be0).
- `history.list` deltas are fine-grained and repeat message ids across
  records; bare no-delta records occur. Trash arrives as
  `labelsAdded: TRASH`; `messagesDeleted` means hard-delete and occurs
  naturally via spam purge (run_c7661e28, run_1f7108069e314da3).
- Trace cost is ~58KB per synced message (run_21e036da6c2e43c8) — a
  whole-mailbox single run would be ~2.4GB of trace.
- `create_object` + `put_markdown` is not atomic — one observed orphan
  (object created, body write failed, message counted as failed).
- Backlinks exist server-side over links-format properties only, as a
  live scan without an index (`~/any/any
  internal/server/handlers_backlinks.go`); links values are arrays of
  `any://<objectId>` strings; query filters match them only with the
  prefix (run_165506a028bf45d7, run_07820022744f4a83).
- html→markdown via vendored pure-Python bs4 + markdownify plus five
  email passes: 20 real fixtures (newsletters, LinkedIn, GitHub,
  invites, human mail) → 12% of input size, 0.3s total, no lxml.
- Contact wiring over 83 synced emails: 47 contacts, all emails
  linked, forward-link counts == backlinks exactly
  (run_93be4c9eef224cae).

## Decision

### 1. One program: `gmailSync@v1` (connectors repo)

A folder program in `repos/_connectors/programs/`, `__any_tool__`,
using `use("gmail@v1")` for listing/labels and its own batched
hydration. Helpers are public `@span` methods on it — notably
`clean_html` — not separate programs (user rule 2026-08-11: features
are programs, helpers are methods). Runs as a cron trigger registered
via the `agent_triggers` dataset; also user/agent-invocable
(`sync_now`, `status`).

### 2. Sync algorithm

**State**: one `sync_state` object in the target space —
`{cursor (historyId), page_token, synced_count}` — written only after
a batch commits.

**Full sync** is chunked across cron ticks: each tick lists one
bounded slice (≤200 messages) of the configured scope, hydrates via
25-part batches with per-part 429 retry, writes objects, checkpoints
`page_token`; the run ends. Scope defaults to `newer_than:1y`
(configurable q/labels); whole-history is opt-in. Rationale: trace
size and crash-loss containment, not API quota — and **fuel**
(ADR-003 §2 as amended): parsing and cleaning mail in interpreted
wasm CPython is fuel-hungry (a 500-message fetch-and-parse exhausted
the old 5B budget mid-run), so the slice constant is a soft cap and
the tick's real governor is cooperative — **the loop checks
`fuel.state` between hydration batches and, when the remaining
budget drops below a floor, checkpoints and exits cleanly**: sync as
much as fits in this tick. An out-of-fuel trap kills the run
unrecoverably; the typed `FuelExhausted` error is the diagnostic
backstop, never the control flow. Per-message `clean_html` fuel is
measured at implementation; if it dominates, the first lever is a
leaner purpose-built converter, not a host syscall.

**Incremental tick** (steady state): `history.list` from the cursor,
**coalesced per message id across all records** (never applied
record-by-record) into added / label-changed / hard-deleted sets;
adds hydrate `format=full`, label changes `format=minimal`, deletes
delete the object; cursor advances to the reply's `historyId`. A 404
on the cursor (Gmail retains history ~a week) falls back to a scoped
re-list from the newest synced `internalDate`.

**Idempotency**: existence-check by `gmail_id` before create — this is
load-bearing because create+body-write is non-atomic and because the
fallback path re-lists already-synced messages. Verified: stale-cursor
replay creates zero duplicates (run_48b36ef7536647d2).

### 3. Email objects

Type `email`, one object **per message** (thread granularity decided
against: label/delete deltas are per-message, and threads remain
reachable via `thread_id`). Properties keep Gmail's names per C1
(`gmail_id`, `thread_id`, `label_ids`, `internal_date`, `snippet`,
`date`) plus header scalars `from`/`to`/`subject` as **plain strings**
— filterable and aggregatable — and `contacts`, a links-format
property (see §5). Body = `clean_html` markdown via `put_markdown`;
the raw MIME stays in Gmail, which is the raw store — the space holds
only the cleaned corpus.

Bulk mail is synced, not skipped: noise is a read-time filter (Gmail's
own `CATEGORY_*` labels, the bulk-header derived keys), never an
ingestion drop — Gmail keeps everything, the space stays complete for
its scope. (People creation is gated differently — §5.)

### 4. `clean_html` — the named filter

Vendored **pure-Python** `bs4` (html.parser backend) + `markdownify`
in the kernel (§6), wrapped by five email-specific passes, in order:
(1) layout-table flattening — cells become blocks, table scaffolding
unwraps (email tables are layout; without this, calendar/LinkedIn mail
is markdown-table soup); (2) quoted-chain removal (`gmail_quote` et
al., cited blockquotes) + hidden-preheader and tracking-pixel drops;
(3) link hygiene — tracking-wrapper unwrap, `utm_` strip, untraceable
links collapse to their text; (4) notification-footer trim — strong
markers ("Reply to this email directly", "You are receiving this
because") cut anywhere, weak ones ("Unsubscribe") only in the trailing
fifth; (5) signature split — returned separately
(`{markdown, signature}`), signatures being persona raw material, not
body noise. Implementation hazard recorded from the probe: no nested
quantifiers in cleanup regexes (a `(\s*\|\s*)+` pass went exponential
on LinkedIn's empty-cell runs).

### 5. People — one person spine, shared with any-ui

There is no sync-private contact type. The person spine is the
`people` type any-ui provisions per space, with a consolidated shape
decided 2026-08-11 (any-ui adjusts its provisioning to match):

| prop | shape | written by |
|---|---|---|
| name | (any.name) | sync on create; UI |
| email_addresses | array of address strings | sync — the resolve key |
| phones | array | UI / future sources |
| role | string | UI / enrichment |
| organization | string | UI / enrichment |
| location | string | UI |
| tags | multiselect | UI |
| *(markdown body)* | dossier prose | enrichment / human |

No machine bookkeeping on the type: `kind`, `is_self`,
`last_interaction` were all considered and rejected — People carries
only the human record. `email_addresses`, not "emails": the space has
an `email` TYPE, and a person prop named `emails` reads as email
object ids (it is address strings; the object link runs the other way,
email.contacts → people). A company/org type is deliberately deferred;
`organization` stays free text.

Injection: each parsed `From`/`To`/`Cc` address resolves through a
per-run cache → `query_objects` contains-match on
`people.email_addresses` → create on miss, writing only name +
email_addresses; the email object's `contacts` links property gets the
`any://<id>` set. **Bulk senders get no people object at all** — the
noise gate (`List-Unsubscribe`/`Precedence` headers, which gmail@v1's
trim gains as derived keys, plus address heuristics — headers catch
most bulk but not all, 13/20 fixtures) gates *creation*, not a kind
field; machine senders remain plain `from` strings on the email
objects. The owner's address is an ordinary person. Provisioning
contract: any-ui provisions the type; the sync ensure-resolves the
identical shape only when it reaches a space first.

Reverse lookup ("every email with this person") uses `backlinks()` or
a prefixed links-value query — **interactive use only, never inside
sync loops**: it is a full collection scan until the server grows a
reverse index (upstream follow-up, noted in the handler).

LLM enrichment of people (title/company from signatures,
relationship dossier in the person body) is a follow-up cron in the
extraction@v1 pattern — out of scope here, gated on its own eval
(synthetic-corpus ground truth, in progress separately).

### 6. Kernel amendment (ADR-002 §4, ADR-003)

Tier-1 allowlist gains stdlib `html` and `email` (both pure, no
ambient authority — they meet the existing tier-1 criterion), and the
kernel bundles vendored pure-Python `bs4` + `markdownify` (MIT) under
`runtime/guest/`, importable by guest programs through the same
allowlist mechanism. ADR-002 §4 is amended in the implementing change.

### 7. Guidance and client fixes riding along

- `_any.md`: links-format values are `any://<objectId>` strings — a
  fetchable object reference (`get_markdown`/`query_objects`); write
  shape `["any://"+id]`; filters need the prefix; prefer `search` to
  opening candidate objects one by one.
- any@v1 `create_type`: forward `format` on inline properties (today
  it forwards only kind/meta — links props silently land as strings).
- Upstream (`~/any/any`): create-path `property.format_violation`
  omits the expected shape that the patch path names; reverse index
  for backlinks at scale. Operational: a plain `go build` of the
  server silently compiles search out (startup WARN only) — dev/rig
  builds must carry `-tags 'fts vector'` (`make build` does).

## Consequences

Steady state is one sub-second incremental tick per cron period plus
one bounded backlog slice while the full sync drains (~30–40 min of
API time for 41.8k messages, spread over ticks). The space gains two
types and a growing contact graph that later sources (calendar,
granola, attio) can attach to — contacts are the shared spine, email
is merely the first feeder. Rejected alternatives and their reasons
live in the probe notes (`scratch/gmail-sync-probes/`, session traces
2026-08-10/11).
