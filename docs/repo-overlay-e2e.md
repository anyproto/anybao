# Two-account repo-overlay e2e (ADR-009 §8)

Point-in-time runbook (2026-07-21): prove the space-resident agent
against a REAL second account — a dedicated "repo" account publishes
the harness; the user's account joins its space read-only and runs the
agent entirely from it. The approve script is the TEMPORARY stand-in
for guest-key spaces.

Topology: existing server `127.0.0.1:7001` (`~/.any`, the user/bao
account) + new server `127.0.0.1:7003` (`~/.any-repo`, the repo
account). Both ride the same `staging.yml` network — the join
handshake needs its coordinator reachable.

## 1. Repo account + server

```fish
cd ~/any/any
./bin/any init --data-dir ~/.any-repo      # mnemonic prints ONCE — save it
./bin/any run --config ./any-config.yml --data-dir ~/.any-repo --addr 127.0.0.1:7003
```

(Flags override the config's unset dataDir/listen; nodeconf + embedder
settings are reused. Keep this running in its own terminal.)

## 2. Repo space + deploy the harness

```fish
set REPO_ID (curl -s -X POST http://127.0.0.1:7003/v1/spaces \
  -d '{"name":"bao-repo","spaceType":"anytype.space"}' | jq -r .id)

cd ~/any/anybao
./runtime/target/release/anyrt deploy --addr http://127.0.0.1:7003 \
  --source . --target $REPO_ID
# expect: programs created…, skills created…, readme updated
# (the kernel is embedded in the anyrt binary — nothing to publish)
```

## 3. Invite

```fish
cd ~/any/any
# public read-only guest key (idempotent; no approval daemon needed):
set TOKEN (./bin/any --addr 127.0.0.1:7003 invite guest-key $REPO_ID)
# invite-only alternative: `invite create` + manual `acl accept
# --permission reader` per joiner (the guest key replaced the interim
# approve_joins.py daemon)
```

## 4. Client: empty bao + overlay config

```fish
# nuke the old bao space (id via: ./bin/any --addr 127.0.0.1:7001 space get / list)
./bin/any --addr 127.0.0.1:7001 space delete <oldBaoId>

cd ~/any/anybao
cat > anybao.toml <<EOF
[agent]
space = "bao"

[overlays]
agent = { space = "$REPO_ID", invite = "$TOKEN" }
EOF
```

## 5. Serve

```fish
ANTHROPIC_API_KEY=... ./runtime/target/release/anyrt serve
```

Serve does NOT block on the join (ADR-009 §8 — no polling). Watch for:
1. `overlay "agent": join requested — the publisher's approval is pending`
2. `overlays still joining/syncing: ["agent"] — will answer with status until synced`
3. `anyrt serving space=… chat=…` (already watching the chat)

If you message bao BEFORE the sync lands, it answers with a status
bubble (`Not ready yet: overlay `agent` (space …) still joining/
syncing …`). The first message AFTER the space syncs logs
`overlays synced — agent ready` and is answered normally (the kernel
is embedded — nothing to download).

The recreated bao space stays EMPTY of programs/skills — serve is
space-only and must not bootstrap it (nothing deploys at serve start).

## 6. Verify the agent runs from the repo, writes to bao

Message bao ("remember that I …") via the UI, then:

```fish
# the run resolved code from the repo space
./runtime/target/release/anyrt trace ls --program toolcaller
./runtime/target/release/anyrt trace show <run_id>
#   spec agent:toolcaller@v1; module.resolve outputs carry spaceId == $REPO_ID;
#   --system shows the overlay skills in the prompt

# memory landed in the USER's bao space
set BAO_ID (curl -s http://127.0.0.1:7001/v1/spaces | jq -r '.spaces[] | select(.name=="bao").id')
set BRAIN (curl -s http://127.0.0.1:7001/v1/spaces/$BAO_ID/agent/brain | jq -r .objectId)
curl -s -X POST http://127.0.0.1:7001/v1/spaces/$BAO_ID/query \
  -d "{\"objectId\":\"$BRAIN\",\"dataset\":\"agent_memory_items\"}" | jq '.records[].content'

# read-only held: membership is reader, and the repo space gained no new objects
cd ~/any/any && ./bin/any --addr 127.0.0.1:7003 members list $REPO_ID
```

## Notes

- First sync of a joined space can lag the join; serve just reports
  status until it lands (no polling, ADR-009 §8).
- `reader` is the server's view-only permission; write rejection is
  enforced by the ACL server-side.
- Dev loop against the repo: edit locally → `anyrt deploy --addr
  http://127.0.0.1:7003 --source . --target $REPO_ID` — the running
  serve picks changes up on the next conversation (probe cache).
