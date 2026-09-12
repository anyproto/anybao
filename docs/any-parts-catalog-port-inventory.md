# Companion to any-parts-catalog-port-plan.md — full call-site inventory of every any-API dependency in anybao at main 43a659c (generated 2026-09-08, read-only survey; line numbers drift with edits).

# anybao → `any` server call-site inventory

Repo: `/home/zarkone/any/anybao`. Pin: any commit `a176029`, vendored
2026-09-04. `anyrt drift` is **clean** against that pin as of this pass
(verified by running `./runtime/target/release/anyrt drift`).

Every path below is absolute-relative to `/home/zarkone/any/anybao/`.

---

## Architecture of the any client in anybao

There are **two independent any clients**, sharing no code and drifting
separately. A wire change hits both, and it also invalidates recorded
traces, because every any call is an `http.*` effect record replayed
byte-for-byte (`runtime/src/replay.rs`).

### 1. Host client (Rust)

`runtime/src/anyapi.rs` (1528 lines), `struct Client` over a `Transport`
trait (`send` / `open_stream` / `send_raw` / `read_raw`). Real transport
is reqwest; tests inject `StubTransport` and `FakeSpace` from
`runtime/src/testutil.rs`. Wraps ~45 routes as named methods.

Consumers:

| file | what it uses any for |
|---|---|
| `runtime/src/serve.rs` (4023 ln) | space/chat/store bootstrap, chat SSE watch, event-trigger SSE, config + secrets stores, presence events, overlay join |
| `runtime/src/deploy.rs` (1314 ln) | `program` + `agent_skill` objects, markdown bodies, source/manifest datasets |
| `runtime/src/resolver.rs` (597 ln) | module resolution — `query_objects` on `program`, `query` on `program_source` |
| `runtime/src/program_schema.rs` (248 ln) | the `program` type/props/datasets ensure |
| `runtime/src/election.rs` (473 ln) | `/v1/devices` (ADR-015) |
| `runtime/src/tracestore.rs` (1371 ln) | `/v1/local/*` collections (ADR-023) |
| `runtime/src/oauth.rs` (1543 ln) | reads/writes `agent_secrets` rows through serve's store handle |
| `runtime/src/triggers.rs` (1598 ln) | chat-record shape parsing (`agent`, `control`, `context`, `attachments`) |

Full method list, `runtime/src/anyapi.rs`:

| line | method | route |
|---|---|---|
| 376 | `list_spaces` | `GET /v1/spaces[?status=]` |
| 387 | `get_space` | `GET /v1/spaces/{s}` |
| 399 | `create_space` | `POST /v1/spaces {"name"}` |
| 405 | `delete_space` | `DELETE /v1/spaces/{id}` |
| 414 | `list_derived_spaces` | `GET /v1/spaces/derived` |
| 426 | `create_derived_space` | `POST /v1/spaces/derived/{name}` |
| 446 | `ensure_bundle` | `POST /v1/spaces/{s}/bundles` |
| 471 | `list_bundles` | `GET /v1/spaces/{s}/bundles` |
| 481 | `bundle_child` | `POST /v1/spaces/{s}/bundles/{id}/children` |
| 504 | `create_invite` | `POST /v1/spaces/{s}/invites` |
| 511 | `join_space` | `POST /v1/spaces/join` |
| 528 | `join_requests` | `GET /v1/spaces/{s}/members/requests` |
| 541 | `acl_accept` | `POST /v1/spaces/{s}/acl/accept` |
| 557 | `members` | `GET /v1/spaces/{s}/members` |
| 568 | `attach_file` | `POST /v1/spaces/{s}/objects/{o}/files?name=` |
| 586 | `file_info` | `GET /v1/spaces/{s}/files/{f}` |
| 595 | `download_file` | `GET /v1/spaces/{s}/files/{f}/content` |
| 604 | `publish_event` | `POST /v1/events` |
| 612 | `list_devices` | `GET /v1/devices` |
| 618 | `upsert_self_device` | `PUT /v1/devices/me` |
| 624 | `activate_device` | `POST /v1/devices/activate` |
| 629 | `create_object` | `POST /v1/spaces/{s}/objects` |
| 639 | `query_objects` | `POST /v1/spaces/{s}/objects/query` |
| 649 | `query` | `POST /v1/spaces/{s}/query` |
| 664 | `modify` | `POST /v1/spaces/{s}/modify` |
| 670 | `upsert_record` | (via `modify`) |
| 688 | `get_markdown` | `GET /v1/spaces/{s}/objects/{o}/editor/markdown` |
| 697 | `put_markdown` | `PUT` same |
| 711 | `list_types` | `GET /v1/spaces/{s}/types` |
| 719 | `list_properties` | `GET /v1/spaces/{s}/types/{t}/properties` |
| 744 | `local_ensure` | `PUT /v1/local/collections` |
| 751 | `local_collections` | `GET /v1/local/collections?scope=&spaceId=` |
| 773 | `local_drop` | `DELETE /v1/local/collections` |
| 785 | `local_insert` | `POST /v1/local/insert` |
| 795 | `local_upsert` | `POST /v1/local/upsert` |
| 805 | `local_update_flag` | `POST /v1/local/update` |
| 814 | `local_get` | `POST /v1/local/get` |
| 826 | `local_query` | `POST /v1/local/query` |
| 832 | `local_aggregate` | `POST /v1/local/aggregate` |
| 847 | `local_delete` | `POST /v1/local/delete` |
| 864 | `create_type` | `POST /v1/spaces/{s}/types` |
| 870 | `list_datasets` | `GET /v1/spaces/{s}/types/{t}/datasets` |
| 885 | `create_dataset` | `POST /v1/spaces/{s}/types/{t}/datasets` |
| 898 | `add_property` | `POST /v1/spaces/{s}/types/{t}/properties` |
| 913 | `get_properties` | `GET /v1/spaces/{s}/properties/{o}` |
| 921 | `set_properties` | `POST /v1/spaces/{s}/properties/{o}/set/{t}` |
| 936 | `chat_send` | `POST /v1/spaces/{s}/objects/{o}/chat/messages` |
| 951 | `search` | `POST /v1/spaces/{s}/search` |
| 963 | `backlinks` | `GET /v1/spaces/{s}/objects/{o}/backlinks` |
| 976 | `subscribe` | generic SSE over a `/query/subscribe`-shaped route |
| 988 | `subscribe_dataset` | `POST /v1/spaces/{s}/query/subscribe` |

Supporting pieces: `sanitize_nuls` at `:36` (NUL write guard applied to
every body), `parse_sse` at `:71` (frame order `ready → snapshot →
changes* → closed`), url-encoding helper at `:348`.

### 2. Guest client (Python)

`repos/_agent/programs/any@v1/program.py` (3021 lines), `class _Client`
plus a flat module surface of ~60 functions (ADR-010 §8). It never calls
a host `any.*` effect.

- `program.py:423-437` — `_call(verb, path, body)` does
  `effect("http." + verb, {"url": base + path, "json": _sanitize_nuls(body)})`,
  raises `AnyError(status, code, message)` from the
  `{"error": {code, message}}` envelope on ≥400.
- `program.py:2655` — base URL from
  `effect("runtime.get", {"key": "any.base_url"})["value"]`.
- `program.py:2641-2678` — the flat-surface preamble: one private
  `_Client` instance, `_sid`/`_space` spaceConfig shape guards.
- `program.py:3010-3021` — the exported name list.
- Per-run caches on `_Client.__init__` (`program.py:407-421`):
  `_types_cache`, `_props_cache`, `_dataset_time_keys`, `_spaces_cache`,
  `_bundle_children`, `_ensured_stores`, `_stub_cache`.
- Every public function is wrapped in `@span(kind="getter"|"mutator")`.

Other guest programs reach it with `use("agent:any@v1")` — call sites:
`progress@v1.py:23`, `extraction@v1.py:90`, `decay@v1.py:30`,
`ui@v1/program.py:13`, `rollup@v1.py:98`, `deepResearch@v1/program.py:185`,
`miniapp@v1/program.py:50`, `reflection@v1.py:56`, `linkgen@v1.py:82`,
`enrich@v1/program.py:294,357,447`, `remind@v1.py:16`,
`programs@v1/program.py:33`, `llm@v1/program.py:923`,
`evolution@v1.py:66`, `recall@v1/program.py:16`, `toolcaller@v1.py:684`,
`config@v1/program.py:34`, `autorecall@v1.py:105`,
`_connectors/programs/gmailSync@v1/program.py` (module-level `_any`).

### 3. The boundary

- `runtime/src/routes.rs:39-70` — capability truth from (method, url).
  `GET`/`HEAD` → read. `POST` to the any netloc ending in one of
  `ANY_READ_POST_SUFFIXES` (`routes.rs:7`: `/query`, `/objects/query`,
  `/search`, `/aggregate`) → `data.read`. Every other any write →
  `data.write`. Non-any → `net.http`. LLM path suffixes → `llm.chat`.
  **Suffix matching**: a new read-POST route not ending in one of those
  four is silently classified `data.write`.
- `runtime/src/broker.rs:1352-1400` — `secrets_read_guard`: any body
  whose `dataset` field is exactly `agent_secrets` is refused
  (`forbidden`), as is any url/body naming the guarded secrets object
  id; and `trace_*` local collections are read-only to the guest
  (`insert|upsert|update|delete|indexes|collections` refused, plus
  `$out`/`$merge` sinks in aggregate pipelines).
- `runtime/src/broker.rs:865`, `:934`, `:1552-1560` — `bao.status` is
  the only any-adjacent named syscall; it writes a shared presence slot
  that serve publishes (it does not call any itself).

---

## 1. `nav`

anybao's `nav` dependency is thin — a passthrough namespace, not
something anybao builds. **There is no `create_page`, `create_folder`,
`nav.pos` lexid allocation, or page-listing-by-parent anywhere.**

| path:line | shape | component |
|---|---|---|
| `repos/_agent/programs/any@v1/program.py:267-272` | `_RESERVED_GROUPS = {"any", "nav", "_ver"}` — never reverse-mapped on read nor xKey-resolved on write | guest any@v1 |
| `.../any@v1/program.py:496` | catalog dedup, "nav is listed twice" in `GET /types` | guest any@v1 |
| `.../any@v1/program.py:587` | `_resolve_prop_groups` doc: any/nav pass through with literal prop keys | guest any@v1 |
| `.../any@v1/program.py:885` | `_resolve_path`: `nav.*` and bare `id` pass through unchanged | guest any@v1 |
| `.../any@v1/program.py:1019` | `_normalize_record`: any/nav/program groups pass verbatim | guest any@v1 |
| `.../any@v1/program.py:1149` | `create_object` doc: reserved groups (any/nav) pass through literal | guest any@v1 |
| `.../any@v1/program.py:1163-1170` | `create_object` accepts exactly top-level `{types, initialProperties, nav, name, description, markdown, body}`; anything else raises | guest any@v1 |
| `.../any@v1/program.py:1276` | `query_objects` doc: builtin `any.*`/`nav.*`/`program.*` filter paths resolve against the catalog | guest any@v1 |
| `.../any@v1/program.py:2381-2383` | `_enrich_hits` primary type = first type that is not `nav` or `editor` | guest any@v1 |
| `repos/_agent/programs/recall@v1/program.py:20-22` | `RESERVED_GROUPS = {"any", "nav"}` — graph neighbors skip them | guest program |
| **`repos/_agent/programs/enrich@v1/program.py:172-175`** | **the only real nav read**: `c.query(space, transcript_id, "editor_blocks", sort=["nav.pos"], limit=BLOCK_LIMIT)` — document order | guest program |
| `repos/_agent/skills/_any.md:31` | "Builtins use their id (`chat`, `editor`, `nav`, `any`)" | skill md |
| `repos/_agent/skills/_any.md:95` | reads come back xKey-nested, "builtin `any`/`nav` groups verbatim" | skill md |
| `repos/_agent/skills/_any.md:264` | "the real derived chat carries no name/nav" | skill md |
| `repos/_agent/skills/_space_context.md:63` | "Main is a nav map, not a catalogue" | skill md |
| `docs/adr/006-data-contracts.md:450-458` | contract: builtins report `xKey == id`; reserved `any`/`nav`/`_ver` pass through | ADR |
| `docs/adr/016-email-runtime-dataset.md:21` | "row + nav presence per email" (why the corpus moved off objects) | ADR |
| `docs/adr/022-property-formats.md:42,47` | `meta.pos` lexid ordering (property order, **not** nav) | ADR |
| `docs/00-plan.md:740` | `nav.parentId` named as the hierarchy edge | doc |
| `docs/helper-style.md:65` | builtin string ids `"chat"`, `"nav"`, `"editor"` | doc |
| `docs/block-editor-query-api-notes.md:16,153,184,202,214-215,227,235,268-269,283,313,383` | design notes mapping a flat block model onto `nav.parentId` / `nav.pos`, a `(nav.parentId, nav.pos)` index, `$graphLookup` over `parentId`. **Doc only — nothing implemented** | doc |
| `repos/_connectors/testing-flow.md:17,20` | "nav folder `connectors > github`" — human instructions | doc |
| `docs/oauth-flow-notes.md:49` | "connectors->google nav folder" — human note | doc |
| `tests/test_any_module.py:264-266` | fake catalog: user type `task` (CID id) + builtin `nav` (id == xKey) | test |
| `tests/test_any_module.py:276-279` | fake `GET /types` returns `{"id": "nav", "name": "Nav", "xKey": "nav"}`; `/types/nav/properties` returns `parentId`, `pos`, `type` | test |
| `tests/test_any_module.py:288,292-294` | `nav: {"parentId": "f1"}` passes through create + read verbatim | test |
| `tests/test_any_module.py:399` | "the wire accepts only types/initialProperties/nav and silently drops the rest" | test |
| `tests/test_any_module.py:630,634` | aggregate group by types incl. `nav` | test |
| `tests/test_any_module.py:726-727,748` | rows carrying `nav` in `any.types` | test |
| `tests/test_any_module.py:1008-1009` | lowercase `nav.parentid` must raise `"parentid" on builtin group "nav"` | test |
| `tests/test_recall_module.py:27,32,266` | fixture object with `"nav": {"parentId": "root"}`; reserved any/nav skipped by neighbors | test |
| `tests/test_any_properties.py:37` | fake catalog row `{"id": "nav", "name": "Nav", "xKey": "nav"}` | test |

`_lexid_after` (`any@v1/program.py:315-324`) exists but is used **only**
for select-option `pos` (`:749`) and property-definition `meta.pos`
(`:1950`, `:1961`, `:2085`) — never `nav.pos`.

## 2. Chat

The general chat is resolved **through the bundles registry**, never by
name or query. This is the most load-bearing chat contract in the repo.

### Host

| path:line | shape | note |
|---|---|---|
| `runtime/src/serve.rs:86-134` | `general_chat(c, space)` — locked read of `GET /v1/spaces/{s}/bundles`, find row `id == "general-chat/v1"` | requires `bundle["derived"] == true` or serve **hard-stops**: `"derived general chat not found in space {space}: general-chat/v1 is bound to non-derived chat object {root}"` |
| `runtime/src/serve.rs:118-124` | ensure only on a definitive miss: row absent **and** `reg["synced"] == true` → `ensure_bundle(space, "general-chat/v1", "General", ["chat"], derived=true)` | 5 attempts, 2 s apart, then `"bundles registry of space {space} never converged (synced: false)"` |
| `runtime/src/serve.rs:216-238` | `provision_agent_stores` → `ensure_bundle(space, "bao/v1", "bao", ["page"], derived=false)` | created root on purpose (a derived root is undeletable) |
| `runtime/src/serve.rs:154-213` | `bundle_child_retry(c, space, bundle, seed, types)` — 409 retry policy | |
| `runtime/src/serve.rs:313-320` | `watch_chat`: `subscribe_dataset(&ctx.space, &ctx.chat, "chat_messages", {"sort": ["-createdAt"], "limit": 64})` | the chat loop |
| `runtime/src/serve.rs:2314-2317` | same call, expanded | |
| `runtime/src/serve.rs:2325-2360` | frame dispatch `ready` / `closed` / `snapshot` (backlog replay) / `changes` | |
| `runtime/src/serve.rs:2721-2727` | `event_source_thread`: `subscribe_dataset(space, object_id, CHAT_MESSAGES, {"sort": ["-createdAt"], "limit": 64})` | ADR-018 event triggers |
| `runtime/src/serve.rs:2014-2020` | `chat_send(space, chat, {"text": …, "agent": {"name": ctx.cfg.agent_name, "done": true}})` | the reply |
| `runtime/src/serve.rs:2237-2242` | error bubble, same shape | |
| `runtime/src/serve.rs:2248-2254` | `{"agent": {"name", "done": true, "outcome": "interrupted", "debugLink": trace_ref}}` | ADR-005 §3 |
| `runtime/src/serve.rs:2281-2283` | comment: outcome rides the `agent` group, not the text | |
| `runtime/src/serve.rs:32` | `CHAT_PROGRAM = "agent:toolcaller@v1"` — the conversation class for trace retention | |
| `runtime/src/triggers.rs:307-311` | `CHAT_MESSAGES = "chat_messages"` — ADR-018 §2, v1 delivers only this | |
| `runtime/src/triggers.rs:331` | comment: objectId alone is ambiguous across spaces | |
| `runtime/src/triggers.rs:394` | well-formed `chat_messages` event triggers → trigger ids | |
| `runtime/src/triggers.rs:475-479` | event carries the record's `agent` and `attachments` when non-null | |
| `runtime/src/triggers.rs:605,614` | `json!({"space": space, "chatId": chat_id})` | |
| `runtime/src/triggers.rs:664` | `ChatInput.context` | |
| `runtime/src/triggers.rs:724-748` | `control_break(record)` — `record.control.kind == "break"`, returns `control.hard`; `is_control(record)` — any control record is a signal, never content | `chat_messages-v5` |
| `runtime/src/triggers.rs:750-765` | `break_live_run(live, hard)` — soft queues a mailbox `{"kind": "break", "hard": false}` item | |
| `runtime/src/triggers.rs:776-790` | `agent.name` distinguishes own bubbles / trigger nudges | |
| `runtime/src/triggers.rs:787-825` | `view_context(record)` — the `context` group trimmed to `{spaceId, objectId?, view?}`; no `spaceId` = no context | ADR-005 §5 |
| `runtime/src/triggers.rs:828-860` | `attachments` map `{id: {type, link}}` folded into the turn as harness-authored lines | |
| `runtime/src/triggers.rs:891-912` | `on_message` — control records break the live run, never inject | |
| `runtime/src/anyapi.rs:385-393` | `get_space` doc: no chat id on the row; general chat is the bundle root | |
| `runtime/src/anyapi.rs:430-500` | the bundles family; body `{id, name, rootTypes, rootProperties, derived}`; reply `{bundle: {id, rootId, roots, losers, derived}, installed}`; path segment percent-encodes the slash (`bao%2Fv1`) | SYN-163 / SYN-172 / any #177 |
| `runtime/src/anyapi.rs:936-949` | `chat_send` | |
| `runtime/src/anyapi.rs:1192,1202,1515,1525` | fake-transport assertions on `chat_messages` | test |

### Guest

| path:line | shape |
|---|---|
| `any@v1/program.py:254-258` | comment: no chat id on a space row; general chat is the `general-chat/v1` bundle root; agent stores are `bao/v1` children |
| `any@v1/program.py:1576` | `get_space` doc: "the space's chat is `general_chat(space)`" |
| `any@v1/program.py:1582-1596` | `general_chat(space)` = `get_bundle(space, "general-chat/v1")["rootId"]`; 404 `bundle.not_found` = nobody ensured it |
| `any@v1/program.py:1598-1624` | `create_space` → `POST /v1/spaces {"name", "description"?}` then `ensure_bundle(id, "general-chat/v1", "General", ["chat"], derived=True)`; returns row + `generalChatId` |
| `any@v1/program.py:2136-2140` | `_bundle_path` — `bundle_id.replace('/', '%2F')` |
| `any@v1/program.py:2141-2168` | `ensure_bundle` |
| `any@v1/program.py:2170-2176` | `list_bundles` → `r["bundles"]` |
| `any@v1/program.py:2177-2185` | `get_bundle` — unwraps the locked-read `{bundle, synced}` envelope |
| `any@v1/program.py:2186-2202` | `bundle_child(space, bundle_id, seed, types)` → `{objectId}`, cached per run |
| `any@v1/program.py:2204-2212` | `resolve_loser(space, bundle_id, loser_root_id)` — body `{loserRootId}`; 409 `bundle.loser_not_ready` / `bundle.not_loser` |
| `any@v1/program.py:2214-2240` | `chat_log(space, chat_id)` — finds the bundle row whose `rootId == chat_id`, ensures the `agent_log` store, then `bundle_child(space, row["id"], "bao/log/v1", [tid])`; raises 404 `bundle.not_found` if the chat is not a bundle root (ADR-017 §0a) |
| `any@v1/program.py:2242-2251` | `_next_seq` — `query(space, host, dataset, includeDeleted=True, sort=["-id"], limit=1)`; tombstones burn ids forever |
| `any@v1/program.py:2253-2291` | `append_turn` / `create_chunk` / `_append_log` — record id is `f"{seq:08d}"`, rejections raise 409 `log.seq_collision` |
| `any@v1/program.py:2293-2305` | `chat_send` — body verbatim; accepted set exactly `{text, replyToMessageId?, agent?, attachments?}`; server 400s `request.unknown_field` otherwise |
| `repos/_agent/programs/toolcaller@v1.py:23-42` | `_ANY_LINK = re.compile(r"\(\s*(any://[^\s)]+)\s*\)")` → auto-attachments `{"a0": {"type": "link", "link": uri}}`; `s/` space links stay text-only |
| `repos/_agent/programs/toolcaller@v1.py:780-783` | attaches them, then `c.chat_send` |
| `repos/_agent/programs/toolcaller@v1.py:864` | `c.append_turn` |
| `repos/_agent/programs/progress@v1.py:113-114` | `general_chat` + `chat_send` (visible terminal bubble, ADR-014 §122) |
| `repos/_agent/programs/remind@v1.py:18` | `c.chat_send` |
| `repos/_agent/programs/deepResearch@v1/program.py:191` | `c.chat_send` |
| `repos/_agent/programs/rollup@v1.py:51,101` | `c.create_chunk`, `c.chat_log` |
| `repos/_agent/programs/extraction@v1.py:92` | `c.chat_log` |
| `repos/_agent/programs/history@v1/program.py:143-165` | `client.chat_log(space, chat_id)["objectId"]` then `client.query(..., "agent_turns"/"agent_chunks", sort=["-seq"])` |
| `repos/_connectors/programs/gmailSync@v1/program.py:686` | `_any.general_chat(agent_space)` |
| `repos/_connectors/programs/gmailSync@v1/program.py:655` | `_any.bundle_child(agent_space, "bao/v1", …)` |

### Skills / docs / tests

- `repos/_agent/skills/_any.md:239-282` — the model-facing chat contract:
  one derived chat per space, `chat_send` with the `agent` marker, read
  with `query(space, chat_id, "chat_messages", sort=["-createdAt"])`,
  the `agent {name, done, outcome?, debugLink?}` / `control {kind, hard?}`
  record shape, "NEVER create a chat object or pick one from a query",
  and the local-scope filter trap on the `chat` type.
- `repos/_agent/skills/_core.md:91-93`, `:163-176`, `:196`, `:228` —
  `bao/runs/v1` and `bao/triggers/v1` bundle children, trigger `spec
  {"dataset": "chat_messages", "objectId": <chat id>, "spaceId": …}`,
  `agent_turns` seq-range query.
- `repos/_agent/skills/_gmailSync.md:36` — "never chat_send it yourself".
- `docs/adr/006-data-contracts.md:18-103` — §0, the canonical contract,
  two amendments (2026-07-16 derived general chat; 2026-08-20 the bundle).
- `docs/adr/017-agent-userspace-datasets.md:40-49` — chat logs as bundle
  children; v1 wires only the general chat's log.
- `docs/adr/018-event-triggers.md:105` — trigger spec shape.
- `docs/adr/015-active-instance-election.md:22` — the split-brain incident.
- `docs/adr/014-program-progress.md:122` — terminal visible bubble.
- `docs/adr/021-credential-entry.md:246`, `docs/credentials-chat-dialog.md:19,209`
  — credential cards are ordinary general-chat messages.
- `docs/bob-78-questionary-report.md:123-126` — `generalChatObjectId` is
  not on the wire; `generalChatId` is anybao's own key (ADR-006 §0).
- `repos/_connectors/testing-flow.md:47` — `$CHAT = general-chat/v1 bundle root`.
- `docs/testing-from-space.md:44` — serve passes `space` + `chatId`.
- Tests: `tests/test_any_module.py:345-362` (two `GET /v1/spaces/s1/bundles/general-chat%2Fv1`),
  `:536-579` (turns/chunks/chat paths), `:839-855`
  (`create_space` installs the derived general chat, asserts
  `POST /v1/spaces` then `POST /v1/spaces/sp9/bundles`),
  `tests/test_progress_module.py:88,217`,
  `repos/_connectors/tests/test_gmailsync_program.py:193`,
  `runtime/src/anyapi.rs:1461-1478` (`ensure_bundle_posts_id_verbatim_in_body`),
  `runtime/src/anyapi.rs:1515-1526` (subscribe body),
  `runtime/src/triggers.rs:1181-1476` (event-source, break, view-context,
  attribution tests).

## 3. Types and datasets

### Types anybao declares (all by xKey, all idempotent ensures)

| xKey | declared at | component |
|---|---|---|
| `program` | `runtime/src/program_schema.rs:23-35` (`PROGRAM_TYPE_XKEY`, `PROGRAM_TYPE_NAME`, `PROPS` = name/version/any_tool/summary, all `meta.index: "none"`) | host |
| `agent_skill` | `runtime/src/deploy.rs:662` (`SKILL_TYPE`), ensure at `:679-741` (type + a `name` string property) | host |
| `agent_config` | `runtime/src/serve.rs:240` `ensure_type(c, space, "Agent Config", "agent_config")` | host |
| `agent_secrets` | `runtime/src/serve.rs:241` | host |
| `agent_trigger` | `runtime/src/serve.rs:242` | host |
| `agent_brain` | `repos/_agent/programs/any@v1/program.py:2446-2447` (via `_ensure_store`) | guest |
| `agent_log` | `repos/_agent/programs/any@v1/program.py:2234-2236` | guest |
| `mini_app` | `repos/_agent/programs/miniapp@v1/program.py:27-28` (`_TYPE_DECL = {"name": "Mini App", "xKey": "mini_app"}`) | guest |
| `mailbox` | `repos/_connectors/programs/gmailSync@v1/program.py:42-45` | connector |
| `sync_state` | `repos/_connectors/programs/gmailSync@v1/program.py:83-96` | connector |
| `enrichments` | `repos/_agent/programs/enrich@v1/program.py:254-258` | guest |
| `enrich_proposal` | `repos/_agent/programs/enrich@v1/program.py:260-264` | guest |
| `Page` (unkeyed) | `repos/_agent/programs/deepResearch@v1/program.py:152-156` — `create_type(space, {"name": "Page"})["xKey"]` | guest |

Also referenced but never declared here: `program` + `mini_app` are
called out at `any@v1/program.py:268-270` as harness-declared USER types
resolved by xKey like any other (ADR-010 §5, ADR-008 §6).

`_SYNTHETIC_TYPES = {"any", "spaceIndex", "type"}` — `any@v1/program.py:274-276`:
listed by `GET /types` in every space but not attachable; naming a new
type after one errors (`program.py:1727-1734`).

**The pre-metatype xKey re-claim** (any PR #176) appears in four places,
all with the same shape — a type listed with an empty `xKey` and a
matching name gets `POST /v1/spaces/{s}/properties/{typeId}/set/type
{"patch": {"xkey": …}}` rather than being shadowed by a duplicate:
`runtime/src/serve.rs:157-167`, `runtime/src/deploy.rs:698-712`,
`runtime/src/program_schema.rs:172-182`, `any@v1/program.py:1737-1748`.

### Datasets declared

Draft shape: `{name, displayName, idRule: "auto"|"user", idPattern?,
deleteBy: "anyone"|"author", skipHistory?, dynamic?, search: {title,
text, scope}, fields: [{key, kind, required?, mutableBy: "author"|"any",
stamp: "creator"|"createTime"|"modifyTime"}]}`.

| dataset | declared at | notes |
|---|---|---|
| `program_source`, `program_manifest` | `runtime/src/program_schema.rs:26-48` | `idRule: user`, `dynamic: true`, **no** `search` mapping (source is code, never indexed); single record `"main"` (`MAIN_RECORD`, `:28`) |
| `agent_config` | `runtime/src/serve.rs:250-262` | `idRule: user`, `dynamic: true`, one declared field `key`; `{key, value}` synced only (ADR-006 §3) |
| `agent_secrets` | `runtime/src/serve.rs:263-303` | the only non-dynamic host store; 16 declared fields incl. `value` (`SECRETS_FIELD`, `serve.rs:372`), `status`, `meta` (object), `requestedAt`/`rejectedAt` datetimes; `value` is ACCOUNT-scoped/synced (ADR-021 §4) |
| `agent_triggers` | `runtime/src/serve.rs:304-311` | `dynamic: true`, `fields: []` — the whole record shape stays undeclared |
| `agent_runs` | `runtime/src/serve.rs:312-319` | same, ADR-023 §1 |
| `agent_memory_items` | `any@v1/program.py:89-117` (`_MEM_DATASET`) | `idRule: auto`, `deleteBy: author`, `search {title: "context", text: "body", scope: "agent"}`; `_MEM_MUTABLE`/`_MEM_CREATE_ONLY` at `:86-89` |
| `agent_job_state` | `any@v1/program.py:118-124` | `dynamic: true`, `skipHistory: true` |
| `agent_roi_injections` | `any@v1/program.py:125-131` | autorecall ROI log |
| `agent_turns` | `any@v1/program.py:132-153` | `idRule: user`, `search {title: "userText", text: "searchText", scope: "history"}`; `llm` object field |
| `agent_chunks` | `any@v1/program.py:154-170` | `search {text: "summary", scope: "history"}`; `periodStart`/`periodEnd` datetimes |
| `email_messages` | `repos/_connectors/programs/gmailSync@v1/program.py:46-81` (`EMAIL_DATASET`) | `idRule: user` (record id = Gmail message id), `deleteBy: author`, `skipHistory: true`, **multi-field** `search {title: "subject", text: ["from","body","notes"], scope: "email"}` (SYN-179); `summary`/`notes` are `mutableBy: author` annotation fields sync never writes |
| `mini_app` | `repos/_agent/programs/miniapp@v1/program.py:29-33` | `dynamic: true`, no search (HTML is code); fields `source`/`state`/`readme`; single record `"main"` |
| `enriched_data` | `repos/_agent/programs/enrich@v1/program.py:53-70` | `idRule: auto`, stamps `createdBy`/`createdAt` |
| `enrich_proposal_items` | `repos/_agent/programs/enrich@v1/program.py:74-80` | `idRule: auto` |
| `program_source`/`program_manifest` (guest twin) | `repos/_agent/programs/programs@v1/program.py:205-226` | "runtime/src/program_schema.rs is normative — keep line-for-line parity" |

### Route call sites

- `POST /v1/spaces/{s}/types` — `anyapi.rs:864-866`; `any@v1/program.py:1752`;
  `serve.rs:169`; `deploy.rs:714`; `program_schema.rs:81-85`.
- `GET /v1/spaces/{s}/types` — `anyapi.rs:711-717`; `any@v1/program.py:1651`;
  `serve.rs:152`; `deploy.rs:686`; `program_schema.rs:164`.
- `GET /v1/spaces/{s}/types/{t}/properties` — `anyapi.rs:719-728`;
  `any@v1/program.py:1694`; `deploy.rs:722`; `program_schema.rs:189`.
- `POST /v1/spaces/{s}/types/{t}/properties` — `anyapi.rs:898-911`;
  `any@v1/program.py:1963-1964`; `deploy.rs:731-735`; `program_schema.rs:98-102`.
- `GET /v1/spaces/{s}/types/{t}/datasets` — `anyapi.rs:870-879`;
  `any@v1/program.py:1780`, `:2351`; `serve.rs:175`; `program_schema.rs:109`.
- `POST /v1/spaces/{s}/types/{t}/datasets` — `anyapi.rs:885-896`;
  `any@v1/program.py:1851`; `serve.rs:182`; `program_schema.rs:117`.
- `PATCH /v1/spaces/{s}/types/{t}/datasets/{d}` — `any@v1/program.py:1847`
  (in-place reconcile of an existing def).
- `DELETE /v1/spaces/{s}/types/{t}/datasets/{d}` — `any@v1/program.py:1863`.
- `POST`/`DELETE` `.../datasets/{d}/fields[/{f}]` — `any@v1/program.py:1880`, `:1895`.
- `GET /v1/spaces/{s}/datasets` — `any@v1/program.py:2346` (only in
  `list_search_scopes`; marked `excluded` in the manifest as harness
  schema discovery, so this guest call is an un-manifested consumer).
- `GET /v1/spaces/{s}/properties/{o}` — `anyapi.rs:913-919`; used by
  `serve.rs:337-341` (`ensure_child_name` read-first).
- Composite guest `create_type` — `any@v1/program.py:1697-1769`:
  probes `list_types`, refuses builtin handles, re-claims a legacy type,
  creates only missing properties, returns `{typeId, xKey, created, addedProps}`.
- Guest store ensure — `any@v1/program.py:2425-2436` (`_ensure_store`),
  cached in `_ensured_stores`.

### Undocumented field anybao depends on

`_addSeq` on dataset records, used as the module probe-cache key:
`runtime/src/resolver.rs:7` (doc), `:185` (`main["_addSeq"].as_i64()`),
`:528` (test); faked at `runtime/src/testutil.rs:115`, `:127`, `:254`.
It is **not** in `api/openapi.vendored.json`, so `anyrt drift` can never
flag a change to it.

## 4. Property formats (ADR-022)

All in `repos/_agent/programs/any@v1/program.py` unless noted.

| line | constant / function | contract |
|---|---|---|
| 279-288 | `_MARKER_XKEYS` | any-ui stamps `xKey: "select"` on every Select, `tags` on every Multiselect, none for Text/Number/Check — so an xKey is a handle only when unique on its type and not a marker. Explicitly a **bridge**: "drop once any-ui carries the marker in `xKind`" |
| 289 | `_FORMATS = ("select","multiselect","links","date","datetime")` | the server formats |
| 290-295 | `_XKIND_MARKERS = ("url","email","longtext")` | client conventions with NO server format — sending a format for them would 400 |
| 296-300 | `_XKIND_OF_FORMAT` | the marker any-ui stamps beside each server format (`datetime` shares `date`) |
| 301-302 | `_KINDS` | `string number boolean null array object datetime` — nothing else; `"text"`/`"date"` as kinds are 400s |
| 303-306 | `_OPTION_COLORS` | any-ui's ten-swatch palette (`optionPalette.ts`) |
| 307-309 | `_PINNED_PATHS` | `id key kind scope items properties format format.type` — a PATCH is 400 `property.immutable`, refused client-side |
| 310 | `_ARCHIVED_META = "anyUiArchived"` | any-ui's removal marker on `meta.<k>` |
| 311 | `_LEXID_ALPHABET` | 62-char byte-compare-ordered alphabet |
| 315-324 | `_lexid_after(pos)` | `""` → `"a0"`, else bump the last char or append `"1"` |
| 326-347 | `_assign_handles(props)` | stamps `handle` on every property row; a true duplicate is suffixed with `~<id>` |
| 350-372 | `_is_archived`, `_options_of`, `_ordered_options`, `_prop_sort_key` | display order |
| 373-405 | `_link_object_id`, `_looks_like_object_id`, `_slugify_option_key`, `_pick_color` | |
| 505-582 | `_type_props`, `_prop_def`, `_resolve_type_seg`, `_resolve_prop_seg`, `_prop_handles`, `_type_handles` | catalog resolution both ways |
| 584-629 | `_resolve_prop_groups` | resolves group + property keys to ids; reserved namespaces pass literal |
| 630-634 | `_write_ctx(create_options)` | |
| 635-687 | `_encode_value` | the value encoder against each property definition |
| 688-713 | `_assert_links_in_space` | a links value must be bare `any://<objectId>` and in-space; a foreign id refused before any write |
| 714-755 | `_option_key` | a missing select option is **created** any-ui style (key/color/lexid `pos` at `:749`) unless `create_options=False`; reported in `createdOptions` |
| 756-779 | `_object_id_by_name` | a links NAME must match exactly one object |
| 780-805 | `_encode_instant` | ADR-019 |
| 806-847 | `_encode_kind` | kind check |
| 848-859 | `_apply_option_patches` | `PATCH /v1/spaces/{s}/types/{t}/properties/{p}` at `:854-855` — options land **before** the value |
| 860-874 | `_apply_unsets` | `POST /v1/spaces/{s}/modify` at `:868` with `$unset` |
| 875-881 | `_write_result` | `{objectId, resolved?, createdOptions?, warnings?}` |
| 926-935 | `_prop_kind` | |
| 1062-1077 | `_display_value` | |
| 1078-1122 | `_hydrate_links` | ADR-022 §3 — batched `{"id": {"$in": [...]}}`, cap 200 (`:1106`), turns link ids into `{id, name, types}` stubs; `_stub_cache` |
| 1123-1137 | `_dexify` | |
| 1653-1696 | `list_properties(space, type_key, include_archived=False)` | returns `[{handle, id, name, xKey, xKind?, kind, scope, format?, options?, meta?}]` in display order |
| 1691-1696 | `_fetch_props` | |
| 1917-1968 | `_post_property` | lowers `format.type ∈ _XKIND_MARKERS` to `kind: "string"` + `xKind` and **drops** `format` from the wire; validates `format.type ∈ _FORMATS` else raises naming both sets ("tags" is reserved server-side); fills option `name`/`color`/`pos`; appends a lexid `meta.pos`; stringifies every meta value |
| 1969-1976 | `_resolve_prop_or_raise` | |
| 1977-2007 | `patch_property(set=, unset=)` | PATCH at `:2003-2004` |
| 2008-2048 | `set_option` | PATCH at `:2042-2043` |
| 2049-2063 | `remove_option` | PATCH at `:2058-2059` |
| 2064-2092 | `reorder_property` | PATCH at `:2087-2088` |
| 2093-2105 | `archive_property(restore=)` | PATCH at `:2101-2102`, writes `meta.anyUiArchived` |
| 2106-2117 | `delete_property` | `DELETE /v1/spaces/{s}/types/{t}/properties/{p}` at `:2113-2114` |
| 2118-2126 | `attach_type` | `POST /v1/spaces/{s}/properties/{o}/attach/{t}` |
| 2127-2135 | `detach_type` | `POST /v1/spaces/{s}/properties/{o}/detach/{t}` |

Elsewhere:
- `runtime/src/program_schema.rs:98-102` — the host writes
  `{"name", "xKey", "kind", "meta": {"index": "none"}}`, so the host
  knows the `meta.index` convention too.
- `docs/adr/022-property-formats.md` — the contract; `:42,47` `meta.pos`
  ordering; `:176` the `$in` hydration batch.
- `repos/_agent/skills/_any.md:33-80` — the model-facing format table.
- Tests: `tests/test_any_properties.py` (whole file, 300+ lines);
  `tests/test_any_module.py:166-242` (creation paths), `:591-614`.

## 5. Query and subscribe

### Host

- `runtime/src/anyapi.rs:639-647` `query_objects` → `POST /v1/spaces/{s}/objects/query`.
- `runtime/src/anyapi.rs:648-662` `query` → `POST /v1/spaces/{s}/query`,
  body `{objectId, dataset, …opts}`; doc "per-object dataset query
  (chat_messages, editor_blocks, …)".
- `runtime/src/anyapi.rs:972-985` `subscribe(path, body)` — generic SSE,
  `parse_sse` frames `ready → snapshot → changes* → closed`, terminal on
  `closed` (docs/04-events.md).
- `runtime/src/anyapi.rs:987-1002` `subscribe_dataset(space, object, dataset, opts)`
  → `POST /v1/spaces/{s}/query/subscribe`.
- **Only two subscribe call sites in the whole repo**:
  `runtime/src/serve.rs:2314` and `runtime/src/serve.rs:2721`, both
  `{"sort": ["-createdAt"], "limit": 64}` on `chat_messages`.
- Query call sites: `serve.rs:384` (config store), `:456`, `:561`,
  `:767` / `:1345` / `:1948` (secrets store), `:2127`, `:2550` / `:2900`
  / `:2965` (`agent_triggers`), `:3130`; `resolver.rs:168`, `:178`
  (`program_source`); `deploy.rs:432`, `:577`, `:769`.

### Guest

- `any@v1/program.py:1267-1309` `query_objects` — `normalize` is
  keyword-only (a positional dict used to land in it and silently drop
  the caller's filter); rejects any option outside
  `{filter, sort, limit, offset}` at `:1288-1294`; POST at `:1302-1303`.
- `any@v1/program.py:1310-1330` `list_programs` — `filter={"any.types": "program"}, limit=200`.
- `any@v1/program.py:1331-1344` `query` — drops `None` opts so callers
  can pass optional filter/sort/limit through unchecked; POST at `:1344`.
- `any@v1/program.py:882-925` `_resolve_path`.
- `any@v1/program.py:936-949` `_resolve_type_value`.
- `any@v1/program.py:950-965` `_resolve_filter`.
- `any@v1/program.py:966-1002` `_encode_filter_value` — `$in`/`$nin`/`$all`
  list handling at `:992`.
- `any@v1/program.py:1003-1015` `_resolve_sort`.
- `any@v1/program.py:1016-1061` `_normalize_record` — rows come back
  `{"id", "any": {…}, "<typeXKey>": {"<propXKey>": value}}`.
- `any@v1/program.py:1456-1479` `aggregate` — `POST /v1/spaces/{s}/aggregate`
  when `object_id`/`dataset` given (`:1471`), else
  `POST /v1/spaces/{s}/objects/aggregate` (`:1474`).
- `any@v1/program.py:1480-1519` `_resolve_pipeline` / `_resolve_field_refs`.
- `any@v1/program.py:2307-2333` `search` — body `{query, scopes?, limit?,
  mode?}`, reply envelope `{hits, mode, vectorStatus}`, each hit
  `{data, dataset, objectId, recordId, scope, score}`; POST at `:2326`.
- `any@v1/program.py:2334-2358` `list_search_scopes` —
  `_FIXED_SCOPES = ("basic","chat","props")` at `:2333`, plus one
  `GET /v1/spaces/{s}/datasets` (`:2346`) and one
  `GET /types/{t}/datasets` per declaring type (`:2351`), reading
  `search.scope`.
- `any@v1/program.py:2360-2401` `_enrich_hits` — one `$in` query at
  `:2369` resolves title/type; prop-dataset hits gain `prop` as
  `"<typeXKey>.<propXKey>"`.

### Instant guard (ADR-019 §4)

- `any@v1/program.py:173-180` — `_STAMP_KEYS = {createdAt, modifiedAt,
  createTime, modifyTime}`, `_TIME_OPS = ($eq $in $nin $gt $gte $lt $lte)`.
- `any@v1/program.py:182-190` `_datetime_keys(decl)`; `:191-196`
  `_DATASET_TIME_KEYS` built from the three declarations this module owns.
- `any@v1/program.py:192-196` `_is_instant` — exactly `{"$date": …}`.
- `any@v1/program.py:198-234` `_check_time_literal` / `_guard_filter` —
  raises client-side because a bare number server-side "silently matches
  all or nothing".
- Guard calls: `:1297-1299` (query_objects), `:1341-1343` (query).

### Operators actually used

`$in`: `any@v1/program.py:698`, `:1106`, `:2369`;
`repos/_agent/programs/recall@v1/program.py:80`;
`repos/_agent/programs/evolution@v1.py:84`;
`repos/_agent/programs/ui@v1/program.py:44`;
`repos/_connectors/programs/gmailSync@v1/program.py:374`.
`$gte`/`$lte`: `recall@v1/program.py:115`, `:125`, `:130`.
`includeDeleted`: `any@v1/program.py:2249` only.
`$nin`: named in `_TIME_OPS` and `_encode_filter_value` only — no live caller.
`$exists`: taught in `_any.md:268` (the local-scope trap) and
`_core.md:89`; used in `docs/adr/026-blobs.md:213` design.

`docs/api-parity.md:64-66` records that the current pin already types
`projection` values as integers, adds `includeDeleted` to the space
query, and adds optional `passages` / `maxData` to search plus per-hit
passages in the reply — additive, not yet exposed by `helper.search`.

## 6. Objects

| operation | route | call sites |
|---|---|---|
| create | `POST /v1/spaces/{s}/objects` | `anyapi.rs:629-637`; `any@v1/program.py:1203`; `deploy.rs:488`, `:589`, `:788`; `resolver.rs:341`, `:512`; `tests/conftest.py:97` |
| query | `POST /v1/spaces/{s}/objects/query` | `anyapi.rs:639`; `any@v1/program.py:697`, `:763`, `:1105`, `:1302`; `tests/conftest.py:100` |
| set props | `POST /v1/spaces/{s}/properties/{o}/set/{t}` | `anyapi.rs:921-934`; `any@v1/program.py:1241-1243`; `serve.rs:164`, `:345`; `deploy.rs:506`, `:703`; `program_schema.rs:179` |
| delete | `DELETE /v1/spaces/{s}/objects/{o}` | `any@v1/program.py:1265` |
| modify | `POST /v1/spaces/{s}/modify` | `anyapi.rs:664`; `any@v1/program.py:868`, `:1351`; `deploy.rs:541` |
| upsert (batch) | `POST /v1/spaces/{s}/upsert` | `any@v1/program.py:1388` |
| delete records | `POST /v1/spaces/{s}/delete-records` | `any@v1/program.py:1396`; `tools/eval/anyeval.py:49` |
| markdown get/put | `GET`/`PUT /v1/spaces/{s}/objects/{o}/editor/markdown` | `anyapi.rs:688-709`; `any@v1/program.py:1522`, `:1528-1529`; `deploy.rs:623`, `:626`, `:781`, `:784`, `:796` |
| markdown patch | `PATCH .../editor/markdown` | `any@v1/program.py:1546` |
| markdown append | `POST .../editor/markdown/append` | `any@v1/program.py:1556` |
| attach file | `POST /v1/spaces/{s}/objects/{o}/files?name=` | `anyapi.rs:568-583`; `any@v1/program.py:2610` |
| file info / bytes | `GET /v1/spaces/{s}/files/{f}[/content]` | `anyapi.rs:586-599`; `any@v1/program.py:2575` |
| list files | `GET /v1/spaces/{s}/files` | `any@v1/program.py:2564` |

Notable body/shape facts:

- `any@v1/program.py:1138-1207` `create_object` — the wire takes **no**
  body/markdown, so `markdown`/`body` is create-then-`put_markdown`
  (`:1204-1206`). Top-level `name`/`description` are routed into the
  `any` group (`:1178-1184`). Synthetic types are refused (`:1186-1194`).
- `any@v1/program.py:1209-1245` `update_object` — resolves and encodes
  everything **before** any write so a bad key can't land a partial
  update; options first, then unsets, then one patch per (type, scope).
- `any@v1/program.py:1246-1257` `_split_by_scope` — the set route takes
  a single scope per call, so patches are split by each property's
  declared `scope` (default `"synced"`).
- `any@v1/program.py:1259-1265` `delete_object` — "Removes the whole
  object… No undo". **No `bin` / restore path exists anywhere in anybao.**
- `any@v1/program.py:1346-1351` `modify` body:
  `{objectId, dataset, records: [{id, upsert?, ops: [{type, path, value}]}]}`.
- `any@v1/program.py:1353-1368` `upsert_record` — whole-value `$set` at
  path `""`, `upsert: True`. Docstring records a live finding: an
  UNDECLARED dataset name 500s on write and reads as `[]`.
- `any@v1/program.py:1370-1388` `upsert_records` — reply
  `{created, updated, skipped, rejections: [{index, id, code, reason}], pages}`
  at HTTP **200 even with rejections**; `pageSize` default 500.
- `any@v1/program.py:1390-1399` `delete_records` — tombstones are
  permanent; re-upserting a deleted id rejects `upsert.record_deleted`.
- `any@v1/program.py:1532-1548` `edit_markdown(space, object_id, edits)`
  → reply `{updated, unchanged}`.
- `runtime/src/serve.rs:335-347` `ensure_child_name` — read `any.name`
  via `get_properties` → `/record/any/name`, write only on mismatch so a
  no-op boot appends no CRDT change.
- `editor_blocks` is **read-only** in anybao — only through `POST /query`:
  `repos/_agent/programs/enrich@v1/program.py:174`,
  `tests/test_enrich_integration.py:76`, `:175`,
  `tests/test_enrich_program.py:42`. **No `/editor/blocks` call site exists.**
- `repos/_agent/skills/_any.md:90-92` — "never get+put round-trip to add
  a section (a concurrent get+put clobbers the body)".
- `repos/_agent/skills/_space_context.md:50` — same rule for Main.
- `repos/_agent/skills/_meta_skill.md:13-25` — skill objects via
  `get_markdown` / `create_object` / `put_markdown`.

## 7. Account and health

anybao deliberately touches almost none of this — `docs/api-parity.md:11-18`
lists health, shutdown, auth, account (+metadata), sync, sync-status,
debug, datasets, files/cache, delete-records and raw `modify` as
`excluded`: harness plumbing, never an agent facade.

| path:line | what |
|---|---|
| `tests/conftest.py:11`, `:145-152` | `GET /v1/health` — the live-server probe that SKIPS the integration suite when nothing answers; default `ANYBAO_TEST_SERVER=http://127.0.0.1:7009` (`conftest.py:29`) |
| `docs/testing-agent-changes.md:39` | `curl -s http://127.0.0.1:7134/v1/health` — rig check |
| `api/coverage.json` | `GET /health`, `GET|POST|DELETE /auth`, `GET /account`, `PUT /account/metadata`, `POST /shutdown`, the four `sync-status` routes, `GET /debug/p2p`, `GET /spaces/{s}/debug[/objects/{o}]`, `GET /datasets`, `GET /spaces/{s}/datasets`, `GET /files/cache` + free/sweep, `POST /spaces/{s}/delete-records`, `POST /spaces/{s}/modify`, `POST /spaces/{s}/sync` — all `excluded` with a reason |
| `docs/api-parity.md:70-71` | the current pin adds `X-Any-Control-Token` to auth + shutdown (SYN-169); explicitly "touches nothing here: anyrt never calls it" |
| `docs/00-plan.md:685` | `/sync-status` reports the space synced (+ config override) — design note, no caller |
| `docs/adr/019-instants.md:10` | upstream `any` #179 (SYN-136), `any-sync-sdk` #107 / v0.2.5 |
| `docs/adr/019-instants.md:229` | any-sync-sdk `/upsert` rejected every `$date` |
| `docs/instants-upstream-tickets.md:8`, `:149-155` | `any-sync-sdk` PR #109 (`fix/upsert-extjson-date`) |
| `docs/datetime-migration-plan.md:3` | pin to `any` PR #179 `d6f02a0` + SDK |
| `docs/adr/016-email-runtime-dataset.md:12` | any-sync-sdk `docs/17-user-datasets.md` is the dataset-schema reference |

**Note**: `any@v1/program.py:2346` calls `GET /v1/spaces/{s}/datasets`
from guest code, but the manifest marks that route `excluded: "schema
discovery (harness)"`. That is an un-manifested guest consumer.

Index version and data-dir wipe procedures are not in this repo; they
live in `docs/prod-repo-account.md` prose and the operator memory notes.

## 8. Links and backlinks

| path:line | shape |
|---|---|
| `runtime/src/anyapi.rs:960-970` | host `backlinks` — `GET /v1/spaces/{s}/objects/{o}/backlinks`, unwraps the `backlinks` envelope, each `{objectId, typeId, propId}` |
| `repos/_agent/programs/any@v1/program.py:2403-2423` | guest `backlinks` — same route (`:2409`), re-maps to `{objectId, type, prop}` as xKeys, raw id only when unresolvable |
| `repos/_agent/skills/_any.md:155-182` | the `any://` grammar contract: typed `any://o\|m\|s\|f/<spaceId>/…`; records as `any://o/<sid>/<oid>/<dataset>/<recordId>`; legacy bare `any://<objectId>` and `any://<spaceId>/<objectId>` still parse; **the strict exception** — links-format property VALUES store exactly `any://<objectId>`, one segment, no space, no fragment |
| `repos/_agent/programs/any@v1/program.py:373-397` | `_link_object_id`, `_looks_like_object_id` |
| `repos/_agent/programs/any@v1/program.py:688-713` | `_assert_links_in_space` — a foreign-space id is refused before any write, error text names `any://o/<spaceId>` as the body alternative |
| `repos/_agent/programs/enrich@v1/program.py:35`, `:277-282` | provenance URIs `any://o/{space}/{transcript}/editor_blocks/{blockId},…` |
| `repos/_agent/programs/toolcaller@v1.py:23-42`, `:780-782` | `[Name](any://…)` in a reply → chat attachment chips; `s/` links stay text-only |
| `repos/_agent/programs/recall@v1/program.py:142`, `:168` | `t.removeprefix("any://")` — strips the prefix off links-property values to get bare target ids |
| `repos/_agent/programs/llm@v1/program.py:8`, `:909`, `:923-926` | `any://f/<spaceId>/<fileId>` file input (ADR-020) |
| `repos/_agent/skills/_core.md:263-264` | `[Object Name](any://o/spaceId/objectId)` is what renders |
| `repos/_agent/skills/_space_context.md:40-41` | Main's id via the `[Main](any://o/…)` link; older docs carry the bare form |
| `repos/_agent/skills/_soul.md:63` | `[Contacts](any://…)` example |
| `docs/00-plan.md:740` | inline `any://` links named as a graph edge source |
| Tests | `tests/test_toolcaller.py:730-744` (all five link kinds → attachments); `tests/test_any_properties.py:183-186`, `:196-200`, `:265`, `:280-296`, `:309`; `tests/test_enrich_program.py:200`, `:209`, `:223-224`, `:324-359`; `tests/test_enrich_integration.py:121`, `:146-156`; `tests/test_recall_module.py:26`, `:33`, `:261`; `tests/test_recall_integration.py:74`, `:85-89`; `tests/test_any_module.py:1249`, `:1259`, `:1285`; `tests/test_llm_module.py:785-787`; `tests/test_skills.py:50` |

No link-index route is called. `backlinks` is the only graph read.

## 9. The api-drift pin

**Pinned spec** — `api/openapi.vendored.json`, 180 553 bytes,
131 paths / 157 operations, `info: {title: "Any API", version: "1.0",
description: "Local HTTP/JSON API wrapping any-sync-sdk. Localhost-only,
no auth in v1."}`. Vendored 2026-09-04 from a staging-clean `:7141`
`GET /v1/openapi.json`, pin → any `a176029` (`docs/api-parity.md:57`).

**Manifest** — `api/coverage.json`:
`{endpoints: {"<METHOD> <path>": {excluded: <reason>|null, fingerprint:
"sha256:<16 hex>", helper: <method>|null}}, note}`.
Counts: **157 total — 58 mapped, 42 excluded, 57 uncovered-and-untriaged-by-design.**

**Detector** — `runtime/src/drift.rs` (338 lines):
- `:1-15` module doc — the client is curated, never autogenerated;
  drift is event-driven.
- `:22` `METHODS = [get, post, put, delete, patch]`.
- `:23` `PROSE = [description, example, examples, title]` — stripped at
  every nesting level.
- `:29-38` `deref`, `:40-65` `resolve` — `$ref`s expanded transitively,
  cycle-guarded (a ref already on the stack stays as its name).
- `:67-85` `norm_params`, `:86-96` `norm_body`, `:97-106` `norm_responses`.
- `:107-118` `endpoint_fingerprint` — hashes `{parameters, requestBody, responses}`.
- `:120-150` `extract_endpoints`, `:152-172` `diff` → `{new, changed, removed}`.
- `:200-206` `run`, `:210-228` `refresh` — refresh rewrites fingerprints
  of endpoints present in BOTH; new/removed stay human-triaged.

**CLI** — `runtime/src/main.rs:118-129` (`Drift { spec, manifest, refresh }`,
defaults `api/openapi.vendored.json` / `api/coverage.json`),
dispatch at `:666-680`, exits 1 on drift.

**Make target** — `Makefile:1`, `Makefile:23-24`:
`api-drift: runtime` → `./runtime/target/release/anyrt drift`.

**Current state** — I ran it:
`api-drift: clean — manifest matches the vendored spec.` exit 0.

**CI gap** — `make api-drift` is **not** in CI. `.github/workflows/ci.yml`
runs `uv sync`, `make kernel`, `ruff check`, `cargo test`, `make runtime`,
`pytest` (job `test`) and `make runtime-check`, `make runtime` (job
`runtime`). What CI *does* enforce is the unit test
`drift.rs:244-263 fingerprints_match_the_pinned_manifest`, which only
proves the manifest was generated from the vendored spec with the current
algorithm. **Nothing in CI compares the vendored spec to a live server**,
so drift against real `any` is discovered only on a manual re-vendor.

Other drift.rs tests: `:265-282` `drift_classes`, `:301-309`
`definition_shape_change_is_drift_and_cycles_terminate` (the 2026-08-01
blind spot — the `$ref` NAME was hashed, not the shape, so
`salience` integer→number went unnoticed), `:311-324`
`prose_churn_is_not_drift`, `:326-337` `response_schema_change_is_drift`.

**Re-pin recipe** — not written down as a runbook. Closest:
`docs/datetime-migration-plan.md:169-170` ("Re-vendor
`api/openapi.vendored.json` from the new server, `make api-drift`,
refresh `api/coverage.json` fingerprints") and
`docs/datetime-migration-plan.md:70` (a warning that object/record/chat
stamps are **not** in the OpenAPI schema, so `make api-drift` is blind to
them — "re-vendor anyway"). The four dated triage records are
`docs/api-parity.md` §C4 (2026-09-04, pin `a176029`), §C3 (2026-08-27,
pin `20708cd`), §C2 (2026-08-01, pin `afa3ed4`), plus §A/§B/§C/§D as the
standing plan. Reconstructed steps: curl `/v1/openapi.json` from a
current server into `api/openapi.vendored.json`; `anyrt drift` to see
new/removed/changed; hand-triage new+removed into `coverage.json`;
`anyrt drift --refresh` for the changed ones; append a dated section to
`docs/api-parity.md`.

Also: `.claude/skills/any-dev/SKILL.md:24` names
`api/openapi.vendored.json` as "wire truth" (`jq '.paths | keys'`);
`docs/adr/002-effect-boundary.md:229` names the drift manifest as the
thing to update when routes change; `docs/00-plan.md:229` and
`docs/01-implementation-plan.md:74` are the original design;
`docs/m3-notes.md:34` and `docs/m4-notes.md:15` record when it landed.

### The 58 mapped endpoints

```
DELETE /spaces/{spaceId}/objects/{objectId}                      helper.delete_object
DELETE /spaces/{spaceId}/objects/{objectId}/chat/messages/{msgId} helper.chat_delete
DELETE /spaces/{spaceId}/objects/{objectId}/editor/blocks/{blockId} helper.delete_block
DELETE /spaces/{spaceId}/types/{typeId}/datasets/{defId}          helper.remove_dataset
DELETE /spaces/{spaceId}/types/{typeId}/datasets/{defId}/fields/{fieldId} helper.remove_dataset_field
GET  /devices                                                     helper.list_devices (+ ADR-015 election read)
GET  /processes                                                   helper.list_processes
GET  /spaces                                                      helper.list_spaces
GET  /spaces/derived                                              anyclient.list_derived_spaces
GET  /spaces/{spaceId}                                            anyclient.get_space
GET  /spaces/{spaceId}/bundles                                    helper.list_bundles
GET  /spaces/{spaceId}/bundles/{bundleId}                         helper.get_bundle
GET  /spaces/{spaceId}/files/{fileId}                             file_info
GET  /spaces/{spaceId}/files/{fileId}/content                     download_file
GET  /spaces/{spaceId}/members                                    members
GET  /spaces/{spaceId}/members/requests                           join_requests
GET  /spaces/{spaceId}/objects/{objectId}/backlinks               anyclient.backlinks / helper.backlinks
GET  /spaces/{spaceId}/objects/{objectId}/editor/markdown         anyclient.get_markdown
GET  /spaces/{spaceId}/types                                      helper catalog (list_types)
GET  /spaces/{spaceId}/types/{typeId}/datasets                    helper.list_datasets
GET  /spaces/{spaceId}/types/{typeId}/properties                  helper catalog (list_properties)
PATCH /spaces/{spaceId}/objects/{objectId}/chat/messages/{msgId}  helper.chat_edit
PATCH /spaces/{spaceId}/objects/{objectId}/editor/blocks/{blockId} helper.patch_block
PATCH /spaces/{spaceId}/objects/{objectId}/editor/markdown        helper.edit_markdown
PATCH /spaces/{spaceId}/types/{typeId}/datasets/{defId}           helper.create_dataset (in-place patch)
POST /devices/activate                                            anyclient.activate_device
POST /events                                                      helper.open_in_ui (+ serve)
POST /processes                                                   helper._process_register
POST /processes/{id}/cancel                                       helper.cancel_process
POST /processes/{id}/finish                                       helper._process_finish
POST /processes/{id}/progress                                     helper._process_progress
POST /spaces/derived/{name}                                       anyclient.create_derived_space
POST /spaces/join                                                 join_space
POST /spaces/{spaceId}/acl/accept                                 acl_accept
POST /spaces/{spaceId}/bundles                                    helper.ensure_bundle
POST /spaces/{spaceId}/bundles/{bundleId}/children                helper.bundle_child
POST /spaces/{spaceId}/bundles/{bundleId}/resolve                 helper.resolve_loser
POST /spaces/{spaceId}/invites                                    create_invite
POST /spaces/{spaceId}/objects                                    helper.create_object
POST /spaces/{spaceId}/objects/aggregate                          helper.aggregate
POST /spaces/{spaceId}/objects/query                              helper.get_object / query_objects
POST /spaces/{spaceId}/objects/{objectId}/chat/messages           helper.chat_send
POST /spaces/{spaceId}/objects/{objectId}/chat/messages/{msgId}/reactions/{emoji} helper.chat_react
POST /spaces/{spaceId}/objects/{objectId}/editor/blocks           helper.create_block
POST /spaces/{spaceId}/objects/{objectId}/editor/markdown/append  helper.append_markdown
POST /spaces/{spaceId}/objects/{objectId}/files                   attach_file
POST /spaces/{spaceId}/properties/{objectId}/attach/{typeId}      helper.attach_type
POST /spaces/{spaceId}/properties/{objectId}/detach/{typeId}      helper.detach_type
POST /spaces/{spaceId}/properties/{objectId}/set/{typeId}         helper.update_object
POST /spaces/{spaceId}/query                                      anyclient.query
POST /spaces/{spaceId}/search                                     helper.search
POST /spaces/{spaceId}/types                                      helper.create_type
POST /spaces/{spaceId}/types/{typeId}/datasets                    helper.create_dataset
POST /spaces/{spaceId}/types/{typeId}/datasets/{defId}/fields     helper.add_dataset_field
POST /spaces/{spaceId}/types/{typeId}/properties                  helper.add_property
POST /spaces/{spaceId}/upsert                                     helper.upsert_records
PUT  /devices/me                                                  anyclient.register_device
PUT  /spaces/{spaceId}/objects/{objectId}/editor/markdown         anyclient.put_markdown
```

Note four mapped-but-uncalled entries: `chat_delete`, `chat_edit`,
`chat_react`, `create_block`/`patch_block`/`delete_block` have manifest
mappings but **no live call site** in the current tree — they were mapped
against the M4 parity plan (`docs/api-parity.md:22-27`) and never built.

### The 42 excluded endpoints

`DELETE /auth`; `DELETE /devices/{peerId}`; `POST /devices/query`,
`POST /devices/query/subscribe`; all 13 `/local/*`
(`DELETE|GET|PUT /local/collections`, `POST /local/{aggregate,delete,get,
indexes,insert,query,update,upsert}` — "host anyapi only, no guest
route", ADR-023); `GET|POST|DELETE /push/token`, `GET /push/subscriptions`;
`GET /account`, `PUT /account/metadata`; `GET|POST /auth`;
`GET /datasets`, `GET /spaces/{s}/datasets`; `GET /debug/p2p`,
`GET /spaces/{s}/debug`, `GET /spaces/{s}/debug/objects/{o}`;
`GET /events/subscribe`; `GET /files/cache`, `POST /files/cache/free`,
`POST /files/cache/sweep`; `GET /health`; `POST /shutdown`;
`GET /spaces/{s}/sync-status` ×3 + `GET /sync-status/subscribe`,
`POST /spaces/{s}/sync`; `POST /spaces/{s}/delete-records`;
`POST /spaces/{s}/modify`.

### The 57 uncovered (no helper, not excluded)

Space management: `DELETE /spaces/{s}`, `PATCH /spaces/{s}`,
`PATCH /spaces/{s}/settings`, `POST /spaces`, `POST /spaces/query`,
`POST /spaces/query/subscribe`, `POST /spaces/{s}/aggregate`,
`GET /spaces/{s}/types/{t}`, `GET /spaces/{s}/objects/{o}`,
`GET /spaces/{s}/properties/{o}`.
ACL / sharing: `POST /spaces/{s}/acl/{add,cancel-join,decline,ownership,
permissions,remove,self-remove,stop-sharing}`,
`POST /spaces/{s}/invite/{accept,decline}`,
`GET|DELETE /spaces/{s}/invites[/{recordId}]`,
`POST|DELETE /spaces/{s}/guest-key`,
`GET /spaces/{s}/members/{me,subscribe,{identity}}`,
`POST /spaces/one-to-one`, `POST /spaces/one-to-one/register-incoming`,
`POST /spaces/{s}/one-to-one/{accept,decline}`.
Identities: `GET /identities`, `/identities/subscribe`, `/identities/{identity}`.
Files v2: `GET /spaces/{s}/files`, `/files/stats`, `/files/subscribe`,
`/files/{f}/status`, `POST /files/{f}/{offload,pin,retry}`,
`DELETE /spaces/{s}/files/{f}`,
`POST /spaces/{s}/objects/{o}/files/query[/subscribe]`.
History: `GET /spaces/{s}/objects/{o}/history` ×4.
Read tracking: `POST .../reactions-read`, `.../messages/{id}/read`,
`.../chat/read-all`.
Subscribe: `POST /spaces/{s}/objects/query/subscribe`,
`POST /spaces/{s}/query/subscribe` **(!)**.
Properties: `PATCH|DELETE /spaces/{s}/types/{t}/properties/{propId}`.

**Two live inconsistencies in the manifest**:
1. `POST /spaces/{spaceId}/query/subscribe` is listed uncovered, but
   `runtime/src/anyapi.rs:988-1001` `subscribe_dataset` calls exactly it,
   from `serve.rs:2314` and `:2721`.
2. `GET /spaces/{s}/files` is listed uncovered, but
   `any@v1/program.py:2564` `list_files` calls it.
3. `PATCH|DELETE /types/{t}/properties/{propId}` are listed uncovered,
   but `any@v1/program.py:2003`, `:2042`, `:2058`, `:2087`, `:2101`,
   `:2113` call them (`docs/api-parity.md:143-149` says these are
   "guest-direct, no host passthrough — the host client has no consumer",
   which is why they read as uncovered).

## 10. e2e, rig scripts, fixtures

### Test harnesses

| path | what it stands up |
|---|---|
| `tests/test_rt_e2e.py` (205 ln) | fully OFFLINE. `:33-137` a `ThreadingHTTPServer` faking Anthropic (`/v1/messages`, `:98`) and any (`/v1/spaces`, `:63`) on an ephemeral port, then runs the real `anyrt` binary (`:21-29`). Skips without `runtime/target/*/anyrt` + `bin/kernel.wasm` |
| `tests/conftest.py` (≈180 ln) | the `integration`-marked surface against a LIVE server. `:29` `DEFAULT_SERVER = "http://127.0.0.1:7009"`, override `ANYBAO_TEST_SERVER`. Deliberately dependency-free stdlib client (`:43-70`). Probe `:145-152`. Wraps `POST /objects` `:97`, `/objects/query` `:100`, `/query` `:103`, `POST /types` `:109`, `/types/{t}/properties` `:112`, `/chat/messages` `:129`, `/search` `:139`, `/backlinks` `:142`, `POST /v1/spaces` `:170` |
| `runtime/src/testutil.rs` (537 ln) | `StubTransport` `:25-107` (canned replies + call log) and `FakeSpace` `:110-530` — a route-matching in-memory any server. Dispatch table `:338-476`: objects/query, objects, query, modify, properties/set, types GET/POST, datasets GET/POST, properties GET/POST, editor/markdown GET/PUT, files GET, invites, join, members/requests, acl/accept, members, **`GET /v1/spaces/_/agent/brain` (`:466`, a dead route)**. `send_raw` `:486-521` for file attach; per-object datasets inject `_addSeq` `:254` |
| `tests/test_any_module.py` (≈1300 ln) | the guest wire contract. `wire()` fake at `:14-34` answers `http.*` by longest-suffix match and captures `(VERB, path, json-body)`; `load()` `:52-60` execs the real module source with kernel globals. Asserts exact route tuples throughout |
| `tests/test_any_properties.py` | ADR-022 encoding/hydration against the same fake |
| `repos/_connectors/tests/test_gmailsync_program.py` | connector-side fake (`:134` reads `rid["$in"]`) |

### Fixtures

`tests/fixtures/` holds **no any-server response fixtures**. Contents:
`llm_anthropic.json`, `llm_openai-compat.json`, `parity/` (dir),
`bob78/` (dir), `program_validation.jsonl`, `recall-eval.jsonl`. All are
LLM or validation fixtures. Every any-wire fake is in code
(`testutil.rs`, `wire()`).

Reminder from `CLAUDE.md`: `tests/fixtures/*.jsonl` are JSONL — one
record per line is the parse contract; never save a pretty-printed
buffer over them.

### Rig / operator docs

| path:line | what |
|---|---|
| `docs/testing-agent-changes.md:39` | `/v1/health` probe |
| `docs/testing-agent-changes.md:60-73` | `anyrt deploy --target agent --config-file configs/anybao.staging.toml`, `anyrt serve`, `anyrt trace ls/show` |
| `docs/testing-agent-changes.md:86` | `POST /v1/spaces {"name":"_agentrepo"}` |
| `docs/prod-repo-account.md:29-49` | `go build -tags 'fts vector'`, `bin/any init/run`, `curl -X POST :7003/v1/spaces` ×2, `invite guest-key` ×2 |
| `docs/prod-repo-account.md:53-55`, `:102-103` | `anyrt deploy --addr http://127.0.0.1:7003 --source repos/_agent --target <raw space id>` |
| `docs/prod-repo-account.md:114` | `curl -s http://127.0.0.1:7003/v1/spaces` verification |
| `docs/repo-overlay-e2e.md:22-101` | full overlay e2e: init, create space, deploy, guest-key, delete old bao space, serve, trace, `curl :7001/v1/spaces \| jq '.spaces[] \| select(.name=="bao").id'` |
| `docs/testing-from-space.md:9` | "`POST /v1/spaces/{space}/query`, objects, records, search" |
| `docs/testing-from-space.md:31-107` | the three paths: `anyrt run --from-space`, deploy+serve, local-dir dev loop |
| `.claude/skills/any-dev/SKILL.md` | the local skill: all interaction through a scratch guest program via `anyrt run`, never raw curl for mutations; names `api/openapi.vendored.json` as wire truth |
| `.claude/skills/check-anybao-changes` | deploy → text bao → read the trace |
| `docs/debugging.md:72` | `POST /v1/local/aggregate` for trace queries |
| `docs/cutover-checklist.md`, `docs/repo-overlay-e2e.md` | cutover gates |

### Scripts

- `oauth-smoke.sh` — no any routes; builds a scratch program dir, runs
  `anyrt run gtest@v1 --programs $D --traces-dir traces`, scrapes the
  Google consent URL.
- `tools/eval/anyeval.py:43` `GET /v1/spaces/{space}`, `:49`
  `POST /v1/spaces/{space}/delete-records` (eval-space teardown).
- `grab-refresh-from-7001.py:33` `/v1/spaces`, `:41` `/v1/spaces/{bao}`,
  `:46` `/v1/spaces/{bao}/query` — ad-hoc untracked utility.
- `mint-google-refresh.py` — no any routes.
- `voice-eval/` — replays recorded conversations against LLM providers
  only. `judge.py:51` Anthropic, `rewrite.py:46-62` Anthropic/OpenAI/
  OpenRouter. **No any-server calls.** `voice-eval/results/*.json`
  contains model text that merely mentions `nav`, `type`, etc.
- `scripts/` does not exist; the only script dir is `tools/eval/`.

---

## Two live breakages found while inventorying

**1. `tests/conftest.py` calls four routes the server deleted.**

- `tests/conftest.py:115-116` `POST /v1/spaces/{s}/objects/{o}/agent/turns`
- `tests/conftest.py:118-119` `POST /v1/spaces/{s}/objects/{o}/agent/chunks`
- `tests/conftest.py:121-122` `GET /v1/spaces/{s}/agent/brain`
- `tests/conftest.py:124-125` `POST /v1/spaces/{s}/agent/memory`

`docs/api-parity.md:99-103` records all four as **removed** at the C3 pin
("Their consumers moved to userspace datasets (ADR-017)"), and none
appear in `api/openapi.vendored.json` (I checked — zero paths matching
`agent`). Callers that would 404 against any current server:
`tests/test_integration.py:26,35,46,49,55,58`,
`tests/test_instants_integration.py:60,62,74,79`,
`tests/test_memory_integration.py:19`, `tests/test_recall_eval.py:66`.
These are `-m integration` tests that skip without a live server, which
is why it has gone unnoticed.

**2. `runtime/src/testutil.rs:466`** still fakes
`GET /v1/spaces/_/agent/brain` → `{"objectId": "brain"}`, a route that no
longer exists.

---

## ADRs that would need amending

Ordered by how much wire surface each owns.

| ADR | owns | why a port touches it |
|---|---|---|
| **006** Data contracts | §0 general chat / bundles, §3 config store, §6 xKey normalization | any chat-resolution or type-handle change lands here first; §0 already carries two amendments and the recovery procedure |
| **022** Property formats | the whole format / xKind / option / handle contract, hydration, option CRUD | the `_MARKER_XKEYS` bridge is explicitly temporary |
| **017** Agent userspace datasets | bundle-child stores, the `includeDeleted` seq probe (§154-163), dataset declaration shape | §163 already notes "servers without `includeDeleted` are …" |
| **016** Email runtime dataset | `email_messages` declaration, multi-field `search.text` | |
| **010** Native introspection | §8 the flat `any@v1` surface, §1 the `_`-private program-plumbing split, §5 harness-declared user types | |
| **019** Instants | the filter guard, stamp/datetime encoding, `$date` on `/upsert` | stamps are not in the OpenAPI schema, so drift is blind here |
| **023** Trace records in local store | `/v1/local/*`, `trace_*` collections, `agent_runs`, the guest write guard | |
| **005** Loop core | §3 break/control records, §5 `context` on the message and `agent.outcome` | |
| **018** Event triggers | `chat_messages` as the only event source, the subscribe shape | |
| **004** Module loading | `program_source` + the `_addSeq` probe key (§4) | `_addSeq` is undocumented upstream |
| **009** Space-resident assets | deploy, overlays, guest-key join | |
| **021** Credential entry | `agent_secrets` shape, account-scoped `value` | |
| **015** Election | `/v1/devices`, `/devices/me`, `/devices/activate` | |
| **014** Progress / **025** Status | `/v1/processes` ×5 and `/v1/events` | 025 is still Proposed |
| **020** File input / **026** Blobs | the files routes, `any://f/` | |
| **002** Effect boundary | §2 names the drift manifest as the thing to update when routes change | **plus a hazard**: `runtime/src/routes.rs:7` `ANY_READ_POST_SUFFIXES` is a suffix match, so a new read-POST route not ending in `/query`, `/objects/query`, `/search` or `/aggregate` is silently classified `data.write` — a capability regression the drift detector cannot see |
