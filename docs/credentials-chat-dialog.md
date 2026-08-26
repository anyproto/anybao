# Credentials in chat — client reference

How a client renders and answers bao's credential prompts, and the
Credentials tool. This is the contract every client (desktop, web,
iOS, Android) implements; the canonical design is
[ADR-021](adr/021-credential-entry.md) (+ [ADR-011 §3](adr/011-oauth-credentials.md)
for OAuth). Reference implementation: any-ui PR
[#632](https://github.com/anyproto/any-ui/pull/632) —
`src/lib/api/credentials.ts`, `src/lib/sync/credentials.ts`,
`src/components/credentials/*`, `src/components/chats/ChatView.tsx`
(`renderMessageRow`), `src/components/chats/messageView.ts`
(`deriveAgentTyping`).

Everything below is data a client already has (chat messages + one
dataset). There is no client↔runtime RPC.

## 1. The two messages

Both are ordinary chat messages in the space's general chat. They are
recognised by an **attachment `type`** (an open enum server-side —
clients that don't know the type show a plain chip, which is the
intended fallback).

### `credential_request` — posted by the runtime (agent-authored)

```json
{
  "text": "I need a credential to continue: GitHub personal access token (`connector.key.github`, used for api.github.com).",
  "agent": { "name": "bao", "done": true },
  "attachments": {
    "credreq": {
      "type": "credential_request",
      "link": "any://o/<secretsObjectId>?key=<ref>"
    }
  }
}
```

- `link` names the **secrets object** (`<secretsObjectId>`) and the
  **ref** (`key=`) — the row id in the `agent_secrets` dataset (§2).
  Parse both; nothing else is in the message.
- `text` is the human line; it differs by cause and is shown as the
  card headline:
  - first ask: *"I need a credential to continue: {label} (`{ref}`, used for {hosts})."*
  - rejected key (the destination answered 401): *"The {label} was rejected by {hosts} (401) — please enter a new one (`{ref}`)."*
- Only the runtime host posts these (never the model). One per
  (chat, ref) until the value is set; a rejection re-asks.
- If the ref is still unanswered and bao is asked again, the runtime
  posts a plain agent message instead of a second card:
  *"Still waiting for `{ref}` — see the credential prompt above. Last attempt: {error}"*. Render it as a normal bubble.

### `credential_set` — sent by the **client as the user** after a save

```json
{
  "text": "Set credential `<ref>` (<label>).",
  "attachments": {
    "credset": {
      "type": "credential_set",
      "link": "any://o/<secretsObjectId>?key=<ref>"
    }
  }
}
```

- Send it **after** the row write (§3) has been acknowledged — the
  runtime retries the moment it sees this message and must find the
  value.
- It is the user's message (their `creator`), no `agent` field. The
  runtime's chat responder starts a run from it and appends an internal
  cue telling the model to carry out the request that needed the key —
  the client does nothing else.
- Send exactly the text above (label = the row's `label`, or the ref
  when absent); the runtime and other clients match on the attachment,
  not the text, but keep it stable.

## 2. The row — `agent_secrets`

Per space, on the **secrets object**: the `bao/v1` bundle's child with
seed `bao/secrets/v1`. Resolve it by seed (the bundle-child API, the
same way the triggers anchor `bao/triggers/v1` is resolved) — or take
`<secretsObjectId>` straight from the attachment link. Dataset name:
`agent_secrets`, record id = the ref.

| field          | type                  | who writes            | meaning                                                                          |
| -------------- | --------------------- | --------------------- | -------------------------------------------------------------------------------- |
| `key`          | string (= id)         | runtime / client      | the ref: `connector.key.<x>`, `llm.key.<p>`, `connector.oauth.<p>.<sub>`         |
| `secret`       | boolean               | runtime / client      | `true` for credential rows, `false` for OAuth metadata rows                      |
| `value`        | string (synced, E2E-encrypted by any-sync) | client / runtime | the secret — account-scoped; **write-only for clients** (§5)        |
| `status`       | `missing` \| `rejected` \| `set` | runtime / client | `rejected` = the destination answered 401 with the stored value          |
| `updatedAt`    | datetime `{"$date": RFC3339}` | client / runtime | last value write                                                            |
| `label`        | string                | runtime               | human name, e.g. "GitHub personal access token"                                 |
| `hosts`        | string[]              | runtime               | destinations the key will be sent to (show it; empty = say so, in red)          |
| `help`         | string                | runtime               | URL where to get the key                                                        |
| `note`         | string                | runtime               | scopes / how-to hint                                                            |
| `requestedBy`  | string                | runtime               | run id that missed it (diagnostics)                                             |
| `requestedIn`  | string                | runtime               | chat id holding the live request bubble — **clients `$unset` it on save**        |
| `requestedAt`  | datetime              | runtime               | when that bubble was posted                                                     |
| `rejectedAt`   | datetime              | runtime               | when the 401 happened                                                           |
| `rejectedWith` | number                | runtime               | the HTTP status (401)                                                           |
| `meta`         | `{ "value": any }`    | runtime               | OAuth metadata rows only (§6)                                                   |

Read it as a live window/subscription on the dataset (the same
mechanism as `agent_triggers`). **Every read returns `value`**: strip
it at the read seam; never render, log, or keep it.

## 3. Writing a credential (the only write a client makes)

One `POST /v1/spaces/{spaceId}/modify` with **per-path ops** (the server
merges records field by field; a whole-record `$set` does *not* remove
absent keys, so `requestedIn` must be unset explicitly):

```json
{
  "objectId": "<secretsObjectId>",
  "dataset": "agent_secrets",
  "records": [{
    "id": "<ref>",
    "upsert": true,
    "ops": [
      { "type": "$set",   "path": "key",       "value": "<ref>" },
      { "type": "$set",   "path": "secret",    "value": true },
      { "type": "$set",   "path": "status",    "value": "set" },
      { "type": "$set",   "path": "updatedAt", "value": { "$date": "2026-08-26T12:00:00.000Z" } },
      { "type": "$set",   "path": "value",     "value": "<secret>" },
      { "type": "$unset", "path": "requestedIn" }
    ]
  }]
}
```

Delete = the same with `status: "missing"` and value `""`.

Rules:
- Never write `label`/`hosts`/`help`/`note` — they are the runtime's.
- Never write refs ending in `.refresh` under `connector.oauth.`
  (managed tokens) or any `.account`/`.granted_scopes`/`.client_id`/
  `.client_secret` row (metadata).
- The value syncs within the account (end-to-end encrypted by
  any-sync), so a key entered on a phone reaches the desktop running
  the agent — no same-device requirement.
- Then send `credential_set` (§1).

## 4. Rendering the request card

Replace the bubble of a message carrying `credential_request` with a
card. Contents, all from the row (§2) except the headline:

1. **Headline** — the message `text` (says first-ask vs rejected).
2. **Label** (row `label`, fallback: the ref) + the **ref** in monospace.
3. **Hosts sentence** — *"This key will only be sent to api.github.com."*;
   with an empty `hosts`: *"No host restriction declared for this key."*
   in a warning colour. This is the moment the human checks where the
   key goes — never omit it.
4. **Where to get it** — `help` as a link + `note`.
5. **Password field + Save** (§3 on save, then `credential_set`), or the
   answered/superseded state (below). Password input, no autocomplete,
   value never echoed, logged, or toasted.

### Per-card state — derived from the chat only

The chat record is immutable, and the row's clock is not comparable
with the message clock, so **do not use the row to decide a card's
state**. For each `credential_request` message, with `ref` from its
link, scanning the chat in time order:

- **answered** — a `credential_set` message for the same `ref` exists
  **after** it. Render *"✓ Set {relative time of that credential_set}"*
  (no form). A single `credential_set` answers every earlier
  unanswered card for that ref.
- **superseded** — unanswered, but a newer `credential_request` for the
  same ref follows. Render *"Superseded by a newer prompt below."* (no
  form).
- **open** — the newest unanswered card for the ref: show the form.

(The reference implementation: `credentialCardState` memo in
`ChatView.tsx`, `requestCardState` in `credentialPresentation.ts`.)

### `credential_set` rendering

A compact system-style row (like a day divider): *"Credential `<ref>`
updated"*. It is the user's own message; don't render it as a bubble.

### Typing indicator

Arm the "bao is thinking" state the moment the client sends
`credential_set` (same as a composer send), until an agent message
lands. Also arm it when the trailing message is agent-authored by a
`trigger:*` identity (a job-done ping) — the runtime starts a run from
those too.

## 5. Security rules (non-negotiable)

- The value is write-only for clients. Strip `value` from every read
  (it syncs to every device of the account).
- Never display, log, toast, copy to analytics, or persist the value
  outside the local-scope write.
- Show `hosts` at entry; an unbound key is called out, not hidden.
- Never write `.refresh` rows; never invent rows for refs the runtime
  didn't ask for (the prompt comes from the host, not the model).

## 6. The Credentials tool (Agent → Credentials)

A list over the `agent_secrets` window plus one synthetic row for the
LLM key when absent (`llm.key.anthropic`, label "Anthropic API key",
hosts `["api.anthropic.com"]`). Per row: label, ref, status badge
(`Missing` red / `Rejected` red / `Set {when}` green), hosts, help,
note, password field + Save, Delete (only when `set`). Saving here also
sends `credential_set` to the general chat (so open cards get answered
and bao retries).

OAuth rows are **read-only** and labelled by sub-ref
(`connector.oauth.<provider>.<sub>`):

| sub               | label                          | shows                                                       |
| ----------------- | ------------------------------ | ----------------------------------------------------------- |
| `refresh`         | `<Provider> connection`        | `Connected {when}` (status `set`) / `Not connected`; no input, never the token |
| `account`         | `<Provider> account`           | `meta.value` (the email)                                    |
| `granted_scopes`  | `<Provider> granted scopes`    | `meta.value` list (strip the `https://www.googleapis.com/auth/` prefix) |
| `client_id`       | `<Provider> OAuth client ID`   | "Bundled with the connector (`<first 12>…`)"                |
| `client_secret`   | `<Provider> OAuth client secret` | "Bundled with the connector"                              |

Connect/disconnect is done from the chat (the provider's `connect()`),
not from the tool.

## 7. Checklist for a new client

- [ ] Recognise `credential_request` / `credential_set` by attachment `type`; unknown types keep their generic chip.
- [ ] Parse `any://o/<objectId>?key=<ref>`.
- [ ] Live-read `agent_secrets` on that object; strip `value`.
- [ ] Card: headline, label, ref, hosts sentence (empty → warning), help + note, password + Save.
- [ ] Per-card state from the chat (answered / superseded / open).
- [ ] Save = the per-path write (§3) → `credential_set` → arm typing.
- [ ] `credential_set` rendered as a system row.
- [ ] Credentials tool with the synthetic LLM row and read-only OAuth rows.
- [ ] Security rules (§5).
