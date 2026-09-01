# LLM models: profiles, backends, and how to add one

The contract is ADR-005 §1; this is the step-by-step. Everything
model-specific lives in `repos/_agent/programs/llm@v1/program.py` as
three pure tables — the loop (`toolcaller@v1`) reads only the resolved
traits through `llm.profile(tier)` and never sees a wire.

| table | keyed by | owns |
|---|---|---|
| `ADAPTERS` | the wire family (`anthropic`, `openai-compat`) | message / tool / file / thinking block shapes, stop-reason normalization |
| `BACKENDS` | where the model is served (`anthropic`, `openai`, `openrouter`, `gemini`, `deepseek`, `groq`, `together`, `vllm`, `llamacpp`, `ollama`, `generic`) | URL path, credential header + `about` label/help, parameter spelling, cache markers, response/usage field unification |
| `PROFILES` | the model family (`claude`, `gpt`, `gemini`, `deepseek-r1`, `deepseek`, `qwen3`, `llama`, `gemma`, `mistral`, `glm`; explicit-only `fenced`, `xml`, `generic`) | `traits` — deviations from `GENERIC_TRAITS` |

A tier row (`agent_config` `llm.tier.<tier>`) picks them:

```json
{"provider": "openai-compat", "model": "deepseek/deepseek-r1-0528",
 "base_url": "https://openrouter.ai/api/v1", "api_key_ref": "llm.key.openrouter",
 "backend": "openrouter",        // optional: inferred from a well-known host, else generic
 "profile": "deepseek-r1",       // optional: matched on `model`, else generic
 "options": {"top_p": 0.95}}     // optional: raw request fields, merged last
```

`api_key_ref: null` = a keyless local server: no credential on the
request, no credential card in the chat.

**Which Anthropic key.** One **scoped to a single workspace** (Console
→ Settings → API keys → Create key → choose a workspace). A personal /
service-account key created *without* a workspace is identity-linked:
the API wants an `anthropic-workspace-id` header on every request and
answers 400 `anthropic-workspace-id is required when authenticating
with an identity-linked API key` without it. bao does not send that
header — the id would have to live apart from the key and go stale on
every rotation — so it asks for a workspace-scoped key instead: the
credential card's note says so up front, the `LlmError` hint says so
after the fact (it is not a credit/billing error), and the host treats
that 400 as a rejected credential, so the replacement-key card
follows. The genuinely-billing 400 — `Your credit balance is too low
to access the Anthropic API` — gets its own hint (Console → Plans &
Billing; a Claude subscription does not fund API use) and no card: the
key is fine.

## Where a trick goes

- Depends on the **URL** you call → a backend hook (`finish` for the
  request, `normalize` for the response).
- Depends on the **model** wherever it is served → a profile trait.
- Depends on **both** → the profile states the intent as a trait
  (`thinking: "off"`), the backend spells it (`reasoning_effort`,
  `reasoning.enabled`, `chat_template_kwargs.enable_thinking`, …).
- Needs **new code** (a text format to lift, a new prompt placement) →
  one new trait in `TRAITS` + its implementation in the one hook that
  owns it (`_prepare`, `_lift`, or a backend). A trait no hook reads
  is caught by `test_every_profile_declares_only_known_traits`.

The traits vocabulary (`TRAITS`): `system_role`, `tool_mode`,
`reasoning`, `thinking`, `cache`, `context_window`, `max_output`,
`sampling`, `prompt_style`, `instructions_at`, `malformed_retries` —
values and defaults in the source, meaning in ADR-005 §1.3.

## Adding a model (or a backend)

1. **Entry.** `PROFILES["<name>"] = {"match": r"<model regex>",
   "traits": {…}}` — only the deviations. For a new host:
   `BACKENDS["<name>"] = {"path", "credential", "finish"?, "normalize"?}`
   and, if it is a well-known host, a `_HOST_BACKENDS` row.
2. **Unit.** The pure transforms, in `tests/test_llm_module.py`
   (parametrize `test_profile_matches_on_model_name`; a `finish` /
   `normalize` test per backend hook).
3. **Wire pin.** One recorded real reply per backend
   (`tests/fixtures/llm_<backend>.json`, `docs/llm-fixtures.md`).
4. **Parity.** Add the target to `TARGETS` in
   `tests/test_llm_parity.py` and record its golden trace:

   ```
   export ANYBAO_SECRET_LLM_KEY_OPENROUTER=<key>      # ANYBAO_SECRET_<ref, upper, dots→_>
   ANYBAO_LLM_PARITY=record uv run pytest tests/test_llm_parity.py -k <target> -s
   ```

   The conversation: a `run_cell` tool loop with canned results, an
   image part, a `length` stop, and a cache read on the second call
   for `cache: "markers"` entries. It writes
   `tests/fixtures/parity/<target>.json` (requests + responses, no key
   material — the credential is a ref + header). From then on the
   default `uv run pytest tests/test_llm_parity.py` replays it
   offline and fails on any request drift. **Supported = the golden
   trace is in the tree.**
5. **Loop check.** Point a rig's tier at the model
   (`cfg.set("llm.tier.codegen", {...})` from a cell, or the toml
   `[config]` table), deploy, text bao, read the trace
   (`docs/testing-agent-changes.md`).

## Supported entries

| target | profile | backend | parity recorded | notes |
|---|---|---|---|---|
| `anthropic-claude` | claude | anthropic | 2026-09-01 | markers: 5214 tokens written on call 1, read on call 2 |
| `anthropic-openai-compat` | claude | generic | 2026-08-31 | Anthropic's `/v1/chat/completions`: no cache reporting → effective `cache: auto` |
| `gemini-openai-compat` | gemini | gemini | 2026-08-31 | `thought_signature` rides the tool call's `provider_state` — required by Gemini 3.x |
| `openai-terra` (`gpt-5.6-terra`) | gpt | openai | 2026-09-01 | direct OpenAI: tools on chat/completions need tier `options {"reasoning_effort": "none"}` (a Responses-API backend is the proper fix); PDF ok as a native `file` part |
| `openrouter-claude` (`anthropic/claude-sonnet-5`) | claude | openrouter | 2026-09-01 | markers through OpenRouter: `cache_write_tokens` 5214 → `cached_tokens` 5214; `reasoning_details` (signed) resent |
| `openrouter-kimi-k3` (`moonshotai/kimi-k3`) | kimi-k3 | openrouter | 2026-09-01 | vision ok; implicit cache 3712 on call 2; may return the final answer under `reasoning` with `content` empty (+ leaked `<|close|>` trailer) — lifted to text; loop-verified on the :7005 rig (tool ids `run_cell:N`) |
| `openrouter-glm-5.3` (`z-ai/glm-5.3`) | glm-5 | openrouter | 2026-09-01 | **text-only** (`vision: false`; `glm-5v-*` is the vision line); implicit cache 3706; **PDF ok** via OpenRouter's parser (`cloudflare-ai`) |
| `openrouter-deepseek-v4` (`deepseek/deepseek-v4-pro-0813`) | deepseek-v4 | openrouter | 2026-09-01 | **text-only** (`vision: false`; `deepseek-v4-flash-vision-*` sees); implicit cache 3840; **PDF ok** via OpenRouter's parser |

**PDF** (ADR-020 §3, recorded 2026-09-01): every entry on the
`anthropic`, `openai` and `openrouter` backends reads the parity PDF
(`pdf_input` is a backend grant) — Anthropic as a `document`, OpenAI
as a native `file` part, OpenRouter as a `file` part parsed
server-side with the free `cloudflare-ai` engine pinned by llm@v1
(the host default is paid OCR; a tier's `options.plugins` overrides).
`gemini-openai-compat` and `anthropic-openai-compat` (generic backend)
refuse before any call.

All reasoning models on OpenRouter answer with `reasoning` +
`reasoning_details`; the `reasoning: "roundtrip"` trait resends the
details on the tool-result turn and every backend above accepted them.

Declared, not yet verified (no golden trace; needs a key / a host):
`openrouter-deepseek-r1` (no system role, reasoning round-trip),
`openrouter-qwen3`, `openrouter-gpt` (`max_completion_tokens`,
`reasoning_effort`), `openrouter-gemma-fenced` (```cell tool
carriage), `ollama-qwen3` (keyless local). Their profile traits are
model-card reads until the parity run says otherwise.
