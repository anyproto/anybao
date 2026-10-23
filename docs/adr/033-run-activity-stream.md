# ADR-033: Run activity stream — self-describing `bao.run.*` events for the chat UI

Status: **Accepted** (2026-10-23)
Date: 2026-10-23
Amends (when accepted): ADR-001 §4d (`meta.label` beside `meta.kind`
on the span end record), ADR-003 §4b (`@span(label=…)`), ADR-010 §4
(deploy validation accepts an optional `label`)
Builds on: any doc 21 (event bus), any doc 22 (processes — the
folded-descriptor convention this copies), ADR-001 §4c/§4d (spans,
`meta.kind`, the immediate-children view), ADR-002 §2 (the broker
classifies every effect `read`/`mutate` — boundary truth), ADR-005 §3/§4
(bubbles, wrap-up, the digest), ADR-023 (the trace store — the durable
twin of this stream), ADR-025 (presence beats — liveness stays there),
ADR-032 draft (client bus — this is one more family on it)
Material: the designers' page "bao activity UI: material for designers"
(Any Dev space, 2026-10-23): the nine categories, the cell anatomy and
the two modes it defines are what this stream must carry.

## Context

The chat shows nothing while bao works except a typing ticker and the
beat's 48-character code preview (ADR-025). Users ask "what is it
doing?" more than anything else: non-technical users want one plain
line ("Updating 3 tasks in Work"), technical users want a Claude
Code-like view of every cell, effect and result, with a stop button.

Everything the UI needs already exists on the host side. The broker
sees every effect cross the boundary with its `read`/`mutate` class;
every tool-method call is a named span with its input, output, the
author's `kind` and the inner mutation count; every cell has a record
with fuel, duration and interruption; the trace store keeps all of it.
What is missing is a live channel from that observation to the client.

Constraints from the owner (2026-10-23): keep it simple; fewer
connections between UI and bao; bao emits, the client renders; a new
tool or connector must render correctly with no client change. Both
modes must stay truthful: nothing in the stream is model-authored.

## Decision

### 1. One event family, host-published, on the account bus

Serve publishes `bao.run.*` events on the event bus (doc 21), scope
`account`, envelope `target` = the run id. Publisher: the host, from
the broker's span and cell hooks and serve's `run_program` wrapper.
Never the guest, never the model: the stream is a second reader of
the same observations the trace writer gets, so it cannot disagree
with the trace.

| type | when | data |
|---|---|---|
| `bao.run.started` | `run_program` entered | descriptor |
| `bao.run.cell` | a `cell` or `bash` span begins | descriptor + `cell: {id, turn, preview, code, command?}` |
| `bao.run.steps` | the cell's immediate children end (coalesced, §4) | descriptor + `cell.id` + `steps: [step, …]` |
| `bao.run.cell_end` | the cell's span ends | descriptor + `cell: {id, ok, error?, interrupted, counts, mutations, durMs, fuelUsed, digest}` |
| `bao.run.turn` | an `llm.chat` span ends (one model call) | descriptor + `turn: {n, stop, usage}` |
| `bao.run.ended` | `run_program` returned | descriptor + `status, stop?, durMs, fuelUsed, mutations, cells, turns` |

**Descriptor, folded into every frame** (doc 22's rule: one frame
materializes the run on a client that missed the start):

```jsonc
"run": {"id": "run_…", "title": "fix the flaky test",   // the chat text, or the program spec
        "startedAt": 1761200000.0, "program": "agent:toolcaller@v1",
        "chat": {"space": "<id>", "object": "<chat id>"} | null,    // null for trigger/control runs
        "device": "<peer id>"},                                    // ADR-015 identity
"budget": {"fuel":  {"used": 12e9, "limit": 250e9},
           "wall":  {"elapsedS": 41.2, "limitS": 1200},
           "turns": {"n": 3, "limit": 300},
           "tokens": {"in": 18200, "out": 1400, "cacheRead": 90000}}   // summed from llm.chat span outputs
```

Nothing is retried or stored. A client that joins mid-run reads the
descriptor off the next frame and renders from there. The durable
history of a run is the trace store (`trace_records`, ADR-023 §5 —
records flush at every span end, so an in-flight run is already
queryable) and the synced `agent_turns` row at the end; reading it
back into the view (backfill) is a follow-up, not part of this ADR.
That is the same split the beat has: live over the bus, durable in
the store.

### 2. The step: self-describing, boundary-classified

One `step` per **immediate child of the cell** — a tool-method span
(`any.update_object`, `linear.create_issue`, `llm.chat`) or a bare
effect not inside any span (`http.get` from cell code). Inner
primitives under a facade are not emitted: they are the trace's
expand-on-demand level (ADR-001 §4d), and the digest shows the model
the same grain.

```jsonc
{"seq": 77,                                    // the span/effect record's seq — the trace handle
 "name": "any.update_object",                  // span name = <module>.<method>; or the effect name
 "class": "mutate",                            // BOUNDARY truth: an effect's class, or mutations > 0 for a span
 "kind": "mutator",                            // the author's declared kind (ADR-001 §4d), narrative, may be absent
 "category": "change",                         // §3 — host-derived from name + class
 "label": "Updating {object} in {space}",      // §3 — a template; the client fills the refs
 "summary": "Update an object's properties → {objectId, …}",   // the method docstring's first line (ADR-010), absent for raw effects
 "refs": {"space": "<space id>", "object": "<object id>"},      // §3 — ids the client resolves to titles
 "input": {"spaceConfig": "<space id>", "object_id": "…", "patch": {…}},   // the recorded input, clipped to 2 KiB
 "ok": true, "error": null,                    // {type, message} when ok is false
 "durMs": 120, "effects": 3, "mutations": 1,   // span meta; effects/mutations absent on a bare effect
 "visibility": "shown"}                        // "hidden" for bookkeeping (§3)
```

`class` is the one field a renderer may trust for "did this change
anything": the broker's classification for an effect, and
`meta.mutations > 0` for a span — never `kind`, which is what the
author *said* (ADR-001 §4d). The write marker in both UI modes keys
on `class`.

### 3. Category, label and refs: one host table, generic fallbacks

The user-facing vocabulary is computed on the host at emission time by
`activity::describe(name, class, input) -> {category, label, refs,
visibility}`. It has three layers; a new method lands in the first
layer that matches, so an unknown method still renders.

**Categories** (closed set, the nine from the designers' page):
`thinking`, `lookup`, `search`, `memory`, `change`, `service`, `web`,
`command`, `self`.

**Layer 1 — prefix rules** (in the runtime, one table):

| name matches | category | visibility |
|---|---|---|
| `llm.*`, `subagent.*`, `deepResearch.*` | `thinking` | shown |
| `any.search`, `any.list_search_scopes`, `webSearch.*`, `search.provider.*` | `search` / `web` | shown |
| `recall.*`, `memory.*`, `history.*`, `autorecall.*` | `memory` | shown |
| `any.*` with class `read` / `mutate` | `lookup` / `change` | shown |
| `sh.*`, `fs.*`, `bash` | `command` | shown |
| `progress.*`, `chain.*`, `status.*`, `config.*`, `programs.*`, `applet.*`, `bao.status`, `chat.send`, `fuel.state`, `oauth.*` | `self` | shown |
| `time.now`, `module.resolve`, `env.get`, `config.get`, `runtime.get`, `trace.*`, `span.*` | `self` | **hidden** |
| `http.*` bare, URL host = the any server | `lookup` / `change` by class | shown |
| `http.*` bare, any other host | `web` | shown |
| `<module>.<method>`, anything else | `service` (service = `<module>`) | shown |

The hidden set is exactly what the digest's `_count_effects` skips
today ("not work done"); the two lists become one constant.

**Layer 2 — the label on the method** (owner, 2026-10-23). The `@span`
decorator (ADR-003 §4b) takes `label=` beside `kind=`: a template over
`{space}`, `{object}`, `{type}`, `{n}`, `{service}`, `{query}`,
`{domain}`, `{command}`, the refs of §3.

```python
@span(kind="mutator", label="Updating {object} in {space}")
def update_object(self, space, object_id, patch): ...

@span(kind="mutator", label="Creating an issue in Linear")
def create_issue(self, title, team, ...): ...
```

The guest records it on the `span.begin` payload like `kind`; the
broker keeps it on the span frame and stamps it on the end record's
`meta.label` (ADR-001 §4d amended: `label` is narrative, like `kind`
and `name`, and carries zero authority — `class` stays the oracle).
The host forwards it on the step verbatim. The words live next to the
method, deploy with the program (hash-gated, no `anyrt` rebuild), and
cannot go stale on a rename. Adding a function and writing how it
reads to a person is one edit in one place. `label` is optional:
deploy validation (ADR-010 §4) keeps requiring `kind` and only checks
that a given `label` uses known placeholders. For the `any@v1` flat
surface the label rides `__any_public__` next to `kind` and `_export`
passes it to the facade span.

**Layer 3 — the fallback label**, for every name with no template:
`"{verb} {where}"`, verb from the method prefix (`list|get|search|
query|read|find|one|whoami|me → Reading`, `create|add|put|append →
Creating`, `update|set|edit|patch|modify|move|upsert|mark → Updating`,
`delete|remove|trash|cancel → Deleting`, else the method name
humanized), `where` = `in {space}` when a space ref exists, else the
service name (`in Linear`), else nothing. So `granola.get_note`
renders "Reading in Granola" on day one, and a connector that does not
exist yet renders the same way.

**Refs** are extracted from the span input by argument NAME, one rule
set for every method: `spaceConfig|space → space`, `object_id|objectId
|id → object`, `type → type`, `query|q → query`, `url → domain`,
`command → command`. The host never resolves a title: ids cross the
bus and the client substitutes names from its own store (it already
renders `any://` links as chips, ADR-022). That is the "fewer
connections" rule: bao knows ids, the UI knows names, neither learns
the other's job.

**Counts** are the client's: consecutive steps in one cell with the
same `name` and `category` fold into one line with `{n}` ("Updating 8
tasks in Work"). The host sends the steps; the client decides the
grain it shows.

### 4. Cadence, size, coalescing

- Steps are coalesced per cell: the emitter flushes a `bao.run.steps`
  frame at most every 250 ms and always at cell end. A cell that
  writes 40 objects in a second is one or two frames, not 40. The bus
  allows 30 msg/s per peer (doc 21); a run stays under 10.
- `data` ≤ 64 KiB (doc 21): `code` clipped at 16 KiB, `input` per step
  at 2 KiB, `digest` at 4 KiB, a `steps` array split across frames at
  48 KiB. Every clip is marked (`…clipped`), and `seq` is the handle
  to the whole record in the trace.
- Emission never blocks the run: the broker hands frames to a
  bounded channel; a serve thread posts them. A full channel drops the
  oldest `steps` frame (the trace has it) — never a `cell`,
  `cell_end`, `started` or `ended` frame.
- Replay mode and `anyrt run` without serve have no emitter: the
  stream is a serve concern, and emitting is not an effect, so the
  trace and replay are untouched by construction.

### 5. What stays where

- **Liveness and the plain line's floor stay in the beat** (ADR-025 /
  ADR-032 `kind: serve`). The beat is cadence-published and cheap; the
  stream is burst-published and rich. A client with the beat alone
  still shows "working" and the code preview; a client with the
  stream shows the plain line from the newest shown step's label.
  Precedence for the plain line: bao's authored `line` (status@v1,
  ADR-025 §3) > newest shown step label > beat preview > run title.
- **How a run ended stays on the final bubble** (ADR-005 §3:
  `agent.done`, `agent.outcome`, `agent.debugLink`). `bao.run.ended`
  is the stream's own terminal frame with the totals; it carries
  `status` and the program's `stop` (`done | wrapup`) but not the
  user-facing stop reason — that is the fixed string in the bubble
  text, and the client already keys on it.
- **Progress bars stay `process.*`** (doc 22, ADR-014). A chain or an
  import that outlives the run is a process, not a run frame.
- **Interim text stays a chat bubble** (`done: false`). The stream
  carries structure, the chat carries words.

### 6. The client

One subscription (any-ui already holds it: `ui.*`, `process.*`,
`bao.status`, `links.updated`) gains `bao.run.*`. A `runActivity`
atom keyed by run id folds frames: descriptor last-write-wins, cells
by id in order, steps appended by `seq`. Two renderers over the same
atom:

- plain: the status line under the user's message = §5 precedence,
  with the client-side fold of consecutive same-name steps into a
  count, the write marker from `class`, the failure from `ok`.
- extended: the run header (title, budget strip from `budget`, stop
  button = the existing `control: {kind: "break"}` record), turn
  blocks, cell blocks (code, steps, digest, footer), collapse rules
  from the designers' page. Step rows expand to `input`; `seq` is
  kept on every step so the follow-up can fetch the full record and
  the inner primitives from the trace by handle.

Unknown `bao.run.*` types and unknown step fields are ignored, the
same rule as every other family on the bus.

### 7. Testing

- Runtime: `activity::describe` table tests — every host effect name
  and every current tool/connector method lands in a category, the
  hidden set equals the digest's skip set, fallback labels for an
  unseen `foo.create_bar` and `foo.list_bars`; refs extraction by
  argument name; clipping marks; a span with `label` forwards it
  verbatim, a span without one gets the fallback.
- Guest: `@span(label=…)` lands on `span.begin` and on `meta.label`
  of the end record; a label with an unknown placeholder is a deploy
  validation error; `any@v1`'s `_export` carries the label from
  `__any_public__`.
- Serve: `StubTransport` publish sequence for one scripted run —
  `started`, `cell` (code present), coalesced `steps` (40 mutations
  in ≤ 3 frames), `cell_end` with `mutations`, `turn` with usage,
  `ended`; the descriptor on every frame; an interrupted cell marks
  `interrupted`; frame size never over 64 KiB.
- Live: a rig run with a 10-object write and a connector call; the
  UI shows "Updating 10 tasks in Work" then "Reading in Linear"; a
  second device sees the same frames; a window opened mid-run renders
  from the next frame.

## Consequences

- No new host effect, no server change. In the runtime: the category
  table and the emitter. In the kernel: one optional `label=` on
  `@span`. In the repos: labels on the methods, added over time. In
  any-ui: one atom and two renderers.
- A new connector or tool renders on day one with the generic label;
  a polished line is one `label=` on the method, deployed with it.
- The stream and the trace cannot drift: same observation, two
  writers.
- Cost: a busy run publishes a few frames per second on the account
  scope; on the network scope that is pub/sub traffic for every online
  device of the account.

## Decided (owner, 2026-10-23)

- **Account scope.** The user texts bao from several devices, so every
  device sees the run, code and inputs included. Device scope is not
  an option.
- **The stop reason stays on the bubble.** Not on the stream in v1.
- **Backfill is a follow-up.** v1 renders from the frames it hears; a
  window opened mid-run starts at the next frame. Reading
  `trace_records` for the history (directly, or through an ADR-032
  request to serve) is its own task after this one ships.

- **The label lives on the method.** `@span(label=…)`, §3 layer 2.
  Not a host-side table.

## Implementation order

Four parts, each its own change: the runtime emitter + category table
(§1–§4), the kernel `label=` on `@span` (§3), the any-ui atom + two
renderers (§6), then labels on the `any@v1` and connector methods —
the fallback covers them until each lands.
