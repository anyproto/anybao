//! Lib-mode smoke test (ADR-009 §6): the public surface an embedder
//! uses — Config builder + TOML, the injectable any-client transport,
//! module resolution, kernel cache — exercised without wasm or a
//! server. The full agent path stays in the pytest e2e suite.

use anyrt::anyapi::{AnyError, Client, Transport};
use anyrt::config::Config;
use anyrt::resolver::{LocalDirResolver, ModuleResolver};
use serde_json::{json, Value};

/// Minimal embedder-side transport double.
struct CannedTransport {
    replies: std::sync::Mutex<Vec<(u16, Value)>>,
}

impl Transport for CannedTransport {
    fn send(&self, _m: &str, _p: &str, _b: Option<&Value>) -> Result<(u16, Value), AnyError> {
        Ok(self
            .replies
            .lock()
            .unwrap()
            .pop()
            .unwrap_or((200, json!({}))))
    }

    fn open_stream(
        &self,
        _p: &str,
        _b: Option<&Value>,
    ) -> Result<Box<dyn Iterator<Item = String>>, AnyError> {
        Ok(Box::new(std::iter::empty()))
    }
}

#[test]
fn config_builder_composes_a_host_config() {
    let cfg = Config::builder()
        .addr("http://127.0.0.1:9009")
        .agent_space("myagent")
        .overlay("agent", "bafycode")
        .overlay("std", "bafystd")
        .traces_dir("/tmp/t")
        .cache_dir("/tmp/c")
        .config_value("llm.tier.chat", json!({"provider": "anthropic"}))
        .secret("llm.key.anthropic", "sk-x")
        .build();
    assert_eq!(cfg.addr, "http://127.0.0.1:9009");
    assert_eq!(cfg.overlays["agent"].space, "bafycode");
    assert_eq!(cfg.config["llm.tier.chat"]["provider"], json!("anthropic"));
    assert_eq!(cfg.secrets["llm.key.anthropic"], "sk-x");
    assert_eq!(cfg.kernel, None); // default: kernel from the space
}

#[test]
fn config_from_toml_is_public() {
    let cfg = Config::from_toml("[overlays]\nagent = \"bafy1\"").unwrap();
    assert_eq!(cfg.overlays["agent"].space, "bafy1");
}

#[test]
fn client_accepts_an_embedder_transport() {
    let t = CannedTransport {
        replies: std::sync::Mutex::new(vec![(200, json!({"spaces": [{"id": "s1"}]}))]),
    };
    let c = Client::with_transport(Box::new(t));
    let spaces = c.list_spaces(None).unwrap();
    assert_eq!(spaces[0]["id"], json!("s1"));
}

#[test]
fn local_dir_resolver_is_public() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(
        dir.path().join("hello@v1.py"),
        "def main(args):\n    return 7\n",
    )
    .unwrap();
    let mut r = LocalDirResolver::new(dir.path().to_path_buf());
    let hit = r.resolve("hello@v1", None).unwrap();
    assert!(hit["source"].as_str().unwrap().contains("return 7"));
}

#[test]
fn kernelcache_boot_error_is_reachable() {
    let t = CannedTransport {
        replies: std::sync::Mutex::new(vec![(200, json!({"records": []}))]),
    };
    let c = Client::with_transport(Box::new(t));
    let dir = tempfile::tempdir().unwrap();
    let err = anyrt::kernelcache::kernel_bytes(&c, "sp", dir.path()).unwrap_err();
    assert!(err.to_string().contains("anyrt deploy"), "{err}");
}
