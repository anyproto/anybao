# Full API parity — standing plan

The coverage manifest (`api/coverage.json`) tracks every `any` endpoint;
`make api-drift` keeps it honest. This doc is the standing plan for what
each uncovered endpoint needs and WHEN — so nothing is lost as the port
proceeds. Timing principle: **cutover only requires parity with
bobrik-watch (category B).** New capabilities (C) are demand-driven —
implemented when a use case or the owning milestone pulls them, not
speculatively.

## A. Harness-internal — `excluded` (never an agent facade)

The agent never calls these; harness plumbing does. Marked excluded in
the manifest with a reason (they must not count as agent-facing gaps):
health, shutdown, auth (GET/POST), account (GET) + account/metadata,
sync, sync-status (+ per-object + subscribe), debug (+ objects),
datasets + spaces/datasets, files/cache (+ free/sweep), delete-records,
raw spaces/modify (helper wraps it internally).

## B. Parity — old anyHelper had these; port before cutover (M4)

- **chat messages** — send / edit / delete / react / read + read-all.
  The agent posts to and manages chat. → chat facade (next).
- **editor blocks** — create / patch / delete + markdown/append.
  Atomic doc editing; append is how history/debug grow O(N). → editor
  facade (next).
- **types + properties creation** — POST types, POST types/:id/
  properties. The agent builds collections/schemas. → schema facade.
- **aggregate** — objects/aggregate + per-object aggregate. Grouping /
  counting. → aggregate facade.
- **query/subscribe + objects/query/subscribe** — live reads; mostly
  the HARNESS (watcher) not an agent facade → harness, not helper.
- **spaces**: PATCH (metadata), DELETE, POST (create), sync,
  properties/get, query, spaces/query — space management; partial agent
  need (create/rename), rest harness.

## C. New capabilities — old harness lacked; demand-driven (M5/M6)

- **ui/commands** (POST + subscribe) — agent drives the connected UI
  (open_space / open_object). Real agent capability (old bobrik `ui`
  tool). → **implementing now** (M4, cheap + good UX).
- **files v2** (attach / download / list / status / pin / retry /
  offload / query / stats) — shipped AFTER the old harness. High future
  value (uploaded docs, images, generated files). **When:** first real
  file use case, or M6 "capabilities the rewrite pays for."
- **identities** (list / get / subscribe) — contacts directory. The
  entity-canonicalization anchor for memory/graph (§4c). **When:** M5.
- **unread/read-tracking** — read-all, messages/:id/read; unread COUNTS
  are already readable as chat-object fields via query. Messenger-aware
  agent. **When:** with the chat facade if cheap, else M6.
- **members / acl / invites / one-to-one / register-incoming** —
  collaboration/sharing; mostly human/UI ops. Thin agent exposure ever.
  **When:** M6 or on-demand. Drift 2026-08-01 added to this group:
  invite accept/decline, guest-key mint/revoke (owner-side overlay op —
  today done via the `any` CLI, `docs/repo-overlay-e2e.md`).

## C2. Drift refresh 2026-08-01 (pin → any@main afa3ed4)

21 new endpoints triaged; `POST /spaces/join` contract change (documented
400 envelope + 409 guest-token-already-tracked) was already handled in
`serve.rs probe_or_join_overlay` — fingerprint refreshed only.

- **Mapped**: objects/:id/backlinks → `anyclient.backlinks` (any@v1
  `any.backlinks` — shipped ahead of the pin); enrich/apply → enrich@v1
  (guest http, the one direct-URL call); `PATCH`/`DELETE`
  types/:id/properties/:propId + properties/:id/attach|detach/:typeId
  → any@v1 `patch_property`/`set_option`/`remove_option`/
  `archive_property`/`delete_property`/`attach_type`/`detach_type`
  (ADR-022 §4; guest-direct, no host passthrough — the host client
  has no consumer).
- **Excluded**: debug/p2p (diagnostic); push/token GET/POST/DELETE +
  push/subscriptions (client push plumbing, UI-side).
- **Unmapped, demand-driven** (C): object **history** ×4 (version
  history/diff — future "what changed" capability); `PATCH
  spaces/:id/settings` (space management); chat
  **reactions-read** (read-tracking group); `DELETE files/:fileId`
  (files v2 group).

## D. M5-owned (not generic helper facades)

- **agent/memory** (POST/PATCH/DELETE) + **agent/brain** — the recall/
  write tools, wrapped by the recall tool in M5.
- **agent/turns + agent/chunks** — anyclient methods already added;
  consumed by the M4 history writer, not an agent facade.

## Future: ephemeral pub/sub for the thinking badge (note, 2026-07-08)

The "bao is thinking" badge and live progress narration currently ride
chat messages with `done:false` (progress bubbles, ADR-005). A
forthcoming **any-sync pub/sub** primitive (send events WITHOUT storing
a CRDT change) is the right long-term home: transient "thinking" /
progress signals should be ephemeral events, not persisted chat writes
that accumulate and sync. **Keep as-is for now** (progress bubbles);
revisit the thinking-badge + interim narration on top of pub/sub when it
lands. Same shape as the UI-command channel's at-most-once ephemerality
(plan §18) — a natural fit.
