---
name: any-dev
description: Interact with the local any server (spaces, types, objects, memory, search) the way the agent does — a scratch guest program run through anyrt with use("any@v1").client(). Use whenever the user asks to create, inspect, or modify things in any/Anytype spaces from this repo, or to reproduce agent behavior against the server.
---

# any-dev — work with `any` through anyrt, like the agent does

All interactions go through the **effect boundary**: a scratch guest
program executed by `anyrt run`, calling `use("any@v1").client()` —
the exact client the agent uses. Never mutate the server with raw
`curl` (reserve it for verifying what the UI would see). Every run
leaves a trace in `traces/`, so your own interactions are debuggable
with the same tools as the agent's.

## Read the docs first

The API contract is the **docstrings in
`programs/any@v1/program.py`** — read the method you're about to call
before calling it. Semantics (xKey rules, property-write shapes, space
discipline) live in `skills/_any.md` — the same guidance the agent
gets. Wire truth is `api/swagger.vendored.json` (`jq '.paths | keys'`).

## Recipe

```sh
D=<scratch dir>                       # session scratchpad, not the repo
mkdir -p $D && cp -r programs/* $D/   # modules resolve from ONE dir —
                                      # -r: folder programs (any@v1/…) too
```

The copy is a **snapshot** — stale copies bite (a method added to
`any@v1` after the copy won't exist in `$D`). Re-run the `cp -r` after
any edit to `programs/`, and prefer a fresh `$D` per session.

Write `$D/dev@v1.py` — **import-free** (guest never imports the host;
`use()`, `effect()`, `json` are the globals you get), one `main(args)`:

```python
def main(args):
    c = use("any@v1").client()
    return {"spaces": c.list_spaces()}   # return data; don't print
```

Run it (pass inputs via `--args`, never bake secrets into the file):

```sh
./runtime/target/release/anyrt run dev@v1 --programs $D \
    --traces-dir traces --args '{"k": "v"}'
```

Output is one JSON line: `{status, value, traceRef, durationMs,
fuelUsed, error}`. `value` is what `main` returned. On failure, or to
see every effect: `anyrt trace show <traceRef>` (`trace ls --program
dev` lists your runs).

- No `--config` needed for the default local server — `run` bootstraps
  like serve: `any.base_url` from `--addr` (default
  `http://127.0.0.1:7001`), model-tier defaults, API keys from env.
  Pass `--config` only to override (the file wins over defaults).
- LLM-free programs need no API key; `use("llm@v1")` picks up
  `ANTHROPIC_API_KEY` from the environment (or `--secrets`).
- One program per step beats one giant program: cheap to re-run, and
  each leaves its own trace.
- To test **deployed** programs (the space form, serve's resolver)
  instead of local copies: `anyrt run '<name>@vN' --from-space bao`
  — no scratch dir, no `--config`, env API keys picked up like serve.
  See `docs/testing-from-space.md`.

## Known server quirks (verify before relying — fixed upstream eventually)

Dated 2026-07-09, observed in traces + agent memory; each is an
upstream `~/any/any` candidate, not something to paper over silently:

- `create_type` REQUIRES `xKey` (derive a slug from the name), and its
  inline `properties` NEVER sync (`list_properties` stays `[]`
  forever). Create the type bare, then `add_property` per field — each
  syncs instantly.
- Property writes on custom types are keyed by **property id** (from
  `add_property`/`list_properties`), not xKey; the type group key is
  the **type id**, not its xKey. Reads come back id-keyed too.
- Object-kind property values take `{"id": <objectId>}` — not a bare
  string, not a list.
- No `create_space` in the client — `c._call("post", "/v1/spaces",
  {"name": ...})` (schema: `api.SpaceCreateRequest`).

## Hard-won rules

- **Never retype ids from memory or truncated output — always re-query
  them** in the same program that uses them. A hand-typed id cost a
  debugging detour ("tree does not exist" is the bad-id symptom, not a
  server fault).
- **Verify return shapes from the source before writing against
  them** — docstrings don't always say (e.g. `get_markdown` returns
  the markdown STRING, not a dict; assuming a dict once overwrote an
  object's body). For read-modify-write, check the read looks sane
  before the write.

## Report, don't absorb — this skill is a dogfooding loop

You are exercising the exact surface the agent uses; every friction
you hit, the agent hits too and burns turns on. The point of using
this skill is to harvest that material. Classify each friction and
file it (task in the dev space + report to the user):

1. **Error-contract gap (upstream, ~/any/any)** — the server answered
   a client-caused fault with `code: "internal"` / a raw internal
   chain (`BuildTree …: tree does not exist`), or an unclear/misspelled
   message. Every client-caused fault should be a typed 4xx whose
   message says what to DO (`type.xkey_required`'s "derive a slug from
   the name" is the gold standard). All responses do carry a `code` —
   `writeError` in `internal/server/errors.go` — the gap is domain
   errors mapped to `internal` instead of a real code.
2. **anyHelper polish (programs/any@v1.py)** — the error IS typed and
   the client could act on it: translate known codes into actionable
   hints in the raised `AnyError` (the agent reads tracebacks), fill a
   missing wrapper, fix a docstring. Fewer turns next time.
3. **Guidance (skills/_any.md)** — the server and client are fine but
   the contract was unwritten or contradicted.

A workaround you didn't surface is a bug you buried — and a turn the
agent will waste forever.
