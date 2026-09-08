# connectors — external-service connectors for the any agent: Linear (issues, read/write), GitHub (issues/PRs/commits/repos/notifications/files + raw API), Telegram (bot updates in, messages/files out), Granola (meeting notes), Attio (CRM), Figma (design files), Intercom (support), Google via googleAuth OAuth (Gmail, Calendar, Drive/Meet transcripts, Sheets) — all read-only except Linear, Telegram and GitHub's raw request()

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
    --config-file configs/anybao.staging.toml
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

| ref | injected as |
|---|---|
| `connector.key.linear` | Authorization, RAW (no Bearer) |
| `connector.key.github` | Authorization, Bearer |
| `connector.key.granola` | Authorization, Bearer |
| `connector.key.attio` | Authorization, Bearer |
| `connector.key.figma` | X-Figma-Token |
| `connector.key.intercom` | Authorization, Bearer |
| `connector.key.telegram` | **the url** — `/bot{credential}/` |
| `connector.oauth.google` | Authorization, Bearer (managed — see below) |

The host injects the value after recording — secrets never enter
guest code or the trace. `connector.key.telegram` is the one ref that
goes into the **url** rather than a header (ADR-008 §1, amended
2026-09-08): the Bot API takes its token as a path segment and offers
no header at all, so `telegram@v1` writes the marker `{credential}`
where the value belongs and the host substitutes it on the wire only
— same custody, same secret-free trace, and the value is scrubbed
back out of anything the response echoes.

Keys are seeded by importing a dotenv-style
.env keyed by the ref itself (`connector.key.linear=…`): any-ui Help →
Import connector keys, or a `.connectors.env` beside `anybao.toml`
(rotation and revoke work the same way — see anybao
docs/config-secrets.md; env vars are no longer read). A missing key
surfaces as an actionable `{ok: false, error}` from every method, not
a traceback.

`connector.oauth.google` is a **managed OAuth ref** (anybao ADR-011):
no stored key — the host runs consent (`googleAuth.connect()`), holds
the refresh token device-local, and refreshes/injects access tokens
at request time. One consent covers gmail/googleCalendar/googleDrive/
googleSheets. Seed the BYO Google Cloud *Desktop app* client through
the same .env path (`connector.oauth.google.client_id` /
`.client_secret`); a missing grant surfaces as a typed
`not_connected` error pointing at `googleAuth.connect()`.

## Status

All six pattern-1 token connectors ported from bobrik-watch (any PR
#75): linear@v1 (read/write), github@v1, granola@v1, attio@v1,
figma@v1, intercom@v1 (read-only). Live-verified: linear, github.
Unverified against live APIs (no test keys yet): granola (young API,
endpoint-shape caveat in its docstring), attio, figma, intercom.
Google family landed with ADR-011 (host-held OAuth): googleAuth@v1 +
gmail@v1, googleCalendar@v1, googleDrive@v1 (Meet transcripts),
googleSheets@v1 — endpoint shapes ported from the bobrik branch, all
awaiting live hardening (testing-flow.md) behind a BYO Google client.
telegram@v1 (2026-09-08, read/write) is the first connector on a
url-injected credential; unit-tested against recorded Bot API
envelopes, not yet live-verified (testing-flow.md), and it is a
connector only — bao answering ON Telegram is a bridge on top of it,
not built.
