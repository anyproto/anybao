# ADR-016: Email corpus on runtime dataset schemas

Status: **Accepted** (2026-08-19; user-directed move after any PR #161
landed runtime datasets), amended 2026-09-24 (reading guidance lives in
the on-demand `gmailSync` skill — BOB-160)
Date: 2026-08-19
Builds on: ADR-012 (gmail sync — §2 sync algorithm, §4 clean_html and
the backfill chain all survive; §3 storage model superseded here, §5
linking amended), ADR-010 §8 (flat any@v1 surface), ADR-006 §6
(xKey-addressed catalog)
Upstream contracts: `~/any/any` PR #161 (SYN-147 — runtime dataset
schemas, generic `/upsert`, x-search chunker; docs/03-api.md § Runtime
dataset schemas), any-sync-sdk `docs/17-user-datasets.md`

## Context

ADR-012 §3 stored mail as **one `email` object per message** with the
body in `editor_blocks` via `put_markdown` — the only shape the server
offered at the time. Costs, all observed live: two non-atomic HTTP
writes per message (orphan objects on body-write failure, §2's
existence check is load-bearing); one CRDT tree + objects-collection
row + nav presence per email (a drained whole-mailbox sync is tens of
thousands of objects polluting `query_objects`, name search, and every
cross-object scan); label flips as per-object `update_object` round
trips; bodies smuggled through `editor_blocks` to reach the search
index.

any PR #156 prototyped the fix as a compiled-in `email` type — one
`email_messages` dataset record per message on a per-address mailbox
object — and was **closed on principle**: nothing mail-specific
belongs in `any` core. any PR #161 (SYN-147, merged 2026-08-19) landed
the generic replacement: **runtime dataset schemas** — a declarative
schema on a user type, enforced by the SDK apply path (required
fields, write-once vs author-mutable, author-only delete, derived
stamps, caller record ids), plus a schema-driven batch `/upsert` and
an `x-search` chunker. This ADR re-derives #156's email model on those
primitives, entirely in anybao.

## Decision

### 1. Storage model

**Amended 2026-09-08 (ADR-027 §2):** `email_messages` is the dataset KEY of one part on the `mailbox` type (draft `key`, not `name`); records live in the collection the declaration reports, and every read and write names the key — resolved against the mailbox object's types by `any@v1`.

A user type **`mailbox`** (xKey `mailbox`, property `address`), one
object per synced address, ensure-created by gmailSync. The type is
**listed, never `hidden`** (amended 2026-09-10): the client reaches a
mailbox through Collections, favorites, search and links and opens the
object as its inbox layout (any-ui `docs/email-dataset-ui.md`); a hidden
type drops out of Collections and the corpus has no entry point left.
Minting a listed type also installs the space's Collections app
(ADR-027 §5), so a fresh space gets the surface with the mailbox. On it, a
runtime dataset **`email_messages`** declared via
`POST …/types/:mailbox/datasets`:

- `idRule: user` — **record id = Gmail message id** (hex, fits the
  default id pattern). The id doubles as the upsert idempotency key;
  no `gmail_id` field exists.
- `deleteBy: author`, with `creator`/`createdAt`/`modifiedAt` stamp
  fields (the time stamps are `datetime` instants, ADR-019) (creator stamp is required by the author gates; the sync
  account is the single writer, per the SDK's IdRule:user contract).
- `skipHistory: true` — the provider is the source of truth; label
  churn must not accrete history rows (#156's stance, kept).
- `search: {title: subject, text: [body, notes], scope: email}` — the
  x-search mapping; the server's SchemaChunker indexes records under
  the declared scope, so mail participates in `/search` without
  `editor_blocks`. **Amended 2026-08-20**: the server grew
  `search.scope` on runtime dataset declarations (any PR #173 / SDK
  #101); mail moves from the generic `basic` scope to its own `email`
  scope — raw mail stops polluting basic content search, and recall@v1
  adds `email` to its default scopes so bao still finds it. Already-
  indexed records keep their stored scope until they re-index.
  **Amended 2026-08-21**: `text` names several fields (SYN-179 — the
  indexer joins the values in mapping order) so user notes index
  alongside the body; `summary` is deliberately NOT mapped — it
  derives from the indexed body and regenerates. Requires a server
  carrying SYN-179; on older servers the array form rejects at
  declaration time. **Amended 2026-08-25**: `text` is `[from, body,
  notes]` — the sender leads, so "mail from X" is a search hit, not
  only a `participants` filter (live: sender queries ranked None in
  every mode on a 427-message corpus). `from` sits first because the
  embedder clamps long records to their head (`docs/13-index.md`).
  Already-indexed records keep the old text until they re-index —
  the index is "from the next change".
- Fields (C1: Gmail's own names, camelCase, verbatim): `threadId`,
  `from`, `to`, `cc`, `subject`, `date`, `internalDate` (number, the
  sort key), `snippet` — write-once; `labelIds` (array,
  `mutableBy: author`) — the one provider-mutable fact; derived
  fields `body` (clean_html markdown, ADR-012 §4 unchanged),
  `signature` (the §4 split, previously discarded — persona raw
  material now lands), `participants` (normalized from+to+cc address
  array, see §3); annotation fields `summary` (generated digest — the
  UI fills it via a small program on a cheap model tier; on failure it
  shows the error and fills nothing) and `notes` (the user's own
  markdown, edited as plain text in the UI — a block editor cannot
  attach to a record: `editor_blocks` is per-object), both string and
  `mutableBy: author`, written after ingest and never by the sync —
  sync upserts omit them, so a re-hydrate or label flip cannot clobber
  an edit. **No `required` fields** — mail is garbage-tolerant; an odd
  message must degrade to empty strings, never reject.

Threads stay data, not structure: same `threadId` = one conversation,
sorted by `internalDate` (#156's stance, kept). `sync_state` remains
the ADR-012 §2 object unchanged, gaining `mailbox_id` (the resolved
mailbox object) and `store` (§5 migration marker).

### 2. Review of #156's structure — kept vs re-derived

| #156 (compiled-in) | here (runtime dataset) |
|---|---|
| per-address mailbox object, seed-derived (`any/email-mailbox/v1/…`) | ensure-by-address: query `mailbox.address`, oldest wins, create on miss. No public derive endpoint exists — and none is needed: the single-ingest-writer contract makes convergence-by-derivation moot. Duplicate mailboxes are never auto-deleted (deleting one deletes its records). |
| record id = provider message id | identical (`idRule: user`) |
| write-once + mutable allow-list `labelIds`, `historyId` | `labelIds` only. Per-record `historyId` dropped — our cursor is space-level in `sync_state`, an unused field is noise. |
| server-derived `participants` + sparse multikey index | client-derived in gmailSync (SDK v1 has **no computed fields** — deliberate, replica determinism). stdlib `email.utils.getaddresses` over From/To/Cc (kernel allowlists `email`, ADR-012 §6), lowercased, deduped, bcc absent by construction. Write-once: label churn never touches it, and a re-hydrate produces the same value. No index declaration exists on runtime datasets; filter cost is the server's concern. |
| bespoke `email` search scope, subject BM25F | x-search under a declared `email` scope (2026-08-20, `search.scope` on the declaration — still no mail-specific chunker; was `basic` until any PR #173). BM25F stays out. |
| bespoke batch endpoint (`created`/`updated`/`unchanged` + label diff) | the generic `/upsert` gives exactly this: absent→create, present→diff of declared-mutable fields, identical→skip, per-record rejections, one CRDT change per page. |
| attachments manifest via files v2 | out of scope, as it already was in ADR-012 (bodies only; raw stays in Gmail). |
| `email_sync_state` sibling dataset | not adopted — ADR-012's `sync_state` object already works and the backfill chain depends on its semantics. |

### 3. Write path (ADR-012 §2 mechanics, re-based)

Hydration (25-part batches, fuel governor, checkpoint-after-commit)
is unchanged. Per hydrated chunk the sync now issues **one
`upsert_records` call** instead of per-message
`create_object` + `put_markdown` — the non-atomicity §2 guarded
against is gone (the body rides in the record; a page is one CRDT
change).

- The **existence pre-check** (`query` on the dataset,
  `{"id": {"$in": ids}}`) survives with a demoted role: it gates
  *hydration + clean_html cost* (Gmail API time and fuel), not
  correctness — `/upsert` is idempotent regardless.
- **Label changes** upsert `{id, fields: {labelIds}}` for ids the
  pre-check confirmed exist. Never for absent ids: upsert would
  *create* a labels-only stub for a message outside the synced scope.
- **Hard deletes** go through the generic
  `POST …/delete-records {objectId, dataset, recordIds}`; ids never
  reuse (tombstones), which is correct for Gmail's spam-purge
  semantics.
- `made`/`skipped` counters read the upsert reply
  (`created`+`updated` / `skipped`) — the server's truth, not the
  client's guess.

### 4. any@v1 surface (ADR-010 §8)

**Amended 2026-09-08 (ADR-027 §2):** `_create_dataset` declares one part per store and returns `collection`; `query` / `upsert_records` / `delete_records` / `aggregate` take the store key.

The dataset machinery is generic program surface, not gmail plumbing —
emails are merely the first corpus. Flat methods, 1:1 over the server
endpoints. Declaration is `_`-private (hidden from `## Tools` and
`help()`, ADR-010 §1; amended 2026-08-31): a program that owns a store
declares its datasets (ADR-017 §1), the chat agent is never offered
the ability — it only reads and writes records through the public
`query` / `upsert_record(s)` / `delete_records`. `_create_dataset`
(ensure-by-name composite, the
`create_type` pattern; since 2026-08-20 the draft stays authoritative
for the mutable `search.*` leaves — a drifted title/text/scope on an
existing def is PATCHed back, so scope changes roll out by deploy;
`text` drift-compares as a normalized list since the server stores a
single-element array as the bare string, SYN-179),
`_list_datasets`, `_remove_dataset`; public
`upsert_records`, `delete_records`; `aggregate` gains
`object_id`/`dataset` params for the per-object variant (dataset field
refs are raw keys — no xKey resolution, no dexify). Dataset field
paths in `query`/`upsert` filters are plain keys, never
`<typeId>.<propId>` pairs. The stale `upsert_record` docstring claim
("only registered datasets are writable") is corrected — declared
runtime datasets are first-class now.

### 5. Migration from the object model

Automatic on next tick, keyed by the `store` marker: a `sync_state`
lacking `store: "email_messages"` is object-era state — its cursor
would otherwise turn every tick into an incremental no-op that never
backfills the dataset (the ADR-012 §2 scope-change hazard, same
mechanism). The tick clears `cursor`/`page_token`, stamps the marker,
and the next slices re-list the whole scope; dataset-keyed idempotency
means every message re-ingests exactly once. Legacy `email` objects
are left in place — deleting thousands of user-space objects is the
user's call, not a sync side effect (memory rule: analyze, don't
autofix); the on-demand `gmailSync` skill documents reading the
corpus, so the agent reads the dataset, not the stale objects
(amendment 2026-09-24, BOB-160: the reading guidance moved there from
the always-on `_any`).

## Consequences

A drained mailbox is one mailbox object + N records instead of N
objects + N editor trees: `query_objects` and name search stay clean,
label flips are single-path `$set`s diffed server-side, a sync page is
one CRDT change, and per-message trace cost drops (one batched effect
per 25 messages replaces 2+ effects per message). Mail reads move from
`query_objects`+`get_markdown` to `query(mailbox, "email_messages")`
with plain-key filters (`{"labelIds": "TRASH"}` contains-matches,
`sort: ["-internalDate"]`) — skills updated accordingly. Search hits
now resolve to (mailbox object, record) rather than a per-email
object. The people spine (ADR-012 §5) is unamputated but re-keyed:
records carry no links-format properties, so email↔person joins run
over `participants` address strings against `people.email_addresses`
— the enrichment follow-up consumes the same join.
