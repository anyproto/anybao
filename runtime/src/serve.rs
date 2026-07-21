//! `anyrt serve` — the outer loop: ensure space/chat/anchor, resolve
//! overlays from the space (space-only, ADR-009 §5 — `anyrt deploy`
//! is the publish step; the kernel is embedded), then watch the chat
//! (drop-snapshot SSE), tick triggers, and answer the localhost
//! control API. Conversations and trigger runs are guest programs
//! through the shared cage.

use crate::anyapi::Client;
use crate::broker::{Broker, SharedMailbox};
use crate::config::Config;
use crate::deploy::ensure_typed;
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
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tracing::{error, info, warn};

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
            warn!("config overrides unavailable ({e}); using defaults");
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
            info!("config: anthropic key loaded from device-local store");
        }
        None => match secrets.get(ANTHROPIC_SECRET_REF).cloned() {
            Some(env_key) if !env_key.is_empty() => {
                match persist_local_secret(c, space, obj, ANTHROPIC_SECRET_REF, &env_key) {
                    Ok(()) => info!("config: anthropic key bootstrapped to device-local store"),
                    Err(e) => warn!(
                        "config: could not persist anthropic key device-locally ({e}); \
                         using env value this run"
                    ),
                }
            }
            _ => warn!(
                "config: no anthropic key — set ANTHROPIC_API_KEY once to seed the \
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

/// The resolver alias namespace (ADR-009 §2): every `[overlays]` entry
/// (its space id) verbatim; a missing `agent` entry binds the alias to
/// the working space — the degenerate single-space shape.
pub fn alias_map(
    overlays: &BTreeMap<String, crate::config::Overlay>,
    working_space: &str,
) -> BTreeMap<String, String> {
    let mut m: BTreeMap<String, String> = overlays
        .iter()
        .map(|(name, o)| (name.clone(), o.space.clone()))
        .collect();
    m.entry("agent".into())
        .or_insert_with(|| working_space.to_string());
    m
}

/// The embedder's handle on a running agent (ADR-009 §6): `ctx` is the
/// per-turn API (`RunCtx::run`); `stop()` flips the shutdown flag and
/// joins the watch/ticker/control threads. In-flight conversation
/// threads are not joined — a running turn finishes on its own.
pub struct AgentHandle {
    pub ctx: Arc<RunCtx>,
    shutdown: Arc<AtomicBool>,
    threads: Vec<std::thread::JoinHandle<()>>,
}

impl AgentHandle {
    /// Signal shutdown and join the service threads. Latency is
    /// bounded by the SSE stream: the watcher only observes the flag
    /// on the next frame/heartbeat (or the sliced reconnect sleep).
    pub fn stop(mut self) -> Result<()> {
        self.shutdown.store(true, Ordering::Relaxed);
        self.join_all()
    }

    /// Block until the service threads exit (the CLI path — they only
    /// exit on `stop()` from another handle-holder or a bind failure).
    pub fn join(mut self) -> Result<()> {
        self.join_all()
    }

    fn join_all(&mut self) -> Result<()> {
        for t in self.threads.drain(..) {
            t.join()
                .map_err(|_| anyhow::anyhow!("agent service thread panicked"))?;
        }
        Ok(())
    }
}

/// The blocking CLI surface: start + wait forever.
pub fn serve(cfg: Config) -> Result<()> {
    start(cfg)?.join()
}

/// One overlay's membership state after the boot probe (ADR-009 §8).
#[derive(Debug, PartialEq, Eq)]
pub enum OverlayMembership {
    /// space visible on this device — usable now
    Member,
    /// join requested (or still syncing) — boot proceeds, readiness is
    /// re-checked when a chat message arrives
    Pending,
}

/// The overlay space's status in THIS account's space list (a local
/// read — deliberately NOT `get_space`, which for a never-tracked id
/// sends the server on an unbounded remote load that can wedge the
/// whole Spaces surface; observed live 2026-07-21, upstream fix
/// pending). None = the account doesn't track the space at all.
fn tracked_status(client: &Client, id: &str) -> Result<Option<String>> {
    for sp in client.list_spaces(None)? {
        if sp["id"] == id {
            return Ok(Some(sp["status"].as_str().unwrap_or("active").to_string()));
        }
    }
    Ok(None)
}

/// ADR-009 §8: probe one overlay; when this account isn't a member yet
/// and an invite is configured, send the join request and PROCEED —
/// never block or poll. The invite may be a RequestToJoin token (owner
/// approval grants `reader`) or a public GUEST token (read-only, no
/// approval — the server auto-detects). A missing invite is a hard
/// boot error. Membership is read off the SPACE LIST (local), never a
/// remote-loading single-space GET.
pub fn probe_or_join_overlay(
    client: &Client,
    name: &str,
    overlay: &crate::config::Overlay,
) -> Result<OverlayMembership> {
    let id = &overlay.space;
    match tracked_status(client, id)? {
        Some(status) if status == "active" => return Ok(OverlayMembership::Member),
        // tracked but still joining/loading (a prior join, a guest
        // load in progress) — nothing to send, just not ready yet
        Some(status) => {
            info!("overlay {name:?}: space tracked, status {status:?} — waiting for it");
            return Ok(OverlayMembership::Pending);
        }
        None => {}
    }
    let Some(invite) = &overlay.invite else {
        anyhow::bail!(
            "overlay {name:?} space {id}: not joined and no invite \
             configured; set `{name} = {{ space = \"{id}\", invite = \"…\" }}` \
             in [overlays] (ADR-009 §8)"
        );
    };
    let status = match client.join_space(invite, Some(&json!({"name": "anybao"}))) {
        Ok((status, _info)) => status,
        // 409 space.already_member / space.deleted: the account already
        // tracks the space (raced our probe, or a locally-deleted guest
        // space) — pending; the message-driven recheck settles it.
        Err(e) if e.status == 409 => {
            info!("overlay {name:?}: {e} — treating as pending sync");
            return Ok(OverlayMembership::Pending);
        }
        Err(e) => anyhow::bail!("overlay {name:?}: join request failed: {e}"),
    };
    if status == 202 {
        // member invite: the publisher's approval is pending; guest
        // token: the space is loading in the background — either way,
        // wait for it to arrive.
        info!("overlay {name:?}: join accepted — waiting for the space to arrive");
    } else {
        info!("overlay {name:?}: joined, waiting for first sync");
    }
    Ok(OverlayMembership::Pending)
}

/// Compile the cage: the embedded kernel, or a `--kernel <path>` dev
/// override (ADR-009 §4 — binary + kernel are one artifact).
fn load_cage(cfg: &Config) -> Result<Arc<Cage>> {
    match &cfg.kernel {
        Some(path) => Cage::new(
            &std::fs::read(path).with_context(|| format!("kernel at {}", path.display()))?,
        ),
        None => Cage::embedded(),
    }
}

/// Everything serve does up to the watch loop, which is spawned —
/// returns immediately with the handle (the lib-mode surface).
pub fn start(mut cfg: Config) -> Result<AgentHandle> {
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
            info!("config obj={obj} overrides={}", overrides.len());
            for (k, v) in overrides {
                cfg.config.insert(k, v);
            }
            bootstrap_secret(&client, &space, &obj, &mut cfg.secrets);
        }
        None => {
            warn!("space has no agentConfigObjectId — running on config defaults");
            if !cfg.secrets.contains_key(ANTHROPIC_SECRET_REF) {
                warn!(
                    "config: no ANTHROPIC_API_KEY and no config object to read a \
                     device-local key from; llm effects will fail"
                );
            }
        }
    }
    let brain = client.get_brain(&space)?["objectId"]
        .as_str()
        .unwrap_or_default()
        .to_string();

    // serve is space-only (ADR-009 §5): programs, skills, and the kernel
    // are already IN the space(s) — `anyrt deploy` is the publish step.
    // The system prompt is composed guest-side (toolcaller@v1) from the
    // space; the host injects no prompt wording (isolation: the agent's
    // context comes from `any`, not the filesystem).

    // Overlays (ADR-009 §2, §8): probe each configured space; unseen +
    // invite → join request sent, boot proceeds (no polling —
    // readiness is re-checked per incoming message).
    let mut pending: BTreeMap<String, String> = BTreeMap::new();
    for (name, overlay) in &cfg.overlays {
        if overlay.space != space
            && probe_or_join_overlay(&client, name, overlay)? == OverlayMembership::Pending
        {
            pending.insert(name.clone(), overlay.space.clone());
        }
    }
    let aliases = alias_map(&cfg.overlays, &space);
    let code_space = aliases["agent"].clone();

    // kernel is embedded (ADR-009 §4) — the cage always boots eagerly;
    // pending overlays only gate program resolution
    let cage = load_cage(&cfg)?;
    if !pending.is_empty() {
        info!(
            "overlays still joining/syncing: {:?} — will answer with status until synced",
            pending.keys().collect::<Vec<_>>()
        );
    }
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
        pending_overlays: Mutex::new(pending),
        client: client.clone(),
        cfg,
        space: space.clone(),
        chat: chat.clone(),
        anchor: anchor.clone(),
        aliases,
        code_space,
    });

    let shutdown = Arc::new(AtomicBool::new(false));
    let mut threads = vec![
        control_api(shared.clone(), ctx.clone(), shutdown.clone()),
        trigger_ticker(shared.clone(), ctx.clone(), shutdown.clone()),
    ];
    info!(
        "anyrt serving space={space} chat={chat} control=127.0.0.1:{}",
        ctx.cfg.control_port
    );

    {
        let (shared, ctx, stop) = (shared.clone(), ctx.clone(), shutdown.clone());
        threads.push(std::thread::spawn(move || {
            while !stop.load(Ordering::Relaxed) {
                // reconnect loop: each feed drops its snapshot, no replay
                match watch_chat(&shared, &ctx, &stop) {
                    Ok(()) => {}
                    Err(e) => warn!("feed error: {e}; reconnecting in 2s"),
                }
                sliced_sleep(Duration::from_secs(2), &stop);
            }
        }));
    }

    Ok(AgentHandle {
        ctx,
        shutdown,
        threads,
    })
}

/// Sleep in 100ms slices so a shutdown flag is observed promptly.
fn sliced_sleep(total: Duration, stop: &AtomicBool) {
    let mut left = total;
    while !stop.load(Ordering::Relaxed) && !left.is_zero() {
        let step = left.min(Duration::from_millis(100));
        std::thread::sleep(step);
        left -= step;
    }
}

pub struct RunCtx {
    pub cage: Arc<Cage>,
    /// overlays whose spaces haven't synced yet (ADR-009 §8):
    /// name → space id — gates program resolution, not the cage
    pub pending_overlays: Mutex<BTreeMap<String, String>>,
    pub client: Arc<Client>,
    pub cfg: Config,
    pub space: String,
    pub chat: String,
    pub anchor: String,
    /// resolver alias namespace (ADR-009 §2) — overlays + the `agent`
    /// default
    pub aliases: BTreeMap<String, String>,
    /// the agent overlay's space id (= aliases["agent"])
    pub code_space: String,
}

impl RunCtx {
    pub fn new_run_id() -> String {
        format!("run_{}", &uuid::Uuid::new_v4().simple().to_string()[..16])
    }

    /// Cheap readiness check (no network) — the trigger ticker skips
    /// while overlays are pending.
    pub fn is_ready(&self) -> bool {
        self.pending_overlays.lock().unwrap().is_empty()
    }

    /// Re-probe pending overlays (ADR-009 §8). The error text is
    /// user-facing status — the watcher bubbles it into the chat while
    /// not ready.
    pub fn ensure_ready(&self) -> Result<()> {
        let mut pending = self.pending_overlays.lock().unwrap();
        if pending.is_empty() {
            return Ok(());
        }
        pending.retain(|_, sid| {
            !matches!(tracked_status(&self.client, sid), Ok(Some(ref st)) if st == "active")
        });
        if pending.is_empty() {
            info!("overlays synced — agent ready");
            return Ok(());
        }
        let list = pending
            .iter()
            .map(|(name, sid)| format!("overlay `{name}` (space `{sid}`)"))
            .collect::<Vec<_>>()
            .join(", ");
        anyhow::bail!("{list} still joining/syncing — waiting for the publisher's approval")
    }

    fn broker(&self, spec: &str, run_id: String) -> Broker {
        let mut writer = TraceWriter::new(json!({"id": run_id, "program": spec,
                                             "host": "rust"}));
        let trace_path = self.cfg.traces_dir.join(format!("{run_id}.jsonl"));
        if let Err(e) = writer.stream_to(&trace_path) {
            warn!("trace streaming unavailable ({e}); will write at run end");
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
            self.aliases.clone(),
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
        self.ensure_ready()?;
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
        // agent code resolves through the explicit alias (ADR-009 §2);
        // codeSpace lets the guest read overlay data (skills) directly.
        // Other overlays ride along for the prompt's repo inventory —
        // `agent` is excluded (it has its own Runtime-context line).
        let overlays: Map<String, Value> = ctx
            .aliases
            .iter()
            .filter(|(name, _)| name.as_str() != "agent")
            .map(|(name, id)| (name.clone(), json!(id)))
            .collect();
        let args = json!({
            "space": ctx.space, "chatId": ctx.chat, "userText": text,
            "agentName": ctx.cfg.agent_name, "traceRef": run_id,
            "codeSpace": ctx.code_space, "overlays": overlays});
        let result = ctx.run(
            "agent:toolcaller@v1",
            &args,
            mailbox,
            interrupt,
            Some(run_id),
        );
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

fn watch_chat(shared: &Arc<Shared>, ctx: &Arc<RunCtx>, stop: &AtomicBool) -> Result<()> {
    for frame in ctx.client.subscribe_dataset(
        &ctx.space,
        &ctx.chat,
        "chat_messages",
        &json!({"sort": ["-createdAt"], "limit": 64}),
    )? {
        // shutdown is observed per frame — the read itself blocks until
        // the server's next event/heartbeat (ADR-009 open Q3)
        if stop.load(Ordering::Relaxed) {
            return Ok(());
        }
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
                        // deferred boot (ADR-009 §8): a message while
                        // overlays are pending gets a status bubble —
                        // the one host-authored operational reply
                        match ctx.ensure_ready() {
                            Ok(_) => {
                                info!("conversation started: {:?}", &text[..text.len().min(60)]);
                                spawn_conversation(shared, ctx, text);
                            }
                            Err(status) => {
                                warn!("not ready: {status}");
                                let _ = ctx.client.chat_send(
                                    &ctx.space,
                                    &ctx.chat,
                                    &json!({
                                    "text": format!("Not ready yet: {status}."),
                                    "agent": {"name": ctx.cfg.agent_name, "done": true}}),
                                );
                            }
                        }
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

fn trigger_ticker(
    shared: Arc<Shared>,
    ctx: Arc<RunCtx>,
    stop: Arc<AtomicBool>,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || loop {
        sliced_sleep(Duration::from_secs(5), &stop);
        if stop.load(Ordering::Relaxed) {
            return;
        }
        // deferred boot (ADR-009 §8): don't burn trigger runs (and the
        // circuit breaker) while overlays are still syncing
        if !ctx.is_ready() {
            continue;
        }
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
    })
}

// --- the localhost control API -------------------------------------------------

fn control_api(
    shared: Arc<Shared>,
    ctx: Arc<RunCtx>,
    stop: Arc<AtomicBool>,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let server = match tiny_http::Server::http(("127.0.0.1", ctx.cfg.control_port)) {
            Ok(s) => s,
            Err(e) => {
                error!("control API bind failed: {e}");
                return;
            }
        };
        // recv_timeout instead of incoming_requests: the accept loop
        // must observe the shutdown flag (ADR-009 §6)
        while !stop.load(Ordering::Relaxed) {
            let mut req = match server.recv_timeout(Duration::from_millis(250)) {
                Ok(Some(r)) => r,
                Ok(None) => continue,
                Err(e) => {
                    error!("control API accept failed: {e}");
                    return;
                }
            };
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
    })
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Overlay;
    use crate::testutil::StubTransport;

    fn overlay(space: &str, invite: Option<&str>) -> Overlay {
        Overlay {
            space: space.into(),
            invite: invite.map(str::to_string),
        }
    }

    fn scripted(replies: &[(u16, Value)]) -> (Client, crate::testutil::CallLog) {
        let stub = StubTransport::new();
        for (status, body) in replies {
            stub.push(*status, body.clone());
        }
        let log = stub.log();
        (Client::with_transport(Box::new(stub)), log)
    }

    fn spaces_reply(rows: Value) -> (u16, Value) {
        (200, json!({"spaces": rows}))
    }

    #[test]
    fn probe_member_when_space_active_in_list() {
        // membership is a LOCAL space-list read — never a single-space
        // GET (which remote-loads never-tracked ids server-side)
        let (c, log) = scripted(&[spaces_reply(json!([{"id": "repo", "status": "active"}]))]);
        let m = probe_or_join_overlay(&c, "agent", &overlay("repo", None)).unwrap();
        assert_eq!(m, OverlayMembership::Member);
        let calls = log.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].1, "/v1/spaces");
    }

    #[test]
    fn probe_pending_when_tracked_but_loading() {
        // a prior join / guest load in progress: tracked, not active —
        // nothing to send, just wait
        let (c, log) = scripted(&[spaces_reply(json!([{"id": "repo", "status": "loading"}]))]);
        let m = probe_or_join_overlay(&c, "agent", &overlay("repo", Some("tok"))).unwrap();
        assert_eq!(m, OverlayMembership::Pending);
        assert_eq!(log.lock().unwrap().len(), 1); // no join call
    }

    #[test]
    fn probe_joins_and_proceeds_pending() {
        // untracked → join 202 → PENDING, no polling (ADR-009 §8)
        let (c, log) = scripted(&[
            spaces_reply(json!([])),
            (202, json!({"id": "repo", "status": "joining"})),
        ]);
        let m = probe_or_join_overlay(&c, "agent", &overlay("repo", Some("tok"))).unwrap();
        assert_eq!(m, OverlayMembership::Pending);
        let calls = log.lock().unwrap();
        assert_eq!(calls.len(), 2); // list + join — nothing else
        assert_eq!(calls[1].1, "/v1/spaces/join");
        assert_eq!(calls[1].2.as_ref().unwrap()["inviteToken"], json!("tok"));
    }

    #[test]
    fn probe_tolerates_already_tracked_409() {
        // guest-token join racing the list read: 409 space.already_member
        // — pending, not a boot failure (ADR-009 §8)
        let (c, _) = scripted(&[
            spaces_reply(json!([])),
            (
                409,
                json!({"error": {"code": "space.already_member",
                                 "message": "already tracks the space"}}),
            ),
        ]);
        let m = probe_or_join_overlay(&c, "agent", &overlay("repo", Some("guestTok"))).unwrap();
        assert_eq!(m, OverlayMembership::Pending);
    }

    #[test]
    fn probe_without_invite_names_the_fix() {
        let (c, _) = scripted(&[spaces_reply(json!([]))]);
        let err = probe_or_join_overlay(&c, "agent", &overlay("repo", None)).unwrap_err();
        assert!(err.to_string().contains("no invite"), "{err}");
        assert!(err.to_string().contains("[overlays]"), "{err}");
    }

    /// The readiness core, minus RunCtx plumbing (a real Cage needs
    /// wasm) — same retain-probe + status text as ensure_ready.
    fn recheck(client: &Client, pending: &mut BTreeMap<String, String>) -> Result<()> {
        pending.retain(|_, sid| client.get_space(sid).is_err());
        if pending.is_empty() {
            return Ok(());
        }
        let list = pending
            .iter()
            .map(|(name, sid)| format!("overlay `{name}` (space `{sid}`)"))
            .collect::<Vec<_>>()
            .join(", ");
        anyhow::bail!("{list} still joining/syncing — waiting for the publisher's approval")
    }

    #[test]
    fn readiness_reports_pending_overlay_status() {
        // FakeSpace has no get_space route → the space is still unseen
        let c = Client::with_transport(Box::new(crate::testutil::FakeSpace::new()));
        let mut pending: BTreeMap<String, String> =
            [("agent".to_string(), "repo".to_string())].into();
        let err = recheck(&c, &mut pending).expect_err("must be pending");
        assert!(err.to_string().contains("overlay `agent`"), "{err}");
        assert!(err.to_string().contains("still joining/syncing"), "{err}");
    }

    #[test]
    fn readiness_clears_once_spaces_are_visible() {
        let (c, _) = scripted(&[spaces_reply(json!([{"id": "repo", "status": "active"}]))]);
        let mut pending: BTreeMap<String, String> =
            [("agent".to_string(), "repo".to_string())].into();
        recheck(&c, &mut pending).unwrap();
        assert!(pending.is_empty());
    }

    #[test]
    fn alias_map_defaults_agent_to_working_space() {
        let m = alias_map(&BTreeMap::new(), "ws1");
        assert_eq!(m.get("agent").map(String::as_str), Some("ws1"));
    }

    #[test]
    fn alias_map_keeps_configured_overlays_verbatim() {
        let mut overlays = BTreeMap::new();
        overlays.insert(
            "agent".to_string(),
            crate::config::Overlay {
                space: "codeSpace".to_string(),
                invite: None,
            },
        );
        overlays.insert(
            "std".to_string(),
            crate::config::Overlay {
                space: "stdSpace".to_string(),
                invite: Some("tok".to_string()),
            },
        );
        let m = alias_map(&overlays, "ws1");
        assert_eq!(m.get("agent").map(String::as_str), Some("codeSpace"));
        assert_eq!(m.get("std").map(String::as_str), Some("stdSpace"));
        assert_eq!(m.len(), 2);
    }
}
