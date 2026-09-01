# BOB-78 questionary — full report (staging, 2026-08-31)

Six questions (types, multi-type links, KB recall, image→content,
PDF→content, agent-authored weather program) against seven preset
candidates, run through the real agent loop on the staging rig
(`:7134`/`:7021`, serve with `--secrets-file .connectors.env.off`).
One conversation per model in the bao general chat, content in a
per-model space (`bob78b-*`), chat + agent log + weather program wiped
between models, expected-model gate on every config's first run.
Traces: `anyrt trace ls --addr http://127.0.0.1:7134`; run ids in
`results2/*.jsonl` (session scratchpad), analysis dumps beside them.

## Verdict table

| model | result | cost (6 q) | typical latency/q | notes |
|---|---|---|---|---|
| claude-sonnet-5 (baseline) | 6/6 | ~$1.0 | 35–100s | native PDF+image; two graceful self-recoveries |
| claude-opus-5 | **6/6** | $1.27 | 24–69s | cleanest execution of all; zero wasted turns |
| z-ai/glm-5.3 (+5v-turbo vision) | 5/6 | $1.02 ($0.19 excl q5) | **5–26s** | fastest + cheapest per pass; q5 (PDF) died on fuel after a 12-min decompression spiral |
| moonshotai/kimi-k3 | **6/6** | $0.67 | 39–152s | cracked the PDF by hand-implementing inflate (152s) |
| qwen/qwen3.8-max | **6/6** | **$0.30** | 24–107s | cheapest overall; also cracked the PDF; q3 links carried a wrong space id |
| gpt-5.6-terra (+luna utility) | **6/6** (after unblock) | $0.23 | 9–49s | tools require `reasoning_effort: "none"` on chat/completions (tier `options`); passed everything reasoning-off, PDF included |
| gemini-3.7-flash | **6/6** (after unblock) | **$0.19** | **5–34s** | `signed_tool_calls` trait + skip sentinel (llm@v1 change); fastest+cheapest full pass, PDF cracked in 34s |

## Per-model notes

**claude-sonnet-5** — everything passes natively. Recovered from two
harness-induced faults on its own: a mis-tagged octet-stream image
("It's a real PNG, just mis-tagged" → built the LLM request manually
with the right mime) and program-placement (`use("weather@v1")`
unqualified resolves in the run's space; read the aliases hint in the
error, moved the program to the home space, ran it).

**claude-opus-5** — the reference run. Correct types/values/links,
receipt and menu extracted exactly, weather program saved home-space
first try, replies precise (noted Tbilisi's local clock). Handled the
"links never mint" error mid-q2 by creating the object first. ~2×
sonnet's price on this workload.

**glm-5.3 + glm-5v-turbo** — the vision pairing WORKS: q4's trace
shows the image call resolving `llm.tier.vision` →
`z-ai/glm-5v-turbo`, receipt read cleanly (store/date/items/total all
correct). q1–q4, q6 are the fastest and cheapest passes in the whole
matrix (often 1–6 turns, $0.01–0.06/question), and it made the nicest
design call of the bench (a `Shopping trip` TYPE, not just a page).
q5 is the failure: `openai-compat cannot read application/pdf` →
instead of degrading, GLM spent 33 turns implementing FlateDecode
inflate by hand in guest python, brute-forced a suspected corrupt
ascii85 byte with adler32 as oracle, and burned the entire 50G fuel
budget (745s, $0.83, run FAILED). No budget awareness despite the
FuelExhausted hint existing.

**kimi-k3** — 6/6. Hit the same PDF wall, also hit `zlib` not being
in the kernel allowlist — and *succeeded* at hand-decoding the stream
in 12 cells/152s ("small adventure: … I decompressed the menu by
hand. It worked, and I've noted the trick"). Native vision fine.
Slowest per-turn of the working set; mid-pack cost.

**qwen3.8-max** — 6/6 at $0.30 total, PDF also cracked by hand
(327s). One real quality flaw: q3's reply text was correct but its
object links used the `_agentrepo` space prefix instead of
`bob78b-qwen` — link fabrication under pressure; the same failure
shape (wrong-space ids from context) appeared in the invalidated v1
round. Also the chattiest about its memory ("third time this exact
setup has rolled through").

## Adapter blockers — found AND fixed same evening

- **OpenAI gpt-5.6 native**: `POST /v1/chat/completions` → 400
  "Function tools with reasoning_effort are not supported … use
  /v1/responses or set reasoning_effort to 'none'". We never send
  `reasoning_effort` — the server default has reasoning on. Fix (no
  code): the tier rows carry `options: {"reasoning_effort": "none"}`
  (merged last into the request). The OpenAI preset must ship with
  that row. Terra then passed 6/6 *with reasoning off*, PDF crack
  included. Follow-up kept open: a **Responses API backend** in
  llm@v1 would restore reasoning+tools together; `openai-terra`
  parity target added + golden recorded (replay green).
- **Gemini 3.7 (openai-compat endpoint)**: 400 "Function call is
  missing a thought_signature in functionCall parts" — position 4 =
  the **autorecall injection**, a client-constructed tool call that
  never came from Gemini, so it carries no signature. Real signatures
  already round-trip via `provider_state`/`extra_content`. Fix
  (llm@v1, deployed to staging): new `signed_tool_calls` trait
  (ADR-005 §1.3 vocabulary amended), true on the `gemini` profile —
  unsigned tool calls get Google's documented skip sentinel
  `extra_content.google.thought_signature =
  "skip_thought_signature_validator"`; provider-signed calls are
  left untouched. Gemini then passed 6/6.

## Runtime/product findings (ordered by impact)

1. **PDF on openai-compat wires needs a real path.** ADR-020 maps
   `application/pdf` → unsupported on the openai wire, so every
   non-Anthropic model must extract text itself — and `zlib` is not
   in the kernel allowlist, so three models implemented inflate from
   scratch (one died doing it). Options, cheapest first: add `zlib`
   to the kernel allowlist; teach `file_content` a `format:"text"`
   PDF extraction host-side; or route PDFs through a vision-capable
   tier as page images. Any of these turns q5 from heroics into
   routine for the whole openai-compat column.
2. **`anyrt run` config is process-local.** A plain run answers
   `config.get` from its seeds/`--config` and refuses `config.set`;
   `run --from-space` claimed serve parity but read the same local
   seeds, so a run-side tier check said nothing about what the serve
   ran — the first benchmark round was invalidated this way (every
   "model" was sonnet). Resolved: `run --from-space` binds the space's
   `agent_config` store (ADR-004 §6, 06aae67).
3. **Cross-space chat responder triggers are dead by construction**:
   `event_source_thread` subscribes with `ctx.space` (the bao space),
   so a `chat_messages` trigger pointing at another space's chat
   starts a source thread that never receives (serve.rs:2374).
   Resolved: `spec.spaceId` (ADR-018 §2, cff1d4a).
4. **`agent_turns`/`agent_chunks` ids are burned forever** —
   client-assigned seqs, and a deleted id can never be reused
   (`upsert.record_deleted`); deleting log records bricks the chat
   log (next `append_turn` 409s) and — worse — **a completed run is
   marked FAILED by the trailing append_turn failure** after all
   work and the reply already landed. The append should probably not
   be run-fatal.
5. **`create_space` (API/guest path) does not install
   `general-chat/v1`**, while the any@v1 docstring promises
   `generalChatObjectId` in the return. UI-created spaces get one;
   API-created don't until someone ensures the bundle. Resolved:
   `create_space` ensures the derived bundle and returns
   `generalChatId` (ADR-006 §0, a2b10c2).
6. **No `url` property format** — `create_type` url → "format.type
   must be one of [select, multiselect, links, date, datetime]"
   (also: "tags is reserved server-side"). Every model fell back to
   plain text and narrated the caveat. Either add the format or bless
   the fallback in `_any`.
7. **File uploads store the request Content-Type verbatim** (no
   sniffing); an octet-stream upload of a real PNG poisons the file
   for llm input. llm@v1's UnsupportedMedia error text is excellent
   (named the mime and the supported list) and enabled sonnet's
   self-recovery.
8. **Wrong-space "verification"** (v1 round, contaminated context):
   a model resolved its target space to the `_agentrepo` overlay,
   "verified" against it, and reported success with fabricated links.
   `_any` guidance candidate: re-resolve the target space by NAME at
   task start; never trust space ids from conversation history.
9. **The conversation window is built from the agent log**
   (`bao/log/v1` → `agent_turns`), not `chat_messages` — deleting
   chat messages does not clear the agent's conversational context.
   Expected under ADR-017, but worth stating in `_any`/docs since it
   surprises exactly when someone tries to "clear the chat".
10. **Brain memory survives wipes** (by design) — three models'
    memory "insisted" the weather program existed when it didn't
    (bench deleted it); all three checked reality and rebuilt, and
    qwen wrote itself a "verify with list_programs before assuming"
    decision. The desired verify-then-act behavior is happening.

## What this changes in the BOB-78 picker

- **Claude preset** (sonnet default, opus chip): confirmed.
- **OpenRouter — GLM 5.3 + glm-5v-turbo**: the pairing is now
  live-verified for images. Ship it, but land finding #1 first or
  PDFs will burn user money on decompression spirals.
- **OpenRouter — Kimi K3** and **Qwen 3.8 Max**: both fully passed;
  qwen is the value pick ($0.30/6 questions) with one link-integrity
  caveat, kimi the robustness pick. Both picker-worthy.
- **OpenAI native**: viable — preset tier rows must include
  `options: {"reasoning_effort": "none"}` until the Responses API
  backend lands; terra+luna passed the full questionary reasoning-off
  at $0.23. Golden: `openai-terra` (recorded 08-31).
- **Gemini**: viable — needs the `signed_tool_calls` llm@v1 change
  (staging-deployed, prod repo deploy pending); cheapest + fastest
  full pass of the matrix ($0.19, 5–34s/q). Same google key powers
  `search.provider.*`. One placement variance: it saved its weather
  program in the content space, not the home space.

## Benchmark integrity notes

The first full round was invalid (finding #2: all seven configs ran
sonnet) and is kept only as harness material; v2 re-ran opus/glm/
kimi/qwen on verified models (model gate reads the trace's `llm:`
line). Isolation in v2: chat messages + agent log wiped (seq-safe
marker), weather programs deleted between configs; brain memory
intentionally left (finding #10). Two harness bugs affected fairness
mid-stream and were fixed: upload mime (octet-stream → real mime) and
the attachment link shape (`any://f/<fileId>` vs canonical
`any://f/<spaceId>/<fileId>` — the 400's message documents the right
shape). opus q1–q3 show status FAILED from finding #4 (work + replies
landed; traces scoreable).

Rigs still running: any `:7134`/`:7021`, serve control `:7016`.
`bob78-*` (v1, sonnet-authored) and `bob78b-*` (v2) spaces retained
for inspection.
