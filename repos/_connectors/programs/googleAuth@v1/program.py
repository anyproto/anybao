"""Google account connection — one consent covers gmail/calendar/drive/sheets.

connect(scopes?, timeout?) runs Google's consent in the user's browser
(host-side OAuth, anybao ADR-011 — no token ever enters guest code)
→ {ok, provider, grantedScopes, account}. status() → {connected,
pending, scopes, account, expiresAt}; disconnect() revokes at Google
AND deletes the local grant. Exposes _CRED, the shared credential ref
every Google connector passes on http calls. connect blocks up to
120s — user-facing turns only, never cron; after consent_timeout the
consent window stays open ~5 min, poll status()."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# The whole token lifecycle is host-side (ADR-011 §6): the broker
# resolves the managed ref below by caching/refreshing access tokens
# and injecting the header at request time. Typed failures arrive as
# EffectError "type: message" — matched by PREFIX here (the static
# connectors' "no secret for credential ref" substring contract does
# not apply to managed refs).

_PROVIDER = "google"
_CRED = {"ref": "connector.oauth.google", "header": "Authorization", "prefix": "Bearer "}

_CONFIGURE_HINT = (
    " — create an OAuth client in Google Cloud Console (APIs & Services > "
    "Credentials > Create credentials > OAuth client ID > Desktop app), then "
    "enter connector.oauth.google.client_id and "
    "connector.oauth.google.client_secret in Credentials in the app (CLI: a "
    ".connectors.env beside anybao.toml)."
)


def _friendly(msg):
    """Map the host's typed failure prefixes (ADR-011 §5/§6) to
    actionable guidance; unknown types pass through verbatim."""
    if msg.startswith("not_configured"):
        return msg + _CONFIGURE_HINT
    if msg.startswith("consent_timeout"):
        return (msg + " — ask the user to finish the Google consent in their "
                      "browser, then call status(): it flips to connected "
                      "when they do.")
    if msg.startswith(("not_connected", "oauth_reconsent_required")):
        return msg + " — call connect() in a user-facing turn to (re)authorize."
    if msg.startswith("consent_denied"):
        return msg + " — the user declined; call connect() again when they are ready."
    return msg


def _call(name, payload):
    try:
        return effect(name, payload)  # noqa: F821 - guest global
    except EffectError as e:  # noqa: F821 - guest global
        return {"ok": False, "error": _friendly(str(e))}


@span("googleAuth.connect", kind="mutator")  # noqa: F821 - guest global
def connect(scopes=None, timeout=None):
    """Run Google consent → {ok, provider, grantedScopes, account}.

    Opens the browser on the serve machine and BLOCKS up to `timeout`
    seconds (default 120) — call only from a user-facing turn. On
    consent_timeout the window stays open ~5 min; poll status().
    scopes: optional list to override the default read-only union
    (gmail/calendar/drive-metadata/sheets); Google re-consents
    incrementally, so adding a scope later keeps earlier grants."""
    payload = {"provider": _PROVIDER}
    if scopes:
        payload["scopes"] = list(scopes)
    if timeout is not None:
        payload["timeout"] = float(timeout)
    return _call("oauth.connect", payload)


@span("googleAuth.status", kind="getter")  # noqa: F821 - guest global
def status():
    """Connection state → {connected, pending, scopes, account, expiresAt}.

    No network: pending=True means a consent window is open right now;
    expiresAt is the cached access token's expiry (null when cold —
    the host refreshes on the next request, that's normal)."""
    return _call("oauth.status", {"provider": _PROVIDER})


@span("googleAuth.disconnect", kind="mutator")  # noqa: F821 - guest global
def disconnect():
    """Revoke the grant at Google + delete it locally → {ok, provider, revoked}.

    revoked=False means Google's revoke endpoint didn't confirm — the
    local grant is deleted regardless; the user can also revoke at
    https://myaccount.google.com/permissions."""
    return _call("oauth.disconnect", {"provider": _PROVIDER})


def main(args):
    return status()
