//! `anyrt serve` — the outer loop: ensure space/chat/anchor, deploy
//! programs + skills, compose the system prompt, then watch the chat
//! (drop-snapshot SSE), tick triggers, and answer the localhost
//! control API. Conversations and trigger runs are guest programs
//! through the shared cage.

use crate::anyapi::Client;
use crate::broker::{Broker, SharedMailbox};
use crate::deploy::{
    compose_system, load_skills_dir, memory_categories_section, tool_docs_section, Deployer,
    SkillDeployer,
};
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
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub struct ServeConfig {
    pub addr: String,
    pub space_name: String,
    pub chat_name: String,
    pub agent_name: String,
    pub programs: PathBuf,
    pub skills: PathBuf,
    pub kernel: PathBuf,
    pub traces_dir: PathBuf,
    pub control_port: u16,
    pub config: BTreeMap<String, Value>,
    pub secrets: BTreeMap<String, String>,
}

fn now_s() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
}

fn ensure_space(c: &Client, name: &str) -> Result<String> {
    for sp in c.list_spaces(None)? {
        if sp["name"] == name && sp.get("status").map(|s| s == "active").unwrap_or(true) {
            return Ok(sp["id"].as_str().unwrap_or_default().to_string());
        }
    }
    let created = c.create_space(name)?;
    Ok(created["id"].as_str().unwrap_or_default().to_string())
}

fn ensure_typed(c: &Client, space: &str, name: &str, type_id: &str) -> Result<String> {
    let rows = c.query_objects(
        space,
        &json!({
        "filter": {"any.name": name, "any.types": type_id}, "limit": 1}),
    )?;
    if let Some(r) = rows.first() {
        return Ok(r["id"].as_str().unwrap_or_default().to_string());
    }
    let created = c.create_object(
        space,
        &json!({
        "types": [type_id],
        "initialProperties": {"any": {"name": name}}}),
    )?;
    Ok(created["objectId"].as_str().unwrap_or_default().to_string())
}

struct Shared {
    triggers: Mutex<BTreeMap<String, Trigger>>,
    scheduler: Mutex<Scheduler>,
    watcher: Mutex<Watcher>,
}

pub fn serve(cfg: ServeConfig) -> Result<()> {
    let client = Arc::new(Client::new(&cfg.addr));
    let space = ensure_space(&client, &cfg.space_name)?;
    let chat = ensure_typed(&client, &space, &cfg.chat_name, "chat")?;
    let anchor = ensure_typed(&client, &space, "agent-triggers", "agent_trigger")?;
    let brain = client.get_brain(&space)?["objectId"]
        .as_str()
        .unwrap_or_default()
        .to_string();

    let deployed = Deployer::new(&client, &space).deploy_dir(&cfg.programs)?;
    println!("deploy → {deployed:?}");
    let skills = SkillDeployer::new(&client, &space).deploy_dir(&cfg.skills)?;
    println!("skills → {skills:?}");

    let mut system = compose_system(&load_skills_dir(&cfg.skills)?, &[]);
    if let Ok(section) = tool_docs_section(&client, &space) {
        if !section.is_empty() {
            system.push_str("\n\n");
            system.push_str(&section);
        }
    }
    if let Ok(section) = memory_categories_section(&client, &space) {
        if !section.is_empty() {
            system.push_str("\n\n");
            system.push_str(&section);
        }
    }

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
        system,
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
    pub cfg: ServeConfig,
    pub space: String,
    pub chat: String,
    pub anchor: String,
    pub system: String,
}

impl RunCtx {
    fn broker(&self) -> Broker {
        let run_id = format!("run_{}", &uuid::Uuid::new_v4().simple().to_string()[..16]);
        let writer = TraceWriter::new(json!({"id": run_id, "program": "",
                                             "host": "rust"}));
        let mut b = Broker::new(
            writer,
            self.cfg.config.clone(),
            self.cfg.secrets.clone(),
            self.cfg.programs.clone(),
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
    ) -> Result<(String, RunResult)> {
        let broker = self.broker();
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
        let run_id = format!("run_{}", &uuid::Uuid::new_v4().simple().to_string()[..16]);
        let args = json!({
            "space": ctx.space, "chatId": ctx.chat, "userText": text,
            "system": ctx.system, "agentName": ctx.cfg.agent_name,
            "traceRef": run_id});
        let result = ctx.run("toolcaller@v1", &args, mailbox, interrupt);
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
            let result = ctx.run(&t.program, &t.args, mailbox, interrupt);
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
