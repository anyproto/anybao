//! Trace format v2 (ADR-001) — the Rust twin of anyrt/trace.py. The
//! contract is STRUCTURAL parity with the Python reference host: same
//! record kinds/fields, same canonical JSON (sorted keys, compact
//! separators — serde_json's default Map is a BTreeMap, matching
//! Python's sort_keys=True), same input_key hash domain.

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::Path;

pub const SCHEMA: i64 = 2;
pub const BLOB_THRESHOLD: usize = 64 * 1024;

pub fn canonical_json(value: &Value) -> String {
    serde_json::to_string(value).expect("json values always serialize")
}

pub fn input_key(effect: &str, canonical_input: &Value) -> String {
    let mut h = Sha256::new();
    h.update(effect.as_bytes());
    h.update(b"\x00"); // hash-domain separator only, never serialized
    h.update(canonical_json(canonical_input).as_bytes());
    format!("sha256:{}", hex::encode(h.finalize()))
}

pub struct TraceWriter {
    pub records: Vec<Value>,
    pub blobs: Vec<(String, String)>,
    seq: i64,
}

impl TraceWriter {
    pub fn new(run: Value) -> Self {
        let mut w = TraceWriter {
            records: Vec::new(),
            blobs: Vec::new(),
            seq: 0,
        };
        w.records
            .push(json!({"kind": "header", "schema": SCHEMA, "run": run}));
        w
    }

    pub fn run_id(&self) -> String {
        self.records[0]["run"]["id"]
            .as_str()
            .unwrap_or_default()
            .to_string()
    }

    fn next_seq(&mut self) -> i64 {
        self.seq += 1;
        self.seq
    }

    fn spill(&mut self, value: Value) -> Value {
        let text = canonical_json(&value);
        if text.len() <= BLOB_THRESHOLD {
            return value;
        }
        let mut h = Sha256::new();
        h.update(text.as_bytes());
        let hash = format!("sha256:{}", hex::encode(h.finalize()));
        let bytes = text.len();
        self.blobs.push((hash.clone(), text));
        json!({"__blob": hash, "bytes": bytes})
    }

    #[allow(clippy::too_many_arguments)]
    pub fn effect(
        &mut self,
        effect: &str,
        cell: Option<&str>,
        input: Value,
        key: &str,
        output: Option<Value>,
        error: Option<Value>,
        meta: Value,
        span: Option<&str>,
    ) -> i64 {
        let seq = self.next_seq();
        let mut rec = Map::new();
        rec.insert("kind".into(), json!("effect"));
        rec.insert("seq".into(), json!(seq));
        rec.insert("effect".into(), json!(effect));
        rec.insert("cell".into(), json!(cell));
        let spilled = self.spill(input); // key was computed pre-spill
        rec.insert("input".into(), spilled);
        rec.insert("key".into(), json!(key));
        let out = match output {
            Some(v) => self.spill(v),
            None => Value::Null,
        };
        rec.insert("output".into(), out);
        rec.insert("error".into(), error.unwrap_or(Value::Null));
        rec.insert("meta".into(), meta);
        if let Some(s) = span {
            // stamp only inside spans — span-free traces stay byte-stable
            rec.insert("span".into(), json!(s));
        }
        self.records.push(Value::Object(rec));
        seq
    }

    pub fn span_begin(
        &mut self,
        span: &str,
        name: &str,
        cell: Option<&str>,
        input: Value,
        key: &str,
        parent: Option<&str>,
    ) {
        let seq = self.next_seq();
        let spilled = self.spill(input);
        self.records.push(json!({
            "kind": "span", "seq": seq, "phase": "begin", "span": span,
            "parent": parent, "name": name, "cell": cell,
            "input": spilled, "key": key,
        }));
    }

    #[allow(clippy::too_many_arguments)]
    pub fn span_end(
        &mut self,
        span: &str,
        name: &str,
        cell: Option<&str>,
        ok: bool,
        output: Option<Value>,
        error: Option<Value>,
        meta: Value,
    ) {
        let seq = self.next_seq();
        let out = match output {
            Some(v) => self.spill(v),
            None => Value::Null,
        };
        self.records.push(json!({
            "kind": "span", "seq": seq, "phase": "end", "span": span,
            "name": name, "cell": cell, "ok": ok,
            "output": out, "error": error.unwrap_or(Value::Null), "meta": meta,
        }));
    }

    pub fn cell(
        &mut self,
        cell: &str,
        ok: bool,
        error: Option<Value>,
        interrupted: bool,
        metrics: Value,
    ) {
        let seq = self.next_seq();
        self.records.push(json!({
            "kind": "cell", "seq": seq, "cell": cell, "ok": ok,
            "error": error.unwrap_or(Value::Null),
            "interrupted": interrupted, "metrics": metrics,
        }));
    }

    pub fn dump(&self, path: &Path) -> anyhow::Result<()> {
        let mut text = String::new();
        for r in &self.records {
            text.push_str(&canonical_json(r));
            text.push('\n');
        }
        fs::write(path, text)?;
        if !self.blobs.is_empty() {
            let mut sorted = self.blobs.clone();
            sorted.sort();
            let side: String = sorted
                .iter()
                .map(|(h, t)| canonical_json(&json!({"hash": h, "data": t})) + "\n")
                .collect();
            fs::write(path.with_extension("jsonl.blobs"), side)?;
        }
        Ok(())
    }
}
