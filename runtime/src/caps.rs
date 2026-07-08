//! caps — CapBAC grant policy (ADR-002 §2, 00-plan "Capabilities &
//! trust"), the Rust twin of anybao/caps.py.
//!
//! Manifest = request, grant = authority — they live in different
//! places. The broker holds the RULE: consult `grants.allowed(cap)`
//! before anything runs. This module is the POLICY it consults:
//!
//! - `GrantSet` — POLA-scoped cap set (exact + prefix-wildcard
//!   entries), Macaroons-style `attenuate` = intersection down the
//!   import chain (a child never exceeds its parent).
//! - `GrantLedger` — user-side, file-backed, keyed by PROGRAM CONTENT
//!   HASH. Editing a manifest changes the hash → the grant no longer
//!   matches → re-prompt; tampering yields a consent dialog, not
//!   authority.
//! - `decide` — trust-tier policy: self/attested auto-grant;
//!   unverified auto-regrants attenuation-only changes and prompts on
//!   expansion (the chat/UI-command channel is the injected prompt).
//! - `Attestation` / `trust_tier` — publisher signature over
//!   (sourceHash, manifestHash, spec); a fork breaks the attestation
//!   and drops to "unverified" for free. Verification is injected.
//!
//! Mounted as `crate::caps` (
//! broker) so the record-mode binary's module tree stays untouched.
#![allow(dead_code)] // consumed by the main.rs grants wiring (next round); unit tests below

use serde_json::{json, Map, Value};
use std::collections::BTreeSet;
use std::fmt;
use std::fs;
use std::path::PathBuf;

/// The grant policy said no — the program must not run with the
/// requested caps (or the policy inputs were unusable).
#[derive(Debug)]
pub enum Denied {
    /// The user (or policy) refused the grant.
    Refused {
        program_hash: String,
        caps: Vec<String>,
    },
    UnknownTier(String),
    /// Ledger file IO/parse failure.
    Ledger(String),
}

impl fmt::Display for Denied {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Denied::Refused { program_hash, caps } => {
                write!(f, "grant refused for {program_hash}: {caps:?}")
            }
            Denied::UnknownTier(t) => write!(f, "unknown trust tier: {t}"),
            Denied::Ledger(e) => write!(f, "grant ledger error: {e}"),
        }
    }
}

impl std::error::Error for Denied {}

// Prefix wildcard: "data.*" covers "data.read"; exact otherwise.
fn entry_covers(entry: &str, cap: &str) -> bool {
    match entry.strip_suffix(".*") {
        Some(stem) => cap.starts_with(&format!("{stem}.")),
        None => entry == cap,
    }
}

/// A set of grant entries: exact caps ("net.http") and prefix
/// wildcards ("data.*"). POLA: allowed() is cover-by-any-entry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GrantSet {
    entries: BTreeSet<String>,
}

impl GrantSet {
    pub fn of<I, S>(caps: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        GrantSet {
            entries: caps.into_iter().map(Into::into).collect(),
        }
    }

    pub fn allowed(&self, cap: &str) -> bool {
        self.entries.iter().any(|e| entry_covers(e, cap))
    }

    fn covers_entry(&self, entry: &str) -> bool {
        // A wildcard is only covered by an equal-or-broader wildcard
        // (it matches infinitely many caps — no exact entry can).
        if let Some(stem) = entry.strip_suffix(".*") {
            let want = format!("{stem}.");
            return self.entries.iter().any(|e| {
                e.strip_suffix(".*")
                    .map(|es| want.starts_with(&format!("{es}.")))
                    .unwrap_or(false)
            });
        }
        self.allowed(entry)
    }

    /// True iff every cap `other` allows, self allows too.
    pub fn covers(&self, other: &GrantSet) -> bool {
        other.entries.iter().all(|e| self.covers_entry(e))
    }

    /// Semantic intersection (Macaroons-style import-chain
    /// attenuation): the result allows a cap iff BOTH sides do. For
    /// this grammar that is exactly the entries of each side the other
    /// covers (of two entries matching the same cap, one prefixes the
    /// other — the narrower survives).
    pub fn attenuate(&self, other: &GrantSet) -> GrantSet {
        let mut kept: BTreeSet<String> = self
            .entries
            .iter()
            .filter(|e| other.covers_entry(e))
            .cloned()
            .collect();
        kept.extend(
            other
                .entries
                .iter()
                .filter(|e| self.covers_entry(e))
                .cloned(),
        );
        GrantSet { entries: kept }
    }
}

/// User-side revocable grant store (never in the shared program
/// object). JSON file at a caller-supplied path; keys are program
/// content hashes, values {caps: sorted, tier, grantedAt}.
pub struct GrantLedger {
    path: PathBuf,
}

impl GrantLedger {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        GrantLedger { path: path.into() }
    }

    fn load(&self) -> Result<Map<String, Value>, Denied> {
        if !self.path.exists() {
            return Ok(Map::new());
        }
        let text = fs::read_to_string(&self.path).map_err(|e| Denied::Ledger(e.to_string()))?;
        serde_json::from_str(&text).map_err(|e| Denied::Ledger(e.to_string()))
    }

    fn save(&self, data: &Map<String, Value>) -> Result<(), Denied> {
        let text = serde_json::to_string_pretty(data).map_err(|e| Denied::Ledger(e.to_string()))?;
        fs::write(&self.path, text + "\n").map_err(|e| Denied::Ledger(e.to_string()))
    }

    pub fn lookup(&self, program_hash: &str) -> Result<Option<Value>, Denied> {
        Ok(self.load()?.get(program_hash).cloned())
    }

    pub fn grant(
        &self,
        program_hash: &str,
        caps: &[String],
        tier: &str,
        ts: &str,
    ) -> Result<(), Denied> {
        let mut data = self.load()?;
        let mut sorted = caps.to_vec();
        sorted.sort();
        data.insert(
            program_hash.into(),
            json!({"caps": sorted, "tier": tier, "grantedAt": ts}),
        );
        self.save(&data)
    }

    pub fn revoke(&self, program_hash: &str) -> Result<(), Denied> {
        let mut data = self.load()?;
        if data.remove(program_hash).is_some() {
            self.save(&data)?;
        }
        Ok(())
    }
}

/// The grant policy. Tier "self" and "attested" auto-grant the
/// requested (= declared) caps — nothing persisted, re-derived per
/// run. Tier "unverified": an existing grant covering the request
/// (attenuation-only change) regrants silently; expansion or first run
/// goes through `prompt` (the chat/UI-command hook) and is `Denied` on
/// refusal. `ts` stamps ledger writes.
pub fn decide<I, S>(
    ledger: &GrantLedger,
    program_hash: &str,
    requested_caps: I,
    tier: &str,
    mut prompt: impl FnMut(&[String]) -> bool,
    ts: &str,
) -> Result<GrantSet, Denied>
where
    I: IntoIterator<Item = S>,
    S: Into<String>,
{
    let mut requested: Vec<String> = requested_caps.into_iter().map(Into::into).collect();
    requested.sort();
    requested.dedup();
    let wanted = GrantSet::of(requested.iter().cloned());
    if tier == "self" || tier == "attested" {
        return Ok(wanted);
    }
    if tier != "unverified" {
        return Err(Denied::UnknownTier(tier.into()));
    }

    if let Some(existing) = ledger.lookup(program_hash)? {
        let existing_caps: Vec<String> = existing["caps"]
            .as_array()
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default();
        if GrantSet::of(existing_caps.iter().cloned()).covers(&wanted) {
            if existing_caps != requested {
                // narrower — record the attenuation
                ledger.grant(program_hash, &requested, tier, ts)?;
            }
            return Ok(wanted);
        }
    }
    if !prompt(&requested) {
        return Err(Denied::Refused {
            program_hash: program_hash.into(),
            caps: requested,
        });
    }
    ledger.grant(program_hash, &requested, tier, ts)?;
    Ok(wanted)
}

/// Publisher identity's signature over (sourceHash, manifestHash,
/// spec) — the piece of publisher authenticity that survives a copy
/// across spaces (in-space authorship is already ACL-signed). Field
/// names are camelCase on the JSON wire (manifest sidecar / record).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Attestation {
    pub publisher: String,
    pub signature: String,
    pub source_hash: String,   // wire: sourceHash
    pub manifest_hash: String, // wire: manifestHash
    pub spec: String,          // name@version
}

/// Map a program to its trust tier. A hash mismatch against the
/// attestation (a fork: copy-and-modify breaks the signature binding)
/// MUST drop to "unverified" — YOUR fork needs YOUR grant. `verifier`
/// is injected; the identities-directory verification is a later wire.
/// `self_publisher` marks self-authored programs until then.
pub fn trust_tier(
    program_hash: &str,
    attestation: Option<&Attestation>,
    verifier: impl Fn(&Attestation) -> bool,
    self_publisher: &str,
) -> &'static str {
    let Some(att) = attestation else {
        return "unverified";
    };
    if program_hash != att.source_hash {
        return "unverified";
    }
    if !verifier(att) {
        return "unverified";
    }
    if att.publisher == self_publisher {
        "self"
    } else {
        "attested"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // --- GrantSet: POLA scoping + attenuation ---

    #[test]
    fn grantset_exact_and_wildcard() {
        let g = GrantSet::of(["net.http", "data.*"]);
        assert!(g.allowed("net.http"));
        assert!(!g.allowed("net.http.raw")); // exact entry is exact
        assert!(g.allowed("data.read") && g.allowed("data.write"));
        assert!(!g.allowed("chat.send"));
    }

    #[test]
    fn grantset_attenuate_is_intersection() {
        let parent = GrantSet::of(["data.*", "net.http"]);
        let child = GrantSet::of(["data.read", "net.http", "chat.send"]);
        let both = parent.attenuate(&child);
        assert!(both.allowed("data.read") && both.allowed("net.http"));
        assert!(!both.allowed("data.write")); // child never asked
        assert!(!both.allowed("chat.send")); // parent never held

        // wildcard ∩ wildcard keeps the narrower prefix
        let narrow = GrantSet::of(["data.a.*"]).attenuate(&GrantSet::of(["data.*"]));
        assert!(narrow.allowed("data.a.x") && !narrow.allowed("data.b"));
    }

    #[test]
    fn grantset_covers() {
        assert!(GrantSet::of(["data.*"]).covers(&GrantSet::of(["data.read"])));
        assert!(GrantSet::of(["data.*"]).covers(&GrantSet::of(["data.sub.*"])));
        assert!(!GrantSet::of(["data.read"]).covers(&GrantSet::of(["data.*"])));
    }

    // --- ledger ---

    #[test]
    fn ledger_roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("grants.json");
        let led = GrantLedger::new(&path);
        assert!(led.lookup("h1").unwrap().is_none());
        led.grant(
            "h1",
            &["net.http".into(), "data.read".into()],
            "unverified",
            "2026-07-08T00:00:00Z",
        )
        .unwrap();
        let got = led.lookup("h1").unwrap().unwrap();
        assert_eq!(
            got,
            json!({"caps": ["data.read", "net.http"], "tier": "unverified",
                   "grantedAt": "2026-07-08T00:00:00Z"})
        );
        // survives a fresh handle (file-backed)
        assert_eq!(GrantLedger::new(&path).lookup("h1").unwrap().unwrap(), got);
        led.revoke("h1").unwrap();
        assert!(led.lookup("h1").unwrap().is_none());
    }

    // --- decide: the tier policy ---

    fn never_prompt(_caps: &[String]) -> bool {
        panic!("prompt must not be called");
    }

    #[test]
    fn decide_auto_grant_tiers() {
        let dir = tempfile::tempdir().unwrap();
        let led = GrantLedger::new(dir.path().join("g.json"));
        for tier in ["self", "attested"] {
            let g = decide(&led, "h1", ["net.http"], tier, never_prompt, "t").unwrap();
            assert!(g.allowed("net.http"));
        }
        // nothing persisted for auto tiers
        assert!(led.lookup("h1").unwrap().is_none());
    }

    #[test]
    fn decide_first_run_prompts_and_persists() {
        let dir = tempfile::tempdir().unwrap();
        let led = GrantLedger::new(dir.path().join("g.json"));
        let mut asked: Vec<Vec<String>> = Vec::new();
        let g = decide(
            &led,
            "h1",
            ["net.http"],
            "unverified",
            |caps| {
                asked.push(caps.to_vec());
                true
            },
            "t1",
        )
        .unwrap();
        assert_eq!(asked, vec![vec!["net.http".to_string()]]);
        assert!(g.allowed("net.http"));
        assert_eq!(
            led.lookup("h1").unwrap().unwrap()["caps"],
            json!(["net.http"])
        );
        // second run: covered → silent
        let g2 = decide(&led, "h1", ["net.http"], "unverified", never_prompt, "t2").unwrap();
        assert!(g2.allowed("net.http"));
    }

    #[test]
    fn decide_attenuation_only_regrants_silently() {
        let dir = tempfile::tempdir().unwrap();
        let led = GrantLedger::new(dir.path().join("g.json"));
        led.grant(
            "h1",
            &["data.*".into(), "net.http".into()],
            "unverified",
            "t0",
        )
        .unwrap();
        let g = decide(&led, "h1", ["data.read"], "unverified", never_prompt, "t1").unwrap();
        assert!(g.allowed("data.read") && !g.allowed("net.http"));
        assert_eq!(
            led.lookup("h1").unwrap().unwrap(),
            json!({"caps": ["data.read"], "tier": "unverified", "grantedAt": "t1"})
        );
    }

    #[test]
    fn decide_expansion_prompts_and_denial_refuses() {
        let dir = tempfile::tempdir().unwrap();
        let led = GrantLedger::new(dir.path().join("g.json"));
        led.grant("h1", &["net.http".into()], "unverified", "t0")
            .unwrap();
        let mut asked: Vec<Vec<String>> = Vec::new();
        decide(
            &led,
            "h1",
            ["net.http", "chat.send"],
            "unverified",
            |caps| {
                asked.push(caps.to_vec());
                true
            },
            "t1",
        )
        .unwrap();
        assert_eq!(
            asked,
            vec![vec!["chat.send".to_string(), "net.http".to_string()]]
        );

        let err = decide(&led, "h2", ["chat.send"], "unverified", |_| false, "t2").unwrap_err();
        assert!(matches!(err, Denied::Refused { .. }));
        // refusal grants nothing
        assert!(led.lookup("h2").unwrap().is_none());
    }

    #[test]
    fn decide_unknown_tier() {
        let dir = tempfile::tempdir().unwrap();
        let led = GrantLedger::new(dir.path().join("g.json"));
        let err = decide(&led, "h", Vec::<String>::new(), "bogus", never_prompt, "t").unwrap_err();
        assert!(matches!(err, Denied::UnknownTier(_)));
    }

    // --- attestation → trust tier ---

    fn att(source_hash: &str, publisher: &str) -> Attestation {
        Attestation {
            publisher: publisher.into(),
            signature: "sig".into(),
            source_hash: source_hash.into(),
            manifest_hash: "mh".into(),
            spec: "t@v1".into(),
        }
    }

    #[test]
    fn trust_tier_paths() {
        let ok = |_: &Attestation| true;
        assert_eq!(trust_tier("h1", None, ok, "self"), "unverified");
        assert_eq!(
            trust_tier("h1", Some(&att("h1", "acme")), ok, "self"),
            "attested"
        );
        assert_eq!(
            trust_tier("h1", Some(&att("h1", "self")), ok, "self"),
            "self"
        );
        // bad signature
        assert_eq!(
            trust_tier("h1", Some(&att("h1", "acme")), |_| false, "self"),
            "unverified"
        );
    }

    #[test]
    fn fork_drops_to_unverified() {
        // copy-and-modify: content hash no longer matches the attested one
        assert_eq!(
            trust_tier("h2-forked", Some(&att("h1", "acme")), |_| true, "self"),
            "unverified"
        );
    }
}
