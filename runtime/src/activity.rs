//! Run activity stream (ADR-034): the live `bao.run.*` event family
//! the chat UI renders. The broker hands every span and effect it
//! observes to a per-run [`RunEmitter`]; the emitter builds frames and
//! pushes them on a bounded [`ActivityQueue`]; one serve thread (the
//! [`Poster`]) merges step frames and posts them on the account bus.
//!
//! The stream is a second reader of the observations the trace writer
//! gets. Emitting is not an effect: nothing here is recorded, and a
//! broker without an emitter (replay, `anyrt run`) behaves the same.

use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, VecDeque};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

// --- frame types (ADR-034 §1) -------------------------------------------------

pub const STARTED: &str = "bao.run.started";
pub const CELL: &str = "bao.run.cell";
pub const STEPS: &str = "bao.run.steps";
pub const CELL_END: &str = "bao.run.cell_end";
pub const TURN: &str = "bao.run.turn";
pub const ENDED: &str = "bao.run.ended";

// --- cadence and size (ADR-034 §4) --------------------------------------------

/// At most one `steps` frame per run in this window.
pub const STEPS_INTERVAL: Duration = Duration::from_millis(250);
/// Frames waiting for the poster; a full queue drops its oldest
/// `steps` frame (the trace has the records).
pub const QUEUE_CAP: usize = 512;
pub const CODE_MAX: usize = 16 * 1024;
pub const INPUT_MAX: usize = 2 * 1024;
pub const DIGEST_MAX: usize = 4 * 1024;
/// A merged `steps` array stays under this; the bus cap is 64 KiB for
/// the whole `data`.
pub const STEPS_FRAME_MAX: usize = 48 * 1024;
const VALUE_MAX: usize = 256;
const MESSAGE_MAX: usize = 1024;
const CLIP_MARK: &str = "…clipped";

// --- the vocabulary (ADR-034 §3) ----------------------------------------------

/// The closed category set (the designers' page).
pub const CATEGORIES: [&str; 9] = [
    "thinking", "lookup", "search", "memory", "change", "service", "web", "command", "self",
];

/// Bookkeeping, not work done: steps with these names are
/// `visibility: hidden`. The toolcaller's digest skips the same set
/// (`_NOT_WORK` in `toolcaller@v1.py`; a test keeps the two equal).
pub const HIDDEN: [&str; 5] = [
    "time.now",
    "module.resolve",
    "env.get",
    "config.get",
    "runtime.get",
];
pub const HIDDEN_PREFIXES: [&str; 2] = ["trace.", "span."];

pub fn is_hidden(name: &str) -> bool {
    HIDDEN.contains(&name) || HIDDEN_PREFIXES.iter().any(|p| name.starts_with(p))
}

const THINKING: [&str; 3] = ["llm.", "subagent.", "deepResearch."];
const SEARCH: [&str; 2] = ["any.search", "any.list_search_scopes"];
const WEB: [&str; 2] = ["webSearch.", "search.provider."];
const MEMORY: [&str; 4] = ["recall.", "memory.", "history.", "autorecall."];
const COMMAND: [&str; 2] = ["sh.", "fs."];
const SELF_PREFIXES: [&str; 9] = [
    "progress.",
    "chain.",
    "status.",
    "config.",
    "programs.",
    "applet.",
    "oauth.",
    "blob.",
    "ui.",
];
const SELF_NAMES: [&str; 5] = [
    "bao.status",
    "chat.send",
    "fuel.state",
    "mailbox.drain",
    "kernel.boot",
];

fn starts_with_any(name: &str, prefixes: &[&str]) -> bool {
    prefixes.iter().any(|p| name.starts_with(p))
}

/// What a step says to a person, computed on the host from the call's
/// name, its boundary class and its input.
#[derive(Debug, Clone, PartialEq)]
pub struct Described {
    pub category: &'static str,
    pub label: String,
    pub refs: Map<String, Value>,
    pub hidden: bool,
    /// the module of a `service` step (`linear`, `granola`)
    pub service: Option<String>,
}

/// Layer 1 (prefix rules) + layer 3 (the fallback label). `label` is
/// layer 2: the template the method's author wrote on `@span`; it is
/// forwarded verbatim. `http_any` says a bare `http.*` call targets
/// the any server (the broker's classifier knows).
pub fn describe(
    name: &str,
    class: &str,
    input: &Value,
    http_any: bool,
    label: Option<&str>,
) -> Described {
    let mut refs = refs_of(input);
    if let Some(l) = label {
        label_refs(l, input, &mut refs);
    }
    let hidden = is_hidden(name);
    let by_class = if class == "mutate" {
        "change"
    } else {
        "lookup"
    };
    let mut service = None;
    let category = if hidden {
        "self"
    } else if starts_with_any(name, &THINKING) {
        "thinking"
    } else if SEARCH.contains(&name) {
        "search"
    } else if starts_with_any(name, &WEB) {
        "web"
    } else if starts_with_any(name, &MEMORY) {
        "memory"
    } else if name.starts_with("any.") {
        by_class
    } else if starts_with_any(name, &COMMAND) || name == "bash" {
        "command"
    } else if starts_with_any(name, &SELF_PREFIXES) || SELF_NAMES.contains(&name) {
        "self"
    } else if name.starts_with("http.") {
        if http_any {
            by_class
        } else {
            "web"
        }
    } else {
        service = name.split_once('.').map(|(m, _)| m.to_string());
        "service"
    };
    let label = match label {
        Some(l) if !l.trim().is_empty() => l.to_string(),
        _ => fallback_label(name, category, class, &refs, service.as_deref()),
    };
    Described {
        category,
        label,
        refs,
        hidden,
        service,
    }
}

/// Refs by argument NAME, one rule set for every method (ADR-034 §3).
/// The host never resolves a title: the client substitutes names from
/// its own store.
pub fn refs_of(input: &Value) -> Map<String, Value> {
    const RULES: [(&str, &[&str]); 6] = [
        ("space", &["spaceConfig", "space"]),
        ("object", &["object_id", "objectId", "id"]),
        ("type", &["type"]),
        ("query", &["query", "q"]),
        ("domain", &["url"]),
        ("command", &["command"]),
    ];
    let mut refs = Map::new();
    let Some(obj) = input.as_object() else {
        return refs;
    };
    for (key, args) in RULES {
        let found = args.iter().find_map(|a| match obj.get(*a) {
            Some(Value::String(s)) if !s.is_empty() => Some(s.as_str()),
            // a `list_spaces()` row stands for its id; the bound
            // `baoSpace` global is `{spaceId, chatId}`
            Some(Value::Object(o)) => ["id", "spaceId"]
                .iter()
                .find_map(|k| o.get(*k).and_then(Value::as_str)),
            _ => None,
        });
        let Some(text) = found else { continue };
        let text = if key == "domain" {
            domain_of(text)
        } else {
            text
        };
        if !text.is_empty() {
            refs.insert(key.into(), json!(clip_text(text, VALUE_MAX)));
        }
    }
    refs
}

/// The values a label's placeholders name (ADR-034 §3 layer 2): a
/// placeholder is an argument of the method, a dotted path into one
/// (`{body.name}`), or alternatives (`{body.name|title}`). Each found scalar becomes a ref under the
/// placeholder's own text, so the client fills the template from the
/// refs alone. The derived refs (`space`, `object`, …) keep their rule.
pub fn label_refs(label: &str, input: &Value, refs: &mut Map<String, Value>) {
    let mut rest = label;
    while let Some(open) = rest.find('{') {
        let Some(len) = rest[open..].find('}') else {
            break;
        };
        let key = &rest[open + 1..open + len];
        rest = &rest[open + len + 1..];
        if key.is_empty() || key == "n" || refs.contains_key(key) {
            continue;
        }
        // alternatives (`{body.name|title}`): the first path with a value
        let found = key.split('|').find_map(|path| match lookup(input, path) {
            Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
            Some(v @ (Value::Number(_) | Value::Bool(_))) => Some(v.to_string()),
            _ => None,
        });
        let Some(text) = found else { continue };
        refs.insert(key.into(), json!(clip_text(&text, VALUE_MAX)));
    }
}

/// `path` inside `value`, dots as steps. A key may itself hold a dot
/// (`any.type`), so each step takes the longest key that exists.
fn lookup<'a>(value: &'a Value, path: &str) -> Option<&'a Value> {
    if path.is_empty() {
        return Some(value);
    }
    let obj = value.as_object()?;
    let mut end = path.len();
    loop {
        if let Some(child) = obj.get(&path[..end]) {
            let rest = path[end..].strip_prefix('.').unwrap_or("");
            if let Some(found) = lookup(child, rest) {
                return Some(found);
            }
        }
        end = path[..end].rfind('.')?;
    }
}

fn domain_of(url: &str) -> &str {
    let rest = url.split_once("://").map_or(url, |(_, r)| r);
    let netloc = rest.split(['/', '?', '#']).next().unwrap_or("");
    let host = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    host.split(':').next().unwrap_or("")
}

/// `googleCalendar` / `send_message` → `Google calendar` / `Send message`.
fn humanize(ident: &str) -> String {
    let mut out = String::new();
    for (i, ch) in ident.chars().enumerate() {
        if ch == '_' || ch == '-' {
            out.push(' ');
        } else if ch.is_uppercase() && i > 0 {
            out.push(' ');
            out.extend(ch.to_lowercase());
        } else if i == 0 {
            out.extend(ch.to_uppercase());
        } else {
            out.push(ch);
        }
    }
    out
}

/// The method's leading word: `get_note` / `getNote` → `get`.
fn first_word(method: &str) -> &str {
    let end = method
        .char_indices()
        .find(|(i, c)| *c == '_' || (*i > 0 && c.is_uppercase()))
        .map_or(method.len(), |(i, _)| i);
    &method[..end]
}

fn verb_of(method: &str) -> Option<&'static str> {
    Some(match first_word(method) {
        "list" | "get" | "search" | "query" | "read" | "find" | "one" | "whoami" | "me" => {
            "Reading"
        }
        "create" | "add" | "put" | "append" => "Creating",
        "update" | "set" | "edit" | "patch" | "modify" | "move" | "upsert" | "mark" => "Updating",
        "delete" | "remove" | "trash" | "cancel" => "Deleting",
        _ => return None,
    })
}

/// Layer 3: `"{verb} {where}"`. A bare effect has no method to read a
/// verb from, so `http.*` and commands get their own generic lines.
fn fallback_label(
    name: &str,
    category: &str,
    class: &str,
    refs: &Map<String, Value>,
    service: Option<&str>,
) -> String {
    if category == "thinking" {
        return "Thinking".into();
    }
    if category == "command" {
        return if refs.contains_key("command") {
            "Running {command}".into()
        } else {
            "Running a command".into()
        };
    }
    if name.starts_with("http.") {
        let verb = if class == "mutate" {
            if category == "web" {
                "Sending to"
            } else {
                "Updating"
            }
        } else {
            "Reading"
        };
        return if category == "web" && refs.contains_key("domain") {
            format!("{verb} {{domain}}")
        } else if category == "web" {
            if class == "mutate" {
                "Sending a request"
            } else {
                "Reading the web"
            }
            .into()
        } else {
            verb.to_string()
        };
    }
    let method = name.rsplit_once('.').map_or(name, |(_, m)| m);
    let verb = verb_of(method).map_or_else(|| humanize(method), String::from);
    // say what exactly, when the arguments name it
    let verb = if first_word(method) == "search" && refs.contains_key("query") {
        "Searching for {query}".to_string()
    } else if refs.contains_key("object") {
        format!("{verb} {{object}}")
    } else {
        verb
    };
    if refs.contains_key("space") {
        format!("{verb} in {{space}}")
    } else if let Some(s) = service {
        format!("{verb} in {}", humanize(s))
    } else {
        verb
    }
}

// --- clipping (ADR-034 §4) ----------------------------------------------------

/// `text` cut to `max` bytes on a char boundary, marked when cut.
pub fn clip_text(text: &str, max: usize) -> String {
    if text.len() <= max {
        return text.to_string();
    }
    let mut end = max.saturating_sub(CLIP_MARK.len());
    while end > 0 && !text.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}{CLIP_MARK}", &text[..end])
}

/// A recorded input for a step: whole when small; else each large
/// value becomes its marked head; else the marked head of the whole.
pub fn clip_input(input: &Value) -> Value {
    let whole = input.to_string();
    if whole.len() <= INPUT_MAX {
        return input.clone();
    }
    if let Some(obj) = input.as_object() {
        let slim: Map<String, Value> = obj
            .iter()
            .map(|(k, v)| {
                let text = match v {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                };
                let v = if text.len() > VALUE_MAX {
                    json!(clip_text(&text, VALUE_MAX))
                } else {
                    v.clone()
                };
                (k.clone(), v)
            })
            .collect();
        let slim = Value::Object(slim);
        if slim.to_string().len() <= INPUT_MAX {
            return slim;
        }
    }
    json!(clip_text(&whole, INPUT_MAX))
}

fn clip_error(error: Option<&Value>) -> Value {
    match error.filter(|e| e.is_object()) {
        Some(e) => json!({
            "type": e["type"],
            "message": clip_text(e["message"].as_str().unwrap_or(""), MESSAGE_MAX),
        }),
        None => Value::Null,
    }
}

// --- frames and the queue -----------------------------------------------------

#[derive(Debug, Clone)]
pub struct Frame {
    pub typ: &'static str,
    pub run: String,
    /// the cell a `steps` frame belongs to (the merge key)
    pub cell: Option<String>,
    pub data: Value,
}

impl Frame {
    /// The bus envelope (any doc 21): account scope, target = run id.
    pub fn envelope(&self) -> Value {
        json!({"type": self.typ, "scope": "account", "target": self.run, "data": self.data})
    }
}

/// The hand-off between the run threads and the poster. `push` never
/// blocks and never fails: emission must not slow a run.
#[derive(Default)]
pub struct ActivityQueue {
    q: Mutex<VecDeque<Frame>>,
    cv: Condvar,
}

impl ActivityQueue {
    pub fn push(&self, frame: Frame) {
        let mut q = self.q.lock().expect("activity queue lock poisoned");
        if q.len() >= QUEUE_CAP {
            // only step frames are droppable: the trace has the records;
            // a lost cell/cell_end/started/ended would break the view
            if let Some(i) = q.iter().position(|f| f.typ == STEPS) {
                q.remove(i);
            }
        }
        q.push_back(frame);
        drop(q);
        self.cv.notify_one();
    }

    /// The oldest frame, waiting up to `wait` for one.
    pub fn pop(&self, wait: Duration) -> Option<Frame> {
        let mut q = self.q.lock().expect("activity queue lock poisoned");
        if q.is_empty() {
            q = self
                .cv
                .wait_timeout(q, wait)
                .expect("activity queue lock poisoned")
                .0;
        }
        q.pop_front()
    }

    /// Merge into `head` (a `steps` frame) the step frames queued
    /// behind it for the same run and cell, up to the first other
    /// frame of that run or the size cap. A later entry for the same
    /// step id replaces the earlier one (`done` over `running`); the
    /// newest descriptor and budget win.
    pub fn absorb(&self, head: &mut Frame) {
        let mut q = self.q.lock().expect("activity queue lock poisoned");
        let mut size = head.data["steps"].to_string().len();
        let mut i = 0;
        while i < q.len() {
            if q[i].run != head.run {
                i += 1;
                continue;
            }
            if q[i].typ != STEPS || q[i].cell != head.cell {
                break;
            }
            let add = q[i].data["steps"].to_string().len();
            if size + add > STEPS_FRAME_MAX {
                break;
            }
            let mut next = q.remove(i).expect("index checked");
            size += add;
            let steps = head.data["steps"]
                .as_array_mut()
                .expect("a steps frame carries steps");
            for step in next.data["steps"].as_array_mut().into_iter().flatten() {
                let step = step.take();
                match steps.iter_mut().find(|s| s["id"] == step["id"]) {
                    Some(slot) => *slot = step,
                    None => steps.push(step),
                }
            }
            head.data["run"] = next.data["run"].take();
            head.data["budget"] = next.data["budget"].take();
        }
    }

    pub fn len(&self) -> usize {
        self.q.lock().expect("activity queue lock poisoned").len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// The poster's pacing state: one `steps` frame per run per
/// [`STEPS_INTERVAL`], everything else in queue order.
#[derive(Default)]
pub struct Poster {
    last_steps: BTreeMap<String, Instant>,
}

impl Poster {
    /// The next frame to post, or None when `wait` passed with an
    /// empty queue. A `steps` frame waits out its run's window, then
    /// takes with it every step frame that queued meanwhile.
    pub fn next(&mut self, queue: &ActivityQueue, wait: Duration) -> Option<Frame> {
        let mut frame = queue.pop(wait)?;
        if frame.typ == STEPS {
            if let Some(last) = self.last_steps.get(&frame.run) {
                let since = last.elapsed();
                if since < STEPS_INTERVAL {
                    std::thread::sleep(STEPS_INTERVAL - since);
                }
            }
            queue.absorb(&mut frame);
            self.last_steps.insert(frame.run.clone(), Instant::now());
        } else if frame.typ == ENDED {
            self.last_steps.remove(&frame.run);
        }
        Some(frame)
    }
}

// --- the per-run emitter ------------------------------------------------------

/// The run descriptor folded into every frame (ADR-034 §1).
pub fn descriptor(
    run_id: &str,
    title: &str,
    started_at: f64,
    program: &str,
    chat: Option<(&str, &str)>,
    trigger: Option<&str>,
    device: Option<&str>,
) -> Value {
    json!({
        "id": run_id, "title": title, "startedAt": started_at, "program": program,
        "chat": chat.map(|(space, object)| json!({"space": space, "object": object})),
        // what fired a run nobody typed: the trigger's id (ADR-023 §8)
        "trigger": trigger,
        "device": device,
    })
}

struct ActiveCell {
    span: String,
    id: String,
    /// shown steps by name — the `cell_end` counts
    counts: BTreeMap<String, u64>,
    /// fuel left when the cell began — `cell_end.fuelUsed` is the drop
    fuel_left: u64,
}

struct OpenStep {
    id: String,
    name: String,
    input: Value,
    kind: Option<String>,
    label: Option<String>,
}

/// One run's emitter, owned by the run's Broker. Every method is
/// cheap and infallible; frames leave through the queue.
pub struct RunEmitter {
    queue: Arc<ActivityQueue>,
    run: Value,
    run_id: String,
    /// remaining fuel, refreshed by the runner's epoch callback
    fuel: Arc<AtomicU64>,
    fuel_limit: u64,
    t0: Instant,
    wall_limit_s: f64,
    turns: u64,
    turn_limit: Option<u64>,
    tokens: [u64; 3],
    cells: u64,
    cell: Option<ActiveCell>,
    /// open child spans of the active cell, by span id
    open: BTreeMap<String, OpenStep>,
    step_n: u64,
}

impl RunEmitter {
    pub fn new(
        queue: Arc<ActivityQueue>,
        run: Value,
        fuel: Arc<AtomicU64>,
        fuel_limit: u64,
        wall_limit_s: f64,
        turn_limit: Option<u64>,
    ) -> Self {
        let run_id = run["id"].as_str().unwrap_or("").to_string();
        RunEmitter {
            queue,
            run,
            run_id,
            fuel,
            fuel_limit,
            t0: Instant::now(),
            wall_limit_s,
            turns: 0,
            turn_limit,
            tokens: [0; 3],
            cells: 0,
            cell: None,
            open: BTreeMap::new(),
            step_n: 0,
        }
    }

    fn budget(&self) -> Value {
        let left = self.fuel.load(Ordering::Relaxed);
        let mut turns = json!({"n": self.turns});
        if let Some(limit) = self.turn_limit {
            turns["limit"] = json!(limit);
        }
        json!({
            "fuel": {"used": self.fuel_limit.saturating_sub(left), "limit": self.fuel_limit},
            "wall": {"elapsedS": (self.t0.elapsed().as_secs_f64() * 10.0).round() / 10.0,
                     "limitS": self.wall_limit_s},
            "turns": turns,
            "tokens": {"in": self.tokens[0], "out": self.tokens[1], "cacheRead": self.tokens[2]},
        })
    }

    fn emit(&self, typ: &'static str, cell: Option<&str>, extra: Value) {
        let mut data = json!({"run": self.run, "budget": self.budget()});
        if let (Some(d), Value::Object(extra)) = (data.as_object_mut(), extra) {
            d.extend(extra);
        }
        self.queue.push(Frame {
            typ,
            run: self.run_id.clone(),
            cell: cell.map(String::from),
            data,
        });
    }

    fn emit_step(&mut self, step: Value) {
        let Some(cell) = &self.cell else { return };
        let id = cell.id.clone();
        self.emit(
            STEPS,
            Some(&id),
            json!({"cell": {"id": id}, "steps": [step]}),
        );
    }

    /// The run descriptor this emitter folds into every frame.
    pub fn run(&self) -> &Value {
        &self.run
    }

    pub fn started(&self) {
        self.emit(STARTED, None, json!({}));
    }

    /// Is `span` (the innermost open span of a call) the active cell —
    /// i.e. is the call an immediate child of the cell?
    pub fn in_cell(&self, span: Option<&str>) -> bool {
        matches!((&self.cell, span), (Some(c), Some(s)) if c.span == s)
    }

    /// A span began. The outermost `cell`/`bash` span opens the cell
    /// frame; its immediate children open `running` steps. Deeper
    /// spans are the trace's expand-on-demand level.
    pub fn span_begin(
        &mut self,
        sid: &str,
        name: &str,
        kind: Option<&str>,
        label: Option<&str>,
        parent: Option<&str>,
        input: &Value,
    ) {
        if self.cell.is_none() {
            if name != "cell" && name != "bash" {
                return;
            }
            self.cells += 1;
            let id = input["cell"].as_str().unwrap_or(sid).to_string();
            let mut cell = json!({"id": id});
            for key in ["turn", "preview"] {
                if !input[key].is_null() {
                    cell[key] = input[key].clone();
                }
            }
            for (key, max) in [("code", CODE_MAX), ("command", INPUT_MAX)] {
                if let Some(text) = input[key].as_str() {
                    cell[key] = json!(clip_text(text, max));
                }
            }
            self.cell = Some(ActiveCell {
                span: sid.to_string(),
                id: id.clone(),
                counts: BTreeMap::new(),
                fuel_left: self.fuel.load(Ordering::Relaxed),
            });
            self.emit(CELL, Some(&id), json!({"cell": cell}));
            return;
        }
        if !self.in_cell(parent) {
            return;
        }
        let open = OpenStep {
            id: sid.to_string(),
            name: name.to_string(),
            input: clip_input(input),
            kind: kind.map(String::from),
            label: label.map(String::from),
        };
        if !is_hidden(name) {
            // the class is unknown until the span ends (it counts the
            // inner mutations); the author's kind stands in for the
            // category until then and carries no authority
            let hint = if kind == Some("mutator") {
                "mutate"
            } else {
                "read"
            };
            let d = describe(name, hint, input, false, label);
            let mut step = running_step(&open.id, name, &d, &open.input);
            if let Some(k) = kind {
                step["kind"] = json!(k);
            }
            self.emit_step(step);
        }
        self.open.insert(sid.to_string(), open);
    }

    /// A span ended; `seq` is its end record's seq (the trace handle).
    #[allow(clippy::too_many_arguments)]
    pub fn span_end(
        &mut self,
        sid: &str,
        name: &str,
        seq: i64,
        ok: bool,
        output: Option<&Value>,
        error: Option<&Value>,
        meta: &Value,
    ) {
        if name == "llm.chat" {
            let usage = output.map_or(&Value::Null, |o| &o["usage"]);
            for (slot, key) in ["in", "out", "cacheRead"].iter().enumerate() {
                self.tokens[slot] += usage[*key].as_u64().unwrap_or(0);
            }
            if self.cell.is_none() {
                // a model call of the loop itself is a turn; one made
                // by cell code is a step
                self.turns += 1;
                let stop = output.map_or(Value::Null, |o| o["stop"].clone());
                self.emit(
                    TURN,
                    None,
                    json!({"turn": {"n": self.turns, "stop": stop, "usage": usage}}),
                );
            }
        }
        if self.cell.as_ref().is_some_and(|c| c.span == sid) {
            let cell = self.cell.take().expect("checked");
            self.open.clear();
            let out = output.unwrap_or(&Value::Null);
            let error = clip_error(error);
            let mut end = json!({
                "id": cell.id, "ok": ok, "error": error,
                // a trapped cell never closes its span: the broker
                // force-closes it with this error (ADR-001 §4c)
                "interrupted": error["type"] == "unclosed_span",
                "counts": cell.counts,
                "mutations": meta["mutations"], "durMs": meta["durMs"],
                "fuelUsed": cell.fuel_left.saturating_sub(self.fuel.load(Ordering::Relaxed)),
            });
            if let Some(d) = out["digest"].as_str() {
                end["digest"] = json!(clip_text(d, DIGEST_MAX));
            }
            self.emit(CELL_END, Some(&cell.id), json!({"cell": end}));
            return;
        }
        let Some(open) = self.open.remove(sid) else {
            return;
        };
        let mutations = meta["mutations"].as_u64().unwrap_or(0);
        let class = if mutations > 0 { "mutate" } else { "read" };
        let label = meta["label"].as_str().or(open.label.as_deref());
        let d = describe(&open.name, class, &open.input, false, label);
        let mut step = done_step(&open.id, seq, &open.name, class, &d, &open.input, ok, error);
        step["durMs"] = meta["durMs"].clone();
        step["effects"] = meta["effects"].clone();
        step["mutations"] = json!(mutations);
        if let Some(k) = meta["kind"].as_str().or(open.kind.as_deref()) {
            step["kind"] = json!(k);
        }
        self.count(&open.name, &d);
        self.emit_step(step);
    }

    /// A bare effect of the cell is about to execute: a `running`
    /// step. Returns the step id for [`Self::effect_end`]; None when
    /// the call is not an immediate child of the cell.
    pub fn effect_begin(
        &mut self,
        span: Option<&str>,
        name: &str,
        class: &str,
        input: &Value,
        http_any: bool,
    ) -> Option<String> {
        if !self.in_cell(span) {
            return None;
        }
        self.step_n += 1;
        let id = format!("e{}", self.step_n);
        if !is_hidden(name) {
            let d = describe(name, class, input, http_any, None);
            let mut step = running_step(&id, name, &d, &clip_input(input));
            step["class"] = json!(class);
            self.emit_step(step);
        }
        Some(id)
    }

    /// A bare effect of the cell was recorded at `seq`. `id` is the
    /// begin's id; an effect that never executed (denied, mocked) has
    /// none and gets a fresh one.
    #[allow(clippy::too_many_arguments)]
    pub fn effect_end(
        &mut self,
        id: Option<String>,
        seq: i64,
        name: &str,
        class: &str,
        input: &Value,
        http_any: bool,
        error: Option<&Value>,
        meta: &Value,
    ) {
        let id = id.unwrap_or_else(|| {
            self.step_n += 1;
            format!("e{}", self.step_n)
        });
        let d = describe(name, class, input, http_any, None);
        let ok = error.is_none_or(|e| e.is_null());
        let mut step = done_step(&id, seq, name, class, &d, &clip_input(input), ok, error);
        if !meta["durMs"].is_null() {
            step["durMs"] = meta["durMs"].clone();
        }
        self.count(name, &d);
        self.emit_step(step);
    }

    fn count(&mut self, name: &str, d: &Described) {
        if d.hidden {
            return;
        }
        if let Some(cell) = self.cell.as_mut() {
            *cell.counts.entry(name.to_string()).or_insert(0) += 1;
        }
    }

    /// `run_program` returned. `stop` is the program's own stop word
    /// (`done | wrapup`); the user-facing stop reason stays on the
    /// final bubble (ADR-034 §5).
    pub fn ended(
        &self,
        status: &str,
        stop: Option<&str>,
        dur_ms: i64,
        fuel_used: u64,
        mutations: u64,
    ) {
        self.emit(
            ENDED,
            None,
            json!({"status": status, "stop": stop, "durMs": dur_ms, "fuelUsed": fuel_used,
                   "mutations": mutations, "cells": self.cells, "turns": self.turns}),
        );
    }
}

/// The terminal frame for a run that never produced an outcome (the
/// cage failed before the program ran): the client must still see the
/// run end.
pub fn ended_without_outcome(queue: &ActivityQueue, run: &Value) {
    queue.push(Frame {
        typ: ENDED,
        run: run["id"].as_str().unwrap_or("").to_string(),
        cell: None,
        data: json!({"run": run, "status": "error"}),
    });
}

fn step_base(id: &str, name: &str, d: &Described, input: &Value) -> Value {
    let mut step = json!({
        "id": id, "name": name, "category": d.category, "label": d.label,
        "refs": d.refs, "input": input,
        "visibility": if d.hidden { "hidden" } else { "shown" },
    });
    if let Some(s) = &d.service {
        step["service"] = json!(s);
    }
    step
}

fn running_step(id: &str, name: &str, d: &Described, input: &Value) -> Value {
    let mut step = step_base(id, name, d, input);
    step["state"] = json!("running");
    step
}

#[allow(clippy::too_many_arguments)]
fn done_step(
    id: &str,
    seq: i64,
    name: &str,
    class: &str,
    d: &Described,
    input: &Value,
    ok: bool,
    error: Option<&Value>,
) -> Value {
    let mut step = step_base(id, name, d, input);
    step["state"] = json!("done");
    step["seq"] = json!(seq);
    step["class"] = json!(class);
    step["ok"] = json!(ok);
    step["error"] = clip_error(error);
    step
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(name: &str, class: &str, input: Value) -> Described {
        describe(name, class, &input, false, None)
    }

    #[test]
    fn prefix_rules_put_every_known_name_in_a_category() {
        let cases = [
            ("llm.chat", "read", "thinking"),
            ("subagent.run", "read", "thinking"),
            ("deepResearch.run", "read", "thinking"),
            ("any.search", "read", "search"),
            ("any.list_search_scopes", "read", "search"),
            ("webSearch.search", "read", "web"),
            ("search.provider.brave", "read", "web"),
            ("recall.recall", "read", "memory"),
            ("memory.save", "mutate", "memory"),
            ("history.turns", "read", "memory"),
            ("autorecall.run", "read", "memory"),
            ("any.get_object", "read", "lookup"),
            ("any.update_object", "mutate", "change"),
            ("sh.run", "mutate", "command"),
            ("fs.read", "read", "command"),
            ("bash", "mutate", "command"),
            ("progress.start", "mutate", "self"),
            ("chain.run", "read", "self"),
            ("status.set", "mutate", "self"),
            ("config.set", "mutate", "self"),
            ("programs.save", "mutate", "self"),
            ("applet.create", "mutate", "self"),
            ("bao.status", "mutate", "self"),
            ("chat.send", "mutate", "self"),
            ("fuel.state", "read", "self"),
            ("oauth.connect", "mutate", "self"),
            ("blob.put", "read", "self"),
            ("linear.create_issue", "mutate", "service"),
            ("granola.get_note", "read", "service"),
        ];
        for (name, class, category) in cases {
            let got = d(name, class, json!({}));
            assert_eq!(got.category, category, "{name}");
            assert!(CATEGORIES.contains(&got.category));
            assert!(!got.hidden, "{name} is work done");
        }
        assert_eq!(
            d("linear.create_issue", "mutate", json!({}))
                .service
                .unwrap(),
            "linear"
        );
    }

    #[test]
    fn the_hidden_set_is_bookkeeping() {
        for name in [
            "time.now",
            "module.resolve",
            "env.get",
            "config.get",
            "runtime.get",
            "trace.effects_of",
            "trace.stats",
            "span.begin",
        ] {
            let got = d(name, "read", json!({}));
            assert!(got.hidden, "{name}");
            assert_eq!(got.category, "self");
        }
    }

    #[test]
    fn bare_http_is_the_any_server_or_the_web() {
        let input = json!({"url": "https://api.example.com:8443/v1/x?y=1"});
        let web = describe("http.get", "read", &input, false, None);
        assert_eq!(web.category, "web");
        assert_eq!(web.label, "Reading {domain}");
        assert_eq!(web.refs["domain"], "api.example.com");
        let post = describe("http.post", "mutate", &input, false, None);
        assert_eq!(post.label, "Sending to {domain}");
        let any = json!({"url": "http://127.0.0.1:7001/v1/spaces/s/objects"});
        assert_eq!(
            describe("http.get", "read", &any, true, None).category,
            "lookup"
        );
        let write = describe("http.post", "mutate", &any, true, None);
        assert_eq!(
            (write.category, write.label.as_str()),
            ("change", "Updating")
        );
    }

    #[test]
    fn fallback_labels_read_the_verb_and_the_place() {
        // a connector that does not exist yet renders on day one
        assert_eq!(
            d("foo.create_bar", "mutate", json!({})).label,
            "Creating in Foo"
        );
        assert_eq!(
            d("foo.list_bars", "read", json!({})).label,
            "Reading in Foo"
        );
        assert_eq!(
            d("granola.getNote", "read", json!({})).label,
            "Reading in Granola"
        );
        assert_eq!(
            d("googleCalendar.send_invite", "mutate", json!({})).label,
            "Send invite in Google calendar"
        );
        let upd = d(
            "any.update_object",
            "mutate",
            json!({"spaceConfig": "sp1", "object_id": "o1", "patch": {}}),
        );
        assert_eq!(upd.label, "Updating {object} in {space}");
        assert_eq!(
            d(
                "any.search",
                "read",
                json!({"space": "sp1", "query": "flaky"})
            )
            .label,
            "Searching for {query} in {space}"
        );
        assert_eq!(
            d("linear.search_issues", "read", json!({"q": "crash"})).label,
            "Searching for {query} in Linear"
        );
        assert_eq!(upd.refs["space"], "sp1");
        assert_eq!(upd.refs["object"], "o1");
        assert_eq!(
            d("any.delete_object", "mutate", json!({})).label,
            "Deleting"
        );
        assert_eq!(d("llm.chat", "read", json!({})).label, "Thinking");
        assert_eq!(
            d("sh.run", "mutate", json!({"command": "ls"})).label,
            "Running {command}"
        );
    }

    #[test]
    fn a_method_label_is_forwarded_verbatim() {
        let got = describe(
            "any.update_object",
            "mutate",
            &json!({"space": "sp1"}),
            false,
            Some("Updating {object} in {space}"),
        );
        assert_eq!(got.label, "Updating {object} in {space}");
    }

    #[test]
    fn a_label_names_the_methods_own_arguments() {
        // a connector says what exactly: its placeholders are its arguments
        let got = describe(
            "linear.create_issue",
            "mutate",
            &json!({"team_id": "t1", "title": "Crash on start", "priority": 2}),
            false,
            Some("Creating Linear issue “{title}” (P{priority}) {missing} {n}"),
        );
        assert_eq!(got.refs["title"], "Crash on start");
        assert_eq!(got.refs["priority"], "2");
        assert!(!got.refs.contains_key("missing") && !got.refs.contains_key("n"));
        // a dotted path reaches into an argument; a key may hold a dot
        let input = json!({
            "spaceConfig": "sp1",
            "body": {"type": "task", "initialProperties": {"any": {"name": "Buy milk"}}},
            "filter": {"any.type": "task"},
        });
        let got = describe(
            "any.create_object",
            "mutate",
            &input,
            false,
            Some("Creating {body.type} “{body.initialProperties.any.name}” in {space} {filter.any.type}"),
        );
        assert_eq!(got.refs["body.type"], "task");
        assert_eq!(got.refs["body.initialProperties.any.name"], "Buy milk");
        assert_eq!(got.refs["filter.any.type"], "task");
        let alt = describe("x.y", "read", &input, false, Some("{body.name|body.type}"));
        assert_eq!(alt.refs["body.name|body.type"], "task");
        assert_eq!(got.refs["space"], "sp1");
        // a long value is clipped, a structure is not a ref
        let got = describe(
            "x.y",
            "read",
            &json!({"a": "v".repeat(5000), "b": [1, 2]}),
            false,
            Some("{a} {b}"),
        );
        assert!(got.refs["a"].as_str().unwrap().len() <= 256);
        assert!(!got.refs.contains_key("b"));
    }

    #[test]
    fn refs_come_from_argument_names() {
        let refs = refs_of(&json!({
            "spaceConfig": {"id": "sp-row", "name": "Work"},
            "id": "obj1", "type": "task", "q": "flaky test",
            "url": "https://user:pw@example.org/path", "command": "make test",
            "other": "ignored",
        }));
        assert_eq!(refs["space"], "sp-row");
        assert_eq!(refs["object"], "obj1");
        assert_eq!(refs["type"], "task");
        assert_eq!(refs["query"], "flaky test");
        assert_eq!(refs["domain"], "example.org");
        assert_eq!(refs["command"], "make test");
        assert_eq!(refs.len(), 6);
        assert!(refs_of(&json!("not an object")).is_empty());
        // the bound space global the model passes most (`baoSpace`)
        let bound = refs_of(&json!({"spaceConfig": {"spaceId": "sp-bao", "chatId": "c1"}}));
        assert_eq!(bound["space"], "sp-bao");
    }

    #[test]
    fn clips_are_marked() {
        assert_eq!(clip_text("short", 10), "short");
        let cut = clip_text(&"é".repeat(100), 50);
        assert!(cut.len() <= 50 && cut.ends_with(CLIP_MARK));
        // a small input passes whole
        let small = json!({"a": 1});
        assert_eq!(clip_input(&small), small);
        // one large value is cut, the small ones stay
        let big = json!({"object_id": "o1", "body": "x".repeat(10_000)});
        let got = clip_input(&big);
        assert_eq!(got["object_id"], "o1");
        assert!(got["body"].as_str().unwrap().ends_with(CLIP_MARK));
        assert!(got.to_string().len() <= INPUT_MAX);
        // many values: the marked head of the whole
        let wide: Map<String, Value> = (0..200)
            .map(|i| (format!("k{i}"), json!("v".repeat(40))))
            .collect();
        let got = clip_input(&Value::Object(wide));
        assert!(got.as_str().unwrap().ends_with(CLIP_MARK));
        assert!(got.as_str().unwrap().len() <= INPUT_MAX);
    }

    fn emitter(queue: &Arc<ActivityQueue>) -> RunEmitter {
        let run = descriptor(
            "run_1",
            "fix the flaky test",
            1761200000.0,
            "agent:toolcaller@v1",
            Some(("sp", "chat1")),
            None,
            Some("peer1"),
        );
        RunEmitter::new(
            queue.clone(),
            run,
            Arc::new(AtomicU64::new(900)),
            1000,
            1200.0,
            Some(300),
        )
    }

    fn drain(queue: &ActivityQueue) -> Vec<Frame> {
        std::iter::from_fn(|| queue.pop(Duration::ZERO)).collect()
    }

    #[test]
    fn one_run_emits_the_frame_sequence_with_the_descriptor_on_every_frame() {
        let queue: Arc<ActivityQueue> = Default::default();
        let mut em = emitter(&queue);
        em.started();
        // turn 1: the loop's own model call
        em.span_begin("s1", "llm.chat", Some("getter"), None, None, &json!({}));
        em.span_end(
            "s1",
            "llm.chat",
            3,
            true,
            Some(&json!({"stop": "tool", "usage": {"in": 100, "out": 20, "cacheRead": 900}})),
            None,
            &json!({"durMs": 800, "effects": 1, "mutations": 0}),
        );
        // the cell
        let code = "any.update_object(sp, o, {})";
        em.span_begin(
            "s2",
            "cell",
            None,
            None,
            None,
            &json!({"cell": "c1", "turn": 1, "preview": code, "code": code}),
        );
        // a tool method: running at begin, done at end
        let input = json!({"spaceConfig": "sp1", "object_id": "o1", "patch": {"done": true}});
        em.span_begin(
            "s3",
            "any.update_object",
            Some("mutator"),
            None,
            Some("s2"),
            &input,
        );
        // its inner primitive is not a step
        assert_eq!(
            em.effect_begin(Some("s3"), "http.patch", "mutate", &json!({}), true),
            None
        );
        em.span_end(
            "s3",
            "any.update_object",
            9,
            true,
            None,
            None,
            &json!({"durMs": 12, "effects": 1, "mutations": 1, "kind": "mutator"}),
        );
        // a bare effect of the cell
        let get = json!({"url": "https://example.com/x"});
        let id = em.effect_begin(Some("s2"), "http.get", "read", &get, false);
        assert!(id.is_some());
        let fail = json!({"type": "HTTPError", "message": "503"});
        em.effect_end(
            id,
            11,
            "http.get",
            "read",
            &get,
            false,
            Some(&fail),
            &json!({"durMs": 40}),
        );
        // bookkeeping: no running entry, a hidden done entry, not counted
        let id = em.effect_begin(Some("s2"), "trace.effects_of", "read", &json!({}), false);
        em.effect_end(
            id,
            12,
            "trace.effects_of",
            "read",
            &json!({}),
            false,
            None,
            &json!({}),
        );
        em.span_end(
            "s2",
            "cell",
            13,
            true,
            Some(&json!({"digest": "Last value: 1"})),
            None,
            &json!({"durMs": 90, "effects": 4, "mutations": 1}),
        );
        em.ended("ok", Some("done"), 1500, 100, 1);

        let frames = drain(&queue);
        let types: Vec<&str> = frames.iter().map(|f| f.typ).collect();
        assert_eq!(
            types,
            [STARTED, TURN, CELL, STEPS, STEPS, STEPS, STEPS, STEPS, CELL_END, ENDED]
        );
        for f in &frames {
            // a late joiner materializes the run from any one frame
            assert_eq!(f.data["run"]["id"], "run_1", "{}", f.typ);
            assert_eq!(f.data["run"]["chat"]["object"], "chat1");
            assert_eq!(
                f.data["budget"]["fuel"],
                json!({"used": 100, "limit": 1000})
            );
            assert_eq!(f.data["budget"]["turns"]["limit"], 300);
            let env = f.envelope();
            assert_eq!(
                (env["scope"].as_str(), env["target"].as_str()),
                (Some("account"), Some("run_1"))
            );
        }
        assert_eq!(
            frames[1].data["turn"],
            json!({"n": 1, "stop": "tool",
            "usage": {"in": 100, "out": 20, "cacheRead": 900}})
        );
        assert_eq!(
            frames[2].data["budget"]["tokens"],
            json!({"in": 100, "out": 20, "cacheRead": 900})
        );
        assert_eq!(
            frames[2].data["cell"],
            json!({"id": "c1", "turn": 1, "preview": code, "code": code})
        );

        let step = |i: usize| frames[i].data["steps"][0].clone();
        let running = step(3);
        assert_eq!(running["state"], "running");
        assert_eq!(running["id"], "s3");
        assert_eq!(running["kind"], "mutator");
        assert_eq!(running["category"], "change"); // the kind's hint, until the end
        assert!(
            running.get("class").is_none(),
            "a span's class is unknown at begin"
        );
        let done = step(4);
        assert_eq!(done["id"], "s3"); // the client replaces by id
        assert_eq!(
            (done["state"].as_str(), done["seq"].as_i64()),
            (Some("done"), Some(9))
        );
        assert_eq!(done["class"], "mutate"); // boundary truth: inner mutations
        assert_eq!(done["label"], "Updating {object} in {space}");
        assert_eq!(done["refs"], json!({"space": "sp1", "object": "o1"}));
        assert_eq!(
            (done["ok"].as_bool(), done["mutations"].as_u64()),
            (Some(true), Some(1))
        );
        assert_eq!(step(5)["state"], "running");
        assert_eq!(step(5)["class"], "read"); // an effect's class is known at begin
        let failed = step(6);
        assert_eq!(failed["ok"], false);
        assert_eq!(failed["error"], fail);
        assert_eq!(failed["category"], "web");
        assert_eq!(step(7)["visibility"], "hidden");

        let end = &frames[8].data["cell"];
        assert_eq!(
            end["counts"],
            json!({"any.update_object": 1, "http.get": 1})
        );
        assert_eq!(end["digest"], "Last value: 1");
        assert_eq!(
            (end["fuelUsed"].as_u64(), end["mutations"].as_u64()),
            (Some(0), Some(1))
        );
        assert_eq!(end["interrupted"], false);
        let ended = &frames[9].data;
        assert_eq!(
            (ended["status"].as_str(), ended["stop"].as_str()),
            (Some("ok"), Some("done"))
        );
        assert_eq!(
            (ended["cells"].as_u64(), ended["turns"].as_u64()),
            (Some(1), Some(1))
        );
    }

    #[test]
    fn a_model_call_inside_a_cell_is_a_step_and_nested_cells_are_not_frames() {
        let queue: Arc<ActivityQueue> = Default::default();
        let mut em = emitter(&queue);
        em.span_begin("s1", "cell", None, None, None, &json!({"cell": "c1"}));
        em.span_begin(
            "s2",
            "subagent.run",
            Some("getter"),
            None,
            Some("s1"),
            &json!({}),
        );
        // the subagent's own loop: deeper than the cell's children
        em.span_begin("s3", "llm.chat", None, None, Some("s2"), &json!({}));
        em.span_end(
            "s3",
            "llm.chat",
            5,
            true,
            Some(&json!({"stop": "done", "usage": {"in": 7, "out": 3}})),
            None,
            &json!({}),
        );
        em.span_begin(
            "s4",
            "cell",
            None,
            None,
            Some("s2"),
            &json!({"cell": "inner"}),
        );
        em.span_end("s4", "cell", 6, true, None, None, &json!({}));
        em.span_end(
            "s2",
            "subagent.run",
            7,
            true,
            None,
            None,
            &json!({"mutations": 0}),
        );
        // a trapped cell: the broker force-closes the span
        let trap = json!({"type": "unclosed_span", "message": "cell c1 ended with span open"});
        em.span_end("s1", "cell", 8, false, None, Some(&trap), &json!({}));
        let frames = drain(&queue);
        let types: Vec<&str> = frames.iter().map(|f| f.typ).collect();
        assert_eq!(types, [CELL, STEPS, STEPS, CELL_END]);
        // the inner model call still counts toward the token budget
        assert_eq!(frames[3].data["budget"]["tokens"]["in"], 7);
        assert_eq!(frames[3].data["budget"]["turns"]["n"], 0);
        assert_eq!(frames[3].data["cell"]["interrupted"], true);
        assert_eq!(frames[2].data["steps"][0]["category"], "thinking");
    }

    #[test]
    fn a_large_cell_and_input_stay_under_the_bus_cap() {
        let queue: Arc<ActivityQueue> = Default::default();
        let mut em = emitter(&queue);
        let code = "x".repeat(200_000);
        em.span_begin(
            "s1",
            "cell",
            None,
            None,
            None,
            &json!({"cell": "c1", "code": code}),
        );
        let input = json!({"body": "y".repeat(500_000)});
        em.span_begin("s2", "any.create_object", None, None, Some("s1"), &input);
        em.span_end(
            "s2",
            "any.create_object",
            4,
            true,
            None,
            None,
            &json!({"mutations": 1}),
        );
        let digest = json!({"digest": "z".repeat(100_000)});
        em.span_end("s1", "cell", 5, true, Some(&digest), None, &json!({}));
        for f in drain(&queue) {
            assert!(f.envelope().to_string().len() < 64 * 1024, "{}", f.typ);
        }
    }

    fn steps_frame(run: &str, cell: &str, id: &str, state: &str) -> Frame {
        Frame {
            typ: STEPS,
            run: run.into(),
            cell: Some(cell.into()),
            data: json!({"run": {"id": run}, "budget": {"n": id},
                         "cell": {"id": cell}, "steps": [{"id": id, "state": state}]}),
        }
    }

    #[test]
    fn forty_mutations_post_as_at_most_three_step_frames() {
        let queue: Arc<ActivityQueue> = Default::default();
        let mut em = emitter(&queue);
        em.span_begin("c", "cell", None, None, None, &json!({"cell": "c1"}));
        for i in 0..40 {
            let sid = format!("s{i}");
            em.span_begin(
                &sid,
                "any.update_object",
                Some("mutator"),
                None,
                Some("c"),
                &json!({}),
            );
            em.span_end(
                &sid,
                "any.update_object",
                i,
                true,
                None,
                None,
                &json!({"mutations": 1}),
            );
        }
        em.span_end("c", "cell", 99, true, None, None, &json!({"mutations": 40}));
        let mut poster = Poster::default();
        let posted: Vec<Frame> =
            std::iter::from_fn(|| poster.next(&queue, Duration::ZERO)).collect();
        let step_frames: Vec<&Frame> = posted.iter().filter(|f| f.typ == STEPS).collect();
        assert!(step_frames.len() <= 3, "{} step frames", step_frames.len());
        // every step arrives once, as its final entry, before the cell ends
        let steps: Vec<&Value> = step_frames
            .iter()
            .flat_map(|f| f.data["steps"].as_array().unwrap())
            .collect();
        let done = steps.iter().filter(|s| s["state"] == "done").count();
        assert_eq!(done, 40);
        assert_eq!(posted.last().unwrap().typ, CELL_END);
        assert_eq!(
            posted.last().unwrap().data["cell"]["counts"]["any.update_object"],
            40
        );
    }

    #[test]
    fn absorb_merges_one_run_and_cell_and_stops_at_the_next_frame_kind() {
        let queue = ActivityQueue::default();
        queue.push(steps_frame("run_a", "c1", "s1", "done"));
        queue.push(steps_frame("run_b", "c9", "x1", "running")); // another run: skipped over
        queue.push(steps_frame("run_a", "c1", "s2", "running"));
        queue.push(Frame {
            typ: CELL_END,
            run: "run_a".into(),
            cell: Some("c1".into()),
            data: json!({}),
        });
        queue.push(steps_frame("run_a", "c2", "s3", "running")); // behind the cell end: stays
        let mut head = steps_frame("run_a", "c1", "s1", "running");
        queue.absorb(&mut head);
        assert_eq!(
            head.data["steps"],
            json!([{"id": "s1", "state": "done"}, {"id": "s2", "state": "running"}])
        );
        assert_eq!(head.data["budget"]["n"], "s2"); // the newest budget wins
        let rest: Vec<(&str, String)> = drain(&queue)
            .iter()
            .map(|f| (f.typ, f.run.clone()))
            .collect();
        assert_eq!(
            rest,
            [
                (STEPS, "run_b".to_string()),
                (CELL_END, "run_a".to_string()),
                (STEPS, "run_a".to_string())
            ]
        );
    }

    #[test]
    fn a_full_queue_drops_the_oldest_step_frame_only() {
        let queue = ActivityQueue::default();
        queue.push(Frame {
            typ: CELL,
            run: "r".into(),
            cell: None,
            data: json!({}),
        });
        for i in 0..QUEUE_CAP {
            queue.push(steps_frame("r", "c1", &format!("s{i}"), "done"));
        }
        assert_eq!(queue.len(), QUEUE_CAP);
        let frames = drain(&queue);
        assert_eq!(frames[0].typ, CELL); // structure survives
        assert_eq!(frames[1].data["steps"][0]["id"], "s1"); // s0 was dropped
    }

    #[test]
    fn a_step_frame_stops_growing_at_the_size_cap() {
        let queue = ActivityQueue::default();
        let big = "v".repeat(20 * 1024);
        for i in 0..4 {
            let mut f = steps_frame("r", "c1", &format!("s{i}"), "done");
            f.data["steps"][0]["input"] = json!(big);
            queue.push(f);
        }
        let mut head = queue.pop(Duration::ZERO).unwrap();
        queue.absorb(&mut head);
        assert_eq!(head.data["steps"].as_array().unwrap().len(), 2);
        assert!(head.envelope().to_string().len() < 64 * 1024);
        assert_eq!(queue.len(), 2);
    }
}
