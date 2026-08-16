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
    pub last_duration_ms: Option<i64>,
    pub last_fuel: Option<i64>,
    pub last_cost_usd: Option<f64>,
    pub run_count: i64,
    pub consecutive_failures: i64,
    pub last_run_ref: Option<String>,
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
            last_duration_ms: None,
            last_fuel: None,
            last_cost_usd: None,
            run_count: 0,
            consecutive_failures: 0,
            last_run_ref: None,
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

    /// Run bookkeeping + the circuit breaker.
    pub fn record_run(&self, t: &mut Trigger, r: &RunResult) -> Value {
        let ts = (self.now)();
        t.last_run_at = Some(ts);
        t.last_status = Some(r.status.clone());
        t.last_duration_ms = Some(r.duration_ms);
        t.last_fuel = r.fuel;
        t.last_run_ref = r.trace_ref.clone();
        t.run_count += 1;
        if r.status == "ok" {
            t.consecutive_failures = 0;
        } else {
            t.consecutive_failures += 1;
            if t.consecutive_failures >= t.max_consecutive_failures {
                t.enabled = false;
                t.last_status = Some("auto_disabled".into());
            }
        }
        json!({"triggerId": t.id, "ts": ts, "status": r.status,
               "durationMs": r.duration_ms, "error": r.error,
               "traceRef": r.trace_ref, "fuel": r.fuel, "costUsd": Value::Null})
    }
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
        last_duration_ms: rec.get("lastDurationMs").and_then(|v| v.as_i64()),
        last_fuel: rec.get("lastFuel").and_then(|v| v.as_i64()),
        last_cost_usd: rec.get("lastCostUsd").and_then(|v| v.as_f64()),
        run_count: rec.get("runCount").and_then(|v| v.as_i64()).unwrap_or(0),
        consecutive_failures: rec
            .get("consecutiveFailures")
            .and_then(|v| v.as_i64())
            .unwrap_or(0),
        last_run_ref: rec
            .get("lastRunRef")
            .and_then(|v| v.as_str())
            .map(String::from),
        next_due: None,
    })
}

pub fn trigger_to_record(t: &Trigger) -> Value {
    json!({
        "name": t.name, "kind": t.kind, "spec": t.spec, "program": t.program,
        "args": t.args, "owner": t.owner, "enabled": t.enabled,
        "limits": t.limits, "maxConsecutiveFailures": t.max_consecutive_failures,
        "lastRunAt": t.last_run_at, "lastStatus": t.last_status,
        "lastDurationMs": t.last_duration_ms, "lastFuel": t.last_fuel,
        "lastCostUsd": t.last_cost_usd, "runCount": t.run_count,
        "consecutiveFailures": t.consecutive_failures,
        "lastRunRef": t.last_run_ref,
    })
}

pub fn rollup(t: &Trigger) -> Value {
    let fail_rate = if t.run_count > 0 {
        t.consecutive_failures as f64 / t.run_count as f64
    } else {
        0.0
    };
    json!({
        "id": t.id, "name": t.name, "kind": t.kind, "owner": t.owner,
        "enabled": t.enabled, "lastRunAt": t.last_run_at,
        "lastStatus": t.last_status, "lastDurationMs": t.last_duration_ms,
        "lastFuel": t.last_fuel, "lastCostUsd": t.last_cost_usd,
        "runCount": t.run_count, "consecutiveFailures": t.consecutive_failures,
        "failureRate": fail_rate, "lastRunRef": t.last_run_ref,
        "limits": t.limits,
    })
}

/// The standing background jobs (ADR-006 §2, ADR-007 §1b/§4).
pub fn standing_triggers(space: &str, chat_id: &str, brain_id: &str, owner: &str) -> Vec<Trigger> {
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
            json!({"space": space, "chatId": chat_id,
                             "brainId": brain_id}),
            owner,
            true,
        ),
        Trigger::cron(
            "linkgen",
            "memory link generation",
            3600.0,
            "agent:linkgen@v1",
            json!({"space": space, "brainId": brain_id}),
            owner,
            true,
        ),
        // §4.2 mechanisms ship DISABLED — each behind its eval (ADR-007)
        Trigger::cron(
            "decay",
            "salience decay",
            86400.0,
            "agent:decay@v1",
            json!({"space": space, "brainId": brain_id}),
            owner,
            false,
        ),
        Trigger::cron(
            "reflection",
            "reflection",
            86400.0,
            "agent:reflection@v1",
            json!({"space": space, "brainId": brain_id}),
            owner,
            false,
        ),
        Trigger::cron(
            "evolution",
            "memory evolution",
            21600.0,
            "agent:evolution@v1",
            json!({"space": space, "brainId": brain_id}),
            owner,
            false,
        ),
    ]
}

// --- the watcher (trigger #1) -------------------------------------------------

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
        Watcher { self_name: self_name.into(), ..Default::default() }
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

    /// The record's text, attributed: a foreign-agent message (a
    /// `trigger:*` nudge, a peer) gets a harness-authored `[from
    /// agent …]` line derived from the record's `agent.name` METADATA
    /// — the model's knowledge of the sender no longer rests on a
    /// spoofable convention inside the message text. Human messages
    /// pass through untouched. Prepended (not an arg) so the
    /// attribution survives into the persisted turn and every future
    /// boot window unchanged.
    pub fn attributed_text(record: &Value) -> String {
        let text = record.get("text").and_then(|t| t.as_str()).unwrap_or("");
        match record
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
        }
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
            mailbox.lock().unwrap().push_back(json!({
                "kind": "inject", "text": Self::attributed_text(record)}));
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

        let mut w = Watcher::new("bao");
        let mb: crate::broker::SharedMailbox = Default::default();
        w.live.insert("c1".into(), mb.clone());
        let m = json!({"id": "m9", "text": "done",
                       "agent": {"name": "trigger:x"}});
        assert!(matches!(w.on_message("c1", &m), WatchAction::Inject));
        let queued = mb.lock().unwrap().pop_front().unwrap();
        assert!(queued["text"].as_str().unwrap().starts_with("[from agent \"trigger:x\""));
    }
}
