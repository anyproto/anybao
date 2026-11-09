//! The frozen content (ADR-034 §2): everything the guest can load from the
//! working space and the test overlays, verified against explicit setup's pins
//! ONCE and then served from this snapshot. The managed resolver never reads a
//! program from the space again, so a redeploy after readiness cannot run, an
//! agent-authored program (ADR-013) is refused typed, and `use()` makes no
//! HTTP at all (N1-R1, N1-I1). Skills are read live by the guest over the
//! data routes; their pins are re-verified at every Open and periodically
//! while active (N2b) — the one live-read set this snapshot cannot serve.
use super::preparation::{FrozenPins, FrozenProgram, FrozenSkill};
use crate::anyapi::Client;
use crate::broker::DeclaredCredentials;
use crate::program_schema::ProgramSchema;
use crate::resolver::{parse, ModuleResolver, ResolveError};
use crate::stores::declared_entries;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

/// Why verification did not produce a snapshot: drift is `NeedsSetup`; a read
/// that failed or content that is not on this device yet is `Syncing`, which
/// preparation reports as the typed "not synced" setup reason.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Verify {
    NeedsSetup,
    /// A read failed or a type is not here yet.
    Syncing,
    /// A pinned object is absent: not synced at preparation; at a later
    /// re-verification (the takeover's), a vanished pin, which is drift (S10).
    Missing,
}

/// The reserved alias of the working space in the pins and the resolver: its
/// programs present at explicit setup are pinned like an overlay's (ADR-034
/// §2; §6's `android-poc-heartbeat@v1` lives there).
pub(crate) const WORKING_ALIAS: &str = "working";

pub(crate) struct VerifiedProgram {
    pub(crate) object_id: String,
    pub(crate) code: String,
    pub(crate) marker: i64,
}

#[derive(Default)]
pub(crate) struct FrozenContent {
    /// `working` and every configured overlay alias → space id; the only
    /// aliases `use()` accepts
    pub(crate) aliases: BTreeMap<String, String>,
    /// (space, name, version) → the verified program
    pub(crate) programs: BTreeMap<(String, String, String), VerifiedProgram>,
    /// `alias:name@version` → verified deploy fingerprint (the readiness report)
    pub(crate) fingerprints: BTreeMap<String, String>,
    /// `alias:name` → verified skill content hash (the readiness report)
    pub(crate) skills: BTreeMap<String, String>,
    /// the declared credential table, from the verified programs only (N1-I-4)
    pub(crate) declared: BTreeMap<String, Value>,
}

/// The managed declared table (ADR-034 §2): the declarations of the verified
/// frozen programs and nothing read later. It has no refresh by construction.
pub(crate) struct FrozenDeclared(BTreeMap<String, Value>);
impl FrozenDeclared {
    pub(crate) fn new(table: BTreeMap<String, Value>) -> Self {
        FrozenDeclared(table)
    }
    #[cfg(test)]
    pub(crate) fn snapshot(&self) -> &BTreeMap<String, Value> {
        &self.0
    }
}
impl DeclaredCredentials for FrozenDeclared {
    fn about(&self, key: &str) -> Option<Value> {
        self.0.get(key).cloned()
    }
}

pub(crate) fn sha256_hex(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))
}

fn string_or_absent<'a>(props: &'a Value, key: &str) -> Result<Option<&'a str>, Verify> {
    match props.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => Ok(Some(s.as_str())),
        Some(_) => Err(Verify::NeedsSetup), // a lossy coercion is drift (N1-R3)
    }
}

fn required<'a>(props: &'a Value, key: &str) -> Result<&'a str, Verify> {
    string_or_absent(props, key)?
        .filter(|s| !s.is_empty())
        .ok_or(Verify::NeedsSetup)
}

/// Verify the working space and every configured overlay against the pins and
/// capture the content. A space without pins must hold no program and no
/// skill; an unconfigured pin alias, an extra object, a drifted fingerprint, a
/// non-canonical property type or a duplicate is `NeedsSetup`; a failed read or
/// a pinned object that is not here yet is `Syncing`.
pub(crate) fn verify(
    client: &Client,
    cfg: &crate::Config,
    pins: &FrozenPins,
) -> Result<FrozenContent, Verify> {
    let configured: BTreeSet<&str> = cfg.overlays.keys().map(String::as_str).collect();
    let covered: BTreeSet<&str> = pins.overlays.iter().map(String::as_str).collect();
    if configured != covered
        || covered.contains(WORKING_ALIAS)
        || pins
            .programs
            .iter()
            .map(|p| p.alias.as_str())
            .chain(pins.skills.iter().map(|s| s.alias.as_str()))
            .any(|alias| alias != WORKING_ALIAS && !covered.contains(alias))
    {
        return Err(Verify::NeedsSetup);
    }
    let mut content = FrozenContent::default();
    // the working space's own programs/skills present at setup, then every overlay
    let spaces = std::iter::once((WORKING_ALIAS.to_owned(), cfg.agent_space.clone())).chain(
        cfg.overlays
            .iter()
            .map(|(a, o)| (a.clone(), o.space.clone())),
    );
    for (alias, space) in spaces {
        content.aliases.insert(alias.clone(), space.clone());
        let pinned: Vec<&FrozenProgram> =
            pins.programs.iter().filter(|p| p.alias == alias).collect();
        verify_programs(client, &space, &alias, &pinned, &mut content)?;
        let skills: Vec<&FrozenSkill> = pins.skills.iter().filter(|s| s.alias == alias).collect();
        verify_skills(client, &space, &alias, &skills, &mut content, false)?;
    }
    Ok(content)
}

fn verify_programs(
    client: &Client,
    space: &str,
    alias: &str,
    pinned: &[&FrozenProgram],
    content: &mut FrozenContent,
) -> Result<(), Verify> {
    let schema = match ProgramSchema::lookup(client, space).map_err(|_| Verify::Syncing)? {
        Some(schema) => schema,
        // no program type: nothing deployed (yet) — fine only when nothing is pinned
        None if pinned.is_empty() => return Ok(()),
        None => return Err(Verify::Syncing),
    };
    let rows = client
        .query_objects(
            space,
            &json!({"filter": {"any.type": schema.type_id}, "limit": 500}),
        )
        .map_err(|_| Verify::Syncing)?;
    if rows.len() >= 500 {
        return Err(Verify::NeedsSetup);
    }
    let mut seen = BTreeSet::new();
    for row in &rows {
        let props = schema.read(row);
        let name = required(&props, "name")?;
        let version = required(&props, "version")?;
        let summary = string_or_absent(&props, "summary")?.unwrap_or("");
        let category = string_or_absent(&props, "category")?.unwrap_or("");
        let any_tool = match props.get("any_tool") {
            None | Some(Value::Null) => false,
            Some(Value::Bool(b)) => *b,
            Some(_) => return Err(Verify::NeedsSetup),
        };
        let credentials = string_or_absent(&props, "credentials")?.unwrap_or("");
        // An unpinned program means the space moved: not the frozen set.
        let pin = pinned
            .iter()
            .find(|p| p.name == name && p.version == version)
            .ok_or(Verify::NeedsSetup)?;
        let object_id = row["id"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or(Verify::NeedsSetup)?;
        let source = client
            .query(space, object_id, &schema.source, &json!({}))
            .map_err(|_| Verify::Syncing)?;
        let main = source.first().ok_or(Verify::Syncing)?;
        let code = main["code"].as_str().ok_or(Verify::Syncing)?;
        let manifest_row = client
            .query(space, object_id, &schema.manifest, &json!({}))
            .map_err(|_| Verify::Syncing)?
            .into_iter()
            .next();
        let manifest = manifest_row
            .as_ref()
            .and_then(|r| r.get("manifest"))
            .cloned()
            .unwrap_or(json!({}));
        // The deploy fingerprint (same inputs as deploy::in_space_fingerprint) plus
        // the managed-side credentials digest the deploy hash omits (N1-R3).
        let fingerprint = crate::deploy::fingerprint(code, summary, any_tool, category, &manifest);
        // deploy writes the source, then the manifest record (only for a truthy
        // manifest): a pinned manifest that is not here yet is syncing, not
        // drift; a manifest the pin does not expect is drift (N2a-r2 R2-4)
        if pin.manifest && manifest_row.is_none() {
            return Err(Verify::Syncing);
        }
        if fingerprint != pin.fingerprint
            || manifest_row.is_some() != pin.manifest
            || sha256_hex(credentials) != pin.credentials_sha256
            || !seen.insert((name.to_owned(), version.to_owned()))
        {
            return Err(Verify::NeedsSetup);
        }
        // The declared table comes from the overlays only (ADR-021 §8.1: manifests
        // deploy wrote into spaces the guest cannot write); a working-space
        // program declares nothing, whatever its `credentials` says (N2a-r2 item 1).
        if alias != WORKING_ALIAS {
            for (reference, about) in declared_entries(&props) {
                content.declared.entry(reference).or_insert(about);
            }
        }
        content
            .fingerprints
            .insert(format!("{alias}:{name}@{version}"), fingerprint);
        content.programs.insert(
            (space.to_owned(), name.to_owned(), version.to_owned()),
            VerifiedProgram {
                object_id: object_id.to_owned(),
                code: code.to_owned(),
                marker: main["_addSeq"].as_i64().unwrap_or(0),
            },
        );
    }
    if seen.len() != pinned.len() {
        return Err(Verify::Missing); // a pinned program is not on this device (yet)
    }
    Ok(())
}

/// Also the takeover's re-verification at every Open (ADR-034 §2/§5):
/// `reverify` is set there. At first preparation an absent skill type or an
/// empty body is a pin not yet synced; at re-verification the same pins were
/// verified present and non-empty on this device, so either is drift — a
/// guest that empties a pinned skill is reported as NeedsSetup, never as a
/// transient read failure (Step B isolation review N-2).
pub(crate) fn verify_skills(
    client: &Client,
    space: &str,
    alias: &str,
    pinned: &[&FrozenSkill],
    content: &mut FrozenContent,
    reverify: bool,
) -> Result<(), Verify> {
    // The same read toolcaller@v1 performs: the agent_skill type, its objects, each body.
    let type_id = client
        .list_types(space)
        .map_err(|_| Verify::Syncing)?
        .into_iter()
        .find(|t| t["xKey"] == crate::deploy::SKILL_TYPE)
        .and_then(|t| t["id"].as_str().map(str::to_owned));
    let Some(type_id) = type_id else {
        return if pinned.is_empty() {
            Ok(())
        } else if reverify {
            Err(Verify::Missing)
        } else {
            Err(Verify::Syncing)
        };
    };
    let rows = client
        .query_objects(
            space,
            &json!({"filter": {"any.type": type_id}, "limit": 500}),
        )
        .map_err(|_| Verify::Syncing)?;
    if rows.len() >= 500 {
        return Err(Verify::NeedsSetup);
    }
    let mut seen = BTreeSet::new();
    for row in &rows {
        let name = row["any"]["name"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or(Verify::NeedsSetup)?;
        let object_id = row["id"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or(Verify::NeedsSetup)?;
        let pin = pinned
            .iter()
            .find(|p| p.name == name)
            .ok_or(Verify::NeedsSetup)?;
        let body = client
            .get_markdown(space, object_id)
            .map_err(|_| Verify::Syncing)?;
        let digest = sha256_hex(&body);
        if digest != pin.sha256 {
            // deploy creates the object, then PUTs its markdown: an empty body
            // against a non-empty pin is syncing at first preparation, not
            // drift (N2a-r2 R2-4); emptied after it was verified, it is drift
            return Err(if body.is_empty() && !reverify {
                Verify::Syncing
            } else {
                Verify::NeedsSetup
            });
        }
        if !seen.insert(name.to_owned()) {
            return Err(Verify::NeedsSetup);
        }
        content.skills.insert(format!("{alias}:{name}"), digest);
    }
    if seen.len() != pinned.len() {
        return Err(Verify::Missing);
    }
    Ok(())
}

/// The managed module resolver: pinned content only, no HTTP. A configured
/// alias resolves in its space; an unqualified `use()` inside a module resolves
/// in that module's defining space, and one from cell code in the working
/// space, exactly like desktop — but only its pinned set. Anything else is the
/// typed `NotPinned` (an agent-authored program lands there), never a wait.
pub(crate) struct FrozenResolver {
    content: Arc<FrozenContent>,
    origins: BTreeMap<String, String>,
    probed: BTreeSet<String>,
}

impl FrozenResolver {
    pub(crate) fn new(content: Arc<FrozenContent>) -> Self {
        FrozenResolver {
            content,
            origins: BTreeMap::new(),
            probed: BTreeSet::new(),
        }
    }

    fn serve(&mut self, space: &str, name: &str, version: &str) -> Result<Value, ResolveError> {
        let key = (space.to_owned(), name.to_owned(), version.to_owned());
        let Some(program) = self.content.programs.get(&key) else {
            return Err(ResolveError::NotPinned {
                space: space.to_owned(),
                spec: format!("{name}@{version}"),
            });
        };
        let cache = if self.probed.insert(program.object_id.clone()) {
            "miss"
        } else {
            "hit"
        };
        self.origins
            .insert(program.object_id.clone(), space.to_owned());
        Ok(json!({
            "spaceId": space, "objectId": program.object_id, "marker": program.marker,
            "sourceHash": format!("sha256:{}", sha256_hex(&program.code)),
            "source": program.code, "cache": cache,
        }))
    }
}

impl ModuleResolver for FrozenResolver {
    fn resolve(&mut self, spec: &str, frm: Option<&str>) -> Result<Value, ResolveError> {
        let (alias, name, version) = parse(spec)?;
        let (name, version) = (name.to_owned(), version.to_owned());
        if let Some(alias) = alias {
            // a configured alias, or a frozen space's raw id — the form the
            // toolcaller and programs@v1 spell working-space tools in (R2-5):
            // the same pinned set, still no HTTP; anything else is unknown
            let space = self
                .content
                .aliases
                .get(alias)
                .cloned()
                .or_else(|| {
                    self.content
                        .aliases
                        .values()
                        .find(|space| space.as_str() == alias)
                        .cloned()
                })
                .ok_or_else(|| ResolveError::UnknownAlias(alias.to_owned()))?;
            return self.serve(&space, &name, &version);
        }
        if let Some(space) = frm.and_then(|oid| self.origins.get(oid)).cloned() {
            return self.serve(&space, &name, &version);
        }
        let working = self
            .content
            .aliases
            .get(WORKING_ALIAS)
            .cloned()
            .ok_or_else(|| ResolveError::UnknownAlias(WORKING_ALIAS.to_owned()))?;
        self.serve(&working, &name, &version)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn content() -> Arc<FrozenContent> {
        let mut c = FrozenContent::default();
        c.aliases.insert(WORKING_ALIAS.into(), "ws".into());
        c.aliases.insert("agent".into(), "space-a".into());
        c.programs.insert(
            ("space-a".into(), "llm".into(), "v1".into()),
            VerifiedProgram {
                object_id: "obj-llm".into(),
                code: "def main(args):\n    return 1\n".into(),
                marker: 7,
            },
        );
        c.programs.insert(
            ("ws".into(), "android-poc-heartbeat".into(), "v1".into()),
            VerifiedProgram {
                object_id: "obj-heartbeat".into(),
                code: "def main(args):\n    return 2\n".into(),
                marker: 1,
            },
        );
        Arc::new(c)
    }

    #[test]
    fn frozen_resolver_serves_only_pinned_content_and_refuses_the_rest_typed() {
        let mut r = FrozenResolver::new(content());
        let served = r.resolve("agent:llm@v1", None).unwrap();
        assert_eq!(served["objectId"], "obj-llm");
        assert_eq!(served["marker"], 7);
        assert_eq!(served["cache"], "miss");
        assert!(served["sourceHash"]
            .as_str()
            .unwrap()
            .starts_with("sha256:"));
        assert_eq!(r.resolve("agent:llm@v1", None).unwrap()["cache"], "hit");
        // transitive: an unqualified import from the served module resolves in its space
        assert!(r.resolve("llm@v1", Some("obj-llm")).is_ok());
        // unqualified from cell code: the working space's pinned set (the heartbeat)
        assert_eq!(
            r.resolve("android-poc-heartbeat@v1", None).unwrap()["objectId"],
            "obj-heartbeat"
        );
        // an agent-authored or unpinned program is refused typed, never a wait
        for (spec, frm) in [
            ("llm@v1", None),
            ("agent:other@v1", None),
            ("mine@v1", Some("obj-llm")),
        ] {
            assert!(
                matches!(r.resolve(spec, frm), Err(ResolveError::NotPinned { .. })),
                "{spec}"
            );
        }
        // a frozen space's raw id resolves the same pinned set (R2-5); an unpinned
        // spec through it is NotPinned, never UnknownAlias
        assert_eq!(
            r.resolve("space-a:llm@v1", None).unwrap()["objectId"],
            "obj-llm"
        );
        assert_eq!(
            r.resolve("ws:android-poc-heartbeat@v1", None).unwrap()["objectId"],
            "obj-heartbeat"
        );
        assert!(matches!(
            r.resolve("ws:mine@v1", None),
            Err(ResolveError::NotPinned { .. })
        ));
        for spec in [
            "private:llm@v1",
            "other-space:llm@v1",
            "space-a/types/../../../../debug/pprof:llm@v1",
            "%2e%2e:llm@v1",
        ] {
            assert!(
                matches!(r.resolve(spec, None), Err(ResolveError::UnknownAlias(_))),
                "{spec}"
            );
        }
    }

    #[test]
    fn frozen_declared_table_answers_from_its_snapshot_only() {
        let table = FrozenDeclared::new(BTreeMap::from([(
            "llm.key.x".to_string(),
            json!({"hosts": ["a.example"]}),
        )]));
        assert_eq!(
            table.about("llm.key.x"),
            Some(json!({"hosts": ["a.example"]}))
        );
        assert_eq!(table.about("llm.key.y"), None);
        assert_eq!(table.snapshot().len(), 1);
    }
}
