# connectors — external-service connectors for the any agent: Linear (issues, read/write), GitHub (issues/PRs/commits/repos/notifications/files + raw API), Granola (meeting notes), Attio (CRM), Figma (design files), Intercom (support) — all read-only except Linear and GitHub's raw request()

An overlay repo (anybao ADR-009): guest programs that connect the
agent to external services. The repo tree is published to an `any`
space with `anyrt deploy`; a runtime consumes it read-only through an
`[overlays]` entry. The agent discovers it from the `## Repos` prompt
section and imports tools on demand — nothing here is injected into
its context.

## Use (from the agent / a guest program)

```python
linear = use("connectors:linear@v1")   # alias comes from [overlays]
linear.whoami()                        # -> {"ok": True, "user": {...}}
```

Discovery: `c.list_programs("<connectors space id>")` lists what the
repo offers; per-tool docs live in the space (`program_description` /
`program_methods` datasets).

## Layout

- `programs/<name>@vN/` — one folder per connector tool:
  `program.py` (import-free guest source — `use()`, `effect()`,
  `span()` are host-provided globals) + `description.md` (Tool
  Description) + `schema.md` (`### method(sig) [kind]` docs).
- `README.md` — this file. Its **first line is the repo's one-line
  description** shown to the agent in `## Repos`; keep it meaningful.
- No `skills/` — workflow skills live in the agent overlay
  (third-overlay skills don't inject, ADR-009 §3).

## Deploy

```sh
# from the anybao checkout (anyrt + config live there):
anyrt deploy --source repos/_connectors --target connectors \
    --config-file anybao.test.toml
```

Hash-gated: only changed assets re-upload; a running serve picks
changes up on its next conversation — no restart. `--target` is the
overlay alias from the config's `[overlays]`, or a raw space id.

## Test

Unit tests run each program through anybao's kernel-fidelity harness
(`tests/kernelenv.py` in the sibling checkout — the real guest kernel,
http served from fixtures recorded off the live APIs):

```sh
cd ~/any/anybao && uv run pytest ../anybao-connectors/tests
```

Against the persistent test rig (`anybao/docs/testing-agent-changes.md`):
deploy as above, message bao in the rig chat to exercise the tool,
then read the run: `anyrt trace ls traces-test --program toolcaller`,
`anyrt trace show <run>`.

## Auth

Every connector names an http-effect credential ref (anybao ADR-008):

| ref | header | seed env var |
|---|---|---|
| `connector.key.linear` | Authorization, RAW (no Bearer) | `LINEAR_API_KEY` |
| `connector.key.github` | Authorization, Bearer | `GITHUB_TOKEN` |
| `connector.key.granola` | Authorization, Bearer | `GRANOLA_API_KEY` |
| `connector.key.attio` | Authorization, Bearer | `ATTIO_API_TOKEN` |
| `connector.key.figma` | X-Figma-Token | `FIGMA_TOKEN` |
| `connector.key.intercom` | Authorization, Bearer | `INTERCOM_ACCESS_TOKEN` |

The host injects the header after recording — secrets never enter
guest code or the trace. Seed once via env (serve persists
device-locally) or write a localValue on the config object. A missing
key surfaces as an actionable `{ok: false, error}` from every method,
not a traceback.

## Status

All six pattern-1 token connectors ported from bobrik-watch (any PR
#75): linear@v1 (read/write), github@v1, granola@v1, attio@v1,
figma@v1, intercom@v1 (read-only). Live-verified: linear, github.
Unverified against live APIs (no test keys yet): granola (young API,
endpoint-shape caveat in its docstring), attio, figma, intercom.
Later phase: the google/OAuth family (gmail, googleCalendar,
googleDrive, googleSheets, googleAuth) — blocked on an `oauthFlow`
host effect (needs an anybao ADR first).
