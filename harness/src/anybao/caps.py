"""caps — CapBAC grant policy (ADR-002 §2, 00-plan "Capabilities & trust").

Manifest = request, grant = authority — they live in different places.
The broker (anyrt.effects) holds the RULE: consult `grants.allowed(cap)`
before anything runs. This module is the POLICY it consults:

- `GrantSet` — POLA-scoped cap set (exact + prefix-wildcard entries),
  Macaroons-style `attenuate` = intersection down the import chain (a
  child never exceeds its parent).
- `GrantLedger` — user-side, file-backed, keyed by PROGRAM CONTENT HASH
  (ProgramSource.fingerprint: source + manifest). Editing a manifest
  changes the hash → the grant no longer matches → re-prompt; tampering
  yields a consent dialog, not authority.
- `decide` — trust-tier policy: self/attested auto-grant; unverified
  auto-regrants attenuation-only changes and prompts on expansion (the
  chat/UI-command channel is the injected `prompt` callable).
- `Attestation` / `trust_tier` — publisher signature over
  (sourceHash, manifestHash, spec); a fork breaks the attestation and
  drops to "unverified" for free. Signature verification is injected
  (identities-directory wire lands later).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path


class CapabilityDenied(Exception):
    """The user (or policy) refused a grant — the program must not run
    with the requested caps."""


def _entry_covers(entry: str, cap: str) -> bool:
    # Prefix wildcard: "data.*" covers "data.read"; exact otherwise.
    if entry.endswith(".*"):
        return cap.startswith(entry[:-1])
    return entry == cap


@dataclass(frozen=True)
class GrantSet:
    """A set of grant entries: exact caps ("net.http") and prefix
    wildcards ("data.*"). POLA: allowed() is cover-by-any-entry."""

    entries: frozenset[str]

    @classmethod
    def of(cls, caps: Iterable[str]) -> GrantSet:
        return cls(frozenset(caps))

    def allowed(self, cap: str) -> bool:
        return any(_entry_covers(e, cap) for e in self.entries)

    def _covers_entry(self, entry: str) -> bool:
        # A wildcard is only covered by an equal-or-broader wildcard
        # (it matches infinitely many caps — no exact entry can).
        if entry.endswith(".*"):
            return any(e.endswith(".*") and entry[:-1].startswith(e[:-1]) for e in self.entries)
        return self.allowed(entry)

    def covers(self, other: GrantSet) -> bool:
        """True iff every cap `other` allows, self allows too."""
        return all(self._covers_entry(e) for e in other.entries)

    def attenuate(self, other: GrantSet) -> GrantSet:
        """Semantic intersection (Macaroons-style import-chain
        attenuation): the result allows a cap iff BOTH sides do. For
        this grammar that is exactly the entries of each side the other
        covers (of two entries matching the same cap, one prefixes the
        other — the narrower survives)."""
        kept = {e for e in self.entries if other._covers_entry(e)}
        kept |= {e for e in other.entries if self._covers_entry(e)}
        return GrantSet(frozenset(kept))


class GrantLedger:
    """User-side revocable grant store (never in the shared program
    object). JSON file at a caller-supplied path; keys are program
    content hashes, values {caps: sorted, tier, grantedAt}."""

    def __init__(self, path: Path | str):
        self._path = Path(path)

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text())

    def _save(self, data: dict) -> None:
        self._path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

    def lookup(self, program_hash: str) -> dict | None:
        return self._load().get(program_hash)

    def grant(self, program_hash: str, caps: Iterable[str], tier: str, ts: str) -> None:
        data = self._load()
        data[program_hash] = {"caps": sorted(caps), "tier": tier, "grantedAt": ts}
        self._save(data)

    def revoke(self, program_hash: str) -> None:
        data = self._load()
        if data.pop(program_hash, None) is not None:
            self._save(data)


def decide(
    ledger: GrantLedger,
    program_hash: str,
    requested_caps: Iterable[str],
    *,
    tier: str,
    prompt: Callable[[list[str]], bool],
    ts: str = "",
) -> GrantSet:
    """The grant policy. Tier "self" and "attested" auto-grant the
    requested (= declared) caps — nothing persisted, re-derived per run.
    Tier "unverified": an existing grant covering the request
    (attenuation-only change) regrants silently; expansion or first run
    goes through `prompt` (the chat/UI-command hook) and raises
    CapabilityDenied on refusal. `ts` stamps ledger writes."""
    requested = sorted(set(requested_caps))
    wanted = GrantSet.of(requested)
    if tier in ("self", "attested"):
        return wanted
    if tier != "unverified":
        raise ValueError(f"unknown trust tier: {tier}")

    existing = ledger.lookup(program_hash)
    if existing is not None and GrantSet.of(existing["caps"]).covers(wanted):
        if existing["caps"] != requested:  # narrower — record the attenuation
            ledger.grant(program_hash, requested, tier, ts)
        return wanted
    if not prompt(requested):
        raise CapabilityDenied(f"grant refused for {program_hash}: {requested}")
    ledger.grant(program_hash, requested, tier, ts)
    return wanted


@dataclass(frozen=True)
class Attestation:
    """Publisher identity's signature over (sourceHash, manifestHash,
    spec) — the piece of publisher authenticity that survives a copy
    across spaces (in-space authorship is already ACL-signed)."""

    # camelCase mirrors the JSON wire shape (manifest sidecar / record).
    publisher: str
    signature: str
    sourceHash: str
    manifestHash: str
    spec: str  # name@version


def trust_tier(
    program_hash: str,
    attestation: Attestation | None,
    verifier: Callable[[Attestation], bool],
    *,
    self_publisher: str = "self",
) -> str:
    """Map a program to its trust tier. A hash mismatch against the
    attestation (a fork: copy-and-modify breaks the signature binding)
    MUST drop to "unverified" — YOUR fork needs YOUR grant. `verifier`
    is injected; the identities-directory verification is a later wire.
    `self_publisher` marks self-authored programs until then."""
    if attestation is None:
        return "unverified"
    if program_hash != attestation.sourceHash:
        return "unverified"
    if not verifier(attestation):
        return "unverified"
    return "self" if attestation.publisher == self_publisher else "attested"
