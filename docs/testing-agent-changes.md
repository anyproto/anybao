# Testing agent changes on the test rig

Runbook (2026-07-24): a persistent scratch stack — its own any server,
account, and spaces on nondefault ports — for exercising `programs/` /
`skills/` / runtime changes end to end (real chat, real UI) without
touching the real bao install on :7001. Complements
[`testing-from-space.md`](testing-from-space.md) (one-shot run
surfaces) and [`repo-overlay-e2e.md`](repo-overlay-e2e.md) (the
two-account read-only overlay proof); the debugging loop is
[`debugging.md`](debugging.md).

## Topology

Everything on one throwaway-port stack, one account owning both
spaces (so the overlay needs no guest-key invite, unlike prod):

| | test rig | real bao |
|---|---|---|
| any server | `127.0.0.1:7134`, data dir `~/any/any/datadirs/staging-user` | `127.0.0.1:7001` |
| serve control port | `7016` | `7010` |
| overlay repo space | `_agentrepo` (programs + skills, ADR-009 §2) | `_agentrepo` |
| working space | `bao` — chat/brain/memory only, created by serve | `bao` |
| traces | the :7134 server's local store (`trace ls --addr http://127.0.0.1:7134`); `traces-staging-7134/` for `backend = "file"` | `traces/` |
| config | `configs/anybao.staging.toml` (gitignored — holds the account + space ids) | `configs/anybao.toml` |

The account is persistent — data dir + mnemonic backup live in
`~/any/any/datadirs/staging-user/`; never recreate it, restart the server and it
reloads. **Programs deploy to the `agent` overlay, never to the
working space** — a working-space copy shadows the overlay for
unqualified specs (ADR-004 §2) and goes stale silently.

## The flow

The rig may already be up from an earlier session — check before
starting anything (a second `run`/`serve` on the same ports just
fails):

```fish
curl -s http://127.0.0.1:7134/v1/health   # test server up? (account non-empty = ours)
lsof -nP -iTCP:7011 -sTCP:LISTEN          # serve up? (control port)
```

Whatever is already running, keep: steps 1/3 are only for the parts
that are down (deploy + chat need no restarts).

```fish
# 0. once per runtime change (skip for programs/skills-only edits)
make runtime

# 1. the test any server (leave running; rebuild ~/any/any first if
#    its binary is stale — symptom: 500 "collection handle is closed").
#    --config is REQUIRED: without it the server boots on a fake
#    nodeconf (lol1-any-sync-node…) — local same-account work seems
#    fine but cross-account overlay joins hang forever
cd ~/any/any && ./any run --config ./configs/any-config.yml --data-dir ~/any/any/datadirs/staging-user --addr 127.0.0.1:7134

# 2. publish the repo to the overlay (hash-gated; rerun after each edit —
#    a running serve picks it up on the next conversation, no restart)
cd ~/any/anybao
./runtime/target/release/anyrt deploy --source . --target agent --config-file configs/anybao.staging.toml

# 3. the agent
ANTHROPIC_API_KEY=(cat ../anthropic.key) ./runtime/target/release/anyrt serve --config-file configs/anybao.staging.toml

# 4. the UI, pointed at the rig (5175 so the real one keeps 5173)
cd ~/any/any-ui && VITE_API_TARGET=http://127.0.0.1:7134 pnpm dev --port 5175
```

Then talk to bao in the test UI's `bao` chat and read the run:

```fish
./runtime/target/release/anyrt trace ls traces-test --program toolcaller
./runtime/target/release/anyrt trace show <run_id>   # + --stats / --seq N
```

The edit loop is 2 → chat → trace: deploy is the only publish step
(serve is space-only, ADR-009 §5), and only changed assets re-upload.

## Resetting

- **Agent state** (brain/memory/chat), keeping the account: delete the
  working space — serve recreates it empty on next start:
  `any --addr 127.0.0.1:7134 space delete --yes <baoId>`.
- **Shipped code**: just redeploy; the overlay updates in place.
- **Fresh overlay space** (e.g. after nuking the data dir — which you
  shouldn't): `POST /v1/spaces {"name":"_agentrepo"}`,
  then `any space update --name _agentrepo <id>` if the name comes
  back null, and put the id in `configs/anybao.staging.toml [overlays] agent`.
