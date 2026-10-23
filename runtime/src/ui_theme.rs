//! ADR-008 §8: one bounded, recorded exchange with the frontend theme receiver.

use crate::anyapi::Frame;
use crate::broker::EffectFailure;
use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::io::{self, BufRead, BufReader, Read};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const FRAME_BYTES: usize = 64 * 1024 + 4096;
const STREAM_BYTES: usize = 1024 * 1024;
const MAX_WINDOWS: usize = 32;

#[cfg(test)]
#[path = "ui_theme_tests.rs"]
mod tests;
const ACTIONS: &[&str] = &[
    "capabilities",
    "catalogue",
    "list",
    "get",
    "select",
    "install",
    "preview",
    "commit",
    "cancel",
    "options.set",
    "options.reset",
    "appearance.set",
    "export",
];

pub(crate) fn is_read(action: Option<&str>) -> bool {
    matches!(action, Some("capabilities" | "catalogue" | "list" | "get"))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Command {
    action: String,
    #[serde(default)]
    params: Map<String, Value>,
    #[serde(default, rename = "windowId")]
    window_id: Option<String>,
    #[serde(default = "default_timeout")]
    timeout: f64,
}

fn default_timeout() -> f64 {
    5.0
}

fn invalid(message: impl ToString) -> EffectFailure {
    EffectFailure {
        type_: "ValueError".into(),
        message: message.to_string(),
    }
}

fn failure(id: &str, code: &str, message: impl ToString) -> Value {
    json!({"ok": false, "requestId": id,
           "error": {"code": code, "message": message.to_string()}})
}

pub(crate) fn request(
    base: &str,
    payload: &Value,
    run_deadline: Option<Instant>,
    interrupt: &AtomicBool,
) -> Result<Value, EffectFailure> {
    let command: Command = serde_json::from_value(payload.clone()).map_err(invalid)?;
    if !ACTIONS.contains(&command.action.as_str()) {
        return Err(invalid(
            "Unknown theme action; discover the supported actions first.",
        ));
    }
    if !command.timeout.is_finite() || command.timeout <= 0.0 || command.timeout > 30.0 {
        return Err(invalid("timeout must be seconds in (0, 30]."));
    }
    match &command.window_id {
        Some(id) if uuid::Uuid::parse_str(id).is_err() => {
            return Err(invalid("windowId must be a UUID from discovery."))
        }
        None if command.action != "capabilities" => {
            return Err(invalid(
                "Discover and choose a windowId before sending a theme action.",
            ))
        }
        _ => {}
    }
    if payload.to_string().len() > 60_000 {
        return Err(invalid(
            "Theme commands carry file references, not package bytes.",
        ));
    }
    let deadline = Instant::now() + Duration::from_secs_f64(command.timeout);
    let deadline = run_deadline.map_or(deadline, |run| run.min(deadline));
    let id = uuid::Uuid::new_v4().to_string();
    let discovery = command.action == "capabilities" && command.window_id.is_none();
    let mut published = false;
    Ok(exchange(base, &command, &id, deadline, interrupt, &mut published)
        .unwrap_or_else(|error| failure(&id,
            if published && !discovery { "outcome_unknown" } else { "unavailable" },
            format!("{error} Read state/discover before retrying; writes are never retried automatically."))))
}

fn remaining(deadline: Instant, interrupt: &AtomicBool) -> io::Result<Duration> {
    if interrupt.load(Ordering::Relaxed) {
        return Err(io::Error::new(
            io::ErrorKind::Interrupted,
            "Theme request interrupted.",
        ));
    }
    deadline
        .checked_duration_since(Instant::now())
        .filter(|d| !d.is_zero())
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::TimedOut,
                "No correlated UI result before the deadline.",
            )
        })
}

fn exchange(
    base: &str,
    command: &Command,
    id: &str,
    deadline: Instant,
    interrupt: &AtomicBool,
    published: &mut bool,
) -> Result<Value, String> {
    let discovery = command.action == "capabilities" && command.window_id.is_none();
    let base = base.trim_end_matches('/');
    let agent = ureq::AgentBuilder::new().redirects(0).build();
    let response = agent
        .get(&format!(
            "{base}/v1/events/subscribe?scope=device&type=ui.theme.result&target={id}"
        ))
        .timeout(remaining(deadline, interrupt).map_err(|e| e.to_string())?)
        .set("Accept", "text/event-stream")
        .call()
        .map_err(|e| e.to_string())?;
    if response.status() != 200 {
        return Err(format!(
            "Result subscription returned HTTP {}.",
            response.status()
        ));
    }
    let mut reader = BufReader::new(response.into_reader());
    let mut windows = BTreeMap::new();
    let mut budget = STREAM_BYTES;
    loop {
        let frame = match read_frame(&mut reader, deadline, interrupt, &mut budget) {
            Ok(Some(frame)) => frame,
            Err(error)
                if discovery && error.kind() == io::ErrorKind::TimedOut && !windows.is_empty() =>
            {
                return Ok(json!({"windows": windows.into_values().collect::<Vec<Value>>()}));
            }
            Ok(None) => return Err("The UI result stream closed.".into()),
            Err(error) => return Err(error.to_string()),
        };
        if frame.event == "closed" {
            return Err("The UI result stream closed.".into());
        }
        if frame.event == "ready" && !*published {
            let timeout = remaining(deadline, interrupt).map_err(|e| e.to_string())?;
            let epoch_ms = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_err(|e| e.to_string())?
                .as_millis();
            let mut data = json!({"version": 1, "requestId": id,
                "expiresAt": (epoch_ms + timeout.as_millis()) as u64,
                "action": command.action, "params": command.params});
            if let Some(window) = &command.window_id {
                data["windowId"] = json!(window);
            }
            // A lost POST reply may follow an applied command: never republish.
            *published = true;
            let reply = agent.post(&format!("{base}/v1/events"))
                .timeout(remaining(deadline, interrupt).map_err(|e| e.to_string())?)
                .send_json(json!({"type": "ui.theme.request", "scope": "device", "target": id, "data": data}))
                .map_err(|e| e.to_string())?;
            let reply = read_json(reply)?;
            if reply["subscribers"].as_u64() == Some(0) {
                return Ok(failure(
                    id,
                    "unavailable",
                    "No UI request consumer is connected.",
                ));
            }
        }
        if *published && frame.event == "event" {
            if let Some(data) = correlated(&frame.data, id, command.window_id.as_deref()) {
                if !discovery {
                    return Ok(data.clone());
                }
                windows.insert(data["windowId"].as_str().unwrap().to_string(), data.clone());
                if windows.len() > MAX_WINDOWS {
                    return Err(
                        "Too many UI windows replied; close extra windows and discover again."
                            .into(),
                    );
                }
            }
        }
    }
}

fn read_json(response: ureq::Response) -> Result<Value, String> {
    let mut bytes = Vec::new();
    response
        .into_reader()
        .take(FRAME_BYTES as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() > FRAME_BYTES {
        return Err("Publish response exceeded its byte limit.".into());
    }
    serde_json::from_slice(&bytes).map_err(|e| e.to_string())
}

fn correlated<'a>(envelope: &'a Value, id: &str, window: Option<&str>) -> Option<&'a Value> {
    let data = &envelope["data"];
    let window_id = data["windowId"].as_str()?;
    (envelope["type"] == "ui.theme.result"
        && envelope["scope"] == "device"
        && envelope["target"] == id
        && envelope["sender"]["self"] == true
        && data["version"] == 1
        && data["requestId"] == id
        && uuid::Uuid::parse_str(window_id).is_ok()
        && window.is_none_or(|w| w == window_id)
        && data["ok"].is_boolean()
        && if data["ok"] == true {
            data.get("result").is_some()
        } else {
            data["error"].is_object()
        })
    .then_some(data)
}

fn read_frame(
    reader: &mut impl BufRead,
    deadline: Instant,
    interrupt: &AtomicBool,
    budget: &mut usize,
) -> io::Result<Option<Frame>> {
    let mut event = "message".to_string();
    let mut data = Vec::new();
    let mut size = 0;
    loop {
        remaining(deadline, interrupt)?;
        let mut line = Vec::new();
        let n = Read::by_ref(reader)
            .take((FRAME_BYTES - size + 1) as u64)
            .read_until(b'\n', &mut line)?;
        if n == 0 {
            return Ok(None);
        }
        size += n;
        if size > FRAME_BYTES || n > *budget {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "UI result stream exceeded its byte limit.",
            ));
        }
        *budget -= n;
        let line = std::str::from_utf8(&line)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?
            .trim_end_matches(['\r', '\n']);
        if line.is_empty() {
            if !data.is_empty() || event != "message" {
                remaining(deadline, interrupt)?;
                return Ok(Some(Frame {
                    event,
                    data: serde_json::from_str(&data.join("\n")).unwrap_or(Value::Null),
                }));
            }
            size = 0;
        } else if let Some(value) = line.strip_prefix("event:") {
            event = value.trim_start().to_string();
        } else if let Some(value) = line.strip_prefix("data:") {
            data.push(value.strip_prefix(' ').unwrap_or(value).to_string());
        }
    }
}
