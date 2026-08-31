# ADR-005: Loop core

Status: **Accepted** (2026-07-07); §1 amended 2026-08-31 (backends,
model profiles, traits — BOB-74)
Date: 2026-07-07
Builds on: ADR-001..004 (accepted); plan §4 (loop control, provider
resolution, orientation summaries), §5 sketch

## Context

The conversation loop — the successor of `toolcall_core@v1.js` — is a
**guest program**: `programs/toolcaller@v1.py`. One invocation is
`main(args)` driving the whole turn cycle inside the cage. The host
contributes only the syscall surface (ADR-002): the engine runs the
program, the broker records every crossing, and a Runner-side mailbox +
hard-break watchdog let an operator steer a live run. Everything the
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
          | ToolCall{id, name, args}            # name == "run_cell"
          | ToolResult{call_id, content, is_error}
          | Thinking{text?, provider_state?}    # opaque blob, round-tripped
          | File{media_type, data, name?}       # base64; adapter routes by media type (ADR-020 §3)
LLMReply  = {parts: [Part], stop: "done"|"tool"|"length", usage: Usage}
Usage     = {in, out, cacheRead, cacheWrite}    # tokens; cache* may be 0
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
does not lift it.

**1.5 Caching.** Every OpenAI-compatible server caches an identical
prefix implicitly (`cache: "auto"`), so the contract is prefix
stability: the system block, tool list and every `prepare` output are
byte-stable across the turns of one conversation, and the per-turn
context suffix rides the user message, never the system. `cache:
"markers"` (Anthropic direct; Claude/Gemini through OpenRouter) adds
explicit breakpoints — the backend writes them, at the same two
positions. Whatever a server reports lands in `usage.cacheRead`/
`cacheWrite` after `normalize_response`, so `trace show --stats` reads
the same for every backend.

**1.6 Credentials.** The api key never enters the guest: the request
names a `credential` (`api_key_ref` + the backend's header/prefix +
`about` label/help, ADR-021 §1) and the host resolves the secret and
sets the header AFTER the payload records (ADR-002). `api_key_ref:
None` sends no credential — a local server needs none, and a null ref
raises no `SecretMissing` and no credential request (ADR-021 §2).

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
  length → ask the model to wrap up text-only (a final constrained
           call), chat_send the summary (v1's max_tokens handling,
           kept). A truncated reply can carry a tool_call that never
           ran; the wrap-up user message MUST lead with a synthetic
           is_error ToolResult per dangling call ("not executed:
           <reason>") before the summarize text — the provider rejects
           a tool_use with no tool_result in the next message.
```

Interim assistant text before tool calls surfaces as `done: false`
progress bubbles (v1 behavior, kept). Errors: `is_error` ToolResult
with the error digest — the model self-corrects; no fix-loop (settled).

### 3. Ceilings and loop control

- **Ceilings, passed in `args`**: `max_turns`, `max_tokens_total` per
  invocation (defaults in resolved-Q1); plus two from the profile
  (§1.3): the boot window is capped at a quarter of `context_window`,
  and a call whose input reached 85% of it is the last — the next
  turn is the wrap-up. Hitting one triggers a
  **wrap-up turn** — a final constrained call ("no more cells;
  summarize state, what's done, what's pending") — never a silent hard
  stop (the no-lossy-truncation doctrine: caps produce a wrap-up, not a
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
  mailbox itself, so `break(hard)` sets a flag the Runner's watchdog
  thread polls; the watchdog interrupts the engine (epoch bump,
  ADR-003) and the HOST posts the terminal bubble (`done: true`) and
  records the run as interrupted — the one thing the guest can no
  longer say for itself.

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

1. **Ceiling defaults**: `max_turns=108`, `max_tokens_total=1M`,
   `max_cost_total` unset — starting values, tuned from metrics later.
2. **Progress bubbles**: keep v1 behavior — every interim text posts as
   a `done: false` bubble; revisit from usage metrics if ever noisy.
3. **Orientation-summary tier**: `classify` (reviewer decision — no
   dedicated tier).
