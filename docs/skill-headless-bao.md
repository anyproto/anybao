# Skill: run `any` + bao headlessly on a server, as a second device

For a coding agent or a script that has shell access to a Linux or
macOS box with no display ("the server"). The user already runs the
Any desktop app with bao on a laptop. The goal: the same account runs
on the server, and bao answers from the server, so the laptop can
sleep or close.

The skill covers five things:

1. Pick and download the release binaries from GitHub.
2. Authorize the server as a new device of the user's account.
3. Start the `any` server and `anyrt serve` (bao) and keep them up.
4. Move the active bao from the laptop to the server, and back.
5. Check the result and fix the common failures.

Companion docs: [`skill-bao-config.md`](skill-bao-config.md) (model
and config rows), [`config-secrets.md`](config-secrets.md) (keys),
[`adr/015-active-instance-election.md`](adr/015-active-instance-election.md)
(the election contract), and the `any` repo's `docs/23-devices.md`
(the devices registry).

## 0. The model in six sentences

An **account** is a 12-word recovery phrase. The desktop app calls it
the **login key**. A **device** is one `any` data dir that holds a
device key derived for that account. One account can have many
devices; they sync through the network. Bao is an app slug (`bao`) in
the account's **devices registry**, and the registry names at most
ONE active bao device at a time. Every other device that runs `anyrt
serve` is a **standby**: it is cheap, it answers nothing, and it takes
over within 10 seconds when the user switches to it.

Two things make the pair work:

- **Same versions.** Every device of the account should run the same
  `any` and `anyrt` builds as the desktop app. A server on an older
  build can read a space written by a newer build as EMPTY, silently.
- **Same channel.** The desktop app joins two read-only "overlay"
  spaces that hold bao's programs and skills. There is a stable pair
  and a nightly pair. The server must join the same pair as the
  laptop's app.

## 1. Ask the user three things

| question | why |
|---|---|
| Stable app or Nightly? (the nightly app is named "Any Nightly") | selects the release pair and the overlay spaces (§2, §4) |
| The 12-word login key, pasted at the moment of §3 only | authorizes the server as a device; never store it in a file in a repo |
| Does the account come from Anytype (the older app)? | an Anytype phrase derives the account at index 0, an `any` phrase at index 1 (§3) |

The login key is in the desktop app under **Settings ▸ Personal ▸
Login key**. The grid reveals on click; Copy and Download work without
a reveal.

## 2. Pick the versions

Three repos ship releases. `anyproto/any` and `anyproto/anybao` are
public; `anyproto/any-ui` (the desktop app) is private.

| what | repo | release tag | Linux asset |
|---|---|---|---|
| `any` server | `anyproto/any` | `vX.Y.Z` (stable), `vX.Y.Z-nightly.<date>.N` (pre-release) | `any-vX.Y.Z-linux-x86_64.tar.gz` + one `SHA256SUMS` |
| `anyrt` (bao runtime) | `anyproto/anybao` | `vX.Y.Z` | `anyrt-vX.Y.Z-x86_64-linux.tar.gz` + `.sha256` |

macOS assets: `any-…-darwin-arm64.tar.gz`, `anyrt-…-aarch64-darwin.tar.gz`.

**Stable app:** take the latest non-pre-release of each repo. That is
the pair the current desktop release bundles. Resolve the tags without
`gh`:

```sh
ANY_TAG=$(curl -fsSL https://api.github.com/repos/anyproto/any/releases/latest | jq -r .tag_name)
RT_TAG=$(curl -fsSL https://api.github.com/repos/anyproto/anybao/releases/latest | jq -r .tag_name)
echo "$ANY_TAG $RT_TAG"
```

When `gh` is logged in to the org, confirm the pair against the
desktop release notes: `gh release view --repo anyproto/any-ui` prints
`Embedded anybao agent: <sha>`; `git describe --tags <sha>` in an
anybao checkout names the anyrt tag. The `any` version is the
`ANY_VERSION` env of the latest `deploy-repos.yml` run in `anyproto/anybao`
(`gh run view <id> --log | grep ANY_VERSION`). On 2026-10-28 the pair
is Any desktop v0.1.14 = `any` v0.3.4 + `anyrt` v0.9.2.

**Nightly app:** take the latest `any` pre-release. anybao publishes no
nightly binaries; the nightly app embeds an anybao main commit. Build
`anyrt` from that commit (`make runtime` in a checkout, see the
anybao `CLAUDE.md`), or use the latest anyrt release when it is at or
after that commit.

## 3. Download, verify, unpack

Keep the `any` tarball layout. The binary finds the llama.cpp
libraries in `llamacpp/` next to itself; the search embedder needs
them.

```sh
set -euo pipefail
STACK=$HOME/anystack            # one root: bin/, data/, bao/
mkdir -p "$STACK"/{bin,data,bao,dl} && cd "$STACK/dl"

# any
curl -fsSLO "https://github.com/anyproto/any/releases/download/$ANY_TAG/any-$ANY_TAG-linux-x86_64.tar.gz"
curl -fsSLO "https://github.com/anyproto/any/releases/download/$ANY_TAG/SHA256SUMS"
grep "any-$ANY_TAG-linux-x86_64.tar.gz" SHA256SUMS | sha256sum -c -
mkdir -p "$STACK/bin/any-$ANY_TAG" && tar xzf "any-$ANY_TAG-linux-x86_64.tar.gz" -C "$STACK/bin/any-$ANY_TAG"
ln -sfn "$STACK/bin/any-$ANY_TAG/any" "$STACK/bin/any"

# anyrt
curl -fsSLO "https://github.com/anyproto/anybao/releases/download/$RT_TAG/anyrt-$RT_TAG-x86_64-linux.tar.gz"
curl -fsSLO "https://github.com/anyproto/anybao/releases/download/$RT_TAG/anyrt-$RT_TAG-x86_64-linux.tar.gz.sha256"
sha256sum -c "anyrt-$RT_TAG-x86_64-linux.tar.gz.sha256"
mkdir -p "$STACK/bin/anyrt-$RT_TAG" && tar xzf "anyrt-$RT_TAG-x86_64-linux.tar.gz" -C "$STACK/bin/anyrt-$RT_TAG"
ln -sfn "$STACK/bin/anyrt-$RT_TAG/anyrt" "$STACK/bin/anyrt"

"$STACK/bin/any" version      # prints "any vX.Y.Z (commit …)"
"$STACK/bin/anyrt" --help >/dev/null && echo anyrt ok
```

With `gh`: `gh release download "$ANY_TAG" --repo anyproto/any
--pattern 'any-*-linux-x86_64.tar.gz' --pattern SHA256SUMS` does the
same fetch.

**Linux needs `libffi.so.8`.** The `any` binary panics at start
without it: `panic: libffi.so.8: cannot open shared object file`.
Debian and Ubuntu: `sudo apt install libffi8`. Fedora: `sudo dnf
install libffi`. NixOS: run every `any` call through a shell that
provides libffi (the `any` repo's `nix develop`); do not hand-wire
`LD_LIBRARY_PATH`. macOS needs nothing.

## 4. Authorize the server as a device

`any init` with the phrase creates the data dir and derives a FRESH
device key for the existing account. The same phrase always gives the
same account id. This is the supported way to add a second device.

Read the phrase from stdin. Never pass it as a flag: flags land in the
shell history and in the process list. Never write it to a file inside
a repo or a backup.

```sh
DATA=$STACK/data
read -rs -p "login key (12 words): " PHRASE; echo
printf '%s\n' "$PHRASE" | "$STACK/bin/any" init --data-dir "$DATA" --mnemonic-stdin
unset PHRASE
```

An Anytype-derived account needs `--index 0` on this command. The
default index 1 is the `any` account. A wrong index creates a
different, empty account; the health check in §5 shows it.

`init` prints `{"accountId": "A…", "created": true}`. Compare the id with the laptop: in the
desktop app, `curl -s http://127.0.0.1:31330/v1/health | jq -r .account`
(the app shows its port under **Settings ▸ Personal ▸ Preferences ▸
Local server**). The two ids must match.

The data dir is now the device. Its `wallet.key` (mode 0600) is the
only secret on disk. Deleting the data dir forgets the device; the
account lives on in the phrase.

## 5. Start the `any` server

```sh
"$STACK/bin/any" run --data-dir "$DATA" --addr 127.0.0.1:7001
```

- No `--config` = the production network. The desktop app is on the
  production network. Do not pass the staging config here.
- The server is a foreground process. §8 wraps it in systemd.
- Bind to loopback. Nothing outside the box needs the HTTP API.
- Default `anyrt` address is `http://127.0.0.1:7001`, so 7001 keeps the
  bao config short.

Wait for health, then check the account:

```sh
A=http://127.0.0.1:7001
for _ in $(seq 120); do
  h=$(curl -fsS $A/v1/health 2>/dev/null) && [ "$(jq -r .bootstrapping <<<"$h")" = false ] && break
  sleep 1
done
jq '{account, version}' <<<"$h"
```

The `account` must equal the laptop's. The server now syncs the
account's spaces from the network; the first sync of a large account
takes minutes.

**Search index.** The default config indexes every space for bao's
search tool, and on first boot downloads the 639 MB embedding model
into `<data-dir>/models/` (progress in the log; boot does not wait).
Keep it on: bao's search runs against THIS server once it is active.
On a small box, lower the cost instead of turning it off:

```yaml
# $DATA/config.yaml — read by `any run` from the data dir root; or pass --config
index:
  local:
    threads: 2        # embedder CPU budget; default = cores - 1
    niceness: 15      # background priority
```

`index: { enabled: false }` turns search off entirely; bao's search
then returns nothing on this device.

Name the device so the laptop UI shows a readable row. The server
seeds the name from the hostname; override it:

```sh
"$STACK/bin/any" --addr 127.0.0.1:7001 devices register --name "server"
"$STACK/bin/any" --addr 127.0.0.1:7001 devices list      # every device + the active bao
```

## 6. Configure and start bao (`anyrt serve`)

`anyrt` reads one TOML host config. The tarball ships none. Fetch the
committed one that matches the channel, then add the server address:

```sh
cd "$STACK/bao"
# stable: the repo-root anybao.toml (production overlays)
curl -fsSL "https://raw.githubusercontent.com/anyproto/anybao/$RT_TAG/anybao.toml" -o anybao.toml
# nightly instead: deploy/nightly.toml carries the nightly overlay pair
# curl -fsSL "https://raw.githubusercontent.com/anyproto/anybao/main/deploy/nightly.toml" -o anybao.toml

# the file sets [agent] space = "bao" and [overlays]; add the rest at the top
{ printf 'addr = "http://127.0.0.1:7001"\n'; cat anybao.toml; } > anybao.toml.new && mv anybao.toml.new anybao.toml
cat >> anybao.toml <<'EOF'

[paths]
traces = "traces"        # raw blobs; relative to this file's directory
EOF
```

What the keys mean:

| key | default | meaning |
|---|---|---|
| `addr` | `http://127.0.0.1:7001` | the `any` server from §5 |
| `[agent] space` | `bao` | bao's home space; serve creates it when missing |
| `[agent] control_port` | `7010` | loopback control API: `GET /status`, `GET /election`, triggers |
| `[overlays] agent`, `connectors` | — | the read-only repo spaces + their public guest invites; serve joins them on first boot |
| `[paths] traces` | `traces` | blob directory for traces; the trace records themselves live in the `any` local store |

The overlay ids must be the pair the laptop's app joins. Stable:
`bafyreibpw22…3ba1m3rdoj9na` (agent) and `bafyreihklpm…3ba1m3rdoj9na`
(connectors), as in the repo-root `anybao.toml`. Nightly:
`bafyreih6db…rspqcv0zl1s1` and `bafyreihuok…rspqcv0zl1s1`, as in
`deploy/nightly.toml`. A nightly app with the stable pair, or the
reverse, is not an error at boot; bao just runs the other channel's
programs.

**Keys.** Bao's credentials (the model key, connector keys) are rows
of the account's secrets dataset. They sync to every device of the
account, end to end encrypted. The server reads the key the user
already entered on the laptop; nothing to copy. If a key is missing,
bao posts a credential request card in the chat; the user fills it in
the app. A `.connectors.env` file next to `anybao.toml` can seed keys
too (`ref=value` lines, see `config-secrets.md`); it is plaintext, keep
it mode 0600 and out of git.

Start:

```sh
cd "$STACK/bao" && "$STACK/bin/anyrt" serve --config-file "$STACK/bao/anybao.toml"
```

Expected log, in order:

1. the join of both overlay spaces and their sync;
2. `anyrt serving space=… chat=… control=127.0.0.1:7010 boot=…s`;
3. `election: peer <server-peer> — standby (the active bao is peer
   <laptop-peer>)` — correct while the laptop is still the active bao;
4. `wait set clear — agent ready`.

A standby answers no chat and runs no triggers. That is the intended
state until §7.

```sh
curl -s http://127.0.0.1:7010/election   # {app, enabled, active, peerId, winner}
curl -s http://127.0.0.1:7010/status
```

## 7. Switch the active bao to the server

The switch is the user's action, from the desktop app, and it works
only while the server's bao is running and beating.

1. Open **Settings ▸ Agent ▸ Devices** on the laptop. The screen has
   `This device` and `Other devices`. Each row shows the device name,
   OS and bao version, whether bao runs there, and which one `Answers
   chat`.
2. The server row shows a **Use server** button (the device name from
   §5). The button appears only when the row carries bao AND a fresh
   presence beat arrived from it. No button = `anyrt serve` is not
   running there, or it started less than a few seconds ago.
3. Click it. The app writes a claim naming the server. Within one
   election poll (10 s) the server logs `election: TAKEOVER — this
   device is now the active bao` and the laptop logs a stand-down. The
   row shows an in-between state until the server's beat flips to
   active; the app never shows an instant "online".
4. Check from the server: `curl -s http://127.0.0.1:7010/election`
   has `"active": true`. `any devices list` shows the server peer under
   `active.bao`.

**Switch back** is the same screen on the laptop: **Use this device**.
It is enabled only while the laptop's own bao beats.

Without the UI, from any device of the account:

```sh
"$STACK/bin/any" --addr 127.0.0.1:7001 devices activate bao                # claim for THIS device
"$STACK/bin/any" --addr 127.0.0.1:7001 devices activate bao --peer <id>    # hand it to another device
```

Nothing checks that the target runs. Handing the role to a device that
is off leaves chat unanswered until someone claims again.

Rules that save time:

- **Keep both serves alive.** A standby costs one registry read per
  10 s. The laptop app may stay open; it simply does not answer.
- **Do not switch and switch back within 10 s.** The election polls
  every 10 s; two devices can overlap for one poll and both answer.
- **Never prune a resting device.** `any devices remove <peer> --yes`
  tombstones that device key forever; only a fresh `any init` (a new
  data dir) brings the box back. Prune only a device that is gone for
  good.
- **The app asks after an outage.** When the active device stops
  beating for 60 s, the laptop app shows a prompt offering to take the
  role. If the server is down for maintenance, the user may accept or
  dismiss; nothing moves on its own.
- Bao can tell the user where it runs ("Where are you running?"); it
  cannot switch. Only the UI or the CLI moves the role.

## 8. Keep it running: systemd

Two user units, `any` first, bao after it. Logs go to the journal.

```ini
# ~/.config/systemd/user/any.service
[Unit]
Description=any server (account device)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=%h/anystack/bin/any run --data-dir %h/anystack/data --addr 127.0.0.1:7001
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=60

[Install]
WantedBy=default.target
```

```ini
# ~/.config/systemd/user/anyrt.service
[Unit]
Description=bao (anyrt serve)
After=any.service
Requires=any.service

[Service]
WorkingDirectory=%h/anystack/bao
ExecStart=%h/anystack/bin/anyrt serve --config-file %h/anystack/bao/anybao.toml
Restart=on-failure
RestartSec=5
TimeoutStopSec=120

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now any.service anyrt.service
sudo loginctl enable-linger "$USER"        # user units survive logout
journalctl --user -u anyrt -f
```

On NixOS, point `ExecStart` at a wrapper script that execs the binary
inside the `any` repo's `nix develop -c`. On macOS use two launchd
agents with the same two commands.

`anyrt serve` finishes in-flight runs on SIGTERM; give it the longer
stop timeout. `any stop --data-dir $DATA` also stops the server by
signal.

## 9. Upgrade, backup, remove

- **Upgrade both together, with the desktop app.** When the app
  updates, repeat §2 and §3 into new `bin/any-<tag>` and
  `bin/anyrt-<tag>` dirs, move the two symlinks, restart `any` then
  `anyrt`. Opening the data dir with a newer `any` is one-way; keep a
  copy of `$DATA` if a downgrade must stay possible.
- **Programs and skills need no restart.** They live in the overlay
  spaces and sync in; a running serve picks a change up on its next
  run. Restart only for a new `anyrt` binary.
- **Backup = the login key**, which the user holds. The data dir is a
  device: a cache of the account's spaces plus the device key. Files
  that were never synced (not durable) exist only in `$DATA/<account>/files/`.
- **Remove the device:** switch bao back to the laptop (§7), stop both
  units, then from the laptop `any devices remove <server-peer> --yes`.
  Then delete `$STACK/data`. Deleting the dir without the prune leaves
  a dead row; a dead row that holds the active claim leaves chat
  unanswered everywhere until another device claims.

## 10. Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `panic: libffi.so.8: cannot open shared object file` | Linux host lacks libffi | §3: install `libffi8`, or run under the `any` nix shell |
| `/v1/health` `account` differs from the laptop | wrong phrase, or wrong `--index` | stop, delete `$DATA`, repeat §4 with the right index |
| health stays `bootstrapping: true` | the first sync is still running | wait; check the `any` log for network errors |
| `space … has no programs` in the serve log | `any` on the server is older than the build that wrote the overlay | upgrade `any` to the current release (§2) |
| serve logs `standby` forever, no `Use server` button | the server's bao is not beating in the UI | confirm `anyrt serve` is up; reopen the Devices screen (it re-reads the registry on open) |
| nobody answers chat, every device says standby | the active claim names a device whose row still exists but is gone | from a live device `any devices remove <dead-peer> --yes`, then the survivors claim |
| `election: … chat is not answered here` while `active: true` | the `chat-watch` trigger is paused or pinned to another device | the `_any` skill / UI triggers: enable it, or unpin |
| `/election` or `/status` does not answer on 7010 | another process holds the control port; the bind failure is silent | `ss -ltnp 'sport = :7010'`; stop the stale `anyrt`, or set `[agent] control_port` |
| `any run` exits: lock held | another `any run` on the same data dir | `ps aux \| grep 'any run'`; stop the stale one |
| two answers to one message right after a switch | the ≤ 10 s election overlap | wait one poll between switches |
| bao says a key is missing on the server | the secrets row has not synced yet, or was never set | wait for sync; or enter it from the credential request card in the chat |
