//! Module resolvers — resolve `[alias:]name@vN` program specs into the
//! `module.resolve` effect reply shape (ADR-004):
//! `{spaceId, objectId, marker, sourceHash, source, cache}`.
//!
//! `AnyModuleResolver` is the Rust twin of anybao/modules.py: programs
//! read back from `any` spaces (the deploy tool's counterpart), with
//! the probe cache keyed on (objectId, marker) — `_addSeq` of the
//! `program_source` main record (ADR-004 §4). `LocalDirResolver` keeps
//! the existing broker `sys_module_resolve` behavior (programs/*.py) so
//! the two can compose: local dir OR space-backed.

// the ported surface IS the contract; the bin grows into it
#![allow(dead_code)]

use crate::anyapi::{AnyError, Client};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::path::PathBuf;

pub const PROGRAM_TYPE: &str = "program";

#[derive(Debug)]
pub enum ResolveError {
    /// bad module spec (need name@vN)
    BadSpec(String),
    /// unknown alias in spec
    UnknownAlias(String),
    /// program (or its source record) not found
    NotFound(String),
    /// any-server call failed
    Api(AnyError),
}

impl fmt::Display for ResolveError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ResolveError::BadSpec(s) => write!(f, "bad module spec (need name@vN): {s:?}"),
            ResolveError::UnknownAlias(a) => write!(f, "unknown alias in spec: {a:?}"),
            ResolveError::NotFound(m) => write!(f, "{m}"),
            ResolveError::Api(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for ResolveError {}

impl From<AnyError> for ResolveError {
    fn from(e: AnyError) -> Self {
        ResolveError::Api(e)
    }
}

/// The one resolver surface — main.rs composes implementations (local
/// dir first, space-backed fallback, or either alone). `frm` is the
/// requester's object id for defining-space resolution (reserved, as in
/// the Python reference).
pub trait ModuleResolver {
    fn resolve(&mut self, spec: &str, frm: Option<&str>) -> Result<Value, ResolveError>;
}

/// (alias|None, name, version) from `[alias:]name@vN`.
fn parse(spec: &str) -> Result<(Option<&str>, &str, &str), ResolveError> {
    let (alias, rest) = match spec.split_once(':') {
        Some((a, r)) => (Some(a), r),
        None => (None, spec),
    };
    let Some((name, version)) = rest.rsplit_once('@') else {
        return Err(ResolveError::BadSpec(spec.to_string()));
    };
    Ok((alias, name, version))
}

/// Space-backed resolution (ADR-004 §2): `name@vN` → current space then
/// private fallback; `alias:name@vN` (agent:/std:/private:) → that
/// space, strict; `<spaceId>:name@vN` → strict.
pub struct AnyModuleResolver {
    client: std::sync::Arc<Client>,
    current: String,
    private: Option<String>,
    /// e.g. {"agent": <overlay space id>, "std": ...}
    aliases: BTreeMap<String, String>,
    /// probe cache: (objectId, marker) pairs already served
    probed: BTreeSet<(String, i64)>,
}

impl AnyModuleResolver {
    pub fn new(
        client: std::sync::Arc<Client>,
        current_space: &str,
        private_space: Option<&str>,
        aliases: BTreeMap<String, String>,
    ) -> Self {
        AnyModuleResolver {
            client,
            current: current_space.to_string(),
            private: private_space.map(str::to_string),
            aliases,
            probed: BTreeSet::new(),
        }
    }

    fn resolve_in(
        &mut self,
        space: &str,
        name: &str,
        version: &str,
        spec: &str,
    ) -> Result<Value, ResolveError> {
        let recs = self.client.query_objects(
            space,
            &json!({"filter": {(format!("{PROGRAM_TYPE}.name")): name,
                               (format!("{PROGRAM_TYPE}.version")): version},
                    "limit": 1}),
        )?;
        let Some(oid) = recs.first().and_then(|r| r["id"].as_str()) else {
            return Err(ResolveError::NotFound(format!(
                "program not found: {spec} (space {space})"
            )));
        };
        let src = self
            .client
            .query(space, oid, "program_source", &json!({}))?;
        let Some(main) = src.first() else {
            return Err(ResolveError::NotFound(format!(
                "program {spec} has no source record"
            )));
        };
        let code = main["code"].as_str().unwrap_or("");
        let marker = main["_addSeq"].as_i64().unwrap_or(0); // probe-cache key (ADR-004 §4)
        let cache = if self.probed.insert((oid.to_string(), marker)) {
            "miss"
        } else {
            "hit"
        };
        Ok(json!({
            "spaceId": space, "objectId": oid, "marker": marker,
            "sourceHash": format!("sha256:{}", hex::encode(Sha256::digest(code.as_bytes()))),
            "source": code, "cache": cache,
        }))
    }
}

impl ModuleResolver for AnyModuleResolver {
    fn resolve(&mut self, spec: &str, _frm: Option<&str>) -> Result<Value, ResolveError> {
        let (alias, name, version) = parse(spec)?;
        let (name, version) = (name.to_string(), version.to_string());
        if let Some(alias) = alias {
            let space = if alias == "private" {
                self.private.clone()
            } else if let Some(s) = self.aliases.get(alias) {
                Some(s.clone())
            } else {
                Some(alias.to_string()) // a raw spaceId prefix
            };
            let space = space
                .filter(|s| !s.is_empty())
                .ok_or_else(|| ResolveError::UnknownAlias(alias.to_string()))?;
            return self.resolve_in(&space, &name, &version, spec);
        }
        // unqualified: current (or the requester's defining space) → private
        let base = self.current.clone();
        match self.resolve_in(&base, &name, &version, spec) {
            Err(ResolveError::NotFound(msg)) => {
                match self.private.clone().filter(|p| *p != base) {
                    Some(p) => self.resolve_in(&p, &name, &version, spec),
                    None => Err(ResolveError::NotFound(msg)), // re-raise the original
                }
            }
            other => other,
        }
    }
}

/// Filesystem resolution over `programs/*.py` — the broker's existing
/// `sys_module_resolve`, extracted so it composes with the space-backed
/// path. Cache is keyed by spec: a hit replays the cached reply with
/// `cache: "hit"`.
pub struct LocalDirResolver {
    programs_dir: PathBuf,
    cache: BTreeMap<String, Value>,
}

impl LocalDirResolver {
    pub fn new(programs_dir: PathBuf) -> Self {
        LocalDirResolver {
            programs_dir,
            cache: BTreeMap::new(),
        }
    }
}

impl ModuleResolver for LocalDirResolver {
    fn resolve(&mut self, spec: &str, _frm: Option<&str>) -> Result<Value, ResolveError> {
        if let Some(cached) = self.cache.get(spec) {
            let mut out = cached.clone();
            out["cache"] = json!("hit");
            return Ok(out);
        }
        let path = self.programs_dir.join(format!("{spec}.py"));
        let source = std::fs::read_to_string(&path).map_err(|_| {
            ResolveError::NotFound(format!("program not found: {spec} ({})", path.display()))
        })?;
        let out = json!({
            "spaceId": "local", "objectId": spec, "marker": 0,
            "sourceHash": format!("sha256:{}", hex::encode(Sha256::digest(source.as_bytes()))),
            "source": source, "cache": "miss",
        });
        self.cache.insert(spec.to_string(), out.clone());
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::FakeSpace;
    use tempfile::tempdir;

    const CODE: &str = "def main(args):\n    return 1\n";

    fn sha(code: &str) -> String {
        format!("sha256:{}", hex::encode(Sha256::digest(code.as_bytes())))
    }

    fn space_with_program(space: &str, name: &str, version: &str, code: &str) -> Client {
        let c = Client::with_transport(Box::new(FakeSpace::new()));
        seed_program(&c, space, name, version, code);
        c
    }

    fn seed_program(c: &Client, space: &str, name: &str, version: &str, code: &str) {
        let res = c
            .create_object(
                space,
                &json!({"types": ["program"],
                        "initialProperties": {"any": {"name": name},
                                              "program": {"name": name, "version": version}}}),
            )
            .unwrap();
        let oid = res["objectId"].as_str().unwrap().to_string();
        c.upsert_record(
            space,
            &oid,
            "program_source",
            "main",
            &json!({"code": code}),
        )
        .unwrap();
    }

    #[test]
    fn resolves_in_current_space_with_reply_shape() {
        let c = space_with_program("cur", "tool", "v1", CODE);
        let mut r = AnyModuleResolver::new(std::sync::Arc::new(c), "cur", None, BTreeMap::new());
        let out = r.resolve("tool@v1", None).unwrap();
        assert_eq!(
            out,
            json!({"spaceId": "cur", "objectId": "obj1", "marker": 1,
                   "sourceHash": sha(CODE), "source": CODE, "cache": "miss"})
        );
        // same (objectId, marker) → probe-cache hit
        assert_eq!(r.resolve("tool@v1", None).unwrap()["cache"], json!("hit"));
    }

    #[test]
    fn falls_back_to_private_space() {
        let c = Client::with_transport(Box::new(FakeSpace::new()));
        seed_program(&c, "priv", "tool", "v1", CODE);
        let mut r =
            AnyModuleResolver::new(std::sync::Arc::new(c), "cur", Some("priv"), BTreeMap::new());
        let out = r.resolve("tool@v1", None).unwrap();
        assert_eq!(out["spaceId"], json!("priv"));
    }

    #[test]
    fn alias_resolution_is_strict() {
        let c = Client::with_transport(Box::new(FakeSpace::new()));
        seed_program(&c, "overlay", "tool", "v1", CODE);
        let mut aliases = BTreeMap::new();
        aliases.insert("agent".to_string(), "overlay".to_string());
        let mut r = AnyModuleResolver::new(std::sync::Arc::new(c), "cur", Some("priv"), aliases);
        assert_eq!(
            r.resolve("agent:tool@v1", None).unwrap()["spaceId"],
            json!("overlay")
        );
        // aliased miss does NOT fall back to private
        assert!(matches!(
            r.resolve("agent:other@v1", None),
            Err(ResolveError::NotFound(_))
        ));
        // unknown alias without private → raw spaceId, strict
        assert!(matches!(
            r.resolve("someSpaceId:tool@v1", None),
            Err(ResolveError::NotFound(_))
        ));
    }

    #[test]
    fn private_alias_without_private_space_errors() {
        let c = Client::with_transport(Box::new(FakeSpace::new()));
        let mut r = AnyModuleResolver::new(std::sync::Arc::new(c), "cur", None, BTreeMap::new());
        assert!(matches!(
            r.resolve("private:tool@v1", None),
            Err(ResolveError::UnknownAlias(a)) if a == "private"
        ));
    }

    #[test]
    fn bad_spec_and_missing_source_record() {
        let c = std::sync::Arc::new(Client::with_transport(Box::new(FakeSpace::new())));
        let mut r = AnyModuleResolver::new(c.clone(), "cur", None, BTreeMap::new());
        assert!(matches!(
            r.resolve("noversion", None),
            Err(ResolveError::BadSpec(_))
        ));
        // program object exists but has no program_source main record
        c.create_object(
            "cur",
            &json!({"types": ["program"],
                    "initialProperties": {"program": {"name": "hollow", "version": "v1"}}}),
        )
        .unwrap();
        let err = r.resolve("hollow@v1", None).unwrap_err();
        assert!(err.to_string().contains("has no source record"));
    }

    #[test]
    fn marker_change_is_a_probe_miss() {
        let c = std::sync::Arc::new(space_with_program("cur", "tool", "v1", CODE));
        let mut r = AnyModuleResolver::new(c.clone(), "cur", None, BTreeMap::new());
        assert_eq!(r.resolve("tool@v1", None).unwrap()["cache"], json!("miss"));
        // rewrite the source record — _addSeq bumps → new marker → miss
        c.upsert_record(
            "cur",
            "obj1",
            "program_source",
            "main",
            &json!({"code": "x = 2\n"}),
        )
        .unwrap();
        let out = r.resolve("tool@v1", None).unwrap();
        assert_eq!(out["cache"], json!("miss"));
        assert_eq!(out["marker"], json!(2));
        assert_eq!(out["source"], json!("x = 2\n"));
    }

    #[test]
    fn local_dir_resolver_reads_and_caches() {
        let dir = tempdir().unwrap();
        std::fs::write(dir.path().join("tool@v1.py"), CODE).unwrap();
        let mut r = LocalDirResolver::new(dir.path().to_path_buf());
        let out = r.resolve("tool@v1", None).unwrap();
        assert_eq!(
            out,
            json!({"spaceId": "local", "objectId": "tool@v1", "marker": 0,
                   "sourceHash": sha(CODE), "source": CODE, "cache": "miss"})
        );
        // second resolve replays the cached reply with cache: hit —
        // even if the file changed on disk (the broker contract)
        std::fs::write(dir.path().join("tool@v1.py"), "changed").unwrap();
        let again = r.resolve("tool@v1", None).unwrap();
        assert_eq!(again["cache"], json!("hit"));
        assert_eq!(again["source"], json!(CODE));
    }

    #[test]
    fn local_dir_resolver_not_found() {
        let dir = tempdir().unwrap();
        let mut r = LocalDirResolver::new(dir.path().to_path_buf());
        let err = r.resolve("ghost@v9", None).unwrap_err();
        assert!(err.to_string().starts_with("program not found: ghost@v9"));
    }
}
