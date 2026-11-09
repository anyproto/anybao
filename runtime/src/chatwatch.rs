//! The chat-watch helpers (ADR-009 §8, ADR-005 §3, ADR-018 §3) shared by the
//! desktop serve and the managed host, moved out of `serve.rs` (N2b-1, the
//! N1 method). Verbatim: `snapshot_backlog`, `preview`, `records_in`,
//! `BREAK_GRACE`, `seed_chat_watch`, `reply_tombstoned`. Two bodies are not:
//! `resolve_reply` takes the client and the chat's addresses instead of the
//! desktop `RunCtx`, and `on_break`'s grace sleeper body is the pure
//! `escalate_soft_break` so the desktop thread and the managed service loop's
//! deadline (no detached sleeper on Android, ADR-034 §4) share one rule.
//! The desktop watch thread, `start_or_inject` and `resolve_sender` stay in
//! `serve`; the managed chat source (N2b-2) is the other caller.
//!
//! N2b-2 C0 moved the chat-run follow-ups here too, so both hosts post the
//! same bubbles in the same order: `short_error`, `is_model_setup`,
//! `tier_identity` and `post_credential_requests` verbatim (the last takes the
//! client and addresses instead of the desktop `RunCtx`); the selection in
//! `start_or_inject` as `follow_ups` + `follow_up_body`; the interrupted
//! `agent_turns` record and its seq probe; and the one stand-down write,
//! `release_chat_watch`.
use crate::anyapi::{AnyError, Client};
use crate::stores::{now_rfc3339, upsert_secret_row};
use crate::triggers::{
    chat_watch_trigger, is_chat_watch, trigger_to_record, LiveRun, RunResult, Trigger, Watcher,
    CHAT_WATCH_ID,
};
use anyhow::{Context, Result};
use serde_json::{json, Value};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;
use tracing::{info, warn};

/// Soft-break grace (ADR-005 §3): the guest only drains its mailbox
/// between turns, so a run stuck inside a long cell cannot honor
/// "stop" by itself. If the break item is still UNDRAINED after this
/// long, the flag goes up and the epoch callback traps the guest at
/// its next tick (a blocked host call — an LLM or HTTP request —
/// returns first; the broker does not yet check the flag mid-call).
/// A guest that HAS drained the item is wrapping up: the escalation
/// stands down and the run's wall deadline is the backstop.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed chat source (N2b-2) is the no-default caller
pub(crate) const BREAK_GRACE: Duration = Duration::from_secs(20);

/// The soft-break escalation (ADR-005 §3), run once `BREAK_GRACE` after a
/// soft break landed on `live`: if the break item still sits in the mailbox
/// (the guest never drained it), set the flag (the Arc is the run's
/// identity; a finished run's flag is a dead letter). Returns whether the
/// break was escalated. The desktop `on_break` calls it from its grace
/// thread; the managed service loop calls it at the tracked deadline.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed chat source (N2b-2) is the no-default caller
pub(crate) fn escalate_soft_break(chat: &str, live: &LiveRun) -> bool {
    let undrained = live
        .mailbox
        .lock()
        .unwrap()
        .iter()
        .any(|m| m["kind"] == "break");
    if !undrained {
        return false; // acknowledged: the guest is wrapping up
    }
    if !live.interrupt.swap(true, Ordering::Relaxed) && Arc::strong_count(&live.interrupt) > 1 {
        info!("soft break on chat {chat} escalated to hard");
    }
    true
}

/// Messages newer than this agent's last own reply, oldest first. The
/// snapshot window is sorted `-createdAt`: walk from the newest record
/// and stop at the first SELF-authored one (own `agent.name`, or a
/// nameless agent record — same rule as the watcher, ADR-009 §8
/// amendment) — everything before it is unanswered. Foreign
/// agent-named messages (`trigger:*` nudges, peers) count as
/// unanswered input. Snapshot records are bare docs (each carries its
/// own `id`), unlike the `{id, doc}` entries of `changes` frames.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed chat source (N2b-2) is the no-default caller
pub(crate) fn snapshot_backlog(data: &Value, self_name: &str) -> Vec<Value> {
    let mut out: Vec<Value> = Vec::new();
    for rec in data["records"].as_array().unwrap_or(&Vec::new()) {
        if Watcher::is_self_message(rec, self_name) {
            // a done:false progress bubble is not an answer — keep
            // scanning; the cut is the last TERMINAL self reply
            if rec["agent"]["done"].as_bool().unwrap_or(true) {
                break;
            }
            continue;
        }
        // a control record (a Stop pressed during a feed gap) must
        // reach on_message — it is text-less by contract
        if crate::triggers::is_control(rec) {
            out.push(rec.clone());
            continue;
        }
        // content = text OR attachments (the server enforces at least
        // one); an attachment-only message is real input, not noise
        let has_text = rec["text"].as_str().is_some_and(|t| !t.is_empty());
        let has_atts = rec["attachments"]
            .as_object()
            .is_some_and(|a| !a.is_empty());
        if has_text || has_atts {
            out.push(rec.clone());
        }
    }
    out.reverse();
    out
}

/// Log-safe head of a message (char-boundary aware — byte slicing
/// panics mid-codepoint).
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed chat source (N2b-2) is the no-default caller
pub(crate) fn preview(text: &str) -> String {
    text.chars().take(60).collect()
}

/// A reply's referent, read from the chat before the watcher sees the
/// record (ADR-005 §5, BOB-65): deleted rows included so a tombstone
/// folds as such; a failed read folds as "not found" — a reply never
/// stalls on its target. Not an effect: like the record itself, it is
/// host-side input; the folded text is what the trace records.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed chat source (N2b-2) is the no-default caller
pub(crate) fn resolve_reply(client: &Client, space: &str, chat: &str, record: Value) -> Value {
    Watcher::with_reply(record, |id| {
        match client.query(
            space,
            chat,
            "chat_messages",
            &json!({"filter": {"id": id}, "includeDeleted": true, "limit": 1}),
        ) {
            Ok(mut rows) if !rows.is_empty() => Some(rows.remove(0)),
            Ok(_) => None,
            Err(e) => {
                warn!("reply target {id}: {e}");
                None
            }
        }
    })
}

/// The NEW `chat_messages` records in a `changes` frame. Only `added`
/// entries are input: an `updated` entry is a reaction toggle or a
/// text edit on a message that already had its turn, and neither
/// starts or injects a run (ADR-018 §2). Reading `updated` too made
/// a reaction on a message older than the process — absent from the
/// watcher's seen-set, which is seeded from the unanswered backlog
/// only — look like a fresh message, and the agent re-answered it.
///
/// And only an `added` entry WITH `ops` is a message (ADR-009 §8
/// promoted rows, BOB-204): `added` means the row entered the watch's
/// visible window, not that it was written. The server keeps one row
/// past the window's limit and, when a visible row is deleted, slides
/// it into view as `added` with its stored doc and NO ops — nothing
/// was written, so there is no change to ship. A written record always
/// carries ops (its stamps at least). The watch uses no projection,
/// the one case where a real change can ship empty ops.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed chat source (N2b-2) is the no-default caller
pub(crate) fn records_in(data: &Value) -> Vec<Value> {
    let mut out = Vec::new();
    if let Some(batches) = data.as_array() {
        for batch in batches {
            for entry in batch["added"].as_array().unwrap_or(&Vec::new()) {
                if entry["ops"].as_array().is_none_or(Vec::is_empty) {
                    warn!(
                        "chat message {} entered the window with no ops — a row promoted \
                         from past the limit, not a new message (ADR-009 §8)",
                        entry["id"].as_str().unwrap_or("")
                    );
                    continue;
                }
                let mut rec = entry["doc"].as_object().cloned().unwrap_or_default();
                rec.insert("id".into(), entry["id"].clone());
                out.push(Value::Object(rec));
            }
        }
    }
    out
}

/// Seed the chat-responder record once (ADR-018 §3): an existing
/// `chat-watch*` record is left untouched (its owner/enabled are user
/// state); otherwise write `chat-watch`, falling through generation
/// suffixes when the bare id was tombstoned by a delete. Returns the
/// live id and the trigger when this boot created it.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the desktop serve is the caller
pub(crate) fn seed_chat_watch(
    client: &Client,
    space: &str,
    anchor: &str,
    dataset: &str,
    chat: &str,
    owner: &str,
) -> Result<(String, Option<Trigger>)> {
    seed_chat_watch_checked(client, space, anchor, dataset, chat, owner, &|| false)
}

/// `seed_chat_watch` with a check before every write: the managed takeover
/// stops here once it is superseded or cancelled (Step B review F5), so a
/// stale takeover never lands an `owner = self` seed after its read. The
/// error says so; the caller re-checks its own flag for the typed reason.
pub(crate) fn seed_chat_watch_checked(
    client: &Client,
    space: &str,
    anchor: &str,
    dataset: &str,
    chat: &str,
    owner: &str,
    halted: &dyn Fn() -> bool,
) -> Result<(String, Option<Trigger>)> {
    let recs = client
        .query(space, anchor, dataset, &json!({}))
        .context("reading agent_triggers to seed the chat responder")?;
    if let Some(id) = recs
        .iter()
        .filter_map(|r| r.get("id").and_then(|v| v.as_str()))
        .find(|id| is_chat_watch(id))
    {
        return Ok((id.to_string(), None));
    }
    for gen in 1..=5u32 {
        let id = if gen == 1 {
            CHAT_WATCH_ID.to_string()
        } else {
            format!("{CHAT_WATCH_ID}-g{gen}")
        };
        let t = chat_watch_trigger(&id, chat, owner);
        if halted() {
            anyhow::bail!("chat responder: seeding halted before writing {id}");
        }
        match client.upsert_record(space, anchor, dataset, &id, &trigger_to_record(&t)) {
            Ok(reply) if reply_tombstoned(&reply) => continue,
            Ok(_) => return Ok((id, Some(t))),
            Err(e) if e.code.contains("record_deleted") => continue,
            Err(e) => return Err(e).context("seeding the chat responder record"),
        }
    }
    anyhow::bail!("chat responder: every generation id is tombstoned")
}

/// A modify reply whose rejections say the id is a deleted record.
pub(crate) fn reply_tombstoned(reply: &Value) -> bool {
    reply["rejections"].as_array().is_some_and(|rs| {
        rs.iter().any(|r| {
            ["reason", "code"]
                .iter()
                .any(|k| r[k].as_str().is_some_and(|v| v.contains("record_deleted")))
        })
    })
}

// --- chat-run follow-ups (ADR-021 §2, ADR-005 §3, ADR-009 §8) ----------------------

/// `type: message` of a run's error envelope, clipped for a chat line.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed chat bookkeeping (N2b-2) is the no-default caller
pub(crate) fn short_error(raw: &str) -> String {
    let s = serde_json::from_str::<Value>(raw)
        .ok()
        .and_then(|v| {
            Some(format!(
                "{}: {}",
                v["type"].as_str()?,
                v["message"].as_str()?
            ))
        })
        .unwrap_or_else(|| raw.to_string());
    let s = s.replace('\n', " ");
    if s.chars().count() > 160 {
        format!("{}…", s.chars().take(160).collect::<String>())
    } else {
        s
    }
}

/// Post the credential-request bubble for each ref still `missing`
/// whose row does not already name this chat as the place a live
/// request sits (`requestedIn`; cleared by the write that sets the
/// value) — ADR-021 §2. Everything the UI renders comes off the row;
/// the message only links it.
/// The onboarding marker (BOB-78, ADR-021 §2): a missing model key on a
/// space that has never chosen a provider — the client renders the
/// provider chooser instead of the plain key card. Derived from state,
/// never tracked: the missing ref is the codegen tier's `api_key_ref` AND
/// every `llm.tier.*` row still equals the embedded seed
/// (`config_defaults.json`). A row seeded by a toml `[config]` table or
/// written by a previous choice differs, and the plain card is posted.
/// No config store (or an unreadable one) = no marker.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed chat bookkeeping (N2b-2) is the no-default caller
pub(crate) fn is_model_setup(missing_ref: &str, tier: impl Fn(&str) -> Option<Value>) -> bool {
    let defaults = crate::config::config_defaults();
    let tiers: Vec<(&String, &Value)> = defaults
        .iter()
        .filter(|(k, _)| k.starts_with("llm.tier."))
        .collect();
    let Some(codegen) = tier("llm.tier.codegen") else {
        return false;
    };
    if codegen.get("api_key_ref").and_then(|v| v.as_str()) != Some(missing_ref) {
        return false;
    }
    tiers.iter().all(|(key, seed)| {
        tier(key)
            .as_ref()
            .map(|row| tier_identity(row) == tier_identity(seed))
            == Some(true)
    })
}

/// The four fields that make a tier row THIS provider — what the seed and a
/// stored row are compared on (extra inferred keys never break equality).
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed chat bookkeeping (N2b-2) is the no-default caller
fn tier_identity(row: &Value) -> [Option<String>; 4] {
    ["provider", "model", "base_url", "api_key_ref"].map(|k| {
        row.get(k)
            .and_then(|v| v.as_str())
            .map(|s| s.trim_end_matches('/').to_string())
    })
}

/// The desktop `post_credential_requests` body (N2b-2 C0, verbatim but for
/// its inputs): the secrets row store (`secrets_obj`/`secrets_dataset`), the
/// chat's space and id, the config store for the onboarding marker and the
/// agent's name come in as parameters instead of the desktop `RunCtx`; its
/// warnings go to `on_warning`, so each host logs them under its own target
/// (desktop keeps `anyrt::serve`, round A review A9). Returns how many cards
/// were posted.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed chat bookkeeping (N2b-2) is the no-default caller
#[allow(clippy::too_many_arguments)]
pub(crate) fn post_credential_requests(
    client: &Client,
    space: &str,
    chat: &str,
    secrets_obj: &str,
    secrets_dataset: &str,
    config: Option<&dyn crate::broker::ConfigStore>,
    agent_name: &str,
    refs: &[String],
    on_warning: &dyn Fn(String),
) -> usize {
    let mut posted = 0;
    for r in refs {
        let rows = client
            .query(space, secrets_obj, secrets_dataset, &json!({}))
            .unwrap_or_default();
        let row = rows
            .iter()
            .find(|row| row.get("key").and_then(|v| v.as_str()) == Some(r.as_str()));
        let status = row
            .and_then(|row| row.get("status"))
            .and_then(|v| v.as_str())
            .unwrap_or("missing");
        let requested_in = row
            .and_then(|row| row.get("requestedIn"))
            .and_then(|v| v.as_str());
        if status == "set" || requested_in == Some(chat) {
            continue;
        }
        let label = row
            .and_then(|row| row.get("label"))
            .and_then(|v| v.as_str())
            .map(str::to_string)
            .unwrap_or_else(|| r.clone());
        let hosts: Vec<String> = row
            .and_then(|row| row.get("hosts"))
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|h| h.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        let used_for = if hosts.is_empty() {
            String::new()
        } else {
            format!(", used for {}", hosts.join(", "))
        };
        // BOB-78: a never-chosen space asking for its model key gets the
        // provider chooser — a `&setup=model` marker on the row LINK (the
        // server's strict body rejects extra attachment fields; the link
        // is stored opaquely and an old client reads only `key`, so it
        // renders today's plain card) and provider-neutral text.
        let setup_model = status != "rejected"
            && config.is_some_and(|cs| {
                is_model_setup(r, |key| {
                    crate::broker::ConfigStore::read(cs, key).ok().flatten()
                })
            });
        let unreviewed = r.starts_with(crate::broker::LOCAL_KEY_PREFIX);
        let text = if unreviewed && status != "rejected" {
            // ADR-021 §8.2: the warning is in the TEXT (old clients render
            // only that); no program is named — a name would be the
            // requester's claim; the run is the provenance the host vouches for
            format!(
                "⚠ Code written by {} (reviewed by no one) asks for a credential: {label} \
                 (`{r}`). It will be sent only to {}.",
                agent_name,
                if hosts.is_empty() {
                    "nowhere".to_string()
                } else {
                    hosts.join(", ")
                }
            )
        } else if status == "rejected" {
            let code = row
                .and_then(|row| row.get("rejectedWith"))
                .and_then(|v| v.as_u64())
                .unwrap_or(401);
            let by = if hosts.is_empty() {
                "the service".to_string()
            } else {
                hosts.join(", ")
            };
            format!("The {label} was rejected by {by} ({code}) — please enter a new one (`{r}`).")
        } else if setup_model {
            "Before I can think, I need a language model. Choose a provider and paste its \
             API key — you can change this later in Model settings."
                .to_string()
        } else {
            format!("I need a credential to continue: {label} (`{r}`{used_for}).")
        };
        let marker = if setup_model { "&setup=model" } else { "" };
        let mut agent = json!({"name": agent_name, "done": true});
        if let Some(run) = row
            .and_then(|row| row.get("requestedBy"))
            .and_then(Value::as_str)
            .filter(|_| unreviewed)
        {
            agent["debugLink"] = json!(run); // §8.2: the trace shows the code
        }
        let sent = client.chat_send(
            space,
            chat,
            &json!({
                "text": text,
                "agent": agent,
                "attachments": {"credreq": {
                    "type": "credential_request",
                    "link": format!("any://o/{}?key={r}{marker}", secrets_obj)}}}),
        );
        match sent {
            Ok(_) => {
                posted += 1;
                // requestedIn/At only — status was stamped by the miss;
                // re-asserting it here could undo a save that landed
                // between the miss and this write
                let patch = json!({"requestedIn": chat,
                                   "requestedAt": {"$date": now_rfc3339()}});
                if let Err(e) =
                    upsert_secret_row(client, space, secrets_obj, secrets_dataset, r, &patch)
                {
                    on_warning(format!("secrets: could not stamp request for {r} ({e})"));
                }
            }
            Err(e) => on_warning(format!("secrets: could not post request for {r} ({e})")),
        }
    }
    posted
}

/// What the host posts into the chat once a conversation run ended (desktop
/// `start_or_inject`, N2b-2 C0): at most one entry — the four cases exclude
/// each other — in desktop order. `posted` is what
/// `post_credential_requests` returned for this run, computed first.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed chat bookkeeping (N2b-2) is the no-default caller
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum FollowUp {
    /// The run died on a credential miss whose request bubble already sits
    /// in the chat (`posted == 0`): the "Still waiting" line.
    StillWaiting { refs: String, why: String },
    /// ADR-009 §8: died on a program this device has not synced yet, changed
    /// nothing, first deferral. Desktop joins the wait set and re-queues the
    /// message; the frozen managed resolver never reports a program miss
    /// (`missing_programs` is empty there), so it never selects this.
    ProgramDeferred { specs: String },
    /// A hard break (ADR-005 §3): "Stopped." and the interrupted turn record.
    Stopped,
    /// Any other failed run: the typed error goes into the chat.
    Error { text: String },
}

#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed chat bookkeeping (N2b-2) is the no-default caller
pub(crate) fn follow_ups(rr: &RunResult, posted: usize, deferrals: u32) -> Vec<FollowUp> {
    let mut out = Vec::new();
    // ADR-021 §2: one request bubble per missing ref (the host
    // is the only emitter). A run that died ON the miss — the
    // LLM key — gets the request instead of "Something broke".
    // …or on the destination's 401 (LlmError) — either way the
    // request bubble (or the "still waiting" line) IS the reply
    // "error" only: an interrupted run's reply is "Stopped.",
    // never the "Still waiting for the credential" line (review)
    let died_on_miss = rr.status == "error" && !rr.missing_secrets.is_empty();
    if died_on_miss && posted == 0 {
        // the bubble for this ref already sits in the chat
        // (dedup) — a silent turn would read as "bao is gone";
        // the last error rides along so the human can see WHY
        let refs = rr.missing_secrets.join("`, `");
        let why = rr
            .error
            .as_deref()
            .map(short_error)
            .filter(|s| !s.is_empty())
            .map(|s| format!(" Last attempt: {s}"))
            .unwrap_or_default();
        out.push(FollowUp::StillWaiting { refs, why });
    }
    // ADR-009 §8: a run that died asking for a program this
    // device hasn't synced yet is a WAIT, not a failure to
    // show: the miss joins the wait set, the message goes back
    // on the backlog (once), and the ticker re-runs it when the
    // program arrives. Only a run that changed nothing is safe
    // to re-run — the trace's mutate count is the oracle; a
    // second miss, or a miss after a mutation, fails loudly
    // below like any other error.
    let program_gap = rr.status == "error" && !rr.missing_programs.is_empty();
    if program_gap && !died_on_miss && !rr.mutated && deferrals == 0 {
        let specs = rr
            .missing_programs
            .iter()
            .map(|(_, spec)| format!("`{spec}`"))
            .collect::<Vec<_>>()
            .join(", ");
        out.push(FollowUp::ProgramDeferred { specs });
    } else if rr.status == "interrupted" {
        // a hard break (ADR-005 §3): the host says the one thing
        // the guest no longer can — the run is over
        out.push(FollowUp::Stopped);
    } else if rr.status != "ok" && !died_on_miss {
        // The typed error goes INTO the chat: the next turn
        // boots with this message in its window, so the model
        // can act on it (FuelExhausted's text says to redo the
        // work in smaller chunks — a bare "something broke"
        // teaches nothing).
        let detail = rr.error.as_deref().map(|raw| {
            serde_json::from_str::<Value>(raw)
                .ok()
                .and_then(|v| {
                    Some(format!(
                        "{}: {}",
                        v["type"].as_str()?,
                        v["message"].as_str()?
                    ))
                })
                .unwrap_or_else(|| raw.to_string())
        });
        let text = match detail {
            Some(d) => format!("Something broke mid-run: {d}"),
            None => "Something broke mid-run.".to_string(),
        };
        out.push(FollowUp::Error { text });
    }
    out
}

/// The `chat_send` body of one follow-up, exactly as desktop posts it.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed chat bookkeeping (N2b-2) is the no-default caller
pub(crate) fn follow_up_body(follow_up: &FollowUp, agent_name: &str, trace_ref: &str) -> Value {
    match follow_up {
        FollowUp::StillWaiting { refs, why } => json!({
        "text": format!("Still waiting for `{refs}` — see the credential prompt above.{why}"),
        "agent": {"name": agent_name, "done": true}}),
        FollowUp::ProgramDeferred { specs } => json!({
        "text": format!(
            "Still fetching my agent code: {specs} hasn't synced to this device yet. \
             I'll answer as soon as it arrives."
        ),
        "agent": {"name": agent_name, "done": true}}),
        FollowUp::Stopped => json!({
        "text": "Stopped.",
        "agent": {"name": agent_name, "done": true,
                  "outcome": "interrupted", "debugLink": trace_ref}}),
        // How the run ended rides the `agent` group (`outcome`,
        // the run id as `debugLink`), not the text (ADR-005 §3):
        // the client renders the mark, the text stays human
        FollowUp::Error { text } => json!({
        "text": text,
        "agent": {"name": agent_name, "done": true,
                  "outcome": "error", "debugLink": trace_ref}}),
    }
}

/// ADR-017 §2: one past the highest id ever written, tombstones
/// included (a deleted id never reuses; tombstones carry no `seq`,
/// so the probe sorts on the record id = the zero-padded seq).
/// `log` is the chat bundle's `bao/log/v1` child, `turns` its
/// `agent_turns` collection.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed chat bookkeeping (N2b-2) is the no-default caller
pub(crate) fn next_turn_seq(
    client: &Client,
    space: &str,
    log: &str,
    turns: &str,
) -> std::result::Result<i64, AnyError> {
    let rows = client.query(
        space,
        log,
        turns,
        &json!({"includeDeleted": true, "sort": ["-id"], "limit": 1}),
    )?;
    let seq = rows
        .first()
        .and_then(|r| r["id"].as_str())
        .and_then(|id| id.parse::<i64>().ok())
        .unwrap_or(0)
        + 1;
    Ok(seq)
}

/// The minimal `agent_turns` record for a hard-broken run (ADR-005 §3):
/// `{userText, replies: [], interrupted: true, traceRef}`, written under
/// the id `{seq:08}` — what the guest's `append_turn` would have logged.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed chat bookkeeping (N2b-2) is the no-default caller
pub(crate) fn interrupted_turn_record(
    seq: i64,
    agent_name: &str,
    user_text: &str,
    trace_ref: &str,
) -> Value {
    json!({
        "seq": seq, "fromAgent": agent_name,
        "userText": user_text, "replies": [], "interrupted": true,
        "traceRef": trace_ref, "searchText": user_text,
        "llm": {"stopReason": "break_hard"}})
}

/// The chat responder is released for real — owner cleared ON THE RECORD
/// (the one stand-down write, ADR-018 §3): a local-only evict would be
/// undone by the next reconcile, which still reads our peer id. `t` is the
/// registry entry the stand-down took out; the write is the whole record
/// with `owner` empty.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed stand-down (N2b-2) is the no-default caller
pub(crate) fn release_chat_watch(
    client: &Client,
    space: &str,
    anchor: &str,
    dataset: &str,
    t: &Trigger,
) -> std::result::Result<Value, AnyError> {
    let mut t = t.clone();
    t.owner.clear();
    client.upsert_record(space, anchor, dataset, &t.id, &trigger_to_record(&t))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::StubTransport;

    fn result(status: &str) -> RunResult {
        let mut rr = RunResult::failed(String::new());
        rr.status = status.into();
        rr.error = None;
        rr
    }

    fn client(replies: &[(u16, Value)]) -> (Client, crate::testutil::CallLog) {
        let stub = StubTransport::new();
        for (status, body) in replies {
            stub.push(*status, body.clone());
        }
        let log = stub.log();
        (Client::with_transport(Box::new(stub)), log)
    }

    /// N2b-2 C0: the desktop selection, case by case — at most one
    /// follow-up, `posted` and `deferrals` as `start_or_inject` passes them.
    #[test]
    fn follow_ups_select_what_desktop_posts() {
        let mut miss = result("error");
        miss.missing_secrets = vec!["llm.key.a".into(), "llm.key.b".into()];
        miss.error = Some(r#"{"type": "LlmError", "message": "401\nunauthorized"}"#.into());
        assert_eq!(
            follow_ups(&miss, 0, 0),
            [FollowUp::StillWaiting {
                refs: "llm.key.a`, `llm.key.b".into(),
                why: " Last attempt: LlmError: 401 unauthorized".into(),
            }]
        );
        assert!(
            follow_ups(&miss, 1, 0).is_empty(),
            "a new card is the reply"
        );
        miss.error = None;
        assert_eq!(
            follow_ups(&miss, 0, 0),
            [FollowUp::StillWaiting {
                refs: "llm.key.a`, `llm.key.b".into(),
                why: String::new(),
            }]
        );
        // an interrupted run is "Stopped.", even with a miss on record
        let mut stopped = result("interrupted");
        stopped.missing_secrets = vec!["llm.key.a".into()];
        assert_eq!(follow_ups(&stopped, 0, 0), [FollowUp::Stopped]);
        assert!(follow_ups(&result("ok"), 0, 0).is_empty());
        let mut failed = result("error");
        failed.error = Some(r#"{"type": "FuelExhausted", "message": "redo in chunks"}"#.into());
        assert_eq!(
            follow_ups(&failed, 0, 0),
            [FollowUp::Error {
                text: "Something broke mid-run: FuelExhausted: redo in chunks".into()
            }]
        );
        failed.error = Some("plain text".into());
        assert_eq!(
            follow_ups(&failed, 0, 0),
            [FollowUp::Error {
                text: "Something broke mid-run: plain text".into()
            }]
        );
        assert_eq!(
            follow_ups(&result("error"), 0, 0),
            [FollowUp::Error {
                text: "Something broke mid-run.".into()
            }]
        );
        // ADR-009 §8: a program gap defers once, only when nothing changed
        let mut gap = result("error");
        gap.missing_programs = vec![("ws".into(), "a@v1".into()), ("ws".into(), "b@v2".into())];
        assert_eq!(
            follow_ups(&gap, 0, 0),
            [FollowUp::ProgramDeferred {
                specs: "`a@v1`, `b@v2`".into()
            }]
        );
        assert!(matches!(
            follow_ups(&gap, 0, 1)[..],
            [FollowUp::Error { .. }]
        ));
        gap.mutated = true;
        assert!(matches!(
            follow_ups(&gap, 0, 0)[..],
            [FollowUp::Error { .. }]
        ));
        // a miss beats a gap: the card (or "Still waiting") is the reply
        gap.mutated = false;
        gap.missing_secrets = vec!["llm.key.a".into()];
        assert!(follow_ups(&gap, 1, 0).is_empty());
    }

    /// N2b-2 C0: every bubble's exact body, as desktop posted it.
    #[test]
    fn follow_up_bodies_are_the_desktop_bubbles() {
        let body = |f: FollowUp| follow_up_body(&f, "bao", "run_1");
        assert_eq!(
            body(FollowUp::StillWaiting {
                refs: "llm.key.a".into(),
                why: " Last attempt: E: m".into()
            }),
            json!({"text": "Still waiting for `llm.key.a` — see the credential prompt above. Last attempt: E: m",
                   "agent": {"name": "bao", "done": true}})
        );
        assert_eq!(
            body(FollowUp::ProgramDeferred {
                specs: "`a@v1`".into()
            }),
            json!({"text": "Still fetching my agent code: `a@v1` hasn't synced to this device yet. I'll answer as soon as it arrives.",
                   "agent": {"name": "bao", "done": true}})
        );
        assert_eq!(
            body(FollowUp::Stopped),
            json!({"text": "Stopped.",
                   "agent": {"name": "bao", "done": true, "outcome": "interrupted", "debugLink": "run_1"}})
        );
        assert_eq!(
            body(FollowUp::Error {
                text: "Something broke mid-run.".into()
            }),
            json!({"text": "Something broke mid-run.",
                   "agent": {"name": "bao", "done": true, "outcome": "error", "debugLink": "run_1"}})
        );
        assert_eq!(short_error(&"x".repeat(200)).chars().count(), 161);
    }

    /// N2b-2 C0: the seq probe (tombstones included, highest id first) and
    /// the interrupted record, exactly as desktop wrote them.
    #[test]
    fn interrupted_turn_seq_probe_and_record() {
        let (c, log) = client(&[
            (200, json!({"records": [{"id": "00000041"}]})),
            (200, json!({"records": []})),
            (500, json!({"error": {"code": "down", "message": "x"}})),
        ]);
        assert_eq!(
            next_turn_seq(&c, "ws", "log-1", "t_agent_turns").unwrap(),
            42
        );
        assert_eq!(
            next_turn_seq(&c, "ws", "log-1", "t_agent_turns").unwrap(),
            1
        );
        assert!(next_turn_seq(&c, "ws", "log-1", "t_agent_turns").is_err());
        let calls = log.lock().unwrap();
        assert_eq!(calls[0].1, "/v1/spaces/ws/query");
        assert_eq!(
            calls[0].2,
            Some(json!({"objectId": "log-1", "dataset": "t_agent_turns",
                        "includeDeleted": true, "sort": ["-id"], "limit": 1}))
        );
        assert_eq!(
            interrupted_turn_record(42, "bao", "hi", "run_1"),
            json!({"seq": 42, "fromAgent": "bao", "userText": "hi", "replies": [],
                   "interrupted": true, "traceRef": "run_1", "searchText": "hi",
                   "llm": {"stopReason": "break_hard"}})
        );
    }

    /// N2b-2 C0: the one stand-down write — the whole record, owner empty,
    /// the caller's entry untouched.
    #[test]
    fn release_chat_watch_writes_the_record_with_its_owner_cleared() {
        let (c, log) = client(&[]);
        let t = chat_watch_trigger("chat-watch-g2", "chat-1", "peer");
        release_chat_watch(&c, "ws", "anchor", "t_agent_triggers", &t).unwrap();
        assert_eq!(t.owner, "peer");
        let mut cleared = t.clone();
        cleared.owner.clear();
        let calls = log.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].1, "/v1/spaces/ws/modify");
        assert_eq!(
            calls[0].2,
            Some(json!({"objectId": "anchor", "dataset": "t_agent_triggers",
                        "records": [{"id": "chat-watch-g2", "upsert": true,
                          "ops": [{"type": "$set", "path": "", "value": trigger_to_record(&cleared)}]}]}))
        );
    }

    struct Seeded;
    impl crate::broker::ConfigStore for Seeded {
        fn read(&self, key: &str) -> std::result::Result<Option<Value>, String> {
            Ok(crate::config::config_defaults().get(key).cloned())
        }
        fn set(&self, _: &str, _: &Value) -> std::result::Result<(), String> {
            Err("read-only".into())
        }
    }

    /// N2b-2 C0: the moved credential helper — a new card is posted and
    /// stamped `requestedIn/At`; a row already requested in this chat, or
    /// set, posts nothing; a failed post is not stamped and not counted; the
    /// config store drives the onboarding marker.
    #[test]
    fn credential_cards_new_existing_set_and_failed() {
        let rows = json!({"records": [
            {"id": "a", "key": "conn.key.a", "status": "missing", "label": "Key A",
             "hosts": ["api.a.test"]},
            {"id": "b", "key": "conn.key.b", "status": "missing", "requestedIn": "chat-1"},
            {"id": "c", "key": "conn.key.c", "status": "set"},
        ]});
        let (c, log) = client(&[
            (200, rows.clone()),
            (200, json!({})),
            (200, json!({})),
            (200, rows.clone()),
            (200, rows.clone()),
            (200, rows.clone()),
            (500, json!({"error": {"code": "down", "message": "x"}})),
        ]);
        let refs: Vec<String> = ["conn.key.a", "conn.key.b", "conn.key.c", "conn.key.d"]
            .map(String::from)
            .to_vec();
        let warnings = std::cell::RefCell::new(Vec::new());
        let posted = post_credential_requests(
            &c,
            "ws",
            "chat-1",
            "sec",
            "t_agent_secrets",
            None,
            "bao",
            &refs,
            &|w| warnings.borrow_mut().push(w),
        );
        assert_eq!(posted, 1);
        // the failed post's warning reaches the caller, desktop's text exactly
        assert_eq!(
            warnings.into_inner(),
            ["secrets: could not post request for conn.key.d (500 down: x)"]
        );
        let calls = log.lock().unwrap();
        let paths: Vec<&str> = calls.iter().map(|c| c.1.as_str()).collect();
        assert_eq!(
            paths,
            [
                "/v1/spaces/ws/query",
                "/v1/spaces/ws/objects/chat-1/chat/messages",
                "/v1/spaces/ws/modify",
                "/v1/spaces/ws/query",
                "/v1/spaces/ws/query",
                "/v1/spaces/ws/query",
                "/v1/spaces/ws/objects/chat-1/chat/messages",
            ]
        );
        assert_eq!(
            calls[1].2,
            Some(
                json!({"text": "I need a credential to continue: Key A (`conn.key.a`, used for api.a.test).",
                        "agent": {"name": "bao", "done": true},
                        "attachments": {"credreq": {"type": "credential_request",
                                                    "link": "any://o/sec?key=conn.key.a"}}})
            )
        );
        let stamp = calls[2].2.as_ref().unwrap();
        assert_eq!(stamp["objectId"], "sec");
        assert_eq!(stamp["dataset"], "t_agent_secrets");
        let ops = &stamp["records"][0]["ops"];
        assert_eq!(stamp["records"][0]["id"], "conn.key.a");
        assert_eq!(
            ops[2],
            json!({"type": "$set", "path": "requestedAt",
                                  "value": ops[2]["value"].clone()})
        );
        assert!(ops[2]["value"]["$date"].is_string());
        assert_eq!(
            ops[3],
            json!({"type": "$set", "path": "requestedIn", "value": "chat-1"})
        );
        assert_eq!(
            calls[6].2.as_ref().unwrap()["text"],
            "I need a credential to continue: conn.key.d (`conn.key.d`)."
        );
        drop(calls);
        // a never-chosen space asking for its seeded model key: the chooser
        let model_ref = crate::config::config_defaults()["llm.tier.codegen"]["api_key_ref"]
            .as_str()
            .unwrap()
            .to_string();
        let (c, log) = client(&[(200, json!({"records": []}))]);
        assert_eq!(
            post_credential_requests(
                &c,
                "ws",
                "chat-1",
                "sec",
                "t_agent_secrets",
                Some(&Seeded),
                "bao",
                std::slice::from_ref(&model_ref),
                &|w| panic!("no warning expected: {w}"),
            ),
            1
        );
        let calls = log.lock().unwrap();
        let card = calls[1].2.as_ref().unwrap();
        assert!(card["text"]
            .as_str()
            .unwrap()
            .starts_with("Before I can think, I need a language model."));
        assert_eq!(
            card["attachments"]["credreq"]["link"],
            format!("any://o/sec?key={model_ref}&setup=model")
        );
    }
}
