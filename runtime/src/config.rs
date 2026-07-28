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
    /// explicit local kernel override (dev, ADR-009 §4);
    /// None = the kernel embedded in the binary
    pub kernel: Option<PathBuf>,
    /// guest-visible config map (pre-`bootstrap`)
    pub config: BTreeMap<String, Value>,
    pub secrets: BTreeMap<String, String>,
    /// Hard secret seeds — write-through to the device-local store at
    /// serve start (rotate when different, EMPTY VALUE DELETES), unlike
    /// `secrets` whose entries are soft (stored value wins). Ref names
    /// are open — any `connector.key.<x>` (or other ref) is accepted.
    /// Sources: a `.connectors.env` beside the config file (CLI), or an
    /// embedder feeding parsed keys from memory (any-ui import dialog /
    /// bundled demo seeds).
    pub secret_overrides: BTreeMap<String, String>,
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
            kernel: None,
            config: BTreeMap::new(),
            secrets: BTreeMap::new(),
            secret_overrides: BTreeMap::new(),
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

/// Programmatic construction for lib embedders (ADR-009 §6) — no
/// file, no env reads.
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

    /// Explicit local kernel (dev); unset = the embedded kernel.
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

    /// Hard secret seed — persisted device-locally at serve start,
    /// overwriting a stored value that differs (rotation); an empty
    /// value deletes the stored secret. See [`Config::secret_overrides`].
    pub fn secret_override(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.cfg.secret_overrides.insert(key.into(), value.into());
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
    /// default is optional. A [`SECRETS_ENV_FILE`] sibling of the
    /// config file (or in cwd when no config path resolves) is parsed
    /// into `secret_overrides` — the hard-seed rotation path.
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
        let mut cfg = match text {
            Some(t) => Config::from_toml(&t)?,
            None => Config::default(),
        };
        let secrets_dir = path
            .and_then(Path::parent)
            .filter(|d| !d.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        let secrets_path = secrets_dir.join(SECRETS_ENV_FILE);
        if let Ok(t) = std::fs::read_to_string(&secrets_path) {
            cfg.secret_overrides.append(&mut parse_secrets_env(&t));
        }
        Ok(cfg)
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

/// Config defaults (ADR-006 §3): the harness's DEFAULT layer, sourced
/// from `config_defaults.json` (embedded at build — data, not Rust
/// literals; edit the json to change model/tier defaults). Sits under
/// any `--config` file and the space-scope override read off the config
/// object at serve start.
pub const CONFIG_DEFAULTS: &str = include_str!("config_defaults.json");

/// Secret refs with env seeding + device-local persistence: (secrets-map
/// ref / config record key, seeding env var). One list drives both
/// [`bootstrap_maps`] (env → `secrets`) and serve's device-local store
/// (ADR-006 §3). LLM/search providers plus connector keys for the
/// connectors overlay (ADR-008 §1: the secret-ref pattern needs no new
/// mechanism per provider — a new connector is one line here).
pub const PROVIDER_SECRET_REFS: &[(&str, &str)] = &[
    ("llm.key.anthropic", "ANTHROPIC_API_KEY"),
    ("google.key.gemini", "GEMINI_API_KEY"),
    ("llm.key.together", "TOGETHER_API_KEY"),
    ("connector.key.linear", "LINEAR_API_KEY"),
    ("connector.key.github", "GITHUB_TOKEN"),
    ("connector.key.granola", "GRANOLA_API_KEY"),
    ("connector.key.attio", "ATTIO_API_TOKEN"),
    ("connector.key.figma", "FIGMA_TOKEN"),
    ("connector.key.intercom", "INTERCOM_ACCESS_TOKEN"),
];

/// The dotenv-style hard-seed file picked up from the config file's
/// directory (fallback: cwd). Lines are `ref=value` keyed by the SECRET
/// REF itself (`connector.key.linear=lin_…`), not env-var names; `#`
/// comments and blank lines ignored, optional single/double quotes
/// stripped. Ref names are open — unknown refs are stored too, so a new
/// connector needs no runtime change. An EMPTY value deletes the stored
/// secret (explicit revoke). This is the pre-secret-system shortcut:
/// values sit in plaintext — keep the file out of version control.
pub const SECRETS_ENV_FILE: &str = ".connectors.env";

/// Parse [`SECRETS_ENV_FILE`] content → hard-seed map. Malformed lines
/// (no `=`) are skipped.
pub fn parse_secrets_env(text: &str) -> BTreeMap<String, String> {
    let mut map = BTreeMap::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim();
        if key.is_empty() {
            continue;
        }
        let value = value.trim();
        let value = value
            .strip_prefix('"')
            .and_then(|v| v.strip_suffix('"'))
            .or_else(|| value.strip_prefix('\'').and_then(|v| v.strip_suffix('\'')))
            .unwrap_or(value);
        map.insert(key.to_string(), value.to_string());
    }
    map
}

/// Guest-config bootstrap: seed `any.base_url` from `addr`, layer
/// `CONFIG_DEFAULTS` under existing keys, pick up provider API keys
/// from env ([`PROVIDER_SECRET_REFS`] — or_insert; ADR-008 §1: keys
/// enter `secrets`, never config). The ONE env-reading lib fn —
/// embedders call it after building a Config, or seed the maps
/// themselves and skip it.
pub fn bootstrap_maps(
    config: &mut BTreeMap<String, Value>,
    secrets: &mut BTreeMap<String, String>,
    addr: &str,
) {
    config
        .entry("any.base_url".into())
        .or_insert_with(|| Value::String(addr.to_string()));
    let defaults: BTreeMap<String, Value> =
        serde_json::from_str(CONFIG_DEFAULTS).expect("config_defaults.json is valid JSON");
    for (key, value) in defaults {
        config.entry(key).or_insert(value);
    }
    for &(secret_ref, env_var) in PROVIDER_SECRET_REFS {
        if let Ok(key) = std::env::var(env_var) {
            secrets.entry(secret_ref.into()).or_insert(key);
        }
    }
}

/// [`bootstrap_maps`] over a whole [`Config`] — the embedder's form.
pub fn bootstrap(cfg: &mut Config) {
    let addr = cfg.addr.clone();
    bootstrap_maps(&mut cfg.config, &mut cfg.secrets, &addr);
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
    fn secrets_env_parses_generically() {
        let m = parse_secrets_env(
            "# comment\n\
             connector.key.linear=lin_123\n\
             connector.key.github = \"gho_456\"  \n\
             connector.key.customxyz='abc'\n\
             connector.key.granola=\n\
             malformed line\n\
             =novalue\n\
             \n\
             llm.key.anthropic=sk-ant-1",
        );
        assert_eq!(m["connector.key.linear"], "lin_123");
        assert_eq!(m["connector.key.github"], "gho_456"); // quotes stripped
        assert_eq!(m["connector.key.customxyz"], "abc"); // unknown ref accepted
        assert_eq!(m["connector.key.granola"], ""); // empty = delete request
        assert_eq!(m["llm.key.anthropic"], "sk-ant-1");
        assert_eq!(m.len(), 5); // malformed + keyless lines skipped
    }

    #[test]
    fn load_picks_up_secrets_env_beside_config_file() {
        let dir = tempfile::tempdir().unwrap();
        let cfg_path = dir.path().join("anybao.toml");
        std::fs::write(&cfg_path, "[agent]\nspace = \"s\"\n").unwrap();
        std::fs::write(
            dir.path().join(SECRETS_ENV_FILE),
            "connector.key.linear=lin_999\n",
        )
        .unwrap();
        let c = Config::load(Some(&cfg_path)).unwrap();
        assert_eq!(c.secret_overrides["connector.key.linear"], "lin_999");
        assert!(c.secrets.is_empty()); // hard seeds land in overrides only
    }

    #[test]
    fn load_without_secrets_file_leaves_overrides_empty() {
        let dir = tempfile::tempdir().unwrap();
        let cfg_path = dir.path().join("anybao.toml");
        std::fs::write(&cfg_path, "").unwrap();
        let c = Config::load(Some(&cfg_path)).unwrap();
        assert!(c.secret_overrides.is_empty());
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
    fn bootstrap_layers_defaults_under_existing_keys() {
        let mut c = Config::builder()
            .addr("http://x:1")
            .config_value("llm.tier.codegen", json!({"provider": "mine"}))
            .build();
        bootstrap(&mut c);
        // existing key wins; absent defaults land; addr seeds base_url
        assert_eq!(c.config["llm.tier.codegen"]["provider"], json!("mine"));
        assert_eq!(c.config["any.base_url"], json!("http://x:1"));
        assert!(c.config.contains_key("llm.tier.classify"));
        // a pre-set base_url is never clobbered
        let mut c2 = Config::builder().addr("http://y:2").build();
        c2.config
            .insert("any.base_url".into(), json!("http://kept:9"));
        bootstrap(&mut c2);
        assert_eq!(c2.config["any.base_url"], json!("http://kept:9"));
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
}
