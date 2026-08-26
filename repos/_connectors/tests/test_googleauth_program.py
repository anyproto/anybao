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
    assert fake.calls == [("oauth.connect", {
        "provider": "google", "scopes": ["s1", "s2"], "timeout": 30.0})]

    out = ga.connect()
    assert out == granted
    assert fake.calls[1] == ("oauth.connect", {"provider": "google"})


def test_status_and_disconnect_pass_through():
    st = {"connected": True, "pending": False, "scopes": ["s1"],
          "account": "u@example.com", "expiresAt": 1785500000.0}
    ga, fake = load({"oauth.status": st,
                     "oauth.disconnect": {"ok": True, "provider": "google",
                                          "revoked": True}})
    assert ga.status() == st
    assert ga.disconnect()["revoked"] is True
    assert [c[0] for c in fake.calls] == ["oauth.status", "oauth.disconnect"]


def test_typed_failures_map_to_actionable_messages():
    cases = [
        ("not_configured", "no connector.oauth.google.client_id — seed it",
         ("Cloud Console", "Credentials")),
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
