# ADR-010: Native introspection — docstrings as the single doc surface

Status: **Accepted** (2026-07-28), amended 2026-07-28 (§1 hard caps;
§4 toolhood is declared, not derived — the shape heuristic misfired
on loop plumbing; §5 summary not indexed), amended 2026-08-04 (§8
flat tool surface — `any@v1` drops the `client()` binder for
module-level functions with a `spaceConfig` first arg + bound space
globals; handle methods were invisible to §3's inventory and the
skill hand-copy drifted)
Date: 2026-07-28
Builds on: ADR-001 §4d (kind vocabulary), ADR-002 (kernel namespace —
amends §3/§4), ADR-004 (module loading), ADR-005 §5 (prompt assembly —
supersedes the 2026-07-18 tool-discovery amendment), ADR-006 (data
contracts), ADR-008 (connector programs), ADR-009 (deploy, folder
programs). Evidence: dev-space task E4.

## Context

The agent discovers a program's API the way any Python programmer
does: `help(obj)`, `inspect.signature`, `dir()`. Traces show it
reaching for all three across many independent conversations (prod:
`import inspect` in 10, `inspect.signature` in 9, `help(` in 4,
`dir(` in 8; e.g. `run_9384fde7beef4734` burned 4 of 8 turns on
signature discovery alone). The kernel blocks everything except
`dir()` — the one that returns names without signatures — so each
blocked attempt is a wasted turn, and with no cross-conversation
memory the cost repeats forever.

Meanwhile every fact introspection would reveal is already in the
code: `use()` executes program source into a real `ModuleType`
(ADR-004), the kernel's CPython 3.13 evaluates typed signatures
(PEP 604 unions need no import), and `@span` preserves signature and
docstring through `functools.wraps`. Documenting the same facts a
second time in per-program markdown and server-side doc datasets is
duplication, and duplication drifts. One surface must own the docs;
the code is the only one that cannot drift from itself.

## Decision

### 1. A module documents itself

Docstrings are the ONLY authored program documentation.

- **Module docstring** = the program description, and it is SHORT:
  first line = the one-liner (hard cap 80 chars), whole docstring
  ~6 lines by convention, hard cap 12 lines / 800 chars. It enters
  the standing prompt for every tool (§3), so the caps are a
  contract, not a style hint — deploy rejects an overlong one
  (ADR-005 §5 prompt-tax doctrine, with teeth).
- **Function docstring** = the method doc: first line = the summary
  (the only line inventories show), the body states what the
  signature cannot — return shape, field trims, rate budgets,
  capability cliffs. Connector convention (ADR-008): return shape is
  mandatory.
- **Signature** = the schema. Typed where types help
  (`state: str | None = None`); annotations render through
  `inspect.signature` and `describe()` (§2).
- **Kind**: `@span(name, kind=...)` stamps `__span_kind__` on the
  wrapped function. The vocabulary is unchanged — `getter` (read),
  `mutator` (write / side-effecting), `setup` (binder) — one meaning
  across discovery and trace (ADR-001 §4d); narrative only, never a
  capability gate (ADR-002).
- **Visibility**: public `def` = visible; leading underscore =
  hidden. `main` is the run entry point, not a method — method
  listings exclude it.

### 2. Kernel introspection surface

- `inspect` joins the tier-1 import allowlist (ADR-002 §4):
  deterministic, effect-free, already interpreter-resident
  (`dataclasses` loads it).
- `describe(obj) -> str` — a kernel global, THE doc renderer:
  - module → module docstring, then one line per visible method:
    `name(sig) [kind]` + first docstring line;
  - function → `name(sig) [kind]` + full docstring.
  Deterministic given source; signatures render annotations and
  defaults; `[kind]` read from `__span_kind__` (absent = no tag).
- `help(obj)` joins the curated builtins (ADR-002 §3) as
  `print(describe(obj))` — the traced output channel, not pydoc's
  pager (stock `help` stays excluded).

One renderer, two callers: what `help()` shows the agent
mid-conversation and what the prompt carries (§3) are the same bytes.

### 3. Prompt composition is introspective

- The toolcaller's `## Tools` block: query the program objects
  (`any_tool`, two-tier space merge unchanged), `use()` each, emit
  `Import:` line + `describe(module)`. Ordering stays
  oldest-first — output is deterministic given sources, sources
  change only on deploy, so the cached prompt prefix holds.
- **Hard convention, now load-bearing**: program module top level is
  defs + constants ONLY — no effects, no I/O at import time. Compose
  executes every tool module each run; the fuel budget bounds a
  violation, the convention forbids it.
- `## Repos` is unchanged (name + first README line; contents stay
  out of context, ADR-009 §2). Repo programs are covered by
  `help(mod)` after import — signatures never enter the standing
  prompt for the long tail.
- Method visibility in the prompt = §1 visibility. There is no
  hide-by-documentation and no hide-by-kind: underscore is the only
  hiding mechanism.

### 4. Toolhood is declared; the one-liner is derived, then cached

(Amended 2026-07-28: pure shape-derivation misfired — loop plumbing
that spans its internals, e.g. autorecall, is shaped exactly like a
tool. Toolhood is a claim about intended audience, so it is
DECLARED.)

- `any_tool`: the source declares `__any_tool__ = True` at module
  top level. Deploy VALIDATES the declared shape — module docstring
  within §1's caps AND ≥1 public `@span`-tagged function (any
  nesting: class methods count) — and rejects a marked program that
  lacks it. `getTools` and prompt-compose filter on the property
  before exec'ing anything.
- `summary` (new string property): the module docstring's first
  line, derived for every program that has one. This is what listing
  surfaces show WITHOUT exec'ing source — `list_programs` returns
  `{name, version, anyTool, summary}`, so browsing a connectors repo
  reads as an annotated inventory; the flow stays list → `use()` →
  `help(mod)` for depth.

Derivation is a static source scan (first statement string literal;
the marker line; `@span(` above a `def`) — a convention check, not a
parser; a program that defeats the scan is breaking §3's top-level
convention anyway.

### 5. Program objects carry source, nothing else

- The server `program` type keeps `program_source` and the
  `name`/`version`/`any_tool`/`summary` properties. The
  `program_description`/`program_methods` datasets and their index
  chunkers are removed upstream (`~/any/any`,
  `internal/program/`).
- **Accepted loss**: method docs leave the search index (source is
  code, never indexed). Discovery = prompt inventory + `help()`.
  Decided at implementation (2026-07-28): `summary` is NOT indexed
  either — builtin type decls carry no `meta["index"]` flag and the
  SDK change isn't warranted; revisit only if evidence demands
  program recall.
- any-ui's ProgramView renders from `program_source` (docstring +
  source); tracked in any-ui, the contract here is: the doc datasets
  are gone.

### 6. Deploy publishes source

A program is `<name>@vN.py` or `<name>@vN/program.py` — no
`description.md`, no `schema.md`, no markdown splitting anywhere in
the pipeline (`toolmd` deleted, with its parity tests). Deploy
writes `program_source`, sets `name`/`version`/`any_tool`/`summary`
(§4), and enforces §1's docstring budget; the fingerprint hashes the
source and the derived properties.

### 7. The convention is taught where programs are authored

The agent authors programs too — the convention must reach it
through the same prompt that carries everything else:

- The injected skill text that teaches program authoring states §1
  outright: short module docstring (first line = one-liner), method
  docstrings with a first-line summary + return shape, typed
  signatures, `@span(name, kind=...)` on every tool method. The
  `## Tools` block itself teaches by example — it is `describe()`
  output, the exact shape the agent should produce.
- Every prompt-injected surface (skills `_*.md`, tool docstrings) is
  audited in the migration: references to the removed doc surfaces
  go away; discovery guidance points at `help()` and
  `list_programs` summaries.
- Any agent-facing program write path (`create_program`, when it
  lands) validates the same convention at write time — the same
  checks deploy runs (§4/§6), so agent-created and deployed programs
  are indistinguishable to every consumer.

### 8. Flat tool surface — spaceConfig (2026-08-04)

Evidence: `run_87d61b0379144eed`. `any@v1`'s API lived on a `[setup]`
handle, so §3's inventory showed one line — `client()` — and the real
surface reached the model only through a hand-maintained skill copy,
which drifted (the search envelope was omitted; the model iterated the
envelope's keys). Exactly the duplication failure this ADR exists to
prevent, caused by the one place the renderer couldn't see.

- **Agent-facing tool modules expose a flat function surface.** The
  primary API is public module-level functions — every one of them
  renders into `## Tools` by §3 with no renderer change. The `setup`
  kind stays legal for genuinely stateful handles (e.g. `memory@v1`),
  but an API meant for the standing prompt must not hide behind one.
- **`spaceConfig` is the explicit per-call context.** Every
  space-scoped `any@v1` function takes it as the FIRST argument: a
  space id string, or a mapping carrying `spaceId` (ui-context shape)
  or `id` (a `list_spaces()` row) — both pass through unchanged, so
  query results and bound globals are directly usable. Account-level
  functions (`list_spaces`, `create_space`) take none.
- **Omission errors transparently.** A first argument that cannot be a
  space ref (empty or whitespace-bearing string — prose that landed in
  the spaceConfig slot — or a non-string non-mapping) raises a
  `TypeError` naming the accepted forms and the bound globals — no
  implicit defaulting; "space always explicit" stands. Id SHAPE is
  server policy: a wrong single token still errors, server-side.
- **Bound space globals.** The toolcaller binds, per run, cell globals
  `currentUserSpace` (the user's live ui-context
  `{spaceId, objectId?, view?, updatedAt}`, or `None` when unknown)
  and `baoSpaceConfig` (`{spaceId, chatId}` of the agent's home
  space). "Here"/"this page" resolve against `currentUserSpace` in
  code the same way the prompt's view line resolves them in prose.
- **Internals**: connection (base url) and per-space catalog caches
  move to a module-private singleton; `client()` is removed. Callers
  that passed the client object now pass the module — same attribute
  surface, duck-type compatible for `(c, space)` binder params.

## Amendments

| ADR | Change |
|-----|--------|
| 002 §3 | `help` added to curated builtins as the `describe()` printer; stock pydoc help remains out |
| 002 §4 | `inspect` added to tier 1 |
| 004 | deploy format note: program objects carry source only |
| 005 §5 | tool-discovery amendment (2026-07-18) superseded by §1–§3; kind-at-point-of-choice and hide-by-underscore survive, hide-by-omission does not |
| 006 | program data contract = `program_source` + `name`/`version`/`any_tool`/`summary` |
| 008 | connector doc convention = docstrings (§1); folder layout loses `description.md`/`schema.md` |
| 009 | deploy pipeline per §6; `## Repos` contract reaffirmed unchanged |

## Consequences

- Zero-turn discovery for prompt-listed tools, one `help()` turn for
  repo connectors; the failure class in E4's traces (blocked
  `help`/`inspect`, guessed kwargs) cannot recur.
- Docs cannot drift from code — there is no second copy.
- Typed signatures become documentation the moment they are written.
- Compose runs every tool module's top level each run: small fuel
  cost, and the defs-and-constants convention becomes a contract
  (§3).
- Search no longer surfaces tool docs (§5, accepted).
- Migration order: kernel (§2) → toolcaller/`any@v1` (§3) → deploy
  (§4/§6) → server (§5) → connectors (github@v1 first: fold
  `schema.md` return-shape prose into docstrings under §1's budget —
  the pattern for the rest) → skills audit (§7) → any-ui.
