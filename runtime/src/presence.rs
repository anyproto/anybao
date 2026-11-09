//! Presence beats (ADR-025 §1/§2), shared by the desktop serve and the
//! managed host: the beat envelope, the cadence/change rule and the live-run
//! stamp reader, moved verbatim out of `serve.rs` (N2b-1, the N1 method) so
//! both hosts publish one `bao.status` shape. What stays in `serve` is the
//! desktop presence thread, its role reader and the chat-ownership log line
//! (they depend on the desktop `Shared`/`RunCtx`). The managed responder
//! beat (ADR-034 §7) adds `sessionId` and the activation pair to this same
//! envelope's `data` and publishes it only while admission is open (N2b-2).
use crate::anyapi::{AnyError, Client};
use crate::broker::PresenceState;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use tracing::{debug, warn};

// --- bao presence beats (ADR-025) ------------------------------------------------
//
// Layer 1 only — deterministic, never model-generated (§1). Serve is
// the SOLE publisher of `bao.status`: a beat every `STATUS_BEAT_S`
// (and immediately when the guest sets a status line — the generation
// poll below), a full-state idempotent envelope on the at-most-once
// bus. The UI marks offline after 3 missed beats (30s, its clock); a
// graceful shutdown publishes one `shutdown` beat for instant
// offline, a crash is covered by the beat TTL.

/// Beat cadence; the UI pairs it with a 3-missed-beats offline cutoff.
pub const STATUS_BEAT_S: f64 = 10.0;

/// The freshest in-flight run's stamp — presence derives `working`
/// from `RunCtx::live_runs`, the SAME registry `/break` resolves
/// against (one source of truth for what's running, never a parallel
/// counter). Freshest by `startedAt`; stampless entries (only the
/// watcher's chat-keyed overlay has those, and it never lands here)
/// are skipped defensively.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed beat (N2b-2) is the no-default caller
pub(crate) fn current_run(runs: &BTreeMap<String, crate::triggers::LiveRun>) -> Option<Value> {
    runs.values()
        .filter(|l| l.stamp.is_object())
        .max_by(|a, b| {
            let at = |l: &&crate::triggers::LiveRun| l.stamp["startedAt"].as_f64().unwrap_or(0.0);
            at(a).total_cmp(&at(b))
        })
        .map(|l| {
            // live activity rides the stamp (ADR-025 §1): tool calls so
            // far + the newest cell's preview — what bao is doing NOW
            let mut run = l.stamp.clone();
            run["cells"] = json!(l.activity.cells());
            if let Some(preview) = l.activity.preview() {
                run["cell"] = json!(preview);
            }
            run
        })
}

/// What a beat says about this device (ADR-025 §1 `role`/`winner`).
/// `responder` becomes `role`: does this device OWN the enabled
/// chat-watch record — the check the chat watch itself connects on
/// (ADR-018 §3) — so `role: active` means "chat is answered here",
/// not "won the election": a paused or repinned responder leaves the
/// election winner beating while nothing answers, which is the
/// BOB-111 symptom. `winner` is the election's claim holder (ADR-015
/// §2), for naming the device that holds bao when this one does not.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed beat (N2b-2) is the no-default caller
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct BeatRole {
    pub(crate) responder: bool,
    /// None when no claim exists, or the election is disabled/pruned
    pub(crate) winner: Option<String>,
}

/// The full-state beat envelope (ADR-025 §1). `state` ∈ boot | idle |
/// working | shutdown; `role` ∈ active | standby with `winner` beside
/// it when the registry names one; `run` rides only while working
/// (the caller passes `current_run`), the line only while fresh
/// (decay: `PresenceState::line`). Timestamps are unix seconds
/// (staleness math is the consumer's job, same as `AgentTypingRow`'s
/// `sinceSec`).
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed beat (N2b-2) is the no-default caller
pub(crate) fn status_envelope(
    identity: &str,
    status: &PresenceState,
    run: Option<Value>,
    state: &str,
    role: &BeatRole,
    now: f64,
) -> Value {
    let mut data = json!({
        "identity": identity,
        "state": state,
        "role": if role.responder { "active" } else { "standby" },
    });
    if let Some(w) = &role.winner {
        data["winner"] = json!(w);
    }
    if let Some(run) = run {
        data["run"] = run;
    }
    if let Some((line, at)) = status.line(now) {
        data["line"] = json!(line);
        data["lineAt"] = json!(at);
    }
    json!({
        "type": "bao.status",
        "scope": "account",
        // the serving peer id (ADR-015 identity) — filterable + the
        // multi-device dedup key (ADR-025 §6)
        "target": identity,
        "data": data,
    })
}

/// Publish one beat at `now` (threaded for the fake-clock sequence
/// tests). Best-effort like every bus write: a failed beat is logged
/// noise, never a serve failure (the next one covers it).
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed beat (N2b-2) is the no-default caller
pub(crate) fn publish_status_beat(
    client: &Client,
    identity: &str,
    status: &PresenceState,
    run: Option<Value>,
    state: &str,
    role: &BeatRole,
    now: f64,
) {
    if let Err(e) = client.publish_event(&status_envelope(identity, status, run, state, role, now))
    {
        if is_signed_out(&e) {
            debug!("bao.status beat not published: {e}");
        } else {
            warn!("bao.status beat not published: {e}");
        }
    }
}

/// `401 auth.required`: the server has no account — the embedder signed
/// out. On a desktop sign-out the host sends `DELETE /v1/auth` while
/// this serve is still stopping (stop joins up to a heartbeat), so the
/// last `shutdown` beat lands on a signed-out server. Expected, and
/// nobody is left to read the beat (BOB-153).
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed beat (N2b-2) is the no-default caller
pub(crate) fn is_signed_out(e: &AnyError) -> bool {
    e.status == 401 && e.code == "auth.required"
}

/// The presence loop's carried state — split out so a pass is
/// unit-testable without real sleeps.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed beat (N2b-2) is the no-default caller
#[derive(Default)]
pub(crate) struct PresenceLoop {
    pub(crate) last_beat: Option<f64>,
    pub(crate) last_sig: String,
    pub(crate) booted: bool,
}

/// What makes a beat DUE besides cadence: any change in what the beat
/// would say — the line generation, which run is live, how far it
/// has come, and the role/winner. Run start/end, every new tool
/// call, a responder flip (takeover, stand-down, pause, repin) and a
/// winner change republish within a poll (~1s),
/// which is what keeps the UI's working/idle flip, the call counter
/// and the role live instead of up to a beat behind (ADR-025 §2).
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed beat (N2b-2) is the no-default caller
pub(crate) fn presence_sig(status: &PresenceState, run: Option<&Value>, role: &BeatRole) -> String {
    format!(
        "{}|{}|{}|{}|{}",
        status.line_gen(),
        run.and_then(|r| r["id"].as_str()).unwrap_or(""),
        run.and_then(|r| r["cells"].as_u64()).unwrap_or(0),
        role.responder,
        role.winner.as_deref().unwrap_or(""),
    )
}

/// One poll pass: publish a beat if due (boot, cadence, or a change —
/// line set, run start/end, tool call, role flip). Returns true when a
/// beat went out.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed beat (N2b-2) is the no-default caller
pub(crate) fn presence_pass(
    client: &Client,
    identity: &str,
    status: &PresenceState,
    run: Option<Value>,
    role: &BeatRole,
    st: &mut PresenceLoop,
    now: f64,
) -> bool {
    let Some(state) = presence_decide(status, run.as_ref(), role, st, now) else {
        return false;
    };
    publish_status_beat(client, identity, status, run, state, role, now);
    true
}

/// The decision half of `presence_pass` (N2b-2 C0): whether a beat is due
/// at `now` and the `state` it says, with the loop's carried state advanced
/// as a published beat advances it. The managed responder beat builds the
/// same envelope from this, adds its `data.sessionId`/`data.activation`
/// (ADR-034 §7) and publishes it itself; a beat is best-effort either way,
/// so the state advances whether or not the publish lands.
#[cfg_attr(not(feature = "desktop"), allow(dead_code))] // the managed beat (N2b-2) is the no-default caller
pub(crate) fn presence_decide(
    status: &PresenceState,
    run: Option<&Value>,
    role: &BeatRole,
    st: &mut PresenceLoop,
    now: f64,
) -> Option<&'static str> {
    let sig = presence_sig(status, run, role);
    let due = st.last_beat.is_none()
        || now - st.last_beat.unwrap() >= STATUS_BEAT_S
        || sig != st.last_sig;
    if !due {
        return None;
    }
    // boot once, then working/idle off the live-run registry (ADR-025 §1)
    let state = if !st.booted {
        "boot"
    } else if run.is_some() {
        "working"
    } else {
        "idle"
    };
    st.last_beat = Some(now);
    st.last_sig = sig;
    st.booted = true;
    Some(state)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// N2b-2 C0: `presence_decide` is `presence_pass`'s decision — the same
    /// state sequence and the same carried state, publish or not.
    #[test]
    fn presence_decide_matches_the_pass() {
        let status = PresenceState::default();
        let role = BeatRole {
            responder: true,
            winner: Some("peer".into()),
        };
        let run = json!({"id": "run_1", "cells": 2});
        let mut st = PresenceLoop::default();
        let mut states = Vec::new();
        for (t, run) in [
            (0.0, None),
            (1.0, None),
            (2.0, Some(&run)),
            (3.0, Some(&run)),
            (13.0, Some(&run)),
            (14.0, None),
        ] {
            states.push(presence_decide(&status, run, &role, &mut st, t));
        }
        assert_eq!(
            states,
            [
                Some("boot"),
                None,
                Some("working"),
                None,
                Some("working"),
                Some("idle")
            ]
        );
        assert_eq!(st.last_beat, Some(14.0));
        assert!(st.booted);
        let flipped = BeatRole {
            responder: false,
            ..role.clone()
        };
        assert_eq!(
            presence_decide(&status, None, &flipped, &mut st, 14.5),
            Some("idle"),
            "a role flip is due at once"
        );
    }
}
