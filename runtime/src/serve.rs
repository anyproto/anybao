//! `anyrt serve` — the outer loop: ensure space/chat/anchor, deploy
//! programs + skills, compose the system prompt, then watch the chat
//! (drop-snapshot SSE), tick triggers, and answer the localhost
//! control API. Conversations and trigger runs are guest programs
//! through the shared cage.

use crate::anyapi::Client;
use crate::broker::{Broker, SharedMailbox};
use crate::config::Config;
use crate::deploy::{ensure_typed, Deployer, SkillDeployer};
use crate::resolver::AnyModuleResolver;
use crate::routes::Classifier;
use crate::runner::{run_program, Cage};
use crate::trace::TraceWriter;
use crate::triggers::{
    rollup, standing_triggers, trigger_to_record, RunResult, Scheduler, Trigger, WatchAction,
    Watcher,
};
use anyhow::{Context, Result};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

fn now_s() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
}

pub fn ensure_space(c: &Client, name: &str) -> Result<String> {
    for sp in c.list_spaces(None)? {
        if sp["name"] == name && sp.get("status").map(|s| s == "active").unwrap_or(true) {
            return Ok(sp["id"].as_str().unwrap_or_default().to_string());
        }
    }
    let created = c.create_space(name)?;
    Ok(created["id"].as_str().unwrap_or_default().to_string())
}

/// Strict lookup by name or id — `run --from-space` must not mint a
/// space on a typo; ensure_space's create is serve/deploy-only.
pub fn find_space(c: &Client, name_or_id: &str) -> Result<String> {
    for sp in c.list_spaces(None)? {
        if (sp["name"] == name_or_id || sp["id"] == name_or_id)
            && sp.get("status").map(|s| s == "active").unwrap_or(true)
        {
            return Ok(sp["id"].as_str().unwrap_or_default().to_string());
        }
    }
    anyhow::bail!("space not found: {name_or_id:?} (run --from-space never creates one)")
}

/// The space's single derived general chat object — every space has
/// exactly one, materialized by the server and reported on the
/// single-space GET (ADR-006 §0). anybao watches this instead of a
/// self-created chat object, so it shares the space's canonical chat
/// with any other client (desktop UI, etc.).
fn general_chat(c: &Client, space: &str) -> Result<String> {
    let info = c.get_space(space)?;
    info["generalChatObjectId"]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .context("space has no generalChatObjectId — any server too old to derive it")
}

/// The space's single derived config object (ADR-006 §3), reported as
/// `agentConfigObjectId` on the single-space GET (same delivery path as
/// generalChatObjectId). Holds the space-scope override layer of the
/// config cascade. Returns None (not an error) when the server is too
/// old to derive it — the harness then runs on hardcoded defaults only.
fn agent_config_object(c: &Client, space: &str) -> Option<String> {
    c.get_space(space).ok()?["agentConfigObjectId"]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

/// The Anthropic API key's config key — the record id/`key` on the config
/// object AND the `secrets` map ref the llm effect resolves (config
/// defaults declare `api_key_ref: "llm.key.anthropic"`).
const ANTHROPIC_SECRET_REF: &str = "llm.key.anthropic";

/// The `agent_config` dataset name (mirrors the server-side type).
const CONFIG_DATASET: &str = "agent_config";

/// Space-scope config overrides read off the config object's
/// `agent_config` dataset: one record per dotted key, `{key, value}`.
/// These shadow the hardcoded `bootstrap` defaults (cascade:
/// space-override ?? default). Best-effort: a query hiccup or an
/// empty/fresh object yields no overrides rather than failing serve.
fn config_overrides(c: &Client, space: &str, obj: &str) -> Vec<(String, Value)> {
    let rows = match c.query(space, obj, CONFIG_DATASET, &json!({})) {
        Ok(rows) => rows,
        Err(e) => {
            eprintln!("config overrides unavailable ({e}); using defaults");
            return Vec::new();
        }
    };
    rows.iter()
        .filter_map(|r| {
            let k = r.get("key")?.as_str()?.to_string();
            Some((k, r.get("value")?.clone()))
        })
        .collect()
}

/// Device-local secret persistence (ADR-006 §3). Config secrets (the
/// Anthropic API key) live as a never-synced `localValue` on their config
/// record, not in synced space data. Bootstrap-once on serve start:
///
/// - stored device-local value present → it is authoritative; load it into
///   `secrets` (any env var is a noop this run);
/// - stored empty but env `ANTHROPIC_API_KEY` present (already in
///   `secrets` via `bootstrap`) → persist it device-locally now, so later
///   starts need no env;
/// - both empty → warn (serve still starts; the llm effect fails on first
///   use until a key is provided).
///
/// Best-effort: a read/write hiccup never fails serve — it falls back to
/// whatever env supplied this run.
fn bootstrap_secret(c: &Client, space: &str, obj: &str, secrets: &mut BTreeMap<String, String>) {
    match stored_local_secret(c, space, obj, ANTHROPIC_SECRET_REF) {
        Some(key) => {
            secrets.insert(ANTHROPIC_SECRET_REF.into(), key);
            println!("config: anthropic key loaded from device-local store");
        }
        None => match secrets.get(ANTHROPIC_SECRET_REF).cloned() {
            Some(env_key) if !env_key.is_empty() => {
                match persist_local_secret(c, space, obj, ANTHROPIC_SECRET_REF, &env_key) {
                    Ok(()) => println!("config: anthropic key bootstrapped to device-local store"),
                    Err(e) => eprintln!(
                        "config: could not persist anthropic key device-locally ({e}); \
                         using env value this run"
                    ),
                }
            }
            _ => eprintln!(
                "WARN config: no anthropic key — set ANTHROPIC_API_KEY once to seed the \
                 device-local store, or write a localValue on the config object; \
                 llm effects will fail until one is set"
            ),
        },
    }
}

/// Read the device-local `localValue` off the config record for `key`
/// (None on any hiccup or when unset/empty).
fn stored_local_secret(c: &Client, space: &str, obj: &str, key: &str) -> Option<String> {
    let rows = c.query(space, obj, CONFIG_DATASET, &json!({})).ok()?;
    rows.iter()
        .find(|r| r.get("key").and_then(|v| v.as_str()) == Some(key))
        .and_then(|r| r.get("localValue").and_then(|v| v.as_str()))
        .map(str::to_string)
        .filter(|s| !s.is_empty())
}

/// The two-step local-scope write the server requires (ADR-006 §3): a
/// synced upsert materializes the record — carrying only the non-secret
/// key name + a `secret` marker — then a device-local `$set` writes the
/// secret into the never-synced `localValue` field. Local scope cannot
/// create records, hence the synced record first.
fn persist_local_secret(c: &Client, space: &str, obj: &str, key: &str, secret: &str) -> Result<()> {
    c.upsert_record(
        space,
        obj,
        CONFIG_DATASET,
        key,
        &json!({"key": key, "secret": true}),
    )?;
    c.set_local_field(
        space,
        obj,
        CONFIG_DATASET,
        key,
        "localValue",
        &json!(secret),
    )?;
    Ok(())
}

struct Shared {
    triggers: Mutex<BTreeMap<String, Trigger>>,
    scheduler: Mutex<Scheduler>,
    watcher: Mutex<Watcher>,
}

pub fn serve(mut cfg: Config) -> Result<()> {
    let client = Arc::new(Client::new(&cfg.addr));
    let space = ensure_space(&client, &cfg.agent_space)?;
    let chat = general_chat(&client, &space)?;
    let anchor = ensure_typed(&client, &space, "agent-triggers", "agent_trigger")?;

    // Config cascade (ADR-006 §3): hardcoded defaults (from `bootstrap`)
    // under the space-scope override layer read off the config object;
    // secrets (the API key) persist device-locally on the same object.
    match agent_config_object(&client, &space) {
        Some(obj) => {
            let overrides = config_overrides(&client, &space, &obj);
            println!("config obj={obj} overrides={}", overrides.len());
            for (k, v) in overrides {
                cfg.config.insert(k, v);
            }
            bootstrap_secret(&client, &space, &obj, &mut cfg.secrets);
        }
        None => {
            eprintln!("space has no agentConfigObjectId — running on config defaults");
            if !cfg.secrets.contains_key(ANTHROPIC_SECRET_REF) {
                eprintln!(
                    "WARN config: no ANTHROPIC_API_KEY and no config object to read a \
                     device-local key from; llm effects will fail"
                );
            }
        }
    }
    let brain = client.get_brain(&space)?["objectId"]
        .as_str()
        .unwrap_or_default()
        .to_string();

    let deployed = Deployer::new(&client, &space).deploy_dir(&cfg.programs)?;
    println!("deploy → {deployed:?}");
    let skills = SkillDeployer::new(&client, &space).deploy_dir(&cfg.skills)?;
    println!("skills → {skills:?}");
    // The system prompt is composed guest-side (toolcaller@v1) from the
    // space — the host publishes skills above but injects no prompt wording
    // (isolation: the agent's context comes from `any`, not the filesystem).

    let kernel_bytes = std::fs::read(&cfg.kernel)
        .with_context(|| format!("kernel at {}", cfg.kernel.display()))?;
    let cage = Cage::new(&kernel_bytes)?;
    std::fs::create_dir_all(&cfg.traces_dir)?;

    let instance = format!("anyrt-{}", std::process::id());
    let mut sched = Scheduler::new(&instance, Box::new(now_s));
    sched.arm();
    let mut registry = BTreeMap::new();
    for t in standing_triggers(&space, &chat, &brain, &instance) {
        client.upsert_record(
            &space,
            &anchor,
            "agent_triggers",
            &t.id,
            &trigger_to_record(&t),
        )?;
        registry.insert(t.id.clone(), t);
    }
    let shared = Arc::new(Shared {
        triggers: Mutex::new(registry),
        scheduler: Mutex::new(sched),
        watcher: Mutex::new(Watcher::default()),
    });

    let ctx = Arc::new(RunCtx {
        cage,
        client: client.clone(),
        cfg,
        space: space.clone(),
        chat: chat.clone(),
        anchor: anchor.clone(),
    });

    control_api(shared.clone(), ctx.clone());
    trigger_ticker(shared.clone(), ctx.clone());
    println!(
        "anyrt serving space={space} chat={chat} control=127.0.0.1:{}",
        ctx.cfg.control_port
    );

    loop {
        // reconnect loop: each feed drops its snapshot, so no replay
        match watch_chat(&shared, &ctx) {
            Ok(()) => {}
            Err(e) => eprintln!("feed error: {e}; reconnecting in 2s"),
        }
        std::thread::sleep(Duration::from_secs(2));
    }
}

pub struct RunCtx {
    pub cage: Arc<Cage>,
    pub client: Arc<Client>,
    pub cfg: Config,
    pub space: String,
    pub chat: String,
    pub anchor: String,
}

impl RunCtx {
    pub fn new_run_id() -> String {
        format!("run_{}", &uuid::Uuid::new_v4().simple().to_string()[..16])
    }

    fn broker(&self, spec: &str, run_id: String) -> Broker {
        let mut writer = TraceWriter::new(json!({"id": run_id, "program": spec,
                                             "host": "rust"}));
        let trace_path = self.cfg.traces_dir.join(format!("{run_id}.jsonl"));
        if let Err(e) = writer.stream_to(&trace_path) {
            eprintln!("trace streaming unavailable ({e}); will write at run end");
        }
        let mut b = Broker::new(
            writer,
            self.cfg.config.clone(),
            self.cfg.secrets.clone(),
            // no local programs dir: serve resolves modules from the
            // space only (the resolver below) — never the filesystem
            None,
            Classifier::new(Some(&self.cfg.addr)),
        );
        b.resolver = Some(Box::new(AnyModuleResolver::new(
            self.client.clone(),
            &self.space,
            None,
            Default::default(),
        )));
        b
    }

    pub fn run(
        &self,
        spec: &str,
        args: &Value,
        mailbox: SharedMailbox,
        interrupt: Arc<AtomicBool>,
        // Some(id) ties this run's trace to an id already handed to the
        // guest (agent_turns.traceRef); None mints a fresh one
        run_id: Option<String>,
    ) -> Result<(String, RunResult)> {
        let broker = self.broker(spec, run_id.unwrap_or_else(Self::new_run_id));
        let run_id = broker.writer.run_id();
        let outcome = run_program(&self.cage, broker, spec, args, mailbox, interrupt, 600.0)?;
        let path = self.cfg.traces_dir.join(format!("{run_id}.jsonl"));
        outcome.broker.writer.dump(&path)?;
        Ok((
            run_id.clone(),
            RunResult {
                status: if outcome.status == "ok" {
                    "ok".into()
                } else {
                    "error".into()
                },
                duration_ms: outcome.duration_ms,
                trace_ref: Some(run_id),
                fuel: Some(outcome.fuel_used as i64),
                error: outcome.error.map(|e| e.to_string()),
            },
        ))
    }
}

fn spawn_conversation(shared: &Arc<Shared>, ctx: &Arc<RunCtx>, text: String) {
    let mailbox: SharedMailbox = Default::default();
    let interrupt = Arc::new(AtomicBool::new(false));
    shared
        .watcher
        .lock()
        .unwrap()
        .live
        .insert(ctx.chat.clone(), mailbox.clone());
    let shared = shared.clone();
    let ctx = ctx.clone();
    std::thread::spawn(move || {
        let run_id = RunCtx::new_run_id();
        let args = json!({
            "space": ctx.space, "chatId": ctx.chat, "userText": text,
            "agentName": ctx.cfg.agent_name, "traceRef": run_id});
        let result = ctx.run("toolcaller@v1", &args, mailbox, interrupt, Some(run_id));
        if let Ok((trace_ref, rr)) = &result {
            if rr.status != "ok" {
                let _ = ctx.client.chat_send(
                    &ctx.space,
                    &ctx.chat,
                    &json!({
                    "text": format!("Something broke mid-run (trace {trace_ref})."),
                    "agent": {"name": ctx.cfg.agent_name, "done": true}}),
                );
            }
        }
        shared.watcher.lock().unwrap().conversation_done(&ctx.chat);
    });
}

fn watch_chat(shared: &Arc<Shared>, ctx: &Arc<RunCtx>) -> Result<()> {
    for frame in ctx.client.subscribe_dataset(
        &ctx.space,
        &ctx.chat,
        "chat_messages",
        &json!({"sort": ["-createdAt"], "limit": 64}),
    )? {
        match frame.event.as_str() {
            "ready" | "snapshot" => continue,
            "closed" => return Ok(()),
            "changes" => {
                for record in records_in(&frame.data) {
                    let action = shared
                        .watcher
                        .lock()
                        .unwrap()
                        .on_message(&ctx.chat, &record);
                    if let WatchAction::Start = action {
                        let text = record["text"].as_str().unwrap_or("").to_string();
                        println!("conversation started: {:?}", &text[..text.len().min(60)]);
                        spawn_conversation(shared, ctx, text);
                    }
                }
            }
            other => anyhow::bail!("unexpected frame {other:?}"),
        }
    }
    Ok(())
}

fn records_in(data: &Value) -> Vec<Value> {
    let mut out = Vec::new();
    if let Some(batches) = data.as_array() {
        for batch in batches {
            for key in ["added", "updated"] {
                for entry in batch[key].as_array().unwrap_or(&Vec::new()) {
                    let mut rec = entry["doc"].as_object().cloned().unwrap_or_default();
                    rec.insert("id".into(), entry["id"].clone());
                    out.push(Value::Object(rec));
                }
            }
        }
    }
    out
}

fn trigger_ticker(shared: Arc<Shared>, ctx: Arc<RunCtx>) {
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(5));
        let due: Vec<Trigger> = {
            let mut reg = shared.triggers.lock().unwrap();
            let sched = shared.scheduler.lock().unwrap();
            let mut out = Vec::new();
            for t in reg.values_mut() {
                if t.kind == "cron" && sched.cron_due(t) {
                    out.push(t.clone());
                    sched.advance_cron(t);
                }
            }
            out
        };
        for t in due {
            let mailbox: SharedMailbox = Default::default();
            let interrupt = Arc::new(AtomicBool::new(false));
            let result = ctx.run(&t.program, &t.args, mailbox, interrupt, None);
            let rr = result.map(|(_, rr)| rr).unwrap_or_else(|e| RunResult {
                status: "error".into(),
                duration_ms: 0,
                trace_ref: None,
                fuel: None,
                error: Some(e.to_string()),
            });
            let mut reg = shared.triggers.lock().unwrap();
            if let Some(live) = reg.get_mut(&t.id) {
                let sched = shared.scheduler.lock().unwrap();
                let run_rec = sched.record_run(live, &rr);
                let ts_ms = (now_s() * 1000.0) as i64;
                let rid = format!("{}:{:020}", t.id, ts_ms);
                let _ = ctx.client.upsert_record(
                    &ctx.space,
                    &ctx.anchor,
                    "agent_trigger_runs",
                    &rid,
                    &run_rec,
                );
                let _ = ctx.client.upsert_record(
                    &ctx.space,
                    &ctx.anchor,
                    "agent_triggers",
                    &t.id,
                    &trigger_to_record(live),
                );
            }
        }
    });
}

// --- the localhost control API -------------------------------------------------

fn control_api(shared: Arc<Shared>, ctx: Arc<RunCtx>) {
    std::thread::spawn(move || {
        let server = match tiny_http::Server::http(("127.0.0.1", ctx.cfg.control_port)) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("control API bind failed: {e}");
                return;
            }
        };
        for mut req in server.incoming_requests() {
            let path = req.url().to_string();
            let method = req.method().as_str().to_string();
            let mut body = String::new();
            let _ = std::io::Read::read_to_string(req.as_reader(), &mut body);
            let reply = handle_control(&shared, &ctx, &method, &path, &body);
            let (status, payload) = match reply {
                Ok(v) => (200, v),
                Err(e) => (400, json!({"error": e.to_string()})),
            };
            let data = payload.to_string();
            let _ = req.respond(
                tiny_http::Response::from_string(data)
                    .with_status_code(status)
                    .with_header(
                        "Content-Type: application/json"
                            .parse::<tiny_http::Header>()
                            .unwrap(),
                    ),
            );
        }
    });
}

fn handle_control(
    shared: &Shared,
    ctx: &RunCtx,
    method: &str,
    path: &str,
    body: &str,
) -> Result<Value> {
    let parts: Vec<&str> = path.trim_matches('/').split('/').collect();
    let mut reg = shared.triggers.lock().unwrap();
    match (method, parts.as_slice()) {
        ("GET", ["triggers"]) => Ok(Value::Array(reg.values().map(rollup).collect())),
        ("GET", ["triggers", id]) => {
            let t = reg.get(*id).context("trigger not found")?;
            Ok(json!({"id": t.id})
                .as_object()
                .cloned()
                .map(|mut m| {
                    m.extend(
                        trigger_to_record(t)
                            .as_object()
                            .cloned()
                            .unwrap_or_default(),
                    );
                    Value::Object(m)
                })
                .unwrap())
        }
        ("GET", ["triggers", id, "runs"]) => {
            let rows = ctx.client.query(
                &ctx.space,
                &ctx.anchor,
                "agent_trigger_runs",
                &json!({"filter": {"triggerId": id}, "sort": ["-ts"],
                        "limit": 20}),
            )?;
            Ok(Value::Array(rows))
        }
        ("PATCH", ["triggers", id]) => {
            let t = reg.get_mut(*id).context("trigger not found")?;
            let patch: Map<String, Value> = serde_json::from_str(body)?;
            if let Some(spec) = patch.get("spec") {
                t.spec = spec.clone();
                t.next_due = None; // re-arm forward on the new schedule
            }
            if let Some(en) = patch.get("enabled").and_then(|v| v.as_bool()) {
                t.enabled = en;
            }
            Ok(trigger_to_record(t))
        }
        ("POST", ["triggers", id, "enable"]) => {
            let t = reg.get_mut(*id).context("trigger not found")?;
            t.enabled = true;
            t.consecutive_failures = 0; // manual re-enable resets the breaker
            t.next_due = None;
            Ok(trigger_to_record(t))
        }
        ("POST", ["triggers", id, "disable"]) => {
            let t = reg.get_mut(*id).context("trigger not found")?;
            t.enabled = false;
            Ok(trigger_to_record(t))
        }
        _ => anyhow::bail!("no route: {method} {path}"),
    }
}
