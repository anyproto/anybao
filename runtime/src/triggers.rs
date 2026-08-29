//! Triggers (ADR-006 §4) — cron/event triggers that run programs;
//! single-owner, at-most-once, boot-disarmed, circuit breaker. The
//! scheduler core is pure over an injected clock; the store persists
//! trigger + run records as datasets on the anchor object.

use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::str::FromStr;

pub const DEFAULT_MAX_CONSECUTIVE_FAILURES: i64 = 3;

#[derive(Clone, Debug)]
pub struct Trigger {
    pub id: String,
    pub name: String,
    pub kind: String, // cron | event
    pub spec: Value,  // {"every_s": n} | {"cron": expr} | {dataset, filter?}
    pub program: String,
    pub args: Value,
    pub owner: String,
    pub enabled: bool,
    pub limits: Value,
    pub max_consecutive_failures: i64,
    // observability rollup
    pub last_run_at: Option<f64>,
    pub last_status: Option<String>,
    pub consecutive_failures: i64,
    pub next_due: Option<f64>,
}

impl Trigger {
    pub fn cron(
        id: &str,
        name: &str,
        every_s: f64,
        program: &str,
        args: Value,
        owner: &str,
        enabled: bool,
    ) -> Self {
        Trigger {
            id: id.into(),
            name: name.into(),
            kind: "cron".into(),
            spec: json!({"every_s": every_s}),
            program: program.into(),
            args,
            owner: owner.into(),
            enabled,
            limits: json!({}),
            max_consecutive_failures: DEFAULT_MAX_CONSECUTIVE_FAILURES,
            last_run_at: None,
            last_status: None,
            consecutive_failures: 0,
            next_due: None,
        }
    }
}

#[derive(Clone, Debug)]
pub struct RunResult {
    pub status: String, // ok | error
    pub duration_ms: i64,
    pub trace_ref: Option<String>,
    pub fuel: Option<i64>,
    pub error: Option<String>,
    /// Static credential refs the run resolved to nothing (ADR-021
    /// §2) — the chat wrapper posts a request bubble per ref.
    pub missing_secrets: Vec<String>,
}

pub struct Scheduler {
    pub instance_id: String,
    pub armed: bool, // boot DISARMED; arm() after sync
    now: Box<dyn Fn() -> f64 + Send>,
}

impl Scheduler {
    pub fn new(instance_id: &str, now: Box<dyn Fn() -> f64 + Send>) -> Self {
        Scheduler {
            instance_id: instance_id.into(),
            armed: false,
            now,
        }
    }

    pub fn arm(&mut self) {
        self.armed = true;
    }

    pub fn instance_id(&self) -> &str {
        &self.instance_id
    }

    fn runnable(&self, t: &Trigger) -> bool {
        self.armed && t.owner == self.instance_id && t.enabled
    }

    /// Strictly forward from now — a missed occurrence while the owner
    /// was down simply does not exist (cold-sync guard).
    pub fn compute_next_due(&self, t: &Trigger) -> Option<f64> {
        let base = (self.now)();
        if let Some(every) = t.spec.get("every_s").and_then(|v| v.as_f64()) {
            return Some(base + every);
        }
        if let Some(expr) = t.spec.get("cron").and_then(|v| v.as_str()) {
            // the `cron` crate wants 6/7 fields (seconds first)
            let full = if expr.split_whitespace().count() == 5 {
                format!("0 {expr}")
            } else {
                expr.to_string()
            };
            if let Ok(schedule) = cron::Schedule::from_str(&full) {
                return schedule
                    .upcoming(chrono::Utc)
                    .next()
                    .map(|dt| dt.timestamp() as f64);
            }
        }
        None
    }

    pub fn cron_due(&self, t: &mut Trigger) -> bool {
        if t.kind != "cron" || !self.runnable(t) {
            return false;
        }
        match t.next_due {
            None => {
                t.next_due = self.compute_next_due(t); // arm forward, no fire
                false
            }
            Some(due) => (self.now)() >= due,
        }
    }

    pub fn advance_cron(&self, t: &mut Trigger) {
        t.next_due = self.compute_next_due(t);
    }

    /// `once` fires when now >= spec.at, provided it never ran. A past
    /// `at` fires late (a late reminder beats a lost one — deliberate
    /// inversion of cron's missed-occurrence rule, ADR-006 §4); a
    /// failed run consumes the shot (record_run sets last_run_at
    /// regardless of status, and the caller disables after the fire).
    pub fn once_due(&self, t: &Trigger) -> bool {
        if t.kind != "once" || !self.runnable(t) || t.last_run_at.is_some() {
            return false;
        }
        t.spec
            .get("at")
            .and_then(|v| v.as_f64())
            .is_some_and(|at| (self.now)() >= at)
    }

    /// Run bookkeeping + the circuit breaker. Only SCHEDULER state is
    /// stamped (ADR-023 §8): `lastRunAt` (the `once` fired-guard and
    /// the cron cadence anchor) and `lastStatus` (the runner's verdict
    /// channel — ok/error/auto_disabled/invalid_spec…). Run history —
    /// ref, duration, fuel, cost, count — is the run summary in
    /// `agent_runs`, never a record field.
    pub fn record_run(&self, t: &mut Trigger, r: &RunResult) {
        let ts = (self.now)();
        t.last_run_at = Some(ts);
        t.last_status = Some(r.status.clone());
        if r.status == "ok" {
            t.consecutive_failures = 0;
        } else {
            t.consecutive_failures += 1;
            if t.consecutive_failures >= t.max_consecutive_failures {
                t.enabled = false;
                t.last_status = Some("auto_disabled".into());
            }
        }
    }
}

/// Pre-device-registry owner stamps (`anyrt-<pid>`, ADR-006 §4 before
/// the 2026-08-24 amendment): a pid never survives a restart, so these
/// values can never match a live instance again — read as UNOWNED.
/// Doubles as the degrade path: an election-disabled run (no peer id)
/// still stamps `anyrt-<pid>`, and stays adoptable across restarts.
pub fn is_legacy_owner(owner: &str) -> bool {
    owner
        .strip_prefix("anyrt-")
        .is_some_and(|rest| !rest.is_empty() && rest.bytes().all(|b| b.is_ascii_digit()))
}

/// The definition core differs — everything the user/agent authors.
/// `enabled` is deliberately excluded (honored in place, no re-arm) and
/// so are the runtime-owned rollup fields.
pub fn definition_differs(a: &Trigger, b: &Trigger) -> bool {
    a.kind != b.kind
        || a.spec != b.spec
        || a.program != b.program
        || a.args != b.args
        || a.name != b.name
        || a.limits != b.limits
        || a.max_consecutive_failures != b.max_consecutive_failures
}

/// One dataset reconcile pass (ADR-006 §4, device-pinning amendment):
/// the registry converges on the `agent_triggers` records.
///
/// - A record pinned to this device (`owner == instance`) is adopted —
///   election-independent, so a standby device fires its pins.
/// - An UNOWNED record (`owner` empty, or a legacy `anyrt-<pid>`
///   stamp) is adopted only by the election-active device, which
///   stamps its peer id.
/// - A foreign-owned record is left alone — and EVICTED from the
///   registry if it was ours before (a repin moves a live trigger off
///   this device within one tick).
/// - A definition-core edit rebuilds the registry entry from the
///   record: crons re-arm strictly forward; a `once` takes the
///   record's `lastRunAt` as its consumed state, so rewriting the
///   definition (which drops the rollup) re-arms the shot.
/// - A registry entry whose record vanished is evicted (delete works).
/// - Standing built-ins are code-owned: their record ids are skipped
///   and their registry entries never evicted.
///
/// Mutates the registry in place; returns the triggers whose records
/// must be (re)persisted with this device's ownership stamp. Malformed
/// records are skipped loudly and keep any live registry entry.
pub fn reconcile_registry(
    reg: &mut BTreeMap<String, Trigger>,
    recs: &[Value],
    instance: &str,
    active: bool,
    standing: &std::collections::BTreeSet<String>,
) -> Vec<Trigger> {
    let mut stamp: Vec<Trigger> = Vec::new();
    let mut seen: std::collections::BTreeSet<String> = Default::default();
    for rec in recs {
        let Some(id) = rec.get("id").and_then(|v| v.as_str()) else {
            continue;
        };
        if standing.contains(id) {
            continue; // built-ins are code-owned, not record-owned
        }
        seen.insert(id.to_string());
        let Some(parsed) = record_to_trigger(id, rec) else {
            tracing::warn!("agent_triggers {id:?}: malformed record — skipped");
            continue;
        };
        let unowned = parsed.owner.is_empty() || is_legacy_owner(&parsed.owner);
        let mine = parsed.owner == instance || (unowned && active);
        match reg.get_mut(id) {
            Some(live) => {
                if !mine {
                    reg.remove(id); // repinned away / unpinned while standby
                    continue;
                }
                if definition_differs(live, &parsed) {
                    let mut t = parsed;
                    let restamp = t.owner != instance;
                    t.owner = instance.to_string();
                    if restamp {
                        stamp.push(t.clone());
                    }
                    reg.insert(id.to_string(), t);
                } else {
                    if parsed.enabled && !live.enabled {
                        // manual re-enable via the dataset: reset the
                        // breaker and re-arm, matching the control
                        // plane's enable — otherwise one more failure
                        // instantly re-trips a spent breaker
                        live.consecutive_failures = 0;
                        live.next_due = None;
                    }
                    live.enabled = parsed.enabled;
                }
            }
            None => {
                if !mine {
                    continue; // pinned to another device
                }
                let mut t = parsed;
                let restamp = t.owner != instance;
                t.owner = instance.to_string();
                if restamp {
                    stamp.push(t.clone());
                }
                reg.insert(id.to_string(), t);
            }
        }
    }
    let gone: Vec<String> = reg
        .keys()
        .filter(|k| !standing.contains(*k) && !seen.contains(*k))
        .cloned()
        .collect();
    for k in gone {
        reg.remove(&k);
    }
    stamp
}

/// `lastStatus` marker: a definition the scheduler can never arm
/// (unparseable cron expression, a `once` without a numeric `at`).
pub const STATUS_INVALID_SPEC: &str = "invalid_spec";
/// Retired `lastStatus` marker (stamped 2026-08-24 while `event` had
/// no evaluator, ADR-018 supersedes it) — still recognized so the
/// health pass clears it off records once the definition is judged.
pub const STATUS_UNSUPPORTED_KIND: &str = "unsupported_kind";
/// `lastStatus` marker: an event source this runtime does not deliver
/// (ADR-018 §2 — v1 delivers `chat_messages` only).
pub const STATUS_UNSUPPORTED_SOURCE: &str = "unsupported_source";

/// The one event source v1 delivers (ADR-018 §2).
pub const CHAT_MESSAGES: &str = "chat_messages";

/// An event trigger's `(dataset, objectId)` source; None when either is
/// missing/empty — the definition can never fire.
pub fn event_source(t: &Trigger) -> Option<(&str, &str)> {
    if t.kind != "event" {
        return None;
    }
    let dataset = t.spec.get("dataset")?.as_str()?;
    let object_id = t.spec.get("objectId")?.as_str()?;
    if dataset.is_empty() || object_id.is_empty() {
        return None;
    }
    Some((dataset, object_id))
}

/// The definition can never arm: a cron whose spec yields no next
/// occurrence, a `once` without a numeric `at`, an event without a
/// `(dataset, objectId)` source.
pub fn spec_invalid(sched: &Scheduler, t: &Trigger) -> bool {
    match t.kind.as_str() {
        "cron" => sched.compute_next_due(t).is_none(),
        "once" => t.spec.get("at").and_then(|v| v.as_f64()).is_none(),
        "event" => event_source(t).is_none(),
        _ => false,
    }
}

/// The reserved chat-responder record (ADR-018 §3): bao's own chat
/// watch as a trigger — visible, pausable, repinnable. Dispatch is
/// native (the ADR-009 §8 watcher), never a program run.
pub const CHAT_WATCH_ID: &str = "chat-watch";
pub const CHAT_WATCH_PROGRAM: &str = "internal:chat-watch";

/// `chat-watch`, or a generation-suffixed reseed (`chat-watch-g2`)
/// minted when the bare id was tombstoned by a delete.
pub fn is_chat_watch(id: &str) -> bool {
    id == CHAT_WATCH_ID
        || id
            .strip_prefix("chat-watch-g")
            .is_some_and(|g| !g.is_empty() && g.bytes().all(|b| b.is_ascii_digit()))
}

pub fn chat_watch_trigger(id: &str, chat_id: &str, owner: &str) -> Trigger {
    let mut t = Trigger::cron(
        id,
        "Chat responder",
        0.0,
        CHAT_WATCH_PROGRAM,
        json!({}),
        owner,
        true,
    );
    t.kind = "event".into();
    t.spec = json!({"dataset": CHAT_MESSAGES, "objectId": chat_id});
    t
}

/// The id of the chat-responder entry this device runs: owned by it
/// and enabled — the gate the chat watch connects on (ADR-018 §3).
pub fn owned_chat_watch(reg: &BTreeMap<String, Trigger>, instance: &str) -> Option<String> {
    reg.values()
        .find(|t| is_chat_watch(&t.id) && t.owner == instance && t.enabled)
        .map(|t| t.id.clone())
}

/// The chat objects this device must watch (ADR-018 §2): one source
/// per distinct `objectId` across its own enabled, well-formed
/// `chat_messages` event triggers → the trigger ids it feeds.
pub fn desired_event_sources(
    reg: &BTreeMap<String, Trigger>,
    instance: &str,
) -> BTreeMap<String, Vec<String>> {
    let mut out: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for t in reg.values() {
        if t.owner != instance || !t.enabled || is_chat_watch(&t.id) {
            continue; // the responder rides the native watcher, not a source thread
        }
        if let Some((CHAT_MESSAGES, object_id)) = event_source(t) {
            out.entry(object_id.to_string())
                .or_default()
                .push(t.id.clone());
        }
    }
    out
}

/// Per-source delivery state (ADR-018 §2): LIVE-ONLY. A (re)connect
/// snapshot only seeds the seen-set — messages that arrived while the
/// owner was down or disconnected do not fire (cron's missed-occurrence
/// rule); `changes` frames fire once per message id, never for
/// self-authored messages (the ADR-009 §8 name-scoped rule).
#[derive(Default)]
pub struct EventSource {
    seen: std::collections::BTreeSet<String>,
}

fn message_id(record: &Value) -> Option<&str> {
    record
        .get("id")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
}

impl EventSource {
    pub fn seed(&mut self, records: &[Value]) {
        for r in records {
            if let Some(id) = message_id(r) {
                self.seen.insert(id.to_string());
            }
        }
    }

    pub fn fresh(&mut self, records: Vec<Value>, self_name: &str) -> Vec<Value> {
        let mut out = Vec::new();
        for r in records {
            let Some(id) = message_id(&r) else {
                continue;
            };
            if !self.seen.insert(id.to_string()) {
                continue;
            }
            if Watcher::is_self_message(&r, self_name) {
                continue;
            }
            out.push(r);
        }
        out
    }
}

/// The program args for one fire: the definition's `args` plus an
/// `event` object naming the message (ADR-018 §2).
pub fn event_args(t: &Trigger, space: &str, object_id: &str, record: &Value) -> Value {
    let mut args = t.args.as_object().cloned().unwrap_or_default();
    let mut event = serde_json::Map::new();
    event.insert("space".into(), json!(space));
    event.insert("objectId".into(), json!(object_id));
    event.insert(
        "messageId".into(),
        record.get("id").cloned().unwrap_or(Value::Null),
    );
    event.insert(
        "text".into(),
        json!(record.get("text").and_then(|v| v.as_str()).unwrap_or("")),
    );
    if let Some(agent) = record.get("agent").filter(|a| !a.is_null()) {
        event.insert("agent".into(), agent.clone());
    }
    if let Some(atts) = record.get("attachments").filter(|a| !a.is_null()) {
        event.insert("attachments".into(), atts.clone());
    }
    args.insert("event".into(), Value::Object(event));
    Value::Object(args)
}

/// Health pass (ADR-006 §4 observability): a trigger that LOOKS armed
/// but can never fire — unarmable spec, undeliverable event source — gets a
/// `lastStatus` marker stamped so the record itself says why nothing
/// happens (a silent never-due trigger was undiagnosable from inside —
/// BOB-39). Markers self-clear once the definition is fixed. Only this
/// device's live, enabled entries are judged; standing built-ins are
/// code-owned and always valid. Returns the triggers whose records
/// need persisting.
pub fn health_pass(
    sched: &Scheduler,
    reg: &mut BTreeMap<String, Trigger>,
    standing: &std::collections::BTreeSet<String>,
) -> Vec<Trigger> {
    let mut changed = Vec::new();
    for t in reg.values_mut() {
        if standing.contains(&t.id) || t.owner != sched.instance_id() || !t.enabled {
            continue;
        }
        let marker = if spec_invalid(sched, t) {
            Some(STATUS_INVALID_SPEC)
        } else if t.kind == "event" && !matches!(event_source(t), Some((CHAT_MESSAGES, _))) {
            Some(STATUS_UNSUPPORTED_SOURCE)
        } else {
            None
        };
        match marker {
            Some(m) if t.last_status.as_deref() != Some(m) => {
                tracing::warn!(
                    "trigger {:?}: {m} — enabled but can never fire as defined",
                    t.id
                );
                t.last_status = Some(m.into());
                changed.push(t.clone());
            }
            None if matches!(
                t.last_status.as_deref(),
                Some(STATUS_INVALID_SPEC | STATUS_UNSUPPORTED_KIND | STATUS_UNSUPPORTED_SOURCE)
            ) =>
            {
                t.last_status = None; // fixed — clear the marker
                changed.push(t.clone());
            }
            _ => {}
        }
    }
    changed
}

/// Parse an `agent_triggers` dataset record into a Trigger (ADR-006 §4
/// dataset-is-source-of-truth). Returns None for records missing the
/// definition core — the caller logs and skips, never crashes.
pub fn record_to_trigger(id: &str, rec: &Value) -> Option<Trigger> {
    let kind = rec.get("kind")?.as_str()?.to_string();
    if !matches!(kind.as_str(), "cron" | "event" | "once") {
        return None;
    }
    let program = rec.get("program")?.as_str()?.to_string();
    Some(Trigger {
        id: id.into(),
        name: rec
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or(id)
            .to_string(),
        kind,
        spec: rec.get("spec").cloned().unwrap_or_else(|| json!({})),
        program,
        args: rec.get("args").cloned().unwrap_or_else(|| json!({})),
        owner: rec
            .get("owner")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        enabled: rec.get("enabled").and_then(|v| v.as_bool()).unwrap_or(true),
        limits: rec.get("limits").cloned().unwrap_or_else(|| json!({})),
        max_consecutive_failures: rec
            .get("maxConsecutiveFailures")
            .and_then(|v| v.as_i64())
            .unwrap_or(DEFAULT_MAX_CONSECUTIVE_FAILURES),
        last_run_at: rec.get("lastRunAt").and_then(|v| v.as_f64()),
        last_status: rec
            .get("lastStatus")
            .and_then(|v| v.as_str())
            .map(String::from),
        consecutive_failures: rec
            .get("consecutiveFailures")
            .and_then(|v| v.as_i64())
            .unwrap_or(0),
        next_due: None,
    })
}

pub fn trigger_to_record(t: &Trigger) -> Value {
    json!({
        "name": t.name, "kind": t.kind, "spec": t.spec, "program": t.program,
        "args": t.args, "owner": t.owner, "enabled": t.enabled,
        "limits": t.limits, "maxConsecutiveFailures": t.max_consecutive_failures,
        "lastRunAt": t.last_run_at, "lastStatus": t.last_status,
        "consecutiveFailures": t.consecutive_failures,
    })
}

pub fn rollup(t: &Trigger) -> Value {
    json!({
        "id": t.id, "name": t.name, "kind": t.kind, "owner": t.owner,
        "enabled": t.enabled, "lastRunAt": t.last_run_at,
        "lastStatus": t.last_status,
        "consecutiveFailures": t.consecutive_failures,
        "limits": t.limits,
    })
}

/// The standing background jobs (ADR-006 §2, ADR-007 §1b/§4).
pub fn standing_triggers(space: &str, chat_id: &str, owner: &str) -> Vec<Trigger> {
    vec![
        Trigger::cron(
            "rollup",
            "history rollup",
            3600.0,
            "agent:rollup@v1",
            json!({"space": space, "chatId": chat_id}),
            owner,
            true,
        ),
        Trigger::cron(
            "extraction",
            "memory extraction",
            900.0,
            "agent:extraction@v1",
            json!({"space": space, "chatId": chat_id}),
            owner,
            true,
        ),
        Trigger::cron(
            "linkgen",
            "memory link generation",
            3600.0,
            "agent:linkgen@v1",
            json!({"space": space}),
            owner,
            true,
        ),
        // §4.2 mechanisms ship DISABLED — each behind its eval (ADR-007)
        Trigger::cron(
            "decay",
            "salience decay",
            86400.0,
            "agent:decay@v1",
            json!({"space": space}),
            owner,
            false,
        ),
        Trigger::cron(
            "reflection",
            "reflection",
            86400.0,
            "agent:reflection@v1",
            json!({"space": space}),
            owner,
            false,
        ),
        Trigger::cron(
            "evolution",
            "memory evolution",
            21600.0,
            "agent:evolution@v1",
            json!({"space": space}),
            owner,
            false,
        ),
    ]
}

// --- the watcher (trigger #1) -------------------------------------------------

/// A chat message as the loop consumes it — see `Watcher::input`.
#[derive(Clone, Debug)]
pub struct ChatInput {
    pub text: String,
    pub context: Value,
}

/// Pure decision logic: dedup by message id, SELF-message skip, and
/// mid-run routing (a message during a live conversation INJECTS into
/// its mailbox instead of starting a new run).
///
/// The skip is name-scoped (ADR-009 §8 amendment, 2026-08-16): only a
/// message from THIS agent's own name — or an agent message with no
/// name, the conservative read of legacy records — never self-triggers.
/// A foreign agent name (`trigger:gmail-backfill`, a peer agent) is
/// user-side input: it starts or injects like a human message. That is
/// the visible-nudge mechanism — a program posts its completion into
/// the chat under a `trigger:*` identity and the loop picks it up with
/// the chat's history in context.
#[derive(Default)]
pub struct Watcher {
    seen: std::collections::BTreeSet<String>,
    pub live: BTreeMap<String, crate::broker::SharedMailbox>,
    /// This agent's chat identity (`agent.name` on its own bubbles).
    pub self_name: String,
}

pub enum WatchAction {
    Dup,
    Skip,
    Inject,
    Start,
}

impl Watcher {
    pub fn new(self_name: impl Into<String>) -> Self {
        Watcher {
            self_name: self_name.into(),
            ..Default::default()
        }
    }

    /// Self-authored (own name, or agent-tagged with no name) — the only
    /// messages that never trigger. Shared with `snapshot_backlog`.
    pub fn is_self_message(record: &Value, self_name: &str) -> bool {
        match record.get("agent").filter(|a| !a.is_null()) {
            None => false,
            Some(agent) => {
                let name = agent.get("name").and_then(|n| n.as_str()).unwrap_or("");
                name.is_empty() || name == self_name
            }
        }
    }

    /// One unanswered chat message as the loop receives it: the
    /// attributed text plus the sender's view — the message's
    /// `context` group (ADR-005 §5: `{spaceId, objectId?, view?}`,
    /// stamped by the client at send time), or Null when the client
    /// sent none. The view rides as an ARG, not folded into the text:
    /// it is prompt-time locator data (the `[now: … | user's view …]`
    /// line + the `currentUserSpace` cell global), and the persisted
    /// turn keeps the raw text — the chat message itself is the
    /// durable record of where the user was.
    pub fn input(record: &Value) -> ChatInput {
        ChatInput {
            text: Self::attributed_text(record),
            context: Self::view_context(record),
        }
    }

    /// The message's `context` group, trimmed to the locator keys, or
    /// Null — a context without a `spaceId` is no context.
    pub fn view_context(record: &Value) -> Value {
        let Some(ctx) = record.get("context").and_then(|c| c.as_object()) else {
            return Value::Null;
        };
        let space = ctx.get("spaceId").and_then(|s| s.as_str()).unwrap_or("");
        if space.is_empty() {
            return Value::Null;
        }
        let mut out = serde_json::Map::new();
        out.insert("spaceId".into(), json!(space));
        for k in ["objectId", "view"] {
            if let Some(v) = ctx
                .get(k)
                .and_then(|v| v.as_str())
                .filter(|v| !v.is_empty())
            {
                out.insert(k.into(), json!(v));
            }
        }
        Value::Object(out)
    }

    /// The record's text, attributed and attachment-aware: a
    /// foreign-agent message (a `trigger:*` nudge, a peer) gets a
    /// harness-authored `[from agent …]` line derived from the
    /// record's `agent.name` METADATA — the model's knowledge of the
    /// sender no longer rests on a spoofable convention inside the
    /// message text. Human messages pass through untouched. The
    /// record's `attachments` map ({id: {type, link}}, create-only)
    /// folds in as `[attachment <type>: <link>]` lines — links are the
    /// doc-19 typed URIs any-ui sends (objects `any://o/<sid>/<oid>`,
    /// files `any://f/<sid>/<fileId>`), which the model resolves via
    /// any@v1. Folded into the TEXT (not an arg) so both attribution
    /// and attachments survive into the persisted turn and every
    /// future boot window unchanged.
    pub fn attributed_text(record: &Value) -> String {
        let text = record.get("text").and_then(|t| t.as_str()).unwrap_or("");
        let mut out = match record
            .get("agent")
            .filter(|a| !a.is_null())
            .and_then(|a| a.get("name"))
            .and_then(|n| n.as_str())
            .filter(|n| !n.is_empty())
        {
            Some(name) => {
                format!("[from agent \"{name}\" — automated message, not the user]\n{text}")
            }
            None => text.to_string(),
        };
        if let Some(atts) = record.get("attachments").and_then(|a| a.as_object()) {
            let mut keys: Vec<&String> = atts.keys().collect();
            keys.sort(); // map order is arbitrary; stable lines for the turn log
            for k in keys {
                let link = atts[k].get("link").and_then(|l| l.as_str()).unwrap_or("");
                if link.is_empty() {
                    continue;
                }
                let kind = atts[k]
                    .get("type")
                    .and_then(|t| t.as_str())
                    .unwrap_or("link");
                if !out.is_empty() {
                    out.push('\n');
                }
                out.push_str(&format!("[attachment {kind}: {link}]"));
                // ADR-021 §5: a credential_set is a cue to ACT, not news
                // to acknowledge — the run that needed the key never
                // finished (or failed), so the model must redo it.
                if kind == "credential_set" {
                    out.push_str(
                        "\n[runtime: the user just entered this credential. Do NOT merely \
                         acknowledge it — go back to the last user request before the \
                         credential prompt and carry it out now, replying with its result.]",
                    );
                }
            }
        }
        out
    }

    pub fn on_message(&mut self, chat_id: &str, record: &Value) -> WatchAction {
        let msg_id = record.get("id").and_then(|v| v.as_str()).unwrap_or("");
        if !msg_id.is_empty() && !self.seen.insert(msg_id.to_string()) {
            return WatchAction::Dup;
        }
        if Self::is_self_message(record, &self.self_name) {
            return WatchAction::Skip; // own bubble — never self-trigger
        }
        if let Some(mailbox) = self.live.get(chat_id) {
            let input = Self::input(record);
            mailbox.lock().unwrap().push_back(json!({
                "kind": "inject", "text": input.text, "context": input.context}));
            return WatchAction::Inject;
        }
        WatchAction::Start
    }

    pub fn conversation_done(&mut self, chat_id: &str) {
        self.live.remove(chat_id);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    fn clock(t: Arc<Mutex<f64>>) -> Box<dyn Fn() -> f64 + Send> {
        Box::new(move || *t.lock().unwrap())
    }

    #[test]
    fn cron_arms_then_fires_then_advances() {
        let t = Arc::new(Mutex::new(1000.0));
        let mut sched = Scheduler::new("i1", clock(t.clone()));
        sched.arm();
        let mut tr = Trigger::cron("x", "x", 60.0, "p@v1", json!({}), "i1", true);
        assert!(!sched.cron_due(&mut tr)); // first check arms
        assert_eq!(tr.next_due, Some(1060.0));
        *t.lock().unwrap() = 1061.0;
        assert!(sched.cron_due(&mut tr));
        sched.advance_cron(&mut tr);
        assert_eq!(tr.next_due, Some(1121.0));
    }

    #[test]
    fn disarmed_or_foreign_never_fires() {
        let t = Arc::new(Mutex::new(1000.0));
        let mut sched = Scheduler::new("i1", clock(t));
        let mut own = Trigger::cron("a", "a", 1.0, "p@v1", json!({}), "i1", true);
        assert!(!sched.cron_due(&mut own)); // disarmed
        sched.arm();
        let mut foreign = Trigger::cron("b", "b", 1.0, "p@v1", json!({}), "other", true);
        assert!(!sched.cron_due(&mut foreign));
    }

    #[test]
    fn once_fires_at_time_late_and_only_once() {
        let t = Arc::new(Mutex::new(1000.0));
        let mut sched = Scheduler::new("i1", clock(t.clone()));
        sched.arm();
        let mut tr = Trigger::cron("r", "remind", 0.0, "agent:remind@v1", json!({}), "i1", true);
        tr.kind = "once".into();
        tr.spec = json!({"at": 1500.0});
        assert!(!sched.once_due(&tr)); // not yet due
        *t.lock().unwrap() = 1500.0;
        assert!(sched.once_due(&tr)); // due at the boundary
        *t.lock().unwrap() = 9999.0;
        assert!(sched.once_due(&tr)); // a PAST at fires late (ADR-006 §4)
                                      // any recorded run (even a failure) consumes the shot
        tr.last_run_at = Some(9999.0);
        assert!(!sched.once_due(&tr));
        // cron never answers once_due, and vice versa
        let mut cr = Trigger::cron("c", "c", 60.0, "p@v1", json!({}), "i1", true);
        assert!(!sched.once_due(&cr));
        assert!(!sched.cron_due(&mut Trigger {
            kind: "once".into(),
            ..cr.clone()
        }));
        let _ = &mut cr;
    }

    #[test]
    fn record_round_trips_and_malformed_is_none() {
        let rec = json!({"name": "oven", "kind": "once",
                         "spec": {"at": 1234.5}, "program": "agent:remind@v1",
                         "args": {"text": "check the oven"}, "enabled": true});
        let t = record_to_trigger("r1", &rec).expect("parses");
        assert_eq!(
            (t.kind.as_str(), t.program.as_str()),
            ("once", "agent:remind@v1")
        );
        assert_eq!(t.spec["at"], json!(1234.5));
        assert_eq!(t.owner, ""); // ownerless — adoptable
                                 // round-trip: to_record → back preserves the definition core
        let back = record_to_trigger("r1", &trigger_to_record(&t)).expect("round-trips");
        assert_eq!((back.kind, back.args), (t.kind, t.args));
        // malformed: unknown kind / missing program
        assert!(record_to_trigger(
            "x",
            &json!({"kind": "sometimes",
                                               "program": "p@v1"})
        )
        .is_none());
        assert!(record_to_trigger("x", &json!({"kind": "once"})).is_none());
    }

    #[test]
    fn legacy_owner_stamps_are_recognized() {
        assert!(is_legacy_owner("anyrt-573756"));
        assert!(is_legacy_owner("anyrt-1"));
        assert!(!is_legacy_owner("")); // empty is unowned, not legacy
        assert!(!is_legacy_owner("anyrt-")); // no pid
        assert!(!is_legacy_owner("anyrt-12x")); // not a pid
        assert!(!is_legacy_owner("12D3KooWQRTgVWHF")); // a peer id
    }

    fn rec(id: &str, owner: &str, extra: Value) -> Value {
        let mut r = json!({"id": id, "kind": "cron", "spec": {"every_s": 60.0},
                           "program": "p@v1", "args": {}, "owner": owner,
                           "enabled": true});
        if let Some(obj) = extra.as_object() {
            for (k, v) in obj {
                r[k] = v.clone();
            }
        }
        r
    }

    #[test]
    fn reconcile_adopts_own_pins_even_on_standby() {
        let mut reg = BTreeMap::new();
        let standing = Default::default();
        let recs = vec![
            rec("mine", "peer-A", json!({})),
            rec("other", "peer-B", json!({})),
        ];
        let stamp = reconcile_registry(&mut reg, &recs, "peer-A", false, &standing);
        assert!(reg.contains_key("mine")); // pin fires regardless of election
        assert!(!reg.contains_key("other")); // foreign pin left alone
        assert!(stamp.is_empty()); // owner already correct — no write
    }

    #[test]
    fn reconcile_unowned_and_legacy_need_the_active_device() {
        let mut reg = BTreeMap::new();
        let standing = Default::default();
        let recs = vec![
            rec("free", "", json!({})),
            rec("stale", "anyrt-4242", json!({})), // dead pid — unowned
        ];
        // standby adopts neither…
        assert!(reconcile_registry(&mut reg, &recs, "peer-A", false, &standing).is_empty());
        assert!(reg.is_empty());
        // …the active device adopts both and stamps its peer id
        let stamp = reconcile_registry(&mut reg, &recs, "peer-A", true, &standing);
        assert_eq!(stamp.len(), 2);
        assert!(stamp.iter().all(|t| t.owner == "peer-A"));
        assert_eq!(reg["free"].owner, "peer-A");
        assert_eq!(reg["stale"].owner, "peer-A");
    }

    #[test]
    fn reconcile_repin_away_and_delete_evict() {
        let mut reg = BTreeMap::new();
        let standing = Default::default();
        let recs = vec![rec("a", "", json!({})), rec("b", "", json!({}))];
        reconcile_registry(&mut reg, &recs, "peer-A", true, &standing);
        assert_eq!(reg.len(), 2);
        // "a" repinned to another device, "b" deleted from the dataset
        let recs = vec![rec("a", "peer-B", json!({}))];
        reconcile_registry(&mut reg, &recs, "peer-A", true, &standing);
        assert!(reg.is_empty());
    }

    #[test]
    fn reconcile_refreshes_edited_definitions_and_rearms() {
        let mut reg = BTreeMap::new();
        let standing = Default::default();
        let recs = vec![rec("j", "peer-A", json!({}))];
        reconcile_registry(&mut reg, &recs, "peer-A", true, &standing);
        reg.get_mut("j").unwrap().next_due = Some(1060.0); // armed
                                                           // enabled-only edit: honored in place, no re-arm
        let mut toggled = rec("j", "peer-A", json!({}));
        toggled["enabled"] = json!(false);
        reconcile_registry(&mut reg, &[toggled], "peer-A", true, &standing);
        assert!(!reg["j"].enabled);
        assert_eq!(reg["j"].next_due, Some(1060.0));
        // spec edit: entry rebuilt from the record, re-armed forward
        let edited = rec("j", "peer-A", json!({"spec": {"every_s": 900.0}}));
        reconcile_registry(&mut reg, &[edited], "peer-A", true, &standing);
        assert_eq!(reg["j"].spec["every_s"], json!(900.0));
        assert_eq!(reg["j"].next_due, None);
        assert!(reg["j"].enabled); // the record is the source of truth
    }

    #[test]
    fn reconcile_reenable_resets_the_breaker() {
        let mut reg = BTreeMap::new();
        let standing = Default::default();
        let recs = vec![rec("j", "peer-A", json!({}))];
        reconcile_registry(&mut reg, &recs, "peer-A", true, &standing);
        {
            let t = reg.get_mut("j").unwrap();
            t.enabled = false;
            t.consecutive_failures = 3;
            t.last_status = Some("auto_disabled".into());
            t.next_due = Some(1060.0);
        }
        let mut resumed = rec("j", "peer-A", json!({}));
        resumed["enabled"] = json!(true);
        reconcile_registry(&mut reg, &[resumed], "peer-A", true, &standing);
        assert!(reg["j"].enabled);
        assert_eq!(reg["j"].consecutive_failures, 0); // breaker reset
        assert_eq!(reg["j"].next_due, None); // re-armed forward
    }

    #[test]
    fn reconcile_rearms_a_rewritten_once_and_skips_standing_and_malformed() {
        let mut reg = BTreeMap::new();
        let standing: std::collections::BTreeSet<String> =
            ["rollup".to_string()].into_iter().collect();
        // a consumed once, re-armed by rewriting the definition
        // (the rewrite drops lastRunAt — the recipe/UI write only the core)
        let consumed = json!({"id": "r", "kind": "once", "spec": {"at": 100.0},
            "program": "p@v1", "args": {}, "owner": "peer-A",
            "enabled": false, "lastRunAt": 100.5});
        reconcile_registry(&mut reg, &[consumed], "peer-A", true, &Default::default());
        assert_eq!(reg["r"].last_run_at, Some(100.5)); // stays consumed
        let rearmed = json!({"id": "r", "kind": "once", "spec": {"at": 500.0},
            "program": "p@v1", "args": {}, "owner": "peer-A", "enabled": true});
        reconcile_registry(&mut reg, &[rearmed], "peer-A", true, &Default::default());
        assert_eq!(reg["r"].last_run_at, None); // fresh shot
        assert!(reg["r"].enabled);
        // standing record ids are ignored; malformed keeps a live entry
        let mut reg2: BTreeMap<String, Trigger> = BTreeMap::new();
        reg2.insert(
            "rollup".into(),
            Trigger::cron(
                "rollup",
                "rollup",
                3600.0,
                "agent:rollup@v1",
                json!({}),
                "",
                true,
            ),
        );
        reg2.insert(
            "ok".into(),
            Trigger::cron("ok", "ok", 60.0, "p@v1", json!({}), "peer-A", true),
        );
        let recs = vec![
            rec("rollup", "peer-B", json!({})), // code-owned — never adopted or evicted
            json!({"id": "ok", "kind": "sometimes", "program": "p@v1"}), // malformed
        ];
        reconcile_registry(&mut reg2, &recs, "peer-A", true, &standing);
        assert!(reg2.contains_key("rollup"));
        assert!(reg2.contains_key("ok")); // malformed record keeps the live entry
    }

    #[test]
    fn spec_invalid_flags_unarmable_definitions() {
        let t = Arc::new(Mutex::new(1000.0));
        let sched = Scheduler::new("i1", clock(t));
        let ok = Trigger::cron("a", "a", 60.0, "p@v1", json!({}), "i1", true);
        assert!(!spec_invalid(&sched, &ok));
        let mut bad_cron = ok.clone();
        bad_cron.spec = json!({"cron": "not a cron"});
        assert!(spec_invalid(&sched, &bad_cron));
        let mut empty_spec = ok.clone();
        empty_spec.spec = json!({});
        assert!(spec_invalid(&sched, &empty_spec));
        let mut once = ok.clone();
        once.kind = "once".into();
        once.spec = json!({"at": 1500.0});
        assert!(!spec_invalid(&sched, &once));
        once.spec = json!({"at": "tomorrow"});
        assert!(spec_invalid(&sched, &once));
        let mut event = ok.clone();
        event.kind = "event".into();
        assert!(spec_invalid(&sched, &event)); // a cron-shaped spec is no source
        event.spec = json!({"dataset": "chat_messages", "objectId": "chat-1"});
        assert!(!spec_invalid(&sched, &event));
        event.spec = json!({"dataset": "objects", "objectId": "o1"});
        assert!(!spec_invalid(&sched, &event)); // well-formed, merely undeliverable
        assert_eq!(event_source(&event), Some(("objects", "o1")));
    }

    #[test]
    fn health_pass_marks_inert_definitions_and_clears_on_fix() {
        let t = Arc::new(Mutex::new(1000.0));
        let sched = Scheduler::new("peer-A", clock(t));
        let standing: std::collections::BTreeSet<String> =
            ["rollup".to_string()].into_iter().collect();
        let mut reg = BTreeMap::new();
        let mut ev = Trigger::cron("ev", "ev", 60.0, "p@v1", json!({}), "peer-A", true);
        ev.kind = "event".into();
        ev.spec = json!({"dataset": "objects", "objectId": "o1"}); // undeliverable source
        reg.insert("ev".into(), ev);
        let mut chat_ev = Trigger::cron("chat", "chat", 60.0, "p@v1", json!({}), "peer-A", true);
        chat_ev.kind = "event".into();
        chat_ev.spec = json!({"dataset": "chat_messages", "objectId": "chat-1"});
        chat_ev.last_status = Some(STATUS_UNSUPPORTED_KIND.into()); // retired marker
        reg.insert("chat".into(), chat_ev);
        let mut bad = Trigger::cron("bad", "bad", 60.0, "p@v1", json!({}), "peer-A", true);
        bad.spec = json!({"cron": "nope"});
        reg.insert("bad".into(), bad);
        // not judged: foreign pin, disabled, standing, healthy
        reg.insert(
            "foreign".into(),
            Trigger::cron("foreign", "f", 60.0, "p@v1", json!({}), "peer-B", true),
        );
        let mut off = Trigger::cron("off", "off", 60.0, "p@v1", json!({}), "peer-A", false);
        off.spec = json!({});
        reg.insert("off".into(), off);
        let mut standing_bad = Trigger::cron(
            "rollup",
            "rollup",
            3600.0,
            "agent:rollup@v1",
            json!({}),
            "peer-A",
            true,
        );
        standing_bad.spec = json!({});
        reg.insert("rollup".into(), standing_bad);
        reg.insert(
            "fine".into(),
            Trigger::cron("fine", "fine", 60.0, "p@v1", json!({}), "peer-A", true),
        );

        let changed = health_pass(&sched, &mut reg, &standing);
        let ids: Vec<&str> = changed.iter().map(|t| t.id.as_str()).collect();
        assert_eq!(ids, ["bad", "chat", "ev"]);
        assert_eq!(
            reg["ev"].last_status.as_deref(),
            Some(STATUS_UNSUPPORTED_SOURCE)
        );
        assert_eq!(reg["chat"].last_status, None); // deliverable — retired marker cleared
        assert_eq!(reg["bad"].last_status.as_deref(), Some(STATUS_INVALID_SPEC));
        assert!(reg["fine"].last_status.is_none());
        // steady state: no repeat writes, no repeat warns
        assert!(health_pass(&sched, &mut reg, &standing).is_empty());
        // fixing the spec clears the marker; a real run status is untouched
        reg.get_mut("bad").unwrap().spec = json!({"every_s": 60.0});
        let cleared = health_pass(&sched, &mut reg, &standing);
        assert_eq!(cleared.len(), 1);
        assert!(reg["bad"].last_status.is_none());
        reg.get_mut("fine").unwrap().last_status = Some("ok".into());
        assert!(health_pass(&sched, &mut reg, &standing).is_empty());
    }

    fn event_trigger(id: &str, owner: &str, object_id: &str, enabled: bool) -> Trigger {
        let mut t = Trigger::cron(id, id, 0.0, "p@v1", json!({"note": "n"}), owner, enabled);
        t.kind = "event".into();
        t.spec = json!({"dataset": "chat_messages", "objectId": object_id});
        t
    }

    #[test]
    fn desired_sources_are_own_enabled_chat_events_grouped_by_object() {
        let mut reg = BTreeMap::new();
        reg.insert("a".into(), event_trigger("a", "peer-A", "chat-1", true));
        reg.insert("b".into(), event_trigger("b", "peer-A", "chat-1", true));
        reg.insert("c".into(), event_trigger("c", "peer-A", "chat-2", true));
        reg.insert(
            "off".into(),
            event_trigger("off", "peer-A", "chat-3", false),
        );
        reg.insert(
            "theirs".into(),
            event_trigger("theirs", "peer-B", "chat-4", true),
        );
        let mut other = event_trigger("other", "peer-A", "o1", true);
        other.spec = json!({"dataset": "objects", "objectId": "o1"});
        reg.insert("other".into(), other);
        reg.insert(
            "cron".into(),
            Trigger::cron("cron", "cron", 60.0, "p@v1", json!({}), "peer-A", true),
        );
        let desired = desired_event_sources(&reg, "peer-A");
        assert_eq!(
            desired,
            BTreeMap::from([
                ("chat-1".to_string(), vec!["a".to_string(), "b".to_string()]),
                ("chat-2".to_string(), vec!["c".to_string()]),
            ])
        );
    }

    #[test]
    fn chat_watch_ids_and_ownership_gate() {
        assert!(is_chat_watch("chat-watch"));
        assert!(is_chat_watch("chat-watch-g2"));
        assert!(!is_chat_watch("chat-watch-g"));
        assert!(!is_chat_watch("chat-watcher"));
        let t = chat_watch_trigger("chat-watch", "chat-1", "peer-A");
        assert_eq!(event_source(&t), Some(("chat_messages", "chat-1")));
        assert_eq!(t.program, CHAT_WATCH_PROGRAM);
        let mut reg = BTreeMap::new();
        reg.insert("chat-watch".into(), t);
        reg.insert("a".into(), event_trigger("a", "peer-A", "chat-1", true));
        assert_eq!(
            owned_chat_watch(&reg, "peer-A").as_deref(),
            Some("chat-watch")
        );
        assert_eq!(owned_chat_watch(&reg, "peer-B"), None);
        // the responder never gets a generic source thread of its own
        let desired = desired_event_sources(&reg, "peer-A");
        assert_eq!(desired["chat-1"], vec!["a".to_string()]);
        reg.get_mut("chat-watch").unwrap().enabled = false;
        assert_eq!(owned_chat_watch(&reg, "peer-A"), None); // paused = nobody answers here
    }

    #[test]
    fn event_source_is_live_only_deduped_and_skips_self() {
        let mut src = EventSource::default();
        // a snapshot seeds but never fires
        src.seed(&[json!({"id": "m1", "text": "old"}), json!({"id": "m2"})]);
        let fresh = src.fresh(
            vec![
                json!({"id": "m1", "text": "replayed"}), // seen in snapshot
                json!({"id": "m3", "text": "new"}),
                json!({"id": "m3", "text": "dup"}),
                json!({"id": "m4", "text": "mine", "agent": {"name": "bao"}}),
                json!({"id": "m5", "text": "nudge", "agent": {"name": "trigger:x"}}),
                json!({"text": "no id"}),
            ],
            "bao",
        );
        let ids: Vec<&str> = fresh.iter().map(|r| r["id"].as_str().unwrap()).collect();
        assert_eq!(ids, ["m3", "m5"]);
    }

    #[test]
    fn event_args_merge_the_definition_args_with_the_message() {
        let t = event_trigger("a", "peer-A", "chat-1", true);
        let record = json!({"id": "m9", "text": "hello", "agent": {"name": "trigger:x"},
                            "attachments": {"f0": {"type": "image", "link": "any://f/s/1"}}});
        let args = event_args(&t, "space-1", "chat-1", &record);
        assert_eq!(args["note"], json!("n"));
        assert_eq!(
            args["event"],
            json!({"space": "space-1", "objectId": "chat-1", "messageId": "m9",
                   "text": "hello", "agent": {"name": "trigger:x"},
                   "attachments": {"f0": {"type": "image", "link": "any://f/s/1"}}})
        );
        let plain = event_args(&t, "space-1", "chat-1", &json!({"id": "m1"}));
        assert_eq!(plain["event"]["text"], json!(""));
        assert!(plain["event"].get("agent").is_none());
    }

    #[test]
    fn circuit_breaker_auto_disables() {
        let t = Arc::new(Mutex::new(0.0));
        let sched = Scheduler::new("i1", clock(t));
        let mut tr = Trigger::cron("x", "x", 1.0, "p@v1", json!({}), "i1", true);
        let fail = RunResult {
            status: "error".into(),
            duration_ms: 1,
            trace_ref: None,
            fuel: None,
            error: Some("boom".into()),
            missing_secrets: Vec::new(),
        };
        for _ in 0..3 {
            sched.record_run(&mut tr, &fail);
        }
        assert!(!tr.enabled);
        assert_eq!(tr.last_status.as_deref(), Some("auto_disabled"));
    }

    #[test]
    fn watcher_dedup_skip_inject_start() {
        let mut w = Watcher::new("bao");
        let human = json!({"id": "m1", "text": "hi"});
        assert!(matches!(w.on_message("c1", &human), WatchAction::Start));
        assert!(matches!(w.on_message("c1", &human), WatchAction::Dup));
        let agent = json!({"id": "m2", "text": "x", "agent": {"name": "bao"}});
        assert!(matches!(w.on_message("c1", &agent), WatchAction::Skip));
        // legacy agent message without a name: conservative skip
        let unnamed = json!({"id": "m2b", "text": "x", "agent": {}});
        assert!(matches!(w.on_message("c1", &unnamed), WatchAction::Skip));
        // a FOREIGN agent name is user-side input — the visible nudge
        let nudge = json!({"id": "m2c", "text": "job done",
                           "agent": {"name": "trigger:gmail-backfill"}});
        assert!(matches!(w.on_message("c1", &nudge), WatchAction::Start));
        w.conversation_done("c1");
        let mb: crate::broker::SharedMailbox = Default::default();
        w.live.insert("c1".into(), mb.clone());
        let m3 = json!({"id": "m3", "text": "also"});
        assert!(matches!(w.on_message("c1", &m3), WatchAction::Inject));
        assert_eq!(mb.lock().unwrap().len(), 1);
        w.conversation_done("c1");
        let m4 = json!({"id": "m4", "text": "fresh"});
        assert!(matches!(w.on_message("c1", &m4), WatchAction::Start));
    }

    #[test]
    fn view_context_rides_the_input_as_an_arg_not_the_text() {
        // the client's `context` group → uiContext, trimmed to the
        // locator keys; the text stays the text (ADR-005 §5)
        let rec = json!({"id": "h1", "text": "do it here",
            "context": {"spaceId": "sp1", "objectId": "ob1", "view": "object",
                        "stray": "x"}});
        let input = Watcher::input(&rec);
        assert_eq!(input.text, "do it here");
        assert_eq!(
            input.context,
            json!({"spaceId": "sp1", "objectId": "ob1", "view": "object"})
        );
        // empties drop; no spaceId = no context at all
        let partial = json!({"text": "x", "context": {"spaceId": "sp1", "objectId": ""}});
        assert_eq!(Watcher::view_context(&partial), json!({"spaceId": "sp1"}));
        assert_eq!(Watcher::view_context(&json!({"text": "x"})), Value::Null);
        assert_eq!(
            Watcher::view_context(&json!({"text": "x", "context": {"objectId": "o"}})),
            Value::Null
        );
        // …and it rides the inject next to the text
        let mut w = Watcher::new("bao");
        let mb: crate::broker::SharedMailbox = Default::default();
        w.live.insert("c1".into(), mb.clone());
        assert!(matches!(w.on_message("c1", &rec), WatchAction::Inject));
        let item = mb.lock().unwrap().pop_front().unwrap();
        assert_eq!(item["kind"], "inject");
        assert_eq!(item["context"]["spaceId"], "sp1");
    }

    #[test]
    fn attribution_is_metadata_derived_and_rides_the_inject() {
        // harness-authored [from agent …] line from agent.name — the
        // model's sender knowledge must not rest on the message text
        let nudge = json!({"text": "job done",
                           "agent": {"name": "trigger:backfill"}});
        assert_eq!(
            Watcher::attributed_text(&nudge),
            "[from agent \"trigger:backfill\" — automated message, not the user]\njob done"
        );
        let human = json!({"id": "h1", "text": "hi"});
        assert_eq!(Watcher::attributed_text(&human), "hi");

        // attachments fold in as harness-derived lines, key-sorted;
        // an attachment-only message still yields non-empty input
        let attached = json!({"id": "h2", "text": "see these",
            "attachments": {
                "f0": {"type": "image", "link": "any://f/sp1/file9"},
                "a0": {"type": "link", "link": "any://o/sp1/obj1"}}});
        assert_eq!(
            Watcher::attributed_text(&attached),
            "see these\n[attachment link: any://o/sp1/obj1]\n\
             [attachment image: any://f/sp1/file9]"
        );
        let only_att = json!({"id": "h3", "text": "",
            "attachments": {"a0": {"type": "link", "link": "any://o/sp1/obj2"}}});
        assert_eq!(
            Watcher::attributed_text(&only_att),
            "[attachment link: any://o/sp1/obj2]"
        );

        let mut w = Watcher::new("bao");
        let mb: crate::broker::SharedMailbox = Default::default();
        w.live.insert("c1".into(), mb.clone());
        let m = json!({"id": "m9", "text": "done",
                       "agent": {"name": "trigger:x"}});
        assert!(matches!(w.on_message("c1", &m), WatchAction::Inject));
        let queued = mb.lock().unwrap().pop_front().unwrap();
        assert!(queued["text"]
            .as_str()
            .unwrap()
            .starts_with("[from agent \"trigger:x\""));
    }
}
