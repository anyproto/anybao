# Debugging runs — where the logs went

There are no debug-log pages in the UI. The trace is the single source
of truth for a run, and traces are **device-local** (ADR-006 §1): a
trace is big and rarely read, so it lives as JSONL on the machine that
ran it, never as synced objects. What syncs is the lean layer — every
turn in `agent_turns` carries `userText`, `replies`, llm scalars, and a
`traceRef` naming the local trace file.

Traces land in `--traces-dir` (default `traces/` relative to where
`anyrt` runs), one `run_<id>.jsonl` per run, plus a `.blobs` sidecar
when values spill (ADR-001 §7). The file streams **in-flight**: the
header lands at run start and every record appends as it commits, so
`tail -f` works on a live run and a crashed run leaves a partial trace
(`trace show` reports it as `status: incomplete`).

## The tools

```sh
# the run finder: one row per run, newest first — time (file mtime),
# run id, program, status, duration, turn count, and turn 1's user
# text as the title. --program filters the cron noise out.
anyrt trace ls                                # 30 newest, all programs
anyrt trace ls --program toolcaller           # just conversations
anyrt trace ls -n 0                           # everything

# human render, chronological — nothing in the trace is invisible:
# status/fuel/wall-time header, a boot: line naming what turn 1 fed
# the model (system prompt size, boot-window message count, tool
# names), loose effects in place, #turn_N blocks (parentless llm.chat
# spans) with the user/assistant text, cell CODE, stop_reason +
# tokens + cacheRead, per-cell effects (mutations marked *), the
# tool_result digest the model saw, and facade spans
# (~ autorecall.plan, ~ memory.save_with_dedup) as a header line with
# their effects + nested llm calls indented beneath. Errors are never
# clipped.
anyrt trace show run_<id>            # bare ids resolve against traces/
anyrt trace show run_<id> --full     # lift all clips (also inlines the
                                     # boot window at turn 1)
anyrt trace show run_<id> --system   # + system prompt text
anyrt trace show run_<id> --boot     # + boot window verbatim (history
                                     # tail + injected context)
anyrt trace show run_<id> --seq 42   # one record, full, blob-resolved

# per-RUN cost/usage table: one row per turn — stop, in, cacheRead,
# cacheWrite, out, cells, effects, llm ms — with totals and costUsd.
# Prices come from runtime/src/model_pricing.json keyed by the model
# the trace recorded (per-MTok USD; edit the json to change rates;
# unknown models render '-').
anyrt trace show run_<id> --stats

# the metrics view over a DIRECTORY: fuel/duration/token
# distributions (p50/p95), effect histogram, tuning suggestions
anyrt trace stats traces/
```

Clipped lines are locators, not the payload: content clips end
`… (+N chars — --full)` so you know how much is hidden, every effect
line prints its `#seq`, and result blocks name the record they were
mined from (`result (mined from #77):` — the provider request right
after the cell). Drill into any of them with `--seq N` (or jq below).

Raw access is just JSONL — one record per line (never pretty-print the
file itself):

```sh
jq . traces/run_<id>.jsonl | less
jq 'select(.kind=="effect" and .meta.class=="mutate")' traces/run_<id>.jsonl
jq 'select(.kind=="span" and .phase=="begin")' traces/run_<id>.jsonl
```

## Chat reply → its trace

```sh
curl -s -X POST http://127.0.0.1:7001/v1/spaces/$SPACE/query \
  -H 'Content-Type: application/json' \
  -d "{\"objectId\": \"$CHAT\", \"dataset\": \"agent_turns\",
       \"sort\": [\"-seq\"], \"limit\": 1}" | jq -r '.records[0].traceRef'
```

Turns written by serves older than 2026-07-18 carry refs that name no
trace file (serve minted two ids per run back then); for those, map a
reply to its trace by time + title: `anyrt trace ls --program
toolcaller` (turn 1's user text is the row title).

The agent can introspect its own runs from inside a conversation too —
`trace.effects_of` / `trace.effect_get` are syscalls — so asking bao
"why did you do that?" in chat works.

## Record kinds (ADR-001)

- `header` — run id, program, schema.
- `effect` — one syscall crossing: input (canonical), `key`
  (sha256 — the replay identity), output/error, `meta.class`
  read|mutate, `span` stamp when inside a span.
- `span` begin/end — guest-declared grouping: `llm.chat` spans are
  turns, `cell` spans are model cells, facade spans (e.g.
  `autorecall.plan`) collapse composites to one line.
- `cell` — the host cell verdict: ok/interrupted + fuel/duration.

A property write's autoresolutions (ADR-022 §2) are readable in the
trace without decoding the wire: the `any.update_object` /
`any.create_object` span result carries `resolved` (`{"task.Status":
"in_review"}` — what the model's words became) and `createdOptions`
(options minted on the way); the PATCH on `…/properties/<propId>`
right before the `set` is the minting itself.

## If you want a run in the UI

The designed escape hatch is an on-demand **promote** — upload one
trace as an `any` object with file attachments (the AnyFileStore slot,
ADR-006 §1). Opt-in per trace, never the default; unbuilt until wanted.
