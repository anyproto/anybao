# ADR-021: Credential entry — request-in-chat, store-read secrets, the Credentials dashboard

Status: **Accepted** (2026-08-26; §4 revised in review — the store is the
source of truth, no runtime write surface; amended the same day: the
value is **account-scoped**, not device-local). **§7 + §8 accepted
2026-09-14** (BOB-94: host binding shipped, declared refs, the open
`local.key.*` namespace for agent-authored programs, host-only card
types).
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

`about` is not a secret and is recorded in the trace like the rest of
the payload. Where the host reads it from depends on the ref's
namespace (§8.1): for the **declared** namespaces (`connector.key.*`,
`llm.key.*`) the descriptor is the one the deployed module exports in
`__any_credentials__` and deploy wrote into the overlay manifest — the
payload's `about` is ignored; for the open `local.key.*` namespace the
payload's `about` is the descriptor, taken once, when the row is
created, and `hosts` is mandatory. Each connector keeps its descriptor
in one module constant (`_CRED`, exported as `__any_credentials__ =
[_CRED]`) — the same place `_NOT_CONNECTED` lives, which shrinks to
one line ("GitHub not connected — enter the token in the chat prompt
above / Credentials").

`about.hosts` is **the** host-binding metadata ADR-011 §4 deferred
here: shown to the human at entry, persisted on the record (§2), and
enforced at hop zero (§7). It is a per-secret destination the human
sees and can edit, not a host allowlist patched in passing.

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
   non-secret metadata (no `localValue` touched; descriptor fields
   follow the §8.4 stamp rule — a miss never rewrites the label or
   hosts of a row that already exists):
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

### 7. Host binding — enforced at hop zero (accepted 2026-09-14, with §8)

A credentialed request whose URL host is not one of the row's `hosts`
fails typed `secret_host_mismatch` **before** the header is attached;
the same-origin redirect rule of ADR-011 §4 covers every later hop.
A `hosts` entry is a host name, optionally with a port
(`api.github.com`, `127.0.0.1:8737`): the host part is compared
case-insensitively and exactly (no suffix matching — `api.github.com`
does not admit `api.github.com.evil.io`); a declared port must match,
an undeclared one admits any. The row is the single truth: what the
card shows at entry is what the broker enforces, and the human edits
it in the dashboard (§6).

A row with no `hosts` is **unbound** and never injected:

- a declared ref (§8.1) always has hosts — they come from the
  manifest, stamped at boot, so an unbound declared row is a stale
  row from before this section; boot re-stamps it;
- a `local.key.*` request that names no `about.hosts` is refused at
  the miss (`secret_hosts_required`) — no card is posted without a
  destination, because the destination is the one thing the human
  is asked to check.

Enforcement is one check in the broker's credential injection
(`resolve_static` → the request), reading `hosts` off the same row it
reads the value from — no in-memory allowlist, nothing to keep in
sync. It runs after the effect is recorded (§4: resolve-after-record)
and before the request is sent, so the refusal is itself in the
trace and replays deterministically.

**The binding is per effect family.** http is the only carrier of a
credential today, so the binding is `hosts`. A future carrier — a
shell env value (ADR-024 masks credential-looking env today and
injects nothing), a database or MCP effect — has its own notion of a
destination and declares its own binding shape on the row
(`{"sh": {...}}`, never a reuse of `hosts`). A row is injectable only
through the families it names; every existing row names http
implicitly and nothing else, so a new carrier injects nothing until
the manifest (declared refs) or the human (open refs) grants it. The
invariant is the one this section states: a secret leaves the host
only toward a destination the human saw on the card.

### 8. Trust model for agent-requested credentials — `local.key.*` (accepted 2026-09-14, BOB-94)

#### 8.0 What the host can and cannot know

Every program — a reviewed overlay module, a working-space program
(ADR-013), an ad-hoc cell — runs in ONE guest interpreter. Guest-side
attribution of an effect to its calling module is forgeable by model
code: a function's `__code__` is assignable, so a frame's filename
proves nothing; a loaded module's facades are plain attributes any
cell can call; `span` names are a display override by contract
(ADR-003 §4b). The host therefore **never asks who is calling**. The
model rests on three host-verifiable facts: the ref's **namespace**,
the request's **destination host** (§7), and the overlay **manifests
deploy wrote** into spaces the guest cannot write (ADR-013 non-goal).

Consequence, stated once: a per-secret "allowed program" field
(BOB-109) cannot be enforced and is superseded by `hosts`.

#### 8.1 Two namespaces, one of them open

- **Declared refs — every namespace but `local.key.*`**
  (`connector.key.*`, `llm.key.*`, `google.key.*` for the search
  providers, the managed `connector.oauth.*` family of ADR-011 §3
  whose descriptors are the provider table). A deployed
  module lists the credentials it uses in a module-level
  `__any_credentials__ = [{"ref", "about": {"label", "hosts",
  "help"?, "note"?}}]` (the ADR-010 §1 self-documentation convention
  extended by one name; connectors export their `_CRED`, `llm@v1` one
  entry per SaaS provider of its backend table). `anyrt deploy`
  validates the shape (ref in a declared namespace, `hosts`
  non-empty) and writes the list into the overlay's manifest record
  (the hash-gated record deploy already keeps). serve reads every
  overlay's manifest at boot and on the hash-gated refresh into the
  **declared table** `{ref → about}` and stamps each ref's row from
  it (§8.4) — so the Credentials dashboard lists every connector's key
  before any miss, and a stored key that was never described gets its
  hosts. A miss on a ref outside `local.key.*` that is **not** in the
  table is refused typed `secret_ref_undeclared` (a bare name, a
  made-up namespace, a `connector.key.<x>` no overlay ships); the
  message names `local.key.*` as the namespace for agent-authored
  programs. The payload's `about` is ignored for declared refs.
- **Open refs — `local.key.*`.** Any program may name one. The
  descriptor is the payload's `about`, taken **once** when the row is
  created; `hosts` is mandatory (§7). A later miss or rejection stamps
  status only. Anything else the model could write onto the row it
  could write today; nothing it writes changes where the secret goes,
  because hosts are frozen at creation and editable by the human
  only.
`llm.key.*` for a **self-hosted** backend (`vllm`, `llama.cpp`,
`ollama`, `generic`) cannot be declared with a host — the host is the
tier's `base_url`, which the model may set through `config@v1`. Those
refs resolve like `local.key.*`: hosts taken from the tier's
`base_url` when the row is created, frozen, human-editable. A SaaS
provider's ref (`llm.key.anthropic`, `.openai`, `.openrouter`,
`.gemini`, …) is declared with its API host and cannot be redirected
by a `base_url` edit — the mismatch is typed and visible.

#### 8.2 The unreviewed card

A `local.key.*` request posts the same `credential_request`
attachment (§2) — one carrier, no new type — with text that carries
the warning, because old clients render only the text:

> ⚠ Code written by bao (reviewed by no one) asks for a credential:
> {label} (`local.key.{name}`). It will be sent only to {hosts}.

any-ui renders it as the credential card with a warning strip, the
hosts in emphasis, and a link to the run that missed the ref
(`requestedBy` — the trace shows the exact code, which is the only
provenance the host can vouch for; §8.0). The card names no program:
a name would be the requester's claim. The `agent` field of the
message carries the run as `debugLink`, as error replies do.

#### 8.3 What a reviewed key can do in unreviewed hands

With §7 enforced, a reviewed ref's secret reaches only its declared
hosts, whoever calls. A cell that calls `http.get` on `api.github.com`
with the GitHub ref has exactly the power of the reviewed connector's
raw `request()` — no new capability, and every such call is in the
trace. ADR-013 §4 ("an authored program may *use* existing credential
refs but cannot mint or read them") stands with this reading: use =
send to its own host.

#### 8.4 Store rules

- **Stamp rule.** The miss/rejection stamp (`ServeSecretStore::stamp`)
  writes descriptor fields (`label`, `hosts`, `help`, `note`) only
  when it **creates** a `local.key.*` row. It never writes them onto
  an existing row, and never for a declared ref (whose descriptor the
  boot stamp owns). Status, `requestedBy`, `rejectedAt/With`,
  `requestedIn/At` stamp as before.
- **`lastUsedAt`** — stamped once per run per ref, at the first
  successful injection (the audit column of §6; one write per run,
  not per effect).
- **Revocation** is the dashboard's Delete (§6) — unchanged; a deleted
  `local.key.*` row re-asks on the next miss with a fresh descriptor,
  which is the promotion path: the human ports the program into
  `repos/`, declares the ref under `connector.key.*`, and the
  `local.key.*` row is deleted by hand. The value does not migrate.

#### 8.5 Resolved questions (from the BOB-94 draft)

1. Loopback-only? **No.** Hosts are mandatory and enforced; the human
   reads the host on the card. A loopback rule would exclude the next
   integration without adding a check the human is not already asked
   to make.
2. Default expiry? **None in v1.** Revocation exists; expiry is a
   policy knob for a later amendment if it earns its keep.
3. Promotion? **Human, through deploy** (8.4); the key does not
   survive it.
4. A single "disable agent-initiated credential requests" setting?
   **Deferred**: cheap once 8.1 exists (refuse every `local.key.*`
   miss typed), not needed for the first cut.

#### 8.6 The card is host-only, enforced

§2 says only the host posts a `credential_request`; the code did not
enforce it (found 2026-09-13): `any@v1.chat_send` passes
`attachments` through verbatim and attachment `type` is an open enum
server-side, so a cell could post a native-looking card for any ref —
with `&setup=model` on the link — and the human would type a secret
into a row the model chose. Host binding does not help there: the
model picks the ref, the host later enforces hosts *it* stamped for
that ref. The broker's guest guard (the same place that refuses the
secrets dataset, ADR-011 §4) therefore refuses a guest chat write
whose `attachments` carry a **host-only type** — `credential_request`
and `credential_set` — typed `host_only`, message "credential cards
are posted by the host". The onboarding marker is covered by the
same rule: it rides that attachment's link. The card's creator field
cannot serve as the check (host and guest post as the same account),
so the boundary is the only place this can hold.

#### 8.7 The guest surface says so

`env(name)` reads the host's non-secret map (`Broker::env`) and is
documented as never a credential; the kernel's `http` verbs document
`credential=` (ref, header, prefix, about) in their docstrings so
`help(http.get)` teaches the mechanism — the anyscribe run
(BOB-87/102, 2026-09-13) showed bao reaching for `env()` because
`help(http.get)` printed only `get(url, **kw)`. `_core` and
`_meta_skill` teach `local.key.*` where programs are authored
(ADR-010 §7).

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
6. Kernel docstrings + skills (§8.7) — no contract change, ships
   first.
7. `__any_credentials__` on the connectors and `llm@v1`; deploy
   validates and writes the manifest list (§8.1).
8. Broker: declared table from the manifests, namespace rule,
   host binding (§7), stamp rule + `lastUsedAt` (§8.4), host-only
   attachment types on guest chat writes (§8.6).
9. Run wrapper: the unreviewed card text + `debugLink` (§8.2).
10. any-ui: warning strip, trace link, editable hosts, `lastUsedAt`
    in the dashboard (§8.2, §6).
