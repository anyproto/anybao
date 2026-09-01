# BOB-78 — next session plan

Written 2026-08-31 end-of-session. Everything referenced is in the
repo or still running; nothing needs re-derivation.

## Where things stand

- UX draft: `docs/bob-78-provider-onboarding-draft.md` (wire contract,
  presets, Model mini-app, task split).
- Interactive mock: https://claude.ai/code/artifact/ae772a69-f650-41b6-bf32-4bc75c6804fd
- Bench report: `docs/bob-78-questionary-report.md` + the readable
  artifact version (link in chat). ALL seven configs pass; gemini and
  gpt-5.6 were unblocked in-session.
- UNCOMMITTED on main: llm@v1 `signed_tool_calls` + ADR-005 §1.3 line;
  `openai-terra` parity target + golden fixture; `model_pricing.json`
  (2 fixes + 7 new rows); the four bob-78 docs; `tests/fixtures/bob78/`
  fixtures. llm@v1 deployed to STAGING repo only.
- Staging rigs RUNNING: any `:7134` (user) / `:7021` (repo owner),
  serve control `:7016`. Spaces `bob78b-*` (valid runs) and `bob78-*`
  (invalid all-sonnet round) retained.

## 1 — Review + decisions (start here)

Read the report, then decide:

1. **Picker lineup v1.** Proposal (all live-verified now):
   Claude (Sonnet default / Opus chip) · OpenRouter (GLM 5.3 default /
   Kimi K3 / Qwen 3.8 Max) · OpenAI (Terra default / Sol chip, luna
   utility, `options.reasoning_effort="none"` in the rows) ·
   Gemini (3.7 Flash — cheapest+fastest full pass, same key covers
   search providers). Trim or keep all four?
2. **Commit + push the working-tree set** (suggested split, one topic
   each): (a) llm@v1 signed_tool_calls + ADR-005 + openai-terra
   target/golden; (b) model_pricing.json; (c) bob-78 docs + fixtures.
3. **Prod repo deploy** of llm@v1 via `:7003` by raw id (after
   merge), then wait-for-sync before touching the owner.
4. **PDF path decision** (report finding #1 — the one red cell):
   zlib into the kernel allowlist vs `file_content` PDF-text
   extraction host-side vs vision-tier page images. Cheapest is zlib;
   cleanest is extraction. Pick one, it unblocks q5-class tasks for
   every openai-compat model.
5. **File the findings** (report has 10): which go to Linear/dev
   space — top candidates: anyrt-run config.set process-local
   divergence; append_turn failure marking a completed run FAILED;
   cross-space chat triggers dead; create_space missing general-chat
   bundle; no url property format.
6. **Rigs**: stop the staging trio or keep for the UI work (the UI
   task wants them up — recommend keep).

Deferred/background: Responses API backend for OpenAI (restores
reasoning+tools); goldens for glm-5v-turbo + qwen3.8-max (id update)
+ opus-5; gemini golden re-record with the sentinel.

## 2 — UI task (any-ui), in build order

Grounding: `CredentialRequestCard.tsx`, `src/lib/api/credentials.ts`,
`src/lib/sync/credentials.ts` (patterns to mirror),
`src/app/spaceMiniApps.ts` + viewModules/view-state/routing (4-point
registration), `SettingsSection`/`SettingsRow` components.

1. **Presets module** (pure data, single source of truth):
   providers → chips → the three tier rows each (incl. OpenAI's
   `options`), key ref, host, help URL, relative price hint. Mirrors
   the bench's `configs.json`.
2. **agent_config data layer**: sibling to `sync/credentials.ts` —
   anchor = `bao/config/v1` child of the `bao/v1` bundle, dataset
   `agent_config`; read hook + a `applyPreset()` write helper
   (three per-path `$set value` upserts via `datasets.modify`).
3. **Card variant**: `CredentialRequestCard` grows the provider-pill
   variant — v1 gate: render it when the ref is `llm.key.*` AND tier
   rows equal defaults (client-side; the runtime `setup:"model"`
   marker can replace the heuristic later). Save = applyPreset +
   existing setSecret + credential_set message (ref follows the
   chosen preset).
4. **Model mini-app**: `AGENT_MINI_APPS` entry `model` + the 4-point
   view registration; single view — current preset card, Change ▾
   (same presets module), collapsed per-tier rows, key status dot
   linking into Credentials, "Changes apply from the next message."
   Custom (non-preset rows) renders read-only.
5. **anybao side, small**: `setup:"model"` marker in
   `post_credential_requests` (+ provider-neutral first-request
   text); one `_any` skill line — model switching goes through the
   Model app, agent shouldn't edit `llm.tier.*` unprompted.
6. **E2E on the staging rig**: fresh account against `:7134`
   (`VITE_API_TARGET`), walk the card for each preset, verify tier
   rows + key land and the next message runs on the chosen model
   (trace `llm:` line — the bench's verification trick).

Estimate: 2–4 obey the existing patterns closely; the card variant is
the only genuinely new UI. anybao PR small; any-ui PR is the bulk.
