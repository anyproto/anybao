//! Fixed upstream protocol; private transport doubles are tests only (docs/chatgpt-local-oauth.md §5–6).

use super::*;
use base64::Engine;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};

pub(super) const CLIENT_ID: &str = "app_EMoamEEZ73f0CkXaXp7hrann";
pub(super) const ISSUER: &str = "https://auth.openai.com";
pub(super) const DEVICE_VERIFY: &str = "https://auth.openai.com/codex/device";
pub(super) const DEVICE_REDIRECT: &str = "https://auth.openai.com/deviceauth/callback";
pub(super) const BROWSER_REDIRECT: &str = "http://localhost:1455/auth/callback";
const MAX_AUTH_BODY: u64 = 131_072;
const CLAIMS: &str = "https://api.openai.com/auth";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Endpoint {
    DeviceCode,
    DevicePoll,
    Token,
}

impl Endpoint {
    fn url(self) -> &'static str {
        match self {
            Self::DeviceCode => "https://auth.openai.com/api/accounts/deviceauth/usercode",
            Self::DevicePoll => "https://auth.openai.com/api/accounts/deviceauth/token",
            Self::Token => "https://auth.openai.com/oauth/token",
        }
    }
}

pub(super) enum AuthBody {
    Json(Value),
    Form(Vec<(&'static str, String)>),
}

pub(super) struct AuthReply {
    pub status: u16,
    pub body: Value,
    pub retry_after: Option<u64>,
}

#[derive(Clone, Copy)]
pub(super) enum TransportFailure {
    BeforeSend,
    Uncertain,
}

pub(super) trait AuthTransport: Send + Sync {
    fn post(
        &self,
        endpoint: Endpoint,
        body: AuthBody,
        timeout: Duration,
    ) -> Result<AuthReply, TransportFailure>;
}

pub(super) struct FixedTransport;

impl AuthTransport for FixedTransport {
    fn post(
        &self,
        endpoint: Endpoint,
        body: AuthBody,
        timeout: Duration,
    ) -> Result<AuthReply, TransportFailure> {
        // docs/chatgpt-local-oauth.md §4/§6: no redirect, pooled-connection replay, or configurable URL.
        let agent = ureq::AgentBuilder::new()
            .redirects(0)
            .try_proxy_from_env(false)
            .https_only(true)
            .max_idle_connections(0)
            .timeout(timeout.min(Duration::from_secs(30)))
            .user_agent(concat!("anybao/", env!("CARGO_PKG_VERSION")))
            .build();
        let request = agent.post(endpoint.url()).set("Accept", "application/json");
        let response = match body {
            AuthBody::Json(value) => request.send_json(value),
            AuthBody::Form(pairs) => {
                let pairs: Vec<_> = pairs.iter().map(|(k, v)| (*k, v.as_str())).collect();
                request.send_form(&pairs)
            }
        };
        read_auth_reply(response)
    }
}

// Keep transport classification and bounded response reads on the same path.
pub(super) fn read_auth_reply(
    response: Result<ureq::Response, ureq::Error>,
) -> Result<AuthReply, TransportFailure> {
    let response = match response {
        Ok(response) | Err(ureq::Error::Status(_, response)) => response,
        // Pinned ureq 2.12.1: unit::connect_inner connects before send_prelude/send_body;
        // stream::connect_host and rtls::connect return these kinds only while
        // resolving/connecting, before any HTTP bytes. This relies on the fresh
        // agent above: no redirects, proxy, or pooled-connection replay. Generic
        // I/O (including TLS I/O), HTTP errors and body-read errors stay uncertain.
        Err(ureq::Error::Transport(error))
            if matches!(
                error.kind(),
                ureq::ErrorKind::Dns | ureq::ErrorKind::ConnectionFailed
            ) =>
        {
            return Err(TransportFailure::BeforeSend);
        }
        Err(_) => return Err(TransportFailure::Uncertain),
    };
    let status = response.status();
    let retry_after = response
        .header("Retry-After")
        .and_then(|s| s.parse::<u64>().ok())
        .map(|s| s.clamp(3, 60));
    let mut bytes = Vec::new();
    response
        .into_reader()
        .take(MAX_AUTH_BODY + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| TransportFailure::Uncertain)?;
    if bytes.len() as u64 > MAX_AUTH_BODY {
        return Err(TransportFailure::Uncertain);
    }
    let body = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    Ok(AuthReply {
        status,
        body,
        retry_after,
    })
}

fn number(value: &Value) -> Option<u64> {
    value.as_u64().or_else(|| value.as_str()?.parse().ok())
}

fn secret_string(value: &Value) -> Result<String, AuthErrorCode> {
    value
        .as_str()
        .filter(|s| token_text(s))
        .map(str::to_owned)
        .ok_or(AuthErrorCode::UnsupportedTokenShape)
}

pub(super) fn token_text(s: &str) -> bool {
    !s.is_empty() && s.len() <= 32_768 && s.bytes().all(|b| b.is_ascii_graphic())
}

pub(super) fn header_text(s: &str) -> bool {
    !s.is_empty() && s.len() <= 256 && s.bytes().all(|b| b.is_ascii_graphic())
}

fn local_label(value: &Value) -> Option<String> {
    value
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= 256 && !s.chars().any(char::is_control))
        .map(str::to_string)
}

fn jwt_claims(token: &str) -> Result<Value, AuthErrorCode> {
    let pieces: Vec<_> = token.split('.').collect();
    if pieces.len() != 3 || pieces.iter().any(|s| s.is_empty()) {
        return Err(AuthErrorCode::UnsupportedTokenShape);
    }
    let bytes = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(pieces[1])
        .map_err(|_| AuthErrorCode::UnsupportedTokenShape)?;
    let claims: Value =
        serde_json::from_slice(&bytes).map_err(|_| AuthErrorCode::UnsupportedTokenShape)?;
    if !claims.is_object() {
        return Err(AuthErrorCode::UnsupportedTokenShape);
    }
    Ok(claims)
}

fn consistent_claim(a: &Value, b: &Value) -> Result<String, AuthErrorCode> {
    let get = |v: &Value| -> Result<Option<String>, AuthErrorCode> {
        if v.is_null() {
            return Ok(None);
        }
        Ok(Some(
            v.as_str()
                .filter(|s| header_text(s))
                .ok_or(AuthErrorCode::UnsupportedTokenShape)?
                .to_string(),
        ))
    };
    match (get(a)?, get(b)?) {
        (Some(a), Some(b)) if a != b => Err(AuthErrorCode::UnsupportedTokenShape),
        (Some(a), _) | (_, Some(a)) => Ok(a),
        _ => Err(AuthErrorCode::UnsupportedTokenShape),
    }
}

pub(super) fn parse_grant(
    body: &Value,
    now: u64,
    old: Option<&Grant>,
) -> Result<Grant, AuthErrorCode> {
    if body
        .get("token_type")
        .is_some_and(|v| !v.as_str().is_some_and(|s| s.eq_ignore_ascii_case("bearer")))
    {
        return Err(AuthErrorCode::UnsupportedTokenShape);
    }
    let access = secret_string(&body["access_token"])?;
    let refresh = match body.get("refresh_token") {
        Some(value) => secret_string(value)?,
        None => old
            .map(|g| g.refresh.0.clone())
            .ok_or(AuthErrorCode::UnsupportedTokenShape)?,
    };
    let claims = jwt_claims(&access)?;
    let identity = match body.get("id_token") {
        Some(token) => jwt_claims(&secret_string(token)?)?,
        None => Value::Null,
    };
    let issuer = consistent_claim(&claims["iss"], &identity["iss"])?;
    if issuer != ISSUER {
        return Err(AuthErrorCode::UnsupportedTokenShape);
    }
    let principal = Principal {
        issuer,
        subject: consistent_claim(&claims["sub"], &identity["sub"])?,
        account_id: consistent_claim(
            &claims[CLAIMS]["chatgpt_account_id"],
            &identity[CLAIMS]["chatgpt_account_id"],
        )?,
    };
    if old.is_some_and(|old| old.principal != principal) {
        return Err(AuthErrorCode::AccountChanged);
    }
    let endpoint_expiry = number(&body["expires_in"])
        .filter(|s| *s > 0)
        .and_then(|s| now.checked_add(s));
    let token_expiry = claims["exp"].as_u64().filter(|s| *s > 0);
    let expiry = [endpoint_expiry, token_expiry]
        .into_iter()
        .flatten()
        .min()
        .filter(|expiry| *expiry > now)
        .ok_or(AuthErrorCode::UnsupportedTokenShape)?;
    let residency_claim = |key| match (&claims[CLAIMS][key], &identity[CLAIMS][key]) {
        (Value::Null, Value::Null) => Ok(None),
        (a, b) => consistent_claim(a, b).map(Some),
    };
    let data_residency = residency_claim("chatgpt_data_residency")?;
    let compute_residency = residency_claim("chatgpt_compute_residency")?;
    let residency = data_residency.or(compute_residency);
    Ok(Grant {
        id: old.map(|g| g.id.clone()).unwrap_or_else(random_id),
        pair_revision: old.map_or(Ok(1), |g| {
            g.pair_revision
                .checked_add(1)
                .ok_or(AuthErrorCode::InvalidStore)
        })?,
        access: Secret(access),
        refresh: Secret(refresh),
        expires_at: expiry,
        principal,
        account_label: local_label(&identity["email"]).or_else(|| local_label(&claims["email"])),
        plan_type: local_label(&claims[CLAIMS]["chatgpt_plan_type"])
            .or_else(|| local_label(&identity[CLAIMS]["chatgpt_plan_type"])),
        residency,
        state: GrantState::Ready,
        retry_after: None,
    })
}

pub(super) fn validate_envelope(
    envelope: &GrantEnvelope,
    slot: &GrantSlot,
) -> Result<(), AuthErrorCode> {
    if envelope.schema_version != 1 || envelope.slot != *slot || envelope.encode().is_err() {
        return Err(AuthErrorCode::InvalidStore);
    }
    if let Some(p) = &envelope.pending {
        if p.epoch != envelope.epoch
            || uuid::Uuid::parse_str(&p.id).is_err()
            || uuid::Uuid::parse_str(&p.owner_instance).is_err()
            || p.expires_at == 0
        {
            return Err(AuthErrorCode::InvalidStore);
        }
    }
    if let Some(grant) = &envelope.grant {
        if uuid::Uuid::parse_str(&grant.id).is_err()
            || grant.pair_revision == 0
            || grant.principal.issuer != ISSUER
            || !header_text(&grant.principal.subject)
            || !header_text(&grant.principal.account_id)
            || grant.expires_at == 0
            || grant.residency.as_deref().is_some_and(|s| !header_text(s))
            || [&grant.account_label, &grant.plan_type]
                .into_iter()
                .flatten()
                .any(|s| local_label(&json!(s)).is_none())
        {
            return Err(AuthErrorCode::InvalidStore);
        }
        match &grant.state {
            GrantState::Ready | GrantState::RefreshInflight { .. } => {
                if !token_text(&grant.access.0) || !token_text(&grant.refresh.0) {
                    return Err(AuthErrorCode::InvalidStore);
                }
                let claims =
                    jwt_claims(&grant.access.0).map_err(|_| AuthErrorCode::InvalidStore)?;
                if claims["iss"]
                    .as_str()
                    .is_some_and(|s| s != grant.principal.issuer)
                    || claims["sub"]
                        .as_str()
                        .is_some_and(|s| s != grant.principal.subject)
                    || claims[CLAIMS]["chatgpt_account_id"]
                        .as_str()
                        .is_some_and(|s| s != grant.principal.account_id)
                    || claims["exp"].as_u64().is_some_and(|s| s < grant.expires_at)
                {
                    return Err(AuthErrorCode::InvalidStore);
                }
                if let GrantState::RefreshInflight { attempt_id } = &grant.state {
                    if uuid::Uuid::parse_str(attempt_id).is_err() {
                        return Err(AuthErrorCode::InvalidStore);
                    }
                }
            }
            GrantState::ReauthRequired { reason } => {
                if !matches!(
                    reason,
                    AuthErrorCode::RefreshUncertain
                        | AuthErrorCode::GrantRejected
                        | AuthErrorCode::AccountChanged
                ) {
                    return Err(AuthErrorCode::InvalidStore);
                }
            }
        }
    }
    Ok(())
}

pub(super) fn error_code(body: &Value) -> &str {
    body["error"]
        .as_str()
        .or_else(|| body["error"]["code"].as_str())
        .unwrap_or("")
}

pub(super) fn rejected_grant(reply: &AuthReply) -> bool {
    matches!(reply.status, 400 | 401 | 403)
        && matches!(
            error_code(&reply.body),
            "invalid_grant"
                | "refresh_token_reused"
                | "refresh_token_expired"
                | "refresh_token_invalidated"
        )
}

pub(super) fn login_error(reply: &AuthReply) -> AuthErrorCode {
    match error_code(&reply.body) {
        "access_denied" | "authorization_declined" => AuthErrorCode::ConsentDenied,
        "expired_token" => AuthErrorCode::LoginExpired,
        "device_auth_disabled" | "device_auth_not_allowed" => AuthErrorCode::DeviceFlowDisabled,
        _ if reply.status == 429 => AuthErrorCode::AuthRateLimited,
        _ => AuthErrorCode::TokenExchangeFailed,
    }
}

pub(super) fn authorization_form(
    code: String,
    verifier: String,
    redirect: &'static str,
) -> AuthBody {
    AuthBody::Form(vec![
        ("grant_type", "authorization_code".into()),
        ("client_id", CLIENT_ID.into()),
        ("code", code),
        ("code_verifier", verifier),
        ("redirect_uri", redirect.into()),
    ])
}

pub(super) struct DeviceCode {
    pub id: Secret,
    pub code: Secret,
    pub interval: u64,
    pub expires_at: u64,
}

pub(super) fn device_code(
    reply: &AuthReply,
    now: u64,
    deadline: u64,
) -> Result<DeviceCode, AuthErrorCode> {
    if reply.status != 200 {
        return Err(if reply.status == 403 {
            AuthErrorCode::DeviceFlowDisabled
        } else {
            login_error(reply)
        });
    }
    let code = secret_string(&reply.body["user_code"])?;
    if code.len() > 64 {
        return Err(AuthErrorCode::UnsupportedTokenShape);
    }
    let interval = number(&reply.body["interval"]).unwrap_or(5).clamp(3, 900);
    let expires_at = match reply.body.get("expires_in") {
        Some(value) => now
            .checked_add(
                number(value)
                    .filter(|n| *n > 0)
                    .ok_or(AuthErrorCode::UnsupportedTokenShape)?,
            )
            .ok_or(AuthErrorCode::UnsupportedTokenShape)?
            .min(deadline),
        None => deadline,
    };
    Ok(DeviceCode {
        id: Secret(secret_string(&reply.body["device_auth_id"])?),
        code: Secret(code),
        interval,
        expires_at,
    })
}

pub(super) fn poll_code(reply: &AuthReply) -> Result<(String, String), AuthErrorCode> {
    if reply.status != 200 {
        return Err(login_error(reply));
    }
    Ok((
        secret_string(&reply.body["authorization_code"])?,
        secret_string(&reply.body["code_verifier"])?,
    ))
}

pub(super) fn browser_start() -> Result<(TcpListener, Secret, Secret, String), AuthErrorCode> {
    let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 1455)).map_err(|e| {
        if e.kind() == std::io::ErrorKind::AddrInUse {
            AuthErrorCode::PortBusy
        } else {
            AuthErrorCode::AuthNetwork
        }
    })?;
    listener
        .set_nonblocking(true)
        .map_err(|_| AuthErrorCode::AuthNetwork)?;
    let verifier = format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    );
    let state = format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    );
    let challenge = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .encode(Sha256::digest(verifier.as_bytes()));
    let mut url = url::Url::parse("https://auth.openai.com/oauth/authorize").expect("fixed URL");
    url.query_pairs_mut().extend_pairs([
        ("response_type", "code"),
        ("client_id", CLIENT_ID),
        ("redirect_uri", BROWSER_REDIRECT),
        ("scope", "openid profile email offline_access"),
        ("code_challenge", &challenge),
        ("code_challenge_method", "S256"),
        ("state", &state),
    ]);
    Ok((listener, Secret(state), Secret(verifier), url.to_string()))
}

pub(super) fn receive_browser(
    listener: TcpListener,
    state: &str,
    deadline: u64,
    monotonic_deadline: Instant,
    clock: &dyn Clock,
    cancel: &AtomicBool,
    mut observe: impl FnMut() -> Result<(), AuthErrorCode>,
) -> Result<String, AuthErrorCode> {
    loop {
        if cancel.load(Ordering::Acquire) {
            return Err(AuthErrorCode::Cancelled);
        }
        if clock.now() >= deadline || Instant::now() >= monotonic_deadline {
            return Err(AuthErrorCode::LoginExpired);
        }
        observe()?;
        match listener.accept() {
            Ok((mut stream, _)) => {
                let outcome = read_callback(
                    &mut stream,
                    state,
                    deadline,
                    monotonic_deadline,
                    clock,
                    cancel,
                    &mut observe,
                );
                let body = if outcome.is_ok() {
                    "Authorization received. Return to Bao."
                } else {
                    "Authorization could not be completed. Return to Bao."
                };
                let response = format!("HTTP/1.1 {}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Security-Policy: default-src 'none'\r\nCache-Control: no-store\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}", if outcome.is_ok() { "200 OK" } else { "400 Bad Request" }, body.len(), body);
                let _ = stream.set_write_timeout(Some(Duration::from_millis(200)));
                let _ = stream.write_all(response.as_bytes());
                if outcome == Err(AuthErrorCode::CallbackMismatch) {
                    continue;
                }
                return outcome;
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                clock.wait(Duration::from_millis(25), cancel);
            }
            Err(_) => return Err(AuthErrorCode::AuthNetwork),
        }
    }
}

fn read_callback(
    stream: &mut TcpStream,
    state: &str,
    deadline: u64,
    monotonic_deadline: Instant,
    clock: &dyn Clock,
    cancel: &AtomicBool,
    observe: &mut impl FnMut() -> Result<(), AuthErrorCode>,
) -> Result<String, AuthErrorCode> {
    stream
        .set_read_timeout(Some(Duration::from_millis(100)))
        .map_err(|_| AuthErrorCode::AuthNetwork)?;
    let mut bytes = Vec::new();
    let read_deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if cancel.load(Ordering::Acquire) {
            return Err(AuthErrorCode::Cancelled);
        }
        if clock.now() >= deadline || Instant::now() >= monotonic_deadline {
            return Err(AuthErrorCode::LoginExpired);
        }
        observe()?;
        if bytes.len() >= 8192 || Instant::now() >= read_deadline {
            return Err(AuthErrorCode::CallbackMismatch);
        }
        let mut buf = [0; 512];
        match stream.read(&mut buf) {
            Ok(0) => return Err(AuthErrorCode::CallbackMismatch),
            Ok(n) => bytes.extend_from_slice(&buf[..n]),
            Err(e)
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                continue
            }
            Err(_) => return Err(AuthErrorCode::CallbackMismatch),
        }
        if bytes.windows(4).any(|w| w == b"\r\n\r\n") {
            break;
        }
    }
    parse_callback(
        std::str::from_utf8(&bytes).map_err(|_| AuthErrorCode::CallbackMismatch)?,
        state,
    )
}

pub(super) fn parse_callback(request: &str, state: &str) -> Result<String, AuthErrorCode> {
    let mismatch = AuthErrorCode::CallbackMismatch;
    let mut lines = request.split("\r\n");
    let parts: Vec<_> = lines.next().ok_or(mismatch)?.split(' ').collect();
    if parts.len() != 3
        || parts[0] != "GET"
        || parts[2] != "HTTP/1.1"
        || !parts[1].starts_with("/auth/callback?")
    {
        return Err(mismatch);
    }
    let host: Vec<_> = lines
        .filter_map(|line| line.split_once(':'))
        .filter(|(key, _)| key.eq_ignore_ascii_case("host"))
        .collect();
    if host.len() != 1 || host[0].1.trim() != "localhost:1455" {
        return Err(mismatch);
    }
    let query = parts[1].strip_prefix("/auth/callback?").ok_or(mismatch)?;
    if query.contains('#') {
        return Err(mismatch);
    }
    let mut params = std::collections::BTreeMap::new();
    for (key, value) in url::form_urlencoded::parse(query.as_bytes()) {
        if params
            .insert(key.into_owned(), value.into_owned())
            .is_some()
        {
            return Err(mismatch);
        }
    }
    if params.get("state").map(String::as_str) != Some(state) {
        return Err(mismatch);
    }
    if params.contains_key("error") {
        return Err(AuthErrorCode::ConsentDenied);
    }
    params
        .remove("code")
        .filter(|s| token_text(s))
        .ok_or(mismatch)
}
