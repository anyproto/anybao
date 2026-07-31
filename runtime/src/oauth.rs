//! Managed OAuth credentials (ADR-011): provider descriptors and the
//! host-held token lifecycle behind `connector.oauth.<provider>` refs.
//! Custody rule (ADR-011 §1): no token — access, refresh, or
//! authorization code — is ever returned to guest code, written to a
//! synced field, or recorded in a trace. The broker resolves managed
//! refs through `OauthState` at injection time (ADR-011 §6); this
//! module owns the state and the refresh-token exchange.

use crate::broker::EffectFailure;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

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

/// One per process, shared across runs/threads: per-run broker
/// snapshots never hold oauth material (`seed` drains it), and the
/// access-token cache is process-wide (ADR-011 §3 — Google caps live
/// tokens per client; a per-run cache would mint one per run).
pub struct OauthState {
    pub providers: BTreeMap<String, ProviderDescriptor>,
    /// `connector.oauth.*` sub-ref values — the authoritative live copy.
    secrets: Mutex<BTreeMap<String, String>>,
    /// Access tokens by handle ref (`connector.oauth.google`).
    tokens: Mutex<BTreeMap<String, CachedToken>>,
    /// Per-ref single-flight refresh gate (ADR-011 §6).
    flight: Mutex<BTreeMap<String, Arc<Mutex<()>>>>,
    pub persist: Option<Box<dyn SecretPersist>>,
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
            tokens: Mutex::new(BTreeMap::new()),
            flight: Mutex::new(BTreeMap::new()),
            persist,
            agent: ureq::agent(),
        }
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

    pub fn secret(&self, key: &str) -> Option<String> {
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
        let client_id = self.secret(&format!("{handle}.client_id")).ok_or_else(|| {
            fail(
                "not_configured",
                format!(
                    "no {handle}.client_id — seed it (and {handle}.client_secret) \
                     via .connectors.env / --secrets-file"
                ),
            )
        })?;
        // A Desktop-client secret is a public-client secret (ADR-011 §3):
        // sent when present, proves nothing, never required.
        let client_secret = self
            .secret(&format!("{handle}.client_secret"))
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
                    self.drop_grant(provider);
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

    /// Forget a dead grant: cache + live copy + stored value (empty
    /// value = the store's delete path).
    fn drop_grant(&self, provider: &str) {
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
            if let Err(e) = p.persist_secret(&refresh_ref, "") {
                tracing::warn!("oauth: could not delete stored {refresh_ref} ({e})");
            }
        }
    }
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
}
