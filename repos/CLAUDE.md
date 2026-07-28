# repos/ — the deployable overlay repos

Each folder here is one **repo** in the ADR-009 package-overlay sense:
a tree of guest programs (and, for the agent repo, skills) that
`anyrt deploy` publishes to an `any` space, which consuming runtimes
join read-only and import from (`use("<alias>:<name>@vN")`).

| folder | space (production) | alias | contents |
|---|---|---|---|
| `_agent` | `_agentrepo` | `agent` | the bao agent: toolcaller loop, tool + cron programs, `_`-system skills |
| `_connectors` | `_connectorsrepo` | `connectors` | external-service connectors (linear, github, granola, attio, figma, intercom) |

Deploy (hash-gated, upsert-only — stale space objects need manual
deletion):

```sh
anyrt deploy --source repos/_agent      --target agent      [--config-file anybao.test.toml]
anyrt deploy --source repos/_connectors --target connectors [--config-file anybao.test.toml]
```

`--target` resolves through the config's `[overlays]`; a raw space id
(+ `--addr`) works for spaces the config doesn't know.

## Authoring a program (ADR-010: docstrings are the docs)

- One program = `programs/<name>@vN.py` (flat, cron jobs) or
  `programs/<name>@vN/program.py` (folder — tests/fixtures ride
  alongside and are ignored by deploy). No description.md, no
  schema.md — the docs ARE the docstrings.
- **Module docstring**: first line = a self-contained one-liner
  ≤ 80 chars (it becomes the cached `summary` property and every
  listing shows exactly that line); whole docstring ~6 lines
  (hard cap 12 / 800 chars). Dev-facing context goes in `#` comments
  below, not in the docstring.
- **A tool** additionally declares `__any_tool__ = True` at module
  top level and carries `@span("<name>.<method>", kind=...)` on every
  public method — deploy validates both and rejects violations.
  Method docstrings: first line = self-contained summary (inventories
  show only it), body = return shape, options, budgets. `help()` in
  the guest renders exactly what you write.
- **Result shapes — trim, don't rename (C1).** Kept result fields
  carry the upstream API's exact names and nesting (`html_url`,
  `updated_at`, `user.login`; GraphQL camelCase verbatim — a GraphQL
  selection IS the trim). Trim to a small field subset and cap string
  lengths, but never translate names: the model's priors are the
  upstream docs, and renamed fields cost a keys()-discovery turn per
  fresh context or, worse, silent `.get()` Nones (github@v1 A/B,
  2026-07-28). Derived keys only where upstream has no scalar
  (`repo`, `is_pr`, decoded `text`, `kind`) — and NAME every method's
  return shape in its docstring (`→ {ok, path, text}`), especially
  derived keys: an undocumented derived key re-triggers the raw-API
  prior (G3).
- **Guest never imports the host**: sources are exec'd in the wasm
  kernel; `use()`, `effect()`, `span()`, `http` are provided globals
  (`# noqa: F821 - guest global`), stdlib imports limited to the
  kernel allowlist.
- Cross-repo deps are alias-qualified (`use("agent:llm@v1")`);
  unqualified specs resolve in the OWNING repo's space only.
- Published overlay versions are frozen — edits bump `name@vN`.

## Testing

Kernel-fidelity harness (`tests/kernelenv.py` at the repo root) runs
program sources under the REAL guest kernel with only the effect
boundary faked. `_connectors/tests` records live-API replies as
fixtures and replays them (see `_connectors/testing-flow.md`).
`uv run pytest` from the anybao root runs everything.

Canonical contracts live in `docs/adr/` — 009 (overlays, deploy),
010 (docstring convention), 008 (connector credentials), 005 §5
(prompt assembly).
