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
    /// Every run in the store, newest first.
    fn list(&self) -> anyhow::Result<Vec<RunMeta>>;
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
