# ADR-004: Module loading & resolution

Status: **Accepted** (2026-07-07), amended 2026-07-17 (§6)
Date: 2026-07-07
Builds on: ADR-001..003 (accepted); plan §4 Overlays, §5 isolation
principle

## Context

Programs are space-resident objects (existing `program` handler type:
source in `program_source`, split tool docs, `any_tool` flag — format
unchanged, sync.py is the writer). v1 resolution: `name@v1` → current
space then private fallback; `private:`/`<spaceId>:` qualifiers; NO
cache so in-UI edits are live on next import. Plan-level decisions
already made: overlay aliases via config, unqualified names never
resolve into overlays, transitive imports defining-space-first,
import-is-an-effect. This ADR fixes the specifier grammar, the loading
mechanics under the wasi split, caching, and capability attenuation
plumbing.

## Decision

### 1. `use()` — explicit, version-pinned program loading

Python `import` statements cannot carry `@v1`, so program loading is a
kernel function (v1's `__import` precedent, Pythonic clothes):

```python
ws = use("websearch@v1")            # current space → private fallback
ws = use("private:websearch@v1")    # private space, strict
ws = use("std:websearch@v2")        # overlay alias (config), strict
ws = use("<spaceId>:websearch@v1")  # explicit space, strict
```

- Returns a module object; top-level of the program source runs once
  per load (same as a Python module).
- Plain `import x` statements remain **stdlib-allowlist only**
  (ADR-002 §4). Tools the agent uses every turn are pre-bound facades
  from the boot prelude — `use()` is for everything ad-hoc.
- **Versions are explicit and exact — no floating `latest`.** A
  floating version would make runs unreproducible at the *source*
  level; the recorded `module.resolve` pins content anyway, so
  `latest` would lie in exactly the place we look for truth.

### 2. Resolution order (fixes the plan's rules in code terms)

1. `name@vN` → current space, then private fallback. **Never overlays.**
2. `alias:name@vN` → that overlay's space, strict (aliases from the
   config cascade; `private:` built-in).
3. `spaceId:name@vN` → strict.
4. **Transitive**: an import inside a program resolves in its
   *defining* space first (lexical scoping — overlay programs are
   self-contained), then the consumer's explicit config pins
   (deliberate shadowing), never silent local fallback.

### 3. Loading mechanics under wasi

**Demand-driven, no source parsing.** `use()` at runtime is the only
discovery mechanism — nothing ever scans program text for imports.
Resolution and fetching are **host-side** (broker effect
`module.resolve`: resolve the space's `program` schema by xKey (ADR-010
§5; no type = no programs there), query the program object by
name/version props → read `program_source`); the guest executes the returned source into a
module object in the kernel namespace. A transitive `use()` inside a
loaded module fires its own resolution at call time, resolved against
the requester's defining space via the frame chain (§5) — the import
tree is discovered by execution, one record at a time.

One trace record per load, and **the record is self-contained**:

```jsonc
{"effect": "module.resolve", "cell": "toolu_01a",
 "input":  {"spec": "websearch@v1", "from": null},   // null = cell code;
                                                     // else requester identity
 "output": {"spaceId": "…", "objectId": "…",
            "marker": 4711,                          // _ver/_addSeq at load
            "sourceHash": "sha256:…",
            "source": {"__blob": "…", "bytes": 18234}},  // ADR-001 spill rule
 "meta":   {"class": "read", "cache": "miss", "durMs": 12}}
```

- Identity pinned twice: `marker` (which CRDT version) + `sourceHash`
  (which bytes); grants bind to the contentHash (CapBAC).
- `from` chains records — cell → program → transitive dep — so the
  whole import tree reconstructs from the log in execution order.
- **Output carries the source itself** (blob-spilled), not just the
  hash: strict replay returns recorded outputs instead of executing,
  and the guest needs real bytes to rebuild the module. Traces are
  therefore self-contained — a run replays bit-exact even after the
  program was edited or deleted. This is the "deterministic
  evaluation" promise (see both the code and the environment as they
  were) delivered literally.

### 4. Cache: probe-validated, never stale

Default ON (it can never serve stale code): host keeps a cache keyed
`(objectId, marker)` where **marker = the object's `_ver`/`_addSeq`**
from the existing row — zero server-side work (the derived-sourceHash
alternative from the plan is not needed; decided here). Every `use()`
does one light probe query for the current marker; hit → cached
source, miss → full fetch. In-UI edits are live on the very next
`use()` — the liveness doctrine holds with the O(tools) boot cost
gone. `module.resolve` records `cache: hit|miss` in meta.

Guest-side module objects persist across cells (kernel semantics), but
`use()` re-probes on every call — a mid-conversation edit yields a
fresh module instance on next `use()` (v1's clear-cache-per-call
behavior, now precise instead of wholesale).

### 5. Capability attenuation plumbing

Each loaded module is tagged with its identity `(space, program,
contentHash)`. Effect calls carry the **active frame chain** (a context
variable maintained by `use()` wrappers); the broker computes the
grant set as the intersection over the chain (ADR-002 §2, CapBAC
attenuation). v2.0's permissive profile makes this cheap bookkeeping
now, enforcement teeth later — same mechanism-first approach as the
broker.

### 6. Runtime composition: which surface resolves where (amended 2026-07-17)

The broker exposes one resolution slot; the two run surfaces compose
it differently, and the difference is the contract:

- **`serve`** — space-backed only (`AnyModuleResolver`): current space,
  no private fallback yet, no aliases, and **no local programs dir** —
  the serving broker structurally cannot read program source off disk.
- **`anyrt run`** (default) — local dir only: `programs/<spec>.py` or
  the folder layout's `programs/<spec>/program.py` (flat wins on a
  tie, matching the deployer's two-layout contract). The dev loop.
- **`anyrt run --from-space <space>`** — serve's composition, one-shot:
  the same space-backed resolver, same no-disk rule (the local dir is
  not consulted at all), so a run tests the *deployed* source. The
  space argument resolves **strictly** by name or id — never creates a
  space (`ensure_space`'s create belongs to serve/deploy). For parity,
  `--from-space` also runs serve's config bootstrap (defaults + env API
  keys), so llm-using programs work one-shot without a hand-built
  `--config`, and binds the space's `agent_config` store when the
  space carries a `bao/v1` bundle (ADR-006 §3): `config.get` reads the
  rows the serve reads, `config.set` writes them, and the run's own
  `--config` keys shadow reads only — a per-run override never
  rewrites the space. A space without the bundle binds nothing (the
  seeds answer `config.get`; `config.set` is refused, loudly, on every
  run surface without a store). Parity extends to the gaps: like serve it wires no
  private space and no aliases, so `private:`/overlay specs error
  identically on both surfaces until those land.

The two modes never mix within a run: a broker has either the local
dir or the space resolver, so every `module.resolve` record in one
trace answers from one world.

### 7. Non-goals

Skills and tool-docs reading (plain anyclient reads, harness-side);
overlay manifest/trust format (plan §4, own ADR when overlays are
built); program *writing* (sync.py / anyPrograms successor — part of
the parity port).

## Consequences

- Reproducibility: spec + recorded marker/hash identify the exact code
  of every run; no floating versions to un-pin it.
- Boot cost drops from O(tools) full fetches to O(tools) light probes
  (and the probes batch, ADR-002 `*_many` style).
- Live-edit workflow preserved exactly; the cache is invisible except
  in latency.
- The overlay story needs zero loader changes when it lands — aliases
  are config, resolution rules already encode the shadowing doctrine.

## Resolved questions (review 2026-07-07)

1. **`use()`** — confirmed as the name.
2. **Boot probe batching** — yes: all pre-bound tools probed in one
   `*_many` call at kernel boot.
3. **`reset()` just cleans the runtime** — no auto-reload; the agent
   re-`use()`s what it needs.
