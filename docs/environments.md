# Environments — what runs where, and how to check prod before a release

Every environment is one or more `any` server accounts (a data dir = an
account; deleting it destroys the spaces it owns unless the mnemonic in
its `ACCOUNT.txt` is saved elsewhere) plus an `anyrt serve` reading one
`configs/anybao.*.toml`. For the existing environments, the server binary
is `~/any/any/bin/any`,
built from any main with the search tags and always run through
`nix develop` (it links `libffi.so.8`):

```fish
cd ~/any/any && nix develop -c go build -tags 'fts vector' -o bin/any ./cmd/any
cd ~/any/anybao && make runtime          # runtime/target/release/anyrt
```

The fleet rule (ADR-027 §6, ADR-010 §5): every server that touches a
space runs the same build. A server on an older build lists ZERO
objects from a newer space, silently. Once the post-port build opens
an account, no older binary opens it again; a data-dir copy is the
only rollback.

## Overview

| env | any server | anyrt control | config | repo spaces owned by | network |
|---|---|---|---|---|---|
| prod (stable channel) | mac (user acct, not this box) | 7010 | `anybao.toml` = `deploy/stable.toml` | stable repo acct **:7006** `datadirs/repo-prod4` (`bin/any --config configs/any-config.yml` = prod network + index off) | prod |
| nightly channel | nightly any-ui builds (identifier `io.anyproto.any.nightly`) | — | `deploy/nightly.toml` | nightly repo acct **:7011** `datadirs/repo-nightly` (`_agentrepo`, `_connectorsrepo` — same names as stable, any-ui hides them by name; account `A7gKeVnP…`, mnemonic `~/.any-accounts/repo-nightly-ACCOUNT.txt`; server `bin/any --config configs/any-config.yml` = prod network + index off) | prod |
| prod-smoke | `:7007` `datadirs/prod-test-user2` | 7014 | `anybao.prod.smoke.toml` — joins the REAL prod repo spaces | repo acct :7006 | prod |
| prod-test | `:7007` `datadirs/prod-test-user2` | 7014 | `anybao.prod.test.toml` — joins the `*-test` copies | test repo acct **:7008** `datadirs/repo-prod4-test` (`_agentrepo-test`, `_connectorsrepo-test`) | prod |
| staging | `:7134` `datadirs/staging` | 7016 | `anybao.staging.toml` | itself (one account owns bao + both repo spaces) | staging |
| android-poc (**proposed, not provisioned**) | desktop `:7140` `datadirs/android-poc-user`; phone uses the same throwaway account | desktop 7141; phone disabled | `configs/anybao.android-poc.toml` (planned) | new repo account `:7142` `datadirs/android-poc-repo`; frozen `_agentrepo-android-poc` / `_connectorsrepo-android-poc` | prod, synthetic data only |

prod-smoke and prod-test share the :7007 account and control port
7014: run one of the two serves at a time. Every provisioned server above
runs `bin/any`, built fresh from `~/any/any` main (`nix develop -c go
build -tags 'fts vector' -o bin/any ./cmd/any`); every account was
created 2026-09-18 on any b5be51c (ADR-029, CRDT mark 2). Two repo accounts because the prod network
allows three shareable spaces per account (`acl: make shareable: space
limit reached` on the fourth guest-key invite).

Space ids and guest-key invite tokens live in each config's
`[overlays]` block. Those per-environment config files remain local;
this tracked inventory names accounts and ports of the original host.

**The old prod repo account** (`datadirs/repo-prod3`, `:7004`, account
A6BkPD…, plus the old prod-test user `datadirs/prod-test-user` on
`:7005`) predates ADR-029 and still owns the pre-mark-2 spaces
(`_agentrepo` bafyreicbqmmvv…, `_connectorsrepo` bafyreiaeedfl…).
Clients on the pre-ADR-029 build (a mac serve that has not been
rebuilt, desktop installs before the nightly that carries any-ui
#1195) resolve programs from it. Nothing is deployed to it any more.
Keep it running until every client is on the new stack, then stop it;
do not open its data dir with the new binary. The still older
`repo-prod` (`:7003`, pre-ADR-027) is retired.

## Android PoC (proposed)

[ADR-034](adr/034-android-agent-host.md) is still Proposed. This is the
fixture specification for T4/T7/T10/T11, not a record of running services.
Check port availability before provisioning; never stop an unrelated
listener to obtain these ports. Do not reuse prod/prod-test accounts,
overlay spaces, control ports or data directories.

Use the user's Mac as the desktop peer and remote probe sender, with
sleep disabled for the test and independent network/provider monitoring.
The phone stays on a wall charger. The desktop Any peer and phone share
one new throwaway user account, with distinct device keys. A second new
repo account owns two shareable test overlay spaces, leaving its third
shareable-space slot unused. Keep account mnemonics outside all repos in
`~/.any-accounts/`; do not put keys in this document or trace exports.

Pin the desktop Any binary to the exact source tag/commit and CRDT version
of the phone's AAR, and desktop `anyrt` to the phone's runtime revision.
Pin each overlay by deployed content hash and record its source revision
independently; a Rust-only change does not force an identical redeploy. Store the binaries separately at
`~/.local/share/anybao-android-poc/bin/`, with their hashes in the test
manifest. A moving main/stable/nightly build is not this fixture. The
current AAR pin is not declared compatible until T1–T4 pass.

Use absolute data paths rooted at
`~/.local/share/anybao-android-poc/datadirs/`: `android-poc-user` on
`127.0.0.1:7140`, and `android-poc-repo` on `127.0.0.1:7142`. Both use the
same prod network as the phone, carrying synthetic data only. Deploy
`repos/_agent` and `repos/_connectors` through the new repo owner, then
record its two space IDs/invites and source hashes in the fixture manifest
and phone configuration, and the per-object pins of the
[setup manifest](#android-poc--explicit-setup-manifest-fields-stage-1-adr-034-25),
read back from the **phone's** local server once it holds the deployed
versions. Freeze these overlays for the whole run.

The planned `configs/anybao.android-poc.toml` has this shape; replace all
placeholders and use an absolute trace path before starting it:

```toml
addr = "http://127.0.0.1:7140"

[agent]
space = "<throwaway-bao-space-id>"
name = "bao"
control_port = 7141

[paths]
traces = "<absolute-private-path>/anybao-android-poc/traces"

[overlays]
agent = { space = "<android-poc-agent-space-id>", invite = "<guest-invite>" }
connectors = { space = "<android-poc-connectors-space-id>", invite = "<guest-invite>" }
```

Keep test API keys in a dedicated local secrets file beside this config,
passed explicitly with `--secrets-file`; never inherit real connector
credentials. Start `anyrt serve` with this config and verify that port
7141 belongs to that process and returns this fixture's identity. A
nonfatal control bind error must fail setup. Traces in Any's local store
belong to the throwaway data dir; `[paths].traces` isolates blob storage.

The Mac sends the 320 nonce messages through its test Any API, records
all replies through hour 81, and keeps evidence in
`~/.local/share/anybao-android-poc/evidence/<run-id>/`. During the phone
soak, desktop `anyrt` is stopped after handoff so it cannot mask a phone
failure. Start it only in the explicit activation/takeover tests; keep
the desktop Any peer and sender running. Record both peer IDs, elected
winner, resolved versions, listening PIDs, account/space IDs and sync
status before each phase. Inventory phone model via device properties
(the user says Phone (2a); a skill's `A063` label is not evidence).

### Campaign reservation and selective network fixture

This setup is proposed; no router, device window or user setting has been
reserved/changed. Stage 0 needs only a supervised phone/desktop fixture;
the full campaign also needs this network setup before T10 can pass.

Reserve the Mac on AC and the phone on a wall charger. Use the separate
`org.any.anykotlin.baoPoc` ID from Stage 0 onward. The `.dev` installation
belongs to devpass/shared staging: never overwrite, uninstall, sign it out
or change its account network. Reserve a non-overlapping idle window and
keep its server stopped without changing its data; if that is impossible,
use another device. On Mac, run `caffeinate -s`, collect `pmset -g log` and
use a sleep-inclusive sender clock. Any sleep invalidates an active soak.

With user agreement, reserve OS/Mainline updates outside the campaign.
Record fingerprint, patch, GMS/APEX versions and BOOT_COUNT at both ends.
OS/APEX changes or reboot invalidate; GMS/Play app updates without reboot
are logged and assessed using the plan's user-reviewed impact matrix,
not automatically treated as host failure. Never silently change security
settings. Inactivity reboot must be resolved before accepting the 80-hour
locked claim. Stage 0 includes the chosen supervised 8-hour screen-off
probe; no reservation or setting has been performed by writing this plan.

Use a dedicated controllable router/SSID, separately addressed phone/Mac,
and IPv4/IPv6 captures/rule counters. Inspect pinned v0.3.5 defaults for
nodeconf peers, iroh relays, pkarr discovery relays, direct hole-punched
P2P and Go push-node traffic. Earlier source observations are provisional.
Client isolation/mDNS suppression alone cannot block global P2P. The sync
partition rule defaults phone egress to deny, allowing only verified model
provider/DNS/essential device-control destinations. Explicitly deny sync
nodes/relays, direct TCP/UDP to Mac/other peers (IPv4, IPv6 and link-local)
and mDNS; inspect mobile-data/alternate-network bypasses too. If a bypass
cannot be excluded, the partition fixture is blocked.

Provide normal, phone-sync-unavailable/provider-reachable, and
phone-provider-unavailable/sync-reachable rule sets with independent
router-side expiry. Mac remains able to sync/claim. Prove dry-run phone
`networkPeers == localPeers == globalPeers == 0`, no phone global peer on
Mac, and no convergence of a nonce canary object until restore, while a
real provider request succeeds. Missing API fields require a revised
measurement contract, not inferred zero. Node-only loss with P2P alive
may be diagnostic, but cannot satisfy the partition arm. Re-resolve DNS/
nodeconf on drift and repeat controls; never rely solely on stale IP lists.

Forced Doze restoration is separate from router expiry. Run three 30 min
unplugged arms, with three probes each at +5/+15/+25 min. The operator stays
present, replugs at minute 30 and completes `deviceidle unforce` plus
`dumpsys battery reset` by minute 32, then verifies state. Late restoration
invalidates the arm. A Mac timer alerts the operator; no on-phone detached
shell timer or wireless ADB is presumed reliable. Wired host profiling is
pre-soak only; smoke/soak use self samples and desktop diagnostic beats.

T11's separate telemetry window uses a consented probe-APK VpnService with
baoPoc-only routing, complete IPv4/IPv6 TCP/UDP forwarding and per-flow
`getConnectionOwnerUid` attribution. Retain UID/package/APK mapping and
labelled positive controls. Go and Rust share one UID; this cannot separate
their flows. Corroborate with router DNS/TLS-name/IP captures and per-UID
netstats deltas, not blanket allowlisting of unclassified OS traffic.
Explicitly record/restore Private DNS or QUIC changes; unresolved app flows
and coverage gaps yield INCOMPLETE. Compare against pinned expected Any,
provider and Go push-node flows. Inspect absent Android FCM/telemetry
components and call paths; encrypted payload contents cannot be proven
from capture alone. Do not use a production Sentry DSN. Remove VPN routing
and verify normal networking before T10/smoke/soak.

The provider canary has its own test key; record whether it still shares
an account/project quota with the phone, and do not call it independent
quota evidence when it does. Collect host-status/diagnostic-freshness beats
and scheduled-heartbeat records live via the desktop peer, with a local
alert at 30 s beat staleness and invalidate at 60 s during smoke/soak;
hourly export grace expires at 65 min. Expected T10 partitions are separate
armed tests with local evidence, never missing-data exceptions in a soak.
Keep two raw evidence copies outside git until handoff, with hashes and
sanitized verdict/arm/ruling/restore artifacts in gate reports. Tooling and
evaluator are planned under tools/android-poc; the reviewable schema,
arm manifest and predicates live in docs/plans/android-host.

## The prod repo account

The prod `_agentrepo` / `_connectorsrepo` overlay spaces (ADR-009) are
owned by a dedicated repo account, separate from any user/bao account.
User accounts join them read-only via public guest-key invites;
`anyrt deploy` publishes through the repo server directly. The repo
server holds no secrets and runs no agent; it only hosts the spaces.
Keep it up so joins and deploys work.

Current identifiers (created 2026-09-18, post ADR-029, any `b5be51c`):

- prod repo account `A8eVQgE3dvkiean5zpRafkUS6eHjuxZpT2qD5erURbZAM1sN`, data dir
  `~/any/any/datadirs/repo-prod4`, server `:7006`
  (mnemonic in the data dir's `ACCOUNT.txt` and `~/.any-accounts/repo-prod4-ACCOUNT.txt`)
- test repo account `A6vWLYFNMH7HfndPBBzV1gu3QaVBsZVEUWZmh9vhrpFk6RS5`, data dir
  `~/any/any/datadirs/repo-prod4-test`, server `:7008` — owns the two
  `*-test` copies (mnemonic in its `ACCOUNT.txt` and `~/.any-accounts/repo-prod4-test-ACCOUNT.txt`)
- prod-test user account `A9ovg4RChkJdwEV9MrBrHg69dX1fpKmGx34ndj7kgtAFnAw1`, data dir
  `~/any/any/datadirs/prod-test-user2`, server `:7007`
  (mnemonic in its `ACCOUNT.txt` and `~/.any-accounts/prod-test-user2-ACCOUNT.txt`)
- `_agentrepo` `bafyreibpw22732c54hh6ut6ivikf22madleosimao6ba6pkjkg23do4xwm.3ba1m3rdoj9na` (:7006)
- `_connectorsrepo` `bafyreihklpm77hvin2xpey3wvzfjpalmgxi3edcbaybittg3zljzrq5exy.3ba1m3rdoj9na` (:7006)
- `_agentrepo-test` `bafyreibtqmlbnnpu5h2gmhtap5bmc7cti27wqtdli5e4ui7dlvdvhxw4wu.ljr8o8cta1f0` (:7008)
- `_connectorsrepo-test` `bafyreidwulcza4q63mfdvp7bxrdpwocawbzdwuntl2aaiugcskuc33jxtu.ljr8o8cta1f0` (:7008)
- invite tokens: `configs/anybao.toml` and `configs/anybao.prod.test.toml`
  `[overlays]`; a lost token is re-minted with `invite guest-key`
  (idempotent)

Retired accounts, mnemonics in `~/any/any/datadirs/ACCOUNTS-retired-*/`
or the archived data dirs: the pre-rename `~/.any-repo` (current
binaries list no spaces from it); the pre-ADR-027 `repo-prod` on
`:7003`, see Overview.

### Bring-up from scratch

```fish
cd ~/any/any

# 1. account — mnemonic prints ONCE; save it to <data-dir>/ACCOUNT.txt
nix develop -c ./bin/any init --data-dir ~/any/any/datadirs/<repo-dir>

# 2. server on the PROD network: no --config
nix develop -c ./bin/any run --data-dir ~/any/any/datadirs/<repo-dir> --addr 127.0.0.1:7006

# 3. the repo spaces (repeat for the -test copies)
curl -s -X POST http://127.0.0.1:7006/v1/spaces -d '{"name":"_agentrepo"}' | jq -r .id   # three shareable spaces per account: the -test copies go on a second account
curl -s -X POST http://127.0.0.1:7006/v1/spaces -d '{"name":"_connectorsrepo"}' | jq -r .id

# 4. public read-only guest keys (idempotent)
nix develop -c ./bin/any --addr 127.0.0.1:7006 invite guest-key <agentrepo-id>
nix develop -c ./bin/any --addr 127.0.0.1:7006 invite guest-key <connectorsrepo-id>

# 5. deploy both repos THROUGH the repo server, by raw space id (Deploying, above)
```

Then point clients at the spaces:

```toml
[overlays]
agent = { space = "<agentrepo-id>", invite = "<agentrepo-token>" }
connectors = { space = "<connectorsrepo-id>", invite = "<connectorsrepo-token>" }
```

Clients that need the ids: `configs/anybao.toml` (and the mac's copy),
`configs/anybao.prod.smoke.toml`, `configs/anybao.prod.test.toml` (the
`-test` pair), and any-ui's `src-tauri/src/agent.rs` DEFAULT_* plus its
mirror in `scripts/dev.sh` (its self-test pins them together).

`anyrt serve` joins both spaces as guest at startup and logs
`overlays synced — agent ready`; until the first sync lands it answers
chat messages with a status bubble (ADR-009 §8). The joining account
must be on the same network as the repo account: the join handshake
goes through the network's coordinator.

## Network selection

No `--config` = the embedded PROD nodeconf. `--config
configs/any-config.yml` is ALSO prod (it only turns indexing off).
Staging needs `--config configs/any-config-staging.yml`. It is easy to
get backwards; the `/v1/health` `account` field tells you which
account you started.

## Deploying

**Stable + nightly deploy from CI.** any-ui's `release.yml` job
`deploy-bao-repos` dispatches anybao `.github/workflows/deploy-repos.yml`
after a release publishes, with `{channel, anybao_sha, any_version}`
(the anybao commit and any release that release bundled). The workflow
starts the channel's owner account on the runner from
`REPO_DEPLOY_ACCOUNTS` (`{"stable": …, "nightly": …}`), checks its id
against `deploy/<channel>.account`, waits for the spaces to sync,
deploys `deploy/<channel>.toml`'s overlays, and waits for sync again.
Re-run a failed deploy from the anybao Actions tab (`workflow_dispatch`,
same inputs). Each run authorizes the owner as a new device.

**By hand** (break-glass for stable/nightly; the normal path for the
`-test` copies and staging): through the OWNING server, by raw space id
or by overlay name with `--config-file deploy/<channel>.toml` (a guest
join 403s `space.read_only`). Hash-gated: a running serve picks a change up on
its next run; restart only for anyrt-binary or kernel changes.

```fish
cd ~/any/anybao
# nightly (repo acct :7011) — by overlay name from deploy/nightly.toml
./runtime/target/release/anyrt deploy --config-file deploy/nightly.toml --addr http://127.0.0.1:7011 --source repos/_agent      --target agent
./runtime/target/release/anyrt deploy --config-file deploy/nightly.toml --addr http://127.0.0.1:7011 --source repos/_connectors --target connectors
# prod (repo acct :7006) — ids from anybao.toml [overlays]
./runtime/target/release/anyrt deploy --addr http://127.0.0.1:7006 --source repos/_agent      --target bafyreibpw22732c54hh6ut6ivikf22madleosimao6ba6pkjkg23do4xwm.3ba1m3rdoj9na
./runtime/target/release/anyrt deploy --addr http://127.0.0.1:7006 --source repos/_connectors --target bafyreihklpm77hvin2xpey3wvzfjpalmgxi3edcbaybittg3zljzrq5exy.3ba1m3rdoj9na
# prod-test copies (test repo acct :7008) — ids from configs/anybao.prod.test.toml
./runtime/target/release/anyrt deploy --addr http://127.0.0.1:7008 --source repos/_agent      --target bafyreibtqmlbnnpu5h2gmhtap5bmc7cti27wqtdli5e4ui7dlvdvhxw4wu.ljr8o8cta1f0
./runtime/target/release/anyrt deploy --addr http://127.0.0.1:7008 --source repos/_connectors --target bafyreidwulcza4q63mfdvp7bxrdpwocawbzdwuntl2aaiugcskuc33jxtu.ljr8o8cta1f0
# staging — one account, so through the user server itself; ids from configs/anybao.staging.toml
./runtime/target/release/anyrt deploy --addr http://127.0.0.1:7134 --source repos/_agent      --target bafyreicqvwx7jsk3cna7dchl2j33g5uvjoknhbnsbc5zw42b6oetvwgyr4.16dimysbbxowm
./runtime/target/release/anyrt deploy --addr http://127.0.0.1:7134 --source repos/_connectors --target bafyreigre22qdll77ukreryla4n56ydm7b3dzsklux2yxsaex6gqexzgdq.16dimysbbxowm
```

The deploy output lists every program/skill it created, updated or
left unchanged. After a deploy through :7006 (or :7008), poll until
`"state":"synced"` before stopping that server:

```fish
curl -s http://127.0.0.1:7006/v1/spaces/<space-id>/sync-status
```

## Check prod now, without waiting for the nightly

This runs the exact prod stack — the prod network, the real prod repo
spaces on :7006, any main, anybao main, any-ui main — from a throwaway
user account on this box, in the browser UI. Four terminals.

### 0. Binaries are current

`bin/any` must be the commit the nightly builds (any main; `/v1/health`
prints it) and `anyrt` must be built from anybao main:

```fish
cd ~/any/any && git log --oneline -1 && nix develop -c go build -tags 'fts vector' -o bin/any ./cmd/any
cd ~/any/anybao && git log --oneline -1 && make runtime
cd ~/any/any-ui && nix develop ~/any/any-ui -c git pull --ff-only     # any-ui main carries the new overlay ids (#940)
```

### 1. The prod repo server (terminal 1)

The owner of the prod repo spaces. Guests sync from the network nodes
once the spaces are there, but keep it up so deploys work:

```fish
cd ~/any/any
nix develop -c ./bin/any run --data-dir ~/any/any/datadirs/repo-prod4 --addr 127.0.0.1:7006
nix develop -c ./bin/any run --data-dir ~/any/any/datadirs/repo-prod4-test --addr 127.0.0.1:7008   # the *-test copies
```

### 2. The throwaway user account (terminal 2)

The account in `datadirs/prod-test-user2` was last opened by the
pre-port build. Opening it with the new one is the one-way door and its
bao space carries old-shape stores that the clean cut leaves unread, so
start from a fresh account (archive the old one, its mnemonic stays in
the archived `ACCOUNT.txt`):

```fish
cd ~/any/any
mv datadirs/prod-test-user2 datadirs/prod-test-user2.old-$(date +%F)
nix develop -c ./bin/any init --data-dir ~/any/any/datadirs/prod-test-user2   # mnemonic prints ONCE → datadirs/prod-test-user2/ACCOUNT.txt
nix develop -c ./bin/any run  --data-dir ~/any/any/datadirs/prod-test-user2 --addr 127.0.0.1:7007
```

No `--config`: prod network. The configs only carry `addr`, so nothing
in `configs/` changes when the account is recreated.

### 3. The bao (terminal 3)

`anybao.prod.smoke.toml` joins the REAL prod overlays via the guest
keys — the same spaces the desktop app will join after the nightly.
The secrets file beside the config supplies the LLM key and the
connector keys:

```fish
cd ~/any/anybao
./runtime/target/release/anyrt serve --config-file configs/anybao.prod.smoke.toml --secrets-file configs/.connectors.env.off
```

Watch the boot log for: the overlay join of both spaces, the
`catalog/general-chat` install on the bao space, the stores'
collections, and finally `anyrt serving space=`. "space … has no
programs" here means the guest account synced an overlay written by a
different build than `bin/any`.

### 4. The UI (terminal 4)

```fish
cd ~/any/any-ui
VITE_API_TARGET=http://127.0.0.1:7007 VITE_ANYRT_TARGET=http://127.0.0.1:7014 nix develop ~/any/any-ui -c pnpm dev
```

Open the printed URL, create a space (the bao home space is hidden by
the UI; drive tests in a user space), send a message to bao. The reply
must land in the same thread. Then read the run:

```fish
./runtime/target/release/anyrt trace ls   --addr http://127.0.0.1:7007 --program toolcaller
./runtime/target/release/anyrt trace show --addr http://127.0.0.1:7007 run_<id>
```

To exercise an UNRELEASED repo change from the same UI instead, deploy
it to the `*-test` copies (Deploying, above) and run the serve with
`configs/anybao.prod.test.toml`; nothing on prod changes.

## After the nightly lands (prod cutover on the mac)

1. Copy the mac data dir (the only rollback), rebuild `any` and `anyrt`
   there from main.
2. Replace the `[overlays]` block of the mac's `anybao.toml` with the
   one in `configs/anybao.toml` here.
3. Start the server, then the serve; send one message from the desktop
   app. A fresh desktop install from the nightly needs no config: the
   app rewrites its template with the new ids on every start.
4. Existing spaces start over for bao (ADR-027): no chat history,
   memory or email corpus from before; the email corpus re-syncs, the
   cron jobs rebuild the rest.

## Staging

```fish
cd ~/any/any
nix develop -c ./bin/any run --config ./configs/any-config-staging.yml --data-dir ~/any/any/datadirs/staging --addr 127.0.0.1:7134
cd ~/any/anybao
./runtime/target/release/anyrt serve --config-file configs/anybao.staging.toml
cd ~/any/any-ui
VITE_API_TARGET=http://127.0.0.1:7134 VITE_ANYRT_TARGET=http://127.0.0.1:7016 nix develop ~/any/any-ui -c pnpm dev
```

Staging is unreachable from prod clients (other network). One account
owns everything, so deploys go through :7134 (Deploying, above).

## Gotchas

- **An account from before the one-type model (any < b5be51c, CRDT
  mark 1) opened by a current server is SILENTLY empty.** Probed
  2026-09-17 on a copy of `datadirs/staging`: `GET …/types` lists only
  the built-in rows (the user types `program`, `agent_skill`, … are
  gone from the listing, not just their objects), every raw row still
  carries `any.types`, and `anyrt serve` boots clean — "credentials
  declared (0 refs)", "overlays probed (0 pending)", no warning — then
  re-provisions its stores under new type ids. A reporter's bao that
  "forgot everything" after an upgrade is this. Fix = a fresh account
  and fresh repo spaces (ADR-029 §8); there is no migration.

- `ps aux | grep 'bin/any run'` before debugging anything that "doesn't
  pick up" a change: a stale server holding a port makes a fresh config
  look broken.
- `anyrt` has no default config: pass `--config-file configs/<name>.toml`
  every time, prod included.
- The `/v1/health` `version` of every server in a fleet must match.
- Retired account mnemonics: `~/any/any/datadirs/ACCOUNTS-retired-*/`.

## android-poc — explicit setup manifest fields (Stage 1, ADR-034 §2/§5)

Explicit setup writes the `PreseededFixture` the phone's worker verifies at
preparation (`runtime/src/managed/preparation.rs`). Every field is read-only
input for the worker; it never derives or repairs one.

| Field | Source at setup |
|---|---|
| `account_root`, `account_id`, `self_peer_id` | the app-private account dir, `/v1/account` id, the self peer of `/v1/devices` |
| `bao_root`, `chat`, `config`/`secrets`/`triggers`/`runs` (`object_id` + `collection`) | the bao/v1 bundle root, the derived general chat, the four store objects and their declared collections |
| `frozen.source_revision` | the repos checkout the overlays were deployed from |
| `frozen.anyrt_revision`, `frozen.trace_schema`, `frozen.kernel_source_sha256`, `frozen.profile_sha256` | the trusted artifact manifest of the APK being set up |
| `frozen.overlays` | every configured overlay alias (`agent`, `connectors`, …); the working space is implicit under the reserved `working` alias and is never listed here |
| `frozen.programs[]` | per program: alias (`working` for working-space programs such as `android-poc-heartbeat@v1`), name, version, `fingerprint` = `deploy::fingerprint` as `anyrt deploy` computes it, `credentials_sha256` = SHA-256 of the raw `credentials` property text as the server returns it (SHA-256 of `""` when absent), and `manifest` = whether the program has a `program_manifest` record (deploy writes one only for a truthy manifest sidecar; a pinned manifest not yet on the device is `NotSynced`, an unexpected one is drift) |
| `frozen.skills[]` | per `agent_skill` object: alias, name, `sha256` of the markdown body **as the server's editor markdown route returns it** — not the repo `.md` file (the deployed body can differ in wrapping/trailing newline); read it back after deploy, from the phone's server (trap 6) |

Rules the worker enforces: an overlay without pins must hold no program and no
skill; a pin alias must be a configured overlay or `working`; every pinned
object must be present with exactly that content; a missing or unsynced
prerequisite is `NeedsSetup(NotSynced)` (main re-requests preparation), drift
is `NeedsSetup(Invalid)`.

Traps a setup implementer must know (N2a round-2 review, R2-10):

1. **Open-ref hosts.** A configured tier/provider/MCP key in an open namespace
   (`llm.key.*` for a self-hosted backend, `local.key.*`, `mcp.key.*`) is usable
   only if its secrets row already carries `hosts` covering the endpoint:
   `persist_local_secret`/`setSecret` write none, and the managed host never
   binds hosts on first use. Setup stamps `hosts` on such rows itself, or
   preparation is `NeedsSetup(Invalid)`.
2. **Working-space completeness.** The working space may hold only pinned
   programs and skills: a `_soul` copy, a user skill or an agent-authored
   program seeded there needs a `working` pin, or preparation is `Invalid`.
   Working-space programs declare no credentials (ADR-021 §8.1), whatever
   their `credentials` property says.
3. **Fingerprints.** `deploy::fingerprint(code, summary, any_tool, category,
   manifest)` over the in-space `program_source.code`, the object's derived
   `summary`/`any_tool`/`category` properties and the `program_manifest`
   record's `manifest` value (`{}` when absent) in Python-canonical JSON —
   `anyrt deploy` prints only `{spec: status}`; compute it from the server
   read-back (a desktop `anyrt` pins emitter is the planned helper).
4. **Skill hashes** are over the JSON `content` field of
   `GET …/objects/{o}/editor/editor_blocks/markdown`, not the HTTP body.
5. This section is the only place these fields are documented; the
   android-poc environment section above points here.
6. **Pin what the phone holds.** Read every pin back from the *phone's* local
   server, after it has synced the deploy — not from the desktop's right after
   deploying. A phone copy still at the previous version of a pinned program
   or skill fails its fingerprint or digest and is `NeedsSetup(Invalid)`, not
   `NotSynced`: preparation does not wait for the newer version (ADR-034 §2).
