# Testing programs from the space — as close to prod as possible

**Question:** can we hit an any-server `/run` endpoint to execute a
deployed program (a py guest program, or the whole `toolcaller` loop)
against the live server, in the real agent runtime environment?

**Short answer:** there is **no `/run` endpoint on the any server**. The
any server (`~/any/any`, port 7001) is a pure data/object store —
`POST /v1/spaces/{space}/query`, objects, records, search. It stores
program *source* (as `program_source` records on `program` objects) but
never executes it. **All program execution lives in `anyrt`.** So
"running from the space" means: point an `anyrt` process at the deployed
source in a space and let it resolve + run it through the effect
boundary. This doc is the how-to.

## The two run surfaces (and the one difference that matters)

| | `anyrt run <spec>` | `anyrt serve` (prod agent) |
|---|---|---|
| what runs | one program's `main(args)`, one-shot | a `toolcaller@v1` conversation per chat message + triggers |
| `use()` resolves from | **local `programs/{spec}.py`** (`broker.rs` `sys_module_resolve`, resolver = `None`) | the **deployed space object** via `AnyModuleResolver` (`serve.rs`, ADR-004 §2) |
| effects / server / LLM | live server + live LLM, **if** you supply config+secrets | live, bootstrapped automatically |
| system prompt | n/a (you call `main` directly) | composed guest-side from the space |
| config bootstrap | **none** — you must pass `--config`/`--secrets` | `bootstrap()` seeds `any.base_url`, model tiers, `ANTHROPIC_API_KEY` from env |

The load-bearing difference: **`anyrt run` executes local source, not
the space object.** It is prod-faithful for the kernel, broker, effect
boundary, trace, live server and LLM — but the *code* comes off your
filesystem, not out of the deployed space. It equals the deployed form
only when you just deployed the same local files.

### Gotcha: folder-format programs are invisible to `anyrt run`

`anyrt deploy` reads **two** program layouts (`deploy.rs` `load_programs`):
flat `name@vN.py` **and** folder `name@vN/` (`program.py` +
`description.md` + `schema.md`). But `anyrt run`'s local resolver only
reads flat `programs/{spec}.py`. Programs that have moved to folder
format — currently `any@v1`, `history@v1`, `llm@v1`, `memory@v1`,
`recall@v1` — therefore **cannot be resolved by `anyrt run`**:

```
$ anyrt run 'any@v1' --args '{}'
{"error":{"message":"KeyError: program not found: any@v1 (programs/any@v1.py)", ...},"status":"error"}
```

Because `toolcaller@v1` (flat) `use()`s `any@v1`/`llm@v1`/etc. (folders),
**the full toolcaller loop cannot run under `anyrt run` today** — its
first `use("any@v1")` fails at resolve. The same drift breaks the
`any-dev` skill's `cp programs/*.py $D/` recipe, which copies only flat
files. Anything depending on a folder program must go through the space
resolver (serve, below — or the `--from-space` follow-up).

## How-to

### Path A — serve + chat: the true deployed space form (recommended)

This is prod. `serve` already runs deployed programs from the space
through `AnyModuleResolver`, composes the system prompt from the space,
and drives the real `toolcaller@v1` loop. Exercise it by deploying and
posting a chat message, then read the trace. This is exactly what the
**`check-anybao-changes` skill** automates.

```sh
# 1. publish local programs/ + skills/ into the agent space (hash-gated;
#    a running serve picks changes up on its NEXT run — no restart)
anyrt deploy --space bao

# 2. send bao a message that exercises your change (via the chat UI, the
#    anytype-send-message skill, or a POST that appends to the chat obj)

# 3. find the conversation's trace and read it end-to-end
anyrt trace ls --program toolcaller        # newest first, titled by the message
anyrt trace show run_<id>                  # turns, cells, effects (* = mutate)
```

A real run captured this way (from the live `bao` serve) —
`run_5e8492fb3ecd4ee7`, "make a book type…", `status: ok`, 10 turns,
`claude-sonnet-5`, 107 effects. Its head shows the space-form resolve:

```
#2 use toolcaller@v1 (miss)
#3 use any@v1 (miss)      ← folder program, resolved FROM THE SPACE
#5 use llm@v1 (miss)      ← folder program
#6 use history@v1 (miss)  ← folder program
#7 use autorecall@v1 (miss)
#8 GET /v1/spaces/bafyrei…/types → 200
...
```

Fidelity: identical to prod — same kernel, broker, effect boundary,
space resolver + probe cache, guest-composed system prompt, autorecall,
live server + live LLM, real space writes. Cost: real side effects on
the target space (memory/graph writes, chat replies) and a real LLM
bill. Use a scratch space if you don't want to touch prod `bao`.

### Path B — `anyrt run`: flat, LLM-optional py programs, scriptable

Best for a single program with deterministic-ish inputs — an `any@v1`
query, a helper, a replayable unit. One-shot, no chat, no serve, and
every run leaves its own trace. This is the `any-dev` skill's path.
Caveats: local source (not the space object), no config bootstrap, and
**flat programs only** (see the folder gotcha above).

```sh
D=<scratchpad dir>                    # NOT the repo
mkdir -p $D && cp programs/*.py $D/   # flat modules resolve from ONE dir
printf '{"any.base_url": "http://127.0.0.1:7001"}' > $D/config.json

cat > $D/probe@v1.py <<'PY'           # import-free; use()/effect()/json are globals
def main(args):
    c = use("any@v1").client()        # NOTE: any@v1 must be a FLAT file in $D
    return {"spaces": c.list_spaces()}
PY

anyrt run probe@v1 --programs $D --config $D/config.json \
    --traces-dir traces --args '{}'
# → one JSON line: {status, value, traceRef, durationMs, fuelUsed, error}
```

- `--config` with `any.base_url` is **required** — `run` does not
  bootstrap it (only `serve` does).
- LLM-free programs need no key; `use("llm@v1")` needs
  `ANTHROPIC_API_KEY` via `--secrets` and the `llm.tier.*` config
  entries `serve` gets from `config_defaults.json`.
- To make a folder program resolvable here today, flatten it into `$D`
  as `name@vN.py` — but that is testing a hand-copied source, not the
  deployed form. Prefer Path A.

## Follow-up proposal (NOT built): `anyrt run --from-space <space>`

The clean fix for "run the deployed space form, one-shot, scriptable,
without a chat message" is a flag on `run` that wires the same
`AnyModuleResolver` `serve` uses:

```sh
anyrt run 'recall@v1' --from-space bao --args '{"query": "dune"}'
anyrt run 'toolcaller@v1' --from-space bao \
    --args '{"chat": "<id>", "text": "hi"}'   # full loop, no serve
```

Sketch: when `--from-space` is set, build the broker with
`resolver = Some(AnyModuleResolver::new(client, space, private, aliases))`
(as `serve.rs` does) and call `bootstrap()` so config defaults + the env
API key are seeded like `serve`. This would run the **actual deployed
folder programs** from the space — closing the folder-format gap for the
one-shot path — while keeping full trace capture.

**ADR note:** this changes how the `run` subcommand composes resolvers,
so it needs an **ADR-004 amendment** (module loading & resolution: the
resolver-composition surface, currently "local dir OR space-backed"),
documenting `run` gaining the space-backed path and the bootstrap
seeding. Per the repo's hard rules, no code lands ahead of that amended
ADR. Filed here as a proposal only.

## See also

- [`docs/debugging.md`](debugging.md) — reading traces (`trace ls/show/stats`).
- [`docs/adr/004-module-loading.md`](adr/004-module-loading.md) — resolver contract.
- `check-anybao-changes` skill — automates Path A end-to-end.
- `any-dev` skill — Path B recipe for `any`-client scratch programs.
