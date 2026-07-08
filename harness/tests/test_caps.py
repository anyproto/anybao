import pytest
from anybao.caps import Attestation, CapabilityDenied, GrantLedger, GrantSet, decide, trust_tier
from anyrt import trace as tr
from anyrt.effects import Broker, EffectError, Registry, effect


def make_registry():
    reg = Registry()
    calls = []

    @effect("http.get", kind="read", registry=reg, cap="net.http")
    def http_get(ctx, url):
        calls.append(url)
        return {"status": 200}

    return reg, calls


# --- broker enforcement (ADR-002 §2: check before consult/execute) ---

def test_broker_denies_and_records():
    reg, calls = make_registry()
    w = tr.TraceWriter(run={"id": "r1"})
    b = Broker(reg, w, grants=GrantSet.of(["data.read"]))
    with pytest.raises(EffectError) as ei:
        b.call("http.get", {"url": "https://a"})
    assert ei.value.type == "capability_denied"
    assert calls == []  # never executed
    rec = w.records[1]
    assert rec["error"]["type"] == "capability_denied"
    assert rec["output"] is None
    assert rec["meta"]["class"] == "read" and rec["meta"]["mocked"] is False


def test_broker_check_precedes_replay_consult():
    reg, _ = make_registry()
    w1 = tr.TraceWriter(run={"id": "r2"})
    Broker(reg, w1).call("http.get", {"url": "https://a"})
    # replaying with a denying grant set: denial wins over the cursor
    b = Broker(reg, tr.TraceWriter(run={"id": "r2r"}), mode="replay",
               cursor=tr.ReplayCursor(w1.records), grants=GrantSet.of([]))
    with pytest.raises(EffectError) as ei:
        b.call("http.get", {"url": "https://a"})
    assert ei.value.type == "capability_denied"


def test_broker_permissive_default_and_covering_grant():
    reg, calls = make_registry()
    b = Broker(reg, tr.TraceWriter(run={"id": "r3"}))  # grants=None
    assert b.call("http.get", {"url": "https://a"})["status"] == 200
    b2 = Broker(reg, tr.TraceWriter(run={"id": "r4"}), grants=GrantSet.of(["net.*"]))
    assert b2.call("http.get", {"url": "https://b"})["status"] == 200
    assert calls == ["https://a", "https://b"]


# --- GrantSet: POLA scoping + attenuation ---

def test_grantset_exact_and_wildcard():
    g = GrantSet.of(["net.http", "data.*"])
    assert g.allowed("net.http")
    assert not g.allowed("net.http.raw")   # exact entry is exact
    assert g.allowed("data.read") and g.allowed("data.write")
    assert not g.allowed("chat.send")


def test_grantset_attenuate_is_intersection():
    parent = GrantSet.of(["data.*", "net.http"])
    child = GrantSet.of(["data.read", "net.http", "chat.send"])
    both = parent.attenuate(child)
    assert both.allowed("data.read") and both.allowed("net.http")
    assert not both.allowed("data.write")  # child never asked
    assert not both.allowed("chat.send")   # parent never held
    # wildcard ∩ wildcard keeps the narrower prefix
    narrow = GrantSet.of(["data.a.*"]).attenuate(GrantSet.of(["data.*"]))
    assert narrow.allowed("data.a.x") and not narrow.allowed("data.b")


def test_grantset_covers():
    assert GrantSet.of(["data.*"]).covers(GrantSet.of(["data.read"]))
    assert GrantSet.of(["data.*"]).covers(GrantSet.of(["data.sub.*"]))
    assert not GrantSet.of(["data.read"]).covers(GrantSet.of(["data.*"]))


# --- ledger ---

def test_ledger_roundtrip(tmp_path):
    led = GrantLedger(tmp_path / "grants.json")
    assert led.lookup("h1") is None
    led.grant("h1", ["net.http", "data.read"], "unverified", "2026-07-08T00:00:00Z")
    got = led.lookup("h1")
    assert got == {"caps": ["data.read", "net.http"], "tier": "unverified",
                   "grantedAt": "2026-07-08T00:00:00Z"}
    # survives a fresh handle (file-backed)
    assert GrantLedger(tmp_path / "grants.json").lookup("h1") == got
    led.revoke("h1")
    assert led.lookup("h1") is None


# --- decide: the tier policy ---

def _never_prompt(caps):
    raise AssertionError("prompt must not be called")


def test_decide_auto_grant_tiers(tmp_path):
    led = GrantLedger(tmp_path / "g.json")
    for tier in ("self", "attested"):
        g = decide(led, "h1", ["net.http"], tier=tier, prompt=_never_prompt)
        assert g.allowed("net.http")
    assert led.lookup("h1") is None  # nothing persisted for auto tiers


def test_decide_first_run_prompts_and_persists(tmp_path):
    led = GrantLedger(tmp_path / "g.json")
    asked = []
    g = decide(led, "h1", ["net.http"], tier="unverified",
               prompt=lambda caps: asked.append(caps) or True, ts="t1")
    assert asked == [["net.http"]]
    assert g.allowed("net.http")
    assert led.lookup("h1")["caps"] == ["net.http"]
    # second run: covered → silent
    g2 = decide(led, "h1", ["net.http"], tier="unverified", prompt=_never_prompt, ts="t2")
    assert g2.allowed("net.http")


def test_decide_attenuation_only_regrants_silently(tmp_path):
    led = GrantLedger(tmp_path / "g.json")
    led.grant("h1", ["data.*", "net.http"], "unverified", "t0")
    g = decide(led, "h1", ["data.read"], tier="unverified", prompt=_never_prompt, ts="t1")
    assert g.allowed("data.read") and not g.allowed("net.http")
    assert led.lookup("h1") == {"caps": ["data.read"], "tier": "unverified", "grantedAt": "t1"}


def test_decide_expansion_prompts_and_denial_raises(tmp_path):
    led = GrantLedger(tmp_path / "g.json")
    led.grant("h1", ["net.http"], "unverified", "t0")
    asked = []
    decide(led, "h1", ["net.http", "chat.send"], tier="unverified",
           prompt=lambda caps: asked.append(caps) or True, ts="t1")
    assert asked == [["chat.send", "net.http"]]
    with pytest.raises(CapabilityDenied):
        decide(led, "h2", ["chat.send"], tier="unverified", prompt=lambda caps: False)
    assert led.lookup("h2") is None  # refusal grants nothing


def test_decide_unknown_tier(tmp_path):
    with pytest.raises(ValueError):
        decide(GrantLedger(tmp_path / "g.json"), "h", [], tier="bogus", prompt=_never_prompt)


# --- attestation → trust tier ---

def _att(source_hash, publisher="acme"):
    return Attestation(publisher=publisher, signature="sig", sourceHash=source_hash,
                       manifestHash="mh", spec="t@v1")


def test_trust_tier_paths():
    def ok(a):
        return True

    assert trust_tier("h1", None, ok) == "unverified"
    assert trust_tier("h1", _att("h1"), ok) == "attested"
    assert trust_tier("h1", _att("h1", publisher="self"), ok) == "self"
    assert trust_tier("h1", _att("h1"), lambda a: False) == "unverified"  # bad signature


def test_fork_drops_to_unverified():
    # copy-and-modify: content hash no longer matches the attested one
    assert trust_tier("h2-forked", _att("h1"), lambda a: True) == "unverified"
