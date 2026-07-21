//! Kernel-from-space boot protocol + content-hash cache (ADR-009 §4).
//! Read the kernel object's markdown manifest (sha256 + fileId — the
//! kernel itself is just an attached file), serve bytes from
//! `<cache>/kernel/<sha256>.wasm`, download + digest-verify on miss.
//! Every failure here is a hard boot error — no silent fallback.

use crate::anyapi::Client;
use crate::deploy::{parse_kernel_manifest, KERNEL_OBJECT_NAME, KERNEL_TYPE};
use anyhow::{bail, Context, Result};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::path::Path;

pub fn kernel_bytes(client: &Client, space: &str, cache_dir: &Path) -> Result<Vec<u8>> {
    let rows = client.query_objects(
        space,
        &json!({
        "filter": {"any.name": KERNEL_OBJECT_NAME, "any.types": KERNEL_TYPE},
        "limit": 1}),
    )?;
    let oid = rows
        .first()
        .and_then(|r| r["id"].as_str())
        .with_context(|| {
            format!("no {KERNEL_OBJECT_NAME} object in space {space} — run `anyrt deploy` first")
        })?;
    let pointer = parse_kernel_manifest(&client.get_markdown(space, oid)?)
        .context("kernel object has no manifest — run `anyrt deploy` first")?;
    let (sha, file_id) = (pointer.sha256.as_str(), pointer.file_id.as_str());

    let cached = cache_dir.join("kernel").join(format!("{sha}.wasm"));
    if let Ok(bytes) = std::fs::read(&cached) {
        // a torn/corrupted cache entry falls through to a re-download
        if hex::encode(Sha256::digest(&bytes)) == sha {
            return Ok(bytes);
        }
    }

    let bytes = client
        .download_file(space, file_id)
        .with_context(|| format!("kernel download (file {file_id})"))?;
    let got = hex::encode(Sha256::digest(&bytes));
    if got != sha {
        bail!("kernel digest mismatch: record says {sha}, downloaded {got}");
    }
    let dir = cached.parent().expect("cache path has a parent");
    std::fs::create_dir_all(dir)?;
    // tmp + rename: a concurrent boot never reads a torn file
    let tmp = dir.join(format!("{sha}.wasm.tmp{}", std::process::id()));
    std::fs::write(&tmp, &bytes)?;
    std::fs::rename(&tmp, &cached)?;
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::StubTransport;
    use serde_json::json;

    const WASM: &[u8] = b"fake-kernel-bytes";

    fn sha_of(bytes: &[u8]) -> String {
        hex::encode(Sha256::digest(bytes))
    }

    /// Stub scripted with the object lookup + the markdown manifest;
    /// raw bytes optional.
    fn scripted(raw: Option<&[u8]>) -> Client {
        let stub = StubTransport::new();
        stub.push(200, json!({"records": [{"id": "kobj"}]}));
        stub.push(
            200,
            json!({"content": format!("# anyrt kernel\n\nsha256: {}\nfileId: f1\n",
                                      sha_of(WASM))}),
        );
        if let Some(bytes) = raw {
            stub.push_raw(bytes.to_vec());
        }
        Client::with_transport(Box::new(stub))
    }

    #[test]
    fn first_boot_downloads_and_caches() {
        let dir = tempfile::tempdir().unwrap();
        let bytes = kernel_bytes(&scripted(Some(WASM)), "sp", dir.path()).unwrap();
        assert_eq!(bytes, WASM);
        let cached = dir
            .path()
            .join("kernel")
            .join(format!("{}.wasm", sha_of(WASM)));
        assert_eq!(std::fs::read(cached).unwrap(), WASM);
    }

    #[test]
    fn second_boot_hits_the_cache() {
        let dir = tempfile::tempdir().unwrap();
        kernel_bytes(&scripted(Some(WASM)), "sp", dir.path()).unwrap();
        // no raw reply scripted: a download attempt would yield empty
        // bytes and a digest error — success proves the cache served
        let bytes = kernel_bytes(&scripted(None), "sp", dir.path()).unwrap();
        assert_eq!(bytes, WASM);
    }

    #[test]
    fn corrupted_cache_entry_redownloads() {
        let dir = tempfile::tempdir().unwrap();
        let entry = dir
            .path()
            .join("kernel")
            .join(format!("{}.wasm", sha_of(WASM)));
        std::fs::create_dir_all(entry.parent().unwrap()).unwrap();
        std::fs::write(&entry, b"torn write").unwrap();
        let bytes = kernel_bytes(&scripted(Some(WASM)), "sp", dir.path()).unwrap();
        assert_eq!(bytes, WASM);
        assert_eq!(std::fs::read(&entry).unwrap(), WASM); // cache repaired
    }

    #[test]
    fn digest_mismatch_is_a_hard_error() {
        let dir = tempfile::tempdir().unwrap();
        let err = kernel_bytes(&scripted(Some(b"wrong bytes")), "sp", dir.path()).unwrap_err();
        assert!(err.to_string().contains("digest mismatch"), "{err}");
        assert!(!dir.path().join("kernel").exists()); // nothing cached
    }

    #[test]
    fn missing_kernel_object_names_the_fix() {
        let stub = StubTransport::new();
        stub.push(200, json!({"records": []}));
        let c = Client::with_transport(Box::new(stub));
        let dir = tempfile::tempdir().unwrap();
        let err = kernel_bytes(&c, "sp", dir.path()).unwrap_err();
        assert!(
            err.to_string().contains("run `anyrt deploy` first"),
            "{err}"
        );
    }
}
