//! Android guest HTTP policy, separate from replay classification (ADR-034 §5).

use crate::broker::EffectFailure;
use std::net::{IpAddr, Ipv4Addr, SocketAddr, ToSocketAddrs};
use std::sync::Arc;
use url::{Host, Url};

const DATA_ROUTES: &str = include_str!("../fixtures/android-guest-data-routes.txt");

fn denied(message: &str) -> EffectFailure {
    EffectFailure {
        type_: "egress_denied".into(),
        message: message.into(),
    }
}

/// Bound to one host-issued lease. Construct a new policy when that lease changes.
#[derive(Clone)]
pub struct AndroidPocEgress {
    origin: Url,
    resolver: Arc<dyn ureq::Resolver>,
}

/// Pure readiness result. DNS must be supplied by the readiness caller and is
/// checked again at every live request (ADR-034 §2/§5).
#[derive(Debug)]
pub struct PublicEndpoint {
    url: Url,
}

impl PublicEndpoint {
    pub fn url(&self) -> &Url {
        &self.url
    }

    pub fn validate_addresses(&self, addresses: &[IpAddr]) -> Result<(), EffectFailure> {
        if addresses.is_empty() || addresses.iter().any(|ip| !public_ip(*ip)) {
            return Err(denied(
                "endpoint resolution contains a non-public address or no addresses",
            ));
        }
        if let Some(ip) = literal_ip(&self.url) {
            if addresses.iter().any(|address| *address != ip) {
                return Err(denied(
                    "endpoint resolution does not match the literal address",
                ));
            }
        }
        Ok(())
    }
}

/// No DNS, credential reads or network. A hostname also needs validate_addresses
/// before activation; a successful syntax check alone is not readiness.
pub fn validate_public_endpoint(raw: &str) -> Result<PublicEndpoint, EffectFailure> {
    let url = parse_url(raw)?;
    if url.scheme() != "https" {
        return Err(denied("public guest endpoints require HTTPS"));
    }
    if let Some(ip) = literal_ip(&url) {
        if !public_ip(ip) {
            return Err(denied("non-public endpoint address"));
        }
    } else {
        let host = url
            .host_str()
            .ok_or_else(|| denied("endpoint has no host"))?;
        let host = host.trim_end_matches('.');
        if !host.contains('.')
            || ["localhost", "local", "internal", "lan", "home", "home.arpa"]
                .iter()
                .any(|suffix| host == *suffix || host.ends_with(&format!(".{suffix}")))
        {
            return Err(denied("local endpoint hostname"));
        }
    }
    Ok(PublicEndpoint { url })
}

/// Pure credential-kind check for readiness, also applied before live injection.
pub fn validate_api_key_credential(reference: &str) -> Result<(), EffectFailure> {
    if reference.starts_with(crate::oauth::OAUTH_REF_PREFIX) {
        return Err(EffectFailure {
            type_: "UnsupportedCapability".into(),
            message: "Android PoC does not support managed OAuth credential injection or refresh"
                .into(),
        });
    }
    Ok(())
}

impl AndroidPocEgress {
    pub fn new(leased_any_origin: &str) -> Result<Self, EffectFailure> {
        let origin = parse_url(leased_any_origin)?;
        if origin.scheme() != "http"
            || origin.path() != "/"
            || origin.query().is_some()
            || !literal_ip(&origin).is_some_and(|ip| ip.is_loopback())
            || origin.port_or_known_default() == Some(0)
        {
            return Err(denied("Any lease must name a literal HTTP loopback origin"));
        }
        Ok(Self {
            origin,
            resolver: Arc::new(|netloc: &str| netloc.to_socket_addrs().map(Iterator::collect)),
        })
    }

    fn check(&self, method: &str, raw: &str) -> Result<Url, EffectFailure> {
        if !matches!(
            method,
            "GET" | "HEAD" | "POST" | "PUT" | "PATCH" | "DELETE" | "OPTIONS"
        ) {
            return Err(denied("unsupported guest HTTP method"));
        }
        let url = parse_url(raw)?;
        if url.origin() == self.origin.origin() {
            if !data_route(method, url.path()) {
                return Err(denied("Any route is outside the guest data allowlist"));
            }
        } else {
            validate_public_endpoint(raw)?;
        }
        Ok(url)
    }

    pub(crate) fn resolve(
        &self,
        method: &str,
        raw: &str,
    ) -> Result<PinnedDestination, EffectFailure> {
        let url = self.check(method, raw)?;
        let port = url
            .port_or_known_default()
            .ok_or_else(|| denied("missing destination port"))?;
        let netloc = format!("{}:{port}", url.host_str().unwrap());
        let addresses = if let Some(ip) = literal_ip(&url) {
            vec![SocketAddr::new(ip, port)]
        } else {
            self.resolver
                .resolve(&netloc)
                .map_err(|_| denied("guest DNS resolution failed"))?
        };
        if addresses.is_empty()
            || addresses.iter().any(|address| {
                address.port() != port
                    || if url.origin() == self.origin.origin() {
                        Some(address.ip()) != literal_ip(&self.origin)
                    } else {
                        !public_ip(address.ip())
                    }
            })
        {
            return Err(denied(
                "guest DNS resolution contains a non-public or mismatched address",
            ));
        }
        Ok(PinnedDestination {
            url,
            netloc,
            addresses,
        })
    }

    pub(crate) fn redirect(&self, current: &str, location: &str) -> Result<String, EffectFailure> {
        // Inspect the raw reference before join removes dot segments.
        validate_reference(location)?;
        let base = parse_url(current)?;
        if location.contains("://") {
            parse_url(location)?;
        } else if location.starts_with("//") {
            parse_url(&format!("{}:{location}", base.scheme()))?;
        } else if location
            .split(['/', '?'])
            .next()
            .unwrap_or("")
            .contains(':')
        {
            return Err(denied("ambiguous redirect scheme"));
        }
        let next = base
            .join(location)
            .map_err(|_| denied("invalid redirect URL"))?;
        parse_url(next.as_str())?;
        Ok(next.into())
    }

    #[cfg(test)]
    pub(crate) fn with_resolver(mut self, resolver: impl ureq::Resolver + 'static) -> Self {
        self.resolver = Arc::new(resolver);
        self
    }
}

pub(crate) struct PinnedDestination {
    url: Url,
    netloc: String,
    addresses: Vec<SocketAddr>,
}

impl PinnedDestination {
    pub(crate) fn url(&self) -> &str {
        self.url.as_str()
    }

    pub(crate) fn agent(self, builder: ureq::AgentBuilder) -> ureq::Agent {
        builder
            .redirects(0)
            .try_proxy_from_env(false)
            .resolver(self)
            .build()
    }
}

impl ureq::Resolver for PinnedDestination {
    fn resolve(&self, netloc: &str) -> std::io::Result<Vec<SocketAddr>> {
        if netloc != self.netloc {
            return Err(std::io::Error::new(
                std::io::ErrorKind::PermissionDenied,
                "unpinned destination",
            ));
        }
        Ok(self.addresses.clone())
    }
}

pub(crate) fn validate_headers(payload: &serde_json::Value) -> Result<(), EffectFailure> {
    let forbidden = |name: &str| {
        matches!(
            name.to_ascii_lowercase().as_str(),
            "host"
                | "content-length"
                | "transfer-encoding"
                | "connection"
                | "upgrade"
                | "trailer"
                | "te"
                | "expect"
                | "proxy-authorization"
                | "proxy-connection"
                | "x-http-method-override"
                | "x-method-override"
        )
    };
    if payload
        .get("headers")
        .and_then(|v| v.as_object())
        .is_some_and(|headers| headers.keys().any(|name| forbidden(name)))
        || payload["credential"]["header"]
            .as_str()
            .is_some_and(forbidden)
    {
        return Err(denied(
            "guest headers cannot override destination, framing or method",
        ));
    }
    Ok(())
}

fn literal_ip(url: &Url) -> Option<IpAddr> {
    match url.host()? {
        Host::Ipv4(ip) => Some(ip.into()),
        Host::Ipv6(ip) => Some(ip.into()),
        _ => None,
    }
}

fn public_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => {
            let [a, b, c, _] = ip.octets();
            !(matches!(a, 0 | 10 | 127 | 224..=255)
                || (a == 100 && (64..=127).contains(&b))
                || (a == 169 && b == 254)
                || (a == 172 && (16..=31).contains(&b))
                || (a == 192
                    && (b == 168 || (b == 0 && matches!(c, 0 | 2)) || (b == 88 && c == 99)))
                || (a == 198 && (matches!(b, 18 | 19) || (b == 51 && c == 100)))
                || (a == 203 && b == 0 && c == 113)
                || ip == Ipv4Addr::new(168, 63, 129, 16))
        }
        IpAddr::V6(ip) => {
            if let Some(ip) = ip.to_ipv4_mapped() {
                return public_ip(ip.into());
            }
            let s = ip.segments();
            (s[0] & 0xe000) == 0x2000
                && !(s[0] == 0x2001 && (s[1] < 0x0200 || s[1] == 0x0db8))
                && s[0] != 0x2002
                && !(s[0] == 0x3fff && s[1] < 0x1000)
        }
    }
}

fn parse_url(raw: &str) -> Result<Url, EffectFailure> {
    validate_reference(raw)?;
    let (scheme, rest) = raw
        .split_once("://")
        .ok_or_else(|| denied("absolute HTTP URL required"))?;
    if !matches!(scheme, "http" | "https") {
        return Err(denied("unsupported URL scheme"));
    }
    let authority = rest.split(['/', '?']).next().unwrap();
    let url = Url::parse(raw).map_err(|_| denied("invalid HTTP URL"))?;
    if !url.username().is_empty()
        || url.password().is_some()
        || url.host().is_none()
        || url.port_or_known_default() == Some(0)
    {
        return Err(denied("URL userinfo, missing host or zero port"));
    }
    let normalized = &url[url::Position::BeforeHost..url::Position::AfterPort];
    // Numeric aliases, %-escaped hosts, userinfo and noncanonical IPv6 must not
    // turn into the privileged leased origin during WHATWG normalization.
    let default_port = if scheme == "https" { 443 } else { 80 };
    if authority != normalized && authority != format!("{normalized}:{default_port}") {
        return Err(denied("noncanonical URL authority"));
    }
    Ok(url)
}

fn validate_reference(raw: &str) -> Result<(), EffectFailure> {
    if raw.is_empty()
        || !raw.is_ascii()
        || raw.bytes().any(|b| b <= 0x20 || b == 0x7f)
        || raw.contains(['\\', '#'])
    {
        return Err(denied("ambiguous URL characters"));
    }
    let without_query = raw.split('?').next().unwrap();
    let path = if let Some((_, rest)) = without_query.split_once("://") {
        rest.find('/').map_or("", |i| &rest[i..])
    } else if let Some(rest) = without_query.strip_prefix("//") {
        rest.find('/').map_or("", |i| &rest[i..])
    } else {
        without_query
    };
    for segment in path.split('/') {
        let decoded = decode_segment(segment)?;
        if decoded == "." || decoded == ".." {
            return Err(denied("URL dot segments are not allowed"));
        }
    }
    Ok(())
}

fn decode_segment(segment: &str) -> Result<String, EffectFailure> {
    let bytes = segment.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            let hex = bytes
                .get(i + 1..i + 3)
                .ok_or_else(|| denied("invalid path escape"))?;
            if !hex
                .iter()
                .all(|b| b.is_ascii_digit() || (b'A'..=b'F').contains(b))
            {
                return Err(denied("noncanonical path escape"));
            }
            let value = u8::from_str_radix(std::str::from_utf8(hex).unwrap(), 16).unwrap();
            if value.is_ascii_alphanumeric() || matches!(value, b'-' | b'.' | b'_' | b'~') {
                return Err(denied("escaped unreserved path character"));
            }
            decoded.push(value);
            i += 3;
        } else {
            decoded.push(bytes[i]);
            i += 1;
        }
    }
    let decoded = String::from_utf8(decoded).map_err(|_| denied("invalid path UTF-8"))?;
    if decoded
        .chars()
        .any(|c| c.is_control() || matches!(c, '%' | '\\' | '?' | '#' | ';'))
    {
        return Err(denied("ambiguous encoded path"));
    }
    Ok(decoded)
}

fn data_route(method: &str, path: &str) -> bool {
    DATA_ROUTES
        .lines()
        .filter(|line| !line.starts_with('#'))
        .any(|line| {
            let Some((allowed_method, pattern)) = line.split_once(' ') else {
                return false;
            };
            if allowed_method != method {
                return false;
            }
            let actual: Vec<_> = path.split('/').collect();
            let expected: Vec<_> = pattern.split('/').collect();
            actual.len() == expected.len()
                && actual.iter().zip(expected).all(|(raw, part)| {
                    if !part.starts_with('{') {
                        return *raw == part;
                    }
                    let Ok(decoded) = decode_segment(raw) else {
                        return false;
                    };
                    !decoded.is_empty()
                        && if part == "{bundleId}" {
                            decoded
                                .split('/')
                                .all(|s| !s.is_empty() && s != "." && s != "..")
                        } else {
                            !decoded.contains('/')
                        }
                })
        })
}

#[cfg(test)]
mod tests;
