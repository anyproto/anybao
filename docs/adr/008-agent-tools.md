# ADR-008: Agent tools — webSearch, deepResearch, subagent, miniapp

Status: **Accepted** (2026-07-17)
Date: 2026-07-17
Builds on: ADR-002 (effect boundary, credential injection), ADR-004
(module loading), ADR-005 (loop core), ADR-006 §3 (config defaults
layer); folder-tool authoring (deploy); no-backcompat principle

## Context

bobrik-watch shipped four agent tools anybao lacks: grounded web
search, a multi-phase deep-research pipeline, task delegation to a
quiet sub-loop, and mini-app authoring over the server's `mini_app`
type. The bobrik JS is source material, not a contract — shapes are
fresh, adapted to the guest-program discipline: import-free sources,
effects for everything nondeterministic, API keys never entering the
guest (the `llm@v1` pattern: config names an `api_key_ref`, the host
injects the secret header after recording — ADR-002).

Two of the tools need a second provider credential (Gemini), which
today's config knows nothing about: `bootstrap` seeds exactly one
secret (`llm.key.anthropic` from `ANTHROPIC_API_KEY`).

## Decision

### 1. Credentials generalize past the LLM key

- New config namespace `search.provider.<tool>`, same value shape as
  `llm.tier.*`: `{provider, model, base_url, api_key_ref}`. Defaults
  (in `config_defaults.json`): `search.provider.websearch` and
  `search.provider.deepresearch`, both `gemini` /
  `gemini-2.5-flash` / `https://generativelanguage.googleapis.com` /
  `api_key_ref: "google.key.gemini"`.
- ~~`bootstrap` seeds `secrets["google.key.gemini"]` from the
  `GEMINI_API_KEY` env var, exactly parallel to the Anthropic key.~~
  (env sourcing removed 2026-07-28 — the ref seeds like any other
  secret: `.connectors.env` / `--secrets-file` hard seeds into the
  device-local store, see `docs/config-secrets.md`.) Secrets stay
  device-local (ADR-006 §3): never config, never readable from cells
  (`config.get` refuses secret keys).
- Guest requests name `credential: {ref: "google.key.gemini",
  header: "x-goog-api-key"}`; the host injects the header value after
  the effect is recorded — the key never appears in the trace.

### 2. http redirect surface (amends ADR-002 §1)

Two additions, both recorded so replay stays pure:

- The http result (`{status, headers, body}`) gains `url` — the FINAL
  url after the client followed redirects.
- With `response: "base64"` the result gains `encoding: "base64"` and
  `body` is the base64 of the raw bytes (ADR-020 §1).
- Requests accept `redirects` — the max follow count for that request;
  `0` = manual, the 3xx and its `location` header come back as data.
- Credentialed requests: the host owns the follow decision — default
  manual, an explicit count follows same-origin only (ADR-011 §4,
  amended 2026-08-02).

Grounded search returns
`vertexaisearch.cloud.google.com/grounding-api-redirect/…` source
links; one recorded no-follow GET per link reads `location` to the
real destination — the destination server is never touched (revised
2026-07-17 from `http.head`: the redirect endpoint speaks HEAD, but
ureq's HEAD handling chokes on it; no-follow GET is also strictly
closer to bobrik's `redirect: manual`).

### 3. `webSearch@v1` (folder tool)

- `search(*queries)` — each query is one Gemini `generateContent` call
  with the `google_search` grounding tool; multi-query fan-out goes
  through the `batch` effect (one guest→host round-trip; host-side
  execution is sequential today — parallelizing `sys_batch` is a
  runtime follow-up, not this tool's concern).
- Per query: the synthesized answer + grounding sources deduped by
  domain, source urls unwrapped per §2 (best-effort — an unresolved
  redirect keeps the original url). Returns a list of formatted
  strings, one per query; a failed query yields an `[ERROR] …` string
  in place, never poisoning the batch.

### 4. `deepResearch@v1` (folder tool)

Four phases, mirroring bobrik's pipeline:

1. Initial grounded call (same wire as §3, `search.provider.deepresearch`).
2. Decomposition via `llm@v1` (`classify` tier): 3–7 follow-up
   questions + a short collection name, JSON-only reply. A failed
   parse degrades to a single research page.
3. Follow-up grounded calls, fanned out via `batch`.
4. Write-out through `any@v1`: one sub-page per follow-up (answer +
   sources as markdown links), one overview page (initial answer,
   `any://` links to the sub-pages, deduped source list, timing
   footer). **Diverges from bobrik**: no bookmark objects, no
   collection — this server has no bookmark/collection builtins, and
   the overview page already is the hub. Page type is discovered:
   an existing `pages`/`page` xKey wins, else idempotent
   `create_type("Page")`.

Progress bubbles (`chat_send`) only when args carry a `chatId`;
otherwise silent. Returns a plain object `{ok, overviewPageId,
subPages, answer, sources, searchQueries, timing, usage}`.

### 5. toolcaller quiet mode + `subagent@v1` (amends ADR-005 §5)

- `toolcaller@v1` args gain `quiet: true`: no chat bubbles, no boot
  window, no auto-recall, no `append_turn`, no ROI log, and no
  `mailbox.drain` (the parent's inject/break stream must not be
  consumed by a child); ceilings still bound the run. The system
  prompt gets one extra line telling the model it is a subagent whose
  final reply returns to the delegating agent. Everything else —
  compose_system over the space, the cell loop, digests — unchanged.
- `subagent@v1.delegate(task, opts?)` =
  `use("toolcaller@v1").main({space, chatId, userText: task,
  quiet: True, …opts})`. Same space, full tool surface, fresh context
  (only the task text). Sequential only; parallel children are out of
  scope.
- Known, accepted: child cells share the kernel namespace with parent
  cells (`subcell` is reentrant by design; nested printers restore).
  Cell ids stay distinct (provider-unique tool-call ids). Depth is
  mechanically possible but prompt-discouraged beyond one level.

### 6. `miniapp@v1` (folder tool)

Over the harness-declared `mini_app` user type (amended 2026-08-26;
the server builtin is deleted — xKey `mini_app`, ensured by this
program, its writer, on `create`; ADR-017 §1): content lives in its
runtime dataset `mini_app` (`idRule: user`, `dynamic`, string fields
`mutableBy: any`, no `search` mapping — HTML is never indexed), single
record `"main"`, flat string fields `source` (full HTML) / `state`
(JSON text) / `readme` (markdown); the object's `any.name` is the
addressing slug. any-ui resolves the type by xKey. Writes are
per-field `$set` ops (via `modify`) so updating state never rewrites
source. Surface (snake_case):
`create(name, source, state?, readme?)`, `update(name, …)`,
`edit(name, old_string, new_string, replace_all?, block?)`,
`get(name, from?, to?)`, `get_source(name, from?, to?)`, `list()`,
`set_state(name, state)`, `get_state(name)`,
`upsert_readme(name, readme)`. Every source write runs the
runtime-script guard: author-written `./react.js` / `./react-dom.js` /
`./useAnytypeState.js` script tags are stripped and all three are
prepended in canonical load order, reported as warnings (the embed
loads them by relative src; they must precede the author's inline
script, react before react-dom — presence-only injection let a
wrong-ordered source through broken). Only these three tags are
touched; everything else in the source is the author's.

### 7. Authoring & testing discipline

Each tool is a `programs/<name>@v1/` folder (`program.py` +
`description.md` + `schema.md`); the description + method list enter
the system prompt via the existing `any_tool` mechanics — no prompt
composer changes. Verification runs against scratch spaces through
`anyrt run`; deploying to the bao space is not this branch's business.

## Consequences

- The agent regains bobrik's reach (search, research, delegation,
  mini-apps) with keys held to the same standard as the LLM key: one
  env var per provider, host-injected, absent from traces.
- `search.provider.*` gives every future keyed tool a home; the
  secret-ref pattern needs no new mechanism per provider.
- Quiet mode makes the toolcaller reusable as a library loop; the
  subagent is a thin wrapper, not a second loop implementation.
- Deferred: parallel subagents, host-parallel `batch`, bookmark/
  collection write-out for deepResearch, kernel-level namespace
  isolation for child loops.
