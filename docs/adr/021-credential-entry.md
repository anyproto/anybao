# ADR-021: Credential entry — request-in-chat, store-read secrets, the Credentials dashboard

Status: **Accepted** (2026-08-26; §4 revised in review — the store is the
source of truth, no runtime write surface; amended the same day: the
value is **account-scoped**, not device-local)
Builds on: ADR-006 §3 (secrets on the derived object — the device-local
scope of that section is superseded by §4 here),
ADR-008 §1 (credential refs), ADR-011 §4 (host binding deferred to
*this* design), §5.1 (the `agent.request` channel), ADR-018 §3 (the
chat responder), `docs/config-secrets.md`.
Retires: the `.connectors.env` import as the *only* UX path (Help →
Import connector keys stays as the bulk/CLI path; it is no longer how
a user learns a key is missing or enters one).

## Context

Today a missing key is discovered by reading prose. The broker fails
the http effect with `RuntimeError: no secret for credential ref
"connector.key.github"` (`broker.rs:759`), each connector
string-matches that message and returns a `_NOT_CONNECTED` paragraph
telling the human to build a `.env` file and use *Help → Import
connector keys* (`github@v1:41`), and any-ui's import restarts the
whole agent because the broker gets a **per-run clone** of
`cfg.secrets` (`serve.rs:1048`) and `bootstrap_secrets` only runs at
serve start. The LLM key is worse: `llm@v1` names the same ref
mechanism (`credential: {ref: prov.api_key_ref}`, `llm@v1:330`), so a
missing Anthropic key throws inside `toolcaller`, the run dies, and
the human gets "Something broke mid-run (trace …): RuntimeError: no
secret for credential ref …" (`serve.rs:1176-1204`) — the one message
the agent can't help with, because the agent needs the key to speak.

What already exists and is reused unchanged:

- The store: `agent_secrets` on the `bao/secrets/v1` bundle child
  (`serve.rs:293`), rows `{key, secret: true, <synced metadata…>,
  value}`; write `persist_local_secret`
  (`serve.rs:492`); open ref set; the guest read-guard.
- The chat `agent` field `{name, debugLink?, done}` — shipped (the
  ADR-011 §5.1 prerequisite is met), written by the guest
  (`toolcaller@v1:507`) and by Rust with no LLM in the loop
  (`serve.rs:1197`).
- Chat `attachments`: `{id → {type, link}}`, `type` is an **open
  enum by design** in `any` (`internal/chat/handler.go:270-300`),
  unknown types render as chips in any-ui. This is the one carrier
  that needs no `any` change; `agent` is a closed allowlist.
- any-ui's own `any` client — it can write a row itself.
- ADR-011 §4 explicitly parked per-secret destination metadata ("each
  secret ships with its host/scope restrictions, most likely bundled
  with its connector, the human double-checks the scope at entry")
  on this design.

## Decision

### 1. A credential is described by its connector (descriptor)

Every program that names a ref passes a **descriptor** alongside it in
the http payload. The guest credential shape becomes

```python
{"ref": "connector.key.github", "header": "Authorization", "prefix": "Bearer ",
 "about": {"label": "GitHub personal access token",
           "hosts": ["api.github.com"],
           "help": "https://github.com/settings/personal-access-tokens/new",
           "note": "Repository: Issues, Pull requests, Contents, Metadata; …"}}
```

`about` is optional (the six connectors and `llm@v1` add it; an
agent-authored connector without it still works, the request is just
terser). It is not a secret and is recorded in the trace like the rest
of the payload. Each connector keeps it in one module constant
(`_CRED`) — the same place `_NOT_CONNECTED` lives today, which shrinks
to one line ("GitHub not connected — enter the token in the chat
prompt above / Credentials").

`about.hosts` is **the** host-binding metadata ADR-011 §4 deferred
here. In this ADR it is *shown* to the human at entry and persisted on
the record (§2); enforcing it at hop zero is §7, a follow-on phase, so
nothing here re-opens the "never patch an allowlist in passing" rule.

### 2. Missing secret = a typed failure and a host-emitted request

The broker's miss becomes typed: `EffectFailure {type_:
"SecretMissing", message: "no secret for credential ref \"…\""}` —
the message stays byte-identical (the string-matching connectors and
ADR-011 §7 keep working), the type is what the runtime acts on.
A request whose credential ref is `null` names no secret at all (an
LLM tier on a keyless local server, ADR-005 §1.6): nothing is
resolved, nothing is marked missing, no request is posted.

On a miss the **host**, not the guest, raises the request — the LLM
key case has no guest that could, and a host-only emitter means the
model cannot fabricate a "paste your key" prompt for a ref it invented
(the phishing argument of ADR-011 §1 applied to secrets). Mechanism:

1. The broker upserts the ref's `agent_secrets` row with synced,
   non-secret metadata (no `localValue` touched):
   `{key, secret: true, status: "missing", label, hosts, help, note,
   requestedAt, requestedBy: <run id — the trace that missed it>,
   requestedIn: <chatId>}`. The dataset is declared, not dynamic (a
   system dataset): these are declared fields — `hosts` an array,
   the two timestamps `datetime` — and there is no reconcile for
   pre-existing datasets (no backward compatibility: recreate).
   `status ∈ {missing, rejected, set}`; `set` is stamped by §4 and by
   `bootstrap_secrets` for every stored/seeded value at boot.
   **`rejected`**: the destination rejected the credential on a request
   carrying the ref — **401**, Google's **400** whose body names
   `API_KEY_INVALID` (Gemini answers a bad key that way, never 401),
   or Anthropic's **400** `anthropic-workspace-id is required when
   authenticating with an identity-linked API key` (a key created
   without a single workspace; bao never sends that header, so the
   key is the wrong kind — the card's `note` asks for a
   workspace-scoped one, ADR-005 §1.6) — the stored value is wrong.
   The broker stamps it (`rejectedAt`,
   `rejectedWith: <status>`) and queues the ref exactly like
   a miss, so a wrong key gets the same card ("…was rejected — enter a
   new one") with no connector involvement and no model-callable
   "ask for a secret" surface. 403 is not a rejection (usually scopes);
   managed OAuth refs are excluded (their own reconsent path).
2. The run wrapper (`start_or_inject`, the same place the "Something
   broke" text is posted) sends **one** chat message per
   `(chat, ref)` while `status == missing`:

   ```json
   {"text": "I need a credential to continue: GitHub personal access token (`connector.key.github`, used for api.github.com).",
    "agent": {"name": "<agent>", "done": true},
    "attachments": {"credreq": {"type": "credential_request",
                                "link": "any://o/<secretsObjectId>?key=connector.key.github"}}}
   ```

**Onboarding marker (BOB-78).** When the missing ref is the codegen
tier's `api_key_ref` and every `llm.tier.*` row still equals the
embedded seed (`config_defaults.json` — the space has never chosen a
provider), the row link carries the marker as a query parameter —
`any://o/<secretsObj>?key=<ref>&setup=model` — and the text is
provider-neutral ("Before I can think, I need a language model…").
The attachment TYPE and shape are unchanged on purpose: the server's
strict body rejects an extra attachment field (`400
request.unknown_field`) but stores the link opaquely; a client that
knows the parameter renders the provider chooser (presets → the three
tier rows, then the chosen key), an older client reads only `key` and
renders the plain card — the default path still works. Derived from
state, never tracked — re-running onboarding is deleting the tier
rows. A rejected key never carries the marker.

   `link` names the row; everything the UI renders comes from the
   row, so the message carries no payload and stays immutable-safe
   (chat attachments are create-only). Dedup: the request is posted
   when the run *ends* (ok or error) and only if the ref is still
   `missing` and the row's `requestedIn` is not this chat; posting
   stamps `requestedIn`/`requestedAt`, the write that sets the value
   clears `requestedIn` — so a retry that fails again re-asks.
3. When the failed ref is the **LLM key** (missing, or the provider's
   401) the run cannot produce a reply; the wrapper's error branch
   posts the request *instead of* "Something broke" — any run that
   failed with a queued credential request is reported by that
   request alone. On serve boot with no LLM key nothing is posted
   proactively — the first user message triggers it, and the
   Credentials dashboard (§5) shows the row regardless.

The agent still gets the connector's `{ok: false, error}` in-turn and
finishes its reply normally ("I can't reach GitHub until the token is
in — I've asked for it above"); the request bubble is the host's.

### 3. The UI renders the request inline

any-ui branches in `ChatView.renderMessageRow` on a
`credential_request` attachment and renders, in place of the plain
bubble: the label, the ref, **hosts** ("this key will be sent only
to api.github.com" — the human-double-checks-the-scope moment of
ADR-011 §4), the help link/note, a password input and a Save button.
Once the row's `status` flips to `set` the same message renders as
"✓ set <relative time>" (live from the dataset window, no message
edit). Unknown/old clients still show a chip → the Credentials
dashboard.

### 4. The store is the source of truth; the value is account-scoped; the broker reads at injection time

There is no runtime write surface and no in-memory secret map to keep
consistent. The `agent_secrets` row IS the credential, and its `value`
is an ordinary **synced** field: whoever holds the space writes it —
the UI on any device (desktop, phone), `.connectors.env` at boot, the
OAuth flow — and the broker **reads the row when it injects**: one
loopback `query` against the secrets object per credentialed http
effect, made *after* the effect is recorded so the trace stays
value-free and replay never reads the store. Consistency stops being a
property to maintain: a retry injected into a live conversation sees
the new value because nothing was snapshotted, and a key entered on a
phone reaches the desktop that runs the agent through sync.

**Why account-scoped is safe (the ADR-011 "at-rest" gate):** any-sync
encrypts every change with the space's ACL read key before it leaves
the device (`objecttree/changebuilder.go`, `readKey.Encrypt`); sync
nodes only ever hold ciphertext, and the bao space is owner-only. The
value therefore never exists in cleartext outside the account's own
devices — the same guarantee a device-local field gave, minus the
"only on the device that typed it" restriction that made remote entry
impossible. This supersedes ADR-006 §3's device-local scope for
secrets.

What stays: the guest read-guard (the whole secrets object is refused
to guest http), `config.get`'s namespace refusal, and
resolve-after-record. Managed OAuth refresh tokens sync the same way —
the active device (ADR-015) refreshes; the rest hold ciphertext.

Why this replaces the per-run clone of `cfg.secrets` (`serve.rs:1048`)
rather than sharing it: the map was inherited from the env-var era —
the store was added as persistence *for* the map. Seeds remain as the
**no-store fallback only** (`anyrt run`, a server without the secrets
object); in serve with a store, `bootstrap_secrets` writes hard/soft
seeds through at boot (stamping `status: "set"`), then the map is
dropped except for seeds the store refused.

The write is one per-path `modify`: `$set key/secret/status/updatedAt/
value`, `$unset requestedIn`. Empty value = delete. Managed OAuth
sub-refs (`connector.oauth.*.refresh`) are not user-writable from the
UI; the OAuth client is bundled with the connector (ADR-011 §3) and
kept as non-secret `meta` rows.

No restart, anywhere: `Help → Import connector keys` performs the
same write per entry through `AgentHandle::set_secret` — the lib
embedder's write (ADR-009 §6 surface), not a runtime route.
`OauthState::secret` falls back to the store too.

`config.get` refuses the `connector.key.*` and `llm.key.*`
namespaces wholesale (amends ADR-011 §9's presence rule for static
refs: with no map there is no presence to check).

### 5. The "credential set" message is the human's

After a successful upsert **the UI sends a normal user message**:

```json
{"text": "Set credential `connector.key.github` (GitHub personal access token).",
 "attachments": {"credset": {"type": "credential_set",
                             "link": "any://o/<secretsObjectId>?key=connector.key.github"}}}
```

Rendered by the same row branch as a compact "connector updated" row.
Because it is a user message, the chat responder (ADR-018 §3) starts
or injects a run exactly as for any other text — no new trigger, no
special-casing in the watcher — and the model sees "the token is in"
in its window and retries (or does the thing for the first time; it
has the original request in the same window). The UI awaits the
row write before sending, so the value is stored before the run can
read it. `Help → Import` posts one such message per ref it set, into
the general chat.

Why not have the serve post it: the human did the action, the
creator field should say so; and a serve-authored `agent` message is
filtered by the responder as its own output, which would force a new
carve-out in the watcher.

### 6. Credentials dashboard

A fourth agent mini-app next to Soul / Scheduled / Skills
(`AGENT_MINI_APPS`, `spaceMiniApps.ts:122`), modelled on **Scheduled**
(dataset on a bao-bundle child, record ids as selection, `.loose()`
schemas, firehose convergence): anchor = `deriveBundleChild(space,
'bao/v1', {seed: 'bao/secrets/v1'})`, window = `agent_secrets`.

Rows shown: every stored/`missing` row, plus the always-present
`llm.key.<provider>` for the configured provider. Per row: label,
ref, status (`missing` / `set <when>`), hosts, help link, a
password input with Save, Delete. Values are never displayed (the
UI strips `value` at its read seam and only ever writes it, §4). Managed OAuth rows render their `.account` /
`.granted_scopes` and a Connect/Disconnect that call `googleAuth`
through the existing `POST /run` — no new surface.

### 7. Host binding (follow-on phase, designed here)

With `hosts` on every row and shown at entry, enforcement is a broker
check at hop zero: a credentialed request whose URL host is not in
the row's `hosts` fails typed `secret_host_mismatch` (the same-origin
redirect rule of ADR-011 §4 already covers later hops). Rows with no
`hosts` (agent-authored connectors that gave no `about`) stay
unbound until the human edits the hosts field in the dashboard —
which is where "the design must also cover the flow where the agent
authors a new connector" lands: the agent's connector requests the
secret, the human sees "no host restriction" in red and types one.
Not shipped in this ADR's first cut; it is listed so the record
shape carries it from day one.

## Out of scope

- A runtime on a machine outside the account (a hosted bao) — the
  value syncs within the account's devices only; a hosted runtime
  would need the ADR-011 §5.1 transport-B hop. A remote serve needs the ADR-011 §5.1 transport-B
  hop for the *value*; the request/`credential_set` messages are
  already the right channel for it.
- Account-scoped (synced) secrets — still gated on at-rest guarantees.
- Editing a message in place: chat attachments are immutable; state
  lives on the row, deliberately.

## Phasing (one topic = one commit)

1. Runtime: typed `SecretMissing`; store-read at injection time
   (seeds as the no-store fallback); `status/updatedAt` stamping
   (`bootstrap_secrets` marks boot-loaded refs `set`). Testable by
   writing a row on a rig mid-conversation.
2. Runtime: `about` descriptor → row metadata; host-emitted
   `credential_request` (incl. the LLM-key error branch); dedup.
3. Connectors + `llm@v1`: `_CRED.about`, `_NOT_CONNECTED` shrinks;
   `_core.md` tells the agent a request bubble was posted for it.
4. any-ui: the two-step row write in the API layer;
   `renderMessageRow` branch for `credential_request` /
   `credential_set`; Import-keys switches to row writes (no restart); the two runtime strings that name
   "Help > Import connector keys" (`broker.rs:886`, `serve.rs:468`)
   get updated with it.
5. any-ui: Credentials dashboard.
6. Host binding enforcement (§7) — its own ADR amendment when picked
   up.
