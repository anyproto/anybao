//! Guest-write allowlists for managed OAuth providers (ADR-034): a
//! descriptor's `guest_writes` lists the only non-`GET`/`HEAD`
//! requests a cell may send through the provider's handle. This module
//! is the pure half — matching, the named body checks, the preflight
//! verdicts. The broker owns the refusal, the preflight reads and the
//! redirect re-check (`broker.rs` `sys_http`).
//!
//! Every check is fail-closed (ADR-034 §3): approve only what parses
//! completely; anything Google might read differently is refused.

use base64::engine::{DecodePaddingMode, GeneralPurpose, GeneralPurposeConfig};
use base64::Engine as _;
use serde::Deserialize;
use serde_json::{Map, Value};
use std::collections::BTreeMap;

/// The typed failure of a refused write (ADR-034 §2).
pub const WRITE_REFUSED: &str = "credential_write_refused";

/// The header that marks a Gmail draft as Bao's (ADR-034 §4).
pub const DRAFT_MARKER: &str = "X-Anybao-Draft";

/// The event-id prefix of Bao's calendar holds (ADR-034 §5); base32hex.
pub const HOLD_PREFIX: &str = "baoagent";

/// Inner requests a Gmail batch may carry (ADR-034 §4).
const BATCH_GET_PREFIX: &str = "/gmail/v1/users/me/";

/// Body keys the calendar checks guard by exact spelling (ADR-034 §5).
const CALENDAR_GUARDED: [&str; 3] = ["id", "visibility", "attendees"];

/// base64url or base64, padding optional — what Google's decoder takes.
const LENIENT_B64: GeneralPurpose = GeneralPurpose::new(
    &base64::alphabet::STANDARD,
    GeneralPurposeConfig::new()
        .with_decode_padding_mode(DecodePaddingMode::RequireNone)
        .with_decode_allow_trailing_bits(true),
);

/// One allowed write (ADR-034 §2).
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WriteRule {
    pub method: String,
    /// `/`-separated template: literal segments, `*` (one segment) or
    /// `prefix*` (one segment starting with `prefix`).
    pub path: String,
    /// name → value: the parameter appears exactly once with this value.
    #[serde(default)]
    pub query: BTreeMap<String, String>,
    #[serde(default)]
    pub query_forbid: Vec<String>,
    #[serde(default)]
    pub check: Option<BodyCheck>,
    #[serde(default)]
    pub preflight: Option<Preflight>,
}

/// Named body checks the host implements (ADR-034 §4, §5).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BodyCheck {
    GmailBatchReads,
    GmailDraftText,
    CalendarHoldInsert,
    CalendarHoldPatch,
}

/// Named ownership reads the host performs before a write.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Preflight {
    GmailDraftOwned,
    CalendarEventSolo,
}

/// The request body as the broker puts it on the wire.
#[derive(Debug, Clone, Copy)]
pub enum Body<'a> {
    None,
    /// `json=`: the wire carries this value's canonical serialisation,
    /// so a duplicate key cannot exist on it.
    Json(&'a Value),
    /// `body=` text.
    Text(&'a str),
    /// A raw blob ref's bytes.
    Bytes(&'a [u8]),
}

/// An allowed write's ownership read: `GET url` with the same
/// credential, then [`verify_preflight`] on the response.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PreflightRead {
    pub kind: Preflight,
    pub url: String,
}

/// Decide one request through a policy-bearing handle (ADR-034 §3):
/// `Ok(None)` sends it, `Ok(Some(read))` sends it after a successful
/// ownership read, `Err(reason)` refuses it.
pub fn check(
    rules: &[WriteRule],
    method: &str,
    url: &str,
    headers: Option<&Map<String, Value>>,
    body: Body<'_>,
) -> Result<Option<PreflightRead>, String> {
    // the parser ureq sends with: dot segments are resolved here, so
    // the path checked is the path sent
    let u = url::Url::parse(url).map_err(|e| format!("unparsable url ({e})"))?;
    if let Some(k) = headers.and_then(|h| h.keys().find(|k| norm(k).contains("method"))) {
        return Err(format!(
            "header {k:?} would tunnel a method past the write allowlist"
        ));
    }
    let pairs: Vec<(String, String)> = u
        .query_pairs()
        .map(|(k, v)| (k.into_owned(), v.into_owned()))
        .collect();
    if let Some((k, _)) = pairs.iter().find(|(k, _)| norm(k).contains("method")) {
        return Err(format!(
            "query parameter {k:?} would tunnel a method past the write allowlist"
        ));
    }
    let method = method.to_ascii_uppercase();
    if method == "GET" || method == "HEAD" {
        return Ok(None);
    }
    let path = u.path();
    let mut first_err: Option<String> = None;
    for rule in rules
        .iter()
        .filter(|r| r.method.eq_ignore_ascii_case(&method))
    {
        if !path_matches(&rule.path, path) {
            continue;
        }
        match check_rule(rule, &pairs, headers, body) {
            Ok(()) => {
                return Ok(rule.preflight.map(|kind| PreflightRead {
                    kind,
                    url: preflight_url(kind, &u),
                }))
            }
            Err(e) => {
                first_err.get_or_insert(e);
            }
        }
    }
    Err(first_err
        .unwrap_or_else(|| format!("{method} {path} is not a write this credential allows")))
}

/// The verdict on a preflight read's `http.get` output (ADR-034 §4, §5).
pub fn verify_preflight(kind: Preflight, out: &Value) -> Result<(), String> {
    let status = out.get("status").and_then(Value::as_u64).unwrap_or(0);
    if status != 200 {
        return Err(format!(
            "could not verify the item is Bao's: the ownership read answered HTTP {status}"
        ));
    }
    let body = match out.get("body") {
        Some(Value::String(s)) => serde_json::from_str::<Value>(s)
            .map_err(|_| "the ownership read returned a non-JSON body".to_string())?,
        Some(v @ Value::Object(_)) => v.clone(),
        _ => return Err("the ownership read returned no JSON body".into()),
    };
    match kind {
        Preflight::GmailDraftOwned => {
            let owned = body
                .pointer("/message/payload/headers")
                .and_then(Value::as_array)
                .is_some_and(|hs| {
                    hs.iter().any(|h| {
                        h.get("name")
                            .and_then(Value::as_str)
                            .is_some_and(|n| n.eq_ignore_ascii_case(DRAFT_MARKER))
                            && h.get("value")
                                .and_then(Value::as_str)
                                .is_some_and(|v| !v.trim().is_empty())
                    })
                });
            if owned {
                Ok(())
            } else {
                Err(format!(
                    "the draft has no {DRAFT_MARKER} header — it is the user's draft, and Bao \
                     changes only its own"
                ))
            }
        }
        Preflight::CalendarEventSolo => {
            if let Some(org) = body.get("organizer") {
                if org.get("self") != Some(&Value::Bool(true)) {
                    return Err("the event is organised by someone else".into());
                }
            }
            let guests = body
                .get("attendees")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter(|g| g.get("self") != Some(&Value::Bool(true)))
                        .count()
                })
                .unwrap_or(0);
            if guests > 0 {
                Err(
                    "the event has guests — a hold the user turned into a meeting is \
                     theirs, and Bao's edit would reach the guests' calendars"
                        .into(),
                )
            } else {
                Ok(())
            }
        }
    }
}

/// A query or header name with case and separators folded away —
/// Google's front end accepts `send_updates` for `sendUpdates`.
fn norm(name: &str) -> String {
    name.chars()
        .filter(char::is_ascii_alphanumeric)
        .map(|c| c.to_ascii_lowercase())
        .collect()
}

fn path_matches(template: &str, path: &str) -> bool {
    let t: Vec<&str> = template.split('/').collect();
    let p: Vec<&str> = path.split('/').collect();
    t.len() == p.len() && t.iter().zip(&p).all(|(ts, ps)| segment_matches(ts, ps))
}

fn segment_matches(template: &str, raw: &str) -> bool {
    match template.strip_suffix('*') {
        // literal: byte-exact, still percent-encoded
        None => template == raw,
        Some(prefix) => decode_segment(raw).is_some_and(|s| s.starts_with(prefix)),
    }
}

/// A wildcard segment, percent-decoded: 1–1024 chars of
/// `A-Za-z0-9-._@+`, never `.`/`..` — else no match.
fn decode_segment(raw: &str) -> Option<String> {
    let s = String::from_utf8(percent_decode(raw)?).ok()?;
    let ok = !s.is_empty()
        && s.len() <= 1024
        && s != "."
        && s != ".."
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '.' | '_' | '@' | '+'));
    ok.then_some(s)
}

/// Strict percent-decoding: a `%` not followed by two hex digits fails.
fn percent_decode(raw: &str) -> Option<Vec<u8>> {
    let b = raw.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' {
            let hex = b.get(i + 1..i + 3)?;
            if !hex.iter().all(u8::is_ascii_hexdigit) {
                return None;
            }
            out.push(u8::from_str_radix(std::str::from_utf8(hex).ok()?, 16).ok()?);
            i += 3;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    Some(out)
}

fn check_rule(
    rule: &WriteRule,
    pairs: &[(String, String)],
    headers: Option<&Map<String, Value>>,
    body: Body<'_>,
) -> Result<(), String> {
    for (name, want) in &rule.query {
        let hits: Vec<&str> = pairs
            .iter()
            .filter(|(k, _)| norm(k) == norm(name))
            .map(|(_, v)| v.as_str())
            .collect();
        match hits.as_slice() {
            [v] if v == want => {}
            [] => return Err(format!("the query parameter {name}={want} is required")),
            [_] => return Err(format!("the query parameter {name} must be {want:?}")),
            _ => return Err(format!("the query parameter {name} appears more than once")),
        }
    }
    if let Some(name) = rule
        .query_forbid
        .iter()
        .find(|f| pairs.iter().any(|(k, _)| norm(k) == norm(f)))
    {
        return Err(format!("the query parameter {name} is not allowed"));
    }
    match rule.check {
        None => Ok(()),
        Some(BodyCheck::GmailBatchReads) => gmail_batch_reads(headers, body),
        Some(BodyCheck::GmailDraftText) => gmail_draft_text(json_object(body)?),
        Some(BodyCheck::CalendarHoldInsert) => calendar_hold_insert(json_object(body)?),
        Some(BodyCheck::CalendarHoldPatch) => calendar_hold_patch(json_object(body)?),
    }
}

fn json_object<'a>(body: Body<'a>) -> Result<&'a Map<String, Value>, String> {
    match body {
        Body::Json(Value::Object(m)) => Ok(m),
        Body::Json(_) => Err("the body must be a JSON object".into()),
        _ => Err("the body must be sent as json= (ADR-034 §3)".into()),
    }
}

fn preflight_url(kind: Preflight, u: &url::Url) -> String {
    let base = format!("{}{}", u.origin().ascii_serialization(), u.path());
    match kind {
        Preflight::GmailDraftOwned => {
            format!("{base}?format=metadata&metadataHeaders={DRAFT_MARKER}")
        }
        Preflight::CalendarEventSolo => base,
    }
}

// --- Gmail (ADR-034 §4) -----------------------------------------------------

/// A Gmail batch whose every part is one inner `GET` of the user's own
/// mailbox, parsed strictly from the request's own boundary.
fn gmail_batch_reads(headers: Option<&Map<String, Value>>, body: Body<'_>) -> Result<(), String> {
    let Body::Text(text) = body else {
        return Err("a batch body must be multipart text (body=)".into());
    };
    let ctypes: Vec<&str> = headers
        .into_iter()
        .flat_map(|h| h.iter())
        .filter(|(k, _)| k.eq_ignore_ascii_case("content-type"))
        .filter_map(|(_, v)| v.as_str())
        .collect();
    let [ctype] = ctypes.as_slice() else {
        return Err("a batch needs exactly one multipart/mixed Content-Type".into());
    };
    let boundary = multipart_boundary(ctype)?;
    if has_bare_line_break(text) {
        return Err("a batch body must use CRLF line endings only".into());
    }
    let delim = format!("--{boundary}");
    let Some(after_first) = text.strip_prefix(delim.as_str()) else {
        return Err("a batch body must start with its boundary (no preamble)".into());
    };
    let segments: Vec<&str> = after_first.split(&format!("\r\n{delim}")).collect();
    let Some((close, parts)) = segments.split_last() else {
        return Err("a batch body has no parts".into());
    };
    if parts.is_empty() {
        return Err("a batch body has no parts".into());
    }
    match close.strip_prefix("--") {
        Some(epilogue) if epilogue.trim_matches(['\r', '\n', ' ', '\t']).is_empty() => {}
        _ => return Err("a batch body must end with its close delimiter".into()),
    }
    for (i, part) in parts.iter().enumerate() {
        let Some(content) = part.strip_prefix("\r\n") else {
            return Err(format!("batch part {i} is malformed"));
        };
        batch_part(content).map_err(|e| format!("batch part {i}: {e}"))?;
    }
    Ok(())
}

fn batch_part(content: &str) -> Result<(), String> {
    let mut lines = content.split("\r\n");
    // the part's own headers, up to the blank line
    let mut part_headers = Vec::new();
    for line in lines.by_ref() {
        if line.is_empty() {
            break;
        }
        part_headers.push(header_line(line)?);
    }
    let ctypes: Vec<&String> = part_headers
        .iter()
        .filter(|(n, _)| n.eq_ignore_ascii_case("content-type"))
        .map(|(_, v)| v)
        .collect();
    match ctypes.as_slice() {
        [v] if media_type(v) == "application/http" => {}
        _ => return Err("must be exactly one Content-Type: application/http".into()),
    }
    // the inner request line
    let request = lines.next().unwrap_or("");
    let fields: Vec<&str> = request.split(' ').collect();
    let (method, path) = match fields.as_slice() {
        [m, p] => (*m, *p),
        [m, p, v] if v.starts_with("HTTP/") => (*m, *p),
        _ => return Err(format!("unparsable inner request line {request:?}")),
    };
    if method != "GET" {
        return Err(format!(
            "an inner {method} — a batch may only read (ADR-034 §4)"
        ));
    }
    if !path.starts_with(BATCH_GET_PREFIX) || path.chars().any(|c| c.is_control()) {
        return Err(format!("inner path {path:?} is outside {BATCH_GET_PREFIX}"));
    }
    // inner headers, then nothing: a GET carries no body
    for line in lines.by_ref() {
        if line.is_empty() {
            break;
        }
        let (name, _) = header_line(line)?;
        if norm(&name).contains("method") {
            return Err(format!("inner header {name:?} would tunnel a method"));
        }
    }
    if lines.any(|l| !l.is_empty()) {
        return Err("an inner request may not carry a body".into());
    }
    Ok(())
}

/// The `boundary` of a `multipart/mixed` content type.
fn multipart_boundary(ctype: &str) -> Result<String, String> {
    let mut parts = ctype.split(';');
    if media_type(parts.next().unwrap_or("")) != "multipart/mixed" {
        return Err("a batch must be multipart/mixed".into());
    }
    let boundaries: Vec<String> = parts
        .filter_map(|p| p.split_once('='))
        .filter(|(k, _)| k.trim().eq_ignore_ascii_case("boundary"))
        .map(|(_, v)| v.trim().trim_matches('"').to_string())
        .collect();
    match boundaries.as_slice() {
        [b] if !b.is_empty()
            && b.len() <= 70
            && b.chars()
                .all(|c| c.is_ascii_alphanumeric() || "'()+_,-./:=?".contains(c)) =>
        {
            Ok(b.clone())
        }
        _ => Err("a batch needs exactly one valid boundary".into()),
    }
}

fn media_type(v: &str) -> String {
    v.split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase()
}

/// A `\n` without its `\r`, or a `\r` without its `\n`.
fn has_bare_line_break(text: &str) -> bool {
    let b = text.as_bytes();
    b.iter().enumerate().any(|(i, &c)| {
        (c == b'\n' && (i == 0 || b[i - 1] != b'\r'))
            || (c == b'\r' && b.get(i + 1) != Some(&b'\n'))
    })
}

/// `Name: value`, the name RFC 5322 ftext (printable ASCII, no `:`,
/// no whitespace — the obsolete `Name :` form refuses).
fn header_line(line: &str) -> Result<(String, String), String> {
    let (name, value) = line
        .split_once(':')
        .ok_or_else(|| format!("malformed header line {line:?}"))?;
    if name.is_empty() || !name.bytes().all(|c| (33..=126).contains(&c)) {
        return Err(format!("malformed header name {name:?}"));
    }
    Ok((name.to_string(), value.trim().to_string()))
}

/// The `drafts.create` / `drafts.update` body: a Bao-marked
/// `text/plain` message and nothing else.
fn gmail_draft_text(m: &Map<String, Value>) -> Result<(), String> {
    if let Some(k) = m.keys().find(|k| *k != "id" && *k != "message") {
        return Err(format!("draft key {k:?} is not allowed"));
    }
    if m.get("id").is_some_and(|v| !v.is_string()) {
        return Err("draft id must be a string".into());
    }
    let msg = m
        .get("message")
        .and_then(Value::as_object)
        .ok_or("a draft needs a message object")?;
    if let Some(k) = msg.keys().find(|k| *k != "raw" && *k != "threadId") {
        return Err(format!("draft message key {k:?} is not allowed"));
    }
    if msg.get("threadId").is_some_and(|v| !v.is_string()) {
        return Err("draft message.threadId must be a string".into());
    }
    let raw = msg
        .get("raw")
        .and_then(Value::as_str)
        .ok_or("a draft needs message.raw")?;
    let normalized: String = raw
        .trim_end_matches('=')
        .chars()
        .map(|c| match c {
            '-' => '+',
            '_' => '/',
            c => c,
        })
        .collect();
    let bytes = LENIENT_B64
        .decode(normalized)
        .map_err(|_| "message.raw is not base64url".to_string())?;
    let headers = header_block(&bytes)?;
    let ctypes: Vec<&String> = headers
        .iter()
        .filter(|(n, _)| n.eq_ignore_ascii_case("content-type"))
        .map(|(_, v)| v)
        .collect();
    match ctypes.as_slice() {
        [] => {}
        [v] if media_type(v) == "text/plain" => {}
        [v] => {
            return Err(format!(
                "a draft must be text/plain, not {} — HTML can load remote content when the \
                 user opens it (ADR-034 §4)",
                media_type(v)
            ))
        }
        _ => return Err("a draft carries more than one Content-Type".into()),
    }
    if !headers
        .iter()
        .any(|(n, v)| n.eq_ignore_ascii_case(DRAFT_MARKER) && !v.is_empty())
    {
        return Err(format!("a draft must carry the {DRAFT_MARKER} header"));
    }
    Ok(())
}

/// A message's header block, parsed strictly: ASCII, CRLF or LF line
/// ends (never a lone CR), every line a header or a continuation, up
/// to the first empty line.
fn header_block(bytes: &[u8]) -> Result<Vec<(String, String)>, String> {
    let mut headers: Vec<(String, String)> = Vec::new();
    let mut rest = bytes;
    loop {
        let (line, next) = match rest.iter().position(|&c| c == b'\n') {
            Some(i) => (&rest[..i], Some(&rest[i + 1..])),
            None => (rest, None),
        };
        let line = line.strip_suffix(b"\r").unwrap_or(line);
        if line.contains(&b'\r') {
            return Err("a lone CR in the message's header block".into());
        }
        if line.is_empty() {
            break;
        }
        if !line.is_ascii() {
            return Err("non-ASCII bytes in the message's header block".into());
        }
        let text = std::str::from_utf8(line).expect("ascii is utf-8");
        if text.starts_with([' ', '\t']) {
            let last = headers
                .last_mut()
                .ok_or("the message starts with a continuation line")?;
            last.1.push(' ');
            last.1.push_str(text.trim());
        } else {
            headers.push(header_line(text)?);
        }
        match next {
            Some(n) => rest = n,
            None => break,
        }
    }
    if headers.is_empty() {
        return Err("the message has no headers".into());
    }
    Ok(headers)
}

// --- Calendar (ADR-034 §5) --------------------------------------------------

/// A guarded key spelled any other way (`Attendees`, `visibility_`) is
/// refused rather than guessed at.
fn calendar_spelling(m: &Map<String, Value>) -> Result<(), String> {
    match m.keys().find(|k| {
        CALENDAR_GUARDED.contains(&norm(k).as_str()) && !CALENDAR_GUARDED.contains(&k.as_str())
    }) {
        Some(k) => Err(format!("event key {k:?} is ambiguous")),
        None => Ok(()),
    }
}

fn calendar_hold_insert(m: &Map<String, Value>) -> Result<(), String> {
    calendar_spelling(m)?;
    if m.contains_key("attendees") {
        return Err("a hold has no attendees — Bao cannot invite anyone (ADR-034 §5)".into());
    }
    let id = m
        .get("id")
        .and_then(Value::as_str)
        .ok_or_else(|| format!("a hold needs an id starting with {HOLD_PREFIX:?}"))?;
    if !(id.starts_with(HOLD_PREFIX)
        && (5..=1024).contains(&id.len())
        && id.chars().all(|c| matches!(c, '0'..='9' | 'a'..='v')))
    {
        return Err(format!(
            "a hold id must be {HOLD_PREFIX:?} + base32hex (0-9a-v), not {id:?}"
        ));
    }
    if m.get("visibility").and_then(Value::as_str) != Some("private") {
        return Err("a hold must have visibility \"private\"".into());
    }
    Ok(())
}

fn calendar_hold_patch(m: &Map<String, Value>) -> Result<(), String> {
    calendar_spelling(m)?;
    if m.contains_key("attendees") {
        return Err("a hold has no attendees — Bao cannot invite anyone (ADR-034 §5)".into());
    }
    if m.contains_key("id") {
        return Err("a patch may not change the event id".into());
    }
    match m.get("visibility") {
        None => Ok(()),
        Some(v) if v.as_str() == Some("private") => Ok(()),
        Some(_) => Err("a hold must stay visibility \"private\"".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn google_rules() -> Vec<WriteRule> {
        crate::oauth::builtin_providers()["google"]
            .guest_writes
            .clone()
            .expect("the google descriptor carries guest_writes")
    }

    fn b64url(s: &str) -> String {
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(s.as_bytes())
    }

    const GMAIL: &str = "https://gmail.googleapis.com";
    const CAL: &str = "https://www.googleapis.com/calendar/v3/calendars/primary/events";

    fn draft_body(raw_text: &str) -> Value {
        json!({"message": {"raw": b64url(raw_text), "threadId": "t1"}})
    }

    const PLAIN: &str = "To: a@x.io\r\nSubject: hi\r\nX-Anybao-Draft: 1\r\n\
                         Content-Type: text/plain; charset=\"utf-8\"\r\n\r\nHello\r\n";

    fn post(url: &str, body: &Value) -> Result<Option<PreflightRead>, String> {
        check(&google_rules(), "POST", url, None, Body::Json(body))
    }

    fn batch_headers() -> Map<String, Value> {
        json!({"Content-Type": "multipart/mixed; boundary=b0und"})
            .as_object()
            .unwrap()
            .clone()
    }

    fn batch(parts: &[&str]) -> String {
        let mut s = String::new();
        for (i, p) in parts.iter().enumerate() {
            s.push_str(&format!(
                "--b0und\r\nContent-Type: application/http\r\nContent-ID: <item{i}>\r\n\r\n{p}\r\n\r\n"
            ));
        }
        s.push_str("--b0und--\r\n");
        s
    }

    fn post_batch(body: &str) -> Result<Option<PreflightRead>, String> {
        let h = batch_headers();
        check(
            &google_rules(),
            "POST",
            &format!("{GMAIL}/batch/gmail/v1"),
            Some(&h),
            Body::Text(body),
        )
    }

    fn hold() -> Value {
        json!({"id": "baoagent0123abcd", "summary": "Focus", "visibility": "private",
               "start": {"dateTime": "2026-11-10T10:00:00Z"},
               "end": {"dateTime": "2026-11-10T11:00:00Z"}})
    }

    // --- the table and the read path ---------------------------------------

    #[test]
    fn the_google_table_parses_and_reads_pass() {
        let rules = google_rules();
        assert_eq!(rules.len(), 7);
        for url in [
            format!("{GMAIL}/gmail/v1/users/me/messages/send"),
            format!("{CAL}?sendUpdates=all"),
            "https://www.googleapis.com/drive/v3/files".to_string(),
        ] {
            assert_eq!(check(&rules, "GET", &url, None, Body::None), Ok(None));
            assert_eq!(check(&rules, "head", &url, None, Body::None), Ok(None));
        }
    }

    #[test]
    fn sends_and_unlisted_writes_are_refused() {
        let empty = json!({});
        let raw = draft_body(PLAIN);
        for (method, url, body) in [
            ("POST", format!("{GMAIL}/gmail/v1/users/me/messages/send"), &raw),
            ("POST", format!("{GMAIL}/gmail/v1/users/me/drafts/send"), &raw),
            ("POST", format!("{GMAIL}/upload/gmail/v1/users/me/messages/send"), &raw),
            ("POST", format!("{GMAIL}/upload/gmail/v1/users/me/drafts"), &raw),
            ("POST", "https://www.googleapis.com/gmail/v1/users/me/messages/send".to_string(), &raw),
            ("POST", format!("{GMAIL}/gmail/v1/users/me/messages"), &raw),
            ("POST", format!("{GMAIL}/gmail/v1/users/me/messages/import"), &raw),
            ("POST", format!("{GMAIL}/gmail/v1/users/me/messages/m1/modify"), &empty),
            ("POST", format!("{GMAIL}/gmail/v1/users/me/settings/forwardingAddresses"), &empty),
            ("PUT", format!("{GMAIL}/gmail/v1/users/me/settings/vacation"), &empty),
            ("POST", format!("{GMAIL}/gmail/v1/users/other@x.io/drafts"), &raw),
            ("POST", format!("{GMAIL}/gmail/v1/users/me/watch"), &empty),
            ("POST", "https://www.googleapis.com/batch".to_string(), &empty),
            ("POST", format!("{CAL}/quickAdd?sendUpdates=none&text=x"), &empty),
            ("POST", format!("{CAL}/import?sendUpdates=none"), &empty),
            ("POST", format!("{CAL}/baoagent1/move?sendUpdates=none&destination=x"), &empty),
            ("POST", format!("{CAL}/watch"), &empty),
            ("POST", "https://www.googleapis.com/calendar/v3/calendars/x%40group.calendar.google.com/events?sendUpdates=none".to_string(), &hold()),
            ("POST", "https://www.googleapis.com/calendar/v3/calendars/primary/acl".to_string(), &empty),
            ("PUT", format!("{CAL}/baoagent1?sendUpdates=none"), &empty),
            ("PATCH", "https://sheets.googleapis.com/v4/spreadsheets/s1".to_string(), &empty),
            ("POST", "https://www.googleapis.com/drive/v3/files/f1/permissions".to_string(), &empty),
        ] {
            let got = check(&google_rules(), method, &url, None, Body::Json(body));
            assert!(got.is_err(), "{method} {url} must be refused, got {got:?}");
        }
    }

    // --- matching (ADR-034 §3) ---------------------------------------------

    #[test]
    fn dot_segments_resolve_before_matching() {
        let raw = draft_body(PLAIN);
        let err = post(
            &format!("{GMAIL}/gmail/v1/users/me/drafts/../messages/send"),
            &raw,
        )
        .unwrap_err();
        assert!(err.contains("/messages/send"), "{err}");
        assert!(post(
            &format!("{GMAIL}/gmail/v1/users/me/drafts/%2e%2e/messages/send"),
            &raw
        )
        .is_err());
    }

    #[test]
    fn literal_segments_are_byte_exact() {
        let raw = draft_body(PLAIN);
        for path in [
            "/gmail/v1/users/me/Drafts",
            "/gmail/v1/users/me/drafts/",
            "/gmail/v1/users/me/drafts;x",
            "/gmail/v1/users/%6de/drafts",
            "/gmail/v1/users/me//drafts",
        ] {
            assert!(post(&format!("{GMAIL}{path}"), &raw).is_err(), "{path}");
        }
        assert_eq!(
            post(&format!("{GMAIL}/gmail/v1/users/me/drafts"), &raw),
            Ok(None)
        );
    }

    #[test]
    fn wildcards_decode_and_keep_to_their_charset() {
        let rules = google_rules();
        let del = |id: &str| {
            check(
                &rules,
                "DELETE",
                &format!("{GMAIL}/gmail/v1/users/me/drafts/{id}"),
                None,
                Body::None,
            )
        };
        assert!(del("r-123456").unwrap().is_some());
        for bad in ["%2Fsend", "a%3Asend", "a%20b", "%2e", "a%zz", "a:send"] {
            assert!(del(bad).is_err(), "{bad}");
        }
        // prefix wildcards decode first, then compare
        let ev = |id: &str| {
            check(
                &rules,
                "DELETE",
                &format!("{CAL}/{id}?sendUpdates=none"),
                None,
                Body::None,
            )
        };
        assert!(ev("baoagent0123").unwrap().is_some());
        assert!(ev("%62aoagent0123").unwrap().is_some());
        assert!(ev("abc123").is_err());
        assert!(ev("baoagent0123_20261110T100000Z").unwrap().is_some());
    }

    #[test]
    fn query_names_fold_aliases_and_count_once() {
        let h = hold();
        assert_eq!(post(&format!("{CAL}?sendUpdates=none"), &h), Ok(None));
        assert_eq!(post(&format!("{CAL}?send_updates=none"), &h), Ok(None));
        for q in [
            "",
            "?sendUpdates=all",
            "?sendUpdates=none&send_updates=all",
            "?sendUpdates=none&sendUpdates=none",
            "?sendUpdates=none&sendNotifications=false",
            "?sendUpdates=none&SEND-NOTIFICATIONS=true",
            "?sendUpdates=None",
        ] {
            assert!(post(&format!("{CAL}{q}"), &h).is_err(), "{q}");
        }
    }

    #[test]
    fn method_tunnels_are_refused_even_on_reads() {
        let rules = google_rules();
        let h = json!({"X-HTTP-Method-Override": "POST"});
        let url = format!("{GMAIL}/gmail/v1/users/me/drafts/send");
        assert!(check(&rules, "GET", &url, h.as_object(), Body::None).is_err());
        let h = json!({"x-method_override": "PUT"});
        assert!(check(&rules, "GET", &url, h.as_object(), Body::None).is_err());
        assert!(check(
            &rules,
            "GET",
            &format!("{url}?$method=POST"),
            None,
            Body::None
        )
        .is_err());
        let plain = json!({"Accept": "application/json"});
        assert_eq!(
            check(&rules, "GET", &url, plain.as_object(), Body::None),
            Ok(None)
        );
    }

    #[test]
    fn rules_without_a_policy_table_are_not_consulted() {
        // an empty table refuses every write; reads pass
        assert!(check(&[], "POST", "https://x/y", None, Body::None).is_err());
        assert_eq!(check(&[], "GET", "https://x/y", None, Body::None), Ok(None));
        assert!(check(&[], "POST", "not a url", None, Body::None).is_err());
    }

    // --- Gmail drafts (ADR-034 §4) -----------------------------------------

    #[test]
    fn a_marked_plain_draft_is_allowed() {
        let url = format!("{GMAIL}/gmail/v1/users/me/drafts");
        assert_eq!(post(&url, &draft_body(PLAIN)), Ok(None));
        // LF line ends, no Content-Type (= text/plain), padded standard base64
        let lf = "To: a@x.io\nX-Anybao-Draft: 1\n\nHi\n";
        assert_eq!(post(&url, &draft_body(lf)), Ok(None));
        let padded = base64::engine::general_purpose::STANDARD.encode(lf);
        assert_eq!(post(&url, &json!({"message": {"raw": padded}})), Ok(None));
        // folded headers stay one header
        let folded = "Subject: a\r\n b\r\nX-Anybao-Draft: 1\r\n\r\nx";
        assert_eq!(post(&url, &draft_body(folded)), Ok(None));
    }

    #[test]
    fn draft_bodies_outside_the_shape_are_refused() {
        let url = format!("{GMAIL}/gmail/v1/users/me/drafts");
        let refused = |body: &Value, why: &str| {
            let got = post(&url, body);
            assert!(got.is_err(), "{why}: {got:?}");
            got.unwrap_err()
        };
        let html = "X-Anybao-Draft: 1\r\nContent-Type: text/html\r\n\r\n<img src=x>";
        assert!(refused(&draft_body(html), "html").contains("text/plain"));
        let multi = "X-Anybao-Draft: 1\r\nContent-Type: multipart/alternative; boundary=b\r\n\r\n";
        refused(&draft_body(multi), "multipart");
        let two =
            "X-Anybao-Draft: 1\r\nContent-Type: text/plain\r\ncontent-type: text/html\r\n\r\nx";
        refused(&draft_body(two), "two content types");
        let unmarked = "To: a@x.io\r\nContent-Type: text/plain\r\n\r\nx";
        assert!(refused(&draft_body(unmarked), "no marker").contains(DRAFT_MARKER));
        let spaced = "X-Anybao-Draft: 1\r\nContent-Type : text/html\r\n\r\nx";
        refused(&draft_body(spaced), "obsolete name syntax");
        let garbage = "X-Anybao-Draft: 1\r\nnot a header\r\n\r\nx";
        refused(&draft_body(garbage), "malformed line");
        let lone_cr = "X-Anybao-Draft: 1\rContent-Type: text/html\r\n\r\nx";
        refused(&draft_body(lone_cr), "lone CR");
        let cont_first = " folded\r\nX-Anybao-Draft: 1\r\n\r\nx";
        refused(&draft_body(cont_first), "leading continuation");
        let utf8 = "X-Anybao-Draft: 1\r\nSubject: Grüße\r\n\r\nx";
        refused(&draft_body(utf8), "non-ascii header");
        refused(&json!({"message": {"raw": "!!!"}}), "not base64");
        refused(
            &json!({"message": {"raw": b64url(PLAIN)}, "x": 1}),
            "extra key",
        );
        refused(
            &json!({"message": {"raw": b64url(PLAIN), "payload": {}}}),
            "payload",
        );
        refused(
            &json!({"message": {"raw": b64url(PLAIN), "threadId": 5}}),
            "numeric thread",
        );
        refused(&json!({"message": {}}), "no raw");
        refused(&json!([]), "array");
        // a json body must be json=
        let text = draft_body(PLAIN).to_string();
        assert!(check(&google_rules(), "POST", &url, None, Body::Text(&text)).is_err());
    }

    #[test]
    fn draft_update_and_delete_need_the_ownership_read() {
        let rules = google_rules();
        let url = format!("{GMAIL}/gmail/v1/users/me/drafts/r-42");
        let pf = check(&rules, "PUT", &url, None, Body::Json(&draft_body(PLAIN)))
            .unwrap()
            .unwrap();
        assert_eq!(pf.kind, Preflight::GmailDraftOwned);
        assert_eq!(
            pf.url,
            format!("{url}?format=metadata&metadataHeaders=X-Anybao-Draft")
        );
        let pf = check(&rules, "DELETE", &url, None, Body::None)
            .unwrap()
            .unwrap();
        assert_eq!(pf.kind, Preflight::GmailDraftOwned);
        let html = draft_body("X-Anybao-Draft: 1\r\nContent-Type: text/html\r\n\r\nx");
        assert!(check(&rules, "PUT", &url, None, Body::Json(&html)).is_err());
    }

    #[test]
    fn the_draft_ownership_verdict_reads_the_marker() {
        let out = |headers: Value| {
            json!({"status": 200, "body":
                json!({"id": "r-42", "message": {"payload": {"headers": headers}}}).to_string()})
        };
        let ok = out(json!([{"name": "x-anybao-draft", "value": "1"}]));
        assert_eq!(verify_preflight(Preflight::GmailDraftOwned, &ok), Ok(()));
        let theirs = out(json!([{"name": "Subject", "value": "mine"}]));
        assert!(verify_preflight(Preflight::GmailDraftOwned, &theirs)
            .unwrap_err()
            .contains("user's draft"));
        let empty = out(json!([{"name": "X-Anybao-Draft", "value": " "}]));
        assert!(verify_preflight(Preflight::GmailDraftOwned, &empty).is_err());
        let missing = json!({"status": 404, "body": "{}"});
        assert!(verify_preflight(Preflight::GmailDraftOwned, &missing)
            .unwrap_err()
            .contains("HTTP 404"));
        let not_json = json!({"status": 200, "body": "<html>"});
        assert!(verify_preflight(Preflight::GmailDraftOwned, &not_json).is_err());
    }

    // --- the sync's batch (ADR-034 §4) -------------------------------------

    #[test]
    fn a_batch_of_inner_gets_is_allowed() {
        let body = batch(&[
            "GET /gmail/v1/users/me/messages/m1?format=full",
            "GET /gmail/v1/users/me/messages/m2?format=metadata HTTP/1.1",
        ]);
        assert_eq!(post_batch(&body), Ok(None));
        // the exact shape gmailSync@v1 sends
        let sync = "--b0und\r\nContent-Type: application/http\r\nContent-ID: <item0>\r\n\r\n\
                    GET /gmail/v1/users/me/messages/abc?format=full\r\n\r\n--b0und--\r\n";
        assert_eq!(post_batch(sync), Ok(None));
        // a quoted boundary
        let h = json!({"content-type": "multipart/mixed; boundary=\"b0und\""});
        let got = check(
            &google_rules(),
            "POST",
            &format!("{GMAIL}/batch/gmail/v1"),
            h.as_object(),
            Body::Text(sync),
        );
        assert_eq!(got, Ok(None));
    }

    #[test]
    fn a_batch_that_could_write_is_refused() {
        let send = "POST /gmail/v1/users/me/messages/send\r\nContent-Type: application/json\r\n\r\n{\"raw\": \"x\"}";
        let refused = |body: &str, why: &str| {
            let got = post_batch(body);
            assert!(got.is_err(), "{why}: {got:?}");
        };
        refused(
            &batch(&["GET /gmail/v1/users/me/messages/m1", send]),
            "inner POST",
        );
        refused(
            &batch(&["get /gmail/v1/users/me/messages/m1"]),
            "lowercase verb",
        );
        refused(
            &batch(&["GET /gmail/v1/users/other/messages/m1"]),
            "other mailbox",
        );
        refused(
            &batch(&["GET https://gmail.googleapis.com/gmail/v1/users/me/messages/m1"]),
            "absolute url",
        );
        refused(
            &batch(&["GET /gmail/v1/users/me/messages/m1\r\nX-HTTP-Method-Override: POST"]),
            "inner tunnel",
        );
        refused(
            &batch(&["GET /gmail/v1/users/me/messages/m1\r\n\r\n{\"body\": 1}"]),
            "inner body",
        );
        let ok = batch(&["GET /gmail/v1/users/me/messages/m1"]);
        refused(&ok.replace("\r\n", "\n"), "LF only");
        refused(&format!("junk\r\n{ok}"), "preamble");
        refused(ok.trim_end_matches("--b0und--\r\n"), "no close delimiter");
        refused(
            &ok.replace("--b0und--", "--b0und--trailing junk"),
            "epilogue",
        );
        refused(
            &ok.replace("Content-Type: application/http", "Content-Type: text/plain"),
            "part type",
        );
        refused(
            &ok.replace("--b0und\r\nContent", "--b0undX\r\nContent"),
            "boundary prefix",
        );
        refused("--b0und--\r\n", "no parts");
        // the content type must name the boundary, once
        for h in [
            json!({}),
            json!({"Content-Type": "multipart/mixed"}),
            json!({"Content-Type": "multipart/related; boundary=b0und"}),
            json!({"Content-Type": "multipart/mixed; boundary=b0und; boundary=x"}),
            json!({"Content-Type": "multipart/mixed; boundary=b0und", "content-type": "x"}),
        ] {
            let got = check(
                &google_rules(),
                "POST",
                &format!("{GMAIL}/batch/gmail/v1"),
                h.as_object(),
                Body::Text(&ok),
            );
            assert!(got.is_err(), "{h}");
        }
        let v = json!({});
        let h = batch_headers();
        assert!(check(
            &google_rules(),
            "POST",
            &format!("{GMAIL}/batch/gmail/v1"),
            Some(&h),
            Body::Json(&v)
        )
        .is_err());
    }

    // --- Calendar holds (ADR-034 §5) ---------------------------------------

    #[test]
    fn a_private_solo_hold_is_allowed() {
        assert_eq!(post(&format!("{CAL}?sendUpdates=none"), &hold()), Ok(None));
        let mut meet = hold();
        meet["conferenceData"] = json!({"createRequest": {"requestId": "r1"}});
        assert_eq!(
            post(
                &format!("{CAL}?sendUpdates=none&conferenceDataVersion=1"),
                &meet
            ),
            Ok(None)
        );
    }

    #[test]
    fn holds_outside_the_shape_are_refused() {
        let url = format!("{CAL}?sendUpdates=none");
        let refused = |body: Value, why: &str| {
            let got = post(&url, &body);
            assert!(got.is_err(), "{why}: {got:?}");
        };
        let with = |k: &str, v: Value| {
            let mut h = hold();
            h[k] = v;
            h
        };
        refused(
            with("attendees", json!([{"email": "x@evil.io"}])),
            "attendees",
        );
        refused(with("attendees", json!([])), "empty attendees");
        refused(
            with("Attendees", json!([{"email": "x@evil.io"}])),
            "attendee spelling",
        );
        refused(with("visibility", json!("default")), "visibility");
        refused(with("Visibility", json!("public")), "visibility spelling");
        refused(with("id", json!("abc0123456")), "foreign id");
        refused(with("id", json!("baoagentXYZ")), "non-base32hex id");
        refused(with("id", json!("baoagent_1")), "underscore id");
        let mut no_id = hold();
        no_id.as_object_mut().unwrap().remove("id");
        refused(no_id, "no id");
        let mut no_vis = hold();
        no_vis.as_object_mut().unwrap().remove("visibility");
        refused(no_vis, "no visibility");
    }

    #[test]
    fn hold_patch_and_delete_are_guarded() {
        let rules = google_rules();
        let url = format!("{CAL}/baoagent0123abcd?sendUpdates=none");
        let patch = |body: Value| check(&rules, "PATCH", &url, None, Body::Json(&body));
        let pf = patch(json!({"summary": "moved"})).unwrap().unwrap();
        assert_eq!(pf.kind, Preflight::CalendarEventSolo);
        assert_eq!(pf.url, format!("{CAL}/baoagent0123abcd"));
        assert!(patch(json!({"visibility": "private"})).unwrap().is_some());
        assert!(patch(json!({"attendees": [{"email": "x@evil.io"}]})).is_err());
        assert!(patch(json!({"visibility": "public"})).is_err());
        assert!(patch(json!({"id": "baoagent9"})).is_err());
        // a foreign id never reaches the preflight
        let foreign = format!("{CAL}/7kq9l2abc?sendUpdates=none");
        assert!(check(&rules, "PATCH", &foreign, None, Body::Json(&json!({}))).is_err());
        assert!(check(&rules, "DELETE", &foreign, None, Body::None).is_err());
        let del = check(&rules, "DELETE", &url, None, Body::None)
            .unwrap()
            .unwrap();
        assert_eq!(del.kind, Preflight::CalendarEventSolo);
        let all = format!("{CAL}/baoagent0123abcd?sendUpdates=all");
        assert!(check(&rules, "DELETE", &all, None, Body::None).is_err());
    }

    #[test]
    fn the_solo_verdict_refuses_meetings() {
        let out = |ev: Value| json!({"status": 200, "body": ev.to_string()});
        let mine = json!({"organizer": {"email": "me@x.io", "self": true}});
        assert_eq!(
            verify_preflight(Preflight::CalendarEventSolo, &out(mine)),
            Ok(())
        );
        let self_only = json!({"attendees": [{"email": "me@x.io", "self": true}]});
        assert_eq!(
            verify_preflight(Preflight::CalendarEventSolo, &out(self_only)),
            Ok(())
        );
        let meeting = json!({"organizer": {"self": true},
            "attendees": [{"email": "me@x.io", "self": true}, {"email": "a@x.io"}]});
        assert!(
            verify_preflight(Preflight::CalendarEventSolo, &out(meeting))
                .unwrap_err()
                .contains("guests")
        );
        let invited = json!({"organizer": {"email": "evil@x.io"}});
        assert!(verify_preflight(Preflight::CalendarEventSolo, &out(invited)).is_err());
        let gone = json!({"status": 410, "body": "{}"});
        assert!(verify_preflight(Preflight::CalendarEventSolo, &gone)
            .unwrap_err()
            .contains("HTTP 410"));
    }
}
