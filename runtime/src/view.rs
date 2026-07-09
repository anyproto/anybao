//! `anyrt trace show` — the human-side trace render (ADR-006 §1's
//! "debug view"). v3 traces carry ONE host cell per run; semantics ride
//! spans: a turn is an `llm.chat` span, a model cell is a `cell` span.
//!
//! The render is a skim view: clipped lines are locators, `#seq` is the
//! drill-down key (`--seq N` dumps one record, blob-resolved). Errors
//! are never clipped — when a run went wrong, the error text is the
//! payload. `--full` lifts every other clip.

use crate::replay::{load_blobs, load_trace, resolve_blobs};
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::Path;

#[derive(Default)]
pub struct ShowOpts {
    pub full: bool,
    pub system: bool,
}

struct Limits {
    user: usize,
    assistant: usize,
    effect: usize,
    code_lines: usize,
    result: usize,
}

impl Limits {
    fn new(full: bool) -> Self {
        if full {
            Limits {
                user: usize::MAX,
                assistant: usize::MAX,
                effect: usize::MAX,
                code_lines: usize::MAX,
                result: usize::MAX,
            }
        } else {
            Limits {
                user: 200,
                assistant: 300,
                effect: 200,
                code_lines: 20,
                result: 500,
            }
        }
    }
}

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

/// Indent a multi-line block, clipping to `max_lines`.
fn indent_block(text: &str, pad: &str, max_lines: usize) -> String {
    let lines: Vec<&str> = text.lines().collect();
    let shown = lines.len().min(max_lines);
    let mut out: Vec<String> = lines[..shown].iter().map(|l| format!("{pad}{l}")).collect();
    if lines.len() > shown {
        out.push(format!("{pad}… (+{} more lines)", lines.len() - shown));
    }
    out.join("\n")
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

/// URL → its meaningful tail: strip scheme+host, stub CID-ish segments.
fn short_path(url: &str) -> String {
    let path = url
        .splitn(4, '/')
        .nth(3)
        .map(|p| format!("/{p}"))
        .unwrap_or_else(|| url.to_string());
    path.split('/')
        .map(|seg| {
            if seg.len() > 24 && seg.starts_with("bafy") {
                format!("{}…", &seg[..8])
            } else {
                seg.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join("/")
}

/// One effect, rendered semantically — the wire body is noise unless
/// something went wrong (errors always render in full) or --full asks.
fn effect_line(r: &Value, limit: usize, pad: &str) -> String {
    let name = s(&r["effect"]);
    let class = s(&r["meta"]["class"]);
    let dur = r["meta"]["durMs"].as_i64().unwrap_or(0);
    let mark = if class == "mutate" { "*" } else { " " };
    let head = format!("{pad}{mark} #{}", r["seq"]);
    let full = limit == usize::MAX;

    if !r["error"].is_null() {
        return format!("{head} {name} [{class}, {dur}ms] !! {}", r["error"]);
    }
    if let Some(rest) = name.strip_prefix("http.") {
        let path = short_path(r["input"]["url"].as_str().unwrap_or("?"));
        let status = r["output"]["status"].as_i64().unwrap_or(0);
        let mut line = format!("{head} {} {path} → {status} ({dur}ms)", rest.to_uppercase());
        if status >= 400 || full {
            line.push_str(&format!(" {}", s(&r["output"]["body"])));
        }
        return line;
    }
    if name == "module.resolve" {
        return format!(
            "{head} use {} ({})",
            s(&r["input"]["spec"]),
            s(&r["output"]["cache"])
        );
    }
    if name == "kernel.boot" {
        return format!(
            "{head} kernel.boot (schema {}, kernel {})",
            r["output"]["trace_schema"],
            clip(&s(&r["output"]["kernel_sha256"]), 12)
        );
    }
    format!(
        "{head} {name} [{class}, {dur}ms] -> {}",
        clip(&r["output"].to_string(), limit)
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

/// Index of the last plain-text user message — turn 1's real
/// userText; everything before it is the boot window.
fn last_text_user_index(req: &Value) -> usize {
    let msgs = req["messages"].as_array().cloned().unwrap_or_default();
    let mut idx = 0;
    for (i, m) in msgs.iter().enumerate() {
        let has_text = m["content"]
            .as_array()
            .map(|blocks| blocks.iter().any(|b| b["type"] == "text"))
            .unwrap_or(false);
        if m["role"] == "user" && has_text {
            idx = i;
        }
    }
    idx
}

fn user_delta(req: &Value, prev_len: usize, limit: usize) -> Vec<String> {
    let msgs = req["messages"].as_array().cloned().unwrap_or_default();
    let mut out = Vec::new();
    for m in msgs.iter().skip(prev_len) {
        if m["role"] != "user" {
            continue;
        }
        for block in m["content"].as_array().unwrap_or(&Vec::new()) {
            if block["type"] == "text" {
                out.push(clip(&s(&block["text"]), limit));
            }
        }
    }
    out
}

/// Join a tool_result block's content (string or text-part array).
fn tool_result_text(block: &Value) -> String {
    match &block["content"] {
        Value::String(t) => t.clone(),
        Value::Array(parts) => parts
            .iter()
            .filter_map(|p| {
                if p["type"] == "text" {
                    p["text"].as_str().map(str::to_string)
                } else {
                    p.as_str().map(str::to_string)
                }
            })
            .collect::<Vec<_>>()
            .join("\n"),
        other => other.to_string(),
    }
}

/// tool_use_id -> (result text the model saw, is_error). Mined from the
/// tool_result blocks inside every provider request in the trace — the
/// cell span itself doesn't carry the digest.
fn tool_results(records: &[Value]) -> BTreeMap<String, (String, bool)> {
    let mut map = BTreeMap::new();
    for r in records {
        if r["kind"] != "effect" || !s(&r["effect"]).starts_with("http.") {
            continue;
        }
        let empty = Vec::new();
        for m in r["input"]["json"]["messages"].as_array().unwrap_or(&empty) {
            for block in m["content"].as_array().unwrap_or(&empty) {
                if block["type"] == "tool_result" {
                    if let Some(id) = block["tool_use_id"].as_str() {
                        let is_error = block["is_error"] == true;
                        map.insert(id.to_string(), (tool_result_text(block), is_error));
                    }
                }
            }
        }
    }
    map
}

/// The system prompt from a provider request: string or block array.
fn system_text(req: &Value) -> String {
    match &req["system"] {
        Value::String(t) => t.clone(),
        Value::Array(blocks) => blocks
            .iter()
            .filter_map(|b| b["text"].as_str())
            .collect::<Vec<_>>()
            .join("\n"),
        _ => String::new(),
    }
}

/// One llm response rendered: assistant text, tool_use code blocks,
/// and the scalar llm line (tokens/cache/stop_reason/duration).
fn llm_body(
    out: &mut String,
    resp: &Value,
    end: Option<&Value>,
    pad: &str,
    lim: &Limits,
    cells: &[(&Value, Option<&Value>)],
) {
    for block in resp["content"].as_array().unwrap_or(&Vec::new()) {
        match block["type"].as_str() {
            Some("text") => out.push_str(&format!(
                "{pad}assistant: {}\n",
                clip(&s(&block["text"]), lim.assistant)
            )),
            Some("tool_use") => {
                let id = s(&block["id"]);
                let executed = cells
                    .iter()
                    .any(|(cb, _)| cb["input"]["cell"].as_str() == Some(id.as_str()));
                out.push_str(&format!(
                    "{pad}cell {id}{}:\n",
                    if executed { "" } else { " — never executed" }
                ));
                let code = block["input"]["code"].as_str().unwrap_or("");
                out.push_str(&indent_block(code, &format!("{pad}|   "), lim.code_lines));
                out.push('\n');
            }
            _ => {}
        }
    }
    let stop = resp["stop_reason"].as_str().unwrap_or("?");
    let u = &resp["usage"];
    let dur = end
        .map(|e| format!("{}ms", e["meta"]["durMs"]))
        .unwrap_or_else(|| "no span end — run ended mid-turn".into());
    out.push_str(&format!(
        "{pad}llm: in={} out={} cacheRead={} stop={stop} ({dur})\n",
        u["input_tokens"].as_i64().unwrap_or(0),
        u["output_tokens"].as_i64().unwrap_or(0),
        u["cache_read_input_tokens"].as_i64().unwrap_or(0),
    ));
}

/// Why an llm span has no exchange — the error in full, or a marker.
fn llm_error(out: &mut String, inner: &[&Value], end: Option<&Value>, pad: &str) {
    let failed = inner
        .iter()
        .find(|r| r["kind"] == "effect" && !r["error"].is_null());
    match (failed, end) {
        (Some(r), _) => out.push_str(&format!(
            "{pad}!! llm error: #{} {} {}\n",
            r["seq"],
            s(&r["effect"]),
            r["error"]
        )),
        (None, Some(e)) if !e["error"].is_null() => {
            out.push_str(&format!("{pad}!! llm error: {}\n", e["error"]))
        }
        _ => out.push_str(&format!(
            "{pad}(incomplete turn — no llm exchange recorded)\n"
        )),
    }
}

fn span_end_of<'a>(records: &'a [Value], begin: &Value) -> Option<&'a Value> {
    records
        .iter()
        .find(|e| e["kind"] == "span" && e["phase"] == "end" && e["span"] == begin["span"])
}

fn seq_range(begin: &Value, end: Option<&Value>) -> (i64, i64) {
    (
        begin["seq"].as_i64().unwrap_or(0),
        end.map(|e| e["seq"].as_i64().unwrap_or(i64::MAX))
            .unwrap_or(i64::MAX),
    )
}

/// A facade span (autorecall.plan, memory.save_with_dedup, …): one
/// header line, then its own effects and child llm calls in order —
/// this is where trigger runs do their real work (ADR-006 §1: facade
/// spans collapse composites, they must not hide them).
fn facade_block(
    out: &mut String,
    records: &[Value],
    begin: &Value,
    end: Option<&Value>,
    pad: &str,
    lim: &Limits,
    cells: &[(&Value, Option<&Value>)],
) {
    let name = s(&begin["name"]);
    let (verdict, dur, counts) = match end {
        Some(e) => (
            if e["ok"] == true { "ok" } else { "FAILED" },
            format!("{}ms", e["meta"]["durMs"]),
            {
                let m = e["meta"]["mutations"].as_i64().unwrap_or(0);
                let eff = format!("{} effects", e["meta"]["effects"]);
                if m > 0 {
                    format!("{eff}, {m} mutate")
                } else {
                    eff
                }
            },
        ),
        None => ("NO END (trap?)", "?".into(), "?".into()),
    };
    out.push_str(&format!("{pad}~ {name} {verdict} ({dur}, {counts})\n"));
    if let Some(e) = end {
        if !e["error"].is_null() {
            out.push_str(&format!("{pad}  error: {}\n", e["error"]));
        }
    }
    let (b_seq, e_seq) = seq_range(begin, end);
    let inner_pad = format!("{pad}  ");
    for r in between(records, b_seq, e_seq) {
        if r["kind"] == "effect" && r["span"] == begin["span"] {
            out.push_str(&effect_line(r, lim.effect, &inner_pad));
            out.push('\n');
        }
        if r["kind"] == "span" && r["phase"] == "begin" && r["parent"] == begin["span"] {
            let cend = span_end_of(records, r);
            if r["name"] == "llm.chat" {
                let (cb, ce) = seq_range(r, cend);
                let cinner = between(records, cb, ce);
                out.push_str(&format!("{inner_pad}llm.chat:\n"));
                match llm_exchange(&cinner) {
                    Some((_, resp)) => {
                        llm_body(out, &resp, cend, &format!("{inner_pad}  "), lim, cells)
                    }
                    None => llm_error(out, &cinner, cend, &format!("{inner_pad}  ")),
                }
            } else {
                facade_block(out, records, r, cend, &inner_pad, lim, cells);
            }
        }
    }
}

fn load_resolved(path: &Path) -> anyhow::Result<Vec<Value>> {
    let mut records = load_trace(path)?;
    let blobs = load_blobs(path)?;
    if !blobs.is_empty() {
        for r in records.iter_mut() {
            if let Some(obj) = r.as_object_mut() {
                for key in ["input", "output"] {
                    if let Some(v) = obj.get(key) {
                        obj.insert(key.into(), resolve_blobs(v.clone(), &blobs));
                    }
                }
            }
        }
    }
    Ok(records)
}

/// `--seq N` drill-down: one record, blob-resolved, pretty.
pub fn show_record(path: &Path, seq: i64) -> anyhow::Result<String> {
    let records = load_resolved(path)?;
    let rec = records
        .iter()
        .find(|r| r["seq"].as_i64() == Some(seq))
        .ok_or_else(|| anyhow::anyhow!("no record with seq {seq}"))?;
    Ok(serde_json::to_string_pretty(rec)? + "\n")
}

pub fn render(path: &Path, opts: &ShowOpts) -> anyhow::Result<String> {
    let records = load_resolved(path)?;
    let lim = Limits::new(opts.full);
    let header = &records[0];
    let mut out = format!(
        "run {} — {}\n",
        s(&header["run"]["id"]),
        s(&header["run"]["program"])
    );

    let all_llm = spans_of(&records, "llm.chat");
    // #turn_N = PARENTLESS llm.chat spans; a child llm call (e.g. the
    // dedup judge inside memory.save_with_dedup) renders under its facade
    let turn_count = all_llm
        .iter()
        .filter(|(b, _)| b["parent"].is_null())
        .count();
    let cells = spans_of(&records, "cell");
    let effects: Vec<&Value> = records.iter().filter(|r| r["kind"] == "effect").collect();
    let mutations = effects
        .iter()
        .filter(|r| r["meta"]["class"] == "mutate")
        .count();
    let results = tool_results(&records);

    // every llm exchange (nested included — their tokens are real spend)
    let exchanges: Vec<Option<(&Value, Value)>> = all_llm
        .iter()
        .map(|(begin, end)| {
            let (b, e) = seq_range(begin, *end);
            llm_exchange(&between(&records, b, e))
        })
        .collect();

    // status — the done-record equivalent, from the terminal cell record
    match records.iter().rev().find(|r| r["kind"] == "cell") {
        Some(r) => {
            let ok = r["ok"] == true;
            let dur = r["metrics"]["duration_ms"].as_f64().unwrap_or(0.0);
            out.push_str(&format!(
                "status: {} — {:.1}s, fuel {}{}\n",
                if ok { "ok" } else { "FAILED" },
                dur / 1000.0,
                r["metrics"]["fuel_used"],
                if r["interrupted"] == true {
                    ", interrupted"
                } else {
                    ""
                }
            ));
            if !r["error"].is_null() {
                out.push_str(&format!("  error: {}\n", r["error"]));
            }
        }
        None => out
            .push_str("status: incomplete — no terminal cell record (crashed or still running)\n"),
    }

    let mut tokens_in = 0i64;
    let mut tokens_out = 0i64;
    let mut cache_read = 0i64;
    let mut cache_write = 0i64;
    let mut model = String::new();
    for ex in exchanges.iter().flatten() {
        let u = &ex.1["usage"];
        tokens_in += u["input_tokens"].as_i64().unwrap_or(0);
        tokens_out += u["output_tokens"].as_i64().unwrap_or(0);
        cache_read += u["cache_read_input_tokens"].as_i64().unwrap_or(0);
        cache_write += u["cache_creation_input_tokens"].as_i64().unwrap_or(0);
        if model.is_empty() {
            if let Some(m) = ex.1["model"].as_str() {
                model = m.to_string();
            }
        }
    }
    out.push_str(&format!(
        "totals: {} turns, {} cells, {} effects ({} mutate)\n",
        turn_count,
        cells.len(),
        effects.len(),
        mutations,
    ));
    if !all_llm.is_empty() {
        out.push_str(&format!(
            "llm: {} — tokens in={} out={} cacheRead={} cacheWrite={}\n",
            if model.is_empty() { "?" } else { &model },
            tokens_in,
            tokens_out,
            cache_read,
            cache_write
        ));
    }

    if opts.system {
        if let Some((req, _)) = exchanges.iter().flatten().next() {
            let sys = system_text(req);
            out.push_str(&format!(
                "\nsystem prompt ({} chars):\n",
                sys.chars().count()
            ));
            out.push_str(&indent_block(&sys, "  ", usize::MAX));
            out.push('\n');
        }
    }

    // chronological walk over top-level items: loose effects, turns
    // (parentless llm.chat), model cells, facade spans. Nothing in the
    // trace is invisible — everything renders exactly once.
    let mut turn_no = 0usize;
    let mut prev_msgs = 0usize;
    for rec in &records {
        if rec["kind"] == "effect" && rec["span"].is_null() {
            // loop plumbing is noise: the digest's trace.* reads and
            // empty mailbox polls (a NON-empty drain is a user injection)
            let name = s(&rec["effect"]);
            let empty_drain = name == "mailbox.drain"
                && rec["output"]["items"]
                    .as_array()
                    .map(|a| a.is_empty())
                    .unwrap_or(false);
            if !name.starts_with("trace.") && !empty_drain {
                out.push_str(&effect_line(rec, lim.effect, "  "));
                out.push('\n');
            }
            continue;
        }
        if rec["kind"] != "span" || rec["phase"] != "begin" || !rec["parent"].is_null() {
            continue;
        }
        let end = span_end_of(&records, rec);
        let (b_seq, e_seq) = seq_range(rec, end);
        let inner = between(&records, b_seq, e_seq);
        match rec["name"].as_str().unwrap_or("") {
            "llm.chat" => {
                turn_no += 1;
                out.push_str(&format!("\n#turn_{turn_no}\n"));
                if let Some((req, resp)) = llm_exchange(&inner) {
                    let skip = if turn_no == 1 {
                        last_text_user_index(req)
                    } else {
                        prev_msgs
                    };
                    for text in user_delta(req, skip, lim.user) {
                        out.push_str(&format!("  user: {text}\n"));
                    }
                    prev_msgs = req["messages"].as_array().map(|m| m.len()).unwrap_or(0) + 1;
                    llm_body(&mut out, &resp, end, "  ", &lim, &cells);
                } else {
                    llm_error(&mut out, &inner, end, "  ");
                }
            }
            "cell" => {
                let id = s(&rec["input"]["cell"]);
                let (verdict, dur, neff) = match end {
                    Some(e) => (
                        if e["ok"] == true { "ok" } else { "FAILED" },
                        format!("{}ms", e["meta"]["durMs"]),
                        format!("{} effects", e["meta"]["effects"]),
                    ),
                    None => ("NO END (trap?)", "?".into(), "?".into()),
                };
                out.push_str(&format!("  cell {id} {verdict} ({dur}, {neff})\n"));
                if let Some(e) = end {
                    if !e["error"].is_null() {
                        out.push_str(&format!("    error: {}\n", e["error"]));
                    }
                }
                for r in &inner {
                    if r["kind"] == "effect" {
                        out.push_str(&effect_line(r, lim.effect, "  "));
                        out.push('\n');
                    }
                }
                if let Some((text, is_error)) = results.get(&id) {
                    // error results render in full — that's the payload
                    let limit = if *is_error { usize::MAX } else { lim.result };
                    let tag = if *is_error { "result !!" } else { "result" };
                    out.push_str(&format!("    {tag}:\n"));
                    out.push_str(&indent_block(&clip(text, limit), "    | ", usize::MAX));
                    out.push('\n');
                }
            }
            _ => {
                out.push('\n');
                facade_block(&mut out, &records, rec, end, "  ", &lim, &cells);
            }
        }
    }
    Ok(out)
}
