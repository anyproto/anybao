# Seeding LLM fixtures (the "one real call" step)

Adapter, backend and profile transforms are tested purely offline
(`tests/test_llm_module.py`): recorded provider response → neutral
parts, and neutral → request. The *wire mapping* is locked by fixtures
recorded from ONE real call per backend; after that CI needs no API
key. The per-model *conversation* is locked by the parity golden traces
(`docs/llm-models.md`).

## To (re)seed a provider fixture

The fixtures under `tests/fixtures/llm_<provider>.json` are raw provider
responses. To re-record one, make a single live call and save the raw
JSON body — e.g. against Anthropic:

```
curl -s https://api.anthropic.com/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-sonnet-5","max_tokens":64,
       "messages":[{"role":"user","content":"say hi"}]}' \
  > tests/fixtures/llm_anthropic.json
```

Eyeball the JSON before committing (responses carry no keys; the request
— which does — is not saved). The OpenAI-compatible fixture is the same
shape from a `/chat/completions` call.

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
