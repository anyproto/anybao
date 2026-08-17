//! Active-instance election (ADR-015) — the `/v1/devices` registry
//! consumer. The winner rule lives SERVER-SIDE (the computed `active`
//! map on GET); this module only registers presence, claims when the
//! registry shows no other live instance, and reports the verdict —
//! it never reimplements the (seq, at, peerId) tiebreak.

use crate::anyapi::Client;
use serde_json::{json, Value};
use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use tracing::{info, warn};

/// anybao's slug in the registry's `apps` map (ADR-015 §1) — constant:
/// one account runs one bao; the agent *name* is presentation.
pub const APP_SLUG: &str = "bao";

/// Reconcile cadence (ADR-015 §4) — manual-switch latency ≤ one poll.
pub const POLL: std::time::Duration = std::time::Duration::from_secs(10);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decision {
    Active,
    Standby,
    Claim,
}

/// Boot-time election state (ADR-015): the gate flag (written only by
/// the election thread after boot) + this device's registry identity.
/// `enabled: false` = the server predates `/v1/devices` — gate
/// permanently true, no thread (§4 degrade).
pub struct Election {
    pub enabled: bool,
    pub active: Arc<AtomicBool>,
    pub self_peer: Option<String>,
}

impl Election {
    fn disabled() -> Self {
        Election {
            enabled: false,
            active: Arc::new(AtomicBool::new(true)),
            self_peer: None,
        }
    }
}

/// A device row's peer id — row id IS the peer id (SYN-165), but the
/// HTTP mapping may surface it as `peerId`.
fn peer_of(d: &Value) -> Option<&str> {
    d["id"].as_str().or_else(|| d["peerId"].as_str())
}

fn has_app(d: &Value, app: &str) -> bool {
    d["apps"].get(app).is_some_and(|v| !v.is_null())
}

/// The ADR-015 §2 rule over one `GET /v1/devices` reply. `winner` is
/// the server's verdict; rows are only consulted for existence — a
/// dangling winner (claim without a row, or a row without the app)
/// does not block a claim.
pub fn decide(reply: &Value, self_peer: &str, app: &str) -> Decision {
    let empty = Vec::new();
    let devices = reply["devices"].as_array().unwrap_or(&empty);
    let winner = reply["active"][app].as_str();
    if winner == Some(self_peer) {
        return Decision::Active;
    }
    let others_with_app = devices
        .iter()
        .any(|d| peer_of(d).is_some_and(|p| p != self_peer) && has_app(d, app));
    if !others_with_app {
        return Decision::Claim; // first/only bao — the majority case
    }
    match winner {
        Some(w)
            if devices
                .iter()
                .any(|d| peer_of(d) == Some(w) && has_app(d, app)) =>
        {
            Decision::Standby
        }
        // other bao rows exist but no live winner (active device's row
        // deleted): survivors claim; the server tiebreak converges them
        _ => Decision::Claim,
    }
}

/// This device's peer id: the GET reply's `self`, falling back to the
/// PUT /me reply's row id — the runtime has no other route to it.
pub fn self_peer_of(get_reply: &Value, put_reply: &Value) -> Option<String> {
    get_reply["self"]
        .as_str()
        .or_else(|| peer_of(put_reply))
        .map(str::to_string)
}

/// One reconcile: fetch → decide → claim-and-verify. `None` on read
/// failure — the caller keeps the last verdict (a transient error must
/// not flap the gate, ADR-015 §4).
pub fn reconcile(client: &Client, self_peer: &str, app: &str) -> Option<bool> {
    let reply = client.list_devices().ok()?;
    match decide(&reply, self_peer, app) {
        Decision::Active => Some(true),
        Decision::Standby => Some(false),
        Decision::Claim => {
            if let Err(e) = client.activate_device(app) {
                warn!("election: claim failed ({e}); keeping last state");
                return None;
            }
            // verify — a concurrent claim may out-seq ours; a still-
            // unsettled Claim verdict reads as active (optimistic, the
            // next poll reconciles)
            let reply = client.list_devices().ok()?;
            Some(decide(&reply, self_peer, app) != Decision::Standby)
        }
    }
}

/// Boot registration + initial verdict (ADR-015 §1). 404 = server
/// predates the devices API → election disabled, gate true (§4);
/// other errors degrade the same way for THIS RUN (never boot a
/// standby off a hiccup — availability over strictness, logged).
pub fn boot(client: &Client, version: &str) -> Election {
    let body = json!({"apps": {APP_SLUG: {"version": version}}});
    let put_reply = match client.upsert_self_device(&body) {
        Ok(r) => r,
        Err(e) if e.status == 404 => {
            info!("devices API unavailable (server predates SYN-165) — election disabled, this instance stays active");
            return Election::disabled();
        }
        Err(e) => {
            warn!("election: device registration failed ({e}) — election disabled this run");
            return Election::disabled();
        }
    };
    let get_reply = client.list_devices().unwrap_or_else(|_| json!({}));
    let Some(peer) = self_peer_of(&get_reply, &put_reply) else {
        warn!("election: no self peer id in /v1/devices replies — election disabled this run");
        return Election::disabled();
    };
    let active = reconcile(client, &peer, APP_SLUG).unwrap_or(true);
    info!(
        "election: peer {peer} — {}",
        if active { "ACTIVE" } else { "standby" }
    );
    Election {
        enabled: true,
        active: Arc::new(AtomicBool::new(active)),
        self_peer: Some(peer),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::StubTransport;
    use std::sync::atomic::Ordering;

    fn dev(peer: &str, apps: Value) -> Value {
        json!({"id": peer, "name": "host", "os": "linux", "apps": apps})
    }

    fn reply(devices: Value, active: Value) -> Value {
        json!({"devices": devices, "active": active})
    }

    // --- decide: the §2 matrix ---

    #[test]
    fn self_winner_is_active() {
        let r = reply(
            json!([dev("p1", json!({"bao": {}})), dev("p2", json!({"bao": {}}))]),
            json!({"bao": "p1"}),
        );
        assert_eq!(decide(&r, "p1", "bao"), Decision::Active);
    }

    #[test]
    fn live_foreign_winner_is_standby() {
        let r = reply(
            json!([dev("p1", json!({"bao": {}})), dev("p2", json!({"bao": {}}))]),
            json!({"bao": "p2"}),
        );
        assert_eq!(decide(&r, "p1", "bao"), Decision::Standby);
    }

    #[test]
    fn only_bao_claims_even_with_dangling_winner() {
        // the winner's row was pruned — "the device doesn't exist"
        let r = reply(
            json!([dev("p1", json!({"bao": {}}))]),
            json!({"bao": "gone"}),
        );
        assert_eq!(decide(&r, "p1", "bao"), Decision::Claim);
        // ... and a plain empty registry claims too
        assert_eq!(decide(&json!({}), "p1", "bao"), Decision::Claim);
    }

    #[test]
    fn winner_row_without_the_app_does_not_block_a_claim() {
        // p2 exists but uninstalled bao (row kept, apps.bao gone/null)
        let r = reply(
            json!([
                dev("p1", json!({"bao": {}})),
                dev("p2", json!({"bao": null}))
            ]),
            json!({"bao": "p2"}),
        );
        assert_eq!(decide(&r, "p1", "bao"), Decision::Claim);
    }

    #[test]
    fn dead_winner_with_live_others_claims() {
        // active device's row deleted; another bao row exists — the
        // first booted survivor claims (concurrent claims converge
        // server-side)
        let r = reply(
            json!([dev("p1", json!({"bao": {}})), dev("p2", json!({"bao": {}}))]),
            json!({}),
        );
        assert_eq!(decide(&r, "p1", "bao"), Decision::Claim);
    }

    #[test]
    fn foreign_devices_without_bao_dont_block() {
        let r = reply(
            json!([
                dev("p1", json!({"bao": {}})),
                dev("p2", json!({"other": {}}))
            ]),
            json!({}),
        );
        assert_eq!(decide(&r, "p1", "bao"), Decision::Claim);
    }

    #[test]
    fn peer_id_key_variant_accepted() {
        let r = json!({"devices": [{"peerId": "p2", "apps": {"bao": {}}}],
                       "active": {"bao": "p2"}});
        assert_eq!(decide(&r, "p1", "bao"), Decision::Standby);
    }

    // --- self peer discovery ---

    #[test]
    fn self_peer_prefers_get_reply_then_put_row() {
        assert_eq!(
            self_peer_of(&json!({"self": "pG"}), &json!({"id": "pP"})),
            Some("pG".into())
        );
        assert_eq!(
            self_peer_of(&json!({}), &json!({"id": "pP"})),
            Some("pP".into())
        );
        assert_eq!(
            self_peer_of(&json!({}), &json!({"peerId": "pP2"})),
            Some("pP2".into())
        );
        assert_eq!(self_peer_of(&json!({}), &json!({})), None);
    }

    // --- boot + reconcile over the stub transport ---

    fn scripted(replies: &[(u16, Value)]) -> (Client, crate::testutil::CallLog) {
        let stub = StubTransport::new();
        for (s, v) in replies {
            stub.push(*s, v.clone());
        }
        let log = stub.log();
        (Client::with_transport(Box::new(stub)), log)
    }

    #[test]
    fn boot_404_disables_election_and_stays_active() {
        let (c, log) = scripted(&[(
            404,
            json!({"error": {"code": "not_found", "message": "no route"}}),
        )]);
        let e = boot(&c, "0.1.0");
        assert!(!e.enabled);
        assert!(e.active.load(Ordering::Relaxed));
        assert_eq!(e.self_peer, None);
        assert_eq!(log.lock().unwrap().len(), 1); // PUT only, no GET
    }

    #[test]
    fn boot_standby_when_live_foreign_winner() {
        let registry = reply(
            json!([
                dev("me", json!({"bao": {}})),
                dev("mac", json!({"bao": {}}))
            ]),
            json!({"bao": "mac"}),
        );
        let (c, log) = scripted(&[
            (200, json!({"id": "me"})), // PUT /me
            (200, registry.clone()),    // GET (self peer discovery)
            (200, registry),            // GET (reconcile)
        ]);
        let e = boot(&c, "0.1.0");
        assert!(e.enabled);
        assert!(!e.active.load(Ordering::Relaxed));
        assert_eq!(e.self_peer.as_deref(), Some("me"));
        let calls = log.lock().unwrap();
        let paths: Vec<&str> = calls.iter().map(|(_, p, _)| p.as_str()).collect();
        assert_eq!(paths, ["/v1/devices/me", "/v1/devices", "/v1/devices"]);
        assert_eq!(
            calls[0].2,
            Some(json!({"apps": {"bao": {"version": "0.1.0"}}}))
        );
    }

    #[test]
    fn boot_first_bao_claims_and_verifies() {
        let empty = reply(json!([dev("me", json!({"bao": {}}))]), json!({}));
        let won = reply(json!([dev("me", json!({"bao": {}}))]), json!({"bao": "me"}));
        let (c, log) = scripted(&[
            (200, json!({"id": "me"})), // PUT /me
            (200, empty.clone()),       // GET (self peer)
            (200, empty),               // GET (reconcile → Claim)
            (200, json!({})),           // POST activate
            (200, won),                 // GET (verify)
        ]);
        let e = boot(&c, "0.1.0");
        assert!(e.enabled && e.active.load(Ordering::Relaxed));
        let calls = log.lock().unwrap();
        assert_eq!(calls[3].0, "POST");
        assert_eq!(calls[3].1, "/v1/devices/activate");
        assert_eq!(calls[3].2, Some(json!({"app": "bao"})));
    }

    #[test]
    fn reconcile_lost_tiebreak_stands_down() {
        // claim posted, but verify shows a live foreign winner
        let no_winner = reply(
            json!([
                dev("me", json!({"bao": {}})),
                dev("mac", json!({"bao": {}}))
            ]),
            json!({}),
        );
        let lost = reply(
            json!([
                dev("me", json!({"bao": {}})),
                dev("mac", json!({"bao": {}}))
            ]),
            json!({"bao": "mac"}),
        );
        let (c, _) = scripted(&[(200, no_winner), (200, json!({})), (200, lost)]);
        assert_eq!(reconcile(&c, "me", "bao"), Some(false));
    }

    #[test]
    fn reconcile_read_failure_keeps_last_state() {
        let (c, _) = scripted(&[(500, json!({}))]);
        assert_eq!(reconcile(&c, "me", "bao"), None);
    }
}
