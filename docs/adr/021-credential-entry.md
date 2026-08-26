# ADR-021: Credential entry — request-in-chat, store-read secrets, the Credentials dashboard

Status: **Accepted** (2026-08-26; §4 revised in review — the store is the
source of truth, no runtime write surface)
Builds on: ADR-006 §3 (device-local secrets on the derived object),
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
  localValue: <device-local>}`; two-step write `persist_local_secret`
  (`serve.rs:492`); open ref set; the guest read-guard.
- The chat `agent` field `{name, debugLink?, done}` — shipped (the
  ADR-011 §5.1 prerequisite is met), written by the guest
  (`toolcaller@v1:507`) and by Rust with no LLM in the loop
  (`serve.rs:1197`).
- Chat `attachments`: `{id → {type, link}}`, `type` is an **open
  enum by design** in `any` (`internal/chat/handler.go:270-300`),
  unknown types render as chips in any-ui. This is the one carrier
  that needs no `any` change; `agent` is a closed allowlist.
- any-ui's own `any` client on the same device as the embedded serve
  (`anyrt::serve::start(cfg)`, any-ui `agent.rs:209`) — it can write
  the device-local field of a row itself.
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
   **`rejected`**: the destination answered **401** to a request
   carrying the ref — the stored value is wrong. The broker stamps it
   (`rejectedAt`, `rejectedWith: 401`) and queues the ref exactly like
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

### 4. The store is the source of truth; the broker reads it at injection time

There is no runtime write surface and no in-memory secret map to keep
consistent. The `agent_secrets` row IS the credential: whoever can
write the device-local field (the UI's own `any` client on the same
device, `.connectors.env` at boot, the OAuth flow) writes it, and the
broker **reads the row when it injects** — one loopback `query`
against the secrets object per credentialed http effect, the same
call `bootstrap_secrets` makes today, made *after* the effect is
recorded so the trace stays value-free and replay never reads the
store. Cost is one local query in front of an outbound request;
consistency stops being a property to maintain: a retry injected
into a live conversation sees the new value because nothing was
snapshotted.

Why this replaces the per-run clone of `cfg.secrets` (`serve.rs:1048`)
rather than sharing it: the map was inherited from the env-var era —
the store was added as persistence *for* the map. Managed OAuth
already lives outside it (`OauthState`, ADR-011 §6, which caches only
the ~1h access token, for refresh cost); static refs now match.

Seeds remain, as the **no-store fallback only**: `anyrt run` (no
space store) and a server without the secrets object keep resolving
from the seeded map, exactly as documented for those modes. In serve
with a store, `bootstrap_secrets` still writes hard/soft seeds
through at boot (now also stamping `status: "set"`), then the map is
dropped; the broker's static-ref path is *store if present, else
seeds*.

The write is the existing two-step (`persist_local_secret`): synced
`{key, secret: true, status: "set", updatedAt, label?, hosts?, …}`
upsert, then the device-local `$set` of the value. Empty value =
delete (the hard-seed semantics). Managed OAuth sub-refs
(`connector.oauth.*.refresh`) are not user-writable from the UI; the
`.client_id`/`.client_secret` refs are ordinary rows. The guest
read-guard is unaffected — it blocks *guest* http against the secrets
object, not the UI's client, and the guest never needs the value.

No restart, anywhere: `Help → Import connector keys` performs the
same writes per entry through `AgentHandle::set_secret` — the lib
embedder's write (ADR-009 §6 surface), not a runtime route — instead
of `restart_agent_with_secret_overrides`. Managed OAuth sub-refs read
the row too (`OauthState::secret` falls back to the store), so
`.client_id`/`.client_secret` entered in the UI need no restart.

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
password input with Save, Delete. Values are never read back (the
device-local field is not in the synced window; the UI only writes
it, §4). Managed OAuth rows render their `.account` /
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

- Remote instances: the write path is same-device (the UI's client
  and the serve share one `any` device store). A remote serve needs the ADR-011 §5.1 transport-B
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
