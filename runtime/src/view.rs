//! `anyrt trace show` — the human-side trace render (ADR-006 §1's
//! "debug view"). v3 traces carry ONE host cell per run; semantics ride
//! spans: a turn is an `llm.chat` span, a model cell is a `cell` span.

use crate::replay::load_trace;
use serde_json::Value;
use std::path::Path;

fn s(v: &Value) -> String {
    v.as_str()
        .map(str::to_string)
        .unwrap_or_else(|| v.to_string())
}

fn clip(text: &str, limit: usize) -> String {
    let t = text.trim();
    if t.chars().count() <= limit {
        t.into()
    } else {
        let cut: String = t.chars().take(limit).collect();
        format!("{cut}…")
    }
}

/// Pair span begin/end records by id (end may be missing on a trap).
fn spans_of<'a>(records: &'a [Value], name: &str) -> Vec<(&'a Value, Option<&'a Value>)> {
    let mut out = Vec::new();
    for r in records {
        if r["kind"] == "span" && r["phase"] == "begin" && r["name"] == name {
            let id = &r["span"];
            let end = records
                .iter()
                .find(|e| e["kind"] == "span" && e["phase"] == "end" && &e["span"] == id);
            out.push((r, end));
        }
    }
    out
}

fn between(records: &[Value], from_seq: i64, to_seq: i64) -> Vec<&Value> {
    records
        .iter()
        .filter(|r| {
            let seq = r["seq"].as_i64().unwrap_or(-1);
            seq > from_seq && seq < to_seq
        })
        .collect()
}

fn effect_line(r: &Value) -> String {
    let class = s(&r["meta"]["class"]);
    let dur = r["meta"]["durMs"].as_i64().unwrap_or(0);
    let out = if r["error"].is_null() {
        format!("-> {}", clip(&r["output"].to_string(), 90))
    } else {
        format!("!! {}", clip(&r["error"].to_string(), 90))
    };
    let mark = if class == "mutate" { "*" } else { " " };
    format!(
        "  {mark} #{} {} [{}, {}ms] {}",
        r["seq"],
        s(&r["effect"]),
        class,
        dur,
        out
    )
}

/// The llm exchange inside an llm.chat span: (request messages,
/// response body) from its inner provider http call.
fn llm_exchange<'a>(inner: &[&'a Value]) -> Option<(&'a Value, Value)> {
    let post = inner
        .iter()
        .find(|r| r["kind"] == "effect" && s(&r["effect"]).starts_with("http."))?;
    let req = &post["input"]["json"];
    let body = post["output"]["body"].as_str()?;
    Some((req, serde_json::from_str(body).ok()?))
}

fn user_delta(req: &Value, prev_len: usize) -> Vec<String> {
    let msgs = req["messages"].as_array().cloned().unwrap_or_default();
    let mut out = Vec::new();
    for m in msgs.iter().skip(prev_len) {
        if m["role"] != "user" {
            continue;
        }
        for block in m["content"].as_array().unwrap_or(&Vec::new()) {
            if block["type"] == "text" {
                out.push(clip(&s(&block["text"]), 200));
            }
        }
    }
    out
}

pub fn render(path: &Path) -> anyhow::Result<String> {
    let records = load_trace(path)?;
    let header = &records[0];
    let mut out = format!(
        "run {} — {}\n",
        s(&header["run"]["id"]),
        s(&header["run"]["program"])
    );

    let turns = spans_of(&records, "llm.chat");
    let cells = spans_of(&records, "cell");
    let effects: Vec<&Value> = records.iter().filter(|r| r["kind"] == "effect").collect();
    let mutations = effects
        .iter()
        .filter(|r| r["meta"]["class"] == "mutate")
        .count();
    let mut tokens_in = 0i64;
    let mut tokens_out = 0i64;
    for (_, end) in &turns {
        if let Some(e) = end {
            if let Some((_, resp)) =
                llm_exchange(&between(&records, 0, e["seq"].as_i64().unwrap_or(i64::MAX)))
            {
                tokens_in += resp["usage"]["input_tokens"].as_i64().unwrap_or(0);
                tokens_out += resp["usage"]["output_tokens"].as_i64().unwrap_or(0);
            }
        }
    }
    out.push_str(&format!(
        "totals: {} turns, {} cells, {} effects ({} mutate), tokens in={} out={}\n",
        turns.len(),
        cells.len(),
        effects.len(),
        mutations,
        tokens_in,
        tokens_out
    ));

    let mut prev_msgs = 0usize;
    for (i, (begin, end)) in turns.iter().enumerate() {
        let b_seq = begin["seq"].as_i64().unwrap_or(0);
        let e_seq = end
            .map(|e| e["seq"].as_i64().unwrap_or(i64::MAX))
            .unwrap_or(i64::MAX);
        let inner = between(&records, b_seq, e_seq);
        out.push_str(&format!("\n#turn_{}\n", i + 1));
        if let Some((req, resp)) = llm_exchange(&inner) {
            for text in user_delta(req, prev_msgs) {
                out.push_str(&format!("  user: {text}\n"));
            }
            prev_msgs = req["messages"].as_array().map(|m| m.len()).unwrap_or(0) + 1;
            for block in resp["content"].as_array().unwrap_or(&Vec::new()) {
                match block["type"].as_str() {
                    Some("text") => {
                        out.push_str(&format!("  assistant: {}\n", clip(&s(&block["text"]), 300)))
                    }
                    Some("tool_use") => out.push_str(&format!(
                        "  cell {} ({} chars)\n",
                        s(&block["id"]),
                        block["input"]["code"].as_str().map(str::len).unwrap_or(0)
                    )),
                    _ => {}
                }
            }
            if let Some(e) = end {
                out.push_str(&format!(
                    "  llm: in={} out={} ({}ms)\n",
                    resp["usage"]["input_tokens"],
                    resp["usage"]["output_tokens"],
                    e["meta"]["durMs"]
                ));
            }
        }
        // model cells executed after this turn's reply, before the next turn
        let next_b = turns
            .get(i + 1)
            .map(|(b, _)| b["seq"].as_i64().unwrap_or(i64::MAX))
            .unwrap_or(i64::MAX);
        for (cb, ce) in &cells {
            let cseq = cb["seq"].as_i64().unwrap_or(0);
            if cseq > e_seq && cseq < next_b {
                let ok = ce
                    .map(|e| e["ok"].as_bool().unwrap_or(false))
                    .unwrap_or(false);
                out.push_str(&format!(
                    "  cell {} {}\n",
                    s(&cb["input"]["cell"]),
                    if ok { "ok" } else { "FAILED" }
                ));
                let ce_seq = ce
                    .map(|e| e["seq"].as_i64().unwrap_or(i64::MAX))
                    .unwrap_or(i64::MAX);
                for r in between(&records, cseq, ce_seq) {
                    if r["kind"] == "effect" {
                        out.push_str(&effect_line(r));
                        out.push('\n');
                    }
                }
            }
        }
    }

    for r in &records {
        if r["kind"] == "cell" {
            out.push_str(&format!(
                "\nrun cell: ok={} interrupted={} fuel={} {}ms\n",
                r["ok"], r["interrupted"], r["metrics"]["fuel_used"], r["metrics"]["duration_ms"]
            ));
        }
    }
    Ok(out)
}
