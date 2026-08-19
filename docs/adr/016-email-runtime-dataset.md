# ADR-016: Email corpus on runtime dataset schemas

Status: **Accepted** (2026-08-19; user-directed move after any PR #161
landed runtime datasets)
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

A user type **`mailbox`** (xKey `mailbox`, property `address`), one
object per synced address, ensure-created by gmailSync. On it, a
runtime dataset **`email_messages`** declared via
`POST …/types/:mailbox/datasets`:

- `idRule: user` — **record id = Gmail message id** (hex, fits the
  default id pattern). The id doubles as the upsert idempotency key;
  no `gmail_id` field exists.
- `deleteBy: author`, with `creator`/`createdAt`/`modifiedAt` stamp
  fields (creator stamp is required by the author gates; the sync
  account is the single writer, per the SDK's IdRule:user contract).
- `skipHistory: true` — the provider is the source of truth; label
  churn must not accrete history rows (#156's stance, kept).
- `search: {title: subject, text: body}` — the x-search mapping; the
  server's SchemaChunker indexes records under scope `basic`, so mail
  participates in `/search` without `editor_blocks`.
- Fields (C1: Gmail's own names, camelCase, verbatim): `threadId`,
  `from`, `to`, `cc`, `subject`, `date`, `internalDate` (number, the
  sort key), `snippet` — write-once; `labelIds` (array,
  `mutableBy: author`) — the one provider-mutable fact; derived
  fields `body` (clean_html markdown, ADR-012 §4 unchanged),
  `signature` (the §4 split, previously discarded — persona raw
  material now lands), `participants` (normalized from+to+cc address
  array, see §3). **No `required` fields** — mail is garbage-tolerant;
  an odd message must degrade to empty strings, never reject.

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
| bespoke `email` search scope, subject BM25F | x-search under the generic `basic` scope — good enough, and the whole point of #161 is no mail-specific chunker. |
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

The dataset machinery is generic agent surface, not gmail plumbing —
emails are merely the first corpus. New flat methods, 1:1 over the
server endpoints: `create_dataset` (ensure-by-name composite, the
`create_type` pattern), `list_datasets`, `remove_dataset`,
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
autofix); `_any.md` documents the corpus move so the agent reads the
dataset, not the stale objects.

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
