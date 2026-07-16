//! Deploy tool — the Rust twin of anybao/deploy.py + skills.py: the
//! generic program/skill publisher (plan §4). Reads a source dir and
//! writes to a TARGET space (the agent overlay, a std overlay, any
//! package space). Hash-gated: unchanged programs skip all writes.
//!
//! Program storage (mirrors internal/program): a `program`-typed object
//! per program, source in `program_source`/"main"/{code}, split tool
//! docs in `program_description`/"main"/{text} + one `program_methods`
//! record per method, `program.any_tool` = has description AND ≥1
//! method. A program is `<name>@<version>` (filename `name@vN.py`).
//! Optional capability manifest (sidecar `name@vN.manifest.json`) is
//! stored in `program_manifest`/"main" and mixed into the fingerprint —
//! manifest = request, grants bind to the content hash (00-plan
//! "Capabilities & trust"). The fingerprint MUST match the Python
//! deployer byte-for-byte: both sides hash the same split form.

// the ported surface IS the contract; the bin grows into it
#![allow(dead_code)]

use crate::anyapi::{AnyError, Client};
use crate::toolmd::{split_tool_markdown, MethodDoc};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::cell::RefCell;
use std::collections::BTreeMap;
use std::fmt;
use std::fmt::Write as _;
use std::path::Path;

pub const PROGRAM_TYPE: &str = "program";

/// Python-truthiness of a JSON value (`if manifest:`) — the manifest is
/// only mixed into the fingerprint when truthy, keeping no-manifest
/// fingerprints identical to the pre-manifest era.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `json.dumps(v, sort_keys=True, separators=(",", ":"))` — byte-exact,
/// including ensure_ascii's \uXXXX escapes (surrogate pairs for astral
/// chars). serde_json's Map is a BTreeMap, so keys are already sorted.
fn python_canonical_json(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => python_json_string(s, out),
        Value::Array(a) => {
            out.push('[');
            for (i, item) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                python_canonical_json(item, out);
            }
            out.push(']');
        }
        Value::Object(o) => {
            out.push('{');
            for (i, (k, item)) in o.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                python_json_string(k, out);
                out.push(':');
                python_canonical_json(item, out);
            }
            out.push('}');
        }
    }
}

fn python_json_string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            '\u{20}'..='\u{7e}' => out.push(c),
            _ => {
                let cp = c as u32;
                if cp < 0x10000 {
                    let _ = write!(out, "\\u{cp:04x}");
                } else {
                    // ensure_ascii encodes astral chars as surrogate pairs
                    let v = cp - 0x10000;
                    let _ = write!(
                        out,
                        "\\u{:04x}\\u{:04x}",
                        0xd800 + (v >> 10),
                        0xdc00 + (v & 0x3ff)
                    );
                }
            }
        }
    }
    out.push('"');
}

/// Fingerprint over the SPLIT form (code + description + sorted
/// methods) so disk and in-space sides normalize identically — never
/// over raw markdown (which wouldn't round-trip). method_tuples:
/// (bare_name, name, kind, text).
pub fn fingerprint(
    code: &str,
    desc: &str,
    method_tuples: &[(String, String, String, String)],
    manifest: &Value,
) -> String {
    let mut h = Sha256::new();
    h.update(code.as_bytes());
    h.update(b"\x00d\x00");
    h.update(desc.as_bytes());
    let mut sorted: Vec<&(String, String, String, String)> = method_tuples.iter().collect();
    sorted.sort();
    for (bare, name, kind, text) in sorted {
        h.update(format!("\x00m\x00{bare}\x00{name}\x00{kind}\x00{text}").as_bytes());
    }
    if truthy(manifest) {
        h.update(b"\x00man\x00");
        let mut canon = String::new();
        python_canonical_json(manifest, &mut canon);
        h.update(canon.as_bytes());
    }
    hex::encode(h.finalize())
}

#[derive(Debug, Clone)]
pub struct ProgramSource {
    pub name: String,
    pub version: String,
    pub code: String,
    /// optional tool-description markdown
    pub tool_md: String,
    /// optional capability manifest sidecar (CapBAC request half):
    /// {"capabilities": [...], "publisher": ..., "attestation": {...}?}
    pub manifest: Value,
}

impl ProgramSource {
    pub fn new(name: &str, version: &str, code: &str, tool_md: &str) -> Self {
        ProgramSource {
            name: name.to_string(),
            version: version.to_string(),
            code: code.to_string(),
            tool_md: tool_md.to_string(),
            manifest: json!({}),
        }
    }

    pub fn with_manifest(mut self, manifest: Value) -> Self {
        self.manifest = manifest;
        self
    }

    pub fn spec(&self) -> String {
        format!("{}@{}", self.name, self.version)
    }

    pub fn split(&self) -> (String, Vec<MethodDoc>) {
        if self.tool_md.is_empty() {
            (String::new(), Vec::new())
        } else {
            split_tool_markdown(&self.tool_md)
        }
    }

    pub fn fingerprint(&self) -> String {
        let (desc, methods) = self.split();
        let tuples: Vec<(String, String, String, String)> = methods
            .into_iter()
            .map(|m| (m.bare_name, m.name, m.kind, m.text))
            .collect();
        fingerprint(&self.code, &desc, &tuples, &self.manifest)
    }
}

/// `^(.+)@(v\d+)$` over the file stem → (name, version).
fn parse_name_version(stem: &str) -> Option<(&str, &str)> {
    let at = stem.rfind('@')?;
    let (name, version) = (&stem[..at], &stem[at + 1..]);
    let digits = version.strip_prefix('v')?;
    if name.is_empty() || digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    Some((name, version))
}

/// Read `<name>@vN.py` program files (+ optional `<name>@vN.md` tool
/// docs, optional `<name>@vN.manifest.json` capability manifests) from
/// a directory.
pub fn load_programs(src_dir: &Path) -> anyhow::Result<Vec<ProgramSource>> {
    let mut paths: Vec<std::path::PathBuf> = std::fs::read_dir(src_dir)?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("py"))
        .collect();
    paths.sort();
    let mut out = Vec::new();
    for py in paths {
        let stem = py.file_stem().and_then(|s| s.to_str()).unwrap_or("");
        let Some((name, version)) = parse_name_version(stem) else {
            continue;
        };
        let md = py.with_extension("md");
        let mf = py.with_extension("manifest.json");
        out.push(ProgramSource {
            name: name.to_string(),
            version: version.to_string(),
            code: std::fs::read_to_string(&py)?,
            tool_md: if md.exists() {
                std::fs::read_to_string(&md)?
            } else {
                String::new()
            },
            manifest: if mf.exists() {
                serde_json::from_str(&std::fs::read_to_string(&mf)?)?
            } else {
                json!({})
            },
        });
    }
    Ok(out)
}

/// Published overlay versions never mutate — edits bump `name@vN`.
#[derive(Debug)]
pub struct FrozenVersionError(pub String);

impl fmt::Display for FrozenVersionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{} is published in a frozen overlay — bump the version",
            self.0
        )
    }
}

impl std::error::Error for FrozenVersionError {}

pub struct Deployer<'a> {
    client: &'a Client,
    space: String,
    frozen: bool,
}

impl<'a> Deployer<'a> {
    pub fn new(client: &'a Client, space: &str) -> Self {
        Deployer {
            client,
            space: space.to_string(),
            frozen: false,
        }
    }

    pub fn frozen(mut self, frozen: bool) -> Self {
        self.frozen = frozen;
        self
    }

    /// Existing program object id by name+version, or None.
    fn find_program(&self, name: &str, version: &str) -> Result<Option<String>, AnyError> {
        let recs = self.client.query_objects(
            &self.space,
            &json!({"filter": {format!("{PROGRAM_TYPE}.name"): name,
                               format!("{PROGRAM_TYPE}.version"): version},
                    "limit": 1}),
        )?;
        Ok(recs
            .first()
            .and_then(|r| r["id"].as_str())
            .map(str::to_string))
    }

    fn in_space_fingerprint(&self, object_id: &str) -> Result<Option<String>, AnyError> {
        let src = self
            .client
            .query(&self.space, object_id, "program_source", &json!({}))?;
        let Some(main) = src.first() else {
            return Ok(None);
        };
        let code = main["code"].as_str().unwrap_or("");
        let desc_recs =
            self.client
                .query(&self.space, object_id, "program_description", &json!({}))?;
        let desc = desc_recs
            .first()
            .and_then(|r| r["text"].as_str())
            .unwrap_or("");
        let methods = self
            .client
            .query(&self.space, object_id, "program_methods", &json!({}))?;
        let tuples: Vec<(String, String, String, String)> = methods
            .iter()
            .map(|m| {
                (
                    m["id"].as_str().unwrap_or("").to_string(),
                    m["name"].as_str().unwrap_or("").to_string(),
                    m["kind"].as_str().unwrap_or("getter").to_string(),
                    m["text"].as_str().unwrap_or("").to_string(),
                )
            })
            .collect();
        let man_recs = self
            .client
            .query(&self.space, object_id, "program_manifest", &json!({}))?;
        let manifest = man_recs
            .first()
            .and_then(|r| r.get("manifest"))
            .cloned()
            .unwrap_or(json!({}));
        Ok(Some(fingerprint(code, desc, &tuples, &manifest)))
    }

    /// Create-or-update one program. Returns "created" | "updated" |
    /// "unchanged" (hash-gated).
    pub fn deploy_one(&self, p: &ProgramSource) -> anyhow::Result<&'static str> {
        let mut oid = self.find_program(&p.name, &p.version)?;
        if let Some(ref existing) = oid {
            if self.in_space_fingerprint(existing)?.as_deref() == Some(p.fingerprint().as_str()) {
                return Ok("unchanged");
            }
            if self.frozen {
                return Err(FrozenVersionError(p.spec()).into());
            }
        }

        let (desc, methods) = p.split();
        let any_tool = !desc.is_empty() && !methods.is_empty();

        let status = match oid {
            None => {
                let res = self.client.create_object(
                    &self.space,
                    &json!({
                    "types": [PROGRAM_TYPE],
                    "initialProperties": {
                        "any": {"name": p.name},
                        PROGRAM_TYPE: {"name": p.name, "version": p.version,
                                       "any_tool": any_tool},
                    }}),
                )?;
                oid = Some(
                    res["objectId"]
                        .as_str()
                        .ok_or_else(|| {
                            anyhow::anyhow!("create_object reply has no objectId: {res}")
                        })?
                        .to_string(),
                );
                "created"
            }
            Some(ref existing) => {
                self.client.set_properties(
                    &self.space,
                    existing,
                    PROGRAM_TYPE,
                    &json!({"any_tool": any_tool}),
                )?;
                "updated"
            }
        };
        let oid = oid.expect("object id is set on both branches");

        self.client.upsert_record(
            &self.space,
            &oid,
            "program_source",
            "main",
            &json!({"code": p.code}),
        )?;
        if truthy(&p.manifest) {
            self.client.upsert_record(
                &self.space,
                &oid,
                "program_manifest",
                "main",
                &json!({"manifest": p.manifest}),
            )?;
        } else {
            // a program that lost its manifest must not keep a stale one
            // (the in-space fingerprint would never converge)
            self.clear_dataset(&oid, "program_manifest")?;
        }
        // rewrite docs: clear then write (a program that lost its .md drops docs)
        self.clear_docs(&oid)?;
        if !desc.is_empty() {
            self.client.upsert_record(
                &self.space,
                &oid,
                "program_description",
                "main",
                &json!({"text": desc}),
            )?;
        }
        for m in &methods {
            self.client.upsert_record(
                &self.space,
                &oid,
                "program_methods",
                &m.bare_name,
                &json!({"name": m.name, "kind": m.kind, "text": m.text, "pos": m.pos}),
            )?;
        }
        Ok(status)
    }

    fn clear_docs(&self, oid: &str) -> Result<(), AnyError> {
        for dataset in ["program_description", "program_methods"] {
            self.clear_dataset(oid, dataset)?;
        }
        Ok(())
    }

    fn clear_dataset(&self, oid: &str, dataset: &str) -> Result<(), AnyError> {
        for rec in self.client.query(&self.space, oid, dataset, &json!({}))? {
            self.client.modify(
                &self.space,
                &json!({
                    "objectId": oid, "dataset": dataset,
                    "records": [{"id": rec["id"], "ops": [{"type": "$unset", "path": ""}]}]}),
            )?;
        }
        Ok(())
    }

    /// Deploy every program in a dir. Returns {spec: status}.
    pub fn deploy_dir(&self, src_dir: &Path) -> anyhow::Result<BTreeMap<String, String>> {
        let mut out = BTreeMap::new();
        for p in load_programs(src_dir)? {
            let status = self.deploy_one(&p)?;
            out.insert(p.spec(), status.to_string());
        }
        Ok(out)
    }
}

// --- skills — the system-prompt components (M4 "skills written fresh") ---

pub const SKILL_TYPE: &str = "agent_skill";

/// {name: markdown} from `<name>.md` files.
pub fn load_skills_dir(path: &Path) -> anyhow::Result<BTreeMap<String, String>> {
    let mut out = BTreeMap::new();
    for entry in std::fs::read_dir(path)? {
        let p = entry?.path();
        if p.extension().and_then(|e| e.to_str()) != Some("md") {
            continue;
        }
        if let Some(stem) = p.file_stem().and_then(|s| s.to_str()) {
            out.insert(stem.to_string(), std::fs::read_to_string(&p)?);
        }
    }
    Ok(out)
}

/// Ensure the agent_skill type exists WITH its name property and return
/// (typeId, namePropId). Both live-caught constraints: a fresh user
/// type has no schema (property writes rejected until one is defined),
/// and raw-client writes key type groups by typeID, not xKey (only
/// builtins have id == xKey).
fn skill_schema(client: &Client, space: &str) -> anyhow::Result<(String, String)> {
    let mut tid: Option<String> = None;
    for t in client.list_types(space)? {
        let key = t["xKey"].as_str().or_else(|| t["key"].as_str());
        if key == Some(SKILL_TYPE) {
            tid = t["id"]
                .as_str()
                .or_else(|| t["typeId"].as_str())
                .map(str::to_string);
            break;
        }
    }
    let tid = match tid {
        Some(t) => t,
        None => {
            let res =
                client.create_type(space, &json!({"name": "Agent Skill", "xKey": SKILL_TYPE}))?;
            res["typeId"]
                .as_str()
                .or_else(|| res["id"].as_str())
                .ok_or_else(|| anyhow::anyhow!("create_type reply has no typeId: {res}"))?
                .to_string()
        }
    };
    for p in client.list_properties(space, &tid)? {
        if p["xKey"].as_str() == Some("name") {
            let pid = p["id"]
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("name property has no id: {p}"))?
                .to_string();
            return Ok((tid, pid));
        }
    }
    let prop = client.add_property(
        space,
        &tid,
        &json!({"name": "Name", "xKey": "name", "kind": "string"}),
    )?;
    let pid = prop["propId"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("add_property reply has no propId: {prop}"))?
        .to_string();
    Ok((tid, pid))
}

/// Skills counterpart of Deployer: agent_skill objects, markdown
/// content, hash-gated by content comparison.
pub struct SkillDeployer<'a> {
    client: &'a Client,
    space: String,
    schema: RefCell<Option<(String, String)>>,
}

impl<'a> SkillDeployer<'a> {
    pub fn new(client: &'a Client, space: &str) -> Self {
        SkillDeployer {
            client,
            space: space.to_string(),
            schema: RefCell::new(None),
        }
    }

    fn ensure_type(&self) -> anyhow::Result<(String, String)> {
        if self.schema.borrow().is_none() {
            *self.schema.borrow_mut() = Some(skill_schema(self.client, &self.space)?);
        }
        Ok(self.schema.borrow().clone().expect("schema just ensured"))
    }

    fn find(&self, name: &str) -> anyhow::Result<Option<String>> {
        let (tid, prop) = self.ensure_type()?;
        let recs = self.client.query_objects(
            &self.space,
            &json!({"filter": {format!("{tid}.{prop}"): name}, "limit": 1}),
        )?;
        Ok(recs
            .first()
            .and_then(|r| r["id"].as_str())
            .map(str::to_string))
    }

    pub fn deploy_one(&self, name: &str, content: &str) -> anyhow::Result<&'static str> {
        if let Some(oid) = self.find(name)? {
            if self.client.get_markdown(&self.space, &oid)? == content {
                return Ok("unchanged");
            }
            self.client.put_markdown(&self.space, &oid, content)?;
            return Ok("updated");
        }
        let (tid, prop) = self.ensure_type()?;
        let res = self.client.create_object(
            &self.space,
            &json!({"types": [tid],
                    "initialProperties": {"any": {"name": name}, tid: {prop: name}}}),
        )?;
        let oid = res["objectId"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("create_object reply has no objectId: {res}"))?;
        self.client.put_markdown(&self.space, oid, content)?;
        Ok("created")
    }

    pub fn deploy_dir(&self, src_dir: &Path) -> anyhow::Result<BTreeMap<String, String>> {
        self.ensure_type()?;
        let mut out = BTreeMap::new();
        for (name, content) in load_skills_dir(src_dir)? {
            let status = self.deploy_one(&name, &content)?;
            out.insert(name, status.to_string());
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::FakeSpace;
    use tempfile::tempdir;

    const PROG: &str = "def main(args):\n    return 1\n";
    const TOOL_MD: &str = "## Tool Description\n\nDoes a thing.\n\n\
## Tool Schema\n### go(x) [getter]\n\nruns go.\n";

    fn manifest() -> Value {
        json!({"capabilities": ["net.http"], "publisher": "acme"})
    }

    // --- fingerprint: golden parity with the Python deployer ---
    // Expected hex values computed with anybao.deploy.ProgramSource
    // against the Python reference sources (py-reference), 2026-07-08.

    #[test]
    fn fingerprint_golden_matches_python() {
        let p = ProgramSource::new("t", "v1", PROG, TOOL_MD);
        assert_eq!(
            p.fingerprint(),
            "56803ac081bcfc341a0dba40bf97077be5faf781198284877b3bdc1ecf084cf7"
        );
    }

    #[test]
    fn fingerprint_golden_with_manifest() {
        let p = ProgramSource::new("t", "v1", PROG, TOOL_MD).with_manifest(manifest());
        assert_eq!(
            p.fingerprint(),
            "95a1703493a548efe052a1fec89a76dead1a5d5cebe388398cc90cba94ec9794"
        );
    }

    #[test]
    fn fingerprint_golden_no_docs() {
        let p = ProgramSource::new("t", "v1", PROG, "");
        assert_eq!(
            p.fingerprint(),
            "0debfbfca65bc345eff4696d3e06a62f026011552eae6094b39b7365c2618cb2"
        );
    }

    #[test]
    fn fingerprint_golden_multi_method_sorted() {
        // dup bare names + unicode + method sorting all in play
        let md = "## Tool Description\nDesc — with unicode ✓\n\n# Tools\n\
## zeta(a) [mutator]\nlast\n## alpha(b)\nfirst\n## alpha(c) [setup]\ndup\n";
        let p = ProgramSource::new("x", "v2", "code2", md);
        assert_eq!(
            p.fingerprint(),
            "6f06490dfb670434306d2c0575cba93335397a286c98f542dbdf749c4bfe0423"
        );
    }

    #[test]
    fn fingerprint_golden_unicode_manifest_ensure_ascii() {
        // exercises json.dumps(ensure_ascii=True) parity: é, ✓,
        //  and an astral surrogate pair
        let man = json!({"publisher": "acmé ✓", "capabilities": ["net.http"],
                         "note": "line\nbreak \"q\" \u{7f} 🎉"});
        let p = ProgramSource::new("t", "v1", "c", "").with_manifest(man.clone());
        let mut canon = String::new();
        python_canonical_json(&man, &mut canon);
        assert_eq!(
            canon,
            r#"{"capabilities":["net.http"],"note":"line\nbreak \"q\" \u007f \ud83c\udf89","publisher":"acm\u00e9 \u2713"}"#
        );
        assert_eq!(
            p.fingerprint(),
            "c63e2485799b0b7a56acb8c09e3bff6d51a7e5d6ae054e0dde526ba2b41bb6ad"
        );
    }

    #[test]
    fn fingerprint_sensitivity() {
        let p = ProgramSource::new("t", "v1", PROG, TOOL_MD);
        let code_change = ProgramSource::new("t", "v1", &format!("{PROG}# x"), TOOL_MD);
        let md_change = ProgramSource::new("t", "v1", PROG, &format!("{TOOL_MD}more"));
        assert_ne!(p.fingerprint(), code_change.fingerprint());
        assert_ne!(p.fingerprint(), md_change.fingerprint());
        // stable across instances
        assert_eq!(
            p.fingerprint(),
            ProgramSource::new("t", "v1", PROG, TOOL_MD).fingerprint()
        );
        // empty manifest keeps the pre-manifest fingerprint
        assert_eq!(
            p.fingerprint(),
            ProgramSource::new("t", "v1", PROG, TOOL_MD)
                .with_manifest(json!({}))
                .fingerprint()
        );
        // manifest edit changes the hash again
        let with_man = ProgramSource::new("t", "v1", PROG, TOOL_MD).with_manifest(manifest());
        let edited = ProgramSource::new("t", "v1", PROG, TOOL_MD)
            .with_manifest(json!({"capabilities": ["net.http", "chat.send"], "publisher": "acme"}));
        assert_ne!(p.fingerprint(), with_man.fingerprint());
        assert_ne!(with_man.fingerprint(), edited.fingerprint());
    }

    // --- load_programs ---

    #[test]
    fn load_programs_parses_name_version() {
        let dir = tempdir().unwrap();
        std::fs::write(dir.path().join("websearch@v1.py"), PROG).unwrap();
        std::fs::write(dir.path().join("websearch@v1.md"), TOOL_MD).unwrap();
        std::fs::write(dir.path().join("notaprogram.py"), "x").unwrap(); // no @vN → skipped
        let progs = load_programs(dir.path()).unwrap();
        assert_eq!(progs.len(), 1);
        assert_eq!(
            (progs[0].name.as_str(), progs[0].version.as_str()),
            ("websearch", "v1")
        );
        assert_eq!(progs[0].tool_md, TOOL_MD);
    }

    #[test]
    fn load_programs_reads_manifest_sidecar() {
        let dir = tempdir().unwrap();
        std::fs::write(dir.path().join("websearch@v1.py"), PROG).unwrap();
        std::fs::write(
            dir.path().join("websearch@v1.manifest.json"),
            manifest().to_string(),
        )
        .unwrap();
        let progs = load_programs(dir.path()).unwrap();
        assert_eq!(progs[0].manifest, manifest());
        // absent sidecar → empty manifest
        std::fs::write(dir.path().join("plain@v1.py"), PROG).unwrap();
        assert_eq!(load_programs(dir.path()).unwrap()[0].manifest, json!({}));
    }

    // --- deploy flows over the in-memory fake space ---

    fn client() -> Client {
        Client::with_transport(Box::new(FakeSpace::new()))
    }

    #[test]
    fn deploy_creates_then_unchanged() {
        let c = client();
        let d = Deployer::new(&c, "agent");
        let p = ProgramSource::new("websearch", "v1", PROG, TOOL_MD);

        assert_eq!(d.deploy_one(&p).unwrap(), "created");
        // redeploy identical → hash-gated skip
        assert_eq!(d.deploy_one(&p).unwrap(), "unchanged");
        // the written source round-trips
        let src = c
            .query("agent", "obj1", "program_source", &json!({}))
            .unwrap();
        assert_eq!(src[0]["code"], json!(PROG));
        let methods = c
            .query("agent", "obj1", "program_methods", &json!({}))
            .unwrap();
        assert_eq!(methods.len(), 1);
        assert_eq!(methods[0]["id"], json!("go"));
    }

    #[test]
    fn deploy_updates_on_code_change() {
        let c = client();
        let d = Deployer::new(&c, "agent");
        d.deploy_one(&ProgramSource::new("t", "v1", PROG, TOOL_MD))
            .unwrap();
        let changed = ProgramSource::new("t", "v1", &format!("{PROG}# changed"), TOOL_MD);
        assert_eq!(d.deploy_one(&changed).unwrap(), "updated");
        let src = c
            .query("agent", "obj1", "program_source", &json!({}))
            .unwrap();
        assert!(src[0]["code"].as_str().unwrap().contains("# changed"));
    }

    #[test]
    fn deploy_without_tooldoc_is_not_a_tool() {
        let c = client();
        let d = Deployer::new(&c, "agent");
        d.deploy_one(&ProgramSource::new("lib", "v1", PROG, ""))
            .unwrap();
        let progs = c
            .query_objects("agent", &json!({"filter": {"program.any_tool": true}}))
            .unwrap();
        assert!(progs.is_empty());
        let desc = c
            .query("agent", "obj1", "program_description", &json!({}))
            .unwrap();
        assert!(desc.is_empty());
    }

    #[test]
    fn frozen_space_rejects_changes_but_allows_unchanged() {
        let c = client();
        let p = ProgramSource::new("t", "v1", PROG, TOOL_MD);
        Deployer::new(&c, "agent").deploy_one(&p).unwrap();
        let frozen = Deployer::new(&c, "agent").frozen(true);
        assert_eq!(frozen.deploy_one(&p).unwrap(), "unchanged");
        let changed = ProgramSource::new("t", "v1", "new code", TOOL_MD);
        let err = frozen.deploy_one(&changed).unwrap_err();
        assert!(err.downcast_ref::<FrozenVersionError>().is_some());
        assert!(err
            .to_string()
            .contains("t@v1 is published in a frozen overlay"));
    }

    #[test]
    fn deploy_writes_manifest_record_and_hash_gates() {
        let c = client();
        let d = Deployer::new(&c, "agent");
        let p = ProgramSource::new("t", "v1", PROG, TOOL_MD).with_manifest(manifest());
        assert_eq!(d.deploy_one(&p).unwrap(), "created");
        let man = c
            .query("agent", "obj1", "program_manifest", &json!({}))
            .unwrap();
        assert_eq!(man[0]["manifest"], manifest());
        assert_eq!(d.deploy_one(&p).unwrap(), "unchanged"); // manifest round-trips
                                                            // manifest edit → new hash → update; dropped manifest clears the record
        let edited = ProgramSource::new("t", "v1", PROG, TOOL_MD)
            .with_manifest(json!({"capabilities": ["net.http", "chat.send"]}));
        assert_eq!(d.deploy_one(&edited).unwrap(), "updated");
        assert_eq!(
            d.deploy_one(&ProgramSource::new("t", "v1", PROG, TOOL_MD))
                .unwrap(),
            "updated"
        );
        assert!(c
            .query("agent", "obj1", "program_manifest", &json!({}))
            .unwrap()
            .is_empty());
    }

    #[test]
    fn deploy_dir_reports_per_spec_status() {
        let dir = tempdir().unwrap();
        std::fs::write(dir.path().join("a@v1.py"), PROG).unwrap();
        std::fs::write(dir.path().join("b@v2.py"), PROG).unwrap();
        let c = client();
        let d = Deployer::new(&c, "agent");
        let out = d.deploy_dir(dir.path()).unwrap();
        assert_eq!(out.get("a@v1").map(String::as_str), Some("created"));
        assert_eq!(out.get("b@v2").map(String::as_str), Some("created"));
        assert_eq!(
            d.deploy_dir(dir.path())
                .unwrap()
                .get("a@v1")
                .map(String::as_str),
            Some("unchanged")
        );
    }

    // --- skills ---

    #[test]
    fn skill_deploy_created_updated_unchanged() {
        let c = client();
        let sd = SkillDeployer::new(&c, "agent");
        assert_eq!(sd.deploy_one("_core", "core body").unwrap(), "created");
        assert_eq!(sd.deploy_one("_core", "core body").unwrap(), "unchanged");
        assert_eq!(sd.deploy_one("_core", "new body").unwrap(), "updated");
    }

    #[test]
    fn skill_deploy_dir_and_schema_reuse() {
        let dir = tempdir().unwrap();
        std::fs::write(dir.path().join("_core.md"), "core").unwrap();
        std::fs::write(dir.path().join("_soul.md"), "soul").unwrap();
        let c = client();
        let sd = SkillDeployer::new(&c, "agent");
        let out = sd.deploy_dir(dir.path()).unwrap();
        assert_eq!(out.get("_core").map(String::as_str), Some("created"));
        assert_eq!(out.get("_soul").map(String::as_str), Some("created"));
    }

    // --- name@vN parsing edges ---

    #[test]
    fn parse_name_version_edges() {
        assert_eq!(
            parse_name_version("websearch@v1"),
            Some(("websearch", "v1"))
        );
        assert_eq!(parse_name_version("a@b@v2"), Some(("a@b", "v2")));
        assert_eq!(parse_name_version("noversion"), None);
        assert_eq!(parse_name_version("bad@vx"), None);
        assert_eq!(parse_name_version("bad@v"), None);
        assert_eq!(parse_name_version("@v1"), None);
    }
}
