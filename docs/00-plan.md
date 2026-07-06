# anybao — current-architecture analysis & grand plan (bobrik respawn)

Sources: the four docs in the `foo` space ("bobrik architecture respawn" brief,
"any harness draft" philosophy doc, "python runtimes" survey, "Tracer — Concept
& Architecture"), plus three parallel code explorations: `cmd/bobrik-watch/`,
`anytype-agent-runtime@v0.1.2` (module cache), and the old generation at
`~/anytype/anytype-api-claude/assistantjs/`.

---

## 1. Current architecture, one picture

```
                    ┌────────────────────────────────────────────────┐
                    │  any (Go server, :7001)                        │
                    │  spaces / objects / datasets / chat / turns /  │
                    │  chunks / memory / debug / search / programs   │
                    └───────▲──────────────────▲─────────────────────┘
                            │ HTTP (everything)│ SSE chat_messages
                            │                  │
┌───────────────────────────┴──────────────────┴────────────────────┐
│ bobrik-watch (Go host, thin)                                      │
│  boot: ensureSpace/Chat/Types → bootstrapSystemFiles (hash-gated  │
│        sync of programs/, skills/, tool-descriptions/ → space)    │
│  watch: hand-rolled SSE loop, skip agent-field msgs               │
│  per message: fresh SobekRuntime → SetupAnySDKDirtyRuntime        │
│               → wrapper imports private:init_agent@v1             │
│  control API :7010 (/bootstrap, /bootstrap-clean)                 │
└───────────────────────────┬───────────────────────────────────────┘
                            │ EvalToString(wrapper)
┌───────────────────────────▼───────────────────────────────────────┐
│ anytype-agent-runtime (sobek = goja fork, pure Go, in-process)    │
│  ES modules name@version ← module resolver ← programs in space    │
│  effects: fetch/fetchBatch/sleep/console/chatReply (traced)       │
│  Trace = map[effect][argsJSON][]outputJSON;  SetMocks = replay    │
│  js.eval({persistent:true}) → long-lived kernel child             │
│  NO timeouts, NO cancellation, NO memory/output caps              │
└───────────────────────────┬───────────────────────────────────────┘
                            │ runs
┌───────────────────────────▼───────────────────────────────────────┐
│ toolcall_core@v1.js (2191 lines — the actual agent)               │
│  single tool: run_cell(code) → js.eval into persistent kernel     │
│  kernel globals: anyHelper, webSearch, convmemory, logs.get,      │
│                  toolEffects.get, _valueStore, _toolEffectsStore  │
│  digest: Output / Last value / Side Effects;                      │
│          MAX_VALUE_INLINE_CHARS=4000 per value, stub+selector     │
│  history: agent_turns (append-only) + agent_chunks (summaries     │
│           w/ fromSeq..toSeq) + agent_debug_log (per-turn page)    │
│  loop: unbounded for(;;), stop_reason-driven, errors → is_error   │
│        tool_result, model self-corrects (tracer deliberately cut) │
│  llm.js: raw Anthropic wire, tiers from config@v1                 │
└────────────────────────────────────────────────────────────────────┘
```

## 2. What is genuinely good — the keep-list

These are the fundamentals the philosophy doc names, and they are real in code:

1. **Toolcall+codeAct hybrid with a single `run_cell` tool.** Proven; the
   model writes cells against pre-bound kernel facades, not N bespoke tools.
2. **Persistent kernel between cells.** Vars, helper functions, imports
   survive across cells within a message — the context-hygiene engine.
3. **Effect tracing + mocks-as-replay.** `Trace` + `SetMocks` +
   `__wrapTrace` is the crown jewel and the seed of "deterministic
   evaluation." Nothing else in the stack is this differentiating.
4. **Stateless agent, state in the CRDT.** turns/chunks/debug/programs/
   skills all live in the space; the process is disposable.
5. **Progressive disclosure of outputs.** The June rework already made
   console.log the primary channel: inline if ≤4k chars, else
   `[N chars, schema … — logs.get(id,i)]` stub with the full value stashed
   in the kernel. The *architecture* of the 4k limit is right.
6. **Live module resolution, no cache.** Edit a program in the space, next
   import sees it. Deliberate; keep.
7. **Errors as `is_error` tool results.** The old tracer fix-loop was
   killed for a good reason (capable models self-recover); don't resurrect
   the loop — resurrect the *trace-shaping* underneath it.
8. **Explicit-pointer summaries.** Chunks carry fromSeq..toSeq; debug pages
   carry per-turn code+result. Summaries never orphan their raw data.

## 3. Failure points & poor decisions (ranked)

1. **No resource governance at all.** The runtime has zero timeouts,
   cancellation, memory or output caps; `sleep` is an uninterruptible
   blocking call; the agent loop is unbounded (`for(;;)`) with no
   turn/token/cost ceiling. Sobek exposes `Interrupt()` but the wrapper
   never surfaces it. A runaway cell or model loops forever and spends
   silently. *This is the single biggest gap regardless of language.*
2. **The permission system the philosophy doc promises does not exist.**
   Effects are all-or-nothing: any program (including imported third-party
   ones) can `fetch` any URL and read `env.CLAUDE_API_KEY`. No capability
   scoping, no per-effect gating, no mutation/read distinction beyond a
   name-prefix heuristic.
3. **The agent core is untestable.** `toolcall_core@v1.js` is a 2191-line
   god-file (LLM loop + digest + compression + debug collector + space
   context) with a module-global `_dc` singleton; there is no mocked-LLM
   test of the loop; JS integration tests need a live server and silently
   skip without one. The record/replay machinery exists but the harness
   never drives its own loop through it.
4. **Trace/mock keys are brittle.** Exact string-match on JSON-serialized
   args (Go header ordering, GET's dropped `method` field). Serialization
   drift silently misses mocks. Auto-wrap re-invocation is a hand-rolled
   string parser (`findLastInvocation`) with a name-prefix mutation guess.
5. **Code-as-strings footguns.** Programs are authored as JS strings inside
   JS, so `[\s\S]` collapses to `[sS]` (cost a real agent ~14 turns); the
   Go wrapper json-quotes args into source. A whole test README exists just
   to document the trap.
6. **Silent best-effort everywhere.** Debug writes, history persist,
   compression, collection-adds all swallow errors — a broken memory path
   is invisible.
7. **Duplicated logic kept in sync by hand.** Two tool-markdown splitters
   (Go `toolmd.go` + JS `anyPrograms._splitToolMarkdown`); fingerprint
   equality silently depends on their identical output.
8. **Anthropic wire-shape coupling.** The loop hard-depends on
   `stop_reason`/content-block semantics; `llm.js` speaks raw Anthropic.
9. **Fragile watch loop.** Hand-parsed SSE, no cursor/dedup; correctness
   rests on snapshot-drop + the `agent`-field filter.
10. **Go-host global state + scattered literals** (`base`, `debugFolderID`,
    space "bao", chat "general", folder names, port 7010).
11. **Per-message boot cost O(tools)** — minor (user: "not a big deal").
    Every message re-imports every program over HTTP and rebuilds the
    full tool-docs prompt. If optimized: cache keyed by
    **objectId + content hash/version — never name-only, never TTL** —
    validated by a **quick per-import probe** (fetch just the hash/
    version marker, full source only on mismatch; no subscriptions in
    the runtime). Two options for the marker, decision deferred to the
    runtime ADR: (a) the record's existing `_ver.id`/`_addSeq` — zero
    server work, bumps on every modify; (b) a handler-stamped derived
    `sourceHash` on `program_source` — small server change, the
    sink.Derive mechanism chat already uses for creator/modifiedAt is
    the precedent (`program` is a registered handler type). Interactive
    in-UI program editing must always take effect immediately; that
    property is why the current loader deliberately has no cache.

## 4. Positions on the brief's specific ideas

### 4k effect output
The mechanism is already the right shape — the June structured-console-log
rework made it progressive disclosure (inline ≤4k, else size+schema+selector
stub, full value stashed, `logs.get(id,i)` to walk). What to improve in v2:

- Make the inline budget a **policy, not a constant**: budget-aware
  (remaining context / expected turns), schema-aware previews (array head +
  length, object keys) instead of hard cutoff.
- **Unify runtime lookup and retrospection** (see "richer history" below):
  today `logs.get` works within a message; across messages you fall back to
  the debug page with a different access idiom. One addressable value store,
  same selectors live and retrospectively.
- **LLM orientation summaries on big results** (additive layer): when a
  value exceeds the inline budget, a cheap/fast model summarizes it into a
  short paragraph — what's in there, what it's about, how to query it —
  rendered alongside the machine-generated stub. Saves the 1–2 main-model
  exploration turns the stub alone usually costs. Guardrails: size/schema/
  selector stay deterministic (never LLM-generated); paragraph is labeled
  model-generated orientation, not evidence; summarizer gets a structured
  SAMPLE (schema + head/tail + random records + counts), never the raw
  blob, so cost doesn't scale with output size; describe-only prompt
  (injection-hardened); skip when the agent already console.logged
  specific fields (it signaled it knows what it wants); batch summary
  calls when one turn produces several big values. The summarizer call is
  itself an effect → replay stays deterministic.

### Tests
Design the new core hexagonal: the agent loop as a **pure function** of
(history, tool results, policies) → next action, with exactly one effect
boundary. Then:
- **The LLM call is an effect too.** Record it in the same trace format as
  fetch. Every real conversation becomes a golden replay test for free —
  the loop runs deterministically against recorded traces, no server, no
  API key. This is the single highest-leverage testing decision.
- Effects behind decorator-wrapped methods (`@effect`) → tracing, mocking,
  and permission checks live in one place; pytest fixtures inject a replay
  trace or a fake.
- Kill the dual splitter: one implementation, one language.

### Python
Recommendation: **yes — CPython, as a standalone harness process** talking
to `any` over HTTP (which the main doc explicitly allows; the pure-Go
constraint only applies if embedding in the `any` binary, which we don't
need). The wazero/pocketpy analysis solved a problem we don't have.

- Module resolution: `importlib.abc.MetaPathFinder` is the reference
  first-class loader — `import tools.websearch` fetches source from the
  space, exactly like today's `SetModuleResolver`, no cache, live edits.
- Persistent kernel: `exec` into a per-conversation module namespace —
  same semantics as the sobek persistent child, trivially.
- Cells: run in-process in that namespace, with a watchdog thread +
  `sys.settrace`/timeout for cancellation, or a subprocess executor when
  we want hard isolation (seccomp/rlimits) — a real upgrade path the Go
  runtime can't offer cheaply.
- Decorators give the clean mockable-method story; `rich` for the operator
  console; pytest replaces the bespoke harness-runner; codegen quality for
  Python cells is empirically better.
- What we give up: single static binary (accepted in the brief), the sobek
  trace machinery (rebuilt anyway to fix key brittleness), in-process
  embedding in Go (not needed — bobrik already talks HTTP only).
- What we must rebuild and improve while rebuilding: trace format v2,
  effects boundary, resource limits (which don't exist today anyway).

Fallback worth naming: retrofit Go+sobek (Interrupt exists, caps addable).
Cheaper short-term, but keeps JS codegen, the string-escaping traps, the
god-file, and the weak test story. The brief's goals point at Python.

### Mocks / tracer heritage
Don't resurrect the Haiku fix-loop — its economics died with weak models.
Do promote the underlying primitives to first-class citizens:
- **Trace format v2**: structured, normalized keys (canonical JSON, ordered
  fields, method-level not raw-HTTP) instead of exact strings; outputs
  addressable; LLM calls included; mutation/read classification declared on
  the effect (decorator arg), not guessed from names.
- **traceDiff isolation** (replayed calls silent, new effects visible) —
  keep exactly as designed; it's the right observability primitive.
- **detectError's classification** (runtime vs api_error vs child_error
  "report honestly, don't retry") — keep as the report-vs-retry policy.
- **Deterministic evaluation**: trace attached to the program version in
  the space → any past run replayable bit-for-bit. This is the philosophy
  doc's promise; v2 should deliver it.

### anyHelper: curated surface + API-drift flow (new requirement)
anyHelper is a hand-tuned agent-ergonomic wrapper over the REST API (the
product of real discovery about how LLMs want to call things) — keep it
curated, never autogenerate. But the server API changes fast; drift must
be detected and closed cheaply:
1. **Coverage manifest** — evolve `docs/10-coverage.md` into a
   machine-readable manifest in anybao: `METHOD /v1/path` →
   `{helper: method}` or `{excluded: reason}`, each entry carrying a
   fingerprint (hash) of the endpoint's swagger fragment (params, body,
   response, error codes). Exclusions are first-class.
2. **Version pin + detector** — anybao vendors the `swagger.json` of the
   `any` version it targets; `make api-drift` (CI) diffs spec vs
   manifest: new endpoint (uncovered), removed endpoint (stale helper),
   fingerprint mismatch (signature drift). Bumping the pin is the moment
   drift surfaces — event-driven, like compile errors on a dep bump.
3. **Drift mini-skill** — on drift, a skill receives the report,
   old/new swagger fragments, current helper source + tool .md docs, and
   the **helper style guide** (the currently-implicit tool-hygiene
   conventions: naming, arg shapes, space: param, error normalization,
   catalog caching — write this doc regardless; it's also what makes
   cheap-model maintenance of the helper possible). It drafts helper
   diff + doc diff + manifest update in the established idiom; human
   reviews the PR.
4. **Behavioral backstop** — replay-based helper tests catch semantic
   drift behind unchanged schemas.

### Overlays: spaces as package repositories (new requirement)
An overlay is a REGULAR space holding programs (existing format:
program_source + split tool docs + any_tool), skills, docs. Join →
semsearch/query/cross-space import all work today; "official
repositories" are just spaces anyproto publishes (pre-shipped
autojoin-readonly later changes distribution, not format). Not injected
into the prompt — guidance + default overlays instead. Design:
- **Aliases via the config effect**: `overlays: {std: <id>, …}` in the
  cascade (per-device/account overlay sets, ordered). `private:` stays
  built-in; `std:websearch@v2` = try-before-copy, no saving.
- **Resolution/shadowing rule**: unqualified names NEVER silently
  resolve into overlays (current space → private, as today); overlays
  need the alias or an explicit entry in the user's ordered resolution
  stack, local always winning — Nix-style deliberate shadowing ("copy to
  my space and modify" beats the overlay), no npm-style supply-chain
  surprise.
- **Transitive imports are defining-space-first** (lexical scoping):
  `std:foo` importing `bar` resolves bar in std, so overlay programs are
  self-contained; consumer shadowing of a transitive dep is an explicit
  config pin, never silent.
- **Format conventions**: manifest object per overlay (derived
  deterministic id — name, description, catalog, anyrt compat);
  published versions are FROZEN, edits bump `name@vN`; every program
  declares required **capabilities** (fetch/llm/chat/space-write) — the
  user sees what third-party code can do, the executor enforces it.
- **Trust**: readonly autojoined official space ⇒ only publisher writes
  ⇒ authenticity by CRDT ACL, no signing infra. Interim: joining = 
  trusting the space's writers; capabilities limit blast radius.

### Capabilities & trust — CapBAC model (new requirement)
The manifest alone can't carry authority — in an editable space any
writer can edit it. CapBAC reframe: **manifest = request, grant =
authority**, and they live in different places:
- **Grants are user-side** (device-local/private space, never in the
  shared program object) and **bound to the program's content hash**
  (source + manifest), not its name. Editing a manifest to add caps just
  changes the hash → grant no longer matches → re-prompt. Tampering
  yields a consent dialog, not authority. Policy smoothing: auto-regrant
  when the new cap set is equal-or-narrower (attenuation-only updates
  are safe by construction); prompt only on expansion.
- **Platform signatures cover the in-space case already**: every CRDT
  change is author-signed (ACL) — "who wrote this" is authenticated
  with zero new infra; readonly official overlays give publisher
  authenticity by ACL. The one gap is **surviving the copy**: an
  optional **attestation** field on the program object — publisher
  identity's signature over (sourceHash, manifestHash, name@version),
  verified via the identities directory (identities are public keys).
- **Trust tiers**: (1) self-authored — low-friction; (2) verified
  publisher attestation from a trusted identity → policy may auto-grant
  declared caps; (3) unverified → explicit grant on first run, re-prompt
  on expansion. Forks behave correctly for free: copy-and-modify breaks
  the attestation → drops to tier 3 as YOUR fork needing YOUR grant.
- **Attenuation down the import chain** (Macaroons-style): program A
  importing B runs B with intersection(A's granted caps, B's requested
  caps) — a child never exceeds its parent. Cheap to enforce at the
  effect boundary; composes with defining-space-first resolution.
- **POLA-scoped caps**: narrow grants (`fetch(*.github.com)`,
  `space-write(<spaceId>)`), user-side revocable ledger; grant prompts
  ride the chat/UI-command channel.
- **Manifest placement**: part of the program type (alongside
  program_source/program_description): requested caps, publisher
  identity, optional attestation. The grant ledger NEVER lives there.

### Workaround & system-skill audit (new requirement)
The helper + system skills are a sediment of three eras: old-anytype-API
fossils (ok:false conventions, error-logging-not-throwing, field-name
gotchas), Sobek-specific quirks (sync-only, fetch auto-parse, the
`[\s\S]` template-literal trap — ALL die with the runtime), and
harness-design guidance (digests, memory reads — invalidated by the v2
redesign). Stale prompt guidance is worse than none: it's paid context
every invocation and actively misleads. Process:
1. **Inventory + provenance tag** every anyHelper workaround and every
   system-skill claim: fossil / current-API-real / runtime-specific /
   harness-design.
2. **Evidence rule**: a workaround survives only with a test proving
   it's still needed — API-behavior claims become replay/contract tests
   (executable documentation). No test, no port.
3. **anybao system skills written fresh** against the new design; port
   only verified items. Python-quirk guidance grown from observed
   failures, never speculatively.
4. **Expiry mechanism**: surviving workarounds annotate the endpoint
   fingerprint they compensate for (drift manifest) — a contract change
   auto-flags the workaround for removal.
5. **Quirks are not set in stone — fix upstream by default.** Old
   Anytype was a blackbox; anybao + any + SDK are one system we control.
   Disposition order for any discovered quirk: fix in `any`/SDK first;
   a workaround is accepted only as a dated bridge referencing the
   upstream issue (no ticket = tolerated bug). The drift mini-skill
   proposes in both directions: helper adaptation OR server-side fix.
   Corollary: anyHelper is a *shrinking* layer by intent — its
   agent-ergonomics discoveries are requirements for evolving the REST
   API itself; over time the API absorbs them and the helper thins.

### Persisted config/state effect (new requirement)
Replace config-as-code (`config@v1` JS source) with config-as-data in Any,
built on the shipped scoped-fields surface (slice 22: property scopes
synced/account/local; record-local writes via `Object.LocalSet`, no DAG
change, never syncs):
- **Derived config object per space** (deterministic seed
  `any/agent-config/v1`, spaceIndex pattern — no discovery).
- **`config` effect with a resolution cascade**: device(local) →
  account → space(synced) → default. Integrations declare keys via the
  same helper: `config.define({key, scope, secret, default})`;
  `config.get(key)` walks the cascade. Per-space overrides = different
  tiers/models per agent instance.
- **Live**: subscribe on the config object → hot-reload without restart
  (a config-change trigger); editable from a settings miniapp.
- **Secrets discipline, enforced not documented**: synced values
  replicate to all members AND live in CRDT history forever (rotation,
  not deletion, is the only fix) — so `secret: true` keys refuse synced
  scope. Better: program code never receives key bytes — the LLM/fetch
  effects resolve credentials inside the effect boundary; programs pass
  tier names/handles. This is also the fix for today's
  `env.CLAUDE_API_KEY`-readable-by-any-program hole.
- ADR caveat: account scope for record fields is declared-but-not-
  writable today (mirror covers objects rows only) — per-account
  cross-device keys may need an objects-row property or small SDK work.
- Bootstrap: first key entry via CLI/env once, persisted thereafter.

### Loop control: break + inject (new requirement)
The toolcaller loop must support external control while running:
- **Inject**: a message arriving mid-run (user follow-up in chat, operator
  note, another agent) is appended into the live `messages[]` before the
  next LLM call, instead of queuing as a separate invocation after the run
  ends (today's behavior — `runAgent` is synchronous, messages just wait).
- **Break**: stop a running loop — soft (a wrap-up turn: "finish up,
  summarize state") and hard (cancel the in-flight cell via executor
  interrupt + terminal `done:true` chat message + debug record noting the
  abort).

Design shape: the conversation runner owns a per-conversation mailbox;
between turns the loop drains it (injected messages → user turns, break →
wrap-up/cancel). Mid-cell hard-break rides the same cancellation machinery
the executor needs anyway (failure point #1). Control sources: the chat
subscription itself (new human message in the same chat = inject by
default), and a control surface (CLI/UI command) for break. This is
impossible to retrofit cleanly today because the loop is a blocking
`for(;;)` inside a single js.eval with no interrupt path.

### Richer history
Turns already carry replies/effects/messageIds/debugRef; debug pages carry
per-turn code+result. The v2 move: **persist the per-turn trace (v2 format)
and value store as the debug record**, so retrospection = the same
`logs.get`-style selectors the agent uses live. "What happened in turn N of
run X" becomes a query + optional replay, not markdown archaeology. Cheap,
because the data already exists at digest time — it's a write-path change,
not a new subsystem.

## 4b. Memory & history — dedicated redesign subtask

Explored separately (two parallel code sweeps: `internal/agentmem` +
`convmemory@v1.js`/`search@v1.js`/`semsearch@v1.js`, and `internal/agentlog`
+ the compression/boot-window code in `toolcall_core@v1.js`). Verdict: the
storage layer is solid; the *policy* layer above it is where both halves
fail — and they fail in mirrored ways, which suggests one unified fix.

### History/compression — findings
- **Single-level compression, no rollup — the core scaling failure.**
  Chunks are never re-summarized; the boot window shows newest 8 chunks +
  8 raw turns, so the live context has a hard, silent horizon of ~80 turns.
  Older chunks scroll out and become unreachable: `expandChunk` needs a seq
  the model can no longer see, and `agent_turns`/`agent_chunks` are
  excluded from the semantic index. "Keep all raw data" holds in storage
  only.
- **Everything is count-bounded, not token-bounded** (TURNS_TO_INJECT=8,
  CHAT_CHUNKS_TO_INJECT=8, TURNS_PER_CHUNK=10, trigger at 16). A few giant
  turns blow the budget; many tiny ones waste it.
- **Compression runs synchronously on the completion hot path** (inside
  persistTurn, after the ✅ reply) — ~every 10th turn pays a summarizer
  LLM round-trip. A persistent summarizer outage creates a growing
  invisible middle (older than raw window, not yet chunked).
- **Client-assigned seq with a single collision retry** — safe only
  because delivery is single-threaded per chat. Server-assigned seq
  removes the entire class.
- **Dead schema**: `think` is declared/validated/rendered but never
  written (reasoning is conflated into `replies`); `cacheRead`/`cacheWrite`
  never populated. Also: the 2 oldest raw-window turns are usually inside
  the newest chunk (double-represented).
- Sound and worth keeping: append-only turns with explicit-pointer chunks
  (fromSeq..toSeq), the lean-turn / fat-debug-log split via `debugRef`.

### Memory — findings
- **The write side is the real functional gap.** `addMemory` has zero
  callers in skills/programs guidance; the agent is told when to *read*
  memory, never when/what to *save*, and there is no write-time dedup.
  Outcome: near-empty memory or unbounded near-dup accumulation.
- **Four recall paths that disagree**: (a) boot injects only category
  *names*; (b) indexed reads (byCategory/byPeriod/recent) work; (c)
  `convmemory.search` is degraded-by-design (the `__searchService` seam it
  waits on was never wired — dead code); (d) `semsearch` over `/search`
  scope `agent` is the ONE live semantic path. Worse, the RLM `search@v1`
  memory scope silently ignores the live index — its candidate generator
  falls back to recency, so the expensive reranker reranks a recency
  window, not a retrieved set.
- **Speculative schema, consumed by nothing**: salience/accessCount/
  confidence/importance are stored, validated, defaulted — and never read
  or updated by anything. Edges are validated and stored but never
  traversed and not indexed (write-only metadata). The philosophy doc's
  graph dimension doesn't exist in practice.
- **"Memory evolution" (consolidation, decay, relinking) is zero code** —
  the mutability mechanics exist (evolve allow-list, cheap content-hash
  reindex) but nothing drives them.

### Lineage: amemory is a modified A-MEM
The old amemory@v2 was an adaptation of **A-MEM** (Xu et al., "A-Mem:
Agentic Memory for LLM Agents", paper at `~/anytype/A-mem/paper.md`) —
Zettelkasten-style atomic notes with three mechanisms that map 1:1 onto
the old code: **note construction** (LLM-generated context/keywords/tags
+ embedding → the classifier path), **link generation** (top-k embedding
neighbors → LLM decides connections → `generateLinks`), and **memory
evolution** (a new memory triggers LLM updates to its retrieved
neighbors' context/keywords/tags → `evolveMemory`). v2 should treat
A-MEM as the reference design for the evolution loop — with the platform
letting us do it better than the paper (below).

### What the old amemory@v2 had — and where it went
The rewrite happened before semsearch shipped and dropped the functional
loop, but the old code splits into two very different categories:

**True regressions — live and working in the old harness, lost in the
rewrite** (recover these first):
- **Write policy as tool-description prose** (`amemory@v2.md`): budget
  ~1–2 saves/turn plus a ✓/✗ table (save: stable preference, decision,
  domain fact, hard lesson, shipped outcome; skip: greetings, meta,
  restatements, low confidence). Directly portable.
- **Dedup on write**: content-only embedding vs same-category items,
  cosine ≥0.85 → skip, returned as `{deduplicated:true, duplicateOf}`
  success. Note the deliberate **dual-embedding design**: content-only
  vector for dedup, enriched (content+context+keywords+tags) vector for
  recall — enriched vectors spread near-dupes apart, so one embedding
  can't serve both.
- **Hybrid ranked recall**: weighted cosine(.45)+FTS(.10)+entity(.15)+
  temporal(.10)+salience(.08)+confidence(.07); per-category temporal decay
  rates; category-filtered searches relaxed the similarity cutoff to 0
  ("surface the preference even without lexical overlap"); LLM query
  rewrite into 1–3 weighted sub-queries. Scoring fields shaped ranking
  internally but were never surfaced to the agent (`_compactResult`).
- **accessCount bump on every recall** (best-effort) — the one live
  maintenance mechanic.

**Already dead before the rewrite — implemented but disabled
(`enableLinks:false`; `reflect`/`decayMemories` never called)**: typed
bidirectional edges + 1-hop traversal with contradicts-penalty, link
generation on write, evolution of linked memories, reflection (synthesis
of unaccessed memories into insights + contradiction detection lowering
older confidence), salience decay with per-category rates and
importance/confidence protection thresholds, fuzzy entity scoring. These
are *unvalidated designs*, not proven regressions — v2 should treat them
as candidates for the background-cognition worker, gated on evaluation,
not as parity requirements.

**Rightly discarded** (don't mourn): hex-vector-in-text-props +
`cosineSimilarityHex` (superseded by the server IVF-SQ index), full-scan
recall (load-all-then-score), JS Levenshtein entity matching, dual edge
storage (JSON + markdown `## Links`), the never-wired 11-type
`memory-bootstrap.js` graph schema, the Haiku metadata-classifier write
path (agent-direct proved better and cheaper), boot-state-as-memory-object.

### Redesign direction (the subtask)
1. **One retrieval plane.** Collapse all semantic recall onto the `/search`
   index: route memory search and RLM candidate generation through it,
   delete the `__searchService` seam, and **index turns/chunks** (the
   missing gated chunker) so deep history — older turns/chunks within
   anybao's own data, NOT legacy bobrik data — is semantically reachable
   without exact seqs. Boot injection, explicit queries, RLM rerank, and drill-down
   all become views over the same index.
2. **Hierarchical, token-budgeted compression.** L1 chunks → L2 rollups
   (recursively), so the boot window covers ALL history at decreasing
   resolution within a token budget instead of a fixed count horizon.
   Chunk summaries keep explicit child pointers (chunk → chunks | turns),
   making drill-down recursive.
3. **A trigger subsystem powering async background evolution.** A-MEM's
   evolution step (and chunk rollup, consolidation, decay) must run
   asynchronously — which needs a general **trigger API**: a separate
   subsystem in the harness package where clients create triggers over
   HTTP on (a) events — "record added to dataset X", "object of type Y
   created" — implemented on top of the existing `query/subscribe` SSE
   primitives, and (b) cron-like schedules. A trigger binds an event to
   running a program with args. This also delivers the platform promise
   ("any program can subscribe to events; every friday; agent is a
   program too") and unifies the harness itself: the chat watcher becomes
   trigger #1 (chat_messages added → agent program), memory evolution a
   trigger on agent_memory_items created, chunk rollup a cron trigger.
   The background-cognition "worker" is then just the trigger executor —
   one scheduling/delivery mechanism, not a bespoke job runner.
   **Execution semantics (single-owner, at-most-once):**
   - **Instance identity**: each anybao instance generates a UUID once,
     persisted device-locally (config effect, device scope). Not the
     `any` deviceId — decoupled from the server process.
   - **Ownership**: trigger objects are synced (UI/agent-editable,
     survive restarts) and carry `owner: <instanceId>` + `enabled`.
     Every instance sees all triggers, runs ONLY its own. Create-flow
     stamps the handling instance as owner by default; explicit `owner`
     param assigns elsewhere. Instances keep an informational
     heartbeat row (id, hostname, lastSeen) for discovery — no
     election built on it.
   - **At-most-once, no fault tolerance by design**: owner down ⇒
     trigger doesn't fire. No leases, no leader election, no retries,
     no catch-up. Dead instance ⇒ manual owner reassignment (agent may
     propose it off a stale heartbeat). Same semantics philosophy as
     the UI-command channel.
   - **Cold-sync guards**: instances boot DISARMED; arm only after
     `/sync-status` reports the space synced (+ config override).
     Cron computes nextDue strictly from now — never looks backward.
     Event subscriptions drop the initial snapshot and react only to
     post-connect deltas; belt-and-braces event-age guard (skip
     records older than instance boot time minus slack).
   - **Run observability**: trigger object carries `lastRunAt`,
     `lastDurationMs`, `lastStatus`, `runCount`, `lastRunRef` (an
     object-kind property → auto-hydrated, UI-navigable). Per-run
     records live in an append-only `trigger_runs` dataset ON the
     trigger object (few-objects rule): ts, duration, status, error,
     and the run's **trace JSON** (the effects trace — same format as
     debug records, so runs are inspectable/replayable with the same
     tooling); oversized traces spill to a file attachment. Knobs:
     `logRuns: false`/sampling for high-frequency crons (synced-write
     churn), retention cap keep-last-N (run logs are operational data —
     the keep-all-raw doctrine applies to turns, not these).
   - Bonus: the chat watcher being trigger #1 under this rule gives
     exactly-one-responder for chat across devices — a double-reply
     hazard current bobrik-watch never addressed.
4. **Write policy + dedup-on-write for memory** — a straight port of the
   old proven loop onto new storage: the amemory@v2.md budget + ✓/✗ policy
   prose, and the 0.85 content-cosine dedup via the live index
   (merge-or-create instead of blind create). Mind the dual-embedding
   lesson: dedup wants content-only similarity; if the index only has
   enriched text, dedup needs its own comparison basis.
5. **Edges become Any properties + backlinks, not a JSON array.** The
   platform already has typed graph relations: object-kind **properties**
   linking to other objects — first-class, queryable, UI-visible, exactly
   the "graph dimension" the philosophy doc wants. Memory links should be
   properties on the memory object (edge type = property, target = object
   ref) instead of the opaque `edges` array — which also lets memories
   link to ANY object (tasks, docs, people), not just other memories.
   Reverse traversal comes from **backlinks** ("which objects refer to
   this one") — needs a backlinks read surface in `any`/SDK (open item:
   native SDK support vs an indexed reverse-lookup over object-kind
   property values). A-MEM's link-generation step then writes properties;
   neighbor-expansion recall walks properties + backlinks.
6. **Make scoring fields load-bearing or cut them.** Old code proves
   accessCount-bump-on-recall and salience/confidence ranking weights
   worked live; restore those with the index as the retrieval engine.
   Evolution/reflection/decay were never validated in production —
   promote them through evolution triggers one at a time, each behind an
   eval.
7. **Server-assigned seq** for turns/chunks; resolve dead fields (`think`
   — wire it to real reasoning content, or drop; `cache*` counters).

## 4c. Graph dimension — relations as the third search axis

FTS+semantic can't retrieve *relations between entities* ("Alice manages
Bob", "decision A overridden by B", "this task came out of that
meeting"). The graph machinery mostly exists — what's missing is reverse
traversal, visibility, and write discipline.

**The graph as it already exists.** Nodes = objects. Edge sources (all
written today): object-kind property values (typed, directed — propId is
the edge label), `nav.parentId` (hierarchy), inline `any://` links in
editor block text, memory edges-as-properties (§4b). Nothing new to
build storage-wise.

**Adopt from GraphRAG / graph DBs:**
- **Local search** (the practical core): hybrid search finds seed
  objects → expand 1–2 hops of neighbors → enriched context. Composes
  with semsearch as-is.
- **Relations indexed as text** (cheap, high value): an **edge chunker**
  emits `"<source name> —<property name>→ <target name>"` (resolved
  names, never raw ids) under a `graph` scope — relations join FTS +
  semantic retrieval on the one retrieval plane (§4b). Same chunker
  treatment for inline `any://` links in block text.
- **Property-graph model**: already have it. Lightweight edge =
  property ref; reify an edge as an object ONLY when it needs
  attributes (strength, validFrom). No graph query language — traversal
  is code (codeAct) over primitives; 1–3 hops covers the real queries.
- **Park for later**: community detection + cluster summaries
  (GraphRAG "global" mode) — a trigger-job experiment once local search
  proves out; hierarchical chunks already cover the temporal analog.

**Read-side primitives:**
- **Auto-hydration** — object-kind props resolve to `{id, name, type}`
  stubs on every read (one level, bounded, name-cached). Highest-
  leverage item here: today refs are opaque bafyrei ids, which is why
  the agent ignores the graph.
- **`neighbors(id)`** — forward refs + backlinks, one call, grouped by
  edge type, names resolved. **`backlinks(id)`** needs a server surface
  (upstream-fix principle → `any`/SDK: maintained reverse index over
  object-kind property values; the indexer is a natural home).
- Optional search flag: hits return with 1-hop neighbor stubs.

**Write-side discipline (the harder half):**
- **Entity canonicalization**: search-before-create doctrine; the
  identities directory anchors people.
- **Typed-property preference** in skills: a fact naming two entities ⇒
  ensure objects, link via property. Prose feeds the index; properties
  feed the graph.
- **Vocabulary drift is the main failure mode** (manages/manager_of/
  is_manager): curated starter edge vocabulary in the official overlay;
  "suggest existing properties first" (catalog query) in the write
  skill; background consolidation trigger flags synonym clusters.
- **Link generation as a trigger job**: A-MEM's link-generation step
  generalized beyond memory — new doc/chunk → seed-search neighbors →
  LLM proposes typed links → written as properties.

## 5. Target architecture sketch (v2)

```
┌──────────────────────────────────────────────────────────────┐
│ anybao (Python process)                                      │
│                                                              │
│  trigger subsystem (HTTP API: event triggers over            │
│  query/subscribe SSE + cron) ──► runs programs               │
│    trigger #1: chat_messages added ──► agent program         │
│    evolution/rollup/decay = event + cron triggers            │
│           │                                                  │
│           ▼ inbox (cursor+dedup) ──► conversation runner     │
│                                          │                   │
│  ┌───────────────────────────────────────▼────────────────┐  │
│  │ agent core (pure: state × policies → action)           │  │
│  │   loop policy: turn/token/cost ceilings, wrap-up turn  │  │
│  │   digest policy: budget-aware inline/stub rendering    │  │
│  └───────────────┬────────────────────────────────────────┘  │
│                  │ every external interaction                │
│  ┌───────────────▼────────────────────────────────────────┐  │
│  │ EFFECT BOUNDARY (the one seam)                         │  │
│  │  @effect(kind=read|mutate, cap=...) decorated methods  │  │
│  │  tracing (v2 keys) · mocks/replay · permissions ·      │  │
│  │  timeouts/cancellation · includes llm.complete()       │  │
│  └───┬─────────────┬─────────────┬───────────────┬────────┘  │
│      │ any HTTP    │ anthropic   │ web           │ executor  │
│      ▼             ▼             ▼               ▼           │
│   anyclient     llm providers   fetch      cell executor     │
│                                            (persistent ns,   │
│                                             watchdog; opt.   │
│                                             subprocess jail) │
│                                                              │
│  module finder: import from space (MetaPathFinder, no cache) │
│  value store: addressable live + persisted to debug record   │
└──────────────────────────────────────────────────────────────┘
     state stays in any: turns/chunks/memory/debug/programs/skills
```

Principles: everything external is an effect (LLM, HTTP, chat, sleep, time,
random — full determinism under replay); one trace format for all of them;
policies (budgets, permissions, digests) are injected config, not constants;
failures write loudly to the debug record, never swallowed.

**The isolation principle (core, user-stated): nothing executes side
effects except through the effect boundary.** One invariant, two faces:
deterministic evaluation (replay is bit-exact) AND confinement (programs
can do only what's allowed, and what's allowed is controlled). Rules:
- **Deny-by-default cell namespace**: curated builtins; no raw
  os/socket/time/random importable. Nondeterminism sources get
  effect-backed shims (`time.now()`, `random()`, env) recorded in the
  trace — sobek's ambient `Date.now()` was a quiet violation of this
  principle: harmless-looking, but it silently poisons replay
  determinism. Enforcement, not convention.
- **Import is an effect**: the module finder is part of the effect
  boundary (sobek already traced module.resolve); the import hook IS the
  allowlist, and the loaded version is part of the recorded run.
- **Staging changes enforcement strength, never the principle**:
  in-process (v2.0) = the invariant holds for honest code (nothing
  ambient reachable ⇒ accidental effects/nondeterminism structurally
  impossible); the security milestone = adversarial strength via
  subprocess + seccomp/landlock, or CPython-on-WASI (deny-by-default by
  construction — no clock/network/fs unless provided; wasm's native
  semantics ARE this principle). Program-facing contract identical at
  every stage.

## 5b. Repo & code structure

Constraint: keep few objects in Any → space programs stay big standalone
single-file units (toolcaller, amemory). Goal: structure clean and simple
enough to maintain by hand or with very cheap models.

Resolution — the object-count constraint only applies to SPACE-resident
code. toolcall_core is 2191 lines not because the space demands it, but
because harness mechanics (digests, compression, debug collector, LLM
wire) leaked up into a space program. Rule: **space programs are
big-but-thin (policy + orchestration); the kernel API under them is fat
(mechanics in git, tested).**

```
anybao/                          uv workspace
├── runtime/  → pkg "anyrt"      the contract programs run against
│   └── src/anyrt/               effects.py (@effect, registry, perms)
│                                 trace.py / replay.py (trace v2, mocks)
│                                 executor.py (persistent ns, timeout,
│                                 cancel) · loader.py (space
│                                 MetaPathFinder) · values.py (store,
│                                 schema, sampling) · limits.py
├── harness/  → pkg "anybao"      the app
│   └── src/anybao/               anyclient.py · llm.py (calls are
│                                 effects) · loop.py (pure core) ·
│                                 digest.py · history.py · memory.py ·
│                                 triggers.py · watch.py (inbox,
│                                 break/inject) · sync.py (ONE splitter)
│                                 · debug.py · main.py
├── programs/                     space-resident, 1 file = 1 object, FEW
│   ├── toolcaller.py             THE agent — policy/orchestration over
│   │                             kernel API; target ≤400 lines
│   ├── amemory.py                memory facade + write-policy prose
│   └── search.py                 RLM search
└── skills/                       markdown, 1 file = 1 object
```

Layering, calls point down only:
`programs (space, agent-editable) → kernel API (facades injected by
executor) → harness (git) → runtime (git) → any HTTP`.

Cheap-model maintainability rules:
- **Uniformity over cleverness** — every effect/trigger/tool facade has
  the identical shape; editing = pattern-matching, no judgment needed
  (the RLM eval showed cheap models write valid uniform cells fine).
- Boring Python: functions + dataclasses + two decorators
  (`@effect`, `@tool`); no inheritance trees, no metaclass/dispatch magic.
- Git modules ≤ ~300–400 lines, one concept each, docstring contract at
  top, adjacent tests runnable offline via replay traces.
- Space programs read top-to-bottom like a script: boot → loop →
  policies; tunable knobs (prompts, budgets, save policy) as named
  constants near the top.

Tradeoff (accepted): mechanics in git are not agent-self-editable
in-space. "Agent is a program too" holds where it matters — toolcaller,
tools, skills stay space-resident. Promotion path if needed later: lift a
module into a space program.

## 6. Phased plan

**Phase 0 — Document the current system** (mostly done by this analysis).
Turn §1–§3 into `docs/` in the respawn folder + diagrams; publish the
current-architecture doc + failure catalog into the space for review.

**Phase 1 — ADRs + one spike.** Short decision records: runtime choice,
trace format v2, effect/permission model, loop policies, process topology
(one anybao process per account, watcher+runner split). The spike:
port the loop skeleton to Python — run_cell against a persistent namespace,
LLM as a recorded effect, one golden replay test seeded from a real
conversation (a current-harness trace converted once into fixture data —
source material, not a compat path).
The spike validates or kills the Python bet in days, not weeks.

**Phase 2 — Core skeleton in `anybao/` (or sibling repo).** Effect
boundary + tracer v2 + replay + cell executor + space module finder +
minimal loop with ceilings and mailbox control (inject/break). Golden replay tests from day one; CI needs no
server, no API key.

**No backward compatibility, anywhere (explicit principle).** anybao is a
fresh start: no migration of legacy data (old turns/chunks/memory/debug
objects are abandoned in place, as the agent-data-layer migration did),
no legacy-JS program support in the executor, no wire/format compat with
bobrik-watch conventions. Dataset and trace shapes are free to change
(server-assigned seq, chunk→child pointers, trace v2) because anybao's
datasets start empty. "Parity" below means FUNCTIONAL parity — same
capabilities, clean-room shapes.

**Phase 3 — Functional-parity port.** Port anyHelper surface (Python client for
`any` HTTP), tool facades + docs pipeline (single splitter), history
(turns/chunks writer, boot-window renderer), debug collector (trace-backed),
skills — the helper/skills port runs through the **workaround & skill
audit** (provenance tags + evidence rule; nothing ports without a test or
a verified claim). Build the **trigger subsystem v1** here and express the chat
watcher as trigger #1 (event trigger on chat_messages + cursor/dedup) —
so the trigger API is proven by the harness's own core loop before
background-evolution consumers land on it. Run anybao side-by-side against a test
space (verification, not compat); old bobrik-watch keeps serving until
cutover, then is retired — no bridges between the two.

**Phase 4 — The new capabilities the rewrite pays for.** Permission
prompts/capability grants per program; budget-aware digests; retrospective
replay ("re-run turn 3 of yesterday's run with mocks"); subprocess
isolation for untrusted programs; and the **memory & history subtask**
(§4b): unified retrieval plane, hierarchical token-budgeted compression,
A-MEM-style async evolution on trigger subsystem (link generation via
properties, neighbor evolution, decay), write/dedup policy, load-bearing
scoring, backlinks-based graph recall. Parts of §4b land earlier where cheap — server-assigned seq and
the turns/chunks index chunker are Phase 3 parity-adjacent; the
hierarchical-compression *data contract* (chunk→child pointers) should be
fixed in Phase 1 ADRs so Phase 3 doesn't bake in the flat shape.

## 7. Open questions

- ~~Cell language = Python implies porting all programs/skills~~ —
  RESOLVED (no-backcompat principle): clean cut, no legacy-JS execution
  in anybao's executor, programs/skills rewritten through the audit gate.
- **Executor isolation level for v2.0**: in-process namespace + watchdog
  (fast, soft limits) vs subprocess-per-conversation (hard rlimits,
  slower). Suggest: in-process first, subprocess (or CPython-on-WASI) as
  the security milestone. Both stages implement the same isolation
  principle (§5 sketch) — staging varies enforcement strength
  (honest-code vs adversarial), never the program-facing contract.
  **CPython-on-WASI note (clarified 2026-07-07)**: python.wasm is
  host-agnostic — the security-milestone engine can be a single static
  **Rust binary embedding wasmtime** (engine #2 behind the same executor
  interface). Fit: effects-as-host-imports makes the isolation principle
  physical (no ambient world in the guest, clock/random virtualized);
  wasmtime fuel metering + epoch interruption + memory caps = the
  hard-limits story done properly (interrupts even C-level loops);
  single-binary distribution regained. Costs: ~2–5× interpreter
  slowdown (fine — cells orchestrate I/O), pure-Python-only guest (C
  extensions effectively out), guest↔host value serialization (same as
  subprocess). If the engine goes Rust, anyrt splits contract (Python
  pkg) / engine (Rust bin) — which is exactly the repo-split trigger.
  Bonus to park: wasm memory snapshots ⇒ resumable kernels.
  User (2026-07-07): familiar with wasmtime, happy to use it —
  Rust+wasmtime is the PRESUMPTIVE security-milestone engine, not just a
  candidate.
- **Where does anybao live** — decided direction: one NEW repo, uv
  workspace with two packages, `runtime/` (cell executor, effect boundary,
  tracer/replay, space module finder — the contract programs are written
  against) and `harness/` (agent loop, watcher, policies, programs/skills).
  Import direction harness → runtime only. Split runtime into its own repo
  once the trace/effect API stabilizes post-parity AND a second consumer
  exists (CLI runner / trigger-driven program execution from `any`). The Go
  `anytype-agent-runtime` repo stays untouched serving legacy bobrik-watch
  until cutover.
- ~~LLM provider abstraction depth~~ — RESOLVED: **provider-neutral
  core + adapters in the LLM effect** (local-cluster use cases are
  assumed; no Anthropic binding). Cheap BECAUSE of the single-tool
  design: `run_cell(code)` reduces the required provider surface to
  "code | final text | length", which every provider (and every
  OpenAI-compat local server: vLLM/llama.cpp/SGLang/ollama/OpenRouter
  = ONE adapter) can express. Neutral message model: typed parts
  (text/tool_call/tool_result/thinking) + normalized stop
  (done|tool|length) + usage. Three leaky spots, each handled: (1)
  Anthropic signed thinking blocks → opaque `provider_state` blobs the
  core never inspects, adapters round-trip; (2) prompt caching → core
  marks a stable-prefix-boundary hint, adapters translate
  (cache_control) or ignore (automatic/prefix caching); (3) tool-weak
  local models → **fenced-code fallback protocol adapter** (codeAct
  heritage: parse a ```cell block from plain text) emulates the tool
  interface on any model, even bare completion endpoints. Evidence:
  glm-5.1 root via OpenRouter + gemini classify already ran in
  production; the eval showed the failure axis is orchestration
  judgment, not wire format.
