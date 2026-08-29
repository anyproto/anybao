//! Trace storage (ADR-001 §8) — the ONE seam between the trace log and
//! wherever it persists. Every writer (`TraceWriter`) and every reader
//! (`trace ls/show/stats`, the guest's `trace.*` syscalls, serve)
//! goes through [`TraceStore`]; nothing else opens a run. Today the
//! only implementation is [`FileTraceStore`] — one `run_<id>.jsonl`
//! per run plus a `.jsonl.blobs` sidecar (§7) in a device-local dir.
//! Moving traces elsewhere (a space, a database) is a second impl of
//! this trait, not a rewrite of the readers.
//!
//! The record shapes are the store's payload, never its concern:
//! `load` returns the intact log exactly as written, and blob
//! resolution stays a reader-side view (`load_resolved`).

use crate::replay::resolve_blobs;
use crate::trace::canonical_json;
use crate::trace::SCHEMA;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

/// A run id as it appears in `traceRef` / `lastRunRef` and on disk.
/// Guests hand these in — the check is what keeps `run` from naming
/// anything but a run.
pub fn valid_run_id(id: &str) -> bool {
    id.starts_with("run_")
        && id.len() > 4
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

/// Listing row: identity + the run's only wall-clock (records are
/// deliberately time-free, ADR-001 §2).
#[derive(Debug, Clone)]
pub struct RunMeta {
    pub id: String,
    pub modified: Option<SystemTime>,
}

/// Streaming sink for one run (ADR-001 §1): records append at commit
/// time so a run is readable in flight and survives a crash.
pub trait TraceSink: Send {
    fn append(&mut self, record: &Value) -> anyhow::Result<()>;
    fn append_blob(&mut self, hash: &str, data: &str) -> anyhow::Result<()>;
    /// Flush whatever the sink still holds; called once at run end.
    fn close(&mut self) -> anyhow::Result<()> {
        Ok(())
    }
}

pub trait TraceStore: Send + Sync {
    /// Open the streaming sink for a new run. Failing here is not
    /// fatal — the writer buffers and `write_run` lands the log at the
    /// end.
    fn open_sink(&self, run_id: &str) -> anyhow::Result<Box<dyn TraceSink>>;
    /// Persist a whole run (the buffered fallback; also the replay
    /// fixture path). Overwrites.
    fn write_run(
        &self,
        run_id: &str,
        records: &[Value],
        blobs: &[(String, String)],
    ) -> anyhow::Result<()>;
    /// Run-end hook (ADR-023 §3): the whole log is landed (streamed or
    /// via `write_run`); returns the run summary (§1) — computed from
    /// the blob-resolved log — and a store that keeps summaries stores
    /// it. `started_at` = the writer's wall-clock.
    fn finish(
        &self,
        run_id: &str,
        records: &[Value],
        blobs: &[(String, String)],
        started_at: f64,
    ) -> anyhow::Result<Value> {
        let _ = run_id;
        Ok(summary_of(records, blobs, started_at, None))
    }
    /// Retention (ADR-023 §6): drop the bodies of runs older than the
    /// cutoffs — `conversations_before` for runs of `chat_program`,
    /// `jobs_before` for every other program (None = keep). Summaries
    /// stay. Returns the number of runs expired. Default: no-op (the
    /// file store keeps everything).
    fn expire(
        &self,
        _chat_program: &str,
        _conversations_before: Option<f64>,
        _jobs_before: Option<f64>,
    ) -> anyhow::Result<usize> {
        Ok(0)
    }
    /// Every run in the store, newest first.
    fn list(&self) -> anyhow::Result<Vec<RunMeta>>;
    /// The run finder over per-run summaries (ADR-023 §5): any-store
    /// `filter`/`sort`/`limit` on `trace_runs` rows. `None` = this
    /// store keeps no summaries (the file store) — callers fall back
    /// to deriving rows from the logs.
    fn find_runs(
        &self,
        _filter: &Value,
        _sort: &Value,
        _limit: usize,
    ) -> anyhow::Result<Option<Vec<Value>>> {
        Ok(None)
    }
    /// Read-only aggregation over one trace collection (ADR-023 §5):
    /// `coll` ∈ records | runs | blobs. Stores without a query engine
    /// answer a typed error.
    fn query(&self, coll: &str, _pipeline: &Value) -> anyhow::Result<Value> {
        anyhow::bail!(
            "trace.query over {coll}: this trace store has no query engine (file backend)"
        )
    }
    /// The intact log (blob refs unresolved). Errors when the run is
    /// unknown or not a schema-2 trace.
    fn load(&self, run_id: &str) -> anyhow::Result<Vec<Value>>;
    /// The run's spilled blobs, hash → canonical text (§7).
    fn blobs(&self, run_id: &str) -> anyhow::Result<BTreeMap<String, String>>;
    /// The log of a run that may still be streaming: whatever is
    /// complete so far (a half-written tail is dropped, not an error).
    /// Default = `load`; stores that stream partial writes override.
    fn load_in_flight(&self, run_id: &str) -> anyhow::Result<Vec<Value>> {
        self.load(run_id)
    }
    /// Just the header — cheap on a file store (first line).
    fn header(&self, run_id: &str) -> anyhow::Result<Value> {
        let records = self.load(run_id)?;
        Ok(records[0].clone())
    }
    /// The log with every `input`/`output` blob ref re-hydrated — the
    /// reader-side view every human/guest consumer wants.
    fn load_resolved(&self, run_id: &str) -> anyhow::Result<Vec<Value>> {
        let mut records = self.load(run_id)?;
        let blobs = self.blobs(run_id)?;
        if !blobs.is_empty() {
            for r in records.iter_mut() {
                if let Some(obj) = r.as_object_mut() {
                    for key in ["input", "output"] {
                        if let Some(v) = obj.get(key) {
                            obj.insert(key.into(), resolve_blobs(v.clone(), &blobs));
                        }
                    }
                }
            }
        }
        Ok(records)
    }
}

pub fn parse_records(text: &str, what: &str) -> anyhow::Result<Vec<Value>> {
    let mut records: Vec<Value> = Vec::new();
    for line in text.lines() {
        if !line.trim().is_empty() {
            records.push(serde_json::from_str(line)?);
        }
    }
    validate_records(records, what)
}

/// Header-first + schema pin (ADR-001) over an already-parsed log.
pub fn validate_records(records: Vec<Value>, what: &str) -> anyhow::Result<Vec<Value>> {
    let has_header = records
        .first()
        .map(|r| r["kind"] == "header")
        .unwrap_or(false);
    anyhow::ensure!(has_header, "not a trace (missing header): {what}");
    anyhow::ensure!(
        records[0]["schema"] == SCHEMA,
        "trace schema {} != {SCHEMA}",
        records[0]["schema"]
    );
    Ok(records)
}

pub fn parse_blobs(text: &str) -> anyhow::Result<BTreeMap<String, String>> {
    let mut out = BTreeMap::new();
    for line in text.lines() {
        if line.trim().is_empty() {
            continue;
        }
        let entry: Value = serde_json::from_str(line)?;
        let hash = entry["hash"].as_str().unwrap_or("").to_string();
        let data = entry["data"].as_str().unwrap_or("").to_string();
        out.insert(hash, data);
    }
    Ok(out)
}

// --- the file store ----------------------------------------------------------

/// `<dir>/<run_id>.jsonl` (+ `.jsonl.blobs`), the device-local layout.
#[derive(Debug, Clone)]
pub struct FileTraceStore {
    dir: PathBuf,
}

impl FileTraceStore {
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        FileTraceStore { dir: dir.into() }
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    pub fn path_of(&self, run_id: &str) -> PathBuf {
        self.dir.join(format!("{run_id}.jsonl"))
    }

    fn blob_path(path: &Path) -> PathBuf {
        let mut os = path.as_os_str().to_os_string();
        os.push(".blobs");
        PathBuf::from(os)
    }

    /// CLI convenience: `trace show <file-or-run-id>` — an existing
    /// path names its own store (parent dir + stem); anything else is a
    /// run id in `default_dir`.
    pub fn locate(arg: &Path, default_dir: &Path) -> (FileTraceStore, String) {
        if arg.exists() {
            let dir = arg
                .parent()
                .filter(|p| !p.as_os_str().is_empty())
                .map(Path::to_path_buf)
                .unwrap_or_else(|| PathBuf::from("."));
            let id = arg
                .file_stem()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_default();
            return (FileTraceStore::new(dir), id);
        }
        (
            FileTraceStore::new(default_dir),
            arg.to_string_lossy().into_owned(),
        )
    }
}

struct FileSink {
    file: fs::File,
    blob_path: PathBuf,
}

impl TraceSink for FileSink {
    fn append(&mut self, record: &Value) -> anyhow::Result<()> {
        writeln!(self.file, "{}", canonical_json(record))?;
        self.file.flush()?;
        Ok(())
    }

    fn append_blob(&mut self, hash: &str, data: &str) -> anyhow::Result<()> {
        // sidecar appends in spill order — consumers key by hash
        let line = canonical_json(&json!({"hash": hash, "data": data})) + "\n";
        fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.blob_path)?
            .write_all(line.as_bytes())?;
        Ok(())
    }
}

impl TraceStore for FileTraceStore {
    fn open_sink(&self, run_id: &str) -> anyhow::Result<Box<dyn TraceSink>> {
        fs::create_dir_all(&self.dir)?;
        let path = self.path_of(run_id);
        let file = fs::File::create(&path)?;
        Ok(Box::new(FileSink {
            file,
            blob_path: Self::blob_path(&path),
        }))
    }

    fn write_run(
        &self,
        run_id: &str,
        records: &[Value],
        blobs: &[(String, String)],
    ) -> anyhow::Result<()> {
        fs::create_dir_all(&self.dir)?;
        let path = self.path_of(run_id);
        let mut text = String::new();
        for r in records {
            text.push_str(&canonical_json(r));
            text.push('\n');
        }
        fs::write(&path, text)?;
        if !blobs.is_empty() {
            let mut sorted = blobs.to_vec();
            sorted.sort();
            let side: String = sorted
                .iter()
                .map(|(h, t)| canonical_json(&json!({"hash": h, "data": t})) + "\n")
                .collect();
            fs::write(Self::blob_path(&path), side)?;
        }
        Ok(())
    }

    fn list(&self) -> anyhow::Result<Vec<RunMeta>> {
        let mut rows: Vec<RunMeta> = fs::read_dir(&self.dir)?
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| p.extension().map(|x| x == "jsonl").unwrap_or(false))
            .filter_map(|p| {
                let id = p.file_stem()?.to_string_lossy().into_owned();
                let modified = p.metadata().and_then(|m| m.modified()).ok();
                Some(RunMeta { id, modified })
            })
            .collect();
        rows.sort_by(|a, b| b.modified.cmp(&a.modified).then_with(|| b.id.cmp(&a.id)));
        Ok(rows)
    }

    fn load(&self, run_id: &str) -> anyhow::Result<Vec<Value>> {
        let path = self.path_of(run_id);
        let text = fs::read_to_string(&path)
            .map_err(|e| anyhow::anyhow!("run {run_id}: {e} ({})", path.display()))?;
        parse_records(&text, &path.display().to_string())
    }

    fn blobs(&self, run_id: &str) -> anyhow::Result<BTreeMap<String, String>> {
        let side = Self::blob_path(&self.path_of(run_id));
        if !side.exists() {
            return Ok(BTreeMap::new());
        }
        parse_blobs(&fs::read_to_string(&side)?)
    }

    fn load_in_flight(&self, run_id: &str) -> anyhow::Result<Vec<Value>> {
        let text = fs::read_to_string(self.path_of(run_id))?;
        // only complete lines — a partially-written tail waits for its \n
        let complete = &text[..text.rfind('\n').map(|i| i + 1).unwrap_or(0)];
        Ok(complete
            .lines()
            .filter_map(|l| serde_json::from_str::<Value>(l).ok())
            .collect())
    }

    fn header(&self, run_id: &str) -> anyhow::Result<Value> {
        use std::io::BufRead;
        let path = self.path_of(run_id);
        let f = fs::File::open(&path)?;
        let first = std::io::BufReader::new(f)
            .lines()
            .next()
            .ok_or_else(|| anyhow::anyhow!("empty trace {}", path.display()))??;
        let h: Value = serde_json::from_str(&first)?;
        anyhow::ensure!(h["kind"] == "header", "not a trace: {}", path.display());
        Ok(h)
    }
}

// --- the any local-store backend (ADR-023) -----------------------------------

use crate::anyapi::Client;
use std::sync::Arc;

pub const RECORDS_COLL: &str = "trace_records";
pub const BLOBS_COLL: &str = "trace_blobs";
pub const RUNS_COLL: &str = "trace_runs";
/// records buffered before a flush (ADR-023 §3)
const FLUSH_EVERY: usize = 64;
/// server cap is 1000 docs per insert/upsert
const CHUNK: usize = 500;
/// query cap
const PAGE: usize = 1000;

/// The run summary (ADR-023 §1) from an in-memory log + its blobs.
pub fn summary_of(
    records: &[Value],
    blobs: &[(String, String)],
    started_at: f64,
    device: Option<&str>,
) -> Value {
    let map: BTreeMap<String, String> = blobs.iter().cloned().collect();
    let resolved: Vec<Value> = records
        .iter()
        .map(|r| {
            let mut r = r.clone();
            if let Some(o) = r.as_object_mut() {
                for key in ["input", "output"] {
                    if let Some(v) = o.get(key) {
                        o.insert(key.into(), resolve_blobs(v.clone(), &map));
                    }
                }
            }
            r
        })
        .collect();
    crate::view::run_summary(&resolved, Some(started_at), Some(now_s()), device)
}

/// Integral f64 → i64 everywhere in a value (|x| < 2^53, exact).
pub fn fold_integral_floats(v: Value) -> Value {
    match v {
        Value::Number(n) => match n.as_f64() {
            Some(f)
                if n.as_i64().is_none()
                    && f.fract() == 0.0
                    && f.abs() < 9.007_199_254_740_992e15 =>
            {
                json!(f as i64)
            }
            _ => Value::Number(n),
        },
        Value::Array(a) => Value::Array(a.into_iter().map(fold_integral_floats).collect()),
        Value::Object(o) => Value::Object(
            o.into_iter()
                .map(|(k, v)| (k, fold_integral_floats(v)))
                .collect(),
        ),
        other => other,
    }
}

/// The first `$out` / `$merge` anywhere in a pipeline — nested `$facet`
/// branches included (ADR-023 §5: the guest's query surface is read-only).
pub fn find_sink_stage(v: &Value) -> Option<&'static str> {
    match v {
        Value::Array(a) => a.iter().find_map(find_sink_stage),
        Value::Object(o) => {
            if o.contains_key("$out") {
                return Some("$out");
            }
            if o.contains_key("$merge") {
                return Some("$merge");
            }
            o.values().find_map(find_sink_stage)
        }
        _ => None,
    }
}

fn now_s() -> f64 {
    SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Trace bodies in three device-local collections of the bao space
/// (`l_s_<space>_trace_{records,blobs,runs}`, any PR #195): never
/// synced, no DAG, the synced data's query language. Records are the
/// ADR-001 shapes plus `runId` and `id = "<runId>:<seq:06>"`; blobs are
/// content-addressed (`id` = hash) so the boot window a conversation
/// re-sends every turn spills once; `trace_runs` holds one summary per
/// run, written by `finish`, and is what `list` reads.
pub struct AnyTraceStore {
    client: Arc<Client>,
    records: Value,
    blobs: Value,
    runs: Value,
    device: Option<String>,
}

impl AnyTraceStore {
    /// Ensure the three collections (idempotent) and hand back the store.
    pub fn new(
        client: Arc<Client>,
        space_id: &str,
        device: Option<String>,
    ) -> anyhow::Result<Self> {
        let records = Client::local_coll(space_id, RECORDS_COLL);
        let blobs = Client::local_coll(space_id, BLOBS_COLL);
        let runs = Client::local_coll(space_id, RUNS_COLL);
        client.local_ensure(
            &records,
            &[
                json!({"fields": ["runId", "seq"], "unique": true}),
                json!({"fields": ["name"]}),
                json!({"fields": ["effect"]}),
                json!({"fields": ["error.type"], "sparse": true}),
                json!({"fields": ["meta.class"]}),
            ],
        )?;
        client.local_ensure(&blobs, &[])?;
        client.local_ensure(
            &runs,
            &[
                json!({"fields": ["program"]}),
                json!({"fields": ["startedAt"]}),
                json!({"fields": ["status"]}),
                json!({"fields": ["mutations"]}),
            ],
        )?;
        Ok(AnyTraceStore {
            client,
            records,
            blobs,
            runs,
            device,
        })
    }

    /// The document a record becomes: the record itself + `runId` +
    /// the sortable id. The header has no seq — it sorts first as 0.
    fn doc_of(run_id: &str, rec: &Value) -> Value {
        let mut d = rec.clone();
        let seq = rec["seq"].as_i64().unwrap_or(0);
        if let Some(o) = d.as_object_mut() {
            o.insert("runId".into(), json!(run_id));
            o.insert("seq".into(), json!(seq));
            o.insert("id".into(), json!(format!("{run_id}:{seq:06}")));
        }
        d
    }

    /// Back from a document to the ADR-001 record (parity with the file
    /// store: the same bytes a `.jsonl` line would hold). any-store
    /// hands integers back as floats (`1986942113.0`); traces carry no
    /// meaningful `x.0` floats, so integral floats within i64's exact
    /// range fold back to integers — `as_i64()` readers (fuel, seq,
    /// tokens) keep working.
    fn record_of(mut doc: Value) -> Value {
        if let Some(o) = doc.as_object_mut() {
            o.remove("runId");
            o.remove("id");
            if o.get("kind") == Some(&json!("header")) {
                o.remove("seq");
            }
        }
        fold_integral_floats(doc)
    }

    fn blob_doc(hash: &str, data: &str) -> Value {
        json!({"id": hash, "bytes": data.len(), "data": data})
    }

    fn query_all(&self, coll: &Value, filter: Value, sort: Value) -> anyhow::Result<Vec<Value>> {
        let mut out = Vec::new();
        let mut offset = 0usize;
        loop {
            let reply = self.client.local_query(
                coll,
                &json!({"filter": filter, "sort": sort, "limit": PAGE, "offset": offset}),
            )?;
            let page = reply["records"].as_array().cloned().unwrap_or_default();
            let n = page.len();
            out.extend(page);
            if n < PAGE {
                return Ok(out);
            }
            offset += n;
        }
    }

    /// Hashes of every blob ref in the log's input/output slots.
    fn blob_refs(records: &[Value]) -> Vec<String> {
        let mut out = Vec::new();
        for r in records {
            for key in ["input", "output"] {
                if let Some(h) = r[key]["__blob"].as_str() {
                    out.push(h.to_string());
                }
            }
        }
        out.sort();
        out.dedup();
        out
    }

    /// Bulk-land docs (write_run / a degraded stream): upsert so a
    /// partially-streamed run re-lands cleanly.
    fn upsert_chunks(&self, coll: &Value, docs: &[Value]) -> anyhow::Result<()> {
        for chunk in docs.chunks(CHUNK) {
            self.client.local_upsert(coll, chunk)?;
        }
        Ok(())
    }
}

struct AnySink {
    client: Arc<Client>,
    records: Value,
    blobs: Value,
    run_id: String,
    batch: Vec<Value>,
}

impl AnySink {
    fn flush(&mut self) -> anyhow::Result<()> {
        if self.batch.is_empty() {
            return Ok(());
        }
        let docs = std::mem::take(&mut self.batch);
        self.client.local_insert(&self.records, &docs)?;
        Ok(())
    }
}

impl TraceSink for AnySink {
    fn append(&mut self, record: &Value) -> anyhow::Result<()> {
        self.batch.push(AnyTraceStore::doc_of(&self.run_id, record));
        // boundaries the readers care about land immediately (ADR-023 §3)
        let boundary = record["kind"] == "cell"
            || (record["kind"] == "span" && record["phase"] == "end")
            || record["kind"] == "header";
        if boundary || self.batch.len() >= FLUSH_EVERY {
            self.flush()?;
        }
        Ok(())
    }

    fn append_blob(&mut self, hash: &str, data: &str) -> anyhow::Result<()> {
        self.client
            .local_upsert(&self.blobs, &[AnyTraceStore::blob_doc(hash, data)])?;
        Ok(())
    }

    fn close(&mut self) -> anyhow::Result<()> {
        self.flush()
    }
}

impl TraceStore for AnyTraceStore {
    fn open_sink(&self, run_id: &str) -> anyhow::Result<Box<dyn TraceSink>> {
        Ok(Box::new(AnySink {
            client: self.client.clone(),
            records: self.records.clone(),
            blobs: self.blobs.clone(),
            run_id: run_id.to_string(),
            batch: Vec::new(),
        }))
    }

    fn write_run(
        &self,
        run_id: &str,
        records: &[Value],
        blobs: &[(String, String)],
    ) -> anyhow::Result<()> {
        let docs: Vec<Value> = records.iter().map(|r| Self::doc_of(run_id, r)).collect();
        self.upsert_chunks(&self.records, &docs)?;
        let bdocs: Vec<Value> = blobs.iter().map(|(h, d)| Self::blob_doc(h, d)).collect();
        self.upsert_chunks(&self.blobs, &bdocs)
    }

    fn finish(
        &self,
        run_id: &str,
        records: &[Value],
        blobs: &[(String, String)],
        started_at: f64,
    ) -> anyhow::Result<Value> {
        let _ = run_id;
        let summary = summary_of(records, blobs, started_at, self.device.as_deref());
        self.client
            .local_upsert(&self.runs, std::slice::from_ref(&summary))?;
        Ok(summary)
    }

    fn expire(
        &self,
        chat_program: &str,
        conversations_before: Option<f64>,
        jobs_before: Option<f64>,
    ) -> anyhow::Result<usize> {
        let mut victims: Vec<String> = Vec::new();
        for (before, is_chat) in [(conversations_before, true), (jobs_before, false)] {
            let Some(before) = before else { continue };
            let prog = if is_chat {
                json!(chat_program)
            } else {
                json!({"$ne": chat_program})
            };
            let rows = self.query_all(
                &self.runs,
                json!({"startedAt": {"$lt": before}, "program": prog, "expired": {"$ne": true}}),
                json!(["startedAt"]),
            )?;
            victims.extend(
                rows.iter()
                    .filter_map(|r| r["id"].as_str().map(str::to_string)),
            );
        }
        if victims.is_empty() {
            return Ok(0);
        }
        for chunk in victims.chunks(200) {
            self.client.local_delete(
                &self.records,
                None,
                Some(&json!({"runId": {"$in": chunk}})),
            )?;
            // the summary stays (ADR-023 §6) — marked so `load` can say why
            for id in chunk {
                self.client.local_update_flag(&self.runs, id, "expired")?;
            }
        }
        // blobs no surviving record references
        let live = self.query_all(
            &self.records,
            json!({"$or": [{"input.__blob": {"$exists": true}}, {"output.__blob": {"$exists": true}}]}),
            json!(["seq"]),
        )?;
        let keep: std::collections::BTreeSet<String> = Self::blob_refs(&live).into_iter().collect();
        let all = self.query_all(&self.blobs, json!({}), json!(["id"]))?;
        let dead: Vec<String> = all
            .iter()
            .filter_map(|b| b["id"].as_str())
            .filter(|h| !keep.contains(*h))
            .map(str::to_string)
            .collect();
        for chunk in dead.chunks(500) {
            self.client.local_delete(&self.blobs, Some(chunk), None)?;
        }
        Ok(victims.len())
    }

    fn list(&self) -> anyhow::Result<Vec<RunMeta>> {
        let rows = self.query_all(&self.runs, json!({}), json!(["-startedAt"]))?;
        Ok(rows
            .into_iter()
            .filter_map(|r| {
                let id = r["id"].as_str()?.to_string();
                let modified = r["endedAt"]
                    .as_f64()
                    .or(r["startedAt"].as_f64())
                    .map(|s| std::time::UNIX_EPOCH + std::time::Duration::from_secs_f64(s));
                Some(RunMeta { id, modified })
            })
            .collect())
    }

    fn find_runs(
        &self,
        filter: &Value,
        sort: &Value,
        limit: usize,
    ) -> anyhow::Result<Option<Vec<Value>>> {
        let sort = if sort.is_null() {
            json!(["-startedAt"])
        } else {
            sort.clone()
        };
        let reply = self.client.local_query(
            &self.runs,
            &json!({"filter": filter, "sort": sort, "limit": limit.clamp(1, PAGE)}),
        )?;
        let rows = reply["records"].as_array().cloned().unwrap_or_default();
        Ok(Some(rows.into_iter().map(fold_integral_floats).collect()))
    }

    fn query(&self, coll: &str, pipeline: &Value) -> anyhow::Result<Value> {
        let target = match coll {
            "records" => &self.records,
            "runs" => &self.runs,
            "blobs" => &self.blobs,
            other => {
                anyhow::bail!("trace.query: coll must be records | runs | blobs, got {other:?}")
            }
        };
        anyhow::ensure!(
            pipeline.is_array(),
            "trace.query: pipeline must be a list of stages"
        );
        if let Some(stage) = find_sink_stage(pipeline) {
            anyhow::bail!(
                "trace.query is read-only: {stage} is not allowed (the guest reads traces, it does not write them)"
            );
        }
        let reply = self.client.local_aggregate(target, pipeline, &json!({}))?;
        Ok(fold_integral_floats(json!({"records": reply["records"]})))
    }

    fn load(&self, run_id: &str) -> anyhow::Result<Vec<Value>> {
        let docs = self.query_all(&self.records, json!({"runId": run_id}), json!(["seq"]))?;
        if docs.is_empty() {
            // expired (retention, ADR-023 §6) vs never existed
            let known = self
                .client
                .local_get(&self.runs, run_id)
                .ok()
                .filter(|r| !r.is_null());
            match known {
                Some(row) if row["expired"] == true => anyhow::bail!(
                    "run {run_id}: expired — body dropped by retention, summary only (effects.runs)"
                ),
                _ => anyhow::bail!("unknown run {run_id}"),
            }
        }
        validate_records(docs.into_iter().map(Self::record_of).collect(), run_id)
    }

    fn blobs(&self, run_id: &str) -> anyhow::Result<BTreeMap<String, String>> {
        let docs = self.query_all(&self.records, json!({"runId": run_id}), json!(["seq"]))?;
        let hashes = Self::blob_refs(&docs);
        if hashes.is_empty() {
            return Ok(BTreeMap::new());
        }
        let rows = self.query_all(&self.blobs, json!({"id": {"$in": hashes}}), json!(["id"]))?;
        Ok(rows
            .into_iter()
            .filter_map(|b| {
                Some((
                    b["id"].as_str()?.to_string(),
                    b["data"].as_str()?.to_string(),
                ))
            })
            .collect())
    }

    fn header(&self, run_id: &str) -> anyhow::Result<Value> {
        let reply = self.client.local_query(
            &self.records,
            &json!({"filter": {"runId": run_id, "kind": "header"}, "limit": 1}),
        )?;
        let h = reply["records"]
            .as_array()
            .and_then(|a| a.first().cloned())
            .ok_or_else(|| anyhow::anyhow!("unknown run {run_id}"))?;
        Ok(Self::record_of(h))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_ids_are_gated() {
        assert!(valid_run_id("run_0123abcd"));
        assert!(valid_run_id("run_a-b_c"));
        assert!(!valid_run_id("run_"));
        assert!(!valid_run_id("../run_x"));
        assert!(!valid_run_id("run_x/../../etc"));
        assert!(!valid_run_id("foo"));
    }

    #[test]
    fn file_store_round_trips_records_and_blobs() {
        let dir = tempfile::tempdir().unwrap();
        let store = FileTraceStore::new(dir.path());
        let records = vec![
            json!({"kind": "header", "schema": SCHEMA, "run": {"id": "run_a", "program": "p"}}),
            json!({"kind": "effect", "seq": 1, "effect": "x.y",
                   "output": {"__blob": "sha256:h", "bytes": 3}}),
        ];
        store
            .write_run("run_a", &records, &[("sha256:h".into(), "[1]".into())])
            .unwrap();
        assert_eq!(store.load("run_a").unwrap(), records);
        assert_eq!(store.header("run_a").unwrap()["run"]["id"], "run_a");
        let resolved = store.load_resolved("run_a").unwrap();
        assert_eq!(resolved[1]["output"], json!([1]));
        let ids: Vec<_> = store.list().unwrap().into_iter().map(|m| m.id).collect();
        assert_eq!(ids, vec!["run_a"]);
        assert!(store.load("run_zzz").is_err());
    }

    #[test]
    fn integral_floats_fold_back_to_ints() {
        let v = json!({"a": 1986942113.0, "b": 1.5, "c": [2.0, {"d": -3.0}], "e": "x", "f": 7});
        let f = fold_integral_floats(v);
        assert!(f["a"].is_i64() && f["a"] == 1986942113);
        assert!(f["b"].is_f64());
        assert!(f["c"][0].is_i64() && f["c"][1]["d"] == -3);
        assert_eq!(f["e"], "x");
        assert_eq!(f["f"], 7);
    }

    #[test]
    fn sink_stages_are_found_anywhere() {
        assert_eq!(find_sink_stage(&json!([{"$match": {"a": 1}}])), None);
        assert_eq!(
            find_sink_stage(&json!([{"$match": {}}, {"$out": "x"}])),
            Some("$out")
        );
        assert_eq!(
            find_sink_stage(&json!([{"$facet": {"a": [{"$merge": {"into": "x"}}]}}])),
            Some("$merge")
        );
    }

    #[test]
    fn locate_splits_a_path_into_store_and_id() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("run_q.jsonl");
        fs::write(&path, "").unwrap();
        let (store, id) = FileTraceStore::locate(&path, Path::new("traces"));
        assert_eq!(store.dir(), dir.path());
        assert_eq!(id, "run_q");
        let (store, id) = FileTraceStore::locate(Path::new("run_bare"), dir.path());
        assert_eq!(store.dir(), dir.path());
        assert_eq!(id, "run_bare");
    }
}

/// Round-trip against a live any server with the local store (any PR
/// #195). Gated: `ANYRT_TEST_ANY_ADDR=http://127.0.0.1:7137 cargo test
/// any_trace_store_round_trip -- --ignored --nocapture`.
#[cfg(test)]
mod live {
    use super::*;
    use crate::anyapi::Client;

    #[test]
    #[ignore]
    fn any_trace_store_round_trip() {
        let Ok(addr) = std::env::var("ANYRT_TEST_ANY_ADDR") else {
            eprintln!("ANYRT_TEST_ANY_ADDR unset — skipped");
            return;
        };
        let client = Arc::new(Client::new(&addr));
        let space = client
            .create_space("tracestore-test")
            .expect("create space")["id"]
            .as_str()
            .unwrap()
            .to_string();
        let store = AnyTraceStore::new(client.clone(), &space, Some("dev-1".into())).unwrap();

        // stream one run through a writer: header + a spilled effect + span + cell
        let mut w = crate::trace::TraceWriter::new(json!({"id": "run_live1", "program": "p@v1"}));
        w.stream_to(&store).unwrap();
        let key = crate::trace::input_key("x.y", &json!({"a": 1}));
        let big = json!({"data": "z".repeat(crate::trace::BLOB_THRESHOLD + 1)});
        w.effect(
            "x.y",
            Some("main"),
            json!({"a": 1}),
            &key,
            Some(big.clone()),
            None,
            json!({"class": "mutate", "durMs": 0}),
            None,
        );
        w.cell(
            "main",
            true,
            None,
            false,
            json!({"fuel_used": 1, "duration_ms": 5}),
        );
        let summary = w.dump(&store).unwrap();
        assert_eq!(summary["id"], "run_live1");
        assert_eq!(summary["device"], "dev-1");

        // list → the summary row; load → the intact log; blobs resolve
        let ids: Vec<_> = store.list().unwrap().into_iter().map(|m| m.id).collect();
        assert!(ids.contains(&"run_live1".to_string()), "{ids:?}");
        let records = store.load("run_live1").unwrap();
        assert_eq!(records[0]["kind"], "header");
        assert_eq!(records[0]["run"]["id"], "run_live1");
        assert!(records[0].get("seq").is_none());
        assert_eq!(records[1]["effect"], "x.y");
        // integers survive the any-store round trip as integers
        assert!(
            records[2]["metrics"]["fuel_used"].is_i64(),
            "{}",
            records[2]
        );
        assert!(
            records[1]["output"]["__blob"].is_string(),
            "spilled: {}",
            records[1]
        );
        let resolved = store.load_resolved("run_live1").unwrap();
        assert_eq!(resolved[1]["output"], big);
        assert_eq!(store.header("run_live1").unwrap()["run"]["program"], "p@v1");
        assert!(store.load("run_nope").is_err());

        // the summary row carries the ADR-023 fields
        let runs = client
            .local_query(
                &Client::local_coll(&space, RUNS_COLL),
                &json!({"filter": {"id": "run_live1"}}),
            )
            .unwrap();
        let row = &runs["records"][0];
        assert_eq!(row["program"], "p@v1");
        assert_eq!(row["status"], "ok");
        assert_eq!(row["mutations"], 1);
        assert_eq!(row["device"], "dev-1");
        assert!(row["startedAt"].as_f64().unwrap() > 0.0);

        // ADR-023 §5: finder over summaries + read-only aggregation
        let found = store
            .find_runs(
                &json!({"program": "p@v1", "mutations": {"$gte": 1}}),
                &Value::Null,
                10,
            )
            .unwrap()
            .unwrap();
        assert!(found.iter().any(|r| r["id"] == "run_live1"), "{found:?}");
        let agg = store
            .query(
                "records",
                &json!([{"$match": {"runId": "run_live1", "kind": "effect"}},
                                      {"$group": {"_id": "$effect", "n": {"$sum": 1}}}]),
            )
            .unwrap();
        // any-store names the group key `id`, not mongo's `_id`
        assert_eq!(agg["records"][0]["id"], "x.y");
        assert_eq!(agg["records"][0]["n"], 1);
        assert!(store.query("records", &json!([{"$out": "x"}])).is_err());
        assert!(store.query("nope", &json!([])).is_err());

        // buffered path (write_run) lands the same shape
        let mut w2 = crate::trace::TraceWriter::new(json!({"id": "run_live2", "program": "p@v1"}));
        w2.cell(
            "main",
            false,
            Some(json!({"type": "Boom"})),
            false,
            json!({}),
        );
        w2.dump(&store).unwrap();
        assert_eq!(store.load("run_live2").unwrap().len(), 2);
        let runs = client
            .local_query(
                &Client::local_coll(&space, RUNS_COLL),
                &json!({"filter": {"id": "run_live2"}}),
            )
            .unwrap();
        assert_eq!(runs["records"][0]["status"], "FAILED");

        // retention (ADR-023 §6): jobs older than "now" expire — bodies
        // + orphan blobs go, the summary stays and load says why
        let n = store.expire("chat@v1", None, Some(now_s() + 1.0)).unwrap();
        assert!(n >= 2, "expired {n}");
        let err = store.load("run_live1").unwrap_err().to_string();
        assert!(err.contains("expired"), "{err}");
        assert!(store.list().unwrap().iter().any(|m| m.id == "run_live1"));
        let blobs = client
            .local_query(&Client::local_coll(&space, BLOBS_COLL), &json!({}))
            .unwrap();
        assert_eq!(blobs["records"].as_array().unwrap().len(), 0, "{blobs}");
        assert_eq!(
            store.expire("chat@v1", None, Some(now_s() + 1.0)).unwrap(),
            0
        );
    }
}
