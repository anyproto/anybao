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
use crate::program_schema::ProgramSchema;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::path::PathBuf;

#[derive(Debug)]
pub enum ResolveError {
    /// bad module spec (need name@vN)
    BadSpec(String),
    /// unknown alias in spec
    UnknownAlias(String),
    /// program (or its source record) not found
    NotFound(String),
    /// a space-backed miss (ADR-009 §8): `name@vN` is not in `space`
    /// on THIS device — a deploy that hasn't synced here yet, or a
    /// program that was never deployed; the host cannot tell which,
    /// so serve waits for it (bounded) before calling it absent.
    /// `spec` is the bare `name@vN`; `msg` the user-facing text.
    Missing {
        space: String,
        spec: String,
        msg: String,
    },
    /// any-server call failed
    Api(AnyError),
}

impl fmt::Display for ResolveError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ResolveError::BadSpec(s) => write!(f, "bad module spec (need name@vN): {s:?}"),
            ResolveError::UnknownAlias(a) => write!(f, "unknown alias in spec: {a:?}"),
            ResolveError::NotFound(m) => write!(f, "{m}"),
            ResolveError::Missing { msg, .. } => write!(f, "{msg}"),
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

/// One in-memory source over an inner resolver: the ENTRY spec is
/// served from caller-provided text, every other spec — the entry's
/// `use()` imports — delegates to `inner` (the serve space resolver).
/// Serve's `POST /run {source}` path (ADR-009 §6): the caller owns the
/// program text; nothing lands in a space or on the filesystem. The
/// inline module's own imports must be alias-qualified
/// (`agent:llm@v1`) — its pseudo object id has no defining space, so
/// an unqualified import falls back to working-space resolution.
pub struct InlineResolver {
    pub spec: String,
    pub source: String,
    pub inner: Box<dyn ModuleResolver + Send>,
}

impl ModuleResolver for InlineResolver {
    fn resolve(&mut self, spec: &str, frm: Option<&str>) -> Result<Value, ResolveError> {
        if spec == self.spec && frm.is_none() {
            return Ok(json!({
                "spaceId": "", "objectId": "inline", "marker": 0,
                "sourceHash": format!(
                    "sha256:{}",
                    hex::encode(Sha256::digest(self.source.as_bytes()))
                ),
                "source": self.source, "cache": "miss",
            }));
        }
        self.inner.resolve(spec, frm)
    }
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
    /// space → its `program` schema. Missing types and legacy schemas
    /// without the publication property are re-probed: deployment may
    /// install or upgrade the schema during the resolver's lifetime.
    schemas: BTreeMap<String, ProgramSchema>,
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
            schemas: BTreeMap::new(),
        }
    }

    fn schema_in(&mut self, space: &str) -> Result<Option<ProgramSchema>, AnyError> {
        if let Some(s) = self
            .schemas
            .get(space)
            .filter(|s| s.deployment_prop().is_some())
        {
            return Ok(Some(s.clone()));
        }
        let found = ProgramSchema::lookup(&self.client, space)?;
        if let Some(s) = &found {
            self.schemas.insert(space.to_string(), s.clone());
        }
        Ok(found)
    }

    fn resolve_in(
        &mut self,
        space: &str,
        name: &str,
        version: &str,
        spec: &str,
    ) -> Result<Value, ResolveError> {
        // no `program` type in the space = nothing was ever deployed there
        let missing = |msg: String| ResolveError::Missing {
            space: space.to_string(),
            spec: format!("{name}@{version}"),
            msg,
        };
        let Some(mut s) = self.schema_in(space)? else {
            return Err(missing(format!(
                "program not found: {spec} (space {space} has no programs)"
            )));
        };
        let recs = self.client.query_objects(
            space,
            &json!({"filter": {s.path("name"): name, s.path("version"): version},
                    "limit": 100}),
        )?;
        // A deployer may add the optional publication property between the
        // schema lookup and this query. Refresh after reading rows so a pending
        // row cannot be interpreted with the pre-installation property map.
        if s.deployment_prop().is_none() {
            if let Some(current) = self.schema_in(space)? {
                s = current;
            }
        }
        let published: Vec<_> = recs
            .iter()
            .filter(|row| s.read(row).get("deployment_fingerprint").is_none())
            .collect();
        if published.len() > 1 {
            return Err(ResolveError::NotFound(format!(
                "program {spec} has multiple published copies; resolve the conflict"
            )));
        }
        let Some(oid) = published.first().and_then(|r| r["id"].as_str()) else {
            if !recs.is_empty() {
                return Err(missing(format!(
                    "program {spec} installation is incomplete"
                )));
            }
            return Err(missing(format!(
                "program not found: {spec} (space {space})"
            )));
        };
        let src = self.client.query(space, oid, &s.source, &json!({}))?;
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
            Err(e @ ResolveError::Missing { .. }) => {
                match self.private.clone().filter(|p| *p != base) {
                    Some(p) => self
                        .resolve_in(&p, &name, &version, spec)
                        .map_err(|e| self.alias_hint(e, &name, &version)),
                    None => Err(self.alias_hint(e, &name, &version)),
                }
            }
            other => other,
        }
    }
}

impl AnyModuleResolver {
    /// An unqualified miss usually means an overlay module named without
    /// its alias (E10: `use("any@v1")` where `use("agent:any@v1")` was
    /// meant) — teach the fix in the error instead of costing a turn.
    fn alias_hint(&self, err: ResolveError, name: &str, version: &str) -> ResolveError {
        let ResolveError::Missing { space, spec, msg } = err else {
            return err;
        };
        let mut aliases: Vec<&str> = self.aliases.keys().map(String::as_str).collect();
        if self.private.is_some() && !aliases.contains(&"private") {
            aliases.push("private");
        }
        if aliases.is_empty() {
            return ResolveError::Missing { space, spec, msg };
        }
        ResolveError::Missing {
            space,
            spec,
            msg: format!(
                "{msg} — unqualified specs resolve in the current space only; \
                 overlay modules need their alias, e.g. use(\"{first}:{name}@{version}\") \
                 (available aliases: {all})",
                first = aliases[0],
                all = aliases.join(", "),
            ),
        }
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
        let s = ProgramSchema::ensure(c, space).unwrap();
        let res = c
            .create_object(
                space,
                &json!({"type": s.type_id,
                        "initialProperties": {"any": {"name": name},
                                              s.type_id.clone(): s.group(&[
                                                  ("name", json!(name)),
                                                  ("version", json!(version))])}}),
            )
            .unwrap();
        let oid = res["objectId"].as_str().unwrap().to_string();
        c.upsert_record(space, &oid, &s.source, "main", &json!({"code": code}))
            .unwrap();
    }

    /// Simulate a resolver that learned the program schema before a newer
    /// deployer added the optional pending-publication property.
    struct LegacySchemaView {
        inner: FakeSpace,
        upgraded: std::sync::Arc<std::sync::atomic::AtomicBool>,
        upgrade_on_query: std::sync::Arc<std::sync::atomic::AtomicBool>,
    }

    impl crate::anyapi::Transport for LegacySchemaView {
        fn send(
            &self,
            method: &str,
            path: &str,
            body: Option<&Value>,
        ) -> Result<(u16, Value), AnyError> {
            if path.ends_with("/objects/query")
                && self
                    .upgrade_on_query
                    .swap(false, std::sync::atomic::Ordering::SeqCst)
            {
                self.upgraded
                    .store(true, std::sync::atomic::Ordering::SeqCst);
            }
            let (status, mut result) = self.inner.send(method, path, body)?;
            if path.ends_with("/properties")
                && !self.upgraded.load(std::sync::atomic::Ordering::SeqCst)
            {
                if let Some(properties) = result["properties"].as_array_mut() {
                    properties.retain(|p| p["xKey"] != "deployment_fingerprint");
                }
            }
            Ok((status, result))
        }

        fn open_stream(
            &self,
            path: &str,
            body: Option<&Value>,
        ) -> Result<Box<dyn Iterator<Item = String>>, AnyError> {
            self.inner.open_stream(path, body)
        }
    }

    fn seed_pending_program(c: &Client, name: &str) -> String {
        let s = ProgramSchema::ensure(c, "cur").unwrap();
        let result = c
            .create_object(
                "cur",
                &json!({
                    "type": s.type_id,
                    "initialProperties": {s.type_id.clone(): s.group(&[
                        ("name", json!(name)), ("version", json!("v1")),
                        ("any_tool", json!(false)),
                        ("deployment_fingerprint", json!("pending-bundle-fingerprint")),
                    ])},
                }),
            )
            .unwrap();
        let oid = result["objectId"].as_str().unwrap().to_owned();
        // This is the interrupted interval after source, before manifest/publish.
        c.upsert_record("cur", &oid, &s.source, "main", &json!({"code": CODE}))
            .unwrap();
        oid
    }

    #[test]
    fn warmed_legacy_schema_cannot_resolve_pending_install() {
        let upgraded = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(true));
        let c = std::sync::Arc::new(Client::with_transport(Box::new(LegacySchemaView {
            inner: FakeSpace::new(),
            upgraded: upgraded.clone(),
            upgrade_on_query: std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false)),
        })));
        seed_program(&c, "cur", "existing", "v1", CODE);
        upgraded.store(false, std::sync::atomic::Ordering::SeqCst);
        let mut r = AnyModuleResolver::new(c.clone(), "cur", None, BTreeMap::new());
        assert_eq!(r.resolve("existing@v1", None).unwrap()["source"], CODE);

        upgraded.store(true, std::sync::atomic::Ordering::SeqCst);
        seed_pending_program(&c, "installing");
        let err = r
            .resolve("installing@v1", None)
            .expect_err("a cached pre-upgrade schema must not hide the pending marker");
        assert!(err.to_string().contains("incomplete"), "{err}");
    }

    #[test]
    fn schema_upgrade_between_schema_read_and_object_query_cannot_expose_pending_source() {
        let upgraded = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(true));
        let upgrade_on_query = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let c = std::sync::Arc::new(Client::with_transport(Box::new(LegacySchemaView {
            inner: FakeSpace::new(),
            upgraded: upgraded.clone(),
            upgrade_on_query: upgrade_on_query.clone(),
        })));
        seed_pending_program(&c, "installing");
        // The schema endpoint still answers its old snapshot. The publication
        // property becomes visible immediately before the object query returns.
        upgraded.store(false, std::sync::atomic::Ordering::SeqCst);
        upgrade_on_query.store(true, std::sync::atomic::Ordering::SeqCst);
        let mut r = AnyModuleResolver::new(c, "cur", None, BTreeMap::new());
        let error = r
            .resolve("installing@v1", None)
            .expect_err("new pending row must be read using the post-query schema");
        assert!(error.to_string().contains("incomplete"), "{error}");
    }

    #[test]
    fn pending_duplicate_does_not_mask_published_program() {
        let c = std::sync::Arc::new(Client::with_transport(Box::new(FakeSpace::new())));
        let pending = seed_pending_program(&c, "tool");
        seed_program(&c, "cur", "tool", "v1", CODE);
        let mut r = AnyModuleResolver::new(c, "cur", None, BTreeMap::new());
        let out = r
            .resolve("tool@v1", None)
            .expect("published duplicate remains usable");
        assert_ne!(out["objectId"], pending);
        assert_eq!(out["source"], CODE);
    }

    #[test]
    fn published_duplicates_are_ambiguous_for_resolve_and_deploy() {
        let c = std::sync::Arc::new(Client::with_transport(Box::new(FakeSpace::new())));
        seed_program(&c, "cur", "tool", "v1", CODE);
        seed_program(&c, "cur", "tool", "v1", "def main(args):\n    return 2\n");
        let before = c.query_objects("cur", &json!({})).unwrap();
        let mut r = AnyModuleResolver::new(c.clone(), "cur", None, BTreeMap::new());
        let error = r
            .resolve("tool@v1", None)
            .expect_err("ambiguous code must not execute");
        assert!(
            error.to_string().contains("multiple published copies"),
            "{error}"
        );
        let source = crate::deploy::ProgramSource::new("tool", "v1", CODE);
        for frozen in [false, true] {
            let error = crate::deploy::Deployer::new(&c, "cur")
                .frozen(frozen)
                .deploy_one(&source)
                .expect_err("ambiguous code must not be overwritten or adopted");
            assert!(
                error.to_string().contains("multiple published copies"),
                "{error}"
            );
        }
        assert_eq!(c.query_objects("cur", &json!({})).unwrap(), before);
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
        // aliased miss does NOT fall back to private — and names where
        // it looked, so serve can wait for exactly that (ADR-009 §8)
        assert!(matches!(
            r.resolve("agent:other@v1", None),
            Err(ResolveError::Missing { space, spec, .. }) if space == "overlay" && spec == "other@v1"
        ));
        // unknown alias without private → raw spaceId, strict
        assert!(matches!(
            r.resolve("someSpaceId:tool@v1", None),
            Err(ResolveError::Missing { space, .. }) if space == "someSpaceId"
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
            Err(ResolveError::Missing { space, .. }) if space == "user"
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
            Err(ResolveError::Missing { space, spec, .. }) if space == "code" && spec == "helper@v1"
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
        let s = ProgramSchema::ensure(&c, "cur").unwrap();
        c.create_object(
            "cur",
            &json!({"type": s.type_id,
                    "initialProperties": {s.type_id.clone(): s.group(&[
                        ("name", json!("hollow")), ("version", json!("v1"))])}}),
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
        let source = ProgramSchema::lookup(&c, "cur").unwrap().unwrap().source;
        c.upsert_record("cur", "obj1", &source, "main", &json!({"code": "x = 2\n"}))
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
