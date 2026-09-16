# Debugging runs — where the logs went

There are no debug-log pages in the UI. The trace is the single source
of truth for a run, and trace **bodies are device-local** (ADR-023): a
serve writes them into the any server's local store — three
never-synced collections of the bao space (`trace_records`,
`trace_blobs`, `trace_runs`, `docs/adr/023-…`). What syncs is the lean
layer: every turn in `agent_turns` carries `userText`, `replies`, llm
scalars and a `traceRef` = the run id, and every run leaves one
`agent_runs` summary (status, cost, mutations, `triggerId`) on the
`bao/runs/v1` store child — the cross-device run finder.

Records stream **in-flight** (the header lands at run start, records
flush at every span end / cell), so a crashed run leaves a partial
trace (`trace show` reports `status: incomplete`). Bodies expire per
`[traces] retain_conversations` (default 60d) / `retain_jobs` (30d);
summaries stay. `anyrt run` lands its trace the same way — in the
local store of the run's space (`--from-space`, else `bao.space`) on
`--addr`; there is no file store. `--traces-dir` (default `traces/`)
is only the raw-blob directory (ADR-026 §1).

Every `trace` subcommand takes `--addr <any url> [--space bao]` to
read a server's store (default `http://127.0.0.1:7001`).

## The tools

```sh
# the run finder: one row per run, newest first — time (run end, or
# start while in flight),
# run id, program, status, duration, turn count, and turn 1's user
# text as the title. --program filters the cron noise out.
anyrt trace ls --addr http://127.0.0.1:7134   # 30 newest, all programs (that server's store)
anyrt trace ls --addr … --program toolcaller  # just conversations
anyrt trace ls --addr … -n 0                  # everything
anyrt trace ls                                # --addr defaults to http://127.0.0.1:7001

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
anyrt trace show --addr … run_<id>   # (every show flag takes --addr;
                                     # without it: http://127.0.0.1:7001)
anyrt trace show run_<id> --full     # lift all clips (also inlines the
                                     # boot window at turn 1)
anyrt trace show run_<id> --system   # + system prompt text
anyrt trace show run_<id> --boot     # + boot window verbatim (history
                                     # tail + injected context)
anyrt trace show run_<id> --seq 42   # one record, full, blob-resolved
                                     # (raw blobs: --traces-dir, the
                                     # serve's dir, default traces/)

# per-RUN cost/usage table: one row per turn — stop, in, cacheRead,
# cacheWrite, out, cells, effects, llm ms — with totals and costUsd.
# Prices come from runtime/src/model_pricing.json keyed by the model
# the trace recorded (per-MTok USD; edit the json to change rates;
# unknown models render '-').
anyrt trace show run_<id> --stats

# the metrics view over a STORE: fuel/duration/token
# distributions (p50/p95), effect histogram, tuning suggestions
anyrt trace stats --addr …
```

Cross-run questions are queries, not file sweeps — the same surface
bao has (`effects.query`, ADR-023 §5): provenance (`$match {name:
"any.create_object", "output.objectId": X}`), audit (`$match
{"meta.class": "mutate"}` → `$group` by `$runId`), failures (`$match
{"error.type": {$exists: true}}`) over `POST /v1/local/aggregate` on
the bao space's `trace_records`; per-run rows on `trace_runs`.

Clipped lines are locators, not the payload: content clips end
`… (+N chars — --full)` so you know how much is hidden, every effect
line prints its `#seq`, and result blocks name the record they were
mined from (`result (mined from #77):` — the provider request right
after the cell). Drill into any of them with `--seq N` (or jq below).

Raw access to a run is the store's query surface — the same one bao
has (`effects.query`, ADR-023 §5): `$match {runId, kind: "effect",
"meta.class": "mutate"}` over `trace_records`, or `--seq N` for one
record.

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

The agent reads runs from inside a conversation too — `effects.of` /
`effects.get` / `effects.runs` / `effects.stats` are kernel globals
over the `trace.*` syscalls (ADR-003 §4), and `run=<traceRef>` reads
any run in this bao's trace store, not just the current one — so
asking bao "why did you do that?" about an earlier reply works: it
dereferences that reply's `traceRef`.

## A reporter's export

Traces are local-store collections of the bao space (ADR-023) on the
reporter's own any server — not files. The desktop's Help ▸ "Export
anybao Data…" ships them as `traces.anyenc.gz`: the any server's
collection export (any docs/26-local-store.md § Export and import —
`trace_runs`, `trace_records`, `trace_blobs`, one gzip'd anyenc
stream). Load it into any server you run and read it with the same
tools; the reporter's bao space id is in the file (and in their
`anybao.toml` next to it in the zip):

```sh
any --addr 127.0.0.1:7199 local import traces.anyenc.gz   # prints the collections + counts
anyrt trace ls   --addr http://127.0.0.1:7199 --space <their bao space id> --program toolcaller
anyrt trace show --addr http://127.0.0.1:7199 --space <their bao space id> run_<id>
any --addr 127.0.0.1:7199 local aggregate trace_records --space <their bao space id> --pipeline '[…]'
```

`--space` takes the raw id: the space does not exist on your server,
only its trace collections do (`trace ls/show` fall back to a space
id whose `trace_runs` collection is present on `--addr`). Raw blobs
(`{__blob}` refs: fetched images, PDFs — ADR-026) are files under the
zip's `traces/blobs/`; point `--traces-dir` at that unpacked directory
so `show --seq` resolves them. Importing the same file twice is a
no-op; a newer export of the same bao overlays the older. The CLI
export for your own rig is `any local export --space <id> --names
trace_runs,trace_records,trace_blobs --out traces.anyenc.gz`.

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
