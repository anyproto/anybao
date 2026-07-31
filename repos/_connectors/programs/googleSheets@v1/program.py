"""Read-only Google Sheets connector — discovery, tabs, ranges, records.

The user's spreadsheets (discovered via Drive files.list, newest
modified first), a spreadsheet's tabs + grid sizes (no cell values —
cheap to call first), A1 ranges as 2-D value arrays (formatted, or
raw via unformatted=True), several ranges in one batch call (capped
at 25), and a header-mapped records view. Auth rides googleAuth@v1
(one consent covers the Google family) — not-connected errors say to
run googleAuth.connect(). Methods return {ok, ...} or {ok: False,
error}. Scopes: spreadsheets.readonly + drive.metadata.readonly."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# Tokens never enter guest code: every request names the managed
# `credential: {ref: "connector.oauth.google", ...}` (from
# googleAuth@v1) and the broker refreshes/injects host-side at
# request time (anybao ADR-011 §6). Trim, don't rename (C1): kept
# fields carry the upstream names (files[].id/name/modifiedTime,
# range, values, valueRanges, sheetId, nextPageToken); the derived
# keys are get_spreadsheet's flattened title + per-tab rows/cols
# (from gridProperties.rowCount/columnCount) and
# read_sheet_as_objects' headers/records view over values.

_auth = use("googleAuth@v1")  # noqa: F821 - guest global
_CRED = _auth._CRED

_SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
_DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
_DEFAULT_PAGE_SIZE = 100
_MAX_PAGE_SIZE = 1000  # Drive files.list pageSize is 1..1000
_MAX_RANGES = 25  # batch_get_values cap — stay well under the read quota
_MAX_RETRIES = 3
_TIMEOUT_S = 60

_AUTH_PREFIXES = ("not_connected", "oauth_reconsent_required",
                  "not_configured", "consent_timeout")


def _clamp_page_size(n):
    if not isinstance(n, (int, float)) or n <= 0:
        return _DEFAULT_PAGE_SIZE
    return min(int(n), _MAX_PAGE_SIZE)


def _quote(s):
    """Percent-encode for a URL query value (no urllib in the kernel)."""
    safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
    return "".join(c if c in safe else "".join(f"%{b:02X}" for b in c.encode()) for c in str(s))


def _qs(params):
    """?k=v&k=v query string; list values repeat the key (?ranges=a&
    ranges=b style); None/"" skipped. The broker's `params` kwarg
    can't repeat keys, hence guest-side assembly."""
    parts = []
    for k, v in params.items():
        if v is None or v == "" or v == []:
            continue
        for item in v if isinstance(v, list) else [v]:
            if item is None or item == "":
                continue
            parts.append(f"{_quote(k)}={_quote(item)}")
    return "?" + "&".join(parts) if parts else ""


def _get(url):
    """One authed GET (Sheets or Drive base) with bounded 429 backoff
    → {ok, body} | {ok: False, error, status?}."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = http.get(url, timeout=_TIMEOUT_S, credential=_CRED)  # noqa: F821 - guest global
        except EffectError as e:  # noqa: F821 - guest global
            msg = str(e)
            if msg.startswith(_AUTH_PREFIXES):
                return {"ok": False, "error": _auth._friendly(msg)}
            return {"ok": False, "error": f"request failed: {msg}"}
        if resp.status == 429 and attempt < _MAX_RETRIES:
            try:
                wait = int(resp.headers.get("retry-after") or 0)
            except ValueError:
                wait = 0
            effect("sleep", {"seconds": min(max(wait, 2 ** attempt), 60)})  # noqa: F821 - guest global
            continue
        if resp.status >= 300:
            if resp.status == 401:
                return {"ok": False, "status": 401,
                        "error": "Sheets rejected the token (HTTP 401) — run "
                                 "googleAuth.connect() in a user-facing turn to reauthorize."}
            if resp.status == 403:
                return {"ok": False, "status": 403,
                        "error": "Sheets denied access (HTTP 403) — the grant is likely "
                                 "missing the spreadsheets.readonly or "
                                 "drive.metadata.readonly scope; re-run "
                                 "googleAuth.connect() to extend consent."}
            try:
                api_msg = (resp.json().get("error") or {}).get("message") or f"HTTP {resp.status}"
            except (ValueError, AttributeError):
                api_msg = f"HTTP {resp.status}"
            return {"ok": False, "status": resp.status, "error": f"Sheets error: {api_msg}"}
        try:
            return {"ok": True, "body": resp.json()}
        except ValueError:
            return {"ok": False, "error": "Sheets returned a non-JSON body"}
    return {"ok": False, "error": f"Sheets rate limit — gave up after {_MAX_RETRIES} retries."}


@span("googleSheets.list_spreadsheets", kind="getter")  # noqa: F821 - guest global
def list_spreadsheets(page_size=None, page_token=None, query=None):
    """The user's Google Sheets files, newest modified first → {ok, files, nextPageToken}.

    Discovery via Drive files.list; files are {id, name, modifiedTime}
    verbatim. query is an optional name substring ANDed into the Drive
    q filter. page_size 1..1000 (default 100). Also the cheapest
    connectivity check."""
    q = "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
    if query and isinstance(query, str):
        # Escape single quotes in the name fragment per Drive query grammar.
        q += " and name contains '" + query.replace("'", "\\'") + "'"
    r = _get(_DRIVE_FILES + _qs({"q": q,
                                 "fields": "nextPageToken,files(id,name,modifiedTime)",
                                 "pageSize": _clamp_page_size(page_size),
                                 "pageToken": page_token,
                                 "orderBy": "modifiedTime desc"}))
    if not r["ok"]:
        return r
    b = r["body"] or {}
    return {"ok": True, "files": b.get("files") or [],
            "nextPageToken": b.get("nextPageToken")}


@span("googleSheets.get_spreadsheet", kind="getter")  # noqa: F821 - guest global
def get_spreadsheet(spreadsheet_id):
    """Metadata only: title + tabs with grid sizes → {ok, spreadsheetId, title, sheets}.

    sheets are {title, sheetId, rows, cols} — title/rows/cols are
    derived flattenings of properties.title and
    gridProperties.rowCount/columnCount (fields-masked request). No
    cell values — cheap, safe to call before reading ranges."""
    if not spreadsheet_id or not isinstance(spreadsheet_id, str):
        return {"ok": False, "error": "spreadsheet_id is required"}
    r = _get(_SHEETS_BASE + "/" + _quote(spreadsheet_id) + _qs({
        "fields": "spreadsheetId,properties.title,"
                  "sheets.properties(title,sheetId,gridProperties)"}))
    if not r["ok"]:
        return r
    b = r["body"] or {}
    sheets = []
    for raw in b.get("sheets") or []:
        p = (raw or {}).get("properties") or {}
        g = p.get("gridProperties") or {}
        sheets.append({"title": p.get("title") or "", "sheetId": p.get("sheetId"),
                       "rows": g.get("rowCount") or 0, "cols": g.get("columnCount") or 0})
    return {"ok": True, "spreadsheetId": b.get("spreadsheetId") or spreadsheet_id,
            "title": (b.get("properties") or {}).get("title") or "",
            "sheets": sheets}


@span("googleSheets.get_values", kind="getter")  # noqa: F821 - guest global
def get_values(spreadsheet_id, range, unformatted=False):
    """One A1 range as a 2-D array → {ok, range, values}.

    range is A1 notation ("Sheet1!A1:D50", or "Sheet1" for the whole
    tab); range and values return verbatim from the API. Trailing
    empty cells/rows are dropped by Google, so rows are ragged.
    unformatted=True → UNFORMATTED_VALUE (raw numbers/dates, for
    computing); default is FORMATTED_VALUE (locale display strings)."""
    if not spreadsheet_id or not isinstance(spreadsheet_id, str):
        return {"ok": False, "error": "spreadsheet_id is required"}
    if not range or not isinstance(range, str):
        return {"ok": False,
                "error": 'range is required (A1 notation, e.g. "Sheet1!A1:D50")'}
    r = _get(_SHEETS_BASE + "/" + _quote(spreadsheet_id) + "/values/" + _quote(range)
             + _qs({"valueRenderOption":
                    "UNFORMATTED_VALUE" if unformatted else "FORMATTED_VALUE",
                    "dateTimeRenderOption": "FORMATTED_STRING"}))
    if not r["ok"]:
        return r
    b = r["body"] or {}
    return {"ok": True, "range": b.get("range") or range, "values": b.get("values") or []}


@span("googleSheets.batch_get_values", kind="getter")  # noqa: F821 - guest global
def batch_get_values(spreadsheet_id, ranges, unformatted=False):
    """Several A1 ranges in one call → {ok, valueRanges: [{range, values}]}.

    Cheaper than N get_values — one request against the per-user read
    quota. ranges is a non-empty list of A1 strings, capped at 25.
    valueRanges keeps the upstream name; each entry trims to
    {range, values}. unformatted as in get_values."""
    if not spreadsheet_id or not isinstance(spreadsheet_id, str):
        return {"ok": False, "error": "spreadsheet_id is required"}
    if not isinstance(ranges, list) or not ranges:
        return {"ok": False, "error": "ranges is required (non-empty list of A1 ranges)"}
    if len(ranges) > _MAX_RANGES:
        return {"ok": False,
                "error": f"too many ranges ({len(ranges)}) — cap is {_MAX_RANGES}"}
    r = _get(_SHEETS_BASE + "/" + _quote(spreadsheet_id) + "/values:batchGet"
             + _qs({"ranges": ranges,
                    "valueRenderOption":
                    "UNFORMATTED_VALUE" if unformatted else "FORMATTED_VALUE",
                    "dateTimeRenderOption": "FORMATTED_STRING"}))
    if not r["ok"]:
        return r
    out = [{"range": (vr or {}).get("range") or "", "values": (vr or {}).get("values") or []}
           for vr in (r["body"] or {}).get("valueRanges") or []]
    return {"ok": True, "valueRanges": out}


@span("googleSheets.read_sheet_as_objects", kind="getter")  # noqa: F821 - guest global
def read_sheet_as_objects(spreadsheet_id, range, header_row=None, unformatted=False):
    """A range as header-mapped records → {ok, range, headers, records}.

    headers/records are derived: header_row (1-based WITHIN the
    returned range, default 1) becomes the keys (empty cells fall back
    to col1, col2, …), rows after it become records. Short rows pad
    against the header length (Google drops trailing empties) so keys
    never misalign. unformatted as in get_values."""
    got = get_values(spreadsheet_id, range, unformatted=unformatted)
    if not got["ok"]:
        return got
    values = got.get("values") or []
    header_idx = 0
    if isinstance(header_row, (int, float)) and header_row > 0:
        header_idx = int(header_row) - 1
    if len(values) <= header_idx:
        return {"ok": True, "range": got["range"], "headers": [], "records": []}
    headers = []
    for i, cell in enumerate(values[header_idx] or []):
        key = "" if cell is None else str(cell)
        headers.append(key or f"col{i + 1}")
    records = []
    for row in values[header_idx + 1:]:
        row = row or []
        records.append({key: row[c] if c < len(row) else ""
                        for c, key in enumerate(headers)})
    return {"ok": True, "range": got["range"], "headers": headers, "records": records}


def main(args):
    args = args if isinstance(args, dict) else {}
    return list_spreadsheets(page_size=args.get("page_size"),
                             page_token=args.get("page_token"),
                             query=args.get("query"))
