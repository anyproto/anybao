# Testing programs from the space — as close to prod as possible

**Question:** can we hit an any-server `/run` endpoint to execute a
deployed program (a py guest program, or the whole `toolcaller` loop)
against the live server, in the real agent runtime environment?

**Short answer:** there is **no `/run` endpoint on the any server**. The
any server (`~/any/any`, port 7001) is a pure data/object store —
`POST /v1/spaces/{space}/query`, objects, records, search. It stores
program *source* (as `program_source` records on `program`-typed objects
— a harness-declared user type, ADR-010 §5) but
never executes it. **All program execution lives in `anyrt`.** "Running
from the space" means pointing an `anyrt` process at the deployed source
and letting it resolve + run through the effect boundary. Three ways to
do that, below.

## The run surfaces

| | `anyrt run` | `anyrt run --from-space` | `anyrt serve` (prod agent) |
|---|---|---|---|
| what runs | one program's `main(args)`, one-shot | same, one-shot | a `toolcaller@v1` conversation per chat message + triggers |
| `use()` resolves from | local `--programs` dir (flat `name@vN.py` **or** folder `name@vN/program.py`) | the **deployed space object** — serve's `AnyModuleResolver`, no disk | the deployed space object (ADR-004 §6) |
| config store | none — seeds + `--config`; `config.set` refused | the space's `agent_config` store when the space has `bao/v1` (read-through, `config.set` writes it; `--config` keys shadow reads this run) | the space's `agent_config` store (ADR-006 §3) |
| good for | dev loop on local source | **testing the deployed form**, scripted | the full loop, end to end |

Every mode records a full trace in `--traces-dir` (default `traces/`) —
read it with `anyrt trace ls` / `trace show` (turns, cells, effects,
`--stats`, `--boot`, `--seq` drill-down): see
[`docs/debugging.md`](debugging.md).

## Path A — `anyrt run --from-space`: the deployed form, one-shot

Serve's exact broker composition (space-backed resolver, no local dir,
bootstrap parity — ADR-004 §6), without needing a chat message. The
space argument is a name or id, resolved **strictly** — a typo errors
out, it never creates a space.

```sh
# a deployed py program (folder or flat — the space doesn't care):
anyrt run 'webSearch@v1' --from-space bao \
    --args '{"query": "anytype local-first sync"}'

# the full toolcaller loop against the live space — the args shape
# serve passes (space = space ID, chatId = the space's generalChat):
anyrt run 'toolcaller@v1' --from-space bao --timeout-s 300 \
    --args '{"space": "<spaceId>", "chatId": "<chatId>",
             "userText": "reply with one short sentence",
             "agentName": "bao", "traceRef": "manual-test"}'
```

- `--addr` (default `http://127.0.0.1:7001`) names the server; a
  `--config` file's `any.base_url` wins over it.
- Config is the SPACE's: `config.get` answers from the bao space's
  `agent_config` rows (what the serve reads) and `config.set` writes
  them — a run that swaps `llm.tier.codegen` swaps it for the serve
  too. `--config` keys shadow reads for this run only. A space with no
  `bao/v1` bundle binds no store (a stderr line says so): seeds only,
  `config.set` refused.
- API keys come from the environment (`ANTHROPIC_API_KEY`,
  `GEMINI_API_KEY`, `TOGETHER_API_KEY`) exactly as they do for serve —
  llm-using programs work with no `--config` at all.
- Output is one JSON line: `{status, traceRef, value, error, …}`; the
  trace has the whole story.
- Mind the side effects: this **is** prod. A toolcaller run posts its
  reply into the real chat and writes `agent_turns`/memory to the live
  space. Point `--from-space` at a scratch space when that matters.

Verified 2026-07-17 against the live `bao` serve's space:
`run_7499df0c27ab4cb1` (webSearch@v1, folder program, resolved with a
real space objectId + CRDT marker) and `run_7d4b9289405a4abf`
(toolcaller@v1: space-resolved `any@v1`/`llm@v1`/`history@v1`/
`recall@v1`, guest-composed system prompt, autorecall, one Sonnet turn,
reply posted to the chat) — byte-for-byte the serve composition.

## Path B — serve + chat: the full loop

For end-to-end behavior checks (watcher, triggers, mailbox interrupts,
the space config object — everything Path A skips by calling `main`
directly), exercise the running agent itself. This is what the
**`check-anybao-changes`** skill automates:

```sh
anyrt deploy --space bao       # hash-gated publish; a running serve
                               # picks changes up on its NEXT run
# …send bao a chat message that exercises the change…
anyrt trace ls --program toolcaller   # find the conversation's trace
anyrt trace show run_<id>
```

## Path C — `anyrt run` on a local dir: the dev loop

Fastest iteration on *undeployed* source — no server round-trip for
resolution, but also not the deployed form. Both program layouts
resolve (flat `name@vN.py`, folder `name@vN/program.py`; flat wins on a
tie). This is the **`any-dev`** skill's recipe:

```sh
D=<scratchpad dir>
mkdir -p $D && cp -r programs/* $D/   # folders too; re-copy after edits

cat > $D/probe@v1.py <<'PY'
def main(args):
    c = use("any@v1").client()
    return {"spaces": [s["name"] for s in c.list_spaces()]}
PY

anyrt run probe@v1 --programs $D --args '{}'
```

No `--config` needed for the default local server: plain `run`
bootstraps like serve — `any.base_url` from `--addr` (default
`http://127.0.0.1:7001`), model-tier defaults, API keys from env; a
`--config` file overrides any of it. Stale copies bite: the
scratch dir is a snapshot, so re-copy after touching `programs/`. When
your dev program's deps should be the *deployed* ones, deploy the dev
program and use Path A instead — the two resolution worlds never mix
within a run (ADR-004 §6).

## See also

- [`docs/testing-agent-changes.md`](testing-agent-changes.md) — the
  persistent test rig (:7009): full chat + UI against a scratch
  server, without touching real bao.
- [`docs/debugging.md`](debugging.md) — the trace toolbox
  (`ls`/`show --full/--system/--stats/--boot/--seq`/`stats`).
- [`docs/adr/004-module-loading.md`](adr/004-module-loading.md) §6 —
  the resolver-composition contract behind all three paths.
- `check-anybao-changes` skill — Path B end-to-end.
- `any-dev` skill — Path C recipe for `any`-client scratch programs.
