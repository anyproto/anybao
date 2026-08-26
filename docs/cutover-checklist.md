# Cutover checklist — anybao replaces bobrik-watch

The M4 gate: run anybao side-by-side against the old binary on a test
space, walk every BOBRIK.md behavior, then retire bobrik-watch. No
bridges — verification, not compatibility (no-backcompat principle).

**Walk 1 (2026-07-08, automated, bao-test space, fts+vector server):**
startup / deploy / skills / triggers / conversation / cell execution /
explicit memory save / auto-recall injection (accessCount + ROI log) /
trace + viewer / control-API patch → live reschedule → extraction fired
(2 runs: 4.2s real work, 5ms no-op cursor hit) — ALL VERIFIED LIVE.
Golden recall eval recall@5 = 1.0; full integration suite 18/18, zero
skips. Six wire bugs live-caught and fixed (commits e5a2040…a08819f);
one upstream addition (any d2165e2: brain bookkeeping datasets); server
must be built with `-tags 'fts vector'` (the warning is in its log).
Remaining before retirement: the human side-by-side day on the real
bao space (below).

## Setup (side-by-side)

```sh
# terminal 1 — the any server
cd ~/any/any && ./bin/any run

# terminal 2 — old agent on its space (unchanged, keeps serving)
cd ~/any/any && ./bin/bobrik-watch --space bao

# terminal 3 — anybao on a TEST space (never the live bao space
# until the walk passes)
cd ~/any/anybao && make kernel
make runtime
ANTHROPIC_API_KEY=... ./runtime/target/release/anyrt serve \
  --addr http://127.0.0.1:7001 --space bao-test
```

## Behavior walk (from BOBRIK.md, mapped to the v2 shapes)

Startup:

- [ ] Space found-or-created by name (adoption rule: name + active).
- [ ] Watched chat found-or-created (`--chat-name`, default `general`;
      a fresh name gives clean v2 `agent_turns`/`agent_chunks`
      datasets per ADR-006 §0).
- [ ] Programs deployed hash-gated (`rollup@v1`, `extraction@v1`,
      `linkgen@v1`, + gated `decay/reflection/evolution@v1` present but
      their triggers DISABLED); re-run of serve reports `unchanged`.
- [ ] `program` user type + `program_source`/`program_manifest`
      datasets present in the agent space (deploy ensured them —
      ADR-010 §5); no `search` mapping on either.
- [ ] Skills deployed as `agent_skill` objects (`_core`, `_soul`,
      `_any`, `_memory`, `_space_context`, `_meta_skill`).
- [ ] Trigger control API answering on `127.0.0.1:7010` (`GET
      /triggers` lists rollup/extraction/linkgen with owner + rollup).

Conversation loop:

- [ ] Human message → conversation starts; agent-authored messages
      (`agent` field) never self-trigger (watcher skip).
- [ ] Progress bubbles arrive as `done: false` chat messages; the final
      reply is `done: true`.
- [ ] Mid-run human message INJECTS into the running conversation
      (mailbox), not a second conversation.
- [ ] Turn persisted to `agent_turns` (server-assigned seq, v2 fields:
      `traceRef`, `interrupted`, llm scalars).
- [ ] Trace written device-local (`traces/run_*.jsonl` + blob sidecar);
      `anyrt trace show traces/run_*.jsonl` renders it.
- [ ] Cell errors surface in the digest; the loop continues.
- [ ] Ceilings produce a wrap-up reply, never a silent cut.

Memory & recall (the v2 replacements for convmemory):

- [ ] Auto-recall: a message topically matching an old memory item gets
      a synthetic `recall` tool result before turn 1 (visible in the
      trace); accessCount bumps on the injected items.
- [ ] Explicit save via `memory.save_with_dedup` dedups (judge verdict
      in the trace as a nested `llm.chat`).
- [ ] Rollup trigger fires (or force via control API) → L1 chunks with
      correct fromSeq/toSeq; boot window shows chunk lines with
      drill-down handles on the NEXT conversation.
- [ ] Extraction trigger scans new turns; ROI datasets populate
      (`agent_roi_injections`, extraction items with provenance).

Cross-space & objects:

- [ ] Create/read/update objects in ANOTHER space from a cell
      (explicit `space` arg — the cross-space model).
- [ ] `any://spaceId/objectId` links render as attachments in chat.

Ops:

- [ ] Kill anybao mid-conversation → restart → no replayed messages
      (drop-snapshot feed), triggers re-arm, circuit breaker states
      survive (TriggerStore).
- [ ] Trigger failure ×3 → auto-disable visible in `GET /triggers`.

## Metrics checkpoint (second of two; ADR-003/005 "decisions from data")

- [ ] Review fuel/duration/token distributions over a day of
      side-by-side traces (`anyrt trace show`; a `trace stats`
      aggregate is a welcome follow-up).
- [ ] Tune: LoopPolicy ceilings, DigestPolicy inline budget,
      BootWindowPolicy total_tokens, trigger `limits` — from p95s, in
      config (not code) where possible.
- [ ] Golden recall eval green against the live index
      (`uv run pytest tests/test_recall_eval.py -m integration`).

## Retire

- [ ] Point anybao at the real bao space (`--space bao`); watch one day.
- [ ] Stop bobrik-watch; archive `cmd/bobrik-watch` upstream (any repo).
- [ ] Note the retirement + date in docs/m4-notes.md.

## Known deltas vs BOBRIK.md (intentional, no-backcompat)

- JS runtime/Sobek → Python cells in the wasi kernel; anyHelper-in-guest
  → the `any.*`/`memory.*` effect catalog; `logs.get` → `values.get`.
- Debug pages → device-local traces + viewer (`traceRef` = local run id;
  promote-to-synced is the later AnyFileStore escape hatch).
- `--bootstrap-clean` → hash-gated deploy makes it unnecessary
  (deploy is idempotent; skills/programs overwrite on change).
- Vector search: real now (`any.search` hybrid index) — the degraded
  convmemory-era caveats are gone.
