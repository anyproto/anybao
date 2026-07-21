//! Host configuration — `anybao.toml` (ADR-009 §1). One TOML file for
//! everything host-side; CLI flags override file values; secrets never
//! appear in it. The `[config]` table is the guest-visible cascade
//! layer that seeds under any `--config` JSON file.

use anyhow::{bail, Context, Result};
use serde::Deserialize;
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

pub const DEFAULT_ADDR: &str = "http://127.0.0.1:7001";
pub const DEFAULT_CONFIG_FILE: &str = "anybao.toml";

/// Serde mirror of `anybao.toml`. Unknown keys are a parse error —
/// typos fail loudly (ADR-009 §1, no forward-compat lenience).
#[derive(Debug, Deserialize, Default)]
#[serde(deny_unknown_fields, default)]
pub struct FileConfig {
    /// any server base url
    pub addr: Option<String>,
    pub agent: AgentSection,
    /// named module sources (the alias namespace); values are STRICTLY
    /// space ids, never names (ADR-009 §2). Bare id, or a table with a
    /// join invite (§8): `agent = { space = "…", invite = "…" }`.
    pub overlays: BTreeMap<String, OverlayEntry>,
    pub paths: PathsSection,
    /// guest-visible cascade layer: flat quoted dotted keys
    pub config: toml::Table,
}

/// TOML form of one overlay: bare space id or inline table.
#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub enum OverlayEntry {
    Id(String),
    Full(OverlayTable),
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OverlayTable {
    pub space: String,
    #[serde(default)]
    pub invite: Option<String>,
}

/// One resolved overlay (ADR-009 §2, §8): the space id plus an
/// optional RequestToJoin invite token for serve's join-on-boot.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Overlay {
    pub space: String,
    pub invite: Option<String>,
}

impl From<OverlayEntry> for Overlay {
    fn from(e: OverlayEntry) -> Self {
        match e {
            OverlayEntry::Id(space) => Overlay {
                space,
                invite: None,
            },
            OverlayEntry::Full(t) => Overlay {
                space: t.space,
                invite: t.invite,
            },
        }
    }
}

#[derive(Debug, Deserialize, Default)]
#[serde(deny_unknown_fields, default)]
pub struct AgentSection {
    /// working space: chat, brain, memory, agent_config,
    /// user-authored skills/programs
    pub space: Option<String>,
    pub name: Option<String>,
    pub control_port: Option<u16>,
}

#[derive(Debug, Deserialize, Default)]
#[serde(deny_unknown_fields, default)]
pub struct PathsSection {
    pub traces: Option<PathBuf>,
    pub cache: Option<PathBuf>,
}

/// The resolved host config — what serve/deploy (and lib embedders)
/// consume. Precedence: built-in defaults < `anybao.toml` < CLI flag.
#[derive(Debug, Clone)]
pub struct Config {
    pub addr: String,
    pub agent_space: String,
    pub agent_name: String,
    pub control_port: u16,
    pub overlays: BTreeMap<String, Overlay>,
    pub traces_dir: PathBuf,
    pub cache_dir: PathBuf,
    /// explicit local kernel override (dev bypass, ADR-009 §4);
    /// None = fetch from the space through the content-hash cache
    pub kernel: Option<PathBuf>,
    /// guest-visible config map (pre-`bootstrap`)
    pub config: BTreeMap<String, Value>,
    pub secrets: BTreeMap<String, String>,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            addr: DEFAULT_ADDR.into(),
            agent_space: "bao".into(),
            agent_name: "bao".into(),
            control_port: 7010,
            overlays: BTreeMap::new(),
            traces_dir: "traces".into(),
            cache_dir: default_cache_dir(),
            kernel: None,
            config: BTreeMap::new(),
            secrets: BTreeMap::new(),
        }
    }
}

/// CLI-flag layer — every field `Some` shadows the file/default value.
#[derive(Debug, Default)]
pub struct CliOverrides {
    pub addr: Option<String>,
    pub space: Option<String>,
    pub agent_name: Option<String>,
    pub control_port: Option<u16>,
    pub traces_dir: Option<PathBuf>,
}

/// Programmatic construction for lib embedders (ADR-009 §6) — no file,
/// no env reads beyond [`Config::default`]'s cache-dir probe (set
/// `cache_dir` explicitly to avoid even that).
pub struct ConfigBuilder {
    cfg: Config,
}

impl ConfigBuilder {
    pub fn addr(mut self, v: impl Into<String>) -> Self {
        self.cfg.addr = v.into();
        self
    }

    pub fn agent_space(mut self, v: impl Into<String>) -> Self {
        self.cfg.agent_space = v.into();
        self
    }

    pub fn agent_name(mut self, v: impl Into<String>) -> Self {
        self.cfg.agent_name = v.into();
        self
    }

    pub fn control_port(mut self, v: u16) -> Self {
        self.cfg.control_port = v;
        self
    }

    /// Add one overlay (`name = spaceId`, strictly ids — ADR-009 §2).
    pub fn overlay(mut self, name: impl Into<String>, space_id: impl Into<String>) -> Self {
        self.cfg.overlays.insert(
            name.into(),
            Overlay {
                space: space_id.into(),
                invite: None,
            },
        );
        self
    }

    /// Overlay with a RequestToJoin invite for join-on-boot (§8).
    pub fn overlay_with_invite(
        mut self,
        name: impl Into<String>,
        space_id: impl Into<String>,
        invite: impl Into<String>,
    ) -> Self {
        self.cfg.overlays.insert(
            name.into(),
            Overlay {
                space: space_id.into(),
                invite: Some(invite.into()),
            },
        );
        self
    }

    pub fn traces_dir(mut self, v: impl Into<PathBuf>) -> Self {
        self.cfg.traces_dir = v.into();
        self
    }

    pub fn cache_dir(mut self, v: impl Into<PathBuf>) -> Self {
        self.cfg.cache_dir = v.into();
        self
    }

    /// Explicit local kernel (dev bypass); unset = fetch from the space.
    pub fn kernel_path(mut self, v: impl Into<PathBuf>) -> Self {
        self.cfg.kernel = Some(v.into());
        self
    }

    /// One guest-visible config key (the `[config]` cascade layer).
    pub fn config_value(mut self, key: impl Into<String>, value: Value) -> Self {
        self.cfg.config.insert(key.into(), value);
        self
    }

    pub fn secret(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.cfg.secrets.insert(key.into(), value.into());
        self
    }

    pub fn build(self) -> Config {
        self.cfg
    }
}

impl Config {
    pub fn builder() -> ConfigBuilder {
        ConfigBuilder {
            cfg: Config::default(),
        }
    }

    /// CLI entry: read `path` (or `./anybao.toml` when present — no
    /// file at all is fine) and resolve over the defaults. An explicit
    /// `--config-file` that doesn't exist is an error; the implicit
    /// default is optional.
    pub fn load(path: Option<&Path>) -> Result<Config> {
        let text = match path {
            Some(p) => Some(
                std::fs::read_to_string(p)
                    .with_context(|| format!("config file at {}", p.display()))?,
            ),
            None => {
                let p = Path::new(DEFAULT_CONFIG_FILE);
                p.exists().then(|| std::fs::read_to_string(p)).transpose()?
            }
        };
        match text {
            Some(t) => Config::from_toml(&t),
            None => Ok(Config::default()),
        }
    }

    pub fn from_toml(text: &str) -> Result<Config> {
        let fc: FileConfig = toml::from_str(text).context("anybao.toml")?;
        Config::from_file(fc)
    }

    pub fn from_file(fc: FileConfig) -> Result<Config> {
        let mut c = Config::default();
        if let Some(addr) = fc.addr {
            c.addr = addr;
        }
        if let Some(space) = fc.agent.space {
            c.agent_space = space;
        }
        if let Some(name) = fc.agent.name {
            c.agent_name = name;
        }
        if let Some(port) = fc.agent.control_port {
            c.control_port = port;
        }
        c.overlays = fc
            .overlays
            .into_iter()
            .map(|(name, entry)| (name, entry.into()))
            .collect();
        if let Some(traces) = fc.paths.traces {
            c.traces_dir = traces;
        }
        if let Some(cache) = fc.paths.cache {
            c.cache_dir = expand_tilde(&cache);
        }
        for (key, value) in fc.config {
            c.config.insert(key, toml_to_json(value)?);
        }
        Ok(c)
    }

    pub fn apply(&mut self, o: CliOverrides) {
        if let Some(addr) = o.addr {
            self.addr = addr;
        }
        if let Some(space) = o.space {
            self.agent_space = space;
        }
        if let Some(name) = o.agent_name {
            self.agent_name = name;
        }
        if let Some(port) = o.control_port {
            self.control_port = port;
        }
        if let Some(traces) = o.traces_dir {
            self.traces_dir = traces;
        }
    }
}

/// `$XDG_CACHE_HOME/anybao`, falling back `~/.cache/anybao`. Env reads
/// live here (CLI-side resolution); lib embedders set `cache_dir`
/// explicitly and never hit this.
pub fn default_cache_dir() -> PathBuf {
    match std::env::var_os("XDG_CACHE_HOME").filter(|v| !v.is_empty()) {
        Some(base) => PathBuf::from(base).join("anybao"),
        None => match std::env::var_os("HOME") {
            Some(home) => PathBuf::from(home).join(".cache").join("anybao"),
            None => PathBuf::from(".cache/anybao"),
        },
    }
}

fn expand_tilde(p: &Path) -> PathBuf {
    match p.strip_prefix("~") {
        Ok(rest) => match std::env::var_os("HOME") {
            Some(home) => PathBuf::from(home).join(rest),
            None => p.to_path_buf(),
        },
        Err(_) => p.to_path_buf(),
    }
}

/// TOML → JSON for the `[config]` guest layer. Datetimes are rejected —
/// no guest config key is a date, and letting one through would leak
/// toml's private serialization shape into `config.get`.
fn toml_to_json(v: toml::Value) -> Result<Value> {
    Ok(match v {
        toml::Value::String(s) => Value::String(s),
        toml::Value::Integer(i) => Value::from(i),
        toml::Value::Float(f) => Value::from(f),
        toml::Value::Boolean(b) => Value::Bool(b),
        toml::Value::Datetime(d) => bail!("datetime not allowed in [config]: {d}"),
        toml::Value::Array(items) => Value::Array(
            items
                .into_iter()
                .map(toml_to_json)
                .collect::<Result<Vec<_>>>()?,
        ),
        toml::Value::Table(t) => Value::Object(
            t.into_iter()
                .map(|(k, v)| Ok((k, toml_to_json(v)?)))
                .collect::<Result<serde_json::Map<_, _>>>()?,
        ),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const FULL: &str = r#"
addr = "http://127.0.0.1:9999"

[agent]
space = "myspace"
name = "mybot"
control_port = 7777

[overlays]
agent = "bafyagent"
std = "bafystd"

[paths]
traces = "t"
cache = "/var/cache/anybao"

[config]
"agent.persona" = "terse"
"llm.tier.chat" = { provider = "anthropic", model = "m" }
"#;

    #[test]
    fn full_example_parses() {
        let c = Config::from_toml(FULL).unwrap();
        assert_eq!(c.addr, "http://127.0.0.1:9999");
        assert_eq!(c.agent_space, "myspace");
        assert_eq!(c.agent_name, "mybot");
        assert_eq!(c.control_port, 7777);
        assert_eq!(c.overlays["agent"].space, "bafyagent");
        assert_eq!(c.overlays["agent"].invite, None);
        assert_eq!(c.overlays["std"].space, "bafystd");
        assert_eq!(c.traces_dir, PathBuf::from("t"));
        assert_eq!(c.cache_dir, PathBuf::from("/var/cache/anybao"));
        assert_eq!(c.config["agent.persona"], json!("terse"));
        assert_eq!(
            c.config["llm.tier.chat"],
            json!({"provider": "anthropic", "model": "m"})
        );
    }

    #[test]
    fn empty_file_is_all_defaults() {
        let c = Config::from_toml("").unwrap();
        assert_eq!(c.addr, DEFAULT_ADDR);
        assert_eq!(c.agent_space, "bao");
        assert_eq!(c.agent_name, "bao");
        assert_eq!(c.control_port, 7010);
        assert!(c.overlays.is_empty());
        assert_eq!(c.traces_dir, PathBuf::from("traces"));
        assert_eq!(c.kernel, None); // no override: kernel comes from the space
    }

    #[test]
    fn unknown_keys_fail_loudly() {
        assert!(Config::from_toml("adr = \"typo\"").is_err());
        assert!(Config::from_toml("[agent]\nspaec = \"x\"").is_err());
        assert!(Config::from_toml("[paths]\ntrace = \"x\"").is_err());
    }

    #[test]
    fn overlay_table_form_carries_invite() {
        let c = Config::from_toml(
            "[overlays]\nagent = { space = \"bafy1\", invite = \"tok\" }\nstd = \"bafy2\"",
        )
        .unwrap();
        assert_eq!(
            c.overlays["agent"],
            Overlay {
                space: "bafy1".into(),
                invite: Some("tok".into())
            }
        );
        assert_eq!(c.overlays["std"].space, "bafy2");
        assert_eq!(c.overlays["std"].invite, None);
        // a typo'd key inside the table still fails loudly
        assert!(Config::from_toml("[overlays]\nagent = { spaec = \"x\" }").is_err());
    }

    #[test]
    fn cli_overrides_shadow_file() {
        let mut c = Config::from_toml(FULL).unwrap();
        c.apply(CliOverrides {
            addr: Some("http://cli:1".into()),
            control_port: Some(1),
            ..Default::default()
        });
        assert_eq!(c.addr, "http://cli:1"); // CLI wins
        assert_eq!(c.control_port, 1);
        assert_eq!(c.agent_space, "myspace"); // untouched flags keep file value
    }

    #[test]
    fn config_table_seeds_under_json_overrides() {
        // cascade: [config] table < --config JSON (insert overwrites)
        let mut c = Config::from_toml(FULL).unwrap();
        c.config.insert("agent.persona".into(), json!("verbose"));
        assert_eq!(c.config["agent.persona"], json!("verbose"));
        assert_eq!(c.config["llm.tier.chat"]["provider"], json!("anthropic"));
    }

    #[test]
    fn datetime_in_config_rejected() {
        let e = Config::from_toml("[config]\n\"a.b\" = 1979-05-27").unwrap_err();
        assert!(e.to_string().contains("datetime"), "{e}");
    }

    #[test]
    fn tilde_cache_expands_against_home() {
        let c = Config::from_toml("[paths]\ncache = \"~/.cache/anybao\"").unwrap();
        if let Some(home) = std::env::var_os("HOME") {
            assert_eq!(c.cache_dir, PathBuf::from(home).join(".cache/anybao"));
        }
    }
}
