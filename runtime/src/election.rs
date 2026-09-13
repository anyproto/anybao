//! Active-instance election (ADR-015) — the `/v1/devices` registry
//! consumer. The winner rule lives SERVER-SIDE (the computed `active`
//! map on GET); this module only registers presence, claims when the
//! registry shows no other live instance, and reports the verdict —
//! it never reimplements the (seq, at, peerId) tiebreak.

use crate::anyapi::Client;
use serde_json::{json, Value};
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

/// One reconcile's outcome (ADR-015 §2): the gate value plus the
/// registry's claim holder — the peer every presence beat names
/// (ADR-025 §1 `winner`) and the standby log line points at (§5).
/// Serve keeps exactly ONE of these (`RunCtx::verdict`), written as a
/// whole after a takeover finishes re-arming, so a reader never sees
/// the gate of one reconcile beside the winner of another.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Verdict {
    pub active: bool,
    /// `active["bao"]` of the reply that produced the verdict; None
    /// when the registry names no claim holder
    pub winner: Option<String>,
}

impl Verdict {
    /// The §4 degrade: no registry ⇒ gate permanently true, no winner.
    pub fn disabled() -> Self {
        Verdict {
            active: true,
            winner: None,
        }
    }

    /// Permanent standby (pruned, §4): gate false, no winner known.
    pub fn standby() -> Self {
        Verdict {
            active: false,
            winner: None,
        }
    }

    fn of(active: bool, reply: &Value, app: &str) -> Self {
        Verdict {
            active,
            winner: reply["active"][app].as_str().map(str::to_string),
        }
    }

    /// The log-line form: `ACTIVE` / `standby (the active bao is peer …)`.
    pub fn describe(&self) -> String {
        if self.active {
            return "ACTIVE".into();
        }
        match &self.winner {
            Some(w) => format!("standby (the active bao is peer {w})"),
            None => "standby (no claim holder in the registry)".into(),
        }
    }
}

/// While this device does not answer chat, the presence thread says
/// why every this many seconds (§5): standby is a silent state — no
/// chat watch, no runs — so one boot line cannot explain a long
/// unanswered chat. Lives beside the beat, not in the election
/// thread: it must keep talking while registry reads fail and on a
/// pruned device, which runs no election thread at all.
pub const STANDBY_LOG_S: f64 = 60.0;

/// Boot-time election state (ADR-015): the boot verdict (serve keeps
/// it as `RunCtx::verdict`, rewritten only by the election thread) +
/// this device's registry identity. `enabled: false` = the server
/// predates `/v1/devices` — gate permanently true, no thread (§4
/// degrade).
pub struct Election {
    pub enabled: bool,
    pub verdict: Verdict,
    pub self_peer: Option<String>,
    /// Tombstoned device (§4): unlike plain standby — which still
    /// fires the triggers pinned to this device (ADR-006 §4) — a
    /// pruned device runs NOTHING.
    pub pruned: bool,
}

impl Election {
    fn disabled() -> Self {
        Election {
            enabled: false,
            verdict: Verdict::disabled(),
            self_peer: None,
            pruned: false,
        }
    }

    /// This device's row is tombstoned (§4): the verdict can never
    /// change for this peer id, so no thread — gate permanently false.
    fn pruned() -> Self {
        Election {
            enabled: false,
            verdict: Verdict::standby(),
            self_peer: None,
            pruned: true,
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

/// The SDK's pruned-write sentinel (409): this device's row was
/// tombstoned — every self-row write is absorbed, forever, until a
/// fresh `any init` derives new peer keys.
const PRUNED: &str = "device.pruned";

/// One reconcile: fetch → decide → claim-and-verify. `None` on read
/// failure — the caller keeps the last verdict (a transient error must
/// not flap the gate, ADR-015 §4).
pub fn reconcile(client: &Client, self_peer: &str, app: &str) -> Option<Verdict> {
    let reply = client.list_devices().ok()?;
    match decide(&reply, self_peer, app) {
        Decision::Active => Some(Verdict::of(true, &reply, app)),
        Decision::Standby => Some(Verdict::of(false, &reply, app)),
        Decision::Claim => {
            if let Err(e) = client.activate_device(app) {
                // pruned mid-run: the claim can never land — stand
                // down rather than keep an unclaimable "last state"
                if e.code == PRUNED {
                    warn!(
                        "election: this device was pruned from the registry — \
                         standing by (a fresh `any init` re-registers)"
                    );
                    return Some(Verdict::of(false, &reply, app));
                }
                warn!("election: claim failed ({e}); keeping last state");
                return None;
            }
            // verify — a concurrent claim may out-seq ours; a still-
            // unsettled Claim verdict reads as active (optimistic, the
            // next poll reconciles)
            let reply = client.list_devices().ok()?;
            let active = decide(&reply, self_peer, app) != Decision::Standby;
            Some(Verdict::of(active, &reply, app))
        }
    }
}

/// Boot registration + initial verdict (ADR-015 §1/§4).
///
/// - 404 = server predates the devices API → election disabled, gate
///   true — today's behavior, unchanged.
/// - 409 `device.pruned` = this device was pruned from the registry
///   (sticky tombstone) → PERMANENT standby, gate false: an
///   excommunicated device answering as the active bao is exactly the
///   split-brain the registry exists to prevent. Only a fresh
///   `any init` (new peer keys) re-registers.
/// - Other errors: brief retry (serve's boot order means the techspace
///   is already open — `list_spaces` ran — so a failure here is a
///   freak), then disabled-active for the run (availability over
///   strictness, logged).
pub fn boot(client: &Client, version: &str) -> Election {
    boot_with(client, version, std::time::Duration::from_secs(2))
}

const BOOT_ATTEMPTS: u32 = 3;

fn boot_with(client: &Client, version: &str, retry_delay: std::time::Duration) -> Election {
    let body = json!({"apps": {APP_SLUG: {"version": version}}});
    let mut last_err = String::new();
    for attempt in 0..BOOT_ATTEMPTS {
        if attempt > 0 {
            std::thread::sleep(retry_delay);
        }
        let put_reply = match client.upsert_self_device(&body) {
            Ok(r) => r,
            Err(e) if e.status == 404 => {
                info!("devices API unavailable (server predates SYN-165) — election disabled, this instance stays active");
                return Election::disabled();
            }
            Err(e) if e.code == PRUNED => {
                warn!(
                    "election: this device was PRUNED from the devices registry — \
                     permanent standby (the agent will not answer here); \
                     a fresh `any init` (new peer keys) re-registers it"
                );
                return Election::pruned();
            }
            Err(e) => {
                last_err = e.to_string();
                continue;
            }
        };
        let get_reply = client.list_devices().unwrap_or_else(|_| json!({}));
        let Some(peer) = self_peer_of(&get_reply, &put_reply) else {
            last_err = "no self peer id in /v1/devices replies".into();
            continue;
        };
        let verdict = reconcile(client, &peer, APP_SLUG).unwrap_or_else(Verdict::disabled);
        info!("election: peer {peer} — {}", verdict.describe());
        return Election {
            enabled: true,
            verdict,
            self_peer: Some(peer),
            pruned: false,
        };
    }
    warn!("election: device registration failed ({last_err}) — election disabled this run");
    Election::disabled()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::StubTransport;

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
        assert!(e.verdict.active);
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
        assert!(!e.verdict.active);
        assert_eq!(e.self_peer.as_deref(), Some("me"));
        assert_eq!(e.verdict.winner.as_deref(), Some("mac")); // the beat names it
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
        assert!(e.enabled && e.verdict.active);
        assert_eq!(e.verdict.winner.as_deref(), Some("me"));
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
        let v = reconcile(&c, "me", "bao").unwrap();
        assert!(!v.active);
        assert_eq!(v.winner.as_deref(), Some("mac")); // from the VERIFY read
    }

    #[test]
    fn verdict_describe_names_the_winner() {
        let won = reply(json!([dev("me", json!({"bao": {}}))]), json!({"bao": "me"}));
        assert_eq!(Verdict::of(true, &won, "bao").describe(), "ACTIVE");
        let lost = reply(json!([]), json!({"bao": "mac"}));
        assert_eq!(
            Verdict::of(false, &lost, "bao").describe(),
            "standby (the active bao is peer mac)"
        );
        assert_eq!(
            Verdict::of(false, &json!({}), "bao").describe(),
            "standby (no claim holder in the registry)"
        );
    }

    #[test]
    fn reconcile_read_failure_keeps_last_state() {
        let (c, _) = scripted(&[(500, json!({}))]);
        assert_eq!(reconcile(&c, "me", "bao"), None);
    }

    fn pruned_reply() -> (u16, Value) {
        (
            409,
            json!({"error": {"code": "device.pruned",
                             "message": "row tombstoned"}}),
        )
    }

    #[test]
    fn boot_pruned_is_permanent_standby() {
        // an excommunicated device must NOT degrade to always-active
        let (c, log) = scripted(&[pruned_reply()]);
        let e = boot(&c, "0.1.0");
        assert!(!e.enabled);
        assert!(!e.verdict.active); // gate FALSE, unlike 404
        assert_eq!(e.self_peer, None);
        assert_eq!(log.lock().unwrap().len(), 1); // no retries on pruned
    }

    #[test]
    fn boot_transient_error_retries_then_succeeds() {
        let won = reply(json!([dev("me", json!({"bao": {}}))]), json!({"bao": "me"}));
        let (c, log) = scripted(&[
            (500, json!({})),           // PUT attempt 1 — transient
            (200, json!({"id": "me"})), // PUT attempt 2
            (200, won.clone()),         // GET (self peer)
            (200, won),                 // GET (reconcile → Active)
        ]);
        let e = boot_with(&c, "0.1.0", std::time::Duration::ZERO);
        assert!(e.enabled && e.verdict.active);
        assert_eq!(e.self_peer.as_deref(), Some("me"));
        assert_eq!(log.lock().unwrap().len(), 4);
    }

    #[test]
    fn boot_persistent_error_disables_active() {
        // availability over strictness once the retries are spent
        let (c, log) = scripted(&[(500, json!({})), (500, json!({})), (500, json!({}))]);
        let e = boot_with(&c, "0.1.0", std::time::Duration::ZERO);
        assert!(!e.enabled);
        assert!(e.verdict.active);
        assert_eq!(log.lock().unwrap().len(), 3);
    }

    #[test]
    fn reconcile_pruned_claim_stands_down() {
        // pruned mid-run: the claim can never land — Some(false), not
        // the keep-last-state None
        let no_winner = reply(json!([dev("me", json!({"bao": {}}))]), json!({}));
        let (c, _) = scripted(&[(200, no_winner), pruned_reply()]);
        let v = reconcile(&c, "me", "bao").unwrap();
        assert!(!v.active);
        assert_eq!(v.winner, None);
    }
}
