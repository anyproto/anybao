---
name: check-anybao-changes
description: End-to-end check of an anybao change against the live agent — deploy programs/skills to the bao space, text bao a message that exercises the change, then read the resulting toolcaller trace to confirm the new behavior. Use after changing programs/, skills/, or runtime code when you want to see it working in a real conversation (not just tests).
---

# check-anybao-changes — verify a change against the live bao agent

Drives a change through the real loop: **deploy → message bao → read the
trace**. Assumes the local `any` server is up on `127.0.0.1:7001` and the
`bao` serve is running (the `rt` tmux pane: `anyrt serve --space bao`).
`anyrt` = `runtime/target/release/anyrt`.

## 1. Build + deploy

If runtime (Rust) changed: `make runtime`. Then publish programs + skills
to the bao space (hash-gated — the running serve picks up **program**
changes on its next run):

```
runtime/target/release/anyrt deploy --addr http://127.0.0.1:7001 --space bao
```

Look for the changed unit in the output (`"any@v1": "updated"`,
`"_any": "updated"`).

**No restart needed for programs OR prompt text.** The system prompt is
composed guest-side by `toolcaller` at the start of EVERY conversation
(skills, tool docs, user skills — all queried live from the space), so
deployed skill/prompt-text changes reach the model on the next message,
same as program changes (verified 2026-08-06: a skill edit showed up in
the very next run's `--system` with no restart). Restart the serve (the
`rt` tmux pane) only when the frozen-at-startup things change: the
`anyrt` binary itself, config/secrets, or overlay membership.

## 2. Text bao a message that exercises the change

Resolve the bao space id and its derived general chat, then POST a user
message (no `agent` field — that's what makes the watcher treat it as
user input and start a toolcaller run):

```
BAO=$(curl -s http://127.0.0.1:7001/v1/spaces | \
  python3 -c "import sys,json;print([s['id'] for s in json.load(sys.stdin)['spaces'] if s['name']=='bao' and s.get('status')=='active'][0])")
CHAT=$(curl -s http://127.0.0.1:7001/v1/spaces/$BAO | \
  python3 -c "import sys,json;print(json.load(sys.stdin)['generalChatObjectId'])")
curl -s -X POST "http://127.0.0.1:7001/v1/spaces/$BAO/objects/$CHAT/chat/messages" \
  -H 'content-type: application/json' \
  -d '{"text":"<a prompt that forces the changed code path>"}'
```

Write the prompt to actually hit the change — e.g. for a search change,
"search the space for X and list title + type of each hit"; for a
config/tier change, something that makes an LLM call. Keep it concrete so
bao can't sidestep it.

## 3. Find + read the resulting trace

The whole user-message→reply exchange is ONE `toolcaller@v1` trace,
titled by the message (cron jobs outnumber conversations, so filter):

```
runtime/target/release/anyrt trace ls --program toolcaller   # newest first
runtime/target/release/anyrt trace show run_<id> --full      # the whole story
```

Verify the change in the trace: the cell that runs the new code + its
`result:` block, the relevant effects (`req:`/`resp:` under `--full`), and
bao's final `assistant:` reply. For a client-side data change (like search
enrichment) the proof is in the **cell result**, not the raw effect —
the effect shows the wire response, the cell shows what the enriched
client returned.

## Notes

- The run is async: after POSTing, give it a few seconds, then
  `trace ls`. A 0-turn trace or missing run means the serve didn't pick
  it up (check the `rt` pane is alive and watching).
- Full trace guide: `docs/debugging.md`; the analyze-a-run recipe is in
  `CLAUDE.md`.
