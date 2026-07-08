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
ls -t traces/ | head                          # newest runs

# human render: status/fuel/wall-time header, #turn_N blocks
# (turns = llm.chat spans) with the user/assistant text, cell CODE,
# stop_reason + tokens + cacheRead, per-cell effects (mutations
# marked *), and the tool_result digest the model saw. Errors are
# never clipped.
anyrt trace show traces/run_<id>.jsonl
anyrt trace show traces/run_<id>.jsonl --full      # lift all clips
anyrt trace show traces/run_<id>.jsonl --system    # + system prompt
anyrt trace show traces/run_<id>.jsonl --seq 42    # one record, full,
                                                   # blob-resolved

# the metrics view over a directory: fuel/duration/token
# distributions (p50/p95), effect histogram, tuning suggestions
anyrt trace stats traces/
```

Clipped lines are locators, not the payload — every effect line prints
its `#seq`; drill down with `--seq N` (or jq below).

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

## If you want a run in the UI

The designed escape hatch is an on-demand **promote** — upload one
trace as an `any` object with file attachments (the AnyFileStore slot,
ADR-006 §1). Opt-in per trace, never the default; unbuilt until wanted.
