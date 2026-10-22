# Skill: change and test bao's config from outside the app

For a coding agent (Claude Code, Codex, a script) that runs on the
user's computer and has `curl` and `jq`. It covers three things:

1. Find the address of the local `any` server the app runs.
2. Read and write bao's config rows, with a preflight before the write.
3. Test the change with one real chat turn.

The config is data the runtime reads on every model call. A write is
live for the next message. No restart, no file edit.

## 1. Find the API address

The desktop app runs an embedded `any` server on loopback. The app shows
its address under **Settings ▸ Preferences ▸ Local server ▸ Address**,
with a Copy button. That address is the `$A` used below. Ask the user
for it when you cannot read the screen.

The default is `http://127.0.0.1:31330`. The port is fixed per install,
so the address stays valid across launches. `ANY_DESKTOP_PORT` overrides
it for one launch. The browser build has no sidecar: the API is the
page's own origin under `/v1`.

Check that the address answers and that it is the right account:

```sh
A=http://127.0.0.1:31330
curl -s $A/v1/account | jq -r .metadata.name
```

## 2. What the config is

Bao keeps its config as rows of the `agent_config` dataset on one
object: the `bao/config/v1` child of the `bao/v1` bundle in the space
named `bao` (anybao ADR-006 §3, ADR-017 §4). One row per dotted key,
`{key, value}`. The rows that matter for a model switch:

| key | what it drives | row shape |
|---|---|---|
| `llm.tier.codegen` | chat, the main model | tier row |
| `llm.tier.classify` | background jobs: extraction, rollups, link generation | tier row |
| `llm.tier.vision` | file and image reads | tier row |
| `llm.tier.judge` | yes/no judging; unset or a TypeSafe Jev row | tier row |
| `search.provider.websearch` | the web search tool | `{provider, model, base_url, api_key_ref}` |
| `search.provider.deepresearch` | the deep research tool | same |

A tier row (ADR-005 §1.1):

```json
{"provider": "openai-compat",
 "model": "minimax/minimax-m3.1-flash:free",
 "base_url": "https://openrouter.ai/api/v1",
 "api_key_ref": "llm.key.openrouter"}
```

- `provider` is the wire family: `anthropic` or `openai-compat`.
- `base_url` is the API root. The runtime infers the backend from the
  host (OpenRouter, OpenAI, Gemini, DeepSeek, Groq, Together, vLLM,
  llama.cpp, Ollama) and the profile from the model name. Do not set
  `backend` or `profile` unless the host is unknown.
- `api_key_ref` names a credential row, never a key. `null` means a
  keyless local server.
- Optional: `options` (raw request fields merged last, for example
  `{"reasoning_effort": "none"}`), `stream: false` (exact usage from
  providers that stream no usage), `timeout` (seconds, or
  `{"idle": 60, "total": 900}`), `traits` (see the pitfalls).

API keys are **not** config. They live in the `agent_secrets` dataset
and the user enters them in **Settings ▸ Credentials** (ADR-021). Never
write a key into a config row. Never read the `value` field of a
secrets row. Section 4 shows how to check a key's status without it.

## 3. Resolve the config object

Four reads. Cache the ids for the session: they never change for a
space.

```sh
A=http://127.0.0.1:31330

# the bao space
SP=$(curl -s $A/v1/spaces | jq -r '(.spaces // .)[] | select(.name == "bao") | .id')

# the Agent Config type: it is hidden, so ask for hidden types
TID=$(curl -s "$A/v1/spaces/$SP/types?includeHidden=true" \
      | jq -r '.types[] | select(.xKey == "agent_config") | .id')

# the config object: a deterministic child of the bao/v1 bundle (idempotent)
OBJ=$(curl -s -X POST "$A/v1/spaces/$SP/bundles/bao%2Fv1/children" \
      -H 'Content-Type: application/json' \
      -d "{\"seed\": \"bao/config/v1\", \"type\": \"$TID\"}" | jq -r .objectId)

# the dataset's collection id (queries and writes take this, not the key)
COL=$(curl -s "$A/v1/spaces/$SP/types/$TID/datasets" \
      | jq -r '.datasets[] | select(.key == "agent_config") | .collection')
```

If `TID` is empty, the runtime has not provisioned this space yet. Stop.
Do not derive the child with another type: the seed is permanent and a
wrong type breaks the runtime's own datasets on it.

Read every row:

```sh
curl -s -X POST "$A/v1/spaces/$SP/query" -H 'Content-Type: application/json' \
  -d "{\"objectId\": \"$OBJ\", \"dataset\": \"$COL\"}" \
  | jq '.records | map({key, value})'
```

Save this output before any write. It is the rollback.

## 4. Preflight before the write

Do these checks in order. Refuse to write when one fails.

1. **Row shape.** `provider` is `anthropic` or `openai-compat`. `model`,
   `base_url`, `api_key_ref` are present. No key material anywhere.
2. **The key exists.** Read the secrets row's status, with a positive
   projection so the value never leaves the server:

   ```sh
   STID=$(curl -s "$A/v1/spaces/$SP/types?includeHidden=true" \
          | jq -r '.types[] | select(.xKey == "agent_secrets") | .id')
   SOBJ=$(curl -s -X POST "$A/v1/spaces/$SP/bundles/bao%2Fv1/children" \
          -H 'Content-Type: application/json' \
          -d "{\"seed\": \"bao/secrets/v1\", \"type\": \"$STID\"}" | jq -r .objectId)
   curl -s -X POST "$A/v1/spaces/$SP/query" -H 'Content-Type: application/json' \
     -d "{\"objectId\": \"$SOBJ\", \"dataset\": \"${STID}_agent_secrets\",
          \"filter\": {\"id\": \"llm.key.openrouter\"},
          \"projection\": {\"id\": 1, \"status\": 1, \"label\": 1, \"hosts\": 1}}" \
     | jq '.records'
   ```

   `status: "set"` is good. `status: "missing"` or no row: tell the user
   to add the key in Settings ▸ Credentials first. The row's `hosts`
   must contain the `base_url` host, or the runtime refuses to inject the
   key (ADR-021 §7).
3. **The model answers.** Make one tiny call to the provider with the
   same model id. Ask the user to run it with their own key, or run it
   with a key they hand you for this check only. Never read the key out
   of the secrets row.

   ```sh
   curl -s https://openrouter.ai/api/v1/chat/completions \
     -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
     -d '{"model": "minimax/minimax-m3.1-flash:free", "max_tokens": 8,
          "messages": [{"role": "user", "content": "Say ok."}]}' \
     | jq '{model, finish: .choices[0].finish_reason, error}'
   ```

   A 404 is a wrong model id. A 401 is a wrong key. For an Anthropic
   row the call is `POST https://api.anthropic.com/v1/messages` with
   the `x-api-key` and `anthropic-version` headers.

## 5. Write the rows

One `modify` call upserts any number of rows. The record id is the
dotted key. The two `$set` ops are the shape the runtime itself writes.

```sh
ROW='{"provider": "openai-compat", "model": "minimax/minimax-m3.1-flash:free",
      "base_url": "https://openrouter.ai/api/v1", "api_key_ref": "llm.key.openrouter"}'

curl -s -X POST "$A/v1/spaces/$SP/modify" -H 'Content-Type: application/json' -d "{
  \"objectId\": \"$OBJ\", \"dataset\": \"$COL\",
  \"records\": [
    {\"id\": \"llm.tier.codegen\",  \"upsert\": true, \"ops\": [
      {\"type\": \"\$set\", \"path\": \"key\",   \"value\": \"llm.tier.codegen\"},
      {\"type\": \"\$set\", \"path\": \"value\", \"value\": $ROW}]},
    {\"id\": \"llm.tier.classify\", \"upsert\": true, \"ops\": [
      {\"type\": \"\$set\", \"path\": \"key\",   \"value\": \"llm.tier.classify\"},
      {\"type\": \"\$set\", \"path\": \"value\", \"value\": $ROW}]},
    {\"id\": \"llm.tier.vision\",   \"upsert\": true, \"ops\": [
      {\"type\": \"\$set\", \"path\": \"key\",   \"value\": \"llm.tier.vision\"},
      {\"type\": \"\$set\", \"path\": \"value\", \"value\": $ROW}]}
  ]}" | jq '{recordIds, rejections}'
```

`recordIds` lists what was written. A `rejections` entry means nothing
of that record landed. Read the rows back (section 3) to confirm.

Rules:

- Write the **whole** value object. The runtime replaces the row, it
  does not merge.
- Change the three tiers together unless the user asks for one. A
  text-only chat model with an image-capable `vision` row on the same
  key is the normal split.
- After the write, Settings ▸ Agent ▸ Model shows **Custom** for rows no
  preset explains. That is expected.

## 6. Test with one chat turn

Post a message to the general chat of the bao space, tagged as an
agent, and wait for a reply whose `agent.done` is true. Bao answers an
agent-tagged message like a user's, and its model sees it marked as
automated.

```sh
CHAT=$(curl -s -X POST $A/v1/catalog/general-chat/setup \
       -H 'Content-Type: application/json' -d "{\"spaceId\": \"$SP\"}" \
       | jq -r '.bundles[0].bundle.rootId')
ME="$(curl -s $A/v1/account | jq -r .metadata.name) (claude)"
SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)

curl -s -X POST "$A/v1/spaces/$SP/objects/$CHAT/chat/messages" \
  -H 'Content-Type: application/json' \
  -d "{\"text\": \"which model are you running on right now? one line.\",
       \"agent\": {\"name\": \"$ME\"}}" | jq -c '{recordIds, rejections}'

# poll until a finished bao bubble arrives (a run can take a minute)
for i in $(seq 1 60); do
  R=$(curl -s -X POST "$A/v1/spaces/$SP/query" -H 'Content-Type: application/json' \
      -d "{\"objectId\": \"$CHAT\", \"dataset\": \"chat_messages\",
           \"filter\": {\"createdAt\": {\"\$gte\": {\"\$date\": \"$SINCE\"}}},
           \"projection\": {\"id\": 1, \"text\": 1, \"agent\": 1}}" \
      | jq -c '[.records[] | select(.agent.done == true and .agent.name != "'"$ME"'")] | last')
  [ "$R" != "null" ] && break
  sleep 3
done
echo "$R" | jq '{text, outcome: .agent.outcome, trace: .agent.debugLink}'
```

Read the result:

- A plain reply with no `outcome` means the run finished. The text
  should name the new model.
- `outcome: "error"` means the run broke. The usual causes after a
  config change are a rejected key, a wrong model id, or a bad row. The
  chat shows the error card. `debugLink` is the run id for
  `anyrt trace show` when the runtime's CLI is at hand.
- No bubble after a few minutes: bao's runtime is not running for this
  account, or it produced an empty final turn. Check the app's agent
  status before you retry.

This posts into the user's real chat and costs one model run. Say so
before you do it, and do it once.

## 7. Roll back

Write the saved rows from section 3 back with the same `modify` call.
The runtime picks them up on the next message.

## Pitfalls

- **Integers in `traits` break every call.** The store returns large
  integers as floats, and the runtime's trait check wants `int` for
  `context_window` and `max_output`. Leave `traits` out, or set only
  string and boolean traits in it. Profiles already carry the right
  integer values for known models.
- **`dataset` is the collection id, not the key.** A query with
  `"dataset": "agent_config"` returns an empty list, not an error.
  Canonical datasets such as `chat_messages` pass through by name.
- **Hidden types.** Without `includeHidden=true` the type list has no
  `agent_config`, and the derive call fails with "type required".
- **The config object's type is the runtime's.** Never derive the
  child with a guessed type. An empty `TID` means wait for the runtime.
- **No merge on write.** A row with only `model` in it loses
  `base_url` and `api_key_ref`.
- **Secrets are readable on loopback.** The account's own devices can
  read secret values over this API. Project the fields you need and
  never print a `value`.
- **The loop budgets once per run.** A write during a running
  conversation applies from the next message, not mid-run.

## Related

- `docs/llm-models.md`: supported models, profiles, how to add one.
- `docs/config-secrets.md`: how keys are stored and seeded.
- ADR-005 §1 (tier rows and traits), ADR-006 §3 (the config store),
  ADR-021 (credentials).
