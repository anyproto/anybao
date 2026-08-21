# Prod repo account — bring-up runbook

The prod `_agentrepo` / `_connectorsrepo` overlay spaces (ADR-009) are
owned by a dedicated **repo account**, separate from the user/bao
account. Its server runs locally at `http://127.0.0.1:7003`, data dir
`~/.any-repo-prod`, on the **production** any-sync network — `any run`
with no `--config` uses the embedded prod nodeconf; passing
`--config ./any-config.yml` would put it on staging instead. User
accounts join the two spaces read-only via public guest-key invites;
`anyrt deploy` publishes through the repo server directly (deploying
through a guest account 403s with `space.read_only`).

Current identifiers (created 2026-08-21):

- account `AAVMk1soGKx5Na4zL5VvxdTq3LLWfVw1j8Yory6kBYnfjsZF`
  (mnemonic in `~/.any-repo-prod/ACCOUNT.txt`)
- `_agentrepo` `bafyreiguz5bowa2efwpcdterogllgflgv66a2du2tfveyxhanpeoo2nh3q.36hw3g1lpszh6`
- `_connectorsrepo` `bafyreieeazppuyowb4bgkhamrwjow4ken3sc4jpjg6fiejkbdvzfly776e.36hw3g1lpszh6`
- guest-key invite tokens: `anybao.toml [overlays]` (gitignored; a lost
  token can be re-minted, step 4 — `invite guest-key` is idempotent)

The pre-rename account in `~/.any-repo` is retired: it pre-dates the
spaceType wire rename (`anytype.*` → `any.*`), so current binaries
derive a fresh empty tech space from it and list no spaces.

## Recreating from scratch

Prerequisites: `~/any/any` on `main`, binary built with the search
tags — `nix develop -c go build -tags 'fts vector' -o bin/any
./cmd/any` (untagged builds silently compile search out). Run every
`bin/any` invocation through `nix develop` (links `libffi.so.8`; the
binary panics without it).

```fish
cd ~/any/any

# 1. account — mnemonic prints ONCE; save it to <data-dir>/ACCOUNT.txt
nix develop -c ./bin/any init --data-dir ~/.any-repo-prod

# 2. server on the PROD network: no --config
nix develop -c ./bin/any run --data-dir ~/.any-repo-prod --addr 127.0.0.1:7003

# 3. the two repo spaces
curl -s -X POST http://127.0.0.1:7003/v1/spaces -d '{"name":"_agentrepo"}' | jq -r .id
curl -s -X POST http://127.0.0.1:7003/v1/spaces -d '{"name":"_connectorsrepo"}' | jq -r .id

# 4. public read-only guest keys (idempotent)
nix develop -c ./bin/any --addr 127.0.0.1:7003 invite guest-key <agentrepo-id>
nix develop -c ./bin/any --addr 127.0.0.1:7003 invite guest-key <connectorsrepo-id>

# 5. deploy both repos THROUGH the repo server, by raw space id
cd ~/any/anybao
./runtime/target/release/anyrt deploy --addr http://127.0.0.1:7003 \
  --source repos/_agent --target <agentrepo-id>
./runtime/target/release/anyrt deploy --addr http://127.0.0.1:7003 \
  --source repos/_connectors --target <connectorsrepo-id>
```

Then point clients at the spaces in their `anybao*.toml`:

```toml
[overlays]
agent = { space = "<agentrepo-id>", invite = "<agentrepo-token>" }
connectors = { space = "<connectorsrepo-id>", invite = "<connectorsrepo-token>" }
```

`anyrt serve` joins both as guest at startup and logs
`overlays synced — agent ready`; until the first sync lands it answers
chat messages with a status bubble (ADR-009 §8, no polling). The
joining account must be on the same (prod) network — the join
handshake goes through the network's coordinator.

## Day-to-day

- Publish a change: `anyrt deploy --addr http://127.0.0.1:7003 --source
  repos/_agent --target <agentrepo-id>` (same for `repos/_connectors`).
  A running serve picks the change up on the next conversation — no
  restart (restart only for anyrt-binary/kernel changes).
- The repo server holds no secrets and runs no agent — it only hosts
  the two spaces; keep it up so joins and deploys work.
- Verify contents: `curl -s http://127.0.0.1:7003/v1/spaces` and
  `anyrt trace`-free — the deploy output itself lists every
  program/skill it created or updated.
