# ADR-005: Loop core

Status: **Proposed** (awaiting review)
Date: 2026-07-07
Builds on: ADR-001..004 (accepted); plan §4 (loop control, provider
resolution, orientation summaries), §5 sketch

## Context

The successor of `toolcall_core@v1.js`'s loop — as harness code
(`loop.py` + `digest.py`), not a space program's inner machinery; the
space-resident `toolcaller.py` becomes policy/orchestration over this
(plan §5b). Already decided at plan level: provider-neutral message
model with adapters in the LLM effect; break/inject via a mailbox;
ceilings instead of the unbounded `for(;;)`; digest = progressive
disclosure + orientation summaries. This ADR fixes the shapes.

## Decision

### 1. Neutral message model (provider adapters live in the `llm` effect)

```python
Message   = {role: "user"|"assistant", parts: [Part]}
Part      = Text{text}
          | ToolCall{id, name, args}            # name == "run_cell"
          | ToolResult{call_id, content, is_error}
          | Thinking{text?, provider_state?}    # opaque blob, round-tripped
LLMReply  = {parts: [Part], stop: "done"|"tool"|"length", usage: Usage}
```

`llm.chat(messages, *, tier, tools, prefix_stable_upto=None)` is an
effect (ADR-001: fully recorded). Adapters: `anthropic` (native;
thinking `provider_state` round-tripped byte-exact; the
`prefix_stable_upto` hint becomes `cache_control`), `openai-compat`
(one adapter = vLLM/llama.cpp/SGLang/ollama/OpenRouter; hint ignored —
their caching is automatic), `fenced` (fallback for tool-weak models:
parses a ```cell block out of plain text into a ToolCall — the codeAct
heritage makes the tool interface emulatable on any completion
endpoint). Tier→provider/model resolution comes from the config effect.

### 2. One tool; the turn cycle

Single tool `run_cell(code)` (RUN_CELL description ported through the
skill audit). Cycle:

```
drain mailbox → llm.chat → stop?
  tool   → executor.run_cell(code, cell_id=tool_call.id)
           → digest (§4) → append ToolResult → loop
  done   → finalize: extract replies + any:// attachments,
           chat.send(done=True), persist turn (ADR-006 shapes), close
  length → synthesize is_error results for dangling calls + ask the
           model to wrap up text-only (v1's max_tokens handling, kept)
```

Interim assistant text before tool calls surfaces as `done: false`
progress bubbles (v1 behavior, kept). Errors: `is_error` ToolResult
with the error digest — the model self-corrects; no fix-loop (settled).

### 3. Ceilings and loop control

- **Ceilings, config-injected**: `max_turns`, `max_tokens_total`,
  `max_cost_total` per invocation. Hitting one triggers a **wrap-up
  turn** — a final constrained call ("no more cells; summarize state,
  what's done, what's pending") — never a silent hard stop (the
  no-lossy-truncation doctrine: caps produce a wrap-up, not a cut).
- **Mailbox, drained between turns** (checked after each cell too):
  `inject(msg)` → appended as a user Message before the next llm.chat;
  `break(soft)` → wrap-up turn now; `break(hard)` →
  `executor.interrupt()` + terminal chat message (`done: true`, noting
  the abort) + turn persisted with `interrupted` marker. Chat messages
  arriving mid-run on the same conversation are injected by default
  (the watcher routes them to the mailbox instead of queueing a new
  invocation).

### 4. Digest policy (`digest.py` — the 4k successor)

Rendering a CellResult (ADR-003) into ToolResult content:

- Sections: **Output** (printed values, numbered) → **Last value** →
  **Side effects** (trace-view summary for the cell: grouped
  `effect × count` + mutations called out individually with any://
  links; full detail via `effects.of(cell_id)`).
- **Budget-aware inline**: per-value inline budget in TOKENS (config;
  policy may scale with remaining context), not a fixed char constant.
  Over budget → stub `[N bytes, schema …, values.get("<cell>", i)]`.
- **Orientation summary** (plan §4, all guardrails apply): values over
  the inline budget get a cheap-tier one-paragraph summary rendered
  next to the machine stub — sampled input, describe-only prompt,
  labeled model-generated; skipped when the cell printed selectively;
  batched via `llm.complete_many`.
- **Teaching hints**: the `*_many` nudge (ADR-002) and future hints of
  the same shape — appended to the digest only when triggered.

### 5. System prompt & boot window assembly

Composition order (content shapes are ADR-006's; the loop owns
assembly): stable block [core skills + tool docs + memory categories]
marked via `prefix_stable_upto` → chunks message → raw turn window →
current user message (timestamp + ui-context suffix). Stable-block
content is fingerprinted; the fingerprint is recorded per run (prompt
drift is diagnosable from traces).

### 6. Purity

The loop is a pure function over (history, policies, mailbox) with
ALL externality via effects — llm.chat, executor cells, chat.send,
persistence. Consequence: a recorded conversation replays the entire
loop deterministically (golden tests), including its digests and
ceiling decisions.

## Consequences

- Provider-neutral by construction; local-cluster = the openai-compat
  adapter + config.
- Runaway loops impossible: every invocation ends via done, wrap-up, or
  break — all three recorded and user-visible.
- The digest keeps v1's proven progressive-disclosure architecture,
  gains token-budgets, orientation summaries, and trace-backed effect
  views.
- toolcaller.py (space program) shrinks to: boot composition choices,
  policy knobs, persona/skill selection — the §5b thin-program goal.

## Open questions (reviewer input wanted)

1. **Ceiling defaults**: max_turns=32, max_tokens_total=512k,
   max_cost_total unset? (Numbers to be tuned from metrics; need
   starting values.)
2. **Progress bubbles**: keep v1's every-interim-text bubble, or only
   bubble when >N seconds elapsed since the last user-visible output?
   Lean: keep v1 behavior first, tune from usage.
3. **Orientation-summary tier**: default to the `classify` tier
   (cheapest) or a dedicated `digest` tier in config? Lean: dedicated
   tier name mapped to the same cheap model — renaming later is free,
   re-plumbing isn't.
