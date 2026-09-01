# BOB-78 model questionary

Six questions that exercise the surfaces a preset model must handle
before it enters the onboarding picker: knowledge-base recall, type
creation, multi-type linking, image input, PDF input, and an
agent-authored program — each ending in real content created in the
space. Run the whole set as ONE conversation per model (the questions
build on each other), on the staging rig (`:7134` user / `:7021` repo
owner, `configs/anybao.staging.toml`, servers with `--config
configs/any-config-staging.yml`), in a per-model test space — never
the bao home space.

## The questions

Send each as its own chat message; wait for the run to finish before
the next.

**Q1 — types + property formats.**
> Create a type "Recipe" with properties: cuisine (select: italian,
> japanese, georgian), cook_minutes (number), source (url). Then add
> two recipes: "Cacio e pepe" (italian, 20 min) and "Khinkali"
> (georgian, 90 min), each with a short description and a plausible
> source link.

Pass: type exists with the right property formats (ADR-022), two
objects with values set, no malformed-tool retry loops.

**Q2 — multi-type + links.**
> Now create a "Meal plan" type where each plan links to several
> recipes (a "dishes" property) plus a date and a note. Make a plan
> for next Saturday with both recipes and a note about shopping.

Pass: second type with a multi-link property, plan object links to
both recipe objects (backlinks resolve), date is a real date value.

**Q3 — knowledge-base recall.**
> Which of my recipes can be cooked in under half an hour, and what
> meal plans use them? Answer from my space and link the objects.

Pass: answers from search/query over Q1–Q2 content (not from priors),
links both directions, no fabricated objects.

**Q4 — image → content.** Attach `fixtures/receipt.png`:
> Here's a photo of a grocery receipt. Create a "Shopping trip" note
> with the store name, date, the items as a checklist, and the total.

Pass: vision tier is exercised (check the trace: the image goes to the
`vision` tier model — on GLM that must be glm-5v-turbo), extracted
fields match the fixture, note object created.

**Q5 — PDF → content.**  Attach `fixtures/menu.pdf`:
> Read this PDF and create a page "Tasting menu notes" with a heading
> per course and a one-line comment each, then link it from the
> Saturday meal plan.

Pass: ADR-020 file input path works (http base64), page structure is
real blocks (not one text blob), link from the Q2 plan object exists.

**Q6 — agent-authored program (ADR-013).**
> Write me a simple weather program: it should fetch the current
> weather for Tbilisi from some open API that needs no key (e.g.
> open-meteo), and print temperature and wind. Save it so I can run it
> again, then run it and tell me the result.

Pass: program authored and stored (ADR-013 store, visible in the
space), the run's trace shows a real `http.get` to the open API, the
reported numbers match the effect result (not hallucinated), and the
program is re-runnable (bao names it / it exists as an object).

## Fixtures

Generated, not committed — put them in `tests/fixtures/bob78/`
(gitignore'd) or the scratchpad:

- `receipt.png` — render ~10 lines of a fake receipt (store name,
  dated items, total) as an image: `magick -size 400x600 -font
  DejaVu-Sans-Mono caption:@receipt.txt receipt.png` (or PIL).
- `menu.pdf` — a one-page, 5-course fake tasting menu; any
  text-to-pdf path works (pandoc, reportlab, libreoffice).

Same fixtures for every model — the comparison is only valid if the
inputs are identical.

## The model matrix

Baseline first; each config = the three `llm.tier.*` rows written to
the test bao space's `agent_config` (via `config@v1.set` from a
scratch run, or `datasets.modify`). `base_url`s: anthropic
`https://api.anthropic.com`, openrouter
`https://openrouter.ai/api/v1`, openai `https://api.openai.com/v1`,
gemini `https://generativelanguage.googleapis.com/v1beta/openai`.

| # | config | chat+utility / images | key ref |
|---|---|---|---|
| 1 | claude-sonnet (baseline) | claude-sonnet-5 + claude-haiku-4-5 utility / sonnet | `llm.key.anthropic` |
| 2 | claude-opus | claude-opus-5 + haiku utility / opus | `llm.key.anthropic` |
| 3 | openrouter-glm | z-ai/glm-5.3 / z-ai/glm-5v-turbo | `llm.key.openrouter` |
| 4 | openrouter-kimi | moonshotai/kimi-k3 / itself | `llm.key.openrouter` |
| 5 | openrouter-qwen | qwen/qwen3.8-max / itself | `llm.key.openrouter` |
| 6 | openai-terra | gpt-5.6-terra + gpt-5.6-luna utility / terra | `llm.key.openai` |
| 7 | gemini-flash | gemini-3.7-flash / itself | `google.key.gemini` |

Optional 8: openai-sol (gpt-5.6-sol, promo pricing) if terra
underwhelms.

Keys: `llm.key.anthropic` and `google.key.gemini` are already in
`configs/.connectors.env.off`; before the run add
`llm.key.openai=$(cat ~/any/openai.key)` and
`llm.key.openrouter=$(cat ~/any/openrouter.key)` lines there (file is
gitignored) and pass `--secrets-file`.

Between configs: rewrite the tier rows (read-through — next message
uses them, no restart), start a NEW conversation, delete the created
objects (or use a fresh scratch space per model) so Q3 can't see a
previous model's content.

## Reading results

Per run: `anyrt trace ls --addr http://127.0.0.1:7005 --program
toolcaller`, then `trace show … --stats` per conversation. Score each
question pass/fail + notes; collect per-conversation cost from
`--stats` (needs the pricing rows for glm-5v-turbo / qwen3.8-max /
gpt-5.6-* / gemini-3.7-flash / claude-opus-5 added to
`model_pricing.json` first, plus the two stale-row fixes — see the
draft doc). Also note: turn count, malformed-tool retries, whether the
model used search vs. fabricated (Q3), image/PDF fidelity (Q4/Q5).

Prereqs before config #3+: parity goldens are the formal gate
(glm-5v-turbo, qwen3.8-max id update, direct-openai target, opus-5)
— the questionary is the loop-level check on top, not a substitute.
