"""programs/googleAuth@v1 through the REAL guest kernel. Pinned here:
the thin wrapping of the oauth.* effects (payload shapes in, envelope
passthrough out), the shared _CRED managed ref the family imports
(anybao ADR-011 §4: full {ref, header, prefix} shape), and the
prefix-matched typed-failure → actionable-message mapping (ADR-011
§5/§6 failure contract; managed refs do NOT use the static
"no secret for credential ref" substring)."""

from connectorenv import connector_kernel


def typed(type_name, message):
    """An exception whose CLASS NAME is the runtime failure type —
    kernelenv maps host exceptions to {type: cls-name, message}, so
    the guest sees the runtime's "type: message" EffectError string."""
    return type(type_name, (Exception,), {})(message)


class FakeOauth:
    """Routes oauth.* effects; an Exception value raises host-side →
    guest EffectError (str(e) is the runtime's "type: message")."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []

    def __call__(self, name, payload):
        self.calls.append((name, payload))
        resp = self.routes.get(name)
        if resp is None:
            raise AssertionError(f"unexpected effect {name}")
        if isinstance(resp, Exception):
            raise resp
        return resp


def load(routes):
    fake = FakeOauth(routes)
    ga = connector_kernel(effect=fake).use("googleAuth@v1")
    return ga, fake


def test_cred_is_the_managed_google_ref():
    ga, _ = load({})
    assert ga._CRED == {"ref": "connector.oauth.google",
                        "header": "Authorization", "prefix": "Bearer "}


def test_connect_passes_scopes_and_timeout():
    granted = {"ok": True, "provider": "google",
               "grantedScopes": ["s1", "s2"], "account": "u@example.com"}
    ga, fake = load({"oauth.connect": granted})
    out = ga.connect(scopes=["s1", "s2"], timeout=30)
    assert out == granted
    client = {"client_id": ga._CLIENT_ID, "client_secret": ga._CLIENT_SECRET}
    assert client["client_id"] and client["client_secret"]  # bundled, ADR-011 §3
    # extra scopes are ADDED to the read union: the host no longer folds
    # earlier grants into a new token (ADR-034 §6)
    assert fake.calls == [("oauth.connect", {
        "provider": "google", "scopes": list(ga._READ_SCOPES) + ["s1", "s2"],
        "timeout": 30.0, **client})]

    out = ga.connect()
    assert out == granted
    assert fake.calls[1] == ("oauth.connect", {"provider": "google", **client})


def test_status_and_disconnect_pass_through():
    st = {"connected": True, "pending": False, "scopes": ["s1"],
          "account": "u@example.com", "expiresAt": 1785500000.0}
    ga, fake = load({"oauth.status": st,
                     "oauth.disconnect": {"ok": True, "provider": "google",
                                          "revoked": True}})
    assert ga.status() == st
    out = ga.disconnect()
    assert out["revoked"] is True
    # both grants: the write handle first, then the read one (ADR-034 §6)
    assert [c for c in fake.calls] == [
        ("oauth.status", {"provider": "google"}),
        ("oauth.disconnect", {"provider": "google_write"}),
        ("oauth.disconnect", {"provider": "google"})]
    assert out["writes"]["revoked"] is True


def test_typed_failures_map_to_actionable_messages():
    cases = [
        ("not_configured", "no OAuth client for google",
         ("ships its own OAuth client", "Credentials")),
        ("consent_timeout", "consent for google is still pending",
         ("status()",)),
        ("not_connected", "google is not connected",
         ("connect()",)),
        ("oauth_reconsent_required", "refresh token is no longer valid",
         ("connect()",)),
        ("consent_denied", 'the provider reported "access_denied"',
         ("connect()",)),
    ]
    for type_name, message, needles in cases:
        ga, _ = load({"oauth.connect": typed(type_name, message)})
        out = ga.connect()
        assert out["ok"] is False
        assert out["error"].startswith(f"{type_name}: {message}"), out["error"]
        for needle in needles:
            assert needle in out["error"], (type_name, out["error"])


def test_unknown_failure_passes_through_verbatim():
    ga, _ = load({"oauth.status": typed("URLError", "transport down")})
    out = ga.status()
    assert out == {"ok": False, "error": "URLError: transport down"}


def test_read_scopes_mirror_the_host_default():
    """connect(scopes=[…]) unions with the guest's copy of the read
    union; it must not drift from runtime/src/oauth_providers.json.
    The write handle's scopes live only in the host table (ADR-034 §6)."""
    import json
    from pathlib import Path

    table = Path(__file__).resolve().parents[3] / "runtime" / "src" / "oauth_providers.json"
    host = json.loads(table.read_text())
    ga, _ = load({})
    assert list(ga._READ_SCOPES) == host["google"]["default_scopes"]
    assert not hasattr(ga, "_WRITE_SCOPES")
    writes = host["google_write"]["default_scopes"]
    assert not any(s.endswith(("gmail.send", "mail.google.com/")) for s in writes)
    assert ga._CRED_WRITE == {"ref": "connector.oauth.google_write",
                              "header": "Authorization", "prefix": "Bearer "}
    refs = [c["ref"] for c in ga.__any_credentials__]
    assert refs == ["connector.oauth.google", "connector.oauth.google_write"]


def test_connect_writes_is_its_own_consent():
    granted = {"ok": True, "provider": "google_write", "grantedScopes": [],
               "account": "u@x"}
    ga, fake = load({"oauth.connect": granted, "oauth.status": {"connected": False}})
    assert ga.connect(writes=True, timeout=60) == granted
    client = {"client_id": ga._CLIENT_ID, "client_secret": ga._CLIENT_SECRET}
    # no scopes: the host table fixes the write consent
    assert fake.calls[0] == ("oauth.connect", {"provider": "google_write",
                                               "timeout": 60.0, **client})
    out = ga.connect(scopes=["s1"], writes=True)
    assert out["ok"] is False and len(fake.calls) == 1
    ga.status(writes=True)
    assert fake.calls[1] == ("oauth.status", {"provider": "google_write"})
    # the default stays read-only
    ga.connect()
    assert fake.calls[2][1]["provider"] == "google" and "scopes" not in fake.calls[2][1]


def test_an_older_runtime_refuses_the_write_consent():
    ga, _ = load({"oauth.connect": typed("not_configured",
                                         'no oauth provider "google_write"')})
    out = ga.connect(writes=True)
    assert out["ok"] is False and "predates the write guard" in out["error"]
    # disconnect still drops the read grant; no writes key without a write handle
    calls = []

    def disc(name, payload):
        calls.append(payload["provider"])
        if payload["provider"] == "google_write":
            raise typed("not_configured", 'no oauth provider "google_write"')
        return {"ok": True, "provider": "google", "revoked": True}

    ga2 = connector_kernel(effect=disc).use("googleAuth@v1")
    out = ga2.disconnect()
    assert calls == ["google_write", "google"]
    assert out == {"ok": True, "provider": "google", "revoked": True}
