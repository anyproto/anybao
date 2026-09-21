//! `anyrt trace stats` — the metrics checkpoint aggregate (ADR-003/005
//! "decisions from data"): scan a traces directory, report fuel /
//! duration / token / effect distributions and p95-derived tuning
//! suggestions. Lean by design — the trace is the database.

use crate::tracestore::TraceStore;
use serde_json::Value;

fn percentile(sorted: &[i64], p: f64) -> i64 {
    if sorted.is_empty() {
        return 0;
    }
    let rank = ((p / 100.0) * sorted.len() as f64).ceil() as usize;
    sorted[rank.clamp(1, sorted.len()) - 1]
}

fn dist(label: &str, values: &mut [i64]) -> String {
    values.sort();
    format!(
        "  {label:<18} n={:<4} min={} p50={} p95={} max={}",
        values.len(),
        values.first().copied().unwrap_or(0),
        percentile(values, 50.0),
        percentile(values, 95.0),
        values.last().copied().unwrap_or(0),
    )
}

pub fn render(store: &dyn TraceStore) -> anyhow::Result<String> {
    let mut runs = 0usize;
    let mut fuel: Vec<i64> = Vec::new();
    let mut duration: Vec<i64> = Vec::new();
    let mut tokens_in: Vec<i64> = Vec::new();
    let mut effects: std::collections::BTreeMap<String, u64> = Default::default();
    let mut mutations = 0u64;

    let mut ids: Vec<String> = store.list()?.into_iter().map(|m| m.id).collect();
    ids.sort();
    for id in &ids {
        let Ok(records) = store.load(id) else {
            continue;
        };
        runs += 1;
        for r in &records {
            match r["kind"].as_str() {
                Some("cell") => {
                    if let Some(f) = r["metrics"]["fuel_used"].as_i64() {
                        fuel.push(f);
                    }
                    if let Some(d) = r["metrics"]["duration_ms"].as_i64() {
                        duration.push(d);
                    }
                }
                Some("effect") => {
                    let name = r["effect"].as_str().unwrap_or("?").to_string();
                    *effects.entry(name).or_default() += 1;
                    if r["meta"]["class"] == "mutate" {
                        mutations += 1;
                    }
                }
                // tokens per llm call: the neutral Reply on the llm.chat
                // span end (ADR-005 §1) — the recorded http body is SSE
                // text on a streamed turn (BOB-149), so it is no source.
                // The prompt in use = in + cacheRead + cacheWrite.
                Some("span") if r["phase"] == "end" && r["name"] == "llm.chat" => {
                    let u = &r["output"]["usage"];
                    if let Some(t) = u["in"].as_i64() {
                        tokens_in.push(
                            t + u["cacheRead"].as_i64().unwrap_or(0)
                                + u["cacheWrite"].as_i64().unwrap_or(0),
                        );
                    }
                }
                _ => {}
            }
        }
    }

    let mut out = format!("{runs} runs\n");
    out.push_str(&dist("fuel/run", &mut fuel));
    out.push('\n');
    out.push_str(&dist("duration_ms/run", &mut duration));
    out.push('\n');
    out.push_str(&dist("tokens_in/call", &mut tokens_in));
    out.push('\n');
    out.push_str(&format!(
        "  effects: {} total, {} mutate\n",
        effects.values().sum::<u64>(),
        mutations
    ));
    for (name, n) in &effects {
        out.push_str(&format!("    {name} ×{n}\n"));
    }
    if !fuel.is_empty() {
        out.push_str(&format!(
            "suggest: fuel ceiling ≈ {} (2×p95); prompt budget check at p95 tokens_in={}\n",
            2 * percentile(&fuel, 95.0),
            percentile(&tokens_in, 95.0),
        ));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokens_in_come_from_the_llm_chat_span_end() {
        // a streamed turn records SSE text as the http body — the
        // Reply on the span end is the one usage source (BOB-149)
        use crate::tracestore::{MemTraceStore, TraceStore};
        use serde_json::json;
        let recs = vec![
            json!({"kind": "header", "schema": 2,
                   "run": {"id": "run_1", "program": "toolcaller@v1", "host": "rust"}}),
            json!({"kind": "span", "seq": 1, "phase": "begin", "span": "t1",
                   "parent": null, "name": "llm.chat", "input": {}}),
            json!({"kind": "effect", "seq": 2, "effect": "http.post", "span": "t1",
                   "error": null, "meta": {"class": "read", "durMs": 40},
                   "input": {"url": "https://api.anthropic.com/v1/messages", "stream": true},
                   "output": {"status": 200,
                              "body": "event: message_start\ndata: {}\n\n"}}),
            json!({"kind": "span", "seq": 3, "phase": "end", "span": "t1",
                   "name": "llm.chat", "ok": true, "error": null, "meta": {"durMs": 100},
                   "output": {"parts": [], "stop": "done",
                              "usage": {"in": 10, "out": 5, "cacheRead": 30, "cacheWrite": 2}}}),
            json!({"kind": "cell", "seq": 4, "cell": "main", "ok": true, "error": null,
                   "interrupted": false, "metrics": {"duration_ms": 25, "fuel_used": 4}}),
        ];
        let store = MemTraceStore::new();
        store.write_run("run_1", &recs, &[]).unwrap();
        let out = render(&store).unwrap();
        assert!(
            out.contains("tokens_in/call     n=1    min=42 p50=42 p95=42 max=42"),
            "{out}"
        );
    }

    #[test]
    fn percentiles_nearest_rank() {
        let v = vec![1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
        assert_eq!(percentile(&v, 50.0), 5);
        assert_eq!(percentile(&v, 95.0), 10);
        assert_eq!(percentile(&[], 95.0), 0);
    }
}
