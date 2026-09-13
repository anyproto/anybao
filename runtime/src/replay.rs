//! Trace-side replay machinery (ADR-001 §5) — the Rust twin of the
//! reference host's replay half of anyrt/trace.py: trace loading with
//! header/schema validation, the `.blobs` sidecar, blob-ref resolution,
//! the strict `ReplayCursor` and the loose FIFO `MockIndex`.
//!
//! Mounted as `crate::replay` (
//! broker) so the record-mode binary's module tree stays untouched;
//! the serve/replay wiring in main.rs lifts it to a top-level module
//! when it lands.
#![allow(dead_code)] // consumed by the main.rs replay wiring (next round); unit tests below

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

/// Loose mock mode: (effect, key) → FIFO output queue (queue order =
/// log order — v1 pop semantics on sturdier keys). Unmatched policy is
/// the caller's ("fail" under tests, "live" interactively).
pub struct MockIndex {
    queues: HashMap<(String, String), VecDeque<Value>>,
}

impl MockIndex {
    pub fn new(records: &[Value]) -> Self {
        let mut queues: HashMap<(String, String), VecDeque<Value>> = HashMap::new();
        for r in records {
            if r["kind"] == "effect" {
                let effect = r["effect"].as_str().unwrap_or("").to_string();
                let key = r["key"].as_str().unwrap_or("").to_string();
                queues
                    .entry((effect, key))
                    .or_default()
                    .push_back(r.clone());
            }
        }
        MockIndex { queues }
    }

    pub fn pop(&mut self, effect: &str, key: &str) -> Option<Value> {
        self.queues
            .get_mut(&(effect.to_string(), key.to_string()))
            .and_then(|q| q.pop_front())
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
