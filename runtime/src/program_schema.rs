//! The `program` type as a harness-declared USER type (ADR-010 §5,
//! ADR-017 §1, ADR-027 §2/§3): one hidden type per space keyed by xKey
//! `program`, four properties (`name`, `version`, `any_tool`,
//! `summary` — none indexed), a shared editor part (`body` — the
//! program object's docs body, held through the type) and two records
//! datasets, `program_source` and `program_manifest` (single record
//! "main"; declared WITHOUT a `search` mapping, so the server never
//! indexes them — source is code, not knowledge). Their collections
//! are read off the declaration (`source` / `manifest`), never
//! composed.
//!
//! Deploy is the writer that ENSURES the store (`ensure`, idempotent);
//! the module resolver only LOOKS it up (`lookup` — a space that never
//! had a program deployed has no type, which is a plain miss, never an
//! ensure). The raw host client keys property groups and filter paths
//! by `<typeId>.<propId>` (only builtins have id == xKey), so every
//! caller goes through `path`/`group`/`read` instead of literals.

use crate::anyapi::{AnyError, Client};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;

/// The type's xKey — the stable handle clients (guest `any@v1`,
/// any-ui) resolve by; never a valid type id.
pub const PROGRAM_TYPE_XKEY: &str = "program";
pub const PROGRAM_TYPE_NAME: &str = "Program";
/// The two stores' dataset KEYS on the type; the collections are
/// `ProgramSchema::source` / `::manifest`.
pub const SOURCE_KEY: &str = "program_source";
pub const MANIFEST_KEY: &str = "program_manifest";
pub const MAIN_RECORD: &str = "main";

/// (xKey, display name, kind) — the declared property set a space
/// must carry to resolve at all.
const PROPS: [(&str, &str, &str); 4] = [
    ("name", "Name", "string"),
    ("version", "Version", "string"),
    ("any_tool", "Any Tool", "boolean"),
    ("summary", "Summary", "string"),
];

/// The credentials a program declares (ADR-021 §8.1): the JSON text of
/// its `__any_credentials__` list, derived by deploy — the host's only
/// source for a declared ref's label and hosts. Ensured by deploy,
/// OPTIONAL at lookup: a space deployed before this property still
/// resolves (its programs simply declare nothing until redeployed).
pub const CREDENTIALS_PROP: (&str, &str, &str) = ("credentials", "Credentials", "string");

/// The parts the type declares: the shared body first (the docs body
/// every program object holds through its type — no per-object
/// attach), then one part per records store.
fn part_drafts() -> [Value; 3] {
    [
        json!({"key": "body", "datasets": [{"module": "editor", "shared": true}]}),
        json!({"key": SOURCE_KEY, "datasets": [{
            "key": SOURCE_KEY, "displayName": "Program Source",
            "idRule": "user", "deleteBy": "anyone", "dynamic": true,
            "fields": [{"key": "code", "kind": "string", "mutableBy": "any"}]}]}),
        json!({"key": MANIFEST_KEY, "datasets": [{
            "key": MANIFEST_KEY, "displayName": "Program Manifest",
            "idRule": "user", "deleteBy": "anyone", "dynamic": true,
            "fields": [{"key": "manifest", "kind": "object", "mutableBy": "any"}]}]}),
    ]
}

/// One space's resolved `program` schema: the type id, the
/// xKey→propId map and the two stores' collections.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProgramSchema {
    pub type_id: String,
    props: BTreeMap<String, String>,
    /// the `program_source` collection (`<typeId>_program_source`)
    pub source: String,
    /// the `program_manifest` collection
    pub manifest: String,
}

impl ProgramSchema {
    /// Read-only resolve: `Ok(None)` when the space has no `program`
    /// type (or the type lacks a declared property).
    pub fn lookup(c: &Client, space: &str) -> Result<Option<Self>, AnyError> {
        let Some(tid) = find_type(c, space)? else {
            return Ok(None);
        };
        let props = prop_map(c, space, &tid)?;
        if PROPS.iter().any(|(k, _, _)| !props.contains_key(*k)) {
            return Ok(None);
        }
        let colls = collections(c, space, &tid)?;
        let (Some(source), Some(manifest)) = (colls.get(SOURCE_KEY), colls.get(MANIFEST_KEY))
        else {
            return Ok(None);
        };
        Ok(Some(ProgramSchema {
            type_id: tid,
            props,
            source: source.clone(),
            manifest: manifest.clone(),
        }))
    }

    /// Ensure type + properties + datasets, idempotently, and return
    /// the resolved schema. Only MISSING pieces are created.
    pub fn ensure(c: &Client, space: &str) -> anyhow::Result<Self> {
        let tid = match find_type(c, space)? {
            Some(t) => t,
            None => {
                // hidden: never offered by a client's type picker (ADR-027 §2)
                let res = c.create_type(
                    space,
                    &json!({"name": PROGRAM_TYPE_NAME, "xKey": PROGRAM_TYPE_XKEY,
                            "hidden": true}),
                )?;
                res["typeId"]
                    .as_str()
                    .or_else(|| res["id"].as_str())
                    .ok_or_else(|| anyhow::anyhow!("create_type reply has no typeId: {res}"))?
                    .to_string()
            }
        };
        let mut props = prop_map(c, space, &tid)?;
        for (xkey, name, kind) in PROPS.iter().copied().chain([CREDENTIALS_PROP]) {
            if props.contains_key(xkey) {
                continue;
            }
            // none of the four is a search target (ADR-010 §5)
            let res = c.add_property(
                space,
                &tid,
                &json!({"name": name, "xKey": xkey, "kind": kind,
                        "meta": {"index": "none"}}),
            )?;
            let pid = res["propId"]
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("add_property reply has no propId: {res}"))?;
            props.insert(xkey.to_string(), pid.to_string());
        }
        // idempotent by dataset KEY; the body part has none of its own
        // (a shared editor dataset lists under the canonical key)
        let mut have = collections(c, space, &tid)?;
        for draft in part_drafts() {
            let ds = &draft["datasets"][0];
            let key = ds["key"].as_str().unwrap_or(crate::anyapi::EDITOR_BLOCKS);
            if !have.contains_key(key) {
                c.add_part(space, &tid, &draft)?;
            }
        }
        have = collections(c, space, &tid)?;
        let (Some(source), Some(manifest)) = (have.get(SOURCE_KEY), have.get(MANIFEST_KEY)) else {
            anyhow::bail!("program stores declared but not listed on {tid}");
        };
        Ok(ProgramSchema {
            type_id: tid,
            props,
            source: source.clone(),
            manifest: manifest.clone(),
        })
    }

    /// Property id for a declared xKey (panics on an undeclared key —
    /// a programming error, the set is static).
    pub fn prop(&self, xkey: &str) -> &str {
        self.props
            .get(xkey)
            .unwrap_or_else(|| panic!("program schema has no property {xkey}"))
    }

    /// Filter/sort path `<typeId>.<propId>`.
    pub fn path(&self, xkey: &str) -> String {
        format!("{}.{}", self.type_id, self.prop(xkey))
    }

    /// The optional `credentials` property's id (None on a space
    /// deployed before ADR-021 §8.1 — nothing declared there).
    pub fn credentials_prop(&self) -> Option<&str> {
        self.props.get(CREDENTIALS_PROP.0).map(String::as_str)
    }

    /// The id-keyed property group `{propId: value}` for a write.
    pub fn group(&self, fields: &[(&str, Value)]) -> Value {
        let mut m = Map::new();
        for (k, v) in fields {
            m.insert(self.prop(k).to_string(), v.clone());
        }
        Value::Object(m)
    }

    /// The xKey-keyed `program` group read back from an objects row
    /// (`{name, version, any_tool, summary}`; missing → `{}`).
    pub fn read(&self, row: &Value) -> Value {
        let mut m = Map::new();
        if let Some(g) = row.get(&self.type_id).and_then(Value::as_object) {
            for (xkey, pid) in &self.props {
                if let Some(v) = g.get(pid) {
                    m.insert(xkey.clone(), v.clone());
                }
            }
        }
        Value::Object(m)
    }
}

fn find_type(c: &Client, space: &str) -> Result<Option<String>, AnyError> {
    Ok(c.list_types(space)?
        .iter()
        .find(|t| t["xKey"] == PROGRAM_TYPE_XKEY)
        .and_then(|t| t["id"].as_str().map(str::to_string)))
}

/// dataset key → collection, read off the type's declarations.
fn collections(c: &Client, space: &str, tid: &str) -> Result<BTreeMap<String, String>, AnyError> {
    Ok(c.list_datasets(space, tid)?
        .iter()
        .filter_map(|d| {
            Some((
                d["key"].as_str()?.to_string(),
                d["collection"].as_str()?.to_string(),
            ))
        })
        .collect())
}

fn prop_map(c: &Client, space: &str, tid: &str) -> Result<BTreeMap<String, String>, AnyError> {
    let mut out = BTreeMap::new();
    for p in c.list_properties(space, tid)? {
        if let (Some(x), Some(id)) = (p["xKey"].as_str(), p["id"].as_str()) {
            out.insert(x.to_string(), id.to_string());
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::FakeSpace;

    fn client() -> Client {
        Client::with_transport(Box::new(FakeSpace::new()))
    }

    #[test]
    fn lookup_misses_then_ensure_creates_once() {
        let c = client();
        assert_eq!(ProgramSchema::lookup(&c, "sp").unwrap(), None);
        let s = ProgramSchema::ensure(&c, "sp").unwrap();
        assert_ne!(s.type_id, PROGRAM_TYPE_XKEY); // a real (generated) id
        assert_eq!(ProgramSchema::lookup(&c, "sp").unwrap(), Some(s.clone()));
        // idempotent: same ids, no duplicate type/props/datasets
        assert_eq!(ProgramSchema::ensure(&c, "sp").unwrap(), s);
        let user_types: Vec<Value> = c
            .list_types("sp")
            .unwrap()
            .into_iter()
            .filter(|t| t["builtIn"] != json!(true))
            .collect();
        assert_eq!(user_types.len(), 1);
        assert_eq!(c.list_properties("sp", &s.type_id).unwrap().len(), 4);
        let ds = c.list_datasets("sp", &s.type_id).unwrap();
        let keys: Vec<&str> = ds.iter().filter_map(|d| d["key"].as_str()).collect();
        // the shared body (held through the type) + the two stores
        assert_eq!(keys, vec!["editor_blocks", SOURCE_KEY, MANIFEST_KEY]);
        // never a search target
        assert!(ds.iter().all(|d| d.get("search").is_none()));
        // the collections are the server's, read back — never composed
        assert_eq!(s.source, format!("{}_{SOURCE_KEY}", s.type_id));
        assert_eq!(s.manifest, format!("{}_{MANIFEST_KEY}", s.type_id));
        // hidden from the first change
        assert_eq!(user_types[0]["hidden"], json!(true));
    }

    #[test]
    fn path_group_read_round_trip() {
        let c = client();
        let s = ProgramSchema::ensure(&c, "sp").unwrap();
        assert_eq!(s.path("name"), format!("{}.{}", s.type_id, s.prop("name")));
        let row = json!({"id": "o1", s.type_id.clone():
            s.group(&[("name", json!("t")), ("any_tool", json!(true))])});
        assert_eq!(s.read(&row), json!({"name": "t", "any_tool": true}));
        assert_eq!(s.read(&json!({"id": "o2"})), json!({}));
    }
}
