"""Read-only Attio CRM connector — records, lists, notes, members.

Discover the workspace's objects and attribute schemas, query records
(people / companies / deals) with Attio's filter/sort DSL, page list
(pipeline) entries, read free-text notes, resolve workspace members.
Ingestion only — never writes back. Methods return {ok, ...} or
{ok: False, error} with actionable messages. Fresh Attio tokens have
NO scopes and 403 until read scopes are granted — errors list which."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# Attio REST API v2. The access token never enters the guest: every
# request names `credential: {ref: "connector.key.attio", prefix:
# "Bearer "}` and the host injects the Authorization header after
# recording (anybao ADR-008 §1).

import json

_BASE = "https://api.attio.com/v2"
_CRED = {"ref": "connector.key.attio", "header": "Authorization", "prefix": "Bearer "}
_MAX_RETRIES = 3
_TIMEOUT_S = 60

_REQUIRED_SCOPES = ("record_permission:read, object_configuration:read, "
                    "list_entry:read, user_management:read, note:read")
_SCOPE_HINT = ("Attio tokens default to NO scopes — create/edit the integration "
               "under Workspace settings -> Developers and grant these read "
               "scopes: " + _REQUIRED_SCOPES + ".")

_NOT_CONNECTED = (
    "Attio not connected — create an access token at Workspace settings -> "
    "Developers (developers.attio.com), grant the read scopes, then set "
    "ATTIO_API_TOKEN in the runtime environment once (serve seeds the "
    "device-local secret store on start) or write a localValue for "
    "connector.key.attio on the config object. " + _SCOPE_HINT
)


def _clamp_limit(limit, default):
    # Attio caps record/list-entry query limits at 500.
    if not isinstance(limit, (int, float)) or limit <= 0:
        return default
    return min(int(limit), 500)


def _err(resp):
    """Non-ok response → {ok: False, error, status}. 403 almost always
    means missing token scopes — spell that out."""
    detail = ""
    try:
        body = resp.json()
        detail = (body.get("message") or json.dumps(body.get("errors"))
                  if body.get("errors") else body.get("message") or "")
        if not detail and body.get("error"):
            detail = json.dumps(body["error"])
    except (ValueError, AttributeError):
        pass
    suffix = f" ({detail})" if detail else ""
    if resp.status == 403:
        return {"ok": False, "status": 403,
                "error": "Attio returned 403 (forbidden) — almost always missing "
                         "token scopes. " + _SCOPE_HINT + suffix}
    if resp.status == 401:
        return {"ok": False, "status": 401,
                "error": "Attio returned 401 — the token is missing or invalid. "
                         "Create a fresh token at Workspace settings -> Developers "
                         "and re-seed connector.key.attio." + suffix}
    return {"ok": False, "status": resp.status,
            "error": f"Attio HTTP {resp.status}" + (f": {detail}" if detail else "")}


def _request(verb, path, body=None):
    """One call with bounded 429 backoff → {ok, body} | {ok: False, ...}."""
    kw = {"timeout": _TIMEOUT_S, "credential": _CRED}
    if body is not None:
        kw["json"] = body
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = getattr(http, verb)(_BASE + path, **kw)  # noqa: F821 - guest global
        except EffectError as e:  # noqa: F821 - guest global
            if "no secret for credential ref" in str(e):
                return {"ok": False, "error": _NOT_CONNECTED}
            return {"ok": False, "error": f"request failed: {e}"}
        if resp.status == 429 and attempt < _MAX_RETRIES:
            try:
                wait = int(resp.headers.get("retry-after") or 0)
            except ValueError:
                wait = 0
            effect("sleep", {"seconds": min(max(wait, attempt + 1), 60)})  # noqa: F821 - guest global
            continue
        if resp.status >= 300:
            return _err(resp)
        try:
            return {"ok": True, "body": resp.json()}
        except ValueError:
            return {"ok": False, "status": resp.status, "error": "unparseable Attio response"}
    return {"ok": False, "error": f"Attio rate limit — gave up after {_MAX_RETRIES} retries."}


def _data(res):
    return (res["body"] or {}).get("data") or []


@span("attio.whoami", kind="getter")  # noqa: F821 - guest global
def whoami():
    """Credential/scope sanity check via the workspace-members list.

    (There is no dedicated token-self endpoint.)

    Returns `{ok, connected, memberCount, members}`.
    """
    res = _request("get", "/workspace_members")
    if not res["ok"]:
        return res
    members = _data(res)
    return {"ok": True, "connected": True, "memberCount": len(members), "members": members}


@span("attio.list_objects", kind="getter")  # noqa: F821 - guest global
def list_objects():
    """The workspace's standard/custom objects and their slugs.

    (people / companies / deals / ...)

    Returns `{ok, objects}`.
    """
    res = _request("get", "/objects")
    return res if not res["ok"] else {"ok": True, "objects": _data(res)}


@span("attio.list_attributes", kind="getter")  # noqa: F821 - guest global
def list_attributes(object):
    """One object's attribute schema — which slugs exist to read/map.

    Returns `{ok, attributes}`.
    """
    if not object:
        return {"ok": False, "error": "object (slug or UUID) is required"}
    res = _request("get", f"/objects/{object}/attributes")
    return res if not res["ok"] else {"ok": True, "attributes": _data(res)}


@span("attio.query_records", kind="getter")  # noqa: F821 - guest global
def query_records(object, filter=None, sorts=None, limit=None, offset=None):
    """The workhorse: page one object's records (filter/sort DSL).

    People / companies / deals; limit default 100, max 500.

    Returns `{ok, records, count, offset, limit}`.
    """
    if not object:
        return {"ok": False, "error": "object (slug or UUID) is required"}
    body = {"limit": _clamp_limit(limit, 100),
            "offset": offset if isinstance(offset, int) and offset >= 0 else 0}
    if filter:
        body["filter"] = filter
    if sorts:
        body["sorts"] = sorts
    res = _request("post", f"/objects/{object}/records/query", body)
    if not res["ok"]:
        return res
    records = _data(res)
    return {"ok": True, "records": records, "count": len(records),
            "offset": body["offset"], "limit": body["limit"]}


@span("attio.get_record", kind="getter")  # noqa: F821 - guest global
def get_record(object, record_id):
    """Single record by record_id UUID.

    Returns `{ok, record}`.
    """
    if not object:
        return {"ok": False, "error": "object (slug or UUID) is required"}
    if not record_id:
        return {"ok": False, "error": "record_id (UUID) is required"}
    res = _request("get", f"/objects/{object}/records/{record_id}")
    if not res["ok"]:
        return res
    return {"ok": True, "record": (res["body"] or {}).get("data")}


@span("attio.list_lists", kind="getter")  # noqa: F821 - guest global
def list_lists():
    """The user's pipelines/segments.

    Returns `{ok, lists}`.
    """
    res = _request("get", "/lists")
    return res if not res["ok"] else {"ok": True, "lists": _data(res)}


@span("attio.query_list_entries", kind="getter")  # noqa: F821 - guest global
def query_list_entries(list, filter=None, sorts=None, limit=None, offset=None):
    """Page a list's entries — same semantics as query_records.

    Returns `{ok, entries, count, offset, limit}`.
    """
    if not list:
        return {"ok": False, "error": "list (slug or UUID) is required"}
    body = {"limit": _clamp_limit(limit, 100),
            "offset": offset if isinstance(offset, int) and offset >= 0 else 0}
    if filter:
        body["filter"] = filter
    if sorts:
        body["sorts"] = sorts
    res = _request("post", f"/lists/{list}/entries/query", body)
    if not res["ok"]:
        return res
    entries = _data(res)
    return {"ok": True, "entries": entries, "count": len(entries),
            "offset": body["offset"], "limit": body["limit"]}


@span("attio.list_notes", kind="getter")  # noqa: F821 - guest global
def list_notes(limit=None, offset=None):
    """Free-text notes attached to records.

    Returns `{ok, notes, count, offset, limit}`.
    """
    limit = _clamp_limit(limit, 50)
    offset = offset if isinstance(offset, int) and offset >= 0 else 0
    res = _request("get", f"/notes?limit={limit}&offset={offset}")
    if not res["ok"]:
        return res
    notes = _data(res)
    return {"ok": True, "notes": notes, "count": len(notes),
            "offset": offset, "limit": limit}


@span("attio.list_workspace_members", kind="getter")  # noqa: F821 - guest global
def list_workspace_members():
    """Team roster for owner/assignee resolution.

    Returns `{ok, members}`.
    """
    res = _request("get", "/workspace_members")
    return res if not res["ok"] else {"ok": True, "members": _data(res)}


def main(args):
    return whoami()
