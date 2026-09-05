# ADR-005: Loop core

Status: **Accepted** (2026-07-07); §1 amended 2026-08-31 (backends,
model profiles, traits — BOB-74); §5 amended 2026-09-05 (identity
block, voice tag, prompt fingerprints)
Date: 2026-07-07
Builds on: ADR-001..004 (accepted); plan §4 (loop control, provider
resolution, orientation summaries), §5 sketch

## Context

The conversation loop — the successor of `toolcall_core@v1.js` — is a
**guest program**: `programs/toolcaller@v1.py`. One invocation is
`main(args)` driving the whole turn cycle inside the cage. The host
contributes only the syscall surface (ADR-002): the engine runs the
program, the broker records every crossing, and a Runner-side mailbox +
hard-break flag let an operator steer a live run. Everything the
loop is made of — the llm adapters, the digest, boot-window and
auto-recall composition, turn persistence — is guest Python composed
from `programs/` modules, so a recorded conversation replays the entire
loop deterministically. Already decided at plan level: provider-neutral
message model with adapters; break/inject via a mailbox; ceilings
instead of the unbounded `for(;;)`; digest = progressive disclosure +
orientation summaries. This ADR fixes the shapes.

## Decision

### 1. Neutral message model; adapters, backends and model profiles live in `llm@v1`

```python
Message   = {role: "user"|"assistant", parts: [Part]}
Part      = Text{text}
          | ToolCall{id, name, args, provider_state?}  # name == "run_cell"; opaque, round-tripped
          | ToolResult{call_id, content, is_error}
          | Thinking{text?, provider_state?}    # opaque blob, round-tripped
          | File{media_type, data, name?}       # base64; adapter routes by media type (ADR-020 §3)
LLMReply  = {parts: [Part], stop: "done"|"tool"|"length", usage: Usage}
Usage     = {in, out, cacheRead, cacheWrite}    # tokens; `in` = the UNCACHED prompt on every wire,
                                                # cache* the cached part (may be 0); context in use = in + cacheRead + cacheWrite
```

`use("llm@v1").chat(messages, *, system, tier, tools)` lives in the
guest: it translates the neutral messages to a provider wire and
issues ONE `http.post` syscall (route-classified `read`/`llm.chat`),
wrapped in an `llm.chat` span so the trace and the digest read it as
one call. Because the crossing is a recorded effect, the full request
and response are in the trace (ADR-001). Tiers: `codegen`, `classify`,
`vision` (file reads — `llm.read`, ADR-020 §4).

**1.1 Tier config** (`config.get("llm.tier.<tier>")`, rows in
`agent_config`, ADR-006 §3):

```python
{"provider": "anthropic" | "openai-compat",   # wire family = adapter
 "model": "…",                                # sent verbatim
 "base_url": "https://…",
 "api_key_ref": "llm.key.<name>" | None,      # None = no credential (local servers)
 "backend": "<name>" | absent,                # where it is served; default: well-known host, else per provider
 "profile": "<name>" | absent,                # which model family; default: matched on `model`
 "options": {…} | absent}                     # raw request fields, merged last
```

Three things vary independently and are kept in three tables, each a
pure, offline-testable transform in `llm@v1`:

| varies with | table | owns |
|---|---|---|
| the **wire family** | adapter (`ADAPTERS`) | neutral ↔ message/tool/file/thinking block shapes; stop-reason normalization |
| **where** the model is served | backend (`BACKENDS`) | URL path, credential header + `about`, parameter spelling (`max_tokens` vs `max_completion_tokens`, `reasoning`/`thinking`/`chat_template_kwargs`/`think`), cache markers, usage field names (`cached_tokens`, `prompt_cache_hit_tokens`), response field names (`reasoning_content` vs `reasoning`) |
| **which model** | profile (`PROFILES`) | traits (1.3) + prompt-level shaping: system placement, tool-call carriage, lifting tool calls out of text, malformed-call handling |

The rule for placing a trick: depends on the URL → backend; depends on
the model wherever it is served → profile; depends on both → the
profile states the *intent* as a trait, the backend translates it to
the wire. Neither table ever imports the other: they meet only through
the traits dict and the request dict.

**1.2 The call pipeline** — every stage is a pure function of its
inputs; only `http.post` is an effect:

```
traits   = PROFILES[profile].traits            # resolved once per call
messages, system, tools
  → profile.prepare(messages, system, tools, traits)   # system→first user turn, tool docs
  → adapter.build_request(…, model)                    # wire family
  → backend.finish_request(req, traits, options)       # param spelling, extras, cache markers
  → effect("http.post", {url: backend.url(base_url), credential: backend.credential(prov), json: req})
  → backend.normalize_response(raw)                    # field-name unification
  → adapter.parse_response(raw)                        # → LLMReply
  → profile.lift(reply, traits)                        # ```cell / <tool_call> → ToolCall; bad args → error part
```

A profile hook that does nothing is the identity; the `generic`
profile and `generic` backend are all-identity, so a tier with only
`{provider, model, base_url, api_key_ref}` is the plain adapter path.

**1.3 Traits** — the neutral vocabulary a profile declares and the
backend + toolcaller act on. The set is closed by code: a profile
naming a trait no hook implements fails the unit suite, so a trait is
never silently decorative.

```python
traits = {
  "system_role":     "native" | "first_user",         # no system role → prepend to the first user turn
  "tool_mode":       "native" | "fenced" | "xml",     # how ToolCalls travel: tool API / ```cell block / <tool_call> text
  "reasoning":       "none" | "advisory" | "roundtrip",  # Thinking parts: absent / kept in trace only / must be resent
  "thinking":        "default" | "on" | "off",       # intent; the backend spells it (or drops it)
  "cache":           "auto" | "markers" | "none",     # prefix caching: implicit / explicit breakpoints / unavailable
  "context_window":  int,                             # tokens — toolcaller budgets ceilings and compaction from it
  "max_output":      int,                             # default output cap when the caller passes none
  "sampling":        {"temperature": …, "top_p": …},  # the model card's recommendation; `options` overrides
  "prompt_style":    "full" | "compact",              # which system-prompt/tool-description variant toolcaller assembles (§5)
  "instructions_at": "system" | "last_user",          # where the per-turn instructions ride
  "malformed_retries": int,                           # re-ask budget for unparseable tool calls before wrap-up
  "vision":          bool,                            # false = text-only: a File part raises UnsupportedMedia before any call
  "signed_tool_calls": bool,                          # provider rejects unsigned functionCall parts (Gemini 3+ thought
                                                      # signatures): real signatures round-trip via provider_state;
                                                      # client-constructed calls get the documented skip sentinel
  "pdf_input":       "none" | "file" | "image_url",    # the PDF carriage on the wire — granted by the BACKEND, not the
                                                      # profile (ADR-020 §3): file part (openai/openrouter/anthropic),
                                                      # PDF data URI under image_url (gemini's OpenAI layer), none
}
```

toolcaller reads the resolved traits through `llm.profile(tier)` and
uses only the loop-facing ones (`context_window`, `max_output`,
`prompt_style`, `instructions_at`, `tool_mode` for the tool
instructions, `malformed_retries`); it never sees provider, backend or
wire. The wire-facing ones are consumed inside `chat()`. A model trick
that no trait can express is new code in exactly one hook plus the
trait that gates it — the config selects behavior, it never defines it.

**1.4 Provider adapters.** `anthropic` — native; Thinking
`provider_state` round-trips byte-exact; `cache_control` breakpoints at
end of system and end of conversation, set by the adapter with no
caller hint (the loop's prefix is append-only, so each call writes the
cache the next one reads). `openai-compat` — one adapter for every
`/chat/completions` server (OpenAI, OpenRouter, vLLM, llama.cpp,
SGLang, ollama, Together, DeepSeek, Groq …); an assistant turn that
carries tool calls sends `content: null`, never `""`; tool-call
arguments that fail to parse become a `ToolCall` with `args: None`
carrying `error`; the loop answers it with an `is_error` ToolResult
instead of a cell, up to `malformed_retries`, then wraps up (the run
never dies on a malformed call). Reasoning text
(`reasoning_content`/`reasoning`, unified by the backend) is captured
as a Thinking part; whether it is resent is the profile's `reasoning`
trait, and the backend carries the provider's resend field
(`reasoning_details` on OpenRouter). `fenced` is the `tool_mode:
"fenced"` trait (the codeAct heritage: a ```cell block parsed out of
plain text emulates the tool interface on any completion endpoint);
`xml` covers the Hermes `<tool_call>` convention when a server template
does not lift it. A `done` reply that carries only reasoning IS the
answer (a template that failed to split thinking from text — Kimi K3
through OpenRouter, with leaked `<|close|>…` markers): `lift` adds the
reasoning text as the Text part, trailer stripped; a `length` stop is
left as the truncation it is.

**1.5 Caching.** Every OpenAI-compatible server caches an identical
prefix implicitly (`cache: "auto"`), so the contract is prefix
stability: the system block, tool list and every `prepare` output are
byte-stable across the turns of one conversation, and the per-turn
context suffix rides the user message, never the system. `cache:
"markers"` (Anthropic direct; Claude/Gemini through OpenRouter) adds
explicit breakpoints — the backend writes them, at the same two
positions; on a backend that cannot (`_MARKER_BACKENDS`) the
effective trait is `auto`, and `llm.profile(tier)` reports the
effective value. Server state that must ride a tool call back (Gemini's
`thought_signature`) is the call's `provider_state`, opaque to the
loop like Thinking's. Whatever a server reports lands in `usage.cacheRead`/
`cacheWrite` after `normalize_response`, and the trace view maps an
OpenAI-compatible exchange onto the block shape it renders
(`view.rs` wire normalization), so `trace ls`/`show`/`--stats` read
the same for every backend.

**1.6 Credentials.** The api key never enters the guest: the request
names a `credential` (`api_key_ref` + the backend's header/prefix +
`about` label/help, ADR-021 §1) and the host resolves the secret and
sets the header AFTER the payload records (ADR-002). `api_key_ref:
None` sends no credential — a local server needs none, and a null ref
raises no `SecretMissing` and no credential request (ADR-021 §2).
A ≥400 status raises `LlmError(status, body_excerpt)`; when the body
is a known *account* error rather than a transient one, the error
also carries a `hint` naming the fix (`_error_hint`), so the chat's
"Something broke" line tells the user what to change instead of
echoing the provider verbatim. Two cases today, both Anthropic 400s
that read alike and need opposite fixes:

- `anthropic-workspace-id is required when authenticating with an
  identity-linked API key` — a personal / service-account key
  created without a single workspace wants a workspace id on every
  request. bao sends none by design: the fix is a key **scoped to
  one workspace**, and the credential's `about.note` says so on the
  entry card before anyone hits it. The host treats this 400 as a
  credential rejection (ADR-021 §2), so the replacement-key card
  follows.
- `Your credit balance is too low to access the Anthropic API` —
  the key is fine, the prepaid API balance is empty (a Claude
  subscription does not fund it). Hint only: no card, nothing to
  replace.

**1.7 Adding a model or a backend** is one table entry plus its
evidence, isolated from the loop:

1. `PROFILES["<name>"] = {"match": r"…", "traits": {…deviations from generic…}}`
   (or `BACKENDS["<name>"]` with its url/credential/param spelling).
   A new trick = one new trait + its implementation in the one hook
   that owns it.
2. Unit: the pure transforms for the entry (`tests/test_llm_module.py`).
3. Wire pin: one recorded real reply per backend
   (`tests/fixtures/llm_<backend>.json`, `docs/llm-fixtures.md`).
4. Parity: the live, key-gated suite (`tests/test_llm_parity.py`) runs
   the reference conversation — multi-turn `run_cell` loop, an image
   part, a wrap-up on `length`, a malformed call — against the entry
   and saves the golden trace; from then on it replays offline. An
   entry is *supported* when its golden trace is in the tree.

`docs/llm-models.md` carries the step-by-step (which file, which
tests, how to run parity against a rig) and the table of supported
entries with the date of their last parity run.

### 2. One tool; the turn cycle

Single tool `run_cell(code)`. Each turn the guest loop runs:

```
drain mailbox (mailbox.drain syscall) → use("llm@v1").chat → stop?
  tool   → subcell(code, cell_id=tool_call.id) inside a "cell" span
           → digest (§4) → append ToolResult → loop
  done   → finalize: replies = assistant text, c.chat_send(done=True),
           append the turn via any@v1 (ADR-006 shapes — the write lands
           in the trace), return
  length → ask the model to wrap up text-only (a final call with the
           SAME tool list — the tools are part of the cached prompt
           prefix; dropping them made the run's largest prompt a
           full cache miss. Text-only is asked, not enforced: a reply
           that still calls a tool gets its dangling result answered
           and one more, tool-less call — amendment 2026-08-31),
           chat_send the summary (v1's max_tokens handling, kept). A truncated reply can carry a tool_call that never
           ran; the wrap-up user message MUST lead with a synthetic
           is_error ToolResult per dangling call ("not executed:
           <reason>") before the summarize text — the provider rejects
           a tool_use with no tool_result in the next message.
```

**A second tool under the `shell` feature (amendment 2026-08-31,
ADR-024 §4).** When the runtime is built with shell effects, the tool
set is `run_cell(code)` + `bash(command, as=None)`. `bash` is not a
second executor: the toolcaller runs it as a subcell in the SAME
kernel — `sh(command)` (ADR-024 §1) inside a `bash` span — and
renders the result raw (stdout, stderr, an `exit N` line only when
non-zero, head/tail truncation) instead of as a Python value. The
result object is bound in the kernel namespace as `sh.last` (and as
`<as>` when given) so the next `run_cell` processes the output
without re-running or re-pasting it; the tool-result footer names the
binding. Everything else in this section — spans, digests, the
`values` store keyed by tool-use id, ceilings, wrap-up handling of
dangling calls — applies to `bash` calls unchanged. Without the
feature the tool set is `run_cell` alone and nothing here changes.

Interim assistant text before tool calls surfaces as `done: false`
progress bubbles (v1 behavior, kept). Errors: `is_error` ToolResult
with the error digest — the model self-corrects; no fix-loop (settled).

### 3. Ceilings and loop control

- **Ceilings, passed in `args`**: `max_turns`, `max_tokens_total` per
  invocation (defaults in resolved-Q1); plus two from the profile
  (§1.3): the boot window is capped at a quarter of `context_window`,
  and a call whose input reached 85% of it is the last — the next
  turn is the wrap-up. Hitting one triggers a
  **wrap-up turn** — a final constrained call ("no more cells; in
  your own voice: what you did, what is done, what is still pending"
  — voiced since the §5 amendment of 2026-09-05, a bare "summarize"
  being a report-genre instruction that flattened the identity exactly
  at the end) — never a silent hard stop (the no-lossy-truncation doctrine: caps produce a wrap-up, not a
  cut).
- **Mailbox, drained between turns via the `mailbox.drain` syscall.**
  The drain is a recorded effect, so injections and soft breaks are IN
  THE TRACE — a replayed conversation replays its interruptions.
  `inject(text)` → appended as a user Message before the next
  `use("llm@v1").chat`; `break(soft)` → a `break` item that trips the
  wrap-up turn now. Chat messages arriving mid-run on the same
  conversation are routed to the mailbox by the watcher (injected by
  default) instead of queueing a new invocation.
- **Hard break stays host-side.** A wedged cell cannot drain the
  mailbox itself, so `break(hard)` sets a flag the engine's epoch
  callback checks on its next tick (ADR-003 — there is no separate
  watchdog thread); the guest traps, and the HOST posts the terminal
  bubble (`done: true`) and records the run as interrupted — the one
  thing the guest can no longer say for itself. The trap lands in
  guest code: a host call already in flight (an LLM or HTTP request)
  returns first — the broker does not yet check the flag mid-call.
- **Who sets the flag (amendment 2026-08-31).** Two setters, both
  through the watcher's `LiveRun {mailbox, interrupt}` for the chat:
  1. **A `break` control record in the chat.** The stop is data on
     the message, never a word: the client posts a chat message with
     an empty text and a `control` group (any `chat_messages-v5`):
     `{kind: "break", hard?: bool}`. The watcher reads the group, not
     the text. `hard: false` (default) = **soft** — the
     `break` item goes in the mailbox (the guest sees it at its next
     drain, between turns, and wraps up) AND a grace timer (20 s)
     sets the flag only if the item is still UNDRAINED then — a run
     stuck inside a long cell never saw it; a guest that drained it
     is wrapping up, and the escalation stands down (the run's wall
     deadline is the backstop). `hard: true` = **hard** — the flag
     goes up immediately and NO mailbox item is queued (a trapped
     run could never use it; it would only buy one billed, discarded
     wrap-up call). A control record is never content: whatever its
     `kind`, it neither injects into a live run nor starts one — a
     break with nothing running cancels any deferred (not-yet-
     started) messages for that chat and is otherwise a no-op.
     Clients render it as a marker in the thread, not a bubble.
  2. **The control API**: `POST /break/<runId>` with `{"hard":
     bool}` (default soft, same grace) — the operator's tool, keyed
     by the id every trace, log line and Stopped-bubble `debugLink`
     shows, and reaching ANY run in flight (chat, trigger, control),
     not just conversations; 400 for an id not in flight. It writes
     nothing to the chat — the chat record is the client's wire.
  The flag is the run's identity: the watcher hands out the run's own
  `Arc`, a timer that fires after the run ended sets a dead letter.
  On the flag the runner's epoch callback traps the guest at the next
  tick; the run reports `interrupted` and the host posts `Stopped.`
  with `done: true` — no "Something broke". The guest's own
  `append_turn` never ran, so the HOST writes the minimal turn —
  `{userText, replies: [], interrupted: true, traceRef}` — at the
  data layer: the next boot window and the log-reading crons see the
  stopped exchange, and the model's view of the conversation matches
  the chat on screen (the trace has the full run).
- **The trailing log append is bookkeeping, never the verdict.** The
  guest writes its `agent_turns` row after the final bubble has
  landed. If that write fails (a tombstoned seq, an unreachable
  store) the run stays `ok` — the reply is on screen, the failed
  effect is in the trace — and the result carries `logError` naming
  it. Failing the run there meant a second "Something broke" bubble
  and an `error` outcome for work that succeeded, which the user
  could not act on. The lost row is still real damage (the next boot
  window lacks the exchange), so the allocator itself must not lose
  rows to tombstones — ADR-017 §2 owns that.
- **How a run ended is data on the bubble, not text.** The host's
  terminal bubbles carry it in the message's `agent` group (any
  `chat_messages-v4`): `outcome` = `error` (the run died — the text
  is "Something broke mid-run: <type>: <msg>") or `interrupted` (a
  hard break — "Stopped."), and `debugLink` = the run id (`run_…`,
  what `anyrt trace show` takes). A normal reply carries neither.
  Clients key their rendering on `outcome` (a warning, a stop mark)
  and the trace lookup on `debugLink`; the text is for the human and
  never carries a trace ref. The vocabulary is the host's — the
  server stores `outcome` opaquely.

### 4. Digest policy (guest-side, in the toolcaller)

The digest is computed in the guest, per model cell: it renders the
`subcell` result (prints + last value, ADR-003) together with that
cell's effect records — read back with `trace.effects_of(span=…)` over
the cell's span — into the ToolResult content:

- Sections: **Output** (printed values, numbered) → **Last value** →
  **Side effects** (trace-view summary for the cell: grouped
  `effect × count` + mutations called out individually with any://
  links; full detail via `effects.of(cell_id)`).
- **Budget-aware inline**: per-value inline budget in TOKENS (policy
  may scale with remaining context), not a fixed char constant. Over
  budget → stub `[N bytes, schema …, values.get("<cell>", i)]`.
- **Orientation summary** (plan §4, all guardrails apply): values over
  the inline budget get a cheap-tier one-paragraph summary rendered
  next to the machine stub — sampled input, describe-only prompt,
  labeled model-generated; skipped when the cell printed selectively;
  batched via one `batch` fan-out over `llm.chat`.
- **Teaching hints**: the `*_many` nudge (ADR-002) and future hints of
  the same shape — appended to the digest only when triggered.

### 5. System prompt & boot window assembly

The stable block [core skills + tool docs + memory categories] is
composed by `anybao serve` (content shapes are ADR-006's) and handed to
the program as the `system` arg; the toolcaller appends a **runtime
context** section (agent space id, chat id, agent name — from its args;
amendment 2026-07-08: composed guest-side, the host writes no prompt
wording, and the ids must be stated because the model has no other
source for them) and passes the result unchanged to every
`use("llm@v1").chat`; the whole thing is stable per instance, and the
adapter's system-end cache breakpoint covers it. Stable-block content
is fingerprinted; the fingerprint is recorded per run (prompt drift is
diagnosable from traces). The rest of the conversation prompt is
assembled GUEST-SIDE: `history@v1` renders the boot window
(hierarchical chunks message → raw turn window), `autorecall@v1`
injects topical hits as a tool result (ADR-007 §5), and the current
user message (timestamp + view suffix) closes it. The suffix rides the
llm message only — the persisted turn keeps the raw `userText`.

The tier's profile picks the variant (§1.3): `prompt_style: "full"`
is the block above; `"compact"` is the same skills with the shorter
tool description and instructions, for models that follow a short
prompt better than a long one; `instructions_at: "last_user"` moves
the per-turn instructions from the system block to the tail of the
user message. Both variants are fingerprinted and byte-stable per
conversation (§1.5).

**The view rides the message (amendment 2026-08-29).** The user's
location is a property of the message they sent, not of the space:
any-ui stamps the chat message's `context` group
(`{spaceId, objectId?, view?}` — the page visible when the user hit
send; omitted when there is none) and the host hands it to the run
unchanged as the `uiContext` arg (a mid-run message rides the mailbox
inject as `context`). The toolcaller turns it into the
`[now: … | user's view — space: …, object: …, view: …]` line on THAT
message and binds it as the `currentUserSpace` cell global (ADR-010
§8); a message without a view degrades to timestamp-only and binds
`None`; an inject with a view rebinds the global, so "here" in code and
in prose always mean the newest message's view. Nothing is read from
the space and nothing is written to it: there is no pointer object, no
staleness age, no live re-read — the message IS the record of where
the user was, durable in the chat and self-describing in every trace.
Cron/trigger runs carry no view. The write side (`any@v1.open_in_ui`)
is unchanged.

**Kernel surface teaching (amendment 2026-07-07)** — the non-stdlib
cell globals (`http`, `print`, `now`/`rand`/`uuid4`, `env`, `use`,
`values`, `effects`) must be taught, but split by reach-frequency so
standing prompt stays minimal (every injected global is prompt tax):
- **Hot surface → the cached core skill** (small stable paragraph, in
  the fingerprinted block): cells are Python; `http.get/post`,
  `print()` = output channel, `now()/rand()/uuid4()`, `use('name@v1')`.
- **Recovery mechanics → just-in-time, no standing prompt**:
  `values.get`/`effects.of` are taught by the artifact that creates the
  need (the digest stub already prints `values.get("cell", i)`; the
  boundary `ImportError` enumerates the allowlist). Same doctrine as
  the `*_many` digest hint.
This is a design constraint on the runtime, not just the prompt: keep
the injected namespace deliberately small so the standing surface stays
cheap. stdlib mechanics are never in the prompt (the model knows them;
imports self-teach on error). Content authored in M4 (core skill,
through the audit); assembled here in M2.

**Tool-method discovery & the kind vocabulary (amendment 2026-07-18)** —
`_tool_docs` renders each `any_tool`'s description plus a one-line
method list. Two rules govern what the model sees:

- **Visibility is by documentation, not by a flag.** A method appears in
  the discovery surface iff it has a `program_methods` record (i.e. a
  `### name(sig) [kind]` heading in the tool's `schema.md`). To keep a
  method callable but hidden — a facade helper the harness uses but the
  agent shouldn't (bobrik's motivation for a hidden kind) — simply do
  not document it; leading-underscore names already never emit a record.
  There is **no hide-by-kind**. This retires bobrik's `[program]` kind,
  whose only job there was `methods.filter(m => m.kind !== "program")`;
  anybao hides by omission, so `[program]` is dropped from the
  vocabulary (toolmd) and `subagent.delegate` — a real entry point the
  agent must see — is a `[mutator]` (it drives a child loop that acts).

- **The kind is shown, at the point of choice.** The method line renders
  the authored kind — `save_with_dedup(candidate, recall) [mutator]`,
  `memory(client, space, llm_chat?) [setup]` — so read/write/bind intent
  is legible before the model fetches the full `program_methods` doc.
  The vocabulary is the SAME narrative set the span carries as
  `meta.kind` (ADR-001 §4d), one meaning across discovery and trace:
  `getter` (read), `mutator` (write / side-effecting), `setup` (a
  binder/constructor, e.g. `use("memory@v1").memory(c, space)`). Kind is
  narrative only — it never gates capability or the boundary read/mutate
  class (ADR-002).

**Identity, voice tag and prompt fingerprints (amendment 2026-09-05).**
Measured on the shipped skills: `_soul` is ~230 tokens of a ~16k-token
system block (1.4%), rendered under a `# Skill: _soul` heading in the
third person, immediately followed by `_core` opening with a second
identity ("You are a code-synthesis agent"), and a warm chat's boot
window replays up to 20k tokens of the agent's own earlier replies as
literal assistant turns. A soul edit therefore barely moves the voice:
position, framing and the transcript outvote it. Four mechanisms fix
that, each byte-stable per conversation (§1.5):

1. **Identity is a section, not a skill.** The body of the `_soul`
   `agent_skill` object is the FIRST bytes of the system block,
   verbatim: no heading, no wrapper, nothing before it. It loads
   two-tier like every `_` skill (ADR-009 §3: the agent overlay ships
   the default; a `_soul` object in the working space shadows it, so
   the user edits their own copy in the space and the next run picks
   it up — no deploy); an empty working-space body falls back to the
   shipped one. `_soul` leaves `SYSTEM_SKILL_ORDER`; the skills band
   starts at `_core`. Content rule: the soul is the only "You are" in
   the prompt — `_core` states the METHOD ("you act through one tool,
   `run_cell` …"), never a second identity. The body is capped at
   2000 tokens (head kept, a marker names the object) so a pasted
   essay cannot eat the prompt.
2. **The voice tag rides the newest message.** One line, derived at
   compose time from the `_soul` object: its `description` property
   (first line) or, absent that, the first sentence of the body;
   whitespace collapsed, `[`, `]` and `|` stripped, ≤120 chars. It is
   appended to the per-message suffix — `[now: … | user's view — … |
   voice: …]` — on the opener and on every mailbox inject, llm copy
   only (the persisted turn keeps the raw `userText`, so the tag never
   re-enters the boot window). Recency is the lever: the newest
   instruction lands AFTER the replayed old replies. Captured once per
   run (the system block is composed once); an edit made while a run
   is live lands on the next run. No system-tail voice line: the tag
   has one home.
   **Under load** (same-day follow-up): after tens of cells the user
   message — and its tag — sit behind tens of thousands of tokens of
   code and digests, and the reply that ends the run is generated at a
   call the loop cannot predict, so it read no voice at all and fell
   into the report genre. The same tag therefore also CLOSES every
   tool-result message and the wrap-up message, as a trailing
   `[voice: …]` text part after the results (both wires already carry
   results + text in one neutral message, §2). ~20 tokens per call,
   append-only and so cache-safe; identical every time. Quiet runs
   carry none (no identity → empty tag).
3. **Fingerprints, recorded on the turn.** `soulFingerprint` =
   sha256(soul body + "\n" + tag)[:16] (absent when no identity was
   composed) and `promptFingerprint` = sha256(system block as sent)[:16],
   both on the turn's `llm` group (ADR-006 §1 amendment). This delivers
   the fingerprint promised above: which soul produced a reply is a
   turn-row read, and the sent bytes stay in the `llm.chat` record.
   Surfacing them on the run summary (`trace ls/show --stats`, ADR-023
   §8) and a deploy-time warning when a working-space `_` skill shadows
   a shipped one are runtime follow-ups.
4. **The boot window knows when the voice changed.** When any raw-tail
   turn carries a recorded `soulFingerprint` different from the current
   one, the boot window opens with one line — `[voice changed after
   turn #N — the replies below were written under an earlier identity;
   take the current identity and the voice line on the newest message
   as the voice, not their tone]` — prepended to its first user message.
   Turns without a recorded fingerprint (pre-amendment rows) never
   trigger it. Only the raw tail replays literal replies; chunk lines
   are summaries and need no marker.

Quiet runs (ADR-008 §5) compose WITHOUT the identity and carry no tag:
a delegated child's report returns to the parent, not to the user, and
the shipped method block is its whole identity.

Precedent elsewhere: harnesses that expose a persona file load it
verbatim as the first section of the prompt, skip it for delegated
children, and write the default as a behavior spec (a sizing rule,
named prohibitions, an earned-depth escape hatch) rather than a trait
list — trait lists change nothing. Rewriting `_soul` along those lines
is a content change reviewed on its own, not part of this amendment.

### 6. Purity

The loop is a pure function over (history, policies, mailbox) with ALL
externality via syscalls — `use("llm@v1").chat`, `subcell`,
`mailbox.drain`, `c.chat_send`, turn persistence through any@v1.
Consequence: a recorded conversation replays the entire loop
deterministically (golden tests), including its digests and ceiling
decisions — the loop is guest code, and guest code sees only the trace.

## Consequences

- Provider-neutral by construction; a new model or server is a
  profile/backend table entry + config, never a loop change (§1.7).
- Runaway loops impossible: every invocation ends via done, wrap-up, or
  break — all three recorded and user-visible.
- The digest keeps v1's proven progressive-disclosure architecture,
  gains token-budgets, orientation summaries, and trace-backed effect
  views.
- The entire loop is `programs/toolcaller@v1.py` — deployed, versioned,
  and hot-swappable like any program; the host never ships to change
  loop policy, only the syscall surface underneath it.

## Resolved questions (review 2026-07-07)

1. **Ceiling defaults**: `max_turns=300` (a long agentic run is hundreds of short cells — 108 cut real runs short), `max_tokens_total=1M`,
   `max_cost_total` unset — starting values, tuned from metrics later.
2. **Progress bubbles**: keep v1 behavior — every interim text posts as
   a `done: false` bubble; revisit from usage metrics if ever noisy.
3. **Orientation-summary tier**: `classify` (reviewer decision — no
   dedicated tier).
