# Seeding LLM fixtures (the "one real call" step)

Adapter translation is tested purely offline (test_llm_adapters.py):
recorded provider response → neutral parts, and neutral → request. The
*wire mapping* is locked by fixtures recorded from ONE real call per
provider; after that CI needs no API key.

## To (re)seed a provider fixture

1. `anybao llm-seed --provider anthropic --tier codegen`  (a tiny CLI
   that runs one live `llm.chat` with the real transport + your config,
   dumps the raw response to `harness/tests/fixtures/llm_<provider>.json`).
   [CLI lands with anyclient/config; until then run the transport by
   hand — see the transport signature in llm.py.]
2. Eyeball the JSON (no secrets: responses carry no keys; the request —
   which does — is NOT saved).
3. Replace the inline `*_RESP` dicts in test_llm_adapters.py with a load
   of the fixture, or keep both (inline = minimal contract, fixture =
   real shape).

## Why only one call

The neutral loop replays from the trace (llm.chat is an effect). A real
conversation trace, recorded once, is a golden test for the WHOLE loop
with zero further calls. The per-provider fixture here is narrower: it
pins the request/response *shape* so a provider-side wire change is
caught by a red translation test, not in production.

## When to re-seed

- A provider changes its wire format (new block type, renamed field).
- Adding a provider (new adapter → new fixture).
- The drift flow (ADR-004 §… analog for providers) can automate the
  "shape changed" signal later.
