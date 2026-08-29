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
                    // provider replies carry usage — tokens per llm call
                    if let Some(body) = r["output"]["body"].as_str() {
                        if let Ok(v) = serde_json::from_str::<Value>(body) {
                            if let Some(t) = v["usage"]["input_tokens"].as_i64() {
                                tokens_in.push(t);
                            }
                        }
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
    fn percentiles_nearest_rank() {
        let v = vec![1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
        assert_eq!(percentile(&v, 50.0), 5);
        assert_eq!(percentile(&v, 95.0), 10);
        assert_eq!(percentile(&[], 95.0), 0);
    }
}
