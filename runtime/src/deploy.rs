//! Deploy tool — the generic program/skill publisher (plan §4). Reads
//! a source dir and writes to a TARGET space (the agent overlay, a std
//! overlay, any package space). Hash-gated: unchanged programs skip
//! all writes.
//!
//! Program storage (ADR-010 §5/§6): a `program`-typed object per
//! program — `program` is a harness-declared USER type that deploy
//! ensures per target space (`program_schema`, xKey `program`) —
//! source in `program_source`/"main"/{code} — docs live in the
//! source's docstrings, there are no doc datasets. Deploy DERIVES the cached
//! properties from a static source scan (ADR-010 §4): `summary` = the
//! module docstring's first line; `any_tool` = the source declares
//! `__any_tool__ = True`, validated to carry the tool shape (docstring
//! within budget + ≥1 public `@span`-tagged def). A program is
//! `<name>@<version>` (filename `name@vN.py` or `name@vN/program.py`).
//! Optional capability manifest (sidecar `name@vN.manifest.json`) is
//! stored in `program_manifest`/"main" and mixed into the fingerprint —
//! manifest = request, grants bind to the content hash (00-plan
//! "Capabilities & trust").

// the ported surface IS the contract; the bin grows into it
#![allow(dead_code)]

use crate::anyapi::{AnyError, Client};
use crate::program_schema::{ProgramSchema, MANIFEST_DATASET, SOURCE_DATASET};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::cell::RefCell;
use std::collections::BTreeMap;
use std::fmt;
use std::fmt::Write as _;
use std::path::Path;

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

// --- static source scan (ADR-010 §4): a convention check, not a parser ---

/// The module docstring: the source's first statement when it is a
/// string literal (shebang/encoding/comment/blank lines skipped).
/// Triple- or single-quoted; the quotes are stripped.
pub fn module_docstring(code: &str) -> Option<String> {
    let mut rest = code;
    loop {
        let line_end = rest.find('\n').map(|i| i + 1).unwrap_or(rest.len());
        let line = rest[..line_end].trim();
        if line.is_empty() || line.starts_with('#') {
            if line_end == rest.len() {
                return None;
            }
            rest = &rest[line_end..];
            continue;
        }
        break;
    }
    for q in ["\"\"\"", "'''", "\"", "'"] {
        if let Some(body) = rest.trim_start().strip_prefix(q) {
            let end = body.find(q)?;
            return Some(body[..end].to_string());
        }
    }
    None
}

/// First non-empty docstring line, trimmed — the cached one-liner.
pub fn summary_of(docstring: &str) -> String {
    docstring
        .lines()
        .find(|l| !l.trim().is_empty())
        .unwrap_or("")
        .trim()
        .to_string()
}

/// The source declares itself an agent tool (ADR-010 §4).
pub fn has_any_tool_marker(code: &str) -> bool {
    code.lines()
        .any(|l| l.trim_start().starts_with("__any_tool__ = True"))
}

/// ≥1 `@span(...)`-decorated def whose name is public — any nesting
/// (class methods count). Comments/blank lines between decorator and
/// def are tolerated; any other statement resets the pending state.
pub fn has_public_span_def(code: &str) -> bool {
    let mut pending = false;
    for line in code.lines() {
        let t = line.trim_start();
        if t.starts_with("@span(") {
            pending = true;
        } else if t.starts_with('@') || t.starts_with('#') || t.is_empty() {
            // another decorator / comment / blank: keep the pending span
        } else if let Some(rest) = t.strip_prefix("def ") {
            if pending && !rest.starts_with('_') {
                return true;
            }
            pending = false;
        } else {
            pending = false;
        }
    }
    false
}

/// ADR-010 §1 hard caps for a tool's module docstring.
const SUMMARY_MAX_CHARS: usize = 80;
const DOCSTRING_MAX_LINES: usize = 12;
const DOCSTRING_MAX_CHARS: usize = 800;

/// Fingerprint over code + the derived properties + manifest. The
/// derived values are hashed too so a derivation-logic change (new
/// scan rules) re-deploys even when the code bytes are identical.
pub fn fingerprint(code: &str, summary: &str, any_tool: bool, manifest: &Value) -> String {
    let mut h = Sha256::new();
    h.update(code.as_bytes());
    h.update(b"\x00s\x00");
    h.update(summary.as_bytes());
    h.update(b"\x00t\x00");
    h.update(if any_tool { b"1" } else { b"0" });
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
    /// optional capability manifest sidecar (CapBAC request half):
    /// {"capabilities": [...], "publisher": ..., "attestation": {...}?}
    pub manifest: Value,
}

impl ProgramSource {
    pub fn new(name: &str, version: &str, code: &str) -> Self {
        ProgramSource {
            name: name.to_string(),
            version: version.to_string(),
            code: code.to_string(),
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

    pub fn summary(&self) -> String {
        module_docstring(&self.code)
            .map(|d| summary_of(&d))
            .unwrap_or_default()
    }

    pub fn any_tool(&self) -> bool {
        has_any_tool_marker(&self.code)
    }

    /// ADR-010 §4: a program marked `__any_tool__ = True` must carry
    /// the tool shape — module docstring within §1's caps and ≥1
    /// public `@span`-tagged def. Errors say what to fix.
    pub fn validate(&self) -> anyhow::Result<()> {
        if !self.any_tool() {
            return Ok(());
        }
        let spec = self.spec();
        let Some(doc) = module_docstring(&self.code) else {
            anyhow::bail!(
                "{spec} declares __any_tool__ but has no module docstring — \
                 write a short one (first line = the one-liner, ADR-010 §1)"
            );
        };
        let summary = summary_of(&doc);
        if summary.is_empty() {
            anyhow::bail!("{spec}: module docstring has no content (ADR-010 §1)");
        }
        if summary.chars().count() > SUMMARY_MAX_CHARS {
            anyhow::bail!(
                "{spec}: docstring first line is {} chars — the one-liner \
                 caps at {SUMMARY_MAX_CHARS} (ADR-010 §1)",
                summary.chars().count()
            );
        }
        let lines = doc.trim().lines().count();
        if lines > DOCSTRING_MAX_LINES || doc.chars().count() > DOCSTRING_MAX_CHARS {
            anyhow::bail!(
                "{spec}: module docstring is {lines} lines / {} chars — it is \
                 standing prompt, cap {DOCSTRING_MAX_LINES} lines / \
                 {DOCSTRING_MAX_CHARS} chars (ADR-010 §1; move depth into \
                 method docstrings)",
                doc.chars().count()
            );
        }
        if !has_public_span_def(&self.code) {
            anyhow::bail!(
                "{spec} declares __any_tool__ but no public @span-tagged def — \
                 span every tool method (ADR-010 §1)"
            );
        }
        Ok(())
    }

    pub fn fingerprint(&self) -> String {
        fingerprint(&self.code, &self.summary(), self.any_tool(), &self.manifest)
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

/// Read program sources from `src_dir`. Two layouts, mixed freely:
///   - **flat**: `<name>@vN.py` (+ optional `<name>@vN.manifest.json`).
///   - **folder**: `<name>@vN/` holding `program.py` + optional
///     `manifest.json`. Anything else in the folder (tests, fixtures)
///     is ignored.
///
/// Docs are the source's docstrings (ADR-010 §1) — there are no doc
/// sidecar files.
pub fn load_programs(src_dir: &Path) -> anyhow::Result<Vec<ProgramSource>> {
    let mut paths: Vec<std::path::PathBuf> = std::fs::read_dir(src_dir)?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .collect();
    paths.sort();
    let mut out = Vec::new();
    for p in paths {
        let src = if p.is_dir() {
            load_program_dir(&p)?
        } else if p.extension().and_then(|e| e.to_str()) == Some("py") {
            load_flat_program(&p)?
        } else {
            None
        };
        if let Some(src) = src {
            out.push(src);
        }
    }
    Ok(out)
}

fn read_opt(p: &Path) -> anyhow::Result<String> {
    Ok(if p.exists() {
        std::fs::read_to_string(p)?
    } else {
        String::new()
    })
}

fn load_flat_program(py: &Path) -> anyhow::Result<Option<ProgramSource>> {
    let stem = py.file_stem().and_then(|s| s.to_str()).unwrap_or("");
    let Some((name, version)) = parse_name_version(stem) else {
        return Ok(None);
    };
    let mf = py.with_extension("manifest.json");
    Ok(Some(ProgramSource {
        name: name.to_string(),
        version: version.to_string(),
        code: std::fs::read_to_string(py)?,
        manifest: load_manifest(&mf)?,
    }))
}

fn load_program_dir(dir: &Path) -> anyhow::Result<Option<ProgramSource>> {
    let stem = dir.file_name().and_then(|s| s.to_str()).unwrap_or("");
    let Some((name, version)) = parse_name_version(stem) else {
        return Ok(None); // not a program folder (e.g. a shared helper dir)
    };
    let code_path = dir.join("program.py");
    if !code_path.exists() {
        return Ok(None);
    }
    Ok(Some(ProgramSource {
        name: name.to_string(),
        version: version.to_string(),
        code: std::fs::read_to_string(&code_path)?,
        manifest: load_manifest(&dir.join("manifest.json"))?,
    }))
}

fn load_manifest(p: &Path) -> anyhow::Result<Value> {
    Ok(if p.exists() {
        serde_json::from_str(&std::fs::read_to_string(p)?)?
    } else {
        json!({})
    })
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
    /// the target space's ensured `program` schema (lazy, once)
    schema: RefCell<Option<ProgramSchema>>,
}

impl<'a> Deployer<'a> {
    pub fn new(client: &'a Client, space: &str) -> Self {
        Deployer {
            client,
            space: space.to_string(),
            frozen: false,
            schema: RefCell::new(None),
        }
    }

    /// The target space's `program` schema, ensured once per deployer
    /// (type + properties + the two datasets — ADR-010 §5).
    fn schema(&self) -> anyhow::Result<ProgramSchema> {
        if self.schema.borrow().is_none() {
            *self.schema.borrow_mut() = Some(ProgramSchema::ensure(self.client, &self.space)?);
        }
        Ok(self.schema.borrow().clone().expect("ensured above"))
    }

    pub fn frozen(mut self, frozen: bool) -> Self {
        self.frozen = frozen;
        self
    }

    /// Existing program object by name+version: (id, program prop
    /// group) — the props feed the in-space fingerprint.
    fn find_program(&self, name: &str, version: &str) -> anyhow::Result<Option<(String, Value)>> {
        let s = self.schema()?;
        let recs = self.client.query_objects(
            &self.space,
            &json!({"filter": {s.path("name"): name, s.path("version"): version},
                    "limit": 1}),
        )?;
        Ok(recs
            .first()
            .and_then(|r| r["id"].as_str().map(|id| (id.to_string(), s.read(r)))))
    }

    fn in_space_fingerprint(
        &self,
        object_id: &str,
        props: &Value,
    ) -> Result<Option<String>, AnyError> {
        let src = self
            .client
            .query(&self.space, object_id, SOURCE_DATASET, &json!({}))?;
        let Some(main) = src.first() else {
            return Ok(None);
        };
        let code = main["code"].as_str().unwrap_or("");
        let summary = props["summary"].as_str().unwrap_or("");
        let any_tool = props["any_tool"].as_bool().unwrap_or(false);
        let man_recs = self
            .client
            .query(&self.space, object_id, MANIFEST_DATASET, &json!({}))?;
        let manifest = man_recs
            .first()
            .and_then(|r| r.get("manifest"))
            .cloned()
            .unwrap_or(json!({}));
        Ok(Some(fingerprint(code, summary, any_tool, &manifest)))
    }

    /// Create-or-update one program. Returns "created" | "updated" |
    /// "unchanged" (hash-gated). Validates the ADR-010 §1/§4 tool
    /// contract before any write.
    pub fn deploy_one(&self, p: &ProgramSource) -> anyhow::Result<&'static str> {
        p.validate()?;
        let found = self.find_program(&p.name, &p.version)?;
        if let Some((ref existing, ref props)) = found {
            if self.in_space_fingerprint(existing, props)?.as_deref()
                == Some(p.fingerprint().as_str())
            {
                return Ok("unchanged");
            }
            if self.frozen {
                return Err(FrozenVersionError(p.spec()).into());
            }
        }

        let (any_tool, summary) = (p.any_tool(), p.summary());
        let s = self.schema()?;
        let (oid, status) = match found {
            None => {
                let res = self.client.create_object(
                    &self.space,
                    &json!({
                    "types": [s.type_id],
                    "initialProperties": {
                        "any": {"name": p.name},
                        s.type_id.clone(): s.group(&[
                            ("name", json!(p.name)), ("version", json!(p.version)),
                            ("any_tool", json!(any_tool)), ("summary", json!(summary))]),
                    }}),
                )?;
                let oid = res["objectId"]
                    .as_str()
                    .ok_or_else(|| anyhow::anyhow!("create_object reply has no objectId: {res}"))?
                    .to_string();
                (oid, "created")
            }
            Some((existing, _)) => {
                self.client.set_properties(
                    &self.space,
                    &existing,
                    &s.type_id,
                    &s.group(&[("any_tool", json!(any_tool)), ("summary", json!(summary))]),
                )?;
                (existing, "updated")
            }
        };

        self.client.upsert_record(
            &self.space,
            &oid,
            SOURCE_DATASET,
            "main",
            &json!({"code": p.code}),
        )?;
        if truthy(&p.manifest) {
            self.client.upsert_record(
                &self.space,
                &oid,
                MANIFEST_DATASET,
                "main",
                &json!({"manifest": p.manifest}),
            )?;
        } else {
            // a program that lost its manifest must not keep a stale one
            // (the in-space fingerprint would never converge)
            self.clear_dataset(&oid, MANIFEST_DATASET)?;
        }
        Ok(status)
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

/// Find-or-create a `type_id`-typed object by `any.name` — the trigger
/// anchor / kernel object / README pattern (unregistered custom type
/// keys work; the server materializes them).
///
/// Racing starts (query-then-create, plus index lag right after a
/// create) can leave SEVERAL anchors; with `limit: 1` + first-row pick
/// every restart re-rolled which one a serve followed, silently
/// splitting dataset state across them (seen live 2026-08-12: two
/// `agent-triggers` anchors on prod, the running serve blind to the
/// one carrying all trigger history). These are identity anchors, so
/// the OLDEST wins — (createdAt, id), rows without a createdAt stamp
/// rank oldest — and every caller converges on the same object.
/// Stale duplicates are not deleted here: their datasets may hold
/// records to merge, which is a per-caller decision.
pub fn ensure_typed(c: &Client, space: &str, name: &str, type_id: &str) -> anyhow::Result<String> {
    let rows = c.query_objects(
        space,
        &json!({
        "filter": {"any.name": name, "any.types": type_id}, "limit": 50}),
    )?;
    let winner = rows.iter().min_by_key(|r| {
        let created = instant_ms(&r["createdAt"]).unwrap_or(i64::MIN);
        (created, r["id"].as_str().unwrap_or_default().to_string())
    });
    if let Some(r) = winner {
        return Ok(r["id"].as_str().unwrap_or_default().to_string());
    }
    let created = c.create_object(
        space,
        &json!({
        "types": [type_id],
        "initialProperties": {"any": {"name": name}}}),
    )?;
    Ok(created["objectId"].as_str().unwrap_or_default().to_string())
}

/// Unix millis of a server instant `{"$date": "<RFC 3339>" | <millis>}`
/// (ADR-019 §1); a bare number is read as unix seconds (a row an older
/// peer materialized). Anything else → None.
pub fn instant_ms(v: &Value) -> Option<i64> {
    match v {
        Value::Object(o) => match o.get("$date") {
            Some(Value::String(s)) => chrono::DateTime::parse_from_rfc3339(s)
                .ok()
                .map(|d| d.timestamp_millis()),
            Some(Value::Number(n)) => n.as_f64().map(|ms| ms as i64),
            _ => None,
        },
        Value::Number(n) => n.as_f64().map(|s| (s * 1000.0) as i64),
        _ => None,
    }
}

// --- repo deploy — a source folder published to a space (ADR-009 §2) ---

pub const README_TYPE: &str = "readme";

/// The overlay's description object, from the source root's README.md.
/// Hash-gated by content comparison (a fresh object reads back "").
pub fn deploy_readme(client: &Client, space: &str, content: &str) -> anyhow::Result<&'static str> {
    let oid = ensure_typed(client, space, "README", README_TYPE)?;
    if client.get_markdown(space, &oid)? == content {
        return Ok("unchanged");
    }
    client.put_markdown(space, &oid, content)?;
    Ok("updated")
}

#[derive(Debug, Default)]
pub struct RepoSummary {
    pub programs: BTreeMap<String, String>,
    pub skills: BTreeMap<String, String>,
    pub readme: Option<&'static str>,
}

/// Deploy one repo folder: `<src>/programs/`, `<src>/skills/`, and a
/// root `README.md` (each optional — kinds are the subfolders present).
pub fn deploy_repo(client: &Client, space: &str, src: &Path) -> anyhow::Result<RepoSummary> {
    let mut out = RepoSummary::default();
    let programs = src.join("programs");
    if programs.is_dir() {
        out.programs = Deployer::new(client, space).deploy_dir(&programs)?;
    }
    let skills = src.join("skills");
    if skills.is_dir() {
        out.skills = SkillDeployer::new(client, space).deploy_dir(&skills)?;
    }
    let readme = src.join("README.md");
    if readme.is_file() {
        out.readme = Some(deploy_readme(
            client,
            space,
            &std::fs::read_to_string(&readme)?,
        )?);
    }
    Ok(out)
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
    let types = client.list_types(space)?;
    for t in &types {
        let key = t["xKey"].as_str().or_else(|| t["key"].as_str());
        if key == Some(SKILL_TYPE) {
            tid = t["id"]
                .as_str()
                .or_else(|| t["typeId"].as_str())
                .map(str::to_string);
            break;
        }
    }
    // A pre-metatype "Agent Skill" (any PR #176) reads back with no
    // xKey — re-claim the handle in place rather than duplicating it.
    if tid.is_none() {
        for t in &types {
            if t["xKey"].as_str().unwrap_or_default().is_empty() && t["name"] == "Agent Skill" {
                if let Some(id) = t["id"].as_str() {
                    client.set_properties(space, id, "type", &json!({"xkey": SKILL_TYPE}))?;
                    tid = Some(id.to_string());
                    break;
                }
            }
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

    /// A well-formed tool source: docstring + marker + spanned def.
    const TOOL: &str = concat!(
        "\"\"\"One-liner summary.\n",
        "\n",
        "Body line of the docstring.\"\"\"\n",
        "\n",
        "__any_tool__ = True\n",
        "\n",
        "\n",
        "@span(\"t.go\", kind=\"getter\")\n",
        "def go(x):\n",
        "    \"\"\"Run go.\"\"\"\n",
        "    return x\n",
    );

    fn manifest() -> Value {
        json!({"capabilities": ["net.http"], "publisher": "acme"})
    }

    // --- static source scan (ADR-010 §4) ---

    #[test]
    fn instant_ms_reads_both_wire_forms_and_legacy_seconds() {
        assert_eq!(
            instant_ms(&json!({"$date": "2026-08-25T16:00:00.000Z"})),
            Some(1787673600000)
        );
        assert_eq!(
            instant_ms(&json!({"$date": "2026-08-25T18:00:00+02:00"})),
            Some(1787673600000)
        );
        assert_eq!(
            instant_ms(&json!({"$date": 1787673600000i64})),
            Some(1787673600000)
        );
        assert_eq!(instant_ms(&json!(1787673600.0)), Some(1787673600000));
        assert_eq!(instant_ms(&json!({"$date": "soon"})), None);
        assert_eq!(instant_ms(&Value::Null), None);
    }

    #[test]
    fn ensure_typed_oldest_instant_wins_over_smaller_id() {
        // ADR-019 §6: the anchor tiebreak parses instants — with the
        // pre-instant `as_f64` every row ranked i64::MIN and the pick
        // silently became "smallest id" (a different anchor than the
        // one carrying trigger history)
        let fake = FakeSpace::new();
        fake.seed_object(
            "sp",
            "zzz-older",
            json!({
            "any": {"name": "agent-triggers", "types": ["anchor"]},
            "createdAt": {"$date": "2026-08-01T00:00:00Z"}}),
        );
        fake.seed_object(
            "sp",
            "aaa-newer",
            json!({
            "any": {"name": "agent-triggers", "types": ["anchor"]},
            "createdAt": {"$date": 1787673600000i64}}),
        );
        let c = Client::with_transport(Box::new(fake));
        assert_eq!(
            ensure_typed(&c, "sp", "agent-triggers", "anchor").unwrap(),
            "zzz-older"
        );
    }

    #[test]
    fn module_docstring_forms() {
        assert_eq!(
            module_docstring(TOOL).as_deref(),
            Some("One-liner summary.\n\nBody line of the docstring.")
        );
        // comments/blank lines before the docstring are skipped
        assert_eq!(
            module_docstring("#!/usr/bin/env python\n# note\n\n'''doc'''\n").as_deref(),
            Some("doc")
        );
        assert_eq!(
            module_docstring("\"one line\"\nx = 1\n").as_deref(),
            Some("one line")
        );
        // first statement not a string → no docstring
        assert_eq!(module_docstring(PROG), None);
        assert_eq!(module_docstring(""), None);
    }

    #[test]
    fn summary_is_first_nonempty_line() {
        assert_eq!(summary_of("\nOne-liner.\nrest"), "One-liner.");
        assert_eq!(summary_of(""), "");
    }

    #[test]
    fn span_def_scan() {
        assert!(has_public_span_def(TOOL));
        // indented (class method) spans count
        assert!(has_public_span_def(
            "class C:\n    @span(\"c.m\", kind=\"getter\")\n    def m(self):\n        pass\n"
        ));
        // an underscore def doesn't
        assert!(!has_public_span_def(
            "@span(\"x\")\ndef _hidden():\n    pass\n"
        ));
        // a statement between decorator and def resets the pending span
        assert!(!has_public_span_def(
            "@span(\"x\")\nY = 1\ndef go():\n    pass\n"
        ));
        // comments/other decorators between span and def are tolerated
        assert!(has_public_span_def(
            "@span(\"x\")\n# note\n@other\ndef go():\n    pass\n"
        ));
        assert!(!has_public_span_def(PROG));
    }

    #[test]
    fn validate_enforces_the_tool_contract() {
        // marked but no docstring
        let p = ProgramSource::new("t", "v1", "__any_tool__ = True\ndef f():\n    pass\n");
        assert!(p
            .validate()
            .unwrap_err()
            .to_string()
            .contains("no module docstring"));
        // marked but no spanned public def
        let p = ProgramSource::new(
            "t",
            "v1",
            "\"\"\"Doc.\"\"\"\n__any_tool__ = True\ndef f():\n    pass\n",
        );
        assert!(p.validate().unwrap_err().to_string().contains("@span"));
        // overlong one-liner
        let long = format!(
            "\"\"\"{}\"\"\"\n__any_tool__ = True\n@span(\"x\")\ndef f():\n    pass\n",
            "x".repeat(81)
        );
        let p = ProgramSource::new("t", "v1", &long);
        assert!(p.validate().unwrap_err().to_string().contains("caps at 80"));
        // overlong docstring body
        let fat = format!(
            "\"\"\"ok\n{}\"\"\"\n__any_tool__ = True\n@span(\"x\")\ndef f():\n    pass\n",
            "line\n".repeat(13)
        );
        let p = ProgramSource::new("t", "v1", &fat);
        assert!(p
            .validate()
            .unwrap_err()
            .to_string()
            .contains("standing prompt"));
        // an UNMARKED program is free-form: long dev docstrings are fine
        let dev = format!("\"\"\"dev doc\n{}\"\"\"\n", "line\n".repeat(40));
        assert!(ProgramSource::new("t", "v1", &dev).validate().is_ok());
        // the well-formed tool passes
        assert!(ProgramSource::new("t", "v1", TOOL).validate().is_ok());
    }

    #[test]
    fn validation_parity_corpus() {
        // Shared fixture corpus (ADR-013 §3 O2): the guest write path
        // (programs@v1) runs the SAME file in pytest — a scan change
        // here that isn't mirrored there (or vice versa) breaks one of
        // the two suites instead of drifting silently. `deploy` states
        // this suite's expected verdict; `guest` is pytest's.
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../tests/fixtures/program_validation.jsonl");
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("corpus at {}: {e}", path.display()));
        let mut n = 0;
        for line in text.lines().filter(|l| !l.trim().is_empty()) {
            let f: Value = serde_json::from_str(line).expect("corpus line is JSON");
            let name = f["name"].as_str().unwrap();
            let p = ProgramSource::new("t", "v1", f["code"].as_str().unwrap());
            let want_ok = f["deploy"]["ok"].as_bool().unwrap();
            match p.validate() {
                Ok(()) => assert!(want_ok, "{name}: deploy validate unexpectedly passed"),
                Err(e) => {
                    assert!(!want_ok, "{name}: deploy validate failed: {e}");
                    if let Some(sub) = f["deploy"]["err"].as_str() {
                        assert!(
                            e.to_string().contains(sub),
                            "{name}: error {e:?} lacks {sub:?}"
                        );
                    }
                }
            }
            n += 1;
        }
        assert!(n >= 10, "corpus suspiciously small ({n} fixtures)");
    }

    // --- fingerprint ---

    #[test]
    fn fingerprint_sensitivity() {
        let p = ProgramSource::new("t", "v1", TOOL);
        let code_change = ProgramSource::new("t", "v1", &format!("{TOOL}# x"));
        assert_ne!(p.fingerprint(), code_change.fingerprint());
        // stable across instances
        assert_eq!(
            p.fingerprint(),
            ProgramSource::new("t", "v1", TOOL).fingerprint()
        );
        // empty manifest keeps the no-manifest fingerprint
        assert_eq!(
            p.fingerprint(),
            ProgramSource::new("t", "v1", TOOL)
                .with_manifest(json!({}))
                .fingerprint()
        );
        // manifest presence and edits change the hash
        let with_man = ProgramSource::new("t", "v1", TOOL).with_manifest(manifest());
        let edited = ProgramSource::new("t", "v1", TOOL)
            .with_manifest(json!({"capabilities": ["net.http", "chat.send"], "publisher": "acme"}));
        assert_ne!(p.fingerprint(), with_man.fingerprint());
        assert_ne!(with_man.fingerprint(), edited.fingerprint());
    }

    #[test]
    fn python_canonical_json_unicode() {
        // json.dumps(ensure_ascii=True, sort_keys=True) parity: é, ✓,
        // control chars, and an astral surrogate pair
        let man = json!({"publisher": "acmé ✓", "capabilities": ["net.http"],
                         "note": "line\nbreak \"q\" \u{7f} 🎉"});
        let mut canon = String::new();
        python_canonical_json(&man, &mut canon);
        assert_eq!(
            canon,
            r#"{"capabilities":["net.http"],"note":"line\nbreak \"q\" \u007f \ud83c\udf89","publisher":"acm\u00e9 \u2713"}"#
        );
    }

    // --- load_programs ---

    #[test]
    fn load_programs_parses_name_version() {
        let dir = tempdir().unwrap();
        std::fs::write(dir.path().join("websearch@v1.py"), PROG).unwrap();
        std::fs::write(dir.path().join("notaprogram.py"), "x").unwrap(); // no @vN → skipped
        std::fs::create_dir(dir.path().join("tool@v2")).unwrap();
        std::fs::write(dir.path().join("tool@v2/program.py"), TOOL).unwrap();
        std::fs::write(dir.path().join("tool@v2/notes.md"), "ignored").unwrap();
        let progs = load_programs(dir.path()).unwrap();
        let specs: Vec<String> = progs.iter().map(|p| p.spec()).collect();
        assert_eq!(specs, ["tool@v2", "websearch@v1"]);
        assert_eq!(progs[0].code, TOOL);
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

    /// The space's resolved program schema — deploy ensured it.
    fn schema(c: &Client, space: &str) -> ProgramSchema {
        ProgramSchema::lookup(c, space)
            .unwrap()
            .expect("deploy ensures the schema")
    }

    #[test]
    fn deploy_ensures_the_program_schema_in_the_target_space() {
        let c = client();
        assert!(ProgramSchema::lookup(&c, "agent").unwrap().is_none());
        Deployer::new(&c, "agent")
            .deploy_one(&ProgramSource::new("t", "v1", PROG))
            .unwrap();
        let s = schema(&c, "agent");
        // the object carries the RESOLVED type id, never the xKey literal
        let rows = c.query_objects("agent", &json!({})).unwrap();
        assert_eq!(rows[0]["any"]["types"], json!([s.type_id]));
    }

    #[test]
    fn deploy_creates_then_unchanged() {
        let c = client();
        let d = Deployer::new(&c, "agent");
        let p = ProgramSource::new("tool", "v1", TOOL);

        assert_eq!(d.deploy_one(&p).unwrap(), "created");
        // redeploy identical → hash-gated skip
        assert_eq!(d.deploy_one(&p).unwrap(), "unchanged");
        // the written source round-trips; derived props are cached
        let src = c
            .query("agent", "obj1", "program_source", &json!({}))
            .unwrap();
        assert_eq!(src[0]["code"], json!(TOOL));
        let s = schema(&c, "agent");
        let tools = c
            .query_objects("agent", &json!({"filter": {s.path("any_tool"): true}}))
            .unwrap();
        assert_eq!(tools.len(), 1);
        assert_eq!(s.read(&tools[0])["summary"], json!("One-liner summary."));
    }

    #[test]
    fn deploy_updates_on_code_change() {
        let c = client();
        let d = Deployer::new(&c, "agent");
        d.deploy_one(&ProgramSource::new("t", "v1", TOOL)).unwrap();
        let changed = ProgramSource::new("t", "v1", &format!("{TOOL}# changed"));
        assert_eq!(d.deploy_one(&changed).unwrap(), "updated");
        let src = c
            .query("agent", "obj1", "program_source", &json!({}))
            .unwrap();
        assert!(src[0]["code"].as_str().unwrap().contains("# changed"));
    }

    #[test]
    fn deploy_unmarked_is_not_a_tool() {
        let c = client();
        let d = Deployer::new(&c, "agent");
        d.deploy_one(&ProgramSource::new("lib", "v1", PROG))
            .unwrap();
        let s = schema(&c, "agent");
        let progs = c
            .query_objects("agent", &json!({"filter": {s.path("any_tool"): true}}))
            .unwrap();
        assert!(progs.is_empty());
    }

    #[test]
    fn deploy_rejects_invalid_tool() {
        let c = client();
        let d = Deployer::new(&c, "agent");
        let bad = ProgramSource::new("t", "v1", "__any_tool__ = True\n");
        let err = d.deploy_one(&bad).unwrap_err();
        assert!(err.to_string().contains("no module docstring"));
        // nothing was written (the schema ensure itself is not a program write)
        assert!(c.query_objects("agent", &json!({})).unwrap().is_empty());
    }

    #[test]
    fn frozen_space_rejects_changes_but_allows_unchanged() {
        let c = client();
        let p = ProgramSource::new("t", "v1", TOOL);
        Deployer::new(&c, "agent").deploy_one(&p).unwrap();
        let frozen = Deployer::new(&c, "agent").frozen(true);
        assert_eq!(frozen.deploy_one(&p).unwrap(), "unchanged");
        let changed = ProgramSource::new("t", "v1", "\"\"\"new\"\"\"\n");
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
        let p = ProgramSource::new("t", "v1", TOOL).with_manifest(manifest());
        assert_eq!(d.deploy_one(&p).unwrap(), "created");
        let man = c
            .query("agent", "obj1", "program_manifest", &json!({}))
            .unwrap();
        assert_eq!(man[0]["manifest"], manifest());
        assert_eq!(d.deploy_one(&p).unwrap(), "unchanged"); // manifest round-trips
                                                            // manifest edit → new hash → update; dropped manifest clears the record
        let edited = ProgramSource::new("t", "v1", TOOL)
            .with_manifest(json!({"capabilities": ["net.http", "chat.send"]}));
        assert_eq!(d.deploy_one(&edited).unwrap(), "updated");
        assert_eq!(
            d.deploy_one(&ProgramSource::new("t", "v1", TOOL)).unwrap(),
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

    // --- repo deploy (ADR-009 §2) ---

    #[test]
    fn repo_deploy_publishes_all_kinds() {
        let dir = tempdir().unwrap();
        std::fs::create_dir(dir.path().join("programs")).unwrap();
        std::fs::create_dir(dir.path().join("skills")).unwrap();
        std::fs::write(dir.path().join("programs/tool@v1.py"), PROG).unwrap();
        std::fs::write(dir.path().join("skills/_core.md"), "core").unwrap();
        std::fs::write(dir.path().join("README.md"), "# my repo\n").unwrap();

        let c = client();
        let out = deploy_repo(&c, "agent", dir.path()).unwrap();
        assert_eq!(
            out.programs.get("tool@v1").map(String::as_str),
            Some("created")
        );
        assert_eq!(out.skills.get("_core").map(String::as_str), Some("created"));
        assert_eq!(out.readme, Some("updated"));

        // README round-trips as the overlay description object
        let objs = c
            .query_objects("agent", &json!({"filter": {"any.name": "README"}}))
            .unwrap();
        let oid = objs[0]["id"].as_str().unwrap();
        assert_eq!(c.get_markdown("agent", oid).unwrap(), "# my repo\n");

        // rerun: everything hash-gated
        let again = deploy_repo(&c, "agent", dir.path()).unwrap();
        assert_eq!(
            again.programs.get("tool@v1").map(String::as_str),
            Some("unchanged")
        );
        assert_eq!(
            again.skills.get("_core").map(String::as_str),
            Some("unchanged")
        );
        assert_eq!(again.readme, Some("unchanged"));
    }

    #[test]
    fn repo_deploy_tolerates_missing_kinds() {
        let dir = tempdir().unwrap(); // empty folder: no kinds at all
        let c = client();
        let out = deploy_repo(&c, "agent", dir.path()).unwrap();
        assert!(out.programs.is_empty());
        assert!(out.skills.is_empty());
        assert_eq!(out.readme, None);
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
