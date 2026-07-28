"""granola@v1 — read-only connector for Granola (AI meeting notes).

Token connector against Granola's official public REST API: the grn_
key never enters the guest — every request names `credential:
{ref: "connector.key.granola", prefix: "Bearer "}` and the host
injects the Authorization header after recording (anybao ADR-008 §1).
Surfaces meeting notes (list), a single note with summary + optional
transcript, and the folder tree. Read-only — the public API has no
writes. All methods return {ok, ...} / {ok: False, error}.

Plan gate: Granola API keys are minted only on Business/Enterprise
plans (Settings -> Connectors -> API keys); free/Basic users cannot
get a grn_ key.

CAVEAT — endpoint shapes: the Granola public API is young (~Feb 2026)
and these paths/field names (GET /v1/notes, GET /v1/notes/{id}
?include=transcript, GET /v1/folders, cursor pagination) are from the
bobrik plan's 2026-06 verification. Confirm against Granola's live API
docs on first use; adjust _BASE / the response field reads if drifted.
"""

_BASE = "https://public-api.granola.ai/v1"
_KEY_PATH = "Granola desktop app -> Settings -> Connectors -> API keys"
_CRED = {"ref": "connector.key.granola", "header": "Authorization", "prefix": "Bearer "}
_DEFAULT_LIMIT = 25
_MAX_LIMIT = 100
_MAX_PAGES = 20
_RATE_RETRIES = 3
_TIMEOUT_S = 60

_NOT_CONNECTED = (
    "Granola not connected — create an API key in " + _KEY_PATH
    + " (requires a Granola Business or Enterprise plan; free/Basic plans "
    + "cannot mint a key), then set GRANOLA_API_KEY in the runtime "
    + "environment once (serve seeds the device-local secret store on "
    + "start) or write a localValue for connector.key.granola on the "
    + "config object."
)


def _clamp_limit(n):
    if not isinstance(n, (int, float)) or n <= 0:
        return _DEFAULT_LIMIT
    return min(int(n), _MAX_LIMIT)


def _get(path, params=None):
    """One GET → {ok, body} | {ok: False, error, status?}. Bounded 429
    backoff honoring Retry-After; 401/403 map back to the key + the
    plan gate."""
    kw = {"timeout": _TIMEOUT_S, "credential": _CRED}
    if params:
        kw["params"] = {k: v for k, v in params.items() if v not in (None, "")}
    for attempt in range(1, _RATE_RETRIES + 2):
        try:
            resp = http.get(_BASE + path, **kw)  # noqa: F821 - guest global
        except EffectError as e:  # noqa: F821 - guest global
            if "no secret for credential ref" in str(e):
                return {"ok": False, "error": _NOT_CONNECTED}
            return {"ok": False, "error": f"request failed: {e}"}
        if resp.status == 429 and attempt <= _RATE_RETRIES:
            try:
                wait = int(resp.headers.get("retry-after") or 0)
            except ValueError:
                wait = 0
            effect("sleep", {"seconds": min(max(wait, attempt), 60)})  # noqa: F821 - guest global
            continue
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status >= 300:
            if resp.status in (401, 403):
                return {"ok": False, "status": resp.status,
                        "error": f"Granola rejected the API key (HTTP {resp.status}). "
                                 f"Re-seed connector.key.granola, or mint a new key in "
                                 f"{_KEY_PATH} (requires a Business/Enterprise plan)."}
            msg = None
            if isinstance(body, dict):
                msg = body.get("error") or body.get("errors") or body.get("message")
            return {"ok": False, "status": resp.status,
                    "error": str(msg) if msg else f"HTTP {resp.status}"}
        return {"ok": True, "body": body, "status": resp.status}
    return {"ok": False, "error": f"Granola rate limit — gave up after {_RATE_RETRIES} retries."}


def _next_cursor(body):
    """The pagination cursor under its likely field names, else None."""
    if not isinstance(body, dict):
        return None
    return (body.get("next_cursor") or body.get("nextCursor") or body.get("cursor")
            or (body.get("pagination") or {}).get("next_cursor"))


def _rows(body, keys):
    """The array payload under the first matching key (the API wraps
    rows under e.g. `notes` / `data` / `folders`)."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in keys:
            if isinstance(body.get(k), list):
                return body[k]
    return []


def _page_all(path, base_params, row_keys, cursor, limit, max_items):
    """Shared cursor-pagination loop → {ok, rows, nextCursor} |
    {ok: False, ...}. Single-page when max_items is None; else follows
    the cursor (bounded by _MAX_PAGES) up to max_items rows."""
    rows = []
    for _ in range(_MAX_PAGES):
        r = _get(path, {**base_params, "cursor": cursor, "limit": limit})
        if not r["ok"]:
            return r
        rows.extend(_rows(r["body"], row_keys))
        cursor = _next_cursor(r["body"])
        if max_items is None:
            break
        if len(rows) >= max_items:
            rows = rows[:max_items]
            break
        if not cursor:
            break
    return {"ok": True, "rows": rows, "nextCursor": cursor}


@span("granola.verify", kind="getter")  # noqa: F821 - guest global
def verify():
    """Connectivity check + key validator (pulls a 1-item note page).
    Use right after seeding a key."""
    r = _get("/notes", {"limit": 1})
    if not r["ok"]:
        return r
    return {"ok": True, "connected": True}


@span("granola.list_notes", kind="getter")  # noqa: F821 - guest global
def list_notes(created_after=None, folder_id=None, cursor=None, limit=None,
               max_items=None):
    """Page the user's meeting notes (newest-first per the API). A note
    only appears once its AI summary + transcript finished generating.
    With max_items set, follows the cursor across pages; otherwise one
    page + nextCursor."""
    max_items = int(max_items) if isinstance(max_items, (int, float)) and max_items > 0 else None
    r = _page_all("/notes", {"created_after": created_after, "folder_id": folder_id},
                  ["notes", "data"], cursor, _clamp_limit(limit), max_items)
    if not r["ok"]:
        return r
    return {"ok": True, "notes": r["rows"], "nextCursor": r["nextCursor"]}


@span("granola.get_note", kind="getter")  # noqa: F821 - guest global
def get_note(id, include_transcript=False):
    """One note with summary and (optionally) the raw transcript. A
    freshly-ended meeting whose summary/transcript hasn't generated yet
    may 404 — retry shortly."""
    if not id or not isinstance(id, str):
        return {"ok": False, "error": "id is required"}
    params = {"include": "transcript"} if include_transcript else None
    r = _get(f"/notes/{id}", params)
    if not r["ok"]:
        if r.get("status") == 404:
            return {"ok": False, "status": 404,
                    "error": f"note not found (or its summary/transcript hasn't "
                             f"generated yet): {id}"}
        return r
    body = r["body"]
    note = body.get("note") if isinstance(body, dict) and body.get("note") else body
    if not note:
        return {"ok": False, "error": f"empty note response for: {id}"}
    return {"ok": True, "note": note}


@span("granola.list_folders", kind="getter")  # noqa: F821 - guest global
def list_folders(cursor=None, limit=None, max_items=None):
    """Accessible folders (hierarchy via parent_folder_id), cursor-
    paginated — same paging contract as list_notes."""
    max_items = int(max_items) if isinstance(max_items, (int, float)) and max_items > 0 else None
    r = _page_all("/folders", {}, ["folders", "data"], cursor,
                  _clamp_limit(limit), max_items)
    if not r["ok"]:
        return r
    return {"ok": True, "folders": r["rows"], "nextCursor": r["nextCursor"]}


def main(args):
    return verify()
