//! The `program` type as a harness-declared USER type (ADR-010 §5,
//! ADR-017 §1): one type per space keyed by xKey `program`, four
//! properties (`name`, `version`, `any_tool`, `summary` — none
//! indexed) and two runtime datasets, `program_source` and
//! `program_manifest` (single record "main"; declared WITHOUT a
//! `search` mapping, so the server never indexes them — source is
//! code, not knowledge).
//!
//! Deploy is the writer that ENSURES the store (`ensure`, idempotent —
//! the same xKey re-claim contract as `skill_schema`); the module
//! resolver only LOOKS it up (`lookup` — a space that never had a
//! program deployed has no type, which is a plain miss, never an
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
pub const SOURCE_DATASET: &str = "program_source";
pub const MANIFEST_DATASET: &str = "program_manifest";
pub const MAIN_RECORD: &str = "main";

/// (xKey, display name, kind) — the declared property set.
const PROPS: [(&str, &str, &str); 4] = [
    ("name", "Name", "string"),
    ("version", "Version", "string"),
    ("any_tool", "Any Tool", "boolean"),
    ("summary", "Summary", "string"),
];

fn dataset_drafts() -> [Value; 2] {
    [
        json!({
            "name": SOURCE_DATASET, "displayName": "Program Source",
            "idRule": "user", "deleteBy": "anyone", "dynamic": true,
            "fields": [{"key": "code", "kind": "string", "mutableBy": "any"}]}),
        json!({
            "name": MANIFEST_DATASET, "displayName": "Program Manifest",
            "idRule": "user", "deleteBy": "anyone", "dynamic": true,
            "fields": [{"key": "manifest", "kind": "object", "mutableBy": "any"}]}),
    ]
}

/// One space's resolved `program` schema: the type id plus the
/// xKey→propId map.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProgramSchema {
    pub type_id: String,
    props: BTreeMap<String, String>,
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
        Ok(Some(ProgramSchema {
            type_id: tid,
            props,
        }))
    }

    /// Ensure type + properties + datasets, idempotently, and return
    /// the resolved schema. Only MISSING pieces are created.
    pub fn ensure(c: &Client, space: &str) -> anyhow::Result<Self> {
        let tid = match find_type(c, space)? {
            Some(t) => t,
            None => {
                let res = c.create_type(
                    space,
                    &json!({"name": PROGRAM_TYPE_NAME, "xKey": PROGRAM_TYPE_XKEY}),
                )?;
                res["typeId"]
                    .as_str()
                    .or_else(|| res["id"].as_str())
                    .ok_or_else(|| anyhow::anyhow!("create_type reply has no typeId: {res}"))?
                    .to_string()
            }
        };
        let mut props = prop_map(c, space, &tid)?;
        for (xkey, name, kind) in PROPS {
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
        let have: Vec<String> = c
            .list_datasets(space, &tid)?
            .iter()
            .filter_map(|d| d["name"].as_str().map(str::to_string))
            .collect();
        for draft in dataset_drafts() {
            let name = draft["name"].as_str().unwrap_or_default();
            if !have.iter().any(|h| h == name) {
                c.create_dataset(space, &tid, &draft)?;
            }
        }
        Ok(ProgramSchema {
            type_id: tid,
            props,
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
    let types = c.list_types(space)?;
    for t in &types {
        let key = t["xKey"].as_str().or_else(|| t["key"].as_str());
        if key == Some(PROGRAM_TYPE_XKEY) {
            return Ok(t["id"]
                .as_str()
                .or_else(|| t["typeId"].as_str())
                .map(str::to_string));
        }
    }
    // A pre-metatype "Program" (any PR #176) reads back with no xKey —
    // re-claim the handle in place rather than duplicating it.
    for t in &types {
        if t["xKey"].as_str().unwrap_or_default().is_empty() && t["name"] == PROGRAM_TYPE_NAME {
            if let Some(id) = t["id"].as_str() {
                c.set_properties(space, id, "type", &json!({"xkey": PROGRAM_TYPE_XKEY}))?;
                return Ok(Some(id.to_string()));
            }
        }
    }
    Ok(None)
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
        assert_eq!(c.list_types("sp").unwrap().len(), 1);
        assert_eq!(c.list_properties("sp", &s.type_id).unwrap().len(), 4);
        let ds = c.list_datasets("sp", &s.type_id).unwrap();
        let names: Vec<&str> = ds.iter().filter_map(|d| d["name"].as_str()).collect();
        assert_eq!(names, vec![SOURCE_DATASET, MANIFEST_DATASET]);
        // never a search target
        assert!(ds.iter().all(|d| d.get("search").is_none()));
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

    #[test]
    fn ensure_reclaims_a_pre_metatype_program_type() {
        let c = client();
        c.create_type("sp", &json!({"name": PROGRAM_TYPE_NAME}))
            .unwrap();
        let s = ProgramSchema::ensure(&c, "sp").unwrap();
        assert_eq!(c.list_types("sp").unwrap().len(), 1);
        assert_eq!(
            c.list_types("sp").unwrap()[0]["xKey"],
            json!(PROGRAM_TYPE_XKEY)
        );
        assert_eq!(c.list_types("sp").unwrap()[0]["id"], json!(s.type_id));
    }
}
