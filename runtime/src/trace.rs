//! Trace format v2 (ADR-001) — the Rust twin of anyrt/trace.py. The
//! contract is STRUCTURAL parity with the Python reference host: same
//! record kinds/fields, same canonical JSON (sorted keys, compact
//! separators — serde_json's default Map is a BTreeMap, matching
//! Python's sort_keys=True), same input_key hash domain.

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

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
    /// Streaming sink (ADR-001 §1 revision 2026-07-08): records append
    /// to the trace file at commit time so the file is tail-able
    /// in-flight and survives a crashed run. `None` = buffered (tests,
    /// replay fixtures) or a degraded stream — `dump()` then rewrites.
    sink: Option<fs::File>,
    path: Option<PathBuf>,
}

impl TraceWriter {
    pub fn new(run: Value) -> Self {
        let mut w = TraceWriter {
            records: Vec::new(),
            blobs: Vec::new(),
            seq: 0,
            sink: None,
            path: None,
        };
        w.push(json!({"kind": "header", "schema": SCHEMA, "run": run}));
        w
    }

    /// Start streaming to `path`: everything committed so far (the
    /// header) is written immediately, every later record appends. A
    /// write failure degrades back to buffered mode — `dump()` at run
    /// end is the fallback rewrite, so no records are ever lost.
    pub fn stream_to(&mut self, path: &Path) -> anyhow::Result<()> {
        if let Some(dir) = path.parent() {
            fs::create_dir_all(dir)?;
        }
        let mut f = fs::File::create(path)?;
        for r in &self.records {
            writeln!(f, "{}", canonical_json(r))?;
        }
        f.flush()?;
        self.sink = Some(f);
        self.path = Some(path.to_path_buf());
        Ok(())
    }

    fn push(&mut self, rec: Value) {
        if let Some(f) = self.sink.as_mut() {
            let line = canonical_json(&rec);
            if writeln!(f, "{line}").and_then(|_| f.flush()).is_err() {
                tracing::warn!("trace stream write failed; buffering until dump");
                self.sink = None;
            }
        }
        self.records.push(rec);
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
        if self.sink.is_some() {
            // sidecar appends in spill order — consumers key by hash
            if let Some(p) = self.blob_path() {
                let line = canonical_json(&json!({"hash": hash, "data": text})) + "\n";
                let _ = fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(p)
                    .and_then(|mut f| f.write_all(line.as_bytes()));
            }
        }
        self.blobs.push((hash.clone(), text));
        json!({"__blob": hash, "bytes": bytes})
    }

    fn blob_path(&self) -> Option<PathBuf> {
        self.path.as_ref().map(|p| p.with_extension("jsonl.blobs"))
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
        self.push(Value::Object(rec));
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
        self.push(json!({
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
        self.push(json!({
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
        self.push(json!({
            "kind": "cell", "seq": seq, "cell": cell, "ok": ok,
            "error": error.unwrap_or(Value::Null),
            "interrupted": interrupted, "metrics": metrics,
        }));
    }

    pub fn dump(&self, path: &Path) -> anyhow::Result<()> {
        // healthy stream to the same path already wrote every byte
        if self.sink.is_some() && self.path.as_deref() == Some(path) {
            return Ok(());
        }
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

#[cfg(test)]
mod tests {
    use super::*;

    fn effect_rec(w: &mut TraceWriter, out: Value) {
        let key = input_key("x.y", &json!({"a": 1}));
        w.effect(
            "x.y",
            Some("main"),
            json!({"a": 1}),
            &key,
            Some(out),
            None,
            json!({"class": "read", "durMs": 0}),
            None,
        );
    }

    #[test]
    fn streaming_appends_per_record_and_matches_dump() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("t.jsonl");
        let mut w = TraceWriter::new(json!({"id": "run_s", "program": "p"}));
        w.stream_to(&path).unwrap();
        // header lands before any effect — the file exists at run start
        assert_eq!(fs::read_to_string(&path).unwrap().lines().count(), 1);
        effect_rec(&mut w, json!({"ok": 1}));
        // record visible in-flight, not just at dump time
        assert_eq!(fs::read_to_string(&path).unwrap().lines().count(), 2);
        effect_rec(&mut w, json!({"ok": 2}));
        w.cell("main", true, None, false, json!({"fuel_used": 1}));

        // dump on the streamed path is a no-op; bytes equal a buffered twin
        w.dump(&path).unwrap();
        let mut twin = TraceWriter::new(json!({"id": "run_s", "program": "p"}));
        effect_rec(&mut twin, json!({"ok": 1}));
        effect_rec(&mut twin, json!({"ok": 2}));
        twin.cell("main", true, None, false, json!({"fuel_used": 1}));
        let twin_path = dir.path().join("twin.jsonl");
        twin.dump(&twin_path).unwrap();
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            fs::read_to_string(&twin_path).unwrap()
        );
    }

    #[test]
    fn streaming_writes_blob_sidecar_at_spill_time() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("t.jsonl");
        let mut w = TraceWriter::new(json!({"id": "run_b", "program": "p"}));
        w.stream_to(&path).unwrap();
        let big = json!({"data": "z".repeat(BLOB_THRESHOLD + 1)});
        effect_rec(&mut w, big);
        let side = fs::read_to_string(path.with_extension("jsonl.blobs")).unwrap();
        assert_eq!(side.lines().count(), 1);
        let entry: Value = serde_json::from_str(side.lines().next().unwrap()).unwrap();
        assert_eq!(entry["hash"], json!(w.blobs[0].0));
    }
}
