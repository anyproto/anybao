# ADR-013: Agent-authored programs — `create_program` in the working space

Status: **Accepted** (2026-08-14), amended 2026-08-14 at implementation
(§1 shadow-guard mechanism = the `overlays.aliases` `runtime.get` key;
§3 syntax gate = `ast.parse`, adding `ast` to the tier-1 allowlist;
§3 "every public method" = module-level public defs — the §3/ADR-010
inventory surface)
Date: 2026-08-13
Builds on: ADR-002/003 (effect boundary, fuel — inherited unchanged),
ADR-004 §2/§4 (resolution order, the probe cache that makes edits
live), ADR-009 §2 (working space vs overlay repos, deploy pipeline),
ADR-010 §4/§6 (docstring convention, derived props) and the §7
pre-commitment ("any agent-facing program write path validates the
same convention at write time — agent-created and deployed programs
are indistinguishable to every consumer"), ADR-011 (secrets stay
host-side)

## Context

The agent cannot author programs. Live evidence (2026-08-13): asked
for "watch my mail, ping me when something matters", bao correctly
discovered there is no write path (`list_properties` on the `program`
type shows no source field — source lives in the `program_source`
dataset only deploy writes) and improvised the only thing it could: a
cron firing a **full `agent:toolcaller@v1` reasoning turn every five
minutes**, indefinitely — ~288 agent turns/day with the whole system
prompt, to do a job a 40-line program does for pennies.

This is a **regression against bobrik-watch**. The old agent had
`anyPrograms.js` (inventory from `~/any/any` @ `3540a93`, the last
commit before `cmd/bobrik-watch` was deleted): `createProgram` /
`updateProgram` / `editProgram` (str_replace) wrote a `program`-typed
object with source in `program_source`/"main"/{code}, `name@version`
addressing, live re-resolve on the next import, all-or-nothing
toolhood, and a **post-save import probe** that returned
`{ok: false, saved: true, hint: …}` instead of leaving a broken tool
bound. Agent-authored tools were first-class the next kernel boot.

What must NOT return with it: bobrik guest code had raw `fetch` to
any host, provider keys in guest-visible env, no fuel or turn
budgets, `js.eval` nested runtimes, and unguarded cross-space writes.
Every one of those is already structurally forbidden here (ADR-002,
ADR-003 §2, ADR-011) — which is precisely why the write path is now
safe to open: a saved program grants no capability `run_cell` doesn't
already have. Persistence, not power.

Scheduling is the one axis where anybao already *exceeds* bobrik:
old programs could never self-register anything (the sole trigger in
all of bobrik-watch was a live chat message); here `agent_triggers`
(ADR-006 §4) lets an authored program be cron/event/once-driven from
day one. This ADR deliberately ships **no** watch/notify design:
the first consumer is a bao-authored `mailWatch` program, which is
the point.

## Decision

### 1. Storage & addressing — the working space, deploy's exact shape

**Amended 2026-09-08 (ADR-027 §2/§3):** the `program` type is hidden and declares a shared editor part (`body`) plus the two stores as parts; `program_source` / `program_manifest` are dataset keys whose collections `ProgramSchema` reads back (`source` / `manifest`).

An agent-authored program is a `program`-typed object **in the
agent's working space**, bit-identical in shape to what deploy
writes: source in `program_source`/"main"/{code}, derived `summary` +
`any_tool` properties per ADR-010 §4, `name@vN` addressing. `program`
is a user type (ADR-010 §5), so `programs@v1` — the working space's
writer — ensures the type + datasets there on `create_program`
(idempotent, the same declaration deploy uses; ADR-017 §1) — deploy
only ever targets overlay/repo spaces. Nothing
downstream can tell the difference (§7 pre-commitment satisfied):
`use("name@vN")` resolves unqualified specs in the working space
(ADR-004 §2), `list_programs` already lists it, `help()` already
renders it, and the resolver's probe cache keys on the source
record's `_addSeq` (ADR-004 §4) — so **an edit is live on the next
`use()`**, the bobrik behavior, for free.

**Shadow guard**: `create_program`/`update_program` REFUSE a
`name@vN` that any joined overlay exports. Working-space copies
shadow the overlay (ADR-004 §2) — the one real footgun here — and
pipeline-owned assets stay pipeline-owned. *Mechanism (2026-08-14)*:
the host run surfaces (serve, `run --from-space`) seed the guest
`runtime.get` key `overlays.aliases` (ADR-006 §3) = the resolver's alias map
(`{alias: spaceId}`); the guard queries each overlay space for the
spec. An absent key (plain local runs) means no overlays joined;
an alias bound to the working space itself (the degenerate
single-space shape, ADR-009 §2) is skipped — it would match the
program being written.

*Render parity (2026-08-14)*: the toolcaller's `## Tools` compose is
overlay-resident module code, so its unqualified `use()` resolves in
the *overlay* (ADR-004 §2.4) and would miss working-space tools; the
compose imports working-space tools space-qualified while the
displayed `Import:` line stays unqualified (that's the spec cell code
should use, where it resolves in the working space).

### 2. Surface — `programs@v1`, a new agent-repo tool

Features are separate programs (2026-08-11 rule): the write path is
its own tool, not new `any@v1` methods. All writes are plain
`any@v1` object/dataset calls — **no new host effects** (thin-host
principle holds).

- `create_program(space, {name, version?, source})` →
  `{ok, objectId, spec, anyTool, probe, hint?}`
- `update_program(space, spec, source)` — full-source replace.
- `edit_program(space, spec, edits)` — str_replace list,
  all-or-nothing, mirroring `edit_markdown`; refuses an edit whose
  result drops `main()`/the module docstring (bobrik's self-heal,
  as a refusal instead of silent stub injection).
- `delete_program(space, spec)`.

Read side stays where it is (`list_programs`, `use()`, `help()` —
ADR-010); no duplicate surface. Source is guest Python; the docs ARE
the docstrings — bobrik's separate markdown side-channel does not
return.

### 3. Write-time validation — deploy parity, guest-implemented

The same static scan deploy runs (ADR-010 §4/§6), enforced before
anything is written: module docstring present, first line ≤ 80 chars
and self-contained, body within the 12-line/800-char cap; `name` a
valid identifier + `@vN`; syntax gate (a SyntaxError is never
saved — `ast.parse`, since `compile` stays outside the curated
builtins; `ast` joins the tier-1 import allowlist for it, amending
ADR-002 §4 the way ADR-010 §2 added `inspect`); import allowlist
scan (guest never imports the host — judged by attempting the
kernel's own `__import__`, so the check cannot drift from the
allowlist); tool programs additionally `__any_tool__ = True` +
`@span` on every public module-level def + method docstrings
("public method" = the ADR-010 §3 inventory surface; class internals
stay free, the `any@v1` shape). The write path is deliberately
STRICTER than deploy where deploy only polices tools (docstring
presence/caps, syntax, imports apply to every authored program) —
agent-authored source has no human reviewer in the loop. The
deploy.rs scan stays **normative** for the shared rules;
`programs@v1` reimplements it in guest Python, and both suites run a
shared fixture corpus (`tests/fixtures/program_validation.jsonl`,
per-suite expected verdicts) so drift breaks tests, not agents (O2).
Validation refusals RAISE (the loud pre-write error); the
`{ok: false, saved: true}` shape below is reserved for the
post-save probe.

**Post-save probe** (bobrik's best trick, kept): after writing,
`use()` the saved spec. On success, derived props are written and the
tool is live. On failure the object stays with `any_tool: false` and
the call returns `{ok: false, saved: true, hint: "fix via
edit_program(...)"}` — a broken save is recoverable state, never a
half-bound tool.

### 4. Execution & guardrails — nothing new, and that's the point

Authored programs run in the same kernel, through the same effect
boundary, under the same fuel governor as deployed ones (ADR-002/003
inherited). Secrets stay host-side; an authored program may *use*
existing credential refs but cannot mint or read them (ADR-011) —
"use" means send to the ref's declared hosts, nowhere else (ADR-021
§7/§8.3). A credential of its own it requests under `local.key.*`
with a mandatory destination (ADR-021 §8.1); the human sees an
unreviewed card (§8.2).
It may self-register `agent_triggers` records — the designed way to
make an authored watch periodic. Subagent recursion stays governed by
`subagent@v1`'s existing rules.

### 5. Toolhood & prompt budget

`__any_tool__` toolhood is immediate on a passing probe — no approval
gate, per the persistence-not-power argument in Context. Prompt cost
is bounded by ADR-010's tiered surface: one summary line per tool in
the inventory, depth on demand via `help()`. The skill teaching this
surface lands in `_meta_skill` (which already owns "you can author
skills" — programs are the sibling paragraph).

### 6. Versioning & promotion

Working-space `@vN` is editable in place (the probe cache makes it
live); ADR-009's freeze applies to published overlay versions only.
A working-space program that proves out is **promoted by a human**:
ported into `repos/`, reviewed, deployed — deploy remains the only
publishing pipeline, and the agent never writes into overlay repo
spaces (non-goal).

## Amendments

| ADR | Change |
|-----|--------|
| 002 §4 | `ast` joins the tier-1 import allowlist — pure, deterministic, already interpreter-resident (the kernel itself parses cells with it); it is the write path's syntax gate and source scanner. `compile`/`exec` stay out of the curated builtins |
| 003 §4b | `span(name=None, kind=None)` — name defaults to the decorated def's `<module>.<function>`; explicit name = deliberate display override. Motivated by this ADR: the first agent-authored write path immediately produced drifted span names |
| 010 §7 | the `create_program` pre-commitment is DELIVERED: `programs@v1` validates the deploy convention at write time |
| 021 §7/§8 | (accepted 2026-09-14) authored programs request their own secrets as `local.key.*` with mandatory hosts; existing refs are usable only toward their declared hosts — the trust model never attributes a call to a program (§8.0) |
