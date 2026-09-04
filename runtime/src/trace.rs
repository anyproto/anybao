//! Trace format v2 (ADR-001) — the Rust twin of anyrt/trace.py. The
//! contract is STRUCTURAL parity with the Python reference host: same
//! record kinds/fields, same canonical JSON (sorted keys, compact
//! separators — serde_json's default Map is a BTreeMap, matching
//! Python's sort_keys=True), same input_key hash domain.

use crate::blob::{self, BlobDir, JSON_MIME, RAW_TEXT_CUTOFF};
use crate::tracestore::{TraceSink, TraceStore};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

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
    /// The raw blob directory (ADR-026 §1) — taken from the store at
    /// `stream_to`; None = buffered writer, every spill stays text.
    pub blob_dir: Option<BlobDir>,
    /// Text spills that went to the directory instead (over
    /// `RAW_TEXT_CUTOFF`, ADR-026 §2): kept in `blobs` for the summary,
    /// never sent to the text-blob store.
    raw_text: BTreeSet<String>,
    /// Text spills the sink refused (ADR-026 §2: the sink stays; these
    /// get one more try at `dump`).
    failed_blobs: Vec<(String, String)>,
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
            blob_dir: None,
            raw_text: BTreeSet::new(),
            failed_blobs: Vec::new(),
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
        self.blob_dir = store.blob_dir().cloned();
        Ok(())
    }

    /// Bytes → the directory → a raw ref (ADR-026 §1). Without a
    /// directory (buffered writer) the ref is still minted so the record
    /// keeps its shape; the warning names the run and hash.
    pub fn put_raw(&self, bytes: &[u8], mime: &str) -> Value {
        let hash = blob::hash_of(bytes);
        match &self.blob_dir {
            Some(dir) => match dir.put(bytes, mime) {
                Ok(r) => r,
                Err(e) => {
                    tracing::warn!(
                        "blob write failed (run {}, {hash}): {e}; the ref is recorded unresolved",
                        self.run_id()
                    );
                    blob::raw_ref(&hash, bytes.len(), mime)
                }
            },
            None => {
                tracing::warn!(
                    "no blob directory (run {}, {hash}): the ref is recorded unresolved",
                    self.run_id()
                );
                blob::raw_ref(&hash, bytes.len(), mime)
            }
        }
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

    /// ADR-001 §7 / ADR-026 §2: a value over the threshold leaves the
    /// record as a ref. Text spills go to the store's text-blob place;
    /// one that would not fit a store request goes to the directory as
    /// raw `application/json` (same hash — the bytes are the text). A
    /// refused text spill never drops the sink.
    fn spill(&mut self, value: Value) -> Value {
        let text = canonical_json(&value);
        if text.len() <= BLOB_THRESHOLD {
            return value;
        }
        let mut h = Sha256::new();
        h.update(text.as_bytes());
        let hash = format!("sha256:{}", hex::encode(h.finalize()));
        let bytes = text.len();
        if bytes > RAW_TEXT_CUTOFF && self.blob_dir.is_some() {
            let r = self.put_raw(text.as_bytes(), JSON_MIME);
            self.raw_text.insert(hash.clone());
            self.blobs.push((hash, text));
            return r;
        }
        if let Some(sink) = self.sink.as_mut() {
            if let Err(e) = sink.append_blob(&hash, &text) {
                tracing::warn!("trace blob write failed ({hash}): {e}; retried at run end");
                self.failed_blobs.push((hash.clone(), text.clone()));
            }
        }
        self.blobs.push((hash.clone(), text));
        json!({"__blob": hash, "bytes": bytes})
    }

    /// ADR-026 §2/§6: a record whose input/output carries raw refs lists
    /// their hashes in `blobs` — the retention sweep's live set.
    fn stamp_raw_refs(rec: &mut Map<String, Value>) {
        let mut refs = Vec::new();
        for key in ["input", "output"] {
            if let Some(v) = rec.get(key) {
                blob::collect_raw_refs(v, &mut refs);
            }
        }
        if !refs.is_empty() {
            refs.sort();
            refs.dedup();
            rec.insert("blobs".into(), json!(refs));
        }
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
        Self::stamp_raw_refs(&mut rec);
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
        let mut rec = json!({
            "kind": "span", "seq": seq, "phase": "begin", "span": span,
            "parent": parent, "name": name, "cell": cell,
            "input": spilled, "key": key,
        });
        Self::stamp_raw_refs(rec.as_object_mut().expect("object"));
        self.push(rec);
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
        let mut rec = json!({
            "kind": "span", "seq": seq, "phase": "end", "span": span,
            "name": name, "cell": cell, "ok": ok,
            "output": out, "error": error.unwrap_or(Value::Null), "meta": meta,
        });
        Self::stamp_raw_refs(rec.as_object_mut().expect("object"));
        self.push(rec);
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
        // the text-blob store never sees a raw-written spill (ADR-026 §2)
        let text_blobs: Vec<(String, String)> = self
            .blobs
            .iter()
            .filter(|(h, _)| !self.raw_text.contains(h))
            .cloned()
            .collect();
        if !(self.sink.is_some() && self.streamed) {
            store.write_run(&run, &self.records, &text_blobs)?;
        } else if !self.failed_blobs.is_empty() {
            let retry = std::mem::take(&mut self.failed_blobs);
            if let Err(e) = store.write_blobs(&retry) {
                tracing::warn!("trace blobs still unwritten at run end ({run}): {e}");
            }
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

    /// ADR-026 §2: a text spill over the request cap is written raw to
    /// the directory (same hash — the bytes are the text), the record
    /// carries the raw ref and lists it in `blobs`; a small spill stays
    /// in the sidecar.
    #[test]
    fn oversize_text_spills_raw_to_the_directory() {
        let dir = tempfile::tempdir().unwrap();
        let store = FileTraceStore::new(dir.path());
        let mut w = TraceWriter::new(json!({"id": "run_r", "program": "p"}));
        w.stream_to(&store).unwrap();
        let big = json!({"data": "z".repeat(RAW_TEXT_CUTOFF + 1)});
        effect_rec(&mut w, big.clone());
        let rec = w.records.last().unwrap();
        assert_eq!(rec["output"]["mime"], json!(JSON_MIME));
        let hash = rec["output"]["__blob"].as_str().unwrap().to_string();
        assert_eq!(rec["blobs"], json!([hash]));
        assert!(!store
            .path_of("run_r")
            .with_extension("jsonl.blobs")
            .exists());
        let on_disk = store
            .blob_dir()
            .unwrap()
            .read_string(&hash)
            .unwrap()
            .unwrap();
        assert_eq!(serde_json::from_str::<Value>(&on_disk).unwrap(), big);
        // readers re-hydrate it like a text spill
        use crate::tracestore::TraceStore as _;
        assert_eq!(store.record("run_r", 1).unwrap()["output"], big);
        // the summary sees it too (dump keeps it in memory)
        w.dump(&store).unwrap();
    }

    struct RefusingSink;
    impl TraceSink for RefusingSink {
        fn append(&mut self, _r: &Value) -> anyhow::Result<()> {
            Ok(())
        }
        fn append_blob(&mut self, _h: &str, _d: &str) -> anyhow::Result<()> {
            anyhow::bail!("413")
        }
    }

    /// ADR-026 §2: a refused text spill never drops the sink — records
    /// keep streaming; the blob is retried at run end.
    #[test]
    fn refused_blob_keeps_the_sink() {
        let mut w = TraceWriter::new(json!({"id": "run_k", "program": "p"}));
        w.sink = Some(Box::new(RefusingSink));
        w.streamed = true;
        effect_rec(&mut w, json!({"data": "z".repeat(BLOB_THRESHOLD + 1)}));
        assert!(w.sink.is_some());
        assert_eq!(w.failed_blobs.len(), 1);
        effect_rec(&mut w, json!({"ok": 1}));
        assert!(w.sink.is_some());
    }
}
