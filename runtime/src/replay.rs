//! Trace-side replay machinery (ADR-001 §5) — the Rust twin of the
//! reference host's replay half of anyrt/trace.py: trace loading with
//! header/schema validation, the `.blobs` sidecar, blob-ref resolution,
//! the strict `ReplayCursor` and the loose FIFO `MockIndex`.
//!
//! Mounted as `crate::replay` (
//! broker) so the record-mode binary's module tree stays untouched;
//! the serve/replay wiring in main.rs lifts it to a top-level module
//! when it lands.

use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap, VecDeque};
use std::fmt;

/// Strict replay only: the next call does not match the next record.
/// The hard error IS the feature (determinism made testable); loose
/// mock mode never raises this — ADR-001 §5.
#[derive(Debug)]
pub struct DivergenceError {
    pub expected: Option<Value>,
    pub actual: Value,
}

fn short_key(rec: &Value) -> String {
    let key = rec.get("key").and_then(|k| k.as_str()).unwrap_or("");
    key.chars().take(24).collect()
}

impl fmt::Display for DivergenceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let exp = match &self.expected {
            Some(r) if r["kind"] == "effect" => format!(
                "{} key={}…",
                r["effect"].as_str().unwrap_or("?"),
                short_key(r)
            ),
            Some(r) => format!("{:?}", r.get("kind").and_then(|k| k.as_str())),
            None => "None".into(),
        };
        let act_name = self
            .actual
            .get("effect")
            .or_else(|| self.actual.get("kind"))
            .and_then(|v| v.as_str())
            .unwrap_or("?");
        write!(
            f,
            "replay divergence: expected {exp}, got {act_name} key={}…",
            short_key(&self.actual)
        )
    }
}

impl std::error::Error for DivergenceError {}

/// Resolve a blob ref (`{"__blob": hash, "bytes": n}`, ADR-001 §7) back
/// to its value. Non-refs — and refs whose blob is missing from the
/// sidecar — pass through unchanged (the ref shape stays detectable).
/// A raw ref (ADR-026 §2, `mime` present) stays a ref: the bytes are
/// a handle, not a value — except the oversize text spill
/// (`application/json`), which re-hydrates like any text spill.
pub fn resolve_blobs(value: Value, blobs: &BTreeMap<String, String>) -> Value {
    let is_ref = value
        .as_object()
        .map(|m| {
            m.contains_key("__blob")
                && m.contains_key("bytes")
                && match m.len() {
                    2 => true,
                    3 => m.get("mime") == Some(&Value::String(crate::blob::JSON_MIME.into())),
                    _ => false,
                }
        })
        .unwrap_or(false);
    if is_ref {
        if let Some(text) = value["__blob"].as_str().and_then(|h| blobs.get(h)) {
            if let Ok(parsed) = serde_json::from_str(text) {
                return parsed;
            }
        }
    }
    value
}

/// Strict replay: every effect call must match the next unconsumed
/// effect record; cell and span records are checkpoints (matched on
/// identity + ok, not metrics/output — those legitimately vary).
/// Only the header (and future non-effect/cell/span kinds) is skipped,
/// at construction; the walk itself is strictly positional.
pub struct ReplayCursor {
    records: Vec<Value>,
    pos: usize,
}

impl ReplayCursor {
    pub fn new(records: &[Value]) -> Self {
        let records = records
            .iter()
            .filter(|r| matches!(r["kind"].as_str(), Some("effect" | "cell" | "span")))
            .cloned()
            .collect();
        ReplayCursor { records, pos: 0 }
    }

    fn peek(&self) -> Option<&Value> {
        self.records.get(self.pos)
    }

    fn take(&mut self) -> Value {
        let rec = self.records[self.pos].clone();
        self.pos += 1;
        rec
    }

    /// A host-emitted record (`meta.hosted`, ADR-011 §6) at the cursor
    /// head that is NOT the record the guest is asking for: consumed
    /// here, written through by the broker's drain before the guest
    /// record is matched. Anything else stays put for the strict match.
    pub fn take_hosted_mismatch(&mut self, effect: &str, key: &str) -> Option<Value> {
        let head = self.peek()?;
        let hosted = head["kind"] == "effect"
            && head
                .get("meta")
                .and_then(|m| m.get("hosted"))
                .and_then(|h| h.as_bool())
                == Some(true);
        let matches = head["effect"] == effect && head["key"] == key;
        (hosted && !matches).then(|| self.take())
    }

    pub fn expect_effect(&mut self, effect: &str, key: &str) -> Result<Value, DivergenceError> {
        let matched = self
            .peek()
            .map(|r| r["kind"] == "effect" && r["effect"] == effect && r["key"] == key)
            .unwrap_or(false);
        if !matched {
            return Err(DivergenceError {
                expected: self.peek().cloned(),
                actual: json!({"kind": "effect", "effect": effect, "key": key}),
            });
        }
        Ok(self.take())
    }

    pub fn expect_cell(&mut self, cell: &str, ok: bool) -> Result<Value, DivergenceError> {
        let matched = self
            .peek()
            .map(|r| r["kind"] == "cell" && r["cell"] == cell && r["ok"] == ok)
            .unwrap_or(false);
        if !matched {
            return Err(DivergenceError {
                expected: self.peek().cloned(),
                actual: json!({"kind": "cell", "cell": cell, "ok": ok}),
            });
        }
        Ok(self.take())
    }

    /// Span checkpoints (ADR-001 §4c): begin matched on (name, key).
    pub fn expect_span_begin(&mut self, name: &str, key: &str) -> Result<Value, DivergenceError> {
        let matched = self
            .peek()
            .map(|r| {
                r["kind"] == "span" && r["phase"] == "begin" && r["name"] == name && r["key"] == key
            })
            .unwrap_or(false);
        if !matched {
            return Err(DivergenceError {
                expected: self.peek().cloned(),
                actual: json!({"kind": "span", "phase": "begin", "name": name, "key": key}),
            });
        }
        Ok(self.take())
    }

    /// End matched on (name, ok); output/meta legitimately unmatched.
    pub fn expect_span_end(&mut self, name: &str, ok: bool) -> Result<Value, DivergenceError> {
        let matched = self
            .peek()
            .map(|r| {
                r["kind"] == "span" && r["phase"] == "end" && r["name"] == name && r["ok"] == ok
            })
            .unwrap_or(false);
        if !matched {
            return Err(DivergenceError {
                expected: self.peek().cloned(),
                actual: json!({"kind": "span", "phase": "end", "name": name, "ok": ok}),
            });
        }
        Ok(self.take())
    }

    pub fn exhausted(&self) -> bool {
        self.pos >= self.records.len()
    }
}

/// The mock spec (ADR-028 §1) — sources (`from` runs, inline
/// `records`), the mockable set (`only`/`except` effect-name globs)
/// and the miss policy. One shape on `anyrt run --mock` and on the
/// toolcaller's cell span (`span.begin` `input.mock`, ADR-028 §3).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MockSpec {
    pub from: Vec<String>,
    pub only: Vec<String>,
    pub except: Vec<String>,
    pub records: Vec<Value>,
    pub unmatched: Unmatched,
}

/// A miss inside the mockable set: `fail` raises a typed
/// `mock_unmatched` into the guest (recorded as an error record);
/// `live` executes and records `meta.mock.unmatched` — the traceDiff
/// predicate (ADR-028 §6).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Unmatched {
    Fail,
    Live,
}

impl Default for MockSpec {
    /// Everything mockable, no sources, miss = fail.
    fn default() -> Self {
        MockSpec {
            from: vec![],
            only: vec![],
            except: vec![],
            records: vec![],
            unmatched: Unmatched::Fail,
        }
    }
}

impl MockSpec {
    /// A bare run id is sugar for `{"from": id}`; `from` may be one id
    /// or a list. Shape errors are the caller's spec error — raised
    /// before anything runs (ADR-028 §5).
    pub fn parse(v: &Value) -> Result<MockSpec, String> {
        let v = match v {
            Value::String(run) => json!({"from": run}),
            Value::Object(_) => v.clone(),
            other => return Err(format!("mock: expected an object or a run id, got {other}")),
        };
        const KNOWN: [&str; 5] = ["from", "only", "except", "records", "unmatched"];
        for k in v.as_object().unwrap().keys() {
            if !KNOWN.contains(&k.as_str()) {
                return Err(format!("mock: unknown key {k:?}"));
            }
        }
        let strings = |key: &str| -> Result<Vec<String>, String> {
            match v.get(key) {
                None | Some(Value::Null) => Ok(vec![]),
                Some(Value::String(s)) => Ok(vec![s.clone()]),
                Some(Value::Array(a)) => a
                    .iter()
                    .map(|x| {
                        x.as_str()
                            .map(str::to_string)
                            .ok_or_else(|| format!("mock.{key}: expected strings, got {x}"))
                    })
                    .collect(),
                Some(other) => Err(format!(
                    "mock.{key}: expected a string or list, got {other}"
                )),
            }
        };
        let from = strings("from")?;
        for r in &from {
            if !crate::tracestore::valid_run_id(r) {
                return Err(format!("mock.from: not a run id: {r:?}"));
            }
        }
        let records = match v.get("records") {
            None | Some(Value::Null) => vec![],
            Some(Value::Array(a)) => a.clone(),
            Some(other) => return Err(format!("mock.records: expected a list, got {other}")),
        };
        for (i, r) in records.iter().enumerate() {
            let effect = r.get("effect").and_then(|e| e.as_str()).unwrap_or("");
            if effect.is_empty() {
                return Err(format!("mock.records[{i}]: missing effect"));
            }
            let has_out = r.get("output").is_some_and(|o| !o.is_null());
            let has_err = r.get("error").is_some_and(|e| e.is_object());
            if has_out == has_err {
                return Err(format!(
                    "mock.records[{i}] ({effect}): exactly one of output / error"
                ));
            }
        }
        let unmatched = match v.get("unmatched") {
            None | Some(Value::Null) => Unmatched::Fail,
            Some(Value::String(s)) if s == "fail" => Unmatched::Fail,
            Some(Value::String(s)) if s == "live" => Unmatched::Live,
            Some(other) => return Err(format!("mock.unmatched: expected fail|live, got {other}")),
        };
        Ok(MockSpec {
            from,
            only: strings("only")?,
            except: strings("except")?,
            records,
            unmatched,
        })
    }

    /// Everything mockable when `only` is empty; `except` subtracts;
    /// `span.*` / `trace.*` never are (ADR-028 §7).
    pub fn mockable(&self, effect: &str) -> bool {
        if never_mockable(effect) {
            return false;
        }
        let in_only = self.only.is_empty() || self.only.iter().any(|g| glob_match(g, effect));
        in_only && !self.except.iter().any(|g| glob_match(g, effect))
    }

    /// The spec as recorded (run header / cell span input) — the
    /// parsed shape, so a recorded spec re-parses to itself.
    pub fn to_value(&self) -> Value {
        let mut m = serde_json::Map::new();
        if !self.from.is_empty() {
            m.insert("from".into(), json!(self.from));
        }
        if !self.only.is_empty() {
            m.insert("only".into(), json!(self.only));
        }
        if !self.except.is_empty() {
            m.insert("except".into(), json!(self.except));
        }
        if !self.records.is_empty() {
            m.insert("records".into(), json!(self.records));
        }
        m.insert(
            "unmatched".into(),
            json!(match self.unmatched {
                Unmatched::Fail => "fail",
                Unmatched::Live => "live",
            }),
        );
        Value::Object(m)
    }
}

/// Never served from a mock (ADR-028 §7): the trace's own machinery
/// (a mocked trace view would lie about the run it is in) and the
/// kernel's plumbing — module resolution, boot pins, runtime wiring,
/// the mailbox, the fuel gauge — which nobody rehearses and which an
/// `unmatched: fail` spec would otherwise kill a cell on (`use()`
/// failing typed mock_unmatched before the rehearsed call is reached).
pub fn never_mockable(effect: &str) -> bool {
    effect.starts_with("span.")
        || effect.starts_with("trace.")
        || matches!(
            effect,
            "module.resolve" | "kernel.boot" | "runtime.get" | "mailbox.drain" | "fuel.state"
        )
}

/// `*` matches any run of characters (including none); everything else
/// is literal: `http.*`, `any.*`, `*`, `sh.run`.
pub fn glob_match(pattern: &str, s: &str) -> bool {
    let (p, t): (Vec<char>, Vec<char>) = (pattern.chars().collect(), s.chars().collect());
    let (mut pi, mut ti) = (0, 0);
    let (mut star, mut mark): (Option<usize>, usize) = (None, 0);
    while ti < t.len() {
        if pi < p.len() && p[pi] == '*' {
            star = Some(pi);
            mark = ti;
            pi += 1;
        } else if pi < p.len() && p[pi] == t[ti] {
            pi += 1;
            ti += 1;
        } else if let Some(sp) = star {
            pi = sp + 1;
            mark += 1;
            ti = mark;
        } else {
            return false;
        }
    }
    while pi < p.len() && p[pi] == '*' {
        pi += 1;
    }
    pi == p.len()
}

/// One queued answer: the record shape the broker consumes (`output` /
/// `error`), where it came from (`meta.mock`, ADR-028 §8), and whether
/// it is consumed on use.
#[derive(Debug, Clone)]
pub struct MockEntry {
    pub rec: Value,
    pub provenance: Value,
    pub repeat: bool,
}

pub const WILDCARD_KEY: &str = "*";

/// Loose mock: `effect → key → FIFO output queue` folded from the log
/// in one pass (ADR-001 §5; queue order = log order — v1 pop semantics
/// on sturdier keys). Inline records sit at the FRONT of their key's
/// queue (they override a `from` record for the same call); a record
/// without `input` takes the wildcard key `(effect, "*")`, consulted
/// after the exact key misses (ADR-028 §1). The mockable set and the
/// miss policy ride along in `spec`.
pub struct MockIndex {
    queues: HashMap<(String, String), VecDeque<MockEntry>>,
    pub spec: MockSpec,
}

impl MockIndex {
    /// The whole log of one run, everything mockable, miss = fail —
    /// the unit-test shape. Provenance names the header's run id.
    pub fn new(records: &[Value]) -> Self {
        let run = records
            .first()
            .and_then(|h| h.get("run"))
            .and_then(|r| r.get("id"))
            .and_then(|i| i.as_str())
            .unwrap_or("")
            .to_string();
        let mut idx = MockIndex {
            queues: HashMap::new(),
            spec: MockSpec::default(),
        };
        idx.fold_run(&run, records);
        idx
    }

    /// Build from a spec: inline records first, then each `from` run
    /// in list order through `load` (one run's blob-resolved records;
    /// an unknown run or a missing blob is the caller's spec error).
    pub fn build(
        spec: MockSpec,
        mut load: impl FnMut(&str) -> Result<Vec<Value>, String>,
    ) -> Result<Self, String> {
        let mut idx = MockIndex {
            queues: HashMap::new(),
            spec: spec.clone(),
        };
        for (i, r) in spec.records.iter().enumerate() {
            let effect = r["effect"].as_str().unwrap_or("").to_string();
            let key = match r.get("input") {
                Some(input) if !input.is_null() => crate::trace::input_key(&effect, input),
                _ => WILDCARD_KEY.to_string(),
            };
            let mut rec = json!({});
            if let Some(o) = r.get("output").filter(|o| !o.is_null()) {
                rec["output"] = o.clone();
            }
            if let Some(e) = r.get("error").filter(|e| e.is_object()) {
                rec["error"] = e.clone();
            }
            idx.queues
                .entry((effect, key))
                .or_default()
                .push_back(MockEntry {
                    rec,
                    provenance: json!({"inline": i}),
                    repeat: r.get("repeat").and_then(|b| b.as_bool()).unwrap_or(false),
                });
        }
        for run in &spec.from {
            let records = load(run)?;
            idx.fold_run(run, &records);
        }
        Ok(idx)
    }

    fn fold_run(&mut self, run: &str, records: &[Value]) {
        for r in records {
            if r["kind"] == "effect" {
                let effect = r["effect"].as_str().unwrap_or("").to_string();
                let key = r["key"].as_str().unwrap_or("").to_string();
                let seq = r.get("seq").cloned().unwrap_or(Value::Null);
                self.queues
                    .entry((effect, key))
                    .or_default()
                    .push_back(MockEntry {
                        rec: r.clone(),
                        provenance: json!({"from": run, "seq": seq}),
                        repeat: false,
                    });
            }
        }
    }

    /// Exact key first, then the wildcard; a `repeat` entry is peeked.
    pub fn take(&mut self, effect: &str, key: &str) -> Option<MockEntry> {
        for k in [key, WILDCARD_KEY] {
            if let Some(q) = self.queues.get_mut(&(effect.to_string(), k.to_string())) {
                match q.front() {
                    Some(e) if e.repeat => return q.front().cloned(),
                    Some(_) => return q.pop_front(),
                    None => {}
                }
            }
        }
        None
    }

    /// The unit-test shape: the consumed record alone.
    pub fn pop(&mut self, effect: &str, key: &str) -> Option<Value> {
        self.take(effect, key).map(|e| e.rec)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::trace::{input_key, TraceWriter};

    fn make_trace() -> TraceWriter {
        let mut w = TraceWriter::new(json!({"id": "t1", "program": "test@v1"}));
        let k1 = input_key("http.get", &json!({"url": "https://a"}));
        w.effect(
            "http.get",
            Some("c1"),
            json!({"url": "https://a"}),
            &k1,
            Some(json!({"status": 200})),
            None,
            json!({"class": "read"}),
            None,
        );
        w.cell("c1", true, None, false, json!({"fuel_used": 123}));
        w
    }

    fn make_span_trace() -> TraceWriter {
        let mut w = TraceWriter::new(json!({"id": "sp1"}));
        let kb = input_key("linear.createTask", &json!({"kwargs": {"title": "t"}}));
        w.span_begin(
            "s1",
            "linear.createTask",
            Some("c1"),
            json!({"kwargs": {"title": "t"}}),
            &kb,
            None,
        );
        let ke = input_key("any.modify", &json!({"n": 1}));
        w.effect(
            "any.modify",
            Some("c1"),
            json!({"n": 1}),
            &ke,
            Some(json!({"ok": 1})),
            None,
            json!({"class": "mutate"}),
            Some("s1"),
        );
        w.span_end(
            "s1",
            "linear.createTask",
            Some("c1"),
            true,
            Some(json!({"id": "T-1"})),
            None,
            json!({"effects": 1, "mutations": 1}),
        );
        w.effect(
            "time.now",
            Some("c1"),
            json!({}),
            &input_key("time.now", &json!({})),
            Some(json!({"epoch": 1.0})),
            None,
            json!({"class": "read"}),
            None,
        );
        w.cell("c1", true, None, false, json!({}));
        w
    }

    #[test]
    fn cursor_happy_path_and_exhaustion() {
        let w = make_trace();
        let mut cur = ReplayCursor::new(&w.records);
        let k1 = input_key("http.get", &json!({"url": "https://a"}));
        let rec = cur.expect_effect("http.get", &k1).unwrap();
        assert_eq!(rec["output"], json!({"status": 200}));
        cur.expect_cell("c1", true).unwrap();
        assert!(cur.exhausted());
    }

    #[test]
    fn cursor_diverges_on_mutated_input() {
        let w = make_trace();
        let mut cur = ReplayCursor::new(&w.records);
        let other = input_key("http.get", &json!({"url": "https://OTHER"}));
        let err = cur.expect_effect("http.get", &other).unwrap_err();
        assert!(err.to_string().starts_with("replay divergence"));
    }

    #[test]
    fn cursor_detects_reordered() {
        let w = make_trace();
        let mut cur = ReplayCursor::new(&w.records);
        // reordered: cell checkpoint before the effect
        assert!(cur.expect_cell("c1", true).is_err());
    }

    #[test]
    fn cursor_span_checkpoints_and_divergence() {
        let w = make_span_trace();
        let mut cur = ReplayCursor::new(&w.records);
        cur.expect_span_begin(
            "linear.createTask",
            &input_key("linear.createTask", &json!({"kwargs": {"title": "t"}})),
        )
        .unwrap();
        cur.expect_effect("any.modify", &input_key("any.modify", &json!({"n": 1})))
            .unwrap();
        cur.expect_span_end("linear.createTask", true).unwrap();
        cur.expect_effect("time.now", &input_key("time.now", &json!({})))
            .unwrap();
        cur.expect_cell("c1", true).unwrap();
        assert!(cur.exhausted());

        // changed facade input diverges at the begin checkpoint
        let mut cur2 = ReplayCursor::new(&w.records);
        assert!(cur2
            .expect_span_begin(
                "linear.createTask",
                &input_key("linear.createTask", &json!({"kwargs": {"title": "OTHER"}})),
            )
            .is_err());

        // skipped begin checkpoint
        let mut cur3 = ReplayCursor::new(&w.records);
        assert!(cur3
            .expect_effect("any.modify", &input_key("any.modify", &json!({"n": 1})))
            .is_err());
    }

    #[test]
    fn mock_index_fifo_and_exhaustion() {
        let mut w = TraceWriter::new(json!({"id": "t2"}));
        let k = input_key("e", &json!({"n": 1}));
        w.effect(
            "e",
            None,
            json!({"n": 1}),
            &k,
            Some(json!("first")),
            None,
            json!({}),
            None,
        );
        w.effect(
            "e",
            None,
            json!({"n": 1}),
            &k,
            Some(json!("second")),
            None,
            json!({}),
            None,
        );
        let mut idx = MockIndex::new(&w.records);
        assert_eq!(idx.pop("e", &k).unwrap()["output"], json!("first"));
        assert_eq!(idx.pop("e", &k).unwrap()["output"], json!("second"));
        assert!(idx.pop("e", &k).is_none());
    }

    #[test]
    fn mock_index_ignores_span_records() {
        let w = make_span_trace();
        let mut idx = MockIndex::new(&w.records);
        assert!(idx
            .pop("any.modify", &input_key("any.modify", &json!({"n": 1})))
            .is_some());
        assert!(idx
            .pop(
                "linear.createTask",
                &input_key("linear.createTask", &json!({"kwargs": {"title": "t"}})),
            )
            .is_none());
    }

    #[test]
    fn blob_spill_resolves() {
        let mut w = TraceWriter::new(json!({"id": "b1"}));
        let big = json!({"body": "x".repeat(100_000)});
        let k = input_key("http.get", &json!({"url": "https://a"}));
        w.effect(
            "http.get",
            None,
            json!({"url": "https://a"}),
            &k,
            Some(big.clone()),
            None,
            json!({}),
            None,
        );
        let rec = &w.records[1];
        let out = rec["output"].as_object().unwrap();
        assert_eq!(out.len(), 2);
        assert!(out.contains_key("__blob") && out.contains_key("bytes")); // spilled
        assert_eq!(rec["input"], json!({"url": "https://a"})); // small input inline

        let blobs: BTreeMap<String, String> = w.blobs.iter().cloned().collect();
        assert_eq!(resolve_blobs(rec["output"].clone(), &blobs), big);
        // non-refs (and missing blobs) pass through unchanged
        assert_eq!(resolve_blobs(json!({"a": 1}), &blobs), json!({"a": 1}));
    }
}

#[cfg(test)]
mod spec_tests {
    use super::*;
    use crate::trace::input_key;

    #[test]
    fn glob_matches_prefix_star_and_literal() {
        assert!(glob_match("http.*", "http.get"));
        assert!(glob_match("*", "anything"));
        assert!(glob_match("sh.run", "sh.run"));
        assert!(!glob_match("sh.run", "sh.runx"));
        assert!(!glob_match("http.*", "any.query"));
        assert!(glob_match("*.get", "config.get"));
    }

    #[test]
    fn spec_parses_sugar_and_rejects_bad_shapes() {
        let s = MockSpec::parse(&json!("run_0123456789abcdef")).unwrap();
        assert_eq!(s.from, vec!["run_0123456789abcdef"]);
        assert_eq!(s.unmatched, Unmatched::Fail);
        assert!(MockSpec::parse(&json!({"frmo": "x"}))
            .unwrap_err()
            .contains("unknown key"));
        assert!(MockSpec::parse(&json!({"from": "nope"}))
            .unwrap_err()
            .contains("not a run id"));
        assert!(
            MockSpec::parse(&json!({"records": [{"effect": "http.get"}]}))
                .unwrap_err()
                .contains("exactly one of output / error")
        );
        assert!(MockSpec::parse(&json!({"unmatched": "maybe"})).is_err());
        let s = MockSpec::parse(&json!({"only": ["http.*"], "except": "http.post",
                                        "unmatched": "live"}))
        .unwrap();
        assert_eq!(s.unmatched, Unmatched::Live);
        assert!(s.mockable("http.get"));
        assert!(!s.mockable("http.post"));
        assert!(!s.mockable("any.query"));
        assert!(!s.mockable("trace.effects_of"));
        assert!(!s.mockable("span.begin"));
        assert!(!s.mockable("module.resolve"));
        assert!(!s.mockable("kernel.boot"));
        assert!(MockSpec::default().mockable("time.now"));
        assert!(MockSpec::default().mockable("config.get"));
        // recorded shape re-parses to itself
        assert_eq!(MockSpec::parse(&s.to_value()).unwrap(), s);
    }

    #[test]
    fn build_puts_inline_first_then_wildcard_and_repeat() {
        let k = input_key("http.get", &json!({"url": "https://a"}));
        let run = vec![
            json!({"kind": "run", "run": {"id": "run_aaaaaaaaaaaaaaaa"}}),
            json!({"kind": "effect", "seq": 1, "effect": "http.get", "key": k,
                   "output": {"status": 200, "body": "recorded"}}),
        ];
        let spec = MockSpec::parse(&json!({
        "from": "run_aaaaaaaaaaaaaaaa",
        "records": [
            {"effect": "http.get", "input": {"url": "https://a"},
             "output": {"status": 200, "body": "inline"}},
            {"effect": "http.post", "output": {"status": 201}, "repeat": true},
            {"effect": "http.put", "error": {"type": "http", "message": "401"}},
        ]}))
        .unwrap();
        let mut idx = MockIndex::build(spec, |_| Ok(run.clone())).unwrap();
        let e = idx.take("http.get", &k).unwrap();
        assert_eq!(e.rec["output"]["body"], "inline");
        assert_eq!(e.provenance, json!({"inline": 0}));
        let e = idx.take("http.get", &k).unwrap();
        assert_eq!(e.rec["output"]["body"], "recorded");
        assert_eq!(
            e.provenance,
            json!({"from": "run_aaaaaaaaaaaaaaaa", "seq": 1})
        );
        assert!(idx.take("http.get", &k).is_none());
        // wildcard + repeat: any input, never consumed
        for _ in 0..3 {
            let e = idx.take("http.post", "sha256:whatever").unwrap();
            assert_eq!(e.rec["output"]["status"], 201);
        }
        let e = idx.take("http.put", "sha256:x").unwrap();
        assert_eq!(e.rec["error"]["type"], "http");
        assert!(idx.take("http.put", "sha256:x").is_none());
        // an unknown run is the caller's error
        let spec = MockSpec::parse(&json!({"from": "run_bbbbbbbbbbbbbbbb"})).unwrap();
        assert!(MockIndex::build(spec, |r| Err(format!("unknown run {r}"))).is_err());
    }
}
