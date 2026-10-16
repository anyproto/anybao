//! The compiled-code cache (ADR-032 §7): marshalled code objects for
//! program sources, keyed `(kernel_sha256, spec, sourceHash)`. Only the
//! host writes it, from the trusted compile job; a run only reads it,
//! through `module.resolve`. Device-local, never synced, outside the
//! blob root (ADR-023 §6 retention would collect it), safe to delete.

use base64::Engine as _;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

/// One kernel's slice of the cache: `<root>/<kernel_sha256>/`.
#[derive(Clone, Debug)]
pub struct CodeCache {
    dir: PathBuf,
    kernel_sha256: String,
}

/// A miss the run noted for the compile job: what to compile and the
/// key to store it under.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CodeMiss {
    pub spec: String,
    pub source_hash: String,
    pub source: String,
}

impl CodeMiss {
    /// The filename the code embeds — `use()` compiles as `<spec>`.
    pub fn filename(&self) -> String {
        format!("<{}>", self.spec)
    }
}

impl CodeCache {
    /// `root` is `<config dir>/cache/code`; nothing is created until
    /// the first store.
    pub fn new(root: &Path, kernel_sha256: &str) -> Self {
        CodeCache {
            dir: root.join(kernel_sha256),
            kernel_sha256: kernel_sha256.to_string(),
        }
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    fn key_hash(&self, spec: &str, source_hash: &str) -> String {
        let mut h = Sha256::new();
        for part in [self.kernel_sha256.as_str(), spec, source_hash] {
            h.update(part.as_bytes());
            h.update(b"\x00"); // hash-domain separator only
        }
        hex::encode(h.finalize())
    }

    fn paths(&self, spec: &str, source_hash: &str) -> (PathBuf, PathBuf) {
        let k = self.key_hash(spec, source_hash);
        (
            self.dir.join(format!("{k}.marshal")),
            self.dir.join(format!("{k}.sha256")),
        )
    }

    /// The cached code, verified against its stored sha256; a missing,
    /// unreadable or mismatched entry is a miss.
    pub fn get(&self, spec: &str, source_hash: &str) -> Option<(Vec<u8>, String)> {
        let (code_path, sum_path) = self.paths(spec, source_hash);
        let code = std::fs::read(code_path).ok()?;
        let stored = std::fs::read_to_string(sum_path).ok()?;
        let actual = hex::encode(Sha256::digest(&code));
        (stored.trim() == actual && !code.is_empty()).then(|| (code, format!("sha256:{actual}")))
    }

    /// Store compiled code under its key (atomic per file: write a
    /// temp, rename; the sha256 lands last, so a torn entry is a miss).
    pub fn put(&self, spec: &str, source_hash: &str, code: &[u8]) -> std::io::Result<()> {
        std::fs::create_dir_all(&self.dir)?;
        let (code_path, sum_path) = self.paths(spec, source_hash);
        let tmp = |p: &Path| p.with_extension(format!("tmp{}", std::process::id()));
        std::fs::write(tmp(&code_path), code)?;
        std::fs::rename(tmp(&code_path), &code_path)?;
        std::fs::write(tmp(&sum_path), hex::encode(Sha256::digest(code)))?;
        std::fs::rename(tmp(&sum_path), &sum_path)
    }

    /// `module.resolve`'s hook: on a hit, the output gains `code` (the
    /// bytes base64, a text spill like `source`) and `codeHash`; on a
    /// miss the caller notes a [`CodeMiss`]. Returns hit/miss, or None
    /// when the output names no source to key on.
    pub fn serve(&self, spec: &str, out: &mut Value) -> Option<(bool, Option<CodeMiss>)> {
        let source_hash = out.get("sourceHash")?.as_str()?.to_string();
        let source = out.get("source")?.as_str()?.to_string();
        match self.get(spec, &source_hash) {
            Some((code, hash)) => {
                out["code"] = Value::String(base64::engine::general_purpose::STANDARD.encode(code));
                out["codeHash"] = Value::String(hash);
                Some((true, None))
            }
            None => Some((
                false,
                Some(CodeMiss {
                    spec: spec.to_string(),
                    source_hash,
                    source,
                }),
            )),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn keyed_by_kernel_spec_and_source_and_verified() {
        let dir = tempfile::tempdir().unwrap();
        let cc = CodeCache::new(dir.path(), "k1");
        assert!(cc.get("a@v1", "sha256:s").is_none());
        cc.put("a@v1", "sha256:s", b"code").unwrap();
        let (code, hash) = cc.get("a@v1", "sha256:s").unwrap();
        assert_eq!(code, b"code");
        assert!(hash.starts_with("sha256:"));
        // every key part matters: spec (the filename differs), source,
        // kernel (marshal is bound to the CPython build)
        assert!(cc.get("agent:a@v1", "sha256:s").is_none());
        assert!(cc.get("a@v1", "sha256:t").is_none());
        assert!(CodeCache::new(dir.path(), "k2")
            .get("a@v1", "sha256:s")
            .is_none());
        // a corrupted entry is a miss, never served
        let (p, _) = cc.paths("a@v1", "sha256:s");
        std::fs::write(p, b"evil").unwrap();
        assert!(cc.get("a@v1", "sha256:s").is_none());
    }

    #[test]
    fn serve_adds_code_on_a_hit_and_notes_a_miss() {
        let dir = tempfile::tempdir().unwrap();
        let cc = CodeCache::new(dir.path(), "k");
        let base = json!({"sourceHash": "sha256:s", "source": "x = 1\n"});
        let mut out = base.clone();
        let (hit, miss) = cc.serve("a@v1", &mut out).unwrap();
        assert!(!hit);
        assert_eq!(miss.unwrap().filename(), "<a@v1>");
        assert_eq!(out, base, "a miss leaves the output alone");
        cc.put("a@v1", "sha256:s", b"\x01\x02").unwrap();
        let mut out = base.clone();
        let (hit, miss) = cc.serve("a@v1", &mut out).unwrap();
        assert!(hit && miss.is_none());
        assert_eq!(out["code"], "AQI=");
        assert!(out["codeHash"].as_str().unwrap().starts_with("sha256:"));
    }
}
