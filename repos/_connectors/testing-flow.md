# Connector test flow — question checklist against the live agent

The loop we use to harden one connector at a time (established on
github@v1, 2026-07-28): unit tests pin the shapes, then a per-method
question checklist drives the REAL agent on the test rig, and every
friction found in the trace becomes a task. Analysis only — fixes are
applied when the user flags them.

## Where things live

- **Test rig** (anybao `docs/testing-agent-changes.md`): any server
  `127.0.0.1:7009` (persistent account, data dir
  `~/any/any-test-7009`), serve control port `7011`, traces in
  anybao's `traces-test/`. Connectors overlay `_connectorsrepo` is
  owned by the repo account on `127.0.0.1:7003`.
- **Question checklist**: page "github methods — question checklist"
  in the dev space (`:7001`), nav folder `connectors > github` — one
  `- [ ]` item per method: the message to send bao + the method it
  exercises.
- **Findings**: tasks in that same nav folder (per-connector series:
  G1, G2, …); cross-connector decisions in the parent `connectors`
  folder (C1, …); agent-loop/kernel themes go to the `anybao polish`
  folder (E-series).

## The loop, per question

```sh
cd ~/any/anybao   # anyrt lives here

# 0. after each connector edit: unit tests + deploy (hash-gated,
#    a running serve picks it up on the next conversation)
uv run pytest repos/_connectors/tests
./runtime/target/release/anyrt deploy --addr http://127.0.0.1:7003 \
    --source repos/_connectors --target <_connectorsrepo space id>

# 1. wipe the rig chat history — each question starts from a clean
#    slate so behavior measures the model's priors + our docs, not
#    lessons carried in chat history (chat_messages, agent_turns,
#    agent_chunks via POST /v1/spaces/:id/delete-records; needs the
#    any server built with author-only agentlog deletes, any PR #139).
#    ALWAYS check the `rejections` array in the response — refusals
#    come back there, not in the HTTP status.

# 2. send the question as the user (no `agent` field = user input)
curl -s -X POST "http://127.0.0.1:7009/v1/spaces/$BAO/objects/$CHAT/chat/messages" \
  -H 'content-type: application/json' -d '{"text":"<question>"}'
# $BAO = bao space id, $CHAT = its general chat (general-chat/v1 bundle root)
# (GET /v1/spaces, then GET /v1/spaces/$BAO)

# 3. wait for the run, then read the whole story
./runtime/target/release/anyrt trace ls traces-test --program toolcaller
./runtime/target/release/anyrt trace show traces-test/run_<id>.jsonl  # --full
```

## Analyzing the trace

Look for, in order: wasted turns before the right call (name/kwarg
guesses — count them, they recur every fresh context); the request
that hit GitHub (right endpoint, params, one call not N); silent
`None`s (bao `.get()`s a field the shape doesn't carry — worse than an
error, nothing prompts recovery); the error contract on failures
({ok: false, error} with an actionable message, never a traceback);
and the final answer's faithfulness to the data.

File each distinct friction as a task (next free id in the folder's
series) citing the run id + turn; append repeat evidence to the
existing task instead of duplicating. Tick the checklist item
surgically — flip the block's `style.checked` via `modify` on the
`editor_blocks` dataset, don't rewrite the page markdown.

## Shape rule (C1)

Trim, don't rename: kept result fields carry the upstream API's exact
names and nesting; derived keys only where upstream has no scalar
(`repo`, `is_pr`, decoded `text`, `kind`). Verified A/B on github@v1:
renamed fields cost a keys()-discovery turn per fresh context
(run_c538db5b6ab84b89) or silent Nones (run_b54f557ac0ce433a);
API-named shapes were consumed correctly first try
(run_d5cb038d1d1941f1, run_8bb9081c38874c4a).
