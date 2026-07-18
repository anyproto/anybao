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
    pub boot: bool,
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

/// Clip for skim-view CONTENT: the tail is a locator, not just an
/// ellipsis — how much is hidden and that --full lifts it (dev C1).
/// Plain `clip` stays for identifiers (titles, hashes) where the
/// locator would be noise.
fn clip_loc(text: &str, limit: usize) -> String {
    let t = text.trim();
    let n = t.chars().count();
    if n <= limit {
        t.into()
    } else {
        let cut: String = t.chars().take(limit).collect();
        format!("{cut}… (+{} chars — --full)", n - limit)
    }
}

/// Indent a multi-line block, clipping to `max_lines`.
fn indent_block(text: &str, pad: &str, max_lines: usize) -> String {
    let lines: Vec<&str> = text.lines().collect();
    let shown = lines.len().min(max_lines);
    let mut out: Vec<String> = lines[..shown].iter().map(|l| format!("{pad}{l}")).collect();
    if lines.len() > shown {
        out.push(format!(
            "{pad}… (+{} more lines — --full)",
            lines.len() - shown
        ));
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

/// Pretty-print a JSON Value as an indented block (--full). Unbounded —
/// --full means "show everything", so no line cap.
fn pretty_json(v: &Value, pad: &str) -> String {
    let text = serde_json::to_string_pretty(v).unwrap_or_else(|_| v.to_string());
    indent_block(&text, pad, usize::MAX)
}

/// Pretty-print a string that may hold JSON (an http body): parsed +
/// indented when it's JSON, indented raw otherwise.
fn pretty_body(raw: &str, pad: &str) -> String {
    match serde_json::from_str::<Value>(raw) {
        Ok(v) => pretty_json(&v, pad),
        Err(_) => indent_block(raw, pad, usize::MAX),
    }
}

/// One effect, rendered semantically — the wire body is noise unless
/// something went wrong (errors always render in full) or --full asks.
/// Under --full the body is pretty-printed as an indented block.
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
        let body = s(&r["output"]["body"]);
        if full {
            // request first (the query/filter/body you sent), then response
            let label = format!("{pad}    ");
            let inner = format!("{pad}      ");
            let req = &r["input"]["json"];
            if !req.is_null() {
                line.push_str(&format!("\n{label}req:\n{}", pretty_json(req, &inner)));
            }
            if !body.is_empty() {
                line.push_str(&format!("\n{label}resp:\n{}", pretty_body(&body, &inner)));
            }
        } else if status >= 400 {
            line.push_str(&format!(" {body}"));
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
    if full {
        return format!(
            "{head} {name} [{class}, {dur}ms] ->\n{}",
            pretty_json(&r["output"], &format!("{pad}    "))
        );
    }
    format!(
        "{head} {name} [{class}, {dur}ms] -> {}",
        clip_loc(&r["output"].to_string(), limit)
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

/// The llm turn's request (the http.* effect's INPUT), independent of
/// whether the call succeeded — a failed turn still recorded its input,
/// so we can show what the user said even when there's no response.
fn llm_request<'a>(inner: &[&'a Value]) -> Option<&'a Value> {
    inner
        .iter()
        .find(|r| r["kind"] == "effect" && s(&r["effect"]).starts_with("http."))
        .map(|post| &post["input"]["json"])
}

/// Render the boot window — the messages the loop assembled BEFORE the
/// current user turn (history tail + injected context), normally skipped
/// on turn 1 as noise. Under --full it's the "what exactly did the loop
/// feed the model" view. `skip` = how many leading messages precede the
/// current user message. System prompt renders separately (--system/--full).
fn boot_window(out: &mut String, req: &Value, skip: usize, pad: &str) {
    if skip == 0 {
        return;
    }
    let msgs = req["messages"].as_array().cloned().unwrap_or_default();
    out.push_str(&format!("{pad}── initial context: {skip} message(s) ──\n"));
    for (i, m) in msgs.iter().take(skip).enumerate() {
        let role = s(&m["role"]);
        for block in m["content"].as_array().unwrap_or(&Vec::new()) {
            let body = match s(&block["type"]).as_str() {
                "text" => s(&block["text"]),
                "tool_use" => format!("[tool_use {} {}]", s(&block["name"]), block["input"]),
                "tool_result" => format!("[tool_result {}]", tool_result_text(block)),
                other => format!("[{other}]"),
            };
            out.push_str(&format!("{pad}[{i}] {role}: {body}\n"));
        }
    }
}

/// Split the harness ui-context suffix (`\n\n[now: …]`, appended by
/// toolcaller@v1) off a user message: `(human message, ui-context?)`.
/// The locator carries the space/object ids the model saw — the audit
/// line — so callers render it unclipped even in the skim view.
fn split_ui_context(text: &str) -> (&str, Option<&str>) {
    match text.rfind("\n\n[now:") {
        Some(pos) => (text[..pos].trim_end(), Some(text[pos..].trim())),
        None => (text.trim(), None),
    }
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

fn user_delta(req: &Value, prev_len: usize) -> Vec<String> {
    let msgs = req["messages"].as_array().cloned().unwrap_or_default();
    let mut out = Vec::new();
    for m in msgs.iter().skip(prev_len) {
        if m["role"] != "user" {
            continue;
        }
        for block in m["content"].as_array().unwrap_or(&Vec::new()) {
            if block["type"] == "text" {
                out.push(s(&block["text"]));
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

/// tool_use_id -> (result text the model saw, is_error, source seq).
/// Mined from the tool_result blocks inside every provider request in
/// the trace — the cell span itself doesn't carry the digest; the seq
/// names the request it was mined from, so --seq finds the full text
/// even when the render clips it (dev C1).
fn tool_results(records: &[Value]) -> BTreeMap<String, (String, bool, i64)> {
    let mut map = BTreeMap::new();
    for r in records {
        if r["kind"] != "effect" || !s(&r["effect"]).starts_with("http.") {
            continue;
        }
        let seq = r["seq"].as_i64().unwrap_or(0);
        let empty = Vec::new();
        for m in r["input"]["json"]["messages"].as_array().unwrap_or(&empty) {
            for block in m["content"].as_array().unwrap_or(&empty) {
                if block["type"] == "tool_result" {
                    if let Some(id) = block["tool_use_id"].as_str() {
                        let is_error = block["is_error"] == true;
                        // first occurrence wins: the request right after
                        // the cell, the closest record to drill into
                        map.entry(id.to_string()).or_insert((
                            tool_result_text(block),
                            is_error,
                            seq,
                        ));
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
                clip_loc(&s(&block["text"]), lim.assistant)
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
    // narrative kind rides the end record's meta (ADR-001 §4d) — show it on
    // the collapsed line so read/write/bind intent is legible at a glance.
    let name = match end.and_then(|e| e["meta"]["kind"].as_str()) {
        Some(k) => format!("{name} [{k}]"),
        None => name.to_string(),
    };
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

/// One `trace ls` row, derived from the records alone. Wall-clock
/// comes from file mtime — records are deliberately time-free.
struct LsRow {
    id: String,
    program: String,
    status: &'static str,
    dur: String,
    turns: usize,
    title: String,
}

/// Status/duration/turns mirror `render`; the title is turn 1's user
/// text (the "title from chat"), mined exactly the way `render` mines
/// the first turn's user delta.
fn ls_row(records: &[Value]) -> LsRow {
    let (status, dur) = match records.iter().rev().find(|r| r["kind"] == "cell") {
        Some(r) => (
            if r["interrupted"] == true {
                "interrupted"
            } else if r["ok"] == true {
                "ok"
            } else {
                "FAILED"
            },
            format!(
                "{:.1}s",
                r["metrics"]["duration_ms"].as_f64().unwrap_or(0.0) / 1000.0
            ),
        ),
        None => ("incomplete", "?".into()),
    };
    let turns: Vec<_> = spans_of(records, "llm.chat")
        .into_iter()
        .filter(|(b, _)| b["parent"].is_null())
        .collect();
    let title = turns
        .first()
        .and_then(|(begin, end)| {
            let (b, e) = seq_range(begin, *end);
            let (req, _) = llm_exchange(&between(records, b, e))?;
            user_delta(req, last_text_user_index(req))
                .into_iter()
                .next()
        })
        .map(|t| clip(&t.split_whitespace().collect::<Vec<_>>().join(" "), 60))
        .unwrap_or_default();
    LsRow {
        id: s(&records[0]["run"]["id"]),
        program: s(&records[0]["run"]["program"]),
        status,
        dur,
        turns: turns.len(),
        title,
    }
}

/// `trace ls` — the run finder: one line per run in `dir`, newest
/// first (file mtime — the run's only wall-clock). Unreadable files
/// render as a `?` row rather than sinking the listing.
pub fn list(dir: &Path, program: Option<&str>, limit: usize) -> anyhow::Result<String> {
    let mut paths: Vec<(std::time::SystemTime, std::path::PathBuf)> = std::fs::read_dir(dir)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().map(|x| x == "jsonl").unwrap_or(false))
        .filter_map(|p| {
            let mtime = p.metadata().and_then(|m| m.modified()).ok()?;
            Some((mtime, p))
        })
        .collect();
    paths.sort_by_key(|(mtime, _)| std::cmp::Reverse(*mtime));

    let mut rows = Vec::new();
    for (mtime, path) in &paths {
        let row = match load_trace(path) {
            Ok(records) if !records.is_empty() => ls_row(&records),
            _ => LsRow {
                id: path
                    .file_stem()
                    .map(|s| s.to_string_lossy().into_owned())
                    .unwrap_or_default(),
                program: "?".into(),
                status: "unreadable",
                dur: "?".into(),
                turns: 0,
                title: String::new(),
            },
        };
        if let Some(f) = program {
            if !row.program.contains(f) {
                continue;
            }
        }
        rows.push((*mtime, row));
    }

    let total = rows.len();
    let shown = if limit == 0 { total } else { total.min(limit) };
    let mut out = String::new();
    for (mtime, row) in rows.into_iter().take(shown) {
        let when = chrono::DateTime::<chrono::Local>::from(mtime).format("%m-%d %H:%M");
        out.push_str(&format!(
            "{when}  {:<20}  {:<14}  {:<11}  {:>7}  {:>2}t  {}\n",
            row.id, row.program, row.status, row.dur, row.turns, row.title
        ));
    }
    if total > shown {
        out.push_str(&format!("… {} more (-n 0 shows all)\n", total - shown));
    }
    Ok(out)
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

    // Boot block (dev C2): everything turn 1 fed the model that the
    // skim view doesn't show — system prompt size, boot-window message
    // count, tool names — announced with the flag that expands each.
    // The full text expands only on request (--system / --boot; --full
    // also dumps the window inline at turn 1): large and stable = noise
    // unless you asked.
    if let Some((req, _)) = exchanges.iter().flatten().next() {
        let sys = system_text(req);
        let kb = sys.len() as f64 / 1024.0;
        let window = last_text_user_index(req);
        let empty = Vec::new();
        let tools: Vec<String> = req["tools"]
            .as_array()
            .unwrap_or(&empty)
            .iter()
            .map(|t| s(&t["name"]))
            .collect();
        out.push_str(&format!(
            "\nboot: system {kb:.1}KB (--system) · window {window} msgs (--boot) · tools: {}\n",
            if tools.is_empty() {
                "none".into()
            } else {
                tools.join(", ")
            }
        ));
        if opts.system {
            out.push_str(&format!("\nsystem prompt ({kb:.1}KB):\n"));
            out.push_str(&indent_block(&sys, "  ", usize::MAX));
            out.push('\n');
        }
        if opts.boot {
            out.push('\n');
            boot_window(&mut out, req, window, "  ");
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
                // User input + ui-context render from the request, BEFORE
                // the exchange/error branch — so a turn that failed
                // mid-call (e.g. a broker error) still shows what the user
                // said and the ui-context locator that led there.
                if let Some(req) = llm_request(&inner) {
                    let skip = if turn_no == 1 {
                        last_text_user_index(req)
                    } else {
                        prev_msgs
                    };
                    // --full on turn 1: show the boot window (system +
                    // history the loop fed) so "what went to the model" is
                    // visible, not just the current user delta.
                    if turn_no == 1 && lim.user == usize::MAX {
                        boot_window(&mut out, req, skip, "  ");
                    }
                    for text in user_delta(req, skip) {
                        let (msg, ui) = split_ui_context(&text);
                        out.push_str(&format!("  user: {}\n", clip_loc(msg, lim.user)));
                        if let Some(ui) = ui {
                            // the locator is an audit line — never clipped
                            out.push_str(&format!("  ui:   {ui}\n"));
                        }
                    }
                    prev_msgs = req["messages"].as_array().map(|m| m.len()).unwrap_or(0) + 1;
                }
                match llm_exchange(&inner) {
                    Some((_, resp)) => llm_body(&mut out, &resp, end, "  ", &lim, &cells),
                    None => llm_error(&mut out, &inner, end, "  "),
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
                if let Some((text, is_error, src)) = results.get(&id) {
                    // error results render in full — that's the payload
                    let limit = if *is_error { usize::MAX } else { lim.result };
                    let tag = if *is_error { "result !!" } else { "result" };
                    out.push_str(&format!("    {tag} (mined from #{src}):\n"));
                    out.push_str(&indent_block(&clip_loc(text, limit), "    | ", usize::MAX));
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

/// Per-MTok USD prices keyed by model-id prefix (dated suffixes match) —
/// viewer data, not config: the trace records the model, so a run is
/// priceable offline. Edit the json to change rates.
const MODEL_PRICING: &str = include_str!("model_pricing.json");

struct Price {
    input: f64,
    output: f64,
    cache_read: f64,
    cache_write: f64,
}

fn price_for(model: &str) -> Option<Price> {
    let table: BTreeMap<String, BTreeMap<String, f64>> =
        serde_json::from_str(MODEL_PRICING).expect("model_pricing.json is valid JSON");
    let (_, p) = table.iter().find(|(k, _)| model.starts_with(k.as_str()))?;
    let get = |k: &str| p.get(k).copied().unwrap_or(0.0);
    Some(Price {
        input: get("in"),
        output: get("out"),
        cache_read: get("cacheRead"),
        cache_write: get("cacheWrite"),
    })
}

#[derive(Default)]
struct TurnStats {
    stop: String,
    input: i64,
    cache_read: i64,
    cache_write: i64,
    output: i64,
    cells: usize,
    effects: usize,
    llm_ms: i64,
}

impl TurnStats {
    fn cost_usd(&self, p: &Price) -> f64 {
        (self.input as f64 * p.input
            + self.cache_read as f64 * p.cache_read
            + self.cache_write as f64 * p.cache_write
            + self.output as f64 * p.output)
            / 1e6
    }
}

/// `trace show --stats` — the per-turn metrics table (ADR-006 viewer
/// parity): stop / tokens / cache / cells / effects / llm time per
/// parentless llm.chat span, totals, and costUsd priced from the model
/// the trace recorded. A turn's cells and effects are everything from
/// its llm.chat begin up to the next turn's begin.
pub fn stats(path: &Path) -> anyhow::Result<String> {
    let records = load_resolved(path)?;
    let turns: Vec<_> = spans_of(&records, "llm.chat")
        .into_iter()
        .filter(|(b, _)| b["parent"].is_null())
        .collect();
    let starts: Vec<i64> = turns
        .iter()
        .map(|(b, _)| b["seq"].as_i64().unwrap_or(0))
        .collect();
    let model = turns
        .first()
        .and_then(|(b, e)| {
            let (bs, es) = seq_range(b, *e);
            llm_request(&between(&records, bs, es)).map(|req| s(&req["model"]))
        })
        .unwrap_or_default();

    let mut rows = Vec::new();
    for (i, (_begin, end)) in turns.iter().enumerate() {
        let mut t = TurnStats::default();
        if let Some(e) = end {
            let u = &e["output"]["usage"];
            t.input = u["in"].as_i64().unwrap_or(0);
            t.cache_read = u["cacheRead"].as_i64().unwrap_or(0);
            t.cache_write = u["cacheWrite"].as_i64().unwrap_or(0);
            t.output = u["out"].as_i64().unwrap_or(0);
            t.stop = s(&e["output"]["stop"]);
            t.llm_ms = e["meta"]["durMs"].as_i64().unwrap_or(0);
        } else {
            t.stop = "NO END".into();
        }
        let from = starts[i];
        let to = starts.get(i + 1).copied().unwrap_or(i64::MAX);
        for r in &records {
            let seq = r["seq"].as_i64().unwrap_or(-1);
            if seq < from || seq >= to {
                continue;
            }
            if r["kind"] == "effect" {
                t.effects += 1;
            }
            if r["kind"] == "span" && r["phase"] == "begin" && r["name"] == "cell" {
                t.cells += 1;
            }
        }
        rows.push(t);
    }

    let price = price_for(&model);
    let mut out = format!(
        "run {} — {} ({})\n",
        s(&records[0]["run"]["id"]),
        s(&records[0]["run"]["program"]),
        if model.is_empty() {
            "no llm calls"
        } else {
            &model
        }
    );
    if let Some(term) = records
        .iter()
        .find(|r| r["kind"] == "cell" && r["cell"] == "main")
    {
        out.push_str(&format!(
            "status: {} — {:.1}s wall, fuel {}\n",
            if term["ok"] == true { "ok" } else { "FAILED" },
            term["metrics"]["duration_ms"].as_f64().unwrap_or(0.0) / 1000.0,
            term["metrics"]["fuel_used"]
        ));
    }
    out.push('\n');
    out.push_str(&format!(
        "{:>4}  {:<8} {:>9} {:>9} {:>9} {:>7} {:>5} {:>7} {:>8} {:>9}\n",
        "turn", "stop", "in", "cacheRd", "cacheWr", "out", "cells", "effects", "llm_ms", "costUsd"
    ));
    let mut tot = TurnStats::default();
    for (i, t) in rows.iter().enumerate() {
        let cost = price
            .as_ref()
            .map(|p| format!("{:.4}", t.cost_usd(p)))
            .unwrap_or_else(|| "-".into());
        out.push_str(&format!(
            "{:>4}  {:<8} {:>9} {:>9} {:>9} {:>7} {:>5} {:>7} {:>8} {:>9}\n",
            i + 1,
            t.stop,
            t.input,
            t.cache_read,
            t.cache_write,
            t.output,
            t.cells,
            t.effects,
            t.llm_ms,
            cost
        ));
        tot.input += t.input;
        tot.cache_read += t.cache_read;
        tot.cache_write += t.cache_write;
        tot.output += t.output;
        tot.cells += t.cells;
        tot.effects += t.effects;
        tot.llm_ms += t.llm_ms;
    }
    let total_cost = price
        .as_ref()
        .map(|p| format!("{:.4}", tot.cost_usd(p)))
        .unwrap_or_else(|| "-".into());
    out.push_str(&format!(
        "{:>4}  {:<8} {:>9} {:>9} {:>9} {:>7} {:>5} {:>7} {:>8} {:>9}\n",
        "tot",
        "",
        tot.input,
        tot.cache_read,
        tot.cache_write,
        tot.output,
        tot.cells,
        tot.effects,
        tot.llm_ms,
        total_cost
    ));
    if price.is_none() && !model.is_empty() {
        out.push_str(&format!(
            "\n(no pricing for {model} — add it to model_pricing.json)\n"
        ));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn split_ui_context_peels_the_now_locator() {
        let (msg, ui) = split_ui_context(
            "hey\n\n[now: Thu 2026-07-16 15:34 UTC | user's view — space: sp1, object: ob1]",
        );
        assert_eq!(msg, "hey");
        assert_eq!(
            ui,
            Some("[now: Thu 2026-07-16 15:34 UTC | user's view — space: sp1, object: ob1]")
        );
        // no suffix → whole text is the message, no locator
        assert_eq!(split_ui_context("just a message"), ("just a message", None));
    }

    /// A minimal complete run: header, one turn (llm.chat span with its
    /// provider http call), terminal cell record.
    fn one_turn_trace(ok: bool) -> Vec<Value> {
        vec![
            json!({"kind": "header", "schema": 2,
                   "run": {"id": "run_abc", "program": "toolcaller@v1", "host": "rust"}}),
            json!({"kind": "span", "seq": 1, "phase": "begin", "span": "sp1",
                   "parent": null, "name": "llm.chat", "cell": null,
                   "input": {}, "key": "k1"}),
            json!({"kind": "effect", "seq": 2, "effect": "http.post", "cell": null,
                   "span": "sp1", "key": "k2", "error": null,
                   "meta": {"class": "read", "durMs": 40},
                   "input": {"url": "https://api.anthropic.com/v1/messages",
                             "json": {"messages": [
                               {"role": "user", "content": [
                                 {"type": "text", "text": "what's the weather in Berlin?"}]}]}},
                   "output": {"status": 200,
                              "body": "{\"content\":[],\"usage\":{},\"stop_reason\":\"end_turn\"}"}}),
            json!({"kind": "span", "seq": 3, "phase": "end", "span": "sp1",
                   "name": "llm.chat", "cell": null, "ok": true,
                   "output": null, "error": null, "meta": {"durMs": 41}}),
            json!({"kind": "cell", "seq": 4, "cell": "main", "ok": ok,
                   "error": null, "interrupted": false,
                   "metrics": {"duration_ms": 5230, "fuel_used": 1}}),
        ]
    }

    /// Two turns with usage + a cell between them: the table attributes
    /// the cell and its effect to turn 1 and prices from the recorded
    /// model (claude-sonnet-5: $3/$15, cache $0.30/$3.75 per MTok).
    fn two_turn_trace() -> Vec<Value> {
        vec![
            json!({"kind": "header", "schema": 2,
                   "run": {"id": "run_st", "program": "toolcaller@v1", "host": "rust"}}),
            json!({"kind": "span", "seq": 1, "phase": "begin", "span": "t1",
                   "parent": null, "name": "llm.chat", "input": {}}),
            json!({"kind": "effect", "seq": 2, "effect": "http.post", "span": "t1",
                   "error": null, "meta": {"class": "read", "durMs": 40},
                   "input": {"url": "https://api.anthropic.com/v1/messages",
                             "json": {"model": "claude-sonnet-5", "messages": []}},
                   "output": {"status": 200, "body": "{}"}}),
            json!({"kind": "span", "seq": 3, "phase": "end", "span": "t1",
                   "name": "llm.chat", "ok": true, "error": null,
                   "meta": {"durMs": 1000},
                   "output": {"parts": [], "stop": "tool",
                              "usage": {"in": 2, "out": 300,
                                        "cacheRead": 0, "cacheWrite": 1_000_000}}}),
            json!({"kind": "span", "seq": 4, "phase": "begin", "span": "c1",
                   "parent": null, "name": "cell", "input": {}}),
            json!({"kind": "effect", "seq": 5, "effect": "http.get", "span": "c1",
                   "error": null, "meta": {"class": "read", "durMs": 5},
                   "input": {}, "output": {}}),
            json!({"kind": "span", "seq": 6, "phase": "end", "span": "c1",
                   "name": "cell", "ok": true, "error": null, "meta": {"durMs": 7},
                   "output": {}}),
            json!({"kind": "span", "seq": 7, "phase": "begin", "span": "t2",
                   "parent": null, "name": "llm.chat", "input": {}}),
            json!({"kind": "span", "seq": 8, "phase": "end", "span": "t2",
                   "name": "llm.chat", "ok": true, "error": null,
                   "meta": {"durMs": 900},
                   "output": {"parts": [], "stop": "done",
                              "usage": {"in": 2, "out": 100,
                                        "cacheRead": 1_000_000, "cacheWrite": 0}}}),
            json!({"kind": "cell", "seq": 9, "cell": "main", "ok": true,
                   "error": null, "interrupted": false,
                   "metrics": {"duration_ms": 2500, "fuel_used": 42}}),
        ]
    }

    #[test]
    fn clip_loc_tail_is_a_locator() {
        assert_eq!(clip_loc("short", 10), "short");
        let out = clip_loc(&"x".repeat(30), 10);
        assert_eq!(out, format!("{}… (+20 chars — --full)", "x".repeat(10)));
    }

    #[test]
    fn tool_results_keep_first_occurrence_seq() {
        let req = |seq: i64| {
            json!({"kind": "effect", "seq": seq, "effect": "http.post",
                   "error": null, "meta": {},
                   "input": {"json": {"messages": [
                       {"role": "user", "content": [
                           {"type": "tool_result", "tool_use_id": "t1",
                            "content": "42"}]}]}},
                   "output": {}})
        };
        let map = tool_results(&[req(7), req(20)]);
        assert_eq!(map["t1"], ("42".into(), false, 7));
    }

    #[test]
    fn stats_table_per_turn_attribution_and_pricing() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("run_st.jsonl");
        let lines: Vec<String> = two_turn_trace().iter().map(|r| r.to_string()).collect();
        std::fs::write(&path, lines.join("\n") + "\n").unwrap();
        let out = stats(&path).unwrap();
        assert!(out.contains("claude-sonnet-5"), "{out}");
        // turn 1: the cell + its effect + the llm http effect belong to it
        let t1 = out
            .lines()
            .find(|l| l.trim_start().starts_with("1 "))
            .unwrap();
        assert!(t1.contains("tool"), "{t1}");
        // 1M cacheWrite tokens at $3.75/MTok + 300 out at $15/MTok ≈ 3.7545
        assert!(t1.contains("3.7545"), "{t1}");
        let t2 = out
            .lines()
            .find(|l| l.trim_start().starts_with("2 "))
            .unwrap();
        // 1M cacheRead at $0.30/MTok + 100 out ≈ 0.3015
        assert!(t2.contains("0.3015"), "{t2}");
        let tot = out
            .lines()
            .find(|l| l.trim_start().starts_with("tot"))
            .unwrap();
        assert!(tot.contains("4.0560"), "{tot}");
        assert!(out.contains("2.5s wall"), "{out}");
    }

    #[test]
    fn stats_unknown_model_renders_dash_cost() {
        let mut records = two_turn_trace();
        records[2]["input"]["json"]["model"] = json!("thinkingmachines/Inkling");
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("run_x.jsonl");
        let lines: Vec<String> = records.iter().map(|r| r.to_string()).collect();
        std::fs::write(&path, lines.join("\n") + "\n").unwrap();
        let out = stats(&path).unwrap();
        assert!(
            out.contains("no pricing for thinkingmachines/Inkling"),
            "{out}"
        );
        assert!(out
            .lines()
            .any(|l| l.trim_start().starts_with("1 ") && l.contains('-')));
    }

    #[test]
    fn ls_row_titles_by_turn_1_user_text() {
        let row = ls_row(&one_turn_trace(true));
        assert_eq!(row.id, "run_abc");
        assert_eq!(row.program, "toolcaller@v1");
        assert_eq!(row.status, "ok");
        assert_eq!(row.dur, "5.2s");
        assert_eq!(row.turns, 1);
        assert_eq!(row.title, "what's the weather in Berlin?");
    }

    #[test]
    fn ls_row_failed_run() {
        assert_eq!(ls_row(&one_turn_trace(false)).status, "FAILED");
    }

    #[test]
    fn ls_row_incomplete_without_terminal_cell() {
        let mut records = one_turn_trace(true);
        records.pop();
        let row = ls_row(&records);
        assert_eq!(row.status, "incomplete");
        assert_eq!(row.dur, "?");
    }

    #[test]
    fn ls_row_turnless_run_has_no_title() {
        let records = vec![
            json!({"kind": "header", "schema": 2,
                   "run": {"id": "run_x", "program": "decay@v1", "host": "rust"}}),
            json!({"kind": "cell", "seq": 1, "cell": "main", "ok": true,
                   "error": null, "interrupted": false,
                   "metrics": {"duration_ms": 100, "fuel_used": 1}}),
        ];
        let row = ls_row(&records);
        assert_eq!(row.turns, 0);
        assert_eq!(row.title, "");
    }
}
