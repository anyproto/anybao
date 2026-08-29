//! Trace format v2 (ADR-001) — the Rust twin of anyrt/trace.py. The
//! contract is STRUCTURAL parity with the Python reference host: same
//! record kinds/fields, same canonical JSON (sorted keys, compact
//! separators — serde_json's default Map is a BTreeMap, matching
//! Python's sort_keys=True), same input_key hash domain.

use crate::tracestore::{TraceSink, TraceStore};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

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
    /// host wall-clock at construction — the run summary's
    /// `startedAt` (records themselves stay time-free, ADR-001 §2)
    pub started_at: f64,
    /// the trigger that fired this run — the summary's `triggerId`
    /// (ADR-023 §8); None = chat/control/embedder run
    pub trigger: Option<String>,
    seq: i64,
    /// Streaming sink (ADR-001 §1 revision 2026-07-08): records append
    /// to the store at commit time so the run is readable in-flight
    /// and survives a crash. `None` = buffered (tests, replay
    /// fixtures) or a degraded stream — `dump()` then writes it whole.
    /// The sink is whatever the [`TraceStore`] opened (ADR-001 §8);
    /// the writer never touches storage directly.
    sink: Option<Box<dyn TraceSink>>,
    streamed: bool,
}

impl TraceWriter {
    pub fn new(run: Value) -> Self {
        let mut w = TraceWriter {
            records: Vec::new(),
            blobs: Vec::new(),
            started_at: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0),
            trigger: None,
            seq: 0,
            sink: None,
            streamed: false,
        };
        w.push(json!({"kind": "header", "schema": SCHEMA, "run": run}));
        w
    }

    /// Start streaming into `store`: everything committed so far (the
    /// header) is written immediately, every later record appends. A
    /// write failure degrades back to buffered mode — `dump()` at run
    /// end is the fallback rewrite, so no records are ever lost.
    pub fn stream_to(&mut self, store: &dyn TraceStore) -> anyhow::Result<()> {
        let mut sink = store.open_sink(&self.run_id())?;
        for r in &self.records {
            sink.append(r)?;
        }
        self.sink = Some(sink);
        self.streamed = true;
        Ok(())
    }

    fn push(&mut self, rec: Value) {
        if let Some(sink) = self.sink.as_mut() {
            if sink.append(&rec).is_err() {
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
        if let Some(sink) = self.sink.as_mut() {
            if sink.append_blob(&hash, &text).is_err() {
                tracing::warn!("trace blob write failed; buffering until dump");
                self.sink = None;
            }
        }
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

    /// Land the run in `store`. A healthy stream into the same store
    /// already wrote every record, so only the run's `finish` (flush +
    /// summary, ADR-023 §3) runs then; otherwise (buffered, or a stream
    /// that degraded) the whole log is written first. Returns the run
    /// summary (ADR-023 §1) for the host to publish.
    pub fn dump(&mut self, store: &dyn TraceStore) -> anyhow::Result<Value> {
        let run = self.run_id();
        if !(self.sink.is_some() && self.streamed) {
            store.write_run(&run, &self.records, &self.blobs)?;
        }
        if let Some(mut sink) = self.sink.take() {
            sink.close()?;
        }
        store.finish(
            &run,
            &self.records,
            &self.blobs,
            self.started_at,
            self.trigger.as_deref(),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tracestore::FileTraceStore;
    use std::fs;

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
        let store = FileTraceStore::new(dir.path());
        let path = store.path_of("run_s");
        let mut w = TraceWriter::new(json!({"id": "run_s", "program": "p"}));
        w.stream_to(&store).unwrap();
        // header lands before any effect — the file exists at run start
        assert_eq!(fs::read_to_string(&path).unwrap().lines().count(), 1);
        effect_rec(&mut w, json!({"ok": 1}));
        // record visible in-flight, not just at dump time
        assert_eq!(fs::read_to_string(&path).unwrap().lines().count(), 2);
        effect_rec(&mut w, json!({"ok": 2}));
        w.cell("main", true, None, false, json!({"fuel_used": 1}));

        // dump on the streamed store is a no-op; bytes equal a buffered twin
        w.dump(&store).unwrap();
        let mut twin = TraceWriter::new(json!({"id": "run_twin", "program": "p"}));
        effect_rec(&mut twin, json!({"ok": 1}));
        effect_rec(&mut twin, json!({"ok": 2}));
        twin.cell("main", true, None, false, json!({"fuel_used": 1}));
        twin.dump(&store).unwrap();
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            fs::read_to_string(store.path_of("run_twin"))
                .unwrap()
                .replace("run_twin", "run_s")
        );
    }

    #[test]
    fn streaming_writes_blob_sidecar_at_spill_time() {
        let dir = tempfile::tempdir().unwrap();
        let store = FileTraceStore::new(dir.path());
        let path = store.path_of("run_b");
        let mut w = TraceWriter::new(json!({"id": "run_b", "program": "p"}));
        w.stream_to(&store).unwrap();
        let big = json!({"data": "z".repeat(BLOB_THRESHOLD + 1)});
        effect_rec(&mut w, big);
        let side = fs::read_to_string(path.with_extension("jsonl.blobs")).unwrap();
        assert_eq!(side.lines().count(), 1);
        let entry: Value = serde_json::from_str(side.lines().next().unwrap()).unwrap();
        assert_eq!(entry["hash"], json!(w.blobs[0].0));
    }
}
