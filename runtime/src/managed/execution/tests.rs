//! N2b-2 C1–C3: the execution service against a fake `any` behind the real
//! gated transport (with scripted subscription feeds), a scripted lane
//! runner, and — for the protocol paths — a real `Session` driven command by
//! command. Design §7 groups 1–7, 12 (scheduled), 14, 16, 17, plus the
//! stand-down write (E5, E10), the beats and the R9 parity rule (C1); groups
//! 8, 9, 11, 12 (chat), 18 and the chat run not live yet (C2); groups 12
//! (events), 13 and 19 (C3).
use super::*;
use crate::anyapi::{AnyError, Transport};
use crate::broker::SharedActivity;
use crate::managed::grant::{CloseReason, EvidenceGrade, GrantError};
use crate::managed::lanes::{inject_if, soft_break_live, test_hooks, LaneOutcome, MailboxError};
use crate::managed::protocol::{
    AdmissionDecision, CallbackSink, ClockCalibration, DrainResult, ReleaseReason, WorkerArtifact,
    WorkerCallback, WorkerCommand, WorkerFailure, WorkerLease, WorkerPhase, WorkerRequest,
    WorkerStartOptions,
};
use crate::managed::session::{
    Inbound, Internal, Owner, Preparedness, Session, SessionConfig, SharedOwner,
};
use crate::managed::transport::{FixtureObjects, GatedTransport, Phase, PhaseGate};
use crate::managed::{start_with, CancelToken, ManagedAgent, PreparationStatus};
use crate::triggers::{chat_watch_trigger, trigger_to_record};
use std::sync::mpsc;

const BUDGET: Duration = Duration::from_secs(10);
const PEER: &str = "peer";
const SPACE: &str = "ws";
const CHAT: &str = "chat-1";
const TRIGGERS: (&str, &str) = ("triggers-obj", "t_agent_triggers");
const RUNS: (&str, &str) = ("runs-obj", "t_agent_runs");
const SECRETS: (&str, &str) = ("sec-obj", "t_agent_secrets");
const TURNS: (&str, &str) = ("log-obj", "t_agent_turns");
const AGENT: &str = "bao";

fn wait_for(what: &str, mut predicate: impl FnMut() -> bool) {
    let deadline = Instant::now() + BUDGET;
    while !predicate() {
        assert!(Instant::now() < deadline, "timed out waiting for: {what}");
        std::thread::sleep(Duration::from_millis(2));
    }
}

fn addr((object_id, collection): (&str, &str)) -> StoreAddress {
    StoreAddress {
        object_id: object_id.into(),
        collection: collection.into(),
    }
}

#[derive(Default)]
struct Boot(AtomicU64);
impl BootClock for Boot {
    fn now_ms(&self) -> Result<u64, GrantError> {
        Ok(self.0.load(Ordering::SeqCst))
    }
}

// --- a fake `any`: records, beats, held and failing calls ----------------------------

type Matcher = Box<dyn Fn(&str, &str, &Value) -> bool + Send>;
/// A held call: what it matches, the "entered" signal, the release wait.
type Hold = (Matcher, mpsc::Sender<()>, mpsc::Receiver<()>);
type Rows = BTreeMap<(String, String), BTreeMap<String, Value>>;

/// Applies `modify` and answers `query` from what it holds, records every
/// call that reached it (so a call the gate denied is absent), and can hold
/// or fail one matching call.
#[derive(Default)]
struct Store {
    rows: Mutex<Rows>,
    calls: Mutex<Vec<(String, String, Value)>>,
    holds: Mutex<Vec<Hold>>,
    /// Matching calls fail once each (a 500 from the server).
    fails: Mutex<Vec<Matcher>>,
    /// Open subscriptions: the object they watch, the line feed.
    streams: Mutex<Vec<(String, mpsc::Sender<String>)>>,
}

/// A subscription: the lines the test pushes; while idle, a `ready` frame
/// every few milliseconds — the frames a live server sends — so a loop
/// blocked in the read reaches its per-frame checks. Ends when the test
/// ends the object's streams.
struct Feed {
    lines: mpsc::Receiver<String>,
    pending: VecDeque<String>,
}
impl Iterator for Feed {
    type Item = String;
    fn next(&mut self) -> Option<String> {
        if let Some(line) = self.pending.pop_front() {
            return Some(line);
        }
        match self.lines.recv_timeout(Duration::from_millis(5)) {
            Ok(line) => Some(line),
            Err(mpsc::RecvTimeoutError::Timeout) => {
                self.pending.extend(["data: {}".to_owned(), String::new()]);
                Some("event: ready".to_owned())
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => None,
        }
    }
}

/// A held call: `entered` fires when it reached the store, dropping the
/// handle (or `release`) lets it proceed.
struct Held {
    entered: mpsc::Receiver<()>,
    release: Option<mpsc::Sender<()>>,
}
impl Held {
    fn wait_entered(&self) {
        self.entered
            .recv_timeout(BUDGET)
            .expect("the held call never reached the store");
    }
    fn release(&mut self) {
        self.release.take();
    }
}

fn is_write_of(id: &'static str) -> impl Fn(&str, &str, &Value) -> bool + Send {
    move |_, path, body| path.ends_with("/modify") && body["records"][0]["id"] == id
}

impl Store {
    fn seed(&self, (object, dataset): (&str, &str), t: &Trigger) {
        self.rows
            .lock()
            .unwrap()
            .entry((object.into(), dataset.into()))
            .or_default()
            .insert(t.id.clone(), trigger_to_record(t));
    }
    fn row(&self, (object, dataset): (&str, &str), id: &str) -> Option<Value> {
        self.rows
            .lock()
            .unwrap()
            .get(&(object.to_owned(), dataset.to_owned()))
            .and_then(|rows| rows.get(id).cloned())
    }
    fn writes_of(&self, id: &str) -> usize {
        self.calls
            .lock()
            .unwrap()
            .iter()
            .filter(|(_, path, body)| path.ends_with("/modify") && body["records"][0]["id"] == id)
            .count()
    }
    fn beats(&self) -> Vec<Value> {
        self.calls
            .lock()
            .unwrap()
            .iter()
            .filter(|(_, path, _)| path == "/v1/events")
            .map(|(_, _, body)| body.clone())
            .collect()
    }
    fn seed_row(&self, (object, dataset): (&str, &str), id: &str, value: Value) {
        self.rows
            .lock()
            .unwrap()
            .entry((object.into(), dataset.into()))
            .or_default()
            .insert(id.into(), value);
    }
    fn rows(&self, (object, dataset): (&str, &str)) -> BTreeMap<String, Value> {
        self.rows
            .lock()
            .unwrap()
            .get(&(object.to_owned(), dataset.to_owned()))
            .cloned()
            .unwrap_or_default()
    }
    /// Every chat post that reached the store, in order.
    fn posts(&self) -> Vec<Value> {
        self.calls
            .lock()
            .unwrap()
            .iter()
            .filter(|(_, path, _)| path.ends_with("/chat/messages"))
            .map(|(_, _, body)| body.clone())
            .collect()
    }
    fn fail(&self, matcher: impl Fn(&str, &str, &Value) -> bool + Send + 'static) {
        self.fails.lock().unwrap().push(Box::new(matcher));
    }
    /// Subscriptions that reached the store for `object`.
    fn subscribed(&self, object: &str) -> usize {
        self.calls
            .lock()
            .unwrap()
            .iter()
            .filter(|(method, _, body)| method == "STREAM" && body["objectId"] == object)
            .count()
    }
    /// One frame to every open subscription of `object`.
    fn push(&self, object: &str, event: &str, data: Value) {
        for (watched, feed) in self.streams.lock().unwrap().iter() {
            if watched == object {
                for line in [
                    format!("event: {event}"),
                    format!("data: {data}"),
                    String::new(),
                ] {
                    let _ = feed.send(line);
                }
            }
        }
    }
    /// The server ends every subscription of `object`.
    fn end_streams(&self, object: &str) {
        self.streams
            .lock()
            .unwrap()
            .retain(|(watched, _)| watched != object);
    }
    fn hold(&self, matcher: impl Fn(&str, &str, &Value) -> bool + Send + 'static) -> Held {
        let (entered_tx, entered) = mpsc::channel();
        let (release, release_rx) = mpsc::channel();
        self.holds
            .lock()
            .unwrap()
            .push((Box::new(matcher), entered_tx, release_rx));
        Held {
            entered,
            release: Some(release),
        }
    }
    fn query(&self, body: &Value) -> Value {
        let key = (
            body["objectId"].as_str().unwrap_or("").to_owned(),
            body["dataset"].as_str().unwrap_or("").to_owned(),
        );
        let rows = self.rows.lock().unwrap();
        let records: Vec<Value> = rows
            .get(&key)
            .into_iter()
            .flatten()
            .filter(|(id, _)| body["filter"]["id"].as_str().is_none_or(|want| want == *id))
            .map(|(id, value)| {
                let mut rec = value.clone();
                rec["id"] = json!(id);
                rec
            })
            .collect();
        json!({ "records": records })
    }
    fn modify(&self, body: &Value) {
        let key = (
            body["objectId"].as_str().unwrap_or("").to_owned(),
            body["dataset"].as_str().unwrap_or("").to_owned(),
        );
        let mut rows = self.rows.lock().unwrap();
        let rows = rows.entry(key).or_default();
        for record in body["records"].as_array().cloned().unwrap_or_default() {
            let id = record["id"].as_str().unwrap_or("").to_owned();
            let row = rows.entry(id).or_insert_with(|| json!({}));
            for op in record["ops"].as_array().cloned().unwrap_or_default() {
                match (op["type"].as_str(), op["path"].as_str()) {
                    (Some("$set"), Some("")) => *row = op["value"].clone(),
                    (Some("$set"), Some(path)) => row[path] = op["value"].clone(),
                    (Some("$unset"), Some(path)) => {
                        if let Some(map) = row.as_object_mut() {
                            map.remove(path);
                        }
                    }
                    _ => {}
                }
            }
        }
    }
}

struct Wire(Arc<Store>);
impl Transport for Wire {
    fn send(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
    ) -> Result<(u16, Value), AnyError> {
        let body = body.cloned().unwrap_or(Value::Null);
        // a call is recorded once its effect is visible — a test that saw a
        // write recorded reads its row — and a held one when it arrives, so
        // a test sees it while it is held
        let record = || {
            self.0
                .calls
                .lock()
                .unwrap()
                .push((method.into(), path.into(), body.clone()));
        };
        // a query answers with the store as it was when the request arrived,
        // so a held query returns a stale snapshot (round A-r2 review B1)
        let snapshot = path.ends_with("/query").then(|| self.0.query(&body));
        let held = {
            let mut holds = self.0.holds.lock().unwrap();
            holds
                .iter()
                .position(|(matches, ..)| matches(method, path, &body))
                .map(|i| holds.remove(i))
        };
        let was_held = held.is_some();
        if let Some((_, entered, release)) = held {
            record();
            let _ = entered.send(());
            let _ = release.recv_timeout(BUDGET);
        }
        let failed = {
            let mut fails = self.0.fails.lock().unwrap();
            fails
                .iter()
                .position(|matches| matches(method, path, &body))
                .map(|i| fails.remove(i))
        };
        let reply = if failed.is_some() {
            Err(AnyError {
                status: 500,
                code: "internal".into(),
                message: "the fake server failed this call".into(),
            })
        } else if let Some(snapshot) = snapshot {
            Ok((200, snapshot))
        } else {
            if path.ends_with("/modify") {
                self.0.modify(&body);
            }
            Ok((200, json!({})))
        };
        if !was_held {
            record();
        }
        reply
    }
    fn open_stream(
        &self,
        path: &str,
        body: Option<&Value>,
    ) -> Result<Box<dyn Iterator<Item = String>>, AnyError> {
        let body = body.cloned().unwrap_or(Value::Null);
        let (feed, lines) = mpsc::channel();
        // registered before the call is recorded: a test that saw the
        // subscription can push to it at once
        self.0
            .streams
            .lock()
            .unwrap()
            .push((body["objectId"].as_str().unwrap_or("").to_owned(), feed));
        self.0
            .calls
            .lock()
            .unwrap()
            .push(("STREAM".into(), path.into(), body));
        Ok(Box::new(Feed {
            lines,
            pending: VecDeque::new(),
        }))
    }
}

// --- a scripted lane runner ------------------------------------------------------------

#[derive(Clone, Copy)]
enum Plan {
    Fail,
    RunPath,
    Unpersisted,
    /// An `error` run that died on the missing credential `MISSING_REF`.
    MissingKey,
}

const MISSING_REF: &str = "llm.key.missing";

/// Runs each job at once with an `ok` result, or as planned per work id; a
/// held work id blocks until released or interrupted (then `interrupted`).
#[derive(Default)]
struct Script {
    started: Mutex<Vec<String>>,
    /// Every run that started: (work id, run id, args, its mailbox).
    runs: Mutex<Vec<(String, String, Value, crate::broker::SharedMailbox)>>,
    plans: Mutex<BTreeMap<String, Plan>>,
    /// work id → (release, whether the hold ignores interrupts)
    holds: Mutex<BTreeMap<String, (mpsc::Receiver<()>, bool)>>,
}
impl Script {
    /// The next run of `work` blocks until the returned sender is dropped.
    fn hold(&self, work: &str) -> mpsc::Sender<()> {
        let (release, rx) = mpsc::channel();
        self.holds.lock().unwrap().insert(work.into(), (rx, false));
        release
    }
    /// Like `hold`, but an interrupt does not end it: a run blocked inside a
    /// host call, which returns only when the call does.
    fn hold_blocked(&self, work: &str) -> mpsc::Sender<()> {
        let (release, rx) = mpsc::channel();
        self.holds.lock().unwrap().insert(work.into(), (rx, true));
        release
    }
    fn plan(&self, work: &str, plan: Plan) {
        self.plans.lock().unwrap().insert(work.into(), plan);
    }
    fn starts(&self, work: &str) -> usize {
        self.started
            .lock()
            .unwrap()
            .iter()
            .filter(|w| *w == work)
            .count()
    }
    fn order(&self) -> Vec<String> {
        self.started.lock().unwrap().clone()
    }
    /// The `n`th run of `work` to start (0-based): run id, args, mailbox.
    fn run(&self, work: &str, n: usize) -> Option<(String, Value, crate::broker::SharedMailbox)> {
        self.runs
            .lock()
            .unwrap()
            .iter()
            .filter(|(w, ..)| w == work)
            .nth(n)
            .map(|(_, run, args, mailbox)| (run.clone(), args.clone(), mailbox.clone()))
    }
}

fn result(job: &LaneJob, status: &str) -> RunResult {
    let mut rr = RunResult::failed(String::new());
    rr.status = status.into();
    rr.error = None;
    rr.trace_ref = Some(job.run_id.clone());
    rr
}

impl LaneRunner for Script {
    fn run(
        &self,
        job: &LaneJob,
        _activity: SharedActivity,
        mut permit: StorePermit,
    ) -> LaneOutcome {
        if permit.attach().is_err() {
            return LaneOutcome {
                result: Err(LaneFailure::Cancelled),
                summary: None,
            };
        }
        let hold = self.holds.lock().unwrap().remove(&job.work_id);
        self.runs.lock().unwrap().push((
            job.work_id.clone(),
            job.run_id.clone(),
            job.args.clone(),
            job.mailbox.raw(),
        ));
        self.started.lock().unwrap().push(job.work_id.clone());
        let mut status = "ok";
        if let Some((release, blocked)) = hold {
            loop {
                if !blocked && job.interrupt.load(Ordering::SeqCst) {
                    status = "interrupted";
                    break;
                }
                if !matches!(
                    release.recv_timeout(Duration::from_millis(2)),
                    Err(mpsc::RecvTimeoutError::Timeout)
                ) {
                    if blocked && job.interrupt.load(Ordering::SeqCst) {
                        status = "interrupted";
                    }
                    break;
                }
            }
        }
        let plan = self.plans.lock().unwrap().get(&job.work_id).copied();
        let result = match plan {
            Some(Plan::RunPath) => Err(LaneFailure::RunPath("resolver: boom".into())),
            Some(Plan::Unpersisted) => {
                Err(LaneFailure::TraceUnpersisted("trace.unpersisted".into()))
            }
            Some(Plan::Fail) if status == "ok" => {
                let mut rr = result(job, "error");
                rr.error = Some("E: failed".into());
                Ok(rr)
            }
            Some(Plan::MissingKey) if status == "ok" => {
                let mut rr = result(job, "error");
                rr.error = Some("E: no key".into());
                rr.missing_secrets = vec![MISSING_REF.into()];
                Ok(rr)
            }
            _ => Ok(result(job, status)),
        };
        let summary = result
            .is_ok()
            .then(|| json!({"id": job.run_id, "title": job.spec, "device": null}));
        LaneOutcome { result, summary }
    }
}

// --- a trace store that records retention sweeps --------------------------------------

/// Only `expire` is exercised: each sweep's cut-offs, and whether the stop
/// flag was up.
#[derive(Default)]
struct Sweeps(Mutex<Vec<(Option<f64>, Option<f64>, Option<u64>)>>);
impl TraceStore for Sweeps {
    fn open_sink(&self, _run_id: &str) -> anyhow::Result<Box<dyn crate::tracestore::TraceSink>> {
        anyhow::bail!("not a run store")
    }
    fn write_run(
        &self,
        _run_id: &str,
        _records: &[Value],
        _blobs: &[(String, String)],
    ) -> anyhow::Result<()> {
        Ok(())
    }
    fn expire(
        &self,
        chat_program: &str,
        conversations_before: Option<f64>,
        jobs_before: Option<f64>,
        max_bytes: Option<u64>,
        _stop: &AtomicBool,
    ) -> anyhow::Result<usize> {
        assert_eq!(chat_program, "agent:toolcaller@v1");
        self.0
            .lock()
            .unwrap()
            .push((conversations_before, jobs_before, max_bytes));
        Ok(0)
    }
    fn list(&self) -> anyhow::Result<Vec<crate::tracestore::RunMeta>> {
        Ok(Vec::new())
    }
    fn load(&self, _run_id: &str) -> anyhow::Result<Vec<Value>> {
        Ok(Vec::new())
    }
    fn blobs(&self, _run_id: &str) -> anyhow::Result<BTreeMap<String, String>> {
        Ok(BTreeMap::new())
    }
}

// --- the rig ---------------------------------------------------------------------------

struct Rig {
    agent: ManagedAgent,
    shared: Arc<Shared>,
    store: Arc<Store>,
    client: Arc<Client>,
    gate: PhaseGate,
    script: Arc<Script>,
    boot: Arc<Boot>,
    wall: Arc<Mutex<f64>>,
    rss: Arc<AtomicU64>,
    sweeps: Arc<Sweeps>,
    service: Arc<ExecutionService>,
}

/// The loops run, but the 5 s passes are driven by the tests (`scan`,
/// `reconcile`); the dispatcher wakes every 20 ms, the beat thread polls
/// every 10 ms against the fake wall clock.
fn cadence() -> Cadence {
    Cadence {
        wake: Duration::from_millis(20),
        sample: Duration::from_secs(3600),
        scan: Duration::from_secs(3600),
        reconcile: Duration::from_secs(3600),
        beat: Duration::from_millis(10),
        reconnect: Duration::from_millis(20),
        break_grace: Duration::from_secs(3600),
        converge: Duration::from_millis(20),
        retention_first: Duration::from_secs(3600),
        retention_every: Duration::from_secs(3600),
    }
}

fn exec_config() -> ExecutionConfig {
    ExecutionConfig {
        space: SPACE.into(),
        chat: CHAT.into(),
        self_peer: PEER.into(),
        agent_name: AGENT.into(),
        triggers: addr(TRIGGERS),
        runs: addr(RUNS),
        secrets: addr(SECRETS),
        turns: addr(TURNS),
        code_space: "agent-space".into(),
        overlays: serde_json::Map::new(),
        retention: Retention::default(),
        cadence: cadence(),
    }
}

fn rig() -> Rig {
    rig_with(exec_config())
}

fn rig_with(config: ExecutionConfig) -> Rig {
    let (tx, rx) = mpsc::channel();
    let agent = start_with(CancelToken::new(), move |ctx| {
        tx.send(ctx.shared.clone()).unwrap();
        while !ctx.idle_until(Instant::now() + Duration::from_secs(300)) {}
        Ok(())
    });
    let shared = rx.recv_timeout(BUDGET).unwrap();
    let store = Arc::new(Store::default());
    let gate = PhaseGate::default();
    let gated = GatedTransport::new(
        Box::new(Wire(store.clone())),
        Arc::downgrade(&shared),
        [SPACE.to_owned()].into(),
        SPACE.into(),
        FixtureObjects {
            config: ("cfg-obj".into(), "t_agent_config".into()),
            secrets: (SECRETS.0.into(), SECRETS.1.into()),
            triggers: (TRIGGERS.0.into(), TRIGGERS.1.into()),
            runs: (RUNS.0.into(), RUNS.1.into()),
            turns: (TURNS.0.into(), TURNS.1.into()),
            chat: CHAT.into(),
            self_peer: PEER.into(),
        },
        gate.clone(),
    );
    let client = Arc::new(Client::with_transport(Box::new(gated)));
    let script = Arc::new(Script::default());
    let boot = Arc::new(Boot(AtomicU64::new(100)));
    let wall = Arc::new(Mutex::new(1_000.0));
    let rss = Arc::new(AtomicU64::new(64 << 20));
    let sweeps = Arc::new(Sweeps::default());
    let service = ExecutionService::start(
        &shared,
        config,
        ExecutionParts {
            runner: script.clone(),
            client: client.clone(),
            status: Default::default(),
            engine: wasmtime::Engine::default(),
            clock: boot.clone(),
            rss: {
                let rss = rss.clone();
                Arc::new(move || Some(rss.load(Ordering::SeqCst)).filter(|b| *b != u64::MAX))
            },
            now: {
                let wall = wall.clone();
                Arc::new(move || *wall.lock().unwrap())
            },
            config_store: None,
            traces: sweeps.clone(),
        },
    )
    .unwrap();
    Rig {
        agent,
        shared,
        store,
        client,
        gate,
        script,
        boot,
        wall,
        rss,
        sweeps,
        service,
    }
}

fn intent(rev: u64) -> ActivationIntent {
    ActivationIntent {
        activation_id: format!("intent-{rev}"),
        intent_revision: rev,
    }
}

fn grant(rev: u64, evidence_seq: u64, at: u64, grade: EvidenceGrade) -> OpenGrant {
    OpenGrant {
        intent: intent(rev),
        evidence_seq,
        observed_at: at,
        expires_at: at + 30_000,
        grade,
    }
}

/// A completed takeover: the responder stamped to this peer, the standing
/// built-ins as given.
fn outcome(standing: Vec<Trigger>) -> TakeoverOutcome {
    TakeoverOutcome {
        chat_watch_id: "chat-watch".into(),
        chat_watch: chat_watch_trigger("chat-watch", CHAT, PEER),
        standing,
        descriptors: 0,
    }
}

fn cron(id: &str, every_s: f64) -> Trigger {
    Trigger::cron(
        id,
        id,
        every_s,
        &format!("agent:{id}@v1"),
        json!({}),
        PEER,
        true,
    )
}

fn once(id: &str, at: f64) -> Trigger {
    let mut t = Trigger::cron(
        id,
        id,
        0.0,
        &format!("agent:{id}@v1"),
        json!({}),
        PEER,
        true,
    );
    t.kind = "once".into();
    t.spec = json!({ "at": at });
    t
}

impl Rig {
    /// What a real takeover writes before its outcome: the standing records
    /// and the responder, owned by this peer (the reconcile at Open reads them).
    fn takeover_writes(&self, outcome: &TakeoverOutcome) {
        for t in outcome.standing.iter().chain([&outcome.chat_watch]) {
            self.store.seed(TRIGGERS, t);
        }
    }
    /// Open on the service directly (the session's part done by hand: the
    /// transport in Execution, the grant on the rig's boot clock).
    fn open(&self, takeover: Option<&TakeoverOutcome>, grant: &OpenGrant) {
        if let Some(outcome) = takeover {
            self.takeover_writes(outcome);
        }
        self.gate.set(Phase::Execution);
        let clock = CalibratedBootClock::new(self.boot.clone(), 0, 0, 0).unwrap();
        self.service.open(grant, "s1", takeover, &clock);
    }
    fn open_activation(&self) {
        let passes = self.reconciles();
        self.open(
            Some(&outcome(vec![])),
            &grant(1, 1, 100, EvidenceGrade::Activation),
        );
        self.reconciled(passes + 1);
    }
    fn inner(&self) -> &Inner {
        &self.service.inner
    }
    fn reconciles(&self) -> u64 {
        self.inner().reconcile_passes.load(Ordering::SeqCst)
    }
    fn reconciled(&self, n: u64) {
        wait_for("a reconcile pass", || self.reconciles() >= n);
    }
    /// One housekeeping pass, on this thread.
    fn reconcile(&self) {
        assert!(
            self.inner().reconcile(&mut false),
            "the reconcile converged"
        );
    }
    /// One due scan and dispatch pass, on this thread.
    fn scan(&self) {
        self.inner().due_scan();
        self.inner().dispatch();
    }
    fn advance(&self, s: f64) {
        *self.wall.lock().unwrap() += s;
    }
    fn now(&self) -> f64 {
        *self.wall.lock().unwrap()
    }
    fn entry(&self, id: &str) -> Option<Trigger> {
        self.inner().lock().registry.get(id).cloned()
    }
    fn run_of(&self, trigger: &str) -> Option<String> {
        self.inner().lock().reserved.get(trigger).cloned()
    }
    fn wait_started(&self, work: &str, n: usize) {
        wait_for(&format!("{work} started {n}×"), || {
            self.script.starts(work) >= n
        });
    }
    fn wait_quiescent(&self) {
        wait_for("quiescence", || self.service.quiescent());
    }
    /// Every other stand-down obligation settled (the owner-clear, the
    /// `shutdown` beat, a beat in flight), so what still holds quiescence is
    /// the one under test.
    /// The session's two releases after a direct `close(true)`.
    fn release_standdown(&self) {
        self.service.release_owner_clear();
        self.service.release_shutdown_beats();
    }
    fn settle_standdown(&self) {
        wait_for("the stand-down write and the beats", || {
            let execution = self.agent.health().execution;
            !execution.standdown_pending
                && !execution.shutdown_beat_pending
                && !execution.beat_in_flight
        });
    }
    /// Two complete beat-thread passes from now: whatever it does with the
    /// current state, it has done (a pass in flight finishes first).
    fn beat_passes(&self, n: u64) {
        let from = self.inner().beat_passes.load(Ordering::SeqCst);
        wait_for("beat-thread passes", || {
            self.inner().beat_passes.load(Ordering::SeqCst) >= from + n
        });
    }
    fn live_interrupt(&self, run: &str) -> Option<bool> {
        self.shared
            .live_runs
            .lock()
            .unwrap()
            .get(run)
            .map(|l| l.interrupt.load(Ordering::SeqCst))
    }
}

// --- a real session over the rig ---------------------------------------------------------

#[derive(Default)]
struct Sink(Mutex<Vec<WorkerCallback>>);
impl CallbackSink for Sink {
    fn emit(&self, callback: WorkerCallback) {
        self.0.lock().unwrap().push(callback);
    }
}

/// The owner as the session sees it, over the rig: Prepared, the rig's
/// service built on request, takeovers recorded (the test posts their
/// outcome), the gate on the rig's transport, Stop's joins on the real
/// registry.
struct RigOwner {
    shared: Arc<Shared>,
    gate: PhaseGate,
    service: Arc<dyn Execution>,
    gates: Arc<Mutex<Vec<Phase>>>,
    spawned: Arc<Mutex<Vec<u64>>>,
}
impl Owner for RigOwner {
    fn preparedness(&self) -> Preparedness {
        Preparedness::Prepared
    }
    fn build_execution(&self, _clock: Arc<dyn BootClock>) -> Option<Arc<dyn Execution>> {
        Some(self.service.clone())
    }
    fn failed(&self) -> bool {
        self.shared.status.load(Ordering::Acquire) >> crate::managed::FAILURE_SHIFT != 0
    }
    fn mark_needs_setup(&self) {}
    fn spawn_takeover(&self, gen: u64, _superseded: Arc<AtomicBool>) -> bool {
        self.spawned.lock().unwrap().push(gen);
        true
    }
    fn cancelled(&self) -> bool {
        self.shared.cancel.is_cancelled()
    }
    fn gate(&self, phase: Phase) {
        self.gate.set(phase);
        self.gates.lock().unwrap().push(phase);
    }
    fn cancel(&self) {
        self.shared.cancel();
    }
    fn join_finished(&self) -> usize {
        let mut owned = self.shared.owned.lock().unwrap();
        let mut remaining = 0;
        let mut index = 0;
        while index < owned.len() {
            if owned[index].1.is_finished() {
                let (_, handle) = owned.swap_remove(index);
                let _ = handle.join();
            } else {
                remaining += 1;
                index += 1;
            }
        }
        remaining
    }
    fn publish(&self, word: u64) {
        self.shared.session_word.store(word, Ordering::Release);
    }
    fn released(&self) -> bool {
        false
    }
}

struct Driver {
    session: Session,
    sink: Arc<Sink>,
    gates: Arc<Mutex<Vec<Phase>>>,
    spawned: Arc<Mutex<Vec<u64>>>,
    next: u64,
    policy: u64,
}

fn lease() -> WorkerLease {
    WorkerLease {
        main_nonce: "nonce".into(),
        generation: 1,
        base_url: "http://127.0.0.1:7001".into(),
        auth_epoch: 1,
        account_id: "acc".into(),
        self_peer_id: PEER.into(),
    }
}

impl Driver {
    fn new(rig: &Rig) -> Self {
        Self::with_execution(rig, rig.service.clone())
    }
    /// The session over `service`, an `Execution` that may wrap the rig's.
    fn with_execution(rig: &Rig, service: Arc<dyn Execution>) -> Self {
        let gates: Arc<Mutex<Vec<Phase>>> = Default::default();
        let spawned: Arc<Mutex<Vec<u64>>> = Default::default();
        let sink = Arc::new(Sink::default());
        let owner = RigOwner {
            shared: rig.shared.clone(),
            gate: rig.gate.clone(),
            service,
            gates: gates.clone(),
            spawned: spawned.clone(),
        };
        let session = Session::new(
            SessionConfig {
                leased_origin: lease().base_url,
                space: SPACE.into(),
                fixture: None,
            },
            sink.clone(),
            rig.boot.clone(),
            Box::new(owner),
        );
        Driver {
            session,
            sink,
            gates,
            spawned,
            next: 0,
            policy: 0,
        }
    }
    fn send(&mut self, build: impl FnOnce(WorkerRequest) -> WorkerCommand) -> Vec<WorkerCallback> {
        self.next += 1;
        let request = WorkerRequest {
            request_id: self.next,
            session_id: "s1".into(),
            lease: lease(),
        };
        self.session.handle(Inbound::Command(build(request)));
        std::mem::take(&mut *self.sink.0.lock().unwrap())
    }
    fn tick(&mut self) -> Vec<WorkerCallback> {
        self.session.tick();
        std::mem::take(&mut *self.sink.0.lock().unwrap())
    }
    fn start(&mut self) -> Vec<WorkerCallback> {
        self.send(|request| WorkerCommand::Start {
            request,
            options: WorkerStartOptions {
                artifact: WorkerArtifact::AndroidPocV1,
                elapsed_realtime_at_start: 100,
            },
            calibration: ClockCalibration {
                native_before: 100,
                android_elapsed: 100,
                native_after: 100,
            },
        })
    }
    fn activate(&mut self, rev: u64) {
        let callbacks = self.send(|request| WorkerCommand::Activate {
            request,
            intent: intent(rev),
        });
        assert!(
            matches!(
                callbacks.last(),
                Some(WorkerCallback::ActivationReady { .. })
            ),
            "activated: {callbacks:?}"
        );
    }
    /// An Open; returns the takeover generation it spawned, if any.
    fn open(&mut self, grant: OpenGrant) -> Option<u64> {
        self.policy += 1;
        let policy_seq = self.policy;
        let before = self.spawned.lock().unwrap().len();
        self.send(|request| WorkerCommand::SetAdmission {
            request,
            policy_seq,
            decision: AdmissionDecision::Open(grant),
        });
        self.spawned.lock().unwrap().get(before).copied()
    }
    fn takeover_done(&mut self, gen: u64, outcome: TakeoverOutcome) -> Vec<WorkerCallback> {
        self.session
            .handle(Inbound::Internal(Internal::TakeoverDone {
                gen,
                outcome: Ok(outcome),
            }));
        std::mem::take(&mut *self.sink.0.lock().unwrap())
    }
    fn close(&mut self, reason: CloseReason, barrier: Option<u64>) -> Vec<WorkerCallback> {
        self.policy += 1;
        let policy_seq = self.policy;
        self.send(|request| WorkerCommand::SetAdmission {
            request,
            policy_seq,
            decision: AdmissionDecision::Close {
                reason,
                revoke_through_intent_revision: barrier,
            },
        })
    }
    fn release(&mut self, barrier: u64) -> Vec<WorkerCallback> {
        self.send(|request| WorkerCommand::Release {
            request,
            reason: ReleaseReason::UserStop,
            deadline_elapsed_realtime: 10_100,
            revoke_through_intent_revision: barrier,
        })
    }
    fn stop(&mut self) -> Vec<WorkerCallback> {
        self.send(|request| WorkerCommand::Stop {
            request,
            deadline_elapsed_realtime: 10_100,
        })
    }
    /// Start, Activate(1), the activation Open and its takeover's outcome:
    /// AppliedOpen with the responder owned by this peer.
    fn open_session(&mut self, rig: &Rig) {
        self.start();
        self.activate(1);
        let passes = rig.reconciles();
        let gen = self
            .open(grant(1, 1, 100, EvidenceGrade::Activation))
            .expect("an activation Open takes over");
        rig.takeover_writes(&outcome(vec![]));
        let callbacks = self.takeover_done(gen, outcome(vec![]));
        assert!(
            matches!(
                callbacks.as_slice(),
                [WorkerCallback::AdmissionApplied {
                    decision: AdmissionDecision::Open(_),
                    ..
                }]
            ),
            "AppliedOpen: {callbacks:?}"
        );
        rig.reconciled(passes + 1);
    }
    fn gate(&self) -> Option<Phase> {
        self.gates.lock().unwrap().last().copied()
    }
    /// Tick until a callback matching `want` arrives.
    fn tick_until(&mut self, what: &str, want: impl Fn(&WorkerCallback) -> bool) {
        let mut seen = Vec::new();
        let deadline = Instant::now() + BUDGET;
        loop {
            let callbacks = self.tick();
            if callbacks.iter().any(&want) {
                return;
            }
            seen.extend(callbacks);
            assert!(Instant::now() < deadline, "no {what}; saw {seen:?}");
            std::thread::sleep(Duration::from_millis(5));
        }
    }
}

fn is_released(c: &WorkerCallback) -> bool {
    matches!(c, WorkerCallback::Released { .. })
}

// --- 1: installation ---------------------------------------------------------------------

/// Production stays closed until C4: the real owner builds no service, so a
/// prepared, bound worker reports `ready: false` and can never open.
#[test]
fn production_builds_no_execution_service_and_is_never_ready() {
    let (internal, _rx) = mpsc::channel();
    let shared = Arc::new(Shared::new(CancelToken::new(), Instant::now(), None));
    shared
        .preparation_status
        .store(PreparationStatus::Prepared.atomic(), Ordering::Release);
    let config = SessionConfig {
        leased_origin: lease().base_url,
        space: SPACE.into(),
        fixture: None,
    };
    let owner = SharedOwner {
        shared: shared.clone(),
        internal,
        config: config.clone(),
    };
    assert!(owner.build_execution(Arc::new(Boot::default())).is_none());
    let sink = Arc::new(Sink::default());
    let mut session = Session::new(
        config,
        sink.clone(),
        Arc::new(Boot(AtomicU64::new(100))),
        Box::new(owner),
    );
    session.handle(Inbound::Command(WorkerCommand::Start {
        request: WorkerRequest {
            request_id: 1,
            session_id: "s1".into(),
            lease: lease(),
        },
        options: WorkerStartOptions {
            artifact: WorkerArtifact::AndroidPocV1,
            elapsed_realtime_at_start: 100,
        },
        calibration: ClockCalibration {
            native_before: 100,
            android_elapsed: 100,
            native_after: 100,
        },
    }));
    let status = session.status();
    assert_eq!(status.phase, WorkerPhase::Prepared);
    assert!(!status.ready, "no execution service: never ready");
    assert!(!ExecutionSnapshot::from_word(shared.execution_word.load(Ordering::SeqCst)).installed);
}

/// The session asks for the service once, when preparation settled
/// Prepared, before its first Prepared Status: that Status says `ready`.
#[test]
fn the_session_installs_the_built_service_and_announces_ready() {
    let rig = rig();
    let mut driver = Driver::new(&rig);
    let callbacks = driver.start();
    assert!(matches!(
        callbacks.as_slice(),
        [
            WorkerCallback::Accepted { .. },
            WorkerCallback::Status { status, .. }
        ] if status.ready && status.phase == WorkerPhase::Prepared
    ));
    let health = rig.agent.health();
    assert!(health.execution.installed);
    assert!(!health.execution.open);
    assert!(
        !health.lane_admission_open,
        "nothing admitted before AppliedOpen"
    );
    wait_for("the service threads", || {
        let h = rig.agent.health();
        [h.service, h.presence, h.housekeeping, h.standdown]
            .iter()
            .all(|t| t.phase != crate::managed::TaskPhase::NotStarted)
    });
}

// --- 2: the stand-down write against takeovers (E5) ----------------------------------------

/// A new activation's takeover does not start while the previous
/// activation's stand-down write is in flight: the owner-clear never
/// crosses a newer takeover's verification.
#[test]
fn a_takeover_waits_for_the_held_stand_down_write() {
    let rig = rig();
    let mut driver = Driver::new(&rig);
    driver.open_session(&rig);
    let mut held = rig.store.hold(is_write_of("chat-watch"));
    driver.close(CloseReason::OwnershipLost, Some(1));
    held.wait_entered();
    assert!(!rig.service.standdown_idle());
    driver.activate(2);
    let spawned = driver.open(grant(2, 2, 100, EvidenceGrade::Activation));
    assert_eq!(spawned, None, "deferred behind the stand-down write");
    driver.tick();
    assert_eq!(driver.spawned.lock().unwrap().len(), 1);
    held.release();
    wait_for("the stand-down write settled", || {
        rig.service.standdown_idle()
    });
    driver.tick();
    assert_eq!(
        driver.spawned.lock().unwrap().len(),
        2,
        "the takeover starts after the write"
    );
    let row = rig.store.row(TRIGGERS, "chat-watch").unwrap();
    assert_eq!(row["owner"], "", "the owner was cleared on the record");
}

/// The stand-down write waits for a superseded takeover still in flight: its
/// one whole-record `owner = self` write lands first, the clear after it.
#[test]
fn the_stand_down_write_waits_for_a_superseded_takeover() {
    let rig = rig();
    let mut driver = Driver::new(&rig);
    driver.open_session(&rig);
    driver.close(CloseReason::SyncLoss, None);
    let gen = driver
        .open(grant(1, 2, 100, EvidenceGrade::Activation))
        .expect("a reopen after a close takes over again");
    driver.close(CloseReason::OwnershipLost, Some(1));
    wait_for("the shutdown beat", || {
        !rig.agent.health().execution.shutdown_beat_pending
    });
    driver.tick();
    // not released while a takeover is in flight: the stand-down thread
    // writes nothing without the release (state, not timing)
    assert!(rig.inner().owner_clear_due.load(Ordering::SeqCst));
    assert!(!rig.inner().owner_clear_released.load(Ordering::SeqCst));
    assert_eq!(
        rig.store.writes_of("chat-watch"),
        0,
        "not released while a takeover is in flight"
    );
    assert!(!rig.service.quiescent());
    driver.takeover_done(gen, outcome(vec![]));
    wait_for("the stand-down write", || {
        rig.store.writes_of("chat-watch") == 1
    });
    rig.wait_quiescent();
    assert_eq!(rig.store.row(TRIGGERS, "chat-watch").unwrap()["owner"], "");
}

// --- 3: quiescence ---------------------------------------------------------------------------

/// Released waits for a report callback held in its bookkeeping write; the
/// transport stays in Execution until it returned.
#[test]
fn release_waits_for_a_held_report_callback() {
    let rig = rig();
    rig.store.seed(TRIGGERS, &once("q-once", 900.0));
    let mut driver = Driver::new(&rig);
    driver.open_session(&rig);
    let mut held = rig.store.hold(is_write_of("q-once"));
    rig.scan();
    held.wait_entered();
    let callbacks = driver.release(1);
    assert!(!callbacks.iter().any(is_released));
    rig.settle_standdown();
    driver.tick();
    assert!(!driver.tick().iter().any(is_released));
    assert!(!rig.service.quiescent(), "the held callback counts");
    assert_eq!(rig.gate.get(), Phase::Execution);
    held.release();
    driver.tick_until("Released", is_released);
    assert_eq!(driver.gate(), Some(Phase::Preparation));
    let row = rig.store.row(TRIGGERS, "q-once").unwrap();
    assert_eq!(row["enabled"], false);
    assert_eq!(row["lastStatus"], "ok");
}

/// The take→register gap (E21): a job out of its inbox and not yet a live
/// run still holds quiescence; the Release's revocation reaches it there, it
/// never runs, and its once shot is restored.
#[test]
fn the_take_to_register_gap_holds_quiescence_and_a_never_started_once_is_restored() {
    let rig = rig();
    rig.store.seed(TRIGGERS, &once("q-gap", 900.0));
    let mut driver = Driver::new(&rig);
    driver.open_session(&rig);
    let (entered_tx, entered) = mpsc::channel();
    let (release, release_rx) = mpsc::channel::<()>();
    test_hooks::hold_after_take("q-gap", move || {
        let _ = entered_tx.send(());
        let _ = release_rx.recv_timeout(BUDGET);
    });
    rig.scan();
    entered.recv_timeout(BUDGET).unwrap();
    let consumed = rig.entry("q-gap").unwrap();
    assert!(!consumed.enabled, "consumed at bind");
    driver.release(1);
    rig.settle_standdown();
    driver.tick();
    assert!(
        !driver.tick().iter().any(is_released),
        "the job in the gap counts"
    );
    assert!(!rig.service.quiescent());
    drop(release);
    driver.tick_until("Released", is_released);
    assert_eq!(rig.script.starts("q-gap"), 0, "never started");
    let restored = rig.entry("q-gap").unwrap();
    assert!(restored.enabled, "the shot is restored");
    assert_eq!(restored.last_run_at, None);
    assert_eq!(rig.store.writes_of("q-gap"), 0, "nothing persisted");
}

/// Released waits for the stand-down write itself.
#[test]
fn release_waits_for_the_held_stand_down_write() {
    let rig = rig();
    let mut driver = Driver::new(&rig);
    driver.open_session(&rig);
    let mut held = rig.store.hold(is_write_of("chat-watch"));
    driver.release(1);
    held.wait_entered();
    assert!(!driver.tick().iter().any(is_released));
    assert!(!rig.agent.health().execution.open);
    assert!(rig.agent.health().execution.standdown_pending);
    held.release();
    driver.tick_until("Released", is_released);
    assert_eq!(rig.store.row(TRIGGERS, "chat-watch").unwrap()["owner"], "");
}

/// Housekeeping's adoption stamp (`owner` = this peer) in flight holds
/// quiescence: it lands before Released, never after main's release.
#[test]
fn release_waits_for_a_held_adoption_stamp() {
    let rig = rig();
    let mut driver = Driver::new(&rig);
    driver.open_session(&rig);
    let mut unowned = cron("h-adopt", 600.0);
    unowned.owner.clear();
    rig.store.seed(TRIGGERS, &unowned);
    let mut held = rig.store.hold(is_write_of("h-adopt"));
    rig.inner().reconcile_now.store(true, Ordering::SeqCst);
    rig.inner().wake();
    held.wait_entered();
    driver.release(1);
    rig.settle_standdown();
    driver.tick();
    assert!(!driver.tick().iter().any(is_released));
    assert!(!rig.service.quiescent(), "the held stamp counts");
    held.release();
    driver.tick_until("Released", is_released);
    assert_eq!(rig.store.row(TRIGGERS, "h-adopt").unwrap()["owner"], PEER);
}

/// E10: the transport went back to Preparation after an idle non-revoking
/// Close; the Release that follows re-authorizes the execution routes for
/// its stand-down write (admission stays closed), and returns them after.
#[test]
fn a_release_after_a_quiescent_close_reauthorizes_its_stand_down_write() {
    let rig = rig();
    let mut driver = Driver::new(&rig);
    driver.open_session(&rig);
    driver.close(CloseReason::SyncLoss, None);
    rig.wait_quiescent();
    driver.tick();
    assert_eq!(driver.gate(), Some(Phase::Preparation));
    let returned = driver.gates.lock().unwrap().len();
    driver.release(1);
    // the write may already have landed and the Release reached quiescence
    // (gate back to Preparation) by now: the history shows the authorization
    assert_eq!(
        driver.gates.lock().unwrap().get(returned),
        Some(&Phase::Execution),
        "cleanup authorization"
    );
    assert!(!rig.shared.pool.admission_open(), "admission stays closed");
    driver.tick_until("Released", is_released);
    assert_eq!(
        rig.store.writes_of("chat-watch"),
        1,
        "the write reached the store"
    );
    assert_eq!(rig.store.row(TRIGGERS, "chat-watch").unwrap()["owner"], "");
    assert_eq!(driver.gate(), Some(Phase::Preparation));
}

// --- 4: once shots -------------------------------------------------------------------------

/// Consumed in memory at bind (`enabled: false`, as desktop's ticker does),
/// and the reserved entry sits reconciliation out (A3), so a run held across
/// two reconciles of the still-`enabled: true` row binds once; the report
/// persists the shot consumed.
#[test]
fn a_once_shot_binds_once_across_reconciles_and_persists_consumed() {
    let rig = rig();
    rig.store.seed(TRIGGERS, &once("o-held", 900.0));
    rig.store.seed(TRIGGERS, &once("o-fast", 900.0));
    rig.open_activation();
    let release = rig.script.hold("o-held");
    rig.scan();
    rig.wait_started("o-held", 1);
    assert!(!rig.entry("o-held").unwrap().enabled);
    for _ in 0..2 {
        rig.reconcile();
        rig.scan();
    }
    assert!(
        !rig.entry("o-held").unwrap().enabled,
        "a reserved entry sits reconciliation out (A3): still consumed"
    );
    assert_eq!(rig.script.starts("o-held"), 1, "no second bind");
    drop(release);
    rig.wait_started("o-fast", 1);
    rig.wait_quiescent();
    for id in ["o-held", "o-fast"] {
        let row = rig.store.row(TRIGGERS, id).unwrap();
        assert_eq!(row["enabled"], false, "{id} persisted consumed");
        assert_eq!(row["lastRunAt"], json!(rig.now()));
        assert_eq!(row["lastStatus"], "ok");
        assert!(!rig.entry(id).unwrap().enabled);
    }
    rig.reconcile();
    rig.scan();
    assert_eq!(rig.script.starts("o-held") + rig.script.starts("o-fast"), 2);
}

// --- 5: arming and eligibility -------------------------------------------------------------

/// An adopted cron (no deadline on its row) is armed forward by the due
/// scan on the real entry, and fires once it is due; disabled crons never
/// reach the queue, so 65 of them make no work and no capacity fault.
#[test]
fn adopted_crons_arm_on_the_real_entry_and_disabled_ones_queue_nothing() {
    let rig = rig();
    rig.store.seed(TRIGGERS, &cron("c-user", 60.0));
    for i in 0..=MAX_PENDING {
        let mut t = cron(&format!("c-off-{i}"), 1.0);
        t.enabled = false;
        rig.store.seed(TRIGGERS, &t);
    }
    rig.open_activation();
    assert_eq!(rig.entry("c-user").unwrap().next_due, None);
    rig.scan();
    assert_eq!(
        rig.entry("c-user").unwrap().next_due,
        Some(rig.now() + 60.0),
        "armed forward"
    );
    assert_eq!(rig.script.starts("c-user"), 0);
    rig.advance(61.0);
    rig.scan();
    rig.wait_started("c-user", 1);
    rig.wait_quiescent();
    assert_eq!(
        rig.entry("c-user").unwrap().next_due,
        Some(rig.now() + 60.0)
    );
    let health = rig.agent.health();
    assert_eq!(health.capacity_faults, 0);
    assert!(!health.execution.capacity_fault);
    assert!(rig.script.order().iter().all(|w| !w.starts_with("c-off")));
}

/// Heartbeat priority among due scheduled work, and a trigger disabled
/// between its queueing and its bind consumes nothing and never runs (E20).
#[test]
fn the_heartbeat_binds_first_and_a_trigger_disabled_after_queueing_never_runs() {
    let rig = rig();
    let mut heartbeat = cron("hb", 60.0);
    heartbeat.program = "android-poc-heartbeat@v1".into();
    rig.store.seed(TRIGGERS, &once("busy", 900.0));
    rig.store.seed(TRIGGERS, &cron("job", 60.0));
    rig.store.seed(TRIGGERS, &heartbeat);
    rig.open_activation();
    let release = rig.script.hold("busy");
    rig.scan(); // busy runs, the crons arm
    rig.wait_started("busy", 1);
    rig.advance(61.0);
    rig.scan(); // both due: queued behind the busy lane
    assert_eq!(rig.inner().lock().queue.len(), 2);
    rig.inner().lock().registry.get_mut("job").unwrap().enabled = false;
    drop(release);
    rig.wait_started("hb", 1);
    rig.wait_quiescent();
    assert_eq!(rig.script.order(), ["busy", "hb"]);
    assert_eq!(rig.script.starts("job"), 0, "disabled before its bind");
    assert_eq!(rig.store.writes_of("job"), 0);
    assert!(is_heartbeat("working:android-poc-heartbeat@v2", SPACE));
    assert!(is_heartbeat("ws:android-poc-heartbeat@v1", SPACE));
    assert!(!is_heartbeat("agent:android-poc-heartbeat@v1", SPACE));
    assert!(!is_heartbeat("android-poc-heartbeats@v1", SPACE));
}

// --- 6/7: the dispatch predicate and the offer refusal ---------------------------------------

/// Nothing scans before an Open; an expired grant (on the calibrated boot
/// clock) and a held gate cause admit nothing; the next fresh grant and an
/// open gate dispatch what waited.
#[test]
fn dispatch_needs_open_a_fresh_grant_and_an_open_gate() {
    let rig = rig();
    rig.store.seed(TRIGGERS, &once("p-once", 900.0));
    rig.scan();
    assert!(
        rig.inner().lock().registry.is_empty(),
        "nothing before an Open"
    );
    rig.open_activation();
    rig.boot.0.store(40_000, Ordering::SeqCst);
    rig.scan();
    assert_eq!(rig.inner().lock().queue.len(), 1, "queued");
    assert_eq!(rig.script.starts("p-once"), 0, "the grant expired");
    rig.shared.pool.close_gate(GateCause::Resource);
    rig.open(None, &grant(1, 2, 40_000, EvidenceGrade::Renewal));
    rig.scan();
    assert_eq!(
        rig.script.starts("p-once"),
        0,
        "another cause holds the gate"
    );
    rig.shared.pool.open_gate(GateCause::Resource);
    rig.scan();
    rig.wait_started("p-once", 1);
}

/// The one reachable offer refusal — the pool closed between `acquire` and
/// `offer` — finalizes the job at once: never run, reservation released, the
/// once shot restored, nothing in flight (D13).
#[test]
fn an_offer_refused_after_acquire_is_finalized_cancelled_at_once() {
    let rig = rig();
    rig.store.seed(TRIGGERS, &once("r-once", 900.0));
    rig.open_activation();
    let lanes = rig.inner().lanes.get().unwrap();
    // under the lock throughout: the dispatcher thread cannot bind it first
    let mut st = rig.inner().lock();
    let work = PendingWork {
        id: "r-once".into(),
        lane: Lane::Scheduled,
        heartbeat: false,
    };
    st.queue.push(work, &rig.shared.pool).unwrap();
    let (work, permit) = st.queue.admit(&rig.shared.pool).unwrap();
    rig.shared.pool.close();
    rig.inner().bind(&mut st, lanes, work, permit);
    assert!(st.reserved.is_empty());
    assert!(st.jobs.is_empty());
    let entry = st.registry.get("r-once").unwrap();
    assert!(entry.enabled);
    assert_eq!(entry.last_run_at, None);
    drop(st);
    assert_eq!(rig.inner().inflight.load(Ordering::SeqCst), 0);
    assert_eq!(rig.script.starts("r-once"), 0);
}

// --- 12: report bookkeeping (scheduled) ---------------------------------------------------

/// `finish_run` per outcome: ok, an error result, a run-path failure and an
/// unlanded trace all record a run (the breaker counts the failures); the
/// summary is published for the runs whose trace landed; three failures
/// auto-disable the trigger, which then never runs again.
#[test]
fn every_report_outcome_records_its_run_and_the_breaker_trips() {
    let rig = rig();
    for id in ["f-ok", "f-err", "f-path", "f-trace"] {
        rig.store.seed(TRIGGERS, &cron(id, 60.0));
    }
    rig.script.plan("f-err", Plan::Fail);
    rig.script.plan("f-path", Plan::RunPath);
    rig.script.plan("f-trace", Plan::Unpersisted);
    rig.open_activation();
    rig.scan();
    let round = |n: usize| {
        rig.advance(61.0);
        rig.scan();
        for id in ["f-ok", "f-err", "f-path", "f-trace"] {
            rig.wait_started(id, n);
        }
        rig.wait_quiescent();
    };
    round(1);
    let row = |id: &str| rig.store.row(TRIGGERS, id).unwrap();
    assert_eq!(row("f-ok")["lastStatus"], "ok");
    assert_eq!(row("f-ok")["consecutiveFailures"], 0);
    for id in ["f-err", "f-path", "f-trace"] {
        assert_eq!(row(id)["lastStatus"], "error", "{id}");
        assert_eq!(row(id)["consecutiveFailures"], 1, "{id}");
        assert_eq!(row(id)["lastRunAt"], json!(rig.now()), "{id}");
    }
    let runs = rig.store.rows.lock().unwrap()[&(RUNS.0.to_owned(), RUNS.1.to_owned())].clone();
    let triggers: BTreeSet<String> = runs
        .values()
        .map(|r| r["triggerId"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(
        triggers,
        ["f-err", "f-ok"].map(String::from).into(),
        "a summary for each landed trace"
    );
    assert!(runs.values().all(|r| r["device"] == PEER));
    round(2);
    round(3);
    for id in ["f-err", "f-path", "f-trace"] {
        assert_eq!(row(id)["lastStatus"], "auto_disabled", "{id}");
        assert_eq!(row(id)["enabled"], false, "{id}");
    }
    let before = rig.script.starts("f-err");
    rig.advance(61.0);
    rig.scan();
    rig.wait_started("f-ok", 4);
    rig.wait_quiescent();
    assert_eq!(
        rig.script.starts("f-err"),
        before,
        "a tripped breaker never runs"
    );
}

// --- 14: the memory report -------------------------------------------------------------------

/// The worker reports `VmRSS` and its boot-clock sample time; a failed
/// sample keeps the previous one (main judges its age). Nothing closes.
#[test]
fn health_reports_the_rss_sample_and_its_boot_clock_time() {
    let rig = rig();
    // the dispatcher's own first sample (at start, then hourly here) first
    wait_for("the first sample", || {
        rig.agent.health().rss_sampled_at_ms == Some(100)
    });
    rig.rss.store(123_456_789, Ordering::SeqCst);
    rig.boot.0.store(5_000, Ordering::SeqCst);
    rig.inner().sample_rss();
    let health = rig.agent.health();
    assert_eq!(health.rss_bytes, Some(123_456_789));
    assert_eq!(health.rss_sampled_at_ms, Some(5_000));
    rig.rss.store(u64::MAX, Ordering::SeqCst); // the sampler fails
    rig.boot.0.store(25_000, Ordering::SeqCst);
    rig.inner().sample_rss();
    let health = rig.agent.health();
    assert_eq!(health.rss_bytes, Some(123_456_789));
    assert_eq!(
        health.rss_sampled_at_ms,
        Some(5_000),
        "stale, for main to judge"
    );
    assert!(health.failures.is_empty(), "nothing closes on memory");
    assert!(!health.execution.open);
}

// --- 16: reopen semantics ------------------------------------------------------------------

/// A renewal refreshes the grant only; an Open with a takeover is a new
/// activation: the standing entries and the responder come from it, the
/// device-pinned user entries stay.
#[test]
fn a_renewal_keeps_the_activation_and_a_takeover_starts_a_new_one() {
    let rig = rig();
    rig.store.seed(TRIGGERS, &cron("u-pin", 600.0));
    let mut first = standing_triggers(SPACE, CHAT, PEER);
    first[0].next_due = Some(4_000.0);
    let passes = rig.reconciles();
    rig.open(
        Some(&outcome(first.clone())),
        &grant(1, 1, 100, EvidenceGrade::Activation),
    );
    rig.reconciled(passes + 1);
    assert_eq!(rig.agent.health().execution.activation, 1);
    assert!(rig.agent.health().execution.owns_chat);
    rig.open(None, &grant(1, 2, 100, EvidenceGrade::Renewal));
    assert_eq!(rig.agent.health().execution.activation, 1, "a renewal");
    assert_eq!(rig.entry(&first[0].id).unwrap().next_due, Some(4_000.0));
    rig.service.close(false);
    let mut second = standing_triggers(SPACE, CHAT, PEER);
    second[0].next_due = Some(9_000.0);
    rig.open(
        Some(&outcome(second)),
        &grant(1, 3, 100, EvidenceGrade::Activation),
    );
    wait_for("the activation word", || {
        rig.agent.health().execution.activation == 2
    });
    assert_eq!(rig.entry(&first[0].id).unwrap().next_due, Some(9_000.0));
    assert!(rig.entry("u-pin").is_some(), "the pinned user entry stays");
    assert!(rig.entry("chat-watch").is_some());
}

// --- 17 and R9: Stop, Release and a revoking Close with a live run ---------------------------

/// Desktop parity (user ruling 2026-11-06, R9): a revoking Close lets the
/// live run finish and its bookkeeping land; the Release that follows
/// interrupts what is still live.
#[test]
fn a_revoking_close_lets_the_live_run_finish_and_release_interrupts() {
    let rig = rig();
    rig.store.seed(TRIGGERS, &once("live-a", 900.0));
    rig.store.seed(TRIGGERS, &once("live-b", 900.0));
    let mut driver = Driver::new(&rig);
    driver.open_session(&rig);
    let release_a = rig.script.hold("live-a");
    let _keep_b = rig.script.hold("live-b");
    rig.scan();
    rig.wait_started("live-a", 1);
    let run = rig.run_of("live-a").unwrap();
    // the close returns after its stand-down, which interrupts synchronously
    // if at all: no wait needed to see that it did not
    driver.close(CloseReason::OwnershipLost, Some(1));
    assert_eq!(rig.live_interrupt(&run), Some(false), "not interrupted");
    drop(release_a);
    wait_for("live-a's bookkeeping", || {
        rig.store.writes_of("live-a") == 1
    });
    assert_eq!(
        rig.store.row(TRIGGERS, "live-a").unwrap()["lastStatus"],
        "ok"
    );
    assert_eq!(
        rig.script.starts("live-b"),
        0,
        "the close cancelled the queue"
    );
    // a fresh activation runs live-b, held; Release interrupts it
    driver.activate(2);
    let gen = match driver.open(grant(2, 2, 100, EvidenceGrade::Activation)) {
        Some(gen) => gen,
        None => {
            wait_for("the stand-down write", || rig.service.standdown_idle());
            driver.tick();
            *driver.spawned.lock().unwrap().last().unwrap()
        }
    };
    rig.takeover_writes(&outcome(vec![]));
    driver.takeover_done(gen, outcome(vec![]));
    rig.reconcile();
    rig.scan();
    rig.wait_started("live-b", 1);
    driver.release(2);
    driver.tick_until("Released", is_released);
    assert_eq!(
        rig.store.row(TRIGGERS, "live-b").unwrap()["lastStatus"],
        "interrupted"
    );
}

/// Stop: stand-down, the live run interrupted, its bookkeeping and the
/// stand-down write settled, then the owner cancelled and every owned
/// thread joined: Drained(DRAINED).
#[test]
fn stop_interrupts_settles_then_cancels_and_drains() {
    let rig = rig();
    rig.store.seed(TRIGGERS, &once("s-live", 900.0));
    let mut driver = Driver::new(&rig);
    driver.open_session(&rig);
    let _hold = rig.script.hold("s-live");
    rig.scan();
    rig.wait_started("s-live", 1);
    driver.stop();
    driver.tick_until("Drained", |c| {
        matches!(
            c,
            WorkerCallback::Drained {
                result: DrainResult::Drained,
                ..
            }
        )
    });
    assert_eq!(
        rig.store.row(TRIGGERS, "s-live").unwrap()["lastStatus"],
        "interrupted",
        "bookkeeping landed before the cancel"
    );
    assert_eq!(rig.store.row(TRIGGERS, "chat-watch").unwrap()["owner"], "");
    assert!(rig.shared.cancel.is_cancelled());
    let health = rig.agent.health();
    assert!(health.failures.is_empty(), "{:?}", health.failures);
}

// --- beats -------------------------------------------------------------------------------

/// Responder beats only while open, with the session and the activation
/// pair; one `shutdown` beat for an activation a revoking close ended, none
/// for a non-revoking close.
#[test]
fn beats_run_only_while_open_and_a_revoking_close_sends_one_shutdown() {
    let rig = rig();
    let states = |rig: &Rig| -> Vec<String> {
        rig.store
            .beats()
            .iter()
            .map(|b| b["data"]["state"].as_str().unwrap().to_owned())
            .collect()
    };
    rig.open_activation();
    wait_for("the first beat", || !rig.store.beats().is_empty());
    let first = rig.store.beats()[0].clone();
    assert_eq!(first["type"], "bao.status");
    assert_eq!(first["target"], PEER);
    assert_eq!(first["data"]["state"], "boot");
    assert_eq!(first["data"]["role"], "active");
    assert_eq!(first["data"]["winner"], PEER);
    assert_eq!(first["data"]["sessionId"], "s1");
    assert_eq!(
        first["data"]["activation"],
        json!({"activationId": "intent-1", "intentRevision": 1})
    );
    rig.advance(10.0);
    wait_for("the cadence beat", || states(&rig).len() >= 2);
    assert_eq!(states(&rig)[1], "idle");
    rig.service.close(false);
    rig.beat_passes(2);
    let after_close = states(&rig).len();
    rig.advance(10.0);
    rig.beat_passes(2);
    assert_eq!(states(&rig).len(), after_close, "no beat while closed");
    assert!(!states(&rig).contains(&"shutdown".to_owned()));
    rig.open(
        Some(&outcome(vec![])),
        &grant(2, 2, 100, EvidenceGrade::Activation),
    );
    wait_for("the reopen beat", || states(&rig).len() > after_close);
    rig.service.close(true);
    rig.release_standdown();
    wait_for("the shutdown beat", || {
        states(&rig).iter().filter(|s| *s == "shutdown").count() == 1
    });
    rig.wait_quiescent();
    let shutdown = rig.store.beats().last().unwrap().clone();
    assert_eq!(shutdown["data"]["state"], "shutdown");
    assert_eq!(shutdown["data"]["role"], "standby");
    assert_eq!(shutdown["data"]["activation"]["intentRevision"], 2);
    rig.advance(30.0);
    rig.beat_passes(2);
    assert_eq!(
        states(&rig).iter().filter(|s| *s == "shutdown").count(),
        1,
        "once"
    );
}

/// An owner cancel outside Stop (a JNI drain, an owner failure) settles what
/// the transport can no longer carry: a revoking close before the cancel and
/// one after it both leave the stand-down write and the `shutdown` beat
/// settled, the held run is interrupted and reported, and nothing holds
/// quiescence for a later Release.
#[test]
fn an_owner_cancel_settles_the_stand_down_obligations() {
    let exited = |rig: &Rig| {
        wait_for("the service threads exited", || {
            let h = rig.agent.health();
            [h.service, h.presence, h.housekeeping, h.standdown]
                .iter()
                .all(|t| t.phase == crate::managed::TaskPhase::Exited)
        })
    };
    let settled = |rig: &Rig| {
        rig.wait_quiescent();
        let execution = rig.agent.health().execution;
        assert!(!execution.standdown_pending);
        assert!(!execution.shutdown_beat_pending);
    };
    // the close first, the cancel right after it
    let rig = rig();
    rig.store.seed(TRIGGERS, &once("c-live", 900.0));
    rig.open_activation();
    let _hold = rig.script.hold("c-live");
    rig.scan();
    rig.wait_started("c-live", 1);
    rig.service.close(true);
    rig.shared.cancel();
    exited(&rig);
    settled(&rig);
    // the cancel first: its threads are gone, so the close marks nothing
    let rig = self::rig();
    rig.open_activation();
    rig.shared.cancel();
    exited(&rig);
    rig.service.close(true);
    settled(&rig);
    assert_eq!(rig.agent.health().execution.standdown_failures, 1);
    assert!(rig
        .store
        .beats()
        .iter()
        .all(|b| b["data"]["state"] != "shutdown"));
}

// --- soft breaks and injection ------------------------------------------------------------

/// A soft-break deadline is escalated by the dispatcher (no detached
/// sleeper), also across a non-revoking close; `inject_if` refuses under the
/// live lock when its check fails.
#[test]
fn a_soft_break_deadline_escalates_and_inject_if_checks_under_the_live_lock() {
    let rig = rig();
    rig.store.seed(TRIGGERS, &once("b-live", 900.0));
    rig.open_activation();
    let _hold = rig.script.hold("b-live");
    rig.scan();
    rig.wait_started("b-live", 1);
    let run = rig.run_of("b-live").unwrap();
    assert_eq!(
        inject_if(&rig.shared, &run, json!({"kind": "inject"}), || false),
        Err(MailboxError::Refused)
    );
    assert_eq!(
        inject_if(&rig.shared, &run, json!({"kind": "inject"}), || true),
        Ok(())
    );
    assert_eq!(
        inject_if(&rig.shared, "run_gone", json!({}), || true),
        Err(MailboxError::NotLive)
    );
    soft_break_live(&rig.shared, &run).unwrap();
    rig.inner()
        .lock()
        .breaks
        .push((Instant::now() + Duration::from_millis(50), run.clone()));
    rig.service.close(false);
    wait_for("the escalation", || rig.live_interrupt(&run) != Some(false));
    rig.wait_quiescent();
    assert_eq!(
        rig.store.row(TRIGGERS, "b-live").unwrap()["lastStatus"],
        "interrupted"
    );
}

// --- the leftover stash and the admission helpers -----------------------------------------

#[test]
fn the_leftover_stash_is_bounded_and_per_thread() {
    assert_eq!(stash_leftovers(vec![json!(1), json!(2)]), Ok(()));
    let too_many: Vec<Value> = (0..MAX_BACKLOG).map(|i| json!(i)).collect();
    assert_eq!(stash_leftovers(too_many.clone()), Err(too_many));
    std::thread::spawn(|| assert!(take_leftovers().is_empty()))
        .join()
        .unwrap();
    assert_eq!(take_leftovers(), [json!(1), json!(2)]);
    assert!(take_leftovers().is_empty());
}

/// The worker's own `VmRSS`: read from procfs where there is one (Android,
/// Linux), absent elsewhere — never a guess.
#[test]
fn vm_rss_reads_procfs_or_reports_nothing() {
    let rss = vm_rss();
    if cfg!(any(target_os = "linux", target_os = "android")) {
        assert!(rss.is_some_and(|b| b > 0));
    } else {
        assert_eq!(rss, None);
    }
}

// --- round A review corrections (A1–A7, A2's Rust half) -------------------------------

fn shutdown_beat_of(rev: u64) -> impl Fn(&str, &str, &Value) -> bool + Send {
    move |_, path, body| {
        path == "/v1/events"
            && body["data"]["state"] == "shutdown"
            && body["data"]["activation"]["intentRevision"] == rev
    }
}

fn shutdown_revisions(rig: &Rig) -> Vec<u64> {
    rig.store
        .beats()
        .iter()
        .filter(|b| b["data"]["state"] == "shutdown")
        .map(|b| b["data"]["activation"]["intentRevision"].as_u64().unwrap())
        .collect()
}

/// A1: an adopted trigger is not dispatchable until its adoption stamp
/// landed, as desktop writes stamps before its scan. A once shot due at its
/// adoption runs once, after the stamp, and its completed record is what a
/// restart reads — the stamp can no longer overwrite it.
#[test]
fn an_adopted_once_waits_for_its_adoption_stamp_and_stays_consumed() {
    let rig = rig();
    let mut unowned = once("a-once", 900.0);
    unowned.owner.clear();
    rig.store.seed(TRIGGERS, &unowned);
    let mut held = rig.store.hold(is_write_of("a-once"));
    rig.open(
        Some(&outcome(vec![])),
        &grant(1, 1, 100, EvidenceGrade::Activation),
    );
    held.wait_entered();
    assert_eq!(
        rig.entry("a-once").unwrap().owner,
        PEER,
        "adopted in memory"
    );
    rig.scan();
    assert_eq!(
        rig.run_of("a-once"),
        None,
        "not dispatchable while its stamp is in flight"
    );
    assert!(!rig.service.quiescent(), "the stamp write counts");
    let passes = rig.reconciles();
    held.release();
    rig.reconciled(passes + 1);
    rig.scan();
    rig.wait_started("a-once", 1);
    rig.wait_quiescent();
    let row = rig.store.row(TRIGGERS, "a-once").unwrap();
    assert_eq!(row["owner"], PEER);
    assert_eq!(row["enabled"], false, "the completion write is the last");
    assert_eq!(row["lastRunAt"], json!(rig.now()));
    // a restart reads that row: nothing is due
    rig.reconcile();
    rig.scan();
    rig.wait_quiescent();
    assert_eq!(rig.script.starts("a-once"), 1);
}

/// A3: the reservation holds through the report's last write, and the
/// reserved entry sits reconciliation out until then. A reconcile reading the
/// stale pre-run row (`enabled: true`) does not undo a tripped breaker, and a
/// scan past the next occurrence binds no second run of a cron whose write
/// is still held.
#[test]
fn a_held_report_write_keeps_the_reservation_and_the_breaker() {
    // the breaker trips; its record write is held across a reconcile
    let rig = rig();
    let mut fragile = cron("r-brk", 60.0);
    fragile.max_consecutive_failures = 1;
    rig.store.seed(TRIGGERS, &fragile);
    rig.script.plan("r-brk", Plan::Fail);
    rig.open_activation();
    rig.scan();
    let mut held = rig.store.hold(is_write_of("r-brk"));
    rig.advance(61.0);
    rig.scan();
    held.wait_entered();
    let tripped = rig.entry("r-brk").unwrap();
    assert!(!tripped.enabled);
    assert_eq!(tripped.last_status.as_deref(), Some("auto_disabled"));
    rig.reconcile();
    let kept = rig.entry("r-brk").unwrap();
    assert!(!kept.enabled, "the stale row did not re-enable it");
    assert_eq!(kept.consecutive_failures, 1, "nor reset the breaker");
    held.release();
    rig.wait_quiescent();
    let row = rig.store.row(TRIGGERS, "r-brk").unwrap();
    assert_eq!(row["enabled"], false);
    assert_eq!(row["lastStatus"], "auto_disabled");
    rig.reconcile();
    rig.advance(61.0);
    rig.scan();
    rig.wait_quiescent();
    assert_eq!(
        rig.script.starts("r-brk"),
        1,
        "a tripped breaker runs no more"
    );
    // a cron's write is held across its next occurrence: no second bind
    let rig = self::rig();
    rig.store.seed(TRIGGERS, &cron("r-cron", 60.0));
    rig.open_activation();
    rig.scan();
    let mut held = rig.store.hold(is_write_of("r-cron"));
    rig.advance(61.0);
    rig.scan();
    held.wait_entered();
    rig.advance(61.0);
    rig.scan();
    {
        let st = rig.inner().lock();
        assert_eq!(
            st.jobs.len(),
            1,
            "still reserved: no second occurrence bound"
        );
        assert!(st.queue.is_empty());
    }
    held.release();
    rig.wait_quiescent();
    assert_eq!(rig.script.starts("r-cron"), 1);
}

/// A4: a due scan that passed its start before a Release's close takes
/// `state` after it, finds the service closed and queues nothing, so the
/// Release reaches quiescence.
#[test]
fn a_scan_racing_a_close_queues_nothing() {
    let rig = rig();
    rig.store.seed(TRIGGERS, &once("s-race", 900.0));
    rig.open_activation();
    let (entered_tx, entered) = mpsc::channel();
    let (go, wait) = mpsc::channel::<()>();
    *rig.inner().before_scan_lock.lock().unwrap() = Some(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = wait.recv_timeout(BUDGET);
    }));
    let inner = rig.service.inner.clone();
    let scan = std::thread::spawn(move || inner.due_scan());
    entered.recv_timeout(BUDGET).unwrap();
    rig.service.close(true);
    rig.release_standdown();
    drop(go);
    scan.join().unwrap();
    assert!(
        rig.inner().lock().queue.is_empty(),
        "nothing queued after the close"
    );
    rig.wait_quiescent();
    assert_eq!(rig.script.starts("s-race"), 0);
}

/// A5: each activation's `shutdown` beat is settled by its own publish only:
/// the first activation's beat, held, returning late does not settle the
/// second's.
#[test]
fn each_activation_settles_only_its_own_shutdown_beat() {
    let rig = rig();
    let mut held_a = rig.store.hold(shutdown_beat_of(1));
    let mut held_b = rig.store.hold(shutdown_beat_of(2));
    rig.open_activation();
    rig.service.close(true);
    rig.release_standdown();
    held_a.wait_entered();
    rig.open(
        Some(&outcome(vec![])),
        &grant(2, 2, 100, EvidenceGrade::Activation),
    );
    rig.service.close(true);
    rig.release_standdown();
    held_a.release();
    held_b.wait_entered();
    // the owner-clear settled, no ordinary beat: what is left is B's beat
    wait_for("the stand-down write", || rig.service.standdown_idle());
    assert_eq!(rig.inner().shutdowns_pending.load(Ordering::SeqCst), 1);
    assert!(
        !rig.service.quiescent(),
        "the second activation's beat still counts"
    );
    held_b.release();
    rig.wait_quiescent();
    assert_eq!(shutdown_revisions(&rig), [1, 2]);
}

/// A5: an ordinary beat in flight holds quiescence — the transport stays in
/// Execution through a non-revoking Close — and the activation still owes its
/// `shutdown` beat at the Release that follows; Released comes after both.
#[test]
fn a_beat_in_flight_holds_quiescence_and_the_owed_shutdown_follows_it() {
    let rig = rig();
    let mut driver = Driver::new(&rig);
    driver.open_session(&rig);
    wait_for("the first beat", || !rig.store.beats().is_empty());
    let mut held = rig
        .store
        .hold(|_, path, body| path == "/v1/events" && body["data"]["state"] == "idle");
    rig.advance(10.0);
    held.wait_entered();
    driver.close(CloseReason::SyncLoss, None);
    driver.tick();
    driver.tick();
    assert_eq!(
        driver.gate(),
        Some(Phase::Execution),
        "the beat in flight keeps the routes"
    );
    assert!(!rig.service.quiescent());
    driver.release(1);
    assert!(!driver.tick().iter().any(is_released));
    held.release();
    driver.tick_until("Released", is_released);
    let states: Vec<String> = rig
        .store
        .beats()
        .iter()
        .map(|b| b["data"]["state"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(
        states,
        ["boot", "idle", "shutdown"],
        "the owed shutdown beat, last"
    );
}

/// A6: a stand-down and a beat thread that first start after the owner's
/// cancel still run their epilogues and settle what a close marked.
#[test]
fn obligations_settle_when_their_threads_start_after_a_cancel() {
    let rig = rig();
    // a second service on the same owner, its threads not started yet
    let inner = Inner::new(
        &rig.shared,
        exec_config(),
        &ExecutionParts {
            runner: rig.script.clone(),
            client: rig.client.clone(),
            status: Default::default(),
            engine: wasmtime::Engine::default(),
            clock: rig.boot.clone(),
            rss: Arc::new(|| None),
            now: Arc::new(|| 1_000.0),
            config_store: None,
            traces: rig.sweeps.clone(),
        },
    );
    let late = ExecutionService {
        inner: inner.clone(),
    };
    rig.gate.set(Phase::Execution);
    let clock = CalibratedBootClock::new(rig.boot.clone(), 0, 0, 0).unwrap();
    late.open(
        &grant(1, 1, 100, EvidenceGrade::Activation),
        "s1",
        Some(&outcome(vec![])),
        &clock,
    );
    late.close(true);
    late.release_owner_clear();
    late.release_shutdown_beats();
    assert!(
        !late.quiescent(),
        "the owner-clear and the shutdown beat are due"
    );
    rig.shared.cancel();
    let threads: Vec<_> = [
        (
            Task::Standdown,
            Inner::standdown_loop as fn(&Inner, &TaskContext),
        ),
        (Task::Presence, Inner::presence_loop),
    ]
    .into_iter()
    .map(|(task, body)| {
        let inner = inner.clone();
        crate::managed::spawn_task(rig.shared.clone(), task, move |ctx| {
            body(&inner, &ctx);
            Ok(())
        })
        .unwrap()
    })
    .collect();
    for thread in threads {
        thread.join().unwrap();
    }
    assert!(late.quiescent(), "settled by the epilogues");
    let st = inner.lock();
    assert!(st.standdown_exited && st.beats_exited);
    assert!(st.shutdowns.is_empty() && st.owner_clear.is_none());
}

/// A7: the stand-down write runs on its own thread, so a soft-break deadline
/// is escalated while that write is held; the revoking close itself
/// interrupts nothing (R9).
#[test]
fn a_soft_break_escalates_while_the_stand_down_write_is_held() {
    let rig = rig();
    rig.store.seed(TRIGGERS, &once("e-live", 900.0));
    rig.open_activation();
    let _hold = rig.script.hold("e-live");
    rig.scan();
    rig.wait_started("e-live", 1);
    let run = rig.run_of("e-live").unwrap();
    soft_break_live(&rig.shared, &run).unwrap();
    let mut held = rig.store.hold(is_write_of("chat-watch"));
    rig.service.close(true);
    assert_eq!(
        rig.live_interrupt(&run),
        Some(false),
        "a revoking close interrupts nothing"
    );
    rig.release_standdown();
    held.wait_entered();
    rig.inner()
        .lock()
        .breaks
        .push((Instant::now(), run.clone()));
    rig.inner().wake();
    wait_for("the escalation while the write is held", || {
        rig.live_interrupt(&run) != Some(false)
    });
    held.release();
    rig.wait_quiescent();
    assert_eq!(
        rig.store.row(TRIGGERS, "e-live").unwrap()["lastStatus"],
        "interrupted"
    );
}

/// A2's Rust half (the defect is Kotlin's, logged for the Kotlin phase): on
/// a reused worker the second activation's Release never reports Released
/// while that activation's run is still live — a run blocked in a host call
/// outlives the interrupt. The Release is answered Failed(DEADLINE_EXCEEDED)
/// at its own deadline; quiescence later shows only as the Start-correlated
/// QUIESCENT Status, never as a late Released.
#[test]
fn a_second_activation_s_release_waits_for_its_live_run() {
    let rig = rig();
    rig.store.seed(TRIGGERS, &once("a2-live", 900.0));
    let mut driver = Driver::new(&rig);
    driver.open_session(&rig);
    driver.close(CloseReason::OwnershipLost, Some(1));
    driver.release(1);
    driver.tick_until("activation 1's Released", is_released);
    driver.activate(2);
    wait_for("the stand-down settled", || rig.service.standdown_idle());
    let gen = driver
        .open(grant(2, 2, 100, EvidenceGrade::Activation))
        .expect("activation 2 takes over");
    let passes = rig.reconciles();
    rig.takeover_writes(&outcome(vec![]));
    driver.takeover_done(gen, outcome(vec![]));
    rig.reconciled(passes + 1);
    let blocked = rig.script.hold_blocked("a2-live");
    rig.scan();
    rig.wait_started("a2-live", 1);
    driver.close(CloseReason::OwnershipLost, Some(2));
    let mut seen = driver.release(2);
    rig.settle_standdown();
    seen.extend(driver.tick());
    assert!(!seen.iter().any(is_released), "the run is live: {seen:?}");
    rig.boot.0.store(10_200, Ordering::SeqCst); // past the Release's deadline
    let at_deadline = driver.tick();
    assert!(
        at_deadline.iter().any(|c| matches!(
            c,
            WorkerCallback::Failed {
                failure: WorkerFailure::DeadlineExceeded,
                ..
            }
        )),
        "{at_deadline:?}"
    );
    assert!(!at_deadline.iter().any(is_released));
    drop(blocked);
    let mut after = Vec::new();
    let deadline = Instant::now() + BUDGET;
    while !after.iter().any(|c| {
        matches!(
            c,
            WorkerCallback::Status { status, .. } if status.phase == WorkerPhase::Quiescent
        )
    }) {
        assert!(Instant::now() < deadline, "no QUIESCENT Status: {after:?}");
        after.extend(driver.tick());
        std::thread::sleep(Duration::from_millis(5));
    }
    assert!(
        !after.iter().any(is_released),
        "no late Released: {after:?}"
    );
    assert_eq!(
        rig.store.row(TRIGGERS, "a2-live").unwrap()["lastStatus"],
        "interrupted"
    );
}

// --- round A-r2 review corrections (B1, B2) ------------------------------------------

/// B1 (the integration lens's schedule): a reconcile response captured before
/// a run's report completed, applied after the report persisted the tripped
/// breaker and removed the reservation, leaves that trigger's row unapplied:
/// the breaker stays tripped and the trigger runs once.
#[test]
fn a_delayed_query_does_not_reenable_a_completed_breaker() {
    let rig = rig();
    let mut fragile = cron("b1-brk", 1.0);
    fragile.max_consecutive_failures = 1;
    rig.store.seed(TRIGGERS, &fragile);
    rig.script.plan("b1-brk", Plan::Fail);
    rig.open_activation();
    rig.scan();
    let mut write = rig.store.hold(is_write_of("b1-brk"));
    rig.advance(5.0);
    rig.scan();
    write.wait_entered();
    assert!(!rig.entry("b1-brk").unwrap().enabled);
    assert!(rig.run_of("b1-brk").is_some());
    let mut query = rig.store.hold(|_, path, _| path.ends_with("/query"));
    let inner = rig.service.inner.clone();
    let reader = std::thread::spawn(move || inner.reconcile(&mut false));
    query.wait_entered(); // its snapshot still says enabled: true
    write.release();
    rig.wait_quiescent(); // the report persisted false and ended the reservation
    assert_eq!(rig.store.row(TRIGGERS, "b1-brk").unwrap()["enabled"], false);
    assert!(rig.run_of("b1-brk").is_none());
    query.release();
    assert!(reader.join().unwrap());
    let after = rig.entry("b1-brk").unwrap();
    assert!(!after.enabled, "the stale row was not applied");
    assert_eq!(after.consecutive_failures, 1);
    assert_eq!(after.last_status.as_deref(), Some("auto_disabled"));
    // two dispatcher passes before any fresh reconcile
    rig.scan();
    rig.advance(5.0);
    rig.scan();
    rig.wait_quiescent();
    assert_eq!(
        rig.script.starts("b1-brk"),
        1,
        "an auto-disabled trigger runs no more"
    );
    // the next pass reads the fresh row and applies it
    rig.reconcile();
    assert!(!rig.entry("b1-brk").unwrap().enabled);
    assert!(
        rig.inner().lock().touched.is_empty(),
        "applied touches are pruned"
    );
}

/// The rig's service, pausing the session right after `close(true)` — before
/// `close_locally` restores cleanup authorization — for two full passes of the
/// real beat thread (the integration lens's B2 schedule).
struct PauseAfterClose {
    service: Arc<ExecutionService>,
    gate: PhaseGate,
    saw_window: AtomicBool,
}
impl Execution for PauseAfterClose {
    fn open(
        &self,
        grant: &OpenGrant,
        session_id: &str,
        takeover: Option<&TakeoverOutcome>,
        clock: &CalibratedBootClock,
    ) {
        self.service.open(grant, session_id, takeover, clock);
    }
    fn close(&self, revoking: bool) {
        self.service.close(revoking);
        if revoking && self.gate.get() == Phase::Preparation {
            let inner = &self.service.inner;
            assert_eq!(inner.shutdowns_pending.load(Ordering::SeqCst), 1);
            let from = inner.beat_passes.load(Ordering::SeqCst);
            wait_for("two beat-thread passes in the window", || {
                inner.beat_passes.load(Ordering::SeqCst) >= from + 2
            });
            assert_eq!(
                inner.shutdowns_pending.load(Ordering::SeqCst),
                1,
                "not run before the session released it"
            );
            self.saw_window.store(true, Ordering::SeqCst);
        }
    }
    fn interrupt(&self) {
        self.service.interrupt();
    }
    fn quiescent(&self) -> bool {
        self.service.quiescent()
    }
    fn capacity_fault(&self) -> bool {
        self.service.capacity_fault()
    }
    fn owns_chat(&self) -> bool {
        self.service.owns_chat()
    }
    fn standdown_idle(&self) -> bool {
        self.service.standdown_idle()
    }
    fn release_owner_clear(&self) {
        self.service.release_owner_clear();
    }
    fn release_shutdown_beats(&self) {
        self.service.release_shutdown_beats();
    }
}

/// B2: SyncLoss returns the transport to Preparation; the Release that
/// follows queues the owed `shutdown` beat. The beat thread gets its window
/// before cleanup authorization and runs nothing; the beat goes out once the
/// session released it, exactly once, before Released.
#[test]
fn the_owed_shutdown_beat_waits_for_cleanup_authorization() {
    let rig = rig();
    let pause = Arc::new(PauseAfterClose {
        service: rig.service.clone(),
        gate: rig.gate.clone(),
        saw_window: AtomicBool::new(false),
    });
    let mut driver = Driver::with_execution(&rig, pause.clone());
    driver.open_session(&rig);
    wait_for("the boot beat", || !rig.store.beats().is_empty());
    driver.close(CloseReason::SyncLoss, None);
    rig.wait_quiescent();
    driver.tick();
    assert_eq!(driver.gate(), Some(Phase::Preparation));
    let mut released = driver.release(1).iter().any(is_released);
    if !released {
        driver.tick_until("Released", is_released);
        released = true;
    }
    assert!(released && pause.saw_window.load(Ordering::SeqCst));
    assert_eq!(rig.store.row(TRIGGERS, "chat-watch").unwrap()["owner"], "");
    assert_eq!(
        shutdown_revisions(&rig),
        [1],
        "the owed shutdown beat, once, before Released"
    );
}

/// B2: a queued `shutdown` beat does not run before the session's own
/// release of it, even with the transport in Execution and the owner-clear
/// released and landed.
#[test]
fn a_shutdown_beat_waits_for_the_session_s_release() {
    let rig = rig();
    rig.open_activation();
    rig.service.close(true);
    rig.service.release_owner_clear();
    wait_for("the stand-down write", || rig.service.standdown_idle());
    rig.beat_passes(2);
    assert!(shutdown_revisions(&rig).is_empty(), "not released yet");
    assert_eq!(rig.inner().shutdowns_pending.load(Ordering::SeqCst), 1);
    assert!(!rig.service.quiescent());
    rig.service.release_shutdown_beats();
    rig.wait_quiescent();
    assert_eq!(shutdown_revisions(&rig), [1]);
}

/// B2: a `shutdown` beat the host transport refuses (its phase) is not
/// settled: it stays owed and goes out once the routes allow it.
#[test]
fn a_gate_refused_shutdown_beat_stays_owed() {
    let rig = rig();
    rig.open_activation();
    rig.service.close(true);
    rig.gate.set(Phase::Preparation);
    rig.release_standdown();
    rig.beat_passes(2);
    assert_eq!(
        rig.inner().shutdowns_pending.load(Ordering::SeqCst),
        1,
        "a refused publish is no attempt"
    );
    assert!(!rig.service.quiescent());
    assert!(shutdown_revisions(&rig).is_empty());
    rig.gate.set(Phase::Execution);
    rig.wait_quiescent();
    assert_eq!(shutdown_revisions(&rig), [1]);
}

// --- N2b-2 C2: the chat source and the chat run's bookkeeping ---------------------------
// Design §7 groups 8, 9, 11, 12 (chat) and 18, plus the run not live yet.

fn user(id: &str, text: &str) -> Value {
    json!({"id": id, "text": text, "creator": "user-1"})
}
fn own_reply(id: &str, text: &str) -> Value {
    json!({"id": id, "text": text, "agent": {"name": AGENT, "done": true}})
}
fn stop_message(id: &str, hard: bool) -> Value {
    json!({"id": id, "control": {"kind": "break", "hard": hard}})
}
/// A `changes` frame adding `records` (each carries its id; the frame
/// splits it off as the server does).
fn added(records: &[Value]) -> Value {
    let added: Vec<Value> = records
        .iter()
        .map(|record| {
            let mut doc = record.clone();
            let id = doc.as_object_mut().unwrap().remove("id").unwrap();
            json!({"id": id, "ops": [{"type": "$set", "path": ""}], "doc": doc})
        })
        .collect();
    json!([{ "added": added }])
}
fn items(mailbox: &crate::broker::SharedMailbox) -> Vec<Value> {
    mailbox.lock().unwrap().iter().cloned().collect()
}
fn inject_item(id: &str, text: &str) -> Value {
    json!({"kind": "inject", "text": text, "context": null, "messageId": id})
}
fn stopped_post(run: &str) -> Value {
    json!({"text": "Stopped.", "agent": {"name": AGENT, "done": true,
                                         "outcome": "interrupted", "debugLink": run}})
}

/// A completed takeover whose responder is `id` (a `chat-watch-g<n>`
/// generation keeps a test's lane hook its own).
fn outcome_for(id: &str) -> TakeoverOutcome {
    TakeoverOutcome {
        chat_watch_id: id.into(),
        chat_watch: chat_watch_trigger(id, CHAT, PEER),
        standing: vec![],
        descriptors: 0,
    }
}

impl Rig {
    fn open_responder(&self, id: &str) {
        let passes = self.reconciles();
        self.open(
            Some(&outcome_for(id)),
            &grant(1, 1, 100, EvidenceGrade::Activation),
        );
        self.reconciled(passes + 1);
    }
    /// The chat watch has subscribed `n` times.
    fn watching(&self, n: usize) {
        wait_for("the chat watch subscribed", || {
            self.store.subscribed(CHAT) >= n
        });
    }
    fn say(&self, records: &[Value]) {
        self.store.push(CHAT, "changes", added(records));
    }
    fn parked(&self) -> usize {
        self.inner().lock().parked.len()
    }
}

/// Group 8: a snapshot's backlog (after the last own terminal reply) starts
/// its first message and injects the next into the live run (desktop
/// `start_or_inject`); the undrained inject is re-dispatched by the lane
/// thread as a new conversation when the run ends; the start is counted once
/// on the responder (`note_chat_start`).
#[test]
fn a_snapshot_starts_its_first_message_and_injects_the_next() {
    let rig = rig();
    let release = rig.script.hold("chat-watch");
    rig.open_activation();
    rig.watching(1);
    rig.store.push(
        CHAT,
        "snapshot",
        json!({"records": [user("m-b", "B"), user("m-a", "A"), own_reply("m-0", "earlier"),
                           user("m-old", "answered")]}),
    );
    rig.wait_started("chat-watch", 1);
    let (run, args, mailbox) = rig.script.run("chat-watch", 0).unwrap();
    assert_eq!(
        args,
        json!({"space": SPACE, "chatId": CHAT, "userText": "A", "uiContext": null,
               "agentName": AGENT, "traceRef": run, "codeSpace": "agent-space",
               "overlays": {}})
    );
    wait_for("B injected into the live run", || {
        !items(&mailbox).is_empty()
    });
    assert_eq!(items(&mailbox), [inject_item("m-b", "B")]);
    assert_eq!(rig.script.starts("chat-watch"), 1);
    drop(release);
    rig.wait_started("chat-watch", 2);
    assert_eq!(rig.script.run("chat-watch", 1).unwrap().1["userText"], "B");
    rig.wait_quiescent();
    assert_eq!(
        rig.script.starts("chat-watch"),
        2,
        "the answered one never runs"
    );
    assert_eq!(
        rig.store.writes_of("chat-watch"),
        1,
        "one conversation start noted"
    );
    let responder = rig.store.row(TRIGGERS, "chat-watch").unwrap();
    assert_eq!(responder["lastStatus"], "ok");
    assert_eq!(responder["owner"], PEER);
    assert!(rig.store.posts().is_empty(), "two ok runs post nothing");
}

/// A message for a run offered but not live yet (held in its take→register
/// gap) waits — the dispatcher cannot inject it yet — and is injected once
/// the run is live: it joins the run, as desktop's pre-registered mailbox
/// takes it.
#[test]
fn a_message_for_a_run_not_live_yet_waits_and_joins_it() {
    let rig = rig();
    let id = "chat-watch-g7";
    let (entered_tx, entered) = mpsc::channel();
    let (go, wait) = mpsc::channel::<()>();
    test_hooks::hold_after_take(id, move || {
        let _ = entered_tx.send(());
        let _ = wait.recv_timeout(BUDGET);
    });
    let release = rig.script.hold(id);
    rig.open_responder(id);
    rig.watching(1);
    rig.say(&[user("m-a", "A")]);
    entered.recv_timeout(BUDGET).unwrap();
    rig.say(&[user("m-b", "B")]);
    wait_for("B parked", || rig.parked() == 1);
    rig.inner().flush_parked();
    assert_eq!(rig.parked(), 1, "not live yet: nothing to inject into");
    drop(go);
    rig.wait_started(id, 1);
    let (_, args, mailbox) = rig.script.run(id, 0).unwrap();
    assert_eq!(args["userText"], "A");
    wait_for("B injected once the run is live", || {
        !items(&mailbox).is_empty()
    });
    assert_eq!(items(&mailbox), [inject_item("m-b", "B")]);
    assert_eq!(rig.parked(), 0);
    assert_eq!(rig.script.starts(id), 1);
    drop(release);
    rig.wait_started(id, 2);
    rig.wait_quiescent();
}

/// Group 12 (chat, `Cancelled`): a hard break on a run not started yet traps
/// it before it runs — nothing posted, as desktop's run that never started —
/// and what waited for it is re-dispatched by its report.
#[test]
fn a_hard_break_before_the_run_starts_posts_nothing_and_re_dispatches_what_waited() {
    let rig = rig();
    let id = "chat-watch-g8";
    let (entered_tx, entered) = mpsc::channel();
    let (go, wait) = mpsc::channel::<()>();
    test_hooks::hold_after_take(id, move || {
        let _ = entered_tx.send(());
        let _ = wait.recv_timeout(BUDGET);
    });
    rig.open_responder(id);
    rig.watching(1);
    rig.say(&[user("m-a", "A")]);
    entered.recv_timeout(BUDGET).unwrap();
    rig.say(&[user("m-b", "B"), stop_message("s-1", true)]);
    wait_for("B parked, the run's own flag up", || {
        rig.parked() == 1
            && rig
                .inner()
                .lock()
                .chat
                .as_ref()
                .is_some_and(|c| c.interrupt.load(Ordering::SeqCst))
    });
    drop(go);
    rig.wait_started(id, 1);
    rig.wait_quiescent();
    assert_eq!(rig.script.run(id, 0).unwrap().1["userText"], "B");
    assert_eq!(rig.script.starts(id), 1, "A never ran");
    assert!(rig.store.posts().is_empty());
    assert!(rig.store.rows(TURNS).is_empty());
}

/// Group 8: a soft break lands in the live run's mailbox with its grace
/// deadline on the dispatcher; the deadline survives a non-revoking close
/// and escalates; the trapped run posts "Stopped." and its turn.
#[test]
fn a_soft_break_escalates_across_a_non_revoking_close() {
    let rig = rig();
    let release = rig.script.hold("chat-watch");
    rig.open_activation();
    rig.watching(1);
    rig.say(&[user("m-a", "A")]);
    rig.wait_started("chat-watch", 1);
    let (run, _, mailbox) = rig.script.run("chat-watch", 0).unwrap();
    rig.say(&[stop_message("s-1", false)]);
    wait_for("the soft break in the mailbox", || {
        !items(&mailbox).is_empty()
    });
    assert_eq!(items(&mailbox), [json!({"kind": "break", "hard": false})]);
    assert_eq!(rig.inner().lock().breaks.len(), 1);
    rig.service.close(false);
    assert_eq!(rig.inner().lock().breaks.len(), 1, "kept across the close");
    rig.inner().lock().breaks[0].0 = Instant::now();
    rig.inner().wake();
    rig.wait_quiescent();
    assert_eq!(rig.store.posts(), [stopped_post(&run)]);
    assert_eq!(
        rig.store.rows(TURNS)["00000001"],
        crate::chatwatch::interrupted_turn_record(1, AGENT, "A", &run)
    );
    drop(release);
}

/// Group 11: a hard break traps the live run; "Stopped." and the interrupted
/// turn follow, the turn under the next seq of the setup-recorded log child.
#[test]
fn a_hard_break_posts_stopped_and_logs_the_turn_under_the_next_seq() {
    let rig = rig();
    rig.store
        .seed_row(TURNS, "00000004", json!({"seq": 4, "userText": "before"}));
    let _release = rig.script.hold("chat-watch");
    rig.open_activation();
    rig.watching(1);
    rig.say(&[user("m-a", "A")]);
    rig.wait_started("chat-watch", 1);
    let run = rig.script.run("chat-watch", 0).unwrap().0;
    rig.say(&[stop_message("s-1", true)]);
    rig.wait_quiescent();
    assert_eq!(rig.store.posts(), [stopped_post(&run)]);
    assert_eq!(
        rig.store.rows(TURNS)["00000005"],
        crate::chatwatch::interrupted_turn_record(5, AGENT, "A", &run)
    );
}

/// Group 9 (D20): a message whose handler is mid-way (its reply referent
/// read held) when a close lands is dropped; the reopen's snapshot — the
/// close reset the watcher — answers it.
#[test]
fn a_message_handled_across_a_close_is_dropped_and_the_reopen_snapshot_answers_it() {
    let rig = rig();
    rig.open_activation();
    rig.watching(1);
    let mut referent = rig
        .store
        .hold(|_, path, body| path.ends_with("/query") && body["objectId"] == CHAT);
    let reply = json!({"id": "m-r", "text": "R", "replyToMessageId": "m-0"});
    rig.say(std::slice::from_ref(&reply));
    referent.wait_entered();
    rig.service.close(false);
    referent.release();
    rig.open(None, &grant(1, 1, 100, EvidenceGrade::Activation));
    rig.watching(2);
    assert_eq!(
        rig.script.starts("chat-watch"),
        0,
        "dropped across the close"
    );
    rig.store
        .push(CHAT, "snapshot", json!({ "records": [reply] }));
    rig.wait_started("chat-watch", 1);
    let text = rig.script.run("chat-watch", 0).unwrap().1["userText"].clone();
    assert!(text.as_str().unwrap().ends_with("\nR"), "{text}");
    rig.wait_quiescent();
}

/// Group 8: a message refused admission (the gate held while open) is
/// dropped, not queued; the reopen's snapshot answers it.
#[test]
fn a_message_refused_admission_is_answered_by_the_reopen_snapshot() {
    let rig = rig();
    rig.open_activation();
    rig.watching(1);
    rig.shared.stand_down(GateCause::Resource, false);
    let mut referent = rig
        .store
        .hold(|_, path, body| path.ends_with("/query") && body["objectId"] == CHAT);
    let reply = json!({"id": "m-r", "text": "R", "replyToMessageId": "m-0"});
    rig.say(&[user("m-a", "A"), reply.clone()]);
    // A was handled before R's referent read: refused, dropped
    referent.wait_entered();
    rig.shared.reopen(GateCause::Resource);
    referent.release();
    rig.wait_started("chat-watch", 1);
    assert!(rig.script.run("chat-watch", 0).unwrap().1["userText"]
        .as_str()
        .unwrap()
        .ends_with("\nR"));
    rig.wait_quiescent();
    rig.service.close(false);
    rig.open(None, &grant(1, 1, 100, EvidenceGrade::Activation));
    rig.watching(2);
    rig.store.push(
        CHAT,
        "snapshot",
        json!({"records": [reply, user("m-a", "A")]}),
    );
    wait_for("A answered after the reopen", || {
        (0..rig.script.starts("chat-watch"))
            .any(|n| rig.script.run("chat-watch", n).unwrap().1["userText"] == "A")
    });
    rig.wait_quiescent();
}

/// Group 11: a run that died on a credential miss posts the request card —
/// and nothing else — and stamps the request on the secrets row.
#[test]
fn a_credential_miss_posts_the_card_and_stamps_the_request() {
    let rig = rig();
    rig.script.plan("chat-watch", Plan::MissingKey);
    rig.open_activation();
    rig.watching(1);
    rig.say(&[user("m-a", "A")]);
    rig.wait_started("chat-watch", 1);
    rig.wait_quiescent();
    assert_eq!(
        rig.store.posts(),
        [json!({
            "text": format!("I need a credential to continue: {MISSING_REF} (`{MISSING_REF}`)."),
            "agent": {"name": AGENT, "done": true},
            "attachments": {"credreq": {"type": "credential_request",
                "link": format!("any://o/{}?key={MISSING_REF}", SECRETS.0)}}})]
    );
    let row = &rig.store.rows(SECRETS)[MISSING_REF];
    assert_eq!(row["requestedIn"], CHAT);
    assert!(row.get("value").is_none());
}

/// Group 11: the card already sits in this chat — "Still waiting", with
/// the run's last error.
#[test]
fn a_credential_miss_with_its_card_already_posted_says_still_waiting() {
    let rig = rig();
    rig.store.seed_row(
        SECRETS,
        MISSING_REF,
        json!({"key": MISSING_REF, "status": "missing", "requestedIn": CHAT}),
    );
    rig.script.plan("chat-watch", Plan::MissingKey);
    rig.open_activation();
    rig.watching(1);
    rig.say(&[user("m-a", "A")]);
    rig.wait_started("chat-watch", 1);
    rig.wait_quiescent();
    let run = rig.script.run("chat-watch", 0).unwrap().0;
    let why = format!(
        " Last attempt: {}",
        crate::chatwatch::short_error("E: no key")
    );
    assert_eq!(
        rig.store.posts(),
        [crate::chatwatch::follow_up_body(
            &crate::chatwatch::FollowUp::StillWaiting {
                refs: MISSING_REF.into(),
                why
            },
            AGENT,
            &run
        )]
    );
}

/// Group 11: a card whose post failed is not counted, so "Still waiting"
/// follows (desktop order); the request is not stamped.
#[test]
fn a_failed_card_post_is_followed_by_still_waiting() {
    let rig = rig();
    rig.store.fail(|_, path, body| {
        path.ends_with("/chat/messages") && body["attachments"]["credreq"].is_object()
    });
    rig.script.plan("chat-watch", Plan::MissingKey);
    rig.open_activation();
    rig.watching(1);
    rig.say(&[user("m-a", "A")]);
    rig.wait_started("chat-watch", 1);
    rig.wait_quiescent();
    let posts = rig.store.posts();
    assert_eq!(posts.len(), 2, "{posts:?}");
    assert!(posts[0]["attachments"]["credreq"].is_object());
    assert!(posts[1]["text"]
        .as_str()
        .unwrap()
        .starts_with(&format!("Still waiting for `{MISSING_REF}`")));
    assert!(rig.store.rows(SECRETS).is_empty(), "nothing stamped");
}

/// Group 12 (chat): an `error` run posts the typed error with its trace; an
/// `ok` run posts nothing and logs no synthetic turn (E15), its summary
/// published under the responder; a run path failure and an unlanded trace
/// post nothing, as desktop's failed `ctx.run`.
#[test]
fn chat_results_are_finalized_as_on_desktop() {
    for (plan, want_post) in [
        (Some(Plan::Fail), true),
        (None, false),
        (Some(Plan::RunPath), false),
        (Some(Plan::Unpersisted), false),
    ] {
        let rig = rig();
        if let Some(plan) = plan {
            rig.script.plan("chat-watch", plan);
        }
        rig.open_activation();
        rig.watching(1);
        rig.say(&[user("m-a", "A")]);
        rig.wait_started("chat-watch", 1);
        rig.wait_quiescent();
        let run = rig.script.run("chat-watch", 0).unwrap().0;
        let posts = rig.store.posts();
        if want_post {
            assert_eq!(
                posts,
                [json!({"text": "Something broke mid-run: E: failed",
                        "agent": {"name": AGENT, "done": true, "outcome": "error",
                                  "debugLink": run}})]
            );
        } else {
            assert!(posts.is_empty(), "{posts:?}");
        }
        assert!(rig.store.rows(TURNS).is_empty(), "no synthetic turn");
        let summaries = rig.store.rows(RUNS);
        if matches!(plan, None | Some(Plan::Fail)) {
            assert_eq!(summaries[&run]["triggerId"], "chat-watch", "{summaries:?}");
        } else {
            assert!(summaries.is_empty(), "no summary without a landed trace");
        }
    }
}

/// Groups 11/18: the session's Release interrupts the live chat run; its
/// "Stopped." and turn record go through the execution routes the session
/// keeps until quiescence, and Released follows them.
#[test]
fn a_release_interrupt_posts_stopped_and_logs_the_turn_before_released() {
    let rig = rig();
    let _release = rig.script.hold("chat-watch");
    let mut driver = Driver::new(&rig);
    driver.open_session(&rig);
    rig.watching(1);
    rig.say(&[user("m-a", "A")]);
    rig.wait_started("chat-watch", 1);
    let run = rig.script.run("chat-watch", 0).unwrap().0;
    let callbacks = driver.release(1);
    if !callbacks.iter().any(is_released) {
        driver.tick_until("Released", is_released);
    }
    assert_eq!(rig.store.posts(), [stopped_post(&run)]);
    assert_eq!(
        rig.store.rows(TURNS)["00000001"],
        crate::chatwatch::interrupted_turn_record(1, AGENT, "A", &run)
    );
    assert_eq!(driver.gate(), Some(Phase::Preparation));
    assert_eq!(rig.store.row(TRIGGERS, "chat-watch").unwrap()["owner"], "");
}

/// The conversation-start write of the responder (owner = this peer) lands
/// before the stand-down write clears the owner: held, it holds the
/// stand-down write and quiescence.
#[test]
fn the_stand_down_write_waits_for_a_conversation_start_write() {
    let rig = rig();
    rig.open_activation();
    rig.watching(1);
    let mut note = rig.store.hold(is_write_of("chat-watch"));
    rig.say(&[user("m-a", "A")]);
    note.wait_entered();
    rig.wait_started("chat-watch", 1);
    wait_for("the run reported", || {
        rig.inner().inflight.load(Ordering::SeqCst) == 0
    });
    assert!(
        !rig.service.quiescent(),
        "the held conversation-start write counts"
    );
    rig.service.close(true);
    rig.release_standdown();
    rig.beat_passes(2);
    assert_eq!(
        rig.store.writes_of("chat-watch"),
        1,
        "the stand-down write waits"
    );
    assert!(!rig.service.quiescent());
    note.release();
    rig.wait_quiescent();
    assert_eq!(rig.store.writes_of("chat-watch"), 2);
    assert_eq!(rig.store.row(TRIGGERS, "chat-watch").unwrap()["owner"], "");
}

/// The managed host runs desktop's conversation program.
#[cfg(feature = "desktop")]
#[test]
fn the_chat_program_is_desktop_s() {
    assert_eq!(chat::CHAT_PROGRAM, crate::serve::CHAT_PROGRAM);
}

/// Desktop's reconnect loop: a stream the server ended is subscribed again
/// after the pause; its snapshot is deduplicated by the watcher's seen-set
/// (only a close resets it), so an answered-but-unreplied message is not
/// run twice.
#[test]
fn a_reconnect_snapshot_is_deduplicated_by_the_seen_set() {
    let rig = rig();
    rig.open_activation();
    rig.watching(1);
    rig.say(&[user("m-a", "A")]);
    rig.wait_started("chat-watch", 1);
    rig.wait_quiescent();
    rig.store.end_streams(CHAT);
    rig.watching(2);
    rig.store
        .push(CHAT, "snapshot", json!({"records": [user("m-a", "A")]}));
    rig.say(&[user("m-b", "B")]);
    rig.wait_started("chat-watch", 2);
    rig.wait_quiescent();
    assert_eq!(rig.script.run("chat-watch", 1).unwrap().1["userText"], "B");
    assert_eq!(rig.script.starts("chat-watch"), 2, "A ran once");
}

// --- N2b-2 C3: event sources and retention ------------------------------------------------
// Design §7 groups 13, 19 (retention) and 12 (event bookkeeping, E14), D11/D17.

const EVENTS: &str = "events-chat";

/// An own enabled `chat_messages` event trigger on `object` (ADR-018 §2).
fn event_trigger(id: &str, object: &str) -> Trigger {
    let mut t = cron(id, 60.0);
    t.kind = "event".into();
    t.spec = json!({"dataset": "chat_messages", "objectId": object});
    t.args = json!({"note": id});
    t
}

impl Rig {
    fn sources(&self) -> usize {
        self.inner().lock().sources.len()
    }
    fn source_subscribed(&self, object: &str, n: usize) {
        wait_for(&format!("{object} subscribed {n}×"), || {
            self.store.subscribed(object) >= n
        });
    }
}

/// Group 13: one message fires the triggers naming its source one after
/// another, each under the event lane's permit — the second waits for the
/// first, as desktop's inline fire — with desktop's args and `finish_run`
/// bookkeeping.
#[test]
fn the_triggers_of_one_message_fire_in_turn_under_the_event_lane() {
    let rig = rig();
    rig.store.seed(TRIGGERS, &event_trigger("e-1", EVENTS));
    rig.store.seed(TRIGGERS, &event_trigger("e-2", EVENTS));
    let first = rig.script.hold("e-1");
    rig.open_activation();
    rig.source_subscribed(EVENTS, 1);
    rig.store
        .push(EVENTS, "changes", added(&[user("m-1", "hello")]));
    rig.wait_started("e-1", 1);
    rig.beat_passes(2);
    assert_eq!(rig.script.starts("e-2"), 0, "e-2 waits for the event lane");
    drop(first);
    rig.wait_started("e-2", 1);
    rig.wait_quiescent();
    assert_eq!(rig.script.order(), ["e-1", "e-2"]);
    for id in ["e-1", "e-2"] {
        let args = rig.script.run(id, 0).unwrap().1;
        assert_eq!(
            args,
            json!({"note": id, "event": {"space": SPACE, "objectId": EVENTS,
                                         "messageId": "m-1", "text": "hello"}})
        );
        let row = rig.store.row(TRIGGERS, id).unwrap();
        assert_eq!(row["lastStatus"], "ok", "{id}: {row}");
        assert!(row["lastRunAt"].is_number());
    }
    // own bubbles never fire, and a message fires once
    rig.store.push(
        EVENTS,
        "changes",
        added(&[own_reply("m-2", "mine"), user("m-1", "hello")]),
    );
    rig.store
        .push(EVENTS, "changes", added(&[user("m-3", "next")]));
    rig.wait_started("e-2", 2);
    rig.wait_quiescent();
    assert_eq!(rig.script.order(), ["e-1", "e-2", "e-1", "e-2"]);
    assert_eq!(
        rig.script.run("e-1", 1).unwrap().1["event"]["messageId"],
        "m-3"
    );
}

/// Group 12 (E14): an event trigger that fails its breaker's count is
/// auto-disabled and persisted so; its source retires — no trigger wants it
/// — with a healthy owner (D17), and a later message fires nothing.
#[test]
fn a_failing_event_trigger_trips_its_breaker_and_its_source_retires() {
    let rig = rig();
    let mut fragile = event_trigger("e-f", EVENTS);
    fragile.max_consecutive_failures = 3;
    rig.store.seed(TRIGGERS, &fragile);
    rig.script.plan("e-f", Plan::Fail);
    rig.open_activation();
    rig.source_subscribed(EVENTS, 1);
    for n in 1..=3 {
        rig.store
            .push(EVENTS, "changes", added(&[user(&format!("m-{n}"), "x")]));
        rig.wait_started("e-f", n);
        rig.wait_quiescent();
    }
    let entry = rig.entry("e-f").unwrap();
    assert!(!entry.enabled);
    assert_eq!(entry.last_status.as_deref(), Some("auto_disabled"));
    assert_eq!(rig.store.row(TRIGGERS, "e-f").unwrap()["enabled"], false);
    wait_for("the source retired", || rig.sources() == 0);
    wait_for("its thread joined", || {
        !rig.shared
            .owned
            .lock()
            .unwrap()
            .iter()
            .any(|(task, _)| *task == Task::EventSource0)
    });
    assert!(rig.agent.health().failures.is_empty(), "a healthy owner");
    rig.store
        .push(EVENTS, "changes", added(&[user("m-4", "x")]));
    rig.beat_passes(3);
    assert_eq!(rig.script.starts("e-f"), 3);
}

/// Group 13: a source whose fire waits for the lane — the trigger's
/// previous run still held — fires once the lane frees, then reads its ended
/// stream and reconnects; the new snapshot only seeds (live-only).
#[test]
fn a_source_reconnects_after_a_fire_that_waited_for_the_lane() {
    let rig = rig();
    rig.store.seed(TRIGGERS, &event_trigger("e-1", EVENTS));
    let first = rig.script.hold("e-1");
    rig.open_activation();
    rig.source_subscribed(EVENTS, 1);
    rig.store
        .push(EVENTS, "changes", added(&[user("m-1", "a")]));
    rig.wait_started("e-1", 1);
    rig.store
        .push(EVENTS, "changes", added(&[user("m-2", "b")]));
    rig.store.end_streams(EVENTS);
    rig.beat_passes(2);
    assert_eq!(rig.script.starts("e-1"), 1, "m-2 waits for the lane");
    assert_eq!(
        rig.store.subscribed(EVENTS),
        1,
        "not reading while it waits"
    );
    drop(first);
    rig.wait_started("e-1", 2);
    rig.source_subscribed(EVENTS, 2);
    rig.store.push(
        EVENTS,
        "snapshot",
        json!({"records": [user("m-9", "old"), user("m-2", "b")]}),
    );
    rig.store
        .push(EVENTS, "changes", added(&[user("m-10", "new")]));
    rig.wait_started("e-1", 3);
    rig.wait_quiescent();
    let fired: Vec<Value> = (0..3)
        .map(|n| rig.script.run("e-1", n).unwrap().1["event"]["messageId"].clone())
        .collect();
    assert_eq!(fired, [json!("m-1"), json!("m-2"), json!("m-10")]);
}

/// ADR-034 §2: a fired event trigger is reserved until its report's last
/// write landed — the next message's fire of it waits for that write, as
/// desktop's inline `finish_run` precedes its next fire.
#[test]
fn an_event_fire_waits_for_the_trigger_s_last_write() {
    let rig = rig();
    rig.store.seed(TRIGGERS, &event_trigger("e-1", EVENTS));
    rig.open_activation();
    rig.source_subscribed(EVENTS, 1);
    let mut write = rig.store.hold(is_write_of("e-1"));
    rig.store
        .push(EVENTS, "changes", added(&[user("m-1", "a")]));
    write.wait_entered();
    rig.store
        .push(EVENTS, "changes", added(&[user("m-2", "b")]));
    rig.beat_passes(3);
    assert_eq!(
        rig.script.starts("e-1"),
        1,
        "m-2 waits for m-1's last write"
    );
    write.release();
    rig.wait_started("e-1", 2);
    rig.wait_quiescent();
    assert_eq!(
        rig.script.run("e-1", 1).unwrap().1["event"]["messageId"],
        "m-2"
    );
}

/// D11/D17: a close ends every source at its next frame (a healthy exit);
/// convergence runs only while open, so the reopen subscribes afresh.
#[test]
fn sources_end_on_a_close_and_start_again_on_the_reopen() {
    let rig = rig();
    rig.store.seed(TRIGGERS, &event_trigger("e-1", EVENTS));
    rig.open_activation();
    rig.source_subscribed(EVENTS, 1);
    rig.service.close(false);
    wait_for("the source ended", || rig.sources() == 0);
    rig.beat_passes(3);
    assert_eq!(rig.store.subscribed(EVENTS), 1, "none while closed");
    rig.open(None, &grant(1, 1, 100, EvidenceGrade::Activation));
    rig.source_subscribed(EVENTS, 2);
    assert!(rig.agent.health().failures.is_empty());
}

/// At most four streams: a fifth wanted source is logged and counted as a
/// capacity fault and watched once a slot is free again (after the owner
/// joined the retired source); a source in another space is not watched.
#[test]
fn a_fifth_source_waits_for_a_free_slot_and_a_foreign_space_is_not_watched() {
    let rig = rig();
    for n in 0..5 {
        rig.store.seed(
            TRIGGERS,
            &event_trigger(&format!("e-{n}"), &format!("chat-{n}x")),
        );
    }
    let mut far = event_trigger("e-far", "far-chat");
    far.spec["spaceId"] = json!("far");
    rig.store.seed(TRIGGERS, &far);
    let faults = rig.shared.pool.capacity_faults();
    rig.open_activation();
    wait_for("four sources", || rig.sources() == 4);
    wait_for("the fifth counted", || {
        rig.shared.pool.capacity_faults() == faults + 1
    });
    let watched = || -> Vec<String> {
        (0..5)
            .map(|n| format!("chat-{n}x"))
            .filter(|object| rig.store.subscribed(object) == 1)
            .collect()
    };
    wait_for("four sources subscribed", || watched().len() == 4);
    let watched = watched();
    let waiting = (0..5)
        .map(|n| format!("chat-{n}x"))
        .find(|object| !watched.contains(object))
        .unwrap();
    // a retired source frees its slot: the trigger behind the first watched
    // one is disabled
    let retired = watched[0].clone();
    let id = format!("e-{}", &retired[5..6]);
    rig.inner().lock().registry.get_mut(&id).unwrap().enabled = false;
    rig.source_subscribed(&waiting, 1);
    assert_eq!(rig.sources(), 4);
    assert_eq!(
        rig.shared.pool.capacity_faults(),
        faults + 1,
        "counted once"
    );
    assert_eq!(rig.store.subscribed("far-chat"), 0);
    assert!(rig.agent.health().failures.is_empty());
}

/// Group 19 (R7 parity): retention sweeps on desktop's cadence — a first
/// pass after the delay, then periodically — in every phase, here with the
/// service never opened; its cut-offs are desktop's.
#[test]
fn retention_sweeps_on_its_cadence_in_every_phase() {
    let mut config = exec_config();
    config.retention = Retention {
        conversations_s: Some(10),
        jobs_s: Some(20),
        max_bytes: Some(1 << 30),
    };
    config.cadence.retention_first = Duration::from_millis(30);
    config.cadence.retention_every = Duration::from_millis(30);
    let rig = rig_with(config);
    wait_for("two sweeps", || {
        rig.inner().sweeps.load(Ordering::SeqCst) >= 2
    });
    assert_eq!(rig.gate.get(), Phase::Preparation);
    assert!(!rig.inner().open.load(Ordering::SeqCst));
    let sweeps = rig.sweeps.0.lock().unwrap().clone();
    assert_eq!(sweeps[0], (Some(990.0), Some(980.0), Some(1 << 30)));
}

/// With nothing configured retention sweeps nothing — and its thread stays
/// (an owned task that must not exit before the cancel).
#[test]
fn unconfigured_retention_sweeps_nothing_and_keeps_its_thread() {
    let mut config = exec_config();
    config.cadence.retention_first = Duration::from_millis(5);
    config.cadence.retention_every = Duration::from_millis(5);
    let rig = rig_with(config);
    wait_for("two retention wakes", || {
        rig.inner().retention_wakes.load(Ordering::SeqCst) >= 2
    });
    assert_eq!(rig.inner().sweeps.load(Ordering::SeqCst), 0);
    assert!(rig.sweeps.0.lock().unwrap().is_empty());
    assert!(rig.agent.health().failures.is_empty());
}
