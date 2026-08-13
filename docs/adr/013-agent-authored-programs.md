# ADR-013: Agent-authored programs — `create_program` in the working space

Status: **Draft** (2026-08-13)
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

An agent-authored program is a `program`-typed object **in the
agent's working space**, bit-identical in shape to what deploy
writes: source in `program_source`/"main"/{code}, derived `summary` +
`any_tool` properties per ADR-010 §4, `name@vN` addressing. Nothing
downstream can tell the difference (§7 pre-commitment satisfied):
`use("name@vN")` resolves unqualified specs in the working space
(ADR-004 §2), `list_programs` already lists it, `help()` already
renders it, and the resolver's probe cache keys on the source
record's `_addSeq` (ADR-004 §4) — so **an edit is live on the next
`use()`**, the bobrik behavior, for free.

**Shadow guard**: `create_program`/`update_program` REFUSE a
`name@vN` that any joined overlay exports. Working-space copies
shadow the overlay (ADR-004 §2) — the one real footgun here — and
pipeline-owned assets stay pipeline-owned.

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
valid identifier + `@vN`; `compile()` gate (a SyntaxError is never
saved); import allowlist scan (guest never imports the host); tool
programs additionally `__any_tool__ = True` + `@span` on every public
method + method docstrings. The deploy.rs scan stays **normative**;
`programs@v1` reimplements it in guest Python, and both suites run a
shared fixture corpus so drift breaks tests, not agents (O2).

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
existing credential refs but cannot mint or read them (ADR-011).
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

## Non-goals

- No agent writes into overlay/repo spaces; no agent-driven deploy.
- No new host effects, no host-side validation service (O2 records
  the escape hatch).
- No JS, no bobrik markdown doc side-channel.
- No watch/notify contract — that's a program bao writes, not an ADR.

## Open questions

- **O1**: should the very first save of a new *tool* (not its edits)
  require a one-line user confirmation in chat? Lean no (persistence
  ≠ power), but the noise/abuse surface of self-multiplying tools is
  worth one review after a month of live use.
- **O2**: validation drift between deploy.rs and the guest
  reimplementation — shared fixtures now; if drift bites anyway, the
  consolidation is a host `program.validate` effect, accepted then as
  a deliberate thin-host exception.
- **O3**: retention/cleanup — authored programs that stop being used
  (decay sweep? manual only? `list_programs` staleness marker).

## Consequences

Closes the last bobrik parity gap and passes it (self-scheduled
watches were impossible there). The 5-minute toolcaller cron gets
replaced by a bao-authored `mailWatch@v1` calling
`connectors:gmailSync@v1.sync_now` + a cheap-tier `agent:llm@v1`
judge + a marker-carrying `chat_send` — same behavior, ~100× cheaper
per tick. `_meta_skill` grows the authoring paragraph; the fixture
corpus lands beside `repos/_agent` tests. Validation code exists
twice by explicit decision (O2), with tests holding the two in sync.
