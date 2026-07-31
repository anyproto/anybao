# OAuth flow design notes (2026-07-29 — conversation reply, saved verbatim)

All the context is in. Here's the full picture and the design thinking you asked for.

## Where we stand

- **Dev-space "connectors repo" page**: all six token connectors are ported and hardened (github + linear fully, others awaiting keys); the Google family is explicitly last, "blocked on the `oauthFlow` host effect — anybao ADR first". The checklist item "think about generic oauth flow host effect" is the one still open — this conversation is that item.
- **The old bobrik PR** (`feat/bobrik-connectors` in ~/any/any): a generic `oauthFlow` host fn (`internal/anyrt/oauth.go`, 285 lines) did auth-code + PKCE over a loopback redirect, opened the browser, and returned `{accessToken, refreshToken, ...}` **into the guest**. `googleAuth@v1.js` then stored the refresh token in config and did token refresh via plain `fetch`, injecting `Authorization: Bearer` itself. One consent covered gmail/calendar/drive/sheets via a scope union.
- **Current anybao contract** (this is the part that changes everything): guest code never touches a credential. Connectors pass `credential: {ref: "connector.key.x", header, prefix}`; the broker resolves the secret **after recording** and injects the header (`broker.rs:552`). Secrets live in the `agent_secrets` dataset — synced part is just `{key, secret: true}`, plaintext is a never-synced device-local field — seeded only via `.connectors.env` hard seeds, guarded from guest reads (`secrets_read_guard`), refused by `config.get`. The ref set is open: any `connector.key.<x>` works with zero runtime change.
- **The "easy UI to insert initial secrets" task** already designed the transport you mentioned: `agent.secretRequest {ref, hint}` carried in the chat message's `agent` field, so *any* client (desktop or mobile) renders an input box, and the value lands in the serve machine's device-local store. It also flags the exfiltration gap (no binding between ref and destination host) with an agreed mitigation direction.

The consequence is clear before we even design: **the bobrik flow cannot be ported as-is.** Its entire token lifecycle lives in the guest — refresh token in guest-readable config, access token in guest memory, Authorization header built in guest code. Under ADR-008 that's three violations. Tokens are secrets; the whole lifecycle has to move behind the effect boundary. The good news: the current `credential: {ref}` mechanism is already the right shape for it.

---

## How OAuth 2.0 actually works

The problem OAuth solves: you want anybao to read your Gmail without giving it your Google password, with access that is **scoped** (only mail, only read), **revocable** (kill it from your Google account page without changing your password), and **auditable** (Google shows "anybao has access to X").

Four roles: the **resource owner** (you), the **client** (anybao — identified by a `client_id` from registering an app in Google Cloud Console), the **authorization server** (accounts.google.com — authenticates you and issues tokens), and the **resource server** (gmail.googleapis.com — accepts tokens).

### The authorization-code grant, step by step

This is the flow for anything with a browser available, and the one the old PR implemented:

1. **Authorization request.** The client builds a URL to the provider's *authorize endpoint* — `client_id`, `redirect_uri`, `response_type=code`, `scope`, plus two security values: a random `state` and a PKCE `code_challenge` (more below) — and opens it in the user's browser.
2. **Authentication + consent.** The user logs in *at Google* (the client never sees the password) and approves the listed scopes.
3. **Redirect with a code.** Google redirects the browser to `redirect_uri` with a short-lived, single-use **authorization code**. The redirect URI must exactly match one registered for the client — this is what stops an attacker from pointing the redirect at themselves.
4. **Code exchange.** The client POSTs the code to the *token endpoint* (server-to-server, not through the browser) together with the PKCE `code_verifier` and gets back:
   - an **access token** — the bearer credential for API calls, short-lived (~1h for Google). Anyone holding it can use it, hence "bearer".
   - a **refresh token** — long-lived, exchangeable for new access tokens without user interaction. *This is the crown jewel*: it is durable, works offline, and equals standing access to the account within the granted scopes.
   - `expires_in`, granted `scope`, `token_type`.
5. **API calls** with `Authorization: Bearer <access_token>`.
6. **Refresh.** When the access token expires: POST `grant_type=refresh_token` to the token endpoint → new access token. No browser, no user.

### The security machinery, and what each piece defends against

- **`state`** — a random nonce echoed back in the redirect. Defends against CSRF: without it, an attacker could trick your callback into completing *their* authorization.
- **PKCE** (RFC 7636) — the client invents a random `code_verifier`, sends its SHA-256 hash (`code_challenge`) in step 1, and the raw verifier in step 4. Defends against **code interception**: on desktop, the redirect travels through a loopback URL another local process could race for; a stolen code is useless without the verifier, and the verifier never travels until the exchange. Mandatory practice for public clients, and increasingly required everywhere (OAuth 2.1 makes it universal).
- **Public vs confidential clients.** A server-side web app can hold a `client_secret` confidentially. A desktop binary cannot — anything shipped to the user is extractable, so native apps are **public clients**: PKCE is the real protection, and Google's "Desktop app" client type issues a client_secret that is explicitly *not* treated as a secret (you still send it, it just proves nothing).
- **Native-app rules** (RFC 8252, the pattern `gcloud auth login` uses, and what bobrik did): use the **system browser** (never an embedded webview — the user must see the real Google URL bar, and the app must not be able to keylog the password), and receive the redirect on a **loopback listener** (`http://127.0.0.1:<random-port>/`), which Google special-cases for Desktop clients so any port matches.
//tolya: i think even for remote machinery we can do the same? we don't need to implement it now, but in the end, remote agent is operated from the app too. its just the difference is that its a remote app, it runs not on the same machine. So we can solve it by transport, i.e. its not like automated server oauth, which is impossible i guess?
- **Scopes** bound at consent; the token is useless outside them. Google tiers matter operationally: *sensitive* scopes (gmail.readonly, calendar.readonly…) need app verification for public use; *restricted* scopes (full drive.readonly) additionally need a CASA security assessment — the old bobrik scope union deliberately stayed out of restricted territory. With your own Cloud project in testing mode none of that blocks you, but it shapes any future "shared client id" ambition.

### Google-specific robustness gotchas (worth encoding in the design, they all bit people before)

- A refresh token is only issued when you ask: `access_type=offline`, and on re-consent only with `prompt=consent` (otherwise Google silently omits it and you're broken after the first hour).
- While the OAuth consent screen is in **Testing** status, refresh tokens die after **7 days**. Publish the app (even unverified) to lift that.
//tolya: make a task in dev space, connectors->google nav folder, publish unverified oauth app
- Refresh tokens are revoked by: user action, password change (for gmail scopes), ~6 months of disuse, and exceeding ~50 live tokens per account per client. So "refresh failed with `invalid_grant`" is a normal lifecycle event, and the design needs a clean re-consent path, not just an error.
- Some providers (Microsoft, Dropbox, most newer ones) **rotate** the refresh token on every refresh — the response carries a new one and the old one dies. The host must persist the rotated token *before* using it, atomically, or a crash between refresh and persist bricks the connection.

### The headless variant: device authorization grant (RFC 8628)

For a machine with no browser: the client polls the token endpoint while showing the user a short code ("go to google.com/device, enter `XYZ-ABC`") to approve *from any device*. This sounds like the perfect answer for remote anyrt — but **Google restricts the device flow to a tiny scope list** (profile, drive.file, YouTube-ish scopes); gmail and calendar are not on it. And the old escape hatch of copy-pasting the code manually (`oob` redirect) was killed by Google in 2022. So for the Google family specifically, remote authorization must come from a different construction — which, conveniently, your secret-request channel already provides (below).

---

## The design: a generic, boundary-respecting OAuth mechanism

Yes, a generic mechanism is possible — OAuth 2.0 auth-code+PKCE is standardized enough that the old bobrik host fn already was provider-agnostic (authUrl/tokenUrl/clientId/scopes as parameters). The redesign is not about genericity, it's about **custody**: every token stays host-side, and the guest deals only in refs and statuses.

### 1. Two kinds of credential ref — same guest surface

Today a ref names a static secret. Add a second kind: a **managed OAuth credential**. The guest-side shape stays exactly what connectors already use:

```python
_CRED = {"ref": "connector.oauth.google"}
resp = http.get(url, credential=_CRED)
```

When the broker resolves an oauth-kind ref, instead of pasting a stored string it: checks the in-memory access token for freshness (expiry minus a safety margin) → if stale, performs the refresh-token exchange host-side (single-flight lock, so concurrent cells don't stampede the token endpoint) → injects `Authorization: Bearer <access>`. The refresh is itself nondeterministic, so it's recorded as an effect (`oauth.refresh`, outcome + expiry only, token material redacted) — "if it isn't in the trace, it didn't happen" holds, and replay just replays the recorded outcome. A refresh failure surfaces to the guest as the same actionable typed failure connectors already handle ("google not connected — re-run connect", pointing at the consent path), so the existing `_NOT_CONNECTED` pattern extends unchanged.

Storage in the existing `agent_secrets` mechanics, no new store: `connector.oauth.google.refresh` (the refresh token, device-local value like any secret — and rewritten in place when a provider rotates it), plus `…google.client_id` / `…google.client_secret` seeded through the normal `.connectors.env` path. Access tokens live only in broker memory — short-lived by design, not worth persisting.

### 2. `oauth.connect` — a host effect that returns no tokens

The one genuinely new effect. Guest calls `oauth.connect({provider: "google", scopes: [...]})` (or a `googleAuth@v1` connector wraps it); the host runs consent — and the guest gets back only `{ok, grantedScopes, account}`. The refresh token goes straight from the token-exchange response into the device-local store, never crossing the boundary. That single change fixes everything wrong with the bobrik version while keeping its whole PKCE/loopback machinery, which was correct and can be ported nearly verbatim (Rust instead of Go; and note `serve.rs` already runs a `tiny_http` loopback listener for the control API — the callback receiver is a natural sibling).

One deliberate split hiding inside "runs consent": the host builds the consent URL and *acquires the resulting code*, but **how the URL reaches the user's browser and where the redirect lands is a transport question** — and making that pluggable is exactly what buys you remote support later:

- **Transport A — co-located desktop** (now): loopback listener on 127.0.0.1, consent URL handed to the UI via the chat `agent` field (better than the host shelling out to `xdg-open` — the serve may be a daemon; the *client* opens the browser). This alone unblocks the Google family for you today.
- **Transport B — remote anyrt** (later, and this is where your secret-request design pays off): the consent has to happen where the human is, not where anyrt runs. Generalize `agent.secretRequest` to an `agent.oauthRequest {provider, scopes, hint}` chat message: the **client app** runs steps 1–3 locally — it has a browser and can listen on its own 127.0.0.1 — and sends back `{code, code_verifier, redirect_uri}` over the same write path a plain secret uses. The serve performs the code exchange and stores the refresh token device-locally. This is strictly better than shipping a refresh token over the channel: the code is single-use and dies in minutes, and PKCE means an eavesdropper who somehow got the message still couldn't... well, with code+verifier together they could — the write path still needs the same trust as secret insertion — but the exposure window collapses from "durable account access" to "minutes, single-use". A headless-with-no-client fallback is a tiny `anyrt oauth connect --remote <addr>` run on the user's laptop doing the same dance. Device flow stays a nice-to-have for providers that allow real scopes on it; it can't be the backbone because Google gates it.
- The dev task's **account-scoped secrets** exploration is orthogonal and stays open: if secrets move from device-local to account scope, transport B's write path becomes ordinary sync — but that decision needs the encryption-at-rest answer first, and nothing in this design depends on it.

### 3. Provider descriptors — genericity as data

A host-side table keyed by provider (config-defaults JSON, same spirit as the open `connector.key.*` set): authorize URL, token URL, default scopes, extra auth params (`access_type=offline`, `prompt=consent`), token-endpoint auth style, whether refresh tokens rotate, and — critically — **allowed injection hosts** (`*.googleapis.com`). Adding Microsoft or Notion later is a table row, not a code change.

### 4. Host binding becomes non-optional

The exfiltration gap already filed in the secrets task — any guest cell can send any secret to any host — was tolerable-ish for a Linear key. A Google access token scoped over your mail is a different blast radius, so the agreed mitigation should land **with or before** the OAuth work, at least for oauth-kind refs: per-ref `{allowed_hosts, header, prefix}` metadata, verified before injection; header/prefix move host-side so guests send `{ref}` only; credentialed requests get `redirects=0` (a cross-host 302 replays the Authorization header to the new host — that's a token leak with zero attacker effort). For OAuth refs the metadata comes free from the provider descriptor.

### 5. Scope strategy

Keep bobrik's shape: **one** `connector.oauth.google` credential, one consent over the union of scopes the installed google connectors need, shared by gmail/calendar/drive/sheets — one refresh token to manage, `include_granted_scopes=true` for incremental re-consent when a new connector wants more. Per-connector Google credentials would multiply consents and refresh-token lifecycle for no isolation gain (they'd all be the same Google account anyway).

---

## Is the current design "ready"? Almost — three things to lock in

1. **Guest credential shape should shrink to `{ref}`.** Today connectors hardcode `header`/`prefix` guest-side. Moving those into per-ref host metadata (already the agreed mitigation direction) is the one contract change that makes static and OAuth credentials indistinguishable to guest code — do it as part of the binding work and the OAuth extension becomes purely additive.
2. **The secret store needs nothing new.** Open ref set + device-local values + rotate/revoke semantics from the `.connectors.env` work already cover refresh-token storage and rotation-in-place. That recent work was the right foundation.
3. **The `agent`-field request channel should be specified verb-extensible.** When you build `secretRequest`, shape it as `agent.request {kind: "secret"|"oauth", ...}` rather than a secret-only payload — then remote OAuth is a second verb on existing plumbing, not new plumbing.

ADR-wise this is one new ADR (011: OAuth credentials — the `oauth.connect` effect, managed refs, provider descriptors, transport A now / B designed-not-built), amending 002 (new effect + refresh sub-effect), 008 (credential shape + host binding), and 006 (secret record metadata). Phasing: binding + `{ref}`-only shape → `oauth.connect` with the desktop loopback transport → port googleAuth/gmail/calendar/drive/sheets from the bobrik branch as thin guest programs (they shrink a lot — all the token logic they used to carry is gone).

Happy to draft ADR-011 along these lines when you want to move on it.
