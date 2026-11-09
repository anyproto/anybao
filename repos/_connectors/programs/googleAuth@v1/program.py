# ADR-011
"""Google account connection — one consent covers gmail/calendar/drive/sheets.

connect(scopes?, timeout?, writes?) runs Google's consent in the user's
browser (host-side OAuth — no token enters guest code) → {ok, provider,
grantedScopes, account}; writes=True is a second, separate consent for
Gmail drafts + calendar holds. status(writes?) → {connected, pending,
scopes, account, expiresAt}; disconnect() revokes both at Google and
deletes them. Exposes _CRED (reads) and _CRED_WRITE (drafts, holds).
connect blocks up to 120s — user-facing turns only, never cron."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# The whole token lifecycle is host-side (ADR-011 §6): the broker
# resolves the managed ref below by caching/refreshing access tokens
# and injecting the header at request time. Typed failures arrive as
# EffectError "type: message" — matched by PREFIX here (the static
# connectors' "no secret for credential ref" substring contract does
# not apply to managed refs).

_PROVIDER = "google"
# ADR-034 §2: writes ride their own provider handle and refresh token.
# Its descriptor (drafts + holds, never a send) exists only in runtimes
# that enforce it, so an older runtime can neither mint nor inject a
# write-capable token — not even one granted on another device.
_WRITE_PROVIDER = "google_write"
# The OAuth client this connector ships with (ADR-011 §3): a Google
# "Desktop app" client is a PUBLIC client — its id and "secret" are
# bundled, extractable by design, and prove nothing; PKCE + the loopback
# redirect are the protection. Only the refresh token is a secret, and
# it never leaves the host. The host remembers these as non-secret
# metadata rows so refresh keeps working across restarts; a self-hosted
# override is a `connector.oauth.google.client_id` row in Credentials.
_CLIENT_ID = "519847309896-if7o0qblp6dr4d5elr1ibuhkge8c2mc3.apps.googleusercontent.com"
_CLIENT_SECRET = "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"
# ADR-021 §8.1: the managed ref's declared destinations — the Google
# API hosts the family (gmail/calendar/drive/sheets) sends the access
# token to; the token endpoint is host-side and not listed here
__any_credentials__ = [{"ref": "connector.oauth.google",
                        "about": {"label": "Google account (OAuth)",
                                  "hosts": ["www.googleapis.com", "gmail.googleapis.com",
                                            "sheets.googleapis.com", "meet.googleapis.com"]}},
                       {"ref": "connector.oauth.google_write",
                        "about": {"label": "Google account — drafts and holds (OAuth)",
                                  "hosts": ["www.googleapis.com", "gmail.googleapis.com"]}}]
_CRED = {"ref": "connector.oauth.google", "header": "Authorization", "prefix": "Bearer "}
_CRED_WRITE = {"ref": "connector.oauth.google_write", "header": "Authorization",
               "prefix": "Bearer "}

# The read handle's default consent (runtime/src/oauth_providers.json
# `google.default_scopes`, pinned equal by test_googleauth_program).
# The host no longer folds earlier grants into a new token
# (include_granted_scopes=false, ADR-034 §6), so an explicit scope list
# is unioned with this one here and a partial list never drops reads.
# The write handle's scopes live only in the host table: what a cell
# may WRITE is fixed by its `guest_writes`, never by a guest list.
_READ_SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

_CONFIGURE_HINT = (
    " — this connector ships its own OAuth client, so this means the "
    "runtime is older than the connector or the provider table lacks "
    "google; a self-hosted override is a connector.oauth.google.client_id "
    "row in Credentials in the app."
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
def connect(scopes=None, timeout=None, writes=False):
    """Run Google consent → {ok, provider, grantedScopes, account}.

    Opens the browser on the serve machine and BLOCKS up to `timeout`
    seconds (default 120) — call only from a user-facing turn. On
    consent_timeout the window stays open ~5 min; poll status().
    Default: the read-only union (gmail/calendar/drive-metadata/sheets);
    scopes: extra read scopes, added to that union. writes=True is a
    separate consent (gmail.compose + calendar.events) for
    gmail.create_draft and googleCalendar.create_event — ask the user
    first; it takes no scopes. Bao still cannot send mail or invite
    anyone: the runtime refuses it whatever is granted."""
    if writes:
        if scopes:
            return {"ok": False, "error": "writes=True takes no scopes — the write "
                                          "consent is fixed by the runtime (ADR-034)"}
        payload = {"provider": _WRITE_PROVIDER, "client_id": _CLIENT_ID,
                   "client_secret": _CLIENT_SECRET}
        if timeout is not None:
            payload["timeout"] = float(timeout)
        out = _call("oauth.connect", payload)
        if (out or {}).get("ok") is False and "no oauth provider" in out.get("error", ""):
            out["error"] = ("this runtime predates the write guard (ADR-034) — Bao "
                            "cannot write to Google until the app is updated")
        return out
    payload = {"provider": _PROVIDER, "client_id": _CLIENT_ID,
               "client_secret": _CLIENT_SECRET}
    if scopes:
        extra = [s for s in scopes if s not in _READ_SCOPES]
        payload["scopes"] = _READ_SCOPES + extra
    if timeout is not None:
        payload["timeout"] = float(timeout)
    return _call("oauth.connect", payload)


@span("googleAuth.status", kind="getter")  # noqa: F821 - guest global
def status(writes=False):
    """Connection state → {connected, pending, scopes, account, expiresAt}.

    No network: pending=True means a consent window is open right now;
    expiresAt is the cached access token's expiry (null when cold —
    the host refreshes on the next request, that's normal). writes=True
    reports the drafts-and-holds grant instead of the read grant."""
    return _call("oauth.status", {"provider": _WRITE_PROVIDER if writes else _PROVIDER})


@span("googleAuth.disconnect", kind="mutator")  # noqa: F821 - guest global
def disconnect():
    """Revoke both grants at Google + delete them locally → {ok, provider, revoked, writes?}.

    The drafts-and-holds grant goes first (writes: its result, when this
    runtime has one). revoked=False means Google's revoke endpoint didn't
    confirm — the local grant is deleted regardless; the user can also
    revoke at https://myaccount.google.com/permissions."""
    w = _call("oauth.disconnect", {"provider": _WRITE_PROVIDER})
    out = _call("oauth.disconnect", {"provider": _PROVIDER})
    if isinstance(out, dict) and isinstance(w, dict) and w.get("ok") is not False:
        out["writes"] = w
    return out


def main(args):
    return status()
