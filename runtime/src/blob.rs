//! The blob directory (ADR-026 §1/§2): bytes as handles. A raw blob
//! is a file at `<traces_dir>/blobs/<hex>` named by the sha256 of its
//! bytes, written once (temp + rename, idempotent by name) and
//! referenced from trace records, cell values and effect payloads by
//! `{"__blob": "sha256:<hex>", "bytes": n, "mime": "<media type>"}`.
//! A ref WITHOUT `mime` is the other shape — a spilled canonical-JSON
//! text in the store's text-blob place (ADR-001 §7, ADR-023 §4). The
//! guest never sees a path; the host never sees the guest hold bytes
//! it did not ask for.

use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

/// A text spill larger than one local-store request (ADR-023 §4:
/// 700 KB canonical) is written raw, `mime: application/json`.
pub const RAW_TEXT_CUTOFF: usize = 700 * 1024;
/// The media type an oversize text spill carries — the one raw kind
/// readers re-hydrate like a text spill (ADR-026 §2).
pub const JSON_MIME: &str = "application/json";

pub fn hash_of(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    format!("sha256:{}", hex::encode(h.finalize()))
}

/// The raw ref shape (ADR-026 §1).
pub fn raw_ref(hash: &str, bytes: usize, mime: &str) -> Value {
    json!({"__blob": hash, "bytes": bytes, "mime": mime})
}

/// `{__blob, bytes, mime}` — bytes in the directory.
pub fn is_raw_ref(v: &Value) -> bool {
    v.as_object().is_some_and(|m| {
        m.len() == 3
            && m.get("__blob").is_some_and(Value::is_string)
            && m.get("bytes").is_some_and(Value::is_number)
            && m.get("mime").is_some_and(Value::is_string)
    })
}

/// Every raw ref's hash anywhere inside `v` (input/output slots carry
/// them nested: an http body, a File part's `data`, a request body).
pub fn collect_raw_refs(v: &Value, out: &mut Vec<String>) {
    match v {
        Value::Object(m) => {
            if is_raw_ref(v) {
                if let Some(h) = m["__blob"].as_str() {
                    out.push(h.to_string());
                }
                return;
            }
            for x in m.values() {
                collect_raw_refs(x, out);
            }
        }
        Value::Array(a) => {
            for x in a {
                collect_raw_refs(x, out);
            }
        }
        _ => {}
    }
}

/// The directory. Cloned freely (a path); every operation opens the
/// file it needs.
#[derive(Debug, Clone)]
pub struct BlobDir {
    dir: PathBuf,
}

impl BlobDir {
    /// `<traces_dir>/blobs` — created on the first write.
    pub fn new(traces_dir: &Path) -> Self {
        BlobDir {
            dir: traces_dir.join("blobs"),
        }
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// `sha256:<hex>` or bare `<hex>` → the file; a hash that is not
    /// hex is refused (the guest hands hashes in — no path can ride
    /// in one). Reads treat a malformed hash as a miss.
    pub fn path_of(&self, hash: &str) -> anyhow::Result<PathBuf> {
        let hex = hash.strip_prefix("sha256:").unwrap_or(hash);
        anyhow::ensure!(
            hex.len() == 64 && hex.bytes().all(|b| b.is_ascii_hexdigit()),
            "not a blob hash: {hash:?}"
        );
        Ok(self.dir.join(hex))
    }

    /// Write `bytes` (idempotent: an existing file of that hash is the
    /// same bytes) and return the raw ref.
    pub fn put(&self, bytes: &[u8], mime: &str) -> anyhow::Result<Value> {
        let hash = hash_of(bytes);
        let path = self.path_of(&hash)?;
        if !path.exists() {
            fs::create_dir_all(&self.dir)?;
            let tmp = self
                .dir
                .join(format!(".{}.{}.tmp", &hash[7..], std::process::id()));
            {
                let mut f = fs::File::create(&tmp)?;
                f.write_all(bytes)?;
                f.sync_all()?;
            }
            // a concurrent writer of the same hash wrote the same bytes;
            // whoever renames second overwrites with an identical file
            fs::rename(&tmp, &path)?;
        }
        Ok(raw_ref(&hash, bytes.len(), mime))
    }

    pub fn exists(&self, hash: &str) -> bool {
        self.path_of(hash).map(|p| p.exists()).unwrap_or(false)
    }

    pub fn size(&self, hash: &str) -> anyhow::Result<Option<u64>> {
        let Ok(path) = self.path_of(hash) else {
            return Ok(None);
        };
        match fs::metadata(&path) {
            Ok(m) => Ok(Some(m.len())),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    /// The whole file; `None` when the hash is not in the directory.
    pub fn read(&self, hash: &str) -> anyhow::Result<Option<Vec<u8>>> {
        let Ok(path) = self.path_of(hash) else {
            return Ok(None);
        };
        match fs::read(&path) {
            Ok(b) => Ok(Some(b)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    /// `length` bytes from `offset` (clamped to the file).
    pub fn read_range(
        &self,
        hash: &str,
        offset: u64,
        length: u64,
    ) -> anyhow::Result<Option<Vec<u8>>> {
        use std::io::{Read, Seek, SeekFrom};
        let Ok(path) = self.path_of(hash) else {
            return Ok(None);
        };
        let mut f = match fs::File::open(&path) {
            Ok(f) => f,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e.into()),
        };
        f.seek(SeekFrom::Start(offset))?;
        let mut out = Vec::new();
        f.take(length).read_to_end(&mut out)?;
        Ok(Some(out))
    }

    /// The oversize-text-spill read: the file as UTF-8.
    pub fn read_string(&self, hash: &str) -> anyhow::Result<Option<String>> {
        Ok(self
            .read(hash)?
            .map(|b| String::from_utf8_lossy(&b).into_owned()))
    }

    /// Every hash in the directory (`sha256:` form), for the retention
    /// sweep (ADR-026 §6).
    pub fn list(&self) -> anyhow::Result<Vec<String>> {
        let rd = match fs::read_dir(&self.dir) {
            Ok(rd) => rd,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(e) => return Err(e.into()),
        };
        let mut out = Vec::new();
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            if name.len() == 64 && name.bytes().all(|b| b.is_ascii_hexdigit()) {
                out.push(format!("sha256:{name}"));
            }
        }
        out.sort();
        Ok(out)
    }

    /// The retention sweep (ADR-026 §6): unlink every file whose hash
    /// is not in `keep`. Returns the number removed.
    pub fn sweep(&self, keep: &std::collections::BTreeSet<String>) -> anyhow::Result<usize> {
        let mut n = 0;
        for h in self.list()? {
            if !keep.contains(&h) {
                self.remove(&h)?;
                n += 1;
            }
        }
        Ok(n)
    }

    pub fn remove(&self, hash: &str) -> anyhow::Result<()> {
        let path = self.path_of(hash)?;
        match fs::remove_file(&path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e.into()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn put_is_idempotent_and_content_addressed() {
        let dir = tempfile::tempdir().unwrap();
        let store = BlobDir::new(dir.path());
        let r1 = store.put(b"hello", "text/plain").unwrap();
        let r2 = store.put(b"hello", "image/png").unwrap();
        assert_eq!(r1["__blob"], r2["__blob"]);
        assert_eq!(r1["bytes"], json!(5));
        assert_eq!(r2["mime"], json!("image/png"));
        assert!(is_raw_ref(&r1));
        assert_eq!(store.list().unwrap().len(), 1);
        let h = r1["__blob"].as_str().unwrap();
        assert_eq!(store.read(h).unwrap().unwrap(), b"hello");
        assert_eq!(store.read_range(h, 1, 3).unwrap().unwrap(), b"ell");
        assert_eq!(store.size(h).unwrap(), Some(5));
        store.remove(h).unwrap();
        assert!(store.read(h).unwrap().is_none());
        assert!(store.path_of("../etc/passwd").is_err());
    }

    #[test]
    fn sweep_keeps_the_referenced_and_drops_the_orphan() {
        let dir = tempfile::tempdir().unwrap();
        let store = BlobDir::new(dir.path());
        let keep_ref = store.put(b"keep", "text/plain").unwrap();
        store.put(b"orphan", "text/plain").unwrap();
        let keep: std::collections::BTreeSet<String> =
            [keep_ref["__blob"].as_str().unwrap().to_string()].into();
        assert_eq!(store.sweep(&keep).unwrap(), 1);
        assert_eq!(store.list().unwrap().len(), 1);
        assert!(store.exists(keep_ref["__blob"].as_str().unwrap()));
    }

    #[test]
    fn raw_refs_are_found_anywhere() {
        let r = raw_ref("sha256:ab", 1, "image/png");
        let v = json!({"json": {"messages": [{"data": r}]}, "body": json!({"__blob": "sha256:t", "bytes": 3})});
        let mut out = Vec::new();
        collect_raw_refs(&v, &mut out);
        assert_eq!(out, vec!["sha256:ab"]); // the text ref is not raw
    }
}
