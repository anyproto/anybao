//! docs/chatgpt-local-oauth.md configuration identity and coherent raw-snapshot validation.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

pub const TARGET_KEY: &str = "llm.configuration.target";
pub const ABANDONED_KEY: &str = "llm.configuration.abandoned";
pub const TIERS: [&str; 3] = ["codegen", "classify", "vision"];
pub const SEARCH: [&str; 2] = ["websearch", "deepresearch"];
pub const SNAPSHOT_LIMIT: usize = 1025;
const MAX_SAFE: f64 = 9_007_199_254_740_991.0;

/// The encoding uses UTF-8 bytes (including for key ordering), IEEE-754 binary64
/// big-endian bits with -0 normalized, and counted containers. No language's
/// JSON printer participates. Values outside the finite, safe-magnitude domain
/// and excessive nesting/size are not model configuration.
pub fn canonical(value: &Value) -> Option<String> {
    fn encode(value: &Value, out: &mut String, depth: usize) -> Option<()> {
        if depth > 32 || out.len() > 131_072 {
            return None;
        }
        match value {
            Value::Null => out.push('n'),
            Value::Bool(v) => out.push(if *v { 't' } else { 'f' }),
            Value::Number(n) => {
                let v = n.as_f64()?;
                if !v.is_finite() || v.abs() > MAX_SAFE {
                    return None;
                }
                let bits = if v == 0.0 { 0 } else { v.to_bits() };
                out.push_str(&format!("d{bits:016x}"));
            }
            Value::String(s) => {
                out.push_str(&format!("s{}:{}", s.len(), hex::encode(s.as_bytes())))
            }
            Value::Array(values) => {
                out.push_str(&format!("a{}[", values.len()));
                for v in values {
                    encode(v, out, depth + 1)?;
                }
                out.push(']');
            }
            Value::Object(values) => {
                out.push_str(&format!("o{}{{", values.len()));
                let mut keys: Vec<_> = values.keys().collect();
                keys.sort();
                for key in keys {
                    encode(&Value::String(key.clone()), out, depth + 1)?;
                    encode(&values[key], out, depth + 1)?;
                }
                out.push('}');
            }
        }
        (out.len() <= 131_072).then_some(())
    }
    let mut out = String::from("bao-model-v1\n");
    encode(value, &mut out, 0)?;
    Some(out)
}

pub fn config_id(value: &Value) -> Option<String> {
    Some(format!(
        "v1:{}",
        hex::encode(Sha256::digest(canonical(value)?.as_bytes()))
    ))
}

pub fn same(a: &Value, b: &Value) -> bool {
    canonical(a).is_some_and(|left| canonical(b).as_ref() == Some(&left))
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Target {
    pub schema_version: u8,
    pub apply_id: String,
    pub tiers: BTreeMap<String, Value>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub search: BTreeMap<String, Value>,
}

pub fn parse_target(value: &Value) -> Option<Target> {
    let text = value.as_str()?;
    if text.len() > 131_072 {
        return None;
    }
    let mut decoded: Value = serde_json::from_str(text).ok()?;
    if decoded.get("schemaVersion").and_then(Value::as_f64) != Some(1.0) {
        return None;
    }
    decoded["schemaVersion"] = json!(1);
    let target: Target = serde_json::from_value(decoded).ok()?;
    if target.schema_version != 1
        || target.apply_id.is_empty()
        || target.apply_id.len() > 256
        || target.apply_id.chars().any(char::is_control)
        || target.tiers.len() != 3
        || !TIERS
            .iter()
            .all(|t| target.tiers.get(*t).is_some_and(Value::is_object))
    {
        return None;
    }
    if target
        .search
        .iter()
        .any(|(k, v)| !SEARCH.contains(&k.as_str()) || !(v.is_object() || v.is_null()))
    {
        return None;
    }
    canonical(&target_identity(&target.tiers, &target.search))?;
    Some(target)
}

pub fn is_model_key(key: &str) -> bool {
    key == TARGET_KEY
        || key == ABANDONED_KEY
        || TIERS.iter().any(|t| key == format!("llm.tier.{t}"))
        || SEARCH.iter().any(|t| key == format!("search.provider.{t}"))
}

pub fn snapshot_query() -> Value {
    let mut keys = vec![TARGET_KEY.to_string(), ABANDONED_KEY.to_string()];
    keys.extend(TIERS.map(|t| format!("llm.tier.{t}")));
    keys.extend(SEARCH.map(|t| format!("search.provider.{t}")));
    json!({"filter": {"$or": [{"id": {"$in": keys}}, {"key": {"$in": keys}}]}, "limit": SNAPSHOT_LIMIT})
}

/// Only the model protocol requires canonical identity. Non-model rows retain
/// the ordinary store's lookup behavior. A missing value is absent; null stays.
pub fn raw_values(rows: &[Value]) -> Option<BTreeMap<String, Value>> {
    if rows.len() >= SNAPSHOT_LIMIT {
        return None;
    }
    let mut values = BTreeMap::new();
    let mut seen = std::collections::BTreeSet::new();
    for row in rows {
        let id = row.get("id").and_then(Value::as_str).unwrap_or("");
        let key = row.get("key").and_then(Value::as_str).unwrap_or("");
        if !is_model_key(id) && !is_model_key(key) {
            continue;
        }
        if id != key || !seen.insert(id) {
            return None;
        }
        if let Some(value) = row.get("value") {
            values.insert(id.to_owned(), value.clone());
        }
    }
    Some(values)
}

pub fn uses_chatgpt(value: &Value) -> bool {
    value["provider"] == "openai-responses"
        || value["backend"] == "openai-codex"
        || value["api_key_ref"]
            .as_str()
            .is_some_and(crate::local_grant::is_reserved_ref)
}

fn abandoned_ids(value: Option<&Value>) -> Option<std::collections::BTreeSet<&str>> {
    let Some(value) = value else {
        return Some(Default::default());
    };
    let values = value.as_array()?;
    if values.len() > 1024 {
        return None;
    }
    let mut out = std::collections::BTreeSet::new();
    for value in values {
        let id = value.as_str()?;
        if id.len() != 64
            || !id
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || !out.insert(id)
        {
            return None;
        }
    }
    Some(out)
}

fn valid_option(value: &Value, field: &str, allowed: &[&str]) -> bool {
    value.as_object().is_some_and(|v| {
        v.len() == 1
            && v.get(field)
                .and_then(Value::as_str)
                .is_some_and(|s| allowed.contains(&s))
    })
}

/// Compatibility is deliberately conservative. This does not claim model
/// entitlement: every request still reaches the local host auth boundary.
pub fn compatible(row: &Value) -> bool {
    let Some(obj) = row.as_object() else {
        return false;
    };
    let known = [
        "provider",
        "model",
        "base_url",
        "api_key_ref",
        "backend",
        "profile",
        "options",
        "stream",
        "timeout",
        "traits",
    ];
    if obj.keys().any(|k| !known.contains(&k.as_str())) {
        return false;
    }
    let providers = ["anthropic", "openai-compat", "openai-responses"];
    if !row["provider"]
        .as_str()
        .is_some_and(|v| providers.contains(&v))
    {
        return false;
    }
    if !row["model"]
        .as_str()
        .is_some_and(|v| !v.is_empty() && v.len() <= 256 && !v.chars().any(char::is_control))
    {
        return false;
    }
    let Some(base) = row["base_url"].as_str() else {
        return false;
    };
    if !url::Url::parse(base)
        .is_ok_and(|u| matches!(u.scheme(), "http" | "https") && u.host_str().is_some())
    {
        return false;
    }
    if !matches!(
        obj.get("api_key_ref"),
        Some(Value::String(_)) | Some(Value::Null)
    ) {
        return false;
    }
    let backends = [
        "openai-codex",
        "anyai",
        "anthropic",
        "openai",
        "openrouter",
        "gemini",
        "deepseek",
        "groq",
        "together",
        "vllm",
        "llamacpp",
        "ollama",
        "generic",
    ];
    if obj
        .get("backend")
        .is_some_and(|v| !v.as_str().is_some_and(|s| backends.contains(&s)))
    {
        return false;
    }
    let profiles = [
        "claude",
        "gpt",
        "gemini",
        "deepseek-v4",
        "deepseek-r1",
        "deepseek",
        "qwen3",
        "llama",
        "gemma",
        "mistral",
        "kimi-k3",
        "kimi",
        "glm-5-flash",
        "glm-5",
        "glm",
        "mercury",
        "fenced",
        "xml",
        "generic",
    ];
    if obj
        .get("profile")
        .is_some_and(|v| !v.as_str().is_some_and(|s| profiles.contains(&s)))
    {
        return false;
    }
    if obj.get("stream").is_some_and(|v| !v.is_boolean()) {
        return false;
    }
    if let Some(timeout) = obj.get("timeout") {
        let valid_seconds = |v: &Value| v.as_f64().is_some_and(|n| n > 0.0 && n <= 86_400.0);
        if let Some(t) = timeout.as_object() {
            if t.keys().any(|k| !["idle", "total"].contains(&k.as_str()))
                || t.values().any(|v| !valid_seconds(v))
            {
                return false;
            }
        } else if !valid_seconds(timeout) {
            return false;
        }
    }
    if obj.get("options").is_some_and(|v| !v.is_object()) {
        return false;
    }
    // The guest refuses malformed variants of its reserved prefix too.
    if row["api_key_ref"].as_str().is_some_and(|r| {
        r.starts_with(crate::local_grant::CHATGPT_REF) && r != crate::local_grant::CHATGPT_REF
    }) {
        return false;
    }
    if obj.get("traits").is_some_and(|v| !valid_traits(v)) {
        return false;
    }
    if !uses_chatgpt(row) {
        return true;
    }
    if row["provider"] != "openai-responses"
        || row["backend"] != "openai-codex"
        || base != "https://chatgpt.com/backend-api/codex"
        || row["api_key_ref"] != crate::local_grant::CHATGPT_REF
        || obj.get("stream").is_some_and(|v| v != &json!(true))
    {
        return false;
    }
    if !row["model"].as_str().is_some_and(|m| {
        m.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-./:".contains(&b))
    }) {
        return false;
    }
    if !obj.contains_key("profile")
        && row["model"]
            .as_str()
            .is_some_and(|m| m.to_lowercase().contains("gemma"))
    {
        return false;
    }
    // Fenced/XML profiles cannot carry the native function loop on this backend.
    if row["profile"]
        .as_str()
        .is_some_and(|p| ["fenced", "xml", "gemma"].contains(&p))
    {
        return false;
    }
    let traits = &row["traits"];
    if traits.get("tool_mode").is_some_and(|v| v != "native")
        || traits["web_search"] == true
        || traits.get("pdf_input").is_some_and(|v| v != "none")
        || traits
            .get("sampling")
            .is_some_and(|v| v.as_object().is_none_or(|m| !m.is_empty()))
        || traits["cache"] == "markers"
    {
        return false;
    }
    obj.get("options")
        .and_then(Value::as_object)
        .is_none_or(|options| {
            options.iter().all(|(key, value)| match key.as_str() {
                "reasoning" => valid_option(
                    value,
                    "effort",
                    &["none", "minimal", "low", "medium", "high", "xhigh"],
                ),
                "text" => valid_option(value, "verbosity", &["low", "medium", "high"]),
                _ => false,
            })
        })
}

fn valid_traits(value: &Value) -> bool {
    value.as_object().is_some_and(|m| {
        m.iter().all(|(key, v)| {
            let choices: &[&str] = match key.as_str() {
                "system_role" => &["native", "first_user"],
                "tool_mode" => &["native", "fenced", "xml"],
                "reasoning" => &["none", "advisory", "roundtrip"],
                "thinking" => &["default", "on", "off"],
                "cache" => &["auto", "markers", "none"],
                "prompt_style" => &["full", "compact"],
                "instructions_at" => &["system", "last_user"],
                "pdf_input" => &["none", "file", "image_url"],
                "vision" | "signed_tool_calls" | "web_search" => return v.is_boolean(),
                "context_window" | "max_output" | "malformed_retries" => {
                    return v.is_i64() || v.is_u64()
                }
                "sampling" => return v.is_object(),
                _ => return false,
            };
            v.as_str().is_some_and(|s| choices.contains(&s))
        })
    })
}
fn target_identity(tiers: &BTreeMap<String, Value>, search: &BTreeMap<String, Value>) -> Value {
    if search.is_empty() {
        json!(tiers)
    } else {
        json!({"tiers": tiers, "search": search})
    }
}
fn compatible_search(name: &str, row: &Value) -> bool {
    if row.is_null() {
        return true;
    }
    let providers: &[&str] = match name {
        "websearch" => &["gemini", "openai-compat"],
        "deepresearch" => &["gemini"],
        _ => return false,
    };
    if uses_chatgpt(row)
        || !row["provider"]
            .as_str()
            .is_some_and(|p| providers.contains(&p))
    {
        return false;
    }
    let mut value = row.clone();
    value["provider"] = json!("openai-compat");
    compatible(&value)
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ConfigurationVerdict {
    pub configuration: &'static str,
    pub apply_id: Option<String>,
    pub config_id: Option<String>,
    pub chatgpt: bool,
}

pub fn verdict(rows: &[Value], overrides: &BTreeMap<String, Value>) -> ConfigurationVerdict {
    let mut out = ConfigurationVerdict {
        configuration: "unknown",
        apply_id: None,
        config_id: None,
        chatgpt: false,
    };
    let Some(raw) = raw_values(rows) else {
        return out;
    };
    let effective: BTreeMap<_, _> = TIERS
        .iter()
        .filter_map(|tier| {
            let key = format!("llm.tier.{tier}");
            overrides
                .get(&key)
                .or(raw.get(&key))
                .map(|value| ((*tier).to_owned(), value.clone()))
        })
        .collect();
    out.chatgpt = effective.values().any(uses_chatgpt);
    out.config_id = (effective.len() == 3)
        .then(|| config_id(&json!(effective)))
        .flatten();
    let Some(abandoned) = abandoned_ids(raw.get(ABANDONED_KEY)) else {
        return out;
    };
    let Some(value) = raw.get(TARGET_KEY) else {
        out.configuration = if out.chatgpt { "unknown" } else { "ordinary" };
        return out;
    };
    let Some(target) = parse_target(value) else {
        return out;
    };
    let apply_hash = hex::encode(Sha256::digest(target.apply_id.as_bytes()));
    if abandoned.contains(apply_hash.as_str()) {
        out.configuration = if out.chatgpt { "unknown" } else { "ordinary" };
        return out;
    }
    let effective_search: BTreeMap<_, _> = target
        .search
        .keys()
        .filter_map(|name| {
            let key = format!("search.provider.{name}");
            overrides
                .get(&key)
                .or(raw.get(&key))
                .map(|v| (name.clone(), v.clone()))
        })
        .collect();
    out.chatgpt |= effective_search.values().any(uses_chatgpt);
    out.config_id = (effective.len() == 3 && effective_search.len() == target.search.len())
        .then(|| config_id(&target_identity(&effective, &effective_search)))
        .flatten();
    out.apply_id = Some(target.apply_id);
    out.configuration = if TIERS.iter().all(|t| {
        let key = format!("llm.tier.{t}");
        raw.get(&key).is_some_and(|v| same(v, &target.tiers[*t]))
            && effective
                .get(*t)
                .is_some_and(|v| same(v, &target.tiers[*t]))
    }) && target.search.iter().all(|(name, v)| {
        let key = format!("search.provider.{name}");
        raw.get(&key).is_some_and(|r| same(r, v))
            && effective_search.get(name).is_some_and(|r| same(r, v))
    }) {
        if effective.values().all(compatible)
            && effective_search
                .iter()
                .all(|(name, row)| compatible_search(name, row))
            && out.config_id.is_some()
        {
            "matched"
        } else {
            "unsupported"
        }
    } else {
        "incomplete"
    };
    out
}

#[cfg(test)]
mod tests;
