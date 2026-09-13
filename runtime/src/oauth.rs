//! Managed OAuth credentials (ADR-011): provider descriptors and the
//! host-held token lifecycle behind `connector.oauth.<provider>` refs.
//! Custody rule (ADR-011 §1): no token — access, refresh, or
//! authorization code — is ever returned to guest code, written to a
//! synced field, or recorded in a trace. The broker resolves managed
//! refs through `OauthState` at injection time (ADR-011 §6); this
//! module owns the state and the refresh-token exchange.

use crate::broker::EffectFailure;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// Managed refs live under this namespace; the suffix is the provider
/// handle, dotted sub-refs are its stored pieces (ADR-011 §3).
pub const OAUTH_REF_PREFIX: &str = "connector.oauth.";

/// Built-in provider table (ADR-011 §2) — host-only data, never merged
/// into guest-readable config (the MODEL_PRICING route, not the
/// config_defaults one).
const PROVIDERS: &str = include_str!("oauth_providers.json");

/// Inject the cached token only while it outlives this margin; below
/// it, refresh first (ADR-011 §6).
const EXPIRY_MARGIN_S: f64 = 120.0;

const TOKEN_TIMEOUT_S: u64 = 30;

/// The consent receiver's fixed lifetime (ADR-011 §5): it outlives a
/// timed-out `connect` — slow consent is the common case — and dies at
/// window end regardless.
const CONSENT_WINDOW_S: f64 = 300.0;

/// `oauth.connect`'s default blocking wait (ADR-011 §5, resolved Q3);
/// afterwards the guest polls `oauth.status`.
const CONNECT_TIMEOUT_S: f64 = 120.0;

#[derive(Debug, Clone, serde::Deserialize)]
pub struct ProviderDescriptor {
    pub authorize_url: String,
    pub token_url: String,
    #[serde(default)]
    pub revoke_url: String,
    #[serde(default)]
    pub auth_params: BTreeMap<String, String>,
    #[serde(default)]
    pub default_scopes: Vec<String>,
    #[serde(default)]
    pub rotates_refresh_token: bool,
    #[serde(default = "client_auth_default")]
    pub client_auth: String,
}

fn client_auth_default() -> String {
    "post_body".into()
}

pub fn builtin_providers() -> BTreeMap<String, ProviderDescriptor> {
    serde_json::from_str(PROVIDERS).expect("oauth_providers.json is valid JSON")
}

/// Write path into the device-local secret store (ADR-006 §3). serve
/// implements it over the any client + secrets object; `anyrt run` (and
/// degraded no-store serve) has None — flows work in-memory only and
/// nothing survives the process.
pub trait SecretPersist: Send + Sync {
    /// Device-local secret value; empty string deletes the stored value.
    fn persist_secret(&self, key: &str, value: &str) -> anyhow::Result<()>;
    /// Non-secret synced metadata record `{key, value}` (ADR-011 §3:
    /// `.granted_scopes` / `.account`).
    fn persist_meta(&self, key: &str, value: &Value) -> anyhow::Result<()>;
}

struct CachedToken {
    access: String,
    expires_at: f64,
}

/// What the host hands the human (ADR-011 §1 corollary: the guest
/// never sees the consent URL — URL delivery through the model would
/// make the phishing surface model-writable).
pub struct ConsentRequest {
    pub provider: String,
    pub url: String,
}

/// Lib-mode consent delivery (ADR-011 §5.1 transport A; ADR-009 §6
/// surface): the embedder owns opening the browser — the serve may be
/// a headless daemon.
#[derive(Clone)]
pub struct ConsentHook(pub Arc<dyn Fn(ConsentRequest) + Send + Sync>);

impl std::fmt::Debug for ConsentHook {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("ConsentHook(..)")
    }
}

#[derive(Clone)]
struct FlowOutcome {
    granted_scopes: Vec<String>,
    account: Option<String>,
}

enum FlowState {
    Pending,
    Done(Result<FlowOutcome, (String, String)>),
}

/// One consent flow: the joinable wait handle `connect` blocks on.
/// The receiver thread owns the listener; this is only the outcome.
struct ConsentFlow {
    state: Mutex<FlowState>,
    cond: Condvar,
}

/// What the receiver thread needs from its spawn site.
struct FlowParams {
    provider: String,
    desc: ProviderDescriptor,
    redirect_uri: String,
    verifier: String,
    state_tok: String,
}

/// One per process, shared across runs/threads: per-run broker
/// snapshots never hold oauth material (`seed` drains it), and the
/// access-token cache is process-wide (ADR-011 §3 — Google caps live
/// tokens per client; a per-run cache would mint one per run).
pub struct OauthState {
    pub providers: BTreeMap<String, ProviderDescriptor>,
    /// `connector.oauth.*` sub-ref values — the authoritative live copy.
    secrets: Mutex<BTreeMap<String, String>>,
    /// The secrets store (ADR-021 §4): a sub-ref absent from the live
    /// copy is read from the row — `.client_id`/`.client_secret`
    /// entered through the UI are ordinary rows, no restart.
    pub source: Option<Arc<dyn crate::broker::SecretSource>>,
    /// Access tokens by handle ref (`connector.oauth.google`).
    tokens: Mutex<BTreeMap<String, CachedToken>>,
    /// Per-ref single-flight refresh gate (ADR-011 §6).
    flight: Mutex<BTreeMap<String, Arc<Mutex<()>>>>,
    /// Pending consent flows by provider (ADR-011 §5): a concurrent
    /// connect JOINS the flow here instead of spawning a second
    /// receiver; entries leave on completion.
    flows: Mutex<BTreeMap<String, Arc<ConsentFlow>>>,
    /// Non-secret grant metadata by sub-ref (`.granted_scopes` /
    /// `.account`) — mirrors the synced store records.
    meta: Mutex<BTreeMap<String, Value>>,
    pub persist: Option<Box<dyn SecretPersist>>,
    /// Wiring sets these before the Arc: lib-mode consent delivery and
    /// the serve shutdown flag flow threads observe.
    pub consent: Option<ConsentHook>,
    pub shutdown: Arc<AtomicBool>,
    /// Long-lived client for token-endpoint calls.
    agent: ureq::Agent,
}

fn now_s() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
}

fn fail(type_: &str, message: String) -> EffectFailure {
    EffectFailure {
        type_: type_.into(),
        message,
    }
}

impl OauthState {
    pub fn new(
        providers: BTreeMap<String, ProviderDescriptor>,
        persist: Option<Box<dyn SecretPersist>>,
    ) -> Self {
        OauthState {
            providers,
            secrets: Mutex::new(BTreeMap::new()),
            source: None,
            tokens: Mutex::new(BTreeMap::new()),
            flight: Mutex::new(BTreeMap::new()),
            flows: Mutex::new(BTreeMap::new()),
            meta: Mutex::new(BTreeMap::new()),
            persist,
            consent: None,
            shutdown: Arc::new(AtomicBool::new(false)),
            agent: ureq::agent(),
        }
    }

    /// The OAuth client for a sub-ref (`.client_id` / `.client_secret`):
    /// the connector-bundled value (metadata, a public client — ADR-011
    /// §3) wins; a seeded secret row is the self-hoster override only
    /// when nothing was bundled.
    fn client_value(&self, key: &str) -> Option<String> {
        let bundled = self
            .meta
            .lock()
            .expect("oauth meta lock poisoned")
            .get(key)
            .and_then(|v| v.as_str().map(str::to_string))
            .filter(|s| !s.is_empty());
        bundled.or_else(|| self.secret(key))
    }

    /// Grant metadata loaded back from the store at boot (serve).
    pub fn seed_meta(&self, key: &str, value: Value) {
        self.meta
            .lock()
            .expect("oauth meta lock poisoned")
            .insert(key.to_string(), value);
    }

    /// Drain every `connector.oauth.*` entry OUT of the map brokers are
    /// cloned from (ADR-011 §4/§6): a per-run secret snapshot never
    /// contains a refresh token, so the static injection path
    /// structurally cannot leak one.
    pub fn seed(&self, secrets: &mut BTreeMap<String, String>) {
        let mut own = self.secrets.lock().expect("oauth secrets lock poisoned");
        let keys: Vec<String> = secrets
            .keys()
            .filter(|k| k.starts_with(OAUTH_REF_PREFIX))
            .cloned()
            .collect();
        for k in keys {
            if let Some(v) = secrets.remove(&k) {
                own.insert(k, v);
            }
        }
    }

    /// The store is the truth when there is one (ADR-021 §4): a
    /// `.client_id`/`.client_secret` rotated through the UI must not be
    /// shadowed by the boot-loaded live copy. The live map answers only
    /// without a store (no-store mode) or when the store is unreachable.
    pub fn secret(&self, key: &str) -> Option<String> {
        if let Some(src) = &self.source {
            if let Ok(v) = src.read(key) {
                return v;
            }
        }
        self.secrets
            .lock()
            .expect("oauth secrets lock poisoned")
            .get(key)
            .cloned()
    }

    /// The cached access token for a handle ref, if it outlives the
    /// margin.
    pub fn fresh_token(&self, handle_ref: &str) -> Option<String> {
        let tokens = self.tokens.lock().expect("oauth tokens lock poisoned");
        let tok = tokens.get(handle_ref)?;
        (tok.expires_at - now_s() > EXPIRY_MARGIN_S).then(|| tok.access.clone())
    }

    pub fn flight_gate(&self, handle_ref: &str) -> Arc<Mutex<()>> {
        self.flight
            .lock()
            .expect("oauth flight lock poisoned")
            .entry(handle_ref.to_string())
            .or_default()
            .clone()
    }

    /// The refresh-token exchange (ADR-011 §6): rotation-safe persist
    /// (new refresh token stored BEFORE the new access token is
    /// published), `invalid_grant` handled as a lifecycle event.
    /// Returns the effect output `{ok, expiresAt, scope, rotated}` —
    /// no token material.
    pub fn refresh(&self, provider: &str) -> Result<Value, EffectFailure> {
        let desc = self
            .providers
            .get(provider)
            .ok_or_else(|| fail("not_configured", format!("no oauth provider {provider:?}")))?;
        let handle = format!("{OAUTH_REF_PREFIX}{provider}");
        let refresh_ref = format!("{handle}.refresh");
        let refresh_token = self.secret(&refresh_ref).ok_or_else(|| {
            fail(
                "not_connected",
                format!("{provider} is not connected — run the provider's connect first"),
            )
        })?;
        let client_id = self
            .client_value(&format!("{handle}.client_id"))
            .ok_or_else(|| {
                fail(
                    "not_configured",
                    format!("no OAuth client for {provider} — the connector must pass client_id to connect()"),
                )
            })?;
        // A Desktop-client secret is a public-client secret (ADR-011 §3):
        // sent when present, proves nothing, never required.
        let client_secret = self
            .client_value(&format!("{handle}.client_secret"))
            .unwrap_or_default();
        if desc.client_auth != "post_body" {
            return Err(fail(
                "token_exchange_failed",
                format!("unsupported client_auth {:?}", desc.client_auth),
            ));
        }

        let resp = self
            .agent
            .post(&desc.token_url)
            .timeout(std::time::Duration::from_secs(TOKEN_TIMEOUT_S))
            .send_form(&[
                ("grant_type", "refresh_token"),
                ("refresh_token", &refresh_token),
                ("client_id", &client_id),
                ("client_secret", &client_secret),
            ]);
        let body: Value = match resp {
            Ok(r) => {
                serde_json::from_str(&r.into_string().unwrap_or_default()).unwrap_or(Value::Null)
            }
            Err(ureq::Error::Status(code, r)) => {
                let body: Value = serde_json::from_str(&r.into_string().unwrap_or_default())
                    .unwrap_or(Value::Null);
                // only the provider's error CODE travels into messages —
                // never the raw body (structural redaction, ADR-011 §9)
                let err_code = body["error"].as_str().unwrap_or("").to_string();
                if err_code == "invalid_grant" {
                    // lifecycle, not a bug (ADR-011 §6): revoked, expired,
                    // 7-day Testing cap, ~6mo idle — drop it, ask to re-consent
                    if let Err(e) = self.drop_grant(provider) {
                        // the dead token stays in the row; the next
                        // refresh fails the same way — loud, not wrong
                        tracing::warn!("oauth: could not delete dead {provider} token ({e})");
                    }
                    return Err(fail(
                        "oauth_reconsent_required",
                        format!(
                            "{provider} refresh token is no longer valid \
                             (invalid_grant) — re-run the provider's connect"
                        ),
                    ));
                }
                return Err(fail(
                    "token_exchange_failed",
                    format!("token endpoint returned {code} ({err_code})"),
                ));
            }
            Err(e) => {
                return Err(fail(
                    "token_exchange_failed",
                    format!("token endpoint unreachable: {e}"),
                ))
            }
        };

        let access = body["access_token"].as_str().unwrap_or("").to_string();
        if access.is_empty() {
            return Err(fail(
                "token_exchange_failed",
                "token endpoint answered 2xx without an access_token".into(),
            ));
        }
        let expires_in = body["expires_in"].as_f64().unwrap_or(3600.0);
        let scope = body["scope"].as_str().unwrap_or("").to_string();
        let rotated = body["refresh_token"].as_str().is_some();

        if let Some(new_refresh) = body["refresh_token"].as_str() {
            // rotation-safe order (ADR-011 §6): the old token is dead the
            // moment the provider answered — persist the new one BEFORE
            // publishing the access token, or a crash bricks the grant
            if let Some(p) = &self.persist {
                if let Err(e) = p.persist_secret(&refresh_ref, new_refresh) {
                    tracing::warn!(
                        "oauth: could not persist rotated {refresh_ref} ({e}); \
                         the grant dies with this process"
                    );
                }
            }
            self.secrets
                .lock()
                .expect("oauth secrets lock poisoned")
                .insert(refresh_ref, new_refresh.to_string());
        }
        let expires_at = now_s() + expires_in;
        self.tokens
            .lock()
            .expect("oauth tokens lock poisoned")
            .insert(handle, CachedToken { access, expires_at });
        Ok(json!({"ok": true, "expiresAt": expires_at, "scope": scope,
                  "rotated": rotated}))
    }

    /// `oauth.connect` (ADR-011 §5): run consent, return no tokens.
    /// Blocks up to `timeout_s` (default 120s, capped at the 5-minute
    /// window); the receiver runs on to window end — a late completion
    /// is observable via `oauth.status`. A concurrent connect for the
    /// same provider joins the pending flow.
    pub fn connect(
        self: &Arc<Self>,
        provider: &str,
        scopes: Option<Vec<String>>,
        timeout_s: Option<f64>,
        // the connector-bundled OAuth client (public client, ADR-011 §3):
        // remembered as non-secret metadata so refresh-at-injection works
        // after a restart without a guest call
        client: Option<(String, String)>,
    ) -> Result<Value, EffectFailure> {
        let desc = self
            .providers
            .get(provider)
            .cloned()
            .ok_or_else(|| fail("not_configured", format!("no oauth provider {provider:?}")))?;
        let handle = format!("{OAUTH_REF_PREFIX}{provider}");
        if let Some((id, secret)) = client.filter(|(id, _)| !id.is_empty()) {
            for (sub, v) in [("client_id", id), ("client_secret", secret)] {
                let key = format!("{handle}.{sub}");
                let changed = self
                    .meta
                    .lock()
                    .expect("oauth meta lock poisoned")
                    .insert(key.clone(), json!(v))
                    .as_ref()
                    .and_then(|old| old.as_str().map(|s| s != v))
                    .unwrap_or(true);
                if changed {
                    if let Some(p) = &self.persist {
                        if let Err(e) = p.persist_meta(&key, &json!(v)) {
                            tracing::warn!("oauth: could not persist {key} ({e})");
                        }
                    }
                }
            }
        }
        let client_id = self
            .client_value(&format!("{handle}.client_id"))
            .ok_or_else(|| {
                fail(
                    "not_configured",
                    format!(
                        "no OAuth client for {provider} — the connector must pass \
                         client_id (and client_secret) to connect(); a self-hosted \
                         override can be entered in Credentials as {handle}.client_id"
                    ),
                )
            })?;

        let flow = {
            let mut flows = self.flows.lock().expect("oauth flows lock poisoned");
            match flows.get(provider) {
                Some(f) => f.clone(), // join the pending flow (§5)
                None => {
                    let f = self.spawn_flow(provider, &desc, &client_id, scopes)?;
                    flows.insert(provider.to_string(), f.clone());
                    f
                }
            }
        };

        let timeout = timeout_s
            .unwrap_or(CONNECT_TIMEOUT_S)
            .clamp(0.05, CONSENT_WINDOW_S);
        let deadline = Instant::now() + Duration::from_secs_f64(timeout);
        let mut st = flow.state.lock().expect("oauth flow lock poisoned");
        loop {
            match &*st {
                FlowState::Done(Ok(o)) => {
                    return Ok(json!({"ok": true, "provider": provider,
                                     "grantedScopes": o.granted_scopes,
                                     "account": o.account}));
                }
                FlowState::Done(Err((t, m))) => return Err(fail(t, m.clone())),
                FlowState::Pending => {}
            }
            if Instant::now() >= deadline || self.shutdown.load(Ordering::Relaxed) {
                return Err(fail(
                    "consent_timeout",
                    format!(
                        "consent for {provider} is still pending — the browser \
                         window stays valid ~5 minutes; poll oauth.status"
                    ),
                ));
            }
            let (guard, _) = flow
                .cond
                .wait_timeout(st, Duration::from_millis(250))
                .expect("oauth flow lock poisoned");
            st = guard;
        }
    }

    /// `oauth.status` (ADR-011 §5): state only, no network.
    pub fn status(&self, provider: &str) -> Result<Value, EffectFailure> {
        if !self.providers.contains_key(provider) {
            return Err(fail(
                "not_configured",
                format!("no oauth provider {provider:?}"),
            ));
        }
        let handle = format!("{OAUTH_REF_PREFIX}{provider}");
        let connected = self.secret(&format!("{handle}.refresh")).is_some();
        // pending = a flow entry whose outcome is not in yet. The receiver
        // thread marks Done and notifies BEFORE it drops the map entry, so
        // a status read right after connect() returns can still see the
        // entry — that window is not "pending".
        let pending = self
            .flows
            .lock()
            .expect("oauth flows lock poisoned")
            .get(provider)
            .is_some_and(|f| {
                matches!(
                    *f.state.lock().expect("oauth flow lock poisoned"),
                    FlowState::Pending
                )
            });
        let meta = self.meta.lock().expect("oauth meta lock poisoned");
        let scopes = meta
            .get(&format!("{handle}.granted_scopes"))
            .cloned()
            .unwrap_or(Value::Null);
        let account = meta
            .get(&format!("{handle}.account"))
            .cloned()
            .unwrap_or(Value::Null);
        let expires_at = self
            .tokens
            .lock()
            .expect("oauth tokens lock poisoned")
            .get(&handle)
            .map(|t| json!(t.expires_at))
            .unwrap_or(Value::Null);
        Ok(json!({"connected": connected, "pending": pending,
                  "scopes": scopes, "account": account,
                  "expiresAt": expires_at}))
    }

    /// `oauth.disconnect` (ADR-011 §8): provider-side revoke
    /// (best-effort — the part that actually ends access) + local
    /// delete, and cancel any pending flow.
    pub fn disconnect(&self, provider: &str) -> Result<Value, EffectFailure> {
        let desc = self
            .providers
            .get(provider)
            .ok_or_else(|| fail("not_configured", format!("no oauth provider {provider:?}")))?;
        if let Some(flow) = self
            .flows
            .lock()
            .expect("oauth flows lock poisoned")
            .remove(provider)
        {
            let mut st = flow.state.lock().expect("oauth flow lock poisoned");
            if matches!(*st, FlowState::Pending) {
                *st = FlowState::Done(Err((
                    "consent_denied".into(),
                    "disconnected while consent was pending".into(),
                )));
                flow.cond.notify_all();
            }
        }
        let handle = format!("{OAUTH_REF_PREFIX}{provider}");
        let refresh = self.secret(&format!("{handle}.refresh"));
        let mut revoked = false;
        if let Some(tok) = &refresh {
            if !desc.revoke_url.is_empty() {
                revoked = self
                    .agent
                    .post(&desc.revoke_url)
                    .timeout(Duration::from_secs(TOKEN_TIMEOUT_S))
                    .send_form(&[("token", tok)])
                    .is_ok();
            }
        }
        if let Err(e) = self.drop_grant(provider) {
            return Err(fail(
                "disconnect_failed",
                format!(
                    "{provider}: provider revoke {} but the stored refresh token \
                     could not be deleted ({e}) — retry disconnect",
                    if revoked {
                        "succeeded"
                    } else {
                        "was not confirmed"
                    }
                ),
            ));
        }
        {
            let mut meta = self.meta.lock().expect("oauth meta lock poisoned");
            for sub in ["granted_scopes", "account"] {
                let key = format!("{handle}.{sub}");
                if meta.remove(&key).is_some() {
                    if let Some(p) = &self.persist {
                        if let Err(e) = p.persist_meta(&key, &Value::Null) {
                            tracing::warn!("oauth: could not clear {key} ({e})");
                        }
                    }
                }
            }
        }
        Ok(json!({"ok": true, "provider": provider, "revoked": revoked}))
    }

    /// Bind the loopback receiver FIRST (a URL whose receiver isn't
    /// listening is a dead-end consent), build the consent URL (PKCE
    /// S256 + state, generated here and never leaving this process —
    /// ADR-011 §5.1), start the receiver thread, deliver the URL.
    fn spawn_flow(
        self: &Arc<Self>,
        provider: &str,
        desc: &ProviderDescriptor,
        client_id: &str,
        scopes: Option<Vec<String>>,
    ) -> Result<Arc<ConsentFlow>, EffectFailure> {
        let server = tiny_http::Server::http("127.0.0.1:0").map_err(|e| {
            fail(
                "RuntimeError",
                format!("could not bind the oauth callback receiver: {e}"),
            )
        })?;
        let port = server
            .server_addr()
            .to_ip()
            .expect("loopback listener has an ip addr")
            .port();
        let redirect_uri = format!("http://127.0.0.1:{port}/oauth/callback");
        let (verifier, challenge) = pkce_pair();
        let state_tok = rand_hex(32);
        let scope_str = scopes
            .unwrap_or_else(|| desc.default_scopes.clone())
            .join(" ");
        let mut params: Vec<(String, String)> = vec![
            ("response_type".into(), "code".into()),
            ("client_id".into(), client_id.to_string()),
            ("redirect_uri".into(), redirect_uri.clone()),
            ("scope".into(), scope_str),
            ("state".into(), state_tok.clone()),
            ("code_challenge".into(), challenge),
            ("code_challenge_method".into(), "S256".into()),
        ];
        params.extend(desc.auth_params.iter().map(|(k, v)| (k.clone(), v.clone())));
        let query: Vec<String> = params
            .iter()
            .map(|(k, v)| {
                format!(
                    "{}={}",
                    crate::broker::urlencode(k),
                    crate::broker::urlencode(v)
                )
            })
            .collect();
        let url = format!("{}?{}", desc.authorize_url, query.join("&"));

        let flow = Arc::new(ConsentFlow {
            state: Mutex::new(FlowState::Pending),
            cond: Condvar::new(),
        });
        {
            let state = self.clone();
            let flow = flow.clone();
            let params = FlowParams {
                provider: provider.to_string(),
                desc: desc.clone(),
                redirect_uri,
                verifier,
                state_tok,
            };
            std::thread::spawn(move || state.run_flow(server, flow, params));
        }
        self.deliver_consent(ConsentRequest {
            provider: provider.to_string(),
            url,
        });
        Ok(flow)
    }

    /// Hook → system browser → the URL on the host log (the honest CLI
    /// path; host-side output, the guest never sees it — ADR-011 §5.1).
    fn deliver_consent(&self, req: ConsentRequest) {
        if let Some(h) = &self.consent {
            (h.0)(req);
            return;
        }
        let opener = if cfg!(target_os = "macos") {
            "open"
        } else {
            "xdg-open"
        };
        let opened = std::process::Command::new(opener)
            .arg(&req.url)
            .spawn()
            .is_ok();
        tracing::warn!(
            "oauth: authorize {} in a browser{}: {}",
            req.provider,
            if opened { " (opened)" } else { "" },
            req.url
        );
    }

    /// The receiver: single-use, bounded by the 5-minute window and the
    /// shutdown flag; `state` mismatch tears it down (a forged or
    /// replayed redirect gets no retries against a live verifier).
    fn run_flow(self: Arc<Self>, server: tiny_http::Server, flow: Arc<ConsentFlow>, p: FlowParams) {
        let FlowParams {
            provider,
            desc,
            redirect_uri,
            verifier,
            state_tok,
        } = p;
        let deadline = Instant::now() + Duration::from_secs_f64(CONSENT_WINDOW_S);
        let outcome: Result<FlowOutcome, (String, String)> = loop {
            if Instant::now() >= deadline {
                break Err((
                    "consent_timeout".into(),
                    "the consent window expired — re-run connect".into(),
                ));
            }
            if self.shutdown.load(Ordering::Relaxed)
                || !matches!(
                    *flow.state.lock().expect("oauth flow lock poisoned"),
                    FlowState::Pending
                )
            {
                // shut down or cancelled (disconnect) — nothing to publish
                break Err(("consent_timeout".into(), "cancelled".into()));
            }
            let Ok(Some(req)) = server.recv_timeout(Duration::from_millis(250)) else {
                continue;
            };
            let url = req.url().to_string();
            let (path, query) = url.split_once('?').unwrap_or((url.as_str(), ""));
            if path != "/oauth/callback" {
                let _ = req
                    .respond(tiny_http::Response::from_string("not found").with_status_code(404));
                continue;
            }
            let q = parse_query(query);
            if q.get("state").map(String::as_str) != Some(state_tok.as_str()) {
                let _ = req.respond(
                    tiny_http::Response::from_string("state mismatch").with_status_code(400),
                );
                break Err((
                    "state_mismatch".into(),
                    "the authorization response carried a wrong state — re-run connect".into(),
                ));
            }
            if let Some(e) = q.get("error") {
                let _ = req.respond(tiny_http::Response::from_string(
                    "authorization was denied — you can close this tab",
                ));
                break Err((
                    "consent_denied".into(),
                    format!("the provider reported {e:?}"),
                ));
            }
            let Some(code) = q.get("code") else {
                let _ = req.respond(
                    tiny_http::Response::from_string("missing code").with_status_code(400),
                );
                break Err((
                    "token_exchange_failed".into(),
                    "the redirect carried neither code nor error".into(),
                ));
            };
            let _ = req.respond(tiny_http::Response::from_string(
                "authorized — you can close this tab",
            ));
            break self.exchange_code(&provider, &desc, code, &redirect_uri, &verifier);
        };

        {
            let mut st = flow.state.lock().expect("oauth flow lock poisoned");
            if matches!(*st, FlowState::Pending) {
                *st = FlowState::Done(outcome);
                flow.cond.notify_all();
            }
        }
        let mut flows = self.flows.lock().expect("oauth flows lock poisoned");
        if flows
            .get(&provider)
            .map(|f| Arc::ptr_eq(f, &flow))
            .unwrap_or(false)
        {
            flows.remove(&provider);
        }
    }

    /// The authorization-code exchange (ADR-011 §5): host-side over
    /// TLS, refresh token straight into the device-local store — it
    /// never crosses the boundary.
    fn exchange_code(
        &self,
        provider: &str,
        desc: &ProviderDescriptor,
        code: &str,
        redirect_uri: &str,
        verifier: &str,
    ) -> Result<FlowOutcome, (String, String)> {
        let handle = format!("{OAUTH_REF_PREFIX}{provider}");
        let refresh_ref = format!("{handle}.refresh");
        let client_id = self
            .client_value(&format!("{handle}.client_id"))
            .unwrap_or_default();
        let client_secret = self
            .client_value(&format!("{handle}.client_secret"))
            .unwrap_or_default();
        let resp = self
            .agent
            .post(&desc.token_url)
            .timeout(Duration::from_secs(TOKEN_TIMEOUT_S))
            .send_form(&[
                ("grant_type", "authorization_code"),
                ("code", code),
                ("redirect_uri", redirect_uri),
                ("client_id", &client_id),
                ("client_secret", &client_secret),
                ("code_verifier", verifier),
            ]);
        let body: Value = match resp {
            Ok(r) => {
                serde_json::from_str(&r.into_string().unwrap_or_default()).unwrap_or(Value::Null)
            }
            Err(ureq::Error::Status(code, r)) => {
                let body: Value = serde_json::from_str(&r.into_string().unwrap_or_default())
                    .unwrap_or(Value::Null);
                let err_code = body["error"].as_str().unwrap_or("");
                return Err((
                    "token_exchange_failed".into(),
                    format!("token endpoint returned {code} ({err_code})"),
                ));
            }
            Err(e) => {
                return Err((
                    "token_exchange_failed".into(),
                    format!("token endpoint unreachable: {e}"),
                ))
            }
        };
        let access = body["access_token"].as_str().unwrap_or("").to_string();
        if access.is_empty() {
            return Err((
                "token_exchange_failed".into(),
                "token endpoint answered 2xx without an access_token".into(),
            ));
        }
        let Some(refresh) = body["refresh_token"].as_str() else {
            // for Google this means access_type=offline/prompt=consent
            // were lost — a descriptor bug worth naming loudly (§5)
            return Err((
                "no_refresh_token".into(),
                "the provider returned no refresh token — for Google this means \
                 access_type=offline/prompt=consent were lost from the request"
                    .into(),
            ));
        };
        // custody (§1): persist device-local FIRST, then publish
        if let Some(p) = &self.persist {
            if let Err(e) = p.persist_secret(&refresh_ref, refresh) {
                tracing::warn!(
                    "oauth: could not persist {refresh_ref} ({e}); \
                     the grant lives only until this process exits"
                );
            }
        }
        self.secrets
            .lock()
            .expect("oauth secrets lock poisoned")
            .insert(refresh_ref, refresh.to_string());

        let granted_scopes: Vec<String> = body["scope"]
            .as_str()
            .unwrap_or("")
            .split_whitespace()
            .map(str::to_string)
            .collect();
        // account = the id_token's email claim, decoded unverified: it
        // arrived over TLS from the token endpoint WE chose (§5)
        let account = body["id_token"].as_str().and_then(id_token_email);
        {
            let mut meta = self.meta.lock().expect("oauth meta lock poisoned");
            meta.insert(format!("{handle}.granted_scopes"), json!(granted_scopes));
            if let Some(a) = &account {
                meta.insert(format!("{handle}.account"), json!(a));
            }
        }
        if let Some(p) = &self.persist {
            let scopes_key = format!("{handle}.granted_scopes");
            if let Err(e) = p.persist_meta(&scopes_key, &json!(granted_scopes)) {
                tracing::warn!("oauth: could not persist {scopes_key} ({e})");
            }
            if let Some(a) = &account {
                let account_key = format!("{handle}.account");
                if let Err(e) = p.persist_meta(&account_key, &json!(a)) {
                    tracing::warn!("oauth: could not persist {account_key} ({e})");
                }
            }
        }
        let expires_at = now_s() + body["expires_in"].as_f64().unwrap_or(3600.0);
        self.tokens
            .lock()
            .expect("oauth tokens lock poisoned")
            .insert(handle, CachedToken { access, expires_at });
        Ok(FlowOutcome {
            granted_scopes,
            account,
        })
    }

    /// Forget a dead grant: cache + live copy + stored value (empty
    /// value = the store's delete path).
    /// Forget the grant. Err = the stored token could NOT be deleted:
    /// with the store as the truth (`secret()`), the caller must not
    /// report the grant gone while the row still holds it.
    fn drop_grant(&self, provider: &str) -> Result<(), String> {
        let handle = format!("{OAUTH_REF_PREFIX}{provider}");
        let refresh_ref = format!("{handle}.refresh");
        self.tokens
            .lock()
            .expect("oauth tokens lock poisoned")
            .remove(&handle);
        self.secrets
            .lock()
            .expect("oauth secrets lock poisoned")
            .remove(&refresh_ref);
        if let Some(p) = &self.persist {
            p.persist_secret(&refresh_ref, "")
                .map_err(|e| e.to_string())?;
        }
        Ok(())
    }
}

/// RFC 4648 §5 base64url, no padding — the PKCE challenge encoding.
/// Local (~15 lines): a base64 crate for one call is not worth a
/// dependency here.
fn b64url_nopad(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = (u32::from(b[0]) << 16) | (u32::from(b[1]) << 8) | u32::from(b[2]);
        for i in 0..(chunk.len() + 1) {
            out.push(ALPHABET[(n >> (18 - 6 * i)) as usize & 63] as char);
        }
    }
    out
}

/// base64url decode (padding-tolerant) — only for the id_token payload.
fn b64url_decode(s: &str) -> Option<Vec<u8>> {
    let mut acc: u32 = 0;
    let mut bits = 0;
    let mut out = Vec::with_capacity(s.len() * 3 / 4);
    for c in s.bytes() {
        let v = match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'-' | b'+' => 62,
            b'_' | b'/' => 63,
            b'=' => continue,
            _ => return None,
        };
        acc = (acc << 6) | u32::from(v);
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
        }
    }
    Some(out)
}

/// PKCE S256 (RFC 7636), unconditional for every provider (ADR-011
/// §2): verifier = 64 hex chars (43–128 legal), challenge =
/// base64url(sha256(verifier)).
fn pkce_pair() -> (String, String) {
    let verifier = rand_hex(32);
    let challenge = b64url_nopad(&Sha256::digest(verifier.as_bytes()));
    (verifier, challenge)
}

fn rand_hex(n_bytes: usize) -> String {
    let mut buf = vec![0u8; n_bytes];
    crate::broker::getrandom(&mut buf);
    hex::encode(buf)
}

fn urldecode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => match u8::from_str_radix(&s[i + 1..i + 3], 16) {
                Ok(b) => {
                    out.push(b);
                    i += 3;
                }
                Err(_) => {
                    out.push(b'%');
                    i += 1;
                }
            },
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

pub(crate) fn parse_query(query: &str) -> BTreeMap<String, String> {
    query
        .split('&')
        .filter_map(|pair| {
            let (k, v) = pair.split_once('=')?;
            Some((urldecode(k), urldecode(v)))
        })
        .collect()
}

/// The email claim off an id_token, decoded WITHOUT verification — the
/// token arrived over TLS directly from the token endpoint we chose
/// (ADR-011 §5); it is display metadata, not an authority.
fn id_token_email(id_token: &str) -> Option<String> {
    let payload = id_token.split('.').nth(1)?;
    let claims: Value = serde_json::from_slice(&b64url_decode(payload)?).ok()?;
    claims["email"].as_str().map(str::to_string)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// A descriptor pointed at a test-local token endpoint.
    fn test_provider(token_url: &str) -> BTreeMap<String, ProviderDescriptor> {
        let mut m = BTreeMap::new();
        m.insert(
            "testprov".into(),
            ProviderDescriptor {
                authorize_url: "http://unused/auth".into(),
                token_url: token_url.into(),
                revoke_url: String::new(),
                auth_params: BTreeMap::new(),
                default_scopes: vec![],
                rotates_refresh_token: false,
                client_auth: "post_body".into(),
            },
        );
        m
    }

    /// Serve `responses` (status, json body) in order off an ephemeral
    /// loopback port; returns the endpoint url. The listener thread ends
    /// with the responses.
    fn fake_token_endpoint(responses: Vec<(u16, Value)>) -> (String, Arc<AtomicUsize>) {
        let server = tiny_http::Server::http("127.0.0.1:0").expect("bind test endpoint");
        let port = server.server_addr().to_ip().expect("ip addr").port();
        let hits = Arc::new(AtomicUsize::new(0));
        let counter = hits.clone();
        std::thread::spawn(move || {
            for (status, body) in responses {
                let Ok(req) = server.recv() else { return };
                counter.fetch_add(1, Ordering::SeqCst);
                let data = body.to_string();
                let resp = tiny_http::Response::from_string(data).with_status_code(status);
                let _ = req.respond(resp);
            }
        });
        (format!("http://127.0.0.1:{port}/token"), hits)
    }

    fn seeded_state(token_url: &str, persist: Option<Box<dyn SecretPersist>>) -> OauthState {
        let state = OauthState::new(test_provider(token_url), persist);
        let mut seeds = BTreeMap::from([
            (
                "connector.oauth.testprov.refresh".to_string(),
                "refresh-secret".to_string(),
            ),
            (
                "connector.oauth.testprov.client_id".to_string(),
                "cid".to_string(),
            ),
            (
                "connector.oauth.testprov.client_secret".to_string(),
                "csec".to_string(),
            ),
            ("connector.key.linear".to_string(), "lin_x".to_string()),
        ]);
        state.seed(&mut seeds);
        // seed drains ONLY the oauth namespace
        assert_eq!(
            seeds.keys().collect::<Vec<_>>(),
            vec!["connector.key.linear"]
        );
        state
    }

    #[derive(Default)]
    struct RecordingPersist(Mutex<Vec<(String, String)>>);
    impl SecretPersist for Arc<RecordingPersist> {
        fn persist_secret(&self, key: &str, value: &str) -> anyhow::Result<()> {
            self.0.lock().unwrap().push((key.into(), value.into()));
            Ok(())
        }
        fn persist_meta(&self, _key: &str, _value: &Value) -> anyhow::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn refresh_caches_and_reports_no_token_material() {
        let (url, hits) = fake_token_endpoint(vec![(
            200,
            json!({"access_token": "tok-access", "expires_in": 3600, "scope": "a b"}),
        )]);
        let state = seeded_state(&url, None);
        let out = state.refresh("testprov").unwrap();
        assert_eq!(out["ok"], json!(true));
        assert_eq!(out["scope"], json!("a b"));
        assert_eq!(out["rotated"], json!(false));
        assert!(!out.to_string().contains("tok-access"));
        assert_eq!(hits.load(Ordering::SeqCst), 1);
        assert_eq!(
            state.fresh_token("connector.oauth.testprov").as_deref(),
            Some("tok-access")
        );
    }

    #[test]
    fn rotation_persists_before_publishing() {
        let (url, _) = fake_token_endpoint(vec![(
            200,
            json!({"access_token": "tok2", "expires_in": 3600,
                   "refresh_token": "refresh-rotated"}),
        )]);
        let log = Arc::new(RecordingPersist::default());
        let state = seeded_state(&url, Some(Box::new(log.clone())));
        let out = state.refresh("testprov").unwrap();
        assert_eq!(out["rotated"], json!(true));
        assert_eq!(
            *log.0.lock().unwrap(),
            vec![(
                "connector.oauth.testprov.refresh".to_string(),
                "refresh-rotated".to_string()
            )]
        );
        assert_eq!(
            state.secret("connector.oauth.testprov.refresh").as_deref(),
            Some("refresh-rotated")
        );
    }

    #[test]
    fn invalid_grant_drops_the_grant() {
        let (url, _) = fake_token_endpoint(vec![(400, json!({"error": "invalid_grant"}))]);
        let log = Arc::new(RecordingPersist::default());
        let state = seeded_state(&url, Some(Box::new(log.clone())));
        let err = state.refresh("testprov").unwrap_err();
        assert_eq!(err.type_, "oauth_reconsent_required");
        assert!(state.secret("connector.oauth.testprov.refresh").is_none());
        // stored value deleted through the empty-value path
        assert_eq!(
            *log.0.lock().unwrap(),
            vec![(
                "connector.oauth.testprov.refresh".to_string(),
                String::new()
            )]
        );
        // and the next attempt is not_connected, not another exchange
        let err = state.refresh("testprov").unwrap_err();
        assert_eq!(err.type_, "not_connected");
    }

    #[test]
    fn missing_client_id_is_not_configured() {
        let (url, hits) = fake_token_endpoint(vec![]);
        let state = OauthState::new(test_provider(&url), None);
        let mut seeds = BTreeMap::from([(
            "connector.oauth.testprov.refresh".to_string(),
            "r".to_string(),
        )]);
        state.seed(&mut seeds);
        let err = state.refresh("testprov").unwrap_err();
        assert_eq!(err.type_, "not_configured");
        assert!(err.message.contains("client_id"));
        assert_eq!(hits.load(Ordering::SeqCst), 0);
    }

    // --- the consent flow (ADR-011 §5, transport A) ---------------------

    use std::time::{Duration, Instant};

    /// A token/revoke endpoint that captures every request body and
    /// answers with a fixed response.
    fn capturing_endpoint(response: Value) -> (String, Arc<Mutex<Vec<String>>>) {
        let server = tiny_http::Server::http("127.0.0.1:0").expect("bind test endpoint");
        let port = server.server_addr().to_ip().expect("ip addr").port();
        let bodies = Arc::new(Mutex::new(Vec::new()));
        let cap = bodies.clone();
        std::thread::spawn(move || {
            for _ in 0..8 {
                let Ok(mut req) = server.recv() else { return };
                let mut body = String::new();
                let _ = req.as_reader().read_to_string(&mut body);
                cap.lock().unwrap().push(body);
                let _ = req.respond(tiny_http::Response::from_string(response.to_string()));
            }
        });
        (format!("http://127.0.0.1:{port}/token"), bodies)
    }

    fn full_token_response() -> Value {
        let id_payload = b64url_nopad(json!({"email": "u@example.com"}).to_string().as_bytes());
        json!({"access_token": "tok-a", "expires_in": 3600,
               "refresh_token": "refresh-new", "scope": "s1 s2",
               "id_token": format!("hdr.{id_payload}.sig")})
    }

    /// A connectable state: client seeded, no grant yet, consent hook
    /// capturing the URL + call count.
    fn connect_state(
        token_url: &str,
        revoke_url: &str,
    ) -> (
        Arc<OauthState>,
        Arc<Mutex<Option<String>>>,
        Arc<AtomicUsize>,
    ) {
        let mut providers = test_provider(token_url);
        let p = providers.get_mut("testprov").unwrap();
        p.revoke_url = revoke_url.into();
        p.default_scopes = vec!["s1".into(), "s2".into()];
        p.auth_params = BTreeMap::from([("prompt".to_string(), "consent".to_string())]);
        let mut state = OauthState::new(providers, None);
        let url_slot = Arc::new(Mutex::new(None));
        let calls = Arc::new(AtomicUsize::new(0));
        let (slot, count) = (url_slot.clone(), calls.clone());
        state.consent = Some(ConsentHook(Arc::new(move |req: ConsentRequest| {
            count.fetch_add(1, Ordering::SeqCst);
            *slot.lock().unwrap() = Some(req.url);
        })));
        let state = Arc::new(state);
        let mut seeds = BTreeMap::from([
            (
                "connector.oauth.testprov.client_id".to_string(),
                "cid".to_string(),
            ),
            (
                "connector.oauth.testprov.client_secret".to_string(),
                "csec".to_string(),
            ),
        ]);
        state.seed(&mut seeds);
        (state, url_slot, calls)
    }

    /// Play the human+browser: wait for the consent URL, then hit the
    /// loopback receiver. Returns the consent URL's query params.
    fn complete_consent(
        url_slot: &Arc<Mutex<Option<String>>>,
        code: &str,
        wrong_state: bool,
        error: Option<&str>,
    ) -> BTreeMap<String, String> {
        let deadline = Instant::now() + Duration::from_secs(3);
        let url = loop {
            if let Some(u) = url_slot.lock().unwrap().clone() {
                break u;
            }
            assert!(Instant::now() < deadline, "consent URL never delivered");
            std::thread::sleep(Duration::from_millis(20));
        };
        let q = parse_query(url.split_once('?').expect("consent url has a query").1);
        let redirect = q["redirect_uri"].clone();
        let state_tok = if wrong_state { "WRONG" } else { &q["state"] };
        let cb = match error {
            Some(e) => format!("{redirect}?error={e}&state={state_tok}"),
            None => format!("{redirect}?code={code}&state={state_tok}"),
        };
        let _ = ureq::get(&cb).timeout(Duration::from_secs(2)).call();
        q
    }

    #[test]
    fn connect_full_flow() {
        let (token_url, bodies) = capturing_endpoint(full_token_response());
        let (state, url_slot, calls) = connect_state(&token_url, "");
        let slot = url_slot.clone();
        let clicker = std::thread::spawn(move || complete_consent(&slot, "authcode1", false, None));
        let out = state.connect("testprov", None, Some(5.0), None).unwrap();
        let q = clicker.join().unwrap();

        assert_eq!(out["ok"], json!(true));
        assert_eq!(out["grantedScopes"], json!(["s1", "s2"]));
        assert_eq!(out["account"], json!("u@example.com"));
        assert!(!out.to_string().contains("tok-a"));
        assert_eq!(calls.load(Ordering::SeqCst), 1);

        // the authorize request carried PKCE S256 + the descriptor params
        assert_eq!(q["code_challenge_method"], "S256");
        assert_eq!(q["response_type"], "code");
        assert_eq!(q["prompt"], "consent");
        assert_eq!(q["scope"], "s1 s2");
        // the exchange presented the matching verifier + the same
        // redirect_uri, and the code we "clicked" with
        let body = parse_query(&bodies.lock().unwrap()[0]);
        assert_eq!(body["grant_type"], "authorization_code");
        assert_eq!(body["code"], "authcode1");
        assert_eq!(body["redirect_uri"], q["redirect_uri"]);
        let challenge = b64url_nopad(&Sha256::digest(body["code_verifier"].as_bytes()));
        assert_eq!(challenge, q["code_challenge"]);

        // custody: grant stored, token cached, status reflects it
        assert_eq!(
            state.secret("connector.oauth.testprov.refresh").as_deref(),
            Some("refresh-new")
        );
        assert_eq!(
            state.fresh_token("connector.oauth.testprov").as_deref(),
            Some("tok-a")
        );
        let st = state.status("testprov").unwrap();
        assert_eq!(st["connected"], json!(true));
        assert_eq!(st["pending"], json!(false));
        assert_eq!(st["scopes"], json!(["s1", "s2"]));
        assert_eq!(st["account"], json!("u@example.com"));
    }

    #[test]
    fn status_is_not_pending_once_the_flow_is_done() {
        // the window the receiver thread leaves open: outcome stored and
        // waiters notified, map entry not yet dropped (run_flow's order).
        // connect() returns on the notify; a status read right after
        // used to report pending: true from the lingering entry (CI
        // flake in connect_full_flow, 2026-09-14).
        let state = seeded_state("http://127.0.0.1:1/token", None);
        state.flows.lock().unwrap().insert(
            "testprov".into(),
            Arc::new(ConsentFlow {
                state: Mutex::new(FlowState::Done(Ok(FlowOutcome {
                    granted_scopes: vec!["s1".into()],
                    account: None,
                }))),
                cond: Condvar::new(),
            }),
        );
        assert_eq!(state.status("testprov").unwrap()["pending"], json!(false));
        // a flow still waiting on consent is the real pending
        state.flows.lock().unwrap().insert(
            "testprov".into(),
            Arc::new(ConsentFlow {
                state: Mutex::new(FlowState::Pending),
                cond: Condvar::new(),
            }),
        );
        assert_eq!(state.status("testprov").unwrap()["pending"], json!(true));
    }

    #[test]
    fn connect_state_mismatch_tears_down() {
        let (token_url, bodies) = capturing_endpoint(full_token_response());
        let (state, url_slot, _) = connect_state(&token_url, "");
        let slot = url_slot.clone();
        let clicker = std::thread::spawn(move || complete_consent(&slot, "authcode1", true, None));
        let err = state
            .connect("testprov", None, Some(5.0), None)
            .unwrap_err();
        clicker.join().unwrap();
        assert_eq!(err.type_, "state_mismatch");
        assert!(bodies.lock().unwrap().is_empty()); // no exchange happened
        assert!(state.secret("connector.oauth.testprov.refresh").is_none());
    }

    #[test]
    fn connect_denied_and_no_refresh_token() {
        let (token_url, _) = capturing_endpoint(full_token_response());
        let (state, url_slot, _) = connect_state(&token_url, "");
        let slot = url_slot.clone();
        let clicker =
            std::thread::spawn(move || complete_consent(&slot, "", false, Some("access_denied")));
        let err = state
            .connect("testprov", None, Some(5.0), None)
            .unwrap_err();
        clicker.join().unwrap();
        assert_eq!(err.type_, "consent_denied");

        // a provider that omits the refresh token is named loudly (§5)
        let (token_url, _) = capturing_endpoint(json!({"access_token": "tok-a"}));
        let (state, url_slot, _) = connect_state(&token_url, "");
        let slot = url_slot.clone();
        let clicker = std::thread::spawn(move || complete_consent(&slot, "authcode2", false, None));
        let err = state
            .connect("testprov", None, Some(5.0), None)
            .unwrap_err();
        clicker.join().unwrap();
        assert_eq!(err.type_, "no_refresh_token");
    }

    #[test]
    fn connect_timeout_then_late_completion() {
        let (token_url, _) = capturing_endpoint(full_token_response());
        let (state, url_slot, _) = connect_state(&token_url, "");
        // nobody clicks inside the connect timeout
        let err = state
            .connect("testprov", None, Some(0.2), None)
            .unwrap_err();
        assert_eq!(err.type_, "consent_timeout");
        let st = state.status("testprov").unwrap();
        assert_eq!(st["connected"], json!(false));
        assert_eq!(st["pending"], json!(true)); // the receiver outlives connect

        // the human clicks late; status flips host-side (§5)
        complete_consent(&url_slot, "authcode3", false, None);
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            let st = state.status("testprov").unwrap();
            if st["connected"] == json!(true) {
                assert_eq!(st["pending"], json!(false));
                break;
            }
            assert!(Instant::now() < deadline, "late completion never landed");
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    #[test]
    fn concurrent_connect_joins_the_pending_flow() {
        let (token_url, _) = capturing_endpoint(full_token_response());
        let (state, url_slot, calls) = connect_state(&token_url, "");
        let s2 = state.clone();
        let waiter = std::thread::spawn(move || s2.connect("testprov", None, Some(5.0), None));
        // second connect while pending: joins — one receiver, one URL
        let err = state
            .connect("testprov", None, Some(0.2), None)
            .unwrap_err();
        assert_eq!(err.type_, "consent_timeout");
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        complete_consent(&url_slot, "authcode4", false, None);
        let out = waiter.join().unwrap().unwrap();
        assert_eq!(out["ok"], json!(true));
    }

    /// ADR-021 §4: the store answers first — a `.client_id` rotated
    /// through the UI is not shadowed by the boot-loaded live copy.
    struct RowSource(BTreeMap<String, String>);
    impl crate::broker::SecretSource for RowSource {
        fn read(&self, key: &str) -> Result<Option<String>, String> {
            Ok(self.0.get(key).cloned())
        }
        fn mark_missing(&self, _key: &str, _about: &Value, _run_id: &str) {}
        fn mark_rejected(&self, _key: &str, _about: &Value, _run_id: &str, _status: u16) {}
    }

    #[test]
    fn store_shadows_the_live_copy_for_sub_refs() {
        let (token_url, _) = capturing_endpoint(full_token_response());
        let mut state = seeded_state(&token_url, None);
        assert_eq!(
            state
                .secret("connector.oauth.testprov.client_id")
                .as_deref(),
            Some("cid")
        );
        state.source = Some(Arc::new(RowSource(BTreeMap::from([(
            "connector.oauth.testprov.client_id".to_string(),
            "cid-rotated".to_string(),
        )]))));
        assert_eq!(
            state
                .secret("connector.oauth.testprov.client_id")
                .as_deref(),
            Some("cid-rotated")
        );
        // a ref the store lacks is a miss, not a stale live hit
        assert!(state.secret("connector.oauth.testprov.refresh").is_none());
    }

    struct FailingPersist;
    impl SecretPersist for FailingPersist {
        fn persist_secret(&self, _key: &str, _value: &str) -> anyhow::Result<()> {
            anyhow::bail!("store write failed")
        }
        fn persist_meta(&self, _key: &str, _value: &Value) -> anyhow::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn disconnect_fails_loudly_when_the_stored_token_survives() {
        let (revoke_url, _) = capturing_endpoint(json!({}));
        let (token_url, _) = capturing_endpoint(full_token_response());
        let mut providers = test_provider(&token_url);
        providers.get_mut("testprov").unwrap().revoke_url = revoke_url;
        let state = OauthState::new(providers, Some(Box::new(FailingPersist)));
        let mut seeds = BTreeMap::from([(
            "connector.oauth.testprov.refresh".to_string(),
            "refresh-secret".to_string(),
        )]);
        state.seed(&mut seeds);
        let err = state.disconnect("testprov").unwrap_err();
        assert_eq!(err.type_, "disconnect_failed");
        assert!(err.message.contains("revoke succeeded"));
    }

    #[test]
    fn disconnect_revokes_and_deletes() {
        let (revoke_url, revoke_bodies) = capturing_endpoint(json!({}));
        let (token_url, _) = capturing_endpoint(full_token_response());
        let mut providers = test_provider(&token_url);
        providers.get_mut("testprov").unwrap().revoke_url = revoke_url;
        let state = OauthState::new(providers, None);
        let mut seeds = BTreeMap::from([
            (
                "connector.oauth.testprov.refresh".to_string(),
                "refresh-secret".to_string(),
            ),
            (
                "connector.oauth.testprov.client_id".to_string(),
                "cid".to_string(),
            ),
        ]);
        state.seed(&mut seeds);
        let out = state.disconnect("testprov").unwrap();
        assert_eq!(out["ok"], json!(true));
        assert_eq!(out["revoked"], json!(true));
        // provider-side revoke saw the token; local copy is gone
        assert!(revoke_bodies.lock().unwrap()[0].contains("token=refresh-secret"));
        assert!(state.secret("connector.oauth.testprov.refresh").is_none());
        assert_eq!(state.status("testprov").unwrap()["connected"], json!(false));
    }

    #[test]
    fn connect_effect_records_no_secret_material() {
        // through the broker: the recorded oauth.connect effect carries
        // provider+scopes in, grantedScopes out — nothing else (§9)
        let (token_url, _) = capturing_endpoint(full_token_response());
        let (state, url_slot, _) = connect_state(&token_url, "");
        let slot = url_slot.clone();
        let clicker = std::thread::spawn(move || complete_consent(&slot, "authcode9", false, None));
        let mut b = crate::broker::Broker::new(
            crate::trace::TraceWriter::new(json!({"id": "run_connect"})),
            BTreeMap::new(),
            BTreeMap::new(),
            None,
            crate::routes::Classifier::new(None),
        );
        b.oauth = Some(state);
        let out = b
            .call(
                "oauth.connect",
                json!({"provider": "testprov", "timeout": 5.0}),
            )
            .unwrap();
        clicker.join().unwrap();
        assert_eq!(out["ok"], json!(true));
        let rec = b
            .writer
            .records
            .iter()
            .find(|r| r["effect"] == "oauth.connect")
            .unwrap();
        assert_eq!(rec["meta"]["class"], json!("mutate"));
        assert_eq!(rec["output"]["grantedScopes"], json!(["s1", "s2"]));
        let dump = serde_json::to_string(&b.writer.records).unwrap();
        for secret in ["tok-a", "refresh-new", "authcode9", "code_verifier"] {
            assert!(!dump.contains(secret), "trace leaked {secret}");
        }
    }
}
