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
    /// objectId → defining space of every module served — the frame
    /// chain's `frm` resolves transitive imports against this
    /// (ADR-004 §2.4)
    origins: BTreeMap<String, String>,
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
            origins: BTreeMap::new(),
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
        // remember where this module lives — its own use() calls
        // resolve here via the frame chain (ADR-004 §2.4)
        self.origins.insert(oid.to_string(), space.to_string());
        Ok(json!({
            "spaceId": space, "objectId": oid, "marker": marker,
            "sourceHash": format!("sha256:{}", hex::encode(Sha256::digest(code.as_bytes()))),
            "source": code, "cache": cache,
        }))
    }
}

impl ModuleResolver for AnyModuleResolver {
    fn resolve(&mut self, spec: &str, frm: Option<&str>) -> Result<Value, ResolveError> {
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
        // Transitive (ADR-004 §2.4): an unqualified import INSIDE a
        // loaded module resolves in that module's DEFINING space,
        // strictly — overlay programs are self-contained; consumer
        // shadowing is an explicit config pin, never a silent local
        // fallback.
        if let Some(def_space) = frm.and_then(|oid| self.origins.get(oid)).cloned() {
            if def_space != self.current {
                return self.resolve_in(&def_space, &name, &version, spec);
            }
        }
        // unqualified from cell code (or a working-space module):
        // current space → private fallback
        let base = self.current.clone();
        match self.resolve_in(&base, &name, &version, spec) {
            Err(ResolveError::NotFound(msg)) => match self.private.clone().filter(|p| *p != base) {
                Some(p) => self
                    .resolve_in(&p, &name, &version, spec)
                    .map_err(|e| self.alias_hint(e, &name, &version)),
                None => Err(self.alias_hint(ResolveError::NotFound(msg), &name, &version)),
            },
            other => other,
        }
    }
}

impl AnyModuleResolver {
    /// An unqualified miss usually means an overlay module named without
    /// its alias (E10: `use("any@v1")` where `use("agent:any@v1")` was
    /// meant) — teach the fix in the error instead of costing a turn.
    fn alias_hint(&self, err: ResolveError, name: &str, version: &str) -> ResolveError {
        let ResolveError::NotFound(msg) = err else {
            return err;
        };
        let mut aliases: Vec<&str> = self.aliases.keys().map(String::as_str).collect();
        if self.private.is_some() && !aliases.contains(&"private") {
            aliases.push("private");
        }
        if aliases.is_empty() {
            return ResolveError::NotFound(msg);
        }
        ResolveError::NotFound(format!(
            "{msg} — unqualified specs resolve in the current space only; \
             overlay modules need their alias, e.g. use(\"{first}:{name}@{version}\") \
             (available aliases: {all})",
            first = aliases[0],
            all = aliases.join(", "),
        ))
    }
}

/// Source path for a spec in a local programs dir — the same two
/// layouts `deploy::load_programs` reads: flat `<spec>.py`, else the
/// tool-authoring folder `<spec>/program.py`.
pub fn local_source_path(dir: &std::path::Path, spec: &str) -> PathBuf {
    let flat = dir.join(format!("{spec}.py"));
    if flat.exists() {
        flat
    } else {
        dir.join(spec).join("program.py")
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
        let path = local_source_path(&self.programs_dir, spec);
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
    fn unqualified_miss_hints_alias_qualification() {
        // E10: the miss error must teach the agent: prefix, naming the
        // configured aliases — a bare "not found" costs a guess turn
        let c = Client::with_transport(Box::new(FakeSpace::new()));
        let aliases = BTreeMap::from([
            ("agent".to_string(), "ovl".to_string()),
            ("connectors".to_string(), "conn".to_string()),
        ]);
        let mut r = AnyModuleResolver::new(std::sync::Arc::new(c), "cur", None, aliases);
        let err = r.resolve("any@v1", None).unwrap_err().to_string();
        assert!(err.starts_with("program not found: any@v1"), "{err}");
        assert!(err.contains("use(\"agent:any@v1\")"), "{err}");
        assert!(
            err.contains("available aliases: agent, connectors"),
            "{err}"
        );
        // qualified misses stay strict and unhinted
        let err = r.resolve("ovl:nope@v1", None).unwrap_err().to_string();
        assert!(!err.contains("available aliases"), "{err}");
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
    fn unqualified_never_resolves_into_overlays() {
        // ADR-004 §2 / ADR-009 §2: a program living only in an overlay
        // is invisible to unqualified specs — the alias is the only way
        let c = Client::with_transport(Box::new(FakeSpace::new()));
        seed_program(&c, "code", "tool", "v1", CODE);
        let mut aliases = BTreeMap::new();
        aliases.insert("agent".to_string(), "code".to_string());
        let mut r = AnyModuleResolver::new(std::sync::Arc::new(c), "user", None, aliases);
        assert!(matches!(
            r.resolve("tool@v1", None),
            Err(ResolveError::NotFound(_))
        ));
        assert_eq!(
            r.resolve("agent:tool@v1", None).unwrap()["spaceId"],
            json!("code")
        );
    }

    #[test]
    fn transitive_imports_resolve_in_the_defining_space() {
        // ADR-004 §2.4: agent:toolcaller's own use("any@v1") resolves
        // in the repo overlay, not the consumer's working space — the
        // exact two-account failure this pins (any@v1 exists ONLY in
        // the code space)
        let c = Client::with_transport(Box::new(FakeSpace::new()));
        seed_program(&c, "code", "toolcaller", "v1", CODE);
        seed_program(&c, "code", "any", "v1", CODE);
        let mut aliases = BTreeMap::new();
        aliases.insert("agent".to_string(), "code".to_string());
        let mut r = AnyModuleResolver::new(std::sync::Arc::new(c), "user", None, aliases);

        let tc = r.resolve("agent:toolcaller@v1", None).unwrap();
        let tc_oid = tc["objectId"].as_str().unwrap().to_string();
        let dep = r.resolve("any@v1", Some(&tc_oid)).unwrap();
        assert_eq!(dep["spaceId"], json!("code"));
    }

    #[test]
    fn transitive_miss_never_falls_back_locally() {
        // a dep missing from the overlay must NOT silently resolve in
        // the consumer's space (supply-chain surprise) — strict error
        let c = Client::with_transport(Box::new(FakeSpace::new()));
        seed_program(&c, "code", "toolcaller", "v1", CODE);
        seed_program(&c, "user", "helper", "v1", CODE); // only user-side
        let mut aliases = BTreeMap::new();
        aliases.insert("agent".to_string(), "code".to_string());
        let mut r = AnyModuleResolver::new(std::sync::Arc::new(c), "user", None, aliases);

        let tc = r.resolve("agent:toolcaller@v1", None).unwrap();
        let tc_oid = tc["objectId"].as_str().unwrap().to_string();
        assert!(matches!(
            r.resolve("helper@v1", Some(&tc_oid)),
            Err(ResolveError::NotFound(_))
        ));
        // while cell code (no frame) still reaches the user-space helper
        assert_eq!(
            r.resolve("helper@v1", None).unwrap()["spaceId"],
            json!("user")
        );
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
    fn local_dir_resolver_reads_folder_layout() {
        // the tool-authoring layout: <spec>/program.py (deploy.rs twin);
        // a flat <spec>.py, when both exist, wins
        let dir = tempdir().unwrap();
        std::fs::create_dir(dir.path().join("tool@v1")).unwrap();
        std::fs::write(dir.path().join("tool@v1").join("program.py"), CODE).unwrap();
        let mut r = LocalDirResolver::new(dir.path().to_path_buf());
        let out = r.resolve("tool@v1", None).unwrap();
        assert_eq!(out["source"], json!(CODE));
        assert_eq!(out["objectId"], json!("tool@v1"));

        std::fs::write(dir.path().join("flat@v1.py"), "x = 1\n").unwrap();
        std::fs::create_dir(dir.path().join("flat@v1")).unwrap();
        std::fs::write(dir.path().join("flat@v1").join("program.py"), "x = 2\n").unwrap();
        assert_eq!(
            local_source_path(dir.path(), "flat@v1"),
            dir.path().join("flat@v1.py")
        );
        // and end-to-end: a fresh resolver serves the flat file's content
        std::fs::write(dir.path().join("tool@v1.py"), "flat").unwrap();
        let mut r2 = LocalDirResolver::new(dir.path().to_path_buf());
        assert_eq!(
            r2.resolve("tool@v1", None).unwrap()["source"],
            json!("flat")
        );
    }

    #[test]
    fn local_dir_resolver_not_found() {
        let dir = tempdir().unwrap();
        let mut r = LocalDirResolver::new(dir.path().to_path_buf());
        let err = r.resolve("ghost@v9", None).unwrap_err();
        assert!(err.to_string().starts_with("program not found: ghost@v9"));
    }
}
