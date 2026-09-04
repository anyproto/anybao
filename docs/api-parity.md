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

## C4. Drift refresh 2026-09-04 (pin → any a176029, staging-clean :7141 `/v1/openapi.json`)

Closes BOB-66 (the 30 routes C3 left untriaged) plus 13 routes new
since the C3 pin. `anyrt drift` is clean.

- **Changed contract, fingerprint refreshed only**: 14 routes. Chat
  message send carries `agent.outcome`, `context`, `control` (ours:
  ADR-005 §5, the outcome/hard-break work); search gains optional
  `passages` / `maxData` and per-hit passages in the reply (additive —
  a capability `helper.search` can expose on demand); every query route
  types `projection` values as integers and the space query adds
  optional `includeDeleted` (no wire change); type property creation
  gains `xKind` (adopted); auth + shutdown carry mode, capabilities and
  the `X-Any-Control-Token` header (SYN-169 — harness routes, see the
  open item below).
- **Mapped** (existing callers, recorded): processes ×5 →
  `list_processes` / `cancel_process` / the `_process_*` wrappers
  behind progress@v1; bundles ×5 → `ensure_bundle` / `list_bundles` /
  `get_bundle` / `bundle_child` / `resolve_loser`; datasets GET/POST/
  PATCH/DELETE → `list_datasets` / `create_dataset` (POST + in-place
  PATCH) / `remove_dataset` — program plumbing, `_`-private on the
  flat surface (ADR-010 §1, ADR-017 §1): the chat agent never sees a
  dataset DECLARATION, only records through query / upsert_records /
  delete_records; `POST /spaces/:id/upsert` →
  `upsert_records`; properties attach/detach → `attach_type` /
  `detach_type` (the C2 mapping, lost in C3 when the path parameter
  was renamed); `POST /events` → `open_in_ui` (+ serve); derived
  spaces GET / POST-by-name → host `list_derived_spaces` /
  `create_derived_space`; devices GET / PUT me / activate → the host's
  ADR-015 election client.
- **Excluded**: `/local/*` ×13 (the ADR-023 trace store — host anyapi
  only, no guest route); `DELETE /auth` (harness auth/boot); devices
  DELETE / query / query-subscribe (client device management);
  `GET /events/subscribe` (client push plumbing).
- **Present, pending a helper** (the history-routes form): dataset
  field POST/DELETE (bao should be able to add/remove a field — today
  `create_dataset` replaces the whole list; a helper is wanted) and
  `GET /spaces/:id/objects/:objectId` (`get_object` reads through the
  query route; the plain GET has no consumer).
- **Open items from this pass**: (1) a guest `list_devices` so bao can
  tell the user which devices exist, which is active and how to
  switch (needs a UI settings page or the chat trigger to surface it);
  (2) dataset field helpers (above). `POST /shutdown` needing the
  control token on a managed server touches nothing here: anyrt never
  calls it.

## C3. Drift refresh 2026-08-27 (pin → any 20708cd, staging :7134 `/v1/openapi.json`)

- **Removed** (stale mappings, routes gone from the server): agent/brain,
  agent/memory (+ item PATCH/DELETE), objects/:id/agent/chunks,
  objects/:id/agent/turns, ui/commands (+ subscribe). Their consumers
  moved to userspace datasets (ADR-017) — `append_turn` /
  `create_chunk` write through `modify`, not a dedicated route.
- **Changed contract, fingerprint refreshed only**: 21 routes (ACL
  family, spaces create/join/get/list/delete/settings, one-to-one,
  account, `GET /datasets`, push/token). Diffed the helper-mapped ones
  (`GET /spaces`, `GET /spaces/:id`, `POST /spaces/join`, `acl/accept`,
  `POST /spaces`, `DELETE /spaces/:id`): operation bodies identical
  except `DELETE /spaces/:id` gaining a documented 409 — description /
  component churn, no wire change.
- **push/token** (POST/DELETE): excluded as *not exposed yet* — client
  push plumbing, no agent use case so far.
- **Uncovered, untriaged**: 30 new routes (bundles, datasets, devices,
  processes, events, derived spaces, attach/detach, upsert, object GET)
  — tracked in BOB-66.

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
