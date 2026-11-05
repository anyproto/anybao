//! Fixed Responses inference boundary (docs/chatgpt-local-oauth.md §6–7).
use super::*;
use crate::chatgpt::{
    AuthErrorCode, ChatGptAuth, CompletionStatus, DispatchCredential, DispatchStamp,
};
use crate::local_grant::CHATGPT_REF;
use reqwest::header::{HeaderMap, HeaderValue};
use std::sync::atomic::Ordering;
use std::time::Duration;

pub(crate) const ENDPOINT: &str = "https://chatgpt.com/backend-api/codex/responses";
const MAX_BYTES: usize = 32 * 1024 * 1024;

/// Owned by the innermost llm.chat span, not by an individual http.post retry.
/// Custody identities stay in host memory and disappear when that call ends.
pub(super) struct CallState {
    original: Option<DispatchStamp>,
    recovered: bool,
    attempts: usize,
    deadline: Instant,
}
impl CallState {
    fn new(deadline: Instant) -> Self {
        Self {
            original: None,
            recovered: false,
            attempts: 0,
            deadline,
        }
    }
}

fn failure(code: &str) -> EffectFailure {
    EffectFailure {
        type_: code.into(),
        message: format!("ChatGPT: {code}"),
    }
}
fn invalid() -> EffectFailure {
    failure("chatgpt_request_invalid")
}

fn keys(value: &Value, allowed: &[&str]) -> Result<(), EffectFailure> {
    let obj = value.as_object().ok_or_else(invalid)?;
    if obj.keys().any(|key| !allowed.contains(&key.as_str())) {
        return Err(invalid());
    }
    Ok(())
}
fn string(value: &Value, max: usize) -> Result<&str, EffectFailure> {
    value
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= max)
        .ok_or_else(invalid)
}
fn ident(value: &Value) -> Result<&str, EffectFailure> {
    string(value, 512).and_then(|s| {
        if s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-./:".contains(&b))
        {
            Ok(s)
        } else {
            Err(invalid())
        }
    })
}
fn enumeration(value: &Value, values: &[&str]) -> Result<(), EffectFailure> {
    if value.as_str().is_some_and(|v| values.contains(&v)) {
        Ok(())
    } else {
        Err(invalid())
    }
}
fn image(value: &Value) -> Result<(), EffectFailure> {
    let s = string(value, MAX_BYTES)?;
    let (prefix, data) = s.split_once(";base64,").ok_or_else(invalid)?;
    if ![
        "data:image/png",
        "data:image/jpeg",
        "data:image/webp",
        "data:image/gif",
    ]
    .contains(&prefix)
    {
        return Err(invalid());
    }
    use base64::Engine as _;
    if base64::engine::general_purpose::STANDARD
        .decode(data)
        .map_err(|_| invalid())?
        .is_empty()
    {
        return Err(invalid());
    }
    Ok(())
}
fn body_policy(body: &Value) -> Result<(), EffectFailure> {
    keys(
        body,
        &[
            "model",
            "instructions",
            "input",
            "tools",
            "store",
            "stream",
            "include",
            "reasoning",
            "text",
        ],
    )?;
    ident(&body["model"])?;
    if !body["instructions"].is_string()
        || body["store"] != false
        || body["stream"] != true
        || body["include"] != json!(["reasoning.encrypted_content"])
    {
        return Err(invalid());
    }
    for (field, sub, values) in [
        (
            "reasoning",
            "effort",
            &["none", "minimal", "low", "medium", "high", "xhigh"][..],
        ),
        ("text", "verbosity", &["low", "medium", "high"][..]),
    ] {
        if let Some(v) = body.get(field) {
            keys(v, &[sub])?;
            enumeration(&v[sub], values)?;
        }
    }
    let tools = body["tools"].as_array().ok_or_else(invalid)?;
    if tools.len() > 128 {
        return Err(invalid());
    }
    let mut names = BTreeSet::new();
    for tool in tools {
        keys(tool, &["type", "name", "description", "parameters"])?;
        if tool["type"] != "function"
            || !tool["description"].is_string()
            || !tool["parameters"].is_object()
            || tool["parameters"]["type"] != "object"
            || !names.insert(ident(&tool["name"])?)
        {
            return Err(invalid());
        }
    }
    let input = body["input"].as_array().ok_or_else(invalid)?;
    if input.len() > 16384 {
        return Err(invalid());
    }
    let mut calls = BTreeSet::new();
    let mut results = BTreeSet::new();
    for item in input {
        match item["type"].as_str() {
            Some("message") => {
                keys(item, &["type", "role", "content", "phase"])?;
                enumeration(&item["role"], &["user", "assistant"])?;
                let assistant = item["role"] == "assistant";
                if let Some(phase) = item.get("phase") {
                    if !assistant {
                        return Err(invalid());
                    }
                    enumeration(phase, &["commentary", "final_answer"])?;
                }
                let content = item["content"]
                    .as_array()
                    .filter(|c| !c.is_empty())
                    .ok_or_else(invalid)?;
                for part in content {
                    match part["type"].as_str() {
                        Some("input_text") if !assistant => {
                            keys(part, &["type", "text"])?;
                            if !part["text"].is_string() {
                                return Err(invalid());
                            }
                        }
                        Some("output_text") if assistant => {
                            keys(part, &["type", "text"])?;
                            if !part["text"].is_string() {
                                return Err(invalid());
                            }
                        }
                        Some("input_image") if !assistant => {
                            keys(part, &["type", "image_url"])?;
                            image(&part["image_url"])?;
                        }
                        _ => return Err(invalid()),
                    }
                }
            }
            Some("function_call") => {
                keys(item, &["type", "call_id", "name", "arguments"])?;
                let id = ident(&item["call_id"])?;
                ident(&item["name"])?;
                if !calls.insert(id)
                    || !serde_json::from_str::<Value>(string(&item["arguments"], MAX_BYTES)?)
                        .map_err(|_| invalid())?
                        .is_object()
                {
                    return Err(invalid());
                }
            }
            Some("function_call_output") => {
                keys(item, &["type", "call_id", "output"])?;
                let id = ident(&item["call_id"])?;
                if !calls.contains(id) || !results.insert(id) || !item["output"].is_string() {
                    return Err(invalid());
                }
            }
            Some("reasoning") => {
                keys(item, &["type", "summary", "encrypted_content"])?;
                if item["summary"] != json!([]) {
                    return Err(invalid());
                }
                string(&item["encrypted_content"], MAX_BYTES)?;
            }
            _ => return Err(invalid()),
        }
    }
    if calls != results {
        return Err(invalid());
    }
    Ok(())
}

fn request_policy(name: &str, payload: &Value) -> Result<(), EffectFailure> {
    keys(
        payload,
        &[
            "url",
            "headers",
            "json",
            "stream",
            "timeout",
            "credential",
            "redirects",
        ],
    )?;
    if name != "http.post"
        || payload["credential"] != json!({"ref": CHATGPT_REF})
        || payload["stream"] != true
        || payload.get("redirects").is_some_and(|v| v != 0)
    {
        return Err(invalid());
    }
    let raw = payload["url"].as_str().ok_or_else(invalid)?;
    // Raw equality deliberately precedes parsing: URL parsers erase dot segments.
    if raw != ENDPOINT && raw != "https://chatgpt.com:443/backend-api/codex/responses" {
        return Err(invalid());
    }
    let url = url::Url::parse(raw).map_err(|_| invalid())?;
    if url.scheme() != "https"
        || url.host_str() != Some("chatgpt.com")
        || url.port_or_known_default() != Some(443)
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != "/backend-api/codex/responses"
    {
        return Err(invalid());
    }
    let headers = payload["headers"].as_object().ok_or_else(invalid)?;
    let mut seen = BTreeSet::new();
    for (key, value) in headers {
        let key = key.to_ascii_lowercase();
        if !seen.insert(key.clone()) {
            return Err(invalid());
        }
        let expected = match key.as_str() {
            "content-type" => "application/json",
            "accept" => "text/event-stream",
            _ => return Err(invalid()),
        };
        if value.as_str() != Some(expected) {
            return Err(invalid());
        }
    }
    Ok(())
}

fn sensitive(value: &str) -> Result<HeaderValue, EffectFailure> {
    if value.is_empty() || value.len() > 32768 || value.bytes().any(|b| b < 0x20 || b == 0x7f) {
        return Err(failure("unsupported_token_shape"));
    }
    let mut header =
        HeaderValue::from_str(value).map_err(|_| failure("unsupported_token_shape"))?;
    header.set_sensitive(true);
    Ok(header)
}
fn headers(credential: &DispatchCredential) -> Result<HeaderMap, EffectFailure> {
    let mut out = HeaderMap::new();
    out.insert(
        "authorization",
        sensitive(&format!("Bearer {}", credential.access.0))?,
    );
    out.insert("chatgpt-account-id", sensitive(&credential.account_id)?);
    if let Some(value) = &credential.residency {
        out.insert("x-openai-internal-codex-residency", sensitive(value)?);
    }
    out.insert("content-type", HeaderValue::from_static("application/json"));
    out.insert("accept", HeaderValue::from_static("text/event-stream"));
    out.insert("originator", HeaderValue::from_static("anybao"));
    out.insert(
        "user-agent",
        HeaderValue::from_static(concat!("anybao/", env!("CARGO_PKG_VERSION"))),
    );
    Ok(out)
}

pub(crate) struct WireReply {
    pub(crate) status: u16,
    pub(crate) body: String,
    pub(crate) retry_after: Option<u64>,
    pub(crate) streaming: bool,
}
pub(crate) trait Transport: Send + Sync {
    fn post(
        &self,
        body: &str,
        headers: HeaderMap,
        control: &Control<'_>,
        idle: Duration,
    ) -> Result<WireReply, EffectFailure>;
}
pub(crate) struct Control<'a> {
    auth: &'a ChatGptAuth,
    stamp: &'a DispatchStamp,
    cancel: &'a AtomicBool,
    deadline: Instant,
}
impl Control<'_> {
    pub(crate) fn check(&self) -> Result<(), EffectFailure> {
        if self.cancel.load(Ordering::Acquire) {
            return Err(failure("cancelled"));
        }
        if Instant::now() >= self.deadline {
            return Err(failure("request_expired"));
        }
        if !self.auth.dispatch_live(self.stamp) {
            return Err(failure("session_ended"));
        }
        Ok(())
    }
}
pub(crate) fn client_builder() -> reqwest::ClientBuilder {
    reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .https_only(true)
        .no_proxy()
        .connection_verbose(false)
        .retry(reqwest::retry::never())
        .http1_only()
}
struct HttpsTransport;
impl Transport for HttpsTransport {
    fn post(
        &self,
        body: &str,
        headers: HeaderMap,
        control: &Control<'_>,
        idle: Duration,
    ) -> Result<WireReply, EffectFailure> {
        control.check()?;
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|_| failure("chatgpt_transport_unavailable"))?;
        runtime.block_on(async {
            let client = client_builder()
                .connect_timeout(Duration::from_secs(30).min(idle))
                .build()
                .map_err(|_| failure("chatgpt_transport_unavailable"))?;
            let work = async {
                control.check()?;
                let request = client
                    .post(ENDPOINT)
                    .headers(headers)
                    .body(body.to_owned())
                    .send();
                let mut response = tokio::time::timeout(idle, request)
                    .await
                    .map_err(|_| failure("URLError"))?
                    .map_err(|_| failure("URLError"))?;
                let status = response.status().as_u16();
                let retry_after = response
                    .headers()
                    .get("retry-after")
                    .and_then(|v| v.to_str().ok())
                    .and_then(|v| v.parse::<u64>().ok())
                    .map(|v| v.min(60));
                let streaming = status == 200
                    || response
                        .headers()
                        .get("content-type")
                        .and_then(|v| v.to_str().ok())
                        .is_some_and(|v| v.starts_with("text/event-stream"));
                let mut bytes = Vec::new();
                loop {
                    control.check()?;
                    let chunk = tokio::time::timeout(idle, response.chunk())
                        .await
                        .map_err(|_| failure("URLError"))?
                        .map_err(|_| failure("URLError"))?;
                    let Some(chunk) = chunk else { break };
                    if bytes.len().saturating_add(chunk.len()) > MAX_BYTES {
                        return Err(failure("chatgpt_reply_too_large"));
                    }
                    bytes.extend_from_slice(&chunk);
                }
                let body = String::from_utf8(bytes).map_err(|_| failure("URLError"))?;
                Ok(WireReply {
                    status,
                    body,
                    retry_after,
                    streaming,
                })
            };
            let stopped = async {
                loop {
                    control.check()?;
                    tokio::time::sleep(Duration::from_millis(20)).await;
                }
            };
            tokio::select! { result = work => result, result = stopped => result }
        })
    }
}

fn provider_code(value: &Value, status: u16) -> (&'static str, u16, bool) {
    let error = value.get("error").unwrap_or(value);
    let code = error["code"]
        .as_str()
        .or_else(|| error["type"].as_str())
        .unwrap_or("");
    match code {
        "token_expired" | "access_token_expired" | "invalid_access_token" | "invalid_token"
            if status == 401 =>
        {
            ("access_token_rejected", 401, true)
        }
        "insufficient_quota" | "usage_limit_reached" | "quota_exceeded" => {
            ("quota_exhausted", 402, false)
        }
        "rate_limit_exceeded" | "rate_limit_error" => ("rate_limited", 429, false),
        "residency_mismatch" | "unsupported_country_region_territory" => {
            ("residency_denied", 403, false)
        }
        "permission_denied" | "policy_violation" | "content_policy_violation" => {
            ("policy_denied", 403, false)
        }
        "server_error" | "overloaded_error" => ("provider_unavailable", 502, false),
        "model_not_found" => ("model_unavailable", 404, false),
        _ if status == 401 => ("authentication_failed", 401, false),
        _ if status == 403 => ("policy_denied", 403, false),
        _ if status == 429 => ("rate_limited", 429, false),
        _ if status >= 500 => ("provider_unavailable", 502, false),
        _ => ("provider_rejected", 400, false),
    }
}
fn safe_error(code: &str, status: u16, retry_after: Option<u64>) -> Value {
    let headers = retry_after
        .map(|v| json!({"retry-after": v.min(60).to_string()}))
        .unwrap_or(json!({}));
    json!({"url": ENDPOINT, "status": status, "headers": headers,
        "body": json!({"error": {"code": code, "message": format!("ChatGPT: {code}")}}).to_string(), "billingSource": "chatgpt-subscription"})
}
fn contains_custody(value: &Value, credential: &DispatchCredential) -> bool {
    let contains = |s: &str| s.contains(&credential.access.0) || s.contains(&credential.account_id);
    match value {
        Value::String(s) => contains(s),
        Value::Array(a) => a.iter().any(|v| contains_custody(v, credential)),
        Value::Object(m) => m
            .iter()
            .any(|(k, v)| contains(k) || contains_custody(v, credential)),
        _ => false,
    }
}

// Preserve no ambiguous raw JSON: Value's default map visitor silently drops
// earlier duplicate keys, which could otherwise conceal reflected credentials.
struct StrictJson(Value);
impl<'de> serde::Deserialize<'de> for StrictJson {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct Visitor;
        impl<'de> serde::de::Visitor<'de> for Visitor {
            type Value = StrictJson;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("unambiguous JSON")
            }
            fn visit_bool<E: serde::de::Error>(self, v: bool) -> Result<Self::Value, E> {
                Ok(StrictJson(v.into()))
            }
            fn visit_i64<E: serde::de::Error>(self, v: i64) -> Result<Self::Value, E> {
                Ok(StrictJson(v.into()))
            }
            fn visit_u64<E: serde::de::Error>(self, v: u64) -> Result<Self::Value, E> {
                Ok(StrictJson(v.into()))
            }
            fn visit_f64<E: serde::de::Error>(self, v: f64) -> Result<Self::Value, E> {
                serde_json::Number::from_f64(v)
                    .map(|v| StrictJson(Value::Number(v)))
                    .ok_or_else(|| E::custom("invalid number"))
            }
            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Self::Value, E> {
                Ok(StrictJson(v.into()))
            }
            fn visit_string<E: serde::de::Error>(self, v: String) -> Result<Self::Value, E> {
                Ok(StrictJson(v.into()))
            }
            fn visit_unit<E: serde::de::Error>(self) -> Result<Self::Value, E> {
                Ok(StrictJson(Value::Null))
            }
            fn visit_seq<A: serde::de::SeqAccess<'de>>(
                self,
                mut a: A,
            ) -> Result<Self::Value, A::Error> {
                let mut out = Vec::new();
                while let Some(StrictJson(v)) = a.next_element()? {
                    out.push(v);
                }
                Ok(StrictJson(Value::Array(out)))
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(
                self,
                mut a: A,
            ) -> Result<Self::Value, A::Error> {
                let mut out = Map::new();
                while let Some(k) = a.next_key::<String>()? {
                    if out.contains_key(&k) {
                        return Err(serde::de::Error::custom("duplicate JSON key"));
                    }
                    let StrictJson(v) = a.next_value()?;
                    out.insert(k, v);
                }
                Ok(StrictJson(Value::Object(out)))
            }
        }
        d.deserialize_any(Visitor)
    }
}

fn parse_wire_json(raw: &str) -> Result<Value, EffectFailure> {
    serde_json::from_str::<StrictJson>(raw)
        .map(|v| v.0)
        .map_err(|e| {
            failure(if e.classify() == serde_json::error::Category::Data {
                "chatgpt_response_rejected"
            } else {
                "URLError"
            })
        })
}

fn inspect_event(event: &Value, credential: &DispatchCredential) -> Result<(), EffectFailure> {
    if contains_custody(event, credential) {
        return Err(failure("chatgpt_response_rejected"));
    }
    // The codec decodes function arguments as a second JSON layer. Inspect that
    // known wire shape too; encrypted reasoning remains an opaque string.
    let items = std::iter::once(&event["item"])
        .chain(event["response"]["output"].as_array().into_iter().flatten());
    let mut arguments: Vec<&str> = items
        .filter(|v| v["type"] == "function_call")
        .filter_map(|v| v["arguments"].as_str())
        .collect();
    if event["type"] == "response.function_call_arguments.done" {
        if let Some(s) = event["arguments"].as_str() {
            arguments.push(s);
        }
    }
    for s in arguments.into_iter().filter(|s| !s.is_empty()) {
        let v = parse_wire_json(s)?;
        if contains_custody(&v, credential) {
            return Err(failure("chatgpt_response_rejected"));
        }
    }
    Ok(())
}

struct ResponseFrame {
    name: Option<String>,
    event: Value,
}

fn response_frames(body: &str) -> Result<Vec<ResponseFrame>, EffectFailure> {
    if !body.ends_with("\n\n") && !body.ends_with("\r\n\r\n") {
        return Err(failure("URLError"));
    }
    let mut frames = Vec::new();
    let mut data = Vec::new();
    let mut name = None;
    for line in body.split('\n') {
        let line = line.trim_end_matches('\r');
        if let Some(s) = line.strip_prefix("data:") {
            data.push(s.trim());
        } else if let Some(s) = line.strip_prefix("event:") {
            name = Some(s.trim().to_owned());
        }
        if line.is_empty() {
            if !data.is_empty() {
                if frames.len() >= 100000 {
                    return Err(failure("URLError"));
                }
                let event = parse_wire_json(&data.join("\n"))?;
                let kind = event["type"].as_str().ok_or_else(|| failure("URLError"))?;
                if name.as_deref().is_some_and(|name| name != kind) {
                    return Err(failure("URLError"));
                }
                frames.push(ResponseFrame {
                    name: name.take(),
                    event,
                });
                data.clear();
            }
            name = None;
        }
    }
    if frames.is_empty() {
        return Err(failure("URLError"));
    }
    Ok(frames)
}

type FieldKey = (u64, Option<u64>, &'static str);

/// Mirror the codec's field identities and accumulation before any write. The
/// retained fields are inspected once after assembly, not on growing prefixes.
#[derive(Default)]
struct ResponseFields {
    items: BTreeMap<u64, (String, String)>,
    values: BTreeMap<FieldKey, String>,
    done: BTreeSet<FieldKey>,
    bytes: usize,
}
impl ResponseFields {
    fn observe(&mut self, e: &Value) -> Result<(), EffectFailure> {
        let bad = || failure("URLError");
        let kind = e["type"].as_str().ok_or_else(bad)?;
        if kind == "response.output_item.added" {
            let index = e["output_index"]
                .as_u64()
                .filter(|i| *i < 4096)
                .ok_or_else(bad)?;
            let id = e["item"]["id"]
                .as_str()
                .filter(|s| !s.is_empty())
                .ok_or_else(bad)?;
            let item_type = e["item"]["type"].as_str().ok_or_else(bad)?;
            if index != self.items.len() as u64 || self.items.values().any(|(old, _)| old == id) {
                return Err(bad());
            }
            self.items
                .insert(index, (id.to_owned(), item_type.to_owned()));
        }
        if kind == "response.output_item.done" {
            if let Some(i) = e["output_index"].as_u64() {
                if let Some(value) = self.values.get(&(i, None, "arguments")) {
                    // Normalization must not repair a field/item mismatch.
                    if e["item"]["arguments"].as_str() != Some(value) {
                        return Err(bad());
                    }
                }
            }
        }
        let Some((prefix, suffix)) = kind.rsplit_once('.') else {
            return Ok(());
        };
        let field = match prefix {
            "response.output_text" => "text",
            "response.refusal" => "refusal",
            "response.function_call_arguments" => "arguments",
            "response.reasoning_summary_text" => "summary_text",
            _ => return Ok(()),
        };
        let i = e["output_index"].as_u64().ok_or_else(bad)?;
        let (id, item_type) = self.items.get(&i).ok_or_else(bad)?;
        let expected = match field {
            "arguments" => "function_call",
            "summary_text" => "reasoning",
            _ => "message",
        };
        if e["item_id"].as_str() != Some(id) || item_type != expected {
            return Err(bad());
        }
        let ci = if field == "arguments" {
            None
        } else {
            Some(
                e[if field == "summary_text" {
                    "summary_index"
                } else {
                    "content_index"
                }]
                .as_u64()
                .filter(|i| *i < 4096)
                .ok_or_else(bad)?,
            )
        };
        let key = (i, ci, field);
        if self.done.contains(&key) {
            return Err(bad());
        }
        let value = match suffix {
            "delta" => e["delta"].as_str().ok_or_else(bad)?,
            "done" => e[if field == "summary_text" {
                "text"
            } else {
                field
            }]
            .as_str()
            .ok_or_else(bad)?,
            _ => return Err(bad()),
        };
        if suffix == "done" {
            self.done.insert(key);
            if let Some(old) = self.values.get(&key) {
                return if old == value { Ok(()) } else { Err(bad()) };
            }
        }
        self.bytes = self
            .bytes
            .checked_add(value.len())
            .filter(|n| *n <= MAX_BYTES)
            .ok_or_else(bad)?;
        self.values.entry(key).or_default().push_str(value);
        Ok(())
    }

    fn inspect(&self, limited: bool, credential: &DispatchCredential) -> Result<(), EffectFailure> {
        for ((_, _, field), value) in &self.values {
            if *field == "arguments" && limited {
                continue;
            } // discarded before any writer
            let value = if *field == "arguments" && !value.is_empty() {
                parse_wire_json(value)?
            } else {
                Value::String(value.clone())
            };
            if contains_custody(&value, credential) {
                return Err(failure("chatgpt_response_rejected"));
            }
        }
        Ok(())
    }
}

fn clear_partial_arguments(e: &mut Value) {
    if e["type"] == "response.function_call_arguments.delta" {
        e["delta"] = json!("");
    }
    if e["type"] == "response.function_call_arguments.done" {
        e["arguments"] = json!("");
    }
    let clear = |item: &mut Value| {
        if item["type"] == "function_call" {
            if let Some(arguments) = item.get_mut("arguments") {
                *arguments = json!("");
            }
        }
    };
    if let Some(item) = e.get_mut("item") {
        clear(item);
    }
    if let Some(items) = e
        .get_mut("response")
        .and_then(|r| r.get_mut("output"))
        .and_then(Value::as_array_mut)
    {
        for item in items {
            clear(item);
        }
    }
}

fn sanitize(
    reply: WireReply,
    credential: &DispatchCredential,
) -> Result<(Value, bool), EffectFailure> {
    if reply.body.len() > MAX_BYTES {
        return Err(failure("chatgpt_reply_too_large"));
    }
    if reply.status != 200 {
        let value = serde_json::from_str::<Value>(&reply.body).unwrap_or(Value::Null);
        let (code, status, reject) = provider_code(&value, reply.status);
        return Ok((
            safe_error(code, status, reply.retry_after),
            reject && !reply.streaming,
        ));
    }
    let mut frames = response_frames(&reply.body)?;
    // No raw provider error object, reflected headers or unparseable body reaches a writer.
    for frame in &frames {
        let event = &frame.event;
        let kind = event["type"].as_str().unwrap();
        if kind == "error"
            || event.get("error").is_some_and(|e| !e.is_null())
            || event["response"].get("error").is_some_and(|e| !e.is_null())
            || kind == "response.failed"
            || (kind == "response.incomplete"
                && event["response"]["incomplete_details"]["reason"] != "max_output_tokens")
        {
            let value = if kind == "error" {
                event
            } else {
                &event["response"]
            };
            let (code, status, _) = provider_code(value, 400);
            return Ok((safe_error(code, status, reply.retry_after), false));
        }
    }
    let last = &frames.last().unwrap().event;
    let limited = last["type"] == "response.incomplete"
        && last["response"]["status"] == "incomplete"
        && last["response"]["incomplete_details"]["reason"] == "max_output_tokens"
        && frames
            .iter()
            .filter(|f| {
                matches!(
                    f.event["type"].as_str(),
                    Some("response.completed" | "response.failed" | "response.incomplete")
                )
            })
            .count()
            == 1;
    let mut fields = ResponseFields::default();
    for frame in &frames {
        fields.observe(&frame.event)?;
    }
    fields.inspect(limited, credential)?;
    for frame in &mut frames {
        // Limit output has no executable calls. Removing all argument carriage
        // consistently preserves the codec's equalities, text, usage and stop.
        if limited {
            clear_partial_arguments(&mut frame.event);
        }
        inspect_event(&frame.event, credential)?;
    }
    let body = if limited {
        let mut body = String::new();
        for frame in frames {
            if let Some(name) = frame.name {
                body.push_str(&format!("event: {name}\n"));
            }
            body.push_str(&format!("data: {}\n\n", frame.event));
        }
        if body.len() > MAX_BYTES {
            return Err(failure("chatgpt_reply_too_large"));
        }
        body
    } else {
        reply.body
    };
    if body.contains(&credential.access.0) || body.contains(&credential.account_id) {
        return Err(failure("chatgpt_response_rejected"));
    }
    Ok((
        json!({"url": ENDPOINT, "status": 200, "headers": {"content-type": "text/event-stream"}, "body": body, "billingSource": "chatgpt-subscription"}),
        false,
    ))
}

impl Broker {
    fn chatgpt_record(
        &mut self,
        name: &str,
        input: Value,
        result: &Result<Value, EffectFailure>,
        start: Instant,
    ) {
        self.bump("read");
        let key = input_key(name, &input);
        let span = self.span_stack.last().map(|s| s.id.clone());
        let (output, error) = match result {
            Ok(v) => (Some(v.clone()), None),
            Err(e) => (None, Some(json!({"type": e.type_, "message": e.message}))),
        };
        self.writer.effect(name, self.current_cell.as_deref(), input, &key, output, error,
            json!({"hosted": true, "class": "read", "mocked": false, "durMs": start.elapsed().as_millis() as u64,
                "credential": {"ref": CHATGPT_REF}, "billingSource": "chatgpt-subscription"}), span.as_deref());
    }

    fn chatgpt_auth_record(&mut self, phase: &str, attempt: usize, outcome: &str, start: Instant) {
        self.chatgpt_record(
            "chatgpt.auth",
            json!({"provider": "openai-codex", "phase": phase, "attempt": attempt}),
            &Ok(json!({"outcome": outcome})),
            start,
        );
    }

    fn chatgpt_completion(
        &self,
        auth: &ChatGptAuth,
        stamp: &DispatchStamp,
        deadline: Instant,
    ) -> Result<(), EffectFailure> {
        loop {
            match auth.check_completion(stamp, &self.interrupt, deadline) {
                Ok(CompletionStatus::Accepted) => return Ok(()),
                Ok(CompletionStatus::Invalidated) => return Err(failure("account_changed")),
                Err(e) if e.code == AuthErrorCode::AuthBusy => std::thread::sleep(
                    Duration::from_millis(10)
                        .min(deadline.saturating_duration_since(Instant::now())),
                ),
                Err(e) => return Err(failure(e.code.as_str())),
            }
        }
    }

    pub(super) fn sys_chatgpt(
        &mut self,
        name: &str,
        payload: &Value,
    ) -> Result<Value, EffectFailure> {
        let start = Instant::now();
        request_policy(name, payload)?;
        let (total, idle) = http_timeouts(payload, true)?;
        let deadline = self
            .deadline
            .map_or(start + total, |d| d.min(start + total));
        let body = self.expand_refs(&payload["json"])?;
        body_policy(&body)?;
        let body = crate::trace::canonical_json(&body);
        if body.len() > MAX_BYTES {
            return Err(invalid());
        }
        let owner = self.span_stack.iter().rposition(|s| s.name == "llm.chat");
        let mut call = owner
            .and_then(|i| self.span_stack[i].chatgpt.take())
            .unwrap_or_else(|| CallState::new(deadline));
        call.deadline = call.deadline.min(deadline);
        let result = self.chatgpt_dispatch(payload, &body, idle, &mut call);
        if let Some(i) = owner {
            self.span_stack[i].chatgpt = Some(call);
        }
        result
    }

    fn chatgpt_dispatch(
        &mut self,
        payload: &Value,
        body: &str,
        idle: Duration,
        call: &mut CallState,
    ) -> Result<Value, EffectFailure> {
        let deadline = call.deadline;
        let auth = self
            .chatgpt_auth
            .clone()
            .ok_or_else(|| failure("chatgpt_inference_unavailable"))?;
        #[cfg(test)]
        let transport = self
            .chatgpt_transport
            .clone()
            .unwrap_or_else(|| Arc::new(HttpsTransport));
        #[cfg(not(test))]
        let transport = HttpsTransport;
        let mut rejected = None;
        loop {
            let attempt = call.attempts + 1;
            let authorize_start = Instant::now();
            let credential = loop {
                match auth.authorize_dispatch_until(
                    call.original.as_ref(),
                    rejected.as_ref(),
                    &self.interrupt,
                    deadline,
                ) {
                    Ok(value) => break value,
                    Err(e) if e.code == AuthErrorCode::AuthBusy && Instant::now() < deadline => {
                        continue
                    }
                    Err(e) => {
                        self.chatgpt_auth_record(
                            "authorize",
                            attempt,
                            e.code.as_str(),
                            authorize_start,
                        );
                        return Err(failure(e.code.as_str()));
                    }
                }
            };
            call.original
                .get_or_insert_with(|| credential.stamp.clone());
            self.chatgpt_auth_record("authorize", attempt, credential.outcome, authorize_start);
            self.chatgpt_completion(&auth, &credential.stamp, deadline)?;
            let control = Control {
                auth: &auth,
                stamp: &credential.stamp,
                cancel: &self.interrupt,
                deadline,
            };
            let headers = headers(&credential)?;
            control.check()?;
            let sent_at = Instant::now();
            call.attempts += 1;
            let result = transport
                .post(body, headers, &control, idle)
                .and_then(|reply| sanitize(reply, &credential));
            let mut reject = false;
            let result = result.and_then(|(out, retry)| {
                reject = retry;
                self.chatgpt_completion(&auth, &credential.stamp, deadline)?;
                Ok(out)
            });
            self.chatgpt_record(
                "chatgpt.inference",
                json!({"provider": "openai-codex", "attempt": attempt, "request": payload}),
                &result,
                sent_at,
            );
            let output = result?;
            if !reject {
                return Ok(output);
            }
            if call.recovered {
                let start = Instant::now();
                loop {
                    match auth.reject_dispatch(&credential.stamp, &self.interrupt, deadline) {
                        Ok(()) => break,
                        Err(e)
                            if e.code == AuthErrorCode::AuthBusy && Instant::now() < deadline =>
                        {
                            continue
                        }
                        Err(e) => {
                            self.chatgpt_auth_record("reject", attempt, e.code.as_str(), start);
                            return Err(failure(e.code.as_str()));
                        }
                    }
                }
                self.chatgpt_auth_record("reject", attempt, "grant_rejected", start);
                return Ok(safe_error("grant_rejected", 401, None));
            }
            call.recovered = true;
            rejected = Some(credential.stamp);
        }
    }
}
