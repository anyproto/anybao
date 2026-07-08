//! The broker + syscall surface (ADR-002 thin host) — record-mode
//! pipeline: classify → key → execute → record. Strict/mock replay is
//! the Python reference host's department until the golden-parity gate
//! promotes it here; caps run permissive (grant policy is data the two
//! hosts will share).

use crate::routes::Classifier;
use crate::trace::{input_key, TraceWriter};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, VecDeque};
use std::path::PathBuf;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

pub struct EffectFailure {
    pub type_: String,
    pub message: String,
}

struct SpanFrame {
    id: String,
    name: String,
    t0: Instant,
    effects: u64,
    mutations: u64,
}

pub struct Broker {
    pub writer: TraceWriter,
    pub current_cell: Option<String>,
    pub config: BTreeMap<String, Value>,
    pub secrets: BTreeMap<String, String>,
    pub env: BTreeMap<String, String>,
    pub programs_dir: PathBuf,
    pub mailbox: VecDeque<Value>,
    pub classifier: Classifier,
    span_stack: Vec<SpanFrame>,
    span_n: u64,
    resolve_cache: BTreeMap<String, Value>,
}

impl Broker {
    pub fn new(
        writer: TraceWriter,
        config: BTreeMap<String, Value>,
        secrets: BTreeMap<String, String>,
        programs_dir: PathBuf,
        classifier: Classifier,
    ) -> Self {
        Broker {
            writer,
            current_cell: None,
            config,
            secrets,
            env: BTreeMap::new(),
            programs_dir,
            mailbox: VecDeque::new(),
            classifier,
            span_stack: Vec::new(),
            span_n: 0,
            resolve_cache: BTreeMap::new(),
        }
    }

    pub fn span_begin(&mut self, name: &str, input: Value) -> String {
        self.span_n += 1;
        let sid = format!("s{}", self.span_n); // execution order => deterministic
        let key = input_key(name, &input);
        let parent = self.span_stack.last().map(|s| s.id.clone());
        let cell = self.current_cell.clone();
        self.writer
            .span_begin(&sid, name, cell.as_deref(), input, &key, parent.as_deref());
        self.span_stack.push(SpanFrame {
            id: sid.clone(),
            name: name.into(),
            t0: Instant::now(),
            effects: 0,
            mutations: 0,
        });
        sid
    }

    pub fn span_end(
        &mut self,
        ok: bool,
        output: Option<Value>,
        error: Option<Value>,
    ) -> Result<(), EffectFailure> {
        let top = self.span_stack.pop().ok_or(EffectFailure {
            type_: "no_open_span".into(),
            message: "span.end without span.begin".into(),
        })?;
        let cell = self.current_cell.clone();
        let meta = json!({
            "durMs": top.t0.elapsed().as_millis() as i64,
            "effects": top.effects, "mutations": top.mutations,
        });
        self.writer
            .span_end(&top.id, &top.name, cell.as_deref(), ok, output, error, meta);
        Ok(())
    }

    pub fn cell_done(
        &mut self,
        cell: &str,
        ok: bool,
        error: Option<Value>,
        interrupted: bool,
        metrics: Value,
    ) {
        while !self.span_stack.is_empty() {
            // a trapped cell skips guest finally — force-close (ADR-001 §4c)
            let err = json!({"type": "unclosed_span",
                             "message": format!("cell {cell} ended with span open")});
            let _ = self.span_end(false, None, Some(err));
        }
        self.writer.cell(cell, ok, error, interrupted, metrics);
    }

    pub fn call(&mut self, name: &str, payload: Value) -> Result<Value, EffectFailure> {
        let class = self.classify(name, &payload);
        let canonical = payload.clone();
        let key = input_key(name, &canonical);
        let span = self.span_stack.last().map(|s| s.id.clone());
        for s in self.span_stack.iter_mut() {
            s.effects += 1;
            if class == "mutate" {
                s.mutations += 1;
            }
        }

        let t0 = Instant::now();
        let result = self.execute(name, &payload);
        let dur_ms = t0.elapsed().as_millis() as i64;
        let mut meta = Map::new();
        meta.insert("durMs".into(), json!(dur_ms));
        meta.insert("mocked".into(), json!(false));
        meta.insert("class".into(), json!(class));
        let cell = self.current_cell.clone();
        match result {
            Ok(output) => {
                self.writer.effect(
                    name,
                    cell.as_deref(),
                    canonical,
                    &key,
                    Some(output.clone()),
                    None,
                    Value::Object(meta),
                    span.as_deref(),
                );
                Ok(output)
            }
            Err(e) => {
                let err = json!({"type": e.type_, "message": e.message});
                self.writer.effect(
                    name,
                    cell.as_deref(),
                    canonical,
                    &key,
                    None,
                    Some(err),
                    Value::Object(meta),
                    span.as_deref(),
                );
                Err(e)
            }
        }
    }

    fn classify(&self, name: &str, payload: &Value) -> &'static str {
        if let Some(verb) = name.strip_prefix("http.") {
            let url = payload.get("url").and_then(|u| u.as_str()).unwrap_or("");
            return self.classifier.kind(&verb.to_uppercase(), url);
        }
        "read" // every other syscall is a read
    }

    // --- the syscall implementations -------------------------------------
    fn execute(&mut self, name: &str, payload: &Value) -> Result<Value, EffectFailure> {
        match name {
            n if n.starts_with("http.") => self.sys_http(n, payload),
            "config.get" => self.sys_config_get(payload),
            "mailbox.drain" => {
                let items: Vec<Value> = self.mailbox.drain(..).collect();
                Ok(json!({"items": items}))
            }
            "time.now" => Ok(json!({"epoch": SystemTime::now()
                .duration_since(UNIX_EPOCH).unwrap().as_secs_f64()})),
            "random.random" => {
                // secrets-grade uniform in [0,1), mirroring the reference host
                let mut buf = [0u8; 8];
                getrandom(&mut buf);
                let bits = u64::from_le_bytes(buf) >> 11; // 53 bits
                Ok(json!({"value": bits as f64 / (1u64 << 53) as f64}))
            }
            "uuid4" => Ok(json!({"hex": uuid::Uuid::new_v4().to_string()})),
            "sleep" => {
                let secs = payload
                    .get("seconds")
                    .and_then(|s| s.as_f64())
                    .unwrap_or(0.0);
                std::thread::sleep(std::time::Duration::from_secs_f64(secs.min(300.0)));
                Ok(json!({"slept": payload.get("seconds").cloned().unwrap_or(Value::Null)}))
            }
            "env.get" => {
                let key = payload.get("name").and_then(|n| n.as_str()).unwrap_or("");
                let present = self.env.contains_key(key);
                Ok(json!({"present": present, "value": self.env.get(key)}))
            }
            "module.resolve" => self.sys_module_resolve(payload),
            "batch" => self.sys_batch(payload),
            "trace.effects_of" => self.sys_effects_of(payload),
            "trace.effect_get" => self.sys_effect_get(payload),
            "kernel.boot" => Ok(payload.clone()), // pins echo into the record
            other => Err(EffectFailure {
                type_: "unknown_effect".into(),
                message: format!("no such effect: {other}"),
            }),
        }
    }

    fn sys_http(&self, name: &str, payload: &Value) -> Result<Value, EffectFailure> {
        let verb = name.strip_prefix("http.").unwrap().to_uppercase();
        let mut url = payload
            .get("url")
            .and_then(|u| u.as_str())
            .ok_or(EffectFailure {
                type_: "TypeError".into(),
                message: "http call without url".into(),
            })?
            .to_string();
        if let Some(params) = payload.get("params").and_then(|p| p.as_object()) {
            let qs: Vec<String> = params
                .iter()
                .map(|(k, v)| {
                    let val = v
                        .as_str()
                        .map(str::to_string)
                        .unwrap_or_else(|| crate::trace::canonical_json(v));
                    format!("{}={}", urlencode(k), urlencode(&val))
                })
                .collect();
            url.push(if url.contains('?') { '&' } else { '?' });
            url.push_str(&qs.join("&"));
        }
        let timeout = payload
            .get("timeout")
            .and_then(|t| t.as_f64())
            .unwrap_or(180.0);
        let mut req =
            ureq::request(&verb, &url).timeout(std::time::Duration::from_secs_f64(timeout));
        if let Some(headers) = payload.get("headers").and_then(|h| h.as_object()) {
            for (k, v) in headers {
                if let Some(s) = v.as_str() {
                    req = req.set(k, s);
                }
            }
        }
        // named-credential injection: value resolved AFTER recording
        if let Some(cred) = payload.get("credential").and_then(|c| c.as_object()) {
            let r = cred.get("ref").and_then(|v| v.as_str()).unwrap_or("");
            let header = cred.get("header").and_then(|v| v.as_str()).unwrap_or("");
            let prefix = cred.get("prefix").and_then(|v| v.as_str()).unwrap_or("");
            let secret = self.secrets.get(r).ok_or(EffectFailure {
                type_: "RuntimeError".into(),
                message: format!("no secret for credential ref {r:?}"),
            })?;
            req = req.set(header, &format!("{prefix}{secret}"));
        }
        let resp = if let Some(body) = payload.get("json").filter(|v| !v.is_null()) {
            req.set("Content-Type", "application/json")
                .send_string(&crate::trace::canonical_json(body))
        } else if let Some(body) = payload.get("body").and_then(|b| b.as_str()) {
            req.send_string(body)
        } else {
            req.call()
        };
        let resp = match resp {
            Ok(r) => r,
            Err(ureq::Error::Status(_, r)) => r, // non-2xx is data, not error
            Err(e) => {
                return Err(EffectFailure {
                    type_: "URLError".into(),
                    message: e.to_string(),
                })
            }
        };
        let status = resp.status();
        let headers: Map<String, Value> = resp
            .headers_names()
            .iter()
            .filter_map(|h| resp.header(h).map(|v| (h.to_lowercase(), json!(v))))
            .collect();
        let body = resp.into_string().map_err(|e| EffectFailure {
            type_: "URLError".into(),
            message: e.to_string(),
        })?;
        Ok(json!({"status": status, "headers": headers, "body": body}))
    }

    fn sys_config_get(&self, payload: &Value) -> Result<Value, EffectFailure> {
        let key = payload.get("key").and_then(|k| k.as_str()).unwrap_or("");
        if self.secrets.contains_key(key) {
            return Err(EffectFailure {
                type_: "ConfigError".into(),
                message: format!("{key:?} is a secret — not readable from cells"),
            });
        }
        match self.config.get(key) {
            Some(v) => Ok(json!({"value": v})),
            None => Err(EffectFailure {
                type_: "ConfigError".into(),
                message: format!("no config value for {key:?}"),
            }),
        }
    }

    fn sys_module_resolve(&mut self, payload: &Value) -> Result<Value, EffectFailure> {
        let spec = payload
            .get("spec")
            .and_then(|s| s.as_str())
            .ok_or(EffectFailure {
                type_: "TypeError".into(),
                message: "module.resolve without spec".into(),
            })?;
        let cache_state = if self.resolve_cache.contains_key(spec) {
            "hit"
        } else {
            "miss"
        };
        if let Some(cached) = self.resolve_cache.get(spec) {
            let mut out = cached.clone();
            out["cache"] = json!(cache_state);
            return Ok(out);
        }
        let path = self.programs_dir.join(format!("{spec}.py"));
        let source = std::fs::read_to_string(&path).map_err(|_| EffectFailure {
            type_: "KeyError".into(),
            message: format!("program not found: {spec} ({})", path.display()),
        })?;
        let mut h = Sha256::new();
        h.update(source.as_bytes());
        let out = json!({
            "spaceId": "local", "objectId": spec, "marker": 0,
            "sourceHash": format!("sha256:{}", hex::encode(h.finalize())),
            "source": source, "cache": cache_state,
        });
        self.resolve_cache.insert(spec.into(), out.clone());
        Ok(out)
    }

    fn sys_batch(&mut self, payload: &Value) -> Result<Value, EffectFailure> {
        let name = payload
            .get("name")
            .and_then(|n| n.as_str())
            .unwrap_or("")
            .to_string();
        let payloads = payload
            .get("payloads")
            .and_then(|p| p.as_array())
            .cloned()
            .unwrap_or_default();
        let results: Vec<Value> = payloads
            .into_iter()
            .map(|p| match self.call(&name, p) {
                Ok(v) => v,
                Err(e) => json!({"error": {"type": e.type_, "message": e.message}}),
            })
            .collect();
        Ok(json!({"results": results}))
    }

    fn sys_effects_of(&self, payload: &Value) -> Result<Value, EffectFailure> {
        let cell = payload.get("cell").and_then(|c| c.as_str());
        let span = payload.get("span").and_then(|s| s.as_str());
        let out: Vec<Value> = self
            .writer
            .records
            .iter()
            .filter(|r| {
                r["kind"] == "effect"
                    && cell.is_none_or(|c| r["cell"] == c)
                    && span.is_none_or(|s| r.get("span").map(|v| v == s).unwrap_or(false))
            })
            .map(|r| {
                json!({
                    "seq": r["seq"], "effect": r["effect"],
                    "class": r["meta"].get("class").cloned().unwrap_or(Value::Null),
                    "mocked": r["meta"].get("mocked").cloned().unwrap_or(Value::Null),
                    "error": r["error"].get("type").cloned().unwrap_or(Value::Null),
                    "span": r.get("span").cloned().unwrap_or(Value::Null),
                })
            })
            .collect();
        Ok(json!({"records": out}))
    }

    fn sys_effect_get(&self, payload: &Value) -> Result<Value, EffectFailure> {
        let seq = payload.get("seq").and_then(|s| s.as_i64()).unwrap_or(-1);
        for r in &self.writer.records {
            if r["kind"] == "effect" && r["seq"] == seq {
                return Ok(r.clone());
            }
        }
        Err(EffectFailure {
            type_: "KeyError".into(),
            message: format!("no effect record with seq {seq}"),
        })
    }
}

fn urlencode(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

fn getrandom(buf: &mut [u8]) {
    // uuid's rng is already OS-backed; reuse it for the random syscall
    for chunk in buf.chunks_mut(16) {
        let bytes = *uuid::Uuid::new_v4().as_bytes();
        chunk.copy_from_slice(&bytes[..chunk.len()]);
    }
}
