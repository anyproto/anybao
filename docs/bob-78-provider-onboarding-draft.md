# BOB-78 — provider choice in onboarding + settings (UX draft)

Draft, 2026-08-31. Bao part + any-ui part. Grounded in what shipped with
BOB-74 (openai-compat, PR #22) and ADR-021 (credential entry).

## The shape of the solution

Provider selection already exists as data: the `agent_config` rows
`llm.tier.{codegen,classify,vision}` (`{provider, model, base_url,
api_key_ref}`, ADR-005 §1.1), read through on every model call — no
restart, no file edit. Keys are `agent_secrets` rows keyed by ref, and
the serve already posts a credential-request chat card for whatever ref
the tier points at, with per-backend label/help (`llm@v1` BACKENDS carry
the OpenRouter/OpenAI credential specs already).

So the whole feature is: **presets, not free-form**. Choosing a
provider = the client writing three tier rows; everything downstream
(which key gets asked for, the card copy, rejection handling, backend
and profile inference by host/model-regex) already works. The runtime
needs approximately nothing for onboarding; the work is any-ui
presentation plus a small settings surface.

## v1 preset matrix

A preset = **one key** and a coherent tier triple: the main model on
`codegen` + `classify`, and — when the main model is text-only
(`vision: False` in its profile: glm-5, deepseek-v4) — a paired
vision-capable model **on the same key** for the `vision` tier. Never
a second key: the user picked the preset because that's the key they
have.

| preset | codegen + classify | vision | key ref | status |
|---|---|---|---|---|
| **Claude (recommended)** | claude-sonnet-5 / claude-haiku-4-5 | claude-sonnet-5 | `llm.key.anthropic` | default today; loop-tested in prod |
| Claude — Opus | claude-opus-5 / claude-haiku-4-5 | claude-opus-5 | `llm.key.anthropic` | model chip on the Claude preset; needs a parity golden + pricing row |
| **OpenRouter — GLM 5.3** | z-ai/glm-5.3 | z-ai/glm-5v-turbo (in-family; falls through `glm-5(?!v)` to the vision-capable `glm` profile) | `llm.key.openrouter` | glm-5.3: parity golden + rig loop run (shellbugs.md); glm-5v-turbo: needs a parity golden + pricing row |
| OpenRouter — Kimi K3 | moonshotai/kimi-k3 | itself (profile is vision-capable) | `llm.key.openrouter` | parity golden; no loop eval yet — behind the "more models" disclosure |
| Google Gemini | gemini-3.7-flash | itself | `google.key.gemini` | loop-tested 6/6 — fastest+cheapest full pass ($0.19); requires llm@v1 `signed_tool_calls` trait (staging-deployed, prod pending); bonus: same key powers `search.provider.*` |
| OpenRouter — Qwen 3.8 Max | qwen/qwen3.8-max | itself (multimodal; matches the existing `qwen3` profile) | `llm.key.openrouter` | replaces DeepSeek in the picker (user call 08-31); `openrouter-qwen3` parity target exists — update its model id + golden; `qwen3` profile's `context_window: 32768` needs bumping to 1M |
| OpenAI — GPT-5.6 | gpt-5.6-terra (chat+images) / gpt-5.6-luna (utility) | terra (vision-capable) | `llm.key.openai` | loop-tested 6/6 (questionary report); tier rows MUST carry `options: {"reasoning_effort": "none"}` (chat/completions rejects tools with reasoning on) until a Responses-API backend lands; `openai-terra` golden recorded; Sol as the "strongest" chip |

Pricing snapshot ($/M in / out, 2026-08-31; Anthropic direct rates,
OpenRouter rows from its live model listing):

| model | in | out | ctx | vision |
|---|---|---|---|---|
| claude-sonnet-5 | 2.00 | 10.00 | 1M | yes |
| claude-haiku-4-5 | 1.00 | 5.00 | 200K | yes |
| claude-opus-5 | 5.00 | 25.00 | 1M | yes |
| z-ai/glm-5.3 | 1.40 | 4.40 | 1.3M | no |
| z-ai/glm-5v-turbo | 1.20 | 4.00 | 203K | yes |
| moonshotai/kimi-k3 | 3.00 | 15.00 | 1M | yes |
| qwen/qwen3.8-max | 2.00 | 6.00 | 1M | yes |
| gemini-3.7-flash | 0.75 | 3.75 | 1M | yes |
| gpt-5.6-terra (OpenAI direct) | 2.00 | 12.00 | 1M | yes |
| gpt-5.6-sol (higher) | 4.00 promo / 5.00 | 20.00 promo / 30.00 | 1M | yes |
| gpt-5.6-luna (utility) | 0.20 | 1.20 | 1M | yes |

**Prices are subject to change** — this table is a 2026-08-31
snapshot. Current rates: anthropic.com/pricing, openai.com/api/pricing,
openrouter.ai/models (per-model pages),
ai.google.dev/pricing. `runtime/src/model_pricing.json` must track
them — trace cost stats price from it. If the onboarding card shows
prices at all, they need the same caveat or a build-time refresh;
alternatively the card shows only relative cost hints ($ / $$ / $$$)
and leaves exact rates to the provider pages.

Two stale rows found in `runtime/src/model_pricing.json` while
compiling this: `claude-sonnet-5` is priced 3.00/15.00 there but
Anthropic's current rate is 2.00/10.00, and
`deepseek/deepseek-v4-pro-0813` carries the batch rate (1.32/3.96)
instead of the interactive 0.66/1.98. Fix alongside the new rows
(glm-5v-turbo, gemini-3.7-flash, claude-opus-5).

**OpenAI native** (researched 08-31): the current lineup is generation
5.6 with three durable capability tiers — **Sol** (flagship; $4/$20
promotional through 2026-11-21, $5/$30 standard), **Terra** (balanced,
$2/$12 — the sonnet-priced one), **Luna** (fast/cheap, $0.20/$1.20 —
the haiku analog). Preset: chat+images on Terra, utility on Luna, Sol
as the "strongest" chip. Base URL `https://api.openai.com/v1`, key ref
`llm.key.openai` (the `openai` backend + host inference already
exist). All three match the `gpt` profile regex; its `context_window:
128000` trait predates these 1M models — bump it when adopting. Gate:
a direct-OpenAI parity target (none exists — `openrouter-gpt` is also
unverified) + a loop eval, per the add-a-model recipe.

Other sonnet-band OpenRouter candidates considered for the DeepSeek
slot: x-ai/grok-4.6 ($2/$6, 500K, vision — no `grok` profile exists,
new-family work) and mistralai/mistral-medium-3-5 ($1.50/$7.50,
`mistral` profile exists). Qwen won on: existing `qwen3` profile,
existing declared parity target, self-sufficient vision, $2/$6.

**OpenAI (direct) is not in v1.** The ticket assumed "openai and
openrouter with glm", but `openrouter-gpt` sits on the
declared-but-unverified list and there is no direct-OpenAI parity
target at all. Adding it is the documented add-a-model recipe
(docs/llm-models.md §61-91: parity golden + a loop run) — gate the
preset on that, don't ship it untested. Pricing rows exist for
GLM 5.3/Kimi K3/DeepSeek V4; gemini-3.7-flash and glm-5v need rows in
`model_pricing.json` before they ship (unpriced models degrade trace
stats, not runs).

Vision-pair caveat to state in the ADR when this lands: the vision
tier's model differs from codegen's, so an image-heavy conversation
mixes model voices. Acceptable — vision calls are description
sub-calls, not the loop driver.

## Onboarding: the chat card grows a provider choice

Onboarding stays purely in chat (there is no product wizard today and
this doesn't add one). Fresh account → lands in Bao's general chat →
first run dies on SecretMissing → serve posts the `credential_request`
card for `llm.key.anthropic`, exactly as now.

**Wire contract** — it is already "a chat record with a special type":
the message carries `attachments: {credreq: {type:
"credential_request", link: "any://o/<secretsObj>?key=<ref>"}}` and no
payload; the client branches on the attachment type and renders the
card, whose content comes from the live `agent_secrets` row. The
onboarding variant keeps that type and adds one marker field on the
attachment:

```json
{"credreq": {"type": "credential_request",
             "link": "any://o/<secretsObj>?key=llm.key.anthropic",
             "setup": "model"}}
```

A client that knows the field renders the provider chooser; an old
client ignores it and renders today's plain Anthropic key card — the
default path still works. (A brand-new attachment *type* would instead
degrade to the unknown-attachment chip, which is why the marker rides
the existing type.)

**How the runtime decides** — in `post_credential_requests`, which
already fires when a run dies on SecretMissing. Set `setup: "model"`
when both hold:

1. the missing ref is the active codegen tier's `api_key_ref`
   (an `llm.key.*` ref), and
2. every `llm.tier.*` row in `agent_config` still equals the embedded
   defaults — `config_defaults.json` is `include_str!`'d into the
   binary, so the comparison is local. A row seeded by a toml
   `[config]` table or written by a previous choice differs, and the
   plain card is posted instead.

"Never chosen" is thus derived from state, not tracked — no marker
key, nothing to reset, and re-running onboarding is just deleting the
tier rows. Dedup, rejection, and superseded flows are untouched: same
message kind, same `requestedIn` stamps. The preset catalog (models,
prices, help links) lives in the client in v1 — the message stays
payload-free per ADR-021, and presets are presentation.

```
┌──────────────────────────────────────────────────┐
│ Bao needs a language model to run.               │
│                                                  │
│ ◉ Claude — recommended                           │
│    Sonnet 5 · needs an Anthropic API key         │
│    (workspace-scoped — see help link)            │
│ ○ OpenRouter                                     │
│    GLM 5.3 ▾ · one key, many models              │
│                                                  │
│ Anthropic API key            [Where to get it ↗] │
│ [ paste the key…                        ] [Save] │
└──────────────────────────────────────────────────┘
```

Selecting OpenRouter swaps the key field's ref/label/help in place
(model select shows GLM 5.3 default, Kimi/DeepSeek under ▾, with a
"no image understanding" note on text-only models). On Save:

1. If a non-default preset: one `datasets.modify` on `agent_config` —
   `$set value = {provider, model, base_url, api_key_ref}` per tier
   row, `upsert: true`. Minimal rows only; `backend`/`profile` stay
   inferred.
2. The existing `setSecret` write to `agent_secrets` for the chosen
   ref (`$set value`, `status:"set"`, `$unset requestedIn`).
3. The existing `credential_set` user message → responder picks the
   run back up; `llm@v1` re-resolves the tier on the next call and hits
   the new provider immediately.

Notes that fall out for free:

- **Old clients degrade gracefully**: they render the same message as
  today's plain Anthropic card — the default path still works.
- **Rejection flow unchanged**: a 401 on the new key posts a rejected
  variant card for *that* ref (the OpenRouter help link comes from its
  BACKENDS credential spec via the secret row's about-fields).
- **One runtime nit**: the card message text is written by serve
  ("I need a credential to continue: Anthropic API key…"). The
  provider-choice variant replaces the heading client-side, so no
  change needed — but if we want the *text fallback* (old clients,
  notifications) to read provider-neutral on the first-ever request,
  that's a one-line copy tweak in `post_credential_requests`. Optional.

## Settings: a "Model" entry in the agent mini-apps

The ticket wants "a settings screen which shows current setup".
Account Settings (`SettingsView`) is the wrong home — tier rows live in
the bao space, next to Soul / Scheduled / Skills / Credentials. Add a
fifth `AGENT_MINI_APPS` entry, **Model** (single view, no sidebar;
standard four-point view registration + `SettingsSection` components):

```
Model
─────────────────────────────────────────────
Provider        Claude (Anthropic)   [Change ▾]
Model           claude-sonnet-5
Key             llm.key.anthropic  ● set        → opens Credentials
─────────────────────────────────────────────
Tiers                                 (collapsed by default)
  codegen   anthropic · claude-sonnet-5 · api.anthropic.com
  classify  anthropic · claude-haiku-4-5 · api.anthropic.com
  vision    anthropic · claude-sonnet-5 · api.anthropic.com
─────────────────────────────────────────────
Changes apply from the next message.
```

- **[Change ▾]** offers the same presets as the onboarding card; if the
  target key ref is unset it asks for the key inline (same write
  sequence as the card, minus the chat message — a `credential_set`
  message is still posted to the general chat, mirroring what the
  Credentials dashboard save does today).
- If the current rows match no preset (someone used `config@v1.set` or
  a toml `[config]` seed), show **"Custom"** and render the tiers
  read-only-expanded. v1 does not need a raw JSON editor — `config@v1`
  and the toml remain the power path.
- Data layer needed: an `agent_config` read hook + anchor resolution by
  seed, sibling to `src/lib/sync/credentials.ts` (same pattern, no
  value-stripping needed — config values aren't secret).
- "Applies from the next message" is precise: `llm@v1` resolves
  per-call, but the loop fixes traits/budgets once per run
  (`toolcaller@v1` run start), so mid-run switches half-apply — the
  copy sidesteps ever promising that.

## Should bao itself change the setting?

Not in v1 — agreed it's fragile. `config@v1.set_model` already exists
for the agent; instead of building guided switching, add one line to
the `_any` skill: when asked to switch models, point the user at the
Model mini-app (or the onboarding card if no key is set) rather than
editing `llm.tier.*` rows itself. Keeps the write path singular.

## Task split

**anybao (small):**
- `setup: "model"` marker in `post_credential_requests` (missing ref =
  codegen tier's `api_key_ref` + tier rows equal embedded defaults)
- optional: provider-neutral first-request card text in
  `post_credential_requests`
- `_any` skill line about model switching
- before OpenAI preset ever ships: direct-OpenAI parity target + loop
  eval (add-a-model recipe)

**any-ui (the bulk):**
- `agent_config` sync layer (anchor by seed + hooks)
- provider-choice variant of `CredentialRequestCard` (presets, model
  select, ref-swapping key field)
- Model mini-app (view registration ×4 + `AGENT_MINI_APPS` entry)
- preset write helper shared by card + Model view

## Open questions

1. ~~Vision tier under a text-only model~~ — resolved: text-only main
   models get a same-key vision pair (GLM → glm-5v; DeepSeek → glm-5v
   or gemini-flash via openrouter). Remaining: pick the exact glm-5v
   model id and cut its parity golden + pricing row.
2. **Which presets in the picker for v1?** Recommendation: Claude +
   OpenRouter (GLM 5.3 default; Kimi K3 as a sibling chip) + Gemini.
   No test-status badges in the UI (user call, 08-31) — everything in
   the picker gets tested before ship; parity goldens + loop evals are
   the gate, tracked here, not surfaced to the user. Gemini is the
   only preset that also covers the search providers with the same
   key.
3. **Key validation on save?** Implicit today (next run either works or
   posts a rejected card). A "test key" ping from the client is v1.1
   at most.
4. **Search providers** (`search.provider.*` → gemini key) are
   untouched by this: a websearch will still ask for the Gemini key via
   its own card even on an OpenRouter preset. Fine, but the Model view
   maybe shouldn't pretend to be the complete model picture — or it
   grows a second section later.
