# ADR-005: Loop core

Status: **Accepted** (2026-07-07)
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

### 1. Neutral message model (provider adapters live in `llm@v1`)

```python
Message   = {role: "user"|"assistant", parts: [Part]}
Part      = Text{text}
          | ToolCall{id, name, args}            # name == "run_cell"
          | ToolResult{call_id, content, is_error}
          | Thinking{text?, provider_state?}    # opaque blob, round-tripped
LLMReply  = {parts: [Part], stop: "done"|"tool"|"length", usage: Usage}
```

`use("llm@v1").chat(messages, *, system, tier, tools,
prefix_stable_upto=None)` lives in the guest: it translates the neutral
messages to a provider wire and issues ONE `http.post` syscall (route-
classified `read`/`llm.chat`), wrapped in an `llm.chat` span so the
trace and the digest read it as one call. Because the crossing is a
recorded effect, the full request and response are in the trace
(ADR-001). The api key never enters the guest: the request names a
`credential` (config `ref` + header); the host resolves the secret and
sets the header AFTER the payload records (ADR-002). Adapters:
`anthropic` (native; thinking `provider_state` round-tripped byte-exact;
the `prefix_stable_upto` hint becomes `cache_control`), `openai-compat`
(one adapter = vLLM/llama.cpp/SGLang/ollama/OpenRouter; hint ignored —
their caching is automatic), `fenced` (fallback for tool-weak models:
parses a ```cell block out of plain text into a ToolCall — the codeAct
heritage makes the tool interface emulatable on any completion
endpoint). Tier→provider/model resolution comes from the `config.get`
syscall.

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
           call), chat_send the summary (v1's max_tokens handling, kept)
```

Interim assistant text before tool calls surfaces as `done: false`
progress bubbles (v1 behavior, kept). Errors: `is_error` ToolResult
with the error digest — the model self-corrects; no fix-loop (settled).

### 3. Ceilings and loop control

- **Ceilings, passed in `args`**: `max_turns`, `max_tokens_total` per
  invocation (defaults in resolved-Q1). Hitting one triggers a
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
the program as the `system` arg; the toolcaller passes it verbatim to
every `use("llm@v1").chat`, and the adapter marks it `prefix_stable_upto`
for caching. Stable-block content is fingerprinted; the fingerprint is
recorded per run (prompt drift is diagnosable from traces). The rest of
the conversation prompt is assembled GUEST-SIDE: `history@v1` renders
the boot window (hierarchical chunks message → raw turn window),
`autorecall@v1` injects topical hits as a tool result (ADR-007 §5), and
the current user message (timestamp + ui-context suffix) closes it.

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

### 6. Purity

The loop is a pure function over (history, policies, mailbox) with ALL
externality via syscalls — `use("llm@v1").chat`, `subcell`,
`mailbox.drain`, `c.chat_send`, turn persistence through any@v1.
Consequence: a recorded conversation replays the entire loop
deterministically (golden tests), including its digests and ceiling
decisions — the loop is guest code, and guest code sees only the trace.

## Consequences

- Provider-neutral by construction; local-cluster = the openai-compat
  adapter + config.
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
